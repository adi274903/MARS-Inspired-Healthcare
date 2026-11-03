"""Run the retrieval evaluation over both suites and write a reproducible result.

Two suites, never merged into one number:

* **S1** (``queries_faceted.jsonl``) -- gold = the exact SQL answer to the filters the router
  parsed, so it measures filter/router fidelity. Huge gold sets make recall structurally
  small, which is why the recall ceiling is printed next to F1.
* **S2** (``queries_semantic.jsonl`` + ``judgments.jsonl``) -- gold = human judgments pooled
  over the top-8 of ``bm25``, ``dense`` and ``hybrid_prod``. This is the suite a semantic
  claim may rest on.

Nothing here calls an LLM or the network, and nothing writes to ``data/``.

    python -m eval.run_eval --suites S1            # no human step needed
    python -m eval.run_eval                        # both suites (S2 needs judgments)
    python -m eval.run_eval --only bm25,dense,hybrid_prod

Output: ``eval/results/<tag>.json`` (byte-identical across runs) and ``eval/results/latest.md``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from eval import metrics, retrievers
from retrieval.queryparse import parse
from services.orchestrator.main import LEGS_BY_INTENT, decide_legs

HERE = Path(__file__).resolve().parent
DEFAULT_S1 = HERE / "queries_faceted.jsonl"
DEFAULT_S2 = HERE / "queries_semantic.jsonl"
DEFAULT_JUDGMENTS = HERE / "judgments.jsonl"
DEFAULT_OUT = HERE / "results"
PRODUCTION_LONG = ("sqlite", "vector", "graph")
METRIC_COLUMNS = ("mrr@1", "mrr@3", "mrr@8", "f1@8", "maxf1@8", "ceiling@8", "ap@8", "ap@20")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_s1(path: Path) -> list[dict[str, Any]]:
    return [
        {"id": row["id"], "question": row["question"], "gold": [str(p) for p in row["gold_pmids"]]}
        for row in load_jsonl(path)
    ]


def load_s2(queries_path: Path, judgments_path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep only queries that have at least one judged-relevant document."""
    by_query: dict[str, list[str]] = {}
    judged_count: dict[str, int] = {}
    for row in load_jsonl(judgments_path):
        query_id = str(row["query_id"])
        judged_count[query_id] = judged_count.get(query_id, 0) + 1
        if row["relevant"]:
            by_query.setdefault(query_id, []).append(str(row["pmid"]))
    rows: list[dict[str, Any]] = []
    stats = {"judged_queries": len(judged_count), "dropped_no_relevant": 0, "dropped_unjudged": 0}
    for row in load_jsonl(queries_path):
        query_id = row["id"]
        if query_id not in judged_count:
            stats["dropped_unjudged"] += 1
            continue
        gold = by_query.get(query_id) or []
        if not gold:
            stats["dropped_no_relevant"] += 1
            continue
        rows.append(
            {
                "id": query_id,
                "question": row["question"],
                "gold": gold,
                "judged": judged_count[query_id],
            }
        )
    return rows, stats


