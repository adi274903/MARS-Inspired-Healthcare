#!/usr/bin/env python3
"""Build the EvalBot deck from scratch (not a template): dark brand system, 16:9.

Design rules
  * ink background #0B1014, teal accent #38BDA8, amber #E8B44A for numbers
  * one idea per slide, kicker + headline, body >= 15pt, captions 11pt
  * screenshots sit on a bordered card on the right; text never runs under it
"""
from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

INK = RGBColor(0x0B, 0x10, 0x14)
PANEL = RGBColor(0x14, 0x1D, 0x24)
LINE = RGBColor(0x24, 0x33, 0x3D)
FG = RGBColor(0xE8, 0xEE, 0xF3)
DIM = RGBColor(0x9B, 0xAA, 0xB5)
ACCENT = RGBColor(0x38, 0xBD, 0xA8)
AMBER = RGBColor(0xE8, 0xB4, 0x4A)
FONT = "DejaVu Sans"

W, H = Inches(13.333), Inches(7.5)
SHOTS = Path("deck/shots")


def new_slide(prs):
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, W, H)
    bg.fill.solid(); bg.fill.fore_color.rgb = INK; bg.line.fill.background()
    bg.shadow.inherit = False
    return slide


def textbox(slide, x, y, w, h, *, size=16, color=FG, bold=False, align=PP_ALIGN.LEFT,
            line=1.25, space_after=0, anchor=MSO_ANCHOR.TOP):
    box = slide.shapes.add_textbox(x, y, w, h)
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = anchor
    frame.margin_left = frame.margin_right = 0
    frame.margin_top = frame.margin_bottom = 0
    para = frame.paragraphs[0]
    para.alignment = align
    para.line_spacing = line
    para.space_after = Pt(space_after)
    return box, frame, para


def write(slide, x, y, w, h, text, **kw):
    box, frame, para = textbox(slide, x, y, w, h, **kw)
    run = para.add_run(); run.text = text
    run.font.size = Pt(kw.get("size", 16)); run.font.bold = kw.get("bold", False)
    run.font.color.rgb = kw.get("color", FG); run.font.name = FONT
    return box


def kicker(slide, label, x=Inches(0.8), y=Inches(0.55)):
    write(slide, x, y, Inches(8), Inches(0.3), label.upper(), size=12, color=ACCENT, bold=True)
    rule = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y + Inches(0.34), Inches(0.62), Pt(2.2))
    rule.fill.solid(); rule.fill.fore_color.rgb = ACCENT; rule.line.fill.background()
    rule.shadow.inherit = False


def headline(slide, text, y=Inches(1.0), w=Inches(11.8), size=32):
    write(slide, Inches(0.8), y, w, Inches(1.0), text, size=size, bold=True)


def bullets(slide, items, *, x=Inches(0.8), y=Inches(2.1), w=Inches(11.6), size=15, gap=10):
    box = slide.shapes.add_textbox(x, y, w, H - y - Inches(0.9))
    frame = box.text_frame
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = 0
    first = True
    for item in items:
        if isinstance(item, tuple):
            lead, rest = item
        else:
            lead, rest = "", item
        para = frame.paragraphs[0] if first else frame.add_paragraph()
        first = False
        para.line_spacing = 1.3
        para.space_after = Pt(gap)
        dot = para.add_run(); dot.text = "—  "
        dot.font.size = Pt(size); dot.font.color.rgb = ACCENT; dot.font.bold = True; dot.font.name = FONT
        if lead:
            head = para.add_run(); head.text = lead
            head.font.size = Pt(size); head.font.bold = True; head.font.color.rgb = FG; head.font.name = FONT
        body = para.add_run(); body.text = rest
        body.font.size = Pt(size); body.font.color.rgb = DIM if lead else FG; body.font.name = FONT
    return box


def panel(slide, x, y, w, h, fill=PANEL, border=LINE):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    shape.fill.solid(); shape.fill.fore_color.rgb = fill
    shape.line.color.rgb = border; shape.line.width = Pt(0.75)
    shape.shadow.inherit = False
    shape.adjustments[0] = 0.04
    return shape


def image_card(slide, path, x, y, h, caption=None, border=True):
    from PIL import Image

    src = Path(path)
    with Image.open(src) as img:
        ratio = img.width / img.height
    w = Emu(int(h * ratio))
    card = panel(slide, x - Inches(0.06), y - Inches(0.06), w + Inches(0.12), h + Inches(0.12))
    if not border:
        card.fill.background(); card.line.fill.background()
    slide.shapes.add_picture(str(src), x, y, width=w, height=h)
    if caption:
        write(slide, x, y + h + Inches(0.12), w, Inches(0.4), caption, size=11, color=DIM)
    return w


