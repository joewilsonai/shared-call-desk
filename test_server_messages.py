"""MCP bindings preserve exact requests; all services replaced, no live state."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def adapter(monkeypatch):
    import service
    sentinel=object()
    monkeypatch.setattr(service,'CallService',lambda:sentinel)
    spec=importlib.util.spec_from_file_location('server_message_test',Path(__file__).with_name('server.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    calls=[]
    class Fake:
        def __getattr__(self,name):
            def method(*args,**kwargs):calls.append((name,args,kwargs));return {'operation':name}
            return method
    monkeypatch.setattr(module,'text_service',lambda:Fake())
    return module,calls


def test_cached_reads_default_to_no_live_refresh(adapter):
    server,calls=adapter
    server.text_inbox();server.text_read('conversation');server.text_status('draft')
    assert [item[2]['refresh'] for item in calls]==[False,False,False]


def test_exact_body_and_reply_binding_survive_mcp_adapter(adapter):
    server,calls=adapter
    body='  Preserve\n"this" \\ $() text 🌙  '
    server.text_draft('owner','key','+15555550123',body,'incoming-id')
    assert calls==[('draft',('owner','key','+15555550123',body),{'reply_to_message_id':'incoming-id'})]


def test_send_never_infers_authorization_or_digest(adapter):
    server,calls=adapter
    with pytest.raises(TypeError):server.text_send('draft','owner','digest')
    assert calls==[]
    server.text_send('draft','owner','exact-digest','Exact user approval')
    assert calls==[('send',('draft','owner','exact-digest','Exact user approval'),{})]


def test_recovery_requires_explicit_termination_evidence(adapter):
    server,calls=adapter
    with pytest.raises(TypeError):server.text_recover_browser('token','owner')
    assert calls==[]
    server.text_recover_browser('token','owner','Prior process and its helpers ended; observed proof.')
    assert calls==[('recover_browser',('token','owner','Prior process and its helpers ended; observed proof.'),{})]


def test_blocked_send_returns_specific_error_and_blocking_draft(adapter,monkeypatch):
    from messaging import TextError
    server,_=adapter
    class Blocked:
        def send(self,*args,**kwargs):
            error=TextError('prior_send_outcome_unresolved');error.blocking_draft_id='text_previous';raise error
    monkeypatch.setattr(server,'text_service',lambda:Blocked())
    result=server.text_send('draft','owner','digest','Authorized exact message')
    assert result['ok'] is False
    assert result['error']['code']=='prior_send_outcome_unresolved'
    assert result['error']['blocking_draft_id']=='text_previous'
    assert result['error']['automatic_resend_allowed'] is False


def test_held_reservation_exposes_specific_code_instead_of_callerror(adapter,monkeypatch):
    from browser_operation import require_browser_available
    server,_=adapter
    class Blocked:
        def send(self,*args,**kwargs):
            require_browser_available({'browser_operation':{'owner':'luna','token':'browser_fixture'}})
    monkeypatch.setattr(server,'text_service',lambda:Blocked())
    result=server.text_send('draft','owner','digest','Authorized exact message')
    assert result['ok'] is False
    assert result['error']['code']=='browser_operation_active'
    assert result['error']['automatic_resend_allowed'] is False
