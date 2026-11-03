import copy
import threading
import pytest
from messaging import TextService,TextError
from service import CallService,read_json,write_json

class FakeUI:
    def __init__(self,source,clock):
        self.source=source;self.clock=clock;self.calls=[];self.body='';self.mode='success'
        self.recipient='+15555550123';self.bad_source=False;self.bad_recipient=False
        self.clean=True;self.messages=[{'message_id':'baseline','direction':'incoming','body':'Prior message','status':'received'}];self.entered=None;self.proceed=None
    def observation(self,view,**values):
        result=copy.deepcopy(self.source)|{'observed_at':self.clock(),'coverage':'complete','view':view,**values}
        if self.bad_source:result['voice_number']='+15555550999'
        return result
    def inspect_source(self):
        self.calls.append('source');return self.observation('calls')
    def inbox(self):
        self.calls.append('inbox')
        return self.observation('inbox',threads=[{'conversation_id':'thread-one','recipient':self.recipient,
            'display_name':'Test recipient','preview':'test','timestamp':'today','unread':True}])
    def read(self,conversation_id):
        self.calls.append('read');assert conversation_id=='thread-one'
        return self.observe_conversation()
    def prepare_send(self,draft):
        self.calls.append('prepare');self.body=draft['body']
        if self.entered:self.entered.set();assert self.proceed.wait(3)
    def inspect_composer(self):
        self.calls.append('composer')
        return self.observation('conversation',conversation_id='thread-one',
            recipient='+15555550999' if self.bad_recipient else self.recipient,
            composer_body=self.body,send_enabled=True,messages=copy.deepcopy(self.messages))
    def click_send(self):
        self.calls.append('send')
        if self.mode=='crash':raise KeyboardInterrupt()
        if self.mode not in ('no_new','existing_id'):
            self.messages.append({'message_id':'new-message','direction':'outgoing','body':self.body,'status':'sent'})
        self.body=''
        if self.mode=='lost_reply':raise RuntimeError('PRIVATE provider failure text')
    def observe_conversation(self):
        self.calls.append('observe')
        return self.observation('conversation',conversation_id='thread-one',recipient=self.recipient,
            composer_body=self.body,messages=copy.deepcopy(self.messages))
    def restore_calls(self):
        self.calls.append('restore_calls')
        return self.observation('calls',state='idle' if self.clean else 'unknown',evidence='Fake exact Calls page idle')
    def verify_calls_idle(self):
        self.calls.append('verify_calls_idle')
        return self.observation('calls',state='idle' if self.clean else 'unknown',evidence='Fresh read-only Calls idle proof')

@pytest.fixture
def setup(tmp_path):
    config=tmp_path/'config.json';write_json(config,{'voice_name':'Elle'})
    calls=CallService(tmp_path/'state',config)
    write_json(calls.home/'google-voice-ui.json',{'app':'Google Chrome','window_id':'w-100','pid':10,
        'process_instance':'offline-process','url_prefix':'https://voice.google.com/u/0/',
        'account_label':'Offline account','voice_number':'+15555550000'})
    texts=TextService(calls,clock=lambda:1000.,sleeper=lambda _:None)
    ui=FakeUI(texts.source(),texts.clock);texts.adapter=ui
    return texts,ui,calls

def draft(texts,key='one',body='Hello test'):
    return texts.draft('test-owner',key,'+15555550123',body)

def send(texts,value):
    return texts.send(value['draft_id'],'test-owner',value['digest'],'Explicit user authorization for this exact offline test')

def test_drafts_preserve_exact_text_and_idempotency_without_ui(setup):
    texts,ui,_=setup;body='  A line\nAnd another  '
    first=draft(texts,body=body);again=draft(texts,body=body)
    assert first['body']==body and first['draft_id']==again['draft_id'] and again['replayed']
    with pytest.raises(TextError,match='idempotency'):draft(texts,body=body+'changed')
    assert not ui.calls

@pytest.mark.parametrize('field,value',[('owner','other'),('digest','stale'),('authorization','')])
def test_send_needs_owner_digest_and_explicit_authorization(setup,field,value):
    texts,ui,_=setup;created=draft(texts)
    args={'draft_id':created['draft_id'],'owner':'test-owner','expected_digest':created['digest'],'authorization':'Exact test authorized'}
    args[{'digest':'expected_digest'}.get(field,field)]=value
    with pytest.raises(TextError):texts.send(**args)
    assert not ui.calls

