"""Markdown subset shaping and the JSON-envelope recovery ladder."""
from __future__ import annotations

import json

from retrieval.shape import APOLOGY, parse_envelope, sanitize_markdown, validate_answer


# ------------------------------------------------------------------ markdown
def test_sanitize_table_becomes_bullets():
    raw = "## Findings\n\n| system | tier |\n|---|---|\n| CAD | internal |\n| CDS | external |\n"
    out = sanitize_markdown(raw)
    assert "|" not in out
    assert "- system — tier" in out
    assert "- CAD — internal" in out
    assert "- CDS — external" in out
    assert "## Findings" in out


def test_sanitize_links_become_plain_urls():
    assert sanitize_markdown("see [WHO](https://who.int/x) now") == "see WHO (https://who.int/x) now"


def test_sanitize_drops_fences_keeps_code():
    out = sanitize_markdown("before\n```python\nprint('hi')\n```\nafter")
    assert "```" not in out
    assert "print('hi')" in out
    assert out.splitlines()[0] == "before"


def test_sanitize_demotes_deep_headings():
    assert sanitize_markdown("#### Deep") == "### Deep"
    assert sanitize_markdown("###### Deeper") == "### Deeper"
    assert sanitize_markdown("### Kept") == "### Kept"


def test_sanitize_drops_horizontal_rules_and_trailing_space():
    out = sanitize_markdown("a   \n---\nb\t\n***\nc")
    assert "---" not in out and "***" not in out
    assert out == "a\nb\nc"


def test_sanitize_collapses_blank_runs():
    assert sanitize_markdown("a\n\n\n\n\nb") == "a\n\nb"


def test_sanitize_keeps_citation_markers():
    assert sanitize_markdown("as shown [1] and [2]") == "as shown [1] and [2]"


def test_sanitize_handles_empty():
    assert sanitize_markdown("") == ""
    assert sanitize_markdown(None) == ""


# ------------------------------------------------------------------ envelope
MALFORMED = {
    "fenced_json": '```json\n{"text": "fenced answer", "checklist": null}\n```',
    "prose_wrapper": 'Sure! Here is the JSON you asked for:\n{"text": "prose wrapped", "claims": {"1": "c"}}\nHope this helps.',
    "python_literals": "{'text': 'python style', 'checklist': None, 'claims': {'1': 'c'}}",
    "unquoted_keys": '{text: "unquoted keys", claims: None, checklist: None}',
    "trailing_comma": '{"text": "trailing comma", "claims": {"1": "c",},}',
    "truncated_string": '{"text": "truncated answer that stops mid sentence',
    "dangling_key": '{"text": "dangling key", "checklist":',
    "python_caps": '{"text": "caps", "checklist": None, "claims": {"1": "c"}, "degraded": True}',
}


def test_parse_envelope_fenced_json():
    env = parse_envelope(MALFORMED["fenced_json"])
    assert env["text"] == "fenced answer"
    assert env["recovered"] is True


def test_parse_envelope_prose_wrapper():
    env = parse_envelope(MALFORMED["prose_wrapper"])
    assert env["text"] == "prose wrapped"
    assert env["claims"] == {"1": "c"}


def test_parse_envelope_python_literals():
    env = parse_envelope(MALFORMED["python_literals"])
    assert env["text"] == "python style"
    assert env["claims"] == {"1": "c"}


def test_parse_envelope_unquoted_keys():
    assert parse_envelope(MALFORMED["unquoted_keys"])["text"] == "unquoted keys"


def test_parse_envelope_trailing_comma():
    assert parse_envelope(MALFORMED["trailing_comma"])["text"] == "trailing comma"


def test_parse_envelope_truncated_keeps_partial_text():
    env = parse_envelope(MALFORMED["truncated_string"])
    assert env["text"] == "truncated answer that stops mid sentence"


def test_parse_envelope_dangling_key():
    assert parse_envelope(MALFORMED["dangling_key"])["text"] == "dangling key"


def test_parse_envelope_capitalised_literals():
    env = parse_envelope(MALFORMED["python_caps"])
    assert env["text"] == "caps"
    assert env["checklist"] is None


def test_parse_envelope_clean_json_round_trips():
    payload = {"text": "ok", "checklist": {"title": "T", "groups": []}, "claims": {"1": "c"}}
    env = parse_envelope(json.dumps(payload))
    assert env == {**payload, "recovered": True}


def test_parse_envelope_never_raises_and_flags_unrecovered():
    env = parse_envelope("just prose, no json anywhere")
    assert env == {"text": "just prose, no json anywhere", "checklist": None, "claims": {}, "recovered": False}
    for junk in ("", "{", "}{", "null", "[]", "'''"):
        assert isinstance(parse_envelope(junk), dict)


# ------------------------------------------------------------------ validate
def test_validate_answer_coerces_and_caps_checklist():
    payload = {
        "text": "  ## ok  ",
        "checklist": {
            "title": 7,
            "groups": [
                {"name": "g1", "items": [str(i) for i in range(20)]},
                {"name": "", "items": ["only item"]},
                {"bogus": "x"},
                {"name": "g4", "items": "not a list"},
                "junk",
                {"name": "g6", "items": ["a"]},
            ],
        },
        "claims": {"1": "claim", "2": 3, "3": {"nested": True}, "4": ""},
        "unknown": "dropped",
        "usage": {"total_tokens": 5},
        "degraded": True,
    }
    out = validate_answer(payload)
    assert set(out) == {"text", "checklist", "claims", "usage", "degraded"}
    assert out["text"] == "## ok"
    assert out["checklist"]["title"] == "7"
    assert len(out["checklist"]["groups"]) <= 4
    assert out["checklist"]["groups"][0]["items"] == [str(i) for i in range(8)]
    assert out["checklist"]["groups"][1] == {"name": "Checklist", "items": ["only item"]}
    assert out["claims"] == {"1": "claim", "2": "3"}
    assert out["usage"] == {"total_tokens": 5}
    assert out["degraded"] is True


def test_validate_answer_falls_back_on_empty_text():
    out = validate_answer({"text": "   "})
    assert out["text"] == APOLOGY
    assert out["checklist"] is None
    assert out["claims"] == {}


def test_validate_answer_survives_junk():
    out = validate_answer({"text": 42, "checklist": "nope", "claims": ["nope"]})
    assert out["text"] == "42"
    assert out["checklist"] is None
    assert out["claims"] == {}
