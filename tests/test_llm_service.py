"""Tests for the llm_service: batching, streamed JSON decoding, hot reload.

The service is driven through a real ``MemoryBus``; model access uses the mock
provider (``LLM_PROVIDER=mock``) so nothing touches the network or a GPU.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from core.bus import MemoryBus, ServiceLoop, collect
from core.config import Settings, get_settings
from core.envelope import Event, new_event
from llm.stream_decode import extract_text
from retrieval.prompt import build_messages, format_evidence
from services.llm_service import main as llm_main
from tests.support import build_fixture_db, fixture_rows

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- helpers
def make_settings(tmp_path, monkeypatch) -> Settings:
    db = build_fixture_db(tmp_path)
    monkeypatch.setenv("MONOLITH", "1")
    monkeypatch.setenv("BUS", "memory")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("MODELS_YAML", str(tmp_path / "models.yaml"))
    return get_settings()


async def wait_subscribed(bus: MemoryBus, count: int = 1, *, timeout: float = 2.0) -> None:
    """MemoryBus has no ready signal: wait until the loop actually subscribed."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if sum(len(subs) for subs in bus._subs.values()) >= count:
            return
        await asyncio.sleep(0.005)
    raise TimeoutError("service loop did not subscribe")


@contextlib.asynccontextmanager
async def running(settings):
    bus = MemoryBus()
    await bus.start()
    spec = llm_main.build(settings, bus)
    for loop in spec.loops:
        await loop.start()
    await wait_subscribed(bus, len(spec.loops))
    try:
        yield bus, spec
    finally:
        for loop in spec.loops:
            await loop.stop()
        await bus.stop()


def make_messages() -> list[dict[str, str]]:
    rows = fixture_rows()[:3]
    return build_messages("How is sepsis triage validated?", rows, stats={"total": 12})


def tokens_of(history: list[Event]) -> list[dict]:
    return [e.payload for e in history if e.type == "llm.token"]


