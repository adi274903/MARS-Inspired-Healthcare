"""Build the judgment pool for suite S2 and judge it interactively (resumable).

Pool = union of the top-8 of ``bm25``, ``dense`` and the shipped ``hybrid_prod`` config, so a
document that no measured system retrieved is never judged. That is the standard pooled
judgment protocol, and it carries its standard bias: a system *outside* the pool is scored
against labels it did not contribute to, so it can only look worse (``dense_random``, the
negative control, is penalised twice — once for being random, once for being unpooled).

Commands::

    python -m eval.judge_pool --build      # write eval/pools.json
    python -m eval.judge_pool              # judge what is left, y/n/s/q
    python -m eval.judge_pool --status     # how much is left

Judgments land in ``eval/judgments.jsonl`` (one line per query/document); re-running resumes.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from eval import retrievers
from retrieval.queryparse import parse

HERE = Path(__file__).resolve().parent
DEFAULT_QUERIES = HERE / "queries_semantic.jsonl"
DEFAULT_POOLS = HERE / "pools.json"
DEFAULT_JUDGMENTS = HERE / "judgments.jsonl"
POOL_CONFIGS = ("bm25", "dense", "hybrid_prod")
POOL_DEPTH = 8
SNIPPET = 320


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_judgments(path: Path) -> dict[tuple[str, str], bool]:
    out: dict[tuple[str, str], bool] = {}
    for row in load_jsonl(path):
        out[(str(row["query_id"]), str(row["pmid"]))] = bool(row["relevant"])
    return out


def build_pools(stores: retrievers.Stores, queries: list[dict[str, Any]]) -> dict[str, Any]:
    configs = retrievers.configs()
    wanted = [configs[name] for name in POOL_CONFIGS]
    pools: dict[str, Any] = {}
    for row in queries:
        question = row["question"]
        parsed = parse(question)
        ranked: dict[str, list[str]] = {}
        for config in wanted:
            ranked[config.name] = retrievers.rank(stores, config, question, parsed)[:POOL_DEPTH]
        union: list[str] = []
        for name in POOL_CONFIGS:
            for pmid in ranked[name]:
                if pmid not in union:
                    union.append(pmid)
        pools[row["id"]] = {
            "question": question,
            "intent": parsed.intent,
            "by_config": ranked,
            "pool": union,
        }
    return pools


def _describe(store, pmid: str) -> str:
    row = store.get_many([pmid]).get(pmid) or {}
    title = (row.get("title") or "").strip()
    abstract = " ".join((row.get("abstract") or "").split())[:SNIPPET]
    facets = " | ".join(
        str(row.get(key) or "?")
        for key in ("year", "a_domain", "c_use_case", "f_algorithm", "g_eval_tier")
    )
    return f"    {title}\n    [{facets}]\n    {abstract}"


def judge(pools_path: Path, judgments_path: Path, *, limit: int | None = None) -> int:
    pools = json.loads(pools_path.read_text())
    judged = load_judgments(judgments_path)
    stores = retrievers.open_stores()
    if not stores.embedder.available():
        print("embedder unavailable: pools cannot be rebuilt, but judging can continue", file=sys.stderr)

    remaining = [
        (query_id, pmid)
        for query_id, entry in pools.items()
        for pmid in entry["pool"]
        if (query_id, pmid) not in judged
    ]
    if not remaining:
        print("nothing left to judge")
        return 0
    print(f"{len(remaining)} documents left to judge. y=relevant n=not relevant s=skip q=quit\n")

    done = 0
    with judgments_path.open("a") as handle:
        seen: set[str] = set()
        for query_id, pmid in remaining:
            if limit is not None and done >= limit:
                break
            entry = pools[query_id]
            if query_id not in seen:
                seen.add(query_id)
                left = sum(1 for q, p in remaining if q == query_id and (q, p) not in judged)
                print(f"\n=== {query_id} ({entry['intent']}) — {left} to judge")
                print(f"    Q: {entry['question']}")
            print(f"\n  PMID {pmid}")
            print(_describe(stores.store, pmid))
            try:
                answer = input("  relevant to this question? [y/n/s/q] ").strip().lower()
            except EOFError:
                print("\n(end of input)")
                break
            if answer == "q":
                break
            if answer == "s":
                continue
            if answer not in {"y", "n"}:
                print("  please answer y, n, s or q")
                continue
            relevant = answer == "y"
            row = {"query_id": query_id, "pmid": pmid, "relevant": relevant, "ts": round(time.time(), 3)}
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            judged[(query_id, pmid)] = relevant
            done += 1
    stores.close()
    print(f"\njudged {done} document(s) this session -> {judgments_path}")
    return 0


def status(pools_path: Path, judgments_path: Path) -> int:
    pools = json.loads(pools_path.read_text())
    judged = load_judgments(judgments_path)
    total = sum(len(entry["pool"]) for entry in pools.values())
    done = sum(1 for query_id, entry in pools.items() for pmid in entry["pool"] if (query_id, pmid) in judged)
    per_query = [
        (query_id, sum(1 for p in entry["pool"] if (query_id, p) in judged), len(entry["pool"]))
        for query_id, entry in pools.items()
    ]
    complete = sum(1 for _q, d, n in per_query if d == n)
    print(f"pool documents: {total}")
    print(f"judged:         {done} ({100.0 * done / total:.1f}%)" if total else "judged: 0")
    print(f"queries fully judged: {complete}/{len(pools)}")
    for query_id, d, n in per_query:
        if d < n:
            print(f"  {query_id}: {d}/{n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build", action="store_true", help="rebuild eval/pools.json")
    parser.add_argument("--status", action="store_true", help="report judging progress")
    parser.add_argument("--limit", type=int, default=None, help="judge at most N documents this session")
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--pools", default=str(DEFAULT_POOLS))
    parser.add_argument("--judgments", default=str(DEFAULT_JUDGMENTS))
    args = parser.parse_args(argv)

    pools_path = Path(args.pools)
    judgments_path = Path(args.judgments)

    if args.build:
        stores = retrievers.open_stores()
        describe = stores.describe()
        print(f"stores: {describe}")
        if not describe["embedder_available"] or not describe["vectors"]:
            print("refusing to build pools: embedder or vector index unavailable", file=sys.stderr)
            stores.close()
            return 2
        pools = build_pools(stores, load_jsonl(Path(args.queries)))
        pools_path.write_text(json.dumps(pools, indent=2, sort_keys=True) + "\n")
        sizes = [len(entry["pool"]) for entry in pools.values()]
        print(f"wrote {len(pools)} pools -> {pools_path} (documents: {sum(sizes)}, mean {sum(sizes) / len(sizes):.1f}/query)")
        stores.close()
        return 0

    if not pools_path.exists():
        print(f"missing {pools_path}: run --build first", file=sys.stderr)
        return 2
    if args.status:
        return status(pools_path, judgments_path)
    return judge(pools_path, judgments_path, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
