"""SQLite retrieval service.

Consumes ``retrieval.requested`` with ``leg == "sqlite"`` and answers with
``sqlite.result``. The parsed query on the wire is only a hint: the question is
re-parsed here so a stale or hostile payload cannot change the SQL shape.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from core.bus import Bus, ServiceLoop
from core.envelope import Event
from core.telemetry import log
from retrieval import stats as stats_mod
from retrieval.queryparse import ParsedQuery, parse
from retrieval.sqlite_store import PaperStore
from services.common import ServiceSpec, dedupe_for, run

NAME = "sqlite_service"
LEG = "sqlite"
IN_TYPES: tuple[str, ...] = ("retrieval.requested",)
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
    filters_applied: bool,
    stats: dict[str, Any] | None,
    degraded: bool = False,
) -> Event:
    payload: dict[str, Any] = {
        "leg": LEG,
        "pmids": pmids,
        "filters_applied": filters_applied,
        "stats": stats,
    }
    if degraded:
        payload["degraded"] = True
    return event.child("sqlite.result", payload, NAME)


def _make_handler(settings) -> Any:
    async def handle(event: Event) -> Event | None:
        payload = event.payload or {}
        if payload.get("leg") != LEG:
            return None
        question = str(payload.get("question") or "")
        k = max(1, _int(payload.get("k"), settings.num_evidence))
        parsed = parse(question)
        store = PaperStore(settings.db_path)
        if not getattr(settings, "sqlite_enabled", True):
            log(NAME, "sqlite.disabled")
            return _result(event, pmids=[], filters_applied=False, stats=None, degraded=True)
        if not store.available():
            log(NAME, "store.unavailable", path=str(settings.db_path))
            return _result(event, pmids=[], filters_applied=False, stats=None, degraded=True)
        try:
            if parsed.intent == "aggregate":
                aggregate = stats_mod.aggregate(settings.db_path, parsed)
                return _result(event, pmids=[], filters_applied=True, stats=aggregate)
            pmids, filters_applied = store.search(parsed, k)
            return _result(event, pmids=pmids, filters_applied=filters_applied, stats=None)
        except Exception as exc:  # a broken store degrades a leg, it never fails a request
            log(NAME, "store.degraded", error=f"{type(exc).__name__}: {exc}")
            return _result(event, pmids=[], filters_applied=False, stats=None, degraded=True)

    return handle


def _query_from_params(
    q: str,
    domain: str,
    tier: str,
    user: str,
    formulation: str,
    year_min: int | None,
    year_max: int | None,
    external: bool,
) -> ParsedQuery:
    parsed = parse(q) if q else ParsedQuery(raw="")
    if domain:
        parsed.filters["a_domain"] = [domain]
    if user:
        parsed.filters["b_end_user"] = [user]
    if formulation:
        parsed.filters["e_formulation"] = [formulation]
    if tier:
        parsed.filters["g_eval_tier"] = [tier]
    if external:
        parsed.flags["external_only"] = True
    if year_min is not None:
        parsed.year_min = year_min
    if year_max is not None:
        parsed.year_max = year_max
    return parsed


def _make_router(settings) -> APIRouter:
    api = APIRouter(prefix="/api", tags=[NAME])

    @api.get("/papers")
    def papers(
        q: str = "",
        domain: str = "",
        tier: str = "",
        user: str = "",
        formulation: str = "",
        year_min: int | None = None,
        year_max: int | None = None,
        external: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        window = max(1, min(limit, MAX_LIMIT))
        start = max(0, offset)
        parsed = _query_from_params(q, domain, tier, user, formulation, year_min, year_max, external)
        store = PaperStore(settings.db_path)
        if not store.available():
            return {"papers": [], "total": 0, "filters_applied": False}
        try:
            pmids, filters_applied = store.search(parsed, start + window)
            if not pmids and not parsed.fts_terms:
                # browse mode (facets only): search()'s relaxed fallback returns
                # nothing without FTS terms, so fall back to a filtered listing.
                pmids = store.recent(parsed, start + window)
                filters_applied = True
            page = pmids[start:start + window]
            rows = store.get_many(page)
        except Exception as exc:
            log(NAME, "papers.degraded", error=f"{type(exc).__name__}: {exc}")
            return {"papers": [], "total": 0, "filters_applied": False}
        return {
            "papers": [rows[p] for p in page if p in rows],
            "total": len(pmids),
            "filters_applied": filters_applied,
        }

    @api.get("/stats")
    def stats() -> dict[str, Any]:
        try:
            return stats_mod.corpus_overview(settings.db_path)
        except Exception as exc:
            log(NAME, "stats.unavailable", error=f"{type(exc).__name__}: {exc}")
            raise HTTPException(status_code=503, detail="store unavailable") from exc

    @api.get("/facets")
    def facets() -> dict[str, Any]:
        try:
            overview = stats_mod.corpus_overview(settings.db_path)
        except Exception as exc:
            log(NAME, "facets.unavailable", error=f"{type(exc).__name__}: {exc}")
            raise HTTPException(status_code=503, detail="store unavailable") from exc
        return {"total": overview["total"], "facets": overview["facets"], "flags": overview["flags"]}

    @api.get("/health-store")
    def health_store() -> dict[str, Any]:
        store = PaperStore(settings.db_path)
        available = store.available()
        rows = 0
        if available:
            try:
                rows = store.count()
            except Exception:  # pragma: no cover - count on a live DB never fails in practice
                rows = 0
        return {"available": available, "rows": rows}

    return api


def build(settings, bus: Bus) -> ServiceSpec:
    loop = ServiceLoop(
        NAME,
        bus,
        _make_handler(settings),
        types=IN_TYPES,
        dedupe=dedupe_for(settings, NAME),
    )

    async def on_start() -> None:
        store = PaperStore(settings.db_path)
        log(NAME, "store.ready", path=str(settings.db_path), available=store.available())

    return ServiceSpec(
        name=NAME,
        router=_make_router(settings),
        port=settings.service_ports[NAME],
        loops=[loop],
        on_start=[on_start],
    )


if __name__ == "__main__":
    run(__name__)
