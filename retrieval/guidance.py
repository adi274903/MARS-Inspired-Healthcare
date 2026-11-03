"""External guidance corpus: reporting guidelines, governance frameworks, norm papers.

Kept separate from the paper corpus on purpose:
* it is not counted in any CORPUS STATS number,
* it is cited as ``type: "web"`` (the UI renders it as "Guidance / law"),
* retrieval is deterministic keyword/IDF matching, so a pointer to TRIPOD+AI never
  depends on a vector index or on the model remembering a citation.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from core.config import ROOT

STOP = {
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "is", "are", "do", "does", "should",
    "what", "which", "how", "my", "our", "we", "i", "it", "with", "without", "that", "this", "be", "can",
    "use", "used", "using", "need", "needs", "required", "require", "have", "has", "any", "about", "if",
    "when", "where", "who", "why", "best", "good", "give", "tell", "me", "please", "would", "could",
}

_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-+]{1,}")


@dataclass
class GuidanceEntry:
    id: str
    kind: str = "reporting"
    name: str = ""
    keys: list[str] = field(default_factory=list)
    use_for: str = ""
    title: str = ""
    authors: str = ""
    venue: str = ""
    year: int | None = None
    url: str = ""
    pmid: str = ""
    doi: str = ""
    summary: str = ""

    def as_citation(self, claim: str | None = None) -> dict[str, Any]:
        citation: dict[str, Any] = {
            "type": "web",
            "title": self.title or self.name,
            "authors": self.authors or self.name,
            "venue": self.venue,
            "year": self.year,
            "url": self.url,
            "claim": claim or self.summary or self.use_for,
            "kind": self.kind,
        }
        # type stays "web" even when a PMID is known: guidance is not corpus evidence, so
        # the answer-service guard must never treat it as a paper citation.
        if self.pmid:
            citation["pmid"] = self.pmid
        if self.doi:
            citation["doi"] = self.doi
        return citation


def load(path: Path | None = None) -> list[GuidanceEntry]:
    path = Path(path or ROOT / "guidance" / "sources.yaml")
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text() or "") or {}
    entries: list[GuidanceEntry] = []
    for item in raw.get("sources") or []:
        if not item.get("id"):
            continue
        entries.append(
            GuidanceEntry(
                id=str(item["id"]),
                kind=str(item.get("kind", "reporting")),
                name=str(item.get("name", "")),
                keys=[str(k).lower() for k in (item.get("keys") or [])],
                use_for=str(item.get("use_for", "")),
                title=str(item.get("title", "")),
                authors=str(item.get("authors", "")),
                venue=str(item.get("venue", "")),
                year=item.get("year"),
                url=str(item.get("url", "")),
                pmid=str(item.get("pmid") or ""),
                doi=str(item.get("doi") or ""),
                summary=str(item.get("summary", "")),
            )
        )
    return entries


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOP]


class GuidanceIndex:
    """Deterministic scorer: alias hits dominate, then IDF-weighted token overlap."""

    def __init__(self, entries: Iterable[GuidanceEntry]) -> None:
        self.entries = list(entries)
        self._docs: list[set[str]] = []
        df: dict[str, int] = {}
        for entry in self.entries:
            blob = " ".join([entry.name, entry.use_for, entry.summary, entry.title, *entry.keys])
            toks = set(_tokens(blob))
            self._docs.append(toks)
            for tok in toks:
                df[tok] = df.get(tok, 0) + 1
        n = max(len(self.entries), 1)
        self._idf = {tok: math.log(1 + n / (1 + count)) for tok, count in df.items()}

    def __len__(self) -> int:
        return len(self.entries)

    def match(self, question: str, k: int = 2, min_score: float = 2.5) -> list[tuple[GuidanceEntry, float]]:
        low = question.lower()
        q_tokens = set(_tokens(question))
        scored: list[tuple[GuidanceEntry, float]] = []
        for entry, doc in zip(self.entries, self._docs):
            score = 0.0
            alias_hit = False
            for alias in entry.keys:
                if alias and alias in low:
                    alias_hit = True
                    score += 3.0 + 0.1 * len(alias.split())
            score += sum(self._idf.get(tok, 0.0) for tok in q_tokens & doc)
            # An explicit alias always qualifies; token overlap alone must clear a higher bar,
            # otherwise a corpus question ("... external validation ...") drags in guidance.
            if score >= min_score and (alias_hit or score >= 6.0):
                scored.append((entry, round(score, 3)))
        scored.sort(key=lambda item: (-item[1], item[0].id))
        return scored[:k]

    def prompt_block(self, matches: list[tuple[GuidanceEntry, float]]) -> str:
        if not matches:
            return ""
        lines = [
            "GUIDANCE (matched from a curated list of reporting guidelines, governance frameworks and norm papers; "
            "cite these with their own bracketed number when you name a framework):"
        ]
        for entry, _score in matches:
            detail = " — ".join(part for part in [entry.name, entry.use_for, entry.summary] if part)
            lines.append(f"- {detail} [{entry.year or ''}]".replace(" []", ""))
        return "\n".join(lines)


def default_index(path: Path | None = None) -> GuidanceIndex | None:
    entries = load(path)
    return GuidanceIndex(entries) if entries else None


def citations_for(matches: list[tuple[GuidanceEntry, float]], start: int = 0) -> list[dict[str, Any]]:
    """Guidance citations, numbered after the paper citations."""
    return [entry.as_citation() for entry, _score in matches]


def _main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Inspect the curated guidance corpus.")
    parser.add_argument("--path", default=None)
    parser.add_argument("--ask", default=None, help="show which entries a question matches")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    entries = load(Path(args.path) if args.path else None)
    index = GuidanceIndex(entries)
    if args.ask:
        matches = index.match(args.ask, k=4)
        for entry, score in matches:
            print(f"{score:6.2f}  {entry.id:18s} {entry.name} — {entry.use_for}")
        if not matches:
            print("no guidance matched")
        return 0
    if args.json:
        print(json.dumps([entry.__dict__ for entry in entries], indent=2, default=str))
        return 0
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.kind] = counts.get(entry.kind, 0) + 1
    print(f"{len(entries)} guidance entries: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for entry in entries:
        print(f"  {entry.kind:14s} {entry.id:18s} {entry.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
