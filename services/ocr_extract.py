"""
ocr_extract.py  (v2 — adds per-word char_span for /redact)

Importable OCR core for the ABC Link pipeline. Wraps a single Tesseract
image_to_data pass and reshapes its flat, parallel-list output into the
THREE-LEVEL structure the downstream pipeline needs:

    text   : full document, reading order, newline-joined at line boundaries
    lines  : the translate-and-replace unit. Each line = joined text + a
             union pixel box + a stable line_id. Translation consumes this;
             layout reassembly drops translated text back into each line box.
    words  : text + pixel box + confidence + parent line_id + char_span.
             /redact maps PII character-spans -> words (via char_span) ->
             pixel boxes for image blackout.

Design decisions (locked with Jacob):
    - bytes in, not a path: extract(image_bytes) so the FastAPI wrapper can
      hand it multipart bytes directly. Stateless, pass-by-value.
    - confidence: KEEP EVERYTHING. Every word retains its conf. Nothing is
      dropped and nothing is flagged at this layer. Dropping low-confidence
      words here is the silent-omission failure the privacy design forbids.
    - lang defaults to "tur+eng": bidirectional docs, let Tesseract pick from
      either glyph set. Direction is resolved downstream at /detect-pii.
    - line_id is the FULL (block, par, line) triple. Tesseract resets
      line_num within each block, so line_num alone collapses lines from
      different blocks together. The triple keeps them distinct.
    - char_span (v2): each word's [start, end) offset into `text`. Built in
      the SAME walk that assembles `text`, so string and offsets cannot drift.
      This is the contract /detect-pii spans map through: PII char-span ->
      overlapping word char_spans -> those words' pixel boxes -> blackout.
      /redact reads char_span + box only; it never rebuilds the string.

The self-test (behind __main__) prints the word table + summary and saves an
overlay with WORD boxes and LINE boxes in distinct colors, so the grouping
can be eyeballed. It also asserts the char_span contract holds (see
_verify_char_spans). The service never writes files; only this self-test does.
"""

import os
from io import BytesIO

import pytesseract
from pytesseract import Output
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Configuration (module load)
# ---------------------------------------------------------------------------
DEFAULT_LANG = "tur+eng"   # bidirectional; direction resolved downstream
REVIEW_THRESHOLD = 60      # self-test only: [ILLEGIBLE] / review preview


def _to_conf(raw) -> int:
    """Tesseract conf comes as str or int; -1 means 'no text here'."""
    return int(raw) if raw not in ("-1", -1) else -1


def extract(image_bytes: bytes, lang: str = DEFAULT_LANG) -> dict:
    """
    Run one Tesseract pass over image bytes and return the three-level
    structure. Pure function: no disk writes, no globals mutated.

    Returns:
        {
          "text":  "<full document, reading order>",
          "lines": [
             {"line_id": "b:p:l", "text": "...", "box": [x, y, w, h]},
             ...
          ],
          "words": [
             {"text": "...", "box": [x, y, w, h], "conf": int,
              "line_id": "b:p:l", "char_span": [start, end]},
             ...
          ],
        }

    char_span is a [start, end) half-open offset into "text". For every word,
    text[start:end] == word["text"] holds exactly (asserted in the self-test).
    """
    img = Image.open(BytesIO(image_bytes))
    data = pytesseract.image_to_data(img, lang=lang, output_type=Output.DICT)

    n = len(data["text"])
    words = []
    # groups: line_id -> accumulator for building line text + union box
    groups = {}
    order = []  # preserve first-seen order of line_ids (== reading order)

    for i in range(n):
        text = data["text"][i].strip()
        conf = _to_conf(data["conf"][i])
        # skip Tesseract's page/block/line scaffolding rows (no real text)
        if not text or conf < 0:
            continue

        x, y = data["left"][i], data["top"][i]
        w, h = data["width"][i], data["height"][i]

        # full triple: block resets line_num, so line_num alone is unsafe
        line_id = f"{data['block_num'][i]}:{data['par_num'][i]}:{data['line_num'][i]}"

        words.append({
            "text": text,
            "box": [x, y, w, h],
            "conf": conf,
            "line_id": line_id,
        })

        if line_id not in groups:
            groups[line_id] = {
                "texts": [],
                "words": [],          # refs to the word dicts in this line
                "left": x, "top": y,
                "right": x + w, "bottom": y + h,
            }
            order.append(line_id)
        g = groups[line_id]
        g["texts"].append(text)
        g["words"].append(words[-1])  # the dict we just appended
        # union box: min-left/top, max-right/bottom across member words
        g["left"] = min(g["left"], x)
        g["top"] = min(g["top"], y)
        g["right"] = max(g["right"], x + w)
        g["bottom"] = max(g["bottom"], y + h)

    lines = []
    for line_id in order:
        g = groups[line_id]
        lines.append({
            "line_id": line_id,
            "text": " ".join(g["texts"]),
            "box": [
                g["left"], g["top"],
                g["right"] - g["left"], g["bottom"] - g["top"],
            ],
        })

    # Build full_text AND stamp each word's char_span in one walk, so the
    # string and the offsets come from the same source. Join contract:
    # words space-joined within a line, lines newline-joined between lines.
    # This reproduces the old '\n'.join(line["text"]) byte-for-byte while
    # recording where every word landed.
    parts = []
    cursor = 0
    for li, line_id in enumerate(order):
        if li > 0:
            cursor += 1          # the "\n" separator between lines
        g = groups[line_id]
        for wi, wd in enumerate(g["words"]):
            if wi > 0:
                cursor += 1      # the " " separator between words
            start = cursor
            end = start + len(wd["text"])
            wd["char_span"] = [start, end]
            cursor = end
        parts.append(" ".join(g["texts"]))
    full_text = "\n".join(parts)

    return {"text": full_text, "lines": lines, "words": words}


