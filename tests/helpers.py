from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def ts(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def gen(event_id: str, kind: str, minutes: float, session: str = "s1", **extra: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "schema": "afr.generic-event.v1",
        "event_id": event_id,
        "ts": ts(minutes),
        "session_id": session,
        "agent": "test-agent",
        "kind": kind,
    }
    rec.update(extra)
    return rec


def hook(event: str, session: str = "sess-1", **extra: Any) -> dict[str, Any]:
    base = {
        "session_id": session,
        "transcript_path": "/home/user/.claude/projects/x/t.jsonl",
        "cwd": "/home/user/proj",
        "permission_mode": "default",
        "hook_event_name": event,
    }
    base.update(extra)
    return base
