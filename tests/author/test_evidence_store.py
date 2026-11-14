from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
import sqlite3
import pytest
from sports_app.auth import Principal
from sports_app.storage import SQLiteStore
from sports_app.evidence import Claim, WEIGHTS
from sports_app.evidence_store import EvidenceRepository, RepositoryReviewGate
from sports_app.core import score_assessment

ADMIN = Principal('author', 'admin', 'a-csrf')
REVIEWER = Principal('reviewer', 'reviewer', 'r-csrf')
AS_OF = datetime(2026, 11, 14, 5, tzinfo=timezone.utc)


@pytest.fixture
def repository():
    root = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix='.evidence-test-', dir=root) as temp:
        yield EvidenceRepository(SQLiteStore(temp, app_root=root), clock=lambda: AS_OF)


def seed(repository):
    for i in (1, 2):
        claim = Claim(f'c{i}', 'o1', tuple(WEIGHTS), f'https://source{i}.example.org/report',
                      'L1', AS_OF-timedelta(hours=2), AS_OF-timedelta(hours=1),
                      f'group{i}', f'synthetic quote {i}', True, False)
        repository.ingest(ADMIN, ADMIN.csrf_token, claim)
        repository.verify(REVIEWER, REVIEWER.csrf_token, receipt_id=f'r{i}', claim_id=claim.claim_id,
                          quote_hash=sha256(claim.quote.encode()).hexdigest(), conclusion='verified')


def item():
    return dict(option_id='o1', assessment_id='a1',
                dimensions=[(f'{i:02d}', 'verified') for i in range(1, 24)],
                reasons=['r1','r2'], failure_paths=['f1','f2'], withdraw_if=['w'], claim_ids=['c1','c2'])


def test_repository_rebuild_produces_eligible_assessment(repository):
    seed(repository)
    result = score_assessment(**item(), evidence_context=repository.load_context('o1', as_of=AS_OF))
    assert result.evidence_score == 100 and result.eligible
    assert RepositoryReviewGate(repository, additional_gate=lambda s: True)({'options':[item()]})


def test_missing_records_and_caller_claims_cannot_certify(repository):
    gate = RepositoryReviewGate(repository, additional_gate=lambda s: True)
    assert not gate({'options':[item()]})
    seed(repository)
    assert not gate({'options':[dict(item(), eligible=True)]})
    assert not RepositoryReviewGate(repository)({'options':[item()]})


def test_ingestion_cannot_assert_verification(repository):
    claim = Claim('c','o1',('04',),'https://source.example.org/a','L1',AS_OF,AS_OF,
                  'g','quote',True,True)
    with pytest.raises(ValueError):
        repository.ingest(ADMIN, ADMIN.csrf_token, claim)
    with pytest.raises(PermissionError):
        repository.ingest(REVIEWER, REVIEWER.csrf_token, replace(claim,source_verified=False))


def test_receipt_identity_and_quote_are_server_bound(repository):
    seed(repository)
    with pytest.raises(PermissionError):
        repository.verify(ADMIN, ADMIN.csrf_token, receipt_id='bad', claim_id='c1',
                          quote_hash='wrong', conclusion='verified')
    with pytest.raises(ValueError):
        repository.verify(REVIEWER, REVIEWER.csrf_token, receipt_id='bad', claim_id='c1',
                          quote_hash='wrong', conclusion='verified')


def test_repository_is_append_only_and_time_filtered(repository):
    seed(repository)
    assert not repository.load_context('o1', as_of=AS_OF-timedelta(seconds=1)).claims
    with closing(repository.store.connect()) as db, db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE evidence_records SET body='{}'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM evidence_records')


def test_confirmation_recomputes_at_current_server_time(repository):
    seed(repository)
    gate = RepositoryReviewGate(repository, additional_gate=lambda s: True)
    assert gate({'options':[item()]})
    repository.clock = lambda: AS_OF+timedelta(hours=49)
    assert not gate({'options':[item()]})


def test_omitted_known_conflict_still_blocks(repository):
    seed(repository)
    claim = Claim('unreferenced-conflict', 'o1', ('04',),
                  'https://conflict.example.org/report', 'L1',
                  AS_OF-timedelta(hours=1), AS_OF-timedelta(hours=1),
                  'third-group', 'synthetic conflicting quote', True, False)
    repository.ingest(ADMIN, ADMIN.csrf_token, claim)
    repository.verify(REVIEWER, REVIEWER.csrf_token, receipt_id='conflict-receipt',
                      claim_id=claim.claim_id, quote_hash=sha256(claim.quote.encode()).hexdigest(),
                      conclusion='conflict')
    assert not RepositoryReviewGate(repository, additional_gate=lambda s: True)({'options':[item()]})


def conditional_snapshot(repository, *, kind='regular_pair', status='conditional_low_confidence'):
    first = item()
    first['dimensions'] = [(key, 'partial' if key == '04' else value)
                           for key, value in first['dimensions']]
    second = dict(item(), option_id='o2', assessment_id='a2', claim_ids=['missing-o2'])
    snapshot = {'recommendation_type': kind, 'recommendation_status': status,
                'options': [first, second]}
    return repository.freeze_recommendation(ADMIN, ADMIN.csrf_token,
                                            freeze_id='recommendation-freeze', snapshot=snapshot)


def test_frozen_regular_pair_quality_gaps_allowed_without_changing_scores(repository):
    seed(repository)
    snapshot = conditional_snapshot(repository)
    gate = RepositoryReviewGate(repository, additional_gate=lambda s: True)
    assert gate(snapshot)
    first = snapshot['options'][0]
    score = score_assessment(**first, evidence_context=repository.load_context('o1', as_of=AS_OF))
    assert score.evidence_score == 93 and not score.eligible and score.quality_gaps
    second = snapshot['options'][1]
    score = score_assessment(**second, evidence_context=repository.load_context('o2', as_of=AS_OF))
    assert score.evidence_score is None and not score.eligible and score.quality_gaps


@pytest.mark.parametrize('kind,status', [('single','conditional_low_confidence'),
                                         ('dream','conditional_low_confidence'),
                                         ('regular_pair','evidence_qualified')])
def test_other_frozen_recommendations_not_relaxed(repository, kind, status):
    seed(repository)
    snapshot = conditional_snapshot(repository, kind=kind, status=status)
    assert not RepositoryReviewGate(repository, additional_gate=lambda s: True)(snapshot)


def test_client_labels_or_changed_freeze_cannot_enable_conditional(repository):
    seed(repository)
    frozen = conditional_snapshot(repository)
    gate = RepositoryReviewGate(repository, additional_gate=lambda s: True)
    unfrozen = {key:value for key,value in frozen.items() if key != 'recommendation_freeze_id'}
    assert not gate(unfrozen)
    frozen['options'][0]['reasons'] = ['changed','after freeze']
    assert not gate(frozen)


def test_conditional_conflict_and_other_hard_gate_still_block(repository):
    seed(repository)
    frozen = conditional_snapshot(repository)
    assert not RepositoryReviewGate(repository, additional_gate=lambda s: False)(frozen)
    repository.verify(REVIEWER, REVIEWER.csrf_token, receipt_id='new-conflict', claim_id='c1',
                      quote_hash=sha256('synthetic quote 1'.encode()).hexdigest(), conclusion='conflict')
    assert not RepositoryReviewGate(repository, additional_gate=lambda s: True)(frozen)
