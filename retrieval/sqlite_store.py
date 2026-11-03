"""SQLite is the system of record: structured filters, FTS5 BM25 and aggregates."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from retrieval.queryparse import ParsedQuery

COLUMNS = [
    "pmid", "doi", "title", "abstract", "summary", "authors", "journal", "journal_abbr", "year",
    "publication_date", "mesh_names", "pub_types", "keywords", "country_data", "country_author",
    "validation_details", "deployment_details", "trial_description", "metrics_reported", "models_used",
    "tags", "a_domain", "b_end_user", "c_use_case", "d_analytics", "e_formulation", "f_algorithm",
    "g_eval_tier", "internal_val", "external_val", "temporal_val", "deployed", "rct", "ci_reported",
    "calibration_reported", "search_text",
]

FLAG_COLUMNS = {
    "external_only": "external_val",
    "temporal_only": "temporal_val",
    "deployed_only": "deployed",
    "rct_only": "rct",
    "with_ci": "ci_reported",
    "calibrated": "calibration_reported",
}

SCHEMA = """
CREATE TABLE papers (
    rowid_pk INTEGER PRIMARY KEY,
    pmid TEXT UNIQUE NOT NULL,
    doi TEXT, title TEXT, abstract TEXT, summary TEXT, authors TEXT,
    journal TEXT, journal_abbr TEXT, year INTEGER, publication_date TEXT,
    mesh_names TEXT, pub_types TEXT, keywords TEXT,
    country_data TEXT, country_author TEXT,
    validation_details TEXT, deployment_details TEXT, trial_description TEXT,
    metrics_reported TEXT, models_used TEXT, tags TEXT,
    a_domain TEXT, b_end_user TEXT, c_use_case TEXT, d_analytics TEXT,
    e_formulation TEXT, f_algorithm TEXT, g_eval_tier TEXT,
    internal_val INTEGER, external_val INTEGER, temporal_val INTEGER,
    deployed INTEGER, rct INTEGER, ci_reported INTEGER, calibration_reported INTEGER,
    search_text TEXT
);
CREATE INDEX idx_year ON papers(year);
CREATE INDEX idx_domain ON papers(a_domain);
CREATE INDEX idx_tier ON papers(g_eval_tier);
CREATE INDEX idx_ext ON papers(external_val);
"""
FTS_SCHEMA = """
CREATE VIRTUAL TABLE papers_fts USING fts5(
    title, summary, abstract, facets, pmid UNINDEXED, tokenize='porter unicode61'
);
"""


def connect(path: Path, *, readonly: bool = True) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _like_clauses(parsed: ParsedQuery) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, keywords in (parsed.filters or {}).items():
        if column not in COLUMNS:
            continue
        ors = []
        for kw in keywords:
            ors.append(f"LOWER(COALESCE({column}, '')) LIKE ?")
            params.append(f"%{kw.lower()}%")
        if ors:
            clauses.append("(" + " OR ".join(ors) + ")")
    for flag, column in FLAG_COLUMNS.items():
        if parsed.flags.get(flag):
            clauses.append(f"COALESCE({column}, 0) = 1")
    if parsed.year_min:
        clauses.append("year >= ?")
        params.append(parsed.year_min)
    if parsed.year_max:
        clauses.append("year <= ?")
        params.append(parsed.year_max)
    return clauses, params


class PaperStore:
    """Read-only access to ``papers.db``. One connection per call keeps it thread-safe."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def available(self) -> bool:
        try:
            with connect(self.path) as conn:
                conn.execute("SELECT 1 FROM papers LIMIT 1")
            return True
        except sqlite3.Error:
            return False

    def count(self) -> int:
        with connect(self.path) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])

    # -- filtering ------------------------------------------------------
    def filter_clauses(self, parsed: ParsedQuery) -> tuple[list[str], list[Any]]:
        return _like_clauses(parsed)

    def search_fts(self, parsed: ParsedQuery, limit: int = 20, *, apply_filters: bool = True) -> list[str]:
        """BM25 over title/summary/abstract/facets; filters applied in SQL."""
        if not parsed.fts_terms:
            return self.recent(parsed, limit) if apply_filters else []
        match = " OR ".join(f'"{t}"' for t in parsed.fts_terms)
        clauses, params = self.filter_clauses(parsed) if apply_filters else ([], [])
        where = " AND ".join(["papers_fts MATCH ?", *clauses])
        sql = (
            "SELECT papers_fts.pmid AS pmid FROM papers_fts "
            "JOIN papers ON papers.pmid = papers_fts.pmid "
            f"WHERE {where} ORDER BY bm25(papers_fts) LIMIT ?"
        )
        with connect(self.path) as conn:
            rows = conn.execute(sql, [match, *params, limit]).fetchall()
        return [r["pmid"] for r in rows if r["pmid"]]

    def recent(self, parsed: ParsedQuery, limit: int = 20) -> list[str]:
        clauses, params = self.filter_clauses(parsed)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT pmid FROM papers{where} ORDER BY COALESCE(year, 0) DESC, pmid DESC LIMIT ?"
        with connect(self.path) as conn:
            return [r["pmid"] for r in conn.execute(sql, [*params, limit]).fetchall()]

    def search(self, parsed: ParsedQuery, limit: int = 20) -> tuple[list[str], bool]:
        """Filtered search with the soft-filter fallback: returns (pmids, filters_applied)."""
        pmids = self.search_fts(parsed, limit, apply_filters=True)
        if len(pmids) >= 3 or not (parsed.filters or parsed.flags or parsed.year_min or parsed.year_max):
            return pmids, True
        relaxed = self.search_fts(parsed, limit, apply_filters=False)
        return relaxed, False

    # -- hydration ------------------------------------------------------
    def get_many(self, pmids: Iterable[str]) -> dict[str, dict[str, Any]]:
        wanted = [p for p in dict.fromkeys(pmids) if p]
        if not wanted:
            return {}
        out: dict[str, dict[str, Any]] = {}
        with connect(self.path) as conn:
            for chunk_start in range(0, len(wanted), 500):
                chunk = wanted[chunk_start:chunk_start + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(f"SELECT * FROM papers WHERE pmid IN ({marks})", chunk).fetchall()
                for row in rows:
                    out[row["pmid"]] = _row_to_dict(row)
        return out

    def by_author(self, author: str, limit: int = 20) -> list[str]:
        like = f"%{author.lower()}%"
        with connect(self.path) as conn:
            rows = conn.execute(
                "SELECT pmid FROM papers WHERE LOWER(authors) LIKE ? ORDER BY COALESCE(year,0) DESC LIMIT ?",
                (like, limit),
            ).fetchall()
        return [r["pmid"] for r in rows]

    def by_mesh(self, mesh: str, limit: int = 20) -> list[str]:
        like = f"%{mesh.lower()}%"
        with connect(self.path) as conn:
            rows = conn.execute(
                "SELECT pmid FROM papers WHERE LOWER(mesh_names) LIKE ? ORDER BY COALESCE(year,0) DESC LIMIT ?",
                (like, limit),
            ).fetchall()
        return [r["pmid"] for r in rows]

    def journal_rollup(self, limit: int = 15, table: str = "papers") -> list[dict[str, Any]]:
        assert table in {"papers"}
        with connect(self.path) as conn:
            rows = conn.execute(
                "SELECT journal AS key, COUNT(*) AS n FROM papers "
                "WHERE journal IS NOT NULL AND journal != '' GROUP BY journal ORDER BY n DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"key": r["key"], "n": r["n"]} for r in rows]

    def similar(self, pmid: str, limit: int = 10) -> list[str]:
        target = self.get_many([pmid]).get(pmid)
        if not target:
            return []
        authors = _json_list(target.get("authors"))[:3]
        mesh = _json_list(target.get("mesh_names"))[:6]
        hurts: list[str] = []
        clauses: list[str] = []
        params: list[Any] = []
        for name in authors:
            clauses.append("LOWER(authors) LIKE ?")
            params.append(f"%{name.lower()}%")
        for term in mesh:
            hurts.append("LOWER(mesh_names) LIKE ?")
            params.append(f"%{term.lower()}%")
        if not clauses and not hurts:
            return []
        where = " OR ".join(clauses + hurts)
        with connect(self.path) as conn:
            rows = conn.execute(
                f"SELECT pmid FROM papers WHERE pmid != ? AND ({where}) "
                "ORDER BY COALESCE(year,0) DESC LIMIT ?",
                [pmid, *params, limit],
            ).fetchall()
        return [r["pmid"] for r in rows]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys()}
    data.pop("rowid_pk", None)
    return data


def _json_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value)
        return [str(v) for v in parsed] if isinstance(parsed, list) else [str(parsed)]
    except (json.JSONDecodeError, TypeError):
        return [part.strip() for part in str(value).split(",") if part.strip()]
