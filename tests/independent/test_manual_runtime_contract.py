"""Independent manual runtime tests. Injected executors only; never genuine AI."""
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
import sqlite3
from threading import Event, Thread
import pytest
from test_pair_policy_contract import batch, setup, ADMIN, AT
from sports_app.agent_input import REQUIRED_OPTION_FIELDS, IDENTITY_FIELDS
from sports_app.manifest import POLICY_FILES
from sports_app.task_runtime import ManualRuntime
from sports_app.publication import canonical, fingerprint


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs): raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


def prepare(setup,executor):
    repo,policy=setup;value=batch()
    documents={key:'independent synthetic rule text: '+key for key in POLICY_FILES}
    value['manifest']['rule_hashes']={key:sha256(text.encode()).hexdigest() for key,text in documents.items()}
    policy.approved_manifest=deepcopy(value['manifest'])
    for option in value['options'].values():
        for key in REQUIRED_OPTION_FIELDS:option.setdefault(key,'synthetic research content')
        identity=option['identity']
        option['event_definition']=dict(
            **{key:identity[key] for key in ('sport','league','business_day','match_id','kickoff')},
            home_team=identity['home_id'],away_team=identity['away_id'],
            market=option['market'],line=option['line'],original_odds=option['original_odds'],
            source_as_of=AT.isoformat())
        option['settlement_scope']=option['scope']
        option['evidence_as_of']=AT.isoformat()
    policy.freeze_batch(ADMIN,'a','batch',value)
    runtime=ManualRuntime(policy,executor=executor,clock=lambda:AT)
    runtime.approve_rules(ADMIN,'a',registry_id='rules',documents=documents,
        policy_revision=value['manifest']['policy_revision'],analysis_revision=value['manifest']['analysis_revision'],
        schema_hash=value['manifest']['schema_hash'],code_hash=value['manifest']['code_hash'])
    return repo,policy,runtime,value


def create(runtime,task='task',input_id='input',rules='rules',batch_id='batch'):
    return runtime.create(ADMIN,'a',task_id=task,input_id=input_id,registry_id=rules,batch_id=batch_id,
                          deadline=(AT+timedelta(minutes=5)).isoformat())


def counts(repo):
    with closing(repo.store.connect()) as db:
        return {table:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                for table in ('agent_input_records','manual_tasks','task_transitions')}


def test_executor_forged_bindings_overwritten_and_completion_is_not_final_decision(setup):
    seen=[]
    def fake(body):
        seen.append(deepcopy(body))
        return dict(raw_response='synthetic non-decision text',provider='other',input_id='forged',input_hash='forged',
                    batch_hash='forged',manifest_hash='forged',execution_id='forged',response_hash='forged')
    repo,policy,runtime,value=prepare(setup,fake);create(runtime)
    assert runtime.execute(ADMIN,'a','task')=='completed'
    result=runtime.load_execution('task')
    assert result['provider']=='codex' and result['execution_id']=='task' and result['input_id']=='input'
    assert result['input_hash']==fingerprint(seen[0])
    assert result['batch_hash']==fingerprint(value) and result['manifest_hash']==fingerprint(value['manifest'])
    assert result['response_hash']==fingerprint(result['raw_response'])
    policy.decision_loader=runtime.load_execution
    with pytest.raises((ValueError,KeyError)):policy.final_snapshot('batch','pair','task')
    with pytest.raises(ValueError):runtime.execute(ADMIN,'a','task')
    assert len(seen)==1


@pytest.mark.parametrize('mode',['cancel','deadline','error','empty'])
def test_cancel_or_deadline_wins_late_response(setup,mode):
    holder={}
    def fake(body):
        runtime=holder['runtime']
        if mode=='cancel':runtime.cancel(ADMIN,'a','task')
        elif mode=='deadline':runtime.clock=lambda:AT+timedelta(minutes=5)
        elif mode=='error':raise RuntimeError('synthetic sensitive exception must not be persisted')
        elif mode=='empty':return {'raw_response':''}
        return {'raw_response':'late synthetic response'}
    repo,_,runtime,_=prepare(setup,fake);holder['runtime']=runtime;create(runtime)
    expected={'cancel':'cancelled','deadline':'expired','error':'failed','empty':'failed'}[mode]
    assert runtime.execute(ADMIN,'a','task')==expected and runtime.load_execution('task') is None
    with closing(repo.store.connect()) as db:
        payloads=[row[0] for row in db.execute('SELECT payload FROM task_transitions')]
    assert all('synthetic sensitive exception' not in p for p in payloads)
    with pytest.raises(ValueError):runtime.execute(ADMIN,'a','task')


def test_concurrent_execute_claims_adapter_only_once(setup):
    entered,release=Event(),Event();calls=[];results=[]
    def fake(body):
        calls.append('called');entered.set()
        assert release.wait(10),'executor release timeout'
        return {'raw_response':'synthetic'}
    _,_,runtime,_=prepare(setup,fake);create(runtime)
    def first():
        try:results.append(runtime.execute(ADMIN,'a','task'))
        except BaseException as exc:results.append(exc)
    thread=Thread(target=first,daemon=True);thread.start()
    try:
        assert entered.wait(10),'first execution did not enter adapter'
        with pytest.raises(ValueError):runtime.execute(ADMIN,'a','task')
    finally:
        release.set();thread.join(12)
    assert not thread.is_alive() and results==['completed'] and calls==['called']


@pytest.mark.parametrize('mode',['missing_registry','wrong_rules','missing_batch'])
def test_rule_or_batch_mismatch_leaves_no_input_task(setup,mode):
    repo,_,runtime,value=prepare(setup,lambda body:{'raw_response':'synthetic'})
    before=counts(repo)
    with pytest.raises(ValueError):
        if mode=='wrong_rules':
            runtime.approve_rules(ADMIN,'a',registry_id='bad',documents={k:'different' for k in POLICY_FILES},
                policy_revision=value['manifest']['policy_revision'],analysis_revision=value['manifest']['analysis_revision'],
                schema_hash=value['manifest']['schema_hash'],code_hash=value['manifest']['code_hash'])
        else:create(runtime,rules='missing' if mode=='missing_registry' else 'rules',
                    batch_id='missing' if mode=='missing_batch' else 'batch')
    assert counts(repo)==before


def test_task_creation_insert_failure_does_not_leave_orphan_input(setup):
    repo,_,runtime,_=prepare(setup,lambda body:{'raw_response':'synthetic'})
    before=counts(repo)
    with closing(repo.store.connect()) as db,db:
        db.execute("CREATE TRIGGER independent_fail_task BEFORE INSERT ON manual_tasks BEGIN SELECT RAISE(ABORT,'synthetic task insert failure'); END")
    with pytest.raises(sqlite3.IntegrityError):create(runtime)
    assert counts(repo)==before,'Task and frozen input must be committed atomically'


def test_duplicate_task_new_input_does_not_leave_orphan(setup):
    repo,_,runtime,_=prepare(setup,lambda body:{'raw_response':'synthetic'})
    create(runtime);before=counts(repo)
    with pytest.raises((ValueError,sqlite3.IntegrityError)):create(runtime,input_id='orphan-input')
    assert counts(repo)==before
