from __future__ import annotations

import json

import pytest
from mcp import Client

from afr.app import App
from afr.mcp_server.server import build_server

from .test_retrieval import episode, load


def _data(result) -> dict:
    if getattr(result, "structured_content", None):
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_mcp_tools_end_to_end(app: App) -> None:
    load(
        app,
        episode(
            "m1",
            0,
            "Error: listen EADDRINUSE: address already in use :::3000",
            "kill the stale dev server on port 3000",
        ),
        episode("m2", 10, "leaked", "nope", workspace="other"),
    )
    server = build_server(app)
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == {"search_experience", "get_episode_evidence", "report_task_outcome"}

        res = _data(await client.call_tool("search_experience", {"query": "EADDRINUSE :::3000", "mode": "hybrid"}))
        assert "untrusted" in res["notice"]
        top = res["hits"][0]
        assert top["episode_ids"] == ["m1"] and top["task_status"] == "verified_success"
        assert top["evidence"] and top["evidence"][0]["exit_code"] == 0
        assert all(h["workspace"] == app.settings.workspace for h in res["hits"])

        ev = _data(await client.call_tool("get_episode_evidence", {"episode_id": "m1"}))
        assert ev["verification_status"] == "verified_success" and ev["verifications"]

        bad = await client.call_tool("get_episode_evidence", {"episode_id": "m2"})
        assert bad.is_error  # other workspace is invisible

        fb = _data(
            await client.call_tool(
                "report_task_outcome",
                {
                    "task_ref": "new-task-1",
                    "consulted_lesson_ids": [top["lesson_id"], "les_made_up"],
                    "reported_outcome": "succeeded",
                },
            )
        )
        assert fb["objective_outcome"] == "unknown"  # self-report is not evidence
        assert fb["consulted_lesson_ids"] == [top["lesson_id"]]  # unknown ids dropped


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
