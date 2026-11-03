#!/usr/bin/env python3
"""Private, local JSON interface for the Call Desk menu-bar app.

Periodic status reads never retrieve credentials, open audio, or contact a
provider. Only an explicit arm action can authorize the saved one-call test.
"""
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
import time
from service import CallService, CallError, read_json, write_json, TZ
import incoming_control
from audio_policy import lease_released, KEEP_VIRTUAL

def error_message(exc):
    if type(exc).__module__=='messaging':
        return {
            'messaging_source_not_enrolled':'Messaging needs a verified Google Voice account and phone number.',
            'messaging_ui_not_installed':'Live messaging is not installed yet. Drafts remain local.',
            'recipient_requires_exact_e164':'Enter the full recipient number beginning with + and the country code.',
            'invalid_body':'Enter a message of 1 to 1600 characters.',
            'conversation_not_cached':'This conversation is not cached yet. Use Refresh conversation to read it.',
            'reply_context_unverified':'Refresh the intended conversation and select the message to reply to again.',
            'message_source_identity_changed':'The connected account or window changed. Review the connection before sending.',
            'cached_message_source_changed':'These cached messages belong to an earlier connection. Refresh the inbox.',
            'draft_digest_changed':'The reviewed draft changed. Review the exact recipient and text again.',
            'message_browser_cleanup_unverified':'Messaging could not verify its return to the Calls page. The browser remains reserved for inspection.',
        }.get(getattr(exc,'code',None),'Messaging stopped before a verified result. '+str(getattr(exc,'code','')).replace('_',' '))
    from google_voice_ui import UIError
    if isinstance(exc,UIError):
        return {
            'PERM_DENIED':'macOS has not granted Call Desk Device Control and Data Access (Accessibility). Enable Call Desk in System Settings > Privacy & Security > Device Control and Data Access, then check connections.',
            'POLICY_DENIED':'The desktop permission policy blocked this action. Call Desk stopped before continuing.',
            'native_window_focus_unverified':'Call Desk could not bring the connected Google Voice window forward. No new call started.',
            'native_focus_helper_unavailable':'The local window-control helper is unavailable. Rebuild the Call Desk native helper before arming.',
            'audio_settings_did_not_open':'Google Voice did not open its audio settings. No call started. Open Google Voice, then check connections.',
            'native_observation_incomplete_or_wrong_window':'Google Voice could not be read completely. No new call was answered; check the Chrome window and try Check connections.',
            'google_voice_window_unavailable':'The connected Google Voice window is unavailable. Bring that Chrome window onscreen, then check connections.',
            'google_voice_controls_unavailable':'Chrome is not exposing controls for the connected Google Voice window. Bring that window onscreen, then check connections.',
            'native_window_identity_changed':'Chrome restarted or the connected window changed. Reconnect Google Voice before arming.',
            'native_window_ambiguous':'The connected browser window could not be identified uniquely. No call started.',
            'native_command_timed_out_delivery_unknown':'The browser control timed out. Check connections before repeating setup.',
            'google_voice_window_not_enrolled':'An agent needs to connect the signed-in Google Voice window.',
            'audio_device_option_not_unique':'The requested audio device is missing or ambiguous. Check the audio-device rows.',
        }.get(exc.code,'Google Voice setup failed: '+exc.code.replace('_',' '))
    return str(exc) if isinstance(exc,(CallError,ValueError)) else type(exc).__name__


