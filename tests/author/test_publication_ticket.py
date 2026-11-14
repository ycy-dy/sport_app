from copy import deepcopy
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest
from ticket_fixture import synthetic_ticket_input
from sports_app.publication_ticket import prepare_snapshot, validate_snapshot
from sports_app.publication import PublicationService, canonical, fingerprint
from sports_app.published_settlement import PublishedSettlementService
from sports_app.storage import SQLiteStore
from sports_app.auth import Principal

ADMIN=Principal('a','admin','csrf')
REVIEWER=Principal('r','reviewer','rcsrf')


def snapshot():
    return dict(synthetic_ticket_input(),slot_key='slot',selection_hash='s',manifest_hash='m')


@pytest.fixture
def setup():
    root=Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix='.publication-ticket-',dir=root) as temp:
        store=SQLiteStore(temp,app_root=root)
        yield store,PublicationService(store,trusted_gate=lambda s:True)


def test_generate_and_bind_reviewed_original(setup):
    store,service=setup
    value=snapshot()
    service.save_draft(ADMIN,'csrf','d',value)
    assert 'settlement_ticket' not in value
    service.review(REVIEWER,'rcsrf','d','r')
    service.confirm(ADMIN,'csrf',draft_id='d',review_id='r',publication_id='p',request_id='req',human_confirmed=True)
    import json
    with closing(store.connect()) as db:
        original=json.loads(db.execute('SELECT original_json FROM publications').fetchone()[0])
    validate_snapshot(original)
    assert original['settlement_ticket']['legs'][0]['options'][0]['option_id']=='o1'
    assert original['settlement_ticket']['legs'][0]['options'][0]['original_odds']=='3'


@pytest.mark.parametrize('mutation',['price','line','identity','option','branch','scope'])
def test_separate_structure_cannot_drift_from_research(mutation):
    value=prepare_snapshot(snapshot())
    leg=value['settlement_ticket']['legs'][0]
    if mutation=='price':leg['options'][0]['original_odds']='99'
    elif mutation=='line':leg['options'][0]['line']='-1'
    elif mutation=='identity':leg['identity']['match_id']='other'
    elif mutation=='option':leg['options'][0]['option_id']='other'
    elif mutation=='branch':value['settlement_ticket']['branches']=[]
    else:leg['scope']='including_ot'
    with pytest.raises(ValueError):prepare_snapshot(value)


@pytest.mark.parametrize('mutation',['no_options','no_assessment','same_match','missing_price','wrong_total','missing_line'])
def test_invalid_source_options_fail_before_draft_write(setup,mutation):
    store,service=setup
    value=snapshot()
    if mutation=='no_options':del value['options']
    elif mutation=='no_assessment':del value['options'][0]['assessment_id']
    elif mutation=='same_match':value['options'][1]['identity']['match_id']='m1'
    elif mutation=='missing_price':del value['options'][0]['original_odds']
    elif mutation=='wrong_total':value['total_investment']='2'
    else:value['options'][0].update(market='HHAD',line=None)
    with pytest.raises((ValueError,KeyError,TypeError)):service.save_draft(ADMIN,'csrf','d',value)
    with closing(store.connect()) as db:
        assert not list(db.execute('SELECT * FROM publication_drafts'))


def test_legacy_missing_structure_reports_incomplete_without_guessing(setup):
    store,_=setup
    with closing(store.connect()) as db,db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?)',
                   ('old','slot','s','m','withdrawn',canonical({'recommendation_status':'conditional_low_confidence'}),'old-time'))
    service=PublishedSettlementService(store)
    assert service.ledger()[0]['leg_statistics_coverage']=='insufficient_frozen_structure'
    assert service.ledger()[0]['legs']==[]
    assert service.statistics()['actual_recommendations']==1
    assert service.statistics()['tickets_missing_leg_structure']==1
    assert service.statistics()['leg_statistics_coverage']=='incomplete'
