from dataclasses import replace
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import pytest
from sports_app.auth import Principal
from sports_app.storage import SQLiteStore
from sports_app.settlement import (Identity, TicketLeg, ResultSnapshot, settle_leg,
                                   pair_outcome, statistics, SettlementRepository)


def fixtures(sport='football', ident='match1'):
    identity = Identity(sport, 'synthetic-league', '2026-11-14', ident,
                        '2026-11-14T18:00:00+08:00', 'home', 'away')
    scope = 'regulation_90' if sport == 'football' else 'including_ot'
    leg = TicketLeg(ident, identity, 'HAD' if sport == 'football' else 'MNL', 'H', None, scope)
    result = ResultSnapshot(identity, 'finished', 'H:A', (2,1) if sport == 'football' else (81,80),
                            scope, () if sport == 'football' else ((20,20),(20,20),(20,20),(21,20)),
                            'synthetic-authority')
    return leg, result


@pytest.mark.parametrize('sport',['football','basketball'])
def test_explicit_score_orientation(sport):
    leg, result = fixtures(sport)
    assert settle_leg(leg,result).outcome == 'hit'
    reverse = replace(result,orientation='A:H',scores=result.scores[::-1],
                      periods=tuple(p[::-1] for p in result.periods))
    assert settle_leg(leg,reverse).outcome == 'hit'
    assert settle_leg(leg,replace(result,orientation='unknown')).outcome == 'pending'


@pytest.mark.parametrize('field,value',[('match_id','different'),('business_day','2026-11-15'),
                                       ('home_id','other'),('kickoff','changed'),('league','other')])
def test_identity_mismatch_pending(field,value):
    leg,result=fixtures()
    assert settle_leg(leg,replace(result,identity=replace(result.identity,**{field:value}))).outcome == 'pending'


@pytest.mark.parametrize('change',[{'status':'cancelled'},{'status':'scheduled'}, {'scores':None},
                                   {'scores':(True,1)},{'scores':(-1,0)}, {'conflict':True},
                                   {'source_id':''},{'scope':'including_ot'}])
def test_unresolved_football_result_pending(change):
    leg,result=fixtures()
    assert settle_leg(leg,replace(result,**change)).outcome == 'pending'


def test_hhad_equality_is_draw_not_push():
    leg,result=fixtures()
    leg=replace(leg,market='HHAD',selection='D',line='-1')
    assert settle_leg(leg,result).outcome == 'hit'
    assert settle_leg(replace(leg,selection='H'),result).outcome == 'miss'


def test_basketball_home_handicap_and_total():
    leg,result=fixtures('basketball')
    assert settle_leg(replace(leg,market='HDC',line='-1.5',selection='A'),result).outcome == 'hit'
    assert settle_leg(replace(leg,market='HILO',line='160.5',selection='O'),result).outcome == 'hit'
    assert settle_leg(replace(leg,market='HDC',line='-1',selection='H'),result).outcome == 'pending'


def test_basketball_ot_verified_by_quarters():
    leg,result=fixtures('basketball')
    valid=replace(result,scores=(90,88),periods=((20,20),)*4+((10,8),))
    assert settle_leg(leg,valid).outcome == 'hit'
    assert settle_leg(leg,replace(valid,periods=((20,20),)*4)).outcome == 'pending'
    assert settle_leg(leg,replace(valid,periods=((21,20),(20,20),(20,20),(20,20),(9,8)))).outcome == 'pending'
    assert settle_leg(leg,replace(result,scores=(80,80),periods=((20,20),)*4)).outcome == 'pending'
    assert settle_leg(leg,replace(result,periods=())).outcome == 'pending'


def test_losing_leg_and_pending_leg_pair_lost_without_changing_pending():
    first,r1=fixtures()
    second,r2=fixtures(ident='match2')
    lost=settle_leg(first,replace(r1,scores=(0,1)))
    pending=settle_leg(second,replace(r2,status='scheduled',scores=None))
    assert pair_outcome([lost,pending]) == 'miss'
    assert pending.outcome == 'pending'
    from dataclasses import asdict
    stats=statistics([{'ticket_id':'pair','outcome':'miss','legs':[asdict(lost),asdict(pending)]}])
    assert stats['settled_denominator'] == 1
    assert stats['leg_settled_denominator'] == 1 and stats['leg_pending'] == 1


def test_denominator_keeps_conditional_excludes_pending_and_rejects_double_count():
    records=[{'ticket_id':'t1','outcome':'miss','recommendation_status':'conditional_low_confidence'},
             {'ticket_id':'t2','outcome':'pending'}, {'ticket_id':'t3','outcome':'hit'}]
    result=statistics(records)
    assert result['actual_recommendations'] == 3 and result['settled_denominator'] == 2
    assert result['miss'] == 1 and result['pending'] == 1
    assert result['actual_stake'] is None and result['actual_return'] is None
    with pytest.raises(ValueError): statistics(records+[records[0]])


def test_correction_new_version_preserves_original_and_history():
    root=Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix='.settlement-test-',dir=root) as temp:
        store=SQLiteStore(temp,app_root=root)
        repo=SettlementRepository(store)
        admin=Principal('synthetic-admin','admin','csrf')
        leg1,result1=fixtures()
        leg2,result2=fixtures(ident='match2')
        original={'recommendation_status':'conditional_low_confidence','original_odds':'frozen'}
        args=dict(ticket_id='ticket',original=original,legs=[leg1,leg2],results=[result1,result2])
        assert repo.record(admin,'csrf',**args)['outcome'] == 'hit'
        assert repo.record(admin,'csrf',**args)['version'] == 1
        args['results']=[replace(result1,scores=(0,1)),result2]
        with pytest.raises(ValueError): repo.record(admin,'csrf',**args)
        corrected=repo.record(admin,'csrf',correction_reason='authority correction',**args)
        assert corrected['version'] == 2 and corrected['outcome'] == 'miss'
        assert len(repo.latest()) == 1
        assert statistics(repo.latest())['miss'] == 1
        assert repo.record(admin,'csrf',correction_reason='authority correction',**args)['version'] == 2
        assert repo.record(admin,'csrf',**args)['version'] == 2
        args['original']=dict(original,original_odds='changed')
        with pytest.raises(ValueError): repo.record(admin,'csrf',correction_reason='bad change',**args)
        with closing(store.connect()) as db,db:
            rows=list(db.execute('SELECT * FROM settlement_versions ORDER BY version'))
            assert len(rows) == 2 and rows[0]['original_json'] == rows[1]['original_json']
            with pytest.raises(sqlite3.IntegrityError): db.execute('DELETE FROM settlement_versions')
            with pytest.raises(sqlite3.IntegrityError): db.execute("UPDATE settlement_versions SET original_json='{}'")
