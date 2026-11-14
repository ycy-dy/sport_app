"""Independent red tests for second-batch interfaces. No real network/CLI/data."""
from decimal import Decimal
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from sports_app.auth import Principal, require
from sports_app.app import create_app
from sports_app.core import Assessment, Branch, branch, validate_assessment, validate_branches
from sports_app.storage import SQLiteStore, SCHEMA


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Independent test blocked external network/CLI')
    monkeypatch.setattr('socket.socket.connect', deny)
    monkeypatch.setattr('socket.create_connection', deny)
    monkeypatch.setattr('subprocess.Popen', deny)


def assessment(score=100, ident='opaque-record', option='B'):
    return Assessment(ident, option, score, (), ('reason1', 'reason2'),
                      ('failure1', 'failure2'), ('withdraw',), ('unverified-claim',))


def hard(errors):
    return any(category == 'HARD_BLOCK' for category, _ in errors)


def test_unverified_claim_ids_cannot_certify_100_points():
    a = assessment(ident='record-B')
    assert not a.eligible or validate_assessment(a), (
        'Unverified ID strings and caller supplied 100 must not certify evidence')


def test_assessment_id_is_opaque_not_an_option_suffix():
    errors = validate_assessment(assessment(score=None), conditional=True)
    assert not hard(errors), 'Opaque assessment_id is not evidence of mismatched option identity'


def test_blank_reasons_and_failure_paths_do_not_satisfy_cardinality():
    a = Assessment('record-B', 'B', 100, (), ('', ' '), ('', ' '), ('',), ('x',))
    assert validate_assessment(a), 'Blank entries cannot satisfy required reasoning fields'


def test_boolean_is_not_a_policy_evidence_score():
    assert hard(validate_assessment(assessment(score=True, ident='record-B'), conditional=True))


# Frozen universes are synthetic fixtures independent of submitted branch rows.
SINGLE_FROZEN = ((("A", "1.50"),), (("B", "2.00"),))
TWO_FROZEN = ((("A", "3"), ("C", "3")), (("B", "3"),))
FOUR_FROZEN = ((("A", "4"), ("C", "4")), (("B", "4"), ("D", "4")))


def audit_branches(rows, frozen):
    return validate_branches(rows, frozen_leg_options=frozen)


def test_branch_factory_valid_control():
    rows = [branch('one', ('A', 'B'), ('1.50', '2.00'), '1', '1')]
    assert audit_branches(rows, SINGLE_FROZEN) == []
    # A complete multi-branch ticket must also pass, not just a one-pair ticket.
    rows = [branch('one', ('A', 'B'), ('3', '3'), '1', '2'),
            branch('two', ('C', 'B'), ('3', '3'), '1', '2')]
    assert audit_branches(rows, TWO_FROZEN) == []


def test_exact_threshold_is_rejected():
    frozen = ((("A", "1.25"),), (("B", "2"),))
    assert hard(audit_branches([branch('one', ('A', 'B'), ('1.25', '2'), '1', '1')], frozen))


def test_forged_product_cannot_override_original_odds():
    # Valid original odds exceed the gate; only submitted arithmetic is forged.
    b = Branch('one', ('A', 'B'), (Decimal('1.50'), Decimal('2.00')),
               Decimal(1), Decimal(1), Decimal(9), Decimal(9))
    errors = audit_branches([b], SINGLE_FROZEN)
    assert hard(errors)
    assert any('乘积伪造' in message for _, message in errors)


def test_direct_dataclass_cannot_bypass_positive_odds():
    b = Branch('one', ('A', 'B'), (Decimal('-3'), Decimal('-3')),
               Decimal(1), Decimal(1), Decimal(9), Decimal(9))
    assert hard(audit_branches([b], SINGLE_FROZEN))


