import json
from pathlib import Path
import pytest
from google_voice_ui import GoogleVoiceUI,number,UIError,walk,route_equivalent,routes_equivalent,system_default_route
from service import write_json

def node(role,name='',children=None,**extra):return dict(role=role,name=name,children=children or [],**extra)
def incoming(caller='(314) 555-0123'):
    return node('webarea','Voice - Calls',[
        node('group','History',[node('statictext','+1 212 555 0123')]),
        node('group','Call panel',[node('group','Ongoing call',[
            node('statictext','Call with '+caller),node('statictext','Incoming call'),
            node('checkbox','Hold call',states=['disabled']),
            node('checkbox','Mute call',states=['disabled']),
            node('button','Open keypad',states=['disabled']),
            node('button','Answer call',ref_id='answer'),node('button','Hang up call',ref_id='decline')])])])
def active(caller='(314) 555-0123'):
    return node('webarea','Voice - Calls',[node('group','Call panel',[
        node('statictext','Call with '+caller),node('checkbox','Hold call'),
        node('checkbox','Mute call'),node('button','Open keypad'),
        node('button','Hang up call',ref_id='hangup')])])
def idle():return node('webarea','Voice - Calls',[node('group','Call panel',[node('button','No contact selected',states=['disabled'])])])
@pytest.fixture
def ui(tmp_path):
    write_json(tmp_path/'google-voice-ui.json',dict(app='Google Chrome',window_id='w-test',pid=1,process_instance='test',url_prefix='https://voice.google.com/u/0/',account_label='test account',answer_label='Answer call',decline_label='Hang up call',hangup_label='Hang up call'))
    u=GoogleVoiceUI(tmp_path);return u

def test_only_incoming_region_provides_number(ui):
    ui.raw=lambda **kw:(incoming(),None,None)
    s=ui.snapshot();assert s['caller_number']=='+13145550123' and s['state']=='ringing'

def test_episode_stable_until_observed_idle(ui):
    ui.raw=lambda **kw:(incoming(),None,None)
    first=ui.snapshot()['ring_id'];assert ui.snapshot()['ring_id']==first
    ui.raw=lambda **kw:(idle(),None,None);assert ui.snapshot()['state']=='idle'
    ui.raw=lambda **kw:(incoming(),None,None);assert ui.snapshot()['ring_id']!=first

def test_history_cannot_trigger_answer(ui):
    web=idle();web['children'].append(node('checkbox','Incoming call from +1 314 555 0123. Answer'))
    ui.raw=lambda **kw:(web,None,None)
    assert ui.snapshot()['state']=='idle'

def test_ambiguous_answer_not_actionable(ui):
    web=incoming();web['children'].append(node('button','Answer call'))
    ui.raw=lambda **kw:(web,None,None)
    assert ui.snapshot()['state']=='unknown'

def test_unknown_caller_not_borrowed_from_history(ui):
    ui.raw=lambda **kw:(incoming('Private number'),None,None)
    assert ui.snapshot()['caller_number'] is None

def test_two_numbers_in_region_fail_closed(ui):
    web=incoming();web['children'][1]['children'].append(node('statictext','Call with +13145550999'))
    ui.raw=lambda **kw:(web,None,None)
    assert ui.snapshot()['state']=='unknown'

def test_answer_once_and_active_episode(ui):
    ui.raw=lambda **kw:(incoming(),None,None)
    s=ui.snapshot();called=[];ui.click=lambda n:called.append(n) or {'delivery':'delivered_unverified'}
    ui.answer(s['ring_id']);assert len(called)==1
    with pytest.raises(UIError,match='already_dispatched'):ui.answer(s['ring_id'])
    ui.raw=lambda **kw:(active(),None,None)
    assert ui.snapshot()['ring_id']==s['ring_id']

def test_different_caller_aborts_answer(ui):
    ui.raw=lambda **kw:(incoming(),None,None);first=ui.snapshot()['ring_id']
    ui.raw=lambda **kw:(incoming('+13145550999'),None,None)
    with pytest.raises(UIError,match='ring_changed'):ui.answer(first)

def test_unowned_active_call_never_hangs_up(ui):
    ui.raw=lambda **kw:(active(),None,None)
    assert ui.snapshot()['ring_id'] is None
    with pytest.raises(UIError,match='not_owned'):ui.hangup()

