#!/usr/bin/env python3
"""Bounded incoming-call watcher. UI operations are delegated to a pinned adapter.

Caller-ID filtering is not authentication. Interruption recovery is deliberately
manual: a restarted watcher never repeats an answer or repairs an uncertain call.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import fcntl
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import signal
import threading
import time
import uuid

from service import CallError, CallService, read_json, write_json
from audio_policy import (KEEP_VIRTUAL,VIRTUAL_ROUTES,audio_cleanup_policy,
                          lease_released,cleanup_complete,release_fields)


class IncomingError(ValueError):
    pass


def expiry_epoch(value):
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise IncomingError('expires_at must include a timezone')
        value = parsed.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise IncomingError('expires_at must be a finite epoch or timezone-qualified ISO date')
    return float(value)


def validate_settings(value):
    if not isinstance(value, dict):
        raise IncomingError('Incoming settings are missing')
    if not isinstance(value.get('enabled'), bool):
        raise IncomingError('enabled must be boolean')
    if value.get('mode') not in {'allowlist', 'all'}:
        raise IncomingError('mode must be allowlist or all')
    numbers = value.get('allowed_numbers', [])
    if not isinstance(numbers, list) or any(not isinstance(n, str) or not re.fullmatch(r'\+[1-9][0-9]{7,14}', n) for n in numbers):
        raise IncomingError('allowed_numbers must contain E.164 numbers')
    if value['mode'] == 'allowlist' and not numbers:
        raise IncomingError('allowlist must not be empty')
    for field, limit, empty in [('owner', 120, False), ('brief', 6000, False), ('context', 10000, True)]:
        text = value.get(field, '' if empty else None)
        if not isinstance(text, str) or len(text) > limit or ('\x00' in text) or (not empty and not text.strip()):
            raise IncomingError('Invalid '+field)
    duration = value.get('max_seconds')
    if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 300:
        raise IncomingError('max_seconds must be 1–300')
    spend = value.get('other_spend_today_usd')
    if isinstance(spend, bool) or not isinstance(spend, (float, int)) or not math.isfinite(spend) or spend < 0:
        raise IncomingError('Outside spend must be finite and nonnegative')
    poll = value.get('poll_seconds', 1.0)
    if isinstance(poll, bool) or not isinstance(poll, (float, int)) or not math.isfinite(poll) or not .1 <= poll <= 10:
        raise IncomingError('poll_seconds must be 0.1–10')
    maximum = value.get('max_calls', 1)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 10:
        raise IncomingError('max_calls must be 1–10')
    readiness = value.get('readiness_seconds', 20)
    if isinstance(readiness, bool) or not isinstance(readiness, (int,float)) or not math.isfinite(readiness) or not 1 <= readiness <= 25:
        raise IncomingError('readiness_seconds must be 1–25')
    try:
        armed_id = str(uuid.UUID(value.get('armed_id')))
    except (ValueError, TypeError, AttributeError):
        raise IncomingError('armed_id must be the UUID generated when arming') from None
    result = dict(value)
    result['expires_at_epoch'] = expiry_epoch(value.get('expires_at'))
    result['poll_seconds'] = float(poll)
    result['armed_id'] = armed_id
    result['max_calls'] = maximum
    result['readiness_seconds'] = float(readiness)
    return result


def policy_fingerprint(settings):
    fields=('armed_id','mode','owner','brief','context','max_seconds','max_calls',
            'expires_at_epoch','readiness_seconds','version','audio_cleanup_policy')
    policy={key:settings.get(key) for key in fields}
    policy['allowed_numbers']=sorted(set(settings.get('allowed_numbers',[])))
    return hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest()


def valid_snapshot(snapshot, now, identity=None, max_age=5.0):
    if not isinstance(snapshot, dict) or snapshot.get('coverage') != 'complete':
        return False
    observed = snapshot.get('observed_at')
    if isinstance(observed, bool) or not isinstance(observed, (int, float)) or not math.isfinite(observed):
        return False
    if not -.5 <= now-observed <= max_age or not snapshot.get('identity'):
        return False
    if identity is not None and snapshot['identity'] != identity:
        return False
    return snapshot.get('state') in {'idle', 'ringing', 'active'} and isinstance(snapshot.get('evidence'), str) and bool(snapshot['evidence'])


def permitted(snapshot, settings):
    # An exact visible number is necessary for allowlisting, not proof of identity.
    return settings['mode'] == 'all' or snapshot.get('caller_number') in settings['allowed_numbers']


def restoration_error(exc, stage):
    """Log an adapter's stable code, never arbitrary exception text or data."""
    kind=type(exc).__name__
    code=getattr(exc,'code',None)
    if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,99}',code):
        code=kind
    return {'error_type':kind,'error_code':code,'error_stage':stage}


