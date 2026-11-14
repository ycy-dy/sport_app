"""Offline deterministic settlement. Unknown/cancelled rules remain pending.

Scores are integers, HDC uses the frozen HOME handicap. No network or actual
money. Supports HAD/HHAD/TTG/HAFU/CRS and MNL/HDC/HILO; missing half-time
provenance or CRS complement contracts fail pending, never inferred.
"""
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
from contextlib import closing
from datetime import datetime, timezone
import json
from .auth import require
from .publication import canonical, fingerprint


@dataclass(frozen=True)
class Identity:
    sport: str
    league: str
    business_day: str
    match_id: str
    kickoff: str
    home_id: str
    away_id: str


@dataclass(frozen=True)
class TicketLeg:
    leg_id: str
    identity: Identity
    market: str
    selection: str
    line: str | None
    scope: str  # football regulation_90; basketball including_ot
    market_contract: dict | None = None


@dataclass(frozen=True)
class ResultSnapshot:
    identity: Identity
    status: str
    orientation: str  # explicit H:A or A:H; never infer from sport
    scores: tuple[int, int] | None
    scope: str
    periods: tuple[tuple[int, int], ...] = ()  # first 4 quarters then each OT
    source_id: str = ''
    conflict: bool = False
    half_scores: tuple[int, int] | None = None
    half_identity: Identity | None = None
    half_orientation: str | None = None
    half_scope: str | None = None
    half_source_id: str | None = None
    half_conflict: bool = False


@dataclass(frozen=True)
class LegSettlement:
    leg_id: str
    outcome: str  # hit/miss/pending
    reason: str
    result_hash: str