@pytest.mark.parametrize('raw,expected',[('3 1 4 5 5 5 0 1 2 3','+13145550123'),('2 2 0 0 0',None),('Incoming +1 3145550123',None),('+44 7700 900123','+447700900123')])
def test_number_requires_whole_visible_field(raw,expected):assert number(raw)==expected


def test_failed_answer_never_claims_later_active_call(ui):
    ui.raw=lambda **kw:(incoming(),None,None)
    ring=ui.snapshot()['ring_id'];ui.click=lambda n:{'delivery':'not_delivered','ok':False}
    ui.answer(ring)
    ui.raw=lambda **kw:(active(),None,None)
    assert ui.snapshot()['ring_id'] is None

def test_unrelated_active_number_never_claimed(ui):
    ui.raw=lambda **kw:(incoming(),None,None)
    ring=ui.snapshot()['ring_id'];ui.click=lambda n:{'delivery':'delivered_unverified','ok':True}
    ui.answer(ring)
    ui.raw=lambda **kw:(active('(314) 555-9999'),None,None)
    assert ui.snapshot()['ring_id'] is None

def test_call_waiting_is_ambiguous(ui):
    web=incoming();web['children'].append(node('button','Hang up call'))
    ui.raw=lambda **kw:(web,None,None)
    assert ui.snapshot()['state']=='unknown'


def test_sanitized_actual_incoming_panel_with_shared_hangup_label(ui):
    panel=json.loads((Path(__file__).parent/'test-fixtures/incoming-call-panel-sanitized.json').read_text())
    web=node('webarea','Voice - Calls',[
        node('group','History',[node('statictext','Call with +12125550999')]),panel])
    ui.raw=lambda **kw:(web,None,None)
    snap=ui.snapshot()
    assert snap['state']=='ringing' and snap['caller_number']=='+13145550123'
    called=[];ui.click=lambda n:called.append(n) or {'delivery':'delivered_unverified'}
    ui.answer(snap['ring_id'])
    assert len(called)==1 and called[0]['name']=='Answer call'


@pytest.mark.parametrize('label',['Hold call','Mute call','Open keypad'])
def test_enabled_active_control_with_answer_is_call_waiting_not_new_ring(ui,label):
    web=incoming()
    next(n for n in walk(web) if n.get('name')==label)['states']=[]
    ui.raw=lambda **kw:(web,None,None)
    assert ui.snapshot()['state']=='unknown'


def test_caller_prefix_required_inside_call_panel(ui):
    web=incoming()
    next(n for n in walk(web) if n.get('name','').startswith('Call with '))['name']='(314) 555-0123'
    ui.raw=lambda **kw:(web,None,None)
    assert ui.snapshot()['state']=='unknown'


def test_incoming_marker_cannot_be_mistaken_for_active(ui):
    web=incoming()
    group=web['children'][1]['children'][0]
    group['children']=[n for n in group['children'] if n.get('name')!='Answer call']
    ui.raw=lambda **kw:(web,None,None)
    assert ui.snapshot()['state']=='unknown'


@pytest.mark.parametrize('value',['Default - AirPods (Bluetooth)','Default - MacBook Pro Speakers (Built-in)'])
def test_default_route_label_recognized(value):
    assert system_default_route(value)


@pytest.mark.parametrize('value',['My Default - Headphones','Default Speaker','Default - ',
    'default - MacBook Pro Speakers','Default - Device\nOther device',' Default - AirPods',None])
def test_default_route_recognition_is_not_substring_matching(value):
    assert not system_default_route(value)


def test_default_role_equivalence_does_not_substitute_explicit_hardware():
    assert route_equivalent('Default - AirPods (Bluetooth)','Default - MacBook Pro Speakers (Built-in)')
    assert not route_equivalent('AirPods (Bluetooth)','Default - AirPods (Bluetooth)')
    assert not route_equivalent('AirPods (Bluetooth)','MacBook Pro Speakers (Built-in)')
    assert not route_equivalent('Elle Call Mic (Virtual)','Google Voice Return (Virtual)')
    assert routes_equivalent({'Microphone':'Built-in','Speakers':'Default - AirPods'},
                             {'Microphone':'Built-in','Speakers':'Default - MacBook'})
    assert not routes_equivalent({'Microphone':'Built-in','Speakers':'Default - AirPods'},
                                 {'Speakers':'Default - MacBook'})
    assert not routes_equivalent({}, {})


