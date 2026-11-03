import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from google_voice_messages import GoogleVoiceMessages
from google_voice_message_parser import walk
from messaging import TextError

FIXTURES=Path(__file__).parent/'fixtures/sms'

class Native:
    def __init__(self):
        self.config={'app':'Google Chrome','window_id':'w-100','pid':10,'process_instance':'offline',
            'account_label':'Google Account: Example Sender (sender@example.com), Google membership',
            'url_prefix':'https://voice.google.com/u/0/','voice_number':'+12025550105'}
        self.session='test';self.operations=[];self.setter_requests=[];self.state='inbox';self.body='';self.deny=False
        self.snapshot_generation=0;self.focus_generation=None
    def _identity(self):return {k:self.config[k] for k in ('app','window_id','pid','process_instance','account_label','url_prefix')}
    def focus_enrolled(self):self.operations.append('exact_focus')
    def raw(self):
        self.snapshot_generation+=1
        name={'settings':'settings','new':'new','recipient':'recipient','suggestion':'suggestion','thread':'thread','filled':'filled'}.get(self.state,'inbox')
        payload=json.loads((FIXTURES/(name+'-native.json')).read_text())
        web=next(n for n in walk(payload['data']['tree']) if n.get('role')=='webarea')
        if self.state=='calls':
            web['name']='Voice - Calls'
            next(n for n in walk(payload['data']['tree']) if n.get('name')=='Address and search bar')['value']='https://voice.google.com/u/0/calls'
        if self.state=='filled':
            next(n for n in walk(web) if n.get('role')=='textfield' and n.get('name')=='Type a message')['value']=self.body
        return web,payload['data']['tree'],{'raw_snapshot':payload}
    def click(self,node):
        name=node['name'];self.operations.append(name)
        if name=='Settings':self.state='settings'
        elif name=='Close':self.state='inbox'
        elif name=='Send new message':self.state='new'
        elif name.startswith('Send to '):self.state='thread'
        elif name=='Calls':self.state='calls'
        elif name=='Messages':self.state='inbox'
        elif name=='Send message':self.state='thread';self.body=''
        else:raise AssertionError(name)
        return {'ok':True,'delivery':'delivered_unverified'}
    def command(self,args,**kwargs):
        if 'focus' in args:
            self.operations.append('focus_recipient');self.focus_generation=self.snapshot_generation
        elif 'press' in args:
            assert '--headed' in args and '--window-id' in args
            assert self.focus_generation==self.snapshot_generation,'STALE_REF: snapshot invalidated prior focus reference'
            assert 'return' in args and self.state=='recipient'
            self.operations.append('commit_recipient');self.state='suggestion'
        else:raise AssertionError(args)
        return {'ok':True}
    def runner(self,args,**kwargs):
        payload=json.loads(kwargs['input']);self.setter_requests.append((args,payload))
        assert args[1]=='--set-field-stdin' and payload['value'] not in args
        if self.deny:return SimpleNamespace(stdout=json.dumps({'ok':False,'error':{'code':'PERM_DENIED'}}))
        if payload['field']=='Type a name or phone number':
            assert self.state=='new' and payload['expected_value']=='';self.state='recipient'
        else:
            assert payload['expected_recipient']=='+12025550186'
            assert payload['expected_value']==self.body
            self.body=payload['value'];self.state='filled' if self.body else 'thread'
        return SimpleNamespace(stdout=json.dumps({k:True for k in ('ok','identity_verified','field_verified','value_set','value_verified')}))
    def snapshot(self):
        return {'coverage':'complete','state':'idle' if self.state=='calls' else 'unknown',
                'identity':self._identity(),'evidence':'Offline exact Calls idle'}

@pytest.fixture
def adapter(tmp_path):return GoogleVoiceMessages(tmp_path,ui=Native(),clock=lambda:1000.)

def test_source_line_is_live_settings_value_not_inbox_number(adapter):
    result=adapter.inspect_source()
    assert result['voice_number']=='+12025550105'
    assert adapter.ui.operations==['exact_focus','Settings','Close']
    adapter.inspect_source()
    assert adapter.ui.operations.count('Settings')==1
    adapter.begin_session();adapter.inspect_source()
    assert adapter.ui.operations.count('Settings')==2

def test_prepare_commits_exact_recipient_then_fills_body_using_stdin_only(adapter):
    adapter.inspect_source()
    draft={'recipient':'+12025550186','body':'Private exact\nmessage'}
    adapter.prepare_send(draft)
    observed=adapter.inspect_composer()
    assert observed['recipient']==draft['recipient'] and observed['composer_body']==draft['body']
    assert 'Send message' not in adapter.ui.operations
    assert len(adapter.ui.setter_requests)==2
    assert adapter.ui.setter_requests[-1][1]['expected_recipient']==draft['recipient']
    assert draft['body'] not in str(adapter.ui.setter_requests[-1][0])

