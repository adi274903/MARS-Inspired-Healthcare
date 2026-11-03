"""``data/papers.jsonl`` -> Neo4j property graph.

Only the shapes :data:`retrieval.graph.TEMPLATES` can query are guaranteed:
``Paper.pmid``/``Paper.year``, ``Author.name``, ``Journal.name``, ``MeSH.name``
with the ``AUTHORED`` / ``PUBLISHED_IN`` / ``HAS_MESH`` relationships.

Axis facets become their own nodes (``Domain``, ``UseCase``, ``Formulation``,
``Algorithm``, ``Model``) and the eval tier is both a ``Paper`` property and an
``EVAL_AT`` edge, mirroring the ``USES`` / ``EVAL_AT`` intent without inventing
templates the retriever does not know about.

If the driver or the server is missing, the build skips with a printed message
instead of failing: the graph leg is optional at runtime.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

# (label, property) - one uniqueness constraint each.
CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("Paper", "pmid"),
    ("Author", "name"),
    ("Journal", "name"),
    ("MeSH", "name"),
    ("Domain", "name"),
    ("UseCase", "name"),
    ("Formulation", "name"),
    ("Algorithm", "name"),
    ("Model", "name"),
)

PAPERS_CYPHER = """
UNWIND $rows AS row
MERGE (p:Paper {pmid: row.pmid})
SET p.year = row.year,
    p.title = row.title,
    p.doi = row.doi,
    p.journal = row.journal,
    p.a_domain = row.a_domain,
    p.b_end_user = row.b_end_user,
    p.c_use_case = row.c_use_case,
    p.d_analytics = row.d_analytics,
    p.e_formulation = row.e_formulation,
    p.f_algorithm = row.f_algorithm,
    p.g_eval_tier = row.g_eval_tier,
    p.external_val = row.external_val,
    p.temporal_val = row.temporal_val,
    p.deployed = row.deployed,
    p.rct = row.rct,
    p.models = row.models,
    p.metrics_reported = row.metrics_reported
