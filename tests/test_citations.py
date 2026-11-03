"""Citation determinism: numbers come from the evidence, never from the model."""
from __future__ import annotations

import json
from pathlib import Path

from retrieval import citations
from retrieval.citations import (
    MARKER,
    build_citations,
    drop_unevidenced,
    format_authors,
    load_guidance,
    match_guidance,
    renumber,
)
from tests.support import fixture_rows


def _rows() -> list[dict]:
    return fixture_rows()[:3]


# ------------------------------------------------------------------ renumber
def test_marker_pattern():
    assert MARKER.findall("see [1], [22] and [1234]") == ["1", "22"]


def test_renumber_keeps_repeats_and_orders_by_first_appearance():
    text, markers = renumber("B [2] then [1] then [2] again [1].", 3)
    assert markers == [(2, 1), (1, 2)]
    assert text == "B [1] then [2] then [1] again [2]."


def test_renumber_drops_out_of_range():
    text, markers = renumber("ok [1] bogus [7] zero [0]", 2)
    assert markers == [(1, 1)]
    assert "[7]" not in text and "[0]" not in text
    assert text.startswith("ok [1] bogus")
    assert renumber("no markers", 4) == ("no markers", [])


def test_renumber_no_evidence_drops_everything():
    assert renumber("a [1] b [2]", 0) == ("a  b ", [])


# ------------------------------------------------------------------ authors
def test_format_authors_json_list():
    assert format_authors('["Ying Wang","Hong Wang","Hui Guo"]') == "Ying Wang, Hong Wang, Hui Guo"


def test_format_authors_python_repr_list():
    assert format_authors("['Ying Wang', 'Hong Wang', 'Hui Guo']") == "Ying Wang, Hong Wang, Hui Guo"


def test_format_authors_plain_and_native_list():
    assert format_authors("Ying Wang, Hong Wang") == "Ying Wang, Hong Wang"
    assert format_authors(["Ying Wang", "Hong Wang"]) == "Ying Wang, Hong Wang"


def test_format_authors_et_al_beyond_four():
    assert format_authors('["A One","B Two","C Three","D Four","E Five"]') == "A One, B Two, C Three, et al."


def test_format_authors_never_raises_on_junk():
    for junk in (None, "", "[]", 42, {"a": 1}, "[not json", "['unclosed", "'", None):
        assert isinstance(format_authors(junk), str)


# ------------------------------------------------------------------ build
def test_build_citations_maps_markers_to_evidence():
    rows = _rows()
    built = build_citations(rows, [(1, 1), (3, 2)])
    assert [c["pmid"] for c in built] == [rows[0]["pmid"], rows[2]["pmid"]]
    assert built[0]["type"] == "pubmed"
    assert built[0]["title"] == rows[0]["title"]
    assert built[0]["authors"] == "Ying Wang, Hong Wang, Hui Guo"
    assert built[0]["year"] == rows[0]["year"]
    assert rows[0]["journal"] in built[0]["venue"]
    assert built[0]["doi"] == rows[0]["doi"]


def test_build_citations_claim_prefers_model_claims_then_fallback():
    rows = _rows()
    built = build_citations(rows, [(1, 1), (2, 2)], {"1": "model claim for one"})
    assert built[0]["claim"] == "model claim for one"
    assert built[1]["claim"]
    assert built[1]["claim"] != "model claim for one"
    assert len(built[1]["claim"]) <= 183


def test_build_citations_honours_new_number_claims():
    rows = _rows()
    built = build_citations(rows, [(2, 1)], {"1": "claim keyed by new number"})
    assert built[0]["claim"] == "claim keyed by new number"


def test_build_citations_skips_missing_index_and_appends_guidance():
    rows = _rows()
    guidance = [{"type": "web", "title": "WHO guidance", "url": "https://example.org"}]
    built = build_citations(rows, [(1, 1), (99, 2)], None, guidance)
    assert len(built) == 2
    assert built[0]["type"] == "pubmed"
    assert built[1] == guidance[0]


# ------------------------------------------------------------------ drop
def test_drop_unevidenced_keeps_only_known_pmids():
    rows = _rows()
    citations_in = [
        {"type": "pubmed", "pmid": rows[0]["pmid"], "title": "kept"},
        {"type": "pubmed", "pmid": "99999999", "title": "invented"},
        {"type": "web", "title": "guidance"},
    ]
    kept, dropped = drop_unevidenced(citations_in, rows)
    assert [c["title"] for c in kept] == ["kept", "guidance"]
    assert dropped == ["invented"]


# ------------------------------------------------------------------ guidance
def test_load_guidance_missing_file_is_empty(tmp_path: Path):
    assert load_guidance(tmp_path / "nope.yaml") == []


def test_load_guidance_reads_yaml(tmp_path: Path):
    path = tmp_path / "guidance.yaml"
    path.write_text(
        "- type: web\n  title: WHO\n  keys: [deployment, workflow]\n",
        encoding="utf-8",
    )
    entries = load_guidance(path)
    assert len(entries) == 1 and entries[0]["title"] == "WHO"


def test_match_guidance_by_lowercase_substring():
    entries = [
        {"type": "web", "title": "WHO", "keys": ["deployment", "Workflow"]},
        {"type": "web", "title": "FDA", "keys": ["regulation"]},
    ]
    assert [e["title"] for e in match_guidance(entries, "How is DEPLOYMENT handled?")] == ["WHO"]
    assert [e["title"] for e in match_guidance(entries, "workflow and regulation")] == ["WHO", "FDA"]
    assert match_guidance(entries, "") == []
    assert match_guidance([], "anything") == []


def test_build_citations_is_json_serialisable():
    built = build_citations(_rows(), [(1, 1), (2, 2)])
    json.dumps(built)
    assert citations.format_authors(None) == ""
