"""MCP controls for a bounded, same-Mac incoming watcher."""
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from zoneinfo import ZoneInfo
from service import CallError,ROOT,read_json,write_json
from incoming import validate_settings, valid_snapshot, restore_arm_audio
from google_voice_ui import routes_equivalent
from audio_policy import audio_cleanup_policy,lease_released
from browser_operation import require_browser_available

@contextlib.contextmanager
def lock(home):
    fd=os.open(home/'incoming-control.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX);yield
    finally:os.close(fd)

@contextlib.contextmanager
def cancellation_lock(home):
    """Short lock: receipt/launch ordering only, never held across native UI."""
    fd=os.open(home/'incoming-disarm.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX);yield
    finally:os.close(fd)

def cancellation_token(home):
    return read_json(home/'incoming-disarm-request.json',{}).get('token')

def stop_matching_incoming(service,settings):
    """Request voice stop without waiting for the long arm/UI control lock."""
    current=read_json(service.home/'incoming-state.json',{}).get('current') or {}
    call_id=current.get('call_id')
    with service.locked() as registry:
        call=registry.get('calls',{}).get(call_id,{})
        can_stop=(call_id is not None and registry.get('active')==call_id and
                  call.get('direction')=='incoming' and not call.get('finished_at') and
                  call.get('owner')==current.get('owner')==settings.get('owner') and
                  same_arm(current.get('armed_id'),settings.get('armed_id')))
    if can_stop:
        service.stop(call_id)
        return call_id
    return None

def watcher_running(home):
    fd=os.open(home/'incoming.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return True
        return False
    finally:os.close(fd)

def status(service):
    policy=read_json(service.home/'incoming-settings.json',{})
    receipt=read_json(service.home/'incoming-status.json',{})
    running=watcher_running(service.home)
    matched=bool(policy.get('armed_id')) and same_arm(policy.get('armed_id'),receipt.get('armed_id'))
    current=receipt if matched else {}
    phase=current.get('phase','launch_pending' if policy.get('enabled') else 'not_started')
    lease=read_json(service.home/'incoming-audio-lease.json',{})
    routing_policy=audio_cleanup_policy(lease if lease else service.config())
    if not running and not policy.get('enabled') and lease and not lease_released(lease):phase='cleanup_required'
    return {'running':running,'enabled':policy.get('enabled',False),'armed_id':policy.get('armed_id'),'receipt_matches_arm':matched,'phase':phase,'status_age_seconds':max(0,time.time()-current.get('updated_at',time.time())) if matched else None,'expires_at':policy.get('expires_at'),'allowed_callers':['…'+n[-4:] for n in policy.get('allowed_numbers',[])],'max_calls':policy.get('max_calls'),'call_id':current.get('call_id'),'reason':current.get('reason'),'error_type':current.get('error_type'),'audio_lease_status':lease.get('status'),'audio_cleanup_policy':routing_policy,'audio_cleanup_evidence':lease.get('audio_cleanup_evidence'),'controls_verified':bool(read_json(service.home/'google-voice-ui.json',{}).get('incoming_controls_verified'))}

def archive_lease(home,lease):
    """Retain each replaced lease, including its original physical routes."""
    if not lease:return
    directory=home/'audio-route-history';directory.mkdir(mode=0o700,exist_ok=True)
    try:key=str(uuid.UUID(lease.get('armed_id')))
    except (ValueError,TypeError,AttributeError):key=hashlib.sha256(json.dumps(lease,sort_keys=True).encode()).hexdigest()
    path=directory/(key+'.json')
    if not path.exists():write_json(path,lease)

def same_arm(left,right):
    try:return uuid.UUID(left)==uuid.UUID(right)
    except (ValueError,TypeError,AttributeError):return False

def cleanup_preparation(service,adapter,armed_id):
    """Release a proven read-only attempt, otherwise restore recorded routes.

    Only an explicit, persisted mutation_started=False enables this path.
    Historical or uncertain leases retain the stricter restoration requirement.
    """
    path=service.home/'incoming-audio-lease.json'
    lease=read_json(path,{})
    if not same_arm(lease.get('armed_id'),armed_id) or lease.get('mutation_started') is not False:
        return restore_arm_audio(service,adapter,armed_id)
    if lease_released(lease):
        return {'restored':True,'reason':'Read-only preparation already released'}
    with service.locked() as registry:
        if registry.get('active'):
            return {'restored':False,'reason':'Call desk remains reserved; read-only lease retained'}
    try:
        close=getattr(adapter,'audio_close',None)
        if close:close()
        snap=adapter.snapshot()
        if not valid_snapshot(snap,time.time(),lease.get('identity')) or snap['state']!='idle':
            raise CallError('Read-only preparation did not return to the verified idle window')
        with service.locked() as registry:
            if registry.get('active'):
                raise CallError('Call desk became reserved during read-only cleanup')
            lease.update(status='restored',restored_at=time.time(),release_kind='read_only_preparation',
                         restoration_evidence='No route selection was dispatched; audio panel closed and the same enrolled window verified idle')
            write_json(path,lease)
        return {'restored':True,'reason':'Read-only preparation closed; unchanged routes released'}
    except Exception as exc:
        lease.update(status='cleanup_required',error_type=type(exc).__name__)
        write_json(path,lease)
        return {'restored':False,'reason':'Read-only preparation cleanup not verified','error_type':type(exc).__name__}

def arm(service,owner,allowed_numbers,brief,authorization,other_spend_today_usd,context='',minutes=10,max_calls=1,max_seconds=180):
    # A prior Stop does not cancel a new explicit arm. Any newer receipt does.
    with cancellation_lock(service.home):
        initial_cancel_token=cancellation_token(service.home)
    if not authorization or not isinstance(authorization,str):raise CallError('Incoming answering requires the user-authorized caller scope and purpose')
    if isinstance(minutes,bool) or not isinstance(minutes,int) or not 1<=minutes<=60:raise CallError('minutes must be 1–60')
    if isinstance(max_calls,bool) or not isinstance(max_calls,int) or max_calls!=1:raise CallError('This incoming version supports one call per arm; max_calls must be 1')
    if not isinstance(allowed_numbers,list) or not allowed_numbers:raise CallError('At least one explicitly authorized caller number is required')
    local=dt.datetime.now(ZoneInfo('America/Chicago'))
    midnight=dt.datetime.combine(local.date()+dt.timedelta(days=1),dt.time.min,tzinfo=local.tzinfo)
    expires=min(time.time()+minutes*60,midnight.timestamp())
    routing_policy=audio_cleanup_policy(service.config())
    settings={'enabled':True,'armed_id':uuid.uuid4().hex,'mode':'allowlist','owner':owner,'allowed_numbers':allowed_numbers,'brief':brief,'context':context,'authorization':authorization,'other_spend_today_usd':other_spend_today_usd,'max_seconds':max_seconds,'max_calls':max_calls,'expires_at':expires,'poll_seconds':1,'readiness_seconds':20,'audio_cleanup_policy':routing_policy}
    settings['armed_id']=validate_settings(settings)['armed_id']
    with lock(service.home):
        if cancellation_token(service.home)!=initial_cancel_token:
            raise CallError('Incoming arm canceled by a newer Stop request')
        if watcher_running(service.home):raise CallError('Incoming watcher already running; disarm and wait before changing caller scope')
        previous=read_json(service.home/'incoming-settings.json',{})
        if previous.get('enabled'):raise CallError('Prior incoming arm remains enabled; explicitly disarm before re-arming, even if its status is old')
        if read_json(service.home/'incoming-state.json',{}).get('current'):raise CallError('Previous incoming call requires cleanup; do not re-arm')
        old_lease=read_json(service.home/'incoming-audio-lease.json',{})
        if old_lease and not lease_released(old_lease):raise CallError('Previous incoming audio lease requires verified cleanup before re-arming')
        cfg=read_json(service.home/'google-voice-ui.json',{})
        if not cfg.get('incoming_controls_verified'):raise CallError('Actual incoming Answer/Decline/End controls have not been verified in this browser yet')
        from google_voice_ui import GoogleVoiceUI
        adapter=GoogleVoiceUI(service.home)
        # Explicit arm may need to bring this exact enrolled window out of a
        # different Space before Chrome publishes its accessibility tree.
        # Keep new reservations excluded during this bounded focus operation;
        # an existing call must never have its window disturbed by arm setup.
        with service.locked() as registry:
            require_browser_available(registry)
            if registry['active']:raise CallError('Call desk is already reserved')
            focus=getattr(adapter,'focus_enrolled',None)
            if focus:focus()
        if cancellation_token(service.home)!=initial_cancel_token:
            raise CallError('Incoming arm canceled by a newer Stop request')
        snap=adapter.snapshot()
        if not valid_snapshot(snap,time.time()) or snap.get('state')!='idle':raise CallError('Enrolled Google Voice window must be verified idle before arming')
        if cancellation_token(service.home)!=initial_cancel_token:
            raise CallError('Incoming arm canceled by a newer Stop request')
        with service.locked() as registry:
            require_browser_available(registry)
            if registry['active']:raise CallError('Call desk is already reserved')
            reserve=max_calls*(max_seconds/60+.25)
            if service.budget_total(registry,local.date().isoformat())+other_spend_today_usd+reserve>100:raise CallError('Potential incoming session charges exceed current daily budget')
            lease={'armed_id':settings['armed_id'],'identity':snap['identity'],'status':'reading_originals',
                   'mutation_started':False,'claimed_at':time.time(),'original_routes':None,
                   'audio_cleanup_policy':routing_policy}
            # Publish the lease while holding the same lock outgoing prepare
            # uses. No native command or provider call occurs under this lock.
            archive_lease(service.home,old_lease)
            write_json(service.home/'incoming-audio-lease.json',lease)
            preparing=dict(settings,enabled=False)
            write_json(service.home/'incoming-settings.json',preparing)
        try:
            original=adapter.routes()
            if not isinstance(original,dict) or not original:raise CallError('Original audio routes could not be read')
            lease['original_routes']=original;write_json(service.home/'incoming-audio-lease.json',lease)
            if cancellation_token(service.home)!=initial_cancel_token:
                raise CallError('Incoming arm canceled by a newer Stop request')
            # Persist intent before the first possible route selection. From
            # this point cleanup must restore originals, including on failure.
            lease.update(status='preparing',mutation_started=True)
            write_json(service.home/'incoming-audio-lease.json',lease)
            prepared=adapter.prepare_audio()
            after=adapter.snapshot()
            if (not isinstance(prepared,dict) or prepared.get('verified') is not True
                    or not routes_equivalent(prepared.get('original_routes'),original)
                    or not valid_snapshot(after,time.time(),lease['identity']) or after['state']!='idle'):
                raise CallError('Pre-armed audio preparation did not verify on the idle enrolled window')
            lease.update(status='prepared',prepared_at=time.time(),preparation_evidence=prepared.get('evidence','Virtual audio routes verified'))
            write_json(service.home/'incoming-audio-lease.json',lease)
        except Exception:
            cleanup_preparation(service,adapter,settings['armed_id'])
            raise
        try:
            # Serialize the last token check with receipt publication. Stop
            # either cancels before launch, or sees and targets this exact arm.
            with cancellation_lock(service.home):
                if cancellation_token(service.home)!=initial_cancel_token:
                    raise CallError('Incoming arm canceled by a newer Stop request')
                write_json(service.home/'incoming-settings.json',settings)
                write_json(service.home/'incoming-launch.json',{'armed_id':settings['armed_id'],'at':time.time(),'state':'launch_intent'})
                fd=os.open(service.home/'incoming-watcher.log',os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
                try:
                    child=subprocess.Popen([str(ROOT/'.venv/bin/python'),str(ROOT/'incoming.py'),'--state-dir',str(service.home),'--config',str(service.config_path),'--armed-id',settings['armed_id']],stdin=subprocess.DEVNULL,stdout=fd,stderr=fd,start_new_session=True,close_fds=True)
                finally:os.close(fd)
                write_json(service.home/'incoming-launch.json',{'armed_id':settings['armed_id'],'at':time.time(),'state':'spawned','pid':child.pid})
        except Exception:
            settings['enabled']=False;write_json(service.home/'incoming-settings.json',settings)
            cleanup_preparation(service,adapter,settings['armed_id'])
            raise
    return status(service)|{'started_pid':child.pid,'note':'Audio routes are preconfigured for this one-call window. Voice billing starts only for an eligible ring. Clean exit verifies the configured audio policy; Google Voice and 1Password must remain available.'}

def disarm(service):
    # Publish cancellation before waiting for an arm's slow native UI work.
    # Capture the target under the short launch lock so an old delayed Stop
    # cannot disable a new explicit arm that starts after this receipt.
    with cancellation_lock(service.home):
        target_settings=read_json(service.home/'incoming-settings.json',{})
        request={'token':str(uuid.uuid4()),'requested_at':time.time(),
                 'target_armed_id':target_settings.get('armed_id')}
        write_json(service.home/'incoming-disarm-request.json',request)
    stopped_call=stop_matching_incoming(service,target_settings)
    restoration=None
    with lock(service.home):
        settings=read_json(service.home/'incoming-settings.json',{})
        if not same_arm(settings.get('armed_id'),request['target_armed_id']):
            return status(service)|{'stop_requested':True,'voice_stop_call_id':stopped_call,
                'idle_arm_restoration':None,'cancel_token':request['token'],
                'note':'Stop receipt applied to the prior/in-progress arm; a later explicit arm was not changed.'}
        if settings:settings['enabled']=False;write_json(service.home/'incoming-settings.json',settings)
        current=read_json(service.home/'incoming-state.json',{}).get('current') or {}
        stopped_call=stop_matching_incoming(service,settings) or stopped_call
        if not watcher_running(service.home) and not current:
            lease=read_json(service.home/'incoming-audio-lease.json',{})
            if lease and not lease_released(lease) and same_arm(lease.get('armed_id'),settings.get('armed_id')):
                from google_voice_ui import GoogleVoiceUI
                restoration=cleanup_preparation(service,GoogleVoiceUI(service.home),settings['armed_id'])
    return status(service)|{'stop_requested':True,'voice_stop_call_id':stopped_call,'idle_arm_restoration':restoration,'cancel_token':request['token'],'note':'Matching incoming voice worker receives a direct stop request. An idle arm is restored directly when its watcher is absent. Active or uncertain calls require the watcher or explicit recovery; inspect status for cleanup failures.'}
