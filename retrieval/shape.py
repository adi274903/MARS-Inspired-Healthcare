"""Output shaping.

The frontend renders a small markdown subset (``##``/``###`` headings, bullets,
numbered items, quotes, bold, italic, inline code). Everything else — tables,
links, fences, ``####`` — is rewritten into that subset here, so a non-compliant
model degrades into readable text instead of broken HTML.

The model's JSON envelope is also repaired here: streaming gets truncated, some
providers emit Python literals, others wrap JSON in prose. ``parse_envelope``
never raises; it loses as little as it can.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterator

APOLOGY = "Sorry — I could not compose an answer from the retrieved evidence. Please try rephrasing the question."

_TABLE_SEP = re.compile(r"^\|?[\s:\-|]+\|[\s:\-|]*$")
_LINK = re.compile(r"\[([^\]\n]+)\]\(\s*([^)\s]+)\s*\)")
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,}|(?:-\s+){2,}-)\s*$")
_TOO_DEEP_HEADING = re.compile(r"(?m)^(\s*)#{4,}\s*")
_TRAILING_WS = re.compile(r"[ \t]+$", re.M)
_BLANK_RUN = re.compile(r"\n{3,}")

_FENCE_LINE = re.compile(r"^\s*(?:```|~~~)")

_SINGLE_QUOTES = re.compile(r"'([^'\n]{0,300})'")
_UNQUOTED_KEY = re.compile(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_\-]*)(\s*:)")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_LITERALS = re.compile(r"\b(True|False|None)\b")
_LITERAL_MAP = {"True": "true", "False": "false", "None": "null"}


# ------------------------------------------------------------------ markdown
def _table_bullets(rows: list[str]) -> list[str]:
    out: list[str] = []
    for row in rows:
        if _TABLE_SEP.match(row):
            continue
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        cells = [cell for cell in cells if cell]
        if cells:
            out.append("- " + " — ".join(cells))
    return out


def sanitize_markdown(text: str) -> str:
    """Rewrite arbitrary model markdown into the frontend's subset."""
    if text is None:
        return ""
    raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return ""

    lines = raw.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if _FENCE_LINE.match(line):
            index += 1  # drop the fence marker, keep what is inside
            continue
        if stripped.count("|") >= 2:
            block: list[str] = []
            while index < len(lines) and lines[index].strip().count("|") >= 2:
                block.append(lines[index].strip())
                index += 1
            out.extend(_table_bullets(block))
            continue
        if _HR.match(line):
            index += 1
            continue
        out.append(line)
        index += 1

    body = "\n".join(out)
    body = _LINK.sub(lambda m: f"{m.group(1).strip()} ({m.group(2).strip()})", body)
    body = _TOO_DEEP_HEADING.sub(r"\1### ", body)
    body = _BLANK_RUN.sub("\n\n", body)
    body = _TRAILING_WS.sub("", body)
    return body.strip()


# ------------------------------------------------------------------ envelope
def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if not _FENCE_LINE.match(text.split("\n", 1)[0]):
        return raw
    text = _FENCE_LINE.sub("", text)
    if "```" in text:
        text = text.split("```", 1)[0]
    return text.strip()


def _repairs(src: str) -> str:
    text = _SINGLE_QUOTES.sub(lambda m: '"' + m.group(1) + '"', src)
    text = _UNQUOTED_KEY.sub(r'\1"\2"\3', text)
    text = _TRAILING_COMMA.sub(r"\1", text)
    text = _LITERALS.sub(lambda m: _LITERAL_MAP[m.group(1)], text)
    return text


def _close_truncated(src: str) -> str:
    """Bracket/string-balance a truncated JSON object."""
    text = src.strip()
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()

    if escaped:
        text = text + "\\"
    if in_string:
        text = text + '"'
    text = text.rstrip()
    while text.endswith(","):
        text = text[:-1].rstrip()
    if text.endswith(":"):
        text += " null"
    for opener in reversed(stack):
        text += "}" if opener == "{" else "]"
    return text


def _try_json(candidate: str) -> Any:
    for text in (candidate, _repairs(candidate)):
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            pass
    for base in (candidate, _repairs(candidate)):
        try:
            return json.loads(_close_truncated(base))
        except (ValueError, TypeError):
            pass
    return _REGEX_MISS


_REGEX_MISS = object()


