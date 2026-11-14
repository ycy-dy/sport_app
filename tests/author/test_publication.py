"""Synthetic-only storage primitive tests; not real publication acceptance."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import pytest
from sports_app.auth import Principal, require
from sports_app.storage import SQLiteStore
from sports_app.publication import PublicationService
from ticket_fixture import synthetic_ticket_input

ADMIN = Principal("synthetic-admin", "admin", "test-csrf")
REVIEWER = Principal("synthetic-reviewer", "reviewer", "review-csrf")


@pytest.fixture
def setup():
    # All database writes stay under the new project, never system tmp/foot.
    root = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix=".publication-test-", dir=root) as temp:
        store = SQLiteStore(temp, app_root=root)
        service = PublicationService(store, trusted_gate=lambda snapshot: True)
        yield store, service


def prepare(service, draft="d1", review="r1", selection="s1", slot="synthetic-slot"):
    snapshot = {"slot_key": slot, "selection_hash": selection,
                "manifest_hash": "synthetic-manifest", "model_probability": None,
                "EV": None}
    from sports_app.publication_ticket import prepare_snapshot
    snapshot.update(synthetic_ticket_input())
    snapshot = prepare_snapshot(snapshot)
    service.save_draft(ADMIN, ADMIN.csrf_token, draft, snapshot)
    service.review(REVIEWER, REVIEWER.csrf_token, draft, review)
    return snapshot


def confirm(service, **changes):
    args = dict(draft_id="d1", review_id="r1", publication_id="p1",
                request_id="req1", human_confirmed=True)
    args.update(changes)
    return service.confirm(ADMIN, ADMIN.csrf_token, **args)


def rows(store, sql):
    with closing(store.connect()) as db:
        return [dict(row) for row in db.execute(sql)]


@pytest.mark.parametrize("token", [None, "", " ", "wrong"])
def test_csrf_nonempty_required(token):
    with pytest.raises(PermissionError):
        require(ADMIN, {"admin"}, token)


def test_empty_server_csrf_rejected():
    with pytest.raises(PermissionError):
        require(Principal("a", "admin", ""), {"admin"}, "")


def test_unwired_trusted_gate_fails_closed(setup):
    store, service = setup
    prepare(service)
    with pytest.raises(PermissionError):
        confirm(PublicationService(store))
    assert rows(store, "SELECT * FROM publications") == []


def test_admin_confirmation_and_role_required(setup):
    store, service = setup
    prepare(service)
    with pytest.raises(PermissionError):
        confirm(service, human_confirmed=False)
    with pytest.raises(PermissionError):
        service.confirm(REVIEWER, REVIEWER.csrf_token, draft_id="d1", review_id="r1",
                        publication_id="p1", request_id="req1", human_confirmed=True)
    assert not rows(store, "SELECT * FROM publications")


def test_review_binds_all_snapshot_content(setup):
    store, service = setup
    snapshot = prepare(service)
    snapshot["manifest_hash"] = "changed"
    service.save_draft(ADMIN, ADMIN.csrf_token, "d1", snapshot)
    with pytest.raises(PermissionError):
        confirm(service)
    assert not rows(store, "SELECT * FROM publications")


def test_gate_rechecked_on_confirmation(setup):
    store, service = setup
    prepare(service)
    rejecting_service = PublicationService(store, trusted_gate=lambda snapshot: False)
    with pytest.raises(PermissionError):
        confirm(rejecting_service)
    assert not rows(store, "SELECT * FROM publications")


def test_publish_and_idempotent_retry(setup):
    store, service = setup
    prepare(service)
    assert confirm(service) == "p1"
    assert confirm(service) == "p1"
    assert len(rows(store, "SELECT * FROM publications")) == 1
    assert len(rows(store, "SELECT * FROM audit_events")) == 1
    with pytest.raises(ValueError):
        confirm(service, publication_id="another")


def test_atomic_replacement_preserves_original(setup):
    store, service = setup
    prepare(service)
    confirm(service)
    original = rows(store, "SELECT * FROM publications")[0]["original_json"]
    prepare(service, draft="d2", review="r2", selection="s2")
    confirm(service, draft_id="d2", review_id="r2", publication_id="p2",
            request_id="req2", replace_id="p1")
    history = rows(store, "SELECT * FROM publications ORDER BY publication_id")
    assert [row["status"] for row in history] == ["superseded", "published"]
    assert history[0]["original_json"] == original
    with closing(store.connect()) as db, db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE publications SET original_json='changed' WHERE publication_id='p1'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM publications WHERE publication_id='p1'")


def test_insert_failure_rolls_back_replacement(setup):
    store, service = setup
    prepare(service)
    confirm(service)
    prepare(service, draft="d2", review="r2", selection="s2")
    # Duplicate publication PK fails AFTER the old slot has been cleared.
    with pytest.raises(sqlite3.IntegrityError):
        confirm(service, draft_id="d2", review_id="r2", publication_id="p1",
                request_id="req2", replace_id="p1")
    assert rows(store, "SELECT status FROM publications") == [{"status": "published"}]
    assert len(rows(store, "SELECT * FROM audit_events")) == 1
    assert len(rows(store, "SELECT * FROM publication_requests")) == 1


def test_audit_failure_rolls_back_every_write(setup):
    store, service = setup
    prepare(service)
    confirm(service)
    prepare(service, draft="d2", review="r2", selection="s2")
    with closing(store.connect()) as db, db:
        db.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON audit_events "
                   "BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        confirm(service, draft_id="d2", review_id="r2", publication_id="p2",
                request_id="req2", replace_id="p1")
    assert rows(store, "SELECT publication_id,status FROM publications") == [
        {"publication_id": "p1", "status": "published"}]
    assert len(rows(store, "SELECT * FROM publication_requests")) == 1


def test_partial_index_blocks_published_and_frozen_same_slot(setup):
    store, service = setup
    prepare(service)
    confirm(service)
    with closing(store.connect()) as db, db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO publications VALUES('p2','synthetic-slot','s','m',"
                       "'frozen','{}','synthetic-time')")


def test_multiple_withdrawn_history_rows_allowed(setup):
    store, _ = setup
    with closing(store.connect()) as db, db:
        for ident in ("historical1", "historical2"):
            db.execute("INSERT INTO publications VALUES(?,?,?,?,?,?,?)",
                       (ident, "same", "s", "m", "withdrawn", "{}", "synthetic-time"))
    assert len(rows(store, "SELECT * FROM publications")) == 2


def test_reviewer_cannot_create_or_modify_draft(setup):
    store, service = setup
    snapshot = prepare(service)
    with pytest.raises(PermissionError):
        service.save_draft(REVIEWER, REVIEWER.csrf_token, "d1", snapshot)
    with pytest.raises(PermissionError):
        service.save_draft(REVIEWER, REVIEWER.csrf_token, "new", snapshot)


def test_idempotency_key_is_scoped_to_actor_and_endpoint(setup):
    store, service = setup
    prepare(service)
    confirm(service)
    prepare(service, draft="d2", review="r2", slot="second-slot")
    other = Principal("second-admin", "admin", "second-csrf")
    assert service.confirm(other, other.csrf_token, draft_id="d2", review_id="r2",
                           publication_id="p2", request_id="req1", human_confirmed=True) == "p2"
    from sports_app.publication import fingerprint
    keys = {row["request_id"] for row in rows(store, "SELECT * FROM publication_requests")}
    assert keys == {fingerprint(["publication.confirm", actor, "req1"])
                    for actor in (ADMIN.actor_id, other.actor_id)}
