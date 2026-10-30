"""Optional paired live-agent evaluation (retrieval arm vs no-retrieval control).

Opt-in only: it runs a real agent CLI with the user's credentials and costs money, so it
refuses to run without ``--i-understand-cost``. For every task both arms get an identical
fresh copy of the fixture project, the same prompt, model, turn/budget limits and
permission set (the same budget cap and timeout). Both arms record hook events (so tool calls and repeated failures are
measured the same way); only the retrieval arm gets the seeded experience via the MCP
server and the proactive failure hook.

Reported metrics are limited to what is actually observable:
- task success: the task's check command exit code, run by this harness afterwards;
- tool calls and repeated failed actions: from afr's own hook recording;
- elapsed time: wall clock around the agent process;
- turns / cost / tokens: only if the agent's JSON output contains them.
Results are written as raw per-run records. With few tasks, no statistical claim is
made; ``summary`` reports counts only.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import typer

from afr.config import CaptureLevel, Settings

ARMS = ("control", "retrieval")


def _afr_cmd() -> str:
    exe = shutil.which("afr")
    return shlex.quote(exe) if exe else f"{shlex.quote(sys.executable)} -m afr"


def _prepare(task: dict[str, Any], base: Path, arm: str, workdir: Path) -> dict[str, Any]:
    proj = workdir / "project"
    shutil.copytree(base / task["fixture"], proj)
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "eval@example.com"],
        ["config", "user.name", "eval"],
        ["add", "."],
        ["commit", "-qm", "fixture"],
    ):
        subprocess.run(["git", *cmd], cwd=proj, check=True, capture_output=True)
    home = workdir / "afr-home"
    settings = Settings(home=home, workspace="live-eval")
    from afr.adapters.claude_code.installer import apply, plan_install

    apply(
        plan_install(
            proj,
            CaptureLevel.SANITIZED_EVIDENCE,
            "live-eval",
            proactive=arm == "retrieval",
            shared=True,
            home=home,
            command=_afr_cmd(),
        )
    )
    argv = [
        task.get("agent_cmd", "claude"),
        "-p",
        task["prompt"],
        "--output-format",
        "json",
        # only flags listed by `claude --help` (2.1.314); turn count is bounded via budget + timeout
        "--model",
        task["model"],
        "--max-budget-usd",
        str(task.get("max_budget_usd", 1.0)),
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--allowedTools",
        *task.get("allowed_tools", ["Bash", "Edit", "Write", "Read", "Glob", "Grep"]),
    ]
    if arm == "retrieval":
        from afr.adapters.generic_jsonl import import_file
        from afr.app import App

        app = App(settings)
        import_file(app.pipeline, base / task["seed_history"], CaptureLevel.SANITIZED_EVIDENCE, "live-eval")
        mcp = workdir / "mcp.json"
        mcp.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "afr": {
                            "command": "sh",
                            "args": [
                                "-c",
                                f"{_afr_cmd()} --home {shlex.quote(str(home))} --workspace live-eval mcp serve",
                            ],
                        }
                    }
                }
            )
        )
        argv += ["--mcp-config", str(mcp)]
        argv[argv.index("--allowedTools") + 1 : argv.index("--allowedTools") + 1] = [
            "mcp__afr__search_experience",
            "mcp__afr__get_episode_evidence",
        ]
    return {"project": proj, "home": home, "argv": argv, "settings": settings}


def _observed(settings: Settings) -> dict[str, Any]:
    from afr.domain.models import EventKind
    from afr.persistence.store import Store

    store = Store(settings)
    tool_calls = failures = repeated = 0
    for ep in store.list_episodes():
        seen: set[str] = set()
        for ev in store.events_for_episode(ep.episode_id):
            if ev.kind in (EventKind.TOOL_RESULT, EventKind.TOOL_FAILURE):
                tool_calls += 1
            if ev.kind == EventKind.TOOL_FAILURE and ev.signature_id:
                failures += 1
                if ev.signature_id in seen:
                    repeated += 1
                seen.add(ev.signature_id)
    return {
        "recorded_tool_calls": tool_calls,
        "failed_tool_calls": failures,
        "repeated_failures": repeated,
        "note": "PostToolUse is matched to state-changing tools only; reads are not counted",
    }


def run_pair(task: dict[str, Any], base: Path, dry_run: bool) -> list[dict[str, Any]]:
    out = []
    for arm in ARMS:
        with tempfile.TemporaryDirectory(prefix=f"afr-live-{arm}-") as tmp:
            prep = _prepare(task, base, arm, Path(tmp))
            rec: dict[str, Any] = {"task": task["id"], "arm": arm, "argv": prep["argv"], "model": task["model"]}
            if dry_run:
                rec["dry_run"] = True
                out.append(rec)
                continue
            t0 = time.monotonic()
            try:
                proc = subprocess.run(
                    prep["argv"],
                    cwd=prep["project"],
                    capture_output=True,
                    text=True,
                    timeout=task.get("timeout_s", 900),
                    check=False,
                )
                rec["agent_exit_code"] = proc.returncode
                try:
                    agent_json = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    agent_json = {}
                for key in ("num_turns", "total_cost_usd", "usage", "duration_ms", "is_error", "subtype"):
                    if key in agent_json:
                        rec[key] = agent_json[key]
            except subprocess.TimeoutExpired:
                rec["timed_out"] = True
            rec["elapsed_s"] = round(time.monotonic() - t0, 1)
            check = subprocess.run(
                shlex.split(task["check"]),
                cwd=prep["project"],
                capture_output=True,
                timeout=300,
                check=False,
            )
            rec["check_exit_code"] = check.returncode
            rec["task_success"] = check.returncode == 0
            rec.update(_observed(prep["settings"]))
            out.append(rec)
    return out


def run_live(tasks_file: Path, agent: str, i_understand_cost: bool, dry_run: bool, output: Path) -> int:
    spec = json.loads(tasks_file.read_text())
    if not dry_run and not i_understand_cost:
        typer.echo("refusing to run a live agent without --i-understand-cost (or use --dry-run)", err=True)
        return 2
    if not dry_run and shutil.which(agent) is None:
        typer.echo(f"agent CLI {agent!r} not found on PATH", err=True)
        return 2
    base = tasks_file.parent
    runs: list[dict[str, Any]] = []
    for task in spec["tasks"]:
        task = {**spec.get("defaults", {}), **task, "agent_cmd": agent}
        runs.extend(run_pair(task, base, dry_run))
    summary: dict[str, Any] = {}
    if not dry_run:
        for arm in ARMS:
            rs = [r for r in runs if r["arm"] == arm]
            summary[arm] = {
                "runs": len(rs),
                "successes": sum(1 for r in rs if r.get("task_success")),
                "repeated_failures": sum(r.get("repeated_failures", 0) for r in rs),
            }
    result = {
        "kind": "live-agent-paired" if not dry_run else "dry-run-plan (no agent was invoked)",
        "runs": runs,
        "summary": summary,
    }
    output.write_text(json.dumps(result, indent=2, default=str))
    typer.echo(f"wrote {output} ({len(runs)} run records; {'DRY RUN, no agent invoked' if dry_run else 'live'})")
    return 0
