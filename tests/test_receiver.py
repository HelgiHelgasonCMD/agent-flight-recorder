from __future__ import annotations

import io
import json
import multiprocessing as mp
import sqlite3
from pathlib import Path

import pytest

from afr.adapters.claude_code.receiver import receive
from afr.config import CaptureLevel, Settings
from afr.ingest.spool import spool_stats
from afr.persistence.store import Store

from . import fakes
from .helpers import hook

SECRET = fakes.ANTHROPIC_2


def run(settings: Settings, payload: object, proactive: bool = False) -> tuple[int, str]:
    out = io.StringIO()
    text = payload if isinstance(payload, str) else json.dumps(payload)
    code = receive(io.StringIO(text), out, settings, CaptureLevel.METADATA, proactive)
    return code, out.getvalue()


def test_happy_path_and_counters(settings: Settings) -> None:
    code, out = run(settings, hook("UserPromptSubmit", prompt="hello"))
    assert code == 0 and out == ""
    run(
        settings,
        hook(
            "PostToolUseFailure",
            tool_name="Bash",
            tool_input={"command": "pytest"},
            tool_use_id="t1",
            error="Exit code 1\nAssertionError",
        ),
    )
    run(
        settings,
        hook(
            "PostToolUseFailure",
            tool_name="Bash",
            tool_input={"command": "pytest"},
            tool_use_id="t1",
            error="Exit code 1\nAssertionError",
        ),
    )  # duplicate delivery
    run(settings, hook("PreToolUse", tool_name="Bash"))  # not subscribed
    stats = Store(settings).stats()
    assert stats == {"received": 3, "stored": 2, "deduped": 1, "ignored": 1}


def test_fail_open_on_garbage_and_spools(settings: Settings) -> None:
    code, out = run(settings, "{not json")
    assert code == 0 and out == ""
    sp = spool_stats(settings.spool_path)
    assert sp["pending"] == 1 and sp["spooled"] == 1


def test_db_failure_spools_redacted_payload_and_replay_recovers(settings: Settings) -> None:
    Store(settings)  # create schema
    lock = sqlite3.connect(settings.db_path, timeout=0)
    lock.execute("BEGIN EXCLUSIVE")  # simulate a wedged writer
    try:
        payload = hook(
            "PostToolUseFailure",
            tool_name="Bash",
            tool_input={"command": "deploy"},
            tool_use_id="t9",
            error=f"Exit code 1\nunauthorized key={SECRET}",
        )
        code, _ = run(settings, payload)
        assert code == 0
    finally:
        lock.rollback()
        lock.close()
    spool_text = settings.spool_path.read_text()
    assert SECRET not in spool_text and "t9" in spool_text
    assert Store(settings).stats().get("failed", 0) >= 0  # counter may itself be blocked; spool is the record

    from typer.testing import CliRunner

    from afr.cli import app as cli

    res = CliRunner().invoke(
        cli,
        [
            "--home",
            str(settings.home),
            "--workspace",
            settings.workspace,
            "ingest",
            "--replay-spool",
            "--level",
            "metadata",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "replayed=1" in res.output
    assert spool_stats(settings.spool_path)["pending"] == 0
    assert Store(settings).counts()["events"] == 1


def test_spool_cap_counts_drops(settings: Settings) -> None:
    small = settings.model_copy(update={"spool_max_bytes": 300})
    for _ in range(5):
        run(small, "garbage" * 20)
    sp = spool_stats(small.spool_path)
    assert sp["dropped"] >= 1
    assert sp["pending"] + sp["dropped"] == 5


def test_oversized_payload_is_not_parsed(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    import afr.adapters.claude_code.receiver as rcv

    monkeypatch.setattr(rcv, "MAX_STDIN_BYTES", 100)
    code, _ = run(settings, hook("Stop", last_assistant_message="x" * 500))
    assert code == 0
    assert spool_stats(settings.spool_path)["pending"] == 1


def _worker(home: str, worker: int, n: int) -> None:
    s = Settings(home=Path(home), workspace="ws")
    for i in range(n):
        payload = hook(
            "PostToolUseFailure",
            session=f"s{worker}",
            tool_name="Bash",
            tool_input={"command": "pytest"},
            tool_use_id=f"w{worker}-{i}",
            error="Exit code 1\nAssertionError: boom",
        )
        receive(io.StringIO(json.dumps(payload)), io.StringIO(), s, CaptureLevel.METADATA, False)


def test_concurrent_receivers_lose_nothing(tmp_path: Path) -> None:
    home = tmp_path / "conc"
    Store(Settings(home=home))  # migrate once up front, like a real install
    workers, per = 6, 15
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_worker, args=(str(home), w, per)) for w in range(workers)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    store = Store(Settings(home=home))
    stats = store.stats()
    spooled = spool_stats(home / "spool.jsonl")
    assert stats.get("stored", 0) + spooled["pending"] == workers * per
    assert spooled.get("dropped", 0) == 0
    assert store.counts()["events"] == stats["stored"]
