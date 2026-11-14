"""Independent offline settlement contract; canonical public outcomes only."""
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
import sqlite3
import pytest
from sports_app.auth import Principal
from sports_app.storage import SQLiteStore
from sports_app.settlement import (Identity, TicketLeg, ResultSnapshot, settle_leg,
                                   pair_outcome, statistics, SettlementRepository)


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect', deny)
    monkeypatch.setattr('socket.create_connection', deny)
    monkeypatch.setattr('subprocess.Popen', deny)


def sample(sport='football', ident='m1'):
    identity = Identity(sport, 'synthetic-league', '2026-11-14', ident,
                        '2026-11-14T18:00:00+08:00', 'home', 'away')
    scope = 'regulation_90' if sport == 'football' else 'including_ot'
    leg = TicketLeg(ident, identity, 'HAD' if sport == 'football' else 'MNL', 'H', None, scope)
    result = ResultSnapshot(identity, 'finished', 'H:A',
                            (2, 1) if sport == 'football' else (114, 101), scope,
                            () if sport == 'football' else ((28,25),(28,25),(28,25),(30,26)),
                            'synthetic-source')
    return leg, result


@pytest.mark.parametrize('sport', ['football', 'basketball'])
def test_direction_and_canonical_outcome(sport):
    leg, result = sample(sport)
    assert settle_leg(leg, result).outcome == 'hit'
    reverse = replace(result, orientation='A:H', scores=result.scores[::-1],
                      periods=tuple(p[::-1] for p in result.periods))
    assert settle_leg(leg, reverse).outcome == 'hit'
    assert settle_leg(leg, replace(result, orientation='unspecified')).outcome == 'pending'


def test_away_home_basketball_home_handicap_not_reversed():
    leg, result = sample('basketball')
    away = replace(leg, market='HDC', selection='A', line='-3.5')
    result = replace(result, orientation='A:H', scores=(101,114),
                     periods=tuple(p[::-1] for p in result.periods))
    assert settle_leg(away, result).outcome == 'miss'


@pytest.mark.parametrize('field,value', [('sport','basketball'), ('league','other'),
    ('business_day','2026-11-15'), ('match_id','other'), ('kickoff','changed'),
    ('home_id','other'), ('away_id','other')])
def test_every_identity_component_must_match(field, value):
    leg, result = sample()
    assert settle_leg(leg, replace(result, identity=replace(result.identity, **{field:value}))).outcome == 'pending'


def test_multiple_ot_requires_tie_before_each_ot():
    leg, result = sample('basketball')
    periods = ((20,20),)*4 + ((8,8),(10,9))
    valid = replace(result, scores=(98,97), periods=periods)
    assert settle_leg(leg, valid).outcome == 'hit'
    # Same final sum, but first OT ended non-tied: second OT is invalid.
    bad = replace(valid, periods=((20,20),)*4 + ((9,8),(9,9)))
    assert settle_leg(leg, bad).outcome == 'pending'


@pytest.mark.parametrize('change', [dict(periods=()), dict(scores=(115,101)),
    dict(scores=(80,80), periods=((20,20),)*4), dict(status='cancelled'),
    dict(conflict=True), dict(source_id=''), dict(scope='regulation')])
def test_basketball_unknown_or_inconsistent_stays_pending(change):
    leg, result = sample('basketball')
    assert settle_leg(leg, replace(result, **change)).outcome == 'pending'


@pytest.mark.parametrize('market,line', [('HDC','-13'), ('HILO','215')])
def test_integer_equality_needs_explicit_rule(market,line):
    leg, result = sample('basketball')
    assert settle_leg(replace(leg, market=market, line=line,
                              selection='H' if market == 'HDC' else 'O'), result).outcome == 'pending'


def test_hhad_draw_is_hit_not_refund():
    leg, result = sample()
    assert settle_leg(replace(leg, market='HHAD', selection='D', line='-1'), result).outcome == 'hit'


def test_failure_plus_pending_preserves_leg_and_denominator():
    first, r1 = sample()
    second, r2 = sample(ident='m2')
    failed = settle_leg(first, replace(r1, scores=(0,1)))
    pending = settle_leg(second, replace(r2, status='scheduled', scores=None))
    assert failed.outcome == 'miss'
    assert pair_outcome([failed, pending]) == 'miss'
    assert pending.outcome == 'pending'
    stats = statistics([dict(ticket_id='t', outcome='miss',
        recommendation_status='conditional_low_confidence', legs=[asdict(failed), asdict(pending)])])
    assert stats['settled_denominator'] == stats['leg_settled_denominator'] == 1
    assert stats['leg_pending'] == 1


def test_statistics_accepts_canonical_contract_without_dropping_history():
    rows = [dict(ticket_id='t1',outcome='miss',recommendation_status='conditional_low_confidence'),
            dict(ticket_id='t2',outcome='pending'),dict(ticket_id='t3',outcome='hit',status='superseded')]
    stats = statistics(rows)
    assert stats['actual_recommendations'] == 3 and stats['settled_denominator'] == 2
    assert stats['actual_stake'] is None and stats['actual_return'] is None
    with pytest.raises(ValueError):
        statistics(rows + [rows[0]])


@pytest.fixture
def repository(tmp_path):
    root = Path(__file__).resolve().parents[2]
    assert tmp_path.resolve().is_relative_to(root), 'Use project-owned --basetemp'
    return SettlementRepository(SQLiteStore(tmp_path / 'synthetic', app_root=root))


def command():
    l1,r1 = sample()
    l2,r2 = sample(ident='m2')
    return dict(ticket_id='synthetic-ticket',original={'recommendation_status':'conditional_low_confidence'},
                legs=[l1,l2],results=[r1,r2])


ADMIN = Principal('independent-admin','admin','csrf')


def test_identical_replay_is_idempotent(repository):
    args = command()
    first = repository.record(ADMIN,'csrf',**args)
    second = repository.record(ADMIN,'csrf',**args)
    assert first == second
    with closing(repository.store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM settlement_versions').fetchone()[0] == 1


def test_identical_correction_retry_does_not_add_version(repository):
    args = command()
    repository.record(ADMIN,'csrf',**args)
    args['results'][0] = replace(args['results'][0], scores=(0,1))
    first = repository.record(ADMIN,'csrf',correction_reason='source correction',**args)
    second = repository.record(ADMIN,'csrf',correction_reason='source correction',**args)
    assert first == second and first['version'] == 2
    with closing(repository.store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM settlement_versions').fetchone()[0] == 2


def test_correction_preserves_original_and_latest_counts_once(repository):
    args = command()
    repository.record(ADMIN,'csrf',**args)
    args['results'][0] = replace(args['results'][0], scores=(0,1))
    repository.record(ADMIN,'csrf',correction_reason='source correction',**args)
    with closing(repository.store.connect()) as db:
        rows=list(db.execute('SELECT * FROM settlement_versions ORDER BY version'))
        assert len(rows) == 2 and rows[0]['original_json'] == rows[1]['original_json']
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM settlement_versions')
    assert len(repository.latest()) == 1
    assert statistics(repository.latest())['settled_denominator'] == 1
    args['original'] = {'changed':'original'}
    with pytest.raises(ValueError):
        repository.record(ADMIN,'csrf',correction_reason='not permitted',**args)