def restore_arm_audio(service, adapter, armed_id, *, clock=time.time):
    """Release one arm's route lease only with an idle window and no call owner."""
    path=service.home/'incoming-audio-lease.json'
    lease=read_json(path,{})
    if not lease or lease_released(lease):
        return {'released':True,'restored':not lease or lease.get('status')=='restored','reason':'No unresolved audio lease'}
    if lease.get('armed_id')!=armed_id:
        return {'restored':False,'reason':'Audio lease belongs to another arm'}
    policy=audio_cleanup_policy(lease)
    if not lease.get('original_routes') and policy!=KEEP_VIRTUAL:
        return {'restored':False,'reason':'Original audio routes unavailable; inspect manually'}
    if lease.get('restoration_attempted_at'):
        return {'restored':False,'reason':'Prior arm route restoration is unverified; inspect manually'}
    stage='enrollment'
    try:
        # Stop may have foregrounded Call Desk. Focus only the exact enrollment
        # that owns this lease, excluding new reservations during activation.
        # Focus verifies native identity; the snapshot below still verifies
        # the Google Voice URL/account and idle state before route selection.
        with service.locked() as registry:
            if registry.get('active'):
                return {'restored':False,'reason':'Call desk remains reserved; routes retained'}
            if read_json(path,{})!=lease:
                return {'restored':False,'reason':'Audio lease changed before restoration; routes retained'}
            identity=getattr(adapter,'_identity',None)
            focus=getattr(adapter,'focus_enrolled',None)
            if not callable(identity) or not callable(focus):
                return {'restored':False,'reason':'Exact enrolled window focus is unavailable; routes retained'}
            if not lease.get('identity') or identity()!=lease['identity']:
                return {'restored':False,'reason':'Enrolled window differs from audio lease; routes retained'}
            stage='focus'
            focus()
        stage='close'
        # A failed preparation may have left our settings panel open. Closing
        # that pinned panel is safe; restoration still requires fresh idle proof.
        close=getattr(adapter,'audio_close',None)
        if close:close()
        stage='snapshot'
        snap=adapter.snapshot()
        if not valid_snapshot(snap,clock(),lease.get('identity')) or snap['state']!='idle':
            return {'restored':False,'reason':'Pinned window is not verified idle; routes retained'}
        lease.update(status='restoring',restoration_attempted_at=clock());write_json(path,lease)
        stage='verify_virtual_routes' if policy==KEEP_VIRTUAL else 'restore_audio'
        receipt=(adapter.verify_audio_routes(VIRTUAL_ROUTES) if policy==KEEP_VIRTUAL
                 else adapter.restore_audio(lease['original_routes']))
        stage='final_snapshot'
        after=adapter.snapshot()
        stage='verification'
        if not isinstance(receipt,dict) or receipt.get('verified') is not True or not valid_snapshot(after,clock(),lease['identity']) or after['state']!='idle':
            raise IncomingError('Arm route restoration not verified')
        evidence=('Virtual Google Voice microphone and speaker defaults verified and retained; no physical route restoration requested'
                  if policy==KEEP_VIRTUAL else receipt.get('evidence','Original audio routes verified'))
        lease.update(release_fields(policy,evidence,clock()))
        write_json(path,lease)
        return {'released':True,'restored':policy!=KEEP_VIRTUAL,'reason':evidence}
    except Exception as exc:
        diagnostic=restoration_error(exc,stage)
        lease.update(status='cleanup_required',**diagnostic);write_json(path,lease)
        return {'restored':False,'reason':'Arm route restoration failed; inspect manually',**diagnostic}


