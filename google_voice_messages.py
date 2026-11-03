"""Exact-window native Google Voice SMS adapter. Never changes audio routes."""
import json
from pathlib import Path
import subprocess
import time
import uuid
from urllib.parse import urlparse

from google_voice_ui import GoogleVoiceUI,call_panel
from google_voice_message_parser import (walk,parse_native_inbox,parse_native_composer,
                                        parse_native_source_line,local_conversation_id,native_message_page)
from messaging import TextError,phone
from service import read_json


class GoogleVoiceMessages:
    def __init__(self,home,*,ui=None,clock=time.time):
        self.home=Path(home);self.clock=clock
        self.ui=ui or GoogleVoiceUI(self.home,clock=clock)
        self.ui.session='sms-'+uuid.uuid4().hex[:16]
        self.config=self.ui.config;phone(self.config.get('voice_number'))
        self.begin_session()

    def begin_session(self):
        self.line_verified_at=None;self.expected_body=None;self.expected_recipient=None

    def _read(self):
        web,_,receipt=self.ui.raw()
        payload=receipt.get('raw_snapshot')
        if not isinstance(payload,dict):raise TextError('native_sms_payload_unavailable')
        return web,payload

    def _options(self):
        return {'account_label':self.config['account_label'],'url_prefix':self.config['url_prefix']}

    def _wait_view(self,*sections):
        # A click receipt can precede both SPA rendering and the AX update.
        # Poll only fresh reads; never replay the action or tolerate a wrong account.
        deadline=time.monotonic()+15
        while True:
            web,payload=self._read()
            section=next((s for s in sections if web.get('name')=='Voice - '+s.title()),None)
            try:
                if section is None:raise TextError('message_navigation_not_settled')
                native_message_page(payload,section=section,**self._options())
                return web,payload
            except TextError as exc:
                if exc.code not in ('message_navigation_not_settled','message_origin_changed','message_page_ambiguous'):raise
                remaining=deadline-time.monotonic()
                if remaining<=0:raise
                time.sleep(min(0.2,remaining))

    def _wrap(self,parsed):
        if self.line_verified_at is None or self.clock()-self.line_verified_at>300:
            raise TextError('fresh_source_line_verification_required')
        return {'identity':self.ui._identity(),'account_label':self.config['account_label'],
                'voice_number':self.config['voice_number'],'coverage':'complete',
                'observed_at':self.clock(),'source_line_verified_at':self.line_verified_at,**parsed}

    def _click_ref(self,payload,ref):
        nodes=[node for node in walk(payload['data']['tree']) if node.get('ref_id')==ref]
        if len(nodes)!=1:raise TextError('message_control_ambiguous')
        result=self.ui.click(nodes[0])
        if result.get('ok') is not True:raise TextError(result.get('error_code') or 'message_action_delivery_unknown')

    def _click_named(self,name,role='button'):
        web,payload=self._read()
        nodes=[node for node in walk(web) if node.get('role')==role and node.get('name')==name
               and 'disabled' not in node.get('states',[])]
        if len(nodes)!=1:raise TextError('message_control_ambiguous')
        self._click_ref(payload,nodes[0].get('ref_id'))
        if name in ('Settings','Calls','Messages'):self._wait_view(name.lower())

    def _composer(self):
        _,payload=self._read()
        return parse_native_composer(payload,source_number=self.config['voice_number'],**self._options()),payload

    def _check_no_call(self,web):
        if call_panel(web) is not None:
            snap=self.ui.snapshot()
            if snap.get('coverage')!='complete' or snap.get('state')!='idle':
                raise TextError('phone_call_prevents_messaging')

    def inspect_source(self):
        if self.line_verified_at is None:
            self.ui.focus_enrolled()
            web,payload=self._read();self._check_no_call(web)
            fields=[node for node in walk(web) if node.get('name')=='Type a message' and node.get('role')=='textfield']
            if any(node.get('value','') for node in fields):raise TextError('existing_browser_draft_requires_review')
            if web.get('name')!='Voice - Settings':
                self._click_named('Settings');_,payload=self._wait_view('settings')
            source=parse_native_source_line(payload,**self._options())
            if source['voice_number']!=self.config['voice_number']:raise TextError('message_source_line_changed')
            self.line_verified_at=self.clock()
            self._click_ref(payload,source['close_ref'])
            web,_=self._wait_view('calls','messages');self._check_no_call(web)
        else:
            web,_=self._read();self._check_no_call(web)
        return self._wrap({'view':'source'})

    def _ensure_messages(self):
        web,_=self._read();self._check_no_call(web)
        if web.get('name')!='Voice - Messages':self._click_named('Messages','radiobutton')

    def inbox(self):
        self._ensure_messages()
        _,payload=self._read()
        return self._wrap(parse_native_inbox(payload,source_number=self.config['voice_number'],**self._options()))

    def _recipient_for(self,conversation_id):
        values=list(read_json(self.home/'messages/conversations.json',{}).values())
        values+=read_json(self.home/'messages/inbox.json',{}).get('threads',[])
        recipients={value.get('recipient') for value in values if value.get('conversation_id')==conversation_id}
        if len(recipients)!=1:raise TextError('conversation_recipient_not_cached')
        if None in recipients:raise TextError('conversation_recipient_unresolved')
        recipient=phone(recipients.pop())
        if local_conversation_id(self.config['voice_number'],recipient)!=conversation_id:
            raise TextError('conversation_source_changed')
        return recipient

    def _set_field(self,field,value,expected_value,recipient=None):
        helper=Path(__file__).parent/'native-build/call-desk-focus'
        c=self.config
        args=[str(helper),'--set-field-stdin','--app',c['app'],'--window-id',c['window_id'],'--pid',str(c['pid']),
              '--process-instance',c['process_instance'],'--budget-ms','6000']
        payload={'field':field,'value':value,'expected_value':expected_value,
                 'expected_url_prefix':c['url_prefix'],'expected_account_label':c['account_label']}
        if recipient is not None:payload['expected_recipient']=recipient
        try:
            result=self.ui.runner(args,input=json.dumps(payload),capture_output=True,text=True,timeout=9)
            receipt=json.loads(result.stdout)
        except subprocess.TimeoutExpired:raise TextError('message_field_set_delivery_unknown') from None
        except (ValueError,OSError):raise TextError('message_field_set_unavailable') from None
        if not isinstance(receipt,dict) or not all(receipt.get(key) is True for key in ('ok','identity_verified','field_verified','value_set','value_verified')):
            code=(receipt.get('error') or {}).get('code') if isinstance(receipt,dict) else None
            raise TextError(code or 'message_field_set_unverified')

    def _open_recipient(self,recipient):
        recipient=phone(recipient);self._ensure_messages()
        web,_=self._read()
        if any(node.get('role')=='textfield' and node.get('name')=='Type a message' and node.get('value','') for node in walk(web)):
            raise TextError('existing_browser_draft_requires_review')
        self._click_named('Send new message')
        seen,_=self._composer()
        if seen['recipient']==recipient and seen['composer_body']=='':return seen
        if seen['recipient'] is not None or seen['composer_body'] or seen['controls']['recipient_value'] not in ('',recipient):
            raise TextError('new_message_composer_not_empty')
        if seen['controls']['recipient_value']=='':self._set_field('Type a name or phone number',recipient,'')
        seen,payload=self._composer()
        if seen['controls']['recipient_value']!=recipient or seen['composer_body']:
            raise TextError('recipient_entry_unverified')
        ref=seen['controls']['recipient']
        result=self.ui.command([self.config.get('desktop',str(Path.home()/'.local/bin/agent-desktop')),
            'focus',ref,'--session',self.ui.session,'--timeout-ms','2500'],timeout=5)
        if result.get('ok') is not True:raise TextError((result.get('error') or {}).get('code') or 'recipient_focus_unverified')
        seen,payload=self._composer()
        fields=[node for node in walk(payload['data']['tree']) if node.get('ref_id')==seen['controls']['recipient']]
        if (seen['controls']['recipient_value']!=recipient or seen['composer_body'] or len(fields)!=1
                or 'focused' not in fields[0].get('states',[])):
            raise TextError('recipient_focus_unverified')
        # The verification snapshot replaces the native ref namespace. Bind
        # the freshly verified field before pressing Return; otherwise the
        # desktop driver still holds the previous snapshot's stale focus ref.
        result=self.ui.command([self.config.get('desktop',str(Path.home()/'.local/bin/agent-desktop')),
            'focus',seen['controls']['recipient'],'--session',self.ui.session,'--timeout-ms','2500'],timeout=5)
        if result.get('ok') is not True:raise TextError((result.get('error') or {}).get('code') or 'recipient_focus_unverified')
        result=self.ui.command([self.config.get('desktop',str(Path.home()/'.local/bin/agent-desktop')),
            '--headed','press','return','--window-id',self.config['window_id'],'--session',self.ui.session],timeout=5)
        if result.get('ok') is not True:raise TextError((result.get('error') or {}).get('code') or 'recipient_confirmation_unknown')
        seen,payload=self._composer()
        choices=[item for item in seen['controls']['suggestions'] if item['recipient']==recipient]
        if len(choices)!=1:raise TextError('recipient_suggestion_ambiguous')
        self._click_ref(payload,choices[0]['ref_id'])
        seen,_=self._composer()
        if seen['recipient']!=recipient or seen['composer_body']:raise TextError('committed_recipient_unverified')
        return seen

    def read(self,conversation_id):
        seen=self._open_recipient(self._recipient_for(conversation_id))
        return self._wrap(seen)

    def read_verified_recipient(self,conversation_id,recipient):
        # Reconciliation already holds an immutable authorized recipient.
        # A named contact need not expose its number in the inbox preview.
        recipient=phone(recipient)
        if local_conversation_id(self.config['voice_number'],recipient)!=conversation_id:
            raise TextError('conversation_source_changed')
        return self._wrap(self._open_recipient(recipient))

    def prepare_send(self,draft):
        seen=self._open_recipient(draft['recipient'])
        if seen['recipient']!=draft['recipient'] or seen['composer_body']:
            raise TextError('message_recipient_or_empty_composer_unverified')
        self.expected_recipient=draft['recipient'];self.expected_body=draft['body']
        self._set_field('Type a message',draft['body'],'',draft['recipient'])

    def inspect_composer(self):
        seen,_=self._composer();return self._wrap(seen)

    def click_send(self):
        seen,payload=self._composer()
        if (self.expected_body is None or seen['recipient']!=self.expected_recipient
                or seen['composer_body']!=self.expected_body or seen['send_enabled'] is not True):
            raise TextError('message_send_preconditions_changed')
        self._click_ref(payload,seen['controls']['send'])

    def observe_conversation(self):
        for attempt in range(4):
            try:return self.inspect_composer()
            except TextError as exc:
                if exc.code not in ('message_recipients_unverified','message_details_ambiguous','message_composer_ambiguous','message_origin_changed') or attempt==3:raise
                time.sleep(0.25)

    def verify_calls_idle(self):
        """Read-only recovery proof; never navigate or touch an existing draft."""
        web,_=self._read()
        if web.get('name')!='Voice - Calls':raise TextError('calls_page_not_verified_idle')
        snap=self.ui.snapshot()
        if snap.get('coverage')!='complete' or snap.get('state')!='idle' or snap.get('identity')!=self.ui._identity():
            raise TextError('calls_page_not_verified_idle')
        return {'identity':self.ui._identity(),'account_label':self.config['account_label'],
                'voice_number':self.config['voice_number'],'coverage':'complete','observed_at':self.clock(),
                'view':'calls','state':'idle','evidence':snap['evidence']}

    def restore_after_source_timeout(self):
        """Recover only a verified source transition; never prepare or send SMS."""
        web,payload=self._read();self._check_no_call(web)
        if self.line_verified_at is None:
            if web.get('name')!='Voice - Settings':
                raise TextError('message_source_unverified_before_cleanup')
            source=parse_native_source_line(payload,**self._options())
            if source['voice_number']!=self.config['voice_number']:
                raise TextError('message_source_line_changed')
            self.line_verified_at=self.clock()
        return self.restore_calls()

    def restore_calls(self):
        web,payload=self._read();self._check_no_call(web)
        if web.get('name')=='Voice - Settings':
            source=parse_native_source_line(payload,**self._options())
            if source['voice_number']!=self.config['voice_number']:raise TextError('message_source_line_changed')
            self._click_ref(payload,source['close_ref']);web,payload=self._wait_view('calls','messages')
        fields=[node for node in walk(web) if node.get('role')=='textfield' and node.get('name')=='Type a message']
        if fields and fields[0].get('value',''):
            seen=parse_native_composer(payload,source_number=self.config['voice_number'],**self._options())
            if seen['composer_body']!=self.expected_body or seen['recipient']!=self.expected_recipient:
                raise TextError('unowned_browser_draft_retained')
            self._set_field('Type a message','',self.expected_body,self.expected_recipient)
            seen,_=self._composer()
            if seen['composer_body']:raise TextError('owned_browser_draft_not_cleared')
        self._click_named('Calls','radiobutton')
        # Navigation and the AX call panel settle asynchronously. Retry only
        # observation of this transition, never the click or any send action.
        deadline=time.monotonic()+10
        while True:
            try:return self.verify_calls_idle()
            except TextError as exc:
                if exc.code!='calls_page_not_verified_idle':raise
                remaining=deadline-time.monotonic()
                if remaining<=0:raise
                time.sleep(min(0.25,remaining))
