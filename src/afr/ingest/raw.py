"""Adapter-neutral raw event. Adapters build these; the pipeline sanitizes and stores them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from afr.domain.models import EventKind


@dataclass
class RawEvent:
    source: str
    agent: str
    session_id: str
    kind: EventKind
    ts: datetime
    dedupe_basis: str
    cwd: str | None = None
    workspace: str | None = None
    repo_id: str | None = None
    episode_id: str | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    command: str | None = None  # raw; never persisted without sanitization
    exit_code: int | None = None
    succeeded: bool | None = None
    duration_ms: int | None = None
    files: list[str] = field(default_factory=list)
    error_text: str | None = None  # raw; signature derived after redaction
    claim_text: str | None = None  # raw; only a boolean "claim detected" is kept
    task_summary: str | None = None
    task_family: str | None = None
    language: str | None = None
    versions: dict[str, str] = field(default_factory=dict)
    git_head: str | None = None
    end_reason: str | None = None
    data: dict[str, Any] = field(default_factory=dict)  # allowlisted scalars only
