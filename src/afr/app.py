"""Composition root: settings → store, pipeline, embedding provider, index/search.

Retrieval dependencies (qdrant-client, embedding models) are imported lazily so that
ingest-only paths such as the hook receiver and ``afr ingest`` stay fast.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from functools import cached_property
from typing import TYPE_CHECKING

from afr.config import Settings
from afr.ingest.pipeline import Pipeline
from afr.persistence.store import Store

if TYPE_CHECKING:
    from afr.retrieval.embeddings import EmbeddingProvider
    from afr.retrieval.index import SyncReport
    from afr.retrieval.search import Searcher, SearchRequest, SearchResult


class App:
    def __init__(self, settings: Settings | None = None, provider: EmbeddingProvider | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self.store = Store(self.settings)
        self.pipeline = Pipeline(self.store, self.settings)
        self._provider = provider

    @cached_property
    def provider(self) -> EmbeddingProvider:
        from afr.retrieval.embeddings import make_provider

        return self._provider or make_provider(self.settings.embedding_provider, self.settings.embedding_model)

    @contextlib.contextmanager
    def searcher(self, sync: bool = True) -> Iterator[Searcher]:
        from afr.retrieval.index import open_client
        from afr.retrieval.search import Searcher

        with open_client(self.settings) as client:
            s = Searcher(self.store, client, self.provider)
            if sync:
                s.index.sync()
            yield s

    def search(self, req: SearchRequest, sync: bool = True) -> SearchResult:
        with self.searcher(sync=sync) as s:
            return s.search(req)

    def sync_index(self, rebuild: bool = False) -> SyncReport:
        from afr.retrieval.index import Index, open_client

        with open_client(self.settings) as client:
            return Index(self.store, client, self.provider).sync(rebuild=rebuild)
