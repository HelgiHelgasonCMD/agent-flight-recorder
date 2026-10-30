from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from afr.adapters.claude_code.installer import apply, is_afr_handler, plan_install, plan_uninstall
from afr.cli import app as cli
from afr.config import CaptureLevel

EXISTING = {
    "permissions": {"allow": ["Bash(npm test)"]},
    "hooks": {"PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "prettier --write"}]}]},
}


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.local.json").write_text(json.dumps(EXISTING))
    return proj


def test_install_preserves_existing_and_uninstall_restores(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    plan = plan_install(proj, CaptureLevel.METADATA, "ws", command="/opt/afr")
    assert plan.path == proj / ".claude" / "settings.local.json"
    assert "+" in plan.diff() and "prettier --write" in json.dumps(plan.after)
    apply(plan)
    data = json.loads(plan.path.read_text())
    assert data["permissions"] == EXISTING["permissions"]
    handlers = [h for groups in data["hooks"].values() for g in groups for h in g["hooks"]]
    ours = [h for h in handlers if is_afr_handler(h)]
    assert len(ours) == 7 and all(h.get("async") for h in ours)
    assert all("--level metadata" in h["command"] for h in ours)

    again = plan_install(proj, CaptureLevel.METADATA, "ws", command="/opt/afr")
    assert not again.changed  # idempotent

    un = plan_uninstall(proj)
    apply(un)
    assert json.loads(plan.path.read_text()) == EXISTING


def test_proactive_handler_is_sync_with_short_timeout(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    plan = plan_install(proj, CaptureLevel.SANITIZED_EVIDENCE, "ws", proactive=True, command="/opt/afr")
    fail = plan.after["hooks"]["PostToolUseFailure"][-1]["hooks"][0]
    assert "--proactive" in fail["command"] and "async" not in fail and fail["timeout"] <= 5


def test_cli_dry_run_writes_nothing(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    before = (proj / ".claude" / "settings.local.json").read_text()
    res = CliRunner().invoke(cli, ["hook", "install", "--project", str(proj), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "dry run" in res.output and "hook receive" in res.output
    assert (proj / ".claude" / "settings.local.json").read_text() == before


def test_cli_refuses_non_project_scope(tmp_path: Path) -> None:
    res = CliRunner().invoke(cli, ["hook", "install", "--scope", "user", "--project", str(tmp_path)])
    assert res.exit_code == 2


def test_never_targets_user_global(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    for shared in (False, True):
        plan = plan_install(proj, CaptureLevel.METADATA, "ws", shared=shared, command="/opt/afr")
        assert plan.path.is_relative_to(proj)
        assert Path.home() / ".claude" not in plan.path.parents or proj in plan.path.parents
