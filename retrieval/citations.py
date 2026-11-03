"""Deterministic citations.

The model only ever produces bracketed numbers. Everything the frontend shows
as a source — title, authors, venue, pmid, doi — comes from the evidence rows
the retrieval layer numbered, never from the model. Markers that point outside
the evidence list are dropped, and the remaining ones are renumbered by first
appearance so the answer can never cite a source that does not exist.
"""
from __future__ import annotations

import ast
import json
import re
from contextlib import suppress
from pathlib import Path
from typing import Any

MARKER = re.compile(r"\[(\d{1,3})\]")

_WS = re.compile(r"\s+")
_SPLIT_NAMES = re.compile(r"\s*[;,]\s*")
_CLAIM_LIMIT = 180


# ------------------------------------------------------------------ markers
def renumber(text: str, n_evidence: int) -> tuple[str, list[tuple[int, int]]]:
    """Keep only markers 1..n_evidence; renumber by order of first appearance.

    Returns ``(text, [(old, new), ...])`` where the map is in first-appearance
    order and repeats of a marker keep the number assigned the first time.
    """
    text = "" if text is None else str(text)
    try:
        limit = max(0, int(n_evidence))
    except (TypeError, ValueError):
        limit = 0

    assigned: dict[int, int] = {}
    order: list[tuple[int, int]] = []

    def _replace(match: re.Match[str]) -> str:
        old = int(match.group(1))
        if old < 1 or old > limit:
            return ""
        new = assigned.get(old)
        if new is None:
            new = len(assigned) + 1
            assigned[old] = new
            order.append((old, new))
        return f"[{new}]"

    return MARKER.sub(_replace, text), order


# ------------------------------------------------------------------ authors
def _as_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str):
        return [str(value).strip()] if str(value).strip() else []
    raw = value.strip()
    if not raw:
        return []
    parsed: Any = None
    if raw[0] in "[(":
        with suppress(Exception):
            parsed = json.loads(raw)
        if not isinstance(parsed, (list, tuple)):
            with suppress(Exception):
                parsed = ast.literal_eval(raw)
    if isinstance(parsed, (list, tuple)):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [name.strip().strip("\"'") for name in _SPLIT_NAMES.split(raw) if name.strip()]


def format_authors(value: Any) -> str:
    """``'["Ying Wang","Hong Wang"]'`` or ``"['Ying Wang']"`` -> readable names."""
    names = _as_names(value)
    if not names:
        return ""
    if len(names) > 4:
        return ", ".join(names[:3]) + ", et al."
    return ", ".join(names)


# ------------------------------------------------------------------ citations
def _clip(text: str, limit: int = _CLAIM_LIMIT) -> str:
    text = _WS.sub(" ", str(text)).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return cut + "..."


def _fallback_claim(evidence: dict[str, Any]) -> str:
    for key in ("summary", "validation_details", "abstract", "metrics_reported"):
        value = evidence.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value)
    return ""


def _venue(evidence: dict[str, Any]) -> str:
    journal = evidence.get("journal") or evidence.get("journal_abbr") or ""
    year = evidence.get("year")
    parts = [str(journal).strip(), str(year).strip() if year not in (None, "") else ""]
    return " ".join(part for part in parts if part).strip()


def _year(evidence: dict[str, Any]) -> int | None:
    raw = evidence.get("year")
    if raw is None or raw == "":
        return None
    try:
        return int(str(raw).strip()[:4])
    except (TypeError, ValueError):
        return None


def _doi(evidence: dict[str, Any]) -> str | None:
    raw = evidence.get("doi")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def build_citations(
    evidence: list[dict],
    markers: list[tuple[int, int]],
    claims: dict | None = None,
    guidance: list[dict] | None = None,
) -> list[dict]:
    """Map surviving markers onto evidence rows; append guidance entries."""
    evidence = evidence or []
    claims = claims if isinstance(claims, dict) else {}
    out: list[dict] = []
    for old, new in markers or []:
        index = int(old) - 1
        if index < 0 or index >= len(evidence):
            continue
        row = evidence[index] if isinstance(evidence[index], dict) else {}
        claim = claims.get(str(old)) or claims.get(str(new)) or _fallback_claim(row)
        out.append(
            {
                "type": "pubmed",
                "title": str(row.get("title") or "").strip(),
                "authors": format_authors(row.get("authors")),
                "venue": _venue(row),
                "year": _year(row),
                "pmid": str(row.get("pmid") or "").strip(),
                "doi": _doi(row),
                "claim": _clip(claim) if claim else "",
            }
        )
    for entry in guidance or []:
        if isinstance(entry, dict):
            out.append(entry)
    return out


def drop_unevidenced(citations: list[dict], evidence: list[dict]) -> tuple[list[dict], list[str]]:
    """Guard: a pubmed citation survives only if its pmid is in the evidence."""
    pmids = {str(row.get("pmid")).strip() for row in (evidence or []) if isinstance(row, dict) and row.get("pmid")}
    kept: list[dict] = []
    dropped: list[str] = []
    for citation in citations or []:
        if not isinstance(citation, dict):
            continue
        if citation.get("type") == "pubmed" and str(citation.get("pmid") or "").strip() not in pmids:
            dropped.append(str(citation.get("title") or citation.get("pmid") or "untitled"))
            continue
        kept.append(citation)
    return kept, dropped


# ------------------------------------------------------------------ guidance
def load_guidance(path: str | Path) -> list[dict]:
    """Optional curated guidance file; a missing or broken file means no guidance."""
    if path is None:
        return []
    try:
        path = Path(path)
        if not path.is_file():
            return []
        import yaml  # local import: guidance is optional, pyyaml may be absent

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("guidance") or data.get("entries") or []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def match_guidance(entries: list[dict], question: str) -> list[dict]:
    """Entries whose ``keys`` substring list matches the (lowercased) question."""
    if not entries or not question:
        return []
    haystack = str(question).lower()
    matched: list[dict] = []
    for entry in entries:
        keys = entry.get("keys") if isinstance(entry, dict) else None
        if not isinstance(keys, list):
            continue
        if any(str(key).lower() in haystack for key in keys if str(key).strip()):
            matched.append(entry)
    return matched
