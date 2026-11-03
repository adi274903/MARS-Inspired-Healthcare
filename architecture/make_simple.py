#!/usr/bin/env python3
"""Generate the minimal EvalBot diagram in a light and a dark theme.

    python3 architecture/make_simple.py

Writes ``evalbot-architecture.svg`` (light) and ``evalbot-architecture-dark.svg``.
Both are the same geometry — six stops, one word each, real brand logos inlined from
``architecture/logos/`` — so no external references and identical rendering anywhere.
GitHub picks the right one through ``<picture>`` in the README; the HTML page toggles
them with a media query.
"""
from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGOS = HERE / "logos"
LIGHT = HERE / "evalbot-architecture.svg"
DARK = HERE / "evalbot-architecture-dark.svg"

W, H = 1440, 320
CARD_W, CARD_H, CARD_Y = 196, 150, 118
PITCH = CARD_W + 36
LEFT = 42

SANS = "Inter, 'DejaVu Sans', sans-serif"
SERIF = "C059, Newsreader, 'Bitstream Charter', Georgia, serif"
CHECK = "M 5 12.5 L 10 17.5 L 19 6.5"

THEMES = {
    "light": {
        "paper0": "#fbf9f4", "paper1": "#f3f0e8", "grid": "#e7e2d6",
        "ink": "#1d2523", "card": "#ffffff", "shadow": "#1d2523",
        "shadow1": "0.05", "shadow2": "0.035",
        "teal": "#0f766e", "blue": "#2f5d7c", "olive": "#556335", "plum": "#7a3f5c",
        "brand": "#1d2523",
    },
    "dark": {
        "paper0": "#151d1b", "paper1": "#0e1413", "grid": "#222d2a",
        "ink": "#e9edea", "card": "#1b2422", "shadow": "#000000",
        "shadow1": "0.45", "shadow2": "0.3",
        "teal": "#34b3a4", "blue": "#6ea8d0", "olive": "#93b06b", "plum": "#cd93b4",
        "brand": "#f2f5f3",
    },
}

# (logo, label, accent) — the three retrieval stores share one card
STOPS = [
    ("googlechrome", "EvalBot", "teal"),
    ("fastapi", "Gateway", "teal"),
    ("python", "Orchestrator", "blue"),
    (("sqlite", "qdrant", "neo4j"), "Retrieval", "olive"),
    ("google", "Gemma 4", "plum"),
    (None, "Answer", "plum"),  # check-mark glyph
]


def logo_path(name: str) -> str:
    raw = (LOGOS / f"{name}.svg").read_text(encoding="utf-8")
    match = re.search(r'<path[^>]*\sd="([^"]+)"', raw)
    if not match:
        raise SystemExit(f"no path in logos/{name}.svg")
    return match.group(1)


def badge(name: str | None, cx: float, cy: float, r: float, fill: str) -> str:
    """One brand mark (or the answer check-mark) in a filled circle."""
    if name is None:
        scale = (r * 1.08) / 24.0
        inner = (f'<path d="{CHECK}" fill="none" stroke="#ffffff" stroke-width="2.6" '
                 f'stroke-linecap="round" stroke-linejoin="round"/>')
    else:
        scale = (r * 1.02) / 24.0
        inner = f'<path d="{logo_path(name)}" fill="#ffffff"/>'
    return (f'  <circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{fill}"/>\n'
            f'  <g transform="translate({cx - 12 * scale:.2f},{cy - 12 * scale:.2f}) '
            f'scale({scale:.4f})">{inner}</g>\n')


def text(x, y, s, size, t, weight=600, fill=None, anchor="middle", family=SANS,
         spacing=0.0) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    fill = fill or t["ink"]
    extra = f' letter-spacing="{spacing}"' if spacing else ""
    return (f'  <text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}"{extra}>{s}</text>\n')