def test_direct_dataclass_infinity_is_rejected_without_crash():
    b = Branch('one', ('A', 'B'), (Decimal('Infinity'), Decimal(2)),
               Decimal(1), Decimal(1), Decimal('Infinity'), Decimal('Infinity'))
    assert hard(audit_branches([b], SINGLE_FROZEN))


def test_total_must_equal_sum_of_equal_branch_stakes():
    rows = [branch('one', ('A', 'B'), ('3', '3'), '1', '1'),
            branch('two', ('C', 'B'), ('3', '3'), '1', '1')]
    errors = audit_branches(rows, TWO_FROZEN)
    assert hard(errors), 'Two unit branches require denominator 2, not 1'
    assert any('整票总投入' in message for _, message in errors)


def test_duplicate_option_combination_not_hidden_by_new_branch_id():
    rows = [branch('one', ('A', 'B'), ('3', '3'), '1', '2'),
            branch('two', ('A', 'B'), ('3', '3'), '1', '2')]
    assert hard(audit_branches(rows, TWO_FROZEN))


def test_cartesian_branch_hole_is_rejected():
    # Correct total for the independently frozen four-pair universe, one row missing.
    rows = [branch(str(i), ids, ('4', '4'), '1', '4')
            for i, ids in enumerate([('A', 'B'), ('A', 'D'), ('C', 'B')])]
    errors = audit_branches(rows, FOUR_FROZEN)
    assert hard(errors), 'A frozen Cartesian combination is missing'
    assert any('整票缺失' in message for _, message in errors)


def test_absent_csrf_tokens_cannot_authenticate():
    with pytest.raises(PermissionError):
        require(Principal('admin', 'admin', None), {'admin'}, None)


def test_anonymous_request_cannot_inherit_global_admin(tmp_path):
    app = create_app(data_dir=str(tmp_path), principal_loader=lambda req: None)
    client = app.test_client()  # no authenticated principal for this visitor
    response = client.post('/api/analysis-runs', headers={'X-CSRF': 'test-csrf'},
                           json={'business_date': '2026-11-14'})
    assert response.status_code == 401, 'Knowing a CSRF string is not request authentication'


def test_unwired_task_cannot_claim_queued(tmp_path):
    app = create_app(data_dir=str(tmp_path), principal_loader=lambda req: Principal('owner', 'admin', 'test-csrf'))
    response = app.test_client().post('/api/analysis-runs',
        headers={'X-CSRF': 'test-csrf'}, json={'business_date': '2026-11-14'})
    assert response.status_code == 501
    assert response.get_json().get('status') != 'queued'


def insert_publication(db, ident, status):
    db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?)',
               (ident, 'same-slot', ident, 'manifest', status, '{}', 'synthetic-time'))


def test_schema_allows_only_one_active_published_or_frozen_slot():
    with sqlite3.connect(':memory:') as db:
        db.executescript(SCHEMA)
        insert_publication(db, 'one', 'published')
        with pytest.raises(sqlite3.IntegrityError):
            insert_publication(db, 'two', 'frozen')


def test_schema_preserves_multiple_withdrawn_historical_tickets():
    with sqlite3.connect(':memory:') as db:
        db.executescript(SCHEMA)
        insert_publication(db, 'old-one', 'withdrawn')
        insert_publication(db, 'old-two', 'withdrawn')
        assert db.execute('SELECT COUNT(*) FROM publications').fetchone()[0] == 2


def test_foot_path_is_rejected_before_any_write_or_connect():
    # Never touch the protected directory: replace both potential write operations.
    with patch.object(Path, 'mkdir', side_effect=AssertionError('mkdir reached')) as mkdir, \
         patch('sports_app.storage.sqlite3.connect', side_effect=AssertionError('connect reached')) as connect, \
         patch.object(Path, 'resolve', autospec=True, side_effect=lambda path: path):
        with pytest.raises(ValueError):
            SQLiteStore('/Users/ycy/ycy/foot/independent-forbidden', app_root='/Users/ycy/ycy/sports-app')
        mkdir.assert_not_called()
        connect.assert_not_called()


