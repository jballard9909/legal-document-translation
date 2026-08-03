"""
ocr_pass_v1.py

First OCR pass over the synthetic decree. Runs Tesseract's image_to_data, which
returns the THREE things we care about for this project, per detected word:
    - text        (the characters Tesseract read)
    - confidence  (0-100 certainty; -1 means "no text here")
    - bounding box (left, top, width, height, in pixels)

Why all three matter for ABC Link:
    - boxes drive (a) image-level PII blackout, (b) the 1:1 layout mirror the
      client wants. You can't reproduce a layout without knowing where text sat
    - confidence drives the [ILLEGIBLE] / human-review flagging rule
    - reading order + boxes are where the Turkish traineddata quirks would show

Outputs:
    - a per-word table printed to the console
    - a summary: average confidence + every word below the review threshold
    - ocr_overlay_v1.png : the source image with boxes drawn on, so you can SEE
      what Tesseract found and where (this is your layout preview, too)
"""

import os

import pytesseract
from pytesseract import Output
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_PNG = os.path.join(HERE, "synthetic_decree_v1.png")
OVERLAY_PNG = os.path.join(HERE, "ocr_overlay_v1.png")

LANG = "eng"            # English first; Turkish ('tur') is the next pass
REVIEW_THRESHOLD = 60  # words below this are [ILLEGIBLE] / review candidates


def run_ocr(image_path: str, lang: str) -> dict:
    """Return Tesseract's structured output as parallel lists."""
    img = Image.open(image_path)
    return pytesseract.image_to_data(img, lang=lang, output_type=Output.DICT)


def print_word_table(data: dict) -> None:
    """Print one row per real word: text | confidence | (x, y, w, h)."""
    n = len(data["text"])
    print(f"{'#':>3}  {'CONF':>5}  {'BOX (x, y, w, h)':<24}  TEXT")
    print("-" * 70)
    idx = 0
    for i in range(n):
        text = data["text"][i].strip()
        conf = int(data["conf"][i]) if data["conf"][i] not in ("-1", -1) else -1
        # skip empty layout rows (Tesseract emits page/block/line scaffolding)
        if not text or conf < 0:
            continue
        idx += 1
        box = (data["left"][i], data["top"][i], data["width"][i], data["height"][i])
        box_str = f"({box[0]}, {box[1]}, {box[2]}, {box[3]})"
        print(f"{idx:>3}  {conf:>5}  {box_str:<24}  {text}")


def summarize(data: dict, threshold: int) -> None:
    """Average confidence and the list of low-confidence (review) words."""
    confs, low = [], []
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        raw = data["conf"][i]
        conf = int(raw) if raw not in ("-1", -1) else -1
        if not text or conf < 0:
            continue
        confs.append(conf)
        if conf < threshold:
            low.append((text, conf))

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    if confs:
        print(f"Words detected      : {len(confs)}")
        print(f"Average confidence  : {sum(confs) / len(confs):.1f}")
        print(f"Min / Max           : {min(confs)} / {max(confs)}")
    else:
        print("No words detected.")

    print(f"\nReview candidates (confidence < {threshold}) "
          f"-> future [ILLEGIBLE] flags:")
    if low:
        for text, conf in low:
            print(f"   [{conf:>3}]  {text}")
    else:
        print("   (none)")
    print("\nNote: expect the rotated stamp and small-print footnote to land "
          "here.\nThat is the lesson, not a bug — these are the elements the "
          "client\nsays current workflows drop.")


def draw_overlay(image_path: str, data: dict, out_path: str,
                 threshold: int) -> None:
    """Draw boxes on the image. Green = confident, red = below threshold."""
    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        raw = data["conf"][i]
        conf = int(raw) if raw not in ("-1", -1) else -1
        if not text or conf < 0:
            continue
        x, y = data["left"][i], data["top"][i]
        w, h = data["width"][i], data["height"][i]
        color = (200, 0, 0) if conf < threshold else (0, 150, 0)
        draw.rectangle([x, y, x + w, y + h], outline=color, width=2)
    img.save(out_path)
    print(f"\nBox overlay saved -> {out_path}")
    print("   green = confident,  red = below review threshold")


def main() -> None:
    if not os.path.exists(INPUT_PNG):
        raise SystemExit(
            f"Input not found: {INPUT_PNG}\n"
            "Run make_synthetic_doc_v1.py first."
        )
    print(f"Running OCR (lang='{LANG}') on {os.path.basename(INPUT_PNG)} ...\n")
    data = run_ocr(INPUT_PNG, LANG)
    print_word_table(data)
    summarize(data, REVIEW_THRESHOLD)
    draw_overlay(INPUT_PNG, data, OVERLAY_PNG, REVIEW_THRESHOLD)


if __name__ == "__main__":
    main()