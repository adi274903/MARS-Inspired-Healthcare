"""Gateway: the only public surface.

Two transports, one event flow. The service owns a dispatcher ``ServiceLoop``
that subscribes to *every* event on the bus and fans it out to per-correlation
``asyncio.Queue`` sinks. REST and SSE handlers register a sink, publish
``chat.requested`` and then read that same queue, so both transports are thin
views over exactly the same events the rest of the system produces.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from core.bus import Bus, ServiceLoop
from core.envelope import Event, new_event
from core.registry import get_profile
from core.telemetry import log
from services.common import ServiceSpec

TERMINAL = frozenset({"answer.completed", "answer.failed"})
KEEPALIVE_S = 15.0
#: grace period past the event deadline before we give up locally
SLACK_S = 0.5
CANCEL_REASON_DISCONNECT = "client_disconnected"
CANCEL_REASON_TIMEOUT = "timeout"

_PENDING: set[asyncio.Task[Any]] = set()


# ------------------------------------------------------------------ payloads
class Message(BaseModel):
    role: str = "user"
    text: str = ""


class ChatContext(BaseModel):
    system: str | None = None
    stage: str | None = None
    region: str | None = None
    # The UI sends an array of focus chips; a bare string is accepted and coerced.
    focus: list[str] = Field(default_factory=list)
    profile: str | None = None

    @field_validator("focus", mode="before")
    @classmethod
    def _coerce_focus(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)
    context: ChatContext = Field(default_factory=ChatContext)


def _request_payload(body: ChatRequest, profile: str | None) -> dict[str, Any]:
    context = body.context.model_dump(exclude_none=True)
    context.pop("profile", None)
    payload: dict[str, Any] = {
        "messages": [{"role": m.role, "text": m.text} for m in body.messages],
        "context": context,
    }
    if profile:
        payload["profile"] = profile
    return payload


def _frame(name: str, data: dict[str, Any]) -> str:
    """SSE frame: ``event: <name>\\ndata: <json>\\n\\n``."""
    return f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"


def _field(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _meta(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = list(payload.get("evidence") or [])
    degraded = payload.get("degraded")
    if degraded is None:
        degraded_out: list[Any] = []
    elif isinstance(degraded, (list, tuple)):
        degraded_out = list(degraded)
    elif isinstance(degraded, dict):
        degraded_out = list(degraded)
    else:
        degraded_out = [degraded] if degraded else []
    def _venue(item: dict) -> str | None:
        """Evidence rows carry journal/year, not a pre-joined venue string."""
        if not isinstance(item, dict):
            return None
        if item.get("venue"):
            return str(item["venue"])
        journal = str(item.get("journal") or item.get("journal_abbr") or "").strip()
        year = str(item.get("year") or "").strip()
        return " ".join(part for part in (journal, year) if part) or None

    return {
        "route": payload.get("route"),
        "evidence_count": len(evidence),
        "sources": [
            {
                "pmid": _field(item, "pmid"),
                "title": _field(item, "title"),
                "venue": _venue(item),
                "year": _field(item, "year"),
                "doi": _field(item, "doi"),
            }
            for item in evidence
        ],
        "degraded": degraded_out,
    }


# ------------------------------------------------------------------ dispatcher
class Dispatcher:
    """Fan every bus event out to the sinks waiting on its correlation id."""

    def __init__(self, maxsize: int = 1000) -> None:
        self._sinks: dict[str, set[asyncio.Queue[Event]]] = {}
        self.maxsize = maxsize

    def register(self, correlation_id: str) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self.maxsize)
        self._sinks.setdefault(correlation_id, set()).add(queue)
        return queue

    def unregister(self, correlation_id: str, queue: asyncio.Queue[Event]) -> None:
        sinks = self._sinks.get(correlation_id)
        if not sinks:
            return
        sinks.discard(queue)
        if not sinks:
            self._sinks.pop(correlation_id, None)

    def sink_count(self, correlation_id: str) -> int:
        return len(self._sinks.get(correlation_id, ()))

    async def handle(self, event: Event) -> None:
        """ServiceLoop handler: never produces events, only fans out."""
        for queue in list(self._sinks.get(event.correlation_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # a stuck client must not stall the bus
                log("gateway", "sink.full", cid=event.correlation_id, type=event.type)
        if event.type in TERMINAL:
            # the terminal event is already queued; drop the registration
            self._sinks.pop(event.correlation_id, None)


async def _publish_quietly(bus: Bus, event: Event) -> None:
    try:
        await bus.publish(event)
    except Exception as exc:  # cancel signals are best effort
        log("gateway", "publish.error", type=event.type, error=str(exc))


def _spawn_publish(bus: Bus, event: Event) -> None:
    """Publish without awaiting: safe inside ``finally`` of a dead generator."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    with contextlib.suppress(RuntimeError):
        task = loop.create_task(_publish_quietly(bus, event))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)


