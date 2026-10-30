"""Bounded dead-letter spool for events that could not be stored.

The hook receiver is fail-open: it must never block or crash the agent. When the
database is unavailable, the (already redacted) payload is appended here so it can be
replayed with ``afr ingest --replay-spool``. Past the size cap events are counted as
dropped. ``afr doctor`` reports both, so data loss is never silent.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]


def _stats_path(spool: Path) -> Path:
    return spool.with_suffix(".stats.json")


def _locked_update(path: Path, fn: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        raw = fh.read()
        data = json.loads(raw) if raw.strip() else {}
        fn(data)
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(data))


def spool_event(spool: Path, max_bytes: int, payload: dict[str, Any], error: str) -> bool:
    """Append a redacted payload. Returns False (and counts a drop) when over the cap."""
    record = json.dumps(
        {"spooled_at": datetime.now(UTC).isoformat(), "error": error[:500], "payload": payload},
        default=str,
    )
    size = spool.stat().st_size if spool.exists() else 0
    if size + len(record) + 1 > max_bytes:

        def _drop(d: dict[str, Any]) -> None:
            d["dropped"] = int(d.get("dropped", 0)) + 1
            d["last_error"] = error[:500]

        _locked_update(_stats_path(spool), _drop)
        return False
    spool.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(spool, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(record + "\n")

    def _count(d: dict[str, Any]) -> None:
        d["spooled"] = int(d.get("spooled", 0)) + 1
        d["last_error"] = error[:500]

    _locked_update(_stats_path(spool), _count)
    return True


def spool_stats(spool: Path) -> dict[str, Any]:
    p = _stats_path(spool)
    data: dict[str, Any] = json.loads(p.read_text()) if p.exists() and p.read_text().strip() else {}
    pending = 0
    if spool.exists():
        with spool.open() as fh:
            pending = sum(1 for line in fh if line.strip())
    data["pending"] = pending
    return data


def drain(spool: Path) -> list[dict[str, Any]]:
    """Atomically take all spooled records (rename-then-read)."""
    if not spool.exists():
        return []
    taken = spool.with_suffix(f".draining.{os.getpid()}")
    os.replace(spool, taken)
    records = []
    with taken.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    taken.unlink()
    return records
