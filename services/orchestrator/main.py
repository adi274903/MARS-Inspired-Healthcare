"""Orchestrator: one chat turn, fanned out to the retrieval legs, fused with RRF.

Design rules:

* state is **per correlation id** and is dropped the moment a turn is fused, so a
  replayed or late ``*.result`` can never corrupt a live turn;
* a missing leg never blocks the answer: as soon as the first result lands a
  short grace timer is armed, and whatever has arrived by then gets fused while
  the rest is reported as ``degraded``;
* the fused evidence is re-ordered to the RRF ranking before it is handed to the
  prompt, because the prompt's numbering *is* the citation contract.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter

from core.bus import Bus, ServiceLoop
from core.envelope import Event
from core.telemetry import log
from retrieval import queryparse, rrf
from retrieval import guidance as corpus_guidance
from retrieval.prompt import build_messages
from retrieval.sqlite_store import PaperStore
from services.common import ServiceSpec, dedupe_for

NAME = "orchestrator"
LEG_ORDER = ("sqlite", "vector", "graph")
RESULT_TYPES = tuple(f"{leg}.result" for leg in LEG_ORDER)
HANDLED_TYPES = ("chat.requested", "answer.failed", *RESULT_TYPES)

#: Intent -> retrieval legs. Disabled legs are dropped afterwards.
LEGS_BY_INTENT: dict[str, tuple[str, ...]] = {
    "aggregate": ("sqlite",),
    "multihop": ("graph", "sqlite"),
    "semantic": ("vector", "sqlite"),
    "faceted": ("sqlite", "vector", "graph"),
    "hybrid": ("sqlite", "vector", "graph"),
}
RRF_WEIGHTS = {"sqlite": 1.0, "vector": 1.0, "graph": 0.8}

#: How long to wait for the remaining legs once the first result arrives.
RESULT_GRACE_S = 0.25


def decide_legs(parsed: Any, settings) -> list[str]:
    """Which legs this question needs, minus the ones disabled in settings."""
    intended = LEGS_BY_INTENT.get(getattr(parsed, "intent", "hybrid"), LEGS_BY_INTENT["hybrid"])
    enabled = {
        "sqlite": bool(getattr(settings, "sqlite_enabled", True)),
        "vector": bool(getattr(settings, "vector_enabled", True)),
        "graph": bool(getattr(settings, "graph_enabled", True)),
    }
    legs = [leg for leg in intended if enabled.get(leg, True)]
    if not legs:
        # A turn with zero legs would never complete: sqlite is the system of record.
        legs = ["sqlite"]
    return legs


@dataclass
class Turn:
    """Everything one correlation id needs until its answer is requested."""

    event: Event
    question: str
    parsed: Any
    context: dict[str, Any]
    messages: list[dict[str, Any]]
    profile: str
    legs: list[str]
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None
    done: bool = False
    started: float = field(default_factory=time.time)


class Orchestrator:
    def __init__(self, settings, bus: Bus) -> None:
        self.settings = settings
        self.bus = bus
        self._turns: dict[str, Turn] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # -- bus entry point -------------------------------------------------
    async def on_event(self, event: Event) -> list[Event]:
        if event.type == "chat.requested":
            return await self.on_chat(event)
        if event.type in RESULT_TYPES:
            await self.on_result(event)
            return []
        if event.type == "answer.failed":
            log(NAME, "answer.failed", cid=event.correlation_id, service=event.payload.get("service"), error=event.payload.get("message"))
            return []
        return []

    # -- inbound ---------------------------------------------------------
    async def on_chat(self, event: Event) -> list[Event]:
        payload = event.payload or {}
        messages = [m for m in (payload.get("messages") or []) if isinstance(m, dict)]
        question = str(messages[-1].get("text") or "") if messages else ""
        parsed = queryparse.parse(question)
        context = payload.get("context") or {}
        profile = str(payload.get("profile") or "chat")
        legs = decide_legs(parsed, self.settings)

        if event.correlation_id in self._turns:
            log(NAME, "chat.duplicate", cid=event.correlation_id)
            return []

        self._turns[event.correlation_id] = Turn(
            event=event,
            question=question,
            parsed=parsed,
            context=dict(context),
            messages=messages,
            profile=profile,
            legs=legs,
        )
        log(NAME, "chat.requested", cid=event.correlation_id, intent=parsed.intent, legs=legs)

        body = {
            "question": question,
            "parsed": parsed.as_dict(),
            "k": int(self.settings.num_evidence),
            "expect": len(legs),
        }
        events: list[Event] = []
        for leg in legs:
            events.append(event.child("retrieval.requested", {**body, "leg": leg}, NAME))
        return events

    async def on_result(self, event: Event) -> None:
        turn = self._turns.get(event.correlation_id)
        if turn is None or turn.done:
            log(NAME, "result.orphan", type=event.type, cid=event.correlation_id)
            return
        leg = str(event.payload.get("leg") or event.type.split(".", 1)[0])
        turn.results[leg] = dict(event.payload or {})
        if len(turn.results) >= len(turn.legs):
            await self._finish(turn)
            return
        if turn.task is None:
            turn.task = asyncio.create_task(self._grace_out(turn), name=f"orchestrator.grace:{event.correlation_id[:8]}")
            self._tasks.add(turn.task)
            turn.task.add_done_callback(self._tasks.discard)

    async def _grace_out(self, turn: Turn) -> None:
        """Fuse whatever arrived after a short grace period (or at the deadline)."""
        remaining = None if turn.event.deadline is None else turn.event.deadline - time.time()
        wait = RESULT_GRACE_S if remaining is None else min(RESULT_GRACE_S, max(0.0, remaining))
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            raise
        await self._finish(turn)

    # -- fusion ----------------------------------------------------------
    async def _finish(self, turn: Turn) -> None:
        if turn.done:
            return
        turn.done = True
        self._turns.pop(turn.event.correlation_id, None)
        try:
            evidence, stats, degraded = self.resolve(turn)
            history = [
                {"role": str(m.get("role") or "user"), "text": str(m.get("text") or "")}
                for m in turn.messages[:-1]
            ][-4:]
            parsed = turn.parsed
            # Curated guidance is matched here (never counted as corpus evidence) so the
            # prompt can name the right framework and the answer can cite it.
            guidance_citations: list[dict[str, Any]] = []
            guidance_block = ""
            index = corpus_guidance.default_index(getattr(self.settings, "guidance_path", None))
            if index is not None and parsed.intent != "aggregate":
                matches = index.match(turn.question, k=2)
                guidance_citations = corpus_guidance.citations_for(matches)
                guidance_block = index.prompt_block(matches)
            completed = turn.event.child(
                "retrieval.completed",
                {
                    "question": turn.question,
                    "parsed": parsed.as_dict(),
                    "evidence": evidence,
                    "stats": stats,
                    "degraded": degraded,
                    "route": parsed.intent,
                    "context": turn.context,
                    "history": history,
                    "guidance": guidance_citations,
                },
                NAME,
            )
            messages = build_messages(
                turn.question,
                evidence,
                stats=stats,
                context=turn.context,
                parsed=parsed,
                max_abstract_chars=int(self.settings.max_abstract_chars),
                history=history,
                guidance=guidance_block,
            )
            generation = turn.event.child(
                "generation.requested",
                {
                    "messages": messages,
                    "profile": turn.profile,
                    "evidence": evidence,
                    "stats": stats,
                    "route": parsed.intent,
                    "context": turn.context,
                    "parsed": parsed.as_dict(),
                },
                NAME,
            )
            await self.bus.publish(completed)
            await self.bus.publish(generation)
            log(
                NAME,
                "retrieval.completed",
                cid=turn.event.correlation_id,
                intent=parsed.intent,
                evidence=len(evidence),
                degraded=degraded,
                stats_total=(stats or {}).get("total"),
            )
        except Exception as exc:  # never strand the correlation
            log(NAME, "fuse.failed", cid=turn.event.correlation_id, error=f"{type(exc).__name__}: {exc}")
            await self.bus.publish(
                turn.event.child("answer.failed", {"service": NAME, "message": f"{type(exc).__name__}: {exc}"}, NAME)
            )

    def resolve(self, turn: Turn) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[str]]:
        """RRF over the legs that answered -> hydrated, RRF-ordered evidence rows."""
        ranked: dict[str, list[str]] = {}
        for leg in LEG_ORDER:
            result = turn.results.get(leg) or {}
            pmids = [str(p) for p in (result.get("pmids") or []) if p]
            if pmids:
                ranked[leg] = pmids
        fused = rrf.fuse(
            ranked,
            k=int(self.settings.rrf_k),
            weights=RRF_WEIGHTS,
            limit=int(self.settings.num_evidence),
        )
        evidence = self._hydrate(fused)

        sqlite_result = turn.results.get("sqlite") or {}
        stats = sqlite_result.get("stats")
        stats = dict(stats) if isinstance(stats, dict) else None

        degraded = [
            leg
            for leg in turn.legs
            if leg not in turn.results or bool((turn.results.get(leg) or {}).get("degraded"))
        ]
        return evidence, stats, degraded

    def _hydrate(self, fused: list[tuple[str, float]]) -> list[dict[str, Any]]:
        if not fused:
            return []
        try:
            rows = PaperStore(self.settings.db_path).get_many([pmid for pmid, _score in fused])
        except Exception as exc:  # unreadable DB must not kill the turn
            log(NAME, "evidence.hydrate.failed", error=f"{type(exc).__name__}: {exc}")
            return []
        ordered = [rows[pmid] for pmid, _score in fused if pmid in rows]
        return ordered[: int(self.settings.num_evidence)]

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        self._turns.clear()


def make_router(service: Orchestrator, settings) -> APIRouter:
    router = APIRouter()

    @router.get("/api/route")
    async def route(q: str = "") -> dict[str, Any]:
        """Introspection endpoint: parse a question and show the leg decision."""
        parsed = queryparse.parse(q)
        legs = decide_legs(parsed, settings)
        body = parsed.as_dict()
        body.update(
            {
                "question": q,
                "route": parsed.intent,
                "legs": legs,
                "expect": len(legs),
                "k": int(settings.num_evidence),
            }
        )
        return body

    return router


def build(settings, bus: Bus) -> ServiceSpec:
    service = Orchestrator(settings, bus)
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
        router=make_router(service, settings),
        port=settings.service_ports.get("orchestrator", 8081),
        loops=[loop],
        on_stop=[service.stop],
        meta={"orchestrator": service},
    )


if __name__ == "__main__":  # pragma: no cover
    from services.common import run

    run("services.orchestrator.main")
