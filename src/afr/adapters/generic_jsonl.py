"""Generic JSONL event importer (``afr.generic-event.v1``).

One JSON object per line, validated against ``schemas/generic-event-v1.schema.json``.
Text in imported records is untrusted data: it is redacted, bounded and stored, never
interpreted as instructions or executed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

from afr import schemas
from afr.config import CaptureLevel
from afr.domain.models import (
    EventKind,
    LessonOrigin,
    RootCauseStatus,
    VerificationEvidence,
    VerificationKind,
    VerificationScope,
)
from afr.ingest.pipeline import Pipeline
from afr.ingest.raw import RawEvent
from afr.lessons.service import add_manual_lesson
from afr.verification import record

SCHEMA_ID = "afr.generic-event.v1"
_EVENT_KINDS = {k.value for k in EventKind} - {"verification", "note"}


def load_schema() -> dict[str, Any]:
    return schemas.load("generic-event-v1.schema.json")


@dataclass
class ImportReport:
    lines: int = 0
    stored: int = 0
    deduped: int = 0
    verifications: int = 0
    lessons: int = 0
    invalid: list[str] = field(default_factory=list)


def _ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _to_raw(rec: dict[str, Any], default_workspace: str | None) -> RawEvent:
    tool = rec.get("tool") or {}
    kind = EventKind(rec["kind"])
    return RawEvent(
        source="generic_jsonl",
        agent=rec["agent"],
        session_id=rec["session_id"],
        kind=kind,
        ts=_ts(rec["ts"]),
        dedupe_basis=rec["event_id"],
        cwd=rec.get("cwd"),
        workspace=rec.get("workspace") or default_workspace,
        repo_id=rec.get("repo"),
        episode_id=rec.get("episode_id"),
        tool_name=tool.get("name"),
        command=tool.get("command"),
        exit_code=tool.get("exit_code"),
        succeeded=(tool.get("exit_code") == 0) if "exit_code" in tool else (kind == EventKind.TOOL_RESULT),
        duration_ms=tool.get("duration_ms"),
        files=list(tool.get("files") or []),
        error_text=rec.get("error"),
        claim_text=rec.get("assistant_claim"),
        task_summary=rec.get("task_summary"),
        task_family=rec.get("task_family"),
        language=rec.get("language"),
        versions=dict(rec.get("versions") or {}),
        git_head=rec.get("git_head"),
    )


def import_records(
    pipeline: Pipeline,
    records: Iterable[tuple[int, dict[str, Any]]],
    capture_level: CaptureLevel = CaptureLevel.SANITIZED_EVIDENCE,
    workspace: str | None = None,
) -> ImportReport:
    schema = load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    report = ImportReport()
    for lineno, rec in records:
        report.lines += 1
        errors = sorted(validator.iter_errors(rec), key=lambda e: list(e.path))
        if errors:
            report.invalid.append(f"line {lineno}: {errors[0].message}")
            continue
        kind = rec["kind"]
        if kind in _EVENT_KINDS:
            res = pipeline.ingest(_to_raw(rec, workspace), capture_level)
            if res.status == "stored":
                report.stored += 1
            else:
                report.deduped += 1
        elif kind == "verification":
            if _already(pipeline, rec["event_id"]):
                report.deduped += 1
                continue
            v = rec["verification"]
            ev = VerificationEvidence(
                evidence_id="ver_" + hashlib.sha256(rec["event_id"].encode()).hexdigest()[:20],
                episode_id=rec["episode_id"],
                kind=VerificationKind(v["kind"]),
                scope=VerificationScope(v.get("scope", "task")),
                command=v.get("command"),
                exit_code=v.get("exit_code"),
                passed=v["passed"],
                ts=_ts(rec["ts"]),
                claim=v.get("claim"),
                note=(v.get("note") or "") + " [imported record]",
            )
            ev.environment.git_head = rec.get("git_head")
            ev.environment.versions = dict(rec.get("versions") or {})
            record(pipeline, ev, v.get("lesson_id"))
            report.verifications += 1
        elif kind == "lesson":
            les = rec["lesson"]
            lesson_id = les.get("lesson_id") or "les_" + hashlib.sha256(rec["event_id"].encode()).hexdigest()[:20]
            if pipeline.store.get_lesson(lesson_id) is not None:
                report.deduped += 1
                continue
            add_manual_lesson(
                pipeline.store,
                episode_ids=[rec["episode_id"]],
                lesson_id=lesson_id,
                created_at=_ts(rec["ts"]),
                problem=les["problem"],
                observed_symptoms=les.get("observed_symptoms", []),
                failed_approaches=les.get("failed_approaches", []),
                successful_approach=les.get("successful_approach"),
                root_cause=les.get("root_cause"),
                root_cause_status=RootCauseStatus(les.get("root_cause_status", "unverified")),
                preconditions=les.get("preconditions", []),
                applies_when=les.get("applies_when", {}),
                contraindications=les.get("contraindications", []),
                versions=les.get("versions", {}),
                supersedes=les.get("supersedes", []),
                origin=LessonOrigin.MANUAL_REVIEWED,
            )
            report.lessons += 1
    return report


def _already(pipeline: Pipeline, event_id: str) -> bool:
    ev_id = "ver_" + hashlib.sha256(event_id.encode()).hexdigest()[:20]
    from sqlalchemy import select

    from afr.persistence import tables as t

    with pipeline.store.engine.connect() as c:
        return (
            c.execute(select(t.verifications.c.evidence_id).where(t.verifications.c.evidence_id == ev_id)).first()
            is not None
        )


def read_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open() as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            yield i, json.loads(line)


def import_file(
    pipeline: Pipeline,
    path: Path,
    capture_level: CaptureLevel = CaptureLevel.SANITIZED_EVIDENCE,
    workspace: str | None = None,
) -> ImportReport:
    return import_records(pipeline, read_jsonl(path), capture_level, workspace)
