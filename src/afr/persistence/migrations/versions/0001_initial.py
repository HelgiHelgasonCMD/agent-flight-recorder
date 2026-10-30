"""Initial schema.

Revision ID: 0001_initial
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String, primary_key=True),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("agent", sa.String, nullable=False),
        sa.Column("workspace", sa.String, nullable=False, index=True),
        sa.Column("repo_id", sa.String),
        sa.Column("started_at", TZ, nullable=False),
        sa.Column("ended_at", TZ),
        sa.Column("end_reason", sa.String),
    )
    op.create_table(
        "episodes",
        sa.Column("episode_id", sa.String, primary_key=True),
        sa.Column("session_id", sa.String, nullable=False),
        sa.Column("workspace", sa.String, nullable=False, index=True),
        sa.Column("repo_id", sa.String),
        sa.Column("agent", sa.String, nullable=False),
        sa.Column("task_summary", sa.Text),
        sa.Column("task_family", sa.String),
        sa.Column("started_at", TZ, nullable=False),
        sa.Column("ended_at", TZ),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("root_cause_status", sa.String, nullable=False),
        sa.Column("evidence_level", sa.String, nullable=False),
        sa.Column("environment", sa.JSON, nullable=False),
        sa.Column("language", sa.String),
        sa.Column("is_open", sa.Boolean, nullable=False),
    )
    op.create_index("ix_episodes_session_open", "episodes", ["session_id", "is_open"])
    op.create_table(
        "events",
        sa.Column("event_id", sa.String, primary_key=True),
        sa.Column("dedupe_key", sa.String, nullable=False, unique=True),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("session_id", sa.String, nullable=False, index=True),
        sa.Column("episode_id", sa.String, index=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("ts", TZ, nullable=False),
        sa.Column("workspace", sa.String, nullable=False),
        sa.Column("repo_id", sa.String),
        sa.Column("tool", sa.JSON),
        sa.Column("signature_id", sa.String, index=True),
        sa.Column("error_excerpt", sa.Text),
        sa.Column("claim_detected", sa.Boolean, nullable=False),
        sa.Column("capture_level", sa.String, nullable=False),
        sa.Column("redactions", sa.Integer, nullable=False),
        sa.Column("data", sa.JSON, nullable=False),
        sa.Column("received_at", TZ, nullable=False),
    )
    op.create_table(
        "failure_signatures",
        sa.Column("signature_id", sa.String, primary_key=True),
        sa.Column("tool_category", sa.String, nullable=False),
        sa.Column("exception_type", sa.String),
        sa.Column("error_code", sa.String),
        sa.Column("resource_type", sa.String),
        sa.Column("normalized_message", sa.Text, nullable=False),
        sa.Column("version_context", sa.JSON, nullable=False),
        sa.Column("first_seen", TZ, nullable=False),
        sa.Column("last_seen", TZ, nullable=False),
        sa.Column("occurrences", sa.Integer, nullable=False),
    )
    op.create_table(
        "verifications",
        sa.Column("evidence_id", sa.String, primary_key=True),
        sa.Column("episode_id", sa.String, nullable=False, index=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("scope", sa.String, nullable=False),
        sa.Column("command", sa.Text),
        sa.Column("exit_code", sa.Integer),
        sa.Column("passed", sa.Boolean, nullable=False),
        sa.Column("ts", TZ, nullable=False),
        sa.Column("environment", sa.JSON, nullable=False),
        sa.Column("failure_signature_ids", sa.JSON, nullable=False),
        sa.Column("supporting_event_ids", sa.JSON, nullable=False),
        sa.Column("claim", sa.Text),
        sa.Column("note", sa.Text),
    )
    op.create_table(
        "lessons",
        sa.Column("lesson_id", sa.String, primary_key=True),
        sa.Column("workspace", sa.String, nullable=False, index=True),
        sa.Column("repo_id", sa.String),
        sa.Column("origin", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("body", sa.JSON, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
    )
    op.create_table(
        "retrieval_feedback",
        sa.Column("feedback_id", sa.String, primary_key=True),
        sa.Column("workspace", sa.String, nullable=False),
        sa.Column("task_ref", sa.String, nullable=False),
        sa.Column("body", sa.JSON, nullable=False),
        sa.Column("ts", TZ, nullable=False),
    )
    op.create_table(
        "index_state",
        sa.Column("doc_id", sa.String, primary_key=True),
        sa.Column("doc_type", sa.String, nullable=False),
        sa.Column("workspace", sa.String, nullable=False),
        sa.Column("content_hash", sa.String, nullable=False),
        sa.Column("indexed_hash", sa.String),
        sa.Column("indexed_at", TZ),
        sa.Column("collection", sa.String),
        sa.Column("error", sa.Text),
    )
    op.create_table(
        "ingest_stats",
        sa.Column("counter", sa.String, primary_key=True),
        sa.Column("value", sa.Integer, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
    )
    op.create_table(
        "proactive_log",
        sa.Column("session_id", sa.String, primary_key=True),
        sa.Column("signature_key", sa.String, primary_key=True),
        sa.Column("ts", TZ, nullable=False),
        sa.Column("lesson_ids", sa.JSON, nullable=False),
    )
    op.create_table(
        "kv",
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
    )


def downgrade() -> None:
    for name in (
        "kv",
        "proactive_log",
        "ingest_stats",
        "index_state",
        "retrieval_feedback",
        "lessons",
        "verifications",
        "failure_signatures",
        "events",
        "episodes",
        "sessions",
    ):
        op.drop_table(name)
