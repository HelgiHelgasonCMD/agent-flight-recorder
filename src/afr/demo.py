"""``afr demo``: deterministic, self-contained end-to-end demonstrations.

Everything runs in a throw-away directory: a tiny git project, an isolated AFR data dir
and embedded Qdrant. Hook events are *scripted* payloads in the documented Claude Code
format, fed through the real receiver; the test commands inside them are actually run
against the fixture project so exit codes and error text are genuine. No agent or
network access is involved.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from afr.app import App
from afr.config import CaptureLevel, Settings

TEST_CMD = [sys.executable, "-m", "unittest", "-q"]
TEST_CMD_STR = "python -m unittest -q"


def say(title: str) -> None:
    typer.echo("\n" + typer.style(f"▶ {title}", fg="cyan", bold=True))


def show(text: str) -> None:
    typer.echo(textwrap.indent(text.rstrip(), "  "))


def _run(cwd: Path) -> tuple[int, str]:
    p = subprocess.run(TEST_CMD, cwd=cwd, capture_output=True, text=True, timeout=60, check=False)
    return p.returncode, (p.stdout + p.stderr).strip()


def _project(root: Path) -> Path:
    proj = root / "billing-service"
    proj.mkdir()
    (proj / "app.py").write_text(
        "import json\n\n\ndef load(path='config.json'):\n    with open(path) as fh:\n        return json.load(fh)\n"
    )
    (proj / "test_config.py").write_text(
        "import unittest\n\nimport app\n\n\nclass ConfigTest(unittest.TestCase):\n"
        "    def test_loads(self):\n        self.assertEqual(app.load()['currency'], 'EUR')\n\n"
        "    def test_settings(self):\n        self.assertTrue(app.load('settings.json')['retry'])\n"
    )
    (proj / "config.json").write_text('{\n  "currency": "EUR",\n  "precision": 2,\n}\n')
    (proj / "settings.json").write_text('{\n  "retry": true\n}\n')
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "demo@example.com"],
        ["config", "user.name", "demo"],
        ["add", "."],
        ["commit", "-qm", "initial"],
    ):
        subprocess.run(["git", *cmd], cwd=proj, check=True, capture_output=True)
    return proj


class HookFeeder:
    """Feeds documented-format hook payloads through the real receiver."""

    def __init__(self, settings: Settings, session: str, cwd: Path, proactive: bool = False) -> None:
        self.settings, self.session, self.cwd, self.proactive = settings, session, cwd, proactive
        self.n = 0

    def send(self, event: str, **fields: Any) -> str:
        from afr.adapters.claude_code.receiver import receive

        payload = {
            "session_id": self.session,
            "transcript_path": "/dev/null",
            "cwd": str(self.cwd),
            "permission_mode": "default",
            "hook_event_name": event,
            **fields,
        }
        out = io.StringIO()
        receive(io.StringIO(json.dumps(payload)), out, self.settings, CaptureLevel.SANITIZED_EVIDENCE, self.proactive)
        return out.getvalue()

    def bash(self, command: str, code: int, output: str) -> str:
        self.n += 1
        if code == 0:
            return self.send(
                "PostToolUse",
                tool_name="Bash",
                tool_input={"command": command},
                tool_response={"stdout": output, "stderr": "", "interrupted": False, "isImage": False},
                tool_use_id=f"toolu_{self.session}_{self.n}",
            )
        return self.send(
            "PostToolUseFailure",
            tool_name="Bash",
            tool_input={"command": command},
            tool_use_id=f"toolu_{self.session}_{self.n}",
            error=f"Exit code {code}\n{output}",
        )

    def edit(self, path: Path) -> None:
        self.n += 1
        self.send(
            "PostToolUse",
            tool_name="Edit",
            tool_input={"file_path": str(path)},
            tool_response={"filePath": str(path)},
            tool_use_id=f"toolu_{self.session}_{self.n}",
        )


def scenario_repeat(app: App, root: Path) -> None:
    from afr.cli import print_hits
    from afr.retrieval.search import SearchRequest
    from afr.verification import verify_with_command

    proj = _project(root)
    s = app.settings
    say("Session 1: the agent hits a config error, retries a wrong fix, then fixes it")
    a = HookFeeder(s, "session-one", proj)
    a.send("SessionStart", source="startup", model="demo-model")
    a.send("UserPromptSubmit", prompt="The config tests are failing, please fix them")
    code, out = _run(proj)
    show(f"$ {TEST_CMD_STR}  → exit {code}\n" + out.splitlines()[-1])
    a.bash(TEST_CMD_STR, code, out)
    a.bash("pip install --upgrade simplejson", 0, "Requirement already satisfied: simplejson (simulated)")
    show("$ pip install --upgrade simplejson   (wrong fix)")
    code, out = _run(proj)
    show(f"$ {TEST_CMD_STR}  → exit {code}   (same failure again)")
    a.bash(TEST_CMD_STR, code, out)
    (proj / "config.json").write_text('{\n  "currency": "EUR",\n  "precision": 2\n}\n')
    a.edit(proj / "config.json")
    show("edit config.json: remove the trailing comma")
    code, out = _run(proj)
    show(f"$ {TEST_CMD_STR}  → exit {code}")
    a.bash(TEST_CMD_STR, code, out)
    a.send("Stop", last_assistant_message="Fixed: config.json had a trailing comma.")
    episode_id = app.store.list_episodes(s.workspace)[-1].episode_id

    say("Objective verification: afr verify runs the check and records exit code + tested state")
    ev, _ = verify_with_command(app.pipeline, episode_id, TEST_CMD, proj)
    ep = app.store.get_episode(episode_id)
    assert ep is not None
    show(
        f"evidence {ev.evidence_id}: `{TEST_CMD_STR}` exit={ev.exit_code} head={(ev.environment.git_head or '')[:10]} "
        f"tree={ev.environment.worktree_fingerprint}\nepisode {ep.episode_id}: outcome={ep.status}, "
        f"root cause={ep.root_cause_status} (passing tests do not verify an explanation)"
    )
    les = app.store.lessons_for_episode(ep.episode_id)[0]
    show(
        f"extracted lesson {les.lesson_id} ({les.origin}):\n  fix    : {les.successful_approach}\n"
        f"  failed : {les.failed_approaches}\n  basis  : {les.confidence_basis}"
    )

    say("Session 2 (new task, proactive hook on): the same class of error appears in another file")
    subprocess.run(["git", "commit", "-qam", "fix config"], cwd=proj, check=True, capture_output=True)
    (proj / "settings.json").write_text('{\n  "retry": true,\n}\n')
    b = HookFeeder(s, "session-two", proj, proactive=True)
    b.send("UserPromptSubmit", prompt="Add retry settings")
    code, out = _run(proj)
    show(f"$ {TEST_CMD_STR}  → exit {code}\n" + out.splitlines()[-1])
    ctx = b.bash(TEST_CMD_STR, code, out)
    if ctx:
        show("hook → Claude additionalContext:\n" + json.loads(ctx)["hookSpecificOutput"]["additionalContext"])
    else:
        show("(proactive hook returned nothing)")

    say("afr search with the exact error line (hybrid BM25 + dense, RRF)")
    err_line = [ln for ln in out.splitlines() if "JSONDecodeError" in ln][-1]
    show(f'$ afr search "{err_line}"')
    res = app.search(SearchRequest(query=err_line, workspace=s.workspace, k=3))
    print_hits(res)

    say("Provenance: afr episode show <episode_id>")
    from afr.cli import render_episode

    render_episode(app, res.hits[0].episode_ids[0])


def scenario_trap(app: App, root: Path) -> None:
    from afr.adapters.generic_jsonl import import_records
    from afr.cli import print_hits
    from afr.retrieval.policy import QueryContext
    from afr.retrieval.search import SearchRequest

    s = app.settings
    say("History: two verified fixes for the same SSL symptom, with different root causes")
    err = (
        "urllib.error.URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "unable to get local issuer certificate (_ssl.c:1006)>"
    )

    now = datetime.now(UTC).replace(microsecond=0)

    def episode(eid: str, days_ago: int, cmd: str, fix: str, lesson: dict[str, Any]) -> list[dict[str, Any]]:
        base = {"schema": "afr.generic-event.v1", "session_id": eid, "episode_id": eid, "agent": "demo"}

        def t(minute: int) -> str:
            return (now - timedelta(days=days_ago, minutes=-minute)).isoformat()

        return [
            {
                **base,
                "event_id": f"{eid}-1",
                "ts": t(1),
                "kind": "tool_failure",
                "tool": {"name": "Bash", "command": cmd},
                "error": err,
            },
            {
                **base,
                "event_id": f"{eid}-2",
                "ts": t(2),
                "kind": "tool_result",
                "tool": {"name": "Bash", "command": fix, "exit_code": 0},
            },
            {
                **base,
                "event_id": f"{eid}-3",
                "ts": t(3),
                "kind": "verification",
                "verification": {"kind": "test_command", "command": cmd, "exit_code": 0, "passed": True},
            },
            {**base, "event_id": f"{eid}-4", "ts": t(4), "kind": "lesson", "lesson": lesson},
        ]

    recs = episode(
        "ep_ssl_macos",
        40,
        "python download_models.py",
        "/Applications/Python 3.12/Install Certificates.command",
        {
            "problem": "Python urlopen fails with SSL CERTIFICATE_VERIFY_FAILED on a fresh python.org macOS install",
            "successful_approach": "run 'Install Certificates.command' from the python.org installer",
            "applies_when": {"os": "darwin"},
        },
    )
    recs += episode(
        "ep_ssl_proxy",
        12,
        "pip install -r requirements.txt",
        "update-ca-certificates && export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt",
        {
            "problem": "SSL CERTIFICATE_VERIFY_FAILED in Linux containers behind the TLS-intercepting proxy",
            "successful_approach": "install the corporate root CA (update-ca-certificates) and set REQUESTS_CA_BUNDLE",
            "applies_when": {"os": "linux"},
        },
    )
    import_records(app.pipeline, enumerate(recs, 1), CaptureLevel.SANITIZED_EVIDENCE, s.workspace)
    show(
        "ep_ssl_macos: fix = run Install Certificates.command   (applies_when os=darwin)\n"
        "ep_ssl_proxy: fix = install corporate CA               (applies_when os=linux)"
    )

    query = "python urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed downloading models"
    say("New task on a Linux CI runner — raw retrieval order (no applicability policy)")
    show(f'$ afr search "{query}" --no-policy -k 2')
    print_hits(app.search(SearchRequest(query=query, workspace=s.workspace, k=2, apply_policy=False)))
    say("Same query with the current environment (--os linux): the macOS-only fix is flagged and downranked")
    show(f'$ afr search "{query}" --os linux -k 2')
    print_hits(app.search(SearchRequest(query=query, workspace=s.workspace, k=2, context=QueryContext(os="linux"))))


def run_demo(scenario: str, home: Path | None, embedding: str | None) -> int:
    if scenario not in ("repeat", "trap", "all"):
        raise typer.BadParameter("scenario must be repeat, trap or all")
    with tempfile.TemporaryDirectory(prefix="afr-demo-") as tmp:
        root = Path(tmp)
        base = Settings.from_env()
        settings = Settings(
            home=home or root / "afr-home",
            workspace="demo",
            embedding_provider=embedding or base.embedding_provider,
            embedding_model=base.embedding_model,
        )
        app = App(settings)
        typer.echo(
            typer.style(f"afr demo — isolated data dir {settings.home} (embedding: {app.provider.name})", bold=True)
        )
        if scenario in ("repeat", "all"):
            scenario_repeat(app, root)
        if scenario in ("trap", "all"):
            if scenario == "all":
                app = App(settings.model_copy(update={"workspace": "demo-trap"}), provider=app.provider)
            scenario_trap(app, root)
        typer.echo("\n" + typer.style("demo complete", fg="green", bold=True))
    return 0