def test_send_click_rechecks_body_and_recipient_before_dispatch(adapter):
    adapter.inspect_source();adapter.prepare_send({'recipient':'+12025550186','body':'Original'})
    adapter.ui.body='Changed externally'
    with pytest.raises(TextError,match='preconditions'):adapter.click_send()
    assert 'Send message' not in adapter.ui.operations

def test_existing_unowned_browser_draft_is_not_overwritten_or_navigated(adapter):
    adapter.ui.state='filled';adapter.ui.body='Existing human draft'
    with pytest.raises(TextError,match='existing_browser_draft'):adapter.inspect_source()
    assert adapter.ui.operations==['exact_focus'] and adapter.ui.setter_requests==[]

def test_cleanup_clears_only_own_exact_body_and_returns_calls(adapter):
    adapter.inspect_source();adapter.prepare_send({'recipient':'+12025550186','body':'Own draft'})
    result=adapter.restore_calls()
    assert result['view']=='calls' and result['state']=='idle'
    assert adapter.ui.setter_requests[-1][1]['value']==''
    assert adapter.ui.setter_requests[-1][1]['expected_value']=='Own draft'
    assert not any('Audio' in action for action in adapter.ui.operations)

def test_changed_body_is_preserved_during_cleanup(adapter):
    adapter.inspect_source();adapter.prepare_send({'recipient':'+12025550186','body':'Own draft'})
    adapter.ui.body='Changed by user';count=len(adapter.ui.setter_requests)
    with pytest.raises(TextError,match='unowned'):adapter.restore_calls()
    assert len(adapter.ui.setter_requests)==count and adapter.ui.state=='filled'

def test_setter_denial_has_no_retry_or_send(adapter):
    adapter.inspect_source();adapter.ui.deny=True
    with pytest.raises(TextError,match='PERM_DENIED'):
        adapter.prepare_send({'recipient':'+12025550186','body':'No send'})
    assert len(adapter.ui.setter_requests)==1 and 'Send message' not in adapter.ui.operations

def test_read_only_recovery_requires_calls_page(adapter):
    with pytest.raises(TextError,match='calls_page'):adapter.verify_calls_idle()
    assert adapter.ui.operations==[]
    adapter.ui.state='calls'
    assert adapter.verify_calls_idle()['state']=='idle' and adapter.ui.operations==[]

def test_cleanup_waits_for_navigation_without_replaying_click(adapter,monkeypatch):
    original=adapter.verify_calls_idle
    reads=[]
    def settling():
        reads.append(True)
        if len(reads)<3:raise TextError('calls_page_not_verified_idle')
        return original()
    monkeypatch.setattr(adapter,'verify_calls_idle',settling)
    monkeypatch.setattr('google_voice_messages.time.sleep',lambda _:None)
    assert adapter.restore_calls()['state']=='idle'
    assert adapter.ui.operations.count('Calls')==1 and len(reads)==3

def test_cleanup_does_not_retry_origin_or_permission_failure(adapter,monkeypatch):
    reads=[]
    def denied():
        reads.append(True);raise TextError('google_voice_tab_changed')
    monkeypatch.setattr(adapter,'verify_calls_idle',denied)
    with pytest.raises(TextError,match='google_voice_tab_changed'):adapter.restore_calls()
    assert len(reads)==1 and adapter.ui.operations.count('Calls')==1


def test_navigation_waits_for_matching_page_without_reclick(adapter,monkeypatch):
    original=adapter._read
    stale=original()
    adapter.ui.state='calls'
    reads=[]
    def delayed():
        reads.append(True)
        return stale if len(reads)<3 else original()
    monkeypatch.setattr(adapter,'_read',delayed)
    monkeypatch.setattr('google_voice_messages.time.sleep',lambda _:None)
    assert adapter._wait_view('calls')[0]['name']=='Voice - Calls'
    assert len(reads)==3 and adapter.ui.operations==[]


def test_navigation_wait_never_retries_account_change(adapter,monkeypatch):
    adapter.ui.state='calls'
    original=adapter._read
    reads=[]
    def changed():
        reads.append(True)
        web,payload=original()
        for node in walk(web):
            if node.get('name')==adapter.config['account_label']:node['name']='Different account'
        return web,payload
    monkeypatch.setattr(adapter,'_read',changed)
    with pytest.raises(TextError,match='account_unverified'):adapter._wait_view('calls')
    assert len(reads)==1 and adapter.ui.operations==[]


def test_reconciliation_can_reopen_exact_approved_number_without_inbox_cache(adapter):
    from google_voice_message_parser import local_conversation_id
    adapter.inspect_source()
    identifier=local_conversation_id(adapter.config['voice_number'],'+12025550186')
    assert adapter.read_verified_recipient(identifier,'+12025550186')['recipient']=='+12025550186'


def test_reconciliation_rejects_mismatched_source_recipient_identifier(adapter):
    with pytest.raises(TextError,match='conversation_source_changed'):
        adapter.read_verified_recipient('sms_wrong','+12025550186')
    assert adapter.ui.operations==[]
