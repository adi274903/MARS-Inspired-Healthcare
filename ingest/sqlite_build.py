"""``data/papers.jsonl`` -> ``data/papers.db`` (SQLite + FTS5).

The schema is imported from :mod:`retrieval.sqlite_store` so the build and the
read path can never drift. List-valued fields (authors, mesh_names, ...) are
stored as JSON, exactly like ``tests/support.build_fixture_db`` does.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

from retrieval.sqlite_store import COLUMNS, FTS_SCHEMA, SCHEMA

FACET_COLUMNS = (
    "a_domain", "b_end_user", "c_use_case", "d_analytics",
    "e_formulation", "f_algorithm", "g_eval_tier",
    "models_used", "metrics_reported",
)

INSERT_COLUMNS = [column for column in COLUMNS if column != "rowid_pk"]


def read_jsonl(path: Path) -> Iterable[dict]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def facets(row: dict) -> str:
    return " ".join(str(row.get(column) or "") for column in FACET_COLUMNS)


def _value(row: dict, column: str):
    value = row.get(column)
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(list(value) if not isinstance(value, dict) else value)
    return value


def build_db(jsonl: Path, db_path: Path, *, force: bool = False, limit: int | None = None) -> dict:
    """Create ``db_path`` from the JSONL. Refuses to overwrite without ``force``."""
    jsonl = Path(jsonl)
    db_path = Path(db_path)
    if not jsonl.exists():
        raise FileNotFoundError(f"jsonl not found: {jsonl}")
    if db_path.exists():
        if not force:
            raise FileExistsError(f"{db_path} exists; pass --force to rebuild")
        db_path.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript(SCHEMA)
        conn.executescript(FTS_SCHEMA)

        marks = ",".join("?" * len(INSERT_COLUMNS))
        insert_sql = f"INSERT OR IGNORE INTO papers ({','.join(INSERT_COLUMNS)}) VALUES ({marks})"
        fts_sql = "INSERT INTO papers_fts (title, summary, abstract, facets, pmid) VALUES (?,?,?,?,?)"

        stats = {
            "jsonl": str(jsonl),
            "db": str(db_path),
            "read": 0,
            "inserted": 0,
            "skipped_no_pmid": 0,
            "skipped_duplicate_pmid": 0,
        }
        pending = 0
        for row in read_jsonl(jsonl):
            stats["read"] += 1
            pmid = str(row.get("pmid") or "").strip()
            if not pmid:
                stats["skipped_no_pmid"] += 1
                continue
            cursor = conn.execute(insert_sql, [_value(row, column) for column in INSERT_COLUMNS])
            if cursor.rowcount == 0:
                stats["skipped_duplicate_pmid"] += 1
                continue
            conn.execute(
                fts_sql,
                (
                    row.get("title") or "",
                    row.get("summary") or "",
                    row.get("abstract") or "",
                    facets(row),
                    pmid,
                ),
            )
            stats["inserted"] += 1
            pending += 1
            if pending >= 500:
                conn.commit()
                pending = 0
            if limit is not None and stats["inserted"] >= limit:
                break
        conn.commit()
    finally:
        conn.close()

    counts = verify(db_path)
    stats.update(counts)
    _print_report(stats)
    return stats


def verify(db_path: Path) -> dict:
    """Cheap post-build counts read back from the database itself."""
    db_path = Path(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        papers = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        distinct = int(conn.execute("SELECT COUNT(DISTINCT pmid) FROM papers").fetchone()[0])
        fts = int(conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0])
        years = conn.execute("SELECT MIN(year), MAX(year) FROM papers WHERE year IS NOT NULL").fetchone()
        domains = int(conn.execute("SELECT COUNT(DISTINCT a_domain) FROM papers").fetchone()[0])
        with_abstract = int(conn.execute("SELECT COUNT(*) FROM papers WHERE COALESCE(abstract,'') != ''").fetchone()[0])
        fts_ok = True
        try:
            conn.execute("SELECT COUNT(*) FROM papers_fts WHERE papers_fts MATCH 'sepsis'").fetchone()
        except sqlite3.Error:
            fts_ok = False
    finally:
        conn.close()
    return {
        "papers": papers,
        "distinct_pmid": distinct,
        "fts_rows": fts,
        "year_min": years[0],
        "year_max": years[1],
        "distinct_domains": domains,
        "with_abstract": with_abstract,
        "fts_query_ok": fts_ok,
    }


def _print_report(stats: dict) -> None:
    print("=" * 60)
    print("jsonl -> sqlite build")
    print("=" * 60)
    print(f"  jsonl                 : {stats['jsonl']}")
    print(f"  db                    : {stats['db']}")
    print(f"  rows read             : {stats['read']}")
    print(f"  papers rows           : {stats.get('papers')}")
    print(f"  distinct pmids        : {stats.get('distinct_pmid')}")
    print(f"  fts rows              : {stats.get('fts_rows')} (match ok: {stats.get('fts_query_ok')})")
    print(f"  skipped (no pmid)     : {stats['skipped_no_pmid']}")
    print(f"  skipped (dup pmid)    : {stats['skipped_duplicate_pmid']}")
    print(f"  years                 : {stats.get('year_min')} .. {stats.get('year_max')}")
    print(f"  distinct a_domain     : {stats.get('distinct_domains')}")
    print(f"  rows with abstract    : {stats.get('with_abstract')}")
    print("=" * 60)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest.sqlite_build", description="Build papers.db from papers.jsonl.")
    parser.add_argument("--jsonl", default="data/papers.jsonl")
    parser.add_argument("--db", default="data/papers.db")
    parser.add_argument("--force", action="store_true", help="rebuild even if the db exists")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        build_db(Path(args.jsonl), Path(args.db), force=args.force, limit=args.limit)
    except FileExistsError as exc:
        print(f"refusing to overwrite: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"{exc}", file=sys.stderr)
        print("  run `python -m ingest.ingest` first to produce the jsonl.", file=sys.stderr)
        return 2
    return 0


def main() -> None:  # pragma: no cover
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
