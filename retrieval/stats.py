"""Deterministic aggregates. Counting questions never reach the model's arithmetic."""
from __future__ import annotations

from typing import Any

from retrieval.queryparse import ParsedQuery
from retrieval.sqlite_store import connect


def aggregate(db_path, parsed: ParsedQuery, limit: int = 25) -> dict[str, Any]:
    """Return an exact count (optionally grouped) for the parsed filters."""
    store_clauses, params = _clauses(parsed)
    where = (" WHERE " + " AND ".join(store_clauses)) if store_clauses else ""
    with connect(db_path) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM papers{where}", params).fetchone()[0])
        result: dict[str, Any] = {
            "total": total,
            "filters": parsed.filters,
            "flags": parsed.flags,
            "year_min": parsed.year_min,
            "year_max": parsed.year_max,
            "group_by": parsed.group_by,
            "rows": [],
        }
        if parsed.group_by:
            column = parsed.group_by
            rows = conn.execute(
                f"SELECT COALESCE(CAST({column} AS TEXT), '(blank)') AS key, COUNT(*) AS n "
                f"FROM papers{where} GROUP BY key ORDER BY n DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
            result["rows"] = [{"key": r["key"], "n": r["n"]} for r in rows]
    return result


def _clauses(parsed: ParsedQuery):
    from retrieval.sqlite_store import FLAG_COLUMNS, _like_clauses

    clauses, params = _like_clauses(parsed)
    return clauses, params


def corpus_overview(db_path) -> dict[str, Any]:
    """Totals used by /api/stats and the answer prompt."""
    with connect(db_path) as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        axes = {
            "a_domain": "a_domain",
            "b_end_user": "b_end_user",
            "c_use_case": "c_use_case",
            "d_analytics": "d_analytics",
            "e_formulation": "e_formulation",
            "f_algorithm": "f_algorithm",
            "g_eval_tier": "g_eval_tier",
        }
        facets: dict[str, list[dict[str, Any]]] = {}
        for name, column in axes.items():
            rows = conn.execute(
                f"SELECT COALESCE({column}, '(blank)') AS key, COUNT(*) AS n FROM papers "
                "GROUP BY key ORDER BY n DESC LIMIT 20"
            ).fetchall()
            facets[name] = [{"key": r["key"], "n": r["n"]} for r in rows]
        flags = {}
        for column in ("external_val", "temporal_val", "internal_val", "deployed", "rct", "ci_reported", "calibration_reported"):
            flags[column] = int(conn.execute(f"SELECT COUNT(*) FROM papers WHERE COALESCE({column},0)=1").fetchone()[0])
        years = conn.execute(
            "SELECT MIN(year) AS lo, MAX(year) AS hi FROM papers WHERE year IS NOT NULL"
        ).fetchone()
    return {
        "total": total,
        "facets": facets,
        "flags": flags,
        "year_range": [years["lo"], years["hi"]],
    }
