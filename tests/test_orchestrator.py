"""Event-driven tests for the orchestrator.

Everything runs on a real ``MemoryBus`` with a real (fixture-built) SQLite DB and
the mock LLM provider: no network, no GPU, no dataset access.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from core.bus import MemoryBus, ServiceLoop, collect
from core.config import Settings, get_settings
from core.envelope import Event, new_event
from retrieval import queryparse, rrf
from services.llm_service import main as llm_main
from services.orchestrator import main as orchestrator
from tests.support import build_fixture_db, fixture_rows

pytestmark = pytest.mark.asyncio

FACETED_Q = "What deep learning transformer approaches are used for diagnosis with external validation?"
AGGREGATE_Q = "How many papers were published by year?"
MULTIHOP_Q = "Which journals publish papers from the same author team?"

LEG_WEIGHTS = {"sqlite": 1.0, "vector": 1.0, "graph": 0.8}


# ---------------------------------------------------------------- fixtures
def make_settings(tmp_path, monkeypatch) -> Settings:
    db = build_fixture_db(tmp_path)
    monkeypatch.setenv("MONOLITH", "1")
    monkeypatch.setenv("BUS", "memory")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("MODELS_YAML", str(tmp_path / "models.yaml"))
    settings = get_settings()
    assert settings.db_path == db
    return settings


async def wait_subscribed(bus: MemoryBus, count: int, *, timeout: float = 2.0) -> None:
    """MemoryBus has no ready signal: wait until every loop actually subscribed."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if sum(len(subs) for subs in bus._subs.values()) >= count:
            return
        await asyncio.sleep(0.005)
    raise TimeoutError("service loops did not subscribe")


@contextlib.asynccontextmanager
async def running(settings, *builders):
    """Start a MemoryBus plus one loop per built spec, tear everything down."""
    bus = MemoryBus()
    await bus.start()
    specs = [builder(settings, bus) for builder in builders]
    total = sum(len(spec.loops) for spec in specs)
    for spec in specs:
        for loop in spec.loops:
            await loop.start()
    await wait_subscribed(bus, total)
    try:
        yield bus, specs
    finally:
        for spec in specs:
            for loop in spec.loops:
                await loop.stop()
            for hook in spec.on_stop:
                await hook()
        await bus.stop()


def pmids_of(rows: list[dict]) -> list[str]:
    return [row["pmid"] for row in rows]


async def ask(bus: MemoryBus, question: str, *, history: tuple[str, ...] = (), profile: str | None = None) -> Event:
    messages = [{"role": "user", "text": text} for text in history]
    messages.append({"role": "user", "text": question})
    payload: dict = {
        "messages": messages,
        "context": {"system": "triage", "stage": "pre-deployment", "region": "eu", "focus": ["validation"]},
    }
    if profile:
        payload["profile"] = profile
    event = new_event("chat.requested", payload, producer="gateway", timeout_s=30.0)
    await bus.publish(event)
    return event


def results_by_type(history: list[Event], type_: str) -> list[Event]:
    return [e for e in history if e.type == type_]


