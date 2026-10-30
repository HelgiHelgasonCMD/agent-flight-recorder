from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from afr.adapters.generic_jsonl import import_records
from afr.app import App
from afr.config import CaptureLevel
from afr.domain.models import OutcomeStatus, RootCauseStatus, VerificationScope
from afr.lessons.service import add_manual_lesson
from afr.verification import verify_with_command

from .helpers import gen


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=r, check=True)
    (r / "check.py").write_text("import sys, json\nsys.exit(0 if json.load(open('cfg.json'))['ok'] else 1)\n")
    (r / "cfg.json").write_text('{"ok": false}')
    subprocess.run(["git", "add", "."], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True)
    return r


def _episode(app: App) -> str:
    recs = [
        gen("p", "prompt", 0, episode_id="ep_v"),
        gen(
            "f",
            "tool_failure",
            1,
            episode_id="ep_v",
            tool={"name": "Bash", "command": "python check.py"},
            error="Exit code 1\nAssertionError: config not ok",
        ),
        gen("e", "tool_result", 2, episode_id="ep_v", tool={"name": "Edit", "files": ["cfg.json"]}),
    ]
    import_records(app.pipeline, enumerate(recs, 1), CaptureLevel.SANITIZED_EVIDENCE)
    return "ep_v"


def test_failing_then_passing_check_records_state(app: App, repo: Path) -> None:
    eid = _episode(app)
    ev, _run = verify_with_command(app.pipeline, eid, [sys.executable, "check.py"], repo)
    assert not ev.passed and ev.exit_code == 1
    assert app.store.get_episode(eid).status == OutcomeStatus.VERIFIED_FAILURE
    fp_before = ev.environment.worktree_fingerprint

    (repo / "cfg.json").write_text('{"ok": true}')
    ev2, _ = verify_with_command(app.pipeline, eid, [sys.executable, "check.py"], repo)
    assert ev2.passed and ev2.exit_code == 0
    assert ev2.environment.git_head and len(ev2.environment.git_head) == 40
    assert ev2.environment.worktree_dirty is True
    assert ev2.environment.worktree_fingerprint != fp_before  # tested state differs
    assert ev2.failure_signature_ids  # linked to the failure it resolves
    ep = app.store.get_episode(eid)
    assert ep.status == OutcomeStatus.VERIFIED_SUCCESS
    assert ep.root_cause_status == RootCauseStatus.UNVERIFIED  # passing tests ≠ verified explanation


def test_root_cause_requires_discriminating_check(app: App, repo: Path) -> None:
    eid = _episode(app)
    (repo / "cfg.json").write_text('{"ok": true}')
    verify_with_command(app.pipeline, eid, [sys.executable, "check.py"], repo)
    claim = "cfg.json had ok=false"
    les = add_manual_lesson(
        app.store,
        [eid],
        "config check fails",
        successful_approach="set ok=true in cfg.json",
        root_cause=claim,
        root_cause_status=RootCauseStatus.VERIFIED,
    )
    assert les.root_cause_status == RootCauseStatus.SUPPORTED  # cannot self-declare "verified"

    with pytest.raises(ValueError):
        verify_with_command(app.pipeline, eid, [sys.executable, "check.py"], repo, VerificationScope.ROOT_CAUSE)
    # discriminating check: reverting only that value reproduces the failure
    probe = [
        sys.executable,
        "-c",
        "import json,subprocess,sys; json.dump({'ok': False}, open('probe.json','w')); "
        "sys.exit(0 if json.load(open('probe.json'))['ok'] is False else 1)",
    ]
    ev, _ = verify_with_command(
        app.pipeline, eid, probe, repo, VerificationScope.ROOT_CAUSE, claim=claim, lesson_id=les.lesson_id
    )
    assert ev.passed
    assert app.store.get_lesson(les.lesson_id).root_cause_status == RootCauseStatus.VERIFIED
    assert app.store.get_episode(eid).status == OutcomeStatus.VERIFIED_SUCCESS