def build(theme: str = "light") -> str:
    t = THEMES[theme]
    out: list[str] = []
    add = out.append

    add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
        f'height="{H}" role="img" aria-label="EvalBot architecture: EvalBot, Gateway, '
        f'Orchestrator, Retrieval, Gemma 4, Answer">\n')
    add('  <title>EvalBot — the path of one question</title>\n')
    add('  <desc>Browser to gateway to orchestrator to retrieval to Gemma 4 to answer.</desc>\n')

    add('  <defs>\n'
        f'    <linearGradient id="paper" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{t["paper0"]}"/>'
        f'<stop offset="1" stop-color="{t["paper1"]}"/></linearGradient>\n'
        '    <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">'
        f'<path d="M 40 0 L 0 0 0 40" fill="none" stroke="{t["grid"]}" stroke-width="1"/>'
        '</pattern>\n'
        f'    <marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{t["ink"]}"/></marker>\n'
        '  </defs>\n')

    add(f'  <rect width="{W}" height="{H}" fill="url(#paper)"/>\n')
    add(f'  <rect width="{W}" height="{H}" fill="url(#grid)" opacity="0.6"/>\n')

    # header: brand mark + wordmark, co-brand on the right
    add(f'  <rect x="42" y="30" width="40" height="40" rx="12" fill="{t["teal"]}"/>\n')
    add('  <path d="M 50.3 50 H 73.7 M 62 38.3 V 61.7" stroke="#ffffff" stroke-width="4.6" '
        'stroke-linecap="round" fill="none"/>\n')
    add('  <rect x="36" y="22" width="1370" height="56" fill="none" data-bounds="1"/>\n')
    add(text(94, 62, "EvalBot", 28, t, weight=500, fill=t["brand"], anchor="start",
             family=SERIF, spacing=-0.5))
    add(text(1398, 58, "CMU · NIST AIMSEC", 10.5, t, fill=t["teal"], anchor="end"))

    # the six stops
    for i, (mark, label, accent) in enumerate(STOPS):
        colour = t[accent]
        x0 = LEFT + i * PITCH
        cx = x0 + CARD_W / 2
        add(f'  <rect x="{x0}" y="{CARD_Y + 2}" width="{CARD_W}" height="{CARD_H}" rx="16" '
            f'fill="{t["shadow"]}" fill-opacity="{t["shadow1"]}"/>\n')
        add(f'  <rect x="{x0}" y="{CARD_Y + 5}" width="{CARD_W}" height="{CARD_H}" rx="16" '
            f'fill="{t["shadow"]}" fill-opacity="{t["shadow2"]}"/>\n')
        add(f'  <rect x="{x0}" y="{CARD_Y}" width="{CARD_W}" height="{CARD_H}" rx="16" '
            f'fill="{t["card"]}" stroke="{colour}" stroke-opacity="0.45" stroke-width="1.5" '
            f'data-bounds="1"/>\n')
        if isinstance(mark, tuple):
            for name, dx in zip(mark, (-46, 0, 46)):
                add(badge(name, cx + dx, CARD_Y + 64, 17, colour))
        else:
            add(badge(mark, cx, CARD_Y + 64, 32, colour))
        add(text(cx, CARD_Y + 126, label, 14, t))

        if i < len(STOPS) - 1:  # arrow to the next stop
            ax = x0 + CARD_W + 4
            add(f'  <line x1="{ax}" y1="{CARD_Y + 75}" x2="{ax + 28}" y2="{CARD_Y + 75}" '
                f'stroke="{t["ink"]}" stroke-width="1.8" stroke-linecap="round" '
                f'stroke-opacity="0.85" marker-end="url(#ah)"/>\n')

    add('</svg>\n')
    return "".join(out)


def main() -> tuple[Path, Path]:
    for path, theme in ((LIGHT, "light"), (DARK, "dark")):
        svg = build(theme)
        path.write_text(svg, encoding="utf-8")
        print(f"wrote {path.name} ({len(svg) / 1024:.0f} kB, {theme})")
    return LIGHT, DARK


if __name__ == "__main__":
    main()
