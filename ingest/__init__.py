"""One-time build pipeline: xlsx -> papers.jsonl -> papers.db + Qdrant + Neo4j.

Nothing in this package touches the dataset at import time. The xlsx is only
opened by ``python -m ingest.ingest`` when a human runs the CLI.
"""
from __future__ import annotations

__all__ = ["ingest", "sqlite_build", "qdrant_index", "neo4j_graph", "verify"]
