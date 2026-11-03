# EvalBot retrieval evaluation

Offline, read-only measurement of the retrieval path. No LLM call, no network, nothing written
to `data/`. Everything reproduces from `data/papers.db` (15,954 rows) and the embedded Qdrant
collection `evalbot_papers` (15,954 × 768-d, `google/embeddinggemma-2`).

```
python -m eval.build_faceted_queries      # S1 gold set (deterministic, committed)
python -m eval.run_eval --suites S1       # S1 numbers, no human step
python -m eval.judge_pool --build         # S2 judgment pools
python -m eval.judge_pool                 # S2 judging (y/n/s/q, resumable)
python -m eval.judge_pool --status        # how much is left
python -m eval.run_eval                   # both suites -> eval/results/
```

## Suites (never merged)

**S1 — faceted questions, deterministic gold.** 40 questions generated from the corpus's own
facet columns (`a_domain × c_use_case × f_algorithm` plus validation flags). Each question is
parsed by the shipped rule router, and the gold set is the **exact SQL result of the filters the
router extracted** (5–300 rows per query). This measures *filter/router fidelity*: given what the
router understood, does retrieval find the matching rows? It favours the SQL leg by construction
and says nothing about semantic quality.

**S2 — clinician-phrased questions, pooled human judgments.** 30 questions in the assistant's
own voice. The pool per query is the union of the top-8 of `bm25`, `dense` and `hybrid_prod`
(455 documents total). Each document is judged relevant / not relevant against the question.
This is the only suite a semantic claim may rest on.

Because the pool is built from those three systems, a document none of them returned is never
judged — the standard pooled-judgment bias. Unjudged means not relevant, so any configuration
outside the pool can only look worse than it is.

## Metrics (definitions are in `eval/metrics.py`, tests in `tests/test_eval_metrics.py`)

| metric | definition |
|---|---|
| `mrr@1`, `mrr@3`, `mrr@8` | mean of `1/rank` of the first relevant document within the cutoff; 0 if none |
| `p@8` | `|relevant ∩ top-8| / 8` |
| `r@8` | `|relevant ∩ top-8| / min(|relevant|, 8)` — budget-normalised, because an 8-slot list cannot recall more than 8 documents |
| `f1@8` | harmonic mean of `p@8` and `r@8` |
| `maxf1@8` | best `f1` over the cutoffs `1..8` (the fair F1 for a ranking) |
| `ceiling@8` | `min(|relevant|, 8) / |relevant|` — the most recall an 8-slot budget can reach, printed so a small F1 on a large gold set is not misread |
| `ap@8` | `Σ_{i: hit, i ≤ 8} P@i / min(|R|, 8)` — average precision, the **PR-AUC** figure at the 8-slot budget. Never quoted without its `k`. |
| `ap@20` | the same at 20 slots, only comparable for configs that retrieve 20+ deep |

CIs are 95% percentile bootstrap over **queries** (1,000 resamples, seeded), so two runs of the
same command produce byte-identical `results/*.json`.

## Configurations

| config | what it is |
|---|---|
| `bm25` | FTS5 BM25 + production facet filters, including the soft-filter fallback |
| `dense` | 768-d EmbeddingGemma ANN, unfiltered, no fusion |
| `graph` | the graph service's default named template (`papers_by_author`) over the SQLite fallback |
| `hybrid_prod` | **the shipped path**: sqlite k=8, vector 32 candidates, graph 20, `rrf.fuse(k=60)`, weights `{sqlite 1.0, vector 1.0, graph 0.8}`, 8 slots |
| `hybrid20` | the same fusion at depth 20, so `ap@20` is comparable across configs |
| `hybrid_nograph` | ablation: sqlite + vector only |
| `hybrid_k30`, `hybrid_k120` | RRF `k` sweep against the production `k=60` |
| `dense_random` | **negative control**: the real index queried with a seeded random unit vector |

Each leg is a mirror of a shipped service (`eval/retrievers.py` names the file it mirrors), and
`run_eval.py` asserts the hybrid leg list equals `services.orchestrator.main.decide_legs` for
every query. Queries whose intent does not fan out to all three legs are excluded and listed in
the result JSON under `excluded` — never silently averaged in.

## Reading the output

`eval/results/latest.md` is the human table; `eval/results/<tag>.json` has every per-query row,
the store description, and the exact config limits. Two rules:

1. A number quoted from S1 is a **filter-fidelity** number; a number quoted from S2 is a
   **semantic-quality** number. Never present one as the other.
2. Always quote the sample size and the budget: "MRR@8 0.85 on 40 faceted questions", not "0.85
   accuracy". `0.98`-style accuracy figures do not exist in this harness and cannot be produced
   by it.