# ---------------------------------------------------------------- tests
async def test_fanout_fuses_and_requests_generation(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    rows = fixture_rows()
    fixture_pmids = pmids_of(rows)
    stats = {
        "total": len(rows),
        "filters": {"f_algorithm": ["deep learning"]},
        "flags": {"external_only": True},
        "year_min": None,
        "year_max": None,
        "group_by": None,
        "rows": [],
    }

    async with running(settings, orchestrator.build) as (bus, _specs):
        chat = await ask(bus, FACETED_Q, history=("what about oncology triage?",))

        requests = await collect(bus, count=3, types=("retrieval.requested",))
        await asyncio.sleep(0.05)  # no fourth leg should appear
        requests = results_by_type(await bus.history(), "retrieval.requested")
        legs = [r.payload["leg"] for r in requests]
        assert sorted(legs) == ["graph", "sqlite", "vector"]
        for request in requests:
            assert request.correlation_id == chat.correlation_id
            assert request.payload["expect"] == 3
            assert request.payload["k"] == settings.num_evidence
            assert request.payload["question"] == FACETED_Q
            assert request.payload["parsed"]["intent"] == "faceted"

        by_leg = {r.payload["leg"]: r for r in requests}
        sqlite_pmids = fixture_pmids[0:4]
        vector_pmids = fixture_pmids[2:6]
        graph_pmids = [fixture_pmids[7], fixture_pmids[0], fixture_pmids[10]]

        await bus.publish(
            by_leg["sqlite"].child(
                "sqlite.result",
                {"leg": "sqlite", "pmids": sqlite_pmids, "filters_applied": True, "stats": stats},
                "sqlite_service",
            )
        )
        await bus.publish(
            by_leg["vector"].child("vector.result", {"leg": "vector", "pmids": vector_pmids}, "vector_service")
        )
        await bus.publish(
            by_leg["graph"].child(
                "graph.result",
                {"leg": "graph", "pmids": graph_pmids, "rows": [], "template": "similar_by_entities"},
                "graph_service",
            )
        )

        completed = (await collect(bus, count=1, types=("retrieval.completed",)))[0]
        assert completed.correlation_id == chat.correlation_id
        payload = completed.payload
        assert payload["route"] == "faceted"
        assert payload["question"] == FACETED_Q
        assert payload["stats"] == stats
        assert payload["degraded"] == []
        assert payload["context"]["region"] == "eu"
        assert payload["history"] == [{"role": "user", "text": "what about oncology triage?"}]

        expected = [
            pmid
            for pmid, _score in rrf.fuse(
                {"sqlite": sqlite_pmids, "vector": vector_pmids, "graph": graph_pmids},
                k=settings.rrf_k,
                weights=LEG_WEIGHTS,
                limit=settings.num_evidence,
            )
        ]
        assert pmids_of(payload["evidence"]) == expected
        assert all("title" in row for row in payload["evidence"])

        generation = (await collect(bus, count=1, types=("generation.requested",)))[0]
        gen = generation.payload
        assert generation.correlation_id == chat.correlation_id
        assert gen["profile"] == "chat"
        assert gen["route"] == "faceted"
        assert gen["parsed"]["intent"] == "faceted"
        assert gen["stats"] == stats
        assert pmids_of(gen["evidence"]) == expected
        assert gen["messages"][0]["role"] == "system"
        prompt = gen["messages"][-1]["content"]
        assert "EVIDENCE:" in prompt
        assert prompt.count("pmid=") == len(expected)
        assert f"QUESTION: {FACETED_Q}" in prompt
        assert "CORPUS STATS:" in prompt
        # the mock provider can only cite evidence if the block is well formed
        assert f"1. pmid={expected[0]}" in prompt

        failed = results_by_type(await bus.history(), "answer.failed")
        assert failed == []


async def test_aggregate_keeps_stats_when_evidence_is_empty(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)

    async with running(settings, orchestrator.build) as (bus, _specs):
        await ask(bus, AGGREGATE_Q)
        requests = await collect(bus, count=1, types=("retrieval.requested",))
        await asyncio.sleep(0.05)
        requests = results_by_type(await bus.history(), "retrieval.requested")
        assert [r.payload["leg"] for r in requests] == ["sqlite"]
        assert requests[0].payload["expect"] == 1

        stats = {"total": 12, "filters": {}, "flags": {}, "year_min": None, "year_max": None, "group_by": "year", "rows": [{"key": "2019", "n": 4}]}
        await bus.publish(
            requests[0].child(
                "sqlite.result",
                {"leg": "sqlite", "pmids": [], "filters_applied": False, "stats": stats},
                "sqlite_service",
            )
        )
        completed = (await collect(bus, count=1, types=("retrieval.completed",)))[0]
        assert completed.payload["evidence"] == []
        assert completed.payload["stats"] == stats
        assert completed.payload["degraded"] == []
        assert completed.payload["route"] == "aggregate"

        generation = (await collect(bus, count=1, types=("generation.requested",)))[0]
        assert "CORPUS STATS: total=12" in generation.payload["messages"][-1]["content"]
        assert "EVIDENCE: (none retrieved)" in generation.payload["messages"][-1]["content"]


async def test_missing_leg_is_degraded_not_fatal(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    fixture_pmids = pmids_of(fixture_rows())

    async with running(settings, orchestrator.build) as (bus, _specs):
        await ask(bus, MULTIHOP_Q)
        requests = await collect(bus, count=2, types=("retrieval.requested",))
        by_leg = {r.payload["leg"]: r for r in requests}
        assert sorted(by_leg) == ["graph", "sqlite"]

        # only sqlite answers; graph never does
        await bus.publish(
            by_leg["sqlite"].child(
                "sqlite.result",
                {"leg": "sqlite", "pmids": fixture_pmids[:3], "filters_applied": True, "stats": {"total": 3}},
                "sqlite_service",
            )
        )
        completed = (await collect(bus, count=1, types=("retrieval.completed",), timeout=5.0))[0]
        assert completed.payload["degraded"] == ["graph"]
        assert pmids_of(completed.payload["evidence"]) == fixture_pmids[:3]
        assert completed.payload["stats"] == {"total": 3}
        assert completed.payload["route"] == "multihop"

        generation = (await collect(bus, count=1, types=("generation.requested",)))[0]
        assert "pmid=" in generation.payload["messages"][-1]["content"]


async def test_late_result_cannot_double_complete(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    fixture_pmids = pmids_of(fixture_rows())

    async with running(settings, orchestrator.build) as (bus, _specs):
        await ask(bus, MULTIHOP_Q)
        requests = await collect(bus, count=2, types=("retrieval.requested",))
        by_leg = {r.payload["leg"]: r for r in requests}
        await bus.publish(
            by_leg["sqlite"].child(
                "sqlite.result",
                {"leg": "sqlite", "pmids": fixture_pmids[:2], "filters_applied": True, "stats": {"total": 2}},
                "sqlite_service",
            )
        )
        await collect(bus, count=1, types=("retrieval.completed",), timeout=5.0)
        # a straggler from the graph leg must be ignored
        await bus.publish(
            by_leg["graph"].child("graph.result", {"leg": "graph", "pmids": fixture_pmids[5:7]}, "graph_service")
        )
        await asyncio.sleep(0.3)
        history = await bus.history()
        assert len(results_by_type(history, "retrieval.completed")) == 1
        assert len(results_by_type(history, "generation.requested")) == 1


async def test_end_to_end_streams_tokens_then_completes(tmp_path, monkeypatch):
    """Orchestrator + llm_service on one bus: chat in, streamed answer out."""
    settings = make_settings(tmp_path, monkeypatch)
    fixture_pmids = pmids_of(fixture_rows())

    async with running(settings, orchestrator.build, llm_main.build) as (bus, _specs):
        chat = await ask(bus, FACETED_Q)
        requests = await collect(bus, count=3, types=("retrieval.requested",))
        for request in requests:
            leg = request.payload["leg"]
            body = {"leg": leg, "pmids": fixture_pmids[:3]}
            if leg == "sqlite":
                body.update({"filters_applied": True, "stats": {"total": 12}})
            await bus.publish(request.child(f"{leg}.result", body, f"{leg}_service"))

        await collect(bus, count=1, types=("retrieval.completed",))
        generation = (await collect(bus, count=1, types=("generation.requested",)))[0]
        assert "pmid=" in generation.payload["messages"][-1]["content"]

        completed = (await collect(bus, count=1, types=("llm.completed",), timeout=10.0))[0]
        tokens = results_by_type(await bus.history(), "llm.token")
        assert tokens, "expected streamed token frames"
        assert all(t.correlation_id == chat.correlation_id for t in tokens)
        assert all(t.event_id != completed.event_id for t in tokens)


async def test_route_endpoint_reports_legs(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    bus = MemoryBus()
    spec = orchestrator.build(settings, bus)
    endpoint = next(route.endpoint for route in spec.router.routes if getattr(route, "path", "") == "/api/route")

    body = await endpoint(FACETED_Q)
    assert body["intent"] == "faceted"
    assert body["route"] == "faceted"
    assert sorted(body["legs"]) == ["graph", "sqlite", "vector"]
    assert body["expect"] == 3
    assert body["k"] == settings.num_evidence
    assert body["question"] == FACETED_Q

    body = await endpoint(AGGREGATE_Q)
    assert body["legs"] == ["sqlite"]
    assert body["expect"] == 1


async def test_decide_legs_drops_disabled_legs(monkeypatch):
    parsed = queryparse.parse(FACETED_Q)
    assert parsed.intent == "faceted"

    settings = Settings(sqlite_enabled=True, vector_enabled=False, graph_enabled=False)
    assert orchestrator.decide_legs(parsed, settings) == ["sqlite"]

    settings = Settings(sqlite_enabled=False, vector_enabled=True, graph_enabled=False)
    assert orchestrator.decide_legs(parsed, settings) == ["vector"]

    settings = Settings(sqlite_enabled=False, vector_enabled=False, graph_enabled=False)
    assert orchestrator.decide_legs(parsed, settings) == ["sqlite"]  # never zero legs

    settings = Settings(sqlite_enabled=True, vector_enabled=True, graph_enabled=True)
    assert set(orchestrator.decide_legs(parsed, settings)) == {"sqlite", "vector", "graph"}

    settings = Settings(sqlite_enabled=True, vector_enabled=True, graph_enabled=True)
    for question, intent, legs in (
        (AGGREGATE_Q, "aggregate", ["sqlite"]),
        (MULTIHOP_Q, "multihop", ["graph", "sqlite"]),
        ("How do frameworks should approach evaluation?", "semantic", ["vector", "sqlite"]),
    ):
        parsed = queryparse.parse(question)
        assert parsed.intent == intent, question
        assert orchestrator.decide_legs(parsed, settings) == legs


async def test_build_returns_service_spec():
    from services.common import ServiceSpec

    spec = orchestrator.build(Settings(), MemoryBus())
    assert isinstance(spec, ServiceSpec)
    assert spec.name == "orchestrator"
    assert len(spec.loops) == 1
    assert isinstance(spec.loops[0], ServiceLoop)
    assert spec.meta["orchestrator"].__class__.__name__ == "Orchestrator"
