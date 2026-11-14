from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import pytest
from sports_app.evidence import Claim, Receipt, EvidenceContext, WEIGHTS
from sports_app.core import score_assessment, validate_assessment, HARD_BLOCK

AS_OF = datetime(2026, 11, 14, 4, tzinfo=timezone.utc)


def fixture():
    dimensions = tuple((f"{i:02d}", "verified") for i in range(1, 24))
    claims = tuple(Claim(f"c{i}", "o1", tuple(WEIGHTS),
                         f"https://source{i}.example.org/report", "L1",
                         AS_OF-timedelta(hours=2), AS_OF-timedelta(hours=1),
                         f"independent-{i}", f"synthetic quote {i}", True, True)
                   for i in (1, 2))
    receipts = tuple(Receipt(c.claim_id, "o1", sha256(c.quote.encode()).hexdigest(),
                             "verified", "synthetic-reviewer", AS_OF) for c in claims)
    return dimensions, EvidenceContext(AS_OF, claims, receipts)


def assess(dimensions=None, context=None):
    default_dimensions, default_context = fixture()
    return score_assessment("o1", default_dimensions if dimensions is None else dimensions,
                            evidence_context=default_context if context is None else context,
                            reasons=("reason one", "reason two"), failure_paths=("path one", "path two"),
                            withdraw_if=("withdraw condition",), claim_ids=("c1", "c2"), assessment_id="a1")


def test_verified_sources_and_current_receipts_score_100():
    result = assess()
    assert result.evidence_score == 100
    assert result.eligible
    assert validate_assessment(result) == []


def test_exactly_70_can_qualify():
    dims, context = fixture()
    # 100 - 01(12) - 07(8) - 09(10) = 70, critical domains intact.
    dims = tuple((key, "unknown" if key in {"01", "07", "09"} else status) for key, status in dims)
    result = assess(dims, context)
    assert result.evidence_score == 70 and result.eligible


def test_critical_partial_preserves_93_but_blocks_eligibility():
    dims, context = fixture()
    result = assess(tuple((key, "partial" if key == "04" else value) for key, value in dims), context)
    assert result.evidence_score == 93
    assert not result.eligible


@pytest.mark.parametrize("age,eligible", [(timedelta(hours=48), True),
                                          (timedelta(hours=48, seconds=1), False)])
def test_critical_time_endpoint(age, eligible):
    dims, context = fixture()
    claims = tuple(replace(c, published_at=AS_OF-age, effective_at=AS_OF-age) for c in context.claims)
    assert assess(dims, replace(context, claims=claims)).eligible is eligible


@pytest.mark.parametrize("age,expected", [(timedelta(days=30), 100),
                                          (timedelta(days=30, seconds=1), None)])
def test_general_time_endpoint(age, expected):
    dims, context = fixture()
    critical = {"04", "13", "18", "19", "21"}
    claims = tuple(replace(c, dimension_ids=tuple(critical)) for c in context.claims)
    older = Claim("old", "o1", tuple(set(WEIGHTS)-critical), "https://history.example.org/a", "L2",
                  AS_OF-age, AS_OF-age, "history", "historical synthetic quote", True, True)
    receipt = Receipt("old", "o1", sha256(older.quote.encode()).hexdigest(), "verified", "reviewer", AS_OF)
    context = replace(context, claims=claims+(older,), receipts=context.receipts+(receipt,))
    result = score_assessment("o1", dims, reasons=("r1", "r2"), failure_paths=("f1", "f2"),
                              withdraw_if=("w",), claim_ids=("c1", "c2", "old"),
                              assessment_id="a", evidence_context=context)
    assert result.evidence_score == expected
    assert result.eligible is (expected == 100)


def test_duplicate_dimensions_null_score():
    assert assess(tuple(("01", "verified") for _ in range(23))).evidence_score is None


def test_reprinted_sources_count_as_one_group():
    dims, context = fixture()
    context = replace(context, claims=tuple(replace(c, group_id="one-original") for c in context.claims))
    result = assess(dims, context)
    assert result.evidence_score == 100 and not result.eligible


@pytest.mark.parametrize("change", [{"source_verified": False}, {"option_id": "other"},
                                    {"url": "http://127.0.0.1/private"},
                                    {"effective_at": AS_OF+timedelta(seconds=1)}])
def test_unreliable_claim_null_score(change):
    dims, context = fixture()
    context = replace(context, claims=(replace(context.claims[0], **change), context.claims[1]))
    assert assess(dims, context).evidence_score is None


@pytest.mark.parametrize("change", [{"quote_hash": "stale"}, {"option_id": "other"},
                                    {"recorded_at": AS_OF+timedelta(seconds=1)}, {"actor_id": " "}])
def test_invalid_receipt_retains_score_without_certification(change):
    dims, context = fixture()
    context = replace(context, receipts=(replace(context.receipts[0], **change), context.receipts[1]))
    result = assess(dims, context)
    assert result.evidence_score == 100 and not result.eligible


def test_conflict_hard_blocks_conditional():
    dims, context = fixture()
    context = replace(context, claims=(replace(context.claims[0], conflict=True), context.claims[1]))
    result = assess(dims, context)
    assert not result.eligible
    assert any(category == HARD_BLOCK for category, _ in validate_assessment(result, conditional=True))


def test_blank_content_and_bool_score_rejected():
    good = assess()
    assert not replace(good, reasons=(" ", "")).eligible
    assert validate_assessment(replace(good, reasons=(" ", "")))
    assert validate_assessment(replace(good, evidence_score=True), conditional=True)
