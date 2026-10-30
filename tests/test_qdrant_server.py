"""Server-mode checks. Run with a Qdrant server: AFR_TEST_QDRANT_URL=http://localhost:6333 pytest -m qdrant_server"""

from __future__ import annotations

import os

import pytest

from afr.app import App
from afr.retrieval.index import Index, open_client
from afr.retrieval.search import SearchRequest

from .test_retrieval import episode, load

pytestmark = [
    pytest.mark.qdrant_server,
    pytest.mark.skipif(not os.environ.get("AFR_TEST_QDRANT_URL"), reason="needs AFR_TEST_QDRANT_URL"),
]


def test_server_payload_indexes_and_hybrid(app: App) -> None:
    assert app.settings.qdrant_url
    load(
        app,
        episode("srv1", 0, "Error [ERR_REQUIRE_ESM]: require() of ES Module", "pin node-fetch@2"),
        episode("srv2", 10, "KeyError: 'x'", "default"),
        episode("srv3", 20, "other ws", "x", workspace="w2"),
    )
    app.sync_index()
    with open_client(app.settings) as client:
        idx = Index(app.store, client, app.provider)
        assert not idx.embedded
        info = client.get_collection(idx.collection)
        assert {"workspace", "repo_id", "error_types", "created_ts"} <= set(info.payload_schema)
        sparse = info.config.params.sparse_vectors["bm25"]
        assert str(sparse.modifier).lower().endswith("idf")
    hits = app.search(SearchRequest(query="ERR_REQUIRE_ESM", workspace=app.settings.workspace, mode="hybrid")).hits
    assert hits[0].episode_ids == ["srv1"]
    assert all(h.workspace == app.settings.workspace for h in hits)
    with open_client(app.settings) as client:
        Index(app.store, client, app.provider).drop()
