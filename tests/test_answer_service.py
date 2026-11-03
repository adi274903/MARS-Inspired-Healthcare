"""Answer service end-to-end over the in-memory bus: evidence -> answer.completed."""
from __future__ import annotations

import asyncio
import json

import pytest

from core.bus import MemoryBus, collect
from core.config import get_settings
from core.envelope import new_event
from services.answer_service import main as answer_main
from services.answer_service.main import build
from tests.support import fixture_rows

CID = "corr-answer-1"


def _raw_with_bad_marker() -> str:
    return json.dumps(
        {
            "text": (
                "#### Findings\n\n"
                "Both systems report internal validation only [1] and [2].\n\n"
                "| system | tier |\n|---|---|\n| CAD | internal |\n\n"
                "An invented source [7] must not survive.\n\n"
                "See [the study](https://example.org/paper) for detail."
            ),
            "checklist": {
                "title": 3,
                "groups": [
                    {"name": "Verify", "items": [str(i) for i in range(11)]},
                    {"name": "", "items": ["single item"]},
                ],
            },
            "claims": {"1": "source 1 supports internal validation", "2": {"bad": "dropped"}},
        }
    )

async def _start(spec) -> None:
    """Start the loops and let each one actually subscribe before publishing."""
    for loop in spec.loops:
        await loop.start()
    await asyncio.sleep(0.02)


async def _pump(settings, bus: MemoryBus, events: list) -> dict:
    spec = build(settings, bus)
    await _start(spec)
    try:
        for event in events:
            await bus.publish(event)
        found = await collect(bus, count=1, types=("answer.completed",))
        return found[0].payload
    finally:
        for loop in spec.loops:
            await loop.stop()


@pytest.mark.asyncio
async def test_answer_completed_composes_text_citations_and_checklist(monkeypatch):
    monkeypatch.setenv("MONOLITH", "1")
    monkeypatch.setenv("BUS", "memory")
    settings = get_settings()
    bus = MemoryBus()
    rows = fixture_rows()[:2]

    events = [
        new_event(
            "retrieval.completed",
            {
                "evidence": rows,
                "stats": {"total": 2},
                "route": "evidence",
                "degraded": False,
                "question": "What do the papers report?",
            },
            producer="orchestrator",
            correlation_id=CID,
        ),
        new_event("llm.token", {"text": '{"text": "partial', "seq": 0}, correlation_id=CID),
        new_event("llm.token", {"text": " stream", "seq": 1}, correlation_id=CID),
        new_event(
            "llm.completed",
            {"raw": _raw_with_bad_marker(), "usage": {"total_tokens": 42}, "model": "test-model", "profile": "chat"},
            correlation_id=CID,
        ),
    ]
    payload = await _pump(settings, bus, events)

    text = payload["text"]
    assert "[7]" not in text
    assert "[1]" in text and "[2]" in text
    assert "|" not in text  # table flattened
    assert "####" not in text  # heading demoted
    assert "the study (https://example.org/paper)" in text

    assert payload["markers"] == [(1, 1), (2, 2)]
    assert [c["pmid"] for c in payload["citations"]] == [rows[0]["pmid"], rows[1]["pmid"]]
    assert [c["type"] for c in payload["citations"]] == ["pubmed", "pubmed"]
    assert payload["citations"][0]["claim"] == "source 1 supports internal validation"
    assert payload["citations"][1]["claim"]  # deterministic fallback from the row
    assert payload["dropped_citations"] == []

    checklist = payload["checklist"]
    assert checklist["title"] == "3"
    assert len(checklist["groups"]) == 2
    assert checklist["groups"][0]["items"] == [str(i) for i in range(8)]
    assert checklist["groups"][1]["name"] == "Checklist"

    assert payload["usage"] == {"total_tokens": 42}
    assert payload["model"] == "test-model"
    assert payload["profile"] == "chat"
    assert payload["route"] == "evidence"
    assert payload["degraded"] is False


@pytest.mark.asyncio
async def test_answer_completed_without_retrieval_has_no_citations(monkeypatch):
    monkeypatch.setenv("MONOLITH", "1")
    monkeypatch.setenv("BUS", "memory")
    settings = get_settings()
    bus = MemoryBus()

    events = [
        new_event("llm.token", {"text": "tokens without evidence"}, correlation_id="corr-2"),
        new_event(
            "llm.completed",
            {"raw": json.dumps({"text": "Answer citing [1] anyway."})},
            correlation_id="corr-2",
        ),
    ]
    payload = await _pump(settings, bus, events)
    assert payload["citations"] == []
    assert payload["markers"] == []
    assert "Answer citing" in payload["text"]
    assert payload["route"] is None
    assert payload["degraded"] is False


@pytest.mark.asyncio
async def test_llm_failed_publishes_answer_failed(monkeypatch):
    monkeypatch.setenv("MONOLITH", "1")
    monkeypatch.setenv("BUS", "memory")
    settings = get_settings()
    bus = MemoryBus()
    spec = build(settings, bus)
    await _start(spec)
    try:
        await bus.publish(
            new_event("llm.failed", {"message": "provider timeout", "partial": "half"}, correlation_id="corr-3")
        )
        found = await collect(bus, count=1, types=("answer.failed",))
        payload = found[0].payload
        assert payload["service"] == "answer_service"
        assert payload["message"] == "provider timeout"
        assert payload["partial"] == "half"
    finally:
        for loop in spec.loops:
            await loop.stop()


@pytest.mark.asyncio
async def test_compose_endpoint_is_synchronous_and_matches_service(monkeypatch, tmp_path):
    monkeypatch.setenv("MONOLITH", "1")
    monkeypatch.setenv("BUS", "memory")
    monkeypatch.setenv("MODELS_YAML", str(tmp_path / "models.yaml"))
    settings = get_settings()
    spec = build(settings, MemoryBus())
    endpoint = next(route.endpoint for route in spec.router.routes if getattr(route, "path", "") == "/api/compose")

    rows = fixture_rows()[:2]
    from services.answer_service.main import ComposeRequest

    result = await endpoint(ComposeRequest(raw=_raw_with_bad_marker(), evidence=rows))
    assert result["markers"] == [(1, 1), (2, 2)]
    assert [c["pmid"] for c in result["citations"]] == [rows[0]["pmid"], rows[1]["pmid"]]
    assert "[7]" not in result["text"]


def test_build_owns_its_port_and_name(monkeypatch):
    monkeypatch.setenv("MONOLITH", "1")
    monkeypatch.setenv("BUS", "memory")
    settings = get_settings()
    spec = build(settings, MemoryBus())
    assert spec.name == "answer_service"
    assert spec.port == settings.service_ports["answer_service"]
    assert spec.router is not None
    assert spec.loops and set(answer_main.CONSUMES) == {
        "llm.token",
        "llm.completed",
        "llm.failed",
        "retrieval.completed",
    }
