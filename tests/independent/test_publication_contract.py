"""Independent synthetic transaction contract tests, not real publication approval."""
from contextlib import closing
from pathlib import Path

import pytest

from sports_app.auth import Principal
from sports_app.storage import SQLiteStore
from sports_app.publication import PublicationService

ADMIN = Principal('independent-admin', 'admin', 'admin-csrf')
OTHER = Principal('independent-other-admin', 'admin', 'other-csrf')
REVIEWER = Principal('independent-reviewer', 'reviewer', 'review-csrf')


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Independent test blocked external network/CLI')
    monkeypatch.setattr('socket.socket.connect', deny)
    monkeypatch.setattr('socket.create_connection', deny)
    monkeypatch.setattr('subprocess.Popen', deny)


@pytest.fixture
def service(tmp_path):
    root = Path(__file__).resolve().parents[2]
    assert tmp_path.resolve().is_relative_to(root), 'Run with project-owned --basetemp'
    store = SQLiteStore(tmp_path / 'data', app_root=root)
    # Deliberately synthetic gate: only transaction/permission behavior is tested.
    return store, PublicationService(store, trusted_gate=lambda snapshot: True)


def snapshot(slot='slot-1'):
    from independent_ticket_fixture import research_snapshot
    return research_snapshot(slot)


def prepare(svc, draft='d1', review='r1', slot='slot-1'):
    body = snapshot(slot)
    svc.save_draft(ADMIN, ADMIN.csrf_token, draft, body)
    svc.review(REVIEWER, REVIEWER.csrf_token, draft, review)
    return body


def confirm(svc, actor=ADMIN, draft='d1', review='r1', pub='p1', key='key-1', replace=None):
    return svc.confirm(actor, actor.csrf_token, draft_id=draft, review_id=review,
                       publication_id=pub, request_id=key, human_confirmed=True,
                       replace_id=replace)


def records(store, table):
    assert table in {'publication_drafts', 'publications', 'publication_requests', 'audit_events'}
    with closing(store.connect()) as db:
        return [dict(row) for row in db.execute('SELECT * FROM ' + table)]


def test_admin_can_save_reviewer_can_review_control(service):
    store, svc = service
    prepare(svc)
    assert confirm(svc) == 'p1'
    published = records(store, 'publications')
    assert len(published) == 1
    import json
    frozen = json.loads(published[0]['original_json'])['settlement_ticket']
    assert len(frozen['legs']) == 2 and len(frozen['branches']) == 1
    assert frozen['branches'][0]['option_ids'] == ['o1', 'o2']
    assert frozen['total_investment'] == '1'


def test_reviewer_cannot_create_draft(service):
    store, svc = service
    with pytest.raises(PermissionError):
        svc.save_draft(REVIEWER, REVIEWER.csrf_token, 'forbidden', snapshot())
    assert records(store, 'publication_drafts') == []


def test_reviewer_cannot_modify_admin_draft(service):
    store, svc = service
    prepare(svc)
    before = records(store, 'publication_drafts')
    changed = dict(snapshot(), manifest_hash='changed')
    with pytest.raises(PermissionError):
        svc.save_draft(REVIEWER, REVIEWER.csrf_token, 'd1', changed)
    assert records(store, 'publication_drafts') == before


def test_replacement_uses_superseded_and_keeps_original(service):
    store, svc = service
    prepare(svc)
    confirm(svc)
    before = records(store, 'publications')[0]
    prepare(svc, 'd2', 'r2')
    confirm(svc, draft='d2', review='r2', pub='p2', key='key-2', replace='p1')
    rows = {row['publication_id']: row for row in records(store, 'publications')}
    assert rows['p1']['status'] == 'superseded'
    assert rows['p2']['status'] == 'published'
    assert rows['p1']['original_json'] == before['original_json']
    assert len(rows) == 2


def test_same_actor_same_key_same_command_is_idempotent(service):
    store, svc = service
    prepare(svc)
    assert confirm(svc) == confirm(svc) == 'p1'
    assert len(records(store, 'publications')) == 1
    assert len(records(store, 'publication_requests')) == 1
    assert len(records(store, 'audit_events')) == 1
    with pytest.raises(ValueError):
        confirm(svc, pub='changed')
    assert len(records(store, 'publication_requests')) == 1


def test_same_key_different_actors_have_independent_scopes(service):
    store, svc = service
    prepare(svc)
    prepare(svc, 'd2', 'r2', 'slot-2')
    assert confirm(svc) == 'p1'
    assert confirm(svc, actor=OTHER, draft='d2', review='r2', pub='p2') == 'p2'
    assert confirm(svc, actor=OTHER, draft='d2', review='r2', pub='p2') == 'p2'
    assert len(records(store, 'publications')) == 2
    assert len(records(store, 'publication_requests')) == 2
    assert len(records(store, 'audit_events')) == 2