def parity(rows: list[dict[str, Any]], settings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split rows by whether the shipped router sends them down all three retrieval legs.

    A row that routes elsewhere (aggregate -> sqlite only, semantic -> vector+sqlite) is
    excluded, because comparing it to a three-leg config would measure a path the product
    does not run.
    """
    keep: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for row in rows:
        parsed = parse(row["question"])
        legs = decide_legs(parsed, settings)
        expected = list(LEGS_BY_INTENT.get(parsed.intent, LEGS_BY_INTENT["hybrid"]))
        if tuple(legs) != PRODUCTION_LONG or tuple(expected) != PRODUCTION_LONG:
            dropped.append({"id": row["id"], "intent": parsed.intent, "legs": legs})
            continue
        keep.append(row)
    return keep, dropped


def evaluate_suite(
    stores: retrievers.Stores,
    rows: list[dict[str, Any]],
    config_map: dict[str, retrievers.Config],
    *,
    ci_n: int,
) -> dict[str, Any]:
    per_config: dict[str, Any] = {}
    for name, config in config_map.items():
        rankings: list[list[str]] = []
        relevants: list[set[str]] = []
        for row in rows:
            parsed = parse(row["question"])
            rankings.append(retrievers.rank(stores, config, row["question"], parsed))
            relevants.append(set(row["gold"]))
        summary = metrics.summarize(rankings, relevants, ci_n=ci_n)
        summary["config"] = {
            "legs": list(config.legs),
            "sqlite_k": config.sqlite_k,
            "vector_k": config.vector_k,
            "rrf_k": config.rrf_k,
            "limit": config.limit,
            "note": config.note,
        }
        summary["empty_rankings"] = sum(1 for ranking in rankings if not ranking)
        summary["ids"] = [row["id"] for row in rows]
        per_config[name] = summary
    return per_config


def render_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = ["# EvalBot retrieval evaluation", ""]
    meta = result["meta"]
    lines += [
        f"Corpus: {meta['stores']['db_rows']} rows in `papers.db`, "
        f"{meta['stores']['vectors']} vectors in `{meta['stores']['collection']}` "
        f"({meta['stores']['index_dim']}-d, `{meta['stores']['embed_model']}`).",
        "",
        "Relevance is binary. `R@8` and `F1@8` are budget-normalised against `min(|R|, 8)`; "
        "`ceiling@8` is the most recall an 8-slot answer list can reach on that gold set. "
        "`AP@8` is the PR-AUC figure at the 8-slot budget. CIs are 95% percentile bootstrap "
        f"over queries ({meta['ci_n']} resamples).",
        "",
    ]
    for suite, body in result["suites"].items():
        lines.append(f"## {suite} — {body['label']}")
        lines.append("")
        lines.append(f"queries: {body['n_queries']} (excluded: {len(body['excluded'])}) · "
                     f"mean gold size: {body.get('mean_gold', 0):.1f}")
        lines.append("")
        header = "| config | " + " | ".join(METRIC_COLUMNS) + " |"
        lines.append(header)
        lines.append("|" + "---|" * (len(METRIC_COLUMNS) + 1))
        for name, summary in body["configs"].items():
            cells = []
            for column in METRIC_COLUMNS:
                entry = summary["metrics"].get(column)
                if entry is None:
                    cells.append("—")
                    continue
                lo, hi = entry["ci95"]
                cells.append(f"{entry['mean']:.3f} [{lo:.3f}, {hi:.3f}]")
            lines.append(f"| `{name}` | " + " | ".join(cells) + " |")
        lines.append("")
        notes = [
            f"- `{name}`: {summary['config']['note']}"
            for name, summary in body["configs"].items()
            if summary["config"]["note"]
        ]
        lines += notes + [""]
    lines += [
        "## Caveats",
        "",
        "- S1 gold is derived from the router's own parse, so it measures filter fidelity, not",
        "  semantic quality, and it favours the SQL leg by construction. It is never merged with S2.",
        "- S2 relevance is pooled from the top-8 of `bm25`, `dense` and `hybrid_prod`; documents no",
        "  measured system returned are unjudged and scored as not relevant. A config outside the",
        "  pool (e.g. the `dense_random` control) is therefore penalised for being unpooled too.",
        "- Queries whose intent does not fan out to all three legs are excluded and listed in the",
        "  result JSON under `excluded`.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suites", default="S1,S2", help="comma list: S1,S2")
    parser.add_argument("--only", default="", help="comma list of config names (default: all)")
    parser.add_argument("--tag", default="run", help="results file name (default: run)")
    parser.add_argument("--ci-n", type=int, default=1000)
    parser.add_argument("--s1", default=str(DEFAULT_S1))
    parser.add_argument("--s2", default=str(DEFAULT_S2))
    parser.add_argument("--judgments", default=str(DEFAULT_JUDGMENTS))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    from core.config import get_settings

    settings = get_settings()
    config_map = retrievers.configs(num_evidence=int(settings.num_evidence))
    if args.only:
        wanted = [name.strip() for name in args.only.split(",") if name.strip()]
        unknown = [name for name in wanted if name not in config_map]
        if unknown:
            print(f"unknown config(s): {unknown}; known: {sorted(config_map)}", file=sys.stderr)
            return 2
        config_map = {name: config_map[name] for name in wanted}

    suites_wanted = {part.strip().upper() for part in args.suites.split(",") if part.strip()}

    stores = retrievers.open_stores()
    describe = stores.describe()
    print(f"stores: {describe}")
    if not describe["embedder_available"]:
        print("embedder unavailable: refusing to publish numbers", file=sys.stderr)
        stores.close()
        return 2
    if not describe["vectors"]:
        print("vector index missing: refusing to publish numbers", file=sys.stderr)
        stores.close()
        return 2

    result: dict[str, Any] = {
        "meta": {
            "stores": describe,
            "configs": {
                name: {
                    "legs": list(config.legs),
                    "sqlite_k": config.sqlite_k,
                    "vector_k": config.vector_k,
                    "rrf_k": config.rrf_k,
                    "limit": config.limit,
                    "note": config.note,
                }
                for name, config in config_map.items()
            },
            "ci_n": args.ci_n,
            "production_long_legs": list(PRODUCTION_LONG),
        },
        "suites": {},
    }

    if "S1" in suites_wanted:
        rows = load_s1(Path(args.s1))
        if not rows:
            print(f"no S1 queries at {args.s1}", file=sys.stderr)
            stores.close()
            return 2
        kept, dropped = parity(rows, settings)
        result["suites"]["S1"] = {
            "label": "faceted questions, deterministic SQL gold (filter/router fidelity)",
            "n_queries": len(kept),
            "excluded": dropped,
            "mean_gold": sum(len(row["gold"]) for row in kept) / len(kept) if kept else 0.0,
            "configs": evaluate_suite(stores, kept, config_map, ci_n=args.ci_n),
        }

    if "S2" in suites_wanted:
        rows, stats = load_s2(Path(args.s2), Path(args.judgments))
        if not rows:
            print(
                f"S2 has no judged queries yet ({stats}). Run `python -m eval.judge_pool --build` "
                "then `python -m eval.judge_pool`.",
                file=sys.stderr,
            )
            stores.close()
            return 3
        kept, dropped = parity(rows, settings)
        result["suites"]["S2"] = {
            "label": "clinician-phrased questions, pooled human judgments (semantic quality)",
            "n_queries": len(kept),
            "excluded": dropped,
            "mean_gold": sum(len(row["gold"]) for row in kept) / len(kept) if kept else 0.0,
            "judging": stats,
            "configs": evaluate_suite(stores, kept, config_map, ci_n=args.ci_n),
        }

    stores.close()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    json_path = out_dir / f"{args.tag}.json"
    json_path.write_text(payload)
    markdown = render_markdown(result)
    (out_dir / "latest.md").write_text(markdown)
    print()
    print(markdown)
    print(f"wrote {json_path} and {out_dir / 'latest.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