def settle_leg(leg, result):
    digest = fingerprint(asdict(result))
    def decision(outcome, reason):
        return LegSettlement(leg.leg_id, outcome, reason, digest)
    if result.identity != leg.identity:
        return decision('pending', 'identity mismatch')
    identity = leg.identity
    if (identity.sport not in {'football', 'basketball'}
            or any(not isinstance(value, str) or not value.strip() for value in asdict(identity).values())
            or identity.home_id == identity.away_id):
        return decision('pending', 'invalid frozen identity')
    if result.conflict or not isinstance(result.source_id, str) or not result.source_id.strip():
        return decision('pending', 'source conflict or missing provenance')
    if result.status != 'finished':
        return decision('pending', 'unfinished or cancelled; settlement rule unconfirmed')
    if result.orientation not in {'H:A', 'A:H'} or result.scope != leg.scope:
        return decision('pending', 'orientation or scope mismatch')
    def valid_pair(pair):
        return isinstance(pair, (tuple, list)) and len(pair) == 2 and all(type(v) is int and v >= 0 for v in pair)
    if not valid_pair(result.scores):
        return decision('pending', 'invalid final score')
    home, away = result.scores if result.orientation == 'H:A' else result.scores[::-1]
    if identity.sport == 'basketball':
        if leg.scope != 'including_ot' or len(result.periods) < 4 or any(not valid_pair(p) for p in result.periods):
            return decision('pending', 'missing quarters or unsupported basketball scope')
        if tuple(sum(p[i] for p in result.periods) for i in (0, 1)) != tuple(result.scores):
            return decision('pending', 'quarter/OT sum mismatch')
        # Every played OT requires a tied cumulative score before it.
        for stop in range(4, len(result.periods)):
            if sum(p[0] for p in result.periods[:stop]) != sum(p[1] for p in result.periods[:stop]):
                return decision('pending', 'OT without preceding tie')
        if home == away:
            return decision('pending', 'finished basketball tied')
    elif leg.scope != 'regulation_90':
        return decision('pending', 'unsupported football scope')
    try:
        if leg.market == 'HAD' and identity.sport == 'football':
            target = 'H' if home > away else 'A' if home < away else 'D'
            choices = {'H', 'D', 'A'}
        elif leg.market == 'HHAD' and identity.sport == 'football':
            line = Decimal(leg.line)
            if not line.is_finite() or line != line.to_integral_value():
                return decision('pending', 'HHAD needs frozen integer home handicap')
            difference = Decimal(home-away)+line
            target = 'H' if difference > 0 else 'A' if difference < 0 else 'D'
            choices = {'H', 'D', 'A'}  # equality is handicap draw, never cancellation
        elif leg.market in {'TTG','HAFU','CRS'} and identity.sport == 'football':
            from .football_markets import validate_football_selection, exact_score
            validate_football_selection(leg.market,leg.selection,leg.market_contract)
            if leg.market == 'TTG':
                target = '7+' if home+away >= 7 else str(home+away)
            elif leg.market == 'HAFU':
                if (result.half_identity != identity or result.half_orientation != result.orientation
                        or result.half_scope != 'regulation_90' or result.half_conflict
                        or not isinstance(result.half_source_id,str) or not result.half_source_id.strip()
                        or not valid_pair(result.half_scores)):
                    return decision('pending','missing/conflicting half-time identity, source, direction or scope')
                ht_home,ht_away = result.half_scores if result.orientation == 'H:A' else result.half_scores[::-1]
                if ht_home > home or ht_away > away:
                    return decision('pending','half-time exceeds regulation final')
                direction = lambda h,a: 'H' if h>a else 'A' if h<a else 'D'
                target = direction(ht_home,ht_away)+direction(home,away)
            elif leg.selection in {'H_OTHER','D_OTHER','A_OTHER'}:
                listed = {exact_score(value) for value in leg.market_contract['listed_scores']}
                if (home,away) in listed:
                    return decision('miss','score belongs to listed exact category, not other complement')
                target = ('H' if home>away else 'A' if home<away else 'D')+'_OTHER'
            else:
                target = f'{home}:{away}'
            return decision('hit' if leg.selection == target else 'miss','frozen football event comparison')
        elif leg.market in {'MNL', 'HDC', 'HILO'} and identity.sport == 'basketball':
            if leg.market == 'MNL':
                target, choices = ('H' if home > away else 'A'), {'H', 'A'}
            else:
                line = Decimal(leg.line)
                if not line.is_finite():
                    return decision('pending', 'nonfinite frozen line')
                difference = Decimal(home-away)+line if leg.market == 'HDC' else Decimal(home+away)-line
                if difference == 0:
                    return decision('pending', 'push/equality rule requires explicit market contract')
                target = ('H' if difference > 0 else 'A') if leg.market == 'HDC' else ('O' if difference > 0 else 'U')
                choices = {'H', 'A'} if leg.market == 'HDC' else {'O', 'U'}
        else:
            return decision('pending', 'unsupported market')
    except (InvalidOperation, TypeError, ValueError):
        return decision('pending', 'invalid frozen line')
    if leg.selection not in choices:
        return decision('pending', 'invalid frozen selection')
    return decision('hit' if leg.selection == target else 'miss', 'deterministic score comparison')


def pair_outcome(legs):
    if len(legs) != 2 or len({leg.leg_id for leg in legs}) != 2:
        raise ValueError('pair requires two distinct legs')
    if any(leg.outcome not in {'hit', 'miss', 'pending'} for leg in legs):
        raise ValueError('invalid outcome')
    if any(leg.outcome == 'miss' for leg in legs):
        return 'miss'
    return 'hit' if all(leg.outcome == 'hit' for leg in legs) else 'pending'


