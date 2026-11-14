"""Temporary approval and fake-executor integration; never deployment approval."""
from copy import deepcopy
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import pytest
from test_pair_policy_contract import batch, ADMIN, REVIEWER, AT
from sports_app.local_assembly import LocalAssembly, RULE_PATHS, CODE_FILES
from sports_app.storage import SQLiteStore
from sports_app.evidence_store import EvidenceRepository, RepositoryReviewGate
from sports_app.pair_policy import RegularPairPolicy
from sports_app.task_runtime import ManualRuntime
from sports_app.agent_input import REQUIRED_OPTION_FIELDS
from sports_app.publication import PublicationService
from sports_app.published_settlement import PublishedSettlementService
from sports_app.settlement import Identity, ResultSnapshot


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


@pytest.fixture
def assembly_case(tmp_path):
    root=Path(__file__).resolve().parents[2]
    assert tmp_path.resolve().is_relative_to(root)
    # Copy only the explicit non-secret source inventory into this test's disposable tree.
    app=tmp_path/'app';policy_root=tmp_path/'rules'
    for name in CODE_FILES:
        destination=app/'src/sports_app'/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes((root/'src/sports_app'/name).read_bytes())
    for name,relative in RULE_PATHS.items():
        destination=policy_root/relative;destination.parent.mkdir(parents=True,exist_ok=True)
        text="synthetic independent rule content\n"
        if name=='policy.yaml':text="spec_revision: 'synthetic-policy'\n"
        if name=='analysis-contract.yaml':text="analysis_revision: 'synthetic-analysis'\n"
        destination.write_text(text)
    capture=LocalAssembly(policy_root=policy_root,app_root=app,approved_manifest={}).capture()
    approved=deepcopy(capture['manifest'])
    assembly=LocalAssembly(policy_root=policy_root,app_root=app,approved_manifest=approved)
    return root,tmp_path,assembly,approved


@pytest.mark.parametrize('mutation',['rule','source','ddl'])
def test_actual_file_drift_rejected_without_auto_updating_approval(assembly_case,mutation):
    _,_,assembly,approved=assembly_case
    assert assembly.assert_approved()['manifest']==approved
    if mutation=='rule':target=assembly.policy_root/RULE_PATHS['scoring.md'];addition='\nchanged policy\n'
    elif mutation=='source':target=assembly.app_root/'src/sports_app/auth.py';addition='\n# independent code drift\n'
    else:target=assembly.app_root/'src/sports_app/storage.py';addition='\nINDEPENDENT_DDL="CREATE TABLE independent_drift(id INTEGER);"\n'
    with target.open('a') as stream:stream.write(addition)
    changed=assembly.capture()
    assert changed['manifest']!=approved
    assert assembly.approved==approved
    with pytest.raises(ValueError):assembly.assert_approved()
    assert assembly.approved==approved


def test_constructor_detaches_external_approval(assembly_case):
    _,_,assembly,approved=assembly_case
    approved['code_hash']='caller-change'
    assert assembly.assert_approved()['manifest']!=approved


def build_chain(case,missing_comparison=False,null_line=False):
    root,temp,assembly,approved=case
    value=batch();value['manifest']=deepcopy(approved)
    for key in ('o1','o2'):
        option=value['options'][key]
        for field in REQUIRED_OPTION_FIELDS:option.setdefault(field,'independent synthetic research')
        identity=option['identity']
        option['event_definition']=dict(sport=identity['sport'],league=identity['league'],business_day=identity['business_day'],
            match_id=identity['match_id'],kickoff=identity['kickoff'],home_team=identity['home_id'],away_team=identity['away_id'],
            market=option['market'],line=option['line'],
            original_odds=option['original_odds'],source_as_of=AT.isoformat())
        option['evidence_as_of']=AT.isoformat();option['settlement_scope']=option['scope']
        alternative=deepcopy(option);alternative.update(option_id=key+'alt',assessment_id=key+'alt-assessment',selection='D')
        value['options'][key+'alt']=alternative
    repo=EvidenceRepository(SQLiteStore(temp/'database',app_root=root),clock=lambda:AT)
    policy=RegularPairPolicy(repo,approved_manifest=approved)
    policy.freeze_batch(ADMIN,'a','batch',value)
    seen=[]
    def fake(body):
        seen.append(deepcopy(body))
        return dict(raw_response='synthetic decision, not AI',selected_candidate_id='pair',candidate_vetoes={},
            option_comparisons={} if missing_comparison else {key:dict(selected_option_id=key,market_version='mv',
                alternative_option_ids=[key+'alt'],rationale='synthetic comparison',server_comparison_verified=True) for key in ('o1','o2')})
    runtime=ManualRuntime(policy,executor=fake,clock=lambda:AT,assembly=assembly)
    assembly.register(runtime,ADMIN,'a',registry_id='temporary-test-approval')
    runtime.create(ADMIN,'a',task_id='task',input_id='input',registry_id='temporary-test-approval',batch_id='batch',
                   deadline=(AT+timedelta(minutes=5)).isoformat())
    policy.decision_loader=runtime.load_execution
    return repo,policy,runtime,seen


