"""Deterministic gates for the isolated sports app package."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

QUALITY_GAP = "QUALITY_GAP"
HARD_BLOCK = "HARD_BLOCK"
EXECUTION_FAILURE = "EXECUTION_FAILURE"
GATE = Decimal("2.5")

@dataclass(frozen=True)
class Assessment:
    assessment_id: str
    option_id: str
    evidence_score: int | None
    quality_gaps: tuple[str, ...]
    reasons: tuple[str, ...]
    failure_paths: tuple[str, ...]
    withdraw_if: tuple[str, ...]
    claim_ids: tuple[str, ...]
    hard_blocks: tuple[str, ...] = ()
    evidence_score_version: str | None = None

    @property
    def eligible(self) -> bool:
        from .evidence import VERSION, text
        return (type(self.evidence_score) is int and 70 <= self.evidence_score <= 100
                and self.evidence_score_version == VERSION
                and not self.quality_gaps and not self.hard_blocks
                and len(self.reasons) >= 2 and all(text(v) for v in self.reasons)
                and len(self.failure_paths) >= 2 and all(text(v) for v in self.failure_paths)
                and bool(self.withdraw_if) and all(text(v) for v in self.withdraw_if)
                and bool(self.claim_ids))


def score_assessment(option_id: str, dimensions: tuple[tuple[str, str], ...], *,
                     reasons: tuple[str, ...], failure_paths: tuple[str, ...],
                     withdraw_if: tuple[str, ...], claim_ids: tuple[str, ...],
                     assessment_id: str, evidence_context=None) -> Assessment:
    """Score server-loaded evidence; legacy status-only inputs cannot certify."""
    from .evidence import evaluate, VERSION
    score, gaps, blocks = evaluate(option_id, dimensions, claim_ids, evidence_context)
    return Assessment(assessment_id, option_id, score, gaps, reasons,
                      failure_paths, withdraw_if, claim_ids, blocks, VERSION)

@dataclass(frozen=True)
class Option:
    option_id: str
    assessment: Assessment


def validate_option(option: Option, *, conditional: bool = False) -> list[tuple[str, str]]:
    """Require explicit object identity; no name/id convention can substitute."""
    errors = []
    if option.assessment.option_id != option.option_id:
        errors.append((HARD_BLOCK, "option与assessment显式绑定不一致"))
    errors.extend(validate_assessment(option.assessment, conditional=conditional))
    return errors

@dataclass(frozen=True)
class Branch:
    branch_id: str
    leg_option_ids: tuple[str, ...]
    original_odds_decimal: tuple[Decimal, ...]
    equal_stake_decimal: Decimal
    total_investment_decimal: Decimal
    odds_product_decimal: Decimal
    effective_multiple_decimal: Decimal


def validate_assessment(a: Assessment, *, conditional: bool = False) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    if not a.assessment_id or not a.option_id:
        errors.append((HARD_BLOCK, "assessment未绑定具体option"))
    from .evidence import text
    errors.extend((HARD_BLOCK, block) for block in a.hard_blocks)
    if len(a.reasons) < 2 or not all(text(v) for v in a.reasons):
        errors.append((QUALITY_GAP, "理由少于2条"))
    if (len(a.failure_paths) < 2 or not all(text(v) for v in a.failure_paths)
            or not a.withdraw_if or not all(text(v) for v in a.withdraw_if)):
        errors.append((QUALITY_GAP, "失败路径或撤销条件不足"))
    if not a.claim_ids:
        errors.append((QUALITY_GAP, "缺少实际引用claim"))
    if a.evidence_score is not None and (type(a.evidence_score) is not int or not 0 <= a.evidence_score <= 100):
        errors.append((HARD_BLOCK, "证据分越界"))
    if not conditional and not a.eligible:
        errors.append((QUALITY_GAP, "option未达到证据准入"))
    return errors


def _decimal(value: str | Decimal) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("赔率/投入必须是有限Decimal") from exc
    if not result.is_finite():
        raise ValueError("赔率/投入必须是有限Decimal")
    return result


def branch(ident: str, option_ids: tuple[str, ...], odds: tuple[str, ...], stake: str, total: str) -> Branch:
    if len(option_ids) != 2 or len(odds) != 2 or not all(option_ids):
        raise ValueError("二串一必须绑定两腿选项")
    original = tuple(_decimal(value) for value in odds)
    if any(value <= 0 for value in original):
        raise ValueError("赔率必须为正")
    equal, denominator = _decimal(stake), _decimal(total)
    if equal <= 0 or denominator <= 0 or denominator < equal:
        raise ValueError("等额投入和总投入必须为正且总投入不小于单分支投入")
    product = original[0] * original[1]
    effective = product * equal / denominator
    return Branch(ident, option_ids, original, equal, denominator, product, effective)


def all_branches(leg_options: tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]],
                 *, equal_stake: str, total_investment: str) -> list[Branch]:
    """Enumerate every option pair from two distinct legs using original odds."""
    import itertools
    return [branch(f"b-{i}", (left[0], right[0]), (left[1], right[1]),
                    equal_stake, total_investment)
            for i, (left, right) in enumerate(itertools.product(*leg_options))]


def validate_branches(branches: list[Branch], *, frozen_leg_options=None) -> list[tuple[str, str]]:
    """Audit complete ticket against independently frozen option universe."""
    from collections import Counter
    from decimal import DecimalException, localcontext
    from itertools import product
    def reject(message):
        return [(HARD_BLOCK, message)]
    if not branches or frozen_leg_options is None:
        return reject("缺少整票分支或冻结两腿全集")
    try:
        if len(frozen_leg_options) != 2:
            return reject("冻结输入必须恰好两腿")
        legs = []
        for leg in frozen_leg_options:
            if not leg:
                return reject("冻结腿不得为空")
            values = {}
            for option_id, raw_odds in leg:
                if not isinstance(option_id, str) or not option_id or option_id in values:
                    return reject("冻结选项ID无效或重复")
                odds = _decimal(raw_odds)
                if odds <= 0:
                    return reject("冻结原赔率必须为有限正数")
                values[option_id] = odds
            legs.append(values)
        if set(legs[0]) & set(legs[1]):
            return reject("两腿不得重用选项ID")
        expected_pairs = set(product(legs[0], legs[1]))
    except (ValueError, TypeError, DecimalException):
        return reject("冻结两腿全集格式或赔率无效")
    errors, seen_ids, pair_counts = [], set(), Counter()
    common_stake = common_total = None
    for item in branches:
        try:
            if not isinstance(item.branch_id, str) or not item.branch_id:
                errors.extend(reject("分支ID无效"))
            elif item.branch_id in seen_ids:
                errors.extend(reject("重复分支ID"))
            seen_ids.add(item.branch_id)
            pair = tuple(item.leg_option_ids)
            if len(pair) != 2 or not all(isinstance(v, str) and v for v in pair):
                errors.extend(reject("分支必须绑定两腿选项")); continue
            pair_counts[pair] += 1
            if pair not in expected_pairs:
                errors.extend(reject("分支组合不属于冻结全集")); continue
            stake = _decimal(item.equal_stake_decimal)
            total = _decimal(item.total_investment_decimal)
            submitted_product = _decimal(item.odds_product_decimal)
            submitted_effective = _decimal(item.effective_multiple_decimal)
            original = tuple(_decimal(v) for v in item.original_odds_decimal)
            frozen_odds = (legs[0][pair[0]], legs[1][pair[1]])
            if original != frozen_odds:
                errors.extend(reject("分支原赔率与冻结输入不一致"))
            if stake <= 0 or total <= 0:
                errors.extend(reject("投入必须为有限正数")); continue
            if common_stake is None:
                common_stake, common_total = stake, total
            if stake != common_stake or total != common_total:
                errors.extend(reject("所有分支须等额且总投入一致"))
            with localcontext() as context:
                context.prec = 28
                if total != stake * len(expected_pairs):
                    errors.extend(reject("整票总投入不等于等额投入乘完整组合数"))
                odds_product = frozen_odds[0] * frozen_odds[1]
                effective = odds_product * stake / total
                if submitted_product != odds_product:
                    errors.extend(reject("分支赔率乘积伪造"))
                if submitted_effective != effective:
                    errors.extend(reject("分支有效倍数未按冻结原赔率重算"))
                if odds_product * stake <= GATE * total:
                    errors.extend(reject("分支有效倍数不严格大于2.5"))
        except (AttributeError, ValueError, TypeError, DecimalException):
            errors.extend(reject("分支字段或数值无效"))
    if set(pair_counts) != expected_pairs or any(n != 1 for n in pair_counts.values()) or len(branches) != len(expected_pairs):
        errors.extend(reject("整票缺失、重复或增加选项组合"))
    return errors


def validate_required_analysis(payload: dict[str, Any], required: list[str]) -> list[tuple[str, str]]:
    return [(QUALITY_GAP, f"缺少039字段:{key}") for key in required
            if payload.get(key) in (None, "", [])]
