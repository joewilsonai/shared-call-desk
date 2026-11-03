import datetime as dt
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import pytest
import desktop_api as api
from service import write_json,read_json,CallError,TZ

def policy(tmp_path,**changes):
    today=dt.datetime.now(TZ).date().isoformat()
    data={'budget_date':today,'outside_commitment_floor_usd':5,'ledger_directory':str(tmp_path/'ledger')}
    data.update(changes);write_json(tmp_path/'desktop-test-policy.json',data)
    (tmp_path/'ledger').mkdir()
    write_json(tmp_path/'ledger'/('elle-media-ledger-'+today+'.json'),{
        'local_date':today,'jobs':[{'status':'completed','actual_cost_usd':2,'quoted_cost_usd':8},
        {'status':'submitted','actual_cost_usd':None,'quoted_cost_usd':4},
        {'status':'failed_not_billed','quoted_cost_usd':8}]})
    return SimpleNamespace(home=tmp_path)

def test_budget_replaces_quote_with_actual_and_counts_pending(tmp_path):
    assert api.outside_spend(policy(tmp_path))==6

def test_budget_rejects_stale_reconciliation(tmp_path):
    with pytest.raises(CallError,match='reconciled'):api.outside_spend(policy(tmp_path,budget_date='2000-01-01'))

def test_budget_does_not_reduce_explicit_outside_commitments(tmp_path):
    assert api.outside_spend(policy(tmp_path,outside_commitment_floor_usd=12))==12

def test_arm_requires_cell_only_configuration(tmp_path):
    s=policy(tmp_path)
    with pytest.raises(CallError,match='cell-only'):api.arm_test(s)

def test_stale_watcher_never_shows_ready(tmp_path,monkeypatch):
    s=SimpleNamespace(home=tmp_path,preflight=lambda:{'active_call':None,'errors':[]})
    monkeypatch.setattr(api.incoming_control,'status',lambda _:dict(enabled=True,running=True,
        phase='watching',status_age_seconds=99,allowed_callers=['…1234'],audio_lease_status='prepared'))
    result=api.desktop_status(s)
    assert result['overall']['state']=='attention'
    assert next(c for c in result['checks'] if c['id']=='incoming')['state']=='attention'

def test_old_browser_check_is_not_green(tmp_path,monkeypatch):
    s=SimpleNamespace(home=tmp_path,preflight=lambda:{'active_call':None,'errors':[]})
    monkeypatch.setattr(api.incoming_control,'status',lambda _:dict(enabled=False,running=False,
        phase='disabled',status_age_seconds=1,allowed_callers=[],audio_lease_status='restored'))
    write_json(tmp_path/'desktop-health.json',{'checked_at':1,'browser':{'coverage':'complete','state':'idle'}})
    result=api.desktop_status(s)
    assert next(c for c in result['checks'] if c['id']=='browser')['state']=='unknown'
    assert result['overall']['state']=='off'


def fake_status_service(tmp_path,monkeypatch,lease_status='cleanup_required'):
    service=SimpleNamespace(home=tmp_path,preflight=lambda:{'active_call':None,'errors':[]},locked=lambda:nullcontext({}))
    monkeypatch.setattr(api.incoming_control,'status',lambda _:dict(enabled=False,running=False,
        phase='cleanup_required',reason=None,status_age_seconds=1,allowed_callers=[],audio_lease_status=lease_status))
    return service


def test_disabled_answering_does_not_hide_audio_cleanup(tmp_path,monkeypatch):
    service=fake_status_service(tmp_path,monkeypatch)
    write_json(tmp_path/'incoming-audio-lease.json',{'status':'cleanup_required','error_type':'UIError'})
    write_json(tmp_path/'desktop-last-action.json',{'action':'arm','ok':False,
        'error':'The requested audio device is missing or ambiguous.'})
    result=api.desktop_status(service)
    assert result['overall']['state']=='attention'
    assert result['overall']['label']=='Audio routing needs cleanup'
    assert result['overall']['detail']=='The requested audio device is missing or ambiguous.'
    automatic=next(c for c in result['checks'] if c['id']=='incoming')
    assert automatic['state']=='attention' and 'Audio routing needs cleanup' in automatic['detail']
    assert not automatic['detail'].startswith('Off.')


def test_specific_lease_cause_survives_unrelated_last_error(tmp_path,monkeypatch):
    service=fake_status_service(tmp_path,monkeypatch)
    write_json(tmp_path/'incoming-audio-lease.json',{'status':'cleanup_required',
        'error_code':'audio_panel_did_not_close'})
    write_json(tmp_path/'desktop-last-action.json',{'action':'diagnose','ok':False,'error':'Different browser problem'})
    result=api.desktop_status(service)
    assert result['overall']['detail']=='Google Voice did not close its audio settings panel.'
    assert result['last_error']=='Different browser problem'