class IncomingController:
    def __init__(self, service, adapter, *, clock=time.time, sleep=time.sleep,
                 stop_event=None, readiness_seconds=None,expected_armed_id=None):
        self.service, self.adapter = service, adapter
        self.home = Path(service.home)
        self.clock, self.sleep = clock, sleep
        self.stop_event = stop_event or threading.Event()
        self.readiness_seconds = readiness_seconds
        self.expected_armed_id=str(uuid.UUID(expected_armed_id)) if expected_armed_id is not None else None
        self.settings_path = self.home/'incoming-settings.json'
        self.state_path = self.home/'incoming-state.json'
        self.status_path = self.home/'incoming-status.json'
        self.state = read_json(self.state_path, {'version':1, 'seen_rings':{}, 'current':None})
        self.state.setdefault('arms',{})
        self.latest_arm=None
        self.interrupted = bool(self.state.get('current'))

    def save(self):
        write_json(self.state_path, self.state)

    def status(self, phase, **extra):
        current = self.state.get('current') or {}
        armed_id=current.get('armed_id') or self.latest_arm
        counts=self.state['arms'].get(armed_id,{})
        value = {'phase':phase, 'updated_at':self.clock(), 'pid':os.getpid(),
                 'call_id':current.get('call_id'), 'armed_id':armed_id,
                 'reserved_calls':counts.get('reserved',0),'started_calls':counts.get('started',0),
                 'completed_calls':counts.get('completed',0),
                 'caller_id_is_authentication':False, **extra}
        write_json(self.status_path, value)
        return value

    def settings(self):
        result=validate_settings(read_json(self.settings_path))
        if self.expected_armed_id is not None and result['armed_id']!=self.expected_armed_id:
            raise IncomingError('Watcher arm identity changed; refusing to adopt another authorization')
        self.latest_arm=result['armed_id']
        return result

    def armed(self, settings):
        return settings['enabled'] and not self.stop_event.is_set() and self.clock() < settings['expires_at_epoch']

    def snapshot(self):
        try:
            return self.adapter.snapshot()
        except Exception as exc:
            self.status('observation_failed', error_type=type(exc).__name__)
            return None

    def same_ring(self, snapshot, current, settings):
        return (valid_snapshot(snapshot, self.clock(), current['identity']) and
                snapshot['state']=='ringing' and snapshot.get('ring_id')==current['ring_id'] and
                snapshot.get('caller_number')==current['caller_number'] and permitted(snapshot, settings) and
                policy_fingerprint(settings)==current['policy_fingerprint'])

    def step(self):
        if self.interrupted:
            return self.status('recovery_required', reason='Previous watcher interrupted; inspect existing call and routes. No automatic answer or cleanup.')
        try:
            settings = self.settings()
        except Exception as exc:
            if self.state.get('current'):
                return self.cleanup('Incoming settings became invalid')
            return self.status('invalid_settings', error_type=type(exc).__name__)
        if self.state.get('current'):
            current = self.state['current']
            if current.get('cleanup_started'):
                return self.cleanup(current['cleanup_reason'])
            if not self.armed(settings):
                return self.cleanup('Watcher stopped or authorization expired')
            if policy_fingerprint(settings)!=current['policy_fingerprint']:
                return self.cleanup('Incoming authorization policy changed during call')
            snap = self.snapshot()
            if not valid_snapshot(snap, self.clock(), current['identity']):
                return self.cleanup('Call observation became uncertain')
            if snap['state']=='idle':
                return self.cleanup('Caller ended the call')
            if snap['state']!='active' or snap.get('ring_id')!=current['ring_id']:
                return self.cleanup('Call identity changed')
            worker = self.service.status(current['call_id'])
            if not worker.get('ready_to_dial') or self.clock() >= current['call_deadline']:
                return self.cleanup('Voice worker stopped or call reached duration bound')
            return self.status('active', caller_masked=self.mask(current['caller_number']))
        if not self.armed(settings):
            return self.status('disabled' if not settings['enabled'] or self.stop_event.is_set() else 'expired')
        counts=self.state['arms'].get(settings['armed_id'],{})
        if counts.get('reserved',0)>=settings['max_calls']:
            return self.status('call_limit_reached',max_calls=settings['max_calls'])
        # Local reservation inspection does not trigger credentials or audio.
        with self.service.locked() as registry:
            if registry.get('active'):
                return self.status('desk_busy', busy_call_id=registry['active'])
        snap = self.snapshot()
        if not valid_snapshot(snap, self.clock()):
            return self.status('observation_uncertain')
        if snap['state']!='ringing':
            return self.status('watching')
        ring_id = snap.get('ring_id')
        if not isinstance(ring_id, str) or not ring_id.strip() or len(ring_id)>500:
            return self.status('observation_uncertain', reason='No stable ring identity')
        if not permitted(snap, settings):
            return self.status('caller_filtered', caller_masked=self.mask(snap.get('caller_number')))
        key=hashlib.sha256(json.dumps([snap['identity'],ring_id], sort_keys=True).encode()).hexdigest()
        if key in self.state['seen_rings']:
            return self.status('ring_already_handled')
        return self.handle_ring(snap, settings, key)

    @staticmethod
    def mask(number):
        return '…'+number[-4:] if isinstance(number,str) else 'unavailable'

    def handle_ring(self, snap, settings, key):
        lease=read_json(self.home/'incoming-audio-lease.json',{})
        if (lease.get('status')!='prepared' or lease.get('armed_id')!=settings['armed_id']
                or lease.get('identity')!=snap['identity'] or not lease.get('original_routes')):
            return self.status('route_preparation_required',reason='Matching verified arm audio lease is required before accepting a ring')
        current={'ring_id':snap['ring_id'], 'identity':snap['identity'],
                 'caller_number':snap.get('caller_number'), 'owner':settings['owner'],
                 'ring_key':key, 'created_at':self.clock(), 'phase':'reserving',
                 'armed_id':settings['armed_id'],'policy_fingerprint':policy_fingerprint(settings),
                 'direction':'incoming','call_started':None,
                 'call_id':None, 'original_routes':lease['original_routes'], 'answer_attempted':False,
                 'audio_cleanup_policy':audio_cleanup_policy(lease)}
        self.state['seen_rings'][key]={'at':self.clock(), 'status':'attempted'}
        self.state['current']=current
        self.save()  # Interruption before reserve receipt still forbids replay.
        try:
            try:
                receipt=self.service.prepare_incoming(
                    owner=settings['owner'], request_key='incoming-'+key,
                    caller_number=snap.get('caller_number'), brief=settings['brief'],
                    context=settings.get('context',''), max_seconds=settings['max_seconds'],
                    authorization='Bounded incoming authorization in incoming-settings.json; expires '+str(settings['expires_at']),
                    other_spend_today_usd=settings['other_spend_today_usd'], ring_id=snap['ring_id'],
                    routing_policy=current['audio_cleanup_policy'])
            except CallError:
                # A documented validation/reservation refusal has no dispatch.
                # In particular, an outbound caller can win the reservation
                # after our free-desk observation; do not disturb that call.
                self.state['seen_rings'][key]['status']='reservation_refused'
                self.state['current']=None;self.save()
                return self.status('reservation_refused')
            current['call_id']=receipt['call_id']
            counts=self.state['arms'].setdefault(current['armed_id'],{'reserved':0,'started':0,'completed':0})
            counts['reserved']+=1
            self.save()
            current['phase']='using_prepared_audio';self.save()
            fresh=self.snapshot();settings=self.settings()
            if not self.armed(settings) or policy_fingerprint(settings)!=current['policy_fingerprint']:
                return self.cleanup('Incoming authorization stopped or changed before voice start')
            if not self.same_ring(fresh,current,settings):
                return self.cleanup('Matching incoming ring was not freshly verifiable before voice start')
            self.service.start(current['call_id'],current['owner'])
            current['phase']='waiting_for_voice';self.save()
            readiness=settings['readiness_seconds'] if self.readiness_seconds is None else self.readiness_seconds
            deadline=min(self.clock()+readiness,settings['expires_at_epoch'])
            while self.clock()<deadline:
                settings=self.settings()
                if not self.armed(settings) or policy_fingerprint(settings)!=current['policy_fingerprint']:
                    return self.cleanup('Incoming authorization stopped, changed, or expired before voice readiness')
                fresh=self.snapshot()
                observation=self.readiness_observation(fresh,current)
                current['last_readiness_observation']=observation
                if observation['classification']=='unverified':
                    current['readiness_unverified_observations']=current.get('readiness_unverified_observations',0)+1
                self.save()
                if observation['classification'] not in {'matching_ring','unverified'}:
                    return self.cleanup('Verified '+observation['classification'].replace('_',' ')+' before voice readiness')
                worker=self.service.status(current['call_id'])
                if (worker.get('ready_to_dial') and observation['classification']=='matching_ring' and self.clock()<deadline):
                    break
                if worker.get('worker_result_status') or worker.get('finished_at'):
                    return self.cleanup('Voice preflight failed; incoming call was not answered')
                self.status('waiting_for_voice', caller_masked=self.mask(current['caller_number']),last_observation=observation)
                remaining=deadline-self.clock()
                if remaining>0:self.sleep(min(settings['poll_seconds'],.5,remaining))
            else:
                last=current.get('last_readiness_observation',{})
                return self.cleanup('Incoming readiness deadline reached; last ring observation was '+last.get('classification','unavailable')+'; call not answered')
            # The final matching observation above is already fresh. Re-reading
            # asynchronously here could turn a harmless transient partial tree
            # into a false "ring ended" claim. The adapter revalidates before
            # its one click; check policy/freshness again without a second poll.
            settings=self.settings()
            if self.clock()>=deadline or not self.armed(settings) or not self.same_ring(fresh,current,settings):
                return self.cleanup('Fresh ring verification or authorization expired immediately before answer')
            intent=self.service.answer_intent(current['call_id'],current['owner'],fresh['evidence'])
            current['answer_attempted']=True;current['phase']='answer_dispatched_or_uncertain';self.save()
            if not intent.get('dispatch_allowed'):
                return self.cleanup('Answer intent already exists; inspect without another answer')
            try:
                receipt=self.adapter.answer(current['ring_id'])
                current['answer_receipt']=self.receipt_summary(receipt);self.save()
            except Exception as exc:
                current['answer_error_type']=type(exc).__name__;self.save()
            after,settle_reason=self.wait_for_answer(current,settings)
            if after is not None:
                self.service.observe(current['call_id'],current['owner'],'answered',after['evidence'])
                current['phase']='active';current['call_started']=self.clock()
                current['call_deadline']=self.clock()+settings['max_seconds']
                self.state['arms'][current['armed_id']]['started']+=1
                self.save()
                return self.status('active',caller_masked=self.mask(current['caller_number']))
            self.service.observe(current['call_id'],current['owner'],'uncertain','Answer dispatched once; expected active call not observed')
            return self.cleanup(settle_reason+'; never retry answer')
        except Exception as exc:
            current['error_type']=type(exc).__name__;self.save()
            return self.cleanup('Incoming attempt failed: '+type(exc).__name__)

    @staticmethod
    def receipt_summary(receipt):
        if not isinstance(receipt,dict):
            return {'delivery':'unknown'}
        return {k:receipt[k] for k in ('delivery','verified','state') if k in receipt}

    def readiness_observation(self,snapshot,current):
        """Classify only complete, fresh evidence as a changed/ended ring."""
        snap=snapshot if isinstance(snapshot,dict) else {}
        classification='unverified'
        if valid_snapshot(snap,self.clock()):
            if snap['identity']!=current['identity']:classification='window_identity_changed'
            elif snap['state']=='idle':classification='ring_ended_idle'
            elif snap.get('caller_number')!=current['caller_number']:classification='caller_changed'
            elif snap.get('ring_id')!=current['ring_id']:classification='ring_identity_changed'
            elif snap['state']!='ringing':classification='call_state_changed'
            else:classification='matching_ring'
        return {'classification':classification,'observed_at':snap.get('observed_at'),
                'coverage':snap.get('coverage','unavailable'),'state':snap.get('state','unavailable'),
                'evidence':str(snap.get('evidence','No accepted observation'))[:300]}

    def wait_for_answer(self,current,settings):
        """Observe one dispatched answer settling; this never dispatches input."""
        deadline=min(self.clock()+3.0,settings['expires_at_epoch'])
        while self.clock()<deadline:
            settings=self.settings()
            if not self.armed(settings) or policy_fingerprint(settings)!=current['policy_fingerprint']:
                return None,'Authorization stopped or changed while answer settled'
            if not self.service.status(current['call_id']).get('ready_to_dial'):
                return None,'Voice worker stopped while answer settled'
            after=self.snapshot()
            if isinstance(after,dict):
                if after.get('identity') is not None and after['identity']!=current['identity']:
                    return None,'Window identity changed while answer settled'
                if after.get('caller_number') is not None and after['caller_number']!=current['caller_number']:
                    return None,'Caller changed while answer settled'
                if after.get('ring_id') is not None and after['ring_id']!=current['ring_id']:
                    return None,'Ring identity changed while answer settled'
            if valid_snapshot(after,self.clock(),current['identity']):
                if after['state']=='idle':
                    return None,'Call ended before answer was verified'
                if (after['state']=='active' and after.get('ring_id')==current['ring_id'] and
                        after.get('caller_number')==current['caller_number']):
                    # Reading the UI can take time. Recheck authorization and
                    # worker readiness after that read, before opening audio.
                    latest=self.settings()
                    if (self.clock()<deadline and self.armed(latest) and
                            policy_fingerprint(latest)==current['policy_fingerprint'] and
                            self.service.status(current['call_id']).get('ready_to_dial')):
                        return after,None
                    return None,'Authorization or voice readiness expired while answer settled'
            remaining=deadline-self.clock()
            if remaining>0:self.sleep(min(.1,settings['poll_seconds'],remaining))
        return None,'Answer outcome did not settle within three seconds'

    def cleanup(self, reason):
        current=self.state['current']
        current.setdefault('cleanup_started',self.clock());current.setdefault('cleanup_reason',reason);self.save()
        call_id=current.get('call_id')
        if not call_id:
            # Reservation may have landed before its receipt was saved/returned.
            return self.status('recovery_required',reason='Reservation outcome uncertain; inspect shared registry')
        if not current.get('stop_requested'):
            self.service.stop(call_id);current['stop_requested']=True;self.save()
        snap=self.snapshot()
        if not valid_snapshot(snap,self.clock(),current['identity']):
            return self.status('cleanup_required',reason='Cannot verify call state; voice stop requested, routes retained')
        if snap['state']=='active':
            if not current['answer_attempted'] or snap.get('ring_id')!=current['ring_id']:
                return self.status('cleanup_required',reason='Active call ownership uncertain; no hangup or route changes')
            if not current.get('hangup_attempted'):
                current['hangup_attempted']=True;self.save()
                try:
                    self.adapter.hangup()
                except Exception as exc:
                    current['hangup_error_type']=type(exc).__name__;self.save()
                snap=self.snapshot()
            if not valid_snapshot(snap,self.clock(),current['identity']) or snap['state']!='idle':
                return self.status('cleanup_required',reason='Hangup not verified; no automatic second click or route changes')
        if snap['state']=='ringing':
            if snap.get('ring_id')!=current['ring_id']:
                return self.status('cleanup_required',reason='Another ring is present; wait for idle before restoring routes')
            if current['answer_attempted']:
                return self.status('cleanup_required',reason='Answer outcome uncertain; wait for idle before restoring routes')
        # A hangup may safely precede this wait. Restoring a microphone/speaker
        # while the voice worker still holds streams can expose or misroute
        # live audio, so restoration and release both require local shutdown.
        worker=self.service.status(call_id)
        if worker.get('worker_active'):
            return self.status('stopping_voice')
        policy=audio_cleanup_policy(current)
        if not current.get('audio_cleanup_verified') and not current.get('routes_restored'):
            if current.get('original_routes') is None and policy!=KEEP_VIRTUAL:
                # Audio preparation could have mutated routes before an exception.
                return self.status('cleanup_required',reason='Original routes unavailable; manual inspection required')
            if current.get('restoration_attempted'):
                return self.status('cleanup_required',reason='Prior restoration was not verified; manual inspection required')
            current['restoration_attempted']=True;self.save()
            try:
                restored=(self.adapter.verify_audio_routes(VIRTUAL_ROUTES) if policy==KEEP_VIRTUAL
                          else self.adapter.restore_audio(current['original_routes']))
            except Exception as exc:
                diagnostic=restoration_error(exc,'verify_virtual_routes' if policy==KEEP_VIRTUAL else 'restore_audio')
                current.update({'restore_'+key:value for key,value in diagnostic.items()});self.save()
                return self.status('cleanup_required',reason='Configured audio cleanup failed; inspect manually',**diagnostic)
            if not isinstance(restored,dict) or restored.get('verified') is not True:
                return self.status('cleanup_required',reason='Configured audio cleanup not verified')
            evidence=('Virtual Google Voice microphone and speaker defaults verified and retained; no physical route restoration requested'
                      if policy==KEEP_VIRTUAL else restored.get('evidence','Adapter verified original routes'))
            current.update(audio_cleanup_verified=True,routes_restored=policy!=KEEP_VIRTUAL,
                           audio_cleanup_evidence=evidence,restoration_evidence=evidence);self.save()
            snap=self.snapshot()
            if not valid_snapshot(snap,self.clock(),current['identity']):
                return self.status('cleanup_required',reason='Fresh post-restoration call state unavailable')
        lease=read_json(self.home/'incoming-audio-lease.json',{})
        if lease.get('armed_id')==current['armed_id']:
            lease.update(release_fields(policy,current['restoration_evidence'],self.clock()))
            write_json(self.home/'incoming-audio-lease.json',lease)
        if snap['state']!='idle':
            return self.status('waiting_for_ring_end',reason='Unanswered call still ringing; configured audio cleanup verified, no answer will be attempted')
        try:
            self.service.finish(call_id,current['owner'],snap['evidence'],current['restoration_evidence'],current['cleanup_reason'])
        except Exception as exc:
            return self.status('cleanup_required',reason='Service requires independent cleanup/recovery evidence',error_type=type(exc).__name__)
        self.state['seen_rings'][current['ring_key']]['status']='finished'
        self.state['arms'][current['armed_id']]['completed']+=1
        self.state['current']=None;self.save()
        return self.status('call_finished',call_id=call_id,outcome=reason)

    def finish_watching(self,result):
        """Normal exits also restore pre-armed routes when no call ever arrived."""
        if self.state.get('current'):
            return result
        armed_id=self.expected_armed_id or self.latest_arm
        restored=restore_arm_audio(self.service,self.adapter,armed_id,clock=self.clock)
        settings=read_json(self.settings_path,{})
        if settings.get('armed_id')==armed_id:
            settings['enabled']=False;write_json(self.settings_path,settings)
        if not cleanup_complete(restored):
            diagnostic={key:restored[key] for key in ('error_type','error_code','error_stage') if key in restored}
            return self.status('cleanup_required',reason=restored['reason'],**diagnostic)
        return result


