"""Vector retrieval service.

Consumes ``retrieval.requested`` with ``leg == "vector"`` and answers with
``vector.result``. A missing collection or embedder is a degraded leg, never an
error: the orchestrator simply fuses fewer lists.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from core.bus import Bus, ServiceLoop
from core.envelope import Event
from core.telemetry import log
from retrieval.vector_store import Embedder, VectorStore
from services.common import ServiceSpec, dedupe_for, run

NAME = "vector_service"
LEG = "vector"
IN_TYPES: tuple[str, ...] = ("retrieval.requested",)
MIN_CANDIDATES = 20


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _result(event: Event, pmids: list[str], *, degraded: bool) -> Event:
    payload: dict[str, Any] = {"leg": LEG, "pmids": pmids}
    if degraded:
        payload["degraded"] = True
    return event.child("vector.result", payload, NAME)


def _make_handler(settings, embedder: Embedder, store: VectorStore) -> Any:
    async def handle(event: Event) -> Event | None:
        payload = event.payload or {}
        if payload.get("leg") != LEG:
            return None
        limit = max(_int(payload.get("k"), settings.num_evidence) * 4, MIN_CANDIDATES)
        question = str(payload.get("question") or "")
        if not getattr(settings, "vector_enabled", True) or not embedder.available():
            log(NAME, "vector.disabled", provider=embedder.provider)
            return _result(event, [], degraded=True)
        if not store.available():
            log(NAME, "collection.missing", collection=store.collection)
            return _result(event, [], degraded=True)
        index_dim = store.dimension()
        if index_dim and index_dim != embedder.dim:
            log(
                NAME,
                "vector.dim_mismatch",
                index_dim=index_dim,
                model_dim=embedder.dim,
                hint="rebuild with: python -m ingest.qdrant_index --recreate",
            )
            return _result(event, [], degraded=True)
        try:
            vector = embedder.query(question)
            if not vector:
                return _result(event, [], degraded=True)
            return _result(event, store.search(vector, limit=limit), degraded=False)
        except Exception as exc:  # store/embedder hiccup: degrade the leg, keep the request alive
            log(NAME, "vector.degraded", error=f"{type(exc).__name__}: {exc}")
            return _result(event, [], degraded=True)

    return handle


def _make_router(embedder: Embedder, store: VectorStore) -> APIRouter:
    api = APIRouter(prefix="/api", tags=[NAME])

    @api.get("/health-store")
    def health_store() -> dict[str, Any]:
        available = store.available()
        return {
            "available": available,
            "points": store.count() if available else 0,
            "provider": embedder.provider,
        }

    return api


def build(settings, bus: Bus) -> ServiceSpec:
    embedder = Embedder(
        settings.embed_provider,
        settings.embed_model,
        settings.embed_dim,
        device=getattr(settings, "embed_device", "cpu"),
        batch=int(getattr(settings, "embed_batch", 32)),
    )
    store = VectorStore(
        settings.qdrant_path,
        settings.qdrant_collection,
        dim=embedder.dim or settings.embed_dim,
        url=None,
    )
    loop = ServiceLoop(
        NAME,
        bus,
        _make_handler(settings, embedder, store),
        types=IN_TYPES,
        dedupe=dedupe_for(settings, NAME),
    )

    async def on_start() -> None:
        log(NAME, "store.ready", collection=store.collection, provider=embedder.provider, available=store.available())

    async def on_stop() -> None:
        store.close()

    return ServiceSpec(
        name=NAME,
        router=_make_router(embedder, store),
        port=settings.service_ports[NAME],
        loops=[loop],
        on_start=[on_start],
        on_stop=[on_stop],
    )


if __name__ == "__main__":
    run(__name__)
