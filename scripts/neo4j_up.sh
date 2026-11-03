#!/usr/bin/env bash
# Local Neo4j for the graph leg. Standalone container (not part of the service pod,
# so 7474/7687 never collide with `podman play kube`).
#
#   ./scripts/neo4j_up.sh          # create + start, wait for Bolt
#   ./scripts/neo4j_up.sh --stop   # stop and remove
#   ./scripts/neo4j_up.sh --status
#
# Then load the graph (needs data/papers.jsonl from the corpus build):
#   python -m ingest.neo4j_graph --uri bolt://localhost:7687 --user neo4j --password "$NEO4J_PASSWORD"
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="${NEO4J_NAME:-evalbot-neo4j}"
IMAGE="${NEO4J_IMAGE:-docker.io/library/neo4j:5-community}"
PASSWORD="${NEO4J_PASSWORD:-evalbotpass}"
DATA="${NEO4J_DATA:-$PWD/data/neo4j}"
ENGINE=""

for candidate in podman docker; do
  if command -v "$candidate" >/dev/null 2>&1; then ENGINE="$candidate"; break; fi
done

if [[ -z "$ENGINE" ]]; then
  echo "No podman or docker found." >&2
  echo "Fallback: the graph leg evaluates its templates over SQLite automatically;" >&2
  echo "leave NEO4J_URI empty in .env and everything else keeps working." >&2
  exit 2
fi

case "${1:-}" in
  --stop)
    "$ENGINE" rm -f "$NAME" >/dev/null 2>&1 || true
    echo "stopped and removed container '$NAME'"
    exit 0
    ;;
  --status)
    "$ENGINE" ps --filter "name=$NAME" --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' || true
    exit 0
    ;;
esac

mkdir -p "$DATA"

if "$ENGINE" container exists "$NAME" >/dev/null 2>&1; then
  "$ENGINE" start "$NAME" >/dev/null
  echo "started existing container '$NAME'"
else
  echo "+ $ENGINE run -d --name $NAME -p 7474:7474 -p 7687:7687 ($IMAGE)"
  "$ENGINE" run -d --name "$NAME" \
    -p 7474:7474 -p 7687:7687 \
    -v "$DATA:/data" \
    -e "NEO4J_AUTH=neo4j/$PASSWORD" \
    -e NEO4J_server_memory_pagecache_size=512M \
    "$IMAGE" >/dev/null
  echo "created container '$NAME' (data in $DATA)"
fi

echo -n "waiting for bolt handshake on 7687 "
for _ in $(seq 1 60); do
  # A TCP connect is not enough: Bolt only answers once the database is started,
  # so probe with cypher-shell inside the container.
  if "$ENGINE" exec "$NAME" cypher-shell -u neo4j -p "$PASSWORD" "RETURN 1" >/dev/null 2>&1; then
    echo "— ready"
    echo
    echo "Neo4j is up. .env should contain:"
    echo "  NEO4J_URI=bolt://localhost:7687"
    echo "  NEO4J_USER=neo4j"
    echo "  NEO4J_PASSWORD=$PASSWORD"
    echo "Browser UI: http://localhost:7474"
    exit 0
  fi
  echo -n "."
  sleep 1
done

echo "— timed out"
echo "Check logs with: $ENGINE logs $NAME" >&2
exit 1
