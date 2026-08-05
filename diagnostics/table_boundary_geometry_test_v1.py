"""
table_boundary_geometry_test_v1.py

THROWAWAY DIAGNOSTIC -- not a pipeline component. Offline: no network, no
Gemini calls, no wired service touched. Reads a saved /ocr output JSON and
analyzes it. Writes nothing.

PURPOSE
-------
Decide, against real coordinates rather than eyeballing, whether a table that
straddles a source page boundary can be detected from OCR word geometry.

Three questions, in order:

  Q1. What separates a COLUMN gap from an ordinary inter-word gap?
      Answered by sweeping candidate thresholds, not by picking one. The
      script prints the observed gap distribution and then shows how the
      boundary verdict changes (or doesn't) across the sweep. A threshold
      whose verdict is stable over a wide band is a derived threshold; one
      that flips between adjacent values is a guess wearing a number.

  Q2. Do page 2's trailing rows and page 3's leading rows actually share the
      same column bands? Measured, with the deltas printed.

  Q3. Does the rule FALSE-POSITIVE anywhere else? The decree contains a
      3-column asset table (page 3) and a genuinely 2-column signature block
      (page 5) that are NOT continuations. A rule that fires on those is
      worse than no rule: it would merge unrelated tables. The script checks
      every page boundary and reports each verdict.

METHOD
------
Column bands: within a line, sort words by x and split wherever the gap to the
next word exceeds the threshold. Each resulting segment's first word's x is a
band start. A line with >= 2 bands is "grid-like" at that threshold.

Trailing run (page N): walk UP from the last non-footer line, collecting
consecutive grid-like lines whose band starts agree with the run's bands
within BAND_TOL. Leading run (page N+1): same, walking DOWN from the first
content line.

Continuation fires only when ALL hold:
  - trailing run has >= MIN_RUN lines and leading run has >= MIN_RUN lines
  - band COUNTS match
  - every band position agrees within BAND_TOL

MIN_RUN >= 2 kills single-line coincidences. Band-count + position matching is
what distinguishes "same table continues" from "a different table happens to
start here" -- the asset table's columns sit at different x than the child
support table's, so it should not match.

WHAT THIS DOES NOT ESTABLISH
----------------------------
That the two runs are semantically the same table. Matching geometry is strong
evidence, not proof. The escape hatch stays the backstop.

Also: this is ONE document. A threshold stable across these five pages is
evidence about this layout, not a universal constant. Treated as such.

USAGE
-----
  python table_boundary_geometry_test_v1.py /path/to/ocr_output.json
  python table_boundary_geometry_test_v1.py --selftest   # embedded fixture

Input JSON may be either the raw node output (a list wrapping one object with
a "pages" array) or that object directly.

Synthetic data only -- permanent project rule.
"""

import json
import re
import sys

# --- tunables (the point of the script is to TEST these, not trust them) ----
GAP_SWEEP = list(range(60, 421, 20))   # candidate column-gap thresholds (px)
BAND_TOL = 25                          # px tolerance when matching band starts
MIN_RUN = 2                            # min lines each side of the boundary
FOOTER_Y_FRAC = 0.92                   # lines below this * page height = footer
DEFAULT_GAP = 150                      # only for the detailed per-page report


# --- geometry helpers -------------------------------------------------------
def _words_of_line(page, line_id):
    return [w for w in page["words"] if w["line_id"] == line_id]


def line_gaps(words):
    """Horizontal gaps between consecutive words in a line, left to right."""
    ws = sorted(words, key=lambda w: w["box"][0])
    gaps = []
    for prev, nxt in zip(ws, ws[1:]):
        gaps.append(nxt["box"][0] - (prev["box"][0] + prev["box"][2]))
    return gaps


def bands_of_line(words, gap_threshold):
    """Column band start x-positions, splitting the line at large gaps."""
    ws = sorted(words, key=lambda w: w["box"][0])
    if not ws:
        return []
    bands = [ws[0]["box"][0]]
    for prev, nxt in zip(ws, ws[1:]):
        if nxt["box"][0] - (prev["box"][0] + prev["box"][2]) > gap_threshold:
            bands.append(nxt["box"][0])
    return bands


def bands_match(a, b, tol=BAND_TOL):
    return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b))


# --- page structure ---------------------------------------------------------
_FOOTER_TEXT = re.compile(
    r"(Page\s+\d+\s*[—-]|Sayfa\s+\d+\s*[—-]|FABRICATED DOCUMENT|"
    r"YAZILIM TEST)", re.IGNORECASE)


def is_footer(line, page_height):
    """Footer by geometry first (position on the page), text as a backstop."""
    y = line["box"][1]
    if page_height and y > FOOTER_Y_FRAC * page_height:
        return True
    return bool(_FOOTER_TEXT.search(line["text"]))


def content_lines(page):
    h = (page.get("image_dimensions") or {}).get("height", 0)
    return [ln for ln in page["lines"] if not is_footer(ln, h)]


