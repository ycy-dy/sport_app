"""Independent event/Option line binding; no task or external IO."""
from copy import deepcopy
from hashlib import sha256
import pytest
from independent_ticket_fixture import research_snapshot
from sports_app.agent_input import build_agent_input, REQUIRED_OPTION_FIELDS
from sports_app.manifest import POLICY_FILES


def valid_option():
    option=research_snapshot()['options'][0]
    for field in REQUIRED_OPTION_FIELDS:option.setdefault(field,'synthetic research')
    identity=option['identity']
    option['settlement_scope']=option['scope']
    option['evidence_as_of']='2026-11-14T12:00:00+08:00'
    option['event_definition']=dict(**{k:identity[k] for k in ('sport','league','business_day','match_id','kickoff')},
        home_team=identity['home_id'],away_team=identity['away_id'],market=option['market'],line=None,
        original_odds=option['original_odds'],source_as_of=option['evidence_as_of'])
    return option


def assemble(option):
    documents={key:'independent synthetic '+key for key in POLICY_FILES}
    return build_agent_input(options=[option],candidates={'synthetic':['o1']},exclusions=[],
        runtime_manifest={'runtime_provider':'codex','rule_hashes':{k:sha256(v.encode()).hexdigest() for k,v in documents.items()}},
        rule_documents=documents)


def test_legal_null_and_finite_line_controls():
    option=valid_option()
    assert assemble(option)['input']['options'][0]['event_definition']['line'] is None
    option.update(market='HHAD',line='-1')
    option['event_definition'].update(market='HHAD',line='-1')
    assert assemble(option)['input']['options'][0]['line']=='-1'


@pytest.mark.parametrize('value',[None,'not_applicable','NaN','Infinity',True,'-1.5'])
def test_hhad_missing_or_invalid_required_line_rejected(value):
    option=valid_option();option.update(market='HHAD',line=value)
    option['event_definition'].update(market='HHAD',line=value)
    with pytest.raises(ValueError):assemble(option)


@pytest.mark.parametrize('field,value',[('market','TTG'),('line','not_applicable'),('sport','basketball'),
    ('league','other'),('business_day','2026-11-15'),('match_id','other'),('kickoff','changed'),
    ('home_team','other'),('away_team','other'),('original_odds','99')])
def test_event_definition_mismatch_rejected(field,value):
    option=valid_option();option['event_definition'][field]=value
    with pytest.raises(ValueError):assemble(option)


def test_scope_mismatch_rejected():
    option=valid_option();option['settlement_scope']='including_ot'
    with pytest.raises(ValueError):assemble(option)


def test_explicit_line_field_cannot_be_omitted():
    option=valid_option();del option['event_definition']['line']
    with pytest.raises(ValueError):assemble(option)