def route_selector(ui,options,current='Google Voice Return (Virtual)'):
    selected={'Speakers':current};clicked=[]
    ui.audio_open=lambda:node('group','Audio settings panel',[
        node('combobox','Speakers',value=selected['Speakers'],states=['expanded'])])
    ui.raw=lambda **kw:(node('webarea','Voice - Calls',[
        node('list','Speakers',[node('listitem',value,available_actions=['Click']) for value in options])]),None,None)
    def click(option):
        clicked.append(option['name']);selected['Speakers']=option['name']
        return {'delivery':'delivered_verified'}
    ui.click=click;ui.routes=lambda:dict(selected)
    return selected,clicked


def test_restoration_selects_unique_current_system_default(ui):
    selected,clicked=route_selector(ui,['Default - MacBook Pro Speakers (Built-in)',
        'MacBook Pro Speakers (Built-in)','Google Voice Return (Virtual)'])
    ui.select_route('Speakers','Default - AirPods (Bluetooth)')
    assert clicked==['Default - MacBook Pro Speakers (Built-in)']
    assert selected['Speakers']=='Default - MacBook Pro Speakers (Built-in)'


def test_two_default_options_fail_even_when_one_exactly_matches_old_name(ui):
    _,clicked=route_selector(ui,['Default - AirPods (Bluetooth)','Default - MacBook Pro Speakers (Built-in)'])
    with pytest.raises(UIError,match='audio_device_option_not_unique'):
        ui.select_route('Speakers','Default - AirPods (Bluetooth)')
    assert clicked==[]


def test_named_airpods_never_falls_back_to_default(ui):
    _,clicked=route_selector(ui,['Default - AirPods (Bluetooth)','MacBook Pro Speakers (Built-in)'])
    with pytest.raises(UIError,match='audio_device_option_not_unique'):
        ui.select_route('Speakers','AirPods (Bluetooth)')
    assert clicked==[]


def test_already_selected_default_needs_no_mutation(ui):
    _,clicked=route_selector(ui,[],current='Default - MacBook Pro Speakers (Built-in)')
    ui.select_route('Speakers','Default - AirPods (Bluetooth)')
    assert clicked==[]


def test_restore_audio_verifies_default_role_after_device_change(ui):
    requested={'Microphone':'MacBook Pro Microphone (Built-in)','Speakers':'Default - AirPods (Bluetooth)'}
    current={**requested,'Speakers':'Default - MacBook Pro Speakers (Built-in)'}
    selections=[]
    ui.select_route=lambda name,value:selections.append((name,value))
    ui.routes=lambda:current;ui.audio_close=lambda:None
    receipt=ui.restore_audio(requested)
    assert receipt['verified'] is True and receipt['actual_routes']==current
    assert selections==list(requested.items())


def focus_receipt(ui,**changes):
    return dict({'ok':True,'exact_window_focused':True,'identity_verified':True,
                 'target':{key:ui.config[key] for key in ('app','window_id','pid','process_instance')}},**changes)


def test_focus_enrolled_passes_exact_native_identity_and_bounded_timeout(ui):
    calls=[]
    ui.command=lambda args,timeout:(calls.append((args,timeout)) or focus_receipt(ui))
    assert ui.focus_enrolled()['exact_window_focused'] is True
    args,timeout=calls[0]
    assert Path(args[0]).name=='call-desk-focus'
    assert args[args.index('--window-id')+1]==ui.config['window_id']
    assert args[args.index('--pid')+1]==str(ui.config['pid'])
    assert args[args.index('--process-instance')+1]==ui.config['process_instance']
    assert timeout==9


@pytest.mark.parametrize('key',['ok','exact_window_focused','identity_verified'])
@pytest.mark.parametrize('value',[False,None,1,'true'])
def test_focus_enrolled_requires_all_verified_boolean_flags(ui,key,value):
    ui.command=lambda *args,**kw:focus_receipt(ui,**{key:value})
    with pytest.raises(UIError,match='native_window_focus_unverified'):ui.focus_enrolled()


def test_focus_enrolled_rejects_wrong_receipt_owner(ui):
    receipt=focus_receipt(ui);receipt['target']['pid']+=1
    ui.command=lambda *args,**kw:receipt
    with pytest.raises(UIError,match='native_window_identity_changed'):ui.focus_enrolled()


