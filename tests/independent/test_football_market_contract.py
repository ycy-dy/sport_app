"""Independent football event boundaries, malformed complements, full publication."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import pytest
from independent_ticket_fixture import research_snapshot
from sports_app.auth import Principal
from sports_app.football_markets import validate_football_selection
from sports_app.settlement import Identity, TicketLeg, ResultSnapshot, settle_leg
from sports_app.storage import SQLiteStore
from sports_app.publication import PublicationService
from sports_app.published_settlement import PublishedSettlementService, union_outcome


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect', deny)
    monkeypatch.setattr('socket.create_connection', deny)
    monkeypatch.setattr('subprocess.Popen', deny)


def event(market, selection, score=(2,1), line=None, contract=None):
    identity=Identity(**research_snapshot()['options'][0]['identity'])
    return (TicketLeg('option',identity,market,selection,line,'regulation_90',contract),
            ResultSnapshot(identity,'finished','H:A',score,'regulation_90',source_id='synthetic'))


@pytest.mark.parametrize('score,selection,expected', [((4,4),'7+','hit'),((7,0),'7+','hit'),
    ((6,0),'7+','miss'),((2,1),'3','hit'),((2,1),'2','miss')])
def test_ttg_independent_buckets(score,selection,expected):
    leg,result=event('TTG',selection,score)
    assert settle_leg(leg,result).outcome==expected


def halftime():
    leg,result=event('HAFU','AH')
    return leg,replace(result,half_scores=(0,1),half_identity=leg.identity,
                      half_orientation='H:A',half_scope='regulation_90',half_source_id='half-source')


def test_hafu_reverse_direction_positive_control():
    leg,result=halftime()
    assert settle_leg(leg,result).outcome=='hit'
    reversed_result=replace(result,orientation='A:H',scores=(1,2),half_orientation='A:H',half_scores=(1,0))
    assert settle_leg(leg,reversed_result).outcome=='hit'
    assert settle_leg(replace(leg,selection='HH'),result).outcome=='miss'


@pytest.mark.parametrize('mutation',['missing','identity','orientation','source','conflict','exceeds'])
def test_hafu_missing_or_inconsistent_halftime_pending(mutation):
    leg,result=halftime()
    changes={'missing':dict(half_scores=None),'identity':dict(half_identity=replace(leg.identity,match_id='other')),
             'orientation':dict(half_orientation='A:H'),'source':dict(half_source_id=''),
             'conflict':dict(half_conflict=True),'exceeds':dict(half_scores=(3,1))}
    assert settle_leg(leg,replace(result,**changes[mutation])).outcome=='pending'


def complement():
    return dict(kind='crs_other_complement_v1',source_id='synthetic-catalog',orientation='H:A',
                scope='regulation_90',listed_scores=['0:0','1:0','0:1','2:1'],
                other_codes=['H_OTHER','D_OTHER','A_OTHER'])


def test_crs_exact_and_complement_exclusion():
    leg,result=event('CRS','2:1')
    assert settle_leg(leg,result).outcome=='hit'
    assert settle_leg(leg,replace(result,scores=(3,1))).outcome=='miss'
    other=replace(leg,selection='H_OTHER',market_contract=complement())
    assert settle_leg(other,result).outcome=='miss'
    assert settle_leg(other,replace(result,scores=(3,1))).outcome=='hit'
    assert settle_leg(replace(other,market_contract=None),result).outcome=='pending'


@pytest.mark.parametrize('listed',[[['2:1']], [{'score':'2:1'}], ['2:1','2:1'], [None], [1], [], '2:1'])
def test_malformed_complement_is_pending_not_unhandled_exception(listed):
    contract=complement();contract['listed_scores']=listed
    leg,result=event('CRS','H_OTHER',contract=contract)
    assert settle_leg(leg,result).outcome=='pending'


@pytest.mark.parametrize('listed',[[['2:1']], [{'score':'2:1'}], [None]])
def test_malformed_complement_validator_explicitly_rejects(listed):
    contract=complement();contract['listed_scores']=listed
    with pytest.raises(ValueError):
        validate_football_selection('CRS','H_OTHER',contract)


@pytest.mark.parametrize('line,score,expected',[('-1',(1,1),'hit'),('-1',(2,1),'hit'),('-1',(3,1),'miss'),
                                               ('1',(1,1),'hit'),('1',(1,2),'hit'),('1',(1,3),'miss')])
def test_dd_two_independent_components_not_unbeaten(line,score,expected):
    plain,result=event('HAD','D',score)
    handicap=replace(plain,market='HHAD',line=line)
    assert union_outcome([settle_leg(plain,result).outcome,settle_leg(handicap,result).outcome])==expected


def test_dd_ttg_publication_full_cartesian_separate_prices(tmp_path):
    root=Path(__file__).resolve().parents[2]
    assert tmp_path.resolve().is_relative_to(root), 'Use project-owned --basetemp'
    store=SQLiteStore(tmp_path/'synthetic',app_root=root)
    pub=PublicationService(store,trusted_gate=lambda body:True)
    body=research_snapshot()
    left,right=body['options']
    left.update(market='HAD',selection='D',original_odds='4')
    left_alt=deepcopy(left);left_alt.update(option_id='o1handicap',assessment_id='a1handicap',market='HHAD',line='-1',original_odds='5')
    right.update(market='TTG',selection='2',original_odds='4')
    right_alt=deepcopy(right);right_alt.update(option_id='o2three',assessment_id='a2three',selection='3',original_odds='6')
    body.update(options=[left,left_alt,right,right_alt],total_investment='4')
    admin=Principal('independent-admin','admin','csrf');reviewer=Principal('independent-reviewer','reviewer','r')
    pub.save_draft(admin,'csrf','draft',body)
    pub.review(reviewer,'r','draft','review')
    pub.confirm(admin,'csrf',draft_id='draft',review_id='review',publication_id='publication',request_id='request',human_confirmed=True)
    results={o['leg_id']:ResultSnapshot(Identity(**o['identity']),'finished','H:A',(2,1),
                                     'regulation_90',source_id='synthetic') for o in (left,right)}
    summary=PublishedSettlementService(store).record(admin,'csrf',publication_id='publication',results=results)
    assert len(summary['branches'])==4 and summary['outcome']=='hit'
    assert sum(row['outcome']=='hit' for row in summary['branches'])==1
    assert {tuple(row['original_odds']) for row in summary['branches']}=={('4','4'),('4','6'),('5','4'),('5','6')}
    assert all(row['total_investment']=='4' and row['equal_stake']=='1' for row in summary['branches'])
    assert summary['profitability'] is None and summary['actual_return'] is None
