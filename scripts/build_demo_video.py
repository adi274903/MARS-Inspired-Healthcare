#!/usr/bin/env python3
"""Build the demo video from real UI screenshots + generated narration cards.

    python scripts/build_demo_video.py SHOT_welcome SHOT_stream SHOT_checklist SHOT_sources SHOT_aggregate [OUT]

Every screenshot is real output from the running stack (OpenRouter + Gemma 4 +
hybrid retrieval). The cards explain what the assistant is *for*: turning
"should we trust this model?" into a cited evaluation plan and a checklist.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, UIH, CAP = 1100, 1082, 98
H = UIH + CAP
BG = (11, 16, 20)
FG = (232, 238, 243)
ACCENT = (56, 189, 168)
DIM = (150, 163, 173)
FONTS = Path("/usr/share/fonts/dejavu-sans-fonts")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)


def wrap(draw, text: str, fnt, width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=fnt) <= width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def base() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (W, H), BG)
    return image, ImageDraw.Draw(image)


def card(kicker: str, headline: str, lines: list[str], foot: str) -> Image.Image:
    image, draw = base()
    draw.rectangle([0, 0, W, 10], fill=ACCENT)
    draw.text((70, 120), kicker.upper(), font=font(18, True), fill=ACCENT)
    y = 158
    for line in wrap(draw, headline, font(44, True), W - 140):
        draw.text((70, y), line, font=font(44, True), fill=FG)
        y += 56
    y += 26
    for entry in lines:
        bullet = "—" if entry.startswith("-") else "›"
        text = entry.lstrip("-› ").strip()
        draw.text((70, y), bullet, font=font(22, True), fill=ACCENT)
        for i, line in enumerate(wrap(draw, text, font(22), W - 190)):
            draw.text((108, y), line, font=font(22), fill=FG if i == 0 else DIM)
            y += 30
        y += 12
    draw.text((70, H - 110), foot, font=font(19), fill=ACCENT)
    return image


def ui_frame(path: Path, caption: str, step: str) -> Image.Image:
    image, draw = base()
    image.paste(Image.open(path).convert("RGB").resize((W, UIH), Image.LANCZOS), (0, 0))
    draw.rectangle([0, UIH, W, H], fill=(15, 21, 26))
    draw.rectangle([0, UIH, W, UIH + 3], fill=ACCENT)
    draw.text((28, UIH + 16), step.upper(), font=font(16, True), fill=ACCENT)
    y = UIH + 40
    for line in wrap(draw, caption, font(19), W - 56)[:2]:
        draw.text((28, y), line, font=font(19), fill=FG)
        y += 25
    return image


def main() -> int:
    if len(sys.argv) < 6:
        print(__doc__, file=sys.stderr)
        return 2
    welcome, stream, checklist, sources, aggregate = (Path(p) for p in sys.argv[1:6])
    out = Path(sys.argv[6]) if len(sys.argv) > 6 else Path("demo/evalbot-demo.mp4")

    beats = [
        (card("what this is",
              "An evaluation assistant for clinical AI",
              ["It does not just answer questions — it tells you what to evaluate, and proves each item.",
               "Grounded in 15,954 extracted papers and 24 reporting & governance frameworks.",
               "Model: Gemma 4 26B-A4B, swappable at runtime."],
              "http://localhost:8080"), 4.4),
        (card("what it does",
              "Four things, per question",
              ["- Turns \u201cshould we trust this model?\u201d into a checkable evaluation plan.",
               "- Cites the evidence behind each item — PubMed, DOI, with the claim attached.",
               "- Names the standard that applies: TRIPOD+AI, PROBAST, FDA SaMD, EU AI Act\u2026",
               "- Answers \u201chow many papers\u2026\u201d from SQL, never from the model's arithmetic."],
              "six services \u00b7 one event bus \u00b7 citations built from records"), 5.0),
        (ui_frame(welcome, "Eight evaluation dimensions drive retrieval and the prompt: safety, fairness, "
                           "privacy, clinical validity, regulatory, robustness, explainability, monitoring.",
                  "the starting point"), 3.6),
        (ui_frame(stream, "\u201cWhat should I evaluate for a sepsis-prediction model?\u201d It plans the evaluation live: "
                          "performance metrics, validation tiers, interpretability \u2014 citing as it streams.",
                  "it plans the evaluation"), 4.4),
        (ui_frame(checklist, "The deliverable: a 10-item checklist across metrics, validation and interpretability \u2014 "
                             "tick items off, copy, or download as Markdown.",
                  "the output you act on"), 4.4),
        (ui_frame(sources, "Traceable by construction: PubMed papers for the evidence, plus a GUIDANCE/LAW card for the "
                           "standard that applies \u2014 here TRIPOD+AI (BMJ 2024).",
                  "every claim has a source"), 4.6),
        (ui_frame(aggregate, "\u201cHow many papers report external validation?\u201d \u2192 4,655, from SQL, with a year "
                             "breakdown. Corpus statistics are exact, not model guesses.",
                  "corpus statistics"), 4.4),
        (card("in one line",
              "It answers: what should we evaluate, and what does the evidence say?",
              ["Every answer: cited or refused. Every number: from SQL. Every model: swappable, hot.",
               "124 tests passing \u00b7 MIT-style service boundaries \u00b7 runs on one process or nine."],
              "github.com/adi274903/MARS-Healthcare"), 4.4),
    ]

    tmp = Path("/tmp/evalbot-demo-frames")
    tmp.mkdir(exist_ok=True)
    images = []
    for index, (image, _dur) in enumerate(beats):
        path = tmp / f"beat{index}.png"
        image.save(path)
        images.append(path)
    durations = [d for _img, d in beats]

    inputs: list[str] = []
    for path, dur in zip(images, durations):
        inputs += ["-loop", "1", "-t", f"{dur}", "-i", str(path)]

    parts, offset, label, fade = [], 0.0, "0:v", 0.45
    for i in range(1, len(images)):
        offset += durations[i - 1] - fade
        parts.append(f"[{label}][{i}:v]xfade=transition=fade:duration={fade}:offset={offset:.3f}[v{i}]")
        label = f"v{i}"
    filter_complex = ";".join(parts) + f";[{label}]format=yuv420p[v]"

    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", filter_complex,
         "-map", "[v]", "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-movflags", "+faststart", str(out)],
        check=True,
    )
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
    print(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height",
                          "-of", "default=nw=1", str(out)], capture_output=True, text=True).stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
