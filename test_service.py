import fcntl
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
import pytest
from service import CallService, CallError, write_json
from audio_policy import KEEP_VIRTUAL,release_fields


def build(tmp_path, spawn=None):
    config=tmp_path/'config.json'
    write_json(config,{'voice_name':'Elle','provider_overrides_verified_at':'test-only','reserve_per_minute_usd':1})
    return CallService(tmp_path/'state',config,spawn=spawn)

def prepare(s, key='one', owner='test'):
    return s.prepare(owner,key,'+15555550123','TEST ONLY; no dial','minimal test context',60,'offline test',0)

@pytest.fixture
def service(tmp_path): return build(tmp_path)

def finish(s,c):
    return s.finish(c,'test','Test browser idle evidence','Test routes never changed','offline test')

def fake_spawn(calls):
    def spawn(*a,**kw):
        calls.append(a)
        return SimpleNamespace(pid=12345,poll=lambda:None)
    return spawn

def ready(s,c):
    d=s.directory(c)
    (d/'events.jsonl').write_text('\n'.join(json.dumps({'kind':k}) for k in ['audio_connected','provider_connected']))
    lock=open(d/'worker.lock','w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    return lock

def test_idempotency_and_conflict(service):
    a=prepare(service);b=prepare(service)
    assert a['call_id']==b['call_id'] and b['replayed']
    with pytest.raises(CallError,match='different inputs'):
        service.prepare('test','one','+15555550124','changed','',60,'test',0)
    with pytest.raises(CallError,match='reserved'): prepare(service,'two','other')

def test_private_context_not_in_status(service):
    c=prepare(service)['call_id']
    service.takeover(c,'other','PRIVATE HANDOFF')
    status=service.status(c)
    assert 'minimal test context' not in json.dumps(status)
    assert 'PRIVATE HANDOFF' not in json.dumps(status)
    service.takeover(c,'test','returned')
    finish(service,c)
    assert 'Test browser idle evidence' not in json.dumps(service.status(c))
    assert 'Test browser idle evidence' in json.dumps(service.report(c))

def test_single_start_and_stale_no_relaunch(tmp_path):
    calls=[];s=build(tmp_path,fake_spawn(calls));c=prepare(s)['call_id']
    assert s.start(c,'test')['new_worker_started']
    assert not s.start(c,'test')['new_worker_started']
    assert len(calls)==1
    with pytest.raises(CallError,match='uncertain'): finish(s,c)
    s.recover(c,'test','Inspected worker absent and provider ended in offline test')
    finish(s,c)
    assert s.start(c,'test')['new_worker_started'] is False

def test_stopped_before_start_does_not_start(tmp_path):
    calls=[];s=build(tmp_path,fake_spawn(calls));c=prepare(s)['call_id'];s.stop(c)
    with pytest.raises(CallError,match='stopped'):s.start(c,'test')
    assert not calls
    finish(s,c)

def test_dial_issued_once_and_phone_never_inferred(service):
    c=prepare(service)['call_id'];lock=ready(service,c)
    try:
        assert service.status(c)['phone_state']=='not_dialed'
        assert service.dial_intent(c,'test','Verified exact account/tab, recipient, routes')['dispatch_allowed']
        assert not service.dial_intent(c,'test','Retry')['dispatch_allowed']
        service.observe(c,'test','uncertain','UI timed out after click')
        assert service.status(c)['phone_state']=='uncertain'
    finally: lock.close()

@pytest.mark.parametrize('terminal',['disconnect','error','worker_stopping','worker_finished'])
def test_cannot_dial_during_shutdown(service,terminal):
    c=prepare(service)['call_id'];lock=ready(service,c)
    try:
        with (service.directory(c)/'events.jsonl').open('a') as f: f.write('\n'+json.dumps({'kind':terminal}))
        assert not service.status(c)['ready_to_dial']
        with pytest.raises(CallError,match='Wait for both'):service.dial_intent(c,'test','browser')
    finally:lock.close()

def test_worker_result_not_enough_to_release(tmp_path):
    s=build(tmp_path,fake_spawn([]));c=prepare(s)['call_id'];s.start(c,'test')
    write_json(s.directory(c)/'result.json',{'provider_session_attempted':True,'audio_stopped':True,'provider_stop_completed':False,'provider_thread_joined':False,'billing':{'status':'pending','cost_usd':.01}})
    assert 'actual_cost_usd' not in s.status(c)
    with pytest.raises(CallError,match='shutdown is uncertain'):finish(s,c)
    s.recover(c,'test','Independently verified remote session ended')
    finish(s,c)
    assert 'actual_cost_usd' not in s.status(c) # reservation held for billing
    s.reconcile(c,.03,'Final provider invoice for offline test')
    assert s.status(c)['actual_cost_usd']==.03

def test_budget_reserved_and_final_not_double_counted(service):
    with pytest.raises(CallError,match='above'):
        service.prepare('test','expensive','+15555550123','test','',60,'test',99)
    c=prepare(service)['call_id']
    finish(service,c)
    assert service.status(c)['actual_cost_usd']==0
    # Operator reconciliation represents the final total, not an increment.
    service.reconcile(c,.5,'final receipt')
    service.reconcile(c,.5,'same final receipt')
    with service.locked() as state:
        assert service.budget_total(state,state['calls'][c]['local_date'])==.5

def race_prepare(home,config,key,q):
    s=CallService(home,config)
    try:q.put(('ok',prepare(s,key)['call_id']))
    except CallError:q.put(('reserved',None))

def test_cross_process_reservation(tmp_path):
    s=build(tmp_path);ctx=multiprocessing.get_context('spawn');q=ctx.Queue()
    procs=[ctx.Process(target=race_prepare,args=(s.home,s.config_path,str(i),q)) for i in range(2)]
    for p in procs:p.start()
    for p in procs:p.join(10);assert p.exitcode==0
    assert sorted(q.get(timeout=2)[0] for _ in procs)==['ok','reserved']

def test_stop_does_not_signal_pid(service):
    c=prepare(service)['call_id']
    service.stop(c)
    assert (service.directory(c)/'STOP').exists()
    assert service.status(c)['phone_state']=='not_dialed'

@pytest.mark.parametrize('number',['555','../escape','+1; touch nope'])
def test_bad_recipient(service,number):
    with pytest.raises(CallError):service.prepare('test','bad',number,'test','',60,'test',0)

def test_cannot_reconcile_unfinished_request(service):
    c=prepare(service)['call_id']
    with pytest.raises(CallError,match='No completed'):service.reconcile(c,0,'premature')

def test_inbound_answer_gate(service):
    c=service.prepare_incoming('test','ring-test','+15555550123','Receptionist test','',60,'test',0,'ring_1')['call_id']
    assert not (service.directory(c)/'ANSWERED').exists()
    lock=ready(service,c)
    try:
        with pytest.raises(CallError,match='answer_intent'): service.dial_intent(c,'test','wrong direction')
        assert service.answer_intent(c,'test','fresh ring')['dispatch_allowed']
        assert not service.answer_intent(c,'test','replay')['dispatch_allowed']
        service.observe(c,'test','answered','Fresh same-call active controls')
        assert json.loads((service.directory(c)/'ANSWERED').read_text())['call_id']==c
    finally: lock.close()

def test_outbound_unknown_number_rejected(service):
    with pytest.raises(CallError):service.prepare('test','x','unknown','test','',60,'test',0)


def test_outgoing_keep_virtual_policy_is_snapshotted_and_reported(service):
    config=service.config();config['audio_cleanup_policy']=KEEP_VIRTUAL;write_json(service.config_path,config)
    c=prepare(service)['call_id']
    assert service.status(c)['audio_cleanup_policy']==KEEP_VIRTUAL
    config['audio_cleanup_policy']='restore_original';write_json(service.config_path,config)
    assert 'keep them as the Google Voice defaults' in service.stop(c)['instruction']
    service.finish(c,'test','Browser verified idle','Observed both virtual defaults retained','Offline test')
    report=service.report(c)
    assert report['audio_cleanup_policy']==KEEP_VIRTUAL
    assert report['audio_cleanup_outcome']=='persistent_virtual_defaults'
    assert report['audio_cleanup_evidence']=='Observed both virtual defaults retained'
    assert report['routes_restored'] is False


def test_outgoing_allowed_only_after_fully_verified_persistent_lease_release(service):
    lease=release_fields(KEEP_VIRTUAL,'Observed virtual defaults retained',123)
    incomplete=dict(lease);incomplete.pop('audio_cleanup_evidence')
    write_json(service.home/'incoming-audio-lease.json',incomplete)
    with pytest.raises(CallError,match='owns the audio routes'):prepare(service)
    write_json(service.home/'incoming-audio-lease.json',lease)
    assert prepare(service)['call_id']
