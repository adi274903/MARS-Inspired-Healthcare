"""Gateway tests: REST + SSE over an in-memory bus, no network, no real model.

The dispatcher loop and the HTTP app share one event loop: the test starts
``spec.loops`` itself and drives the ASGI app through
``httpx.AsyncClient(transport=ASGITransport(app))``, publishing the events the
rest of the system would produce directly on the ``MemoryBus``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from core.bus import MemoryBus
from core.config import get_settings
from core.envelope import Event
from services.common import make_app
from services.gateway.main import build as build_gateway
from tests.support import build_fixture_db

BODY: dict[str, Any] = {
    "messages": [{"role": "user", "text": "what models were used?"}],
    "context": {"system": "clinical", "stage": "extraction", "region": "EU", "focus": "llm"},
}

RETRIEVAL = {
    "question": "what models were used?",
    "parsed": {"focus": "llm"},
    "evidence": [
        {"pmid": "111", "title": "Paper one", "venue": "JTest", "year": 2021, "doi": "10.1/one"},
        {"pmid": "222", "title": "Paper two", "venue": "JTest", "year": 2022, "doi": "10.1/two"},
    ],
    "stats": {"n": 2},
    "degraded": ["vector_off"],
    "route": "local",
}

ANSWER = {
    "text": "Hello world",
    "checklist": {"population": "adults"},
    "citations": [{"pmid": "111", "title": "Paper one"}],
    "markers": [],
    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    "model": "mock/model",
    "profile": "chat",
    "route": "local",
    "degraded": [],
}


# ------------------------------------------------------------------ helpers
def make_settings(tmp_path: Path, monkeypatch, *, chat_timeout: float = 5.0):
    db = build_fixture_db(tmp_path)
    monkeypatch.setenv("MONOLITH", "1")
    monkeypatch.setenv("BUS", "memory")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("MODELS_YAML", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events" / "events.db"))
    monkeypatch.setenv("CHAT_TIMEOUT_S", str(chat_timeout))
    return get_settings()


async def wait_until(predicate: Callable[[], Awaitable[bool]], *, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


async def matching(bus: MemoryBus, type_: str, *, exclude: str | None = None) -> list[Event]:
    return [e for e in await bus.history() if e.type == type_ and (exclude is None or e.correlation_id != exclude)]


async def wait_for_event(bus: MemoryBus, type_: str, *, exclude: str | None = None) -> Event:
    async def ready() -> bool:
        return bool(await matching(bus, type_, exclude=exclude))

    await wait_until(ready)
    return (await matching(bus, type_, exclude=exclude))[0]


async def wait_for_count(bus: MemoryBus, type_: str, count: int) -> None:
    async def enough() -> bool:
        return len(await matching(bus, type_)) >= count

    await wait_until(enough)


@contextlib.asynccontextmanager
async def gateway_harness(settings) -> AsyncIterator[tuple[MemoryBus, httpx.AsyncClient, Any]]:
    bus = MemoryBus()
    await bus.start()
    spec = build_gateway(settings, bus)
    app = make_app("gateway", settings, spec.router)
    for hook in spec.on_start:
        await hook()
    for loop in spec.loops:
        await loop.start()
    await asyncio.sleep(0.02)  # let the dispatcher subscription register
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            yield bus, client, spec
    finally:
        for loop in spec.loops:
            await loop.stop()
        await bus.stop()


def parse_sse(lines: list[str]) -> list[tuple[str, Any]]:
    """Parse ``event:``/``data:`` blocks; every non-blank line must be an SSE field."""
    frames: list[tuple[str, Any]] = []
    name: str | None = None
    for line in lines:
        if line == "" or line.startswith(":"):
            continue
        assert line.startswith(("event: ", "data: ")), f"bad SSE line: {line!r}"
        if line.startswith("event: "):
            name = line[len("event: ") :]
        else:
            assert name is not None, "data before event"
            frames.append((name, json.loads(line[len("data: ") :])))
            name = None
    return frames


async def publish_answer(bus: MemoryBus, correlation_id: str, *, chunks: tuple[str, ...] = ("Hello ", "world")) -> None:
    await bus.publish(Event(type="retrieval.completed", payload=RETRIEVAL, correlation_id=correlation_id))
    for seq, text in enumerate(chunks):
        await bus.publish(Event(type="llm.token", payload={"seq": seq, "text": text}, correlation_id=correlation_id))
    await bus.publish(Event(type="answer.completed", payload=dict(ANSWER), correlation_id=correlation_id))


# ------------------------------------------------------------------ tests
@pytest.mark.asyncio
async def test_post_returns_exactly_text_checklist_citations(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    async with gateway_harness(settings) as (bus, client, spec):
        pending = asyncio.create_task(client.post("/api/chat", json=BODY))
        request = await wait_for_event(bus, "chat.requested")
        assert request.payload["messages"] == [{"role": "user", "text": "what models were used?"}]
        # focus is an array in the UI contract; a bare string is coerced to a one-item list.
        assert request.payload["context"] == {
            "system": "clinical",
            "stage": "extraction",
            "region": "EU",
            "focus": ["llm"],
        }
        assert "profile" not in request.payload
        assert request.deadline is not None
        assert request.producer == "gateway"

        await publish_answer(bus, request.correlation_id)
        response = await pending

        assert response.status_code == 200
        assert response.json() == {
            "text": "Hello world",
            "checklist": ANSWER["checklist"],
            "citations": ANSWER["citations"],
        }
        # the sink is pruned once the correlation settled
        assert spec.meta["dispatcher"].sink_count(request.correlation_id) == 0


@pytest.mark.asyncio
async def test_model_profile_override_from_header_and_context(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    async with gateway_harness(settings) as (bus, client, spec):
        pending = asyncio.create_task(client.post("/api/chat", json=BODY, headers={"X-Model-Profile": "judge"}))
        request = await wait_for_event(bus, "chat.requested")
        assert request.payload["profile"] == "judge"
        await publish_answer(bus, request.correlation_id)
        assert (await pending).status_code == 200

        body_with_profile = {**BODY, "context": {**BODY["context"], "profile": "judge"}}
        pending = asyncio.create_task(client.post("/api/chat", json=body_with_profile))
        request = await wait_for_event(bus, "chat.requested", exclude=request.correlation_id)
        assert request.payload["profile"] == "judge"
        await publish_answer(bus, request.correlation_id)
        assert (await pending).status_code == 200


@pytest.mark.asyncio
async def test_stream_emits_meta_delta_done_in_order(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    async with gateway_harness(settings) as (bus, client, spec):
        async def publisher() -> None:
            request = await wait_for_event(bus, "chat.requested")
            await publish_answer(bus, request.correlation_id)

        pub = asyncio.create_task(publisher())
        async with client.stream("POST", "/api/chat/stream", json=BODY) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["x-accel-buffering"] == "no"
            lines = [line async for line in response.aiter_lines()]
        await pub

        frames = parse_sse(lines)
        assert [name for name, _ in frames] == ["meta", "delta", "delta", "done"]

        meta = frames[0][1]
        assert meta["route"] == "local"
        assert meta["evidence_count"] == 2
        assert meta["sources"] == [
            {"pmid": "111", "title": "Paper one", "venue": "JTest", "year": 2021, "doi": "10.1/one"},
            {"pmid": "222", "title": "Paper two", "venue": "JTest", "year": 2022, "doi": "10.1/two"},
        ]
        assert meta["degraded"] == ["vector_off"]

        deltas = [data["text"] for name, data in frames if name == "delta"]
        assert "".join(deltas) == ANSWER["text"]
        assert frames[-1][1] == ANSWER
        assert frames[-1][1] == ANSWER


@pytest.mark.asyncio
async def test_rest_timeout_returns_504(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch, chat_timeout=0.2)
    async with gateway_harness(settings) as (bus, client, spec):
        response = await client.post("/api/chat", json=BODY)
        assert response.status_code == 504
        assert response.json()["detail"] == "model did not answer in time"
        assert await matching(bus, "chat.requested")


@pytest.mark.asyncio
async def test_stream_timeout_emits_error_frame(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch, chat_timeout=0.2)
    async with gateway_harness(settings) as (bus, client, spec):
        async with client.stream("POST", "/api/chat/stream", json=BODY) as response:
            lines = [line async for line in response.aiter_lines()]
        frames = parse_sse(lines)
        assert frames and frames[-1][0] == "error"
        assert "did not answer" in frames[-1][1]["message"]


@pytest.mark.asyncio
async def test_answer_failed_is_503_and_error_frame(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    async with gateway_harness(settings) as (bus, client, spec):
        pending = asyncio.create_task(client.post("/api/chat", json=BODY))
        request = await wait_for_event(bus, "chat.requested")
        await bus.publish(
            Event(
                type="answer.failed",
                payload={"service": "llm_service", "message": "model exploded"},
                correlation_id=request.correlation_id,
            )
        )
        response = await pending
        assert response.status_code == 503
        assert response.json()["detail"] == "model exploded"

        async def publisher() -> None:
            second = await wait_for_event(bus, "chat.requested", exclude=request.correlation_id)
            await bus.publish(Event(type="llm.token", payload={"seq": 0, "text": "par"}, correlation_id=second.correlation_id))
            await bus.publish(
                Event(
                    type="answer.failed",
                    payload={"service": "llm_service", "message": "model exploded"},
                    correlation_id=second.correlation_id,
                )
            )

        pub = asyncio.create_task(publisher())
        async with client.stream("POST", "/api/chat/stream", json=BODY) as response:
            lines = [line async for line in response.aiter_lines()]
        await pub
        frames = parse_sse(lines)
        assert [name for name, _ in frames] == ["delta", "error"]
        assert frames[-1][1] == {"message": "model exploded", "partial": "par"}


@pytest.mark.asyncio
async def test_client_disconnect_publishes_chat_cancelled(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch, chat_timeout=30.0)
    async with gateway_harness(settings) as (bus, client, spec):
        async def consume() -> None:
            async with client.stream("POST", "/api/chat/stream", json=BODY) as response:
                async for _chunk in response.aiter_bytes():
                    pass

        stream_task = asyncio.create_task(consume())
        request = await wait_for_event(bus, "chat.requested")
        await asyncio.sleep(0.05)
        stream_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stream_task

        await wait_for_count(bus, "chat.cancelled", 1)
        cancelled = (await matching(bus, "chat.cancelled"))[0]
        assert cancelled.payload["reason"] == "client_disconnected"
        assert cancelled.correlation_id == request.correlation_id
        assert spec.meta["dispatcher"].sink_count(request.correlation_id) == 0


def test_healthz_with_testclient(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    bus = MemoryBus()
    spec = build_gateway(settings, bus)
    app = make_app("gateway", settings, spec.router)
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "gateway"
    assert body["ok"] is True
    assert body["degraded"] == []
    assert body["model"] == "google/gemma-4-26b-a4b-it"
    assert client.post("/api/chat", json={"messages": []}).status_code == 422


def test_ui_is_served_after_the_api_routes(tmp_path, monkeypatch):
    make_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("MONOLITH", "0")  # standalone: the gateway serves the UI itself
    (tmp_path / "index.html").write_text("<html><body>evalbot ui</body></html>")
    (tmp_path / "app.js").write_text("console.log('ui')")
    settings = get_settings()

    bus = MemoryBus()
    spec = build_gateway(settings, bus)
    app = make_app("gateway", settings, spec.router)
    client = TestClient(app)

    assert "evalbot ui" in client.get("/").text
    assert "console.log" in client.get("/app.js").text
    # API routes still win over the static catch-all
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["service"] == "gateway"
    assert client.get("/does-not-exist.txt").status_code == 404


@pytest.mark.asyncio
async def test_keepalive_comments_while_waiting(tmp_path, monkeypatch):
    import services.gateway.main as gateway_main

    monkeypatch.setattr(gateway_main, "KEEPALIVE_S", 0.05)
    settings = make_settings(tmp_path, monkeypatch, chat_timeout=0.3)
    async with gateway_harness(settings) as (bus, client, spec):
        async with client.stream("POST", "/api/chat/stream", json=BODY) as response:
            lines = [line async for line in response.aiter_lines()]
        keepalives = [line for line in lines if line.startswith(":")]
        assert keepalives, "expected ': keepalive' comment lines while waiting"
        assert set(keepalives) == {": keepalive"}
        frames = parse_sse(lines)
        assert frames[-1][0] == "error"