def routing_status(service,incoming,last_action):
    """Surface unresolved routing separately from the answering toggle."""
    lease=read_json(service.home/'incoming-audio-lease.json',{})
    stage=lease.get('status',incoming.get('audio_lease_status'))
    released=lease_released(lease)
    unresolved=stage is not None and not released
    # Old status-only snapshots used 'restored'; persistent release always
    # requires its full verified receipt, never just a terminal-looking word.
    if not lease and stage=='restored':unresolved=False
    persistent=released and lease.get('status')=='released'
    cleanup=unresolved and (stage=='cleanup_required' or incoming.get('phase')=='cleanup_required'
                            or (not incoming.get('enabled') and not incoming.get('running')))
    cause=None
    if cleanup:
        # Keep the specific native failure instead of replacing it with the
        # generic exception class or the fact that answering is switched off.
        code=lease.get('error_code')
        cause=lease.get('error') or lease.get('reason')
        if not cause and code:
            cause={
                'audio_settings_did_not_open':'Google Voice did not open its audio settings.',
                'audio_panel_did_not_close':'Google Voice did not close its audio settings panel.',
                'audio_device_option_not_unique':'The requested audio device is missing or ambiguous.',
                'audio_selection_not_verified':'The selected microphone or speaker route could not be verified.',
            }.get(code,'Audio routing error: '+str(code).replace('_',' '))
        if not cause:
            source=last_action.get('error_action',last_action.get('action'))
            cause=(last_action.get('error') if source in ('arm','disarm') else None) or incoming.get('reason')
        if not cause and lease.get('error_type') not in (None,'UIError','CallError'):
            cause='Audio setup failed: '+lease['error_type']
        if not cause:
            cause=('The configured virtual microphone and speaker routing has not been verified.'
                   if lease.get('audio_cleanup_policy')==KEEP_VIRTUAL else
                   'The original microphone and speaker routing has not been verified restored.')
    return {'lease_status':stage,'unresolved':unresolved,'needs_cleanup':cleanup,'cause':cause,
            'persistent_defaults':persistent}


def displayed_error(service,incoming,active,last_action):
    """Hide a resolved asynchronous Stop error without editing its receipt."""
    error=last_action.get('error')
    source=last_action.get('error_action',last_action.get('action'))
    if (not error or last_action.get('action')!='disarm' or last_action.get('ok') is not True
            or source not in ('arm','disarm') or incoming.get('enabled') is not False
            or incoming.get('running') is not False or active is not None):
        return error
    target=last_action.get('disarm_target_armed_id')
    if not target:return error
    lease=read_json(service.home/'incoming-audio-lease.json',{})
    settings=read_json(service.home/'incoming-settings.json',{})
    current=read_json(service.home/'incoming-state.json',{}).get('current')
    evidence=lease.get('audio_cleanup_evidence') if lease.get('status')=='released' else lease.get('restoration_evidence')
    if (current or settings.get('enabled') is not False or not lease_released(lease)
            or not isinstance(evidence,str) or not evidence.strip()
            or not incoming_control.same_arm(incoming.get('armed_id'),target)
            or not incoming_control.same_arm(settings.get('armed_id'),target)
            or not incoming_control.same_arm(lease.get('armed_id'),target)):
        return error
    return None


