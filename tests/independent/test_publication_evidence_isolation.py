"""Event-synchronized same-file SQLite isolation; synthetic publication only."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import sqlite3
from threading import Event, Thread

import pytest

from sports_app.auth import Principal
from sports_app.evidence import Claim, WEIGHTS
from sports_app.evidence_store import EvidenceRepository, RepositoryReviewGate
from sports_app.publication import PublicationService
from sports_app.storage import SQLiteStore

ADMIN = Principal('isolation-admin', 'admin', 'a-csrf')
REVIEWER = Principal('isolation-reviewer', 'reviewer', 'r-csrf')
AS_OF = datetime(2026, 11, 14, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('External network/CLI forbidden')
    monkeypatch.setattr('socket.socket.connect', deny)
    monkeypatch.setattr('socket.create_connection', deny)
    monkeypatch.setattr('subprocess.Popen', deny)


class ImmediateBusyStore:
    """Same SQLite file, independent connection, no busy waiting or sleeps."""
    def __init__(self, store):
        self.store = store

    def connect(self):
        db = self.store.connect()
        db.execute('PRAGMA busy_timeout=0')
        return db


@pytest.mark.parametrize('journal_mode', ['delete', 'wal'])
def test_conflict_writer_cannot_commit_between_gate_read_and_publication_commit(tmp_path, journal_mode):
    root = Path(__file__).resolve().parents[2]
    assert tmp_path.resolve().is_relative_to(root), 'Use project-owned --basetemp'
    store = SQLiteStore(tmp_path / 'synthetic-db', app_root=root)
    with closing(store.connect()) as db:
        assert db.execute('PRAGMA journal_mode=' + journal_mode).fetchone()[0].lower() == journal_mode
    repository = EvidenceRepository(store, clock=lambda: AS_OF)
    # Initialize before acquiring the publication lock; schema writes are not part of the race.
    writer_repository = EvidenceRepository(ImmediateBusyStore(store), clock=lambda: AS_OF)
    for index in (1, 2):
        claim = Claim(f'c{index}', 'o1', tuple(WEIGHTS),
                      f'https://source{index}.example.org/report', 'L1',
                      AS_OF - timedelta(hours=2), AS_OF - timedelta(hours=1),
                      f'group-{index}', f'synthetic quote {index}', True, False)
        repository.ingest(ADMIN, ADMIN.csrf_token, claim)
        repository.verify(REVIEWER, REVIEWER.csrf_token, receipt_id=f'r{index}',
                          claim_id=claim.claim_id,
                          quote_hash=sha256(claim.quote.encode()).hexdigest(), conclusion='verified')
    conflict = Claim('conflict', 'o1', ('04',), 'https://third.example.org/report', 'L1',
                     AS_OF - timedelta(hours=1), AS_OF - timedelta(hours=1),
                     'group-3', 'synthetic conflicting quote', True, False)
    repository.ingest(ADMIN, ADMIN.csrf_token, conflict)
    conflict_hash = sha256(conflict.quote.encode()).hexdigest()

    def append_conflict(repo):
        repo.verify(REVIEWER, REVIEWER.csrf_token, receipt_id='conflict-receipt',
                    claim_id='conflict', quote_hash=conflict_hash, conclusion='conflict')

    item = dict(option_id='o1', assessment_id='a1',
                dimensions=[(f'{i:02d}', 'verified') for i in range(1, 24)],
                reasons=['r1', 'r2'], failure_paths=['f1', 'f2'], withdraw_if=['w'],
                claim_ids=['c1', 'c2'])
    from independent_ticket_fixture import research_snapshot
    snapshot = research_snapshot('synthetic-slot')
    snapshot['options'][0].update(item)
    # Both independent legs need repository evidence before review can qualify.
    from dataclasses import replace
    for index in (1, 2):
        second_claim = replace(claim, claim_id=f'other-c{index}', option_id='o2',
                               group_id=f'other-group-{index}', quote=f'other synthetic quote {index}')
        repository.ingest(ADMIN, ADMIN.csrf_token, second_claim)
        repository.verify(REVIEWER, REVIEWER.csrf_token, receipt_id=f'other-r{index}',
                          claim_id=second_claim.claim_id,
                          quote_hash=sha256(second_claim.quote.encode()).hexdigest(), conclusion='verified')
    snapshot['options'][1]['claim_ids'] = ['other-c1', 'other-c2']
    base_gate = RepositoryReviewGate(repository, additional_gate=lambda body: True)
    service = PublicationService(store, trusted_gate=base_gate)
    for draft, review in [('d1', 'review1'), ('d2', 'review2')]:
        service.save_draft(ADMIN, ADMIN.csrf_token, draft, snapshot)
        service.review(REVIEWER, REVIEWER.csrf_token, draft, review)

    gate_read = Event()
    writer_done = Event()
    observations = []

    def writer():
        try:
            if not gate_read.wait(10):
                raise AssertionError('Publication never reached synchronized gate read')
            try:
                append_conflict(writer_repository)
            except sqlite3.OperationalError as exc:
                observations.append(('blocked', exc.sqlite_errorcode))
            else:
                observations.append(('committed', None))
        except BaseException as exc:
            observations.append(('unexpected', repr(exc)))
        finally:
            writer_done.set()

    def synchronized_gate(body):
        allowed = base_gate(body)
        assert allowed, 'Positive control must qualify before the conflict'
        gate_read.set()
        assert writer_done.wait(10), 'Writer did not finish; timeout is a harness failure'
        assert observations == [('blocked', sqlite3.SQLITE_BUSY)], observations
        return allowed

    service._gate = synchronized_gate
    thread = Thread(target=writer, name='independent-conflict-writer', daemon=True)
    thread.start()
    try:
        assert service.confirm(ADMIN, ADMIN.csrf_token, draft_id='d1', review_id='review1',
                               publication_id='p1', request_id='req1', human_confirmed=True) == 'p1'
    finally:
        gate_read.set()
        thread.join(12)
        assert not thread.is_alive(), 'Writer thread did not terminate'
    assert observations == [('blocked', sqlite3.SQLITE_BUSY)]
    with closing(store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM evidence_records WHERE record_id='conflict-receipt'").fetchone()[0] == 0

    # After publication commits, exactly the same authenticated conflict write can commit.
    append_conflict(writer_repository)
    service._gate = base_gate
    assert base_gate(snapshot) is False, 'Omitted but now committed conflict must be detected'
    with pytest.raises(PermissionError):
        service.confirm(ADMIN, ADMIN.csrf_token, draft_id='d2', review_id='review2',
                        publication_id='p2', request_id='req2', human_confirmed=True, replace_id='p1')
    with closing(store.connect()) as db:
        assert [tuple(row) for row in db.execute('SELECT publication_id,status FROM publications')] == [('p1', 'published')]
        assert db.execute('SELECT COUNT(*) FROM publication_requests').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM audit_events').fetchone()[0] == 1
