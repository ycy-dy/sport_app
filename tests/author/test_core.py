from decimal import Decimal
import pytest
from sports_app.core import Assessment, all_branches, branch, validate_branches, validate_option, score_assessment

def test_status_strings_without_server_evidence_cannot_certify():
    dimensions = tuple((f"{i:02d}", "verified") for i in range(1, 24))
    a = score_assessment("o1", dimensions, reasons=("a","b"), failure_paths=("x","y"), withdraw_if=("z",), claim_ids=("c",), assessment_id="assessment")
    assert a.evidence_score is None
    assert not a.eligible

def test_option_requires_explicit_binding():
    a = Assessment("a", "o2", 70, (), ("a","b"), ("x","y"), ("z",), ("c",))
    from sports_app.core import Option
    assert validate_option(Option("o1", a))

# Whole-ticket positive and forgery cases now live in test_ticket_gate.py.
# A single branch is not a complete two-branch ticket.
def test_nonfinite_original_odds_constructor_rejected():
    with pytest.raises(ValueError):
        branch("bad", ("o1", "o2"), ("nan", "1.5"), "1", "2")