def desktop_status(service):
    check=service.preflight()
    incoming=incoming_control.status(service)
    last_action=read_json(service.home/'desktop-last-action.json',{})
    routing=routing_status(service,incoming,last_action)
    cached=read_json(service.home/'desktop-health.json',{})
    age=time.time()-cached.get('checked_at',0)
    browser=cached.get('browser',{})
    browser_fresh=0<=age<60
    browser_error={'native_observation_incomplete_or_wrong_window':'The browser read was incomplete. Check connections again.',
        'google_voice_window_unavailable':'The connected Google Voice window is unavailable. Bring it onscreen and check connections.',
        'google_voice_controls_unavailable':'Chrome is not exposing this window\'s controls. Bring Google Voice onscreen and check connections.',
        'native_window_identity_unavailable':'Chrome did not expose the connected window identity. Check connections again.',
        'native_window_ambiguous':'The connected Chrome window could not be identified uniquely.',
        'native_window_identity_changed':'Chrome restarted or the enrolled window changed. An agent must reconnect it.',
        'google_voice_tab_changed':'Open Google Voice in the enrolled Chrome window.',
        'google_voice_account_unverified':'The expected Google account could not be verified.'}.get(browser.get('evidence'),'The enrolled browser is not verified.')
    active=check.get('active_call')
    browser_operation=check.get('browser_operation')
    watcher_fresh=incoming.get('status_age_seconds') is not None and incoming['status_age_seconds']<8
    checks=[
        {'id':'service','label':'Shared call service','state':'ok',
         'detail':'Local service responds. Codex and Claude use the same call reservation.'},
        {'id':'devices','label':'Audio devices','state':'ok' if not check['errors'] else 'attention',
         'detail':'Both virtual audio devices are available.' if not check['errors'] else '; '.join(check['errors'])},
        {'id':'browser','label':'Google Voice','state':'ok' if browser_fresh and browser.get('coverage')=='complete' and browser.get('state') in ('idle','ringing','active') else 'unknown',
         'detail':('Verified '+browser['state']+' '+str(int(age))+' seconds ago.') if browser_fresh and browser.get('coverage')=='complete' else (browser_error if browser_fresh else 'Use Check connections to verify the enrolled Chrome window.')},
        {'id':'voice','label':'Voice engine','state':'ok' if active and active.get('provider_connected') else 'off',
         'detail':'Provider session connected.' if active and active.get('provider_connected') else 'Standby. Credentials and provider connection are checked when a call starts.'},
        {'id':'agents','label':'Agent access','state':'ok' if active else 'off',
         'detail':('Current owner: '+active['owner']) if active else 'Shared MCP interface available. No agent currently owns a call; idle client connections are not monitored.'},
        {'id':'incoming','label':'Automatic answering','state':'ok' if incoming['running'] and watcher_fresh and incoming['phase']=='watching' else ('attention' if incoming['enabled'] or routing['unresolved'] else 'off'),
         'detail':('Audio routing needs cleanup. '+routing['cause']) if routing['needs_cleanup'] else (('One test call from '+', '.join(incoming['allowed_callers'])+'.') if incoming['running'] and incoming['enabled'] else ('Off. Virtual audio defaults remain configured.' if routing['persistent_defaults'] else 'Off. Starting this app does not arm answering.'))},
    ]
    if routing['needs_cleanup']:
        checks[-1]['state']='attention'
    checks.append({'id':'messaging','label':'Text messaging','state':'busy' if browser_operation else 'off',
        'detail':('Browser reserved by '+str(browser_operation.get('owner','messaging'))+'. Calls wait for verified return to the Calls page.')
                 if browser_operation else 'Messages open from local cache. Live refresh and sending are explicit actions.'})
    if routing['needs_cleanup'] and (not active or active.get('phone_state')!='answered'):
        overall={'state':'attention','label':'Audio routing needs cleanup','detail':routing['cause']}
    elif active:
        overall={'state':'busy','label':'Call in progress' if active.get('phone_state')=='answered' else 'Call setup / cleanup',
                 'detail':active['owner']+' · '+active['phase']}
    elif browser_operation:
        overall={'state':'busy','label':'Browser reserved for messaging',
                 'detail':'The messaging operation must verify browser cleanup before a call can start.'}
    elif any(c['state']=='attention' for c in checks):
        overall={'state':'attention','label':'Needs attention','detail':incoming.get('reason') or 'Check the connection details below.'}
    elif incoming['running'] and incoming['enabled']:
        overall={'state':'ready','label':'Test answering armed','detail':'One call only. Provider audio is verified during call setup.'}
    else:
        overall={'state':'off','label':'Answering off','detail':('Virtual audio defaults remain configured. No call is armed.' if routing['persistent_defaults'] else 'Call Desk is running. Arm a test when ready.')}
    return {'overall':overall,'checks':checks,'incoming':incoming,'active_call':active,'routing':routing,
            'browser_operation':browser_operation,
            'updated_at':dt.datetime.now(TZ).isoformat(),
            'last_error':displayed_error(service,incoming,active,last_action)}


def diagnose(service):
    from google_voice_ui import GoogleVoiceUI
    from browser_operation import navigation_guard
    with navigation_guard(service):
        try:
            snap=GoogleVoiceUI(service.home).snapshot()
        except Exception as exc:
            snap={'coverage':'partial','state':'unknown','evidence':getattr(exc,'code',type(exc).__name__)}
    write_json(service.home/'desktop-health.json',{'checked_at':time.time(),
               'browser':{k:snap.get(k) for k in ('coverage','state','evidence')}})
    return desktop_status(service)


