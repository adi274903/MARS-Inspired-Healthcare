"""MONOLITH=1: every service in one process over the in-memory bus.

Identical service code, identical events — this is the debuggable topology used
by tests and by local development, not a parallel implementation.
"""
from __future__ import annotations

import asyncio
import importlib
from typing import Any

from core.bus import build_bus
from core.config import get_settings
from core.telemetry import log
from services.common import ServiceSpec

SERVICES: tuple[str, ...] = (
    "services.orchestrator.main",
    "services.sqlite_service.main",
    "services.vector_service.main",
    "services.graph_service.main",
    "services.llm_service.main",
    "services.answer_service.main",
    "services.event_log.main",
    "services.control_plane.main",
    "services.gateway.main",
)


def build_system(settings=None, bus=None, modules: tuple[str, ...] = SERVICES) -> tuple[Any, list[ServiceSpec]]:
    settings = settings or get_settings()
    bus = bus or build_bus(settings.redis_url, kind="memory")
    specs: list[ServiceSpec] = []
    for module in modules:
        try:
            mod = importlib.import_module(module)
        except ModuleNotFoundError:
            continue
        specs.append(mod.build(settings, bus))
    return bus, specs


def build_app(settings=None, bus=None):
    """One FastAPI app with every service's router mounted (used by tests)."""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    settings = settings or get_settings()
    bus, specs = build_system(settings, bus)
    app = FastAPI(title="evalbot-monolith", version="1.0")
    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["*"], allow_headers=["*"]
    )
    for spec in specs:
        if spec.router is not None:
            app.include_router(spec.router)
    # The UI catch-all must be mounted after every router so /api/* and /healthz win.
    try:
        from services.gateway.main import mount_ui

        mount_ui(app, settings)
    except Exception:  # no index.html, or starlette refuses: the API still works
        pass
    app.state.bus = bus
    app.state.specs = specs
    app.state.settings = settings

    @app.on_event("startup")
    async def _startup() -> None:
        await bus.start()
        for spec in specs:
            for hook in spec.on_start:
                await hook()
            for loop in spec.loops:
                await loop.start()
        log("monolith", "system.start", services=[s.name for s in specs])

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        for spec in specs:
            for loop in spec.loops:
                await loop.stop()
            for hook in spec.on_stop:
                await hook()
        await bus.stop()

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"service": "monolith", "ok": True, "services": [s.name for s in specs]}

    return app


def main() -> None:
    import uvicorn

    settings = get_settings()
    app = build_app(settings)
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level=settings.log_level)


if __name__ == "__main__":
    main()