def test_successful_send_records_distinct_bubble_not_delivery(setup):
    texts,ui,calls=setup;created=draft(texts)
    result=send(texts,created)
    assert result['phase']=='sent_observed' and result['sent_message_id']=='new-message'
    assert result['delivery_status']=='not_verified' and result['browser_cleanup_verified']
    assert ui.calls.count('send')==1 and read_json(calls.home/'registry.json')['browser_operation'] is None
    assert send(texts,created)['replayed'] and ui.calls.count('send')==1

@pytest.mark.parametrize('mode',['no_new','existing_id'])
def test_old_identical_bubble_cannot_prove_new_send(setup,mode):
    texts,ui,_=setup;created=draft(texts);ui.mode=mode
    ui.messages=[{'message_id':'old-message','direction':'outgoing','body':created['body'],'status':'sent'}]
    assert send(texts,created)['phase']=='send_unknown'
    assert send(texts,created)['replayed'] and ui.calls.count('send')==1
    with pytest.raises(TextError,match='prior_send_outcome_unresolved'):send(texts,draft(texts,'two'))

def test_lost_click_reply_is_unknown_then_read_only_reconciliation(setup):
    texts,ui,_=setup;created=draft(texts);ui.mode='lost_reply'
    result=send(texts,created)
    assert result['phase']=='send_unknown' and result['error_code']=='RuntimeError'
    assert 'PRIVATE' not in str(result)
    reconciled=texts.status(created['draft_id'],refresh=True)
    assert reconciled['phase']=='sent_observed' and ui.calls.count('send')==1

def test_crash_after_intent_never_redispatches(setup):
    texts,ui,_=setup;created=draft(texts);ui.mode='crash'
    with pytest.raises(KeyboardInterrupt):send(texts,created)
    assert texts.get(created['draft_id'])['send_intent']
    assert send(texts,created)['replayed'] and ui.calls.count('send')==1

@pytest.mark.parametrize('wrong',['source','recipient','body'])
def test_identity_recipient_and_exact_composer_checked_before_send(setup,wrong):
    texts,ui,_=setup;created=draft(texts)
    if wrong=='source':ui.bad_source=True
    elif wrong=='recipient':ui.bad_recipient=True
    else:
        prepare=ui.prepare_send
        def mismatch(value):prepare(value);ui.body+='changed'
        ui.prepare_send=mismatch
    result=send(texts,created)
    assert result['phase']=='failed_before_send' and 'send' not in ui.calls
    if wrong=='source':assert 'prepare' not in ui.calls and 'restore_calls' not in ui.calls

def test_browser_cleanup_failure_retains_durable_reservation(setup):
    texts,ui,calls=setup;created=draft(texts);ui.clean=False
    result=send(texts,created)
    assert result['phase']=='sent_observed' and result['browser_cleanup_verified'] is False
    assert read_json(calls.home/'registry.json')['browser_operation']
    with pytest.raises(Exception,match='Browser is reserved'):
        calls.prepare('caller','one','+15555550123','offline','',30,'offline',0)

def test_cached_reads_and_drafts_do_not_touch_ui_during_active_call(setup):
    texts,ui,calls=setup
    texts.inbox(refresh=True);texts.read('thread-one',refresh=True);ui.calls.clear()
    calls.prepare('caller','one','+15555550123','offline','',30,'offline',0)
    assert texts.inbox()['threads'] and texts.read('thread-one')['recipient']==ui.recipient
    created=draft(texts)
    assert not ui.calls
    assert send(texts,created)['phase']=='failed_before_send' and not ui.calls

def test_two_simultaneous_send_requests_dispatch_once(setup):
    texts,ui,_=setup;created=draft(texts)
    ui.entered=threading.Event();ui.proceed=threading.Event();results=[]
    worker=threading.Thread(target=lambda:results.append(send(texts,created)))
    worker.start();assert ui.entered.wait(3)
    second=send(texts,created)
    assert second['replayed'] and second['phase']=='preflight'
    ui.proceed.set();worker.join(3)
    assert not worker.is_alive() and results[0]['phase']=='sent_observed'
    assert ui.calls.count('send')==1