def show_voice(service):
    """Show only the enrolled native window and recheck its page/account.

    This is recovery, not browser enrollment: never launch a URL, create a
    window, search another window, or rewrite the saved enrollment.
    """
    from google_voice_ui import GoogleVoiceUI,UIError
    from browser_operation import navigation_guard
    with navigation_guard(service):
        adapter=GoogleVoiceUI(service.home)
        # Do not replace snapshots/refs used by an incoming watcher. The same
        # control lock prevents simultaneous Show requests in this namespace.
        adapter.session='call-desk-show'
        adapter.focus_enrolled()
        # snapshot uses raw(allow_audio=False), independently checking native
        # identity, HTTPS URL, Voice webarea, and the enrolled account control.
        snap=adapter.snapshot()
        if snap.get('coverage')!='complete':
            raise UIError(snap.get('evidence') or 'native_observation_incomplete_or_wrong_window')
        if snap.get('identity')!=adapter._identity():
            raise UIError('native_window_identity_changed')
        write_json(service.home/'desktop-health.json',{'checked_at':time.time(),
                   'browser':{k:snap.get(k) for k in ('coverage','state','evidence')}})
        return desktop_status(service)


def verified_arm_preparation(service,result):
    """Prepared routing resolves a prior setup error, not watcher readiness."""
    incoming=result.get('incoming') or {}
    routing=result.get('routing') or {}
    if (incoming.get('enabled') is not True or routing.get('needs_cleanup') is not False
            or routing.get('lease_status')!='prepared'
            or incoming.get('phase') not in ('launch_pending','starting','watching')):
        return False
    lease=read_json(service.home/'incoming-audio-lease.json',{})
    settings=read_json(service.home/'incoming-settings.json',{})
    evidence=lease.get('preparation_evidence')
    return (settings.get('enabled') is True and lease.get('status')=='prepared'
            and isinstance(evidence,str) and bool(evidence.strip())
            and incoming_control.same_arm(incoming.get('armed_id'),lease.get('armed_id'))
            and incoming_control.same_arm(settings.get('armed_id'),lease.get('armed_id')))


def record_success(service,action,result,*,disarm_receipt=None):
    """A successful read/diagnosis does not resolve an unrelated setup error."""
    previous=read_json(service.home/'desktop-last-action.json',{})
    # A later explicit action must not resurrect a Stop error that the pure
    # status projection already considers resolved by matching cleanup proof.
    error=displayed_error(service,result.get('incoming') or {},result.get('active_call'),previous)
    source=previous.get('error_action',previous.get('action'))
    unresolved=(result.get('routing') or {}).get('unresolved',False)
    browser_ok=any(c.get('id')=='browser' and c.get('state')=='ok' for c in result.get('checks',[]))
    # A prepared arm deliberately owns live routes. That is not unresolved
    # cleanup, and must not keep the previous arm's focus/setup error visible.
    if error and action=='arm' and source=='arm' and verified_arm_preparation(service,result):
        error=None
    elif error and not unresolved:
        if action=='diagnose' and source=='diagnose' and browser_ok:
            error=None
        elif action=='show-voice' and source=='show-voice' and browser_ok:
            error=None
        elif action=='disarm' and source in ('arm','disarm'):
            error=None
    record={'action':action,'at':time.time(),'ok':True}
    if action=='disarm' and isinstance(disarm_receipt,dict):
        # Bind this completed command to its own cancellation receipt, rather
        # than whichever arm happens to exist when a later status read runs.
        cancellation=read_json(service.home/'incoming-disarm-request.json',{})
        token=disarm_receipt.get('cancel_token')
        if token and cancellation.get('token')==token:
            target=cancellation.get('target_armed_id')
            if incoming_control.same_arm(target,target):
                record.update(disarm_target_armed_id=target,disarm_request_token=token)
    if error:
        record.update(error=error,error_action=source,
                      error_at=previous.get('error_at',previous.get('at')))
    write_json(service.home/'desktop-last-action.json',record)
    result['last_error']=error
    return result


