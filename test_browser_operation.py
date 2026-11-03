"""Offline cross-process browser ownership checks; never contact UI/providers."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
import fcntl
import os
import pytest
import google_voice_ui
import incoming_control
from browser_operation import BrowserOperation,navigation_guard
from service import CallService,CallError,read_json,write_json


@pytest.fixture
def desk(tmp_path):
    config=tmp_path/'config.json'
    write_json(config,{'voice_name':'Elle','reserve_per_minute_usd':1})
    service=CallService(tmp_path/'state',config)
    identity={'app':'Google Chrome','window_id':'w-123','pid':123,
              'process_instance':'fixture-generation','url_prefix':'https://voice.google.com/u/0/',
              'account_label':'Fixture account'}
    write_json(service.home/'google-voice-ui.json',identity|{'incoming_controls_verified':True})
    return service,identity


def call(service):
    return service.prepare('caller','call-key','+15555550123','Offline call','',60,'Offline only',0)


def acquire(service,identity,key='sms-key'):
    browser=BrowserOperation(service)
    with browser.session_lock():return browser.acquire('texter',key,identity=identity)


def test_browser_acquire_requires_session_lock(desk):
    service,identity=desk
    with pytest.raises(CallError,match='session_lock'):BrowserOperation(service).acquire('texter','key',identity=identity)


def test_durable_browser_ownership_survives_exception_and_blocks_calls(desk):
    service,identity=desk;browser=BrowserOperation(service)
    with pytest.raises(RuntimeError):
        with browser.session_lock():
            browser.acquire('texter','key',identity=identity)
            raise RuntimeError('worker crashed after navigation')
    with pytest.raises(CallError,match='Browser is reserved'):call(service)
    with pytest.raises(CallError,match='Browser is reserved'):
        with navigation_guard(service):pytest.fail('No live reader while navigation is unresolved')
    assert browser.status()['status']=='active'


def test_same_active_key_resumes_but_changed_input_cannot(desk):
    service,identity=desk;first=acquire(service,identity)
    again=acquire(service,identity)
    assert again['resumed'] and again['token']==first['token']
    with pytest.raises(CallError,match='reserved'):acquire(service,identity,'other-key')


def test_browser_rejects_current_enrollment_change(desk):
    service,identity=desk
    write_json(service.home/'google-voice-ui.json',identity|{'pid':999})
    with pytest.raises(CallError,match='enrollment changed'):acquire(service,identity)
    assert read_json(service.home/'registry.json',{}).get('active') is None


@pytest.mark.parametrize('kind',['enabled','current','audio_cleanup','watcher','active_call'])
def test_live_sms_rejects_each_voice_owner(desk,kind):
    service,identity=desk;fd=None
    if kind=='enabled':write_json(service.home/'incoming-settings.json',{'enabled':True})
    elif kind=='current':write_json(service.home/'incoming-state.json',{'current':{'call_id':'pending'}})
    elif kind=='audio_cleanup':write_json(service.home/'incoming-audio-lease.json',{'status':'cleanup_required'})
    elif kind=='active_call':call(service)
    else:
        fd=os.open(service.home/'incoming.lock',os.O_CREAT|os.O_RDWR,0o600)
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        with pytest.raises(CallError,match='must wait'):acquire(service,identity)
    finally:
        if fd is not None:os.close(fd)


def test_verified_released_audio_does_not_block_sms(desk):
    service,identity=desk
    write_json(service.home/'incoming-audio-lease.json',{'status':'released','audio_cleanup_policy':'keep_virtual',
        'release_kind':'persistent_virtual_defaults','audio_cleanup_verified':True,'routes_restored':False,
        'audio_cleanup_evidence':'Fixture verified virtual routes'})
    assert acquire(service,identity)['status']=='active'


@pytest.mark.parametrize('defect',['token','owner','identity','empty_proof','outside_lock'])
def test_release_requires_matching_fence_owner_identity_and_proof(desk,defect):
    service,identity=desk;lease=acquire(service,identity);browser=BrowserOperation(service)
    args=[lease['token'],'texter',identity,'Verified same Calls page idle']
    if defect=='token':args[0]='browser_stale'
    elif defect=='owner':args[1]='other'
    elif defect=='identity':args[2]=identity|{'window_id':'w-999'}
    elif defect=='empty_proof':args[3]=''
    if defect=='outside_lock':
        with pytest.raises(CallError):browser.release(*args)
    else:
        with browser.session_lock():
            with pytest.raises(CallError):browser.release(*args)
    assert browser.status()['token']==lease['token']


def test_verified_release_preserves_history_and_allows_call(desk):
    service,identity=desk;lease=acquire(service,identity);browser=BrowserOperation(service)
    with browser.session_lock():
        released=browser.release(lease['token'],'texter',identity,'Verified same Calls page idle')
    assert released['status']=='released'
    registry=read_json(service.home/'registry.json')
    assert registry['browser_operation'] is None
    assert registry['browser_operations'][lease['token']]['restoration_evidence']=='Verified same Calls page idle'
    with pytest.raises(CallError,match='already released'):acquire(service,identity)
    assert call(service)['phase']=='prepared'


def test_incoming_arm_rejects_browser_owner_before_focus(desk,monkeypatch):
    service,identity=desk;acquire(service,identity)
    no_ui=SimpleNamespace(focus_enrolled=lambda:pytest.fail('Must not focus'),
                          snapshot=lambda:pytest.fail('Must not inspect'))
    monkeypatch.setattr(google_voice_ui,'GoogleVoiceUI',lambda _:no_ui)
    with pytest.raises(CallError,match='Browser is reserved'):
        incoming_control.arm(service,'caller',['+15555550123'],'Offline only','Offline only',0)
    assert not (service.home/'incoming-audio-lease.json').exists()


def test_stop_is_available_with_unresolved_sms_reservation(desk):
    service,identity=desk;lease=acquire(service,identity)
    assert incoming_control.disarm(service)['stop_requested'] is True
    assert BrowserOperation(service).status()['token']==lease['token']


def test_call_and_sms_race_has_exactly_one_winner(desk):
    service,identity=desk;barrier=Barrier(2)
    def claim_call():
        barrier.wait()
        try:call(service);return 'call'
        except CallError:return 'rejected'
    def claim_sms():
        barrier.wait()
        try:acquire(service,identity);return 'sms'
        except CallError:return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(claim_call),executor.submit(claim_sms)]
        result=[f.result(timeout=5) for f in futures]
    assert result.count('rejected')==1
    registry=read_json(service.home/'registry.json')
    assert bool(registry.get('active')) != bool(registry.get('browser_operation'))


def test_shared_session_lock_serializes_live_navigation(desk):
    service,identity=desk;browser=BrowserOperation(service)
    with browser.session_lock():
        with pytest.raises(CallError,match='in progress'):
            with navigation_guard(service):pytest.fail('Must not run concurrently')
        with pytest.raises(CallError,match='in progress'):acquire(service,identity)
