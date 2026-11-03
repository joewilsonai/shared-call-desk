import contextlib
import fcntl
import json
from pathlib import Path
import threading
import uuid

import pytest

from incoming import IncomingController, IncomingError, run_watcher, validate_settings, restore_arm_audio
from google_voice_ui import UIError
from audio_policy import KEEP_VIRTUAL,VIRTUAL_ROUTES,lease_released
from service import CallError, write_json


class Clock:
    def __init__(self): self.value=1000.0
    def time(self): return self.value
    def sleep(self,seconds): self.value+=seconds


class Service:
    def __init__(self,home):
        self.home=home;self.active=None;self.calls=[];self.stopped=False
        self.ready=True;self.finished=False;self.prepares=0

    @contextlib.contextmanager
    def locked(self): yield {'active':self.active}

    def prepare_incoming(self,**request):
        self.calls.append(('prepare',request));self.prepares+=1
        self.active='call_test';return {'call_id':'call_test'}

    def start(self,call_id,owner): self.calls.append(('start',call_id))
    def status(self,call_id):
        return {'ready_to_dial':self.ready and not self.stopped,'worker_active':not self.stopped}
    def answer_intent(self,call_id,owner,evidence):
        self.calls.append(('answer_intent',evidence));return {'dispatch_allowed':True}
    def observe(self,call_id,owner,state,evidence): self.calls.append(('observe',state))
    def stop(self,call_id): self.calls.append(('stop',call_id));self.stopped=True
    def finish(self,call_id,owner,hangup,restore,outcome):
        self.calls.append(('finish',outcome));self.active=None;self.finished=True


class UI:
    def __init__(self,clock):
        self.clock=clock;self.state='ringing';self.number='+15555550123'
        self.ring='ring-one';self.identity='browser-voice-window-one'
        self.coverage='complete';self.age=0;self.calls=[];self.on_prepare=None
        self.answer_uncertain=False;self.hangup_uncertain=False;self.restored=True
        self.original={'microphone':'built-in mic','speaker':'built-in speaker'}

    def _identity(self):return self.identity

    def focus_enrolled(self):self.calls.append('focus')

    def snapshot(self):
        self.calls.append('snapshot')
        if self.on_prepare and self.calls.count('snapshot')>=2:
            action=self.on_prepare;self.on_prepare=None;action()
        return {'observed_at':self.clock.time()-self.age,'identity':self.identity,
                'coverage':self.coverage,'state':self.state,'caller_number':self.number,
                'ring_id':self.ring if self.state in {'ringing','active'} else None,
                'evidence':'Fake exact target is '+self.state}

    def prepare_audio(self):
        self.calls.append('prepare_audio')
        if self.on_prepare:self.on_prepare()
        return {'original_routes':self.original,'verified':True,'evidence':'Fake routes prepared'}

    def answer(self,ring):
        self.calls.append('answer');assert ring==self.ring
        if self.answer_uncertain:
            self.state='unknown';raise TimeoutError('simulated uncertain answer')
        self.state='active';return {'delivery':'delivered'}

    def hangup(self):
        self.calls.append('hangup')
        if self.hangup_uncertain:raise TimeoutError('simulated uncertain hangup')
        self.state='idle';return self.snapshot()

    def restore_audio(self,original):
        self.calls.append('restore_audio');assert original==self.original
        return {'verified':self.restored,'evidence':'Fake original routes restored'}

    def verify_audio_routes(self,expected):
        assert expected==VIRTUAL_ROUTES
        self.calls.append('verify_virtual')
        return {'verified':self.restored,'actual_routes':dict(VIRTUAL_ROUTES),'evidence':'Fake virtual defaults observed'}


def settings():
    return {'enabled':True,'mode':'allowlist','allowed_numbers':['+15555550123'],
            'owner':'test-watcher','brief':'Authorized fake inbound test','context':'No personal data',
            'max_seconds':60,'other_spend_today_usd':0,'expires_at':1100,'poll_seconds':.1,
            'armed_id':'00000000-0000-4000-8000-000000000001'}


def lease(home,ui,armed_id=None):
    write_json(home/'incoming-audio-lease.json',{'armed_id':armed_id or settings()['armed_id'],
        'identity':ui.identity,'status':'prepared','original_routes':ui.original,'prepared_at':999})


