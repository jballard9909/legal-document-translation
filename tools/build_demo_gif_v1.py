"""
build_demo_gif_v1.py

Assembles the README demo GIF from five still screenshots.

WHERE THIS SITS
---------------
Portfolio tooling, not a pipeline component. Nothing here touches the
translation pipeline, the microservices, or any document data. It reads
screenshots the operator captured by hand and emits docs/assets/demo.gif.

WHY A SCRIPT AND NOT AN IMAGE EDITOR
-------------------------------------
GIF requires every frame to share one pixel canvas, but the five source
screenshots are three different shapes (wide document crops, a wide n8n node
chain, a squarer JSON payload panel). Normalising them by hand is five manual
operations that must be redone in full whenever a label, a duration, or a
single re-captured frame changes. Here it is one edit to BEATS and one re-run.

DESIGN RULES (deliberate, not incidental)
------------------------------------------
1. NEVER UPSCALE. Enlarging a screenshot softens text, and the whole point of
   the GIF is that the placeholder tokens and redaction boxes are legible.
   Frames are placed at native size or scaled DOWN. A frame narrower than its
   target raises rather than silently interpolating.
2. Dark chrome, light documents. Padding and bands share one charcoal. The two
   dark frames (n8n canvas, JSON payload) sit inset at native size and blend
   into the padding; the three light document frames run edge to edge. The
   alternation reads as a convention rather than a strobe.
3. Document frames are TOP-ANCHORED. The Turkish translation wraps to a
   different line count than the English, so centring would shift the text
   block vertically between beats and the eye would read "something moved"
   instead of "something changed". Top-anchoring holds section 1.5's first line
   in place across all three appearances.
4. A step counter on every frame. A looping GIF has no beginning; most viewers
   arrive mid-sequence and need to know where they are.

FAIL-LOUD
---------
Missing source file, unreadable image, or a frame that would need upscaling
raises immediately. A silently degraded frame is worse than no GIF, because it
ships to a portfolio without anyone noticing.

No document data, no PII, no network. Operates only on files it is pointed at.
"""

import os
import sys
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

# --- Canvas -----------------------------------------------------------------
# Width is set by the NARROWEST document frame so nothing is ever enlarged.
# Height clears the tallest frame (the JSON payload panel) plus both bands.
CANVAS_W = 1053
CANVAS_H = 540
BAND_H = 44

CANVAS_BG = (24, 24, 27)
BAND_BG = (39, 39, 42)
LABEL_FG = (244, 244, 245)
COUNTER_FG = (161, 161, 170)

CLAIM = "Machine-readable PII is stripped before any cloud call"

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]

# --- Beats ------------------------------------------------------------------
# (filename, top label, seconds, anchor, fit)
#   anchor: "top" holds the frame against the top band; "center" centres it.
#   fit:    "full" scales the frame to canvas width; "native" places it at its
#           own size, centred horizontally, letting the charcoal padding frame
#           it. Dark frames use "native" so they are never resampled.
BEATS: List[Tuple[str, str, float, str, str]] = [
    ("source_paragraph_en.png",
     "Source document \u2014 synthetic test data", 3.0, "top", "full"),
    ("pii_protection_chain.png",
     "OCR \u2192 detect \u2192 redact \u2192 anonymize. All local.", 4.0, "center", "native"),
    ("source_paragraph_redacted.png",
     "Nothing has left the machine yet.", 3.0, "top", "full"),
    ("text_for_llm.png",
     "This is what the cloud actually received.", 5.0, "center", "native"),
    ("translated_paragraph_tr.png",
     "Names restored locally, after translation.", 4.0, "top", "full"),
]


