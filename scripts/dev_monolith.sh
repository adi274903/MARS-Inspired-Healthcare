#!/usr/bin/env bash
# Development topology: every service in one process, in-memory bus, no podman.
# This is the debuggable path used while iterating on the pipeline.
#
#   ./scripts/dev_monolith.sh              # PORT=8080
#   PORT=9000 ./scripts/dev_monolith.sh
#
# The service code and the events are identical to the podman topology; only the
# transport (memory instead of redis) differs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
PORT="${PORT:-8080}"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: no python at $PYTHON" >&2
  echo "  python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

for artifact in data/papers.db; do
  if [[ ! -f "$artifact" ]]; then
    echo "warning: $artifact is missing - retrieval legs will report unavailable." >&2
    echo "         build it with: DATASET_PATH=/path/to/final_merged_dataset.xlsx ./scripts/build_all.sh" >&2
  fi
done

echo "+ MONOLITH=1 BUS=memory PORT=$PORT $PYTHON -m services.monolith.app"
echo "  UI:       http://localhost:$PORT/"
echo "  health:   http://localhost:$PORT/healthz"
echo "  smoke:    PORT=$PORT ./scripts/smoke.sh"
exec env MONOLITH=1 BUS=memory PORT="$PORT" "$PYTHON" -m services.monolith.app