def test_reply_requires_cached_matching_context(setup):
    texts,ui,_=setup
    ui.messages=[{'message_id':'incoming-one','direction':'incoming','body':'Hi','status':'received'}]
    with pytest.raises(TextError,match='reply_context_unverified'):
        texts.draft('test-owner','reply',ui.recipient,'Reply','incoming-one')
    texts.read('thread-one',refresh=True)
    created=texts.draft('test-owner','reply',ui.recipient,'Reply','incoming-one')
    assert created['reply_to_message_id']=='incoming-one'

@pytest.mark.parametrize('location',['before_tail','missing_tail'])
def test_newly_loaded_history_cannot_be_mistaken_for_new_sent_message(setup,location):
    texts,ui,_=setup;created=draft(texts);ui.mode='no_new'
    previous={'message_id':'tail','direction':'incoming','body':'Prior tail','status':'received'}
    ui.messages=[previous]
    assert send(texts,created)['phase']=='send_unknown'
    matching={'message_id':'historical','direction':'outgoing','body':created['body'],'status':'sent'}
    ui.messages=[matching,previous] if location=='before_tail' else [matching]
    assert texts.status(created['draft_id'],refresh=True)['phase']=='send_unknown'
    assert ui.calls.count('send')==1

def test_concurrent_status_refresh_never_overwrites_active_send(setup):
    texts,ui,_=setup;created=draft(texts)
    entered=threading.Event();proceed=threading.Event();results=[]
    original=ui.click_send
    def pause_click():
        entered.set();assert proceed.wait(3);original()
    ui.click_send=pause_click
    worker=threading.Thread(target=lambda:results.append(send(texts,created)))
    worker.start();assert entered.wait(3)
    observed=texts.status(created['draft_id'],refresh=True)
    assert observed['phase']=='send_intent' and observed['refresh_deferred']
    proceed.set();worker.join(3)
    assert not worker.is_alive() and results[0]['phase']=='sent_observed'
    assert texts.status(created['draft_id'])['phase']=='sent_observed'

@pytest.mark.parametrize('operation',['inbox','read','status'])
def test_refresh_must_be_a_boolean(setup,operation):
    texts,ui,_=setup
    with pytest.raises(TextError,match='invalid_refresh'):
        if operation=='inbox':texts.inbox(refresh='false')
        elif operation=='read':texts.read('thread-one',refresh='false')
        else:texts.status(draft(texts)['draft_id'],refresh='false')
    assert not ui.calls

@pytest.mark.parametrize('field,value',[('voice_number','+15555550999'),('account_label','Another account'),('pid',99)])
def test_changed_source_enrollment_rejects_old_draft_before_ui(setup,field,value):
    texts,ui,calls=setup;created=draft(texts)
    path=calls.home/'google-voice-ui.json';source=read_json(path);source[field]=value;write_json(path,source)
    with pytest.raises(TextError,match='source_identity_changed'):send(texts,created)
    assert not ui.calls

def test_empty_baseline_cannot_establish_a_new_message(setup):
    texts,ui,_=setup;created=draft(texts);ui.messages=[]
    result=send(texts,created)
    assert result['phase']=='send_unknown' and ui.calls.count('send')==1

def test_explicit_browser_recovery_does_not_resend_or_resolve_unknown(setup):
    texts,ui,calls=setup;created=draft(texts);ui.mode='no_new';ui.clean=False
    assert send(texts,created)['phase']=='send_unknown'
    active=read_json(calls.home/'registry.json')['browser_operation']
    ui.clean=True;before=list(ui.calls)
    result=texts.recover_browser(active['token'],active['owner'],'Operator inspected prior process and children ended')
    assert result['released'] and result['unknown_messages_unmodified']
    assert ui.calls==before+['verify_calls_idle']
    assert texts.status(created['draft_id'])['phase']=='send_unknown'
    assert read_json(calls.home/'registry.json')['browser_operation'] is None

def test_sent_candidate_requires_stable_reobservation(setup):
    texts,ui,_=setup;created=draft(texts);count=0
    original=ui.observe_conversation
    def changing():
        nonlocal count
        count+=1
        observation=original()
        if count%2==0:observation['messages']=[m for m in observation['messages'] if m['message_id']!='new-message']
        return observation
    ui.observe_conversation=changing
    assert send(texts,created)['phase']=='send_unknown'

