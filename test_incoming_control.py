from pathlib import Path
import contextlib
from types import SimpleNamespace
import threading
import uuid
import time

import pytest

import google_voice_ui
import incoming_control
from incoming import IncomingController,run_watcher
from service import CallError,CallService,read_json,write_json
from audio_policy import KEEP_VIRTUAL,VIRTUAL_ROUTES,lease_released


@pytest.fixture
def service(tmp_path):
    config=tmp_path/'config.json'
    write_json(config,{'voice_name':'Elle','reserve_per_minute_usd':1,'provider_overrides_verified_at':'offline-test'})
    return CallService(tmp_path/'state',config)


def policy(owner='test'):
    return {'enabled':True,'armed_id':str(uuid.uuid4()),'mode':'allowlist','owner':owner,
            'allowed_numbers':['+15555550123'],'brief':'Offline test','context':'',
            'authorization':'Offline test only','other_spend_today_usd':0,
            'max_seconds':60,'max_calls':1,'expires_at':4102444800,
            'poll_seconds':.1,'readiness_seconds':20}


class NoUI:
    def __init__(self):self.calls=0
    def snapshot(self):self.calls+=1;raise AssertionError('UI must not be used')


def install_fake_ui(service,monkeypatch):
    write_json(service.home/'google-voice-ui.json',{'incoming_controls_verified':True})
    original={'Microphone':'Physical microphone','Speakers':'Physical speakers'}
    ui=SimpleNamespace(calls=[],original=original)
    ui._identity=lambda:'test-window'
    ui.focus_enrolled=lambda:None
    ui.snapshot=lambda:{'coverage':'complete','state':'idle','identity':'test-window','observed_at':time.time(),'evidence':'Fresh fake idle window'}
    ui.routes=lambda:dict(original)
    def prepare():
        ui.calls.append('prepare')
        assert read_json(service.home/'incoming-audio-lease.json')['original_routes']==original
        return {'verified':True,'original_routes':dict(original),'evidence':'Fake prepared routes'}
    def restore(routes):
        ui.calls.append('restore');assert routes==original
        return {'verified':True,'evidence':'Fake restored original routes'}
    ui.prepare_audio=prepare;ui.restore_audio=restore
    def verify(routes):
        assert routes==VIRTUAL_ROUTES;ui.calls.append('verify_virtual')
        return {'verified':True,'actual_routes':dict(VIRTUAL_ROUTES),'evidence':'Fake virtual defaults observed'}
    ui.verify_audio_routes=verify
    monkeypatch.setattr(google_voice_ui,'GoogleVoiceUI',lambda _:ui)
    return ui


def invoke_arm(service):
    return incoming_control.arm(service,'test',['+15555550123'],'Offline test','User-authorized offline setup',0)


def test_enabled_old_arm_blocks_retry_even_with_historical_status(service,monkeypatch):
    old=policy();write_json(service.home/'incoming-settings.json',old)
    write_json(service.home/'incoming-status.json',{'phase':'call_limit_reached','updated_at':1,'armed_id':str(uuid.uuid4())})
    spawned=[]
    monkeypatch.setattr(incoming_control.subprocess,'Popen',lambda *a,**k:spawned.append(a))
    with pytest.raises(CallError,match='explicitly disarm'):invoke_arm(service)
    assert not spawned
    assert read_json(service.home/'incoming-settings.json')['armed_id']==old['armed_id']


def test_pending_child_cannot_allow_second_launch(service,monkeypatch):
    install_fake_ui(service,monkeypatch)
    commands=[]
    def spawn(args,**kwargs):commands.append(args);return SimpleNamespace(pid=12345)
    monkeypatch.setattr(incoming_control.subprocess,'Popen',spawn)
    first=invoke_arm(service)
    configured=read_json(service.home/'incoming-settings.json')
    assert first['phase']=='launch_pending' and not first['receipt_matches_arm']
    assert commands[0][commands[0].index('--armed-id')+1]==configured['armed_id']
    with pytest.raises(CallError,match='explicitly disarm'):invoke_arm(service)
    assert len(commands)==1


