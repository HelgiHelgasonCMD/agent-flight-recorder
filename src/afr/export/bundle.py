"""experience-bundle.v1 export/import (schema: schemas/experience-bundle-v1.schema.json)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import jsonschema
from sqlalchemy import insert

from afr import __version__, schemas
from afr.domain.models import (
    EnvironmentFingerprint,
    Episode,
    Event,
    EventKind,
    FailureSignature,
    Lesson,
    ToolCall,
    VerificationEvidence,
)
from afr.persistence.store import Store
from afr.redaction import RULES_VERSION, redact_obj

SCHEMA_FILE = "experience-bundle-v1.schema.json"


class BundleValidationError(ValueError):
    pass


def validate(bundle: dict[str, Any]) -> None:
    validator = jsonschema.Draft202012Validator(
        schemas.load(SCHEMA_FILE), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    errors = sorted(validator.iter_errors(bundle), key=lambda e: list(e.absolute_path))
    if errors:
        e = errors[0]
        loc = "/".join(str(p) for p in e.absolute_path)
        raise BundleValidationError(f"{loc or '<root>'}: {e.message} ({len(errors)} error(s))")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _lesson(les: Lesson) -> dict[str, Any]:
    return {
        "lesson_id": les.lesson_id,
        "origin": str(les.origin),
        "status": str(les.status),
        "problem": les.problem,
        "observed_symptoms": les.observed_symptoms,
        "failed_approaches": les.failed_approaches,
        "successful_approach": les.successful_approach,
        "successful_approach_missing_reason": les.successful_approach_missing_reason,
        "root_cause": les.root_cause,
        "root_cause_status": str(les.root_cause_status),
        "preconditions": les.preconditions,
        "applies_when": les.applies_when,
        "contraindications": les.contraindications,
        "versions": les.versions,
        "verification": {
            "task_status": str(les.task_status),
            "evidence_ids": les.verification_ids,
            "verified_at": _iso(les.verified_at),
        },
        "source_episode_ids": les.source_episode_ids,
        "signature_ids": les.signature_ids,
        "error_types": les.error_types,
        "tools": les.tools,
        "language": les.language,
        "confidence_basis": les.confidence_basis,
        "superseded_by": les.superseded_by,
        "invalidated_reason": les.invalidated_reason,
        "created_at": les.created_at.isoformat(),
        "updated_at": les.updated_at.isoformat(),
    }


def export_bundle(
    store: Store,
    workspace: str,
    episode_ids: list[str] | None = None,
    include_excerpts: bool = False,
    include_invalidated: bool = False,
) -> dict[str, Any]:
    episodes = [e for e in store.list_episodes(workspace) if episode_ids is None or e.episode_id in episode_ids]
    lessons = store.list_lessons(workspace)
    out_eps = []
    for ep in episodes:
        events = store.events_for_episode(ep.episode_id)
        sig_map = {s.signature_id: s for s in store.get_signatures(e.signature_id for e in events if e.signature_id)}
        observations = []
        attempts = []
        for ev in events:
            if ev.signature_id and ev.signature_id in sig_map:
                sig = sig_map[ev.signature_id]
                observations.append(
                    {
                        "event_id": ev.event_id,
                        "ts": ev.ts.isoformat(),
                        "excerpt": ev.error_excerpt if include_excerpts else None,
                        "signature": sig.model_dump(mode="json"),
                    }
                )
            if ev.tool is not None:
                attempts.append(
                    {
                        "event_id": ev.event_id,
                        "ts": ev.ts.isoformat(),
                        "tool_name": ev.tool.tool_name,
                        "tool_category": ev.tool.tool_category,
                        "program": ev.tool.program,
                        "command": ev.tool.command if include_excerpts else None,
                        "files": ev.tool.files if include_excerpts else [],
                        "exit_code": ev.tool.exit_code,
                        "succeeded": ev.tool.succeeded,
                        "duration_ms": ev.tool.duration_ms,
                        "signature_id": ev.signature_id,
                    }
                )
        evidence = [
            {
                "evidence_id": v.evidence_id,
                "kind": str(v.kind),
                "scope": str(v.scope),
                "command": v.command,
                "exit_code": v.exit_code,
                "passed": v.passed,
                "ts": v.ts.isoformat(),
                "revision": v.environment.git_head,
                "worktree_fingerprint": v.environment.worktree_fingerprint,
                "failure_signature_ids": v.failure_signature_ids,
                "supporting_event_ids": v.supporting_event_ids,
                "claim": v.claim,
                "note": v.note,
            }
            for v in store.verifications_for(ep.episode_id)
        ]
        ep_lessons = [
            _lesson(les)
            for les in lessons
            if ep.episode_id in les.source_episode_ids
            and les.source_episode_ids[0] == ep.episode_id  # each lesson exported once, under its primary episode
            and (include_invalidated or str(les.status) != "invalidated")
        ]
        out_eps.append(
            {
                "episode_id": ep.episode_id,
                "task_summary": ep.task_summary,
                "task_family": ep.task_family,
                "agent": ep.agent,
                "language": ep.language,
                "evidence_level": str(ep.evidence_level),
                "started_at": ep.started_at.isoformat(),
                "ended_at": _iso(ep.ended_at),
                "environment": {
                    "repo_id": ep.repo_id,
                    "revision": ep.environment.git_head,
                    "worktree_fingerprint": ep.environment.worktree_fingerprint,
                    "os": ep.environment.os,
                    "versions": ep.environment.versions,
                },
                "observations": observations,
                "attempts": attempts,
                "outcome": {
                    "status": str(ep.status),
                    "root_cause_status": str(ep.root_cause_status),
                    "evidence": evidence,
                },
                "lessons": ep_lessons,
                "provenance": {
                    "source": events[0].source if events else "unknown",
                    "session_id": ep.session_id,
                    "event_ids": sorted(e.event_id for e in events),
                },
            }
        )
    bundle: dict[str, Any] = {
        "schema_version": "experience-bundle.v1",
        "export_id": f"exp_{uuid.uuid4().hex[:20]}",
        "created_at": datetime.now(UTC).isoformat(),
        "producer": {"name": "agent-flight-recorder", "version": __version__},
        "workspace": workspace,
        "redaction": {"applied": True, "rules_version": RULES_VERSION, "excerpts_included": include_excerpts},
        "episodes": out_eps,
    }
    # Export boundary: everything was redacted at ingest; redact again in case rules improved.
    bundle["episodes"], _ = redact_obj(bundle["episodes"])
    validate(bundle)
    return bundle


def _dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def import_bundle(store: Store, bundle: dict[str, Any], workspace: str | None = None) -> dict[str, int]:
    """Load a bundle into the store, preserving ids. Existing ids are skipped."""
    validate(bundle)
    counts = {"episodes": 0, "events": 0, "verifications": 0, "lessons": 0, "skipped": 0}
    ws = workspace or bundle.get("workspace") or store.settings.workspace
    from afr.persistence import tables as t

    for e in bundle["episodes"]:
        if store.get_episode(e["episode_id"]) is not None:
            counts["skipped"] += 1
            continue
        env = e["environment"]
        started = _dt(e["started_at"])
        assert started is not None
        ep = Episode(
            episode_id=e["episode_id"],
            session_id=e["provenance"].get("session_id") or f"imported-{e['episode_id']}",
            workspace=ws,
            repo_id=env["repo_id"],
            agent=e.get("agent", "unknown"),
            task_summary=e["task_summary"],
            task_family=e.get("task_family"),
            started_at=started,
            ended_at=_dt(e.get("ended_at")),
            status=e["outcome"]["status"],
            root_cause_status=e["outcome"]["root_cause_status"],
            evidence_level=e.get("evidence_level", "metadata"),
            environment=EnvironmentFingerprint(
                os=env.get("os"),
                repo_id=env["repo_id"],
                git_head=env["revision"],
                worktree_fingerprint=env.get("worktree_fingerprint"),
                versions=env["versions"],
            ),
            language=e.get("language"),
        )
        obs = {o["event_id"]: o for o in e["observations"]}
        with store.engine.begin() as conn:
            store.insert_episode(conn, ep, is_open=False)
            for o in e["observations"]:
                store.upsert_signature(conn, FailureSignature.model_validate(o["signature"]), _dt(o["ts"]) or started)
            for a in e["attempts"]:
                failed = a["succeeded"] is False
                ev = Event(
                    event_id=a["event_id"],
                    dedupe_key=f"bundle:{a['event_id']}",
                    source=e["provenance"]["source"],
                    session_id=ep.session_id,
                    episode_id=ep.episode_id,
                    kind=EventKind.TOOL_FAILURE if failed else EventKind.TOOL_RESULT,
                    ts=_dt(a["ts"]) or started,
                    workspace=ws,
                    repo_id=ep.repo_id,
                    tool=ToolCall(
                        tool_name=a["tool_name"],
                        tool_category=a["tool_category"],
                        program=a.get("program"),
                        command=a.get("command"),
                        exit_code=a.get("exit_code"),
                        succeeded=a.get("succeeded"),
                        duration_ms=a.get("duration_ms"),
                        files=a.get("files", []),
                    ),
                    signature_id=a.get("signature_id"),
                    error_excerpt=(obs.get(a["event_id"]) or {}).get("excerpt"),
                    capture_level="imported",
                    data={"imported_from": bundle["export_id"]},
                )
                store.insert_event(conn, ev)
                counts["events"] += 1
            attempt_ids = {a["event_id"] for a in e["attempts"]}
            for pid in e["provenance"]["event_ids"]:
                if pid in attempt_ids:
                    continue
                # non-tool source events (prompt/stop/...) are referenced, not exported
                store.insert_event(
                    conn,
                    Event(
                        event_id=pid,
                        dedupe_key=f"bundle:{pid}",
                        source=e["provenance"]["source"],
                        session_id=ep.session_id,
                        episode_id=ep.episode_id,
                        kind=EventKind.NOTE,
                        ts=started,
                        workspace=ws,
                        repo_id=ep.repo_id,
                        capture_level="imported",
                        data={"imported_from": bundle["export_id"], "provenance_only": True},
                    ),
                )
            for v in e["outcome"]["evidence"]:
                store.add_verification(
                    conn,
                    VerificationEvidence(
                        evidence_id=v["evidence_id"],
                        episode_id=ep.episode_id,
                        kind=v["kind"],
                        scope=v["scope"],
                        command=v.get("command"),
                        exit_code=v.get("exit_code"),
                        passed=v["passed"],
                        ts=_dt(v["ts"]) or started,
                        environment=EnvironmentFingerprint(
                            git_head=v.get("revision"), worktree_fingerprint=v.get("worktree_fingerprint")
                        ),
                        failure_signature_ids=v.get("failure_signature_ids", []),
                        supporting_event_ids=v.get("supporting_event_ids", []),
                        claim=v.get("claim"),
                        note=v.get("note"),
                    ),
                )
                counts["verifications"] += 1
            conn.execute(
                insert(t.sessions)
                .prefix_with("OR IGNORE")
                .values(
                    session_id=ep.session_id,
                    source=e["provenance"]["source"],
                    agent=ep.agent,
                    workspace=ws,
                    repo_id=ep.repo_id,
                    started_at=started,
                )
            )
        for les in e["lessons"]:
            if store.get_lesson(les["lesson_id"]) is not None:
                continue
            ver = les["verification"]
            store.upsert_lesson(
                Lesson(
                    lesson_id=les["lesson_id"],
                    workspace=ws,
                    repo_id=ep.repo_id,
                    origin=les["origin"],
                    status=les["status"],
                    problem=les["problem"],
                    observed_symptoms=les["observed_symptoms"],
                    failed_approaches=les["failed_approaches"],
                    successful_approach=les["successful_approach"],
                    successful_approach_missing_reason=les.get("successful_approach_missing_reason"),
                    root_cause=les.get("root_cause"),
                    root_cause_status=les.get("root_cause_status", "unverified"),
                    preconditions=les["preconditions"],
                    applies_when=les.get("applies_when", {}),
                    contraindications=les["contraindications"],
                    versions=les["versions"],
                    verification_ids=ver["evidence_ids"],
                    task_status=ver["task_status"],
                    source_episode_ids=les["source_episode_ids"],
                    signature_ids=les.get("signature_ids", []),
                    error_types=les.get("error_types", []),
                    tools=les.get("tools", []),
                    language=les.get("language"),
                    confidence_basis=les["confidence_basis"],
                    superseded_by=les.get("superseded_by"),
                    invalidated_reason=les.get("invalidated_reason"),
                    created_at=_dt(les.get("created_at")) or started,
                    updated_at=_dt(les.get("updated_at")) or started,
                    verified_at=_dt(ver.get("verified_at")),
                )
            )
            counts["lessons"] += 1
        counts["episodes"] += 1
    return counts