def statistics(latest_records):
    """One latest record per actual ticket; conditional labels never excluded.

    Branch hit is NOT profitability. No monetary or probability metrics inferred.
    """
    ids = [row['ticket_id'] for row in latest_records]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate ticket versions; select latest explicitly')
    settled = [row for row in latest_records if row['outcome'] in {'hit', 'miss'}]
    if any(row['outcome'] not in {'hit','miss','pending'} for row in latest_records):
        raise ValueError('invalid outcome')
    leg_records = [leg for row in latest_records for leg in row.get('legs', [])]
    if any(leg.get('outcome') not in {'hit','miss','pending'} for leg in leg_records):
        raise ValueError('invalid leg outcome')
    return {'leg_settled_denominator': sum(leg['outcome'] in {'hit','miss'} for leg in leg_records),
            'leg_pending': sum(leg['outcome'] == 'pending' for leg in leg_records),
            'actual_recommendations': len(latest_records), 'settled_denominator': len(settled),
            'hit': sum(row['outcome'] == 'hit' for row in settled),
            'miss': sum(row['outcome'] == 'miss' for row in settled),
            'pending': len(latest_records)-len(settled), 'actual_stake': None, 'actual_return': None}


SCHEMA = """
CREATE TABLE IF NOT EXISTS settlement_versions (
 ticket_id TEXT NOT NULL, version INTEGER NOT NULL, original_hash TEXT NOT NULL,
 original_json TEXT NOT NULL, result_json TEXT NOT NULL, settlement_json TEXT NOT NULL,
 correction_reason TEXT NOT NULL, actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(ticket_id,version)
);
CREATE TRIGGER IF NOT EXISTS settlement_no_update BEFORE UPDATE ON settlement_versions
 BEGIN SELECT RAISE(ABORT,'immutable settlement version'); END;
CREATE TRIGGER IF NOT EXISTS settlement_no_delete BEFORE DELETE ON settlement_versions
 BEGIN SELECT RAISE(ABORT,'immutable settlement version'); END;
"""


class SettlementRepository:
    """Offline service: caller supplies frozen ticket; cannot mutate previous original."""
    def __init__(self, store):
        self.store = store
        with closing(store.connect()) as db, db:
            db.executescript(SCHEMA)

    def record(self, principal, csrf, *, ticket_id, original, legs, results, correction_reason=''):
        require(principal, {'admin'}, csrf)
        if len(legs) != 2 or len(results) != 2 or len({leg.identity.match_id for leg in legs}) != 2:
            raise ValueError('requires two distinct frozen matches and results')
        binding = {'ticket': original, 'legs': [asdict(leg) for leg in legs]}
        original_hash = fingerprint(binding)
        settlements = [settle_leg(leg, result) for leg, result in zip(legs, results)]
        summary = {'ticket_id': ticket_id, 'outcome': pair_outcome(settlements),
                   'legs': [asdict(s) for s in settlements],
                   'recommendation_status': original.get('recommendation_status'),
                   'actual_stake': None, 'actual_return': None}
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM settlement_versions WHERE ticket_id=? ORDER BY version DESC LIMIT 1',
                                  (ticket_id,)).fetchone()
            if previous:
                if previous['original_hash'] != original_hash:
                    raise ValueError('correction must preserve original')
                if previous['result_json'] == canonical([asdict(r) for r in results]):
                    # Same-result replay is a read of the latest committed version,
                    # not a new correction, even when a retry omits its old reason.
                    return dict(json.loads(previous['settlement_json']), version=previous['version'])
                if not isinstance(correction_reason, str) or not correction_reason.strip():
                    raise ValueError('changed result requires correction reason')
            version = previous['version']+1 if previous else 1
            db.execute('INSERT INTO settlement_versions VALUES(?,?,?,?,?,?,?,?,?)',
                       (ticket_id, version, original_hash, canonical(binding), canonical([asdict(r) for r in results]),
                        canonical(summary), correction_reason, principal.actor_id, datetime.now(timezone.utc).isoformat()))
        return dict(summary, version=version)

    def latest(self):
        with closing(self.store.connect()) as db:
            rows = db.execute('SELECT s.settlement_json,s.version FROM settlement_versions s '
                              'WHERE s.version=(SELECT MAX(v.version) FROM settlement_versions v WHERE v.ticket_id=s.ticket_id)')
            return [dict(json.loads(row['settlement_json']), version=row['version']) for row in rows]
