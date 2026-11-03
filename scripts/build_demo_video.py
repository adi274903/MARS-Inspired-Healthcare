#!/usr/bin/env python3
"""Build the demo video from real UI screenshots + generated title cards.

Input: the four JPEGs captured from the live UI (paths passed as arguments, in order).
Output: demo/evalbot-demo.mp4 — 1100x1180 device-style frames with a caption bar,
crossfaded. Everything shown is real output from the running stack.
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

FONT_DIR = Path("/usr/share/fonts/dejavu-sans-fonts")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(FONT_DIR / name), size)


def canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (W, H), BG)
    return image, ImageDraw.Draw(image)


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=fnt) <= width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def title_card(headline: str, sub: str, foot: str) -> Image.Image:
    image, draw = canvas()
    draw.rectangle([0, 0, W, 10], fill=ACCENT)
    y = 360
    for line in wrap(draw, headline, font(46, True), W - 140):
        draw.text((70, y), line, font=font(46, True), fill=FG)
        y += 58
    y += 18
    for line in wrap(draw, sub, font(24), W - 140):
        draw.text((70, y), line, font=font(24), fill=DIM)
        y += 34
    draw.text((70, H - 130), foot, font=font(20), fill=ACCENT)
    return image


def ui_frame(path: Path, caption: str, step: str) -> Image.Image:
    image, draw = canvas()
    shot = Image.open(path).convert("RGB").resize((W, UIH), Image.LANCZOS)
    image.paste(shot, (0, 0))
    draw.rectangle([0, UIH, W, H], fill=(15, 21, 26))
    draw.rectangle([0, UIH, W, UIH + 3], fill=ACCENT)
    draw.text((28, UIH + 18), step, font=font(17, True), fill=ACCENT)
    y = UIH + 42
    for line in wrap(draw, caption, font(20), W - 56)[:2]:
        draw.text((28, y), line, font=font(20), fill=FG)
        y += 26
    return image


def main() -> int:
    if len(sys.argv) < 5:
        print("usage: build_demo_video.py SHOT1 SHOT2 SHOT3 SHOT4 [OUT]", file=sys.stderr)
        return 2
    shots = [Path(p) for p in sys.argv[1:5]]
    out = Path(sys.argv[5]) if len(sys.argv) > 5 else Path("demo/evalbot-demo.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path("/tmp/evalbot-demo-frames")
    tmp.mkdir(exist_ok=True)

    frames = [
        (title_card(
            "EvalBot — live demo",
            "Gemma 4 26B-A4B via OpenRouter · hybrid retrieval over 15,954 papers · "
            "server-sent events · citations built from the records, not the model.",
            "http://localhost:8080  ·  MONOLITH=1  ·  124 tests passing"), 3.2),
        (ui_frame(shots[0], "The UI is served by the same gateway process. Badge reads Live; "
                            "context chips and focus areas drive retrieval and the prompt.", "0 · UI"), 3.4),
        (ui_frame(shots[1], "Question 1 streams tokens. Retrieval reported 8 papers; "
                            "Gemma cites [4] and [8] — real PMIDs, live.", "1 · STREAMING"), 3.6),
        (ui_frame(shots[2], "Citations are renumbered from the retrieved set: PubMed + DOI links, "
                            "a claim per source, and a checkable checklist card.", "2 · CITATIONS"), 4.2),
        (ui_frame(shots[3], "\u201cHow many…\u201d never reaches the model's arithmetic: the aggregate leg "
                            "answers from SQL — 4,655 papers, distributed by year.", "3 · SQL AGGREGATE"), 4.2),
        (title_card(
            "What this proves",
            "One question \u2192 gateway \u2192 event bus \u2192 orchestrator \u2192 sqlite + qdrant + neo4j "
            "\u2192 Gemma 4 \u2192 answer service \u2192 SSE. Numbers come from SQL, sources from the corpus, "
            "and the model is swappable at runtime.",
            "github.com/adi274903/MARS-Healthcare"), 3.6),
    ]

    images = []
    for index, (image, _dur) in enumerate(frames):
        path = tmp / f"frame{index}.png"
        image.save(path)
        images.append(path)

    durations = [d for _img, d in frames]
    fade = 0.45
    inputs: list[str] = []
    for path, dur in zip(images, durations):
        inputs += ["-loop", "1", "-t", f"{dur}", "-i", str(path)]

    parts, offset, label = [], 0.0, "0:v"
    for i in range(1, len(images)):
        offset += durations[i - 1] - fade
        parts.append(
            f"[{label}][{i}:v]xfade=transition=fade:duration={fade}:offset={offset:.3f}[v{i}]"
        )
        label = f"v{i}"
    filter_complex = ";".join(parts) + f";[{label}]format=yuv420p[v]"

    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", filter_complex,
           "-map", "[v]", "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    size = out.stat().st_size / 1e6
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration,size:stream=width,height,codec_name",
                            "-of", "default=nw=1", str(out)], capture_output=True, text=True).stdout
    print(f"wrote {out} ({size:.1f} MB)")
    print(probe.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