@pytest.fixture
def setup(tmp_path):
    clock=Clock();service=Service(tmp_path);ui=UI(clock)
    write_json(tmp_path/'incoming-settings.json',settings())
    lease(tmp_path,ui)
    controller=IncomingController(service,ui,clock=clock.time,sleep=clock.sleep,readiness_seconds=.3)
    return controller,service,ui,clock


def test_filters_non_allowlisted_and_unknown_before_any_mutation(setup):
    controller,service,ui,_=setup
    for number in ['+15555550999',None]:
        ui.number=number
        assert controller.step()['phase']=='caller_filtered'
    assert service.calls==[] and 'prepare_audio' not in ui.calls


@pytest.mark.parametrize('kind',['partial','stale','unknown'])
def test_incomplete_or_stale_evidence_never_answers(setup,kind):
    controller,service,ui,_=setup
    if kind=='partial':ui.coverage='partial'
    elif kind=='stale':ui.age=10
    else:ui.state='unknown'
    assert controller.step()['phase']=='observation_uncertain'
    assert not service.calls


def test_busy_desk_does_not_touch_browser(setup):
    controller,service,ui,_=setup;service.active='outbound_call'
    assert controller.step()['phase']=='desk_busy'
    assert not ui.calls and not service.calls


def test_outbound_reservation_wins_race_without_inbound_cleanup(setup):
    controller,service,ui,_=setup
    def refuse(**_):
        service.active='outbound-won-race'
        raise CallError('Call desk already reserved')
    service.prepare_incoming=refuse
    assert controller.step()['phase']=='reservation_refused'
    assert controller.state['current'] is None and service.active=='outbound-won-race'
    assert not service.stopped and 'prepare_audio' not in ui.calls


def test_ring_disappears_during_audio_preparation_no_billable_start(setup):
    controller,service,ui,_=setup
    ui.on_prepare=lambda:setattr(ui,'state','idle')
    assert controller.step()['phase']=='call_finished'
    assert 'start' not in [c[0] for c in service.calls]
    assert 'answer' not in ui.calls and 'restore_audio' in ui.calls and service.finished


def test_caller_changes_during_preparation_is_not_answered(setup):
    controller,service,ui,_=setup
    ui.on_prepare=lambda:setattr(ui,'number','+15555550999')
    assert controller.step()['phase']=='waiting_for_ring_end'
    assert 'start' not in [c[0] for c in service.calls] and 'answer' not in ui.calls


def test_credential_readiness_timeout_does_not_pick_up_silently(setup):
    controller,service,ui,clock=setup;service.ready=False
    assert controller.step()['phase']=='waiting_for_ring_end'
    assert clock.value>=1000.3 and 'answer' not in ui.calls
    assert service.stopped and 'restore_audio' in ui.calls
    ui.state='idle'
    assert controller.step()['phase']=='call_finished'


def test_transient_partial_readiness_snapshot_does_not_end_real_ring(setup):
    controller,service,ui,clock=setup
    original=ui.snapshot;unverified=[True]
    def snapshot():
        value=original()
        if any(call[0]=='start' for call in service.calls) and unverified:
            unverified.pop();value.update(coverage='partial',state='unknown',ring_id=None,
                                          caller_number=None,evidence='Transient native AX read incomplete')
        return value
    ui.snapshot=snapshot
    assert controller.step()['phase']=='active'
    assert not service.stopped and ui.calls.count('answer')==1 and clock.time()>=1000.1
    assert controller.state['current']['readiness_unverified_observations']==1


def test_persistent_unknown_readiness_keeps_original_deadline_and_honest_reason(setup):
    controller,service,ui,clock=setup
    original=ui.snapshot
    def snapshot():
        value=original()
        if any(call[0]=='start' for call in service.calls):
            value.update(coverage='partial',state='unknown',ring_id=None,
                         caller_number=None,evidence='Native observer incomplete')
        return value
    ui.snapshot=snapshot
    assert controller.step()['phase']=='cleanup_required'
    current=controller.state['current']
    assert current['last_readiness_observation']['classification']=='unverified'
    assert 'unverified' in current['cleanup_reason'] and 'ended' not in current['cleanup_reason']
    assert 1000.3<=clock.time()<1000.31 and 'answer' not in ui.calls and service.stopped


