"""PublicApiGuard: default-deny API, abuse limits, hardened headers (a tiny app, no MARS services needed)."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from core.security import CSP, PublicApiGuard

TOKEN = "t0ken-for-tests"


def make(**kw) -> TestClient:
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def home():
        return "<html></html>"

    @app.get("/api/events")
    def events():
        return []

    @app.post("/api/chat")
    async def chat(request: Request):
        return {"profile_header": request.headers.get("x-model-profile")}

    @app.post("/api/chat/stream")
    def stream():
        return {"ok": True}

    app.add_middleware(PublicApiGuard, admin_token=TOKEN, enabled=kw.pop("enabled", True), **kw)
    return TestClient(app)


def ip(n: int) -> dict[str, str]:
    return {"x-forwarded-for": f"203.0.113.{n}"}


def test_default_deny_needs_the_admin_token():
    c = make()
    assert c.get("/api/events", headers=ip(1)).status_code == 401
    assert c.get("/api/events", headers={**ip(1), "authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/events", headers={**ip(1), "authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert c.get("/api/events", headers=ip(1)).headers["www-authenticate"] == "Bearer"


def test_chat_routes_and_the_ui_are_public():
    c = make()
    assert c.get("/", headers=ip(2)).status_code == 200
    assert c.post("/api/chat", json={"a": 1}, headers=ip(2)).status_code == 200
    assert c.post("/api/chat/stream", json={"a": 1}, headers=ip(2)).status_code == 200


def test_security_headers_and_csp_on_html():
    r = make().get("/", headers=ip(3))
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-security-policy"] == CSP and "frame-ancestors 'none'" in CSP
    denied = make().get("/api/events", headers=ip(3))
    assert denied.headers["x-content-type-options"] == "nosniff"  # error responses are hardened too


def test_per_ip_rate_limit_and_retry_after():
    c = make(per_minute=3)
    for _ in range(3):
        assert c.post("/api/chat", json={"a": 1}, headers=ip(4)).status_code == 200
    blocked = c.post("/api/chat", json={"a": 1}, headers=ip(4))
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    assert c.post("/api/chat", json={"a": 1}, headers=ip(5)).status_code == 200  # another caller is unaffected


def test_daily_cap():
    c = make(daily_cap=2, per_minute=50)
    assert [c.post("/api/chat", json={"a": 1}, headers=ip(6)).status_code for _ in range(3)] == [200, 200, 429]


def test_oversized_body_is_rejected():
    c = make(max_body=50)
    r = c.post("/api/chat", json={"text": "x" * 200}, headers=ip(7))
    assert r.status_code == 413


def test_caller_cannot_choose_the_model_profile():
    c = make()
    r = c.post("/api/chat", json={"a": 1}, headers={**ip(8), "x-model-profile": "judge"})
    assert r.json() == {"profile_header": None}


def test_disabled_mode_changes_nothing_but_adds_headers():
    c = make(enabled=False)
    assert c.get("/api/events", headers=ip(9)).status_code == 200
    assert c.get("/", headers=ip(9)).headers["x-content-type-options"] == "nosniff"