def test_reserved_cleanup_call_still_shows_routing_failure_as_primary_status(tmp_path,monkeypatch):
    service=fake_status_service(tmp_path,monkeypatch)
    service.preflight=lambda:{'active_call':{'owner':'elle','phone_state':'idle','phase':'cleanup_required'},'errors':[]}
    write_json(tmp_path/'incoming-audio-lease.json',{'status':'cleanup_required','reason':'Original speakers not restored.'})
    result=api.desktop_status(service)
    assert result['overall']['label']=='Audio routing needs cleanup'
    assert result['overall']['detail']=='Original speakers not restored.'


def test_successful_diagnose_keeps_unresolved_setup_error(tmp_path,monkeypatch):
    service=fake_status_service(tmp_path,monkeypatch)
    write_json(tmp_path/'incoming-audio-lease.json',{'status':'cleanup_required','error_type':'UIError'})
    write_json(tmp_path/'desktop-last-action.json',{'action':'arm','at':123,'ok':False,'error':'Speaker restoration could not be verified.'})
    result=api.record_success(service,'diagnose',api.desktop_status(service))
    saved=read_json(tmp_path/'desktop-last-action.json')
    assert result['last_error']=='Speaker restoration could not be verified.'
    assert saved['action']=='diagnose' and saved['ok'] is True
    assert saved['error_action']=='arm' and saved['error_at']==123
    again=api.desktop_status(service)
    assert again['overall']['label']=='Audio routing needs cleanup'
    assert again['overall']['detail']=='Speaker restoration could not be verified.'


def test_successful_diagnose_does_not_clear_unrelated_budget_error(tmp_path,monkeypatch):
    service=fake_status_service(tmp_path,monkeypatch,'restored')
    write_json(tmp_path/'incoming-audio-lease.json',{'status':'restored'})
    write_json(tmp_path/'desktop-last-action.json',{'action':'arm','ok':False,'error':'Budget must be refreshed.'})
    result=api.record_success(service,'diagnose',api.desktop_status(service))
    assert result['last_error']=='Budget must be refreshed.'


def test_verified_diagnose_can_clear_its_own_error_after_routes_restored(tmp_path,monkeypatch):
    service=fake_status_service(tmp_path,monkeypatch,'restored')
    write_json(tmp_path/'incoming-audio-lease.json',{'status':'restored'})
    write_json(tmp_path/'desktop-last-action.json',{'action':'diagnose','ok':False,'error':'Browser not verified.'})
    write_json(tmp_path/'desktop-health.json',{'checked_at':time.time(),
        'browser':{'coverage':'complete','state':'idle','evidence':'Verified idle'}})
    result=api.record_success(service,'diagnose',api.desktop_status(service))
    assert result['last_error'] is None
    assert 'error' not in read_json(tmp_path/'desktop-last-action.json')


def test_periodic_status_does_not_mutate_error_record(tmp_path,monkeypatch):
    service=fake_status_service(tmp_path,monkeypatch)
    record={'action':'arm','ok':False,'error':'Specific setup failure.'}
    write_json(tmp_path/'desktop-last-action.json',record)
    api.desktop_status(service)
    api.desktop_status(service)
    assert read_json(tmp_path/'desktop-last-action.json')==record


def test_diagnose_preserves_specific_native_error_code(tmp_path,monkeypatch):
    import google_voice_ui
    service=fake_status_service(tmp_path,monkeypatch)
    def fail():raise google_voice_ui.UIError('audio_panel_did_not_close')
    monkeypatch.setattr(google_voice_ui,'GoogleVoiceUI',lambda _:SimpleNamespace(snapshot=fail))
    api.diagnose(service)
    assert read_json(tmp_path/'desktop-health.json')['browser']['evidence']=='audio_panel_did_not_close'


def prepared_arm(tmp_path,phase='watching'):
    service=SimpleNamespace(home=tmp_path)
    armed_id=str(uuid.uuid4())
    write_json(tmp_path/'incoming-settings.json',{'enabled':True,'armed_id':armed_id})
    write_json(tmp_path/'incoming-audio-lease.json',{'status':'prepared','armed_id':armed_id,
               'preparation_evidence':'Both routes and same idle window verified'})
    write_json(tmp_path/'desktop-last-action.json',{'action':'arm','ok':False,
               'error':'Previous focus failed','error_action':'arm'})
    result={'incoming':{'enabled':True,'armed_id':armed_id,'phase':phase},
            'routing':{'lease_status':'prepared','unresolved':True,'needs_cleanup':False},'checks':[]}
    return service,result