@pytest.mark.parametrize('change',['idle','caller','ring','identity'])
def test_verified_readiness_change_fails_immediately(setup,change):
    controller,service,ui,clock=setup
    original=ui.snapshot
    def snapshot():
        value=original()
        if any(call[0]=='start' for call in service.calls):
            if change=='idle':value.update(state='idle',caller_number=None,ring_id=None)
            elif change=='caller':value['caller_number']='+15555550999'
            elif change=='ring':value['ring_id']='different-ring'
            else:value['identity']='different-window'
        return value
    ui.snapshot=snapshot
    controller.step()
    assert clock.time()==1000 and service.stopped and 'answer' not in ui.calls


def test_stop_during_unknown_readiness_stops_without_waiting_for_full_deadline(setup):
    controller,service,ui,clock=setup
    original=ui.snapshot
    def snapshot():
        value=original()
        if any(call[0]=='start' for call in service.calls):value.update(coverage='partial',state='unknown')
        return value
    ui.snapshot=snapshot
    def stop(seconds):clock.sleep(seconds);controller.stop_event.set()
    controller.sleep=stop
    controller.step()
    assert clock.time()<1000.3 and service.stopped and 'answer' not in ui.calls


def test_stop_active_call_hangs_up_once_restores_and_releases(setup):
    controller,service,ui,_=setup
    assert controller.step()['phase']=='active'
    assert ('observe','answered') in service.calls
    controller.stop_event.set()
    assert controller.step()['phase']=='call_finished'
    assert ui.calls.count('answer')==1 and ui.calls.count('hangup')==1
    assert ui.calls.count('restore_audio')==1 and service.finished


def test_answer_transition_can_settle_without_repeating_click(setup):
    controller,service,ui,clock=setup
    original_snapshot=ui.snapshot
    settling=[]
    def answer(ring):
        ui.calls.append('answer');settling.extend(['ringing','partial','active'])
        return {'delivery':'delivered_unverified'}
    def snapshot():
        if settling:
            state=settling.pop(0)
            ui.state='unknown' if state=='partial' else state
            ui.coverage='partial' if state=='partial' else 'complete'
        return original_snapshot()
    ui.answer=answer;ui.snapshot=snapshot
    assert controller.step()['phase']=='active'
    assert clock.time()>=1000.2 and ui.calls.count('answer')==1
    assert ('observe','answered') in service.calls and not service.stopped


def test_stop_during_answer_transition_never_opens_answered_audio(setup):
    controller,service,ui,clock=setup
    def answer(ring):ui.calls.append('answer');return {'delivery':'delivered_unverified'}
    ui.answer=answer
    def stop_on_wait(seconds):
        clock.sleep(seconds);controller.stop_event.set()
    controller.sleep=stop_on_wait
    assert controller.step()['phase']=='cleanup_required'
    assert ui.calls.count('answer')==1 and service.stopped
    assert ('observe','answered') not in service.calls


@pytest.mark.parametrize('field,value',[('identity','different-window'),('number','+15555550999'),('ring','different-ring')])
def test_answer_transition_identity_change_fails_without_wait_or_retry(setup,field,value):
    controller,service,ui,clock=setup
    def answer(ring):
        ui.calls.append('answer');setattr(ui,field,value)
        return {'delivery':'delivered_unverified'}
    ui.answer=answer
    controller.step()
    assert clock.time()==1000 and ui.calls.count('answer')==1 and service.stopped
    assert ('observe','answered') not in service.calls


def test_answer_transition_timeout_never_repeats_click(setup):
    controller,service,ui,clock=setup
    def answer(ring):ui.calls.append('answer');return {'delivery':'unknown'}
    ui.answer=answer
    assert controller.step()['phase']=='cleanup_required'
    assert 1003<=clock.time()<1003.01 and ui.calls.count('answer')==1
    assert ('observe','answered') not in service.calls and service.stopped


