"""Reciprocal Rank Fusion over the retrieval legs."""
from __future__ import annotations

from typing import Iterable, Mapping


def fuse(
    ranked: Mapping[str, Iterable[str]],
    *,
    k: int = 60,
    weights: Mapping[str, float] | None = None,
    limit: int = 50,
) -> list[tuple[str, float]]:
    """``ranked`` maps leg name -> ordered ids. Higher score = better."""
    weights = dict(weights or {})
    scores: dict[str, float] = {}
    for leg, ids in ranked.items():
        w = weights.get(leg, 1.0)
        for position, doc_id in enumerate(ids):
            scores[doc_id] = scores.get(doc_id, 0.0) + w / (k + position + 1)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered[:limit]


def weighted_lists(*, graph_cap: int = 20) -> dict[str, float]:
    """Default leg weights: BM25 and dense agree most often; graph adds reach."""
    return {"sqlite": 1.0, "fts": 1.0, "vector": 1.0, "graph": 0.8, "limit": graph_cap}
