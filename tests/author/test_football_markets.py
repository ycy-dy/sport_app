from dataclasses import replace, asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest
from ticket_fixture import synthetic_ticket_input
from sports_app.settlement import Identity, TicketLeg, ResultSnapshot, settle_leg
from sports_app.publication_ticket import prepare_snapshot
from sports_app.publication import PublicationService
from sports_app.published_settlement import PublishedSettlementService
from sports_app.storage import SQLiteStore
from sports_app.auth import Principal


def base(market,selection,score=(2,1),line=None,contract=None):
    identity=Identity('football','synthetic','2026-11-14','m1','2026-11-14T18:00:00+08:00','h','a')
    leg=TicketLeg('o',identity,market,selection,line,'regulation_90',contract)
    result=ResultSnapshot(identity,'finished','H:A',score,'regulation_90',source_id='authority')
    return leg,result


@pytest.mark.parametrize('goals,selection,expected',[(8,'7+','hit'),(7,'7+','hit'),(6,'7+','miss'),
                                                   (3,'3','hit'),(3,'2','miss'),(2,'2','hit'),(1,'3','miss')])
def test_ttg_buckets(goals,selection,expected):
    leg,result=base('TTG',selection,(goals,0))
    assert settle_leg(leg,result).outcome==expected


def test_hafu_explicit_half_source_identity_and_direction():
    leg,result=base('HAFU','AH')
    complete=replace(result,half_scores=(0,1),half_identity=leg.identity,half_orientation='H:A',
                     half_scope='regulation_90',half_source_id='half-authority')
    assert settle_leg(leg,complete).outcome=='hit'
    assert settle_leg(replace(leg,selection='HH'),complete).outcome=='miss'
    assert settle_leg(leg,result).outcome=='pending'
    assert settle_leg(leg,replace(complete,half_conflict=True)).outcome=='pending'
    assert settle_leg(leg,replace(complete,half_orientation='A:H')).outcome=='pending'
    assert settle_leg(leg,replace(complete,half_identity=replace(leg.identity,match_id='other'))).outcome=='pending'
    assert settle_leg(leg,replace(complete,half_scores=(3,0))).outcome=='pending'
    reverse=replace(complete,orientation='A:H',scores=(1,2),half_orientation='A:H',half_scores=(1,0))
    assert settle_leg(leg,reverse).outcome=='hit'


def contract():
    return {'kind':'crs_other_complement_v1','source_id':'synthetic-complete-market',
            'orientation':'H:A','scope':'regulation_90',
            'listed_scores':['0:0','1:0','0:1','2:1'],
            'other_codes':['H_OTHER','D_OTHER','A_OTHER']}


@pytest.mark.parametrize('listed',[[['2:1']],[{'score':'2:1'}],[None],[True],['02:1'],['2:1','2:1']])
def test_malformed_crs_catalog_rejected_and_settlement_pending(listed):
    from sports_app.football_markets import validate_football_selection
    malformed=contract()
    malformed['listed_scores']=listed
    with pytest.raises(ValueError):
        validate_football_selection('CRS','H_OTHER',malformed)
    leg,result=base('CRS','H_OTHER',contract=malformed)
    assert settle_leg(leg,result).outcome=='pending'
    value=synthetic_ticket_input()
    value['options'][0].update(market='CRS',selection='H_OTHER',market_contract=malformed)
    with pytest.raises(ValueError):
        prepare_snapshot(value)


def test_crs_exact_and_explicit_other_complement():
    leg,result=base('CRS','2:1')
    assert settle_leg(leg,result).outcome=='hit'
    assert settle_leg(leg,replace(result,scores=(3,1))).outcome=='miss'
    other=replace(leg,selection='H_OTHER',market_contract=contract())
    assert settle_leg(other,result).outcome=='miss'
    assert settle_leg(other,replace(result,scores=(3,1))).outcome=='hit'
    assert settle_leg(replace(other,market_contract=None),result).outcome=='pending'
    assert settle_leg(replace(other,selection='D_OTHER'),replace(result,scores=(4,4))).outcome=='hit'


@pytest.mark.parametrize('line,score,expected',[('-1',(1,1),'hit'),('-1',(2,1),'hit'),('-1',(3,1),'miss'),
                                              ('1',(1,1),'hit'),('1',(1,2),'hit'),('1',(1,3),'miss')])
def test_dd_components_are_union_not_unbeaten(line,score,expected):
    a,result=base('HAD','D',score)
    b=replace(a,market='HHAD',selection='D',line=line)
    from sports_app.published_settlement import union_outcome
    assert union_outcome([settle_leg(a,result).outcome,settle_leg(b,result).outcome])==expected


@pytest.mark.parametrize('market,selection',[('TTG','7+'),('HAFU','AH'),('CRS','2:1')])
def test_generator_supports_frozen_football_options(market,selection):
    value=synthetic_ticket_input()
    value['options'][0].update(market=market,selection=selection)
    ticket=prepare_snapshot(value)['settlement_ticket']
    assert ticket['legs'][0]['options'][0]['market']==market


def test_generator_requires_crs_complement_contract_and_no_fake_dd_quote():
    value=synthetic_ticket_input()
    value['options'][0].update(market='CRS',selection='H_OTHER')
    with pytest.raises(ValueError):prepare_snapshot(value)
    value['options'][0]['market_contract']=contract()
    assert prepare_snapshot(value)['settlement_ticket']['legs'][0]['options'][0]['market_contract']==contract()
    value['options'][0].update(market='DD',selection='D')
    with pytest.raises(ValueError):prepare_snapshot(value)


def test_ttg_and_dd_full_two_leg_publication_settlement_preserves_separate_prices():
    root=Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix='.football-markets-',dir=root) as temp:
        store=SQLiteStore(temp,app_root=root)
        pub=PublicationService(store,trusted_gate=lambda s:True)
        value=synthetic_ticket_input()
        first=value['options'][0];second=value['options'][1]
        first.update(market='HAD',selection='D',original_odds='4')
        alternative=dict(first,option_id='o1handicap',assessment_id='a1handicap',market='HHAD',line='-1',original_odds='5')
        second.update(market='TTG',selection='2',original_odds='4')
        other_total=dict(second,option_id='o2three',assessment_id='a2three',selection='3',original_odds='5')
        value.update(options=[first,alternative,second,other_total],total_investment='4',
                     slot_key='slot',selection_hash='s',manifest_hash='m')
        admin=Principal('admin','admin','csrf');reviewer=Principal('reviewer','reviewer','r')
        pub.save_draft(admin,'csrf','d',value);pub.review(reviewer,'r','d','r')
        pub.confirm(admin,'csrf',draft_id='d',review_id='r',publication_id='p',request_id='req',human_confirmed=True)
        results={}
        for leg_id,option,score in [('l1',first,(2,1)),('l2',second,(2,1))]:
            results[leg_id]=ResultSnapshot(Identity(**option['identity']),'finished','H:A',score,
                                           'regulation_90',source_id='authority')
        summary=PublishedSettlementService(store).record(admin,'csrf',publication_id='p',results=results)
        assert summary['outcome']=='hit' and len(summary['branches'])==4
        assert sum(b['outcome']=='hit' for b in summary['branches'])==1
        assert {tuple(b['original_odds']) for b in summary['branches']}=={('4','4'),('4','5'),('5','4'),('5','5')}