def test_duplicate_ring_durable_across_clean_restart(setup):
    controller,service,ui,clock=setup
    policy=settings();policy['max_calls']=2
    write_json(service.home/'incoming-settings.json',policy)
    assert controller.step()['phase']=='active'
    ui.state='idle'
    assert controller.step()['phase']=='call_finished'
    other=IncomingController(service,ui,clock=clock.time,sleep=clock.sleep)
    ui.state='ringing'
    assert other.step()['phase']=='ring_already_handled'
    assert service.prepares==1 and ui.calls.count('answer')==1


def test_uncertain_answer_never_retries_or_releases(setup):
    controller,service,ui,_=setup;ui.answer_uncertain=True
    assert controller.step()['phase']=='cleanup_required'
    assert controller.step()['phase']=='cleanup_required'
    assert ui.calls.count('answer')==1 and not service.finished and service.stopped
    assert 'restore_audio' not in ui.calls  # Could expose a live physical mic.


def test_uncertain_hangup_does_not_repeat_or_restore_live_call(setup):
    controller,service,ui,_=setup
    controller.step();ui.hangup_uncertain=True;controller.stop_event.set()
    assert controller.step()['phase']=='cleanup_required'
    assert controller.step()['phase']=='cleanup_required'
    assert ui.calls.count('hangup')==1 and 'restore_audio' not in ui.calls and not service.finished


def test_restarted_interrupted_watcher_only_reports_recovery(setup):
    controller,service,ui,clock=setup;controller.step()
    before=len(ui.calls)
    other=IncomingController(service,ui,clock=clock.time,sleep=clock.sleep)
    assert other.step()['phase']=='recovery_required' and len(ui.calls)==before


def test_expired_arm_never_starts(setup):
    controller,service,ui,clock=setup;clock.value=1101
    assert controller.step()['phase']=='expired' and not service.calls and not ui.calls


def test_policy_change_before_answer_prevents_dispatch(setup):
    controller,service,ui,_=setup
    def change():
        value=settings();value['allowed_numbers']=['+15555550999']
        write_json(service.home/'incoming-settings.json',value)
    ui.on_prepare=change
    assert controller.step()['phase']=='waiting_for_ring_end' and 'answer' not in ui.calls


def test_prepared_arm_skips_audio_mutation_and_restores_original_routes(setup):
    controller,service,ui,_=setup
    def fail():
        raise AssertionError('Per-ring audio preparation must not run')
    ui.prepare_audio=fail
    assert controller.step()['phase']=='active'
    controller.stop_event.set()
    assert controller.step()['phase']=='call_finished' and 'restore_audio' in ui.calls
    assert 'prepare_audio' not in ui.calls


def test_failed_restoration_holds_desk_and_does_not_loop_mutation(setup):
    controller,service,ui,_=setup;ui.restored=False
    ui.on_prepare=lambda:setattr(ui,'state','idle')
    assert controller.step()['phase']=='cleanup_required'
    assert controller.step()['phase']=='cleanup_required'
    assert ui.calls.count('restore_audio')==1 and not service.finished


