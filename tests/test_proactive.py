from __future__ import annotations

import io
import json

from afr.adapters.claude_code.receiver import receive
from afr.app import App
from afr.config import CaptureLevel

from .helpers import hook
from .test_retrieval import episode, load

ERR = "Exit code 1\nError: listen EADDRINUSE: address already in use :::3000"


def fire(app: App, session: str, tool_use_id: str, error: str = ERR) -> str:
    out = io.StringIO()
    payload = hook(
        "PostToolUseFailure",
        session=session,
        tool_name="Bash",
        tool_input={"command": "npm run dev"},
        tool_use_id=tool_use_id,
        error=error,
    )
    assert receive(io.StringIO(json.dumps(payload)), out, app.settings, CaptureLevel.METADATA, True) == 0
    return out.getvalue()


def _seed(app: App) -> None:
    # record the same failure through the hook path, then a verified, reviewed lesson for it
    fire(app, "seed", "t0")
    ep = app.store.list_episodes()[0]
    from afr.adapters.generic_jsonl import import_records

    from .helpers import gen

    import_records(
        app.pipeline,
        enumerate(
            [
                gen(
                    "v",
                    "verification",
                    5,
                    session="seed",
                    episode_id=ep.episode_id,
                    verification={
                        "kind": "test_command",
                        "command": "npm run dev -- --check",
                        "exit_code": 0,
                        "passed": True,
                    },
                ),
                gen(
                    "l",
                    "lesson",
                    6,
                    session="seed",
                    episode_id=ep.episode_id,
                    lesson={
                        "problem": "dev server port 3000 already taken",
                        "successful_approach": "stop the stale dev server (lsof -i :3000) before restarting",
                        "contraindications": ["another app intentionally uses port 3000"],
                    },
                ),
            ],
            1,
        ),
        CaptureLevel.SANITIZED_EVIDENCE,
    )


def test_proactive_emits_bounded_untrusted_context_once(app: App) -> None:
    _seed(app)
    out = fire(app, "new-session", "t1")
    data = json.loads(out)
    hso = data["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUseFailure"
    ctx = hso["additionalContext"]
    assert "untrusted" in ctx and "stale dev server" in ctx and "same failure signature" in ctx
    assert len(ctx) <= 1500
    assert fire(app, "new-session", "t2") == ""  # deduped per (session, signature)
    assert app.store.stats()["proactive_deduped"] == 1


def test_proactive_respects_workspace_and_verification(app: App) -> None:
    load(
        app,
        episode("u1", 0, "Error: listen EADDRINUSE: address already in use :::3000", "unverified idea", verified=False),
    )
    assert fire(app, "s", "t1") == ""  # unverified experience is never pushed
    other = app.settings.model_copy(update={"workspace": "other"})
    _seed(app)
    out = io.StringIO()
    payload = hook(
        "PostToolUseFailure",
        session="x",
        tool_name="Bash",
        tool_input={"command": "npm run dev"},
        tool_use_id="t9",
        error=ERR,
    )
    receive(io.StringIO(json.dumps(payload)), out, other, CaptureLevel.METADATA, True)
    assert out.getvalue() == ""


def test_over_budget_emits_nothing(app: App) -> None:
    _seed(app)
    slow = app.settings.model_copy(update={"proactive_budget_ms": 0})
    out = io.StringIO()
    payload = hook(
        "PostToolUseFailure",
        session="b",
        tool_name="Bash",
        tool_input={"command": "npm run dev"},
        tool_use_id="tb",
        error=ERR,
    )
    receive(io.StringIO(json.dumps(payload)), out, slow, CaptureLevel.METADATA, True)
    assert out.getvalue() == ""
    assert app.store.stats()["proactive_over_budget"] == 1


def test_rate_limit_per_session(app: App) -> None:
    _seed(app)
    limited = app.settings.model_copy(update={"proactive_max_per_session": 0})
    out = io.StringIO()
    payload = hook(
        "PostToolUseFailure",
        session="r",
        tool_name="Bash",
        tool_input={"command": "npm run dev"},
        tool_use_id="tr",
        error=ERR,
    )
    receive(io.StringIO(json.dumps(payload)), out, limited, CaptureLevel.METADATA, True)
    assert out.getvalue() == "" and app.store.stats()["proactive_rate_limited"] == 1