def test_fake_offline_chain_input_to_frozen_publication_and_review(assembly_case):
    repo,policy,runtime,seen=build_chain(assembly_case)
    assert runtime.execute(ADMIN,'a','task')=='completed'
    record=runtime.load_execution('task')
    policy._input_binding(record,'batch',seen[0]['batch'])
    assert seen[0]['assembled_input']['input']['rule_documents']==assembly_case[2].assert_approved()['documents']
    final=policy.final_snapshot('batch','pair','task')
    frozen=repo.freeze_recommendation(ADMIN,'a',freeze_id='freeze',snapshot=final)
    pub=PublicationService(repo.store,trusted_gate=RepositoryReviewGate(repo,additional_gate=policy))
    pub.save_draft(ADMIN,'a','draft',frozen);pub.review(REVIEWER,'r','draft','review')
    assert pub.confirm(ADMIN,'a',draft_id='draft',review_id='review',publication_id='pub',request_id='req',human_confirmed=True)=='pub'
    results={leg['leg_id']:ResultSnapshot(Identity(**leg['identity']),'finished','H:A',(2,0),
             'regulation_90',source_id='synthetic') for leg in final['settlement_ticket']['legs']}
    service=PublishedSettlementService(repo.store)
    assert service.record(ADMIN,'a',publication_id='pub',results=results)['outcome']=='hit'
    assert service.statistics()['actual_recommendations']==1


def test_wrong_task_cannot_claim_completed_result(assembly_case):
    _,policy,runtime,_=build_chain(assembly_case);runtime.execute(ADMIN,'a','task')
    record=runtime.load_execution('task');policy.decision_loader=lambda task:deepcopy(record)
    with pytest.raises(ValueError):policy.final_snapshot('batch','pair','wrong-task')


def test_completed_adapter_missing_comparison_cannot_publish(assembly_case):
    repo,policy,runtime,_=build_chain(assembly_case,missing_comparison=True)
    assert runtime.execute(ADMIN,'a','task')=='completed'
    with pytest.raises(ValueError):policy.final_snapshot('batch','pair','task')
    candidate=policy.build('batch','pair');candidate['decision_id']='task'
    pub=PublicationService(repo.store,trusted_gate=RepositoryReviewGate(repo,additional_gate=policy))
    pub.save_draft(ADMIN,'a','draft',candidate)
    with pytest.raises(PermissionError):pub.review(REVIEWER,'r','draft','review')
    with closing(repo.store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM publications').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM publication_reviews').fetchone()[0]==0


def test_straight_market_accepts_explicit_null_line_without_placeholder(assembly_case):
    # Strict compatibility test: HAD has no handicap and must not need fake text.
    _,_,runtime,_=build_chain(assembly_case,null_line=True)
    assert runtime.execute(ADMIN,'a','task')=='completed'


def test_source_drift_after_task_create_blocks_adapter(assembly_case):
    _,_,runtime,seen=build_chain(assembly_case)
    with (assembly_case[2].app_root/'src/sports_app/auth.py').open('a') as stream:
        stream.write('\n# post-creation independent drift\n')
    with pytest.raises(ValueError):runtime.execute(ADMIN,'a','task')
    assert seen==[] and runtime.load_execution('task') is None
