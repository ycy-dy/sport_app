"""Settlement bound exclusively to immutable publication originals.

Frozen original schema: settlement_ticket.legs (two legs with identity,scope,
options[{option_id,market,selection,line,original_odds}]), equal_stake,
total_investment, branches[{branch_id,option_ids}]. All branch arithmetic is
revalidated. Caller supplies results only, never original legs/lines/prices.
"""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
from .auth import require
from .core import branch, validate_branches
from .publication import canonical, fingerprint
from .settlement import Identity, TicketLeg, settle_leg, statistics

SCHEMA = """
CREATE TABLE IF NOT EXISTS published_settlement_versions (
 publication_id TEXT NOT NULL, version INTEGER NOT NULL, original_hash TEXT NOT NULL,
 result_json TEXT NOT NULL, summary_json TEXT NOT NULL, correction_reason TEXT NOT NULL,
 actor_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(publication_id,version)
);
CREATE TRIGGER IF NOT EXISTS published_settlement_no_update BEFORE UPDATE ON published_settlement_versions
 BEGIN SELECT RAISE(ABORT,'immutable settlement'); END;
CREATE TRIGGER IF NOT EXISTS published_settlement_no_delete BEFORE DELETE ON published_settlement_versions
 BEGIN SELECT RAISE(ABORT,'immutable settlement'); END;
"""


def union_outcome(outcomes):
    if not outcomes or any(value not in {'hit','miss','pending'} for value in outcomes):
        raise ValueError('invalid union outcomes')
    return 'hit' if 'hit' in outcomes else 'pending' if 'pending' in outcomes else 'miss'


def conjunction(outcomes):
    return 'miss' if 'miss' in outcomes else 'pending' if 'pending' in outcomes else 'hit'


def frozen_ticket(original):
    """Reject malformed/incomplete frozen tickets before any settlement write."""
    ticket = original['settlement_ticket']
    legs = ticket['legs']
    if len(legs) != 2 or len({leg['leg_id'] for leg in legs}) != 2:
        raise ValueError('two distinct legs required')
    identities = [Identity(**leg['identity']) for leg in legs]
    if (identities[0].match_id == identities[1].match_id
            or identities[0].sport != identities[1].sport):
        raise ValueError('distinct matches of the same sport required')
    option_maps, universes = [], []
    for leg in legs:
        options = leg['options']
        values = {value['option_id']: value for value in options}
        if not values or len(values) != len(options):
            raise ValueError('empty or duplicate frozen options')
        option_maps.append(values)
        universes.append(tuple((key, value['original_odds']) for key,value in values.items()))
    audited = []
    for item in ticket['branches']:
        ids = tuple(item['option_ids'])
        if len(ids) != 2 or any(ids[i] not in option_maps[i] for i in (0,1)):
            raise ValueError('branch option outside frozen leg')
        audited.append(branch(item['branch_id'], ids,
                              tuple(option_maps[i][ids[i]]['original_odds'] for i in (0,1)),
                              ticket['equal_stake'], ticket['total_investment']))
    if validate_branches(audited, frozen_leg_options=tuple(universes)):
        raise ValueError('invalid frozen branch universe or cost')
    return ticket, identities, option_maps, audited


