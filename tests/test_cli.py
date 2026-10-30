from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from afr.cli import app as cli
from afr.export.bundle import validate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AFR_EMBEDDING_PROVIDER", "hashing")
    monkeypatch.setenv("AFR_INDEX_PREFIX", f"cli{tmp_path.name[-8:]}")
    home = tmp_path / "home"
    runner = CliRunner()

    def _run(*args: str, code: int = 0) -> str:
        res = runner.invoke(cli, ["--home", str(home), "--workspace", "cli", *args])
        assert res.exit_code == code, res.output
        return res.output

    return _run


def test_ingest_search_show_export_doctor(run, tmp_path: Path) -> None:
    out = run("ingest", str(ROOT / "examples" / "events.jsonl"))
    assert "stored=7" in out and "invalid=0" in out
    out = run("ingest", str(ROOT / "examples" / "events.jsonl"))
    assert "deduped=7" in out  # idempotent re-import

    out = run("search", "sqlite3.OperationalError: database is locked", "--json")
    data = json.loads(out)
    top = data["hits"][0]
    assert top["task_status"] == "verified_success"  # observed re-run of the failing command
    assert top["fix"] == "edit jobs/export.py"

    out = run("episode", "show", top["episode_ids"][0])
    assert "observed_rerun" in out and "database is locked" in out

    out = run("lesson", "list")
    assert top["lesson_id"] in out

    bundle_path = tmp_path / "bundle.json"
    out = run("export", "-o", str(bundle_path))
    assert "schema valid" in out
    validate(json.loads(bundle_path.read_text()))

    out = run("doctor")
    assert "ingest_counters" in out and "pending=0" in out


def test_verify_requires_command(run) -> None:
    run("verify", "ep_missing", code=2)


def test_generic_schema_rejects_bad_records(run, tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps(
            {
                "schema": "afr.generic-event.v1",
                "event_id": "x",
                "ts": "2026-01-01T00:00:00Z",
                "session_id": "s",
                "agent": "a",
                "kind": "tool_failure",
            }
        )
        + "\n"
    )
    out = run("ingest", str(bad), code=2)
    assert "invalid=1" in out


def test_search_has_no_cross_workspace_by_default(run) -> None:
    from afr.retrieval.search import SearchRequest, build_filter

    flt = build_filter(SearchRequest(query="x", workspace="cli"))
    assert flt.must[0].key == "workspace" and flt.must[0].match.any == ["cli"]