def outside_spend(service):
    """Use today's named ledger and explicit outside commitments, fail stale."""
    cfg=read_json(service.home/'desktop-test-policy.json',{})
    today=dt.datetime.now(TZ).date().isoformat()
    if cfg.get('budget_date')!=today:
        raise CallError('Today\'s outside spending has not been reconciled. Ask an agent to refresh the test budget before arming.')
    floor=float(cfg['outside_commitment_floor_usd'])
    path=Path(cfg['ledger_directory'])/('elle-media-ledger-'+today+'.json')
    ledger=read_json(path,{})
    if ledger.get('local_date')!=today:raise CallError('Today\'s media ledger is unavailable.')
    total=0.0
    for job in ledger.get('jobs',[]):
        if job.get('status') in ('quoted','planned','failed_not_billed'):continue
        cost=job.get('actual_cost_usd')
        if cost is None:cost=job.get('quoted_cost_usd',job.get('estimated_cost_usd',0))
        total+=float(cost)
    return max(floor,total)


def arm_test(service):
    cfg=read_json(service.home/'desktop-test-policy.json',{})
    if cfg.get('mode')!='own_cell_test' or len(cfg.get('allowed_numbers',[]))!=1:
        raise CallError('An agent must configure the authorized cell-only test first.')
    return incoming_control.arm(service,owner='elle',allowed_numbers=cfg['allowed_numbers'],
        brief=cfg['brief'],authorization=cfg['authorization'],
        other_spend_today_usd=outside_spend(service),minutes=10,max_calls=1,max_seconds=120)


TEXT_ACTIONS={
    'text-inbox':('inbox',{'limit','cursor','refresh'}),
    'text-read':('read',{'conversation_id','limit','before','refresh'}),
    'text-draft':('draft',{'owner','request_key','recipient','body','reply_to_message_id'}),
    'text-send':('send',{'draft_id','owner','expected_digest','authorization'}),
    'text-status':('status',{'draft_id','refresh'}),
    'text-recover-browser':('recover_browser',{'operation_token','owner','termination_evidence'}),
}


def read_text_payload(stream=None):
    """Bounded private stdin JSON; message bodies never enter command arguments."""
    stream=stream if stream is not None else sys.stdin.buffer
    raw=stream.read(65_537)
    if len(raw)>65_536:raise ValueError('The messaging request is too large.')
    if not raw:return {}
    try:
        def reject_constant(_):raise ValueError('Invalid JSON constant')
        data=json.loads(raw,parse_constant=reject_constant)
    except (ValueError,UnicodeError):
        raise ValueError('The messaging request must be valid JSON.') from None
    if not isinstance(data,dict):raise ValueError('The messaging request must be a JSON object.')
    return data


def text_action(service,action,payload):
    """Dispatch one explicit text operation without touching voice status receipts."""
    from messaging import TextService
    if action not in TEXT_ACTIONS:raise ValueError('Unknown messaging action.')
    method,allowed=TEXT_ACTIONS[action]
    if not isinstance(payload,dict) or set(payload)-allowed:
        raise ValueError('The messaging request contains unsupported fields.')
    if 'refresh' in payload and type(payload['refresh']) is not bool:
        raise ValueError('Refresh must be true or false.')
    return getattr(TextService(service),method)(**payload)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['status','diagnose','show-voice','arm','disarm',*TEXT_ACTIONS])
    args=parser.parse_args();service=CallService();os.umask(0o077)
    try:
        if args.action in TEXT_ACTIONS:
            print(json.dumps(text_action(service,args.action,read_text_payload())))
            return 0
        disarm_receipt=None
        if args.action=='status':result=desktop_status(service)
        elif args.action=='diagnose':result=diagnose(service)
        elif args.action=='show-voice':result=show_voice(service)
        elif args.action=='arm':
            arm_test(service);result=desktop_status(service)
        else:
            disarm_receipt=incoming_control.disarm(service)
            # Also stop the active voice stream if a different local agent owns
            # it. This explicit button is a stop request, never a phone hangup.
            with service.locked() as registry:active=registry.get('active')
            if active:service.stop(active)
            result=desktop_status(service)
        if args.action!='status':
            result=record_success(service,args.action,result,disarm_receipt=disarm_receipt)
        print(json.dumps(result))
    except Exception as exc:
        error=error_message(exc)
        if args.action not in TEXT_ACTIONS:
            write_json(service.home/'desktop-last-action.json',{'action':args.action,'at':time.time(),'ok':False,'error':error,'error_action':args.action,'error_at':time.time()})
        print(json.dumps({'error':error}));return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
