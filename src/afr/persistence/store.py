"""Repository layer over the SQLite event store."""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, and_, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from afr.config import Settings
from afr.domain.models import (
    EnvironmentFingerprint,
    Episode,
    Event,
    FailureSignature,
    Lesson,
    LessonStatus,
    RetrievalFeedback,
    Session,
    ToolCall,
    VerificationEvidence,
)
from afr.persistence import tables as t
from afr.persistence.db import make_engine, migrate, with_retry


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Store:
    def __init__(self, settings: Settings, engine: Engine | None = None) -> None:
        self.settings = settings
        settings.home.mkdir(parents=True, exist_ok=True)
        self.engine = engine or make_engine(settings.db_path)
        with_retry(lambda: migrate(self.engine))
        self._salt: bytes | None = None

    # -- infra -----------------------------------------------------------------
    @property
    def salt(self) -> bytes:
        if self._salt is None:
            path = self.settings.salt_path
            if not path.exists():
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, secrets.token_bytes(32))
                except Exception:
                    os.close(fd)
                    raise
                os.close(fd)
            self._salt = path.read_bytes()
        return self._salt

    def bump(self, conn: Connection, counter: str, n: int = 1) -> None:
        now = utcnow()
        res = conn.execute(
            update(t.ingest_stats)
            .where(t.ingest_stats.c.counter == counter)
            .values(value=t.ingest_stats.c.value + n, updated_at=now)
        )
        if res.rowcount == 0:
            try:
                with conn.begin_nested():
                    conn.execute(insert(t.ingest_stats).values(counter=counter, value=n, updated_at=now))
            except IntegrityError:
                conn.execute(
                    update(t.ingest_stats)
                    .where(t.ingest_stats.c.counter == counter)
                    .values(value=t.ingest_stats.c.value + n, updated_at=now)
                )

    def stats(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            return {r.counter: r.value for r in conn.execute(select(t.ingest_stats))}

    def kv_get(self, key: str) -> Any:
        with self.engine.connect() as conn:
            row = conn.execute(select(t.kv.c.value).where(t.kv.c.key == key)).first()
        return row[0] if row else None

    def kv_set(self, key: str, value: Any) -> None:
        def _do() -> None:
            with self.engine.begin() as conn:
                conn.execute(delete(t.kv).where(t.kv.c.key == key))
                conn.execute(insert(t.kv).values(key=key, value=value))

        with_retry(_do)

    # -- sessions --------------------------------------------------------------
    def upsert_session(self, conn: Connection, s: Session) -> None:
        row = conn.execute(select(t.sessions).where(t.sessions.c.session_id == s.session_id)).first()
        if row is None:
            conn.execute(insert(t.sessions).values(**s.model_dump()))
            return
        values: dict[str, Any] = {}
        if _aware(s.started_at) < _aware(row.started_at):  # type: ignore[operator]
            values["started_at"] = s.started_at
        if s.ended_at:
            values["ended_at"] = s.ended_at
            values["end_reason"] = s.end_reason
        if s.repo_id and not row.repo_id:
            values["repo_id"] = s.repo_id
        if values:
            conn.execute(update(t.sessions).where(t.sessions.c.session_id == s.session_id).values(**values))

    # -- events ----------------------------------------------------------------
    def event_exists(self, conn: Connection, dedupe_key: str) -> bool:
        return conn.execute(select(t.events.c.event_id).where(t.events.c.dedupe_key == dedupe_key)).first() is not None

    def insert_event(self, conn: Connection, ev: Event) -> None:
        data = ev.model_dump(mode="json")
        data["ts"] = ev.ts
        data["received_at"] = utcnow()
        conn.execute(insert(t.events).values(**data))

    def events_for_episode(self, episode_id: str, conn: Connection | None = None) -> list[Event]:
        q = select(t.events).where(t.events.c.episode_id == episode_id).order_by(t.events.c.ts)
        if conn is not None:
            rows = conn.execute(q).all()
        else:
            with self.engine.connect() as c:
                rows = c.execute(q).all()
        return [self._event(r) for r in rows]

    def get_events(self, event_ids: Sequence[str]) -> list[Event]:
        with self.engine.connect() as c:
            rows = c.execute(select(t.events).where(t.events.c.event_id.in_(list(event_ids)))).all()
        return [self._event(r) for r in rows]

    @staticmethod
    def _event(r: Any) -> Event:
        d = dict(r._mapping)
        d.pop("received_at", None)
        d["tool"] = ToolCall.model_validate(d["tool"]) if d["tool"] else None
        d["ts"] = _aware(d["ts"])
        return Event.model_validate(d)

    # -- episodes --------------------------------------------------------------
    def open_episode(self, conn: Connection, session_id: str) -> Episode | None:
        row = conn.execute(
            select(t.episodes)
            .where(and_(t.episodes.c.session_id == session_id, t.episodes.c.is_open.is_(True)))
            .order_by(t.episodes.c.started_at.desc())
        ).first()
        return self._episode(row) if row else None

    def episode_covering(self, conn: Connection, session_id: str, ts: datetime) -> Episode | None:
        """Closed episode in this session whose time window contains ``ts`` (late events)."""
        rows = conn.execute(
            select(t.episodes).where(t.episodes.c.session_id == session_id).order_by(t.episodes.c.started_at.desc())
        ).all()
        for row in rows:
            ep = self._episode(row)
            end = ep.ended_at
            if ep.started_at <= ts and (end is None or ts <= end):
                return ep
        return None

    def insert_episode(self, conn: Connection, ep: Episode, is_open: bool = True) -> None:
        values = ep.model_dump(mode="json")
        values.update(started_at=ep.started_at, ended_at=ep.ended_at, is_open=is_open)
        conn.execute(insert(t.episodes).values(**values))

    def update_episode(self, conn: Connection, episode_id: str, **values: Any) -> None:
        if "environment" in values and isinstance(values["environment"], EnvironmentFingerprint):
            values["environment"] = values["environment"].model_dump(mode="json")
        for k in ("status", "root_cause_status", "evidence_level"):
            if k in values and values[k] is not None:
                values[k] = str(values[k])
        conn.execute(update(t.episodes).where(t.episodes.c.episode_id == episode_id).values(**values))

    def get_episode(self, episode_id: str, conn: Connection | None = None) -> Episode | None:
        q = select(t.episodes).where(t.episodes.c.episode_id == episode_id)
        if conn is not None:
            row = conn.execute(q).first()
        else:
            with self.engine.connect() as c:
                row = c.execute(q).first()
        return self._episode(row) if row else None

    def list_episodes(self, workspace: str | None = None, limit: int | None = None) -> list[Episode]:
        q = select(t.episodes).order_by(t.episodes.c.started_at)
        if workspace:
            q = q.where(t.episodes.c.workspace == workspace)
        if limit:
            q = q.limit(limit)
        with self.engine.connect() as c:
            return [self._episode(r) for r in c.execute(q).all()]

    @staticmethod
    def _episode(r: Any) -> Episode:
        d = dict(r._mapping)
        d.pop("is_open", None)
        d["started_at"] = _aware(d["started_at"])
        d["ended_at"] = _aware(d["ended_at"])
        return Episode.model_validate(d)

    # -- signatures ------------------------------------------------------------
    def upsert_signature(self, conn: Connection, sig: FailureSignature, ts: datetime) -> None:
        res = conn.execute(
            update(t.failure_signatures)
            .where(t.failure_signatures.c.signature_id == sig.signature_id)
            .values(occurrences=t.failure_signatures.c.occurrences + 1, last_seen=ts)
        )
        if res.rowcount == 0:
            conn.execute(
                insert(t.failure_signatures).values(
                    **sig.model_dump(mode="json"), first_seen=ts, last_seen=ts, occurrences=1
                )
            )

    def get_signatures(self, ids: Iterable[str]) -> list[FailureSignature]:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return []
        with self.engine.connect() as c:
            rows = c.execute(select(t.failure_signatures).where(t.failure_signatures.c.signature_id.in_(ids))).all()
        out = []
        for r in rows:
            d = dict(r._mapping)
            for k in ("first_seen", "last_seen", "occurrences"):
                d.pop(k)
            out.append(FailureSignature.model_validate(d))
        order = {sid: i for i, sid in enumerate(ids)}
        return sorted(out, key=lambda s: order[s.signature_id])

    def signature_occurrences(self, signature_id: str) -> int:
        with self.engine.connect() as c:
            row = c.execute(
                select(t.failure_signatures.c.occurrences).where(t.failure_signatures.c.signature_id == signature_id)
            ).first()
        return int(row[0]) if row else 0

    # -- verifications ---------------------------------------------------------
    def add_verification(self, conn: Connection, v: VerificationEvidence) -> None:
        values = v.model_dump(mode="json")
        values["ts"] = v.ts
        conn.execute(insert(t.verifications).values(**values))

    def verifications_for(self, episode_id: str, conn: Connection | None = None) -> list[VerificationEvidence]:
        q = select(t.verifications).where(t.verifications.c.episode_id == episode_id).order_by(t.verifications.c.ts)
        if conn is not None:
            rows = conn.execute(q).all()
        else:
            with self.engine.connect() as c:
                rows = c.execute(q).all()
        out = []
        for r in rows:
            d = dict(r._mapping)
            d["ts"] = _aware(d["ts"])
            out.append(VerificationEvidence.model_validate(d))
        return out

    # -- lessons ---------------------------------------------------------------
    def upsert_lesson(self, lesson: Lesson, conn: Connection | None = None) -> None:
        body = lesson.model_dump(mode="json")

        def _do(c: Connection) -> None:
            c.execute(delete(t.lessons).where(t.lessons.c.lesson_id == lesson.lesson_id))
            c.execute(
                insert(t.lessons).values(
                    lesson_id=lesson.lesson_id,
                    workspace=lesson.workspace,
                    repo_id=lesson.repo_id,
                    origin=str(lesson.origin),
                    status=str(lesson.status),
                    body=body,
                    created_at=lesson.created_at,
                    updated_at=lesson.updated_at,
                )
            )

        if conn is not None:
            _do(conn)
        else:

            def _tx() -> None:
                with self.engine.begin() as c:
                    _do(c)

            with_retry(_tx)

    def get_lesson(self, lesson_id: str) -> Lesson | None:
        with self.engine.connect() as c:
            row = c.execute(select(t.lessons.c.body).where(t.lessons.c.lesson_id == lesson_id)).first()
        return Lesson.model_validate(row[0]) if row else None

    def list_lessons(
        self, workspace: str | None = None, statuses: Sequence[LessonStatus] | None = None
    ) -> list[Lesson]:
        q = select(t.lessons.c.body).order_by(t.lessons.c.created_at)
        if workspace:
            q = q.where(t.lessons.c.workspace == workspace)
        if statuses:
            q = q.where(t.lessons.c.status.in_([str(s) for s in statuses]))
        with self.engine.connect() as c:
            return [Lesson.model_validate(r[0]) for r in c.execute(q).all()]

    def lessons_for_episode(self, episode_id: str) -> list[Lesson]:
        return [les for les in self.list_lessons() if episode_id in les.source_episode_ids]

    # -- feedback --------------------------------------------------------------
    def add_feedback(self, fb: RetrievalFeedback) -> None:
        def _do() -> None:
            with self.engine.begin() as c:
                c.execute(
                    insert(t.retrieval_feedback).values(
                        feedback_id=fb.feedback_id,
                        workspace=fb.workspace,
                        task_ref=fb.task_ref,
                        body=fb.model_dump(mode="json"),
                        ts=fb.ts,
                    )
                )

        with_retry(_do)

    def list_feedback(self, workspace: str | None = None) -> list[RetrievalFeedback]:
        q = select(t.retrieval_feedback.c.body).order_by(t.retrieval_feedback.c.ts)
        if workspace:
            q = q.where(t.retrieval_feedback.c.workspace == workspace)
        with self.engine.connect() as c:
            return [RetrievalFeedback.model_validate(r[0]) for r in c.execute(q).all()]

    # -- deletion / retention --------------------------------------------------
    def delete_episode(self, episode_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}

        def _do() -> None:
            with self.engine.begin() as c:
                counts["events"] = c.execute(delete(t.events).where(t.events.c.episode_id == episode_id)).rowcount
                counts["verifications"] = c.execute(
                    delete(t.verifications).where(t.verifications.c.episode_id == episode_id)
                ).rowcount
                counts["episodes"] = c.execute(delete(t.episodes).where(t.episodes.c.episode_id == episode_id)).rowcount
                n = 0
                for row in c.execute(select(t.lessons.c.lesson_id, t.lessons.c.body)).all():
                    les = Lesson.model_validate(row.body)
                    if episode_id in les.source_episode_ids:
                        remaining = [e for e in les.source_episode_ids if e != episode_id]
                        if not remaining:
                            c.execute(delete(t.lessons).where(t.lessons.c.lesson_id == les.lesson_id))
                        else:
                            les.source_episode_ids = remaining
                            self.upsert_lesson(les, c)
                        n += 1
                counts["lessons_touched"] = n

        with_retry(_do)
        return counts

    def episodes_before(self, cutoff: datetime) -> list[str]:
        with self.engine.connect() as c:
            rows = c.execute(select(t.episodes.c.episode_id).where(t.episodes.c.started_at < cutoff)).all()
        return [r[0] for r in rows]

    # -- index state -----------------------------------------------------------
    def index_states(self) -> dict[str, tuple[str | None, str | None]]:
        with self.engine.connect() as c:
            return {r.doc_id: (r.indexed_hash, r.collection) for r in c.execute(select(t.index_state)).all()}

    def mark_indexed(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return

        def _do() -> None:
            with self.engine.begin() as c:
                c.execute(delete(t.index_state).where(t.index_state.c.doc_id.in_([r["doc_id"] for r in rows])))
                c.execute(insert(t.index_state), rows)

        with_retry(_do)

    def forget_index_state(self, doc_ids: Sequence[str] | None = None) -> None:
        with self.engine.begin() as c:
            if doc_ids is None:
                c.execute(delete(t.index_state))
            else:
                c.execute(delete(t.index_state).where(t.index_state.c.doc_id.in_(list(doc_ids))))

    def index_errors(self) -> int:
        with self.engine.connect() as c:
            return int(
                c.execute(
                    select(func.count()).select_from(t.index_state).where(t.index_state.c.error.is_not(None))
                ).scalar()
                or 0
            )

    # -- proactive dedupe ------------------------------------------------------
    def proactive_seen(self, conn: Connection, session_id: str, key: str) -> bool:
        return (
            conn.execute(
                select(t.proactive_log.c.ts).where(
                    and_(t.proactive_log.c.session_id == session_id, t.proactive_log.c.signature_key == key)
                )
            ).first()
            is not None
        )

    def proactive_count(self, conn: Connection, session_id: str) -> int:
        return int(
            conn.execute(
                select(func.count()).select_from(t.proactive_log).where(t.proactive_log.c.session_id == session_id)
            ).scalar()
            or 0
        )

    def proactive_record(self, conn: Connection, session_id: str, key: str, lesson_ids: list[str]) -> None:
        conn.execute(
            insert(t.proactive_log).values(session_id=session_id, signature_key=key, ts=utcnow(), lesson_ids=lesson_ids)
        )

    def counts(self) -> dict[str, int]:
        out = {}
        with self.engine.connect() as c:
            for name, table in (
                ("sessions", t.sessions),
                ("episodes", t.episodes),
                ("events", t.events),
                ("signatures", t.failure_signatures),
                ("verifications", t.verifications),
                ("lessons", t.lessons),
                ("feedback", t.retrieval_feedback),
            ):
                out[name] = int(c.execute(select(func.count()).select_from(table)).scalar() or 0)
        return out