def test_named_unresolved_inbox_rows_are_visible_but_not_actionable(setup):
    texts,ui,_=setup
    ui.inbox=lambda:ui.observation('inbox',threads=[{'conversation_id':'unresolved-one','recipient':None,
        'can_reply':False,'display_name':'Saved contact','preview':'Hello','timestamp':'display time','unread':None}])
    result=texts.inbox(refresh=True)
    assert result['threads'][0]['display_name']=='Saved contact' and result['unresolved_thread_count']==1

def test_primary_read_error_is_preserved_when_cleanup_also_fails(setup):
    texts,ui,calls=setup;ui.clean=False
    def fail():raise TextError('native_primary_read_failed')
    ui.inbox=fail
    with pytest.raises(TextError) as caught:texts.inbox(refresh=True)
    assert caught.value.code=='native_primary_read_failed'
    assert caught.value.cleanup_error_code=='calls_page_not_idle'
    assert 'native_primary_read_failed' in str(caught.value) and 'calls_page_not_idle' in str(caught.value)
    diagnostic=read_json(calls.home/'messages/last-browser-error.json')
    assert diagnostic['primary_error_code']=='native_primary_read_failed'
    assert diagnostic['primary_stage']=='action'
    assert diagnostic['cleanup_error_code']=='calls_page_not_idle'
    assert diagnostic['cleanup_stage']=='restore_calls'
    assert diagnostic['browser_cleanup_verified'] is False
    assert read_json(calls.home/'registry.json')['browser_operation']

def test_source_failure_is_logged_without_leaking_exception_text(setup):
    texts,ui,calls=setup
    def fail():raise RuntimeError('PRIVATE raw source failure text')
    ui.inspect_source=fail
    with pytest.raises(TextError) as caught:texts.inbox(refresh=True)
    assert caught.value.code=='RuntimeError'
    diagnostic=read_json(calls.home/'messages/last-browser-error.json')
    assert diagnostic['primary_stage']=='source_verification'
    assert diagnostic['cleanup_error_code']=='message_source_unverified_before_cleanup'
    assert 'PRIVATE' not in str(caught.value) and 'PRIVATE' not in str(diagnostic)


def test_first_send_can_reopen_recipient_before_inbox_refresh(setup,monkeypatch):
    texts,ui,_=setup
    original=ui.read
    def read(identifier):
        cache=read_json(texts.directory/'conversations.json',{})
        assert cache[identifier]['recipient']=='+15555550123'
        return original(identifier)
    monkeypatch.setattr(ui,'read',read)
    assert send(texts,draft(texts))['phase']=='sent_observed'


def test_independent_confirmation_resolves_lock_with_audit_and_no_browser(setup):
    texts,ui,_=setup;ui.mode='no_new';created=draft(texts)
    assert send(texts,created)['phase']=='send_unknown'
    before=list(ui.calls)
    result=texts.confirm_sent(created['draft_id'],'test-owner',created['digest'],
        'recipient_confirmation','Recipient confirmed this exact message arrived in our current chat.')
    assert result['phase']=='sent_confirmed' and result['delivery_status']=='recipient_confirmed'
    assert result['confirmation']['previous_phase']=='send_unknown'
    assert result['confirmation']['evidence_source']=='operator_reported'
    assert ui.calls==before
    assert texts.status(created['draft_id'],refresh=True)['phase']=='sent_confirmed' and ui.calls==before
    ui.mode='success'
    assert send(texts,draft(texts,'next','Next authorized message'))['phase']=='sent_observed'
    assert texts.confirm_sent(created['draft_id'],'test-owner',created['digest'],
        'recipient_confirmation','Same independent confirmation.')['replayed']


def test_confirmation_cannot_turn_unsent_or_changed_draft_into_sent(setup):
    texts,ui,_=setup;created=draft(texts)
    for owner,digest,kind,code in [('other',created['digest'],'recipient_confirmation','owner'),
        ('test-owner','wrong','recipient_confirmation','digest'),
        ('test-owner',created['digest'],'elapsed_time','kind'),
        ('test-owner',created['digest'],'recipient_confirmation','send_intent')]:
        with pytest.raises(TextError,match=code):texts.confirm_sent(created['draft_id'],owner,digest,kind,'External evidence of the exact message.')
    assert texts.get(created['draft_id'])['phase']=='draft' and not ui.calls


