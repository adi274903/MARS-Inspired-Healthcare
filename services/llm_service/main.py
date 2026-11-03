"""LLM service: stream a model answer, emit decoded token batches, then a completion.

The service is deliberately thin: it owns no prompt logic and no answer parsing
beyond the incremental ``text`` decoder. It keeps one :class:`LLMClient` per
model profile so a ``model.changed`` event only reloads the profile it names.

Streaming is decoupled from batching: a producer task pumps the provider into a
queue, and the consumer flushes on *either* ``token_batch_chars`` characters or
``token_batch_ms`` milliseconds, whichever comes first, so a slow model still
produces smooth frames instead of a burst at the end.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter

from core import registry
from core.bus import Bus, ServiceLoop
from core.envelope import Event
from core.telemetry import log
from llm.client import LLMClient
from llm.stream_decode import JsonTextStreamer
from retrieval.prompt import ANSWER_SCHEMA
from services.common import ServiceSpec, dedupe_for

NAME = "llm_service"
HANDLED_TYPES = ("generation.requested", "model.changed")

#: ``LLM_PROVIDER`` values we are allowed to force onto a profile.
FORCEABLE_PROVIDERS = {"mock", "hf_local", "openai_compat"}


class TokenBatcher:
    """Group decoded answer fragments into UI-sized frames.

    A frame is released when it holds ``char_limit`` characters **or** when
    ``ms_limit`` milliseconds have passed since the frame's first character.
    ``seq`` counts released frames, starting at 1.
    """

    def __init__(self, *, char_limit: int = 24, ms_limit: int = 40) -> None:
        self.char_limit = max(1, int(char_limit))
        self.ms_limit = max(0, int(ms_limit))
        self._buf = ""
        self._started: float | None = None
        self.seq = 0

    def pending(self) -> str:
        return self._buf

    def add(self, text: str, *, now: float | None = None) -> str | None:
        """Append ``text``; return a frame if one is due, else ``None``."""
        now = time.monotonic() if now is None else now
        if text:
            if not self._buf:
                self._started = now
            self._buf += text
        if not self._buf:
            return None
        elapsed_ms = 0.0 if self._started is None else (now - self._started) * 1000.0
        if len(self._buf) >= self.char_limit or elapsed_ms >= self.ms_limit:
            return self.take()
        return None

    def take(self) -> str | None:
        """Force the pending frame out (timer fired / stream ended)."""
        if not self._buf:
            return None
        frame, self._buf, self._started = self._buf, "", None
        self.seq += 1
        return frame

    def timeout(self, *, now: float | None = None) -> float | None:
        """Seconds until a time-based flush is due; ``None`` if nothing is pending."""
        if not self._buf or self._started is None or self.ms_limit <= 0:
            return None
        now = time.monotonic() if now is None else now
        return max(0.0, self.ms_limit / 1000.0 - (now - self._started))


class ServiceClient(LLMClient):
    """``LLMClient`` with an optional provider override (tests / offline demos)."""

    def __init__(self, settings, profile_name: str = "chat", *, force: str | None = None) -> None:
        super().__init__(settings, profile_name, profiles_path=settings.models_yaml)
        self.force = force

    def profile(self) -> dict[str, Any]:
        profile = dict(super().profile())
        if self.force and profile.get("provider") != self.force:
            profile["provider"] = self.force
        return profile

    def reload(self) -> dict[str, Any]:
        super().reload()
        return self.profile()


class LLMService:
    def __init__(self, settings, bus: Bus) -> None:
        self.settings = settings
        self.bus = bus
        force = (getattr(settings, "llm_provider", "") or "").strip().lower()
        self.force = force if force in FORCEABLE_PROVIDERS else None
        self._clients: dict[str, ServiceClient] = {}
        self._verify: dict[str, dict[str, Any]] = {}

    # -- client cache ----------------------------------------------------
    def client_for(self, profile_name: str | None) -> ServiceClient:
        name = (profile_name or "chat").strip() or "chat"
        client = self._clients.get(name)
        if client is None:
            client = ServiceClient(self.settings, name, force=self.force)
            self._clients[name] = client
        return client

    # -- bus entry point -------------------------------------------------
    async def on_event(self, event: Event) -> list[Event]:
        if event.type == "generation.requested":
            return await self.on_generate(event)
        if event.type == "model.changed":
            await self.on_model_changed(event)
            return []
        return []

    # -- generation ------------------------------------------------------
    async def on_generate(self, event: Event) -> list[Event]:
        payload = event.payload or {}
        messages = [
            {"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")}
            for m in (payload.get("messages") or [])
            if isinstance(m, dict)
        ]
        profile_name = str(payload.get("profile") or "chat")
        client = self.client_for(profile_name)
        streamer = JsonTextStreamer()
        batcher = TokenBatcher(
            char_limit=int(getattr(self.settings, "token_batch_chars", 24)),
            ms_limit=int(getattr(self.settings, "token_batch_ms", 40)),
        )
        raw_parts: list[str] = []
        decoded_parts: list[str] = []

        async def publish_frame(text: str | None) -> None:
            if not text:
                return
            await self.bus.publish(event.child("llm.token", {"seq": batcher.seq, "text": text}, NAME))

        async def drain() -> None:
            await publish_frame(batcher.take())

        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for chunk in client.stream(messages, schema=ANSWER_SCHEMA):
                    await queue.put(("chunk", chunk))
            except Exception as exc:  # provider failure: report, never raise out of the loop
                await queue.put(("error", exc))
            finally:
                await queue.put(("end", None))

        pump_task = asyncio.create_task(pump(), name=f"llm.stream:{event.correlation_id[:8]}")
        error: BaseException | None = None
        try:
            while True:
                timeout = batcher.timeout()
                try:
                    kind, value = await asyncio.wait_for(queue.get(), timeout)
                except asyncio.TimeoutError:
                    await drain()  # token_batch_ms elapsed
                    continue
                if kind == "chunk":
                    chunk_text = "" if value is None else str(value)
                    raw_parts.append(chunk_text)
                    decoded = streamer.feed(chunk_text)
                    if decoded:
                        decoded_parts.append(decoded)
                        await publish_frame(batcher.add(decoded))
                elif kind == "error":
                    error = value if isinstance(value, BaseException) else RuntimeError(str(value))
                    break
                else:  # "end"
                    break
        finally:
            if not pump_task.done():
                pump_task.cancel()
            try:
                await pump_task
            except (asyncio.CancelledError, Exception):
                pass

        await drain()
        tail = streamer.finish()
        if tail:
            decoded_parts.append(tail)
            batcher.add(tail)
            await drain()

        if error is not None:
            partial = "".join(decoded_parts)
            failure: dict[str, Any] = {"message": f"{type(error).__name__}: {error}"}
            if partial:
                failure["partial"] = partial
            log(NAME, "generation.failed", cid=event.correlation_id, profile=profile_name, error=failure["message"])
            return [event.child("llm.failed", failure, NAME)]

        raw = "".join(raw_parts)
        text = "".join(decoded_parts)
        prompt_tokens = sum(len(m["content"]) for m in messages) // 4
        completion = {
            "raw": raw,
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(raw) // 4},
            "model": str(client.profile().get("model") or ""),
            "profile": profile_name,
            "text": text,
            "tokens_emitted": batcher.seq,
        }
        log(
            NAME,
            "generation.completed",
            cid=event.correlation_id,
            profile=profile_name,
            chars=len(text),
            tokens=batcher.seq,
        )
        return [event.child("llm.completed", completion, NAME)]

    # -- model hot swap ---------------------------------------------------
    async def on_model_changed(self, event: Event) -> None:
        payload = event.payload or {}
        profile_name = str(payload.get("profile") or "chat")
        try:
            client = self.client_for(profile_name)
            cfg = client.reload() or {}
            self._verify.pop(client.profile_name, None)
            log(NAME, "model.changed", profile=profile_name, model=cfg.get("model"), provider=cfg.get("provider"))
        except Exception as exc:  # a bad profile must never take the service down
            log(NAME, "model.changed.failed", profile=profile_name, error=f"{type(exc).__name__}: {exc}")

    # -- introspection ----------------------------------------------------
    async def describe(self, profile_name: str = "chat", *, refresh: bool = False) -> dict[str, Any]:
        client = self.client_for(profile_name)
        cfg = dict(client.profile())
        cached = profile_name in self._verify
        status = self._verify.get(profile_name)
        if status is None or refresh:
            try:
                verified = await client.verify()
                status = {
                    "resolved": bool(verified.resolved),
                    "model": verified.model,
                    "provider": verified.provider,
                    "detail": verified.detail,
                }
            except Exception as exc:
                status = {
                    "resolved": False,
                    "model": cfg.get("model", ""),
                    "provider": cfg.get("provider", ""),
                    "detail": {"error": f"{type(exc).__name__}: {exc}"},
                }
            self._verify[profile_name] = status
            cached = False
        return {
            "profile": profile_name,
            "config": cfg,
            "model": status.get("model") or cfg.get("model", ""),
            "provider": status.get("provider") or cfg.get("provider", ""),
            "resolved": bool(status.get("resolved")),
            "verify": status.get("detail") or {},
            "cached": cached,
        }


def make_router(service: LLMService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/models")
    async def models(profile: str = "chat", refresh: int = 0) -> dict[str, Any]:
        return await service.describe(profile, refresh=bool(refresh))

    @router.get("/api/profiles")
    async def profiles() -> dict[str, Any]:
        return {"profiles": registry.load_profiles(service.settings.models_yaml)}

    return router


def build(settings, bus: Bus) -> ServiceSpec:
    service = LLMService(settings, bus)
    loop = ServiceLoop(
        NAME,
        bus,
        service.on_event,
        group=NAME,
        types=HANDLED_TYPES,
        dedupe=dedupe_for(settings, NAME),
    )
    return ServiceSpec(
        name=NAME,
        router=make_router(service),
        port=settings.service_ports.get("llm_service", 8085),
        loops=[loop],
        meta={"llm": service},
    )


if __name__ == "__main__":  # pragma: no cover
    from services.common import run

    run("services.llm_service.main")
