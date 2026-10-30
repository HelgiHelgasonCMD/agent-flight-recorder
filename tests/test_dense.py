"""Real sentence-transformers model (downloads ~90 MB once). Run with: pytest -m dense"""

from __future__ import annotations

import pytest

from afr.app import App
from afr.config import Settings
from afr.retrieval.search import SearchRequest

from .test_retrieval import episode, load

pytestmark = pytest.mark.dense


@pytest.fixture
def dense_app(settings: Settings) -> App:
    pytest.importorskip("sentence_transformers")
    return App(settings.model_copy(update={"embedding_provider": "sentence-transformers"}))


def test_dense_finds_paraphrase_that_bm25_misses(dense_app: App) -> None:
    load(
        dense_app,
        episode(
            "d1",
            0,
            "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock",
            "add user to the docker group",
        ),
        episode("d2", 10, "fatal: refusing to merge unrelated histories", "use --allow-unrelated-histories"),
        episode("d3", 20, "KeyError: 'DATABASE_URL'", "copy .env.example"),
    )
    q = "my account lacks rights to reach the container engine"
    dense = dense_app.search(SearchRequest(query=q, workspace=dense_app.settings.workspace, mode="dense", k=3)).hits
    bm25 = dense_app.search(SearchRequest(query=q, workspace=dense_app.settings.workspace, mode="bm25", k=3)).hits
    assert dense[0].episode_ids == ["d1"]
    assert not bm25 or bm25[0].episode_ids != ["d1"]
    hybrid = dense_app.search(SearchRequest(query=q, workspace=dense_app.settings.workspace, mode="hybrid", k=3)).hits
    assert hybrid[0].episode_ids == ["d1"]
