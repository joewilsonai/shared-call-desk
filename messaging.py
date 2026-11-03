"""Private SMS drafts and one-shot sending through an injected, verified UI.

No provider API or browser implementation lives here. Adapters return normalized
fresh observations with identity, account_label, voice_number, coverage and
observed_at. They must bound native operations and never change audio routes.
"""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from service import CallError,read_json,write_json


class TextError(CallError):
    def __init__(self,code,*,cleanup_error_code=None):
        self.code=code;self.primary_error_code=code;self.cleanup_error_code=cleanup_error_code
        message=code+(('; browser cleanup: '+cleanup_error_code) if cleanup_error_code else '')
        super().__init__(message)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()


def safe_code(exc):
    code=getattr(exc,'code',None)
    return code if isinstance(code,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,99}',code) else type(exc).__name__


def text(value,field,limit=200):
    if not isinstance(value,str) or not value.strip() or len(value)>limit or '\x00' in value:
        raise TextError('invalid_'+field)
    return value


def phone(value):
    if not isinstance(value,str) or not re.fullmatch(r'\+[1-9][0-9]{7,14}',value):
        raise TextError('recipient_requires_exact_e164')
    return value


class TextService:
    def __init__(self,call_service,adapter=None,*,clock=time.time,browser=None,sleeper=time.sleep,monotonic=time.monotonic):
        self.calls=call_service;self.home=Path(call_service.home)
        self.directory=self.home/'messages';self.directory.mkdir(mode=0o700,exist_ok=True)
        self.adapter=adapter;self.clock=clock;self.browser=browser
        self.sleeper=sleeper;self.monotonic=monotonic

    @contextlib.contextmanager
    def locked(self):
        fd=os.open(self.directory/'state.lock',os.O_CREAT|os.O_RDWR,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            yield
        finally:os.close(fd)

    def source(self):
        config=read_json(self.home/'google-voice-ui.json',{})
        keys=('app','window_id','pid','process_instance','url_prefix','account_label')
        if any(key not in config for key in keys):raise TextError('messaging_source_not_enrolled')
        identity={key:config[key] for key in keys}
        return {'identity':identity,'account_label':text(config['account_label'],'account',1000),
                'voice_number':phone(config.get('voice_number'))}

    def ui(self):
        if self.adapter is None:
            try:from google_voice_messages import GoogleVoiceMessages
            except ImportError:raise TextError('messaging_ui_not_installed') from None
            self.adapter=GoogleVoiceMessages(self.home)
        return self.adapter

    def guard(self):
        if self.browser is None:
            from browser_operation import BrowserOperation
            self.browser=BrowserOperation(self.calls)
        return self.browser

    def path(self,draft_id):
        if not isinstance(draft_id,str) or not re.fullmatch(r'text_[0-9a-f]{32}',draft_id):
            raise TextError('invalid_draft_id')
        return self.directory/(draft_id+'.json')

    def get(self,draft_id):
        record=read_json(self.path(draft_id))
        if not record:raise TextError('unknown_draft')
        return record

    def save(self,record):
        record['updated_at']=self.clock();write_json(self.path(record['draft_id']),record)

    def operation_lock(self,draft_id):
        path=self.path(draft_id).with_suffix('.operation.lock')
        fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd);return None
        return fd

    @staticmethod
    def public(record):
        fields=('draft_id','owner','recipient','body','reply_to_message_id','conversation_id','digest','phase',
                'created_at','updated_at','error_code','sent_message_id','browser_cleanup_verified','browser_cleanup_error_code','confirmation','verification_checks','verification_last_error')
        return {key:record[key] for key in fields if key in record}|{'delivery_status':record.get('delivery_status','not_verified')}

    def valid(self,observation,source,view=None):
        if not isinstance(observation,dict):raise TextError('message_observation_unavailable')
        stamp=observation.get('observed_at')
        if (observation.get('coverage')!='complete' or type(stamp) not in (int,float)
                or not -.5<=self.clock()-stamp<=5):raise TextError('message_observation_not_fresh_complete')
        if any(observation.get(key)!=source[key] for key in ('identity','account_label','voice_number')):
            raise TextError('message_source_identity_changed')
        if view is not None and observation.get('view')!=view:raise TextError('message_view_unverified')
        return observation

    def conversation(self,observation,source,recipient):
        self.valid(observation,source,'conversation')
        if observation.get('recipient')!=recipient:raise TextError('message_recipient_changed')
        text(observation.get('conversation_id'),'conversation_id')
        messages=observation.get('messages')
        if not isinstance(messages,list):raise TextError('message_list_unavailable')
        ids=set()
        for item in messages:
            if not isinstance(item,dict):raise TextError('invalid_message_record')
            key=text(item.get('message_id'),'message_id')
            if key in ids:raise TextError('ambiguous_message_identity')
            ids.add(key)
            if item.get('direction') not in ('incoming','outgoing'):raise TextError('message_direction_unverified')
            if not isinstance(item.get('body'),str):raise TextError('message_body_unavailable')
        return observation

    def cache_conversation(self,observation,source):
        self.conversation(observation,source,phone(observation.get('recipient')))
        cache=read_json(self.directory/'conversations.json',{})
        keys=('conversation_id','recipient','messages')
        cache[observation['conversation_id']]={key:observation[key] for key in keys}|{
            'cached_at':self.clock(),'source_digest':digest(source)}
        write_json(self.directory/'conversations.json',cache)

    def draft(self,owner,request_key,recipient,body,reply_to_message_id=None):
        owner=text(owner,'owner',120);request_key=text(request_key,'request_key')
        recipient=phone(recipient);text(body,'body',1600)
        source=self.source();conversation_id=None
        if reply_to_message_id is not None:
            text(reply_to_message_id,'reply_to_message_id')
            cache=read_json(self.directory/'conversations.json',{})
            matches=[thread for thread in cache.values() if thread.get('source_digest')==digest(source)
                     and thread.get('recipient')==recipient
                     and any(m.get('message_id')==reply_to_message_id for m in thread.get('messages',[]))]
            if len(matches)!=1:raise TextError('reply_context_unverified')
            conversation_id=matches[0]['conversation_id']
        payload={'recipient':recipient,'body':body,'reply_to_message_id':reply_to_message_id,
                 'conversation_id':conversation_id,'source':source}
        binding=digest(payload);key=digest([owner,request_key])
        with self.locked():
            index=read_json(self.directory/'draft-index.json',{})
            if key in index:
                prior=self.get(index[key])
                if prior['digest']!=binding:raise TextError('idempotency_key_conflict')
                return self.public(prior)|{'replayed':True}
            record={'draft_id':'text_'+uuid.uuid4().hex,'owner':owner,'digest':binding,
                    'phase':'draft','created_at':self.clock(),**payload}
            self.save(record);index[key]=record['draft_id'];write_json(self.directory/'draft-index.json',index)
            return self.public(record)|{'replayed':False}

    def browser_session(self,owner,source,action,on_cleanup=None):
        """Only the adapter operates UI. Unverified cleanup retains its lease."""
        browser=self.guard();adapter=self.ui()
        with browser.session_lock():
            lease=browser.acquire(owner,'text-ui-'+uuid.uuid4().hex,kind='sms',identity=source['identity'])
            source_verified=False;security_denied=False;primary_error=None;stage='source_verification'
            try:
                if callable(getattr(adapter,'begin_session',None)):adapter.begin_session()
                self.valid(adapter.inspect_source(),source)
                source_verified=True
                stage='action'
                return action(adapter)
            except Exception as exc:
                primary_error=exc
                security_denied=getattr(exc,'code',None) in ('PERM_DENIED','POLICY_DENIED')
                raise
            finally:
                cleaned=False;code=None;cleanup_stage='source_verification'
                try:
                    if security_denied:raise TextError('message_security_denial_requires_review')
                    if not source_verified:
                        recover=getattr(adapter,'restore_after_source_timeout',None)
                        if safe_code(primary_error)!='message_navigation_not_settled' or not callable(recover):
                            raise TextError('message_source_unverified_before_cleanup')
                        cleanup_stage='recover_source_navigation'
                        proof=self.valid(recover(),source,'calls')
                    else:
                        self.valid(adapter.inspect_source(),source)
                        cleanup_stage='restore_calls'
                        proof=self.valid(adapter.restore_calls(),source,'calls')
                    if proof.get('state')!='idle':raise TextError('calls_page_not_idle')
                    evidence=text(proof.get('evidence'),'cleanup_evidence',2000)
                    cleanup_stage='release'
                    browser.release(lease['token'],owner,source['identity'],evidence)
                    cleaned=True
                except Exception as exc:code=safe_code(exc)
                if primary_error is not None or code is not None:
                    diagnostic={'operation_token':lease['token'],'owner':owner,'at':self.clock(),
                        'primary_error_code':safe_code(primary_error) if primary_error is not None else None,
                        'primary_stage':stage if primary_error is not None else None,
                        'cleanup_error_code':code,'cleanup_stage':cleanup_stage if code else None,
                        'browser_cleanup_verified':cleaned}
                    with contextlib.suppress(OSError):write_json(self.directory/'last-browser-error.json',diagnostic)
                if on_cleanup:on_cleanup(cleaned,code)
                if not cleaned and on_cleanup is None:
                    raise TextError(safe_code(primary_error) if primary_error is not None else 'message_browser_cleanup_unverified',cleanup_error_code=code) from None

    def inbox(self,limit=20,cursor=None,refresh=False):
        if type(refresh) is not bool:raise TextError('invalid_refresh')
        if type(limit) is not int or not 1<=limit<=100:raise TextError('invalid_limit')
        source=self.source()
        if refresh:
            def load(adapter):
                seen=self.valid(adapter.inbox(),source,'inbox')
                threads=seen.get('threads')
                if not isinstance(threads,list):raise TextError('message_inbox_unavailable')
                for thread in threads:
                    text(thread.get('conversation_id'),'conversation_id')
                    if thread.get('recipient') is None:
                        if thread.get('can_reply') is not False:raise TextError('unresolved_recipient_must_be_nonactionable')
                    else:phone(thread['recipient'])
                write_json(self.directory/'inbox.json',{'threads':threads,'cached_at':self.clock(),'source_digest':digest(source)})
            self.browser_session('text-inbox',source,load)
        cache=read_json(self.directory/'inbox.json',{})
        if cache and cache.get('source_digest')!=digest(source):raise TextError('cached_message_source_changed')
        offset=0;version=digest(cache)[:16]
        if cursor is not None:
            if not isinstance(cursor,str) or not re.fullmatch(version+r':[0-9]+',cursor):raise TextError('stale_inbox_cursor')
            offset=int(cursor.split(':')[1])
        threads=cache.get('threads',[])
        return {'threads':threads[offset:offset+limit],
                'next_cursor':f'{version}:{offset+limit}' if offset+limit<len(threads) else None,
                'cached_at':cache.get('cached_at'),'source':'live' if refresh else 'cached',
                'scope':'observed_threads_only','unresolved_thread_count':sum(t.get('recipient') is None for t in threads)}

    def read(self,conversation_id,limit=50,before=None,refresh=False):
        if type(refresh) is not bool:raise TextError('invalid_refresh')
        text(conversation_id,'conversation_id')
        if type(limit) is not int or not 1<=limit<=200:raise TextError('invalid_limit')
        source=self.source()
        if refresh:
            def load(adapter):
                seen=adapter.read(conversation_id)
                if seen.get('conversation_id')!=conversation_id:raise TextError('conversation_changed')
                self.cache_conversation(seen,source)
            self.browser_session('text-read',source,load)
        cache=read_json(self.directory/'conversations.json',{}).get(conversation_id)
        if not cache:raise TextError('conversation_not_cached')
        if cache.get('source_digest')!=digest(source):raise TextError('cached_message_source_changed')
        messages=cache['messages'];end=len(messages)
        if before is not None:
            indices=[i for i,item in enumerate(messages) if item['message_id']==before]
            if len(indices)!=1:raise TextError('message_cursor_not_found')
            end=indices[0]
        start=max(0,end-limit);items=messages[start:end]
        return {'conversation_id':conversation_id,'recipient':cache['recipient'],'messages':items,
                'next_before':items[0]['message_id'] if start and items else None,
                'cached_at':cache['cached_at'],'source':'live' if refresh else 'cached',
                'scope':'observed_messages_only'}

    def sent_candidate(self,record,observation):
        seen=self.conversation(observation,record['source'],record['recipient'])
        if seen['conversation_id']!=record['send_intent']['conversation_id']:
            raise TextError('conversation_changed')
        baseline=set(record['send_intent']['baseline_message_ids'])
        # A temporarily empty/virtualized history is not proof this is the
        # first message. Until the UI supplies verified-empty evidence, retain
        # uncertainty rather than promote a delayed-loaded old bubble.
        if not baseline:return None
        tail=record['send_intent']['baseline_message_ids'][-1:]
        positions={item['message_id']:i for i,item in enumerate(seen['messages'])}
        if tail and tail[0] not in positions:return None
        after=positions[tail[0]] if tail else -1
        matches=[item for i,item in enumerate(seen['messages']) if i>after and item['message_id'] not in baseline
                 and item['direction']=='outgoing' and item['body']==record['body'] and item.get('status') in ('sent','outgoing_observed')]
        if len(matches)!=1 or seen.get('composer_body')!='':return None
        return matches[0]['message_id']

    def sent_proof(self,record,observation,reobservation):
        first=self.sent_candidate(record,observation)
        if first is None or first!=self.sent_candidate(record,reobservation):return False
        record.update(phase='sent_observed',sent_message_id=first,sent_observed_at=self.clock())
        record.pop('error_code',None);self.cache_conversation(reobservation,record['source'])
        return True

    def check_send_receipt(self,record,adapter,*,reopen):
        if reopen:
            reader=getattr(adapter,'read_verified_recipient',None)
            first=(reader(record['send_intent']['conversation_id'],record['recipient']) if callable(reader)
                   else adapter.read(record['send_intent']['conversation_id']))
        else:first=adapter.observe_conversation()
        self.conversation(first,record['source'],record['recipient'])
        if first['conversation_id']!=record['send_intent']['conversation_id']:raise TextError('conversation_changed')
        second=adapter.observe_conversation()
        confirmed=self.sent_proof(record,first,second)
        if confirmed:record.pop('verification_last_error',None)
        self.save(record)
        return confirmed

    def check_after_send(self,record,adapter):
        # Reuse the status-refresh check inside the original browser lease.
        # A check may reopen only this immutable recipient. It never sends.
        transient={'message_navigation_not_settled','message_recipients_unverified',
                   'message_details_ambiguous','message_composer_ambiguous','message_page_ambiguous',
                   'native_observation_incomplete_or_wrong_window','STALE_REF'}
        deadline=self.monotonic()+20
        for attempt in range(4):
            if attempt and self.monotonic()>=deadline:break
            record['verification_checks']=attempt+1;self.save(record)
            try:
                if self.check_send_receipt(record,adapter,reopen=attempt>0):return
            except Exception as exc:
                code=safe_code(exc)
                if code not in transient:raise
                record['verification_last_error']=code;self.save(record)
            if attempt<3 and self.monotonic()<deadline:self.sleeper(0.4)
        if record.get('verification_last_error'):record['error_code']=record['verification_last_error']
        self.save(record)

    def send(self,draft_id,owner,expected_digest,authorization):
        owner=text(owner,'owner',120);text(authorization,'authorization',3000)
        fd=self.operation_lock(draft_id)
        if fd is None:
            record=self.get(draft_id)
            if record['owner']!=owner:raise TextError('draft_owner_mismatch')
            if record['digest']!=expected_digest:raise TextError('draft_digest_changed')
            return self.public(record)|{'replayed':True}
        try:return self._send(draft_id,owner,expected_digest,authorization)
        finally:os.close(fd)

    def _send(self,draft_id,owner,expected_digest,authorization):
        owner=text(owner,'owner',120);text(authorization,'authorization',3000)
        with self.locked():
            record=self.get(draft_id)
            if record['owner']!=owner:raise TextError('draft_owner_mismatch')
            if record['digest']!=expected_digest:raise TextError('draft_digest_changed')
            if record.get('send_intent') or record['phase'] in ('sent_observed','send_unknown','preflight'):
                return self.public(record)|{'replayed':True}
            if self.source()!=record['source']:raise TextError('message_source_identity_changed')
            for path in self.directory.glob('text_*.json'):
                prior=read_json(path,{})
                if (prior.get('draft_id')!=draft_id and prior.get('phase') in ('send_intent','send_unknown')
                        and prior.get('recipient')==record['recipient'] and prior.get('source')==record['source']):
                    error=TextError('prior_send_outcome_unresolved');error.blocking_draft_id=prior['draft_id'];raise error
            record.update(phase='preflight',authorization=authorization)
            record.pop('error_code',None);self.save(record)
        def cleanup(done,code):
            record['browser_cleanup_verified']=done
            if code:record['browser_cleanup_error_code']=code
            elif done:record.pop('browser_cleanup_error_code',None)
            self.save(record)
        def dispatch(adapter):
            adapter.prepare_send(self.public(record))
            seen=self.conversation(adapter.inspect_composer(),record['source'],record['recipient'])
            if seen.get('composer_body')!=record['body'] or seen.get('send_enabled') is not True:
                raise TextError('composer_body_or_send_control_unverified')
            if record.get('conversation_id') and seen['conversation_id']!=record['conversation_id']:
                raise TextError('reply_conversation_changed')
            # Retain the verified recipient mapping before dispatch, including
            # the first conversation with a number. Receipt reconciliation
            # must be able to reopen it even if the inbox cache predates it.
            self.cache_conversation(seen,record['source'])
            record.update(phase='send_intent',send_intent={'at':self.clock(),'conversation_id':seen['conversation_id'],
                'baseline_message_ids':[item['message_id'] for item in seen['messages']]})
            self.save(record)  # Durable before the only permitted send click.
            adapter.click_send()
            record['phase']='send_unknown';self.save(record)
            self.check_after_send(record,adapter)
        try:self.browser_session(owner,record['source'],dispatch,cleanup)
        except Exception as exc:
            if record['phase'] not in ('sent_observed','sent_confirmed'):
                record.update(phase='send_unknown' if record.get('send_intent') else 'failed_before_send',error_code=safe_code(exc))
            self.save(record)
        return self.public(record)

    def status(self,draft_id,refresh=False):
        if type(refresh) is not bool:raise TextError('invalid_refresh')
        if not refresh:return self.public(self.get(draft_id))
        fd=self.operation_lock(draft_id)
        if fd is None:return self.public(self.get(draft_id))|{'refresh_deferred':True}
        try:return self._status(draft_id,refresh=True)
        finally:os.close(fd)

    def _status(self,draft_id,refresh=False):
        record=self.get(draft_id)
        if not refresh or not record.get('send_intent') or record['phase'] in ('sent_observed','sent_confirmed'):return self.public(record)
        if self.source()!=record['source']:raise TextError('message_source_identity_changed')
        def inspect(adapter):
            self.check_send_receipt(record,adapter,reopen=True)
        def cleanup(done,code):
            record['browser_cleanup_verified']=done
            if code:record['browser_cleanup_error_code']=code
            elif done:record.pop('browser_cleanup_error_code',None)
            self.save(record)
        try:self.browser_session(record['owner'],record['source'],inspect,cleanup)
        except Exception as exc:
            record['error_code']=safe_code(exc);self.save(record)
        return self.public(record)

    def confirm_sent(self,draft_id,owner,expected_digest,confirmation_kind,evidence):
        """Record independent operator evidence; never click, retry or release a browser."""
        owner=text(owner,'owner',120);text(evidence,'confirmation_evidence',3000)
        if confirmation_kind not in ('recipient_confirmation','authenticated_thread_with_reply','sent_message_observation'):
            raise TextError('invalid_confirmation_kind')
        fd=self.operation_lock(draft_id)
        if fd is None:raise TextError('message_operation_in_progress')
        try:
            with self.locked():
                record=self.get(draft_id)
                if record['owner']!=owner:raise TextError('draft_owner_mismatch')
                if record['digest']!=expected_digest:raise TextError('draft_digest_changed')
                if record.get('confirmation'):return self.public(record)|{'replayed':True}
                if not record.get('send_intent'):raise TextError('confirmation_requires_send_intent')
                record['confirmation']={'kind':confirmation_kind,'evidence':evidence,
                    'evidence_source':'operator_reported','owner':owner,'at':self.clock(),
                    'previous_phase':record['phase'],'previous_error_code':record.get('error_code')}
                record.update(phase='sent_confirmed',delivery_status={
                    'recipient_confirmation':'recipient_confirmed',
                    'authenticated_thread_with_reply':'reply_observed',
                    'sent_message_observation':'not_verified'}[confirmation_kind])
                record.pop('error_code',None);self.save(record)
                return self.public(record)
        finally:os.close(fd)

    def recover_browser(self,operation_token,owner,termination_evidence):
        """Explicit recovery: read-only Calls/idle proof, never send or navigate."""
        text(operation_token,'operation_token');text(owner,'owner',120)
        text(termination_evidence,'termination_evidence',3000)
        browser=self.guard();source=self.source()
        with browser.session_lock():
            active=browser.status()
            if not isinstance(active,dict) or active.get('token')!=operation_token or active.get('owner')!=owner:
                raise TextError('browser_recovery_owner_or_token_mismatch')
            if active.get('identity')!=source['identity']:raise TextError('browser_recovery_identity_changed')
            proof=self.valid(self.ui().verify_calls_idle(),source,'calls')
            if proof.get('state')!='idle':raise TextError('calls_page_not_verified_idle')
            evidence=text(proof.get('evidence'),'cleanup_evidence',2000)
            browser.release(operation_token,owner,source['identity'],evidence)
            write_json(self.directory/('browser-recovery-'+uuid.uuid4().hex+'.json'),{
                'operation_token':operation_token,'owner':owner,'at':self.clock(),
                'termination_evidence':termination_evidence,'termination_source':'operator_reported',
                'browser_evidence':evidence,'unknown_messages_unmodified':True})
        return {'released':True,'operation_token':operation_token,'unknown_messages_unmodified':True}
