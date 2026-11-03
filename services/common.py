"""Shared service scaffolding: the one shape every service returns."""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.bus import Bus, ServiceLoop, build_bus
from core.dedupe import MemoryDedupe, SqliteDedupe
from core.telemetry import log


@dataclass
class ServiceSpec:
    """What a service contributes: an optional HTTP router, bus loops and lifecycle hooks."""

    name: str
    router: APIRouter | None = None
    port: int = 0
    loops: list[ServiceLoop] = field(default_factory=list)
    on_start: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    on_stop: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def dedupe_for(settings, name: str) -> Any:
    """Durable idempotency in the split topology, cheap LRU in a single process."""
    if getattr(settings, "bus_kind", "memory") == "redis" and not getattr(settings, "monolith", False):
        return SqliteDedupe(settings.events_db.with_name(f"dedupe-{name}.db"))
    return MemoryDedupe()


def make_app(name: str, settings, router: APIRouter | None = None) -> FastAPI:
    """The HTTP shell of a service: CORS, /healthz and the service's own router."""
    app = FastAPI(title=f"evalbot-{name}", version="1.0", docs_url="/docs")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if router is not None:
        app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"service": name, "ok": True}

    return app


async def run_standalone(module: str, settings) -> None:
    """Run one service as its own process (uvicorn + its own bus)."""
    import uvicorn

    bus = build_bus(settings.redis_url, kind=settings.bus_kind)
    await bus.start()
    spec: ServiceSpec = importlib.import_module(module).build(settings, bus)
    app = make_app(spec.name, settings, spec.router)
    for hook in spec.on_start:
        await hook()
    for loop in spec.loops:
        await loop.start()
    log(spec.name, "service.start", port=spec.port)
    try:
        server = uvicorn.Server(
            uvicorn.Config(app, host="0.0.0.0", port=spec.port or settings.service_ports.get(spec.name, 8099), log_level=settings.log_level)
        )
        await server.serve()
    finally:
        for loop in spec.loops:
            await loop.stop()
        for hook in spec.on_stop:
            await hook()
        await bus.stop()


def run(module: str) -> None:
    """CLI entry: ``python -m services.<name>.main``."""
    from core.config import get_settings

    asyncio.run(run_standalone(module, get_settings()))