def _run(page, lines, gap_threshold):
    """Collect a maximal run of consecutive grid-like lines with stable bands.
    `lines` is already ordered in the direction of travel."""
    run, ref = [], None
    for ln in lines:
        b = bands_of_line(_words_of_line(page, ln["line_id"]), gap_threshold)
        if len(b) < 2:
            break
        if ref is None:
            ref = b
        elif not bands_match(b, ref):
            break
        run.append((ln, b))
    return run, ref


def trailing_run(page, gap_threshold):
    return _run(page, list(reversed(content_lines(page))), gap_threshold)


def leading_run(page, gap_threshold):
    return _run(page, content_lines(page), gap_threshold)


def continuation_verdict(page_n, page_n1, gap_threshold):
    tail, tail_bands = trailing_run(page_n, gap_threshold)
    head, head_bands = leading_run(page_n1, gap_threshold)
    fires = (
        len(tail) >= MIN_RUN and len(head) >= MIN_RUN
        and tail_bands is not None and head_bands is not None
        and bands_match(tail_bands, head_bands)
    )
    return {
        "fires": fires,
        "tail_lines": len(tail), "head_lines": len(head),
        "tail_bands": tail_bands, "head_bands": head_bands,
        "tail": tail, "head": head,
    }


# --- reports ----------------------------------------------------------------
def report_gap_distribution(pages):
    print("=" * 74)
    print("Q1  INTRA-LINE WORD GAP DISTRIBUTION (all pages, all lines)")
    print("=" * 74)
    all_gaps = []
    for p in pages:
        for ln in p["lines"]:
            all_gaps.extend(line_gaps(_words_of_line(p, ln["line_id"])))
    all_gaps.sort()
    if not all_gaps:
        print("no gaps found")
        return
    n = len(all_gaps)
    print(f"gaps measured: {n}   min={all_gaps[0]}  max={all_gaps[-1]}")
    for q in (50, 75, 90, 95, 98, 99):
        print(f"  p{q:<3} = {all_gaps[int(n * q / 100)]:>5} px")
    print("\nlargest 15 gaps, with the line they came from:")
    rows = []
    for p in pages:
        for ln in p["lines"]:
            ws = _words_of_line(p, ln["line_id"])
            for g in line_gaps(ws):
                rows.append((g, p["page_index"], ln["text"]))
    rows.sort(reverse=True)
    for g, pi, text in rows[:15]:
        print(f"  {g:>5} px  p{pi}  {text[:62]}")
    print("\nNOTE: a clean separation shows as a visible jump between ordinary")
    print("word spacing and column gaps. If the two ranges overlap, no single")
    print("threshold is safe and the sweep below will show verdicts flipping.")


def report_sweep(pages):
    print("\n" + "=" * 74)
    print("Q1/Q3  THRESHOLD SWEEP -- which boundaries fire, at each threshold")
    print("=" * 74)
    print("A derived threshold is one where the verdict row is IDENTICAL across")
    print("a wide range. Flipping verdicts = the threshold is doing the work,")
    print("not the geometry.\n")
    boundaries = [(pages[i], pages[i + 1]) for i in range(len(pages) - 1)]
    labels = [f"{a['page_index']}->{b['page_index']}" for a, b in boundaries]
    print(f"{'gap':>5}  " + "  ".join(f"{l:>6}" for l in labels))
    print("-" * 74)
    for gt in GAP_SWEEP:
        cells = []
        for a, b in boundaries:
            v = continuation_verdict(a, b, gt)
            cells.append("FIRE" if v["fires"] else "-")
        print(f"{gt:>5}  " + "  ".join(f"{c:>6}" for c in cells))
    print("\nEXPECTED (this document): exactly one column fires -- the boundary")
    print("holding the split child-support table. Any other column firing is a")
    print("false positive and must be understood before wiring anything.")


def report_boundaries(pages, gap_threshold):
    print("\n" + "=" * 74)
    print(f"Q2  PER-BOUNDARY DETAIL at gap threshold = {gap_threshold} px")
    print("=" * 74)
    for a, b in [(pages[i], pages[i + 1]) for i in range(len(pages) - 1)]:
        v = continuation_verdict(a, b, gap_threshold)
        print(f"\n--- page_index {a['page_index']} -> {b['page_index']}  "
              f"{'CONTINUATION' if v['fires'] else 'no continuation'} ---")
        print(f"  trailing run: {v['tail_lines']} line(s)  bands={v['tail_bands']}")
        for ln, bands in v["tail"]:
            print(f"      {bands}  {ln['text'][:56]}")
        print(f"  leading  run: {v['head_lines']} line(s)  bands={v['head_bands']}")
        for ln, bands in v["head"]:
            print(f"      {bands}  {ln['text'][:56]}")
        if v["tail_bands"] and v["head_bands"] and \
                len(v["tail_bands"]) == len(v["head_bands"]):
            deltas = [abs(x - y) for x, y in zip(v["tail_bands"], v["head_bands"])]
            print(f"  band deltas: {deltas} px   (tolerance {BAND_TOL})")
        if v["fires"]:
            first_ln, _ = v["tail"][-1]   # topmost line of the trailing run
            print(f"  -> hint header would be sourced from: {first_ln['text'][:56]!r}")