def stat(slide, x, y, w, number, label, sub="", color=AMBER):
    panel(slide, x, y, w, Inches(1.85))
    write(slide, x + Inches(0.28), y + Inches(0.22), w - Inches(0.5), Inches(0.7), number, size=34, bold=True, color=color)
    write(slide, x + Inches(0.28), y + Inches(0.95), w - Inches(0.5), Inches(0.35), label, size=13, bold=True)
    if sub:
        write(slide, x + Inches(0.28), y + Inches(1.32), w - Inches(0.5), Inches(0.4), sub, size=11, color=DIM)


def chip(slide, x, y, w, h, label, color=ACCENT, fill=PANEL):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    shape.fill.solid(); shape.fill.fore_color.rgb = fill
    shape.line.color.rgb = color; shape.line.width = Pt(1)
    shape.shadow.inherit = False
    shape.text_frame.word_wrap = True
    para = shape.text_frame.paragraphs[0]; para.alignment = PP_ALIGN.CENTER
    run = para.add_run(); run.text = label
    run.font.size = Pt(12); run.font.bold = True; run.font.color.rgb = color; run.font.name = FONT
    return shape


def footer(slide, text):
    write(slide, Inches(0.8), H - Inches(0.55), Inches(11.7), Inches(0.3), text, size=10, color=RGBColor(0x6B, 0x7C, 0x88))


