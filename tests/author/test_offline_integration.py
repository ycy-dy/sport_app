"""Real local rule texts + fake executor + synthetic facts; no real AI."""
from copy import deepcopy
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest
from test_pair_policy import batch,ADMIN,REVIEWER,AT
from sports_app.local_assembly import LocalAssembly
from sports_app.storage import SQLiteStore
from sports_app.evidence_store import EvidenceRepository,RepositoryReviewGate
from sports_app.pair_policy import RegularPairPolicy
from sports_app.task_runtime import ManualRuntime
from sports_app.publication import PublicationService
from sports_app.published_settlement import PublishedSettlementService
from sports_app.settlement import ResultSnapshot,Identity
from sports_app.agent_input import REQUIRED_OPTION_FIELDS
from review_fixture import add_coverage,attach_reviews

APP=Path(__file__).resolve().parents[2]
POLICY=APP.parent/'foot'


def local_capture():
    return LocalAssembly(policy_root=POLICY,app_root=APP,approved_manifest={}).capture()


def value_for(version):
    value=batch();value['manifest']=version
    for key in ('o1','o2'):
        option=value['options'][key]
        for field in REQUIRED_OPTION_FIELDS:option.setdefault(field,'synthetic research')
        identity=option['identity']
        option['event_definition']=dict(sport='football',league=identity['league'],business_day=identity['business_day'],
            match_id=identity['match_id'],kickoff=identity['kickoff'],home_team=identity['home_id'],away_team=identity['away_id'],
            market=option['market'],line=None,original_odds=option['original_odds'],source_as_of=AT.isoformat())
        option['evidence_as_of']=AT.isoformat();option['settlement_scope']=option['scope']
        other=deepcopy(option);other.update(option_id=key+'alt',assessment_id=key+'alt-assessment',selection='D')
        value['options'][key+'alt']=other
    add_coverage(value)
    return value


@pytest.fixture
def chain():
    captured=local_capture()  # inspect only, no persistent policy approval
    assembly=LocalAssembly(policy_root=POLICY,app_root=APP,approved_manifest=captured['manifest'])
    with TemporaryDirectory(prefix='.offline-integration-',dir=APP) as temp:
        evidence=EvidenceRepository(SQLiteStore(temp,app_root=APP),clock=lambda:AT)
        policy=RegularPairPolicy(evidence,approved_manifest=captured['manifest'])
        policy.freeze_batch(ADMIN,'a','batch',value_for(captured['manifest']))
        yield evidence,policy,assembly


def response():
    return {'raw_response':'synthetic test decision, not real AI','selected_candidate_id':'pair1','candidate_vetoes':{},
            'option_comparisons':{key:{'selected_option_id':key,'market_version':'mv1',
                'alternative_option_ids':[key+'alt'],'rationale':'synthetic comparison',
                'server_comparison_verified':True} for key in ('o1','o2')}}


def runtime_for(chain,fake):
    evidence,policy,assembly=chain
    # Review records exist before execution; never mutate immutable response afterward.
    reviewed=attach_reviews(policy,response(),'batch')
    def with_reviews(body):
        returned=fake(body)
        if returned.get('option_comparisons'):
            returned['option_comparisons']=deepcopy(reviewed['option_comparisons'])
        return returned
    runtime=ManualRuntime(policy,executor=with_reviews,clock=lambda:AT,assembly=assembly)
    assembly.register(runtime,ADMIN,'a',registry_id='actual-rules-test-approval')
    runtime.create(ADMIN,'a',task_id='task',input_id='input',registry_id='actual-rules-test-approval',
                   batch_id='batch',deadline=(AT+timedelta(minutes=5)).isoformat())
    policy.decision_loader=runtime.load_execution
    return runtime


def test_actual_rule_assembly_to_offline_synthetic_review_and_settlement(chain):
    evidence,policy,assembly=chain
    runtime=runtime_for(chain,lambda body:response())
    assert runtime.execute(ADMIN,'a','task')=='completed'
    final=policy.final_snapshot('batch','pair1','task')
    frozen=evidence.freeze_recommendation(ADMIN,'a',freeze_id='freeze',snapshot=final)
    gate=RepositoryReviewGate(evidence,additional_gate=policy)
    pub=PublicationService(evidence.store,trusted_gate=gate)
    pub.save_draft(ADMIN,'a','draft',frozen);pub.review(REVIEWER,'r','draft','review')
    pub.confirm(ADMIN,'a',draft_id='draft',review_id='review',publication_id='publication',
                request_id='request',human_confirmed=True)
    results={leg['leg_id']:ResultSnapshot(Identity(**leg['identity']),'finished','H:A',(1,0),
                                          'regulation_90',source_id='synthetic-authority')
             for leg in final['settlement_ticket']['legs']}
    service=PublishedSettlementService(evidence.store)
    assert service.record(ADMIN,'a',publication_id='publication',results=results)['outcome']=='hit'
    assert service.statistics()['actual_recommendations']==1


def test_missing_comparison_does_not_promote_completed_adapter(chain):
    _,policy,_=chain
    runtime=runtime_for(chain,lambda body:dict(response(),option_comparisons={}))
    assert runtime.execute(ADMIN,'a','task')=='completed'
    with pytest.raises(ValueError):policy.final_snapshot('batch','pair1','task')


def test_wrong_task_execution_identity_rejected(chain):
    _,policy,_=chain
    runtime=runtime_for(chain,lambda body:response());runtime.execute(ADMIN,'a','task')
    record=runtime.load_execution('task')
    policy.decision_loader=lambda ident:record
    with pytest.raises(ValueError):policy.final_snapshot('batch','pair1','another-task')


@pytest.mark.parametrize('field',['code_hash','schema_hash','policy_revision','rule_hashes'])
def test_unapproved_actual_rule_or_code_drift_rejected(field):
    captured=local_capture();approved=deepcopy(captured['manifest'])
    approved[field]='different' if field!='rule_hashes' else {}
    assembly=LocalAssembly(policy_root=POLICY,app_root=APP,approved_manifest=approved)
    with pytest.raises(ValueError):assembly.assert_approved()
