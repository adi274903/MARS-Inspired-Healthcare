#!/usr/bin/env bash
# One-time corpus build: xlsx -> papers.jsonl -> papers.db -> Qdrant -> verify.
#
# Nothing here runs automatically. A human runs this once (or after the dataset
# changes). Each step is guarded: an existing artifact is skipped unless FORCE=1.
#
#   DATASET_PATH=/abs/path/final_merged_dataset.xlsx ./scripts/build_all.sh
#   FORCE=1 ./scripts/build_all.sh            # rebuild every artifact
#   FORCE_QDRANT=1 ./scripts/build_all.sh     # rebuild only the vector index
#
# Override inputs/outputs with: DATASET_PATH, JSONL_PATH, DB_PATH, QDRANT_PATH,
# QDRANT_COLLECTION, EMBED_PROVIDER, EMBED_DIM, PYTHON.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
JSONL_PATH="${JSONL_PATH:-$ROOT/data/papers.jsonl}"
DB_PATH="${DB_PATH:-$ROOT/data/papers.db}"
QDRANT_PATH="${QDRANT_PATH:-$ROOT/data/qdrant}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-evalbot_papers}"
EMBED_PROVIDER="${EMBED_PROVIDER:-hash}"
EMBED_DIM="${EMBED_DIM:-256}"
FORCE="${FORCE:-0}"
FORCE_JSONL="${FORCE_JSONL:-0}"
FORCE_SQLITE="${FORCE_SQLITE:-0}"
FORCE_QDRANT="${FORCE_QDRANT:-0}"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: no python at $PYTHON" >&2
  echo "  create the venv first: python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

run() {
  echo "+ $*"
  "$@"
}

# ---------------------------------------------------------------- step 0: jsonl
if [[ ! -f "$JSONL_PATH" || "$FORCE" == "1" || "$FORCE_JSONL" == "1" ]]; then
  DATASET="${DATASET_PATH:-$ROOT/final_merged_dataset.xlsx}"
  if [[ ! -f "$DATASET" ]]; then
    cat >&2 <<EOF
error: dataset not found, cannot build $JSONL_PATH

  looked for: \$DATASET_PATH, then $ROOT/final_merged_dataset.xlsx

  set it explicitly and re-run:
    DATASET_PATH=/absolute/path/to/final_merged_dataset.xlsx ./scripts/build_all.sh

  (the workbook is ~28 MB with an ~80 MB sharedStrings part; the stdlib
   streaming reader in ingest/ingest.py handles it, but expect minutes)
EOF
    exit 2
  fi
  CMD=("$PYTHON" -m ingest.ingest --xlsx "$DATASET" --out "$JSONL_PATH")
  if [[ -f "$JSONL_PATH" ]]; then CMD+=(--force); fi
  run "${CMD[@]}"
else
  echo "skip ingest: $JSONL_PATH exists (FORCE=1 to rebuild)"
fi

if [[ ! -f "$JSONL_PATH" ]]; then
  echo "error: $JSONL_PATH is missing after ingest" >&2
  exit 1
fi

# --------------------------------------------------------------- step 1: sqlite
if [[ -f "$DB_PATH" && "$FORCE" != "1" && "$FORCE_SQLITE" != "1" ]]; then
  echo "skip sqlite: $DB_PATH exists (FORCE=1 to rebuild)"
else
  CMD=("$PYTHON" -m ingest.sqlite_build --jsonl "$JSONL_PATH" --db "$DB_PATH")
  if [[ -f "$DB_PATH" ]]; then CMD+=(--force); fi
  run "${CMD[@]}"
fi

# --------------------------------------------------------------- step 2: qdrant
if [[ -d "$QDRANT_PATH" && "$FORCE" != "1" && "$FORCE_QDRANT" != "1" ]]; then
  echo "skip qdrant: $QDRANT_PATH exists (FORCE=1 or FORCE_QDRANT=1 to rebuild)"
else
  run "$PYTHON" -m ingest.qdrant_index \
    --jsonl "$JSONL_PATH" \
    --path "$QDRANT_PATH" \
    --collection "$QDRANT_COLLECTION" \
    --embed-provider "$EMBED_PROVIDER" \
    --dim "$EMBED_DIM" \
    --resume
fi

# ----------------------------------------------------- step 3: neo4j (optional)
if [[ -n "${NEO4J_URI:-}" ]]; then
  run "$PYTHON" -m ingest.neo4j_graph --jsonl "$JSONL_PATH"
else
  echo "skip neo4j: NEO4J_URI unset (the graph leg falls back to SQLite)"
fi

# --------------------------------------------------------------- step 4: verify
run "$PYTHON" -m ingest.verify \
  --jsonl "$JSONL_PATH" \
  --db "$DB_PATH" \
  --qdrant-path "$QDRANT_PATH" \
  --collection "$QDRANT_COLLECTION"

echo "build complete: $JSONL_PATH -> $DB_PATH + $QDRANT_PATH ($QDRANT_COLLECTION)"
