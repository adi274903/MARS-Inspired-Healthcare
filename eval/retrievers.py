"""Retrieval configurations for the evaluation harness.

Every leg here is a **mirror of a shipped service**, not a reimplementation:

* ``leg_sqlite``   <- ``services/sqlite_service/main.py``  (``PaperStore.search``: FTS5 BM25
  with the soft-filter fallback, so a filter that matches <3 rows falls back to unfiltered)
* ``leg_vector``   <- ``services/vector_service/main.py``  (``max(k*4, 20)`` candidates, no
  facet filters, asymmetric EmbeddingGemma query prompt)
* ``leg_graph``    <- ``services/graph_service/main.py``   (named template only; the default
  ``papers_by_author`` with params taken from the question, evaluated over SQLite when
  Neo4j is not configured)
* ``fuse``         <- ``services/orchestrator/main.py``    (``rrf.fuse`` with the production
  weights and ``k``)

``run_eval.py`` asserts that the hybrid config's leg list equals
``services.orchestrator.main.decide_legs`` for every query, so the harness cannot silently
measure a path the product does not run.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from retrieval import rrf
from retrieval.graph import LocalGraph, extract_graph_params, render
from retrieval.queryparse import ParsedQuery
from retrieval.sqlite_store import PaperStore
from retrieval.vector_store import Embedder, VectorStore

#: Same weights the orchestrator passes to ``rrf.fuse``.
PRODUCTION_WEIGHTS = {"sqlite": 1.0, "vector": 1.0, "graph": 0.8}
#: ``MIN_CANDIDATES`` in the vector service: the leg always retrieves at least 20.
VECTOR_MIN_CANDIDATES = 20
#: Default template in the graph service when the parser names none.
GRAPH_DEFAULT_TEMPLATE = "papers_by_author"
#: The graph service forces ``limit`` from the payload, defaulting to 20.
GRAPH_DEFAULT_LIMIT = 20


@dataclass
class Stores:
    """The three read-only stores plus the graph fallback, opened once per run."""

    db_path: Path
    qdrant_path: Path
    collection: str
    embed_provider: str = "local"
    embed_model: str = "google/embeddinggemma-2"
    embed_dim: int = 768
    embed_device: str = "cpu"
    store: PaperStore = field(init=False)
    embedder: Embedder = field(init=False)
    vector: VectorStore = field(init=False)
    graph: LocalGraph = field(init=False)

    def __post_init__(self) -> None:
        self.store = PaperStore(Path(self.db_path))
        self.embedder = Embedder(
            provider=self.embed_provider,
            model=self.embed_model,
            dim=self.embed_dim,
            device=self.embed_device,
        )
        self.vector = VectorStore(Path(self.qdrant_path), self.collection)
        self.graph = LocalGraph(self.store)

    def describe(self) -> dict[str, Any]:
        return {
            "db": str(self.db_path),
            "db_rows": self.store.count() if self.store.available() else 0,
            "qdrant": str(self.qdrant_path),
            "collection": self.collection,
            "index_dim": self.vector.dimension(),
            "vectors": self.vector.count(),
            "embed_provider": self.embedder.provider,
            "embed_model": self.embedder.model_name,
            "embed_dim": self.embedder.dim,
            "embedder_available": self.embedder.available(),
        }

    def close(self) -> None:
        self.vector.close()


def leg_sqlite(stores: Stores, parsed: ParsedQuery, k: int) -> list[str]:
    """Mirror of the sqlite leg: filtered BM25 with the relaxed fallback."""
    pmids, _filters_applied = stores.store.search(parsed, k)
    return pmids[:k]


def leg_vector(stores: Stores, question: str, k: int) -> list[str]:
    """Mirror of the vector leg: unfiltered ANN, ``max(k*4, 20)`` candidates."""
    if not stores.embedder.available() or not stores.vector.available():
        return []
    limit = max(k * 4, VECTOR_MIN_CANDIDATES)
    vector = stores.embedder.query(question)
    if not vector:
        return []
    return stores.vector.search(vector, limit=limit)


def leg_graph(stores: Stores, parsed: ParsedQuery, question: str) -> list[str]:
    """Mirror of the graph leg: one named template, params from the question only."""
    template = parsed.graph_template or GRAPH_DEFAULT_TEMPLATE
    params = extract_graph_params(parsed, question)
    params["limit"] = GRAPH_DEFAULT_LIMIT
    allowed = {"author", "limit"}
    render(template, {k: v for k, v in params.items() if k in allowed})
    out = stores.graph.query(template, {k: v for k, v in params.items() if k in allowed})
    return [str(p) for p in out.get("pmids") or []]


def leg_vector_random(stores: Stores, question: str, k: int, *, seed: int) -> list[str]:
    """Negative control: the real ANN index queried with a seeded random unit vector."""
    if not stores.vector.available():
        return []
    rng = random.Random(f"{seed}:{question}")
    dim = stores.vector.dimension() or stores.embedder.dim
    vector = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = sum(value * value for value in vector) ** 0.5 or 1.0
    return stores.vector.search([value / norm for value in vector], limit=max(k * 4, VECTOR_MIN_CANDIDATES))


@dataclass(frozen=True)
class Config:
    """One retrieval configuration under test."""

    name: str
    legs: tuple[str, ...]
    sqlite_k: int
    vector_k: int
    rrf_k: int = 60
    limit: int = 8
    weights: dict[str, float] = field(default_factory=lambda: dict(PRODUCTION_WEIGHTS))
    note: str = ""
    random_seed: int = 0


def rank(stores: Stores, config: Config, question: str, parsed: ParsedQuery) -> list[str]:
    """Run one config on one question, returning the ranking exactly as the leg/fusion step emits it."""
    if len(config.legs) == 1 and config.legs[0] != "graph":
        leg = config.legs[0]
        if leg == "sqlite":
            return leg_sqlite(stores, parsed, config.sqlite_k)[: config.limit]
        if leg == "vector":
            if config.name.endswith("_random"):
                return leg_vector_random(stores, question, config.vector_k, seed=config.random_seed)[: config.limit]
            return leg_vector(stores, question, config.vector_k)[: config.limit]
    if config.legs == ("graph",):
        return leg_graph(stores, parsed, question)[: config.limit]

    ranked: dict[str, list[str]] = {}
    for leg in ("sqlite", "vector", "graph"):
        if leg not in config.legs:
            continue
        if leg == "sqlite":
            pmids = leg_sqlite(stores, parsed, config.sqlite_k)
        elif leg == "vector":
            if config.name.endswith("_random"):
                pmids = leg_vector_random(stores, question, config.vector_k, seed=config.random_seed)
            else:
                pmids = leg_vector(stores, question, config.vector_k)
        else:
            pmids = leg_graph(stores, parsed, question)
        if pmids:
            ranked[leg] = pmids
    fused = rrf.fuse(ranked, k=config.rrf_k, weights=config.weights, limit=config.limit)
    return [pmid for pmid, _score in fused]


def configs(num_evidence: int = 8, depth: int = 20) -> dict[str, Config]:
    """The measured configurations. ``hybrid_prod`` is the shipped path, byte for byte."""
    return {
        "bm25": Config(
            name="bm25", legs=("sqlite",), sqlite_k=depth, vector_k=depth, limit=depth,
            note="FTS5 BM25 + production facet filters (+ soft-filter fallback)",
        ),
        "dense": Config(
            name="dense", legs=("vector",), sqlite_k=depth, vector_k=depth, limit=depth,
            note="768-d EmbeddingGemma ANN, unfiltered, no fusion",
        ),
        "graph": Config(
            name="graph", legs=("graph",), sqlite_k=depth, vector_k=depth, limit=depth,
            note="default named template (papers_by_author) over the SQLite fallback",
        ),
        "hybrid_prod": Config(
            name="hybrid_prod", legs=("sqlite", "vector", "graph"), sqlite_k=num_evidence,
            vector_k=num_evidence, rrf_k=60, limit=num_evidence,
            note="PRODUCTION: sqlite k=8, vector 32 candidates, graph 20, RRF k=60, 8 slots",
        ),
        "hybrid20": Config(
            name="hybrid20", legs=("sqlite", "vector", "graph"), sqlite_k=depth,
            vector_k=depth, rrf_k=60, limit=depth,
            note="same fusion at depth 20 so AP@20 is comparable across configs",
        ),
        "hybrid_nograph": Config(
            name="hybrid_nograph", legs=("sqlite", "vector"), sqlite_k=depth, vector_k=depth,
            rrf_k=60, limit=depth, note="ablation: does the graph leg contribute at all?",
        ),
        "hybrid_k30": Config(
            name="hybrid_k30", legs=("sqlite", "vector", "graph"), sqlite_k=depth, vector_k=depth,
            rrf_k=30, limit=depth, note="RRF k sweep",
        ),
        "hybrid_k120": Config(
            name="hybrid_k120", legs=("sqlite", "vector", "graph"), sqlite_k=depth, vector_k=depth,
            rrf_k=120, limit=depth, note="RRF k sweep",
        ),
        "dense_random": Config(
            name="dense_random", legs=("vector",), sqlite_k=depth, vector_k=depth, limit=depth,
            random_seed=20261103,
            note="NEGATIVE CONTROL: same index, seeded random query vector (must score ~0)",
        ),
    }


def rank_with_config(stores: Stores, config: Config, question: str, parsed: ParsedQuery) -> list[str]:
    return rank(stores, config, question, parsed)


def unique_preserving_order(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


def open_stores(*, embed_provider: str | None = None) -> Stores:
    """Open the stores from the environment, using the same env vars as the services.

    ``EVAL_EMBED_PROVIDER`` exists only so the harness can be pointed at ``hash`` for a
    deliberately broken embedding comparison; the measured runs use the real model.
    """
    import os

    from core.config import get_settings

    settings = get_settings()
    provider = embed_provider or os.environ.get("EVAL_EMBED_PROVIDER") or "local"
    return Stores(
        db_path=Path(settings.db_path),
        qdrant_path=Path(settings.qdrant_path),
        collection=settings.qdrant_collection,
        embed_provider=provider,
        embed_model=settings.embed_model,
        embed_dim=settings.embed_dim,
        embed_device=settings.embed_device or "cpu",
    )