def test_arm_focuses_before_first_snapshot(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch);events=[]
    original_snapshot=ui.snapshot
    ui.focus_enrolled=lambda:events.append('focus')
    ui.snapshot=lambda:(events.append('snapshot') or original_snapshot())
    monkeypatch.setattr(incoming_control.subprocess,'Popen',lambda *a,**k:SimpleNamespace(pid=12345))
    invoke_arm(service)
    assert events[0:2]==['focus','snapshot']
    assert events.count('focus')==1


def test_reserved_call_blocks_focus_and_snapshot(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    ui.focus_enrolled=lambda:pytest.fail('A reserved call must not be focused by incoming arm')
    ui.snapshot=lambda:pytest.fail('A reserved call must not be inspected by incoming arm')
    with service.locked() as registry:registry['active']='existing-call'
    with pytest.raises(CallError,match='already reserved'):invoke_arm(service)
    assert not (service.home/'incoming-audio-lease.json').exists()


def test_unverified_focus_aborts_before_snapshot_routes_or_launch(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    def fail_focus():raise google_voice_ui.UIError('native_window_focus_unverified')
    ui.focus_enrolled=fail_focus
    ui.snapshot=lambda:pytest.fail('Cannot inspect after failed exact focus')
    monkeypatch.setattr(incoming_control.subprocess,'Popen',lambda *a,**k:pytest.fail('Cannot launch'))
    with pytest.raises(google_voice_ui.UIError,match='focus_unverified'):invoke_arm(service)
    assert ui.calls==[] and not (service.home/'incoming-audio-lease.json').exists()


def test_stop_during_focus_cancels_before_first_snapshot(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    ui.focus_enrolled=lambda:write_json(service.home/'incoming-disarm-request.json',{'token':'new-stop'})
    ui.snapshot=lambda:pytest.fail('Stop during focus must abort before snapshot')
    with pytest.raises(CallError,match='canceled'):invoke_arm(service)
    assert not (service.home/'incoming-audio-lease.json').exists()


def test_disarm_then_explicit_rearm_gets_new_child_identity(service,monkeypatch):
    install_fake_ui(service,monkeypatch)
    commands=[]
    def spawn(args,**kwargs):commands.append(args);return SimpleNamespace(pid=12345+len(commands))
    monkeypatch.setattr(incoming_control.subprocess,'Popen',spawn)
    invoke_arm(service);first=read_json(service.home/'incoming-settings.json')['armed_id']
    incoming_control.disarm(service)
    invoke_arm(service);second=read_json(service.home/'incoming-settings.json')['armed_id']
    assert first!=second and len(commands)==2


def test_delayed_old_child_rejects_new_arm_before_ui_or_status_write(service):
    new=policy();write_json(service.home/'incoming-settings.json',new)
    receipt={'phase':'watching','armed_id':new['armed_id'],'updated_at':123}
    write_json(service.home/'incoming-status.json',receipt)
    ui=NoUI()
    result=run_watcher(service,ui,expected_armed_id=str(uuid.uuid4()))
    assert result['phase']=='arm_mismatch' and ui.calls==0
    assert read_json(service.home/'incoming-status.json')==receipt


def test_matching_but_disarmed_child_exits_without_ui(service):
    new=policy();new['enabled']=False;write_json(service.home/'incoming-settings.json',new)
    ui=NoUI()
    result=run_watcher(service,ui,expected_armed_id=uuid.UUID(new['armed_id']).hex)
    assert result['phase']=='disabled' and ui.calls==0
    assert result['armed_id']==new['armed_id']


def test_bound_controller_never_adopts_replacement_arm(service):
    old=policy();write_json(service.home/'incoming-settings.json',old)
    ui=NoUI();controller=IncomingController(service,ui,expected_armed_id=old['armed_id'])
    write_json(service.home/'incoming-settings.json',policy())
    assert controller.step()['phase']=='invalid_settings' and ui.calls==0


def prepare_current(service,direction='incoming',owner='test'):
    settings=policy(owner)
    if direction=='incoming':
        write_json(service.home/'incoming-settings.json',settings)
        call=service.prepare_incoming(owner,'one','+15555550123','Offline test','',60,'offline',0,'ring-offline')
    else:
        call=service.prepare(owner,'one','+15555550123','Offline test','',60,'offline',0)
        write_json(service.home/'incoming-settings.json',settings)
    current={'call_id':call['call_id'],'owner':owner,'armed_id':settings['armed_id'],'direction':direction}
    write_json(service.home/'incoming-state.json',{'current':current})
    return call['call_id'],settings,current


def test_disarm_directly_stops_matching_incoming_even_without_watcher(service):
    call,_,_=prepare_current(service)
    assert not incoming_control.watcher_running(service.home)
    result=incoming_control.disarm(service)
    assert result['voice_stop_call_id']==call
    assert (service.directory(call)/'STOP').exists()
    assert not read_json(service.home/'incoming-settings.json')['enabled']
    assert service.status(call)['stop_requested_at']


def test_disarm_never_stops_outgoing_call(service):
    call,_,_=prepare_current(service,direction='outgoing')
    result=incoming_control.disarm(service)
    assert result['voice_stop_call_id'] is None and not (service.directory(call)/'STOP').exists()


@pytest.mark.parametrize('mismatch',['owner','arm','call_id'])
def test_disarm_rejects_mismatched_current_binding(service,mismatch):
    call,settings,current=prepare_current(service)
    if mismatch=='owner':current['owner']='another-agent'
    elif mismatch=='arm':current['armed_id']=str(uuid.uuid4())
    else:current['call_id']='call_'+uuid.uuid4().hex
    write_json(service.home/'incoming-state.json',{'current':current})
    result=incoming_control.disarm(service)
    assert result['voice_stop_call_id'] is None and not (service.directory(call)/'STOP').exists()


def test_stale_status_cannot_impersonate_current_arm(service):
    settings=policy();write_json(service.home/'incoming-settings.json',settings)
    write_json(service.home/'incoming-status.json',{'armed_id':str(uuid.uuid4()),'phase':'active','call_id':'old-private-call','updated_at':1})
    result=incoming_control.status(service)
    assert result['phase']=='launch_pending' and result['call_id'] is None
    assert not result['receipt_matches_arm'] and result['status_age_seconds'] is None


def test_arm_prepares_routes_before_spawn_without_reserving_a_call(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    def spawn(*args,**kwargs):
        lease=read_json(service.home/'incoming-audio-lease.json')
        assert lease['status']=='prepared' and ui.calls==['prepare']
        assert read_json(service.home/'registry.json')['active'] is None
        return SimpleNamespace(pid=1234)
    monkeypatch.setattr(incoming_control.subprocess,'Popen',spawn)
    invoke_arm(service)
    assert read_json(service.home/'incoming-audio-lease.json')['original_routes']==ui.original


def test_failed_launch_restores_routes_and_disables_arm(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    def fail(*args,**kwargs):raise OSError('fake launch failure')
    monkeypatch.setattr(incoming_control.subprocess,'Popen',fail)
    with pytest.raises(OSError):invoke_arm(service)
    assert ui.calls==['prepare','restore']
    assert not read_json(service.home/'incoming-settings.json')['enabled']
    assert read_json(service.home/'incoming-audio-lease.json')['status']=='restored'


def test_route_claim_blocks_outgoing_during_preparation(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    original_prepare=ui.prepare_audio
    def prepare():
        with pytest.raises(CallError,match='owns the audio routes'):
            service.prepare('other','outgoing','+15555550123','test','',60,'authorized',0)
        return original_prepare()
    ui.prepare_audio=prepare
    monkeypatch.setattr(incoming_control.subprocess,'Popen',lambda *a,**k:SimpleNamespace(pid=1234))
    invoke_arm(service)


def test_public_arm_rejects_multiple_calls_for_initial_version(service):
    with pytest.raises(CallError,match='one call per arm'):
        incoming_control.arm(service,'test',['+15555550123'],'test','authorized',0,max_calls=2)


def test_unclean_lease_blocks_outgoing_even_when_disarmed(service):
    write_json(service.home/'incoming-audio-lease.json',{'status':'cleanup_required'})
    with pytest.raises(CallError,match='owns the audio routes'):
        service.prepare('other','outgoing','+15555550123','test','',60,'authorized',0)


def test_partial_arm_preparation_restores_previously_saved_originals(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    def fail():
        assert read_json(service.home/'incoming-audio-lease.json')['original_routes']==ui.original
        assert read_json(service.home/'incoming-audio-lease.json')['mutation_started'] is True
        ui.calls.append('partial_prepare')
        raise RuntimeError('Fake partial route mutation')
    ui.prepare_audio=fail
    spawned=[]
    monkeypatch.setattr(incoming_control.subprocess,'Popen',lambda *a,**k:spawned.append(a))
    with pytest.raises(RuntimeError):invoke_arm(service)
    assert ui.calls==['partial_prepare','restore'] and not spawned
    assert read_json(service.home/'incoming-audio-lease.json')['status']=='restored'
    assert not read_json(service.home/'incoming-settings.json')['enabled']


def test_failed_arm_restoration_is_visible_and_does_not_repeat(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    monkeypatch.setattr(incoming_control.subprocess,'Popen',lambda *a,**k:SimpleNamespace(pid=1234))
    invoke_arm(service)
    def fail_restore(routes):
        ui.calls.append('failed_restore')
        return {'verified':False}
    ui.restore_audio=fail_restore
    first=incoming_control.disarm(service)
    second=incoming_control.disarm(service)
    assert first['phase']==second['phase']=='cleanup_required'
    assert ui.calls.count('failed_restore')==1


def test_system_default_endpoint_drift_during_arm_is_equivalent(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    original={'Microphone':'Physical microphone','Speakers':'Default - AirPods (Bluetooth)'}
    ui.routes=lambda:dict(original)
    ui.prepare_audio=lambda:{'verified':True,'original_routes':{**original,'Speakers':'Default - MacBook Pro Speakers (Built-in)'},'evidence':'Default endpoint changed'}
    monkeypatch.setattr(incoming_control.subprocess,'Popen',lambda *a,**k:SimpleNamespace(pid=1234))
    invoke_arm(service)
    assert read_json(service.home/'incoming-audio-lease.json')['original_routes']==original


def wait_until(predicate,seconds=2):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.005)
    assert predicate(), 'Concurrent test condition did not complete'


def threaded(function,result,key):
    def run():
        try:result[key]=function()
        except BaseException as exc:result[key]=exc
    thread=threading.Thread(target=run,daemon=True);thread.start();return thread


def test_stop_during_slow_arm_publishes_cancel_then_prevents_spawn(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    entered,release=threading.Event(),threading.Event()
    original_prepare=ui.prepare_audio
    def slow_prepare():
        entered.set();assert release.wait(2)
        return original_prepare()
    ui.prepare_audio=slow_prepare
    spawned=[];results={}
    monkeypatch.setattr(incoming_control.subprocess,'Popen',lambda *a,**k:spawned.append(a) or SimpleNamespace(pid=1234))
    armer=threaded(lambda:invoke_arm(service),results,'arm')
    assert entered.wait(2)
    stopper=threaded(lambda:incoming_control.disarm(service),results,'stop')
    try:
        wait_until(lambda:(service.home/'incoming-disarm-request.json').exists())
        assert stopper.is_alive()  # Waiting for native preparation's control lock.
    finally:release.set()
    armer.join(3);stopper.join(3)
    assert not armer.is_alive() and not stopper.is_alive()
    assert isinstance(results['arm'],CallError) and 'canceled' in str(results['arm'])
    assert not isinstance(results['stop'],BaseException)
    assert spawned==[] and ui.calls==['prepare','restore']
    assert not read_json(service.home/'incoming-settings.json')['enabled']
    assert read_json(service.home/'incoming-audio-lease.json')['status']=='restored'


def test_active_voice_stops_before_disarm_can_acquire_control_lock(service):
    call,_,_=prepare_current(service)
    results={}
    with incoming_control.lock(service.home):
        stopper=threaded(lambda:incoming_control.disarm(service),results,'stop')
        wait_until(lambda:(service.directory(call)/'STOP').exists())
        assert stopper.is_alive()
        assert (service.home/'incoming-disarm-request.json').exists()
    stopper.join(2)
    assert not stopper.is_alive() and not isinstance(results['stop'],BaseException)
    assert results['stop']['voice_stop_call_id']==call


def test_historical_stop_receipt_does_not_cancel_new_explicit_arm(service,monkeypatch):
    incoming_control.disarm(service)
    old_token=read_json(service.home/'incoming-disarm-request.json')['token']
    install_fake_ui(service,monkeypatch)
    monkeypatch.setattr(incoming_control.subprocess,'Popen',lambda *a,**k:SimpleNamespace(pid=1234))
    invoke_arm(service)
    assert read_json(service.home/'incoming-settings.json')['enabled']
    assert read_json(service.home/'incoming-disarm-request.json')['token']==old_token


def test_delayed_old_stop_does_not_disable_arm_started_after_its_receipt(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    arrived,release=threading.Event(),threading.Event()
    real_lock=incoming_control.lock
    @contextlib.contextmanager
    def delayed_lock(home):
        if threading.current_thread().name=='delayed-stop':
            arrived.set();assert release.wait(2)
        with real_lock(home):yield
    monkeypatch.setattr(incoming_control,'lock',delayed_lock)
    monkeypatch.setattr(incoming_control.subprocess,'Popen',lambda *a,**k:SimpleNamespace(pid=1234))
    results={}
    def stop():
        try:results['stop']=incoming_control.disarm(service)
        except BaseException as exc:results['stop']=exc
    stopper=threading.Thread(target=stop,name='delayed-stop',daemon=True);stopper.start()
    assert arrived.wait(2)
    try:
        invoke_arm(service)
        armed_id=read_json(service.home/'incoming-settings.json')['armed_id']
    finally:release.set()
    stopper.join(2)
    assert not stopper.is_alive() and not isinstance(results['stop'],BaseException)
    current=read_json(service.home/'incoming-settings.json')
    assert current['armed_id']==armed_id and current['enabled']
    assert ui.calls==['prepare']


def test_original_route_read_failure_releases_without_route_restoration(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    def fail_read():
        lease=read_json(service.home/'incoming-audio-lease.json')
        assert lease['status']=='reading_originals' and lease['mutation_started'] is False
        ui.calls.append('read_failed')
        raise google_voice_ui.UIError('audio_settings_did_not_open')
    ui.routes=fail_read
    ui.audio_close=lambda:ui.calls.append('close_panel')
    spawned=[]
    monkeypatch.setattr(incoming_control.subprocess,'Popen',lambda *a,**k:spawned.append(a))
    with pytest.raises(google_voice_ui.UIError,match='audio_settings_did_not_open'):
        invoke_arm(service)
    lease=read_json(service.home/'incoming-audio-lease.json')
    assert lease['status']=='restored' and lease['release_kind']=='read_only_preparation'
    assert lease['original_routes'] is None and lease['mutation_started'] is False
    assert ui.calls==['read_failed','close_panel'] and not spawned
    assert not read_json(service.home/'incoming-settings.json')['enabled']


def test_read_only_cleanup_keeps_lease_if_idle_identity_is_uncertain(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    good_snapshot=ui.snapshot
    def fail_read():
        ui.snapshot=lambda:{**good_snapshot(),'identity':'different-window'}
        raise google_voice_ui.UIError('audio_route_not_readable')
    ui.routes=fail_read;ui.audio_close=lambda:ui.calls.append('close_panel')
    with pytest.raises(google_voice_ui.UIError):invoke_arm(service)
    lease=read_json(service.home/'incoming-audio-lease.json')
    assert lease['status']=='cleanup_required' and lease['mutation_started'] is False
    assert ui.calls==['close_panel']
    assert incoming_control.status(service)['phase']=='cleanup_required'


@pytest.mark.parametrize('flag',[True,None])
def test_uncertain_or_started_mutation_cannot_use_read_only_release(service,monkeypatch,flag):
    ui=install_fake_ui(service,monkeypatch)
    armed_id=str(uuid.uuid4())
    lease={'armed_id':armed_id,'status':'preparing','identity':'test-window','original_routes':None}
    if flag is not None:lease['mutation_started']=flag
    write_json(service.home/'incoming-audio-lease.json',lease)
    result=incoming_control.cleanup_preparation(service,ui,armed_id)
    assert result['restored'] is False
    assert read_json(service.home/'incoming-audio-lease.json')['status']!='restored'
    assert ui.calls==[]


def test_keep_virtual_arm_and_disarm_releases_without_physical_restore(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    config=read_json(service.config_path);config['audio_cleanup_policy']=KEEP_VIRTUAL;write_json(service.config_path,config)
    monkeypatch.setattr(incoming_control.subprocess,'Popen',lambda *a,**k:SimpleNamespace(pid=1234))
    invoke_arm(service)
    assert read_json(service.home/'incoming-audio-lease.json')['audio_cleanup_policy']==KEEP_VIRTUAL
    # A later config edit must not change this arm's cleanup target.
    config['audio_cleanup_policy']='restore_original';write_json(service.config_path,config)
    result=incoming_control.disarm(service)
    lease=read_json(service.home/'incoming-audio-lease.json')
    assert lease_released(lease) and lease['original_routes']==ui.original
    assert lease['routes_restored'] is False and ui.calls==['prepare','verify_virtual']
    assert result['idle_arm_restoration']['released'] is True
    assert result['idle_arm_restoration']['restored'] is False
    assert incoming_control.status(service)['phase']!='cleanup_required'


def test_next_arm_archives_previous_physical_routes(service,monkeypatch):
    ui=install_fake_ui(service,monkeypatch)
    previous={'armed_id':str(uuid.uuid4()),'status':'restored','original_routes':dict(ui.original),'restored_at':100}
    write_json(service.home/'incoming-audio-lease.json',previous)
    config=read_json(service.config_path);config['audio_cleanup_policy']=KEEP_VIRTUAL;write_json(service.config_path,config)
    monkeypatch.setattr(incoming_control.subprocess,'Popen',lambda *a,**k:SimpleNamespace(pid=1234))
    invoke_arm(service)
    archived=read_json(service.home/'audio-route-history'/(previous['armed_id']+'.json'))
    assert archived==previous
    assert read_json(service.home/'incoming-audio-lease.json')['armed_id']!=previous['armed_id']

def test_status_uses_config_policy_only_when_no_lease_exists(service):
    config=read_json(service.config_path);config['audio_cleanup_policy']=KEEP_VIRTUAL;write_json(service.config_path,config)
    result=incoming_control.status(service)
    assert result['audio_cleanup_policy']==KEEP_VIRTUAL
    assert result['audio_lease_status'] is None and result['audio_cleanup_evidence'] is None
    write_json(service.home/'incoming-audio-lease.json',{'status':'restored','audio_cleanup_policy':'restore_original'})
    assert incoming_control.status(service)['audio_cleanup_policy']=='restore_original'
