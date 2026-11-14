"""Synthetic execution validation only; NOT evidence of real Codex execution."""
from copy import deepcopy
import pytest
from sports_app.pair_policy import periods
from sports_app.publication import fingerprint
from review_fixture import add_coverage,attach_reviews
from test_pair_policy import batch, setup, ADMIN


@pytest.mark.parametrize('kickoff,label',[('2026-11-14T22:59:59+08:00','early'),
 ('2026-11-14T23:00:00+08:00','late'),('2026-11-15T00:30:00+08:00','late'),
 ('2026-11-15T02:00:00+08:00','late')])
def test_basketball_full_business_date_boundary(kickoff,label):
    value=batch();value['sport']='basketball'
    for event in value['events']:
        event['identity']['sport']='basketball';event['identity']['kickoff']=kickoff
    assert set(periods(value)[1].values())=={label}


def test_basketball_cross_midnight_not_cross_period(setup):
    _,policy=setup
    value=batch();value['sport']='basketball'
    for i,event in enumerate(value['events']):
        event['identity']['sport']='basketball'
        event['identity']['kickoff']=['2026-11-14T22:00:00+08:00','2026-11-15T01:00:00+08:00'][i]
        option=value['options'][f'o{i+1}'];option['market']='MNL'
        market=value['markets'][f'o{i+1}'];market['market']='MNL';market['outcomes']={'H':'3','A':'3'}
    value['candidates']['pair1']['period']='early'
    add_coverage(value)
    policy.freeze_batch(ADMIN,'a','b',value)
    with pytest.raises(ValueError):policy.build('b','pair1')


def record(policy,value):
    candidate=policy.build('b','pair1')
    digest=policy.freeze_agent_input(ADMIN,'a',input_id='input1',batch_id='b',
                                    assembled_input={'synthetic':'full input test'})
    return {'provider':'codex','status':'completed','execution_id':'synthetic-execution',
            'batch_hash':fingerprint(value),'manifest_hash':candidate['manifest_hash'],
            'selected_candidate_id':'pair1','input_id':'input1','input_hash':digest,
            'raw_response':'synthetic only, not real AI','response_hash':fingerprint('synthetic only, not real AI'),
            'completed_at':'2026-11-14T12:00:00+08:00','candidate_vetoes':{},
            'option_comparisons':{key:{'selected_option_id':key,'market_version':'mv1',
                                      'alternative_option_ids':[key+'alternative'],
                                      'rationale':'synthetic comparison','server_comparison_verified':True}
                                  for key in ('o1','o2')}}


def test_synthetic_record_validates_boundary_but_default_loader_refuses(setup):
    _,policy=setup;value=batch()
    for key in ('o1','o2'):
        value['options'][key+'alternative']=dict(value['options'][key],option_id=key+'alternative',selection='D')
    add_coverage(value)
    policy.freeze_batch(ADMIN,'a','b',value)
    with pytest.raises(ValueError):policy.final_snapshot('b','pair1','synthetic-execution')
    execution=attach_reviews(policy,record(policy,value),'b')
    policy.decision_loader=lambda key:deepcopy(execution)
    final=policy.final_snapshot('b','pair1','synthetic-execution')
    assert policy(final)
    assert final['decision_status']=='validated_server_execution'
    assert not policy(policy.build('b','pair1'))


@pytest.mark.parametrize('mutation',['response','binding','comparison','provider','status','input_hash','input_id'])
def test_unbound_or_incomplete_execution_rejected(setup,mutation):
    _,policy=setup;value=batch()
    for key in ('o1','o2'):
        value['options'][key+'alternative']=dict(value['options'][key],option_id=key+'alternative',selection='D')
    add_coverage(value)
    policy.freeze_batch(ADMIN,'a','b',value)
    execution=attach_reviews(policy,record(policy,value),'b')
    if mutation=='response':execution['raw_response']='changed'
    elif mutation=='binding':execution['batch_hash']='other'
    elif mutation=='comparison':execution['option_comparisons']={}
    elif mutation=='provider':execution['provider']='other'
    elif mutation=='input_hash':execution['input_hash']='other-nonempty-hash'
    elif mutation=='input_id':execution['input_id']='missing-record'
    else:execution['status']='timeout'
    policy.decision_loader=lambda key:execution
    with pytest.raises((ValueError,KeyError)):policy.final_snapshot('b','pair1','synthetic-execution')
