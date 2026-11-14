"""Independent review bindings; synthetic coverage and quotes, not semantics."""
from copy import deepcopy
from datetime import timedelta
import pytest
from test_pair_policy_contract import setup, ADMIN, AT
from test_decision_execution_contract import universe, execution
from independent_decision_review_fixture import attach_reviews, READER
from sports_app.decision_review import DecisionReviewRepository


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect',deny)
    monkeypatch.setattr('socket.create_connection',deny)
    monkeypatch.setattr('subprocess.Popen',deny)


def prepare(setup):
    _,policy=setup;value=universe();policy.freeze_batch(ADMIN,'a','batch',value)
    record=attach_reviews(policy,execution(policy,value))
    policy.decision_loader=lambda ident:deepcopy(record)
    assert policy.final_snapshot('batch','pair','execution')['decision_status']=='validated_server_execution'
    return policy,value,record


@pytest.mark.parametrize('mutation',['missing','boolean_only','wrong_scope','expired','wrong_version'])
def test_final_gate_requires_current_server_comparison(setup,mutation):
    policy,value,record=prepare(setup)
    comparison=record['option_comparisons']['o1']
    if mutation=='missing':comparison['review_id']='missing'
    elif mutation=='boolean_only':comparison.pop('review_id');comparison['server_comparison_verified']=True
    elif mutation=='wrong_scope':comparison['review_id']=record['option_comparisons']['o2']['review_id']
    elif mutation=='expired':policy.evidence.clock=lambda:AT+timedelta(minutes=30)
    else:record['manifest_hash']='drift'
    with pytest.raises(ValueError):policy.final_snapshot('batch','pair','execution')


@pytest.mark.parametrize('mutation',['quote_hash','other_option_claim','unauthorized_actor'])
def test_bad_quote_receipt_scope_or_actor_cannot_create_review(setup,mutation):
    policy,value,record=prepare(setup)
    repo=policy.review_repository
    source=repo.validate(record['option_comparisons']['o1']['review_id'],batch_id='batch',batch=value,
                         kind='comparison',scope=['o1'],at=AT)['source_quotes']
    if mutation=='quote_hash':source['o1']['review-source-o1']='wrong'
    elif mutation=='other_option_claim':source={'o1':{'review-source-o2':'wrong'}}
    actor=ADMIN if mutation=='unauthorized_actor' else READER
    with pytest.raises((ValueError,PermissionError)):
        repo.save(actor,actor.csrf_token,review_id='bad',batch_id='batch',kind='comparison',scope=['o1'],
                  source_quotes=source,rationale='synthetic',valid_until=(AT+timedelta(minutes=10)).isoformat())


@pytest.mark.parametrize('flag',[True,False,None])
def test_valid_server_review_not_granted_or_revoked_by_legacy_boolean(setup,flag):
    policy,_,record=prepare(setup)
    for comparison in record['option_comparisons'].values():
        comparison['server_comparison_verified']=flag
    assert policy.final_snapshot('batch','pair','execution')['decision_status']=='validated_server_execution'


def cost_universe():
    value=universe()
    # Candidate has HAD x adjacent TTG (2,3), two unit branches, total 2.
    left=value['options']['o1'];left['original_odds']='4'
    value['markets']['o1']['outcomes']={'H':'4','D':'4','A':'4'}
    weak=deepcopy(left);weak.update(option_id='weak',assessment_id='weak-a',selection='D',original_odds='1.5',market_snapshot_id='weak-market')
    value['options']['weak']=weak
    value['markets']['weak-market']=deepcopy(value['markets']['o1'])
    value['markets']['weak-market']['outcomes']={'H':'4','D':'1.5','A':'4'}
    right=value['options']['o2'];right.update(market='TTG',selection='2',original_odds='4')
    value['markets']['o2'].update(market='TTG',outcomes={str(i):'4' for i in range(7)})
    value['markets']['o2']['outcomes']['3']='3';value['markets']['o2']['outcomes']['7+']='4'
    other=deepcopy(right);other.update(option_id='ttg3',assessment_id='ttg3-a',selection='3',original_odds='3')
    value['options']['ttg3']=other
    value['candidates']={'pair':dict(option_ids=['o1','o2','ttg3'],period='early_middle',equal_stake='1',total_investment='2')}
    return value


def test_replacement_ticket_single_weak_branch_is_not_average_accepted(setup):
    _,policy=setup;value=cost_universe();repo=DecisionReviewRepository(policy)
    original=repo.ticket_costs(value,'o1','o1',AT)['pair']
    assert original['compliant'] and len(original['branches'])==2
    weak=repo.ticket_costs(value,'o1','weak',AT)['pair']
    from decimal import Decimal
    multiples=sorted(Decimal(b['effective_multiple']) for b in weak['branches'])
    assert multiples==[Decimal('2.25'),Decimal('3')]
    assert not weak['compliant'] and weak['reasons']
    assert weak['total_investment']=='2' and weak['equal_stake']=='1'


def test_replacement_ticket_wrong_total_rejected_even_when_multiples_pass(setup):
    _,policy=setup;value=cost_universe();value['candidates']['pair']['total_investment']='1'
    row=DecisionReviewRepository(policy).ticket_costs(value,'o1','o1',AT)['pair']
    from decimal import Decimal
    assert len(row['branches'])==2
    assert all(Decimal(b['effective_multiple'])>Decimal('2.5') for b in row['branches'])
    assert not row['compliant'] and row['reasons']


def test_replacement_incompatible_structure_explicitly_noncompliant(setup):
    _,policy=setup;value=cost_universe()
    # HHAD x HHAD is outside the authorized eight structures.
    for key in ('o1','o2'):
        value['options'][key].update(market='HHAD',selection='H',line='-1',original_odds='4')
        value['markets'][key].update(market='HHAD',line='-1',outcomes={'H':'4','D':'4','A':'4'})
    value['candidates']['pair'].update(option_ids=['o1','o2'],total_investment='1')
    row=DecisionReviewRepository(policy).ticket_costs(value,'o1','o1',AT)['pair']
    assert not row['compliant']
    assert any('authorized structure' in reason for reason in row['reasons'])