def test_process_lock_excludes_second_watcher(setup):
    _,service,ui,_=setup
    with (service.home/'incoming.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert run_watcher(service,ui)['phase']=='already_running'
    assert not ui.calls


def test_default_one_call_limit_persists_across_restart(setup):
    controller,service,ui,clock=setup
    assert controller.step()['phase']=='active'
    current=controller.state['current']
    assert current['direction']=='incoming' and current['call_started']==clock.time()
    ui.state='idle'
    assert controller.step()['phase']=='call_finished'
    counts=controller.state['arms'][settings()['armed_id']]
    assert counts=={'reserved':1,'started':1,'completed':1}
    other=IncomingController(service,ui,clock=clock.time,sleep=clock.sleep)
    ui.state='ringing';ui.ring='second-ring'
    assert other.step()['phase']=='call_limit_reached'
    assert service.prepares==1 and ui.calls.count('answer')==1


def test_new_arm_has_new_limit_and_old_completion_counts_remain(setup):
    controller,service,ui,_=setup
    controller.step();ui.state='idle';controller.step()
    policy=settings();policy['armed_id']=str(uuid.uuid4())
    write_json(service.home/'incoming-settings.json',policy)
    lease(service.home,ui,policy['armed_id'])
    service.stopped=False;ui.state='ringing';ui.ring='new-arm-ring'
    assert controller.step()['phase']=='active'
    assert controller.state['arms'][settings()['armed_id']]['completed']==1
    assert controller.state['arms'][policy['armed_id']]['reserved']==1


@pytest.mark.parametrize('keep_virtual',[False,True])
def test_worker_must_stop_before_audio_cleanup(setup,keep_virtual):
    controller,service,ui,_=setup
    if keep_virtual:use_virtual_policy(service)
    controller.step()
    def delayed_stop(call_id):service.calls.append(('stop',call_id))
    service.stop=delayed_stop;controller.stop_event.set()
    assert controller.step()['phase']=='stopping_voice'
    assert ui.calls.count('hangup')==1 and 'restore_audio' not in ui.calls and 'verify_virtual' not in ui.calls
    assert not service.finished
    service.stopped=True
    assert controller.step()['phase']=='call_finished'
    assert ui.calls.count('hangup')==1
    assert ui.calls.count('verify_virtual' if keep_virtual else 'restore_audio')==1


@pytest.mark.parametrize('change',['allowlist','brief','armed_id','version'])
def test_policy_version_change_during_active_call_stops_and_cleans(setup,change):
    controller,service,ui,_=setup;controller.step()
    policy=settings()
    if change=='allowlist':policy['allowed_numbers']=['+15555550999']
    elif change=='brief':policy['brief']='Different authorized purpose'
    elif change=='armed_id':policy['armed_id']=str(uuid.uuid4())
    else:policy['version']=2
    write_json(service.home/'incoming-settings.json',policy)
    result=controller.step()
    assert result['phase']=='call_finished' and service.stopped
    assert 'policy changed' in result['outcome'] and ui.calls.count('hangup')==1


def test_readiness_comes_from_settings_and_defaults_to_twenty(setup):
    controller,service,ui,clock=setup
    assert validate_settings(settings())['readiness_seconds']==20
    controller.readiness_seconds=None;service.ready=False
    policy=settings();policy['readiness_seconds']=2
    write_json(service.home/'incoming-settings.json',policy)
    assert controller.step()['phase']=='waiting_for_ring_end'
    assert 1002<=clock.time()<1002.2 and 'answer' not in ui.calls


@pytest.mark.parametrize('field,value',[('mode','contacts'),('expires_at',None),('expires_at','2026-11-02'),('max_seconds',301),('other_spend_today_usd',float('nan')),('max_calls',0),('max_calls',11),('max_calls',True),('armed_id','not-a-uuid'),('readiness_seconds',26)])
def test_invalid_policy_rejected(field,value):
    policy=settings();policy[field]=value
    with pytest.raises((IncomingError,ValueError)):validate_settings(policy)


def test_missing_prepared_lease_never_starts_on_ring(setup):
    controller,service,ui,_=setup
    (service.home/'incoming-audio-lease.json').unlink()
    assert controller.step()['phase']=='route_preparation_required'
    assert not service.calls and 'answer' not in ui.calls


def test_expired_no_call_window_restores_routes_once(setup):
    controller,service,ui,clock=setup
    ui.state='idle';clock.value=1101
    result=controller.finish_watching(controller.step())
    assert result['phase']=='expired'
    assert ui.calls.count('restore_audio')==1
    assert not json.loads((service.home/'incoming-settings.json').read_text())['enabled']
    controller.finish_watching(result)
    assert ui.calls.count('restore_audio')==1


def test_arm_restoration_refuses_active_call_or_changed_window(setup):
    controller,service,ui,clock=setup
    ui.state='idle';service.active='outbound'
    assert not restore_arm_audio(service,ui,settings()['armed_id'],clock=clock.time)['restored']
    assert not ui.calls
    service.active=None;ui.identity='another-window'
    assert not restore_arm_audio(service,ui,settings()['armed_id'],clock=clock.time)['restored']
    assert not ui.calls


def test_arm_restoration_focus_is_locked_and_precedes_panel_or_snapshot(setup):
    _,service,ui,clock=setup;ui.state='idle'
    held=False
    @contextlib.contextmanager
    def locked():
        nonlocal held
        assert not held
        held=True
        try:yield {'active':None}
        finally:held=False
    def focus():
        assert held
        ui.calls.append('focus')
    service.locked=locked;ui.focus_enrolled=focus
    ui.audio_close=lambda:ui.calls.append('close')
    result=restore_arm_audio(service,ui,settings()['armed_id'],clock=clock.time)
    assert result['restored']
    assert ui.calls==['focus','close','snapshot','restore_audio','snapshot']
    assert not held


@pytest.mark.parametrize('reason',['other_arm','missing_identity','missing_routes','already_attempted','missing_focus','missing_identity_reader','changed_lease'])
def test_arm_restoration_refuses_focus_until_lease_and_enrollment_pass(setup,reason):
    _,service,ui,clock=setup;ui.state='idle'
    path=service.home/'incoming-audio-lease.json'
    original=json.loads(path.read_text())
    if reason=='other_arm':original['armed_id']=str(uuid.uuid4())
    if reason=='missing_identity':original.pop('identity')
    if reason=='missing_routes':original.pop('original_routes')
    if reason=='already_attempted':original['restoration_attempted_at']=clock.time()
    if reason=='missing_focus':ui.focus_enrolled=None
    if reason=='missing_identity_reader':ui._identity=None
    write_json(path,original)
    if reason=='changed_lease':
        @contextlib.contextmanager
        def locked():
            write_json(path,dict(original,armed_id=str(uuid.uuid4())))
            yield {'active':None}
        service.locked=locked
    result=restore_arm_audio(service,ui,settings()['armed_id'],clock=clock.time)
    assert not result['restored']
    assert not ui.calls


def test_arm_restoration_still_requires_same_idle_snapshot_after_focus(setup):
    _,service,ui,clock=setup;ui.state='idle'
    def focus():
        ui.calls.append('focus')
        ui.identity='changed-after-focus'
    ui.focus_enrolled=focus
    result=restore_arm_audio(service,ui,settings()['armed_id'],clock=clock.time)
    assert not result['restored']
    assert ui.calls==['focus','snapshot']
    assert 'restoration_attempted_at' not in json.loads((service.home/'incoming-audio-lease.json').read_text())


@pytest.mark.parametrize('stage',['focus','close','snapshot','restore_audio','final_snapshot'])
def test_arm_restoration_records_exact_error_code_and_stage(setup,stage):
    _,service,ui,clock=setup
    ui.state='idle'
    original_snapshot=ui.snapshot
    reads=0
    def fail():
        error=UIError('native_read_unavailable')
        error.args=('private raw exception payload',)
        raise error
    def snapshot():
        nonlocal reads
        reads+=1
        if (stage=='snapshot' and reads==1) or (stage=='final_snapshot' and reads==2):fail()
        return original_snapshot()
    def restore(original):
        ui.calls.append('restore_audio')
        fail()
    ui.snapshot=snapshot
    if stage=='focus':ui.focus_enrolled=fail
    ui.audio_close=fail if stage=='close' else lambda:None
    if stage=='restore_audio':ui.restore_audio=restore
    result=restore_arm_audio(service,ui,settings()['armed_id'],clock=clock.time)
    saved=json.loads((service.home/'incoming-audio-lease.json').read_text())
    for evidence in (result,saved):
        assert evidence['error_type']=='UIError'
        assert evidence['error_code']=='native_read_unavailable'
        assert evidence['error_stage']==stage
        assert 'private raw exception payload' not in json.dumps(evidence)
    assert saved['status']=='cleanup_required'
    expected_mutations=1 if stage in ('restore_audio','final_snapshot') else 0
    assert ui.calls.count('restore_audio')==expected_mutations
    if expected_mutations:
        assert not restore_arm_audio(service,ui,settings()['armed_id'],clock=clock.time)['restored']
        assert ui.calls.count('restore_audio')==1


@pytest.mark.parametrize('code',['private text\ncredentials',{'raw':'data'},'x'*101,None])
def test_restoration_rejects_unsafe_error_codes(setup,code):
    _,service,ui,clock=setup;ui.state='idle'
    def fail():raise UIError(code)
    ui.audio_close=fail
    result=restore_arm_audio(service,ui,settings()['armed_id'],clock=clock.time)
    assert result['error_code']=='UIError'
    assert json.loads((service.home/'incoming-audio-lease.json').read_text())['error_code']=='UIError'


def test_finish_watching_preserves_restoration_error_diagnostics(setup):
    controller,service,ui,clock=setup;ui.state='idle';clock.value=1101
    def fail(original):raise UIError('audio_panel_did_not_close')
    ui.restore_audio=fail
    result=controller.finish_watching(controller.step())
    assert result['phase']=='cleanup_required'
    assert result['error_stage']=='restore_audio'
    assert result['error_code']=='audio_panel_did_not_close'
    saved=json.loads((service.home/'incoming-status.json').read_text())
    assert saved['error_code']==result['error_code']


def test_call_cleanup_preserves_restoration_error_without_retry(setup):
    controller,service,ui,_=setup
    assert controller.step()['phase']=='active'
    def fail(original):
        ui.calls.append('restore_audio')
        raise UIError('audio_selection_not_verified')
    ui.restore_audio=fail;controller.stop_event.set()
    result=controller.step()
    assert result['phase']=='cleanup_required'
    assert result['error_stage']=='restore_audio'
    assert result['error_code']=='audio_selection_not_verified'
    current=json.loads((service.home/'incoming-state.json').read_text())['current']
    assert current['restore_error_type']=='UIError'
    assert current['restore_error_code']=='audio_selection_not_verified'
    assert current['restore_error_stage']=='restore_audio'
    assert controller.step()['phase']=='cleanup_required'
    assert ui.calls.count('restore_audio')==1 and not service.finished


def use_virtual_policy(service):
    for name in ('incoming-audio-lease.json','incoming-settings.json'):
        path=service.home/name
        value=json.loads(path.read_text());value['audio_cleanup_policy']=KEEP_VIRTUAL
        write_json(path,value)


def test_idle_arm_leaves_virtual_defaults_and_preserves_physical_history(setup):
    controller,service,ui,clock=setup;use_virtual_policy(service)
    ui.state='idle';clock.value=1101
    result=controller.finish_watching(controller.step())
    saved=json.loads((service.home/'incoming-audio-lease.json').read_text())
    assert result['phase']=='expired' and lease_released(saved)
    assert saved['status']=='released' and saved['routes_restored'] is False
    assert saved['original_routes']==ui.original
    assert 'retained' in saved['audio_cleanup_evidence']
    assert ui.calls.count('verify_virtual')==1 and 'restore_audio' not in ui.calls
    controller.finish_watching(result)
    assert ui.calls.count('verify_virtual')==1


def test_virtual_defaults_mismatch_never_falls_back_to_physical_restore(setup):
    _,service,ui,clock=setup;use_virtual_policy(service)
    ui.state='idle';ui.restored=False
    result=restore_arm_audio(service,ui,settings()['armed_id'],clock=clock.time)
    saved=json.loads((service.home/'incoming-audio-lease.json').read_text())
    assert not result['restored'] and not lease_released(saved)
    assert saved['status']=='cleanup_required' and saved['original_routes']==ui.original
    assert ui.calls.count('verify_virtual')==1 and 'restore_audio' not in ui.calls
    restore_arm_audio(service,ui,settings()['armed_id'],clock=clock.time)
    assert ui.calls.count('verify_virtual')==1


def test_incoming_call_keeps_policy_snapshot_and_reports_retained_defaults(setup):
    controller,service,ui,_=setup;use_virtual_policy(service)
    assert controller.step()['phase']=='active'
    assert next(value for name,value in service.calls if name=='prepare')['routing_policy']==KEEP_VIRTUAL
    # Configuration changes do not rewrite the in-flight call's routing policy.
    write_json(service.home/'config.json',{'audio_cleanup_policy':'restore_original'})
    evidence=[]
    original_finish=service.finish
    def finish(call_id,owner,hangup,restore,outcome):
        evidence.append(restore);return original_finish(call_id,owner,hangup,restore,outcome)
    service.finish=finish;controller.stop_event.set()
    assert controller.step()['phase']=='call_finished'
    saved=json.loads((service.home/'incoming-audio-lease.json').read_text())
    assert lease_released(saved) and saved['routes_restored'] is False
    assert 'retained' in evidence[0]
    assert 'restore_audio' not in ui.calls and ui.calls.count('verify_virtual')==1
