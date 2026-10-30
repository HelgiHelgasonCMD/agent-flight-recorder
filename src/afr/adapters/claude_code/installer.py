"""Project-scoped, opt-in Claude Code hook installation.

Writes only ``<project>/.claude/settings.local.json`` (default, personal, not committed)
or ``<project>/.claude/settings.json`` (``--shared``). Never touches ``~/.claude``.
Existing settings and other hooks are preserved; our handlers are identified by their
``afr ... hook receive`` command so uninstall removes exactly what install added.
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from afr.config import CaptureLevel

RECORD_EVENTS: dict[str, str | None] = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    # attempts that change state or run commands; reads/searches are not attempts
    "PostToolUse": "Bash|PowerShell|Edit|MultiEdit|Write|NotebookEdit",
    "PostToolUseFailure": None,
    "Stop": None,
    "StopFailure": None,
    "SessionEnd": None,
}
RECORD_TIMEOUT_S = 10
PROACTIVE_TIMEOUT_S = 5


@dataclass
class Plan:
    path: Path
    before: dict[str, Any]
    after: dict[str, Any]

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def diff(self) -> str:
        a = json.dumps(self.before, indent=2, sort_keys=False).splitlines(keepends=True) if self.before else []
        b = json.dumps(self.after, indent=2, sort_keys=False).splitlines(keepends=True) if self.after else []
        return "".join(difflib.unified_diff(a, b, fromfile=f"{self.path} (current)", tofile=f"{self.path} (proposed)"))


def afr_command() -> str:
    exe = shutil.which("afr")
    if exe:
        return shlex.quote(str(Path(exe).resolve()))
    return f"{shlex.quote(sys.executable)} -m afr"


def is_afr_handler(handler: Any) -> bool:
    return (
        isinstance(handler, dict)
        and isinstance(handler.get("command"), str)
        and " hook receive" in handler["command"]
        and "afr" in handler["command"]
    )


def settings_path(project: Path, shared: bool) -> Path:
    return project / ".claude" / ("settings.json" if shared else "settings.local.json")


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _strip_afr(settings: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(settings)
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                new_groups.append(group)
                continue
            kept = [h for h in group["hooks"] if not is_afr_handler(h)]
            if kept:
                g = dict(group)
                g["hooks"] = kept
                new_groups.append(g)
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    if not hooks:
        del out["hooks"]
    return out


def plan_install(
    project: Path,
    capture_level: CaptureLevel,
    workspace: str,
    proactive: bool = False,
    shared: bool = False,
    home: Path | None = None,
    command: str | None = None,
) -> Plan:
    path = settings_path(project, shared)
    before = _load(path)
    after = _strip_afr(before)
    base = command or afr_command()
    args = f"hook receive --level {capture_level} --workspace {shlex.quote(workspace)}"
    if home is not None:
        args += f" --home {shlex.quote(str(home))}"
    hooks = after.setdefault("hooks", {})
    for event, matcher in RECORD_EVENTS.items():
        sync = proactive and event == "PostToolUseFailure"
        handler: dict[str, Any] = {
            "type": "command",
            "command": f"{base} {args}" + (" --proactive" if sync else ""),
            "timeout": PROACTIVE_TIMEOUT_S if sync else RECORD_TIMEOUT_S,
        }
        if not sync:
            handler["async"] = True  # recording never blocks the agent
        group: dict[str, Any] = {"hooks": [handler]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)
    return Plan(path, before, after)


def plan_uninstall(project: Path, shared: bool = False) -> Plan:
    path = settings_path(project, shared)
    before = _load(path)
    return Plan(path, before, _strip_afr(before))


def apply(plan: Plan) -> None:
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    if plan.path.exists():
        backup = plan.path.with_suffix(plan.path.suffix + ".afr-backup")
        shutil.copy2(plan.path, backup)
    tmp = plan.path.with_suffix(".afr-tmp")
    tmp.write_text(json.dumps(plan.after, indent=2) + "\n")
    os.replace(tmp, plan.path)


def is_gitignored(project: Path, path: Path) -> bool | None:
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", str(path)], cwd=project, capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    return None
