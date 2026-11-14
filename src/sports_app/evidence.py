"""Pure scoring of server-loaded evidence records; never fetches URLs.

EvidenceContext is an internal repository boundary, NOT a request DTO. Source
verification and receipts must be loaded from the authenticated server record
store. The HTTP API is intentionally not wired to this module yet.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from ipaddress import ip_address
from urllib.parse import urlsplit

WEIGHTS = {"01":12,"04":14,"07":8,"09":10,"12":10,"13":10,"15":8,
           "17":6,"18":14,"19":4,"21":4}
CRITICAL = frozenset({"04", "13", "18", "19", "21"})
VERSION = "evidence-policy-1.0.0"


@dataclass(frozen=True)
class Claim:
    claim_id: str
    option_id: str
    dimension_ids: tuple[str, ...]
    url: str
    level: str
    published_at: datetime
    effective_at: datetime
    group_id: str  # server-resolved original/republished source group
    quote: str
    non_odds: bool
    source_verified: bool
    conflict: bool = False


@dataclass(frozen=True)
class Receipt:
    claim_id: str
    option_id: str
    quote_hash: str
    conclusion: str
    actor_id: str
    recorded_at: datetime


@dataclass(frozen=True)
class EvidenceContext:
    as_of: datetime
    claims: tuple[Claim, ...]
    receipts: tuple[Receipt, ...]


def text(value):
    return isinstance(value, str) and bool(value.strip())


def aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def safe_url(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or host.lower() == "localhost"
                or "." not in host or host.lower().endswith((".local", ".internal", ".localhost"))):
            return False
        try:
            return ip_address(host).is_global
        except ValueError:
            return True  # no DNS/network at scoring time; verified store owns source validation
    except (ValueError, TypeError):
        return False


def evaluate(option_id, dimensions, claim_ids, context):
    gaps, blocks = [], []
    expected = {f"{number:02d}" for number in range(1, 24)}
    try:
        ids = [ident for ident, status in dimensions]
        statuses = dict(dimensions)
        valid = (len(ids) == 23 and set(ids) == expected
                 and all(status in {"verified", "partial", "unknown", "not_applicable", "conflict"}
                         for status in statuses.values()))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        return None, ("维度全集无效",), ()
    if any(statuses[key] == "conflict" for key in WEIGHTS):
        blocks.append("计分维度存在来源冲突")
    if not isinstance(context, EvidenceContext) or not aware(context.as_of):
        return None, ("缺少服务器证据或as_of",), tuple(blocks)
    claims = {claim.claim_id: claim for claim in context.claims}
    if len(claims) != len(context.claims) or len(set(claim_ids)) != len(claim_ids):
        return None, ("证据ID重复",), tuple(blocks)
    used = {key: [] for key in WEIGHTS}
    groups = set()
    unreliable = False
    for ident in claim_ids:
        claim = claims.get(ident)
        if claim is None or claim.option_id != option_id:
            gaps.append("引用不存在或错绑Option")
            unreliable = True
            continue
        if claim.conflict and set(claim.dimension_ids) & WEIGHTS.keys():
            blocks.append("已记录来源冲突:" + ident)
        valid_source = (
            claim.non_odds is True and claim.source_verified is True
            and claim.level in {"L1", "L2"} and safe_url(claim.url)
            and text(claim.group_id) and text(claim.quote)
            and aware(claim.published_at) and aware(claim.effective_at)
            and claim.published_at <= claim.effective_at <= context.as_of
            and context.as_of - claim.effective_at <= timedelta(days=30)
        )
        if not valid_source:
            unreliable = True
            gaps.append("引用来源或时效未核实:" + ident)
            continue
        groups.add(claim.group_id)
        digest = sha256(claim.quote.encode("utf-8")).hexdigest()
        receipt_valid = any(
            receipt.claim_id == ident and receipt.option_id == option_id
            and receipt.quote_hash == digest and receipt.conclusion == "verified"
            and text(receipt.actor_id) and aware(receipt.recorded_at)
            and claim.published_at <= receipt.recorded_at <= context.as_of
            for receipt in context.receipts
        )
        if not receipt_valid:
            gaps.append("缺少当前人工凭据:" + ident)
        for key in claim.dimension_ids:
            if key in used and (key not in CRITICAL or
                    context.as_of - claim.effective_at <= timedelta(hours=48)):
                used[key].append(claim)
    if len(groups) < 2:
        gaps.append("实际引用独立非赔率证据组不足2")
    for key in WEIGHTS:
        if statuses[key] in {"verified", "partial"} and not used[key]:
            unreliable = True
            gaps.append("计分维度缺可靠来源:" + key)
    for key in CRITICAL:
        if statuses[key] != "verified" or not used[key]:
            gaps.append("关键域缺少48小时verified证据:" + key)
    score = None if unreliable else sum(
        weight if statuses[key] == "verified" else weight // 2 if statuses[key] == "partial" else 0
        for key, weight in WEIGHTS.items())
    return score, tuple(dict.fromkeys(gaps)), tuple(dict.fromkeys(blocks))