@pytest.mark.parametrize('phase',['launch_pending','starting','watching'])
def test_successful_prepared_arm_clears_only_prior_arm_error(tmp_path,phase):
    service,result=prepared_arm(tmp_path,phase)
    result=api.record_success(service,'arm',result)
    assert result['last_error'] is None
    assert 'error' not in read_json(tmp_path/'desktop-last-action.json')
    # Clearing the stale error does not promote watcher startup to ready.
    assert result['incoming']['phase']==phase


@pytest.mark.parametrize('defect',['wrong_arm','disabled','cleanup','missing_evidence','failed_watcher'])
def test_unverified_arm_cannot_clear_previous_error(tmp_path,defect):
    service,result=prepared_arm(tmp_path)
    lease=read_json(tmp_path/'incoming-audio-lease.json')
    if defect=='wrong_arm':lease['armed_id']=str(uuid.uuid4())
    elif defect=='disabled':write_json(tmp_path/'incoming-settings.json',{'enabled':False,'armed_id':lease['armed_id']})
    elif defect=='cleanup':result['routing']['needs_cleanup']=True
    elif defect=='missing_evidence':lease.pop('preparation_evidence')
    elif defect=='failed_watcher':result['incoming']['phase']='watcher_failed'
    write_json(tmp_path/'incoming-audio-lease.json',lease)
    assert api.record_success(service,'arm',result)['last_error']=='Previous focus failed'


def test_prepared_arm_does_not_clear_unrelated_error(tmp_path):
    service,result=prepared_arm(tmp_path)
    write_json(tmp_path/'desktop-last-action.json',{'action':'show-voice','ok':False,'error':'Unrelated error'})
    assert api.record_success(service,'arm',result)['last_error']=='Unrelated error'


def stopped_arm(tmp_path,monkeypatch):
    service=fake_status_service(tmp_path,monkeypatch,'restored')
    arm_id=str(uuid.uuid4())
    incoming=dict(enabled=False,running=False,phase='disabled',armed_id=arm_id,
                  status_age_seconds=1,allowed_callers=[],audio_lease_status='restored')
    monkeypatch.setattr(api.incoming_control,'status',lambda _:dict(incoming))
    record={'action':'disarm','ok':True,'error':'Previous arm failed','error_action':'arm',
            'disarm_target_armed_id':arm_id,'disarm_request_token':'stop-fixture'}
    write_json(tmp_path/'desktop-last-action.json',record)
    write_json(tmp_path/'incoming-settings.json',{'enabled':False,'armed_id':arm_id})
    write_json(tmp_path/'incoming-audio-lease.json',{'status':'restored','armed_id':arm_id,
               'restoration_evidence':'Original routes and idle enrolled window verified'})
    return service,incoming,record


def test_status_hides_async_stop_error_only_after_same_arm_restoration(tmp_path,monkeypatch):
    service,incoming,record=stopped_arm(tmp_path,monkeypatch)
    before=(tmp_path/'desktop-last-action.json').read_bytes()
    assert api.desktop_status(service)['last_error'] is None
    assert api.desktop_status(service)['overall']['state']=='off'
    assert (tmp_path/'desktop-last-action.json').read_bytes()==before
    assert read_json(tmp_path/'desktop-last-action.json')==record


def test_later_explicit_action_does_not_resurrect_resolved_stop_error(tmp_path,monkeypatch):
    service,_,_=stopped_arm(tmp_path,monkeypatch)
    result=api.record_success(service,'show-voice',api.desktop_status(service))
    assert result['last_error'] is None
    assert 'error' not in read_json(tmp_path/'desktop-last-action.json')


@pytest.mark.parametrize('defect',['restoring','wrong_arm','running','enabled','active','current',
                                 'no_proof','unrelated','failed_stop','legacy_unbound'])
