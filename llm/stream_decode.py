"""Incremental decoder for a streamed JSON envelope.

The answer is a JSON object, but the user must see tokens immediately. This
walks the raw stream and emits only the value of the top-level ``text`` string
as it arrives — handling escapes and ``\\uXXXX`` sequences split across chunks.

If the provider ignores the schema and streams prose, we detect it early and
pass the raw text straight through (``mode == "passthrough"``), so a
non-compliant provider degrades to streamed plain text instead of a blank page.
"""
from __future__ import annotations

import re
from typing import Literal

KEY = re.compile(r'"text"\s*:\s*"')
ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class JsonTextStreamer:
    def __init__(self, detect_after: int = 40) -> None:
        self.buf = ""
        self.pos = 0
        self.emitted = 0
        self.mode: Literal["detect", "json", "passthrough", "done"] = "detect"
        self._pending_escape = ""
        self.text = ""

    # -- public ----------------------------------------------------------
    def feed(self, chunk: str) -> str:
        """Add a raw chunk, return the new decoded text (possibly empty)."""
        if not chunk or self.mode == "done":
            return ""
        self.buf += chunk
        if self.mode == "detect":
            self._detect()
        if self.mode == "passthrough":
            return self._passthrough_delta()
        if self.mode == "json":
            return self._scan_json()
        return ""

    def finish(self) -> str:
        """Flush whatever is left (truncated streams keep their partial text)."""
        if self.mode == "detect":
            # Never found a JSON envelope: everything is prose.
            self.mode = "passthrough"
            return self._passthrough_delta()
        if self.mode == "json":
            out = self._scan_json()
            self.mode = "done"
            return out
        out = self._passthrough_delta() if self.mode == "passthrough" else ""
        self.mode = "done"
        return out

    # -- internals -------------------------------------------------------
    def _detect(self) -> None:
        head = self.buf.lstrip()
        if len(head) < 1:
            return
        if head.startswith("{"):
            match = KEY.search(self.buf)
            if match:
                self.pos = match.end()
                self.mode = "json"
            elif len(self.buf) > 4000:  # a JSON object that never names `text`
                self.mode = "passthrough"
            return
        if head.startswith("```"):
            # fenced JSON block: find the brace and retry on the payload
            start = self.buf.find("{")
            if start == -1:
                if len(self.buf) > 200:
                    self.mode = "passthrough"
                return
            self.buf = self.buf[start:]
            self._detect()
            return
        if len(head) >= 1 and len(self.buf) >= 40:
            self.mode = "passthrough"
            return
        if len(self.buf) >= 40:
            self.mode = "passthrough"

    def _passthrough_delta(self) -> str:
        delta = self.buf[self.emitted:]
        self.emitted = len(self.buf)
        self.text += delta
        return delta

    def _scan_json(self) -> str:
        out: list[str] = []
        while self.pos < len(self.buf):
            char = self.buf[self.pos]
            if self._pending_escape:
                consumed = self._consume_escape()
                if consumed is None:  # incomplete escape: wait for more bytes
                    break
                if consumed:
                    out.append(consumed)
                continue
            if char == "\\":
                self._pending_escape = "\\"
                self.pos += 1
                continue
            if char == '"':
                self.mode = "done"
                break
            out.append(char)
            self.pos += 1
        text = "".join(out)
        self.text += text
        return text

    def _consume_escape(self) -> str | None:
        """Decode from ``self.pos`` (just after the backslash). None = need more input."""
        remaining = self.buf[self.pos:]
        if not remaining:
            return None
        head = remaining[0]
        if head == "u":
            hexdigits = remaining[1:5]
            if len(hexdigits) < 4:
                return None
            if not re.fullmatch(r"[0-9a-fA-F]{4}", hexdigits):
                self._pending_escape = ""
                self.pos += 1
                return ""
            self._pending_escape = ""
            self.pos += 5
            return chr(int(hexdigits, 16))
        self._pending_escape = ""
        self.pos += 1
        return ESCAPES.get(head, head)


def extract_text(raw: str) -> str | None:
    """One-shot helper used by tests and the non-streaming path."""
    streamer = JsonTextStreamer()
    parts = [streamer.feed(raw), streamer.finish()]
    return "".join(parts) if streamer.mode != "detect" else None
