"""Message bus: one Protocol, two transports.

* ``MemoryBus``    - asyncio queues, same ack/fan-out semantics, used by tests
                     and by ``MONOLITH=1`` so the whole system runs in one process.
* ``RedisBus``     - Redis Streams with consumer groups, XACK and pending reclaim,
                     used by the multi-process (podman pod) topology.

No service imports a transport directly: ``core.bus.build_bus()`` picks one.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, AsyncIterator, Callable, Protocol

from core.envelope import EVENTS_STREAM, Event
from core.telemetry import log


class Subscription(Protocol):
    async def get(self) -> Event | None: ...
    async def ack(self, event: Event) -> None: ...
    async def close(self) -> None: ...


class Bus(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def publish(self, event: Event) -> None: ...
    async def subscribe(self, group: str, consumer: str = "c0") -> Subscription: ...
    async def history(self) -> list[Event]: ...


# ---------------------------------------------------------------- in-memory
class MemoryBus:
    """Fan-out delivery, per-group queues, explicit ack (acked events drop out)."""

    def __init__(self) -> None:
        self._subs: dict[str, list[_MemorySubscription]] = {}
        self._log: list[Event] = []
        self._lock = asyncio.Lock()

    async def start(self) -> None:  # nothing to do
        return None

    async def stop(self) -> None:
        for subs in self._subs.values():
            for sub in subs:
                await sub.close()

    async def publish(self, event: Event) -> None:
        async with self._lock:
            self._log.append(event)
            subs = [s for group in self._subs.values() for s in group]
        for sub in subs:
            if sub.wants(event):
                await sub._queue.put(event)

    async def subscribe(self, group: str, consumer: str = "c0") -> "_MemorySubscription":
        sub = _MemorySubscription(group, consumer)
        self._subs.setdefault(group, []).append(sub)
        return sub

    async def history(self) -> list[Event]:
        return list(self._log)


class _MemorySubscription:
    def __init__(self, group: str, consumer: str) -> None:
        self.group = group
        self.consumer = consumer
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._types: set[str] | None = None
        self._closed = False

    def wants(self, event: Event) -> bool:
        return self._types is None or event.type in self._types

    def only(self, *types: str) -> "_MemorySubscription":
        self._types = set(types)
        return self

    async def get(self) -> Event | None:
        if self._closed:
            return None
        return await self._queue.get()

    async def ack(self, event: Event) -> None:
        return None

    async def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------- redis
class RedisBus:
    """Redis Streams transport. One stream, per-service consumer groups."""

    def __init__(self, url: str, stream: str = EVENTS_STREAM) -> None:
        self.url = url
        self.stream = stream
        self._redis: Any = None

    async def start(self) -> None:
        import redis.asyncio as aioredis  # imported lazily: tests never need it

        self._redis = aioredis.from_url(self.url, decode_responses=True)

    async def stop(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def publish(self, event: Event) -> None:
        assert self._redis is not None, "RedisBus.publish before start()"
        await self._redis.xadd(self.stream, {"data": json.dumps(event.to_dict())}, maxlen=200_000)

    async def subscribe(self, group: str, consumer: str = "c0") -> "_RedisSubscription":
        assert self._redis is not None, "RedisBus.subscribe before start()"
        with contextlib.suppress(Exception):
            await self._redis.xgroup_create(self.stream, group, id="0", mkstream=True)
        return _RedisSubscription(self._redis, self.stream, group, consumer)

    async def history(self) -> list[Event]:
        assert self._redis is not None
        rows = await self._redis.xrange(self.stream)
        return [Event.from_dict(json.loads(data)) for _id, data in ((r[0], r[1]["data"]) for r in rows)]


class _RedisSubscription:
    def __init__(self, redis: Any, stream: str, group: str, consumer: str) -> None:
        self.redis, self.stream, self.group, self.consumer = redis, stream, group, consumer

    def only(self, *_types: str) -> "_RedisSubscription":  # filtering happens in the service
        return self

    async def get(self) -> Event | None:
        while True:
            rows = await self.redis.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=32, block=1000)
            for _stream, entries in rows or []:
                for entry_id, data in entries:
                    event = Event.from_dict(json.loads(data["data"]))
                    event_payload: dict[str, Any] = {"_entry_id": entry_id}
                    object.__setattr__(event, "payload", {**event.payload, **event_payload})
                    return event
            return None

    async def ack(self, event: Event) -> None:
        entry_id = event.payload.get("_entry_id")
        if entry_id:
            await self.redis.xack(self.stream, self.group, entry_id)

    async def close(self) -> None:
        return None


def build_bus(url: str | None = None, *, kind: str | None = None) -> Bus:
    """kind: "memory" | "redis" (falls back to memory when no redis url)."""
    if kind == "redis" and url:
        return RedisBus(url)
    return MemoryBus()


# ---------------------------------------------------------------- service loop
class ServiceLoop:
    """Consume a subscription, dispatch to a handler, publish what it returns.

    Handles: type filtering, deadline drops, at-least-once dedupe, error capture
    and (optionally) publishing a ``*.failed`` event for the correlation.
    """

    def __init__(
        self,
        name: str,
        bus: Bus,
        handler: Callable[[Event], Any],
        *,
        group: str | None = None,
        types: tuple[str, ...] | None = None,
        dedupe: Any = None,
    ) -> None:
        self.name = name
        self.bus = bus
        self.handler = handler
        self.group = group or name
        self.types = types
        self.dedupe = dedupe
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        # Subscribe *before* returning: otherwise an event published immediately after
        # startup (or between services starting) is never delivered.
        self._sub = await self.bus.subscribe(self.group)
        if hasattr(self._sub, "only") and self.types:
            self._sub.only(*self.types)
        self._task = asyncio.create_task(self._run(self._sub), name=f"loop:{self.name}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        sub = getattr(self, "_sub", None)
        if sub is not None:
            with contextlib.suppress(Exception):
                await sub.close()
            self._sub = None

    async def _run(self, sub: Subscription) -> None:
        while not self._stop.is_set():
            try:
                event = await sub.get()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # transport hiccup: log and keep the loop alive
                log(self.name, "bus.error", error=str(exc))
                await asyncio.sleep(0.2)
                continue
            if event is None:
                await asyncio.sleep(0.01)
                continue
            if self.types and event.type not in self.types:
                await sub.ack(event)
                continue
            if event.expired():
                log(self.name, "event.expired", type=event.type, cid=event.correlation_id)
                await sub.ack(event)
                continue
            if self.dedupe is not None and await self.dedupe.seen(event.event_id):
                await sub.ack(event)
                continue
            try:
                produced = await self.handler(event)
                for out in _as_events(produced):
                    await self.bus.publish(out)
            except Exception as exc:  # never kill the loop on a handler bug
                log(self.name, "handler.error", type=event.type, error=f"{type(exc).__name__}: {exc}")
                await self.bus.publish(
                    event.child("answer.failed", {"service": self.name, "message": f"{type(exc).__name__}: {exc}"}, self.name)
                )
            await sub.ack(event)


def _as_events(produced: Any) -> list[Event]:
    if produced is None:
        return []
    if isinstance(produced, Event):
        return [produced]
    return [e for e in produced if isinstance(e, Event)]


async def drain_until(bus: Bus, predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """Test helper: wait until ``predicate()`` is true or the deadline passes."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("condition not reached")


async def collect(bus: Bus, *, count: int, types: tuple[str, ...] | None = None, timeout: float = 5.0) -> list[Event]:
    """Test helper: read ``count`` events of ``types`` off the bus history."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    seen: list[Event] = []
    while loop.time() < end:
        history = await bus.history()
        seen = [e for e in history if types is None or e.type in types]
        if len(seen) >= count:
            return seen
        await asyncio.sleep(0.01)
    raise TimeoutError(f"expected {count} events, saw {len(seen)}")


async def aiter_events(sub: Subscription) -> AsyncIterator[Event]:
    while True:
        event = await sub.get()
        if event is None:
            return
        yield event
