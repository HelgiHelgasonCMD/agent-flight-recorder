"""BM25 as Qdrant sparse vectors.

Document vectors carry the BM25 term-frequency component
``tf·(k1+1) / (tf + k1·(1 - b + b·dl/avgdl))``; the collection's sparse vector is
configured with ``Modifier.IDF`` so Qdrant multiplies in inverse document frequency at
query time from live collection statistics. Query vectors are binary term indicators.
The product summed over matching terms is Okapi BM25.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass

from afr.retrieval.tokenize import tokenize

K1 = 1.2
B = 0.75


def term_id(term: str) -> int:
    return int.from_bytes(hashlib.blake2b(term.encode(), digest_size=4).digest(), "big") & 0x7FFFFFFF


@dataclass(frozen=True)
class SparseVec:
    indices: list[int]
    values: list[float]


def doc_length(text: str) -> int:
    return len(tokenize(text))


def encode_document(text: str, avgdl: float) -> SparseVec:
    tokens = tokenize(text)
    dl = len(tokens) or 1
    tf: Counter[int] = Counter(term_id(t) for t in tokens)
    norm = K1 * (1 - B + B * dl / max(avgdl, 1.0))
    items = sorted((idx, n * (K1 + 1) / (n + norm)) for idx, n in tf.items())
    return SparseVec([i for i, _ in items], [v for _, v in items])


def encode_query(text: str) -> SparseVec:
    ids = sorted({term_id(t) for t in tokenize(text)})
    return SparseVec(ids, [1.0] * len(ids))
