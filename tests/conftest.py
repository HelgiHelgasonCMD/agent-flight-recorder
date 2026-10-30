from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from afr.app import App
from afr.config import CaptureLevel, Settings
from afr.retrieval.embeddings import HashingProvider

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # never touch the developer's real data dir
    monkeypatch.setenv("AFR_HOME", str(tmp_path / "afr-home"))
    for var in ("AFR_WORKSPACE", "AFR_QDRANT_URL", "AFR_CAPTURE_LEVEL"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        home=tmp_path / "afr-home",
        workspace="ws-test",
        embedding_provider="hashing",
        capture_level=CaptureLevel.SANITIZED_EVIDENCE,
        qdrant_url=os.environ.get("AFR_TEST_QDRANT_URL") or None,
        # unique collection per test so a shared Qdrant server cannot leak state between tests
        index_prefix=f"t{uuid.uuid4().hex[:12]}",
    )


@pytest.fixture
def app(settings: Settings) -> Iterator[App]:
    a = App(settings, provider=HashingProvider())
    yield a
    if settings.qdrant_url:  # shared server: remove this test's collection
        from afr.retrieval.index import Index, open_client

        with open_client(settings) as client:
            Index(a.store, client, a.provider).drop()
