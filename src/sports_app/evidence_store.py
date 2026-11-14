"""Authenticated, append-only evidence records. No HTTP ingestion routes.

Claims are unverified on ingestion. A reviewer records verification separately;
source truth still depends on that human workflow, not an automated URL claim.
"""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json

from .auth import require
from .evidence import Claim, Receipt, EvidenceContext, WEIGHTS, aware, safe_url, text
from .publication import canonical, fingerprint
from .core import score_assessment, validate_option, Option

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_records (
 record_id TEXT PRIMARY KEY, kind TEXT NOT NULL, option_id TEXT NOT NULL,
 body TEXT NOT NULL, actor_id TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON evidence_records
 BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON evidence_records
 BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
"""


class EvidenceRepository:
    def __init__(self, store, *, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with closing(store.connect()) as db, db:
            db.executescript(SCHEMA)

    def _append(self, db, ident, kind, option_id, body, actor_id):
        stamp = self.clock()
        if not aware(stamp):
            raise ValueError('server clock must be timezone aware')
        db.execute('INSERT INTO evidence_records VALUES(?,?,?,?,?,?)',
                   (ident, kind, option_id, canonical(body), actor_id, stamp.isoformat()))

    def ingest(self, principal, csrf, claim):
        require(principal, {'admin'}, csrf)
        if not isinstance(claim, Claim) or not text(claim.claim_id) or not text(claim.option_id):
            raise ValueError('invalid claim identity')
        if claim.source_verified is not False or claim.conflict is not False:
            raise ValueError('ingestion cannot assert verification or conflict status')
        if (not safe_url(claim.url) or claim.level not in {'L1','L2'}
                or not aware(claim.published_at) or not aware(claim.effective_at)
                or not text(claim.group_id) or not text(claim.quote)):
            raise ValueError('invalid claim metadata')
        body = asdict(claim)
        body['published_at'] = claim.published_at.isoformat()
        body['effective_at'] = claim.effective_at.isoformat()
        with closing(self.store.connect()) as db, db:
            self._append(db, claim.claim_id, 'claim', claim.option_id, body, principal.actor_id)
        return fingerprint(body)

    def verify(self, principal, csrf, *, receipt_id, claim_id, quote_hash, conclusion):
        require(principal, {'reviewer'}, csrf)
        if conclusion not in {'verified', 'conflict'} or not text(receipt_id):
            raise ValueError('invalid verification conclusion')
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM evidence_records WHERE record_id=? AND kind='claim'",
                             (claim_id,)).fetchone()
            if row is None or row['actor_id'] == principal.actor_id:
                raise PermissionError('missing claim or self verification')
            body = json.loads(row['body'])
            if sha256(body['quote'].encode()).hexdigest() != quote_hash:
                raise ValueError('quote changed')
            verification = {'claim_id': claim_id, 'quote_hash': quote_hash,
                            'conclusion': conclusion, 'claim_fingerprint': fingerprint(body)}
            self._append(db, receipt_id, 'receipt', row['option_id'], verification, principal.actor_id)

    def freeze_recommendation(self, principal, csrf, *, freeze_id, snapshot):
        """Internal admin-only freeze; never trust request labels in the gate."""
        require(principal, {'admin'}, csrf)
        if not text(freeze_id) or 'recommendation_freeze_id' in snapshot:
            raise ValueError('invalid freeze input')
        body = {'snapshot_hash': fingerprint(snapshot),
                'recommendation_type': snapshot.get('recommendation_type'),
                'recommendation_status': snapshot.get('recommendation_status')}
        with closing(self.store.connect()) as db, db:
            self._append(db, freeze_id, 'recommendation', '', body, principal.actor_id)
        return dict(snapshot, recommendation_freeze_id=freeze_id)

    def conditional_scope(self, snapshot):
        ident = snapshot.get('recommendation_freeze_id')
        if not ident:
            return False
        with closing(self.store.connect()) as db:
            row = db.execute("SELECT body FROM evidence_records WHERE record_id=? AND kind='recommendation'",
                             (ident,)).fetchone()
        if row is None:
            raise ValueError('unknown recommendation freeze')
        frozen = json.loads(row['body'])
        original = {key: value for key, value in snapshot.items() if key != 'recommendation_freeze_id'}
        if frozen['snapshot_hash'] != fingerprint(original):
            raise ValueError('recommendation differs from server freeze')
        return (frozen['recommendation_type'] == 'regular_pair'
                and frozen['recommendation_status'] == 'conditional_low_confidence')

    def load_context(self, option_id, *, as_of):
        if not aware(as_of):
            raise ValueError('as_of required')
        with closing(self.store.connect()) as db:
            rows = list(db.execute('SELECT * FROM evidence_records WHERE option_id=? ORDER BY record_id',
                                   (option_id,)))
        visible = [r for r in rows if datetime.fromisoformat(r['recorded_at']) <= as_of]
        claims, receipts = [], []
        for row in visible:
            if row['kind'] != 'claim':
                continue
            body = json.loads(row['body'])
            checks = []
            for candidate in visible:
                if candidate['kind'] != 'receipt':
                    continue
                check = json.loads(candidate['body'])
                if (check['claim_id'] == row['record_id']
                        and check['claim_fingerprint'] == fingerprint(body)
                        and check['quote_hash'] == sha256(body['quote'].encode()).hexdigest()):
                    checks.append(check)
                    receipts.append(Receipt(row['record_id'], option_id, check['quote_hash'],
                                            check['conclusion'], candidate['actor_id'],
                                            datetime.fromisoformat(candidate['recorded_at'])))
            body['source_verified'] = any(c['conclusion'] == 'verified' for c in checks)
            body['conflict'] = any(c['conclusion'] == 'conflict' for c in checks)
            body['published_at'] = datetime.fromisoformat(body['published_at'])
            body['effective_at'] = datetime.fromisoformat(body['effective_at'])
            body['dimension_ids'] = tuple(body['dimension_ids'])
            claims.append(Claim(**body))
        return EvidenceContext(as_of, tuple(claims), tuple(receipts))


class RepositoryReviewGate:
    """Recompute evidence from repository, never accept snapshot Claim/Receipt.

    Additional business gates are mandatory (identity, branches, 039, manifest,
    candidate ranking, etc.). This is NOT a default permissive publish gate.
    """
    def __init__(self, repository, *, additional_gate=None):
        self.repository = repository
        self.additional_gate = additional_gate

    def __call__(self, snapshot):
        if self.additional_gate is None or self.additional_gate(snapshot) is not True:
            return False
        options = snapshot.get('options')
        if not isinstance(options, list) or not options:
            return False
        try:
            conditional = self.repository.conditional_scope(snapshot)
        except (ValueError, TypeError):
            return False
        if conditional:
            # Legacy two-option controls remain valid; complete composed tickets
            # identify two legs explicitly and may contain >2 independently scored options.
            leg_ids = {item.get('leg_id') for item in options}
            if (len({item.get('option_id') for item in options}) != len(options)
                    or (leg_ids == {None} and len(options) != 2)
                    or (leg_ids != {None} and (None in leg_ids or len(leg_ids) != 2))):
                return False
        as_of = self.repository.clock()  # actual server decision time, not client backdating
        for item in options:
            if any(key in item for key in ('claims', 'receipts', 'evidence_context', 'eligible', 'evidence_score')):
                return False
            try:
                option_id = item['option_id']
                context = self.repository.load_context(option_id, as_of=as_of)
                # Do not let a caller hide a known conflict by omitting its ID.
                if any(claim.conflict and set(claim.dimension_ids) & set(WEIGHTS)
                       for claim in context.claims):
                    return False
                assessment = score_assessment(
                    option_id, tuple(tuple(v) for v in item['dimensions']),
                    reasons=tuple(item['reasons']), failure_paths=tuple(item['failure_paths']),
                    withdraw_if=tuple(item['withdraw_if']), claim_ids=tuple(item['claim_ids']),
                    assessment_id=item['assessment_id'],
                    evidence_context=context)
                if validate_option(Option(option_id, assessment), conditional=conditional):
                    return False
            except (KeyError, ValueError, TypeError):
                return False
        return True
