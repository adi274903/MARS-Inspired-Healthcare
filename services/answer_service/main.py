"""Answer service: JSON envelope in, renderable answer + verified citations out.

Consumes ``retrieval.completed`` (the numbered evidence, the stats block, the
route) and the streamed LLM events, and publishes one ``answer.completed`` with
shaped markdown, a coerced checklist and citations built *only* from the
evidence rows. The model's numbers are renumbered into the surviving set, so a
hallucinated ``[7]`` never reaches the UI.

The same composition is exposed synchronously as ``POST /api/compose`` for tests
and for debugging a single raw envelope.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.bus import Bus, ServiceLoop
from core.envelope import Event
from core.telemetry import log
from retrieval import citations, shape
from retrieval import guidance as corpus_guidance
from services.common import ServiceSpec, dedupe_for

SERVICE = "answer_service"
CONSUMES = ("llm.token", "llm.completed", "llm.failed", "retrieval.completed")
_MAX_STATES = 256


class _State:
    """Per-correlation scratch space; dropped as soon as the answer is out."""

    __slots__ = ("evidence", "stats", "route", "degraded", "decoded_text", "seq_last", "question")

    def __init__(self) -> None:
        self.evidence: list[dict] = []
        self.stats: dict | None = None
        self.route: Any = None
        self.degraded: bool = False
        self.decoded_text: str = ""
        self.seq_last: int = -1
        self.question: str = ""


def _compose(
    raw: str,
    evidence: list[dict],
    *,
    fallback_text: str = "",
    guidance: list[dict] | None = None,
    usage: Any = None,
) -> dict[str, Any]:
    """Envelope -> sanitized text, renumbered markers and verified citations."""
    env = shape.parse_envelope(raw)
    text = shape.sanitize_markdown(env.get("text") or fallback_text or "")
    text, markers = citations.renumber(text, len(evidence))
    built = citations.build_citations(evidence, markers, env.get("claims"), guidance or [])
    kept, dropped = citations.drop_unevidenced(built, evidence)
    body = shape.validate_answer({**env, "text": text})
    return {
        "text": body["text"],
        "checklist": body["checklist"],
        "claims": body["claims"],
        "citations": kept,
        "markers": markers,
        "dropped_citations": dropped,
        "usage": usage,
        "recovered": env.get("recovered", False),
    }


class ComposeRequest(BaseModel):
    raw: str = Field(default="")
    evidence: list[dict] = Field(default_factory=list)
    question: str = ""


def _router(guidance_index: corpus_guidance.GuidanceIndex | None) -> APIRouter:
    router = APIRouter(tags=[SERVICE])

    @router.post("/api/compose")
    async def compose(request: ComposeRequest) -> dict[str, Any]:
        matches = (
            guidance_index.match(request.question, k=2)
            if guidance_index is not None and request.question
            else []
        )
        return _compose(request.raw, request.evidence, guidance=corpus_guidance.citations_for(matches))

    return router


def build(settings, bus: Bus) -> ServiceSpec:
    # Curated guidance corpus (reporting guidelines / governance frameworks). The
    # deterministic alias+IDF matcher keeps a pure data question from pulling guidance in.
    guidance_path = getattr(settings, "guidance_path", None) or (settings.models_yaml.parent / "guidance.yaml")
    guidance_index = corpus_guidance.default_index(guidance_path)
    states: dict[str, _State] = {}

    def _state(correlation_id: str) -> _State:
        state = states.get(correlation_id)
        if state is None:
            if len(states) >= _MAX_STATES:
                states.pop(next(iter(states)), None)
            state = _State()
            states[correlation_id] = state
        return state

    def _guidance_for(state: _State) -> list[dict]:
        if guidance_index is None or not state.question:
            return []
        return corpus_guidance.citations_for(guidance_index.match(state.question, k=2))

    async def handle(event: Event) -> list[Event] | None:
        correlation_id = event.correlation_id
        payload = event.payload or {}

        if event.type == "retrieval.completed":
            state = _state(correlation_id)
            evidence = payload.get("evidence")
            state.evidence = [row for row in evidence if isinstance(row, dict)] if isinstance(evidence, list) else []
            state.stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else None
            state.route = payload.get("route")
            state.degraded = bool(payload.get("degraded"))
            state.question = str(payload.get("question") or "")
            log(SERVICE, "retrieval.stored", cid=correlation_id, evidence=len(state.evidence))
            return None

        if event.type == "llm.token":
            state = _state(correlation_id)
            chunk = payload.get("text")
            if isinstance(chunk, str) and chunk:
                state.decoded_text += chunk
            seq = payload.get("seq")
            if isinstance(seq, int):
                state.seq_last = seq
            return None

        if event.type == "llm.completed":
            state = _state(correlation_id)
            raw = payload.get("raw")
            raw = raw if isinstance(raw, str) else ""
            fallback = state.decoded_text or str(payload.get("text") or "")
            composed = _compose(
                raw,
                state.evidence,
                fallback_text=fallback,
                guidance=_guidance_for(state),
                usage=payload.get("usage"),
            )
            states.pop(correlation_id, None)
            log(
                SERVICE,
                "answer.completed",
                cid=correlation_id,
                markers=len(composed["markers"]),
                citations=len(composed["citations"]),
                dropped=len(composed["dropped_citations"]),
            )
            return [
                event.child(
                    "answer.completed",
                    {
                        "text": composed["text"],
                        "checklist": composed["checklist"],
                        "citations": composed["citations"],
                        "markers": composed["markers"],
                        "usage": payload.get("usage"),
                        "model": payload.get("model"),
                        "profile": payload.get("profile"),
                        "route": state.route,
                        "degraded": state.degraded,
                        "dropped_citations": composed["dropped_citations"],
                    },
                    SERVICE,
                )
            ]

        if event.type == "llm.failed":
            states.pop(correlation_id, None)
            message = payload.get("message") or "the model call failed"
            log(SERVICE, "answer.failed", cid=correlation_id, message=str(message)[:200])
            return [
                event.child(
                    "answer.failed",
                    {
                        "service": SERVICE,
                        "message": payload.get("message"),
                        "partial": payload.get("partial") or "",
                    },
                    SERVICE,
                )
            ]

        return None

    loop = ServiceLoop(SERVICE, bus, handle, types=CONSUMES, dedupe=dedupe_for(settings, SERVICE))
    return ServiceSpec(
        name=SERVICE,
        router=_router(guidance_index),
        port=settings.service_ports["answer_service"],
        loops=[loop],
        meta={"consumes": list(CONSUMES), "guidance": len(guidance_index) if guidance_index is not None else 0},
    )
