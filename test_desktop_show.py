"""Offline Show Google Voice recovery; no live browser/native commands."""
import fcntl
import json
import os
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import desktop_api as api
import google_voice_ui
from service import CallError,read_json,write_json


@pytest.fixture
def setup(tmp_path,monkeypatch):
    enrollment={'app':'Google Chrome','window_id':'w-4349','pid':669,
                'process_instance':'fixture-generation','url_prefix':'https://voice.google.com/u/0/',
                'account_label':'Fixture account'}
    write_json(tmp_path/'google-voice-ui.json',enrollment)
    service=SimpleNamespace(home=tmp_path,locked=lambda:nullcontext({}))
    ui=SimpleNamespace(session='incoming-google-voice',events=[])
    ui._identity=lambda:dict(enrollment)
    ui.focus_enrolled=lambda:ui.events.append(('focus',ui.session))
    def snapshot():
        ui.events.append(('snapshot',ui.session))
        return {'identity':dict(enrollment),'coverage':'complete','state':'idle','evidence':'Verified enrolled page'}
    ui.snapshot=snapshot
    monkeypatch.setattr(google_voice_ui,'GoogleVoiceUI',lambda _:ui)
    monkeypatch.setattr(api,'desktop_status',lambda _:{'overall':{'state':'off'},'checks':[]})
    return service,ui,enrollment


def test_show_focuses_then_verifies_in_separate_ref_namespace(setup):
    service,ui,enrollment=setup
    assert api.show_voice(service)['overall']['state']=='off'
    assert ui.events==[('focus','call-desk-show'),('snapshot','call-desk-show')]
    assert read_json(service.home/'google-voice-ui.json')==enrollment
    assert read_json(service.home/'desktop-health.json')['browser']['state']=='idle'


def test_failed_focus_never_reads_or_reenrolls(setup):
    service,ui,enrollment=setup
    def failed():raise google_voice_ui.UIError('native_window_focus_unverified')
    ui.focus_enrolled=failed
    with pytest.raises(google_voice_ui.UIError,match='focus_unverified'):api.show_voice(service)
    assert ui.events==[]
    assert read_json(service.home/'google-voice-ui.json')==enrollment
    assert not (service.home/'desktop-health.json').exists()


@pytest.mark.parametrize('reason',['google_voice_tab_changed','google_voice_account_unverified',
                                    'google_voice_window_unavailable','PERM_DENIED'])
def test_page_account_and_stale_window_fail_without_navigation(setup,reason):
    service,ui,enrollment=setup
    ui.snapshot=lambda:{'coverage':'partial','state':'unknown','evidence':reason}
    with pytest.raises(google_voice_ui.UIError,match=reason):api.show_voice(service)
    assert read_json(service.home/'google-voice-ui.json')==enrollment
    assert not (service.home/'desktop-health.json').exists()


def test_mismatched_verified_identity_is_rejected(setup):
    service,ui,_=setup
    ui.snapshot=lambda:{'coverage':'complete','identity':{'window_id':'w-99'}}
    with pytest.raises(google_voice_ui.UIError,match='identity_changed'):api.show_voice(service)


def test_external_setup_lock_rejects_show_without_focus(setup):
    service,ui,_=setup
    fd=os.open(service.home/'incoming-control.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(CallError,match='in progress'):api.show_voice(service)
        assert ui.events==[]
    finally:os.close(fd)


def test_show_does_not_clear_unrelated_arm_error(setup):
    service,_,_=setup
    write_json(service.home/'desktop-last-action.json',{'action':'arm','ok':False,'error':'Routing setup failed'})
    result=api.record_success(service,'show-voice',{'checks':[{'id':'browser','state':'ok'}]})
    assert result['last_error']=='Routing setup failed'


def test_successful_show_can_clear_its_own_previous_error(setup):
    service,_,_=setup
    write_json(service.home/'desktop-last-action.json',{'action':'show-voice','ok':False,'error':'Window unavailable'})
    result=api.record_success(service,'show-voice',{'checks':[{'id':'browser','state':'ok'}]})
    assert result['last_error'] is None


def test_cli_dispatches_show_without_falling_through_to_disarm(setup,monkeypatch,capsys):
    service,ui,_=setup
    monkeypatch.setattr(api,'CallService',lambda:service)
    monkeypatch.setattr(sys,'argv',['desktop_api.py','show-voice'])
    monkeypatch.setattr(api.incoming_control,'disarm',lambda _:pytest.fail('Show must not disarm'))
    assert api.main()==0
    assert json.loads(capsys.readouterr().out)['overall']['state']=='off'
    assert ui.events[-1]==('snapshot','call-desk-show')