def test_status_preserves_error_without_complete_matching_stop_proof(tmp_path,monkeypatch,defect):
    service,incoming,record=stopped_arm(tmp_path,monkeypatch)
    lease=read_json(tmp_path/'incoming-audio-lease.json')
    if defect=='restoring':lease['status']='restoring'
    elif defect=='wrong_arm':lease['armed_id']=str(uuid.uuid4())
    elif defect=='running':incoming['running']=True
    elif defect=='enabled':incoming['enabled']=True
    elif defect=='active':service.preflight=lambda:{'active_call':{'owner':'test','phase':'active','phone_state':'answered'},'errors':[]}
    elif defect=='current':write_json(tmp_path/'incoming-state.json',{'current':{'call_id':'pending'}})
    elif defect=='no_proof':lease.pop('restoration_evidence')
    elif defect=='unrelated':record['error_action']='show-voice'
    elif defect=='failed_stop':record['ok']=False
    elif defect=='legacy_unbound':record.pop('disarm_target_armed_id')
    write_json(tmp_path/'incoming-audio-lease.json',lease)
    write_json(tmp_path/'desktop-last-action.json',record)
    assert api.desktop_status(service)['last_error']=='Previous arm failed'


def test_recorded_stop_is_bound_to_its_own_cancel_receipt(tmp_path):
    service=SimpleNamespace(home=tmp_path);arm_id=str(uuid.uuid4())
    write_json(tmp_path/'incoming-disarm-request.json',{'token':'own-stop','target_armed_id':arm_id})
    write_json(tmp_path/'desktop-last-action.json',{'action':'arm','ok':False,'error':'Old setup error'})
    result={'routing':{'unresolved':True},'checks':[]}
    api.record_success(service,'disarm',result,disarm_receipt={'cancel_token':'own-stop'})
    saved=read_json(tmp_path/'desktop-last-action.json')
    assert saved['disarm_target_armed_id']==arm_id and saved['disarm_request_token']=='own-stop'
    assert saved['error']=='Old setup error'


def test_different_stop_receipt_cannot_bind_current_arm(tmp_path):
    service=SimpleNamespace(home=tmp_path)
    write_json(tmp_path/'incoming-disarm-request.json',{'token':'newer-stop','target_armed_id':str(uuid.uuid4())})
    api.record_success(service,'disarm',{'checks':[]},disarm_receipt={'cancel_token':'older-stop'})
    assert 'disarm_target_armed_id' not in read_json(tmp_path/'desktop-last-action.json')


def persistent_release(arm_id):
    return {'armed_id':arm_id,'status':'released','audio_cleanup_policy':'keep_virtual',
            'release_kind':'persistent_virtual_defaults','audio_cleanup_verified':True,
            'routes_restored':False,'audio_cleanup_evidence':'Both configured virtual routes and idle window verified'}


def test_persistent_defaults_show_off_without_cleanup_and_preserve_error_receipt(tmp_path,monkeypatch):
    service,incoming,record=stopped_arm(tmp_path,monkeypatch)
    incoming['audio_lease_status']='released'
    write_json(tmp_path/'incoming-audio-lease.json',persistent_release(incoming['armed_id']))
    result=api.desktop_status(service)
    assert result['routing']['persistent_defaults'] is True
    assert result['routing']['needs_cleanup'] is False
    assert result['routing']['unresolved'] is False
    assert result['overall']['state']=='off'
    assert 'Virtual audio defaults remain configured' in result['overall']['detail']
    automatic=next(c for c in result['checks'] if c['id']=='incoming')
    assert automatic['state']=='off'
    assert 'Virtual audio defaults remain configured' in automatic['detail']
    assert result['last_error'] is None
    assert read_json(tmp_path/'desktop-last-action.json')==record


@pytest.mark.parametrize('change',[
    {'audio_cleanup_verified':False}, {'audio_cleanup_verified':1},
    {'audio_cleanup_evidence':''}, {'routes_restored':True},
    {'audio_cleanup_policy':'restore_original'}, {'release_kind':'unknown'},
])
def test_unverified_persistent_release_remains_cleanup_and_keeps_error(tmp_path,monkeypatch,change):
    service,incoming,_=stopped_arm(tmp_path,monkeypatch)
    incoming['audio_lease_status']='released'
    lease=persistent_release(incoming['armed_id']);lease.update(change)
    write_json(tmp_path/'incoming-audio-lease.json',lease)
    result=api.desktop_status(service)
    assert result['routing']['persistent_defaults'] is False
    assert result['overall']['state']=='attention'
    assert result['routing']['needs_cleanup'] is True
    assert result['last_error']=='Previous arm failed'


def test_persistent_release_does_not_clear_unrelated_error(tmp_path,monkeypatch):
    service,incoming,record=stopped_arm(tmp_path,monkeypatch)
    write_json(tmp_path/'incoming-audio-lease.json',persistent_release(incoming['armed_id']))
    record['error_action']='show-voice'
    write_json(tmp_path/'desktop-last-action.json',record)
    assert api.desktop_status(service)['last_error']=='Previous arm failed'
