from __future__ import annotations

from datetime import timedelta

from afr.adapters.generic_jsonl import import_records
from afr.app import App
from afr.config import CaptureLevel
from afr.domain.models import LessonOrigin, OutcomeStatus, RootCauseStatus

from . import fakes
from .helpers import gen

SECRET = fakes.GITHUB


def _attempt(app: App, level: CaptureLevel, session: str = "s1") -> None:
    recs = [
        gen("p1", "prompt", 0, session),
        gen(
            "f1",
            "tool_failure",
            1,
            session,
            tool={"name": "Bash", "command": "python -m pytest -q", "exit_code": 1},
            error=f"Exit code 1\nModuleNotFoundError: No module named 'yaml'\nGITHUB_TOKEN={SECRET}",
        ),
        gen(
            "r1",
            "tool_result",
            2,
            session,
            tool={"name": "Bash", "command": "pip install --upgrade pip", "exit_code": 0},
        ),
        gen(
            "f2",
            "tool_failure",
            3,
            session,
            tool={"name": "Bash", "command": "python -m pytest -q", "exit_code": 1},
            error="Exit code 1\nModuleNotFoundError: No module named 'yaml'",
        ),
        gen(
            "r2",
            "tool_result",
            4,
            session,
            tool={"name": "Bash", "command": "pip install pyyaml==6.0.2", "exit_code": 0},
        ),
        gen("r3", "tool_result", 5, session, tool={"name": "Bash", "command": "python -m pytest -q", "exit_code": 0}),
        gen("st", "stop", 6, session, assistant_claim="Fixed: tests pass now."),
    ]
    rep = import_records(app.pipeline, enumerate(recs, 1), level, app.settings.workspace)
    assert not rep.invalid, rep.invalid


def test_secrets_never_reach_sqlite(app: App) -> None:
    _attempt(app, CaptureLevel.SANITIZED_EVIDENCE)
    raw = b"".join(p.read_bytes() for p in app.settings.home.glob("afr.sqlite3*"))
    assert SECRET.encode() not in raw


def test_metadata_level_keeps_no_text(app: App) -> None:
    _attempt(app, CaptureLevel.METADATA)
    ep = app.store.list_episodes()[0]
    events = app.store.events_for_episode(ep.episode_id)
    tools = [e.tool for e in events if e.tool]
    assert all(t.command is None and t.files == [] for t in tools)
    assert all(e.error_excerpt is None for e in events)
    assert {t.program for t in tools} == {"python", "pip"}
    assert any(e.signature_id for e in events)  # derived, normalized signature is allowlisted


def test_observed_rerun_verifies_task_not_root_cause(app: App) -> None:
    _attempt(app, CaptureLevel.SANITIZED_EVIDENCE)
    ep = app.store.list_episodes()[0]
    assert ep.status == OutcomeStatus.VERIFIED_SUCCESS
    assert ep.root_cause_status == RootCauseStatus.UNVERIFIED
    vers = app.store.verifications_for(ep.episode_id)
    assert len(vers) == 1 and vers[0].kind == "observed_rerun"
    assert vers[0].failure_signature_ids  # linked to the failure it resolves
    les = app.store.lessons_for_episode(ep.episode_id)[0]
    assert les.origin == LessonOrigin.EVIDENCE_EXTRACTED
    assert les.successful_approach == "ran `pip install pyyaml==6.0.2`"
    assert les.failed_approaches == ["ran `pip install --upgrade pip` — the same failure recurred"]
    assert les.root_cause is None and les.root_cause_status == RootCauseStatus.UNVERIFIED


def test_metadata_only_never_invents_fix(app: App) -> None:
    _attempt(app, CaptureLevel.METADATA)
    ep = app.store.list_episodes()[0]
    les = app.store.lessons_for_episode(ep.episode_id)[0]
    assert les.successful_approach is None
    assert les.origin == LessonOrigin.CANDIDATE
    assert "metadata-only" in (les.successful_approach_missing_reason or "")


def test_claim_without_evidence_is_only_claimed(app: App) -> None:
    recs = [
        gen("p1", "prompt", 0),
        gen(
            "f1",
            "tool_failure",
            1,
            tool={"name": "Bash", "command": "make build"},
            error="fatal error: foo.h: No such file",
        ),
        gen("st", "stop", 2, assistant_claim="I've fixed the build."),
    ]
    import_records(app.pipeline, enumerate(recs, 1), CaptureLevel.SANITIZED_EVIDENCE)
    ep = app.store.list_episodes()[0]
    assert ep.status == OutcomeStatus.CLAIMED_SUCCESS
    les = app.store.lessons_for_episode(ep.episode_id)[0]
    assert les.origin == LessonOrigin.CANDIDATE and les.successful_approach is None


def test_duplicates_and_out_of_order(app: App) -> None:
    recs = [
        gen("p1", "prompt", 0),
        gen("st", "stop", 10),
        gen("late", "tool_failure", 5, tool={"name": "Bash", "command": "pytest"}, error="AssertionError: x"),
        gen("late", "tool_failure", 5, tool={"name": "Bash", "command": "pytest"}, error="AssertionError: x"),
        gen("p2", "prompt", 20),
    ]
    rep = import_records(app.pipeline, enumerate(recs, 1), CaptureLevel.METADATA)
    assert rep.stored == 4 and rep.deduped == 1
    eps = app.store.list_episodes()
    assert len(eps) == 2
    first = app.store.events_for_episode(eps[0].episode_id)
    assert [e.kind for e in first] == ["prompt", "tool_failure", "stop"]  # late event landed in its window
    stats = app.store.stats()
    assert stats["received"] == 5 and stats["deduped"] == 1 and stats["stored"] == 4


def test_missing_prompt_creates_episode(app: App) -> None:
    recs = [gen("f1", "tool_failure", 1, tool={"name": "Bash", "command": "pytest"}, error="TypeError: bad")]
    import_records(app.pipeline, enumerate(recs, 1), CaptureLevel.METADATA)
    assert len(app.store.list_episodes()) == 1


def test_distinct_tasks_not_merged(app: App) -> None:
    _attempt(app, CaptureLevel.SANITIZED_EVIDENCE, session="a")
    _attempt(app, CaptureLevel.SANITIZED_EVIDENCE, session="b")
    eps = app.store.list_episodes()
    assert len(eps) == 2
    assert len({e.episode_id for e in eps}) == 2
    assert app.store.signature_occurrences(app.store.events_for_episode(eps[0].episode_id)[1].signature_id or "") == 4
    assert eps[1].started_at - eps[0].started_at == timedelta(0)
