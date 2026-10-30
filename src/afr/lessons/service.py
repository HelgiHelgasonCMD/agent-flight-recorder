"""Manual lessons, review, supersession and invalidation."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from afr.domain.models import (
    EvidenceLevel,
    Lesson,
    LessonOrigin,
    LessonStatus,
    OutcomeStatus,
    RootCauseStatus,
    VerificationScope,
)
from afr.persistence.store import Store
from afr.redaction import sanitize


def _clean(text: str | None, limit: int = 2000) -> str | None:
    return sanitize(text, limit).text if text else None


def _clean_list(items: list[str], limit: int = 1000) -> list[str]:
    return [sanitize(i, limit).text for i in items if i and i.strip()][:20]


def add_manual_lesson(
    store: Store,
    episode_ids: list[str],
    problem: str,
    *,
    lesson_id: str | None = None,
    created_at: datetime | None = None,
    observed_symptoms: list[str] | None = None,
    failed_approaches: list[str] | None = None,
    successful_approach: str | None = None,
    root_cause: str | None = None,
    root_cause_status: RootCauseStatus = RootCauseStatus.UNVERIFIED,
    preconditions: list[str] | None = None,
    applies_when: dict[str, str] | None = None,
    contraindications: list[str] | None = None,
    versions: dict[str, str] | None = None,
    supersedes: list[str] | None = None,
    origin: LessonOrigin = LessonOrigin.MANUAL_REVIEWED,
) -> Lesson:
    if not episode_ids:
        raise ValueError("a lesson must cite at least one source episode")
    episodes = []
    for eid in episode_ids:
        ep = store.get_episode(eid)
        if ep is None:
            raise KeyError(f"unknown episode {eid}")
        episodes.append(ep)
    primary = episodes[0]
    now = created_at or datetime.now(UTC)
    verifications = [v for e in episodes for v in store.verifications_for(e.episode_id)]
    task_ver = [v for v in verifications if v.scope == VerificationScope.TASK]
    passed = [v for v in task_ver if v.passed]
    rc_checked = any(
        v.scope == VerificationScope.ROOT_CAUSE
        and v.passed
        and v.claim
        and root_cause
        and v.claim.strip() == root_cause.strip()
        for v in verifications
    )
    # "verified" root cause can only come from a recorded discriminating check
    if root_cause_status == RootCauseStatus.VERIFIED and not rc_checked:
        root_cause_status = RootCauseStatus.SUPPORTED
    if rc_checked:
        root_cause_status = RootCauseStatus.VERIFIED

    sig_ids: list[str] = []
    error_types: set[str] = set()
    tools: set[str] = set()
    for ep in episodes:
        for ev in store.events_for_episode(ep.episode_id):
            if ev.signature_id and ev.signature_id not in sig_ids:
                sig_ids.append(ev.signature_id)
            if ev.tool:
                tools.add(ev.tool.tool_category)
    for sig in store.get_signatures(sig_ids):
        error_types.update(x for x in (sig.exception_type, sig.error_code) if x)

    status = primary.status
    if passed:
        basis = f"reviewed lesson; task verification {passed[-1].kind} passed"
    elif status == OutcomeStatus.VERIFIED_FAILURE:
        basis = "reviewed lesson; the recorded task check failed"
    else:
        basis = "reviewed lesson; no objective task verification on record"
    basis += f"; root cause {root_cause_status}"

    lid = lesson_id or "les_" + hashlib.sha256(f"{episode_ids}|{problem}|{now.isoformat()}".encode()).hexdigest()[:20]
    lesson = Lesson(
        lesson_id=lid,
        workspace=primary.workspace,
        repo_id=primary.repo_id,
        origin=origin,
        problem=_clean(problem) or "",
        observed_symptoms=_clean_list(observed_symptoms or []),
        failed_approaches=_clean_list(failed_approaches or []),
        successful_approach=_clean(successful_approach, 4000),
        successful_approach_missing_reason=None if successful_approach else "not provided by reviewer",
        root_cause=_clean(root_cause),
        root_cause_status=root_cause_status,
        preconditions=_clean_list(preconditions or []),
        applies_when={k: sanitize(v, 200).text for k, v in (applies_when or {}).items()},
        contraindications=_clean_list(contraindications or []),
        versions=dict(versions or primary.environment.versions),
        verification_ids=[v.evidence_id for v in verifications],
        task_status=status,
        source_episode_ids=list(episode_ids),
        signature_ids=sig_ids,
        error_types=sorted(error_types),
        tools=sorted(tools),
        language=primary.language,
        confidence_basis=basis,
        created_at=now,
        updated_at=now,
        verified_at=passed[-1].ts if passed else None,
    )
    store.upsert_lesson(lesson)
    for old_id in supersedes or []:
        supersede(store, old_id, lesson.lesson_id)
    # a reviewed lesson replaces the automatic candidate for the same episodes
    for ep in episodes:
        from afr.lessons.extract import auto_lesson_id

        auto = store.get_lesson(auto_lesson_id(ep.episode_id))
        if auto is not None and auto.status == LessonStatus.ACTIVE:
            supersede(store, auto.lesson_id, lesson.lesson_id)
        _raise_evidence_level(store, ep.episode_id)
    return lesson


def _raise_evidence_level(store: Store, episode_id: str) -> None:
    with store.engine.begin() as conn:
        store.update_episode(conn, episode_id, evidence_level=EvidenceLevel.REVIEWED_LESSON)


def supersede(store: Store, old_id: str, new_id: str) -> Lesson:
    old = store.get_lesson(old_id)
    if old is None:
        raise KeyError(f"unknown lesson {old_id}")
    old.status = LessonStatus.SUPERSEDED
    old.superseded_by = new_id
    old.updated_at = datetime.now(UTC)
    store.upsert_lesson(old)
    return old


def invalidate(store: Store, lesson_id: str, reason: str) -> Lesson:
    les = store.get_lesson(lesson_id)
    if les is None:
        raise KeyError(f"unknown lesson {lesson_id}")
    les.status = LessonStatus.INVALIDATED
    les.invalidated_reason = _clean(reason, 500)
    les.updated_at = datetime.now(UTC)
    store.upsert_lesson(les)
    return les
