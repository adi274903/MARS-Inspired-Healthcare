"""Retrieval metrics: MRR@k, precision/recall/F1@k, AP@k (the PR-AUC figure) and bootstrap CIs.

Relevance is binary and judged per query; a *ranking* is an ordered list of PMIDs.
Definitions are pinned here and mirrored verbatim in ``eval/README.md``:

* ``mrr_at_k``        ``1/rank`` of the first relevant document within ``k``, else 0.
* ``precision_at_k``  ``|relevant ∩ top-k| / k``.
* ``recall_at_k``     ``|relevant ∩ top-k| / min(|relevant|, k)`` -- *budget-normalised*:
                      an 8-slot answer list can never recall more than 8 documents, so
                      dividing by the full gold size would report a ceiling, not quality.
* ``f1_at_k``         harmonic mean of the two above.
* ``max_f1_at_k``     best ``f1`` over the cutoffs ``1..k`` (the fair F1 for a ranking).
* ``ap_at_k``         ``Σ_{i: hit, i ≤ k} P@i / min(|R|, k)`` -- average precision, i.e. the
                      area under the precision-recall curve at this budget. This is the
                      number reported as "PR-AUC"; it is never reported without its ``k``.
* ``recall_ceiling``  ``min(|R|, k) / |R|`` -- the most recall a ``k``-slot budget can reach,
                      printed next to F1 so a small F1 on a large gold set is not misread.

Every mean comes with a 95% percentile bootstrap CI over *queries* (the unit of
sampling), seeded so two runs of the harness produce identical numbers.
"""
from __future__ import annotations

import random
from statistics import mean
from typing import Any, Iterable, Sequence

__all__ = [
    "mrr_at_k",
    "precision_at_k",
    "recall_at_k",
    "f1_at_k",
    "max_f1_at_k",
    "ap_at_k",
    "recall_ceiling",
    "bootstrap_ci",
    "per_query",
    "summarize",
]


def _hits(ranking: Sequence[str], relevant: Iterable[str], k: int) -> int:
    pool = set(relevant)
    return sum(1 for doc in list(ranking)[:k] if doc in pool)


def mrr_at_k(ranking: Sequence[str], relevant: Iterable[str], k: int) -> float:
    pool = set(relevant)
    if not pool:
        raise ValueError("mrr_at_k needs at least one relevant document")
    for position, doc in enumerate(list(ranking)[:k], start=1):
        if doc in pool:
            return 1.0 / position
    return 0.0


def precision_at_k(ranking: Sequence[str], relevant: Iterable[str], k: int) -> float:
    if k <= 0:
        raise ValueError("k must be positive")
    return _hits(ranking, relevant, k) / float(k)


def recall_at_k(ranking: Sequence[str], relevant: Iterable[str], k: int) -> float:
    pool = set(relevant)
    if not pool:
        raise ValueError("recall_at_k needs at least one relevant document")
    return _hits(ranking, pool, k) / float(min(len(pool), k))


def f1_at_k(ranking: Sequence[str], relevant: Iterable[str], k: int) -> float:
    p = precision_at_k(ranking, relevant, k)
    r = recall_at_k(ranking, relevant, k)
    return 0.0 if p + r == 0 else 2 * p * r / (p + r)


def max_f1_at_k(ranking: Sequence[str], relevant: Iterable[str], k: int) -> float:
    return max((f1_at_k(ranking, relevant, cutoff) for cutoff in range(1, k + 1)), default=0.0)


def ap_at_k(ranking: Sequence[str], relevant: Iterable[str], k: int) -> float:
    pool = set(relevant)
    if not pool:
        raise ValueError("ap_at_k needs at least one relevant document")
    total = 0.0
    found = 0
    for position, doc in enumerate(list(ranking)[:k], start=1):
        if doc in pool:
            found += 1
            total += found / float(position)
    return total / float(min(len(pool), k))


