#!/usr/bin/env bash
# Tear down the podman pod created by ./scripts/up.sh (containers, then the pod).
set -euo pipefail

POD="${POD:-evalbot}"

if ! command -v podman >/dev/null 2>&1; then
  cat <<EOF
podman is not available on this host.

If you started the monolith instead, stop it with:
  pkill -f "services.monolith.app"
EOF
  exit 0
fi

if ! podman pod exists "$POD" 2>/dev/null; then
  echo "pod $POD does not exist - nothing to do"
  exit 0
fi

while read -r container; do
  [[ -z "$container" ]] && continue
  echo "+ podman rm -f $container"
  podman rm -f "$container" >/dev/null 2>&1 || true
done < <(podman ps -a --filter "pod=$POD" --format "{{.Names}}")

echo "+ podman pod rm -f $POD"
podman pod rm -f "$POD"

echo "pod $POD removed"
