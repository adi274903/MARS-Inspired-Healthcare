#!/usr/bin/env bash
# curl-based smoke test for a running instance (monolith or pod).
#
#   ./scripts/smoke.sh                 # http://localhost:8080
#   PORT=9000 ./scripts/smoke.sh
#   BASE_URL=http://localhost:8080 ./scripts/smoke.sh
#
# One pass/fail line per check; exits non-zero if any check fails.
set -uo pipefail

PORT="${PORT:-8080}"
BASE_URL="${BASE_URL:-http://localhost:$PORT}"
QUESTION="${QUESTION:-Which papers externally validate a sepsis prediction model?}"
TIMEOUT="${TIMEOUT:-60}"

FAILED=0
TMPDIR_SMOKE="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_SMOKE"' EXIT

pass() { printf 'PASS  %s\n' "$1"; }
fail() {
  printf 'FAIL  %s\n' "$1"
  if [[ -n "${2:-}" ]]; then printf '      %s\n' "$2"; fi
  FAILED=$((FAILED + 1))
}

require_curl() {
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required for smoke.sh" >&2
    exit 2
  fi
}

echo "smoke: $BASE_URL"

# ------------------------------------------------------------------ 1. /healthz
require_curl
HEALTH_BODY="$TMPDIR_SMOKE/health.json"
HEALTH_CODE="$(curl -sS -m "$TIMEOUT" -o "$HEALTH_BODY" -w '%{http_code}' "$BASE_URL/healthz" 2>"$TMPDIR_SMOKE/health.err" || true)"
if [[ "$HEALTH_CODE" == "200" ]] && grep -q '"ok"' "$HEALTH_BODY"; then
  pass "GET /healthz -> 200 $(head -c 160 "$HEALTH_BODY")"
else
  fail "GET /healthz -> $HEALTH_CODE" "$(cat "$TMPDIR_SMOKE/health.err" 2>/dev/null | head -c 200)/$(head -c 200 "$HEALTH_BODY" 2>/dev/null)"
fi

# ------------------------------------------------------- 2. /api/chat (blocking)
CHAT_BODY="$TMPDIR_SMOKE/chat.json"
CHAT_CODE="$(curl -sS -m "$TIMEOUT" -o "$CHAT_BODY" -w '%{http_code}' \
  -H 'content-type: application/json' \
  -d "{\"messages\":[{\"role\":\"user\",\"text\":\"$QUESTION\"}]}" \
  "$BASE_URL/api/chat" 2>"$TMPDIR_SMOKE/chat.err" || true)"
if [[ "$CHAT_CODE" == "200" ]] && grep -q '"text"' "$CHAT_BODY"; then
  TEXT_LEN="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("text") or ""))' "$CHAT_BODY" 2>/dev/null || echo 0)"
  if [[ "${TEXT_LEN:-0}" -gt 0 ]]; then
    pass "POST /api/chat -> 200 (${TEXT_LEN} chars of answer)"
  else
    fail "POST /api/chat -> 200 but the answer text is empty"
  fi
else
  fail "POST /api/chat -> $CHAT_CODE" "$(head -c 300 "$CHAT_BODY" 2>/dev/null)$(head -c 200 "$TMPDIR_SMOKE/chat.err" 2>/dev/null)"
fi

# ------------------------------------------------- 3. /api/chat/stream (SSE)
STREAM="$TMPDIR_SMOKE/stream.txt"
STREAM_CODE="$(curl -sS -N -m "$TIMEOUT" -o "$STREAM" -w '%{http_code}' \
  -H 'content-type: application/json' \
  -H 'accept: text/event-stream' \
  -d "{\"messages\":[{\"role\":\"user\",\"text\":\"$QUESTION\"}]}" \
  "$BASE_URL/api/chat/stream" 2>"$TMPDIR_SMOKE/stream.err" || true)"
FIRST_EVENT="$(grep -m1 '^event: ' "$STREAM" 2>/dev/null | sed 's/^event: //' | tr -d '\r')"
LAST_EVENT="$(grep '^event: ' "$STREAM" 2>/dev/null | tail -n1 | sed 's/^event: //' | tr -d '\r')"
if [[ "$FIRST_EVENT" == "meta" && "$LAST_EVENT" == "done" ]]; then
  pass "POST /api/chat/stream -> $STREAM_CODE (first=meta, last=done, $(grep -c '^event: ' "$STREAM") frames)"
else
  fail "POST /api/chat/stream -> $STREAM_CODE (first='${FIRST_EVENT:-none}' last='${LAST_EVENT:-none}')" \
    "$(head -c 300 "$STREAM" 2>/dev/null)$(head -c 200 "$TMPDIR_SMOKE/stream.err" 2>/dev/null)"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then
  echo "smoke: OK (3/3)"
  exit 0
fi
echo "smoke: FAILED ($FAILED of 3 checks)"
exit 1
