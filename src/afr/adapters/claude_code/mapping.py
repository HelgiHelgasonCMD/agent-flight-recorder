"""Map Claude Code hook stdin payloads to RawEvents.

Field names follow the official hooks reference (https://code.claude.com/docs/en/hooks),
checked against Claude Code 2.1.314. Only documented fields are read; anything else is
ignored. Missing fields stay ``None``; nothing is guessed.

Handled events: SessionStart, UserPromptSubmit, PostToolUse, PostToolUseFailure, Stop,
StopFailure, SessionEnd. ``prompt`` text and ``last_assistant_message`` are never stored:
the prompt contributes only a hash (dedupe basis), the final message only a boolean
"success claimed" flag.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from afr.domain.models import EventKind
from afr.ingest.raw import RawEvent

SOURCE = "claude_code"
SUPPORTED_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "StopFailure",
    "SessionEnd",
)
_EXIT_RE = re.compile(r"^Exit code (\d+)\s*$", re.MULTILINE)


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:32]


def _tool_files(tool_name: str, tool_input: dict[str, Any], tool_response: Any) -> list[str]:
    files: list[str] = []
    fp = tool_input.get("file_path") or tool_input.get("notebook_path")
    if isinstance(fp, str):
        files.append(fp)
    if isinstance(tool_response, dict):
        diff = tool_response.get("bashEditDiff")
        if isinstance(diff, dict) and isinstance(diff.get("changedFiles"), list):
            files.extend(f for f in diff["changedFiles"] if isinstance(f, str))
    return files


def to_raw(payload: dict[str, Any], received_at: datetime | None = None) -> RawEvent | None:
    """Returns None for unsupported events (recorded as 'ignored' by the caller)."""
    name = _str(payload.get("hook_event_name"))
    session_id = _str(payload.get("session_id"))
    if name not in SUPPORTED_EVENTS or session_id is None:
        return None
    ts = received_at or datetime.now(UTC)
    common: dict[str, Any] = {
        "source": SOURCE,
        "agent": "claude-code",
        "session_id": session_id,
        "ts": ts,
        "cwd": _str(payload.get("cwd")),
    }
    data: dict[str, Any] = {}
    if _str(payload.get("permission_mode")):
        data["permission_mode"] = payload["permission_mode"]
    if _str(payload.get("agent_type")):
        data["agent_type"] = payload["agent_type"]
    prompt_id = _str(payload.get("prompt_id"))

    if name == "SessionStart":
        if _str(payload.get("model")):
            data["model"] = payload["model"]
        if _str(payload.get("source")):
            data["start_source"] = payload["source"]
        basis = f"{payload.get('source')}|{_digest(data)}|{ts.isoformat()}"
        return RawEvent(kind=EventKind.SESSION_START, dedupe_basis=basis, data=data, **common)

    if name == "UserPromptSubmit":
        prompt = payload.get("prompt")
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16] if isinstance(prompt, str) else None
        if prompt_hash:
            data["prompt_sha256_16"] = prompt_hash
        basis = prompt_id or f"{prompt_hash}|{ts.isoformat()}"
        return RawEvent(kind=EventKind.PROMPT, dedupe_basis=basis, data=data, **common)

    if name in ("PostToolUse", "PostToolUseFailure"):
        tool_name = _str(payload.get("tool_name")) or "unknown"
        raw_input = payload.get("tool_input")
        tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        tool_response = payload.get("tool_response")
        tool_use_id = _str(payload.get("tool_use_id"))
        command = _str(tool_input.get("command")) if tool_name in ("Bash", "PowerShell") else None
        failure = name == "PostToolUseFailure"
        error = _str(payload.get("error")) if failure else None
        exit_code: int | None = None
        if error:
            m = _EXIT_RE.search(error)
            exit_code = int(m.group(1)) if m else None
        if failure and payload.get("is_interrupt") is True:
            data["is_interrupt"] = True
        basis = tool_use_id or _digest({k: payload.get(k) for k in ("tool_name", "tool_input", "error")})
        return RawEvent(
            kind=EventKind.TOOL_FAILURE if failure else EventKind.TOOL_RESULT,
            dedupe_basis=f"{name}|{basis}",
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            command=command,
            exit_code=exit_code,
            # PostToolUse fires for results the tool did not report as failures; a
            # non-zero Bash exit arrives as PostToolUseFailure with "Exit code N".
            succeeded=not failure,
            duration_ms=_int(payload.get("duration_ms")),
            files=_tool_files(tool_name, tool_input, tool_response),
            error_text=error,
            data=data,
            **common,
        )

    if name in ("Stop", "StopFailure"):
        msg = _str(payload.get("last_assistant_message"))
        if name == "StopFailure":
            # here last_assistant_message is the API error string, not a claim
            data["stop_failure"] = _str(payload.get("error")) or "unknown"
            msg = None
        basis = f"{name}|{prompt_id or ''}|{_digest(msg)}|{ts.isoformat()}"
        return RawEvent(kind=EventKind.STOP, dedupe_basis=basis, claim_text=msg, data=data, **common)

    # SessionEnd
    reason = _str(payload.get("reason"))
    return RawEvent(kind=EventKind.SESSION_END, dedupe_basis=f"end|{reason}", end_reason=reason, data=data, **common)