# ------------------------------------------------------------------ service
class Gateway:
    def __init__(self, settings, bus: Bus) -> None:
        self.settings = settings
        self.bus = bus
        self.dispatcher = Dispatcher()

    # -- timing ---------------------------------------------------------
    @property
    def chat_timeout(self) -> float:
        return float(getattr(self.settings, "chat_timeout_s", 120.0) or 120.0)

    def _profile_for(self, header: str | None, body: ChatRequest) -> str | None:
        value = (header or "").strip() or (body.context.profile or "").strip()
        return value or None

    def _start_event(self, body: ChatRequest, profile: str | None) -> Event:
        return new_event(
            "chat.requested",
            _request_payload(body, profile),
            producer="gateway",
            timeout_s=self.chat_timeout,
        )

    # -- routes ---------------------------------------------------------
    def router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/api/chat")
        async def chat(body: ChatRequest, request: Request) -> dict[str, Any]:
            event = self._start_event(body, self._profile_for(request.headers.get("x-model-profile"), body))
            queue = self.dispatcher.register(event.correlation_id)
            try:
                await self.bus.publish(event)
                terminal = await self._first_terminal(queue, self.chat_timeout + SLACK_S)
            finally:
                self.dispatcher.unregister(event.correlation_id, queue)
            if terminal is None:
                raise HTTPException(504, "model did not answer in time")
            if terminal.type == "answer.failed":
                raise HTTPException(503, str(terminal.payload.get("message") or "answer failed"))
            payload = terminal.payload
            return {
                "text": str(payload.get("text") or ""),
                "checklist": payload.get("checklist"),
                "citations": list(payload.get("citations") or []),
            }

        @router.post("/api/chat/stream")
        async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
            event = self._start_event(body, self._profile_for(request.headers.get("x-model-profile"), body))
            queue = self.dispatcher.register(event.correlation_id)
            try:
                await self.bus.publish(event)
            except Exception as exc:
                self.dispatcher.unregister(event.correlation_id, queue)
                raise HTTPException(503, f"bus unavailable: {exc}") from exc
            return StreamingResponse(
                self._frames(event, queue, self.chat_timeout + SLACK_S),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        @router.get("/healthz")
        async def healthz() -> dict[str, Any]:
            model: str | None = None
            try:
                model = get_profile(Path(self.settings.models_yaml), "chat").get("model")
            except Exception:  # never call the network on a health probe
                model = None
            return {"service": "gateway", "ok": True, "model": model, "degraded": []}

        self._mount_static(router)
        return router

    # -- internals ------------------------------------------------------
    @staticmethod
    async def _first_terminal(queue: asyncio.Queue[Event], timeout: float) -> Event | None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                event = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if event.type in TERMINAL:
                return event

    async def _frames(self, event: Event, queue: asyncio.Queue[Event], timeout: float) -> AsyncIterator[str]:
        deadline = time.monotonic() + timeout
        partial: list[str] = []
        settled = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    yield _frame("error", {"message": "model did not answer in time", "partial": "".join(partial)})
                    settled = True
                    return
                try:
                    incoming = await asyncio.wait_for(queue.get(), timeout=min(KEEPALIVE_S, remaining))
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if incoming.type == "retrieval.completed":
                    yield _frame("meta", _meta(incoming.payload))
                elif incoming.type == "llm.token":
                    chunk = str(incoming.payload.get("text") or "")
                    partial.append(chunk)
                    yield _frame("delta", {"text": chunk})
                elif incoming.type == "answer.completed":
                    settled = True
                    yield _frame("done", dict(incoming.payload))
                    return
                elif incoming.type == "answer.failed":
                    settled = True
                    yield _frame(
                        "error",
                        {
                            "message": str(incoming.payload.get("message") or "answer failed"),
                            "partial": str(incoming.payload.get("partial") or "".join(partial)),
                        },
                    )
                    return
        finally:
            self.dispatcher.unregister(event.correlation_id, queue)
            if not settled:
                _spawn_publish(self.bus, event.child("chat.cancelled", {"reason": CANCEL_REASON_DISCONNECT}, "gateway"))

    def _mount_static(self, router: APIRouter) -> None:
        """Serve the bundled UI from the repo root, after every API route.

        FastAPI refuses ``router.mount("/")`` (a mount has an empty path, and its
        ``include_router`` requires a non-empty one), so the UI is served by two
        explicit routes registered last on this router. Inside ``MONOLITH`` the
        catch-all would shadow routes the host app registers afterwards (its own
        ``/healthz``), so there the host mounts the UI itself via
        :func:`mount_ui`.
        """
        root = Path(self.settings.models_yaml).parent
        index = root / "index.html"
        if not index.exists():
            return
        try:
            static = StaticFiles(directory=str(root), html=True)
        except Exception as exc:  # pragma: no cover - starlette import-time quirks
            log("gateway", "static.mount_failed", error=str(exc))
            return
        if getattr(self.settings, "monolith", False):
            return

        @router.get("/", include_in_schema=False)
        async def ui_index() -> FileResponse:
            return FileResponse(index)

        @router.get("/{file_path:path}", include_in_schema=False)
        async def ui_asset(file_path: str, request: Request) -> Response:
            return await static.get_response(file_path or "index.html", request.scope)


def mount_ui(app: FastAPI, settings) -> bool:
    """Mount the bundled UI at ``/``: call it on the app *after* every router."""
    from core.config import ROOT

    # The UI lives beside the repo's index.html, not next to the model registry
    # (which is often pointed at a scratch/tmp copy).
    candidates = [ROOT, Path(settings.models_yaml).parent]
    root = next((path for path in candidates if (path / "index.html").exists()), None)
    if root is None:
        return False
    try:
        app.mount("/", StaticFiles(directory=str(root), html=True), name="ui")
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log("gateway", "static.mount_failed", error=str(exc))
        return False


def build(settings, bus: Bus) -> ServiceSpec:
    gateway = Gateway(settings, bus)
    loop = ServiceLoop("gateway.dispatch", bus, gateway.dispatcher.handle, group="gateway.dispatch")
    return ServiceSpec(
        name="gateway",
        router=gateway.router(),
        port=settings.port,
        loops=[loop],
        meta={"dispatcher": gateway.dispatcher},
    )


def main() -> None:  # ``python -m services.gateway.main``
    from services.common import run

    run("services.gateway.main")


if __name__ == "__main__":
    main()
