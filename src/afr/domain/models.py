"""Typed internal records. These are the canonical shapes stored in SQLite."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class OutcomeStatus(StrEnum):
    UNKNOWN = "unknown"
    CLAIMED_SUCCESS = "claimed_success"
    VERIFIED_SUCCESS = "verified_success"
    VERIFIED_FAILURE = "verified_failure"


class RootCauseStatus(StrEnum):
    """Kept separate from task outcome: passing tests do not validate an explanation."""

    UNVERIFIED = "unverified"
    SUPPORTED = "supported"  # human-reviewed, consistent with evidence, no discriminating check
    VERIFIED = "verified"  # an explicit discriminating check was recorded


class EvidenceLevel(StrEnum):
    """How much evidence backs an episode or lesson (ordered)."""

    METADATA = "metadata"
    SANITIZED_EVIDENCE = "sanitized_evidence"
    REVIEWED_LESSON = "reviewed_lesson"


class EventKind(StrEnum):
    SESSION_START = "session_start"
    PROMPT = "prompt"
    TOOL_RESULT = "tool_result"
    TOOL_FAILURE = "tool_failure"
    STOP = "stop"
    SESSION_END = "session_end"
    VERIFICATION = "verification"
    NOTE = "note"


class LessonOrigin(StrEnum):
    EVIDENCE_EXTRACTED = "evidence_extracted"
    CANDIDATE = "candidate"
    MANUAL_REVIEWED = "manual_reviewed"


class LessonStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


class VerificationKind(StrEnum):
    TEST_COMMAND = "test_command"  # afr verify ran it and saw the exit code
    OBSERVED_RERUN = "observed_rerun"  # a previously failing command later succeeded
    CI_RESULT = "ci_result"
    FIXTURE_REPLAY = "fixture_replay"
    EXTERNAL_CHECK = "external_check"


class VerificationScope(StrEnum):
    TASK = "task"
    ROOT_CAUSE = "root_cause"


class Session(BaseModel):
    session_id: str
    source: str
    agent: str
    workspace: str
    repo_id: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    end_reason: str | None = None


class ToolCall(BaseModel):
    tool_name: str
    tool_category: str
    tool_use_id: str | None = None
    program: str | None = None  # first token of a shell command, allowlisted metadata
    command: str | None = None  # redacted command line, sanitized_evidence only
    exit_code: int | None = None
    succeeded: bool | None = None
    duration_ms: int | None = None
    files: list[str] = Field(default_factory=list)  # repo-relative paths, sanitized_evidence only


class EnvironmentFingerprint(BaseModel):
    os: str | None = None
    repo_id: str | None = None
    git_head: str | None = None
    worktree_fingerprint: str | None = None
    worktree_dirty: bool | None = None
    versions: dict[str, str] = Field(default_factory=dict)


class FailureSignature(BaseModel):
    signature_id: str
    tool_category: str
    exception_type: str | None = None
    error_code: str | None = None
    resource_type: str | None = None
    normalized_message: str
    version_context: dict[str, str] = Field(default_factory=dict)


class Event(BaseModel):
    event_id: str
    dedupe_key: str
    source: str
    session_id: str
    episode_id: str | None = None
    kind: EventKind
    ts: datetime
    workspace: str
    repo_id: str | None = None
    tool: ToolCall | None = None
    signature_id: str | None = None
    error_excerpt: str | None = None  # redacted, bounded; sanitized_evidence only
    claim_detected: bool = False
    capture_level: str
    redactions: int = 0
    data: dict[str, Any] = Field(default_factory=dict)  # allowlisted extra metadata


class VerificationEvidence(BaseModel):
    evidence_id: str
    episode_id: str
    kind: VerificationKind
    scope: VerificationScope = VerificationScope.TASK
    command: str | None = None
    exit_code: int | None = None
    passed: bool
    ts: datetime
    environment: EnvironmentFingerprint = Field(default_factory=EnvironmentFingerprint)
    failure_signature_ids: list[str] = Field(default_factory=list)
    supporting_event_ids: list[str] = Field(default_factory=list)
    claim: str | None = None  # for root_cause scope: the explanation being checked
    note: str | None = None


class Episode(BaseModel):
    episode_id: str
    session_id: str
    workspace: str
    repo_id: str | None = None
    agent: str
    task_summary: str | None = None
    task_family: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    status: OutcomeStatus = OutcomeStatus.UNKNOWN
    root_cause_status: RootCauseStatus = RootCauseStatus.UNVERIFIED
    evidence_level: EvidenceLevel = EvidenceLevel.METADATA
    environment: EnvironmentFingerprint = Field(default_factory=EnvironmentFingerprint)
    language: str | None = None


class Lesson(BaseModel):
    lesson_id: str
    workspace: str
    repo_id: str | None = None
    origin: LessonOrigin
    status: LessonStatus = LessonStatus.ACTIVE
    problem: str
    observed_symptoms: list[str] = Field(default_factory=list)
    failed_approaches: list[str] = Field(default_factory=list)
    successful_approach: str | None = None
    successful_approach_missing_reason: str | None = None
    root_cause: str | None = None
    root_cause_status: RootCauseStatus = RootCauseStatus.UNVERIFIED
    preconditions: list[str] = Field(default_factory=list)
    applies_when: dict[str, str] = Field(default_factory=dict)  # e.g. {"os": "linux", "python": ">=3.12"}
    contraindications: list[str] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)
    verification_ids: list[str] = Field(default_factory=list)
    task_status: OutcomeStatus = OutcomeStatus.UNKNOWN
    source_episode_ids: list[str] = Field(default_factory=list)
    signature_ids: list[str] = Field(default_factory=list)
    error_types: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    language: str | None = None
    confidence_basis: str
    superseded_by: str | None = None
    invalidated_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    verified_at: datetime | None = None


class RetrievalFeedback(BaseModel):
    feedback_id: str
    workspace: str
    task_ref: str  # caller-supplied task id
    episode_id: str | None = None  # recorded episode of the new task, if known
    retrieved_ids: list[str] = Field(default_factory=list)
    consulted_lesson_ids: list[str] = Field(default_factory=list)
    outcome: OutcomeStatus  # objective: taken from the linked episode's evidence, never self-reported
    reported_outcome: str | None = None  # the agent's own claim, kept separately
    evidence_ids: list[str] = Field(default_factory=list)
    note: str | None = None
    ts: datetime
