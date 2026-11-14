from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import pytest
from test_pair_policy import setup,batch,ADMIN,REVIEWER,AT
from sports_app.decision_review import DecisionReviewRepository
from sports_app.evidence import Claim


def prepare(evidence,policy):
    value=batch()
    catalog={'catalog_id':'synthetic-HDA','version':'v1','source_id':'synthetic-authority',
             'exhaustive':True,'universe':['home','draw','away']}
    for key in ('o1','o2'):
        option=value['options'][key]
        option.update(coverage_contract=catalog,covered_atoms=['home'])
        alt=deepcopy(option);alt.update(option_id=key+'alt',selection='D',covered_atoms=['draw'])
        value['options'][key+'alt']=alt
        claim=Claim(key+'claim',key,('04',),'https://authority.example.org/report','L1',
                    AT-timedelta(hours=1),AT-timedelta(hours=1),'g','synthetic quoted rationale',True,False)
        evidence.ingest(ADMIN,'a',claim)
        evidence.verify(REVIEWER,'r',receipt_id=key+'receipt',claim_id=claim.claim_id,
                        quote_hash=sha256(claim.quote.encode()).hexdigest(),conclusion='verified')
    policy.freeze_batch(ADMIN,'a','b',value)
    repo=DecisionReviewRepository(policy);policy.review_repository=repo
    return repo,value


def save(repo,kind='comparison'):
    return repo.save(REVIEWER,'r',review_id='review',batch_id='b',kind=kind,scope=['o1'],
        source_quotes={'o1':{'o1claim':sha256('synthetic quoted rationale'.encode()).hexdigest()}},
        rationale='human synthetic interpretation, not real semantics',valid_until=(AT+timedelta(hours=1)).isoformat())


def test_server_record_recomputes_universe_coverage_prices_and_costs(setup):
    evidence,policy=setup;repo,value=prepare(evidence,policy)
    save(repo)
    body=repo.validate('review',batch_id='b',batch=value,kind='comparison',scope=['o1'],at=AT)
    computed=body['computed']['o1']
    assert set(computed['alternatives'])=={'o1alt'}
    assert computed['alternatives']['o1alt']['added']==['home']
    assert computed['alternatives']['o1alt']['lost']==['draw']
    assert computed['candidate_costs']['pair1']['total_investment']=='1'
    assert body['sources']['o1:o1claim']['quote']=='synthetic quoted rationale'


@pytest.mark.parametrize('change',['expired','wrong_scope','wrong_version','missing_record'])
def test_stale_or_misbound_record_rejected(setup,change):
    evidence,policy=setup;repo,value=prepare(evidence,policy);save(repo)
    scope=['o1'];at=AT;ident='review'
    if change=='expired':at+=timedelta(hours=2)
    elif change=='wrong_scope':scope=['o2']
    elif change=='wrong_version':value['market_version']='changed'
    else:ident='missing'
    with pytest.raises(ValueError):repo.validate(ident,batch_id='b',batch=value,kind='comparison',scope=scope,at=at)


def test_replacement_costs_enumerate_every_branch_and_weak_alternative(setup):
    evidence,policy=setup;repo,value=prepare(evidence,policy)
    value['options']['o1alt']['original_odds']='1.5'
    value['markets']['o1']['outcomes']['D']='1.5'
    computed=repo.comparison(value,'o1',AT)
    base=computed['candidate_costs']['pair1']
    alternative=computed['alternatives']['o1alt']['replacement_costs']['pair1']
    assert base['compliant'] and len(base['branches'])==1
    assert alternative['compliant'] and alternative['branches'][0]['effective_multiple']=='4.5'
    value['options']['o2']['original_odds']='1.34'
    value['markets']['o2']['outcomes']['H']='1.34'
    weak=repo.ticket_costs(value,'o1','o1alt',AT)['pair1']
    assert not weak['compliant'] and weak['reasons']
    assert weak['branches'][0]['effective_multiple']=='2.010'


def test_risk_review_requires_actual_quote_receipt_and_reviewer(setup):
    evidence,policy=setup;repo,value=prepare(evidence,policy)
    save(repo,'risk')
    assert repo.validate('review',batch_id='b',batch=value,kind='risk',scope=['o1'],at=AT)['scope']==['o1']
    with pytest.raises(PermissionError):repo.save(ADMIN,'a',review_id='bad',batch_id='b',kind='risk',scope=['o1'],
        source_quotes={'o1':{}},rationale='x',valid_until=(AT+timedelta(hours=1)).isoformat())
    with pytest.raises(ValueError):repo.save(REVIEWER,'r',review_id='bad',batch_id='b',kind='risk',scope=['o1'],
        source_quotes={'o1':{'o1claim':'wrong quote'}},rationale='x',valid_until=(AT+timedelta(hours=1)).isoformat())