"""

AUTHORS_CYPHER = """
UNWIND $rows AS row
UNWIND row.authors AS name
WITH row, name WHERE name IS NOT NULL AND name <> ''
MERGE (a:Author {name: name})
MERGE (p:Paper {pmid: row.pmid})
MERGE (a)-[:AUTHORED]->(p)
"""

JOURNALS_CYPHER = """
UNWIND $rows AS row
WITH row WHERE row.journal IS NOT NULL AND row.journal <> ''
MERGE (j:Journal {name: row.journal})
MERGE (p:Paper {pmid: row.pmid})
MERGE (p)-[:PUBLISHED_IN]->(j)
"""

MESH_CYPHER = """
UNWIND $rows AS row
UNWIND row.mesh_names AS name
WITH row, name WHERE name IS NOT NULL AND name <> ''
MERGE (m:MeSH {name: name})
MERGE (p:Paper {pmid: row.pmid})
MERGE (p)-[:HAS_MESH]->(m)
"""

DOMAIN_CYPHER = """
UNWIND $rows AS row
WITH row WHERE row.a_domain IS NOT NULL AND row.a_domain <> ''
MERGE (n:Domain {name: row.a_domain})
MERGE (p:Paper {pmid: row.pmid})
MERGE (p)-[:IN_DOMAIN]->(n)
"""

USE_CASE_CYPHER = """
UNWIND $rows AS row
WITH row WHERE row.c_use_case IS NOT NULL AND row.c_use_case <> ''
MERGE (n:UseCase {name: row.c_use_case})
MERGE (p:Paper {pmid: row.pmid})
MERGE (p)-[:HAS_USE_CASE]->(n)
"""

FORMULATION_CYPHER = """
UNWIND $rows AS row
WITH row WHERE row.e_formulation IS NOT NULL AND row.e_formulation <> ''
MERGE (n:Formulation {name: row.e_formulation})
MERGE (p:Paper {pmid: row.pmid})
MERGE (p)-[:HAS_FORMULATION]->(n)
"""

ALGORITHM_CYPHER = """
UNWIND $rows AS row
WITH row WHERE row.f_algorithm IS NOT NULL AND row.f_algorithm <> ''
MERGE (n:Algorithm {name: row.f_algorithm})
MERGE (p:Paper {pmid: row.pmid})
MERGE (p)-[:USES_ALGORITHM]->(n)
"""

MODELS_CYPHER = """
UNWIND $rows AS row
UNWIND row.model_list AS name
WITH row, name WHERE name IS NOT NULL AND name <> ''
MERGE (n:Model {name: name})
MERGE (p:Paper {pmid: row.pmid})
MERGE (p)-[:USES]->(n)
"""

EVAL_CYPHER = """
UNWIND $rows AS row
WITH row WHERE row.g_eval_tier IS NOT NULL AND row.g_eval_tier <> ''
MERGE (n:EvaluationTier {name: row.g_eval_tier})
MERGE (p:Paper {pmid: row.pmid})
MERGE (p)-[:EVAL_AT]->(n)
"""

BLOCKS: tuple[tuple[str, str], ...] = (
    ("papers", PAPERS_CYPHER),
    ("authors", AUTHORS_CYPHER),
    ("journals", JOURNALS_CYPHER),
    ("mesh", MESH_CYPHER),
    ("domains", DOMAIN_CYPHER),
    ("use_cases", USE_CASE_CYPHER),
    ("formulations", FORMULATION_CYPHER),
    ("algorithms", ALGORITHM_CYPHER),
    ("models", MODELS_CYPHER),
    ("eval_tiers", EVAL_CYPHER),
)

_SPLIT = re.compile(r"\s*[;,]\s*")


def constraint_cypher(label: str, prop: str) -> str:
    return f"CREATE CONSTRAINT {label.lower()}_{prop}_unique IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"


def split_models(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [part for part in (p.strip() for p in _SPLIT.split(text)) if part]


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def to_node_row(row: dict) -> dict:
    """One JSONL record -> the flat parameter row every Cypher block consumes."""
    return {
        "pmid": str(row.get("pmid") or "").strip(),
        "year": row.get("year"),
        "title": row.get("title") or "",
        "doi": row.get("doi") or "",
        "journal": row.get("journal") or "",
        "a_domain": row.get("a_domain") or "",
        "b_end_user": row.get("b_end_user") or "",
        "c_use_case": row.get("c_use_case") or "",
        "d_analytics": row.get("d_analytics") or "",
        "e_formulation": row.get("e_formulation") or "",
        "f_algorithm": row.get("f_algorithm") or "",
        "g_eval_tier": row.get("g_eval_tier") or "",
        "external_val": row.get("external_val"),
        "temporal_val": row.get("temporal_val"),
        "deployed": row.get("deployed"),
        "rct": row.get("rct"),
        "models": row.get("models_used") or "",
        "model_list": split_models(row.get("models_used")),
        "metrics_reported": row.get("metrics_reported") or "",
        "authors": _as_list(row.get("authors")),
        "mesh_names": _as_list(row.get("mesh_names")),
    }


def read_jsonl(path: Path) -> Iterable[dict]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def _batches(rows: list[dict], size: int) -> Iterable[list[dict]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def build(
    jsonl: Path,
    uri: str,
    user: str,
    password: str,
    *,
    batch: int = 2000,
    limit: int | None = None,
) -> dict:
    """Load the graph. Returns ``{"skipped": True, ...}`` when Neo4j is unavailable."""
    jsonl = Path(jsonl)
    if not jsonl.exists():
        raise FileNotFoundError(f"jsonl not found: {jsonl}")
    if not uri:
        print("[neo4j] NEO4J_URI is not set - skipping graph build (graph leg runs on SQLite instead).")
        return {"skipped": True, "reason": "no uri"}

    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("[neo4j] the neo4j driver is not installed - skipping graph build.")
        return {"skipped": True, "reason": "no driver"}

    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001 - any connection problem means "graph unavailable"
        print(f"[neo4j] cannot reach {uri}: {exc} - skipping graph build.")
        return {"skipped": True, "reason": str(exc)}

    rows: list[dict] = []
    for raw in read_jsonl(jsonl):
        node = to_node_row(raw)
        if not node["pmid"]:
            continue
        rows.append(node)
        if limit is not None and len(rows) >= limit:
            break

    stats: dict[str, Any] = {"jsonl": str(jsonl), "uri": uri, "rows": len(rows), "batches": 0, "skipped": False}
    try:
        with driver.session() as session:
            for label, prop in CONSTRAINTS:
                session.run(constraint_cypher(label, prop)).consume()
            print(f"[neo4j] {len(CONSTRAINTS)} constraints ensured")
            for name, query in BLOCKS:
                written = 0
                for chunk in _batches(rows, batch):
                    summary = session.run(query, rows=chunk).consume()
                    stats["batches"] += 1
                    written += int(getattr(summary.counters, "nodes_created", 0) or 0)
                print(f"[neo4j] {name:<14} rows={len(rows)} batches={-(-len(rows) // batch) if rows else 0}")
                stats[f"{name}_nodes"] = written
    finally:
        driver.close()

    print(f"[neo4j] done: {len(rows)} papers, {stats['batches']} batched statements")
    return stats


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest.neo4j_graph", description="Load papers.jsonl into Neo4j.")
    parser.add_argument("--jsonl", default="data/papers.jsonl")
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", ""))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD", ""))
    parser.add_argument("--batch", type=int, default=2000)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        result = build(
            Path(args.jsonl),
            args.uri,
            args.user,
            args.password,
            batch=args.batch,
            limit=args.limit,
        )
    except FileNotFoundError as exc:
        print(f"{exc}", file=sys.stderr)
        print("  run `python -m ingest.ingest` first to produce the jsonl.", file=sys.stderr)
        return 2
    # a skipped graph build is not a failure: the graph leg is optional and
    # falls back to LocalGraph over SQLite (build_all.sh keeps going).
    if result.get("skipped"):
        print(f"[neo4j] graph build skipped ({result.get('reason', 'unavailable')})")
    return 0


def main() -> None:  # pragma: no cover
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
