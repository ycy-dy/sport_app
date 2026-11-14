from hashlib import sha256
import pytest
from sports_app.agent_input import build_agent_input, REQUIRED_OPTION_FIELDS, IDENTITY_FIELDS
from sports_app.manifest import POLICY_FILES, manifest


def inputs():
    # Synthetic policy texts test plumbing only, not actual 039 semantic coverage.
    documents = {name: 'synthetic document '+name for name in POLICY_FILES}
    rules = {name: sha256(value.encode()).hexdigest() for name,value in documents.items()}
    versions = manifest(policy_revision='test-policy',analysis_revision='test-analysis',
                        schema_hash='test-schema',code_hash='test-code',codex_identity='codex',rules=rules)
    option = {key: 'synthetic research' for key in REQUIRED_OPTION_FIELDS}
    from ticket_fixture import synthetic_ticket_input
    option.update(synthetic_ticket_input()['options'][0])
    identity=option['identity']
    option['settlement_scope']=option['scope']
    option['event_definition']=dict(sport=identity['sport'],league=identity['league'],
        business_day=identity['business_day'],match_id=identity['match_id'],kickoff=identity['kickoff'],
        home_team=identity['home_id'],away_team=identity['away_id'],market=option['market'],line=None,
        original_odds=option['original_odds'],source_as_of='2026-11-14T05:00:00+00:00')
    option['evidence_as_of'] = '2026-11-14T05:00:00+00:00'
    return dict(options=[option],candidates=[{'id':'synthetic-candidate'}],exclusions=[],
                runtime_manifest=versions,rule_documents=documents)


def test_full_documents_and_manifest_frozen_without_execution():
    args = inputs()
    result = build_agent_input(**args)
    assert set(result['input']['rule_documents']) == set(POLICY_FILES)
    assert result['input']['execution_status'] == 'input_assembled_not_executed'
    args['options'][0]['covered_paths'] = 'modified'
    assert result['input']['options'][0]['covered_paths'] == 'synthetic research'


@pytest.mark.parametrize('field',REQUIRED_OPTION_FIELDS)
def test_missing_039_field_rejected(field):
    args = inputs()
    del args['options'][0][field]
    with pytest.raises(ValueError): build_agent_input(**args)


@pytest.mark.parametrize('market,line,sport', [('HAD',None,'football'),('MNL',None,'basketball'),
    ('HHAD','-1','football'),('HDC','-2.5','basketball'),('HILO','161.5','basketball')])
def test_market_specific_valid_line(market,line,sport):
    args=inputs();option=args['options'][0]
    scope='regulation_90' if sport=='football' else 'including_ot'
    option.update(market=market,line=line,scope=scope,settlement_scope=scope)
    option['identity']['sport']=sport
    option['event_definition'].update(market=market,line=line,sport=sport)
    assert build_agent_input(**args)


@pytest.mark.parametrize('market,line',[('HAD','not_applicable'),('HAD','0'),('HHAD',None),
    ('HDC',None),('HILO','not_applicable'),('HDC','NaN'),('HILO','Infinity'),('HHAD','-0.5')])
def test_invalid_empty_or_placeholder_market_line_rejected(market,line):
    args=inputs();option=args['options'][0]
    option.update(market=market,line=line)
    option['event_definition'].update(market=market,line=line)
    with pytest.raises(ValueError):build_agent_input(**args)


@pytest.mark.parametrize('field,value',[('market','MNL'),('line','-1'),('home_team','other'),
    ('away_team','other'),('match_id','other'),('kickoff','other'),('original_odds','99')])
def test_event_frozen_option_mismatch_rejected(field,value):
    args=inputs();args['options'][0]['event_definition'][field]=value
    with pytest.raises(ValueError):build_agent_input(**args)


def test_scope_mismatch_rejected():
    args=inputs();args['options'][0]['settlement_scope']='including_ot'
    with pytest.raises(ValueError):build_agent_input(**args)


def test_policy_drift_and_unaccepted_probabilities_rejected():
    args = inputs()
    args['rule_documents']['analysis-contract.yaml'] += 'changed'
    with pytest.raises(ValueError): build_agent_input(**args)
    args = inputs()
    args['options'][0]['model_probability'] = 0.8
    with pytest.raises(ValueError): build_agent_input(**args)
