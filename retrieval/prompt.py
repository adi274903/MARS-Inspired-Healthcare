"""Prompt assembly: evidence-only answering, exact numbers, frontend-shaped output."""
from __future__ import annotations

import json
from typing import Any

from retrieval.queryparse import ParsedQuery

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "checklist": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "items": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name", "items"],
                    },
                },
            },
            "required": ["title", "groups"],
        },
        "claims": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["text"],
    "propertyOrdering": ["text", "checklist", "claims"],
}

SYSTEM = """You are EvalBot, an evidence assistant for a scoping review of clinical AI papers.

Rules:
1. Answer ONLY from the numbered EVIDENCE records and the CORPUS STATS block. Never use outside knowledge for factual claims.
2. Cite with bracketed numbers that match the evidence list, like [1] or [2][3]. Never cite a number that is not in the list.
3. If the evidence does not support an answer, say so plainly in one sentence and stop. Do not guess.
4. If CORPUS STATS is present, quote those numbers exactly as given. Do not count evidence records yourself.
5. These are LLM extraction records, not verified ground truth. Never present an extracted field as a confirmed finding without that caveat.
6. Output a single JSON object: {"text": markdown, "checklist": {...}, "claims": {"1": "what source 1 supports"}}.
7. The markdown "text" may use only: "## " and "### " headings, "- " bullets, "1. " numbered items, "> " quotes, **bold**, *italic*, `code`. No tables, no links, no code fences.
8. Keep "text" under 350 words. Put actionable, checkable items in the checklist instead of prose."""

TRUST_NOTES = """Field-trust caveats from a 40-paper, 27-field verification pass:
- confidence_intervals_reported and training dataset_size fields were wrong in ~70% of checked rows: never assert them.
- metrics_reported was accurate ~70% of the time; external validation ~85%.
- calibration_reported, deployed, rct and the per-paper summary were accurate 95-100%.
Say "the extraction reports ..." rather than "the paper shows ..."."""

MARKDOWN_RULES = (
    "Format reminder: '## ' headings, '- ' bullets, '1. ' numbered items, '> ' quote, **bold**, *italic*. "
    "No tables, no markdown links, no code fences."
)


def format_evidence(rows: list[dict[str, Any]], *, max_abstract_chars: int = 900) -> str:
    """Numbered evidence block. The numbering is the citation contract."""
    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        venue = " ".join(part for part in [row.get("journal") or row.get("journal_abbr") or "", str(row.get("year") or "")] if part).strip()
        facets = " | ".join(
            f"{key}={row.get(column)}"
            for key, column in (
                ("domain", "a_domain"), ("user", "b_end_user"), ("use_case", "c_use_case"),
                ("analytics", "d_analytics"), ("formulation", "e_formulation"),
                ("algorithm", "f_algorithm"), ("tier", "g_eval_tier"),
            )
            if row.get(column)
        )
        lines.append(f"{index}. pmid={row.get('pmid', '')} | {row.get('title', '').strip()} | {venue} | {facets}")
        summary = (row.get("summary") or "").strip()
        if summary:
            lines.append(f"   extraction summary: {summary[:400]}")
        details = (row.get("validation_details") or "").strip()
        if details:
            lines.append(f"   validation details: {details[:300]}")
        metrics = (row.get("metrics_reported") or "").strip()
        if metrics:
            lines.append(f"   metrics: {metrics[:200]}")
        abstract = (row.get("abstract") or "").strip()
        if abstract:
            lines.append(f"   abstract: {abstract[:max_abstract_chars]}")
    return "\n".join(lines)


def format_stats(stats: dict[str, Any] | None) -> str:
    if not stats:
        return ""
    parts = [f"total={stats.get('total')}"]
    if stats.get("filters"):
        parts.append("filters=" + json.dumps(stats["filters"], separators=(",", ":")))
    if stats.get("flags"):
        parts.append("flags=" + json.dumps(stats["flags"], separators=(",", ":")))
    if stats.get("year_min") or stats.get("year_max"):
        parts.append(f"years={stats.get('year_min') or ''}-{stats.get('year_max') or ''}")
    head = "CORPUS STATS: " + " ".join(parts)
    rows = stats.get("rows") or []
    if rows and stats.get("group_by"):
        body = "; ".join(f"{r['key']}={r['n']}" for r in rows[:20])
        head += f"\nCORPUS STATS breakdown by {stats['group_by']}: {body}"
    return head


def build_messages(
    question: str,
    rows: list[dict[str, Any]],
    *,
    stats: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    parsed: ParsedQuery | None = None,
    max_abstract_chars: int = 900,
    history: list[dict[str, str]] | None = None,
    guidance: str = "",
) -> list[dict[str, str]]:
    context = context or {}
    framing = [f"{label}: {value}" for label, value in (
        ("System type", context.get("system")), ("Stage", context.get("stage")),
        ("Region", context.get("region")),
    ) if value]
    focus = context.get("focus") or []
    if focus:
        framing.append("Focus areas: " + ", ".join(str(f) for f in focus))
    if parsed:
        framing.append(f"Query route: {parsed.intent}")
        if parsed.filters:
            framing.append("Applied facet filters: " + json.dumps(parsed.filters, separators=(",", ":")))

    user_parts = []
    if framing:
        user_parts.append("TAILORING:\n" + "\n".join(f"- {line}" for line in framing))
    stats_block = format_stats(stats)
    if stats_block:
        user_parts.append(stats_block)
    if rows:
        user_parts.append("EVIDENCE:\n" + format_evidence(rows, max_abstract_chars=max_abstract_chars))
    else:
        user_parts.append("EVIDENCE: (none retrieved)")
    if guidance:
        user_parts.append(guidance)
    user_parts.append(TRUST_NOTES)
    user_parts.append(f"QUESTION: {question}")
    user_parts.append(MARKDOWN_RULES)
    user_parts.append("Respond with the JSON object only.")

    messages = [{"role": "system", "content": SYSTEM}]
    for turn in (history or [])[-4:]:
        messages.append({"role": turn.get("role", "user"), "content": str(turn.get("text", ""))[:2000]})
    messages.append({"role": "user", "content": "\n\n".join(user_parts)})
    return messages
