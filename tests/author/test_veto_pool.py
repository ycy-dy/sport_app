"""Synthetic execution and evidence only, never real AI validation."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import pytest
from test_pair_policy import setup,batch,ADMIN,REVIEWER,AT
from sports_app.evidence import Claim,WEIGHTS
from sports_app.publication import fingerprint


def prepare(policy,evidence):
    value=batch()
    for key in ('o1','o2'):
        base=value['options'][key]
        alt=deepcopy(base);alt.update(option_id=key+'low',assessment_id=key+'low-assessment')
        value['options'][key+'low']=alt
        compare=deepcopy(base);compare.update(option_id=key+'compare',selection='D')
        value['options'][key+'compare']=compare
        claim_ids=[]
        for i in (1,2):
            ident=f'{key}claim{i}'
            claim=Claim(ident,key,tuple(WEIGHTS),f'https://source{i}.example.org/a','L1',
                        AT-timedelta(hours=2),AT-timedelta(hours=1),f'g{i}','synthetic risk fact',True,False)
            evidence.ingest(ADMIN,'a',claim)
            evidence.verify(REVIEWER,'r',receipt_id=ident+'receipt',claim_id=ident,
                            quote_hash=sha256(claim.quote.encode()).hexdigest(),conclusion='verified')
            claim_ids.append(ident)
        base['claim_ids']=claim_ids
        base['dimensions']=[(f'{i:02d}','verified') for i in range(1,24)]
    value['candidates']['B']={'option_ids':['o1low','o2low'],'period':'early_middle','equal_stake':'1','total_investment':'1'}
    value['candidates']['C']={'option_ids':['o1','o2low'],'period':'early_middle','equal_stake':'1','total_investment':'1'}
    policy.freeze_batch(ADMIN,'a','b',value)
    digest=policy.freeze_agent_input(ADMIN,'a',input_id='input',batch_id='b',assembled_input={'synthetic':'input'})
    record={'provider':'codex','status':'completed','execution_id':'execution','batch_hash':fingerprint(value),
            'manifest_hash':fingerprint(value['manifest']),'selected_candidate_id':'B','input_id':'input',
            'input_hash':digest,'raw_response':'synthetic response','response_hash':fingerprint('synthetic response'),
            'completed_at':AT.isoformat(),'candidate_vetoes':{},
            'option_comparisons':{key:{'selected_option_id':key,'market_version':'mv1',
                'alternative_option_ids':[key[:2]+'compare'],'rationale':'synthetic comparison',
                'server_comparison_verified':True} for key in ('o1low','o2low')}}
    return value,record


def test_qualified_vetoed_then_conditional_rebuilt_without_score_changes(setup):
    evidence,policy=setup;_,record=prepare(policy,evidence)
    record['candidate_vetoes']={'pair1':{'option_ids':['o1'],'reason':'sourced risk','claim_ids':['o1claim1']}}
    policy.decision_loader=lambda key:deepcopy(record)
    final=policy.final_snapshot('b','B','execution')
    assert final['recommendation_status']=='conditional_low_confidence'
    assert final['vetoed_option_ids']==['o1']
    assert all(not result['eligible'] and result['score'] is None for result in final['assessment_summary'].values())
    assert final['joint_p_mkt']==str((1/ __import__('decimal').Decimal(3))**2)


def test_qualified_not_vetoed_blocks_conditionals_and_option_reuse(setup):
    evidence,policy=setup;_,record=prepare(policy,evidence)
    policy.decision_loader=lambda key:deepcopy(record)
    with pytest.raises(ValueError):policy.final_snapshot('b','B','execution')
    record['candidate_vetoes']={'pair1':{'option_ids':['o1'],'reason':'sourced risk','claim_ids':['o1claim1']}}
    record['selected_candidate_id']='C'
    with pytest.raises(ValueError):policy.final_snapshot('b','C','execution')


def test_veto_scope_and_input_record_are_required(setup):
    evidence,policy=setup;_,record=prepare(policy,evidence)
    record['candidate_vetoes']={'pair1':{'reason':'no scope','claim_ids':['o1claim1']}}
    policy.decision_loader=lambda key:record
    with pytest.raises(ValueError):policy.final_snapshot('b','B','execution')
