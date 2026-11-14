from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest
from ticket_fixture import synthetic_ticket_input
from sports_app.storage import SQLiteStore
from sports_app.evidence_store import EvidenceRepository,RepositoryReviewGate
from sports_app.pair_policy import RegularPairPolicy
from sports_app.auth import Principal
from sports_app.manifest import POLICY_FILES
from sports_app.publication import PublicationService

AT=datetime(2026,11,14,4,tzinfo=timezone.utc)
ADMIN=Principal('admin','admin','a');REVIEWER=Principal('reviewer','reviewer','r')


def batch():
    manifest={'runtime_provider':'codex','policy_revision':'p','analysis_revision':'a',
              'schema_hash':'s','code_hash':'c','rule_hashes':{key:'synthetic-hash' for key in POLICY_FILES}}
    options=synthetic_ticket_input()['options']
    events=[];markets={}
    for option in options:
        option.update(evidence_version='ev1',market_snapshot_id=option['option_id'],
                      dimensions=[(f'{i:02d}','unknown') for i in range(1,24)],
                      reasons=['仅按市场基准，缺非赔率依据','市场结构可行但高风险'],
                      failure_paths=['路径一','路径二'],withdraw_if=['来源出现冲突'],claim_ids=['missing'])
        event={'identity':option['identity'],'on_sale':True,'conflict':False,'source_id':'synthetic',
               'as_of':'2026-11-14T11:00:00+08:00','valid_until':'2026-11-14T18:00:00+08:00',
               'sales_close':'2026-11-14T17:00:00+08:00'}
        events.append(event)
        markets[option['option_id']]={'match_id':option['identity']['match_id'],'market':'HAD','line':None,
            'version':'mv1','source_id':'synthetic','on_sale':True,'conflict':False,'supports_pair':True,
            'as_of':event['as_of'],'valid_until':event['valid_until'],'sales_close':event['sales_close'],
            'outcomes':{'H':'3','D':'3','A':'3'}}
    return {'manifest':manifest,'evidence_score_version':'evidence-policy-1.0.0','evidence_version':'ev1',
            'market_version':'mv1','schedule_complete':True,'candidate_universe_complete':True,
            'adapter_id':'synthetic-adapter','conflict':False,'sport':'football','business_day':'2026-11-14',
            'as_of':'2026-11-14T11:00:00+08:00','valid_until':'2026-11-14T18:00:00+08:00',
            'events':events,'markets':markets,'options':{o['option_id']:o for o in options},
            'candidates':{'pair1':{'option_ids':['o1','o2'],'period':'early_middle','equal_stake':'1','total_investment':'1'}}}


@pytest.fixture
def setup():
    root=Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix='.pair-policy-',dir=root) as temp:
        store=SQLiteStore(temp,app_root=root)
        evidence=EvidenceRepository(store,clock=lambda:AT)
        policy=RegularPairPolicy(evidence,approved_manifest=batch()['manifest'])
        yield evidence,policy


def test_conditional_candidate_without_execution_cannot_publish(setup):
    evidence,policy=setup
    policy.freeze_batch(ADMIN,'a','b',batch())
    snapshot=policy.build('b','pair1')
    assert snapshot['recommendation_status']=='conditional_low_confidence'
    assert all(not a['eligible'] and a['quality_gaps'] for a in snapshot['assessment_summary'].values())
    frozen=evidence.freeze_recommendation(ADMIN,'a',freeze_id='f',snapshot=snapshot)
    gate=RepositoryReviewGate(evidence,additional_gate=policy)
    assert policy.validate_candidate(frozen)
    assert snapshot['decision_status']=='candidate_only_agent_incomplete'
    assert not gate(frozen)
    pub=PublicationService(evidence.store,trusted_gate=gate)
    pub.save_draft(ADMIN,'a','d',frozen)
    with pytest.raises(PermissionError):pub.review(REVIEWER,'r','d','r')
    with pytest.raises(ValueError):policy.final_snapshot('b','pair1','missing-execution')


@pytest.mark.parametrize('mutation',['sale','cutoff','identity','price','market_complete','cross_period','structure','version'])
def test_hard_inputs_cannot_be_conditionally_bypassed(setup,mutation):
    _,policy=setup
    value=batch()
    if mutation=='sale':value['events'][0]['on_sale']=False
    elif mutation=='cutoff':value['events'][0]['sales_close']='2026-11-14T12:00:00+08:00'
    elif mutation=='identity':value['events'][0]['identity']=dict(value['events'][0]['identity'],home_id='other')
    elif mutation=='price':value['markets']['o1']['outcomes']['H']='4'
    elif mutation=='market_complete':del value['markets']['o1']['outcomes']['D']
    elif mutation=='cross_period':
        value['events'][1]['identity']['kickoff']='2026-11-14T23:00:00+08:00'
    elif mutation=='structure':
        value['options']['o1']['market']='CRS'
    else:value['options']['o1']['evidence_version']='other'
    policy.freeze_batch(ADMIN,'a','b',value)
    with pytest.raises(ValueError):policy.build('b','pair1')


def test_verified_pair_precedes_higher_benchmark_conditional(setup):
    from datetime import timedelta
    from hashlib import sha256
    from sports_app.evidence import Claim,WEIGHTS
    evidence,policy=setup
    value=batch()
    # A second candidate uses the same real events but separately researched options.
    for old_id in ('o1','o2'):
        alt=deepcopy(value['options'][old_id]);alt_id=old_id+'alt'
        alt.update(option_id=alt_id,assessment_id=alt_id+'a',claim_ids=[])
        value['options'][alt_id]=alt
        claims=[]
        for i in (1,2):
            ident=f'{old_id}-claim-{i}'
            claim=Claim(ident,old_id,tuple(WEIGHTS),f'https://source{i}.example.org/report','L1',
                        AT-timedelta(hours=2),AT-timedelta(hours=1),f'group{i}','synthetic quote',True,False)
            evidence.ingest(ADMIN,'a',claim)
            evidence.verify(REVIEWER,'r',receipt_id=ident+'receipt',claim_id=ident,
                            quote_hash=sha256(claim.quote.encode()).hexdigest(),conclusion='verified')
            claims.append(ident)
        value['options'][old_id]['claim_ids']=claims
        value['options'][old_id]['dimensions']=[(f'{i:02d}','verified') for i in range(1,24)]
    value['candidates']['pair2']={'option_ids':['o1alt','o2alt'],'period':'early_middle',
                                  'equal_stake':'1','total_investment':'1'}
    policy.freeze_batch(ADMIN,'a','b',value)
    assert policy.build('b','pair1')['recommendation_status']=='evidence_qualified'
    with pytest.raises(ValueError):policy.build('b','pair2')


def test_missing_upstream_and_client_tampering_rejected(setup):
    _,policy=setup
    value=batch();value['schedule_complete']=False
    with pytest.raises(ValueError):policy.freeze_batch(ADMIN,'a','bad',value)
    policy.freeze_batch(ADMIN,'a','b',batch())
    snapshot=policy.build('b','pair1');snapshot['market_version']='forged'
    assert not policy(snapshot)
