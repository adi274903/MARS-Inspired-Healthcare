"""Guidance corpus: deterministic matching, citation shape, absence tolerance."""
from __future__ import annotations

from pathlib import Path

from retrieval import guidance


def test_loads_curated_entries():
    entries = guidance.load()
    assert len(entries) >= 20
    ids = [e.id for e in entries]
    assert len(ids) == len(set(ids))
    assert {e.kind for e in entries} >= {"reporting", "governance", "documentation", "evaluation"}
    assert all(e.url for e in entries), "every entry needs a stable link"


def test_alias_match_is_decisive():
    index = guidance.GuidanceIndex(guidance.load())
    top = index.match("which reporting guideline applies to a prediction model", k=1)
    assert top and top[0][0].id == "tripod-ai"
    top = index.match("how do I document a model for clinicians", k=1)
    assert top and top[0][0].id == "model-cards"
    top = index.match("is my CDS a medical device under FDA and the EU AI Act", k=2)
    assert [e.id for e, _ in top] == ["eu-ai-act", "fda-gmlp"]


def test_corpus_questions_do_not_pull_guidance():
    index = guidance.GuidanceIndex(guidance.load())
    assert index.match("sepsis external validation") == []
    assert index.match("how many oncology papers report external validation") == []


def test_citations_are_web_type_and_carry_claim():
    index = guidance.GuidanceIndex(guidance.load())
    matches = index.match("what reporting standard for a prediction model", k=1)
    cites = guidance.citations_for(matches)
    assert len(cites) == 1
    cite = cites[0]
    assert cite["type"] in {"web", "pubmed"}
    assert cite["title"] and cite["url"] and cite["claim"]


def test_prompt_block_lists_names():
    index = guidance.GuidanceIndex(guidance.load())
    block = index.prompt_block(index.match("LLM chatbot health advice reporting", k=2))
    assert "GUIDANCE" in block and "CHART" in block


def test_missing_file_is_tolerated(tmp_path: Path):
    missing = tmp_path / "nope.yaml"
    assert guidance.load(missing) == []
    assert guidance.default_index(missing) is None