def _candidates(raw: str) -> Iterator[str]:
    seen: set[str] = set()

    def fresh(value: str) -> bool:
        if not value or value in seen:
            return False
        seen.add(value)
        return True

    if fresh(raw):
        yield raw
    unfenced = _strip_fences(raw)
    if fresh(unfenced):
        yield unfenced
    for source in (raw, unfenced):
        start = source.find("{")
        if start < 0:
            continue
        end = source.rfind("}")
        if end > start:
            inner = source[start : end + 1]
            if fresh(inner):
                yield inner
        tail = source[start:]
        if fresh(tail):
            yield tail


_TEXT_KEY = re.compile(r'"?text"?\s*:\s*"')


def _salvage_text(raw: str) -> str:
    match = _TEXT_KEY.search(raw)
    if not match:
        return ""
    tail = raw[match.end() :]
    end = 0
    escaped = False
    while end < len(tail):
        char = tail[end]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            break
        end += 1
    fragment = tail[:end]
    try:
        return json.loads('"' + fragment + '"')
    except ValueError:
        return fragment.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def _coerce_claims(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, claim in value.items():
        if isinstance(claim, (dict, list, tuple, set)):
            continue
        text = str(claim).strip()
        if text:
            out[str(key)] = text
    return out


def _normalise(payload: dict[str, Any]) -> dict[str, Any]:
    text = payload.get("text")
    if isinstance(text, (list, tuple)):
        text = "\n".join(str(part) for part in text)
    text = "" if text is None else text if isinstance(text, str) else str(text)
    checklist = payload.get("checklist") if isinstance(payload.get("checklist"), dict) else None
    return {"text": text, "checklist": checklist, "claims": _coerce_claims(payload.get("claims"))}


def parse_envelope(raw: str) -> dict:
    """Recover ``{"text", "checklist", "claims"}`` from whatever arrived."""
    source = "" if raw is None else str(raw)
    for candidate in _candidates(source):
        parsed = _try_json(candidate)
        if parsed is _REGEX_MISS:
            continue
        if isinstance(parsed, dict):
            return {**_normalise(parsed), "recovered": True}
        if isinstance(parsed, str) and parsed.strip():
            return {"text": parsed.strip(), "checklist": None, "claims": {}, "recovered": True}

    salvaged = _salvage_text(source)
    if salvaged.strip():
        return {"text": _repairs(salvaged), "checklist": None, "claims": {}, "recovered": True}
    return {"text": source.strip(), "checklist": None, "claims": {}, "recovered": False}


# ------------------------------------------------------------------ validation
_MAX_GROUPS = 4
_MAX_ITEMS = 8


def _coerce_checklist(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    title = value.get("title")
    title = str(title).strip() if isinstance(title, (str, int, float)) else ""
    raw_groups = value.get("groups")
    groups: list[dict] = []
    if isinstance(raw_groups, list):
        for group in raw_groups[: _MAX_GROUPS * 2]:
            if len(groups) >= _MAX_GROUPS:
                break
            if not isinstance(group, dict):
                continue
            name = group.get("name")
            name = str(name).strip() if isinstance(name, (str, int, float)) else ""
            raw_items = group.get("items")
            items: list[str] = []
            if isinstance(raw_items, list):
                for item in raw_items:
                    if len(items) >= _MAX_ITEMS:
                        break
                    if isinstance(item, (str, int, float)) and str(item).strip():
                        items.append(str(item).strip())
            if not name and not items:
                continue
            groups.append({"name": name or "Checklist", "items": items})
    if not title and not groups:
        return None
    return {"title": title or "Checklist", "groups": groups}


def validate_answer(payload: dict) -> dict:
    """Guarantee the shape the frontend renders, coerce or drop the rest."""
    payload = payload if isinstance(payload, dict) else {}
    text = payload.get("text")
    if isinstance(text, (list, tuple)):
        text = "\n".join(str(part) for part in text)
    text = "" if text is None else text if isinstance(text, str) else str(text)
    text = sanitize_markdown(text)
    if not text.strip():
        text = APOLOGY

    out: dict[str, Any] = {
        "text": text,
        "checklist": _coerce_checklist(payload.get("checklist")),
        "claims": _coerce_claims(payload.get("claims")),
    }
    for key in ("usage", "degraded"):
        if key in payload:
            out[key] = payload[key]
    return out
