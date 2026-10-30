"""Evidence-bounded lesson extraction.

Extraction never invents an explanation. It only restates observed actions:
- *failed approaches*: actions taken between two occurrences of the same failure;
- *successful approach*: actions taken after the last failure, reported only if a
  verification on record shows the task passed afterwards.
With metadata-only capture the actions are not visible, so the successful approach is
left empty with an explicit reason. A root cause is never extracted automatically.
"""

from __future__ import annotations

from datetime import datetime
from itertools import pairwise

from afr.domain.models import (
    Episode,
    Event,
    EventKind,
    EvidenceLevel,
    FailureSignature,
    Lesson,
    LessonOrigin,
    OutcomeStatus,
    RootCauseStatus,
    VerificationEvidence,
    VerificationScope,
)

MAX_ACTIONS = 6


def auto_lesson_id(episode_id: str) -> str:
    return "les_auto_" + episode_id.removeprefix("ep_")


def describe_action(ev: Event) -> str | None:
    tool = ev.tool
    if tool is None:
        return None
    if tool.tool_category in {"read", "search", "web", "agent"}:
        return None  # observation, not an attempted change
    if tool.command:
        return f"ran `{tool.command}`"
    if tool.files:
        return f"{tool.tool_name.lower()} {', '.join(tool.files[:4])}"
    if tool.program:
        return f"ran {tool.program} ({tool.tool_category})"
    return f"used {tool.tool_name} ({tool.tool_category})"


def _segment_actions(events: list[Event]) -> list[str]:
    out = []
    for ev in events:
        if ev.kind != EventKind.TOOL_RESULT:
            continue
        d = describe_action(ev)
        if d and d not in out:
            out.append(d)
    return out[:MAX_ACTIONS]


def extract(
    episode: Episode,
    events: list[Event],
    verifications: list[VerificationEvidence],
    signatures: dict[str, FailureSignature],
    now: datetime,
) -> Lesson | None:
    failures = [e for e in events if e.kind == EventKind.TOOL_FAILURE and e.signature_id]
    if not failures:
        return None
    primary_sig_id = max(
        {e.signature_id for e in failures if e.signature_id},
        key=lambda s: sum(1 for e in failures if e.signature_id == s),
    )
    primary = signatures.get(primary_sig_id)
    sig_ids = list(dict.fromkeys(e.signature_id for e in failures if e.signature_id))

    metadata_only = episode.evidence_level == EvidenceLevel.METADATA
    failed_approaches: list[str] = []
    successful: str | None = None
    missing: str | None = None

    order = sorted(events, key=lambda e: e.ts)
    fail_idx = [i for i, e in enumerate(order) if e.kind == EventKind.TOOL_FAILURE]
    for a, b in pairwise(fail_idx):
        if order[a].signature_id == order[b].signature_id:
            acts = _segment_actions(order[a + 1 : b])
            tool_b = order[b].tool
            failed_cmd = tool_b.command if tool_b else None
            if acts:
                failed_approaches.append("; ".join(acts) + " — the same failure recurred")
            elif failed_cmd:
                failed_approaches.append(f"re-ran `{failed_cmd}` unchanged — the same failure recurred")

    task_ver = [v for v in verifications if v.scope == VerificationScope.TASK]
    passed = [v for v in task_ver if v.passed]
    last_fail_ts = order[fail_idx[-1]].ts
    if episode.status == OutcomeStatus.VERIFIED_SUCCESS and passed:
        after = [e for e in order if e.ts > last_fail_ts]
        acts = _segment_actions(after)
        # the verifying re-run itself is evidence, not part of the approach
        acts = [a for a in acts if not any(v.command and v.command in a for v in passed)]
        if metadata_only:
            missing = "metadata-only capture: actions after the failure were not recorded"
        elif acts:
            successful = "; ".join(acts)
        else:
            missing = "verification passed but no state-changing action was observed after the last failure"
    elif episode.status == OutcomeStatus.CLAIMED_SUCCESS:
        missing = "agent claimed success but no objective verification is on record"
    else:
        missing = "no verified successful outcome for this episode"

    if successful and passed:
        origin = LessonOrigin.EVIDENCE_EXTRACTED
        v = passed[-1]
        basis = (
            f"{v.kind}: `{v.command or 'check'}` passed"
            + (f" (exit {v.exit_code})" if v.exit_code is not None else "")
            + "; approach restated from observed actions; root cause unverified"
        )
    else:
        origin = LessonOrigin.CANDIDATE
        basis = "candidate: " + (missing or "unreviewed")

    symptom_lines = []
    for sid in sig_ids:
        sig = signatures.get(sid)
        if sig:
            symptom_lines.append(sig.normalized_message)
    if not metadata_only:
        for e in failures:
            if e.error_excerpt:
                line = e.error_excerpt.strip().splitlines()[-1][:300]
                if line not in symptom_lines:
                    symptom_lines.append(line)

    if episode.task_summary:
        problem = episode.task_summary
    elif primary:
        label = primary.exception_type or primary.error_code or "failure"
        problem = f"{label} during {primary.tool_category}: {primary.normalized_message}"
    else:
        problem = "tool failure"

    return Lesson(
        lesson_id=auto_lesson_id(episode.episode_id),
        workspace=episode.workspace,
        repo_id=episode.repo_id,
        origin=origin,
        problem=problem,
        observed_symptoms=symptom_lines[:6],
        failed_approaches=failed_approaches[:6],
        successful_approach=successful,
        successful_approach_missing_reason=None if successful else missing,
        root_cause=None,
        root_cause_status=RootCauseStatus.UNVERIFIED,
        versions=dict(episode.environment.versions),
        verification_ids=[v.evidence_id for v in task_ver],
        task_status=episode.status,
        source_episode_ids=[episode.episode_id],
        signature_ids=sig_ids,
        error_types=sorted(
            {x for s in sig_ids if (sg := signatures.get(s)) for x in (sg.exception_type, sg.error_code) if x}
        ),
        tools=sorted({e.tool.tool_category for e in events if e.tool}),
        language=episode.language,
        confidence_basis=basis,
        created_at=now,
        updated_at=now,
        verified_at=passed[-1].ts if passed else None,
    )