def recall_ceiling(relevant: Iterable[str], k: int) -> float:
    size = len(set(relevant))
    if not size:
        raise ValueError("recall_ceiling needs at least one relevant document")
    return min(size, k) / float(size)


def bootstrap_ci(values: Sequence[float], *, n: int = 1000, alpha: float = 0.05, seed: int = 1234) -> tuple[float, float]:
    """95% percentile bootstrap CI of the mean, resampling queries with a fixed seed."""
    clean = [float(v) for v in values]
    if not clean:
        return (0.0, 0.0)
    if len(clean) == 1:
        return (clean[0], clean[0])
    rng = random.Random(seed)
    size = len(clean)
    means = []
    for _ in range(n):
        means.append(mean(clean[rng.randrange(size)] for _ in range(size)))
    means.sort()
    lo = means[int((alpha / 2) * n)]
    hi = means[min(n - 1, int((1 - alpha / 2) * n))]
    return (lo, hi)


def per_query(
    rankings: Sequence[Sequence[str]],
    relevants: Sequence[Iterable[str]],
    *,
    mrr_ks: Sequence[int] = (1, 3, 8),
    f1_k: int = 8,
    ap_ks: Sequence[int] = (8, 20),
) -> list[dict[str, Any]]:
    """One row per query, with every metric that the ranking depth can support."""
    if len(rankings) != len(relevants):
        raise ValueError("rankings and relevance sets must line up")
    rows: list[dict[str, Any]] = []
    for index, (ranking, relevant) in enumerate(zip(rankings, relevants)):
        pool = set(relevant)
        depth = len(ranking)
        row: dict[str, Any] = {
            "index": index,
            "depth": depth,
            "relevant": len(pool),
            "hits": sum(1 for doc in ranking if doc in pool),
        }
        for k in mrr_ks:
            row[f"mrr@{k}"] = mrr_at_k(ranking, pool, k) if pool else 0.0
        row[f"p@{f1_k}"] = precision_at_k(ranking, pool, f1_k) if pool else 0.0
        row[f"r@{f1_k}"] = recall_at_k(ranking, pool, f1_k) if pool else 0.0
        row[f"f1@{f1_k}"] = f1_at_k(ranking, pool, f1_k) if pool else 0.0
        row[f"maxf1@{f1_k}"] = max_f1_at_k(ranking, pool, f1_k) if pool else 0.0
        row[f"ceiling@{f1_k}"] = recall_ceiling(pool, f1_k) if pool else 0.0
        for k in ap_ks:
            row[f"ap@{k}"] = ap_at_k(ranking, pool, k) if pool else 0.0
        rows.append(row)
    return rows


def summarize(
    rankings: Sequence[Sequence[str]],
    relevants: Sequence[Iterable[str]],
    *,
    mrr_ks: Sequence[int] = (1, 3, 8),
    f1_k: int = 8,
    ap_ks: Sequence[int] = (8, 20),
    ci_n: int = 1000,
    seed: int = 1234,
) -> dict[str, Any]:
    """Means + 95% bootstrap CIs over queries, for one retrieval configuration."""
    rows = per_query(rankings, relevants, mrr_ks=mrr_ks, f1_k=f1_k, ap_ks=ap_ks)
    keys = [key for key in rows[0] if key not in {"index", "depth", "relevant", "hits"}] if rows else []
    out: dict[str, Any] = {"n_queries": len(rows), "metrics": {}}
    for key in keys:
        values = [row[key] for row in rows]
        lo, hi = bootstrap_ci(values, n=ci_n, seed=seed)
        out["metrics"][key] = {"mean": mean(values) if values else 0.0, "ci95": [lo, hi]}
    out["mean_relevant"] = mean([row["relevant"] for row in rows]) if rows else 0.0
    out["mean_hits"] = mean([row["hits"] for row in rows]) if rows else 0.0
    out["mean_depth"] = mean([row["depth"] for row in rows]) if rows else 0.0
    out["per_query"] = rows
    return out
