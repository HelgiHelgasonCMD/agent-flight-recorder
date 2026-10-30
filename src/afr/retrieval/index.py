"""Qdrant index: derived, rebuildable, never authoritative.

SQLite holds the canonical records. ``sync`` computes documents from SQLite, compares a
content hash with ``index_state`` and upserts only what changed, using deterministic
UUIDv5 point ids so repeated or interrupted syncs are idempotent. If Qdrant is down,
nothing canonical is lost: the next sync (or ``afr index rebuild``) catches up, and
``afr doctor`` reports the lag.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from afr.config import Settings
from afr.persistence.store import Store
from afr.retrieval import bm25
from afr.retrieval.documents import Doc, build_documents
from afr.retrieval.embeddings import EmbeddingProvider

NAMESPACE = uuid.UUID("5b3e2a59-6f41-4d0e-9a57-2f1f7f7d3a10")
DENSE = "dense"
SPARSE = "bm25"
KEYWORD_FIELDS = (
    "workspace",
    "repo_id",
    "doc_type",
    "status",
    "task_status",
    "origin",
    "language",
    "tools",
    "error_types",
    "signature_ids",
    "versions_flat",
    "lesson_id",
)
FLOAT_FIELDS = ("created_ts", "verified_ts")
EMBED_BATCH = 64


def point_id(doc_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE, doc_id))


class IndexLocked(RuntimeError):
    pass


LOCK_WAIT_S = 5.0


@contextlib.contextmanager
def open_client(settings: Settings) -> Iterator[QdrantClient]:
    """Embedded (local, file-locked) by default; server when AFR_QDRANT_URL is set.

    Embedded mode allows one process at a time; we wait briefly for the lock and
    otherwise fail with a clear message (use a Qdrant server for concurrent access).
    """
    if settings.qdrant_url:
        client = QdrantClient(url=settings.qdrant_url, timeout=settings.qdrant_timeout_s)
    else:
        settings.qdrant_path.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                client = QdrantClient(path=str(settings.qdrant_path))
                break
            except RuntimeError as exc:
                if "already accessed" not in str(exc):
                    raise
                if time.monotonic() > deadline:
                    raise IndexLocked(
                        f"embedded Qdrant at {settings.qdrant_path} is in use by another process; "
                        "retry, or run a Qdrant server and set AFR_QDRANT_URL for concurrent access"
                    ) from exc
                time.sleep(0.2)
    try:
        yield client
    finally:
        client.close()


@dataclass
class SyncReport:
    collection: str
    total_docs: int
    upserted: int
    deleted: int
    unchanged: int
    avgdl: float


class Index:
    def __init__(self, store: Store, client: QdrantClient, provider: EmbeddingProvider) -> None:
        self.store = store
        self.client = client
        self.provider = provider
        self.collection = f"{store.settings.index_prefix}_{provider.name}_{provider.dim}"
        self.embedded = type(getattr(client, "_client", None)).__name__ == "QdrantLocal"

    # -- collection --------------------------------------------------------------
    def ensure_collection(self) -> None:
        if self.client.collection_exists(self.collection):
            return
        self.client.create_collection(
            self.collection,
            vectors_config={DENSE: qm.VectorParams(size=self.provider.dim, distance=qm.Distance.COSINE)},
            sparse_vectors_config={SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
        )
        # Payload indexes matter for the server; embedded mode ignores them (and warns).
        if self.embedded:
            return
        for f in KEYWORD_FIELDS:
            self.client.create_payload_index(self.collection, f, qm.PayloadSchemaType.KEYWORD)
        for f in FLOAT_FIELDS:
            self.client.create_payload_index(self.collection, f, qm.PayloadSchemaType.FLOAT)

    def drop(self) -> None:
        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)

    # -- sync ----------------------------------------------------------------------
    def _avgdl(self, docs: list[Doc], recompute: bool) -> float:
        key = f"bm25_avgdl:{self.collection}"
        stored = self.store.kv_get(key)
        if stored is not None and not recompute:
            return float(stored)
        lengths = [bm25.doc_length(d.text) for d in docs] or [50]
        avg = sum(lengths) / len(lengths)
        self.store.kv_set(key, avg)
        return avg

    def _points(self, docs: list[Doc], avgdl: float) -> list[qm.PointStruct]:
        dense = self.provider.embed([d.text for d in docs])
        points = []
        for doc, vec in zip(docs, dense, strict=True):
            sv = bm25.encode_document(doc.text, avgdl)
            points.append(
                qm.PointStruct(
                    id=point_id(doc.doc_id),
                    vector={DENSE: vec, SPARSE: qm.SparseVector(indices=sv.indices, values=sv.values)},
                    payload={**doc.payload, "doc_id": doc.doc_id},
                )
            )
        return points

    def sync(self, rebuild: bool = False) -> SyncReport:
        if rebuild:
            self.drop()
            self.store.forget_index_state()
        self.ensure_collection()
        docs = build_documents(self.store)
        avgdl = self._avgdl(docs, recompute=rebuild)
        state = self.store.index_states()
        changed = [d for d in docs if state.get(d.doc_id, (None, None)) != (d.content_hash, self.collection)]
        current_ids = {d.doc_id for d in docs}
        stale = [doc_id for doc_id in state if doc_id not in current_ids]
        now = datetime.now(UTC)
        for i in range(0, len(changed), EMBED_BATCH):
            batch = changed[i : i + EMBED_BATCH]
            self.client.upsert(self.collection, self._points(batch, avgdl), wait=True)
            self.store.mark_indexed(
                [
                    {
                        "doc_id": d.doc_id,
                        "doc_type": d.doc_type,
                        "workspace": d.workspace,
                        "content_hash": d.content_hash,
                        "indexed_hash": d.content_hash,
                        "indexed_at": now,
                        "collection": self.collection,
                        "error": None,
                    }
                    for d in batch
                ]
            )
        if stale:
            self.client.delete(
                self.collection,
                points_selector=qm.PointIdsList(points=[point_id(s) for s in stale]),
                wait=True,
            )
            self.store.forget_index_state(stale)
        return SyncReport(self.collection, len(docs), len(changed), len(stale), len(docs) - len(changed), avgdl)

    def pending(self) -> int:
        """Docs whose canonical content is not (yet) reflected in this collection."""
        docs = build_documents(self.store)
        state = self.store.index_states()
        stale = sum(1 for doc_id in state if doc_id not in {d.doc_id for d in docs})
        return stale + sum(1 for d in docs if state.get(d.doc_id, (None, None)) != (d.content_hash, self.collection))

    def count(self) -> int:
        if not self.client.collection_exists(self.collection):
            return 0
        return int(self.client.count(self.collection, exact=True).count)

    def info(self) -> dict[str, Any]:
        return {"collection": self.collection, "points": self.count(), "provider": self.provider.name}
