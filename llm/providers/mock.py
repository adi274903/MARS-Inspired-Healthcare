"""Deterministic offline provider: streams a JSON envelope built from the evidence block.

This is what tests and GPU-less demos run on, and it exercises the whole
streaming/citation path for real.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator

from llm.base import Capabilities

EVIDENCE_LINE = re.compile(r"^(\d+)\.\s+pmid=(\S+)\s+\|\s+([^|]+)\|", re.M)
STATS_LINE = re.compile(r"^CORPUS STATS:[^\n]*", re.M)
QUESTION = re.compile(r"QUESTION:\s*(.+)", re.S)


def _evidence(messages: list[dict[str, str]]) -> list[tuple[int, str, str]]:
    blob = "\n".join(m.get("content", "") for m in messages)
    return [(int(n), pmid, title.strip()) for n, pmid, title in EVIDENCE_LINE.findall(blob)]


def _question(messages: list[dict[str, str]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            match = QUESTION.search(message.get("content", ""))
            if match:
                return match.group(1).strip().splitlines()[0][:200]
            return message.get("content", "").strip().splitlines()[0][:200]
    return ""


class MockProvider:
    name = "mock"

    def __init__(self, settings=None, profile: dict | None = None) -> None:
        self.settings = settings
        self.profile = profile or {}
        self.model = self.profile.get("model", "mock/gemma-4-26b-a4b-it")

    def capabilities(self) -> Capabilities:
        return Capabilities(context_window=16384, json_mode=True, streaming=True, price_per_1m_in=0.0, price_per_1m_out=0.0)

    async def verify(self) -> dict[str, Any]:
        return {"resolved": True, "model": self.model, "provider": self.name, "context_window": 16384}

    # -- answer construction ---------------------------------------------
    def render(self, messages: list[dict[str, str]]) -> str:
        evidence = _evidence(messages)
        question = _question(messages)
        stats = STATS_LINE.search("\n".join(m.get("content", "") for m in messages))
        lines: list[str] = []
        if stats:
            count = re.search(r"total=(\d+)", stats.group(0))
            filter_note = re.search(r"filters=(.+)$", stats.group(0))
            lines.append("## Corpus counts")
            lines.append("")
            lines.append(f"- **{count.group(1) if count else 'n/a'} papers** match your filters ({filter_note.group(1) if filter_note else 'none'}).")
            lines.append("")
        if evidence:
            lines.append("## What the extracted papers show")
            lines.append("")
            lines.append(f"{question or 'Your question'} — the strongest matches in the corpus are:")
            lines.append("")
            for n, _pmid, title in evidence[:6]:
                lines.append(f"- **{title}** [{n}]")
            lines.append("")
            lines.append("> These are extraction records, not full-text evidence. Verify any number against the source paper before quoting it.")
        elif not stats:
            lines.append("## No in-corpus evidence")
            lines.append("")
            lines.append("I could not find papers in the corpus that match this question, so I will not answer from general knowledge.")
        citations = {"claims": {str(n): f"Supports the point drawn from {title}." for n, _pmid, title in evidence[:6]}}
        checklist = {
            "title": "Follow-up checks",
            "groups": [
                {"name": "Evidence", "items": ["Open the cited papers and verify the extracted fields", "Check whether the cohort matches your intended-use population"]},
                {"name": "Next step", "items": ["Ask for a facet breakdown (year, domain, validation tier) to size the subgroup"]},
            ],
        }
        payload = {"text": "\n".join(lines), "checklist": checklist, "claims": citations["claims"]}
        return json.dumps(payload)

    async def complete(self, messages, *, schema=None, temperature=0.2, max_tokens=1200) -> str:
        return self.render(messages)

    async def stream(self, messages, *, schema=None, temperature=0.2, max_tokens=1200) -> AsyncIterator[str]:
        text = self.render(messages)
        for start in range(0, len(text), 37):
            await asyncio.sleep(0)
            yield text[start:start + 37]
