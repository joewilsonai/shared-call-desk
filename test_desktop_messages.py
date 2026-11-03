"""Desktop transport tests: temporary files, no browser, no SMS sends."""
import io
import json
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import desktop_api as api
import google_voice_ui
import messaging
from service import CallService,CallError,read_json,write_json


def test_private_stdin_preserves_message_exactly():
    value={'recipient':'+15555550123','body':'  First line\n"Quoted" \\ $() `literal` 🌙  '}
    raw=json.dumps(value,ensure_ascii=False).encode()
    assert api.read_text_payload(io.BytesIO(raw))==value


@pytest.mark.parametrize('raw',[b'[]',b'null',b'{"limit":NaN}',b'\xff',b'{',b' ' * 65_537])
def test_bad_stdin_is_rejected_without_echoing_contents(raw):
    with pytest.raises(ValueError) as error:api.read_text_payload(io.BytesIO(raw))
    assert str(error.value) in {
        'The messaging request is too large.',
        'The messaging request must be valid JSON.',
        'The messaging request must be a JSON object.',
    }


def test_send_requires_explicit_authorization_argument(monkeypatch):
    calls=[]
    class Fake:
        def __init__(self,_):pass
        def send(self,draft_id,owner,expected_digest,authorization):
            calls.append((draft_id,owner,expected_digest,authorization));return {'phase':'send_unknown'}
    monkeypatch.setattr(messaging,'TextService',Fake)
    payload={'draft_id':'existing','owner':'test','expected_digest':'exact-digest'}
    with pytest.raises(TypeError):api.text_action(object(),'text-send',payload)
    assert calls==[]
    payload['authorization']='User approved this exact draft.'
    assert api.text_action(object(),'text-send',payload)['phase']=='send_unknown'
    assert calls==[('existing','test','exact-digest','User approved this exact draft.')]


@pytest.mark.parametrize('payload',[{'refresh':'false'},{'refresh':1},{'body':'unrelated'}])
def test_unsupported_inbox_payload_never_constructs_backend(monkeypatch,payload):
    monkeypatch.setattr(messaging,'TextService',lambda _:pytest.fail('Invalid request reached backend'))
    with pytest.raises(ValueError):api.text_action(object(),'text-inbox',payload)


def test_local_draft_cache_and_status_never_open_ui(tmp_path,monkeypatch):
    service=CallService(home=tmp_path)
    write_json(tmp_path/'google-voice-ui.json',{
        'app':'Google Chrome','window_id':'w-1','pid':123,'process_instance':'fixture',
        'url_prefix':'https://voice.google.com/u/0/','account_label':'Fixture account',
        'voice_number':'+15555550100'})
    monkeypatch.setattr(messaging.TextService,'ui',lambda _:pytest.fail('Local request opened UI'))
    assert api.text_action(service,'text-inbox',{})['threads']==[]
    payload={'owner':'test','request_key':'draft-one','recipient':'+15555550123',
             'body':' Exact text\nSecond line '}
    first=api.text_action(service,'text-draft',payload)
    second=api.text_action(service,'text-draft',payload)
    assert first['draft_id']==second['draft_id']
    status=api.text_action(service,'text-status',{'draft_id':first['draft_id']})
    assert status['phase']=='draft' and status['body']==payload['body']
    assert not status.get('sent_message_id')


def test_text_cli_failure_preserves_voice_receipt(tmp_path,monkeypatch,capsys):
    service=SimpleNamespace(home=tmp_path)
    receipt={'action':'arm','ok':False,'error':'Existing voice error'}
    write_json(tmp_path/'desktop-last-action.json',receipt)
    monkeypatch.setattr(api,'CallService',lambda:service)
    monkeypatch.setattr(sys,'argv',['desktop_api.py','text-send'])
    monkeypatch.setattr(api,'read_text_payload',lambda:{})
    monkeypatch.setattr(api,'text_action',lambda *args:(_ for _ in ()).throw(ValueError('Missing draft')))
    assert api.main()==1
    assert json.loads(capsys.readouterr().out)=={'error':'Missing draft'}
    assert read_json(tmp_path/'desktop-last-action.json')==receipt


def test_text_cli_success_never_calls_voice_mutations(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(api,'CallService',lambda:SimpleNamespace(home=tmp_path))
    monkeypatch.setattr(sys,'argv',['desktop_api.py','text-inbox'])
    monkeypatch.setattr(api,'read_text_payload',lambda:{'refresh':False})
    monkeypatch.setattr(api,'text_action',lambda *args:{'threads':[],'source':'cached'})
    monkeypatch.setattr(api,'record_success',lambda *args,**kwargs:pytest.fail('Text changed voice receipt'))
    monkeypatch.setattr(api.incoming_control,'disarm',lambda _:pytest.fail('Text disarmed calls'))
    assert api.main()==0
    assert json.loads(capsys.readouterr().out)['source']=='cached'


@pytest.mark.parametrize('action',[api.show_voice,api.diagnose])
def test_live_voice_checks_respect_durable_message_reservation(tmp_path,monkeypatch,action):
    service=SimpleNamespace(home=tmp_path,locked=lambda:nullcontext({'browser_operation':{'owner':'text-owner'}}))
    monkeypatch.setattr(google_voice_ui,'GoogleVoiceUI',lambda _:pytest.fail('Reserved browser was accessed'))
    with pytest.raises(CallError,match='Browser is reserved'):action(service)
    assert not (tmp_path/'desktop-health.json').exists()


def test_periodic_status_reports_message_reservation_without_reading_messages(tmp_path,monkeypatch):
    operation={'owner':'text-owner','kind':'sms','status':'active'}
    service=SimpleNamespace(home=tmp_path,preflight=lambda:{'errors':[],'active_call':None,'browser_operation':operation})
    monkeypatch.setattr(api.incoming_control,'status',lambda _:dict(enabled=False,running=False,
        phase='disabled',status_age_seconds=1,allowed_callers=[],audio_lease_status=None))
    monkeypatch.setattr(messaging,'TextService',lambda _:pytest.fail('Periodic status read message contents'))
    result=api.desktop_status(service)
    assert result['browser_operation']==operation
    assert result['overall']['label']=='Browser reserved for messaging'
    assert next(c for c in result['checks'] if c['id']=='messaging')['state']=='busy'


def test_recovery_stdin_forwards_evidence_without_creating_it(monkeypatch):
    calls=[]
    class Fake:
        def __init__(self,_):pass
        def recover_browser(self,operation_token,owner,termination_evidence):
            calls.append((operation_token,owner,termination_evidence));return {'released':True}
    monkeypatch.setattr(messaging,'TextService',Fake)
    args={'operation_token':'token','owner':'owner'}
    with pytest.raises(TypeError):api.text_action(object(),'text-recover-browser',args)
    assert not calls
    args['termination_evidence']='Observed all prior processes stopped.'
    assert api.text_action(object(),'text-recover-browser',args)=={'released':True}
    assert calls==[('token','owner','Observed all prior processes stopped.')]
