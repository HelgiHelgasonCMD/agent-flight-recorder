from __future__ import annotations

from typing import Any

import pytest

from afr.adapters.generic_jsonl import import_records
from afr.app import App
from afr.config import CaptureLevel
from afr.lessons.service import supersede
from afr.retrieval import bm25
from afr.retrieval.index import Index, open_client
from afr.retrieval.policy import QueryContext
from afr.retrieval.search import SearchRequest, WorkspaceRequired
from afr.retrieval.tokenize import tokenize

from . import fakes
from .helpers import gen

SECRET = fakes.GITHUB


def episode(
    eid: str,
    minute: float,
    error: str,
    fix: str,
    *,
    workspace: str | None = None,
    versions: dict[str, str] | None = None,
    verified: bool = True,
    lesson: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    extra: dict[str, Any] = {"episode_id": eid}
    if workspace:
        extra["workspace"] = workspace
    if versions:
        extra["versions"] = versions
    recs = [
        gen(
            f"{eid}-f",
            "tool_failure",
            minute,
            session=eid,
            tool={"name": "Bash", "command": "pytest -q"},
            error=error,
            task_summary=f"task {eid}",
            **extra,
        ),
        gen(
            f"{eid}-r",
            "tool_result",
            minute + 1,
            session=eid,
            tool={"name": "Bash", "command": fix, "exit_code": 0},
            **extra,
        ),
    ]
    if verified:
        recs.append(
            gen(
                f"{eid}-v",
                "verification",
                minute + 2,
                session=eid,
                verification={"kind": "test_command", "command": "pytest -q", "exit_code": 0, "passed": True},
                **extra,
            )
        )
    recs.append(
        gen(
            f"{eid}-l",
            "lesson",
            minute + 3,
            session=eid,
            lesson=lesson or {"problem": f"{error.splitlines()[-1]}", "successful_approach": fix},
            **extra,
        )
    )
    return recs


def load(app: App, *episodes: list[dict[str, Any]]) -> None:
    recs = [r for ep in episodes for r in ep]
    rep = import_records(app.pipeline, enumerate(recs, 1), CaptureLevel.SANITIZED_EVIDENCE, app.settings.workspace)
    assert not rep.invalid, rep.invalid


def search(app: App, query: str, **kw: Any) -> list[Any]:
    req = SearchRequest(query=query, workspace=kw.pop("workspace", app.settings.workspace), **kw)
    return app.search(req).hits


def test_tokenizer_keeps_compounds_and_parts() -> None:
    toks = tokenize(
        "Error [ERR_REQUIRE_ESM]: run with --no-cache-dir in src/app/config.py numpy==2.0.1 ModuleNotFoundError"
    )
    for t in (
        "err_require_esm",
        "require",
        "esm",
        "--no-cache-dir",
        "cache",
        "src/app/config.py",
        "numpy==2.0.1",
        "modulenotfounderror",
        "module",
        "found",
    ):
        assert t in toks, t


def test_bm25_vectors_follow_tf_saturation() -> None:
    one = bm25.encode_document("alpha beta", avgdl=2)
    many = bm25.encode_document("alpha alpha alpha alpha beta", avgdl=2)
    a = bm25.term_id("alpha")
    w1 = one.values[one.indices.index(a)]
    w4 = many.values[many.indices.index(a)]
    assert w1 < w4 < (bm25.K1 + 1)  # grows with tf but saturates


def test_exact_error_code_wins_lexically(app: App) -> None:
    load(
        app,
        episode(
            "e1", 0, "Error [ERR_REQUIRE_ESM]: require() of ES Module not supported", "set type module in package.json"
        ),
        episode("e2", 10, "Error: Cannot find module 'express'", "npm install express"),
        episode("e3", 20, "TypeError: Cannot read properties of undefined (reading 'map')", "guard undefined list"),
    )
    hits = search(app, "ERR_REQUIRE_ESM", mode="bm25")
    assert hits[0].episode_ids == ["e1"]
    assert "err_require_esm" in hits[0].matched_terms
    assert hits[0].retrievers.get("bm25") == 1


def test_idf_rare_term_outranks_common_term(app: App) -> None:
    common = "connection error while running tests"
    load(
        app,
        episode("c1", 0, f"RuntimeError: {common} zzrareterm", "fix a"),
        episode("c2", 10, f"RuntimeError: {common} {common}", "fix b"),
        episode("c3", 20, f"RuntimeError: {common}", "fix c"),
    )
    hits = search(app, "zzrareterm connection error", mode="bm25")
    assert hits[0].episode_ids == ["c1"]


def test_workspace_is_mandatory_and_isolating(app: App) -> None:
    load(
        app,
        episode("mine", 0, "KeyError: 'DATABASE_URL'", "export DATABASE_URL in .env"),
        episode("theirs", 10, "KeyError: 'DATABASE_URL' exact same text", "other team fix", workspace="other-ws"),
    )
    with pytest.raises(WorkspaceRequired):
        search(app, "DATABASE_URL", workspace="")
    hits = search(app, "KeyError DATABASE_URL exact same text", mode="hybrid", k=10)
    assert {h.workspace for h in hits} == {app.settings.workspace}
    hits = search(app, "KeyError DATABASE_URL exact same text", mode="hybrid", k=10, allow_workspaces=["other-ws"])
    assert {h.workspace for h in hits} == {app.settings.workspace, "other-ws"}


def test_filters_and_time_window(app: App) -> None:
    load(
        app,
        episode("old", 0, "ValueError: bad config", "fix old", versions={"python": "3.11.4"}),
        episode("new", 60 * 24 * 30, "ValueError: bad config again", "fix new", versions={"python": "3.12.1"}),
    )
    hits = search(app, "ValueError bad config", version="python=3.12.1")
    assert [h.episode_ids for h in hits] == [["new"]]
    from .helpers import T0

    hits = search(app, "ValueError bad config", until=T0.replace(day=15))
    assert [h.episode_ids for h in hits] == [["old"]]


def test_policy_demotes_version_mismatch_and_flags_it(app: App) -> None:
    load(
        app,
        episode(
            "v1",
            0,
            "ImportError: cannot import name 'BaseSettings' from 'pydantic'",
            "pip install pydantic-settings and import from pydantic_settings",
            versions={"pydantic": "2.7.0"},
        ),
        episode(
            "v2",
            10,
            "ImportError: cannot import name 'BaseSettings' from 'pydantic' (v1 project)",
            "pin pydantic<2",
            versions={"pydantic": "1.10.13"},
        ),
    )
    q = "ImportError: cannot import name 'BaseSettings' from 'pydantic'"
    raw = search(app, q, apply_policy=False)
    ctx = QueryContext(versions={"pydantic": "1.10.15"})
    ranked = search(app, q, context=ctx)
    assert ranked[0].episode_ids == ["v2"]
    flagged = next(h for h in ranked if h.episode_ids == ["v1"])
    assert any(c["code"] == "version_mismatch" for c in flagged.cautions)
    assert {tuple(h.episode_ids) for h in raw} == {tuple(h.episode_ids) for h in ranked}


def test_superseded_and_invalidated(app: App) -> None:
    load(
        app,
        episode("s1", 0, "OSError: [Errno 24] Too many open files", "ulimit -n 4096"),
        episode("s2", 10, "OSError: [Errno 24] Too many open files", "close file handles in the watcher"),
    )

    def reviewed(eid: str) -> str:
        return next(les.lesson_id for les in app.store.lessons_for_episode(eid) if les.origin == "manual_reviewed")

    l1, l2 = reviewed("s1"), reviewed("s2")
    supersede(app.store, l1, l2)
    hits = search(app, "Too many open files Errno 24")
    assert hits[0].lesson_id == l2
    old = next(h for h in hits if h.lesson_id == l1)
    assert any(c["code"] == "superseded" for c in old.cautions)
    from afr.lessons.service import invalidate

    invalidate(app.store, l1, "wrong")
    assert all(h.lesson_id != l1 for h in search(app, "Too many open files Errno 24"))


def test_sync_is_idempotent_and_reindex_recovers(app: App) -> None:
    load(app, episode("i1", 0, "SyntaxError: invalid syntax", "fix the typo"))
    r1 = app.sync_index()
    r2 = app.sync_index()
    assert r1.upserted >= 1 and r2.upserted == 0 and r2.unchanged == r1.total_docs
    # drop the derived index entirely: canonical data must rebuild it
    with open_client(app.settings) as client:
        Index(app.store, client, app.provider).drop()
    r3 = app.sync_index(rebuild=True)
    assert r3.total_docs == r1.total_docs
    assert search(app, "SyntaxError invalid syntax", mode="bm25")[0].episode_ids == ["i1"]


def test_qdrant_failure_does_not_lose_events(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    load(app, episode("q1", 0, "MemoryError: out of memory", "stream the file"))

    def boom(*a: Any, **k: Any) -> None:
        raise ConnectionError("qdrant down")

    monkeypatch.setattr("qdrant_client.QdrantClient.upsert", boom)
    with pytest.raises(ConnectionError):
        app.sync_index()
    monkeypatch.undo()
    assert app.store.counts()["events"] == 2  # canonical store untouched
    with open_client(app.settings) as client:
        assert Index(app.store, client, app.provider).pending() >= 1
    app.sync_index()
    with open_client(app.settings) as client:
        assert Index(app.store, client, app.provider).pending() == 0


def test_redaction_at_index_and_retrieval_boundaries(app: App) -> None:
    load(app, episode("sec", 0, f"PermissionError: denied using token {SECRET}", f"rotate {SECRET} and retry"))
    hits = search(app, f"PermissionError denied token {SECRET}", k=3)
    assert hits and SECRET not in str([h.as_dict() for h in hits])
    with open_client(app.settings) as client:
        pts, _ = client.scroll(Index(app.store, client, app.provider).collection, limit=100, with_payload=True)
    assert SECRET not in str([p.payload for p in pts])
