"""Ingestion: dedupe → capture-level policy → redaction/limits → episode assignment → persist.

Everything that reaches SQLite has passed through ``afr.redaction``. Raw command lines,
error text and file paths are dropped entirely at the ``metadata`` capture level; only
derived, allowlisted fields (program name, category, exit status, a redacted+normalized
failure signature, keyed hashes) remain.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Connection

from afr.config import CaptureLevel, Settings
from afr.domain.models import (
    EnvironmentFingerprint,
    Episode,
    Event,
    EventKind,
    EvidenceLevel,
    FailureSignature,
    OutcomeStatus,
    Session,
    ToolCall,
    VerificationEvidence,
    VerificationKind,
)
from afr.episodes.signatures import build_signature
from afr.episodes.tools import categorize, normalize_command, program_of
from afr.ingest.raw import RawEvent
from afr.lessons.extract import extract
from afr.persistence.db import with_retry
from afr.persistence.store import Store, utcnow
from afr.redaction import keyed_hash, redact, sanitize

CLAIM_RE = re.compile(
    r"\b(fixed|resolved|works now|is working|now passes?|all tests pass(?:ed)?|tests? (?:are )?passing|"
    r"should (?:now )?work|problem solved|issue solved)\b",
    re.IGNORECASE,
)
_ALLOWED_DATA_TYPES = (str, int, float, bool)


@dataclass
class IngestResult:
    status: str  # stored | deduped
    event_id: str | None
    episode_id: str | None
    signature_id: str | None = None


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class Pipeline:
    def __init__(self, store: Store, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or store.settings

    # -- helpers ---------------------------------------------------------------
    def _dedupe_key(self, raw: RawEvent) -> str:
        basis = f"{raw.source}|{raw.session_id}|{raw.kind}|{raw.dedupe_basis}"
        return hashlib.sha256(basis.encode()).hexdigest()

    def _files(self, raw: RawEvent) -> list[str]:
        out = []
        cwd = Path(raw.cwd) if raw.cwd else None
        for f in raw.files[:20]:
            p = Path(f)
            rel: str | None = None
            if cwd and p.is_absolute():
                try:
                    rel = str(p.relative_to(cwd))
                except ValueError:
                    rel = None
            elif not p.is_absolute():
                rel = f
            if rel is not None:
                out.append(redact(rel).text[:200])
            else:
                out.append("ext:" + keyed_hash(f, self.store.salt))
        return out

    @staticmethod
    def _clean_data(data: dict[str, object]) -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in data.items():
            if isinstance(v, _ALLOWED_DATA_TYPES):
                out[str(k)[:64]] = redact(v).text[:200] if isinstance(v, str) else v
        return out

    # -- main entry ------------------------------------------------------------
    def ingest(self, raw: RawEvent, capture_level: CaptureLevel | None = None) -> IngestResult:
        level = capture_level or self.settings.capture_level
        sanitized = level == CaptureLevel.SANITIZED_EVIDENCE
        workspace = raw.workspace or self.settings.workspace
        dedupe_key = self._dedupe_key(raw)
        limit = self.settings.max_excerpt_chars

        tool: ToolCall | None = None
        command_hash: str | None = None
        if raw.tool_name:
            category = categorize(raw.tool_name, raw.command)
            cmd = sanitize(raw.command, 400).text if (sanitized and raw.command) else None
            if raw.command:
                command_hash = keyed_hash(normalize_command(redact(raw.command).text), self.store.salt)
            tool = ToolCall(
                tool_name=raw.tool_name[:128],
                tool_category=category,
                tool_use_id=raw.tool_use_id,
                program=program_of(raw.command) if raw.command else None,
                command=cmd,
                exit_code=raw.exit_code,
                succeeded=raw.succeeded,
                duration_ms=raw.duration_ms,
                files=self._files(raw) if sanitized else [],
            )

        signature: FailureSignature | None = None
        excerpt: str | None = None
        redactions = 0
        if raw.error_text:
            red = redact(raw.error_text)
            redactions += red.count
            signature = build_signature(tool.tool_category if tool else "other", red.text, raw.versions)
            if sanitized:
                excerpt = sanitize(red.text, limit).text
        claim = bool(raw.claim_text and CLAIM_RE.search(raw.claim_text))

        data = self._clean_data(raw.data)
        if command_hash:
            data["command_hash"] = command_hash

        def _tx() -> IngestResult:
            with self.store.engine.begin() as conn:
                self.store.bump(conn, "received")
                if self.store.event_exists(conn, dedupe_key):
                    self.store.bump(conn, "deduped")
                    return IngestResult("deduped", None, None)
                result = self._persist(
                    conn, raw, level, workspace, dedupe_key, tool, signature, excerpt, claim, redactions, data
                )
                self.store.bump(conn, "stored")
                return result

        try:
            return with_retry(_tx)
        except Exception:
            with self.store.engine.begin() as conn:
                self.store.bump(conn, "failed")
            raise

    def _persist(
        self,
        conn: Connection,
        raw: RawEvent,
        level: CaptureLevel,
        workspace: str,
        dedupe_key: str,
        tool: ToolCall | None,
        signature: FailureSignature | None,
        excerpt: str | None,
        claim: bool,
        redactions: int,
        data: dict[str, object],
    ) -> IngestResult:
        store = self.store
        ts = raw.ts
        store.upsert_session(
            conn,
            Session(
                session_id=raw.session_id,
                source=raw.source,
                agent=raw.agent,
                workspace=workspace,
                repo_id=raw.repo_id,
                started_at=ts,
                ended_at=ts if raw.kind == EventKind.SESSION_END else None,
                end_reason=raw.end_reason,
            ),
        )

        episode = self._assign_episode(conn, raw, level, workspace)
        if signature is not None:
            store.upsert_signature(conn, signature, ts)

        event = Event(
            event_id=new_id("ev"),
            dedupe_key=dedupe_key,
            source=raw.source,
            session_id=raw.session_id,
            episode_id=episode.episode_id if episode else None,
            kind=raw.kind,
            ts=ts,
            workspace=workspace,
            repo_id=raw.repo_id,
            tool=tool,
            signature_id=signature.signature_id if signature else None,
            error_excerpt=excerpt,
            claim_detected=claim,
            capture_level=str(level),
            redactions=redactions,
            data=data,
        )
        store.insert_event(conn, event)

        if episode is not None:
            self._after_event(conn, episode, event)
        return IngestResult("stored", event.event_id, episode.episode_id if episode else None, event.signature_id)

    def _assign_episode(self, conn: Connection, raw: RawEvent, level: CaptureLevel, workspace: str) -> Episode | None:
        store = self.store
        if raw.kind == EventKind.SESSION_START:
            return None
        evidence_level = (
            EvidenceLevel.SANITIZED_EVIDENCE if level == CaptureLevel.SANITIZED_EVIDENCE else EvidenceLevel.METADATA
        )

        def make(episode_id: str | None, is_open: bool) -> Episode:
            ep = Episode(
                episode_id=episode_id or new_id("ep"),
                session_id=raw.session_id,
                workspace=workspace,
                repo_id=raw.repo_id,
                agent=raw.agent,
                task_summary=sanitize(raw.task_summary, 500).text if raw.task_summary else None,
                task_family=raw.task_family,
                started_at=raw.ts,
                evidence_level=evidence_level,
                environment=EnvironmentFingerprint(repo_id=raw.repo_id, git_head=raw.git_head, versions=raw.versions),
                language=raw.language,
            )
            store.insert_episode(conn, ep, is_open=is_open)
            return ep

        if raw.episode_id:  # explicit grouping supplied by the source
            ep = store.get_episode(raw.episode_id, conn)
            if ep is None:
                return make(raw.episode_id, is_open=True)
            self._widen(conn, ep, raw)
            return ep

        if raw.kind == EventKind.PROMPT:
            current = store.open_episode(conn, raw.session_id)
            if current is not None:
                self._close(conn, current, raw.ts)
            return make(None, is_open=True)

        current = store.open_episode(conn, raw.session_id)
        if current is not None and raw.ts < current.started_at:
            late = store.episode_covering(conn, raw.session_id, raw.ts)
            if late is not None:
                return late
        if current is None:
            late = store.episode_covering(conn, raw.session_id, raw.ts)
            if late is not None:
                return late
            if raw.kind == EventKind.SESSION_END:
                return None
            current = make(None, is_open=True)
        self._widen(conn, current, raw)
        return current

    def _widen(self, conn: Connection, ep: Episode, raw: RawEvent) -> None:
        values: dict[str, object] = {}
        if raw.ts < ep.started_at:
            values["started_at"] = raw.ts
        if raw.task_summary and not ep.task_summary:
            values["task_summary"] = sanitize(raw.task_summary, 500).text
        if raw.versions or raw.git_head:
            env = ep.environment.model_copy(deep=True)
            env.versions.update(raw.versions)
            env.git_head = raw.git_head or env.git_head
            values["environment"] = env
        if values:
            self.store.update_episode(conn, ep.episode_id, **values)

    def _close(self, conn: Connection, ep: Episode, ts: datetime) -> None:
        self.store.update_episode(conn, ep.episode_id, is_open=False, ended_at=ts)
        self.refresh_auto_lesson(conn, ep.episode_id)

    def _after_event(self, conn: Connection, ep: Episode, event: Event) -> None:
        store = self.store
        if event.kind == EventKind.STOP:
            if event.claim_detected and ep.status == OutcomeStatus.UNKNOWN:
                store.update_episode(conn, ep.episode_id, status=OutcomeStatus.CLAIMED_SUCCESS)
            self._close(conn, ep, event.ts)
            return
        if event.kind == EventKind.SESSION_END:
            self._close(conn, ep, event.ts)
            return
        if event.kind == EventKind.TOOL_RESULT and event.tool and event.data.get("command_hash"):
            self._observed_rerun(conn, ep, event)

    def _observed_rerun(self, conn: Connection, ep: Episode, event: Event) -> None:
        """A command that failed earlier in this episode now succeeded: objective evidence
        that the task-level failure is resolved in the current working state. It says
        nothing about *why*, so root-cause status is untouched."""
        if event.tool is None or event.tool.tool_category not in {"test", "build", "typecheck", "lint", "shell"}:
            return
        earlier = [
            e
            for e in self.store.events_for_episode(ep.episode_id, conn)
            if e.kind == EventKind.TOOL_FAILURE
            and e.data.get("command_hash") == event.data.get("command_hash")
            and e.ts <= event.ts
        ]
        if not earlier:
            return
        ver = VerificationEvidence(
            evidence_id=new_id("ver"),
            episode_id=ep.episode_id,
            kind=VerificationKind.OBSERVED_RERUN,
            command=event.tool.command or event.tool.program,
            exit_code=event.tool.exit_code,
            passed=True,
            ts=event.ts,
            environment=ep.environment,
            failure_signature_ids=list(dict.fromkeys(e.signature_id for e in earlier if e.signature_id)),
            supporting_event_ids=[*(e.event_id for e in earlier), event.event_id],
            note="previously failing command succeeded later in the same episode (reported by the agent runtime)",
        )
        self.store.add_verification(conn, ver)
        self.store.update_episode(conn, ep.episode_id, status=OutcomeStatus.VERIFIED_SUCCESS)

    def refresh_auto_lesson(self, conn: Connection, episode_id: str) -> None:
        store = self.store
        ep = store.get_episode(episode_id, conn)
        if ep is None:
            return
        events = store.events_for_episode(episode_id, conn)
        sig_ids = [e.signature_id for e in events if e.signature_id]
        sigs = _signatures_in_tx(conn, sig_ids) if sig_ids else {}
        verifications = store.verifications_for(episode_id, conn)
        # timestamp derived lessons from their evidence, not from when extraction ran
        evidence_time = max([e.ts for e in events] + [v.ts for v in verifications], default=utcnow())
        lesson = extract(ep, events, verifications, sigs, evidence_time)
        if lesson is None:
            return
        lesson.updated_at = utcnow()
        existing = store.get_lesson(lesson.lesson_id)
        if existing is not None:
            lesson.created_at = existing.created_at
            lesson.status = existing.status
            lesson.superseded_by = existing.superseded_by
            lesson.invalidated_reason = existing.invalidated_reason
        store.upsert_lesson(lesson, conn)


def _signatures_in_tx(conn: Connection, ids: list[str]) -> dict[str, FailureSignature]:
    from sqlalchemy import select

    from afr.persistence import tables as t

    rows = conn.execute(select(t.failure_signatures).where(t.failure_signatures.c.signature_id.in_(ids))).all()
    out = {}
    for r in rows:
        d = dict(r._mapping)
        for k in ("first_seen", "last_seen", "occurrences"):
            d.pop(k)
        out[d["signature_id"]] = FailureSignature.model_validate(d)
    return out
