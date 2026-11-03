#!/usr/bin/env python
"""Measure the dense leg instead of asserting it works.

Embeds a handful of queries and papers with three candidate document layouts and
reports hit@k / MRR per layout, plus dimension, norms and latency.

    .venv/bin/python scripts/embed_probe.py                       # fixture, all variants
    .venv/bin/python scripts/embed_probe.py --variant b           # one variant
    .venv/bin/python scripts/embed_probe.py --jsonl data/papers.jsonl --limit 200

The point of the variant comparison: for tabular records, do the structured axis
values (domain / use-case / formulation / algorithm / validation tier) belong in
the embedded text, and how much abstract do we actually get to use?
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retrieval.vector_store import Embedder  # noqa: E402

FIXTURE = Path("tests/fixtures/sample_rows.jsonl")

QUERIES: list[tuple[str, set[str]]] = [
    ("models that predict sepsis before it is clinically apparent", {"34152373", "31567890", "32143567"}),
    ("a language model answering patients' own questions about blood thinners", {"33990123"}),
    ("accuracy dropping on darker skin in a dermatology image model", {"29876543"}),
    ("gradient boosted model for in-hospital death validated at a second hospital", {"32143567"}),
    ("radiomics predicting complete response to chemotherapy in breast cancer", {"32789012"}),
]

FACETS = ("a_domain", "c_use_case", "e_formulation", "f_algorithm", "g_eval_tier")


def load_rows(jsonl: Path, limit: int | None) -> list[dict]:
    rows = []
    with jsonl.open() as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
            if limit and len(rows) >= limit:
                break
    return rows


def facet_sentence(row: dict) -> str:
    values = [str(row.get(key) or "").strip() for key in FACETS]
    values = [value for value in values if value]
    return "[" + " · ".join(values) + "] " if values else ""


def build_text(row: dict, variant: str) -> str:
    abstract_cap = {"a": 1200, "b": 1200, "c": 2000}[variant]
    title = str(row.get("title") or "")
    summary = str(row.get("summary") or "")
    abstract = str(row.get("abstract") or "")[:abstract_cap]
    prefix = facet_sentence(row) if variant in {"b", "c"} else ""
    return " ".join(part for part in (prefix, title, summary, abstract) if part.strip())


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def evaluate(embedder: Embedder, rows: list[dict], variant: str) -> dict:
    docs = [build_text(row, variant) for row in rows]
    titles = [str(row.get("title") or "") for row in rows]
    pmids = [str(row.get("pmid")) for row in rows]
    t0 = time.time()
    doc_vectors = embedder.documents(docs, titles=titles)
    embed_s = time.time() - t0
    t1 = time.time()
    query_vectors = [embedder.query(question) for question, _ in QUERIES]
    query_ms = (time.time() - t1) * 1000 / max(len(QUERIES), 1)

    hits_at = {1: 0, 3: 0, 5: 0}
    rr = 0.0
    detail: list[dict] = []
    for (question, expected), qvec in zip(QUERIES, query_vectors):
        scored = sorted(((cosine(qvec, dvec), pmid) for dvec, pmid in zip(doc_vectors, pmids)), reverse=True)
        ranked = [pmid for _score, pmid in scored]
        rank = next((i + 1 for i, pmid in enumerate(ranked) if pmid in expected), None)
        for k in hits_at:
            if rank and rank <= k:
                hits_at[k] += 1
        if rank:
            rr += 1.0 / rank
        detail.append({
            "query": question,
            "expected": sorted(expected),
            "rank": rank,
            "top3": [(pmid, round(score, 4)) for score, pmid in scored[:3]],
        })
    n = len(QUERIES)
    norms = [round(math.sqrt(sum(v * v for v in vec)), 4) for vec in doc_vectors[:3]]
    return {
        "variant": variant,
        "dim": len(doc_vectors[0]) if doc_vectors else 0,
        "doc_norms": norms,
        "embed_seconds": round(embed_s, 2),
        "query_ms": round(query_ms, 1),
        "hit@1": f"{hits_at[1]}/{n}",
        "hit@3": f"{hits_at[3]}/{n}",
        "hit@5": f"{hits_at[5]}/{n}",
        "mrr": round(rr / n, 3),
        "detail": detail,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jsonl", default=str(FIXTURE))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--model", default="google/embeddinggemma-2")
    parser.add_argument("--dim", type=int, default=768)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--variant", default="a,b,c", help="comma list of a,b,c")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    rows = load_rows(Path(args.jsonl), args.limit)
    print(f"rows={len(rows)} model={args.model} dim={args.dim} device={args.device}")
    embedder = Embedder("local", args.model, args.dim, device=args.device)
    t0 = time.time()
    embedder.query("warm up the model")
    print(f"model load + warmup: {time.time() - t0:.1f}s | backend={embedder._backend} dim={embedder.dim}")
    print(f"describe: {embedder.describe()}\n")

    variants = [v.strip() for v in args.variant.split(",") if v.strip()]
    results = [evaluate(embedder, rows, variant) for variant in variants]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            print(f"── variant {result['variant']}  dim={result['dim']}  norms={result['doc_norms']}  "
                  f"embed={result['embed_seconds']}s  query={result['query_ms']}ms")
            print(f"   hit@1={result['hit@1']}  hit@3={result['hit@3']}  hit@5={result['hit@5']}  MRR={result['mrr']}")
            for row in result["detail"]:
                mark = "✓" if row["rank"] else "✗"
                print(f"   {mark} rank={row['rank']} {row['query'][:62]}")
                print(f"       top3={row['top3']}")
            print()
        best = max(results, key=lambda r: (r["mrr"], r["variant"] == "c"))
        print(f"BEST: variant {best['variant']}  MRR={best['mrr']}  hit@5={best['hit@5']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
