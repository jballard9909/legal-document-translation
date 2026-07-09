"""
redact_core.py

Importable core for /redact: given a page image + that page's OCR word-boxes +
that page's detected PII spans, black out the pixels of every word touched by
PII. Bytes in, bytes out. No FastAPI, no disk, no globals -- redact_service.py
wraps this, its self-test drives it, both import the SAME function.

WHERE THIS SITS
---------------
Last service before the cloud boundary. /detect-pii removed PII from the TEXT;
this removes PII from the IMAGE. Both are needed: the anonymized text and the
source page image travel onward together, so a name blacked out of the text but
left legible in the image is still a leak. This closes that.

THE MAPPING (the load-bearing idea)
-----------------------------------
/detect-pii returns entity spans as CHARACTER offsets [start, end) into the
page text. /ocr returns each word with char_span [start, end) into that SAME
text, plus that word's pixel box. So:

    PII entity span  --overlaps-->  word char_spans  -->  those words' boxes

A word is redacted iff its char_span overlaps any kept PII span. This is why
fragmented names ("J" "ane" "A." "Doe") and multi-line entities all redact
correctly for free: every piece whose char_span overlaps the entity is caught,
regardless of how OCR split it or which line it fell on. We redact the WHOLE
word box (over-redaction, the safe direction) rather than a sub-word slice.

TWO SAFEGUARDS
--------------
1. SCORE_FLOOR: Presidio emits low-confidence false positives (e.g. a 0.01
   US_DRIVER_LICENSE on a case-number tail). Entities below the floor are
   dropped BEFORE mapping to boxes, so redaction never fires on garbage-score
   hits alone. Where a real high-score entity covers the same region, that
   region is still redacted -- the floor removes noise, not coverage.

2. PAD: Tesseract boxes hug the glyphs; a pixel-tight blackout can leave a
   sliver of an ascender/descender. Each box is expanded by PAD pixels before
   drawing (clamped to image bounds). Over-covers slightly -- safe direction.

RASTER PARITY (the precondition, asserted)
------------------------------------------
The word boxes are only valid on the exact raster /ocr measured. The caller
supplies expected (width, height) from the /ocr envelope's image_dimensions;
redact_page() asserts the image it's about to draw on matches. If it doesn't,
it RAISES rather than draw boxes in the wrong place -- a mis-sized image is a
privacy failure, so we abort instead of leaking.
"""

from io import BytesIO

from PIL import Image, ImageDraw

# --- tunable safeguards (locked with Jacob) ---
SCORE_FLOOR = 0.30   # drop PII entities below this before mapping to boxes
PAD = 3              # pixels of padding added around each redacted word box


def _spans_overlap(a_start: int, a_end: int,
                   b_start: int, b_end: int) -> bool:
    """Half-open [start, end) overlap test. Touching-but-not-crossing
    (a_end == b_start) is NOT overlap, matching char_span semantics."""
    return not (a_end <= b_start or a_start >= b_end)


def compute_redaction_boxes(words: list, entities: list,
                            score_floor: float = SCORE_FLOOR) -> list:
    """
    Pure geometry step (no image): decide which word boxes to redact.

    Args:
        words:    /ocr words, each {"box":[x,y,w,h], "char_span":[s,e], ...}
        entities: /detect-pii entities, each {"start":s, "end":e,
                  "score":f, "entity_type":str, ...}
        score_floor: entities strictly below this are ignored.

    Returns a list of audit records, one per redacted word:
        {"box":[x,y,w,h], "text":str, "entity_types":[...]}
    Separated from drawing so it can be unit-tested and logged without pixels.
    """
    kept = [e for e in entities if e.get("score", 0) >= score_floor]
    boxes = []
    for w in words:
        ws, we = w["char_span"]
        hits = [e for e in kept
                if _spans_overlap(ws, we, e["start"], e["end"])]
        if hits:
            boxes.append({
                "box": w["box"],
                "text": w.get("text", ""),
                "entity_types": sorted({h["entity_type"] for h in hits}),
            })
    return boxes


def redact_page(image_bytes: bytes,
                words: list,
                entities: list,
                expected_dims: dict = None,
                score_floor: float = SCORE_FLOOR,
                pad: int = PAD) -> dict:
    """
    Black out PII word-boxes on a page image.

    Args:
        image_bytes:   the page PNG/JPEG bytes (from rasterize_pdf_page).
        words:         /ocr words for THIS page (box + char_span).
        entities:      /detect-pii entities for THIS page.
        expected_dims: {"width":W, "height":H} from /ocr's image_dimensions.
                       If given, asserted against the actual image before any
                       drawing -- a mismatch RAISES (raster-parity tripwire).
        score_floor:   see module docstring.
        pad:           pixels added around each box (clamped to image bounds).

    Returns:
        {
          "image_bytes": <redacted PNG bytes>,
          "redacted_count": int,          # word boxes blacked out
          "audit": [ {box, text, entity_types}, ... ],  # what + why
        }

    Raises:
        ValueError if expected_dims is given and does not match the image.
    """
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    w_img, h_img = img.size

    if expected_dims is not None:
        ew, eh = expected_dims["width"], expected_dims["height"]
        if (w_img, h_img) != (ew, eh):
            # Raster parity broken -> boxes would land wrong -> abort, don't leak.
            raise ValueError(
                f"raster parity mismatch: image is {w_img}x{h_img} but /ocr "
                f"measured {ew}x{eh}; refusing to redact to avoid mis-placed "
                f"boxes."
            )

    audit = compute_redaction_boxes(words, entities, score_floor=score_floor)

    draw = ImageDraw.Draw(img)
    for rec in audit:
        x, y, bw, bh = rec["box"]
        # pad, clamped to image bounds
        x0 = max(0, x - pad)
        y0 = max(0, y - pad)
        x1 = min(w_img, x + bw + pad)
        y1 = min(h_img, y + bh + pad)
        draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0))

    out = BytesIO()
    img.save(out, format="PNG")
    return {
        "image_bytes": out.getvalue(),
        "redacted_count": len(audit),
        "audit": audit,
    }