def build(out: Path) -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H

    # 1 ─ title
    s = new_slide(prs)
    band = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, W, Pt(6))
    band.fill.solid(); band.fill.fore_color.rgb = ACCENT; band.line.fill.background(); band.shadow.inherit = False
    image_card(s, SHOTS / "07-banner-dark.png", Inches(0.8), Inches(0.75), Inches(1.55), border=False)
    write(s, Inches(0.8), Inches(2.85), Inches(11.5), Inches(1.6),
          "An evaluation assistant\nfor clinical AI", size=48, bold=True, line=1.08)
    write(s, Inches(0.8), Inches(4.65), Inches(9.6), Inches(1.0),
          "Ask what to evaluate for a clinical model. It answers with a cited evaluation plan, "
          "a checklist you can tick off, and the standard that applies.", size=18, color=DIM, line=1.35)
    chair = chip(s, Inches(0.8), Inches(5.95), Inches(3.05), Inches(0.42), "CMU · NIST AIMSEC")
    chip(s, Inches(4.0), Inches(5.95), Inches(2.25), Inches(0.42), "Gemma 4 26B-A4B")
    chip(s, Inches(6.4), Inches(5.95), Inches(2.6), Inches(0.42), "15,954 papers")
    footer(s, "github.com/adi274903/MARS-Healthcare   ·   live at localhost:8080")

    # 2 ─ problem
    s = new_slide(prs)
    kicker(s, "the gap")
    headline(s, "Evaluation is scattered. Claims go out unaudited.", size=30)
    bullets(s, [
        ("Reporting guidance is splintered. ", "TRIPOD+AI, PROBAST+AI, CONSORT-AI, DECIDE-AI, CLAIM, CHART."),
        ("Governance sits elsewhere. ", "NIST AI RMF, FUTURE-AI, WHO, FDA GMLP/PCCP/SaMD, CHAI, EU AI Act, ISO/IEC 42001."),
        ("The evidence of what the field actually did ", "is buried in 15,954 papers nobody has time to read."),
        ("So every team invents its own checklist, ", "and a model card becomes an act of memory rather than a citation."),
    ], y=Inches(2.3), size=16, gap=16)
    footer(s, "the problem is retrieval + synthesis, not a lack of knowledge")

    # 3 ─ what it does (4 cards)
    s = new_slide(prs)
    kicker(s, "what it does")
    headline(s, "Four things, on every question", size=30)
    cards = [
        ("01", "Plans", "Turns \u201cshould we trust this model?\u201d into concrete, checkable evaluation items."),
        ("02", "Cites", "Every item points at a PubMed or DOI record, with the claim it supports attached."),
        ("03", "Names the standard", "TRIPOD+AI, PROBAST+AI, FDA SaMD, EU AI Act, NIST AI RMF — matched, not guessed."),
        ("04", "Counts exactly", "Corpus totals come from SQL, never from the model's arithmetic."),
    ]
    for i, (num, title, body) in enumerate(cards):
        col, row = i % 2, i // 2
        x = Inches(0.8) + col * Inches(6.15)
        y = Inches(2.15) + row * Inches(2.35)
        panel(s, x, y, Inches(5.75), Inches(2.05))
        write(s, x + Inches(0.3), y + Inches(0.22), Inches(1.0), Inches(0.4), num, size=22, bold=True, color=ACCENT)
        write(s, x + Inches(0.3), y + Inches(0.7), Inches(5.1), Inches(0.4), title, size=19, bold=True)
        write(s, x + Inches(0.3), y + Inches(1.18), Inches(5.15), Inches(0.8), body, size=13, color=DIM, line=1.25)

    # 4 ─ pipeline
    s = new_slide(prs)
    kicker(s, "how one question flows")
    headline(s, "Routed by intent before a single token is generated", size=28)
    steps = [("ask", "system type\nstage · region"), ("route", "aggregate · faceted\nsemantic · multihop"),
             ("retrieve", "SQL · BM25\nvectors · graph"), ("compose", "evidence + stats\n+ guidance"), ("answer", "streamed, cited\nor refused")]
    x = Inches(0.8)
    for i, (label, detail) in enumerate(steps):
        chip(s, x, Inches(2.3), Inches(2.05), Inches(0.55), label.upper())
        write(s, x, Inches(3.0), Inches(2.05), Inches(0.9), detail, size=11, color=DIM, align=PP_ALIGN.CENTER, line=1.25)
        if i < len(steps) - 1:
            arrow = s.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, x + Inches(2.1), Inches(2.43), Inches(0.3), Inches(0.26))
            arrow.fill.solid(); arrow.fill.fore_color.rgb = LINE; arrow.line.fill.background(); arrow.shadow.inherit = False
        x += Inches(2.45)
    panel(s, Inches(0.8), Inches(4.3), Inches(11.7), Inches(2.1))
    write(s, Inches(1.1), Inches(4.55), Inches(11.1), Inches(0.35), "WHY THE ROUTER MATTERS", size=11, bold=True, color=ACCENT)
    bullets(s, [
        ("\u201cHow many papers\u2026\u201d never reaches the model. ", "It is a SQL aggregate, quoted verbatim."),
        ("A question about a regulation ", "pulls guidance, not an arbitrary paper."),
    ], x=Inches(1.1), y=Inches(4.95), w=Inches(11.0), size=14, gap=8)

    # 5-9 ─ demo
    demo = [
        ("01-welcome.jpg", "demo · 1", "Start from a clinical system",
         "Pick system type, stage and region; ask in plain language. The UI shows how many papers the answer stands on."),
        ("02-eval-plan.jpg", "demo · 2", "It plans the evaluation, live",
         "Performance metrics, validation frameworks, interpretability — generated as you watch, with citations appearing inline."),
        ("03-checklist.jpg", "demo · 3", "The deliverable: a checklist",
         "10 items across metrics, validation and interpretability. Tick them off, copy as Markdown, or download the file."),
        ("04-sources.jpg", "demo · 4", "Every item is traceable",
         "PubMed records with PMID and DOI, and a GUIDANCE / LAW card for the standard that applies — here TRIPOD+AI."),
        ("05-aggregate.jpg", "demo · 5", "Counts come from SQL",
         "\u201cHow many papers report external validation?\u201d → 4,655, with a year breakdown. Not a guess."),
    ]
    for shot, kick, title, caption in demo:
        s = new_slide(prs)
        kicker(s, kick)
        headline(s, title, w=Inches(6.3), size=28)
        write(s, Inches(0.8), Inches(2.15), Inches(5.9), Inches(2.6), caption, size=15, color=DIM, line=1.4)
        image_card(s, SHOTS / shot, Inches(7.35), Inches(1.15), Inches(5.35))
        footer(s, "live capture from localhost:8080 · Gemma 4 26B-A4B via OpenRouter")

    # 10 ─ numbers
    s = new_slide(prs)
    kicker(s, "what is inside")
    headline(s, "Numbers a reviewer can check", size=30)
    stat(s, Inches(0.8), Inches(2.2), Inches(2.8), "15,954", "papers", "7 extraction axes")
    stat(s, Inches(3.75), Inches(2.2), Inches(2.8), "4,655", "externally validated", "flag, not text match")
    stat(s, Inches(6.7), Inches(2.2), Inches(2.8), "24", "frameworks", "8 reporting · 6 governance")
    stat(s, Inches(9.65), Inches(2.2), Inches(2.8), "124", "offline tests", "no network in CI")
    bullets(s, [
        ("Cross-store consistency: ", "15,954 rows in JSONL and SQLite, 15,954 papers in the graph (100,185 authors, 6,068 MeSH terms)."),
        ("Flags reproduce exactly: ", "external 4,655 · temporal 1,352 · internal 14,738 · deployed 628 · RCT 368 · CI 8,348."),
    ], y=Inches(4.4), size=15, gap=12)
    footer(s, "numbers printed by the build, not remembered")

    # 11 ─ architecture
    s = new_slide(prs)
    kicker(s, "how it is built")
    headline(s, "Nine services, one event bus", size=30)
    # the detail diagram has a light background: give it a light card so it reads as a figure
    from PIL import Image as _Image

    px, py, ph = Inches(3.9), Inches(1.85), Inches(4.95)
    with _Image.open(SHOTS / "06-architecture.png") as _img:
        pw = Emu(int(ph * _img.width / _img.height))
    panel(s, px - Inches(0.14), py - Inches(0.14), pw + Inches(0.28), ph + Inches(0.28),
          fill=RGBColor(0xF3, 0xF7, 0xF8), border=LINE)
    s.shapes.add_picture(str(SHOTS / "06-architecture.png"), px, py, width=pw, height=ph)
    bullets(s, [
        ("SQLite ", "is the system of record: 15,954 rows, FTS5, every exact count."),
        ("Qdrant ", "holds 768-d EmbeddingGemma-2 vectors; Neo4j holds the paper graph."),
        ("Both are derived: ", "rebuildable from the JSONL, and the answer still works if either is down."),
    ], x=Inches(0.8), y=Inches(2.2), w=Inches(2.9), size=13, gap=12)
    footer(s, "same code runs as one process (MONOLITH=1) or nine over Redis Streams")

    # 12 ─ three columns
    s = new_slide(prs)
    kicker(s, "the parts that matter")
    headline(s, "Three stores, one rule: the record is the source of truth", size=26)
    cols = [
        ("SQLite", "system of record", "15,954 rows · FTS5 BM25 · every exact count and facet filter. Read-only at request time."),
        ("Qdrant", "dense recall", "768-d EmbeddingGemma-2 with asymmetric query/document prompts; bf16 on an 8 GB GPU."),
        ("Neo4j", "multi-hop graph", "papers, authors, journals, MeSH. Named Cypher templates only — with a SQLite fallback."),
    ]
    for i, (name, role, body) in enumerate(cols):
        x = Inches(0.8) + i * Inches(3.95)
        panel(s, x, Inches(2.2), Inches(3.6), Inches(3.1))
        write(s, x + Inches(0.28), Inches(2.45), Inches(3.0), Inches(0.4), name, size=20, bold=True, color=ACCENT)
        write(s, x + Inches(0.28), Inches(2.9), Inches(3.0), Inches(0.3), role.upper(), size=10, bold=True, color=DIM)
        write(s, x + Inches(0.28), Inches(3.3), Inches(3.05), Inches(1.9), body, size=13, color=FG, line=1.3)
    footer(s, "Qdrant and Neo4j are derived and rebuildable — SQLite stays authoritative")

    # 13 ─ honesty
    s = new_slide(prs)
    kicker(s, "the safety behaviour")
    headline(s, "It refuses more than a chatbot does", size=30)
    bullets(s, [
        ("No invented citations. ", "Citations are built from retrieved records; markers outside the evidence are dropped."),
        ("No invented numbers. ", "Corpus statistics are SQL results quoted verbatim."),
        ("No false authority. ", "Extraction fields are labelled unverified — 40-paper audit found CI fields wrong ~70% of the time."),
        ("No silent failure. ", "A missing leg is reported as degraded; a missing model returns a readable error."),
    ], size=16, gap=16)
    footer(s, "cite-or-refuse is the product, not a feature flag")

    # 14 ─ close
    s = new_slide(prs)
    band = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, H - Pt(6), W, Pt(6))
    band.fill.solid(); band.fill.fore_color.rgb = ACCENT; band.line.fill.background(); band.shadow.inherit = False
    write(s, Inches(0.8), Inches(2.2), Inches(11.6), Inches(2.2),
          "What should we evaluate,\nand what does the evidence say?", size=40, bold=True, line=1.15)
    write(s, Inches(0.8), Inches(4.5), Inches(10.5), Inches(1.2),
          "Ask it about your own model. Every answer is cited or refused; every number comes from the corpus; "
          "the model behind it is swappable at runtime.", size=17, color=DIM, line=1.4)
    chip(s, Inches(0.8), Inches(5.7), Inches(4.1), Inches(0.5), "github.com/adi274903/MARS-Healthcare")
    chip(s, Inches(5.1), Inches(5.7), Inches(2.4), Inches(0.5), "demo video + PDF")

    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(out)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.2f} MB, {len(prs.slides._sldIdLst)} slides)")
    return out


if __name__ == "__main__":
    import sys

    build(Path(sys.argv[1] if len(sys.argv) > 1 else "deck/EvalBot-deck.pptx"))
