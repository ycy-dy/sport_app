"""Injected fake executor tests only: never real AI/network."""
from datetime import timedelta
from hashlib import sha256
from contextlib import closing
import sqlite3
import pytest
from test_pair_policy import setup,batch,ADMIN,AT
from sports_app.task_runtime import ManualRuntime
from sports_app.manifest import POLICY_FILES
from sports_app.agent_input import REQUIRED_OPTION_FIELDS,IDENTITY_FIELDS


def prepare(policy,evidence,executor):
    documents={key:'synthetic full document '+key for key in POLICY_FILES}
    value=batch()
    value['manifest']['rule_hashes']={key:sha256(text.encode()).hexdigest() for key,text in documents.items()}
    policy.approved_manifest=value['manifest']
    for option in value['options'].values():
        for key in REQUIRED_OPTION_FIELDS:
            option.setdefault(key,'synthetic research')
        identity=option['identity']
        option['event_definition']=dict(sport=identity['sport'],league=identity['league'],
            business_day=identity['business_day'],match_id=identity['match_id'],kickoff=identity['kickoff'],
            home_team=identity['home_id'],away_team=identity['away_id'],market=option['market'],line=None,
            original_odds=option['original_odds'],source_as_of=AT.isoformat())
        option['settlement_scope']=option['scope']
        option['evidence_as_of']=AT.isoformat()
    policy.freeze_batch(ADMIN,'a','b',value)
    runtime=ManualRuntime(policy,executor=executor,clock=lambda:AT)
    runtime.approve_rules(ADMIN,'a',registry_id='rules',documents=documents,
                         policy_revision='p',analysis_revision='a',schema_hash='s',code_hash='c')
    return runtime


def create(runtime):
    return runtime.create(ADMIN,'a',task_id='task',input_id='input',registry_id='rules',
                          batch_id='b',deadline=(AT+timedelta(minutes=5)).isoformat())


def test_full_input_task_holds_id_and_adapter_writes_bound_result(setup):
    evidence,policy=setup
    seen=[]
    def fake(body):
        seen.append(body)
        return {'raw_response':'synthetic response','input_hash':'forged','provider':'other'}
    runtime=prepare(policy,evidence,fake);create(runtime)
    assert runtime.execute(ADMIN,'a','task')=='completed'
    result=runtime.load_execution('task')
    assert result['provider']=='codex' and result['input_id']=='input' and result['input_hash']!='forged'
    assert set(seen[0]['assembled_input']['input']['rule_documents'])==set(POLICY_FILES)
    assert seen[0]['batch']['candidates']
    policy._input_binding(result,'b',seen[0]['batch'])
    with closing(evidence.store.connect()) as db,db:
        with pytest.raises(sqlite3.IntegrityError):db.execute("UPDATE task_transitions SET state='completed'")
        with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM approved_rules')


def test_unconfigured_executor_does_not_accept_task(setup):
    evidence,policy=setup
    runtime=prepare(policy,evidence,None)
    with pytest.raises(ValueError):create(runtime)
    assert runtime.load_execution('task') is None


@pytest.mark.parametrize('mode',['failure','empty','cancel','late'])
def test_failure_cancel_and_late_never_promote_success(setup,mode):
    evidence,policy=setup
    runtime=None
    def fake(body):
        if mode=='failure':raise RuntimeError('synthetic private error')
        if mode=='empty':return {'raw_response':''}
        if mode=='cancel':runtime.cancel(ADMIN,'a','task')
        if mode=='late':runtime.clock=lambda:AT+timedelta(minutes=6)
        return {'raw_response':'late synthetic result'}
    runtime=prepare(policy,evidence,fake);create(runtime)
    expected={'failure':'failed','empty':'failed','cancel':'cancelled','late':'expired'}[mode]
    assert runtime.execute(ADMIN,'a','task')==expected
    assert runtime.load_execution('task') is None
    with pytest.raises(ValueError):runtime.execute(ADMIN,'a','task')


def counts(store):
    with closing(store.connect()) as db:
        return tuple(db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                     for name in ('agent_input_records','manual_tasks','task_transitions'))


@pytest.mark.parametrize('target',['manual_tasks','task_transitions'])
def test_creation_insert_failure_rolls_back_all_records(setup,target):
    evidence,policy=setup
    runtime=prepare(policy,evidence,lambda body:{'raw_response':'synthetic'})
    before=counts(evidence.store)
    with closing(evidence.store.connect()) as db,db:
        db.execute(f"CREATE TRIGGER synthetic_insert_failure BEFORE INSERT ON {target} "
                   "BEGIN SELECT RAISE(ABORT,'synthetic insert failure'); END")
    with pytest.raises(sqlite3.IntegrityError):create(runtime)
    assert counts(evidence.store)==before


@pytest.mark.parametrize('task_id,input_id',[('task','new-input'),('new-task','input')])
def test_duplicate_task_or_input_rolls_back_all_records(setup,task_id,input_id):
    evidence,policy=setup
    runtime=prepare(policy,evidence,lambda body:{'raw_response':'synthetic'})
    create(runtime)
    before=counts(evidence.store)
    with pytest.raises(sqlite3.IntegrityError):
        runtime.create(ADMIN,'a',task_id=task_id,input_id=input_id,registry_id='rules',batch_id='b',
                       deadline=(AT+timedelta(minutes=5)).isoformat())
    assert counts(evidence.store)==before


def test_missing_rules_or_rule_drift_rejected(setup):
    evidence,policy=setup
    runtime=prepare(policy,evidence,lambda body:{'raw_response':'synthetic'})
    with pytest.raises(ValueError):runtime.create(ADMIN,'a',task_id='t',input_id='i',registry_id='missing',
                                                  batch_id='b',deadline=(AT+timedelta(minutes=5)).isoformat())
    with pytest.raises(ValueError):runtime.approve_rules(ADMIN,'a',registry_id='bad',documents={'policy.md':'missing'},
                                                        policy_revision='p',analysis_revision='a',schema_hash='s',code_hash='c')
