"""Ranking metrics over episode-id rankings with graded relevance."""

from __future__ import annotations

import math
from collections.abc import Sequence


def recall_at_k(ranking: Sequence[str], qrels: dict[str, int], k: int) -> float:
    relevant = {d for d, g in qrels.items() if g > 0}
    if not relevant:
        return 0.0
    return len(relevant & set(ranking[:k])) / len(relevant)


def mrr(ranking: Sequence[str], qrels: dict[str, int], k: int = 10, min_grade: int = 2) -> float:
    for i, d in enumerate(ranking[:k], 1):
        if qrels.get(d, 0) >= min_grade:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranking: Sequence[str], qrels: dict[str, int], k: int) -> float:
    def dcg(grades: Sequence[int]) -> float:
        return float(sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades)))

    actual = dcg([qrels.get(d, 0) for d in ranking[:k]])
    ideal = dcg(sorted(qrels.values(), reverse=True)[:k])
    return actual / ideal if ideal else 0.0


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))
    return s[idx]
