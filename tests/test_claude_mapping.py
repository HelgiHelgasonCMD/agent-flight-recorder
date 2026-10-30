"""Payload shapes copied from the official hooks reference (code.claude.com/docs/en/hooks)."""

from __future__ import annotations

import json

from afr.adapters.claude_code.mapping import to_raw
from afr.domain.models import EventKind

from .conftest import FIXTURES


def load(name: str) -> dict:
    return json.loads((FIXTURES / "claude_code" / name).read_text())


def test_post_tool_use_failure_doc_example() -> None:
    raw = to_raw(load("post_tool_use_failure.json"))
    assert raw is not None
    assert raw.kind == EventKind.TOOL_FAILURE
    assert raw.tool_name == "Bash" and raw.command == "npm test"
    assert raw.exit_code == 1 and raw.succeeded is False
    assert raw.duration_ms == 4187
    assert "Cannot find module 'express'" in (raw.error_text or "")
    assert raw.dedupe_basis.endswith("toolu_01ABC123...")


def test_post_tool_use_write_doc_example() -> None:
    raw = to_raw(load("post_tool_use.json"))
    assert raw is not None and raw.kind == EventKind.TOOL_RESULT
    assert raw.files == ["/path/to/file.txt"] and raw.command is None
    assert raw.succeeded is True and raw.exit_code is None  # not reported → not guessed


def test_prompt_text_not_carried() -> None:
    raw = to_raw(load("user_prompt_submit.json"))
    assert raw is not None and raw.kind == EventKind.PROMPT
    assert raw.task_summary is None
    assert "factorial" not in json.dumps(raw.data)
    assert "prompt_sha256_16" in raw.data


def test_stop_and_session_events() -> None:
    stop = to_raw(load("stop.json"))
    assert stop is not None and stop.kind == EventKind.STOP and stop.claim_text
    fail = to_raw(load("stop_failure.json"))
    assert fail is not None and fail.claim_text is None and fail.data["stop_failure"] == "rate_limit"
    start = to_raw(load("session_start.json"))
    assert start is not None and start.kind == EventKind.SESSION_START and start.data["start_source"] == "resume"
    end = to_raw(load("session_end.json"))
    assert end is not None and end.kind == EventKind.SESSION_END and end.end_reason == "other"


def test_unsupported_or_malformed() -> None:
    assert to_raw({"hook_event_name": "PreToolUse", "session_id": "x"}) is None
    assert to_raw({"hook_event_name": "Stop"}) is None  # no session id: do not guess
    raw = to_raw({"hook_event_name": "PostToolUseFailure", "session_id": "x"})
    assert raw is not None and raw.tool_name == "unknown" and raw.error_text is None
