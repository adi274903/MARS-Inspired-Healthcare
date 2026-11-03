"""Graph retrieval service.

Consumes ``retrieval.requested`` with ``leg == "graph"`` and answers with
``graph.result``. Cypher is never built from user text: only the named templates
in ``retrieval.graph.TEMPLATES`` are rendered, and user text only ever becomes a
bound parameter.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from core.bus import Bus, ServiceLoop
from core.envelope import Event
from core.telemetry import log
from retrieval.graph import TEMPLATES, LocalGraph, Neo4jGraph, extract_graph_params, render
from retrieval.queryparse import parse
from retrieval.sqlite_store import PaperStore
from services.common import ServiceSpec, dedupe_for, run

NAME = "graph_service"
LEG = "graph"
IN_TYPES: tuple[str, ...] = ("retrieval.requested",)
DEFAULT_TEMPLATE = "papers_by_author"
MAX_LIMIT = 200


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _result(
    event: Event,
    *,
    pmids: list[str],
    rows: list[dict[str, Any]],
    template: str,
    degraded: bool = False,
) -> Event:
    payload: dict[str, Any] = {"leg": LEG, "pmids": pmids, "rows": rows, "template": template}
    if degraded:
        payload["degraded"] = True
    return event.child("graph.result", payload, NAME)


class _Retrievers:
    """Neo4j when configured *and* reachable, local SQLite templates otherwise."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.local = LocalGraph(PaperStore(settings.db_path))
        self.neo4j: Neo4jGraph | None = (
            Neo4jGraph(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
            if getattr(settings, "neo4j_uri", "")
            else None
        )
        self._neo4j_checked = False
        self._neo4j_ok = False

    def resolve(self) -> Any:
        if self.neo4j is not None:
            if not self._neo4j_checked:
                self._neo4j_checked = True
                self._neo4j_ok = self.neo4j.available()
                log(NAME, "neo4j.checked", available=self._neo4j_ok)
            if self._neo4j_ok:
                return self.neo4j
        return self.local if self.local.available() else None

    def close(self) -> None:
        if self.neo4j is not None:
            self.neo4j.close()


def _safe_params(template: str, params: dict[str, Any]) -> dict[str, Any]:
    """Drop anything the template does not declare, so render() never raises."""
    allowed = set(TEMPLATES[template]["params"])
    return {k: v for k, v in params.items() if k in allowed}


def _query(retriever: Any, template: str, params: dict[str, Any]) -> dict[str, Any]:
    render(template, params)  # validates the template/params pair before any I/O
    return retriever.query(template, params)


def _make_handler(settings, retrievers: _Retrievers) -> Any:
    async def handle(event: Event) -> Event | None:
        payload = event.payload or {}
        if payload.get("leg") != LEG:
            return None
        question = str(payload.get("question") or "")
        parsed = parse(question)
        template = str(payload.get("template") or parsed.graph_template or DEFAULT_TEMPLATE)
        if not getattr(settings, "graph_enabled", True) or template not in TEMPLATES:
            log(NAME, "graph.disabled", template=template)
            return _result(event, pmids=[], rows=[], template=template, degraded=True)
        params = extract_graph_params(parsed, question)
        params.update(payload.get("params") or {})
        if payload.get("pmid"):
            params["pmid"] = str(payload["pmid"])
        params["limit"] = max(1, min(_int(params.get("limit"), 20), MAX_LIMIT))
        retriever = retrievers.resolve()
        if retriever is None:
            log(NAME, "graph.unavailable", template=template)
            return _result(event, pmids=[], rows=[], template=template, degraded=True)
        try:
            out = _query(retriever, template, _safe_params(template, params))
        except Exception as exc:  # a broken graph degrades a leg, it never fails a request
            log(NAME, "graph.degraded", template=template, error=f"{type(exc).__name__}: {exc}")
            return _result(event, pmids=[], rows=[], template=template, degraded=True)
        return _result(
            event,
            pmids=[str(p) for p in out.get("pmids") or []],
            rows=list(out.get("rows") or []),
            template=str(out.get("template") or template),
        )

    return handle


def _make_router(retrievers: _Retrievers) -> APIRouter:
    api = APIRouter(prefix="/api", tags=[NAME])

    @api.get("/graph/templates")
    def templates() -> dict[str, Any]:
        return {"templates": sorted(TEMPLATES)}

    @api.get("/graph/{template}")
    def graph_query(
        template: str,
        author: str = "",
        mesh: str = "",
        pmid: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        if template not in TEMPLATES:
            raise HTTPException(status_code=404, detail=f"unknown graph template: {template}")
        retriever = retrievers.resolve()
        if retriever is None:
            raise HTTPException(status_code=503, detail="graph unavailable")
        params: dict[str, Any] = {"limit": max(1, min(limit, MAX_LIMIT))}
        if author:
            params["author"] = author
        if mesh:
            params["mesh"] = mesh
        if pmid:
            params["pmid"] = pmid
        try:
            return _query(retriever, template, _safe_params(template, params))
        except Exception as exc:
            log(NAME, "graph.query.degraded", template=template, error=f"{type(exc).__name__}: {exc}")
            raise HTTPException(status_code=503, detail="graph query failed") from exc

    return api


def build(settings, bus: Bus) -> ServiceSpec:
    retrievers = _Retrievers(settings)
    loop = ServiceLoop(
        NAME,
        bus,
        _make_handler(settings, retrievers),
        types=IN_TYPES,
        dedupe=dedupe_for(settings, NAME),
    )

    async def on_start() -> None:
        log(NAME, "graph.ready", backend="neo4j" if settings.neo4j_uri else "local")

    async def on_stop() -> None:
        retrievers.close()

    return ServiceSpec(
        name=NAME,
        router=_make_router(retrievers),
        port=settings.service_ports[NAME],
        loops=[loop],
        on_start=[on_start],
        on_stop=[on_stop],
    )


if __name__ == "__main__":
    run(__name__)
