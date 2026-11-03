"""Cross-process, same-user coordination for the local call desk.

No browser automation happens here. UI observations are operator assertions,
recorded separately from locally verified worker state.
"""
from __future__ import annotations
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from zoneinfo import ZoneInfo
from audio_policy import (KEEP_VIRTUAL,VIRTUAL_ROUTES,audio_cleanup_policy,
                          lease_released,cleanup_instruction)

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo('America/Chicago')

class CallError(ValueError):
    pass

def now():
    return dt.datetime.now(TZ).isoformat()

def write_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=2, allow_nan=False)
        f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)

def read_json(path, default=None):
    try: return json.loads(Path(path).read_text())
    except FileNotFoundError: return default

def text_value(value, name, maximum=4000, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > maximum:
        raise CallError(f'{name} must be a string of 1–{maximum} characters' if not empty else f'{name} is too long')
    if '\x00' in value: raise CallError(f'{name} contains a NUL byte')
    return value.strip()

class CallService:
    def __init__(self, home=None, config=None, spawn=None):
        self.home = Path(home or os.environ.get('ELLE_CALL_STATE_DIR', ROOT / 'state')).resolve()
        self.config_path = Path(config or os.environ.get('ELLE_CALL_CONFIG', ROOT / 'config.json')).resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.chmod(0o700)
        self.spawn = spawn or subprocess.Popen
        self.children = {}

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.home / 'registry.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            state = read_json(self.home / 'registry.json', {'version':1,'active':None,'calls':{},'keys':{}})
            yield state
            write_json(self.home / 'registry.json', state)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)

    def config(self):
        value = read_json(self.config_path)
        if not value: raise CallError('Local call-service config is missing')
        return value

    def directory(self, call_id):
        if not re.fullmatch(r'call_[0-9a-f]{32}', call_id): raise CallError('Invalid call ID')
        return self.home / call_id

    def get(self, state, call_id, owner=None):
        self.directory(call_id)
        if call_id not in state['calls']: raise CallError('Unknown call ID')
        call = state['calls'][call_id]
        if owner is not None and owner != call['owner']: raise CallError('Owner differs; use call_takeover after an explicit handoff')
        return call

    def worker_active(self, call_id):
        directory = self.directory(call_id)
        fd = os.open(directory / 'worker.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally: os.close(fd)

    def events(self, call_id):
        p = self.directory(call_id) / 'events.jsonl'
        if not p.exists(): return []
        events = []
        for line in p.read_text().splitlines():
            try: events.append(json.loads(line))
            except json.JSONDecodeError: continue  # last line may be in flight
        return events

    def refresh(self, call):
        call_id = call['call_id']; directory = self.directory(call_id)
        result = read_json(directory / 'result.json')
        active = self.worker_active(call_id)
        events = self.events(call_id)
        kinds = {e.get('kind') for e in events}
        call['worker_active'] = active
        call['audio_connected'] = active and 'audio_connected' in kinds
        call['provider_connected'] = active and 'provider_connected' in kinds
        terminal = bool(kinds & {'disconnect','error','worker_stopping','worker_finished'})
        call['ready_to_dial'] = call['audio_connected'] and call['provider_connected'] and not terminal and not result and not call.get('stop_requested_at') and not call.get('finished_at')
        if result is not None:
            call['worker_result'] = {k:v for k,v in result.items() if k not in ('events','transcript','prompt')}
            bill = result.get('billing') or {}
            cost = bill.get('cost_usd')
            if not call.get('billing_reconciliations') and bill.get('status') in {'final','not_started'} and isinstance(cost, (float,int)) and math.isfinite(cost) and cost >= 0:
                call['actual_cost_usd'] = cost
            if not active and not call.get('finished_at'): call['phase'] = 'cleanup_required'
        elif call.get('start_intent_at') and not active and not call.get('finished_at'):
            # A worker may not have reached its lock yet. Never spawn another one.
            call['phase'] = 'starting_or_uncertain'
        if active and not call.get('finished_at'):
            call['phase'] = 'stopping' if call.get('stop_requested_at') else ('audio_ready' if call['ready_to_dial'] else 'starting')
        child = self.children.get(call_id)
        if child and child.poll() is not None: self.children.pop(call_id,None)
        return call

    def summary(self, call):
        fields={'call_id','owner','local_date','created_at','phase','phone_state','reservation_usd','actual_cost_usd','worker_active','audio_connected','provider_connected','ready_to_dial','start_intent_at','stop_requested_at','dial_intent_at','answer_intent_at','direction','finished_at'}
        return {k:v for k,v in call.items() if k in fields} | {
            'recipient_masked': 'unknown' if call['request']['recipient']=='unknown' else '…' + call['request']['recipient'][-4:],
            'voice_name': call['request']['voice_name'],
            'observation_count': len(call['observations']),
            'browser_control': 'client_agent_computer_use',
            'phone_state_source': 'operator_reported',
            'worker_result_status': (call.get('worker_result') or {}).get('status'),
            'conversation_id': (call.get('worker_result') or {}).get('conversation_id'),
            'audio_cleanup_policy':audio_cleanup_policy(call['request']),
            'audio_cleanup_outcome':call.get('audio_cleanup_outcome'),
        }

    def budget_total(self, state, date):
        return sum(c.get('actual_cost_usd', c['reservation_usd']) for c in state['calls'].values() if c['local_date']==date)

    def prepare(self, owner, request_key, recipient, brief, context, max_seconds, authorization, other_spend_today_usd, *, direction='outgoing', ring_id=None,routing_policy=None):
        owner = text_value(owner,'owner',120); key=text_value(request_key,'request_key',200)
        if direction not in {'incoming','outgoing'}: raise CallError('Invalid direction')
        recipient = 'unknown' if direction=='incoming' and recipient is None else text_value(recipient,'recipient',32)
        if direction=='incoming': ring_id=text_value(ring_id,'ring_id',200)
        if (direction!='incoming' or recipient!='unknown') and not re.fullmatch(r'\+[1-9][0-9]{7,14}', recipient): raise CallError('Use an explicitly authorized E.164 number, such as +15555550123')
        brief=text_value(brief,'brief',6000); context=text_value(context,'context',10000,empty=True)
        authorization=text_value(authorization,'authorization',3000)
        if isinstance(max_seconds,bool) or not isinstance(max_seconds,int) or not 1<=max_seconds<=300: raise CallError('max_seconds must be 1–300')
        if isinstance(other_spend_today_usd,bool) or not isinstance(other_spend_today_usd,(int,float)) or not math.isfinite(other_spend_today_usd) or other_spend_today_usd<0: raise CallError('other_spend_today_usd must be a finite nonnegative known total, including pending commitments outside this service')
        config=self.config()
        policy=audio_cleanup_policy(config if routing_policy is None else {'audio_cleanup_policy':routing_policy})
        request=dict(owner=owner,recipient=recipient,brief=brief,context=context,max_seconds=max_seconds,authorization=authorization,other_spend_today_usd=other_spend_today_usd,voice_name=config.get('voice_name','Elle'),direction=direction,ring_id=ring_id,audio_cleanup_policy=policy)
        digest=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
        key_digest=hashlib.sha256((owner+'\0'+key).encode()).hexdigest()
        with self.locked() as state:
            if key_digest in state['keys']:
                call=state['calls'][state['keys'][key_digest]]
                if call['request_hash']!=digest: raise CallError('Idempotency key already used with different inputs')
                return self.summary(self.refresh(call)) | {'replayed':True}
            from browser_operation import require_browser_available
            require_browser_available(state)
            if direction=='outgoing':
                incoming=read_json(self.home/'incoming-settings.json',{})
                lease=read_json(self.home/'incoming-audio-lease.json',{})
                if incoming.get('enabled') or (lease and not lease_released(lease)):
                    raise CallError('Incoming arm owns the audio routes; disarm and verify cleanup before an outgoing call')
            if state['active']: raise CallError('Call desk is reserved by '+state['active']+'; inspect and finish cleanup before another call')
            local_date=dt.datetime.now(TZ).date().isoformat()
            reserve=round(max_seconds/60.0*config.get('reserve_per_minute_usd',1.0)+.25,4)
            total=self.budget_total(state,local_date)+other_spend_today_usd+reserve
            if total>100: raise CallError(f'Known spend plus reservation would be ${total:.2f}, above the $100 daily authorization')
            call_id='call_'+uuid.uuid4().hex
            directory=self.directory(call_id); directory.mkdir(mode=0o700)
            request.update(call_id=call_id,estimated_cost_usd=reserve)
            write_json(directory/'request.json',request)
            call={'call_id':call_id,'owner':owner,'request':request,'request_hash':digest,'local_date':local_date,'created_at':now(),'phase':'prepared','direction':direction,'phone_state':'ringing' if direction=='incoming' else 'not_dialed','observations':[],'reservation_usd':reserve,'worker_active':False,'audio_connected':False,'provider_connected':False,'ready_to_dial':False}
            state['calls'][call_id]=call;state['keys'][key_digest]=call_id;state['active']=call_id
            return self.summary(call)|{'replayed':False,'budget_basis':'Caller-reported outside spend plus service actuals/unresolved reservations; reservation is conservative, not a provider quote'}

    def prepare_incoming(self,owner,request_key,caller_number,brief,context,max_seconds,authorization,other_spend_today_usd,ring_id,*,routing_policy=None):
        return self.prepare(owner,request_key,caller_number,brief,context,max_seconds,authorization,other_spend_today_usd,direction='incoming',ring_id=ring_id,routing_policy=routing_policy)

    def start(self,call_id,owner):
        with self.locked() as state:
            call=self.get(state,call_id,owner);self.refresh(call)
            if call.get('start_intent_at') or call.get('finished_at'):
                return self.summary(call)|{'replayed':True,'new_worker_started':False}
            if state['active']!=call_id: raise CallError('Call does not own the desk')
            if call.get('stop_requested_at'): raise CallError('This request was stopped; finish it and prepare a fresh request')
            if call['local_date'] != dt.datetime.now(TZ).date().isoformat(): raise CallError('Reservation is from a previous day; finish this unused request and prepare with a fresh budget')
            cfg=self.config()
            if not cfg.get('provider_overrides_verified_at'): raise CallError('Provider prompt/first-message override setup has not been verified')
            call['start_intent_at']=now();call['phase']='starting'
            # Commit intent BEFORE spawning. An interruption here remains uncertain,
            # never an invitation to launch a second provider session.
            write_json(self.home/'registry.json',state)
            log=self.directory(call_id)/'worker.log'
            fd=os.open(log,os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
            try:
                child=self.spawn([str(ROOT/'.venv/bin/python'),str(ROOT/'worker.py'),'--call-dir',str(self.directory(call_id)),'--config',str(self.config_path)],stdin=subprocess.DEVNULL,stdout=fd,stderr=fd,start_new_session=True,close_fds=True)
                self.children[call_id]=child
                call['spawned_pid']=child.pid
            except Exception as exc:
                call['phase']='cleanup_required';call['spawn_error_type']=type(exc).__name__
                call['actual_cost_usd']=0
                write_json(self.directory(call_id)/'result.json',{'status':'spawn_failed','billing':{'status':'not_started','cost_usd':0}})
            finally: os.close(fd)
            return self.summary(call)|{'replayed':False,'new_worker_started':bool(call.get('spawned_pid'))}

    def status(self,call_id):
        with self.locked() as state: return self.summary(self.refresh(self.get(state,call_id)))

    def dial_intent(self,call_id,owner,evidence):
        evidence=text_value(evidence,'evidence',5000)
        with self.locked() as state:
            call=self.get(state,call_id,owner);self.refresh(call)
            if call.get('direction')=='incoming': raise CallError('Use answer_intent for an incoming call')
            if call.get('dial_intent_at'): return self.summary(call)|{'dispatch_allowed':False,'reason':'Dial intent already issued. Inspect browser state; never repeat the click after an uncertain response.'}
            if not call['ready_to_dial']: raise CallError('Wait for both audio_connected and provider_connected; stopped/finished calls cannot dial')
            call['dial_intent_at']=now();call['dial_evidence']=evidence;call['phone_state']='dial_intent_recorded'
            return self.summary(call)|{'dispatch_allowed':True,'instruction':'One authorized UI dial attempt only. Persist browser outcome through call_observe; never retry on timeout.'}

    def answer_intent(self,call_id,owner,evidence):
        evidence=text_value(evidence,'evidence',5000)
        with self.locked() as state:
            call=self.get(state,call_id,owner);self.refresh(call)
            if call.get('direction')!='incoming': raise CallError('Answer requires an incoming call record')
            if call.get('answer_intent_at'): return self.summary(call)|{'dispatch_allowed':False}
            if not call['ready_to_dial']: raise CallError('Incoming voice worker is not ready')
            call['answer_intent_at']=now();call['answer_evidence']=evidence
            return self.summary(call)|{'dispatch_allowed':True}

    def observe(self,call_id,owner,phone_state,evidence):
        evidence=text_value(evidence,'evidence',5000)
        if phone_state not in {'dialing','ringing','answered','voicemail','busy','ended','idle','uncertain'}: raise CallError('Invalid phone_state')
        with self.locked() as state:
            call=self.get(state,call_id,owner)
            if call.get('finished_at'): raise CallError('Call is finished')
            if phone_state not in {'idle','uncertain'} and not (call.get('dial_intent_at') or call.get('answer_intent_at') or (call.get('direction')=='incoming' and phone_state=='ringing')): raise CallError('No dial/answer intent recorded')
            call['phone_state']=phone_state
            call['observations'].append({'at':now(),'phone_state':phone_state,'evidence':evidence,'source':'operator_reported'})
            if phone_state=='answered' and call.get('direction')=='incoming':
                if call.get('stop_requested_at'): raise CallError('Cannot open incoming audio after stop')
                write_json(self.directory(call_id)/'ANSWERED',{'call_id':call_id,'observed_at':now()})
            return self.summary(self.refresh(call))

    def stop(self,call_id):
        with self.locked() as state:
            call=self.get(state,call_id)
            if not call.get('finished_at'):
                call.setdefault('stop_requested_at',now())
                write_json(self.directory(call_id)/'STOP',{'requested_at':call['stop_requested_at']})
            return self.summary(self.refresh(call))|{'instruction':'Voice stop requested; this does not hang up Google Voice. End the browser call. '+cleanup_instruction(audio_cleanup_policy(call['request']))}

    def finish(self,call_id,owner,hangup_evidence,restoration_evidence,outcome):
        for key,value in [('hangup_evidence',hangup_evidence),('restoration_evidence',restoration_evidence),('outcome',outcome)]: text_value(value,key,5000)
        with self.locked() as state:
            call=self.get(state,call_id,owner);self.refresh(call)
            if call.get('finished_at'): return self.summary(call)|{'replayed':True}
            if call['worker_active']: raise CallError('Worker is still running; stop it and wait before releasing the desk')
            if call.get('start_intent_at') and not (self.directory(call_id)/'result.json').exists():
                raise CallError('Worker outcome is uncertain. Use call_recover after inspecting processes and provider state; no automatic release or redial')
            result = read_json(self.directory(call_id)/'result.json', {})
            if result.get('provider_session_attempted') and not call.get('recovery_evidence'):
                if not all(result.get(k) is True for k in ('audio_stopped','provider_stop_completed','provider_thread_joined')):
                    raise CallError('Provider/audio shutdown is uncertain; independently inspect termination, then call_recover')
            if not call.get('start_intent_at'): call['actual_cost_usd']=0
            call.update(finished_at=now(),phase='finished',phone_state='idle',hangup_evidence=hangup_evidence,restoration_evidence=restoration_evidence,outcome=outcome)
            policy=audio_cleanup_policy(call['request'])
            call.update(audio_cleanup_evidence=restoration_evidence,
                        audio_cleanup_outcome='persistent_virtual_defaults' if policy==KEEP_VIRTUAL else 'original_routes_restored',
                        routes_restored=policy!=KEEP_VIRTUAL)
            if state['active']==call_id: state['active']=None
            return self.summary(call)|{'replayed':False,'cleanup_source':'operator_reported_browser_and_routes; locally_verified_worker_stopped'}

    def recover(self,call_id,owner,evidence):
        evidence=text_value(evidence,'evidence',5000)
        with self.locked() as state:
            call=self.get(state,call_id,owner);self.refresh(call)
            if call['worker_active']: raise CallError('Worker is still active')
            if not call.get('start_intent_at') or call.get('finished_at'): raise CallError('Recovery only applies to an uncertain started call')
            # Local process absence alone cannot prove provider termination or cost.
            # STOP also prevents a delayed worker from starting a session.
            write_json(self.directory(call_id)/'STOP',{'requested_at':now()})
            call['recovery_evidence']={'at':now(),'source':'operator_reported','text':evidence}
            if not (self.directory(call_id)/'result.json').exists():
                write_json(self.directory(call_id)/'result.json',{'status':'recovered_uncertain','billing':{'status':'unknown','cost_usd':None}})
            return self.summary(self.refresh(call))

    def takeover(self,call_id,new_owner,handoff_evidence):
        new_owner=text_value(new_owner,'new_owner',120);text_value(handoff_evidence,'handoff_evidence',3000)
        with self.locked() as state:
            call=self.get(state,call_id)
            call.setdefault('handoffs',[]).append({'at':now(),'from':call['owner'],'to':new_owner,'evidence':handoff_evidence})
            call['owner']=new_owner
            return self.summary(self.refresh(call))

    def reconcile(self,call_id,cost_usd,evidence):
        evidence=text_value(evidence,'evidence',5000)
        if isinstance(cost_usd,bool) or not isinstance(cost_usd,(float,int)) or not math.isfinite(cost_usd) or cost_usd<0: raise CallError('Final cost must be finite and nonnegative')
        with self.locked() as state:
            call=self.get(state,call_id);self.refresh(call)
            if call['worker_active']: raise CallError('Wait for the worker to stop before reconciling final cost')
            if not call.get('finished_at') and not (self.directory(call_id)/'result.json').exists(): raise CallError('No completed worker result or finished request to reconcile')
            call['actual_cost_usd']=cost_usd
            call.setdefault('billing_reconciliations',[]).append({'at':now(),'cost_usd':cost_usd,'evidence':evidence,'source':'operator_verified_provider_receipt'})
            return self.summary(call)

    def report(self,call_id):
        with self.locked() as state:
            call=self.get(state,call_id); self.refresh(call)
            return self.summary(call)|{'brief':call['request']['brief'],'observations':call['observations'],'outcome':call.get('outcome'),'handoffs':call.get('handoffs',[]),'recovery_evidence':call.get('recovery_evidence'),'hangup_evidence':call.get('hangup_evidence'),'restoration_evidence':call.get('restoration_evidence'),'audio_cleanup_evidence':call.get('audio_cleanup_evidence'),'routes_restored':call.get('routes_restored'),'billing_reconciliations':call.get('billing_reconciliations',[]),'events':self.events(call_id),'worker_result':call.get('worker_result'),'evidence_note':'Browser states and configured routing outcomes are operator reports. Transcripts are recognition hypotheses. A spoken claim of booking/completion needs separate confirmation.'}

    def preflight(self):
        cfg=self.config();devices=[];errors=[]
        try:
            import sounddevice as sd
            available=sd.query_devices()
            for direction,key in [('input','input_device'),('output','output_device')]:
                matches=[d for d in available if d['name']==cfg[key] and d['max_'+direction+'_channels']>0]
                devices.append({'name':cfg[key],'direction':direction,'matches':len(matches)})
                if len(matches)!=1: errors.append('Expected one '+direction+' device '+cfg[key])
        except Exception as exc: errors.append(type(exc).__name__)
        with self.locked() as state:
            active=self.summary(self.refresh(state['calls'][state['active']])) if state['active'] else None
            browser_operation=state.get('browser_operation')
            browser_summary=({k:browser_operation.get(k) for k in ('token','owner','kind','status','created_at')}
                             if isinstance(browser_operation,dict) else browser_operation)
            date=dt.datetime.now(TZ).date().isoformat()
            subtotal=self.budget_total(state,date)
        policy=audio_cleanup_policy(cfg)
        return {'service':'shared-call-desk','version':'0.2.0','devices':devices,'errors':errors,'active_call':active,'browser_operation':browser_summary,'local_date':date,'service_committed_usd':subtotal,'provider_setup_verified_at':cfg.get('provider_overrides_verified_at'),'audio_cleanup_policy':policy,'virtual_browser_routes':dict(VIRTUAL_ROUTES),'audio_cleanup_instruction':cleanup_instruction(policy),'credentials':'1Password authorization is checked only when starting; may require Touch ID','browser':'Client agent must verify signed-in Google Voice and operate UI; no browser API dial is implemented','same_user_trust':True,'remote_access':False,'live_session_started':False}