async def wait_for(predicate, *, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("condition not reached")


# ---------------------------------------------------------------- batching
async def test_token_batcher_flushes_on_char_limit():
    batcher = llm_main.TokenBatcher(char_limit=10, ms_limit=60_000)
    assert batcher.add("12345", now=0.0) is None
    assert batcher.add("67890", now=0.001) == "1234567890"
    assert batcher.seq == 1
    assert batcher.pending() == ""

    # a frame shorter than the limit stays pending until the stream ends
    assert batcher.add("abc", now=0.002) is None
    assert batcher.take() == "abc"
    assert batcher.seq == 2
    assert batcher.take() is None


async def test_token_batcher_flushes_on_time():
    batcher = llm_main.TokenBatcher(char_limit=1000, ms_limit=40)
    assert batcher.add("slow", now=10.0) is None
    assert batcher.timeout(now=10.0) == pytest.approx(0.04)
    assert batcher.timeout(now=10.02) == pytest.approx(0.02)
    assert batcher.add("", now=10.05) == "slow"  # ms_limit elapsed
    assert batcher.seq == 1
    assert batcher.timeout() is None


# ---------------------------------------------------------------- streaming
async def test_stream_emits_tokens_before_completion(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    payload = {"messages": make_messages(), "profile": "chat"}

    async with running(settings) as (bus, _spec):
        event = new_event("generation.requested", payload, producer="orchestrator", timeout_s=30.0)
        await bus.publish(event)

        completed = (await collect(bus, count=1, types=("llm.completed",), timeout=10.0))[0]
        history = await bus.history()
        types = [e.type for e in history]
        assert "llm.token" in types
        assert types.index("llm.token") < types.index("llm.completed")
        assert "llm.failed" not in types
        assert "answer.failed" not in types

        tokens = [t for t in tokens_of(history) if t["text"]]
        assert [t["seq"] for t in tokens] == list(range(1, len(tokens) + 1))
        streamed = "".join(t["text"] for t in tokens)
        assert streamed.strip()

        body = completed.payload
        assert completed.correlation_id == event.correlation_id
        assert body["profile"] == "chat"
        assert body["model"]
        assert body["usage"]["prompt_tokens"] > 0
        assert body["usage"]["completion_tokens"] == len(body["raw"]) // 4

        decoded = json.loads(body["raw"])
        assert "text" in decoded
        assert decoded["text"].strip()
        assert streamed == decoded["text"]
        assert body["text"] == decoded["text"]
        assert body["tokens_emitted"] == len(tokens)

        # the mock provider echoes the evidence it can see in the prompt
        assert "pmid=" in body["raw"] or "Corpus counts" in decoded["text"]


async def test_batching_splits_into_multiple_frames(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    settings.token_batch_chars = 16
    settings.token_batch_ms = 5

    async with running(settings) as (bus, _spec):
        await bus.publish(new_event("generation.requested", {"messages": make_messages()}, producer="orchestrator"))
        await collect(bus, count=1, types=("llm.completed",), timeout=10.0)
        tokens = tokens_of(await bus.history())
        assert len(tokens) > 3
        # a frame can overshoot by at most one provider chunk (mock streams 37 chars)
        assert all(len(t["text"]) <= 16 + 37 for t in tokens)
        assert [t["seq"] for t in tokens] == list(range(1, len(tokens) + 1))
        streamed = "".join(t["text"] for t in tokens)
        completed = [e for e in await bus.history() if e.type == "llm.completed"][0]
        assert streamed == json.loads(completed.payload["raw"])["text"]


async def test_scripted_stream_yields_decoded_fragments(tmp_path, monkeypatch):
    """A hand-fed stream proves tokens carry the JSON 'text' value, not raw JSON."""
    settings = make_settings(tmp_path, monkeypatch)
    settings.token_batch_chars = 10_000
    settings.token_batch_ms = 5
    chunks = ['{"text": "Hello wor', 'ld, EvalBot.', '", "claims": {}}']
    raw = "".join(chunks)

    async with running(settings) as (bus, spec):
        client = spec.meta["llm"].client_for("chat")

        async def fake_stream(messages, *, schema=None):
            for chunk in chunks:
                yield chunk
                await asyncio.sleep(0.001)

        client.stream = fake_stream
        await bus.publish(new_event("generation.requested", {"messages": make_messages(), "profile": "chat"}, producer="orchestrator"))
        completed = (await collect(bus, count=1, types=("llm.completed",), timeout=5.0))[0]

        tokens = tokens_of(await bus.history())
        streamed = "".join(t["text"] for t in tokens)
        assert streamed == "Hello world, EvalBot."
        assert "{" not in streamed and '"text"' not in streamed
        assert completed.payload["raw"] == raw
        assert completed.payload["text"] == "Hello world, EvalBot."
        assert extract_text(raw) == "Hello world, EvalBot."
        assert json.loads(completed.payload["raw"])["text"] == streamed


async def test_failing_stream_emits_llm_failed_with_partial(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)

    async with running(settings) as (bus, spec):
        client = spec.meta["llm"].client_for("chat")

        async def broken_stream(messages, *, schema=None):
            yield '{"text": "partial ans'
            await asyncio.sleep(0.001)
            raise RuntimeError("provider went away")

        client.stream = broken_stream
        await bus.publish(new_event("generation.requested", {"messages": make_messages(), "profile": "chat"}, producer="orchestrator"))
        failed = (await collect(bus, count=1, types=("llm.failed",), timeout=5.0))[0]
        assert "provider went away" in failed.payload["message"]
        assert failed.payload["partial"].startswith("partial ans")

        history = await bus.history()
        assert "llm.completed" not in [e.type for e in history]
        assert "answer.failed" not in [e.type for e in history]


# ---------------------------------------------------------------- hot reload
async def test_model_changed_reloads_client(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)

    async with running(settings) as (bus, spec):
        service = spec.meta["llm"]
        client = service.client_for("chat")
        calls: list[int] = []
        original = client.reload

        def spy() -> dict:
            calls.append(1)
            return original()

        client.reload = spy
        await bus.publish(new_event("model.changed", {"profile": "chat", "config": {"model": "x/y"}}, producer="control_plane"))
        await wait_for(lambda: bool(calls))
        assert len(calls) == 1
        assert service._verify.get("chat") is None


async def test_model_changed_survives_bad_profile(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)

    async with running(settings) as (bus, spec):
        service = spec.meta["llm"]
        client = service.client_for("judge")

        def explode() -> dict:
            raise RuntimeError("registry is on fire")

        client.reload = explode
        event = new_event("model.changed", {"profile": "judge", "config": {}}, producer="control_plane")
        assert await service.on_event(event) == []  # swallowed, not raised
        await bus.publish(event)

        # the loop is still alive and still answers
        await bus.publish(new_event("generation.requested", {"messages": make_messages(), "profile": "judge"}, producer="orchestrator"))
        completed = (await collect(bus, count=1, types=("llm.completed",), timeout=5.0))[0]
        assert completed.payload["profile"] == "judge"


# ---------------------------------------------------------------- http surface
async def test_models_endpoint_caches_verify(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    bus = MemoryBus()
    spec = llm_main.build(settings, bus)
    endpoint = next(r.endpoint for r in spec.router.routes if getattr(r, "path", "") == "/api/models")

    first = await endpoint()
    assert first["profile"] == "chat"
    assert first["provider"] == "mock"  # LLM_PROVIDER override wins over the profile
    assert first["model"]
    assert first["resolved"] is True
    assert first["cached"] is False

    second = await endpoint()
    assert second["cached"] is True
    assert second["verify"] == first["verify"]

    refreshed = await endpoint("chat", 1)
    assert refreshed["cached"] is False


async def test_profiles_endpoint_lists_registry(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    spec = llm_main.build(settings, MemoryBus())
    endpoint = next(r.endpoint for r in spec.router.routes if getattr(r, "path", "") == "/api/profiles")

    body = await endpoint()
    assert "chat" in body["profiles"]
    assert "model" in body["profiles"]["chat"]


async def test_build_returns_service_spec():
    from services.common import ServiceSpec

    spec = llm_main.build(Settings(), MemoryBus())
    assert isinstance(spec, ServiceSpec)
    assert spec.name == "llm_service"
    assert len(spec.loops) == 1
    assert isinstance(spec.loops[0], ServiceLoop)
    assert "llm" in spec.meta


async def test_evidence_block_is_prompt_shaped():
    """Sanity check on the block the service streams over (pmid= lines)."""
    rows = fixture_rows()[:3]
    block = format_evidence(rows)
    assert "pmid=" in block
    assert block.splitlines()[0].startswith("1. pmid=")
    assert rows[0]["pmid"] in block
