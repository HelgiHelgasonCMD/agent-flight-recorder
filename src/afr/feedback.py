"""Retrieval feedback: did consulting a lesson precede an objectively verified outcome?"""

from __future__ import annotations

from datetime import UTC, datetime

from afr.domain.models import OutcomeStatus, RetrievalFeedback
from afr.ingest.pipeline import new_id
from afr.persistence.store import Store
from afr.redaction import sanitize

REPORTED = ("succeeded", "failed", "abandoned", "unknown")


def report(
    store: Store,
    workspace: str,
    task_ref: str,
    consulted_lesson_ids: list[str],
    retrieved_ids: list[str],
    reported_outcome: str,
    episode_id: str | None = None,
    note: str | None = None,
) -> RetrievalFeedback:
    if reported_outcome not in REPORTED:
        raise ValueError(f"reported_outcome must be one of {REPORTED}")
    outcome = OutcomeStatus.UNKNOWN
    evidence_ids: list[str] = []
    if episode_id:
        ep = store.get_episode(episode_id)
        if ep is None or ep.workspace != workspace:
            raise KeyError(f"unknown episode {episode_id} in workspace {workspace}")
        outcome = ep.status
        evidence_ids = [v.evidence_id for v in store.verifications_for(episode_id)]
    known = {les.lesson_id for les in store.list_lessons(workspace)}
    fb = RetrievalFeedback(
        feedback_id=new_id("fb"),
        workspace=workspace,
        task_ref=sanitize(task_ref, 200).text,
        episode_id=episode_id,
        retrieved_ids=[r[:200] for r in retrieved_ids][:50],
        consulted_lesson_ids=[c for c in consulted_lesson_ids if c in known][:50],
        outcome=outcome,
        reported_outcome=reported_outcome,
        evidence_ids=evidence_ids,
        note=sanitize(note, 1000).text if note else None,
        ts=datetime.now(UTC),
    )
    store.add_feedback(fb)
    return fb
