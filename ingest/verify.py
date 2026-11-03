"""Cross-store consistency check: jsonl vs SQLite vs Qdrant.

Prints one table and exits non-zero when the JSONL and the database disagree
(that pair is the source of truth; Qdrant and Neo4j are derived and optional).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from retrieval.sqlite_store import PaperStore, connect
from retrieval.stats import corpus_overview

FLAG_KEYS = (
    "internal_val", "external_val", "temporal_val", "deployed", "rct", "ci_reported",
    "calibration_reported",
)
AXIS_KEYS = (
    "a_domain", "b_end_user", "c_use_case", "d_analytics", "e_formulation", "f_algorithm", "g_eval_tier",
)


def jsonl_pmids(path: Path) -> tuple[int, list[str]]:
    """(line count, distinct pmids in first-seen order)."""
    lines = 0
    seen: dict[str, None] = {}
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            lines += 1
            pmid = str(json.loads(line).get("pmid") or "").strip()
            if pmid:
                seen.setdefault(pmid, None)
    return lines, list(seen)


def db_pmids(db_path: Path) -> tuple[int, list[str]]:
    with connect(Path(db_path), readonly=True) as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        rows = conn.execute("SELECT pmid FROM papers ORDER BY pmid").fetchall()
    return total, [str(row["pmid"]) for row in rows]


def qdrant_pmids(path: Path, collection: str, sample: str) -> dict[str, Any]:
    """Payload lookup for one pmid, plus the collection count (empty when absent)."""
    try:
        from retrieval.vector_store import VectorStore
    except Exception:  # noqa: BLE001
        return {"available": False, "count": 0, "payload": None}
    store = VectorStore(Path(path), collection)
    if not store.available():
        return {"available": False, "count": 0, "payload": None}
    return {"available": True, "count": store.count(), "payload": store.by_pmid(sample)}


def spot_check(pmids: list[str], db: list[str], count: int = 10) -> list[dict[str, Any]]:
    if not pmids:
        return []
    step = max(len(pmids) // count, 1)
    chosen = pmids[::step][:count]
    db_set = set(db)
    return [{"pmid": pmid, "jsonl": True, "sqlite": pmid in db_set} for pmid in chosen]


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest.verify", description="Cross-store consistency report.")
    parser.add_argument("--jsonl", default="data/papers.jsonl")
    parser.add_argument("--db", default="data/papers.db")
    parser.add_argument("--qdrant-path", default="data/qdrant")
    parser.add_argument("--collection", default="evalbot_papers")
    args = parser.parse_args(argv)

    jsonl_path, db_path = Path(args.jsonl), Path(args.db)
    if not jsonl_path.exists():
        print(f"missing jsonl: {jsonl_path}", file=sys.stderr)
        return 2
    if not db_path.exists():
        print(f"missing db: {db_path}", file=sys.stderr)
        return 2

    jsonl_lines, jsonl_ids = jsonl_pmids(jsonl_path)
    db_total, db_ids = db_pmids(db_path)
    store = PaperStore(db_path)
    qdrant = qdrant_pmids(Path(args.qdrant_path), args.collection, jsonl_ids[0] if jsonl_ids else "")
    vector_store = None
    if qdrant["available"]:
        from retrieval.vector_store import VectorStore

        vector_store = VectorStore(Path(args.qdrant_path), args.collection)

    ok = True
    print("=" * 74)
    print("cross-store consistency")
    print("=" * 74)
    print(f"{'store':<26}{'rows':>12}{'distinct pmid':>16}{'status':>18}")
    print("-" * 74)
    qdrant_rows = qdrant["count"] if qdrant["available"] else "n/a"
    print(f"{'papers.jsonl':<26}{jsonl_lines:>12}{len(jsonl_ids):>16}{'-':>18}")
    print(f"{'papers.db (papers)':<26}{db_total:>12}{len(set(db_ids)):>16}{'-':>18}")
    print(f"{'qdrant ' + args.collection:<26}{str(qdrant_rows):>12}{str(qdrant_rows):>16}"
          f"{('ok' if qdrant['available'] else 'absent'):>18}")

    if len(jsonl_ids) != db_total or len(jsonl_ids) != len(set(db_ids)):
        ok = False
        print("-" * 74)
        print(f"MISMATCH  jsonl distinct pmids={len(jsonl_ids)}  db rows={db_total}  db distinct={len(set(db_ids))}")
    else:
        print("-" * 74)
        print(f"MATCH     jsonl and sqlite agree on {len(jsonl_ids)} distinct pmids")

    print()
    print("-" * 74)
    print("10-pmid spot check")
    print("-" * 74)
    print(f"{'pmid':<14}{'jsonl':>8}{'sqlite':>8}{'qdrant':>10}{'year':>8}  journal")
    rows = spot_check(jsonl_ids, db_ids)
    hydrate = store.get_many([row["pmid"] for row in rows]) if rows else {}
    for row in rows:
        payload = vector_store.by_pmid(row["pmid"]) if vector_store is not None else None
        vector_flag = "yes" if payload else ("no" if vector_store is not None else "n/a")
        record = hydrate.get(row["pmid"], {})
        print(
            f"{row['pmid']:<14}{'yes':>8}{('yes' if row['sqlite'] else 'NO'):>8}"
            f"{vector_flag:>10}{str(record.get('year') or '-'):>8}  {record.get('journal') or '-'}"
        )
        if not row["sqlite"]:
            ok = False

    overview = corpus_overview(db_path)
    print()
    print("-" * 74)
    print(f"corpus overview: total={overview['total']}  years={overview['year_range'][0]}..{overview['year_range'][1]}")
    print("-" * 74)
    print(f"{'flags':<26}{'n':>8}")
    for key in FLAG_KEYS:
        print(f"{key:<26}{overview['flags'].get(key, 0):>8}")
    print()
    print(f"{'axis':<20}{'top values':<54}")
    for key in AXIS_KEYS:
        values = overview["facets"].get(key, [])[:3]
        joined = ", ".join(f"{v['key']} ({v['n']})" for v in values) or "-"
        print(f"{key:<20}{joined:<54}")
    print("=" * 74)
    print("result: OK" if ok else "result: FAILED")
    return 0 if ok else 1


def main() -> None:  # pragma: no cover
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