class PublishedSettlementService:
    def __init__(self, store):
        self.store = store
        with closing(store.connect()) as db, db:
            db.executescript(SCHEMA)

    def record(self, principal, csrf, *, publication_id, results, correction_reason=''):
        require(principal, {'admin'}, csrf)
        # Canonical order makes equivalent mapping replay deterministic.
        if not isinstance(results, dict):
            raise ValueError('results must be keyed by frozen leg id')
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            publication = db.execute('SELECT * FROM publications WHERE publication_id=?',
                                     (publication_id,)).fetchone()
            if publication is None:
                raise ValueError('publication not found')
            original = json.loads(publication['original_json'])
            ticket, identities, option_maps, branches = frozen_ticket(original)
            leg_ids = {leg['leg_id'] for leg in ticket['legs']}
            if set(results)-leg_ids:
                raise ValueError('result supplied for unknown leg')
            original_hash = fingerprint(original)
            result_json = canonical({key: asdict(value) for key,value in sorted(results.items())})
            previous = db.execute('SELECT * FROM published_settlement_versions WHERE publication_id=? '
                                  'ORDER BY version DESC LIMIT 1', (publication_id,)).fetchone()
            if previous:
                if previous['original_hash'] != original_hash:
                    raise ValueError('published original changed')
                if previous['result_json'] == result_json:
                    return dict(json.loads(previous['summary_json']), version=previous['version'])
                if not isinstance(correction_reason,str) or not correction_reason.strip():
                    raise ValueError('changed result requires correction reason')
            option_results, leg_results = {}, []
            for i, leg in enumerate(ticket['legs']):
                outcomes = []
                for option_id, value in option_maps[i].items():
                    frozen = TicketLeg(option_id, identities[i], value['market'], value['selection'],
                                       value['line'], leg['scope'], value.get('market_contract'))
                    if leg['leg_id'] not in results:
                        outcome = 'pending'
                        detail = {'leg_id':option_id,'outcome':outcome,'reason':'no result', 'result_hash':None}
                    else:
                        detail = asdict(settle_leg(frozen, results[leg['leg_id']]))
                        outcome = detail['outcome']
                    option_results[option_id] = detail
                    outcomes.append(outcome)
                leg_results.append({'leg_id':leg['leg_id'], 'outcome':union_outcome(outcomes)})
            branch_results = [{'branch_id':b.branch_id, 'option_ids':list(b.leg_option_ids),
                               'outcome':conjunction([option_results[key]['outcome'] for key in b.leg_option_ids]),
                               'original_odds':[str(v) for v in b.original_odds_decimal],
                               'equal_stake':str(b.equal_stake_decimal),
                               'total_investment':str(b.total_investment_decimal)} for b in branches]
            summary = {'ticket_id':publication_id, 'outcome':union_outcome([b['outcome'] for b in branch_results]),
                       'recommendation_status':original.get('recommendation_status'),
                       'options':option_results, 'legs':leg_results, 'branches':branch_results,
                       'original_hash':original_hash, 'actual_stake':None, 'actual_return':None,
                       'profitability':None}
            version = previous['version']+1 if previous else 1
            db.execute('INSERT INTO published_settlement_versions VALUES(?,?,?,?,?,?,?,?)',
                       (publication_id, version, original_hash, result_json, canonical(summary),
                        correction_reason, principal.actor_id, datetime.now(timezone.utc).isoformat()))
        return dict(summary,version=version)

    def ledger(self):
        """Publication universe, not current-slot or reviewed-only universe."""
        with closing(self.store.connect()) as db:
            rows = list(db.execute('SELECT p.*,s.summary_json,s.version FROM publications p '
                                   'LEFT JOIN published_settlement_versions s ON s.publication_id=p.publication_id '
                                   'AND s.version=(SELECT MAX(v.version) FROM published_settlement_versions v '
                                   'WHERE v.publication_id=p.publication_id) ORDER BY p.publication_id'))
        answer = []
        for row in rows:
            if row['summary_json'] is not None:
                summary = json.loads(row['summary_json'])
            else:
                original = json.loads(row['original_json'])
                summary = {'ticket_id':row['publication_id'], 'outcome':'pending',
                           'recommendation_status':original.get('recommendation_status'),
                           'legs':[{'leg_id':leg['leg_id'],'outcome':'pending'}
                                   for leg in original.get('settlement_ticket',{}).get('legs',[])],
                           'actual_stake':None,'actual_return':None}
            original = json.loads(row['original_json'])
            try:
                frozen_ticket(original)
                coverage = 'complete'
            except (KeyError, ValueError, TypeError):
                coverage = 'insufficient_frozen_structure'
            answer.append(dict(summary,version=row['version'],publication_status=row['status'],
                               leg_statistics_coverage=coverage))
        return answer

    def statistics(self):
        ledger = self.ledger()
        missing = sum(row['leg_statistics_coverage'] != 'complete' for row in ledger)
        return dict(statistics(ledger), leg_statistics_coverage='incomplete' if missing else 'complete',
                    tickets_missing_leg_structure=missing)