def run_watcher(service,adapter,*,stop_event=None,expected_armed_id=None):
    service.home.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Share launch serialization with incoming_control.arm/disarm. A delayed
    # child validates its own arm while holding both locks; it cannot adopt a
    # replacement arm or prevent the correct new child from taking the desk.
    launch_fd=os.open(service.home/'incoming-control.lock',os.O_CREAT|os.O_RDWR,0o600)
    fcntl.flock(launch_fd,fcntl.LOCK_EX)
    fd=os.open(service.home/'incoming.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return {'phase':'already_running'}
        try:
            expected=str(uuid.UUID(expected_armed_id))
            actual=str(uuid.UUID(read_json(service.home/'incoming-settings.json',{}).get('armed_id')))
        except (ValueError,TypeError,AttributeError):
            return {'phase':'invalid_arm_identity'}
        if actual!=expected:
            # Do not overwrite the current arm's status with an old child's
            # rejection receipt. Its structured stdout is sufficient evidence.
            return {'phase':'arm_mismatch','expected_armed_id':expected}
        controller=IncomingController(service,adapter,stop_event=stop_event,expected_armed_id=expected)
        controller.latest_arm=expected
        controller.status('starting')
        os.close(launch_fd);launch_fd=None
        try:
            while True:
                result=controller.step()
                if result['phase'] in {'recovery_required','invalid_settings','disabled','expired','call_limit_reached','route_preparation_required'}:
                    return controller.finish_watching(result)
                if controller.stop_event.is_set() and result['phase'] not in {'stopping_voice','waiting_for_ring_end'}:
                    return controller.finish_watching(result)
                settings=controller.settings()
                # Stopped/expired watchers get a short bounded cleanup window.
                current=controller.state.get('current')
                if current and not controller.armed(settings) and controller.clock()-current.get('cleanup_started',controller.clock())>15:
                    return controller.status('cleanup_required',reason='Cleanup did not verify before bounded shutdown')
                controller.sleep(settings['poll_seconds'])
        except Exception as exc:
            # An unexpected observer/controller error must not leave the voice
            # worker running merely because this process exits.
            if controller.state.get('current'):
                with contextlib.suppress(Exception):
                    controller.cleanup('Watcher failed: '+type(exc).__name__)
            return controller.finish_watching(controller.status('watcher_failed',error_type=type(exc).__name__))
    finally:
        os.close(fd)
        if launch_fd is not None:os.close(launch_fd)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--state-dir',type=Path)
    parser.add_argument('--armed-id',required=True)
    parser.add_argument('--adapter-module',default='google_voice_ui')
    args=parser.parse_args()
    os.umask(0o077)
    stopped=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):
        signal.signal(sig,lambda *_:stopped.set())
    service=CallService(home=args.state_dir,config=args.config)
    module=importlib.import_module(args.adapter_module)
    adapter=module.GoogleVoiceUI(service.home)
    result=run_watcher(service,adapter,stop_event=stopped,expected_armed_id=args.armed_id)
    print(json.dumps(result))


if __name__=='__main__':
    main()
