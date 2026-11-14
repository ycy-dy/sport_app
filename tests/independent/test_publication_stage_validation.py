"""Matched-fingerprint legacy corruption must not bypass stage validation."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path

import pytest

from independent_ticket_fixture import research_snapshot
from sports_app.auth import Principal
from sports_app.publication import PublicationService, canonical, fingerprint
from sports_app.storage import SQLiteStore

ADMIN = Principal('stage-admin', 'admin', 'admin-csrf')
REVIEWER = Principal('stage-reviewer', 'reviewer', 'review-csrf')
TABLES = ('publication_drafts', 'publication_reviews', 'publications',
          'publication_requests', 'audit_events')


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect', deny)
    monkeypatch.setattr('socket.create_connection', deny)
    monkeypatch.setattr('subprocess.Popen', deny)


@pytest.fixture
def setup(tmp_path):
    root = Path(__file__).resolve().parents[2]
    assert tmp_path.resolve().is_relative_to(root), 'Use project-owned --basetemp'
    store = SQLiteStore(tmp_path / 'synthetic', app_root=root)
    gate_calls = []
    def always_true(body):
        gate_calls.append(deepcopy(body))
        return True
    svc = PublicationService(store, trusted_gate=always_true)
    # Real positive-control service path, before legacy fixture corruption.
    svc.save_draft(ADMIN, ADMIN.csrf_token, 'old-draft', research_snapshot())
    svc.review(REVIEWER, REVIEWER.csrf_token, 'old-draft', 'old-review')
    assert svc.confirm(ADMIN, ADMIN.csrf_token, draft_id='old-draft', review_id='old-review',
                       publication_id='old-publication', request_id='old-request',
                       human_confirmed=True) == 'old-publication'
    with closing(store.connect()) as db:
        valid = json.loads(db.execute("SELECT original_json FROM publications WHERE publication_id='old-publication'").fetchone()[0])
    assert valid['settlement_ticket']['branches']
    return store, svc, valid, gate_calls


def all_rows(store):
    with closing(store.connect()) as db:
        return {table: sorted(tuple(row) for row in db.execute('SELECT * FROM ' + table))
                for table in TABLES}


def corrupt(valid, mutation):
    body = deepcopy(valid)
    if mutation == 'missing_structure':
        del body['settlement_ticket']
    elif mutation == 'missing_branch':
        body['settlement_ticket']['branches'] = []
    elif mutation == 'price_drift':
        body['settlement_ticket']['legs'][0]['options'][0]['original_odds'] = '99'
    else:
        raise AssertionError('Unknown fixture mutation')
    return body


def seed_legacy(store, body, include_review):
    digest = fingerprint(body)
    with closing(store.connect()) as db, db:
        db.execute('INSERT INTO publication_drafts VALUES(?,?,?)',
                   ('legacy-draft', canonical(body), digest))
        if include_review:
            # Deliberately simulate an old approved record with the SAME bad content hash.
            db.execute('INSERT INTO publication_reviews VALUES(?,?,?,?,?)',
                       ('legacy-review', 'legacy-draft', digest, REVIEWER.actor_id, 'synthetic-time'))
        draft = db.execute("SELECT * FROM publication_drafts WHERE draft_id='legacy-draft'").fetchone()
        assert fingerprint(json.loads(draft['snapshot_json'])) == draft['fingerprint'] == digest
        if include_review:
            review = db.execute("SELECT * FROM publication_reviews WHERE review_id='legacy-review'").fetchone()
            assert review['fingerprint'] == digest and review['draft_id'] == 'legacy-draft'


@pytest.mark.parametrize('mutation', ['missing_structure', 'missing_branch', 'price_drift'])
def test_review_rejects_bad_structure_even_with_matching_draft_hash(setup, mutation):
    store, svc, valid, calls = setup
    seed_legacy(store, corrupt(valid, mutation), include_review=False)
    before, call_count = all_rows(store), len(calls)
    with pytest.raises(ValueError):
        svc.review(REVIEWER, REVIEWER.csrf_token, 'legacy-draft', 'legacy-review')
    assert all_rows(store) == before
    assert len(calls) == call_count, 'Structural rejection must precede permissive business gate'


@pytest.mark.parametrize('mutation', ['missing_structure', 'missing_branch', 'price_drift'])
def test_confirm_rejects_bad_structure_even_with_matching_review_hash(setup, mutation):
    store, svc, valid, calls = setup
    seed_legacy(store, corrupt(valid, mutation), include_review=True)
    before, call_count = all_rows(store), len(calls)
    with pytest.raises(ValueError):
        svc.confirm(ADMIN, ADMIN.csrf_token, draft_id='legacy-draft', review_id='legacy-review',
                    publication_id='new-publication', request_id='new-request', human_confirmed=True,
                    replace_id='old-publication')
    assert all_rows(store) == before
    assert len(calls) == call_count, 'Structural rejection must precede permissive business gate'
    with closing(store.connect()) as db:
        assert db.execute("SELECT status FROM publications WHERE publication_id='old-publication'").fetchone()[0] == 'published'
