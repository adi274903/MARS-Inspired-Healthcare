#!/usr/bin/env bash
# Bring the split topology up in a single podman pod ("evalbot").
#
#   ./scripts/up.sh            # pod + redis + 9 services
#   POD=evalbot IMAGE=python:3.12-slim ./scripts/up.sh
#
# Every service is the same repo bind-mounted into one container:
#   python -m services.<name>.main   with BUS=redis and its own PORT.
# Inside a pod all containers share a network namespace, so redis is reachable
# at 127.0.0.1:6379.
#
# No podman? The script prints the monolith command instead of failing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POD="${POD:-evalbot}"
IMAGE="${IMAGE:-docker.io/library/python:3.12-slim}"
REDIS_IMAGE="${REDIS_IMAGE:-docker.io/library/redis:7-alpine}"

# service name -> host port (must match core/config.py service_ports)
SERVICES=(
  "gateway:8080"
  "orchestrator:8081"
  "sqlite_service:8082"
  "vector_service:8083"
  "graph_service:8084"
  "llm_service:8085"
  "answer_service:8086"
  "event_log:8087"
  "control_plane:8090"
)

monolith_fallback() {
  cat <<EOF

podman is not available on this host.

Stop-gap: run the monolith (identical service code, in-memory bus):

  cd "$ROOT"
  MONOLITH=1 BUS=memory PORT=8080 .venv/bin/python -m services.monolith.app

then check it with:

  PORT=8080 ./scripts/smoke.sh
EOF
}

if ! command -v podman >/dev/null 2>&1; then
  monolith_fallback
  exit 0
fi

echo "+ podman pod create --name $POD --share net"
if ! podman pod exists "$POD" 2>/dev/null; then
  podman pod create --name "$POD" --share net \
    -p 8080:8080 -p 8081:8081 -p 8082:8082 -p 8083:8083 -p 8084:8084 \
    -p 8085:8085 -p 8086:8086 -p 8087:8087 -p 8090:8090 -p 6379:6379
else
  echo "  pod $POD already exists"
fi

echo "+ podman run --pod $POD redis"
if ! podman container exists "${POD}_redis" 2>/dev/null; then
  podman run -d --pod "$POD" --name "${POD}_redis" "$REDIS_IMAGE"
else
  echo "  redis container already exists"
fi

# Wait for redis to answer before the services start publishing.
for _ in $(seq 1 30); do
  if podman exec "${POD}_redis" redis-cli ping >/dev/null 2>&1; then
    echo "  redis: PONG"
    break
  fi
  sleep 1
done

for entry in "${SERVICES[@]}"; do
  name="${entry%%:*}"
  port="${entry##*:}"
  container="${POD}_${name}"
  echo "+ podman run --pod $POD $container (services.$name.main, PORT=$port)"
  if podman container exists "$container" 2>/dev/null; then
    echo "  $container already exists (use ./scripts/down.sh first)"
    continue
  fi
  podman run -d --pod "$POD" --name "$container" \
    -e BUS=redis \
    -e REDIS_URL=redis://127.0.0.1:6379/0 \
    -e PORT="$port" \
    -e PYTHONPATH=/app \
    -e PYTHONUNBUFFERED=1 \
    -v "$ROOT":/app:ro \
    -w /app \
    "$IMAGE" \
    sh -c "pip install --quiet --no-cache-dir -r requirements.txt && exec python -m services.${name}.main"
done

echo
echo "pod $POD up. Gateway: http://localhost:8080  (health: /healthz)"
echo "logs: podman logs -f ${POD}_gateway     teardown: ./scripts/down.sh"
