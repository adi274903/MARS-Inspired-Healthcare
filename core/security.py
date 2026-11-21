"""Public-deployment hardening as one ASGI middleware (``PUBLIC_API_ONLY=1``; off for local dev and the test suite).

* default-deny: the only public API routes are ``POST /api/chat`` and ``POST /api/chat/stream``; every other
  ``/api/*`` route (event log, admin, internal service routes) needs ``Authorization: Bearer $ADMIN_TOKEN``,
  compared in constant time;
* abuse limits on the two chat routes: request body size, per-IP requests per minute, and a daily question cap
  (the free LLM tier and the single instance are the scarce resources; CORS does not stop ``curl``);
* the caller-chosen ``X-Model-Profile`` header is dropped on public requests;
* security headers on every response (CSP for the UI, nosniff, no framing, no referrer, locked-down permissions).

Pure ASGI (not ``BaseHTTPMiddleware``) so Server-Sent Events keep streaming. Counters live in process memory,
which is exact for the single Cloud Run instance this runs on (``max_instances = 1``).
"""
from __future__ import annotations

import hmac
import json
import os
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from core.telemetry import count, log

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

PUBLIC_POST = {"/api/chat", "/api/chat/stream"}

#: CSP for the UI served by the backend itself. The page has one inline config script and loads Google Fonts.
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)
HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]


def public_mode() -> bool:
    return os.environ.get("PUBLIC_API_ONLY", "").strip().lower() in {"1", "true", "yes"}


def client_ip(scope: Scope) -> str:
    """Cloud Run appends the real client address as the LAST x-forwarded-for entry; earlier ones are caller-supplied."""
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            parts = [p.strip() for p in value.decode("latin-1").split(",") if p.strip()]
            if parts:
                return parts[-1]
    client = scope.get("client")
    return client[0] if client else "unknown"


class PublicApiGuard:
    def __init__(self, app: Any, admin_token: str, *, per_minute: int | None = None, daily_cap: int | None = None,
                 max_body: int | None = None, enabled: bool | None = None) -> None:
        self.app = app
        self.admin_token = admin_token
        self.enabled = public_mode() if enabled is None else enabled
        self.per_minute = per_minute if per_minute is not None else int(os.environ.get("RATE_LIMIT_PER_MIN", "8"))
        self.daily_cap = daily_cap if daily_cap is not None else int(os.environ.get("DAILY_QUESTION_CAP", "400"))
        self.max_body = max_body if max_body is not None else int(os.environ.get("MAX_BODY_BYTES", "32768"))
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._day = time.strftime("%Y-%m-%d", time.gmtime())
        self._today = 0

    # -- helpers ---------------------------------------------------------
    def _is_admin(self, scope: Scope) -> bool:
        supplied = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                supplied = value
        expected = f"Bearer {self.admin_token}".encode()
        return bool(self.admin_token) and hmac.compare_digest(supplied.strip(), expected)

    def _rate_limited(self, ip: str) -> int | None:
        """Seconds until the caller may retry, or None if allowed (records the hit)."""
        now = time.monotonic()
        window = self._hits[ip]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return max(1, int(60 - (now - window[0])) + 1)
        window.append(now)
        return None

    def _over_daily_cap(self) -> bool:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self._day:
            self._day, self._today = day, 0
        if self._today >= self.daily_cap:
            return True
        self._today += 1
        return False

    @staticmethod
    async def _reply(send: Send, status: int, detail: str, extra: list[tuple[bytes, bytes]] | None = None) -> None:
        body = json.dumps({"detail": detail}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *HEADERS,
                   *(extra or [])]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    # -- ASGI ------------------------------------------------------------
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for key, value in HEADERS:
                    if key not in present:
                        headers.append((key, value))
                content_type = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if content_type.startswith(b"text/html") and b"content-security-policy" not in present:
                    headers.append((b"content-security-policy", CSP.encode()))
                message = {**message, "headers": headers}
            await send(message)

        if not self.enabled:
            await self.app(scope, receive, send_with_headers)
            return

        method, path = scope["method"], scope["path"].rstrip("/") or "/"
        if method == "OPTIONS" or not path.startswith("/api/"):
            await self.app(scope, receive, send_with_headers)  # CORS preflight and the static UI
            return

        if method == "POST" and path in PUBLIC_POST:
            length = next((int(v) for k, v in scope.get("headers", []) if k == b"content-length" and v.isdigit()), 0)
            if length > self.max_body:
                count("security.too_large")
                await self._reply(send_with_headers, 413, "request too large")
                return
            retry = self._rate_limited(client_ip(scope))
            if retry is not None:
                count("security.rate_limited")
                log("security", "rate_limited", ip=client_ip(scope))
                await self._reply(send_with_headers, 429, "too many requests; slow down",
                                  [(b"retry-after", str(retry).encode())])
                return
            if self._over_daily_cap():
                count("security.daily_cap")
                await self._reply(send_with_headers, 429, "daily question limit reached; try again tomorrow",
                                  [(b"retry-after", b"3600")])
                return
            # Public callers may not choose the model profile.
            scope = {**scope, "headers": [(k, v) for k, v in scope["headers"] if k != b"x-model-profile"]}
            seen = 0

            async def receive_limited() -> dict[str, Any]:
                nonlocal seen
                message = await receive()
                if message["type"] == "http.request":
                    seen += len(message.get("body", b""))
                    if seen > self.max_body:  # chunked body with no content-length: cut it off (-> 422, never parsed)
                        count("security.too_large")
                        return {"type": "http.request", "body": b"", "more_body": False}
                return message

            await self.app(scope, receive_limited, send_with_headers)
            return

        if self._is_admin(scope):
            await self.app(scope, receive, send_with_headers)
            return
        count("security.denied")
        await self._reply(send_with_headers, 401, "unauthorized", [(b"www-authenticate", b"Bearer")])