# --- self-test fixture (transcribed subset of the real /ocr output) ---------
def _w(text, x, y, w, h, lid):
    return {"text": text, "box": [x, y, w, h], "conf": 90, "line_id": lid}


def _selftest_pages():
    """Smoke test only: proves the code runs and the band matcher behaves.
    NOT a substitute for the real run -- the sweep needs all five pages."""
    p2_words = [
        _w("Item", 326, 2838, 77, 41, "14:1:1"),
        _w("Amount", 1255, 2843, 138, 27, "14:1:1"),
        _w("/", 1403, 2843, 11, 27, "14:1:1"),
        _w("Detail", 1425, 2841, 99, 29, "14:1:1"),
        _w("Monthly", 326, 2933, 135, 37, "17:1:1"),
        _w("base", 471, 2933, 69, 28, "17:1:1"),
        _w("child", 552, 2933, 77, 28, "17:1:1"),
        _w("support", 642, 2939, 117, 30, "17:1:1"),
        _w("obligation", 770, 2933, 158, 37, "17:1:1"),
        _w("$1,240.00", 1257, 2932, 154, 35, "17:1:1"),
        _w("Page", 755, 3127, 67, 31, "19:1:1"),
    ]
    p3_words = [
        _w("Payor", 326, 314, 91, 35, "10:1:1"),
        _w("Jordan", 1256, 312, 103, 28, "10:1:1"),
        _w("A.", 1370, 314, 36, 27, "10:1:1"),
        _w("Millbrook", 1419, 312, 160, 28, "10:1:1"),
        _w("Payee", 326, 406, 91, 35, "10:2:1"),
        _w("Casey", 1257, 406, 94, 35, "10:2:1"),
        _w("R.", 1362, 406, 33, 27, "10:2:1"),
        _w("Millbrook", 1408, 404, 160, 28, "10:2:1"),
        _w("Effective", 326, 495, 142, 28, "10:3:1"),
        _w("date", 480, 495, 64, 28, "10:3:1"),
        _w("July", 1256, 495, 65, 37, "10:3:1"),
        _w("1,", 1333, 497, 24, 32, "10:3:1"),
        _w("2025", 1372, 497, 75, 26, "10:3:1"),
    ]
    return [
        {"page_index": 1, "image_dimensions": {"width": 2550, "height": 3300},
         "words": p2_words, "lines": [
             {"line_id": "14:1:1", "text": "Item Amount / Detail",
              "box": [326, 2838, 1198, 41]},
             {"line_id": "17:1:1",
              "text": "Monthly base child support obligation $1,240.00",
              "box": [326, 2932, 1085, 38]},
             {"line_id": "19:1:1", "text": "Page 2 — Synthetic Test Data Only",
              "box": [755, 3125, 1039, 33]}]},
        {"page_index": 2, "image_dimensions": {"width": 2550, "height": 3300},
         "words": p3_words, "lines": [
             {"line_id": "10:1:1", "text": "Payor Jordan A. Millbrook",
              "box": [326, 312, 1253, 37]},
             {"line_id": "10:2:1", "text": "Payee Casey R. Millbrook",
              "box": [326, 404, 1242, 37]},
             {"line_id": "10:3:1", "text": "Effective date July 1, 2025",
              "box": [326, 495, 1121, 37]}]},
    ]


def _selftest():
    pages = _selftest_pages()
    v = continuation_verdict(pages[0], pages[1], DEFAULT_GAP)
    assert v["fires"], "fixture should fire a continuation"
    assert v["tail_lines"] == 2 and v["head_lines"] == 3, \
        f"unexpected run lengths: {v['tail_lines']}, {v['head_lines']}"
    assert len(v["tail_bands"]) == 2, "expected 2 column bands"
    # footer must be excluded from the trailing run
    assert all("Page 2" not in ln["text"] for ln, _ in v["tail"]), \
        "footer leaked into the trailing run"
    print("selftest: PASS  (bands "
          f"{v['tail_bands']} vs {v['head_bands']}, runs "
          f"{v['tail_lines']}/{v['head_lines']})")
    report_boundaries(pages, DEFAULT_GAP)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip().splitlines()[-6])
    if sys.argv[1] == "--selftest":
        _selftest()
        return
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        data = data[0]
    pages = sorted(data["pages"], key=lambda p: p["page_index"])
    missing = [p["page_index"] for p in pages if not p.get("words")]
    if missing:
        sys.exit(f"pages missing 'words' geometry: {missing}")
    print(f"loaded {len(pages)} pages from {sys.argv[1]}")
    report_gap_distribution(pages)
    report_sweep(pages)
    report_boundaries(pages, DEFAULT_GAP)


if __name__ == "__main__":
    main()