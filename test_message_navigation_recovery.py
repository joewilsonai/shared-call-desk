"""Offline regressions for delayed AX navigation and reservation errors."""
import pytest
from google_voice_messages import GoogleVoiceMessages
from messaging import TextError
from test_google_voice_messages import Native


def timing(monkeypatch):
    elapsed=[0.]
    monkeypatch.setattr('google_voice_messages.time.monotonic',lambda:elapsed[0])
    monkeypatch.setattr('google_voice_messages.time.sleep',lambda seconds:elapsed.__setitem__(0,elapsed[0]+seconds))
    return elapsed


def test_page_transition_can_settle_after_three_seconds_without_reclick(tmp_path,monkeypatch):
    elapsed=timing(monkeypatch);ui=Native();adapter=GoogleVoiceMessages(tmp_path,ui=ui)
    original=adapter._read;stale=original()
    monkeypatch.setattr(adapter,'_read',lambda:stale if ui.state=='calls' and elapsed[0]<3 else original())
    adapter._click_named('Calls','radiobutton')
    assert adapter._read()[0]['name']=='Voice - Calls'
    assert ui.operations==['Calls']
    assert 3<=elapsed[0]<16


def test_idle_panel_can_settle_after_three_seconds_without_reclick(tmp_path,monkeypatch):
    elapsed=timing(monkeypatch);ui=Native();adapter=GoogleVoiceMessages(tmp_path,ui=ui)
    original=ui.snapshot
    monkeypatch.setattr(ui,'snapshot',lambda:original() if elapsed[0]>=3 else {'coverage':'complete','state':'unknown'})
    assert adapter.restore_calls()['state']=='idle'
    assert ui.operations==['Calls'] and 3<=elapsed[0]<16


def test_source_timeout_cleanup_verifies_settings_number_and_returns_calls(tmp_path,monkeypatch):
    timing(monkeypatch);ui=Native();ui.state='settings';adapter=GoogleVoiceMessages(tmp_path,ui=ui)
    assert adapter.restore_after_source_timeout()['state']=='idle'
    assert ui.operations==['Close','Calls']
    assert ui.setter_requests==[]


def test_source_timeout_cleanup_refuses_changed_line_without_navigation(tmp_path,monkeypatch):
    timing(monkeypatch);ui=Native();ui.state='settings';ui.config['voice_number']='+12025550999'
    adapter=GoogleVoiceMessages(tmp_path,ui=ui)
    with pytest.raises(TextError,match='message_source_line_changed'):adapter.restore_after_source_timeout()
    assert ui.operations==[]


def test_navigation_wait_expires_without_replaying_action(tmp_path,monkeypatch):
    elapsed=timing(monkeypatch);ui=Native();adapter=GoogleVoiceMessages(tmp_path,ui=ui)
    stale=adapter._read()
    monkeypatch.setattr(adapter,'_read',lambda:stale)
    with pytest.raises(TextError,match='message_navigation_not_settled'):adapter._wait_view('calls')
    assert 14<=elapsed[0]<=16 and ui.operations==[]


def test_source_timeout_cleanup_refuses_unverified_nonsettings_page(tmp_path,monkeypatch):
    timing(monkeypatch);ui=Native();adapter=GoogleVoiceMessages(tmp_path,ui=ui)
    with pytest.raises(TextError,match='message_source_unverified_before_cleanup'):adapter.restore_after_source_timeout()
    assert ui.operations==[]