def _load_font(candidates: List[str], size: int) -> ImageFont.FreeTypeFont:
    """First readable font wins. Falls back to PIL's bitmap default rather than
    dying -- a plain-looking label still ships; a crash does not."""
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _fit(img: Image.Image, mode: str) -> Image.Image:
    """Scale a frame for placement. Never enlarges -- see design rule 1."""
    inner_h = CANVAS_H - (2 * BAND_H)

    if mode == "native":
        target_w, target_h = img.width, img.height
    elif mode == "full":
        target_w = CANVAS_W
        target_h = round(img.height * CANVAS_W / img.width)
    else:
        raise ValueError(f"Unknown fit mode: {mode!r} (expected 'full' or 'native')")

    if target_w > img.width:
        raise ValueError(
            f"Frame is {img.width}px wide but placement needs {target_w}px. "
            f"Upscaling softens text -- re-capture this frame at "
            f"{target_w}px or wider, or lower CANVAS_W."
        )

    # A frame taller than the usable band gets scaled down to fit; that is a
    # reduction, so it stays inside rule 1.
    if target_h > inner_h:
        shrink = inner_h / target_h
        target_w = round(target_w * shrink)
        target_h = inner_h

    if (target_w, target_h) == img.size:
        return img.convert("RGB")
    return img.convert("RGB").resize((target_w, target_h), Image.LANCZOS)


def compose_frame(src_path: str, label: str, index: int, total: int,
                  anchor: str, fit: str) -> Image.Image:
    """Place one screenshot on the shared canvas with its bands and counter."""
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"Beat {index}/{total}: no such file: {src_path}")

    with Image.open(src_path) as raw:
        raw.load()
        placed = _fit(raw, fit)

    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), CANVAS_BG)

    x = (CANVAS_W - placed.width) // 2
    if anchor == "top":
        y = BAND_H
    elif anchor == "center":
        y = BAND_H + ((CANVAS_H - 2 * BAND_H) - placed.height) // 2
    else:
        raise ValueError(f"Unknown anchor: {anchor!r} (expected 'top' or 'center')")
    canvas.paste(placed, (x, y))

    d = ImageDraw.Draw(canvas)
    label_font = _load_font(FONT_BOLD_CANDIDATES, 19)
    claim_font = _load_font(FONT_CANDIDATES, 17)
    counter_font = _load_font(FONT_CANDIDATES, 16)

    d.rectangle([0, 0, CANVAS_W, BAND_H], fill=BAND_BG)
    d.text((18, BAND_H // 2), label, fill=LABEL_FG, font=label_font, anchor="lm")

    counter = f"{index}/{total}"
    d.text((CANVAS_W - 18, BAND_H // 2), counter,
           fill=COUNTER_FG, font=counter_font, anchor="rm")

    d.rectangle([0, CANVAS_H - BAND_H, CANVAS_W, CANVAS_H], fill=BAND_BG)
    d.text((18, CANVAS_H - BAND_H // 2), CLAIM,
           fill=LABEL_FG, font=claim_font, anchor="lm")

    return canvas


def build(frames_dir: str, out_path: str) -> str:
    """Compose every beat and write the looping GIF. Returns the output path."""
    if os.path.exists(out_path):
        raise FileExistsError(
            f"{out_path} already exists. Versioning discipline: move or rename "
            f"the previous GIF rather than overwriting the record."
        )

    total = len(BEATS)
    frames, durations = [], []

    for i, (fname, label, secs, anchor, fit) in enumerate(BEATS, start=1):
        src = os.path.join(frames_dir, fname)
        frame = compose_frame(src, label, i, total, anchor, fit)
        frames.append(frame)
        durations.append(int(round(secs * 1000)))
        print(f"  beat {i}/{total}  {fname:34s} {secs:>4.1f}s  {anchor}/{fit}")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    # Per-frame durations in ms; loop=0 means loop forever.
    frames[0].save(
        out_path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )

    size = os.path.getsize(out_path)
    print(f"\nWrote {out_path}")
    print(f"  {CANVAS_W}x{CANVAS_H}  {total} frames  "
          f"{sum(durations)/1000:.1f}s  {size/1024:.0f} KB")
    if size > 10 * 1024 * 1024:
        print("  WARNING: over GitHub's 10MB limit for images and GIFs.")
    return out_path


if __name__ == "__main__":
    frames_dir = sys.argv[1] if len(sys.argv) > 1 else "docs/assets/frames"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "docs/assets/demo.gif"
    print(f"Building demo GIF from {frames_dir}")
    build(frames_dir, out_path)