"""Transactional publication primitive. No routes, AI, or background work.

The trusted gate must revalidate the server-loaded snapshot at review AND
confirmation time. No default gate exists; this primitive alone is not a
business-policy validator and must not be exposed by an HTTP route yet.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json

from .auth import require


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


class PublicationService:
    def __init__(self, store, *, trusted_gate=None):
        self.store = store
        self._gate = trusted_gate

    def _validate(self, snapshot):
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be an object")
        for key in ("slot_key", "selection_hash", "manifest_hash"):
            if not isinstance(snapshot.get(key), str) or not snapshot[key].strip():
                raise ValueError("missing snapshot field: " + key)
        from .publication_ticket import validate_snapshot
        validate_snapshot(snapshot)
        if self._gate is None:
            raise PermissionError("trusted policy gate not connected")
        # Pass a detached copy: a gate cannot mutate the reviewed original.
        if self._gate(json.loads(canonical(snapshot))) is not True:
            raise PermissionError("policy revalidation failed")

    def save_draft(self, principal, csrf, draft_id, snapshot):
        require(principal, {"admin"}, csrf)
        if not isinstance(draft_id, str) or not draft_id.strip():
            raise ValueError("empty draft id")
        from .publication_ticket import prepare_snapshot
        snapshot = prepare_snapshot(snapshot)
        serialized = canonical(snapshot)
        digest = fingerprint(snapshot)
        with closing(self.store.connect()) as db, db:
            db.execute("INSERT INTO publication_drafts VALUES(?,?,?) "
                       "ON CONFLICT(draft_id) DO UPDATE SET "
                       "snapshot_json=excluded.snapshot_json,fingerprint=excluded.fingerprint",
                       (draft_id, serialized, digest))
        return digest

    @staticmethod
    def _draft(db, draft_id):
        row = db.execute("SELECT * FROM publication_drafts WHERE draft_id=?",
                         (draft_id,)).fetchone()
        if row is None:
            raise ValueError("draft not found")
        snapshot = json.loads(row["snapshot_json"])
        if fingerprint(snapshot) != row["fingerprint"]:
            raise ValueError("corrupt draft fingerprint")
        return snapshot, row["fingerprint"]

    def review(self, principal, csrf, draft_id, review_id):
        require(principal, {"admin", "reviewer"}, csrf)
        if not isinstance(review_id, str) or not review_id.strip():
            raise ValueError("empty review id")
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            snapshot, digest = self._draft(db, draft_id)
            self._validate(snapshot)
            db.execute("INSERT INTO publication_reviews VALUES(?,?,?,?,?)",
                       (review_id, draft_id, digest, principal.actor_id, now()))
        return digest

    def confirm(self, principal, csrf, *, draft_id, review_id,
                publication_id, request_id, human_confirmed=False, replace_id=None):
        require(principal, {"admin"}, csrf)
        if human_confirmed is not True:
            raise PermissionError("explicit admin confirmation required")
        if any(not isinstance(v, str) or not v.strip()
               for v in (draft_id, review_id, publication_id, request_id)):
            raise ValueError("empty command id")
        scoped_request_id = fingerprint(["publication.confirm", principal.actor_id, request_id])
        command_hash = fingerprint([principal.actor_id, draft_id, review_id,
                                    publication_id, replace_id])
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM publication_requests WHERE request_id=?",
                                  (scoped_request_id,)).fetchone()
            if previous:
                if previous["command_hash"] != command_hash:
                    raise ValueError("idempotency key reused for another command")
                return previous["publication_id"]
            snapshot, digest = self._draft(db, draft_id)
            review = db.execute("SELECT * FROM publication_reviews WHERE review_id=?",
                                (review_id,)).fetchone()
            if (review is None or review["draft_id"] != draft_id
                    or review["fingerprint"] != digest):
                raise PermissionError("review does not bind current snapshot")
            self._validate(snapshot)
            active = db.execute("SELECT publication_id FROM publications WHERE slot_key=? "
                                "AND status IN ('published','frozen')",
                                (snapshot["slot_key"],)).fetchone()
            if replace_id is None:
                if active:
                    raise ValueError("active slot occupied")
            elif active is None or active["publication_id"] != replace_id:
                raise ValueError("replacement target is not current in this slot")
            if replace_id is not None:
                db.execute("UPDATE publications SET status='superseded' WHERE publication_id=?",
                           (replace_id,))
            timestamp = now()
            db.execute("INSERT INTO publications VALUES(?,?,?,?,?,?,?)",
                       (publication_id, snapshot["slot_key"], snapshot["selection_hash"],
                        snapshot["manifest_hash"], "published", canonical(snapshot), timestamp))
            db.execute("INSERT INTO publication_requests VALUES(?,?,?)",
                       (scoped_request_id, command_hash, publication_id))
            db.execute("INSERT INTO audit_events(actor_id,role,event,payload,created_at) "
                       "VALUES(?,?,?,?,?)", (principal.actor_id, principal.role,
                       "publication_confirmed", canonical({"publication_id": publication_id,
                       "review_id": review_id, "fingerprint": digest,
                       "replaces": replace_id}), timestamp))
        return publication_id
