"""Suite S1: faceted questions whose gold set is the exact SQL answer, not a human judgment.

Each generated question is parsed by the shipped rule router (``retrieval.queryparse``), and
the gold set is then the exact set of rows matching *the filters the router extracted* -- so
S1 measures filter/router fidelity, i.e. "given what the router understood, does retrieval
find the matching rows". It cannot say anything about semantic quality, so it is always
reported separately from S2 and never merged into one number.

Determinism: the candidate list, the parse, the gold set and the output order are all fixed.
``--seed`` only shuffles nothing; it exists to keep the CLI uniform across the harness.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from retrieval.queryparse import ParsedQuery, parse
from retrieval.sqlite_store import PaperStore, connect

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "papers.db"
DEFAULT_OUT = Path(__file__).resolve().parent / "queries_faceted.jsonl"

#: Phrase pools. Each phrase is checked against the router's own alias table below, so a
#: phrase that the router would not map is dropped rather than silently producing no filter.
DOMAINS = [
    "oncology", "sepsis", "cardiovascular", "emergency", "radiology",
    "mental health", "pediatric", "diabetes", "neurology", "surgery",
]
USE_CASES = ["prognosis", "diagnosis", "screening", "monitoring", "triage"]
ALGORITHMS = ["deep learning", "random forest", "llm", "logistic regression"]
FLAGS = [
    "external validation", "temporal validation", "calibration",
    "randomized controlled trial",
]

MIN_GOLD = 5
MAX_GOLD = 300
TARGET = 40


def gold_pmids(db_path: Path, parsed: ParsedQuery) -> list[str]:
    """Every row matching the parsed filters, independent of any keyword score."""
    clauses, params = PaperStore(db_path).filter_clauses(parsed)
    if not clauses:
        return []
    sql = "SELECT pmid FROM papers WHERE " + " AND ".join(clauses) + " ORDER BY pmid"
    with connect(db_path) as conn:
        return [str(row["pmid"]) for row in conn.execute(sql, params).fetchall()]


def signature(parsed: ParsedQuery) -> str:
    return json.dumps(
        {"filters": parsed.filters, "flags": parsed.flags, "years": [parsed.year_min, parsed.year_max]},
        sort_keys=True,
    )


def candidate_questions() -> Iterable[str]:
    for domain in DOMAINS:
        for algorithm in ALGORITHMS:
            for use_case in USE_CASES:
                yield f"{domain} studies using {algorithm} for {use_case}"
                for flag in FLAGS:
                    yield f"{domain} studies using {algorithm} for {use_case} with {flag}"


def build(db_path: Path, target: int = TARGET) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for question in candidate_questions():
        if len(out) >= target:
            break
        parsed = parse(question)
        if parsed.intent not in {"hybrid", "faceted"}:
            continue  # aggregate/multihop turns return stats or a template, not a ranking
        if not parsed.fts_terms or not (parsed.filters or parsed.flags or parsed.year_min or parsed.year_max):
            continue
        sig = signature(parsed)
        if sig in seen:
            continue
        gold = gold_pmids(db_path, parsed)
        if not (MIN_GOLD <= len(gold) <= MAX_GOLD):
            continue
        seen.add(sig)
        out.append(
            {
                "id": f"s1-{len(out) + 1:02d}",
                "suite": "S1",
                "question": question,
                "intent": parsed.intent,
                "filters": parsed.filters,
                "flags": parsed.flags,
                "fts_terms": parsed.fts_terms,
                "gold_size": len(gold),
                "gold_pmids": gold,
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--target", type=int, default=TARGET)
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"missing database: {db_path}")
        return 2
    rows = build(db_path, target=args.target)
    out_path = Path(args.out)
    out_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    sizes = [row["gold_size"] for row in rows]
    print(f"wrote {len(rows)} S1 queries -> {out_path}")
    if sizes:
        print(f"gold size: min {min(sizes)} median {sorted(sizes)[len(sizes) // 2]} max {max(sizes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
