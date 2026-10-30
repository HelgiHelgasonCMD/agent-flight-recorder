"""Embedding providers.

``SentenceTransformerProvider`` is the real dense retriever (CPU, small model).
``HashingProvider`` is a deterministic, *non-semantic* feature-hashing stand-in used
only by fast unit tests; benchmark results are never produced with it.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Protocol

from afr.retrieval.tokenize import tokenize


class EmbeddingProvider(Protocol):
    name: str
    dim: int
    semantic: bool

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingProvider:
    semantic = False

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim
        self.name = f"hashing-{dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in tokenize(text):
                h = hashlib.blake2b(tok.encode(), digest_size=8).digest()
                idx = int.from_bytes(h[:4], "big") % self.dim
                vec[idx] += 1.0 if h[4] & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class SentenceTransformerProvider:
    semantic = True

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.name = "st-" + re.sub(r"[^a-z0-9]+", "-", model_name.lower().split("/")[-1]).strip("-")
        self._model: Any = None
        self.dim = 384 if "minilm-l6" in model_name.lower() else self._load().get_sentence_embedding_dimension()

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - depends on install extras
                raise RuntimeError("dense retrieval needs the 'dense' extra: uv sync --extra dense") from exc
            try:  # use the local cache without a network round-trip when the model is present
                self._model = SentenceTransformer(self.model_name, device="cpu", local_files_only=True)
            except OSError:
                self._model = SentenceTransformer(self.model_name, device="cpu")
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vecs = model.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, v)) for v in vecs]


def make_provider(kind: str, model_name: str) -> EmbeddingProvider:
    if kind == "hashing":
        return HashingProvider()
    if kind in ("sentence-transformers", "st"):
        return SentenceTransformerProvider(model_name)
    raise ValueError(f"unknown embedding provider {kind!r}")
