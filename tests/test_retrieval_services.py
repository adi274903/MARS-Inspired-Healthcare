"""Event + HTTP tests for the three retrieval services (offline, fixture DB only)."""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient

from core.bus import MemoryBus, collect
from core.config import get_settings
from core.envelope import Event, new_event
from retrieval.queryparse import parse
from services.common import ServiceSpec, make_app
from services.graph_service import main as graph_service
from services.sqlite_service import main as sqlite_service
from services.vector_service import main as vector_service
from tests.support import build_fixture_db

SEPSIS_Q = "external validation of sepsis prediction models"
AGGREGATE_Q = "how many papers report external validation"
# The brief's aggregate question ("how many oncology papers report external
# validation"): the fixture's oncology rows report no external validation, so the
# exact count is 0 -- kept as a separate case below.
AGGREGATE_ONCOLOGY_Q = "how many oncology papers report external validation"


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    db = build_fixture_db(tmp_path)
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("MONOLITH", "1")
    monkeypatch.setenv("BUS", "memory")
    monkeypatch.setenv("EMBED_PROVIDER", "hash")
    monkeypatch.setenv("QDRANT_PATH", str(tmp_path / "qdrant"))
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events" / "events.db"))
    monkeypatch.delenv("NEO4J_URI", raising=False)
    cfg = get_settings()
    assert cfg.monolith and cfg.bus_kind == "memory"
    return cfg


async def wait_subscribed(bus: MemoryBus, count: int = 1, timeout: float = 2.0) -> None:
    """MemoryBus only fans out to subscriptions that already exist: wait for them."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if sum(len(subs) for subs in getattr(bus, "_subs", {}).values()) >= count:
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("service loops never subscribed")


@contextlib.asynccontextmanager
async def running(spec: ServiceSpec, bus: MemoryBus) -> AsyncIterator[ServiceSpec]:
    for hook in spec.on_start:
        await hook()
    for loop in spec.loops:
        await loop.start()
    await wait_subscribed(bus, len(spec.loops))
    try:
        yield spec
    finally:
        for loop in spec.loops:
            await loop.stop()
        for hook in spec.on_stop:
            await hook()


async def request_leg(bus: MemoryBus, leg: str, question: str, **extra: Any) -> Event:
    payload: dict[str, Any] = {
        "leg": leg,
        "question": question,
        "parsed": parse(question).as_dict(),
        "k": 8,
        "expect": 2,
        **extra,
    }
    event = new_event("retrieval.requested", payload, producer="test", timeout_s=30)
    await bus.publish(event)
    return event


async def first(bus: MemoryBus, type_: str) -> Event:
    events = await collect(bus, count=1, types=(type_,), timeout=5.0)
    return events[0]


def client_for(settings, module) -> TestClient:
    spec = module.build(settings, MemoryBus())
    return TestClient(make_app(module.NAME, settings, spec.router))


# ------------------------------------------------------------------ sqlite leg
@pytest.mark.asyncio
async def test_sqlite_leg_returns_fixture_pmid(settings):
    bus = MemoryBus()
    spec = sqlite_service.build(settings, bus)
    async with running(spec, bus):
        requested = await request_leg(bus, "sqlite", SEPSIS_Q)
        out = await first(bus, "sqlite.result")
    payload = out.payload
    assert out.correlation_id == requested.correlation_id
    assert payload["leg"] == "sqlite"
    assert "19902300" in payload["pmids"]
    assert payload["stats"] is None
    assert isinstance(payload["filters_applied"], bool)
    assert "degraded" not in payload


@pytest.mark.asyncio
async def test_sqlite_leg_ignores_foreign_leg(settings):
    bus = MemoryBus()
    spec = sqlite_service.build(settings, bus)
    async with running(spec, bus):
        await request_leg(bus, "vector", SEPSIS_Q)
        await asyncio.sleep(0.25)
        history = await bus.history()
    assert not [e for e in history if e.type == "sqlite.result"]


@pytest.mark.asyncio
async def test_sqlite_leg_aggregate_returns_stats(settings):
    bus = MemoryBus()
    spec = sqlite_service.build(settings, bus)
    async with running(spec, bus):
        await request_leg(bus, "sqlite", AGGREGATE_Q)
        out = await first(bus, "sqlite.result")
    payload = out.payload
    assert payload["pmids"] == []
    assert payload["stats"] is not None
    assert payload["stats"]["total"] > 0


@pytest.mark.asyncio
async def test_sqlite_leg_aggregate_oncology_filters_out_everything(settings):
    """The fixture's oncology rows report no external validation (total == 0)."""
    bus = MemoryBus()
    spec = sqlite_service.build(settings, bus)
    async with running(spec, bus):
        await request_leg(bus, "sqlite", AGGREGATE_ONCOLOGY_Q)
        out = await first(bus, "sqlite.result")
    payload = out.payload
    assert payload["pmids"] == []
    assert payload["stats"] is not None
    assert payload["stats"]["total"] == 0


