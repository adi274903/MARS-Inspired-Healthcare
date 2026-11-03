"""End-to-end: the real services, one bus, one fixture corpus, no network.

This is the test that proves the event contract actually composes: gateway ->
orchestrator -> retrieval legs -> llm -> answer -> gateway, including the SSE
transport and the guidance corpus.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.bus import MemoryBus, collect
from core.envelope import new_event
from core.config import get_settings
from tests.support import build_fixture_db, fixture_rows

SERVICES = (
    "services.orchestrator.main",
    "services.sqlite_service.main",
    "services.vector_service.main",
    "services.graph_service.main",
    "services.llm_service.main",
    "services.answer_service.main",
    "services.event_log.main",
)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> dict:
    db = build_fixture_db(tmp_path / "db")
    models = tmp_path / "models.yaml"
    monkeypatch.setenv("MONOLITH", "1")
    monkeypatch.setenv("BUS", "memory")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("GUIDED_JSON", "0")
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events" / "events.db"))
    monkeypatch.setenv("MODELS_YAML", str(models))
    monkeypatch.setenv("QDRANT_PATH", str(tmp_path / "qdrant"))
    monkeypatch.setenv("GUIDANCE_PATH", str(Path(__file__).resolve().parent.parent / "guidance" / "sources.yaml"))
    monkeypatch.setenv("CHAT_TIMEOUT_S", "20")
    monkeypatch.delenv("NEO4J_URI", raising=False)
    return {"db": db, "models": models}


async def _start(settings, modules=SERVICES):
    from services.monolith.app import build_system

    bus, specs = build_system(settings, MemoryBus(), modules)
    await bus.start()
    for spec in specs:
        for hook in spec.on_start:
            await hook()
        for loop in spec.loops:
            await loop.start()
    await asyncio.sleep(0)  # let the loop tasks enter their first await
    return bus, specs


async def _stop(bus, specs):
    for spec in specs:
        for loop in spec.loops:
            await loop.stop()
        for hook in spec.on_stop:
            await hook()
    await bus.stop()


async def _ask(bus, question: str, context: dict | None = None):
    event = new_event(
        "chat.requested",
        {"messages": [{"role": "user", "text": question}], "context": context or {}},
        producer="test",
        timeout_s=25,
    )
    await bus.publish(event)
    answers = await collect(bus, count=1, types=("answer.completed", "answer.failed"), timeout=15)
    return event, answers[0]


def _pmids() -> set[str]:
    return {row["pmid"] for row in fixture_rows()}


async def test_faceted_question_returns_grounded_citations(env):
    settings = get_settings()
    bus, specs = await _start(settings)
    try:
        event, answer = await _ask(bus, "external validation of sepsis prediction models")
        assert answer.type == "answer.completed", answer.payload
        payload = answer.payload
        assert payload["text"].strip(), "answer text must not be empty"
        assert payload["citations"], "a grounded answer must cite papers"
        # guidance citations are type "web" and legitimately carry their own PMID/DOI, so the
        # corpus-membership check only applies to paper citations.
        papers = [c for c in payload["citations"] if c.get("type") == "pubmed"]
        assert papers, "expected paper citations for a corpus question"
        assert {c["pmid"] for c in papers} <= _pmids()
        # markers in the text must be inside the citation range
        import re

        used = {int(n) for n in re.findall(r"\[(\d{1,3})\]", payload["text"])}
        assert used <= set(range(1, len(payload["citations"]) + 1))
        assert payload["route"] in {"hybrid", "faceted", "semantic", "multihop", "aggregate"}
    finally:
        await _stop(bus, specs)


async def test_aggregate_question_quotes_the_sql_count(env):
    settings = get_settings()
    bus, specs = await _start(settings)
    try:
        _, answer = await _ask(bus, "how many papers report external validation")
        assert answer.type == "answer.completed", answer.payload
        text = answer.payload["text"]
        # The fixture has exactly four papers with external_val=1.
        assert "4" in text and "papers" in text.lower()
        assert answer.payload["route"] == "aggregate"
        assert answer.payload["citations"] == []
    finally:
        await _stop(bus, specs)


async def test_guidance_question_cites_a_framework(env):
    settings = get_settings()
    bus, specs = await _start(settings)
    try:
        _, answer = await _ask(bus, "which reporting guideline applies to a prediction model")
        payload = answer.payload
        assert answer.type == "answer.completed"
        web = [c for c in payload["citations"] if c.get("type") == "web"]
        assert web, f"expected a guidance citation, got {payload['citations']}"
        assert any("TRIPOD" in (c.get("title") or "") for c in web)
        # guidance must never be smuggled in as paper evidence
        assert all(c.get("type") != "pubmed" or c.get("pmid") in _pmids() for c in payload["citations"])
    finally:
        await _stop(bus, specs)


async def test_event_log_records_the_timeline(env):
    settings = get_settings()
    bus, specs = await _start(settings)
    try:
        event, _answer = await _ask(bus, "sepsis prediction models")
        history = await bus.history()
        types = [e.type for e in history if e.correlation_id == event.correlation_id]
        assert types[0] == "chat.requested"
        for expected in ("retrieval.requested", "retrieval.completed", "generation.requested", "llm.token", "answer.completed"):
            assert expected in types, f"{expected} missing from {types}"
        assert types.index("retrieval.completed") < types.index("generation.requested")
        assert types.index("llm.token") < types.index("answer.completed")
    finally:
        await _stop(bus, specs)


# ---------------------------------------------------------------- HTTP surface
def test_http_chat_and_stream_end_to_end(env):
    from services.monolith.app import build_app

    settings = get_settings()
    app = build_app(settings)
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200

        body = {
            "messages": [{"role": "user", "text": "external validation of sepsis prediction models"}],
            "context": {"system": "Risk classifier", "stage": "Pilot", "region": "US", "focus": ["Clinical validity"]},
        }
        reply = client.post("/api/chat", json=body)
        assert reply.status_code == 200, reply.text
        data = reply.json()
        assert data["text"].strip()
        assert isinstance(data["citations"], list) and data["citations"]
        assert set(data) == {"text", "checklist", "citations"}

        stream = client.post("/api/chat/stream", json=body)
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        raw = stream.text
        frames = [line.split(":", 1)[1].strip() for line in raw.splitlines() if line.startswith("event:")]
        assert frames[0] == "meta", frames
        assert "delta" in frames
        assert frames[-1] == "done", frames

        # the audit trail is queryable afterwards
        events = client.get("/api/events", params={"type": "answer.completed", "limit": 5})
        assert events.status_code == 200
        assert isinstance(events.json(), list) and events.json()