# ---------------------------------------------------------------------------
# Self-test (behind __main__): word table + summary + overlay + span check.
# Never runs on import. The service never writes files.
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_PNG = os.path.join(HERE, "synthetic_decree_v1.png")
OVERLAY_PNG = os.path.join(HERE, "ocr_overlay_v2.png")


def _verify_char_spans(result: dict) -> None:
    """
    The char_span contract, asserted:
      (1) text[start:end] == word["text"] for every word.
      (2) spans are non-overlapping and in non-decreasing order (sanity).
    If (1) holds for all words, /redact can trust char_span blindly.
    """
    text = result["text"]
    prev_end = -1
    for idx, wd in enumerate(result["words"]):
        start, end = wd["char_span"]
        slice_ = text[start:end]
        assert slice_ == wd["text"], (
            f"span mismatch at word {idx}: char_span={wd['char_span']} "
            f"-> text[{start}:{end}]={slice_!r} but word.text={wd['text']!r}"
        )
        assert start >= prev_end, (
            f"span order violation at word {idx}: start={start} "
            f"< prev_end={prev_end}"
        )
        prev_end = end
    print(f"char_span check: PASS  ({len(result['words'])} words, "
          f"all text[start:end] == word.text)")


def _print_word_table(words: list) -> None:
    print(f"{'#':>3}  {'CONF':>5}  {'BOX (x, y, w, h)':<24}  "
          f"{'LINE':<8}  {'SPAN':<12}  TEXT")
    print("-" * 92)
    for idx, wd in enumerate(words, start=1):
        b = wd["box"]
        box_str = f"({b[0]}, {b[1]}, {b[2]}, {b[3]})"
        s = wd["char_span"]
        span_str = f"[{s[0]},{s[1]})"
        print(f"{idx:>3}  {wd['conf']:>5}  {box_str:<24}  "
              f"{wd['line_id']:<8}  {span_str:<12}  {wd['text']}")


def _summarize(words: list, threshold: int) -> None:
    confs = [wd["conf"] for wd in words]
    low = [(wd["text"], wd["conf"]) for wd in words if wd["conf"] < threshold]

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
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


def _draw_overlay(image_path: str, result: dict, out_path: str,
                  threshold: int) -> None:
    """Word boxes (green/red by conf) + line boxes (blue) for grouping check."""
    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    # line boxes first (drawn under words), distinct color
    for line in result["lines"]:
        x, y, w, h = line["box"]
        draw.rectangle([x, y, x + w, y + h], outline=(0, 90, 220), width=3)

    # word boxes on top
    for wd in result["words"]:
        x, y, w, h = wd["box"]
        color = (200, 0, 0) if wd["conf"] < threshold else (0, 150, 0)
        draw.rectangle([x, y, x + w, y + h], outline=color, width=1)

    img.save(out_path)
    print(f"\nBox overlay saved -> {out_path}")
    print("   blue = line box (grouping),  green = confident word,  "
          "red = below review threshold")


def main() -> None:
    if not os.path.exists(INPUT_PNG):
        raise SystemExit(
            f"Input not found: {INPUT_PNG}\n"
            "Run make_synthetic_doc_v1.py first."
        )
    print(f"Running OCR (lang='{DEFAULT_LANG}') on "
          f"{os.path.basename(INPUT_PNG)} ...\n")
    with open(INPUT_PNG, "rb") as f:
        image_bytes = f.read()
    result = extract(image_bytes)
    _verify_char_spans(result)          # v2: assert the contract before use
    _print_word_table(result["words"])
    _summarize(result["words"], REVIEW_THRESHOLD)
    _draw_overlay(INPUT_PNG, result, OVERLAY_PNG, REVIEW_THRESHOLD)


if __name__ == "__main__":
    main()