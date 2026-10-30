"""``afr hook receive``: the command Claude Code runs for each configured hook event.

Fail-open by design: whatever happens, exit 0 and never write to stdout unless emitting
a proactive context note. Failures are counted (SQLite ``ingest_stats``) or spooled
(``spool.jsonl``) so ``afr doctor`` can surface them.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from typing import IO, Any

from afr.config import CaptureLevel, Settings
from afr.redaction import redact_obj

MAX_STDIN_BYTES = 2_000_000


def _spool(settings: Settings, payload: Any, error: str) -> None:
    from afr.ingest.spool import spool_event

    try:
        safe, _ = redact_obj(payload if isinstance(payload, dict) else {"unparsed": str(payload)[:2000]})
        spool_event(settings.spool_path, settings.spool_max_bytes, safe, error)
    except Exception:  # noqa: S110 - last resort; nothing else we can safely do
        pass


def receive(
    stdin: IO[str],
    stdout: IO[str],
    settings: Settings,
    capture_level: CaptureLevel,
    proactive: bool,
) -> int:
    t0 = time.monotonic()
    payload: Any = None
    try:
        raw_text = stdin.read(MAX_STDIN_BYTES + 1)
        if len(raw_text) > MAX_STDIN_BYTES:
            _spool(settings, {"oversized_bytes": len(raw_text)}, "payload exceeds size limit")
            return 0
        payload = json.loads(raw_text)
        if not isinstance(payload, dict):
            _spool(settings, {"unparsed": raw_text[:2000]}, "payload is not a JSON object")
            return 0

        from afr.adapters.claude_code.mapping import to_raw
        from afr.domain.models import EventKind
        from afr.ingest.pipeline import Pipeline
        from afr.persistence.store import Store

        raw = to_raw(payload)
        store = Store(settings)
        if raw is None:
            with store.engine.begin() as conn:
                store.bump(conn, "ignored")
            return 0
        if raw.cwd:
            from pathlib import Path

            from afr.episodes.fingerprint import repo_identity

            raw.repo_id = repo_identity(Path(raw.cwd))
            if raw.kind in (EventKind.PROMPT, EventKind.SESSION_START):
                from afr.episodes.fingerprint import git_head

                raw.git_head = git_head(Path(raw.cwd))
        result = Pipeline(store, settings).ingest(raw, capture_level)

        if proactive and payload.get("hook_event_name") == "PostToolUseFailure" and result.status == "stored":
            from afr.adapters.claude_code.proactive import lookup, render
            from afr.redaction import redact

            out = lookup(
                store,
                settings.workspace,
                raw.session_id,
                result.signature_id,
                redact(raw.error_text).text if raw.error_text else None,
                settings.proactive_budget_ms,
                settings.proactive_max_per_session,
                started=t0,
            )
            if out is not None:
                stdout.write(render(out))
                stdout.flush()
    except Exception as exc:
        _spool(settings, payload, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
    return 0


def main(capture_level: CaptureLevel, proactive: bool, settings: Settings) -> int:
    return receive(sys.stdin, sys.stdout, settings, capture_level, proactive)
