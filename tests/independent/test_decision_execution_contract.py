"""Synthetic loader boundary only. No actual runtime/input-store attestation."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import pytest
from test_pair_policy_contract import batch, setup, ADMIN, REVIEWER, AT
from sports_app.evidence import Claim, WEIGHTS
from sports_app.publication import fingerprint
from independent_decision_review_fixture import attach_reviews


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect', deny)
    monkeypatch.setattr('socket.create_connection', deny)
    monkeypatch.setattr('subprocess.Popen', deny)


def universe():
    value=batch()
    for key in ('o1','o2'):
        alternative=deepcopy(value['options'][key])
        alternative.update(option_id=key+'alt',assessment_id=key+'alt-assessment',selection='D')
        value['options'][key+'alt']=alternative
    # Independent lower market benchmark candidate (1/8 / (1/4+1/8+1/4)=1/5 per leg).
    for market in value['markets'].values():market['outcomes']['D']='8'
    value['candidates']['second']=dict(option_ids=['o1alt','o2alt'],period='early_middle',equal_stake='1',total_investment='1')
    for key in ('o1alt','o2alt'):value['options'][key]['original_odds']='8'
    from independent_decision_review_fixture import add_coverage
    return add_coverage(value)


def bind_input(policy, record):
    record['input_id'] = 'independent-input'
    record['input_hash'] = policy.freeze_agent_input(
        ADMIN, 'a', input_id=record['input_id'], batch_id='batch',
        assembled_input={'synthetic': 'independent assembled payload; no real AI'})
    return record


def execution(policy,value,selected='pair'):
    candidate=policy.build('batch',selected)
    options=candidate['options']
    raw='independent synthetic response; not real AI'
    record = dict(provider='codex',status='completed',execution_id='execution',batch_hash=fingerprint(value),
        manifest_hash=candidate['manifest_hash'],selected_candidate_id=selected,input_hash='synthetic-input-not-attested',
        raw_response=raw,response_hash=fingerprint(raw),completed_at=AT.isoformat(),candidate_vetoes={},
        option_comparisons={o['option_id']:dict(selected_option_id=o['option_id'],market_version='mv',
            alternative_option_ids=[o['option_id'][:-3] if o['option_id'].endswith('alt') else o['option_id']+'alt'],
            rationale='synthetic same-event comparison',server_comparison_verified=True) for o in options})
    return bind_input(policy, record)


def test_candidate_build_never_authorizes_default_final_gate(setup):
    _,policy=setup;value=universe();policy.freeze_batch(ADMIN,'a','batch',value)
    candidate=policy.build('batch','pair')
    assert policy.validate_candidate(candidate)
    assert candidate['decision_status']=='candidate_only_agent_incomplete'
    assert policy(candidate) is False
    with pytest.raises(ValueError):policy.final_snapshot('batch','pair','execution')


def test_synthetic_record_positive_control_preserves_benchmark(setup):
    _,policy=setup;value=universe();policy.freeze_batch(ADMIN,'a','batch',value)
    candidate=policy.build('batch','pair');record=execution(policy,value)
    record=attach_reviews(policy,record)
    policy.decision_loader=lambda key:deepcopy(record)
    final=policy.final_snapshot('batch','pair','execution')
    assert policy(final)
    assert final['joint_p_mkt']==candidate['joint_p_mkt']
    assert final['market_ranking']==candidate['market_ranking']
    assert final['model_probability'] is None and final['EV'] is None


@pytest.mark.parametrize('mutation',['no_record','empty_response','response_drift','batch_drift','manifest_drift',
    'missing_input','provider','timeout','cancelled','forged_success','execution_id','no_comparisons','unverified_comparison',
    'self_comparison','different_match','future_completion','missing_input_record','wrong_input_batch'])
def test_incomplete_or_mismatched_execution_refused(setup,mutation):
    _,policy=setup;value=universe();policy.freeze_batch(ADMIN,'a','batch',value)
    record=execution(policy,value)
    if mutation=='no_record':record=None
    elif mutation=='empty_response':record['raw_response']=''
    elif mutation=='response_drift':record['raw_response']='changed'
    elif mutation=='batch_drift':record['batch_hash']='other'
    elif mutation=='manifest_drift':record['manifest_hash']='other'
    elif mutation=='missing_input':record['input_hash']=''
    elif mutation=='provider':record['provider']='other'
    elif mutation=='timeout':record['status']='timeout'
    elif mutation=='cancelled':record['status']='cancelled'
    elif mutation=='forged_success':record.update(status='running',success=True)
    elif mutation=='execution_id':record['execution_id']='other'
    elif mutation=='no_comparisons':record['option_comparisons']={}
    elif mutation=='unverified_comparison':record['option_comparisons']['o1']['server_comparison_verified']=False
    elif mutation=='self_comparison':record['option_comparisons']['o1']['alternative_option_ids']=['o1']
    elif mutation=='different_match':record['option_comparisons']['o1']['alternative_option_ids']=['o2alt']
    elif mutation=='missing_input_record':record['input_id']='absent-record'
    elif mutation=='wrong_input_batch':
        policy.freeze_batch(ADMIN,'a','other-batch',value)
        record['input_id']='other-batch-input'
        record['input_hash']=policy.freeze_agent_input(ADMIN,'a',input_id=record['input_id'],
            batch_id='other-batch',assembled_input={'synthetic':'wrong batch control'})
    else:record['completed_at']=(AT+timedelta(seconds=1)).isoformat()
    record=attach_reviews(policy,record)
    if mutation=='unverified_comparison':
        # A legacy boolean cannot authorize when the actual server review is absent.
        record['option_comparisons']['o1'].pop('review_id',None)
        record['option_comparisons']['o1']['server_comparison_verified']=True
    policy.decision_loader=lambda key:deepcopy(record)
    with pytest.raises((ValueError,KeyError)):policy.final_snapshot('batch','pair','execution')


def test_client_success_fields_cannot_authorize_candidate(setup):
    _,policy=setup;value=universe();policy.freeze_batch(ADMIN,'a','batch',value)
    candidate=policy.build('batch','pair')
    candidate.update(decision_status='validated_server_execution',decision_id='execution',provider='codex',status='completed')
    assert policy(candidate) is False


@pytest.mark.parametrize('mode',['absent','unrecorded','verified'])
def test_second_rank_requires_repository_sourced_veto_and_keeps_market_values(setup,mode):
    repo,policy=setup;value=universe();policy.freeze_batch(ADMIN,'a','batch',value)
    candidate=policy.build('batch','second')
    assert candidate['market_ranking']==['pair','second']
    record=execution(policy,value,'second')
    if mode!='absent':record['candidate_vetoes']={'pair':dict(reason='synthetic verified risk',claim_ids=['risk'],option_ids=['o1'])}
    if mode=='verified':
        claim=Claim('risk','o1',('04',),'https://risk.example.org/report','L1',AT-timedelta(hours=2),
                    AT-timedelta(hours=1),'risk-group','synthetic risk quote',True,False)
        repo.ingest(ADMIN,'a',claim)
        repo.verify(REVIEWER,'r',receipt_id='risk-receipt',claim_id='risk',
                    quote_hash=sha256(claim.quote.encode()).hexdigest(),conclusion='verified')
    record=attach_reviews(policy,record)
    policy.decision_loader=lambda key:deepcopy(record)
    if mode=='verified':
        final=policy.final_snapshot('batch','second','execution')
        assert policy(final)
        assert final['joint_p_mkt']==candidate['joint_p_mkt']
        assert final['market_ranking']==['second']
        assert final['vetoed_option_ids']==['o1']
    else:
        with pytest.raises(ValueError):policy.final_snapshot('batch','second','execution')


def test_nonempty_unbound_input_hash_must_not_certify_final_decision(setup):
    # Deliberately red until execution input is checked against actual frozen input storage.
    _,policy=setup;value=universe();policy.freeze_batch(ADMIN,'a','batch',value)
    record=execution(policy,value);record['input_hash']='different-nonempty-input'
    record=attach_reviews(policy,record)
    policy.decision_loader=lambda key:deepcopy(record)
    with pytest.raises(ValueError):policy.final_snapshot('batch','pair','execution')


def qualify_primary(repo,value):
    for key in ('o1','o2'):
        option=value['options'][key]
        option['dimensions']=[(f'{i:02d}','verified') for i in range(1,24)]
        option['claim_ids']=[]
        for index in (1,2):
            ident=f'{key}-qualified-{index}'
            claim=Claim(ident,key,tuple(WEIGHTS),f'https://source{index}.example.org/report','L1',
                        AT-timedelta(hours=2),AT-timedelta(hours=1),f'group-{index}',
                        'synthetic qualified evidence',True,False)
            repo.ingest(ADMIN,'a',claim)
            repo.verify(REVIEWER,'r',receipt_id=ident+'receipt',claim_id=ident,
                        quote_hash=sha256(claim.quote.encode()).hexdigest(),conclusion='verified')
            option['claim_ids'].append(ident)


def deferred_execution(policy,value,selected):
    # Do not call build(selected): that is exactly the premature rejection under test.
    raw='synthetic veto/reselection, not real Codex'
    option_ids=value['candidates'][selected]['option_ids']
    record = dict(provider='codex',status='completed',execution_id='execution',batch_hash=fingerprint(value),
        manifest_hash=fingerprint(value['manifest']),selected_candidate_id=selected,
        input_hash='synthetic-input-not-attested',raw_response=raw,response_hash=fingerprint(raw),
        completed_at=AT.isoformat(),candidate_vetoes={},
        option_comparisons={key:dict(selected_option_id=key,market_version='mv',
            alternative_option_ids=[key[:-3] if key.endswith('alt') else key+'alt'],
            rationale='synthetic same-event comparison',server_comparison_verified=True) for key in option_ids})
    return bind_input(policy, record)


def seed_option_risk(repo):
    claim=Claim('option-risk','o1',('04',),'https://risk.example.org/report','L1',
                AT-timedelta(hours=2),AT-timedelta(hours=1),'risk-group',
                'synthetic risk applies to selected o1 option',True,False)
    repo.ingest(ADMIN,'a',claim)
    repo.verify(REVIEWER,'r',receipt_id='option-risk-receipt',claim_id=claim.claim_id,
                quote_hash=sha256(claim.quote.encode()).hexdigest(),conclusion='verified')


@pytest.mark.parametrize('has_veto',[False,True])
def test_qualified_veto_rebuilds_conditional_pool_without_forging_scores(setup,has_veto):
    repo,policy=setup;value=universe();qualify_primary(repo,value)
    policy.freeze_batch(ADMIN,'a','batch',value)
    primary=policy.build('batch','pair')
    assert primary['recommendation_status']=='evidence_qualified'
    assert all(a['eligible'] and a['score']==100 for a in primary['assessment_summary'].values())
    record=deferred_execution(policy,value,'second')
    if has_veto:
        seed_option_risk(repo)
        record['candidate_vetoes']={'pair':dict(reason='risk applies to o1',claim_ids=['option-risk'],option_ids=['o1'])}
    record=attach_reviews(policy,record)
    policy.decision_loader=lambda key:deepcopy(record)
    if not has_veto:
        with pytest.raises(ValueError):policy.final_snapshot('batch','second','execution')
    else:
        final=policy.final_snapshot('batch','second','execution')
        assert final['recommendation_status']=='conditional_low_confidence'
        assert all(not a['eligible'] and a['quality_gaps'] for a in final['assessment_summary'].values())
        assert policy(final)
        # Primary evidence is not rewritten to manufacture fallback eligibility.
        primary_after=policy.build('batch','pair')
        assert primary_after['assessment_summary']==primary['assessment_summary']
        assert primary_after['joint_p_mkt']==primary['joint_p_mkt']


def test_vetoed_option_cannot_reenter_via_new_partner_candidate(setup):
    repo,policy=setup;value=universe();qualify_primary(repo,value)
    value['candidates']['reuse']=dict(option_ids=['o1','o2alt'],period='early_middle',equal_stake='1',total_investment='1')
    policy.freeze_batch(ADMIN,'a','batch',value)
    seed_option_risk(repo)
    record=deferred_execution(policy,value,'reuse')
    record['candidate_vetoes']={'pair':dict(reason='risk applies to o1',claim_ids=['option-risk'],option_ids=['o1'])}
    record=attach_reviews(policy,record)
    policy.decision_loader=lambda key:deepcopy(record)
    with pytest.raises(ValueError):policy.final_snapshot('batch','reuse','execution')
