"""Build retrievable documents from lessons and episodes.

Text is redacted again at this boundary (defense in depth) before it is embedded or
stored as Qdrant payload.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from afr.domain.models import EventKind, Lesson, LessonOrigin, LessonStatus
from afr.persistence.store import Store
from afr.redaction import redact

MAX_DOC_CHARS = 4000


@dataclass
class Doc:
    doc_id: str
    doc_type: str
    workspace: str
    text: str
    payload: dict[str, Any]

    @property
    def content_hash(self) -> str:
        blob = json.dumps({"t": self.text, "p": self.payload}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _ts(dt: Any) -> float | None:
    return dt.timestamp() if dt is not None else None


def lesson_doc(les: Lesson) -> Doc:
    parts = [les.problem]
    parts += [f"symptom: {s}" for s in les.observed_symptoms]
    parts += [f"failed approach: {f}" for f in les.failed_approaches]
    if les.successful_approach:
        parts.append(f"fix: {les.successful_approach}")
    if les.root_cause:
        parts.append(f"root cause: {les.root_cause}")
    parts += [f"precondition: {p}" for p in les.preconditions]
    parts += [f"not applicable: {c}" for c in les.contraindications]
    parts += les.error_types
    parts += [f"{k} {v}" for k, v in les.versions.items()]
    text = redact("\n".join(parts)).text[:MAX_DOC_CHARS]
    payload = {
        "doc_type": "lesson",
        "workspace": les.workspace,
        "repo_id": les.repo_id,
        "lesson_id": les.lesson_id,
        "episode_ids": les.source_episode_ids,
        "status": str(les.status),
        "origin": str(les.origin),
        "task_status": str(les.task_status),
        "root_cause_status": str(les.root_cause_status),
        "language": les.language,
        "tools": les.tools,
        "error_types": les.error_types,
        "signature_ids": les.signature_ids,
        "versions": les.versions,
        "versions_flat": [f"{k}={v}" for k, v in les.versions.items()],
        "created_ts": _ts(les.created_at),
        "verified_ts": _ts(les.verified_at),
        "has_fix": bool(les.successful_approach),
        "text": text,
    }
    return Doc(f"lesson:{les.lesson_id}", "lesson", les.workspace, text, payload)


def episode_doc(store: Store, episode_id: str) -> Doc | None:
    ep = store.get_episode(episode_id)
    if ep is None:
        return None
    events = store.events_for_episode(episode_id)
    failures = [e for e in events if e.kind == EventKind.TOOL_FAILURE]
    if not failures:
        return None
    sigs = store.get_signatures(e.signature_id for e in failures if e.signature_id)
    parts = [ep.task_summary or ""]
    parts += [s.normalized_message for s in sigs]
    for e in failures:
        if e.error_excerpt:
            parts.append(e.error_excerpt.strip().splitlines()[-1])
        if e.tool and e.tool.command:
            parts.append(f"failed command: {e.tool.command}")
    parts += [x for s in sigs for x in (s.exception_type, s.error_code) if x]
    parts += [f"{k} {v}" for k, v in ep.environment.versions.items()]
    text = redact("\n".join(p for p in parts if p)).text[:MAX_DOC_CHARS]
    payload = {
        "doc_type": "episode",
        "workspace": ep.workspace,
        "repo_id": ep.repo_id,
        "lesson_id": None,
        "episode_ids": [ep.episode_id],
        "status": str(LessonStatus.ACTIVE),
        "origin": "episode",
        "task_status": str(ep.status),
        "root_cause_status": str(ep.root_cause_status),
        "language": ep.language,
        "tools": sorted({e.tool.tool_category for e in events if e.tool}),
        "error_types": sorted({x for s in sigs for x in (s.exception_type, s.error_code) if x}),
        "signature_ids": [s.signature_id for s in sigs],
        "versions": ep.environment.versions,
        "versions_flat": [f"{k}={v}" for k, v in ep.environment.versions.items()],
        "created_ts": _ts(ep.ended_at or ep.started_at),
        "verified_ts": None,
        "has_fix": False,
        "text": text,
    }
    return Doc(f"episode:{ep.episode_id}", "episode", ep.workspace, text, payload)


def build_documents(store: Store, workspace: str | None = None) -> list[Doc]:
    docs: list[Doc] = []
    covered: set[str] = set()
    lessons = store.list_lessons(workspace)
    by_id = {les.lesson_id: les for les in lessons}
    for les in lessons:
        if les.status == LessonStatus.INVALIDATED:
            continue
        replacement = by_id.get(les.superseded_by or "")
        if (
            les.status == LessonStatus.SUPERSEDED
            and les.origin != LessonOrigin.MANUAL_REVIEWED
            and replacement is not None
            and set(les.source_episode_ids) <= set(replacement.source_episode_ids)
        ):
            continue  # an automatic draft replaced by a reviewed lesson for the same episode
        docs.append(lesson_doc(les))
        if les.status == LessonStatus.ACTIVE:
            covered.update(les.source_episode_ids)
    for ep in store.list_episodes(workspace):
        if ep.episode_id in covered:
            continue
        d = episode_doc(store, ep.episode_id)
        if d is not None:
            docs.append(d)
    return docs