def test_send_waits_for_delayed_bubble_without_a_second_click(setup,monkeypatch):
    texts,ui,_=setup;created=draft(texts);original=ui.observe_conversation
    reads=[]
    def delayed():
        seen=original();reads.append(True)
        if len(reads)<4:seen['messages']=seen['messages'][:1]
        return seen
    monkeypatch.setattr(ui,'observe_conversation',delayed)
    result=send(texts,created)
    assert result['phase']=='sent_observed' and result['verification_checks']>=2
    assert ui.calls.count('send')==1 and ui.calls.count('read')>=1


def test_send_runs_status_refresh_when_only_reopening_reveals_receipt(setup,monkeypatch):
    texts,ui,_=setup;created=draft(texts);original=ui.observe_conversation
    opened={'value':False}
    def observed():
        seen=original()
        if not opened['value']:seen['messages']=seen['messages'][:1]
        return seen
    def verified_reader(identifier,recipient):
        assert identifier=='thread-one' and recipient==created['recipient']
        opened['value']=True;ui.calls.append('verified_read');return observed()
    monkeypatch.setattr(ui,'observe_conversation',observed)
    monkeypatch.setattr(ui,'read_verified_recipient',verified_reader,raising=False)
    result=send(texts,created)
    assert result['phase']=='sent_observed' and result['verification_checks']==2
    assert ui.calls.count('send')==1 and ui.calls.count('verified_read')==1


def test_send_recovers_transient_navigation_error_by_reading_only(setup,monkeypatch):
    texts,ui,_=setup;created=draft(texts);original=ui.observe_conversation
    reads=[]
    def observed():
        reads.append(True)
        if len(reads)==1:raise TextError('message_navigation_not_settled')
        return original()
    monkeypatch.setattr(ui,'observe_conversation',observed)
    result=send(texts,created)
    assert result['phase']=='sent_observed' and result['verification_checks']==2
    assert 'error_code' not in result and 'verification_last_error' not in result
    assert ui.calls.count('send')==1


def test_permanent_uncertainty_is_bounded_and_never_resends(setup):
    texts,ui,_=setup;ui.mode='no_new';created=draft(texts)
    result=send(texts,created)
    assert result['phase']=='send_unknown' and result['verification_checks']==4
    assert ui.calls.count('send')==1 and ui.calls.count('read')==3
    assert send(texts,created)['replayed'] and ui.calls.count('send')==1


def test_post_send_account_mismatch_stops_without_reopening(setup,monkeypatch):
    texts,ui,_=setup;created=draft(texts);original=ui.observe_conversation
    def observed():
        result=original();result['account_label']='Wrong account';return result
    monkeypatch.setattr(ui,'observe_conversation',observed)
    result=send(texts,created)
    assert result['phase']=='send_unknown' and result['error_code']=='message_source_identity_changed'
    assert result['verification_checks']==1 and 'read' not in ui.calls
    assert ui.calls.count('send')==1


def test_receipt_check_deadline_stops_further_reads(setup):
    texts,ui,_=setup;ui.mode='no_new';ticks=iter([0,0,21])
    texts.monotonic=lambda:next(ticks)
    result=send(texts,draft(texts))
    assert result['phase']=='send_unknown' and result['verification_checks']==1
    assert ui.calls.count('send')==1 and 'read' not in ui.calls


def test_source_navigation_timeout_can_cleanup_without_replaying_message_action(setup):
    texts,ui,calls=setup
    def fail():
        ui.calls.append('source');raise TextError('message_navigation_not_settled')
    def restore():
        ui.calls.append('recover_source_timeout')
        return ui.observation('calls',state='idle',evidence='Verified exact source and idle Calls after navigation timeout')
    ui.inspect_source=fail;ui.restore_after_source_timeout=restore
    with pytest.raises(TextError,match='message_navigation_not_settled'):texts.inbox(refresh=True)
    assert ui.calls==['source','recover_source_timeout']
    assert read_json(calls.home/'registry.json')['browser_operation'] is None
    diagnostic=read_json(calls.home/'messages/last-browser-error.json')
    assert diagnostic['primary_error_code']=='message_navigation_not_settled'
    assert diagnostic['browser_cleanup_verified'] is True
