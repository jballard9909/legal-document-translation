"""
region_scan_v1.py

The "detect-before-read" privacy layer.

THE PROBLEM THIS SOLVES
-----------------------
Tesseract OCR silently DROPS content it can't read (the rotated stamp in our
synthetic decree produced zero words, zero boxes). You cannot black out what you
never detected, so any PII inside a dropped region would leak straight to a
cloud VLM. Confidence thresholds don't help: the stamp didn't score low, it
scored NOTHING.

THE INVERSION
-------------
Stop trusting OCR to find everything. Instead:
  1. Detect every INK region on the page using local computer vision (no AI,
     no network) -- this finds the stamp even though it's unreadable, because
     rotated text is still ink.
  2. Run OCR and collect the word-boxes it DID find.
  3. Reconcile: any ink-region with no OCR words inside it is "UNACCOUNTED".
  4. Treat every unaccounted region as presumptively PII-bearing -> blackout
     candidate BEFORE anything leaves the machine.

This script proves step 1-3 on the synthetic decree and shows the stamp finally
getting a bounding box. Blackout itself is the next step, once we trust the
detection.

Fully local. Fully synthetic input. No network, no AI in the detection path.

Output:
  region_scan_overlay_v1.png
    green  = content region accounted for by OCR (normal text)
    red    = UNACCOUNTED region (presumptive PII -> blackout candidate)
"""

import os

import cv2
import numpy as np
import pytesseract
from pytesseract import Output
from PIL import Image

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_PNG = os.path.join(HERE, "synthetic_doc_v2.png")
OVERLAY_PNG = os.path.join(HERE, "region_scan_overlay_v1.png")

LANG = "eng"

# --- Tuning knobs (expect to iterate on these) ---
# Threshold: pixels darker than this become "ink". Otsu picks it automatically,
# so this manual value is a fallback only.
USE_OTSU = True
MANUAL_THRESH = 180

# Dilation: how aggressively to merge nearby ink into blobs. Wider kernel =
# letters merge into words/lines; too wide = whole page becomes one blob.
DILATE_KERNEL = (15, 9)   # (width, height) in pixels; wider horizontally to join words on a line
DILATE_ITERS = 2

# Ignore tiny specks (noise) and full-page-sized boxes (the page border).
MIN_AREA = 400            # px^2; drop dust
MAX_AREA_FRAC = 0.45      # drop any region bigger than this fraction of the page


def find_ink_regions(image_path: str):
    """Local CV: return bounding boxes (x, y, w, h) of every ink cluster."""
    # Load as grayscale
    gray = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise SystemExit(f"Could not read image: {image_path}")
    page_area = gray.shape[0] * gray.shape[1]

    # Threshold -> binary ink mask. THRESH_BINARY_INV makes ink = white (255),
    # paper = black (0), which is what morphology/contours expect.
    if USE_OTSU:
        _, binary = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )
    else:
        _, binary = cv2.threshold(
            gray, MANUAL_THRESH, 255, cv2.THRESH_BINARY_INV
        )

    # Dilate: fatten ink so nearby marks merge into regions.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, DILATE_KERNEL)
    dilated = cv2.dilate(binary, kernel, iterations=DILATE_ITERS)

    # Contours -> one outline per merged blob; take its bounding rectangle.
    contours, _ = cv2.findContours(
        dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    regions = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = w * h
        if area < MIN_AREA:
            continue
        if area > MAX_AREA_FRAC * page_area:
            continue
        regions.append((x, y, w, h))
    return regions


def get_ocr_word_boxes(image_path: str, lang: str):
    """Return bounding boxes (x, y, w, h) of every word OCR actually read."""
    img = Image.open(image_path)
    data = pytesseract.image_to_data(img, lang=lang, output_type=Output.DICT)
    boxes = []
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        raw = data["conf"][i]
        conf = int(raw) if raw not in ("-1", -1) else -1
        if not text or conf < 0:
            continue
        boxes.append(
            (data["left"][i], data["top"][i], data["width"][i], data["height"][i])
        )
    return boxes


def box_center(b):
    x, y, w, h = b
    return (x + w / 2, y + h / 2)


def point_in_box(px, py, box):
    x, y, w, h = box
    return x <= px <= x + w and y <= py <= y + h


def reconcile(regions, word_boxes):
    """
    Split content regions into accounted (contains >=1 OCR word center) and
    unaccounted (contains none). Unaccounted = presumptive PII.
    """
    accounted, unaccounted = [], []
    for region in regions:
        has_word = False
        for wb in word_boxes:
            cx, cy = box_center(wb)
            if point_in_box(cx, cy, region):
                has_word = True
                break
        (accounted if has_word else unaccounted).append(region)
    return accounted, unaccounted


def draw_overlay(image_path, accounted, unaccounted, out_path):
    """Green = accounted, Red = unaccounted (presumptive PII)."""
    img = cv2.imread(image_path)  # BGR
    for (x, y, w, h) in accounted:
        cv2.rectangle(img, (x, y), (x + w, y + h), (0, 150, 0), 2)   # green
    for (x, y, w, h) in unaccounted:
        cv2.rectangle(img, (x, y), (x + w, y + h), (0, 0, 200), 3)   # red
    cv2.imwrite(out_path, img)


def main():
    if not os.path.exists(INPUT_PNG):
        raise SystemExit(
            f"Input not found: {INPUT_PNG}\nRun make_synthetic_doc_v1.py first."
        )

    print("Step 1: detecting ink regions (local CV, no AI) ...")
    regions = find_ink_regions(INPUT_PNG)
    print(f"  content regions found: {len(regions)}")

    print(f"Step 2: collecting OCR word-boxes (lang='{LANG}') ...")
    word_boxes = get_ocr_word_boxes(INPUT_PNG, LANG)
    print(f"  OCR words found: {len(word_boxes)}")

    print("Step 3: reconciling ...")
    accounted, unaccounted = reconcile(regions, word_boxes)
    print(f"  accounted regions   (green): {len(accounted)}")
    print(f"  UNACCOUNTED regions (red)  : {len(unaccounted)}  <- presumptive PII")

    if unaccounted:
        print("\n  Unaccounted region boxes (x, y, w, h) -> blackout candidates:")
        for b in unaccounted:
            print(f"    {b}")
    else:
        print("\n  No unaccounted regions. (If the stamp isn't caught, the "
              "dilation\n  kernel likely merged it into a neighbor -- tune and "
              "re-run.)")

    draw_overlay(INPUT_PNG, accounted, unaccounted, OVERLAY_PNG)
    print(f"\nOverlay saved -> {OVERLAY_PNG}")
    print("  green = OCR accounted,  red = unaccounted (presumptive PII)")


if __name__ == "__main__":
    main()