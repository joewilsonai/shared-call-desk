"""Strict native-AX adapter for one enrolled Google Voice window.

Uses Joe's installed agent-desktop/elle-read tools. Never searches another
window, infers caller numbers from history, or retries an uncertain click.
"""
from __future__ import annotations
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from service import read_json,write_json
from audio_policy import VIRTUAL_ROUTES

class UIError(RuntimeError):
    def __init__(self,code,original_routes=None):
        super().__init__(code);self.code=code;self.original_routes=original_routes

def walk(node):
    if isinstance(node,list):
        for n in node:yield from walk(n)
    elif isinstance(node,dict):
        yield node
        for n in node.get('children',[]):yield from walk(n)

def enabled(node):return 'disabled' not in node.get('states',[])

def system_default_route(value):
    """Recognize the exact Google Voice system-default label form.

    The suffix is the current macOS default device and can legitimately change
    when headphones disconnect. A named physical or virtual device is never
    treated as default merely because its name contains that word.
    """
    return isinstance(value,str) and re.fullmatch(r'Default - \S(?:[^\r\n]*\S)?',value) is not None

def route_equivalent(left,right):
    if not isinstance(left,str) or not isinstance(right,str) or not left or not right:return False
    return left==right or (system_default_route(left) and system_default_route(right))

def routes_equivalent(left,right):
    return (isinstance(left,dict) and isinstance(right,dict)
            and set(left)==set(right)=={'Microphone','Speakers'}
            and all(route_equivalent(left[key],right[key]) for key in left))

def number(text):
    if not isinstance(text,str) or not re.fullmatch(r'[+()\d\s.\-]+',text.strip()):return None
    digits=re.sub(r'\D','',text)
    if len(digits)==10:return '+1'+digits
    if len(digits)==11 and digits.startswith('1'):return '+'+digits
    if text.strip().startswith('+') and 8<=len(digits)<=15:return '+'+digits
    return None

def smallest_group(root, labels):
    """Narrowest single subtree with every exact control, excluding call history."""
    found=[]
    for node in walk(root):
        allnodes=list(walk(node))
        if all(sum(n.get('role')=='button' and n.get('name')==label and enabled(n) for n in allnodes)==1 for label in labels):
            found.append((len(allnodes),node))
    if not found:return None
    _,group=min(found,key=lambda v:v[0])
    # A match spanning the whole history page is not an incoming-call region.
    if len(list(walk(group)))>100:return None
    return group

def call_panel(web):
    panels=[n for n in walk(web) if n.get('role')=='group' and n.get('name')=='Call panel']
    return panels[0] if len(panels)==1 and len(list(walk(panels[0])))<=200 else None

def panel_caller(panel):
    # The live AX label includes this exact prefix. A history number, a date,
    # or a phone-shaped substring elsewhere must never establish the caller.
    fields=[n.get('name',n.get('value','')) for n in walk(panel) if n.get('role')=='statictext' and n.get('name',n.get('value','')).startswith('Call with ')]
    return (len(fields)==1,number(fields[0][len('Call with '):]) if len(fields)==1 else None)

def panel_controls(panel):
    result={}
    for label in ('Hold call','Mute call','Open keypad'):
        found=[n for n in walk(panel) if n.get('role') in ('button','checkbox') and n.get('name')==label]
        if len(found)!=1:return None
        result[label]=enabled(found[0])
    return result

def incoming_group(web,answer,decline):
    panel=call_panel(web)
    if panel is None:return None
    nodes=list(walk(panel));controls=panel_controls(panel)
    if controls is None or any(controls.values()):return None
    if sum(n.get('role')=='statictext' and n.get('name')=='Incoming call' for n in nodes)!=1:return None
    if not panel_caller(panel)[0]:return None
    for label in set((answer,decline)):
        if not label:return None
        # Exactly one action on the entire selected page and inside its call
        # panel. Another call's controls make this observation ambiguous.
        all_matches=[n for n in walk(web) if n.get('role')=='button' and n.get('name')==label and enabled(n)]
        matches=[n for n in nodes if n.get('role')=='button' and n.get('name')==label and enabled(n)]
        if len(matches)!=1 or len(all_matches)!=1:return None
    return panel

