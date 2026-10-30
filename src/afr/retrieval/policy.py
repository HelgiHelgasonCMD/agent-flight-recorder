"""Post-fusion applicability policy.

Works on ranks and evidence, never on raw scores: every candidate gets a *tier* from its
evidence and applicability, and is demoted by a bounded number of rank positions
(``RANK_PENALTY[tier]``). The final order is ``fused_rank + penalty`` (ties: tier, then
fused rank). A bounded penalty, rather than sorting by tier first, keeps relevance in
charge: an irrelevant-but-verified record cannot leapfrog a relevant record that merely
carries a caution, while a known-inapplicable fix still drops below nearby applicable
ones. Cautions are always returned so the caller can see why something was demoted.

Tier 0: verified fix, applicable in the current context.
Tier 1: verified fix with soft concerns (stale evidence, from another project).
Tier 2: no verified fix (unverified/claimed/candidate, failed attempt, different error).
Tier 3: known-inapplicable (precondition or major-version mismatch, superseded,
        contradicted by a later failure).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from afr.episodes.signatures import extract_code, extract_exception
from afr.retrieval.tokenize import tokenize


@dataclass
class QueryContext:
    repo_id: str | None = None
    os: str | None = None
    versions: dict[str, str] = field(default_factory=dict)
    now: datetime | None = None


@dataclass
class Caution:
    code: str
    detail: str
    tier: int

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


def _major(v: str) -> int | None:
    m = re.match(r"\s*v?(\d+)", v)
    return int(m.group(1)) if m else None


def _spec_ok(spec: str, actual: str) -> bool | None:
    try:
        return Version(actual) in SpecifierSet(spec)
    except (InvalidSpecifier, InvalidVersion):
        return None


def assess(
    payload: dict[str, Any],
    lesson: Any | None,
    ctx: QueryContext,
    query: str,
    stale_after_days: int,
    others: list[dict[str, Any]],
) -> list[Caution]:
    cautions: list[Caution] = []
    task_status = payload.get("task_status")
    has_fix = bool(payload.get("has_fix"))

    if payload.get("status") == "superseded":
        by = getattr(lesson, "superseded_by", None)
        cautions.append(Caution("superseded", f"superseded by {by}" if by else "superseded", 3))

    if payload.get("doc_type") == "episode":
        if task_status == "verified_failure":
            cautions.append(
                Caution("failed_attempt", "this attempt failed verification; use it as what NOT to repeat", 2)
            )
        else:
            cautions.append(Caution("no_lesson", "raw episode without a reviewed or verified lesson", 2))
    elif task_status == "verified_failure":
        cautions.append(Caution("failed_attempt", "the recorded check failed after this approach", 2))
    elif task_status == "claimed_success":
        cautions.append(Caution("unverified", "success was claimed by the agent but never objectively verified", 2))
    elif task_status != "verified_success":
        cautions.append(Caution("unverified", "no objective verification on record", 2))
    elif not has_fix:
        cautions.append(Caution("no_fix_recorded", "verified outcome but the fix itself was not captured", 2))

    if payload.get("origin") == "candidate":
        cautions.append(Caution("candidate", "automatically extracted candidate, not reviewed", 2))

    now = ctx.now or datetime.now(UTC)
    vts = payload.get("verified_ts")
    if vts is not None:
        age_days = (now.timestamp() - float(vts)) / 86400
        if age_days > stale_after_days:
            cautions.append(Caution("stale", f"evidence is {int(age_days)} days old", 1))

    if ctx.repo_id and payload.get("repo_id") and payload["repo_id"] != ctx.repo_id:
        cautions.append(Caution("other_project", "recorded in a different repository", 1))

    applies = getattr(lesson, "applies_when", {}) or {}
    for key, want in applies.items():
        if key == "os":
            if ctx.os and ctx.os.lower() != want.lower():
                cautions.append(Caution("precondition_mismatch", f"requires os={want}, current os={ctx.os}", 3))
        elif key in ctx.versions:
            ok = _spec_ok(want, ctx.versions[key])
            if ok is False:
                cautions.append(
                    Caution("precondition_mismatch", f"requires {key}{want}, current {key} {ctx.versions[key]}", 3)
                )

    for name, ver in (payload.get("versions") or {}).items():
        if name in ctx.versions and name not in applies:
            a, b = _major(str(ver)), _major(ctx.versions[name])
            if a is not None and b is not None and a != b:
                cautions.append(
                    Caution("version_mismatch", f"verified with {name} {ver}, current {name} {ctx.versions[name]}", 3)
                )

    q_codes = {x for x in (extract_code(query), extract_exception(query)) if x}
    d_codes = set(payload.get("error_types") or [])
    if q_codes and d_codes and not (q_codes & d_codes):
        cautions.append(
            Caution("different_error", f"query error {sorted(q_codes)} differs from recorded {sorted(d_codes)}", 2)
        )

    fix = getattr(lesson, "successful_approach", None)
    if fix:
        fix_tokens = set(tokenize(fix))
        for other in others:
            if other.get("doc_id") == payload.get("doc_id"):
                continue
            for failed in other.get("_failed_approaches", []):
                ft = set(tokenize(failed))
                if fix_tokens and ft and len(fix_tokens & ft) / len(fix_tokens | ft) >= 0.6:
                    later = (other.get("created_ts") or 0) > (payload.get("created_ts") or 0)
                    if later:
                        cautions.append(
                            Caution("contradicted", f"the same approach later failed ({other.get('doc_id')})", 3)
                        )
                        break
    return cautions


RANK_PENALTY = {0: 0, 1: 1, 2: 3, 3: 6}


def tier_of(cautions: list[Caution]) -> int:
    return max((c.tier for c in cautions), default=0)


def policy_key(tier: int, fused_rank: int) -> tuple[int, int, int]:
    return (fused_rank + RANK_PENALTY[tier], tier, fused_rank)