@pytest.mark.asyncio
async def test_sqlite_leg_degrades_when_db_missing(settings, tmp_path):
    settings.db_path = tmp_path / "missing" / "papers.db"
    bus = MemoryBus()
    spec = sqlite_service.build(settings, bus)
    async with running(spec, bus):
        await request_leg(bus, "sqlite", SEPSIS_Q)
        out = await first(bus, "sqlite.result")
    payload = out.payload
    assert payload["pmids"] == []
    assert payload["filters_applied"] is False
    assert payload["stats"] is None
    assert payload["degraded"] is True


# ------------------------------------------------------------------ vector leg
@pytest.mark.asyncio
async def test_vector_leg_degrades_without_collection(settings):
    bus = MemoryBus()
    spec = vector_service.build(settings, bus)
    async with running(spec, bus):
        requested = await request_leg(bus, "vector", SEPSIS_Q)
        out = await first(bus, "vector.result")
    payload = out.payload
    assert out.correlation_id == requested.correlation_id
    assert payload["leg"] == "vector"
    assert payload["pmids"] == []
    assert payload["degraded"] is True


@pytest.mark.asyncio
async def test_vector_leg_degrades_when_disabled(settings):
    settings.vector_enabled = False
    bus = MemoryBus()
    spec = vector_service.build(settings, bus)
    async with running(spec, bus):
        await request_leg(bus, "vector", SEPSIS_Q)
        out = await first(bus, "vector.result")
    assert out.payload["pmids"] == []
    assert out.payload["degraded"] is True


# ------------------------------------------------------------------- graph leg
@pytest.mark.asyncio
async def test_graph_leg_papers_by_author(settings):
    bus = MemoryBus()
    spec = graph_service.build(settings, bus)
    async with running(spec, bus):
        requested = await request_leg(
            bus,
            "graph",
            "who has published on CAD ultrasound",
            template="papers_by_author",
            params={"author": "Wang"},
        )
        out = await first(bus, "graph.result")
    payload = out.payload
    assert out.correlation_id == requested.correlation_id
    assert payload["leg"] == "graph"
    assert payload["template"] == "papers_by_author"
    assert "19902300" in payload["pmids"]
    assert "degraded" not in payload


@pytest.mark.asyncio
async def test_graph_leg_journal_rollup_returns_rows(settings):
    bus = MemoryBus()
    spec = graph_service.build(settings, bus)
    async with running(spec, bus):
        await request_leg(bus, "graph", "which journals publish these papers", template="journal_rollup", params={})
        out = await first(bus, "graph.result")
    payload = out.payload
    assert payload["template"] == "journal_rollup"
    assert payload["rows"]
    assert {"key", "n"} <= set(payload["rows"][0])


@pytest.mark.asyncio
async def test_graph_leg_degrades_when_disabled(settings):
    settings.graph_enabled = False
    bus = MemoryBus()
    spec = graph_service.build(settings, bus)
    async with running(spec, bus):
        await request_leg(bus, "graph", "who has published with Wang", template="papers_by_author")
        out = await first(bus, "graph.result")
    assert out.payload["pmids"] == []
    assert out.payload["degraded"] is True


# ---------------------------------------------------------------- HTTP routers
def test_sqlite_http_papers_and_filters(settings):
    with client_for(settings, sqlite_service) as client:
        resp = client.get("/api/papers", params={"q": SEPSIS_Q, "limit": 5})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] > 0
        assert len(body["papers"]) == min(5, body["total"])
        assert {"pmid", "title"} <= set(body["papers"][0])

        filtered = client.get("/api/papers", params={"domain": "Oncology", "limit": 10}).json()
        assert filtered["total"] > 0
        assert "19902300" in {p["pmid"] for p in filtered["papers"]}

        paged = client.get("/api/papers", params={"limit": 2, "offset": 2}).json()
        assert len(paged["papers"]) <= 2


def test_sqlite_http_stats_facets_health(settings):
    with client_for(settings, sqlite_service) as client:
        stats = client.get("/api/stats").json()
        assert stats["total"] == 12
        assert "facets" in stats and "flags" in stats and "year_range" in stats

        facets = client.get("/api/facets").json()
        assert facets["total"] == 12
        assert facets["facets"]["a_domain"]
        assert isinstance(facets["flags"], dict)

        health = client.get("/api/health-store").json()
        assert health == {"available": True, "rows": 12}


def test_vector_http_health_store(settings):
    with client_for(settings, vector_service) as client:
        assert client.get("/healthz").json()["ok"] is True
        health = client.get("/api/health-store").json()
        assert health["available"] is False
        assert health["points"] == 0
        assert health["provider"] == "hash"


def test_graph_http_templates_and_queries(settings):
    with client_for(settings, graph_service) as client:
        templates = client.get("/api/graph/templates").json()["templates"]
        assert "papers_by_author" in templates
        assert "journal_rollup" in templates

        by_author = client.get("/api/graph/papers_by_author", params={"author": "Wang", "limit": 10}).json()
        assert "19902300" in by_author["pmids"]
        assert by_author["template"] == "papers_by_author"

        rollup = client.get("/api/graph/journal_rollup", params={"limit": 5}).json()
        assert rollup["rows"]
        assert all({"key", "n"} <= set(row) for row in rollup["rows"])

        assert client.get("/api/graph/not_a_template").status_code == 404