def scored(dimensions):
    from sports_app.core import score_assessment
    return score_assessment('B', tuple(dimensions), assessment_id='record-B',
        reasons=('r1', 'r2'), failure_paths=('f1', 'f2'),
        withdraw_if=('stop',), claim_ids=('synthetic-unverified',))


def test_all_23_verified_cannot_produce_230_points():
    result = scored([(f'{i:02d}', 'verified') for i in range(1, 24)])
    assert result.evidence_score is None or 0 <= result.evidence_score <= 100
    assert not result.eligible, 'Unverified claim strings cannot certify all 23 dimensions'


def test_non_scoring_dimensions_cannot_add_evidence_points():
    weighted = {1, 4, 7, 9, 12, 13, 15, 17, 18, 19, 21}
    result = scored([(f'{i:02d}', 'unknown' if i in weighted else 'verified')
                     for i in range(1, 24)])
    assert result.evidence_score in (None, 0), 'Market/model dimensions have zero evidence weight'
    assert not result.eligible


def test_duplicate_dimension_names_do_not_form_valid_23_dimensions():
    result = scored([('01', 'verified')] * 23)
    assert not result.eligible
    assert result.evidence_score is None or result.quality_gaps


def test_request_identity_survives_interleaved_other_request(tmp_path):
    from flask import g, request
    # Headers are used only by this injected synthetic identity fixture, not an auth design.
    def loader(req):
        role = req.headers.get('X-Test-Role', 'viewer')
        return Principal(role, role, 'test-csrf')
    app = create_app(data_dir=str(tmp_path), principal_loader=loader)
    @app.before_request
    def interleave_after_authentication():
        if request.headers.get('X-Test-Interleave') == 'yes':
            # g belongs to the app context. A nested request alone reuses it;
            # model an independent request with an explicitly separate app context.
            outer_globals = g._get_current_object()
            outer_principal = g.principal
            with app.app_context():
                assert g._get_current_object() is not outer_globals
                with app.test_client() as other:
                    assert other.get('/healthz', headers={'X-Test-Role': 'viewer'}).status_code == 200
                    assert g.principal.role == 'viewer'
            assert g._get_current_object() is outer_globals
            assert g.principal is outer_principal
    response = app.test_client().post('/api/analysis-runs', headers={
        'X-Test-Role': 'admin', 'X-Test-Interleave': 'yes', 'X-CSRF': 'test-csrf'})
    assert response.status_code == 501, 'Admin request must keep its own principal, not nested viewer'
    assert 'CURRENT_PRINCIPAL' not in app.config, 'Identity must live in request-local context'


def test_new_project_data_path_is_allowed_not_rejected_as_foot():
    import sports_app.storage as storage
    root = Path('/Users/ycy/ycy/sports-app')
    target = root / 'tests/independent/.synthetic-data'
    with patch.object(Path, 'mkdir', side_effect=AssertionError('mkdir reached')) as mkdir, \
         patch('sports_app.storage.sqlite3.connect', side_effect=AssertionError('connect reached')) as connect, \
         patch.object(Path, 'resolve', autospec=True, side_effect=lambda path: path):
        assert storage.safe_data_dir(target, app_root=root) == target
        mkdir.assert_not_called()
        connect.assert_not_called()


def test_arbitrary_external_data_path_is_not_an_isolated_project_root():
    root = Path('/Users/ycy/ycy/sports-app')
    target = Path('/outside-project/synthetic-data')
    with patch.object(Path, 'mkdir', side_effect=AssertionError('mkdir reached')) as mkdir, \
         patch('sports_app.storage.sqlite3.connect', side_effect=AssertionError('connect reached')) as connect, \
         patch.object(Path, 'resolve', autospec=True, side_effect=lambda path: path):
        with pytest.raises(ValueError):
            SQLiteStore(target, app_root=root)
        mkdir.assert_not_called()
        connect.assert_not_called()
