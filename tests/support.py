"""Shared test helpers: a real SQLite DB built from the 12-row fixture (never the xlsx)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from retrieval.sqlite_store import COLUMNS, FTS_SCHEMA, SCHEMA

FIXTURE = Path(__file__).parent / "fixtures" / "sample_rows.jsonl"


def fixture_rows() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def build_fixture_db(path: Path, rows: list[dict] | None = None) -> Path:
    rows = rows if rows is not None else fixture_rows()
    path = Path(path) / "papers.db" if Path(path).is_dir() else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executescript(FTS_SCHEMA)
    insert_cols = [c for c in COLUMNS if c != "rowid_pk"]
    marks = ",".join("?" * len(insert_cols))
    for row in rows:
        values = []
        for column in insert_cols:
            value = row.get(column)
            if isinstance(value, list):
                value = json.dumps(value)
            values.append(value)
        conn.execute(f"INSERT INTO papers ({','.join(insert_cols)}) VALUES ({marks})", values)
        conn.execute(
            "INSERT INTO papers_fts (title, summary, abstract, facets, pmid) VALUES (?,?,?,?,?)",
            (
                row.get("title", ""),
                row.get("summary", ""),
                row.get("abstract", ""),
                " ".join(str(row.get(k) or "") for k in ("a_domain", "b_end_user", "c_use_case", "d_analytics", "e_formulation", "f_algorithm", "g_eval_tier", "models_used", "metrics_reported")),
                row.get("pmid", ""),
            ),
        )
    conn.commit()
    conn.close()
    return path
