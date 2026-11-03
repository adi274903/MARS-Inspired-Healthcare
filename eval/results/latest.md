# EvalBot retrieval evaluation

Corpus: 15954 rows in `papers.db`, 15954 vectors in `evalbot_papers` (768-d, `google/embeddinggemma-2`).

Relevance is binary. `R@8` and `F1@8` are budget-normalised against `min(|R|, 8)`; `ceiling@8` is the most recall an 8-slot answer list can reach on that gold set. `AP@8` is the PR-AUC figure at the 8-slot budget. CIs are 95% percentile bootstrap over queries (1000 resamples).

## S1 — faceted questions, deterministic SQL gold (filter/router fidelity)

queries: 40 (excluded: 0) · mean gold size: 75.5

| config | mrr@1 | mrr@3 | mrr@8 | f1@8 | maxf1@8 | ceiling@8 | ap@8 | ap@20 |
|---|---|---|---|---|---|---|---|---|
| `bm25` | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.996 [0.989, 1.000] | 1.000 [1.000, 1.000] | 0.299 [0.213, 0.393] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| `dense` | 0.150 [0.050, 0.275] | 0.171 [0.071, 0.292] | 0.215 [0.117, 0.331] | 0.097 [0.056, 0.144] | 0.228 [0.129, 0.347] | 0.299 [0.213, 0.393] | 0.048 [0.025, 0.077] | 0.039 [0.018, 0.067] |
| `graph` | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.299 [0.213, 0.393] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] |
| `hybrid_prod` | 0.700 [0.550, 0.850] | 0.850 [0.775, 0.925] | 0.850 [0.775, 0.925] | 0.549 [0.527, 0.575] | 0.876 [0.818, 0.933] | 0.299 [0.213, 0.393] | 0.382 [0.345, 0.425] | 0.178 [0.157, 0.199] |
| `hybrid20` | 0.925 [0.850, 1.000] | 0.963 [0.925, 1.000] | 0.963 [0.925, 1.000] | 0.617 [0.572, 0.669] | 0.973 [0.940, 1.000] | 0.299 [0.213, 0.393] | 0.507 [0.442, 0.576] | 0.440 [0.400, 0.483] |
| `hybrid_nograph` | 0.925 [0.850, 1.000] | 0.963 [0.925, 1.000] | 0.963 [0.925, 1.000] | 0.617 [0.572, 0.669] | 0.973 [0.940, 1.000] | 0.299 [0.213, 0.393] | 0.507 [0.442, 0.576] | 0.440 [0.400, 0.483] |
| `hybrid_k30` | 0.925 [0.850, 1.000] | 0.963 [0.925, 1.000] | 0.963 [0.925, 1.000] | 0.617 [0.572, 0.669] | 0.973 [0.940, 1.000] | 0.299 [0.213, 0.393] | 0.505 [0.441, 0.575] | 0.439 [0.399, 0.481] |
| `hybrid_k120` | 0.925 [0.850, 1.000] | 0.963 [0.925, 1.000] | 0.963 [0.925, 1.000] | 0.617 [0.572, 0.669] | 0.973 [0.940, 1.000] | 0.299 [0.213, 0.393] | 0.507 [0.442, 0.576] | 0.440 [0.400, 0.483] |
| `dense_random` | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.299 [0.213, 0.393] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] |

- `bm25`: FTS5 BM25 + production facet filters (+ soft-filter fallback)
- `dense`: 768-d EmbeddingGemma ANN, unfiltered, no fusion
- `graph`: default named template (papers_by_author) over the SQLite fallback
- `hybrid_prod`: PRODUCTION: sqlite k=8, vector 32 candidates, graph 20, RRF k=60, 8 slots
- `hybrid20`: same fusion at depth 20 so AP@20 is comparable across configs
- `hybrid_nograph`: ablation: does the graph leg contribute at all?
- `hybrid_k30`: RRF k sweep
- `hybrid_k120`: RRF k sweep
- `dense_random`: NEGATIVE CONTROL: same index, seeded random query vector (must score ~0)

## Caveats

- S1 gold is derived from the router's own parse, so it measures filter fidelity, not
  semantic quality, and it favours the SQL leg by construction. It is never merged with S2.
- S2 relevance is pooled from the top-8 of `bm25`, `dense` and `hybrid_prod`; documents no
  measured system returned are unjudged and scored as not relevant. A config outside the
  pool (e.g. the `dense_random` control) is therefore penalised for being unpooled too.
- Queries whose intent does not fan out to all three legs are excluded and listed in the
  result JSON under `excluded`.