def active_group(web,hangup,answer='Answer call'):
    panel=call_panel(web)
    if panel is None:return None
    nodes=list(walk(panel));controls=panel_controls(panel)
    if controls is None or not all(controls.values()) or not panel_caller(panel)[0]:return None
    if any(n.get('role')=='statictext' and n.get('name')=='Incoming call' for n in nodes):return None
    if any(n.get('role')=='button' and n.get('name')==answer and enabled(n) for n in walk(web)):return None
    matches=[n for n in walk(web) if n.get('role')=='button' and n.get('name')==hangup and enabled(n)]
    if len(matches)!=1 or not any(n is matches[0] for n in nodes):return None
    return panel

def owned_active_group(web,hangup,caller,answer='Answer call'):
    panel=active_group(web,hangup,answer)
    return panel if panel is not None and caller is not None and panel_caller(panel)[1]==caller else None

class GoogleVoiceUI:
    def __init__(self,home,*,runner=subprocess.run,clock=time.time):
        self.home=Path(home);self.runner=runner;self.clock=clock
        self.config=read_json(self.home/'google-voice-ui.json')
        if not self.config:raise UIError('google_voice_window_not_enrolled')
        self.session='incoming-google-voice'
        self.episode_path=self.home/'ui-ring-episode.json'
        self.last=None

    def command(self,args,timeout=8):
        env=os.environ.copy()
        desktop=self.config.get('desktop',str(Path.home()/'.local/bin/agent-desktop'))
        env['PATH']=os.pathsep.join([str(Path(sys.executable).parent),str(Path(desktop).parent),env.get('PATH','/usr/bin:/bin')])
        try:r=self.runner(args,capture_output=True,text=True,timeout=timeout,env=env)
        except subprocess.TimeoutExpired:raise UIError('native_command_timed_out_delivery_unknown')
        try:return json.loads(r.stdout)
        except (ValueError,TypeError):raise UIError('native_response_unreadable')

    def focus_enrolled(self,budget_ms=6000):
        """Explicit arm-time recovery, never a side effect of reading status."""
        c=self.config
        target={key:c[key] for key in ('app','window_id','pid','process_instance')}
        diagnostic={'attempt_id':uuid.uuid4().hex,'started_at':self.clock(),'state':'starting',
                    'target':target,'budget_ms':budget_ms if type(budget_ms) is int else None}
        path=self.home/'last-native-focus.json'
        write_json(path,diagnostic)
        try:
            if type(budget_ms) is not int or not 1<=budget_ms<=15000:
                raise UIError('native_focus_budget_invalid')
            helper=Path(__file__).parent/'native-build'/'call-desk-focus'
            args=[str(helper),'--app',c['app'],'--window-id',c['window_id'],
                  '--pid',str(c['pid']),'--process-instance',c['process_instance'],
                  '--budget-ms',str(budget_ms)]
            diagnostic['state']='dispatched';write_json(path,diagnostic)
            try:receipt=self.command(args,timeout=budget_ms/1000+3)
            except OSError:raise UIError('native_focus_helper_unavailable') from None
            if not isinstance(receipt,dict):raise UIError('native_window_focus_unverified')
            # Whitelist scalars; never persist a raw UI tree, arbitrary helper
            # payload, window title, or external exception text in this record.
            summary={key:receipt[key] for key in ('ok','exact_window_focused','identity_verified',
                     'activation_dispatched','raise_dispatched') if type(receipt.get(key)) is bool}
            if type(receipt.get('elapsed_ms')) in (int,float) and math.isfinite(receipt['elapsed_ms']):
                summary['elapsed_ms']=receipt['elapsed_ms']
            native=receipt.get('diagnostic')
            if isinstance(native,dict):
                summary['diagnostic']={key:native[key] for key in ('phase','activation_accepted','resolution_attempts',
                    'last_ax_error','exact_focused','focused_window_id','frontmost_pid','onscreen','confirmations')
                    if type(native.get(key)) in (str,int,bool) and (not isinstance(native[key],str) or len(native[key])<=100)}
            error=receipt.get('error') or {}
            if isinstance(error,dict) and isinstance(error.get('code'),str):
                summary['error_code']=error['code'][:100]
            diagnostic['receipt']=summary
            if isinstance(error,dict) and error.get('code') in ('PERM_DENIED','POLICY_DENIED'):
                raise UIError(error['code'])
            if any(receipt.get(key) is not True for key in ('ok','exact_window_focused','identity_verified')):
                raise UIError('native_window_focus_unverified')
            actual=receipt.get('target')
            if not isinstance(actual,dict) or any(actual.get(key)!=c[key] for key in target):
                raise UIError('native_window_identity_changed')
            diagnostic['state']='verified'
            return receipt
        except Exception as exc:
            diagnostic.update(state='failed',error_code=getattr(exc,'code',type(exc).__name__))
            raise
        finally:
            diagnostic['finished_at']=self.clock()
            write_json(path,diagnostic)

    def raw(self,allow_audio=False):
        c=self.config
        budget=c.get('native_read_budget_ms',12000)
        if type(budget) is not int or not 1000<=budget<=15000:
            raise UIError('native_read_budget_invalid')
        pinned_reader=Path(__file__).with_name('pinned_reader.py')
        reader=c.get('reader',str(pinned_reader))
        # GUI/login-item PATH resolves env python3 to Apple's older Python.
        # This known Python helper must use the service's validated interpreter.
        is_pinned=Path(reader)==pinned_reader
        reader_command=[sys.executable,reader] if is_pinned or Path(reader).name=='elle-read' else [reader]
        identity_args=['--pid',str(c['pid']),'--process-instance',c['process_instance']] if is_pinned else []
        # Opening/closing Chrome's audio overlay can briefly invalidate AX.
        # Retry only this read, never the action that produced the transition.
        for attempt in range(2 if allow_audio else 1):
            try:
                d=self.command(reader_command+['observe','--app',c['app'],'--window-id',c['window_id'],*identity_args,'--session',self.session,'--budget-ms',str(budget),'--max-depth','45'],timeout=budget/1000+3)
            except UIError as exc:
                if not allow_audio or attempt or exc.code!='native_command_timed_out_delivery_unknown':raise
                continue
            if d.get('status')=='complete' and d.get('identity_verified'):break
            error=(d.get('error') or {}).get('code')
            if error in ('POLICY_DENIED','PERM_DENIED'):raise UIError(error)
            target=d.get('target') or {}
            if any(target.get(k) not in (None,c[k]) for k in ('window_id','pid','process_instance')):break
        if d.get('status')!='complete' or not d.get('identity_verified'):
            code=(d.get('error') or {}).get('code')
            if (code=='ACTION_NOT_SUPPORTED' and
                    (d.get('error',{}).get('details') or {}).get('kind')=='window_without_accessibility_element'):
                raise UIError('google_voice_controls_unavailable')
            raise UIError({'WINDOW_NOT_FOUND':'google_voice_window_unavailable',
                           'TARGET_CHANGED':'native_window_identity_changed',
                           'IDENTITY_UNAVAILABLE':'native_window_identity_unavailable',
                           'AMBIGUOUS_TARGET':'native_window_ambiguous'}.get(code,'native_observation_incomplete_or_wrong_window'))
        target=d.get('target') or {}
        if any(target.get(k)!=c[k] for k in ('window_id','pid','process_instance')):raise UIError('native_window_identity_changed')
        tree=((d.get('raw_snapshot') or {}).get('data') or {}).get('tree',[])
        nodes=list(walk(tree))
        urls=[n.get('value','') for n in nodes if n.get('role')=='textfield' and n.get('name')=='Address and search bar']
        if len(urls)!=1:raise UIError('google_voice_url_unavailable')
        url=urls[0] if '://' in urls[0] else 'https://'+urls[0]
        parsed=urlparse(url);expected=urlparse(c['url_prefix'])
        if parsed.scheme!='https' or parsed.netloc!=expected.netloc or not parsed.path.startswith(expected.path):raise UIError('google_voice_tab_changed')
        web=[n for n in nodes if n.get('role')=='webarea' and n.get('name','').startswith('Voice - ')]
        if len(web)!=1:raise UIError('google_voice_page_ambiguous')
        account=[n for n in walk(web[0]) if n.get('role')=='button' and n.get('name')==c['account_label']]
        audio=[n for n in walk(web[0]) if n.get('name')=='Audio settings panel']
        if len(account)!=1 and not (allow_audio and len(audio)==1):raise UIError('google_voice_account_unverified')
        return web[0],tree,d

    def _identity(self):
        return {k:self.config[k] for k in ('app','window_id','pid','process_instance','url_prefix','account_label')}

    def snapshot(self):
        now=self.clock()
        try:web,_,_=self.raw()
        except UIError as exc:
            return {'observed_at':now,'identity':self._identity(),'coverage':'partial','state':'unknown','caller_number':None,'ring_id':None,'evidence':exc.code}
        nodes=list(walk(web));c=self.config
        answer=c.get('answer_label');decline=c.get('decline_label');hangup=c.get('hangup_label')
        result={'observed_at':self.clock(),'identity':self._identity(),'coverage':'complete','state':'unknown','caller_number':None,'ring_id':None,'evidence':'Pinned Google Voice window inspected; state unresolved'}
        buttons=[n for n in nodes if n.get('role')=='button' and enabled(n)]
        answers=[n for n in buttons if answer and n.get('name')==answer]
        hangs=[n for n in buttons if hangup and n.get('name')==hangup]
        episode=read_json(self.episode_path,{})
        if len(answers)==1 and decline:
            group=incoming_group(web,answer,decline)
            if group is None:return result
            caller=panel_caller(group)[1]
            if episode.get('phase')!='ringing' or episode.get('caller_number')!=caller:
                episode={'phase':'ringing','caller_number':caller,'ring_id':'ring_'+uuid.uuid4().hex}
                write_json(self.episode_path,episode)
            result.update(state='ringing',caller_number=caller,ring_id=episode['ring_id'],evidence='Fresh incoming controls in pinned Google Voice window; caller '+('…'+caller[-4:] if caller else 'withheld/unavailable'))
        elif len(hangs)==1 and not answers:
            if active_group(web,hangup,answer) is None:return result
            # Preserve only an episode we actually dispatched an answer for.
            known=(episode.get('answer_dispatched') and episode.get('answer_delivery') in ('delivered_verified','delivered_unverified','unknown') and episode.get('phase') in ('ringing','active') and owned_active_group(web,hangup,episode.get('caller_number'),answer) is not None)
            if known:
                episode['phase']='active';write_json(self.episode_path,episode)
            result.update(state='active',caller_number=episode.get('caller_number') if known else None,ring_id=episode.get('ring_id') if known else None,evidence='Unique active-call hangup control observed in pinned Google Voice window')
        elif not answers and not hangs:
            idle=[n for n in nodes if n.get('role')=='button' and n.get('name')=='No contact selected' and 'disabled' in n.get('states',[])]
            panels=[n for n in nodes if n.get('name')=='Call panel']
            if len(idle)==1 and len(panels)==1:
                if episode.get('phase')!='idle':write_json(self.episode_path,{'phase':'idle'})
                result.update(state='idle',evidence='Pinned Google Voice call panel idle; no incoming-answer or active-call control')
        self.last=result
        return result

    def click(self,node):
        ref=node.get('ref_id')
        if not ref:raise UIError('control_has_no_action_reference')
        result=self.command([self.config.get('desktop',str(Path.home()/'.local/bin/agent-desktop')),'click',ref,'--session',self.session,'--timeout-ms','2500'],timeout=5)
        data=result.get('data') or {};err=result.get('error') or {}
        disp=data.get('disposition') or err.get('disposition') or {}
        return {'delivery':disp.get('delivery','unknown'),'ok':result.get('ok') is True,'error_code':err.get('code')}

    def answer(self,expected_ring_id):
        snap=self.snapshot()
        if snap['state']!='ringing' or snap.get('ring_id')!=expected_ring_id:raise UIError('ring_changed_before_answer')
        web,_,_=self.raw()
        group=incoming_group(web,self.config['answer_label'],self.config['decline_label'])
        if group is None:raise UIError('incoming_controls_no_longer_unique')
        nodes=[n for n in walk(group) if n.get('role')=='button' and n.get('name')==self.config['answer_label'] and enabled(n)]
        if panel_caller(group)[1]!=snap['caller_number']:raise UIError('caller_changed_before_answer')
        episode=read_json(self.episode_path,{})
        if episode.get('answer_dispatched'):raise UIError('answer_already_dispatched')
        episode['answer_dispatched']=True;episode['answer_delivery']='unknown';write_json(self.episode_path,episode)
        receipt=self.click(nodes[0]);episode['answer_delivery']=receipt['delivery']
        if receipt.get('error_code') in ('POLICY_DENIED','PERM_DENIED'):episode['answer_delivery']='not_delivered'
        write_json(self.episode_path,episode)
        return receipt

    def hangup(self):
        snap=self.snapshot();episode=read_json(self.episode_path,{})
        if snap['state']!='active' or not episode.get('answer_dispatched') or snap['ring_id']!=episode.get('ring_id'):raise UIError('active_call_not_owned')
        web,_,_=self.raw();group=owned_active_group(web,self.config['hangup_label'],episode.get('caller_number'),self.config['answer_label'])
        if group is None:raise UIError('active_caller_changed_before_hangup')
        nodes=[n for n in walk(group) if n.get('role')=='button' and n.get('name')==self.config['hangup_label'] and enabled(n)]
        if len(nodes)!=1:raise UIError('hangup_control_not_unique')
        return self.click(nodes[0])

    def audio_open(self):
        web,_,_=self.raw(allow_audio=True)
        if not any(n.get('name')=='Audio settings panel' for n in walk(web)):
            nodes=[n for n in walk(web) if n.get('role')=='button' and n.get('name')=='Audio settings']
            if len(nodes)!=1:raise UIError('audio_settings_button_not_unique')
            receipt=self.click(nodes[0])
            if receipt.get('error_code') in ('POLICY_DENIED','PERM_DENIED'):raise UIError(receipt['error_code'])
            # AXPress can return before Chrome paints its overlay. Observe the
            # result of this one click; never click again to guess at a toggle.
            for _ in range(4):
                web,_,_=self.raw(allow_audio=True)
                if any(n.get('name')=='Audio settings panel' for n in walk(web)):break
                time.sleep(.15)
        panels=[n for n in walk(web) if n.get('name')=='Audio settings panel']
        if len(panels)!=1:raise UIError('audio_settings_did_not_open')
        return panels[0]

    def routes(self):
        panel=self.audio_open();result={}
        for name in ('Microphone','Speakers'):
            nodes=[n for n in walk(panel) if n.get('role')=='combobox' and n.get('name')==name]
            if len(nodes)!=1 or not nodes[0].get('value'):raise UIError('audio_route_not_readable')
            result[name]=nodes[0]['value']
        return result

    def select_route(self,name,value):
        panel=self.audio_open();nodes=[n for n in walk(panel) if n.get('role')=='combobox' and n.get('name')==name]
        if len(nodes)!=1:raise UIError('route_selector_ambiguous')
        if route_equivalent(nodes[0].get('value'),value):return
        if 'expanded' not in nodes[0].get('states',[]):
            receipt=self.click(nodes[0])
            if receipt.get('error_code') in ('POLICY_DENIED','PERM_DENIED'):raise UIError(receipt['error_code'])
        web,_,_=self.raw(allow_audio=True)
        lists=[n for n in walk(web) if n.get('role')=='list' and n.get('name')==name]
        for _ in range(2):
            if lists:break
            time.sleep(.1)
            web,_,_=self.raw(allow_audio=True)
            lists=[n for n in walk(web) if n.get('role')=='list' and n.get('name')==name]
        if len(lists)!=1:raise UIError('audio_option_list_not_unique')
        # Resolve the stable default role only within this selector's visible
        # list, and only when exactly one selectable default option exists.
        # All explicitly named hardware/virtual devices still match exactly.
        choices=[n for n in walk(lists[0]) if route_equivalent(n.get('name'),value)
                 and 'Click' in n.get('available_actions',[]) and enabled(n)]
        if len(choices)!=1:raise UIError('audio_device_option_not_unique')
        receipt=self.click(choices[0])
        if receipt.get('error_code') in ('POLICY_DENIED','PERM_DENIED'):raise UIError(receipt['error_code'])
        # Selection receipt isn't proof; always check the actual resulting value.
        for _ in range(3):
            if route_equivalent(self.routes().get(name),value):return
            time.sleep(.15)
        raise UIError('audio_selection_not_verified')

    def audio_close(self):
        web,_,_=self.raw(allow_audio=True)
        if not any(n.get('name')=='Audio settings panel' for n in walk(web)):return
        self.focus_enrolled()
        # An open device list consumes Escape before the dialog can close.
        # Inspect each transition instead of blindly dispatching two keys.
        for _ in range(2):
            web,_,_=self.raw(allow_audio=True)
            if not any(n.get('name')=='Audio settings panel' for n in walk(web)):return
            expanded=any(n.get('role')=='combobox' and 'expanded' in n.get('states',[]) for n in walk(web))
            receipt=self.command([self.config.get('desktop',str(Path.home()/'.local/bin/agent-desktop')),'--headed','press','escape','--window-id',self.config['window_id'],'--session',self.session],timeout=5)
            if (receipt.get('error') or {}).get('code') in ('POLICY_DENIED','PERM_DENIED'):raise UIError(receipt['error']['code'])
            for _ in range(6):
                time.sleep(.1)
                web,_,_=self.raw(allow_audio=True)
                has_panel=any(n.get('name')=='Audio settings panel' for n in walk(web))
                still_expanded=any(n.get('role')=='combobox' and 'expanded' in n.get('states',[]) for n in walk(web))
                if not has_panel or (expanded and not still_expanded):break
            if not any(n.get('name')=='Audio settings panel' for n in walk(web)):return
            if not expanded or any(n.get('role')=='combobox' and 'expanded' in n.get('states',[]) for n in walk(web)):break
        raise UIError('audio_panel_did_not_close')

    def prepare_audio(self):
        original=None
        try:
            original=self.routes()
            write_json(self.home/'incoming-original-routes.json',{'routes':original,'identity':self._identity(),'at':self.clock()})
            if routes_equivalent(original,VIRTUAL_ROUTES):
                actual=original
            else:
                for name,value in VIRTUAL_ROUTES.items():self.select_route(name,value)
                actual=self.routes()
            self.audio_close()
            return {'verified':routes_equivalent(actual,VIRTUAL_ROUTES),'original_routes':original,'evidence':'Both virtual audio selections verified from live controls'}
        except Exception as exc:
            raise UIError(getattr(exc,'code',type(exc).__name__),original_routes=original) from None

    def restore_audio(self,original_routes):
        for name in ('Microphone','Speakers'):self.select_route(name,original_routes[name])
        actual=self.routes();self.audio_close()
        return {'verified':routes_equivalent(actual,original_routes),'actual_routes':actual,'evidence':'Original microphone and speaker selections re-read; system-default roles preserved'}

    def verify_audio_routes(self,expected_routes):
        """Inspect the selected routes without selecting either device."""
        actual=self.routes();self.audio_close()
        return {'verified':routes_equivalent(actual,expected_routes),'actual_routes':actual,
                'evidence':'Configured microphone and speaker selections read from live controls; no device selection dispatched'}
