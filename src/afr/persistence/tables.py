"""SQLAlchemy Core table definitions (the authoritative event store).

Schema changes go through Alembic migrations in ``afr/persistence/migrations``.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

SCHEMA_HEAD = "0001_initial"

metadata = MetaData()

sessions = Table(
    "sessions",
    metadata,
    Column("session_id", String, primary_key=True),
    Column("source", String, nullable=False),
    Column("agent", String, nullable=False),
    Column("workspace", String, nullable=False, index=True),
    Column("repo_id", String),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True)),
    Column("end_reason", String),
)

episodes = Table(
    "episodes",
    metadata,
    Column("episode_id", String, primary_key=True),
    Column("session_id", String, nullable=False),
    Column("workspace", String, nullable=False, index=True),
    Column("repo_id", String),
    Column("agent", String, nullable=False),
    Column("task_summary", Text),
    Column("task_family", String),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True)),
    Column("status", String, nullable=False),
    Column("root_cause_status", String, nullable=False),
    Column("evidence_level", String, nullable=False),
    Column("environment", JSON, nullable=False),
    Column("language", String),
    Column("is_open", Boolean, nullable=False, default=True),
    Index("ix_episodes_session_open", "session_id", "is_open"),
)

events = Table(
    "events",
    metadata,
    Column("event_id", String, primary_key=True),
    Column("dedupe_key", String, nullable=False, unique=True),
    Column("source", String, nullable=False),
    Column("session_id", String, nullable=False, index=True),
    Column("episode_id", String, index=True),
    Column("kind", String, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("workspace", String, nullable=False),
    Column("repo_id", String),
    Column("tool", JSON),
    Column("signature_id", String, index=True),
    Column("error_excerpt", Text),
    Column("claim_detected", Boolean, nullable=False, default=False),
    Column("capture_level", String, nullable=False),
    Column("redactions", Integer, nullable=False, default=0),
    Column("data", JSON, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
)

failure_signatures = Table(
    "failure_signatures",
    metadata,
    Column("signature_id", String, primary_key=True),
    Column("tool_category", String, nullable=False),
    Column("exception_type", String),
    Column("error_code", String),
    Column("resource_type", String),
    Column("normalized_message", Text, nullable=False),
    Column("version_context", JSON, nullable=False),
    Column("first_seen", DateTime(timezone=True), nullable=False),
    Column("last_seen", DateTime(timezone=True), nullable=False),
    Column("occurrences", Integer, nullable=False, default=1),
)

verifications = Table(
    "verifications",
    metadata,
    Column("evidence_id", String, primary_key=True),
    Column("episode_id", String, nullable=False, index=True),
    Column("kind", String, nullable=False),
    Column("scope", String, nullable=False),
    Column("command", Text),
    Column("exit_code", Integer),
    Column("passed", Boolean, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("environment", JSON, nullable=False),
    Column("failure_signature_ids", JSON, nullable=False),
    Column("supporting_event_ids", JSON, nullable=False),
    Column("claim", Text),
    Column("note", Text),
)

lessons = Table(
    "lessons",
    metadata,
    Column("lesson_id", String, primary_key=True),
    Column("workspace", String, nullable=False, index=True),
    Column("repo_id", String),
    Column("origin", String, nullable=False),
    Column("status", String, nullable=False),
    Column("body", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

retrieval_feedback = Table(
    "retrieval_feedback",
    metadata,
    Column("feedback_id", String, primary_key=True),
    Column("workspace", String, nullable=False),
    Column("task_ref", String, nullable=False),
    Column("body", JSON, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
)

index_state = Table(
    "index_state",
    metadata,
    Column("doc_id", String, primary_key=True),
    Column("doc_type", String, nullable=False),
    Column("workspace", String, nullable=False),
    Column("content_hash", String, nullable=False),
    Column("indexed_hash", String),
    Column("indexed_at", DateTime(timezone=True)),
    Column("collection", String),
    Column("error", Text),
)

ingest_stats = Table(
    "ingest_stats",
    metadata,
    Column("counter", String, primary_key=True),
    Column("value", Integer, nullable=False, default=0),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

proactive_log = Table(
    "proactive_log",
    metadata,
    Column("session_id", String, primary_key=True),
    Column("signature_key", String, primary_key=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("lesson_ids", JSON, nullable=False),
)

kv = Table(
    "kv",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", JSON, nullable=False),
)