def test_focus_enrolled_preserves_permission_denial_without_retry(ui):
    calls=[]
    ui.command=lambda *args,**kw:(calls.append(args) or {'ok':False,'error':{'code':'PERM_DENIED'}})
    with pytest.raises(UIError,match='PERM_DENIED'):ui.focus_enrolled()
    assert len(calls)==1


def test_reading_snapshot_does_not_focus_browser(ui):
    ui.focus_enrolled=lambda:pytest.fail('Snapshot must never change browser focus')
    ui.raw=lambda **kw:(idle(),None,None)
    assert ui.snapshot()['state']=='idle'


def test_native_focus_logs_compact_private_success_without_raw_payload(ui):
    receipt=focus_receipt(ui,activation_dispatched=True,raise_dispatched=True,
        diagnostic={'phase':'verifying_focus','frontmost_pid':ui.config['pid'],
                    'confirmations':2,'private_window_title':'Do not persist'},
        raw_tree={'name':'Do not persist'})
    ui.command=lambda *args,**kw:receipt
    ui.focus_enrolled()
    path=ui.home/'last-native-focus.json';saved=json.loads(path.read_text())
    assert saved['state']=='verified' and saved['receipt']['diagnostic']['confirmations']==2
    assert saved['target']['pid']==ui.config['pid']
    assert 'Do not persist' not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_native_focus_logs_failure_phase_and_permission(ui):
    ui.command=lambda *args,**kw:{'ok':False,'activation_dispatched':False,
        'diagnostic':{'phase':'preflight'},'error':{'code':'PERM_DENIED','message':'Not persisted'}}
    with pytest.raises(UIError,match='PERM_DENIED'):ui.focus_enrolled()
    saved=json.loads((ui.home/'last-native-focus.json').read_text())
    assert saved['state']=='failed' and saved['error_code']=='PERM_DENIED'
    assert saved['receipt']['error_code']=='PERM_DENIED'
    assert saved['receipt']['diagnostic']['phase']=='preflight'


def test_native_focus_logs_timeout_without_claiming_delivery(ui):
    def timeout(*args,**kw):raise UIError('native_command_timed_out_delivery_unknown')
    ui.command=timeout
    with pytest.raises(UIError,match='delivery_unknown'):ui.focus_enrolled()
    saved=json.loads((ui.home/'last-native-focus.json').read_text())
    assert saved['state']=='failed' and saved['error_code']=='native_command_timed_out_delivery_unknown'
    assert 'receipt' not in saved


def test_native_focus_logs_start_before_dispatch(ui):
    def command(*args,**kw):
        saved=json.loads((ui.home/'last-native-focus.json').read_text())
        assert saved['state']=='dispatched' and 'finished_at' not in saved
        return focus_receipt(ui)
    ui.command=command;ui.focus_enrolled()


def raw_read_receipt(ui):
    web=idle();web['children'].append(node('button',ui.config['account_label']))
    tree=node('window',children=[node('textfield','Address and search bar',value='https://voice.google.com/u/0/calls'),web])
    return {'status':'complete','identity_verified':True,
            'target':{key:ui.config[key] for key in ('window_id','pid','process_instance')},
            'raw_snapshot':{'data':{'tree':tree}}}


@pytest.mark.parametrize('configured,expected',[(None,12000),(1000,1000),(15000,15000)])
def test_raw_read_budget_default_override_and_outer_timeout(ui,configured,expected):
    if configured is not None:ui.config['native_read_budget_ms']=configured
    calls=[]
    ui.command=lambda args,timeout:(calls.append((args,timeout)) or raw_read_receipt(ui))
    ui.raw()
    args,timeout=calls[0]
    assert args[args.index('--budget-ms')+1]==str(expected)
    assert timeout==expected/1000+3
    assert len(calls)==1


@pytest.mark.parametrize('invalid',[0,999,15001,True,'12000',12000.0])
def test_raw_read_budget_rejects_invalid_config_before_native_call(ui,invalid):
    ui.config['native_read_budget_ms']=invalid
    ui.command=lambda *a,**k:pytest.fail('Invalid budget must not call the native reader')
    with pytest.raises(UIError,match='native_read_budget_invalid'):ui.raw()
