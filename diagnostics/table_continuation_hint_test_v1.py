"""
table_continuation_hint_test_v1.py

THROWAWAY DIAGNOSTIC -- not a pipeline component.
Purpose: decide whether a "continuation hint" prepended to a page whose opening
lines continue a table begun on the PREVIOUS source page makes Gemini emit those
lines as Markdown TABLE ROWS (instead of prose) -- and, critically, whether it
splits OCR-fused rows correctly -- under the project's real translation prompt,
in both EN->TR and TR->EN, for BOTH candidate hint styles.

Why this exists (the bug it probes)
-----------------------------------
The synthetic decree's child-support table straddles a source page boundary.
Page 2 (page_index=1) holds the header + first row; page 3 (page_index=2) opens
with the remaining 6 rows. Because pages are translated one call per page, the
page-3 call sees six bare label-value lines with no header and no table cue, and
translates them as prose. Downstream, md_render_v1 needs a real table to mirror
the source. This script tests the fix candidate at the ONLY place the lost
structure can be restored: the translation call's prompt.

Two hint styles under test (they feed two different render strategies)
----------------------------------------------------------------------
  "headerless" -> emit pipe rows WITHOUT re-emitting header/separator.
                  (feeds render Path A: cross-page splice into one table.)
  "full"       -> emit a COMPLETE table, re-emitting header + separator row.
                  (feeds render Path B: a second, adjacent table.)

Extra stressor baked into the real fixture
-------------------------------------------
Page-3's structured_text glues THREE logical rows onto one physical line
("Payment method ... State Disbursement Unit Health insurance coverage ...
employer plan Uninsured medical/dental expenses ... Respondent"). A correct
result splits these into three 2-column rows. A wrong-but-confident result
mashes them; the acceptable-uncertain result fires the escape hatch. All three
outcomes are distinguished below.

What it does NOT assert
-----------------------
It does not judge whether the translated Turkish/English CELL TEXT is correct --
that's a human call, so raw output is dumped for the eye. The machine verdict is
purely structural (row count, column count, header/separator presence, escape
hatch, hint leak). No "correct translation" is hardcoded.

Writes no files. Imports no versioned core. Reads GEMINI_API_KEY from env.
All fixtures are fully synthetic -- PII is already tokenized ([PERSON_1] etc.),
exactly as the real /anonymize output that feeds translation. No real PII.

Run:  python table_continuation_hint_test_v1.py
"""

import os
import re
import sys
import time

# --- Gemini client -----------------------------------------------------------
# Same SDK/model as placeholder_survival_test_v1 and the production translate
# node, so results transfer. Install into abclink if absent:
#   pip install google-genai
try:
    from google import genai
except ImportError:
    sys.exit("google-genai not installed. Run: pip install google-genai")

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    sys.exit("GEMINI_API_KEY not set. Export it in the abclink env before running.")

client = genai.Client(api_key=API_KEY)
MODEL_NAME = "gemini-2.5-flash"  # MUST match the production translate node.

REPS = 3            # reps per (direction x hint style) -- model is nondeterministic
THROTTLE_S = 13     # ~5 req/min free-tier ceiling; lower if your key allows more

# A sentinel wrapped around the hint so we can detect leakage unambiguously:
# if it shows up in the output, the model reproduced the context note.
SENTINEL = "\u00abCONTINUATION-CONTEXT\u00bb"   # «CONTINUATION-CONTEXT»

# The escape-hatch marker the production prompt defines (em-dash), plus an
# ASCII-hyphen fallback in case the model normalizes the dash.
HATCH_EM = "[TABLE \u2014 STRUCTURE UNCERTAIN, REVIEW]"
HATCH_ASCII = "[TABLE - STRUCTURE UNCERTAIN, REVIEW]"


# --- Fixtures ----------------------------------------------------------------
# EN->TR fixture: the ACTUAL page-3 (page_index=2) child-support continuation
# region, copied verbatim from Build Structured Text -- including the OCR garble
# (5596 for 55%, 4596 for 45%) and the three rows fused onto one physical line.
# A trailing section header + first clause is appended as the real boundary, so
# the model must ALSO decide where the table ends and prose resumes.
EN_CONTINUATION = (
    "Payor [PERSON_1]\n\n"
    "Payee [PERSON_2]\n\n"
    "Effective date [DATE_TIME_1], [DATE_TIME_2]\n\n"
    "Payment method Income withholding order / State Disbursement Unit "
    "Health insurance coverage for Minor Children Maintained by Respondent "
    "through employer plan Uninsured medical/dental expenses Shared 5596 "
    "Petitioner / 4596 Respondent\n\n"
    "V. SPOUSAL SUPPORT\n\n"
    "5.1. Petitioner shall pay to Respondent spousal maintenance in the amount "
    "of $600.00 per month, commencing [DATE_TIME_3]."
)

# TR->EN fixture: a structurally-PARALLEL synthetic Turkish continuation of the
# same shape -- 3 clean label-value rows + 3 rows fused onto one line + a
# boundary section. Fully fabricated; tokens stand in for PII. This checks the
# hint works symmetrically, not that any specific translation is "right".
TR_CONTINUATION = (
    "\u00d6deyici [PERSON_1]\n\n"
    "Al\u0131c\u0131 [PERSON_2]\n\n"
    "Y\u00fcr\u00fcrl\u00fck tarihi [DATE_TIME_1], [DATE_TIME_2]\n\n"
    "\u00d6deme y\u00f6ntemi Gelirden kesinti emri / Eyalet \u00d6deme Birimi "
    "K\u00fc\u00e7\u00fck \u00c7ocuklar i\u00e7in sa\u011fl\u0131k "
    "sigortas\u0131 kapsam\u0131 \u0130\u015fveren plan\u0131 \u00fczerinden "
    "Cevap Veren taraf\u0131ndan sa\u011flan\u0131r Sigortas\u0131z "
    "t\u0131bbi/di\u015f masraflar\u0131 Payla\u015f\u0131ml\u0131 %55 "
    "Dilek\u00e7e Sahibi / %45 Cevap Veren\n\n"
    "V. NAFAKA\n\n"
    "5.1. Dilek\u00e7e Sahibi, Cevap Veren'e ayl\u0131k $600.00 nafaka "
    "\u00f6deyecektir; ba\u015flang\u0131\u00e7 [DATE_TIME_3]."
)

# The previous page's column headers, in the SOURCE language of each direction
# (page-3 input is source-language; the header is context for column semantics,
# never re-translated as document text).
FIXTURES = {
    "en>tr": {"text": EN_CONTINUATION, "header": ("Item", "Amount / Detail")},
    "tr>en": {"text": TR_CONTINUATION, "header": ("\u00d6\u011fe", "Miktar / Detay")},
}

# Expected data rows for both fixtures: Payor/Payee/Effective/Payment/Health/
# Uninsured -> 6 rows, each 2 columns. Language-agnostic ground truth.
EXPECTED_DATA_ROWS = 6
EXPECTED_COLS = 2


# --- Hint construction -------------------------------------------------------
def build_hint(style, header):
    """Return the continuation-context preface, wrapped in SENTINEL markers so
    leakage is detectable. `style` is 'headerless' or 'full'."""
    cols = f'"{header[0]}" | "{header[1]}"'
    if style == "headerless":
        directive = (
            "The lines that follow CONTINUE a two-column table begun on the "
            f"previous page, whose columns are {cols}. Render them as Markdown "
            "table rows in pipe format, one row per record. Do NOT emit a "
            "header row and do NOT emit a separator row -- the header already "
            "appeared on the previous page. Where several records were run "
            "together on one line by OCR, split them into separate rows."
        )
    elif style == "full":
        directive = (
            "The lines that follow CONTINUE a two-column table begun on the "
            f"previous page, whose columns are {cols}. Render them as a "
            "COMPLETE Markdown table: re-emit the header row and its separator "
            "row, then one row per record. Where several records were run "
            "together on one line by OCR, split them into separate rows."
        )
    else:
        raise ValueError(f"unknown hint style: {style}")
    return (
        f"{SENTINEL}\n{directive}\nThis context note is instructions only; do "
        f"NOT translate it and do NOT reproduce it in your output.\n{SENTINEL}\n\n"
    )


# --- Production prompt (reproduced faithfully from the translate node) --------
def build_prompt(direction, text_with_hint):
    src, tgt = ("Turkish", "English") if direction == "tr>en" else ("English", "Turkish")
    return f"""You are a certified legal translator. Translate the document text below from {src} into {tgt}.
CONTENT RULES
- Translate everything. Do not summarize, explain, comment, or omit anything. Do not add content that is not in the source.
- Use formal legal register appropriate to the target language.
- Preserve verbatim, without translating: case numbers, docket numbers, account numbers, monetary figures, alphanumeric identifiers, form codes (e.g. FL-190), and URLs.
- For statutory and legal citations, preserve the numbers verbatim but translate the descriptive words around them. Example: "Family Code, \u00a7\u00a7 2330, 7636, 7637" becomes "Medeni Kanun 2330, 7636, 7637 maddeleri". The section numbers never change; the label and the \u00a7 symbol are rendered in the target language.
- Mark any genuinely illegible passage as [ILLEGIBLE]. Keep this marker exactly, in English, regardless of direction.
PLACEHOLDER TOKENS
- The text contains tokens in square brackets with an underscore and number, like [PERSON_1], [LOCATION_2], [DATE_TIME_3]. These are opaque identifiers standing in for redacted content.
- Reproduce each token exactly -- same brackets, same word, same number. Never translate, reformat, renumber, space, or alter them in any way.
- You may reposition a token within its sentence so the surrounding translation reads naturally in the target language. The token's internal text must remain untouched.
OUTPUT FORMAT -- MARKDOWN
Produce the translation as Markdown that mirrors the structure of the source.
- Use # for the document's main title and ## for section headings.
- Separate paragraphs with a blank line, exactly as they appear in the input.
- Section and clause numbers (for example I., VIII., 1.1., 2.3.) are LITERAL TEXT. Reproduce them exactly as written. Do NOT convert them into Markdown numbered or ordered lists, and do NOT use Markdown list syntax (1., -, *) anywhere. A heading keeps its number inline, e.g. "## I. FINDINGS OF FACT".
- Render selection marks textually: a checked box as (X), an unchecked box as ( ).
- For a stamp or seal containing readable text, translate that text in place. For a purely graphical mark with no readable text, insert a bracketed note in the target language describing it, e.g. [\u0130mza] / [signature], [m\u00fch\u00fcr] / [seal].
- Mirror page-number lines in the target language, e.g. "Page 1 of 1" becomes "Sayfa 1/1".
TABLES -- WITH AN UNCERTAINTY ESCAPE HATCH
Some input arrives as tabular content whose row and column structure was damaged by OCR (missing delimiters, fused rows, glued words).
- If you can confidently reconstruct the rows and columns, render a proper Markdown table: header row, separator row, one row per record.
- If the structure is genuinely ambiguous and you cannot reconstruct it with confidence, DO NOT GUESS. Translate the content as plain text lines and prefix the whole block, on its own line, with this exact marker: [TABLE \u2014 STRUCTURE UNCERTAIN, REVIEW]. Keep this marker exactly, in English, regardless of direction.
- Never invent a column assignment to make a table look clean. A wrong cell placement in a legal table is a substantive error; a flagged uncertain block is not.
Text to translate:
{text_with_hint}
Output only the translated Markdown. No preamble, no notes, no code fences, no surrounding quotation marks."""


# --- Scoring -----------------------------------------------------------------
_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")  # same shape md_render uses


def _pipe_rows(text):
    return [ln.strip() for ln in text.splitlines()
            if ln.strip().startswith("|") and ln.strip().endswith("|")
            and len(ln.strip()) > 1]


def _cells(row):
    parts = row.split("|")
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [c.strip() for c in parts]


def score(output):
    rows = _pipe_rows(output)
    sep_rows = [r for r in rows if _SEP_RE.match(r)]
    data_rows = [r for r in rows if not _SEP_RE.match(r)]
    # In "full" style the first non-separator pipe row is the re-emitted header;
    # we report data_rows raw and let the report interpret per style.
    col_counts = [len(_cells(r)) for r in data_rows]
    return {
        "emitted_pipe_rows": len(rows) > 0,
        "n_data_rows": len(data_rows),
        "col_counts": col_counts,
        "all_two_col": bool(col_counts) and all(c == EXPECTED_COLS for c in col_counts),
        "has_separator": len(sep_rows) > 0,
        "hatch_fired": (HATCH_EM in output) or (HATCH_ASCII in output),
        "hint_leaked": SENTINEL in output,
        "raw": output,
    }


# --- Runner ------------------------------------------------------------------
def call_gemini(prompt):
    for attempt in range(5):
        try:
            resp = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            return resp.text or ""
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                m = re.search(r"retry in ([\d.]+)s", msg) or \
                    re.search(r"retryDelay['\"]?:\s*['\"]?(\d+)", msg)
                wait = float(m.group(1)) + 1 if m else 15.0
                print(f"    [rate-limited] waiting {wait:.0f}s "
                      f"(attempt {attempt + 1}/5)")
                time.sleep(wait)
                continue
            print(f"    [API error] {e}")
            return ""
    return ""


def run():
    styles = ("headerless", "full")
    directions = ("en>tr", "tr>en")
    results = {}  # (direction, style) -> list of score dicts

    for direction in directions:
        fx = FIXTURES[direction]
        for style in styles:
            key = (direction, style)
            results[key] = []
            hint = build_hint(style, fx["header"])
            prompt = build_prompt(direction, hint + fx["text"])
            for rep in range(REPS):
                print(f"[call] {direction} / {style} / rep {rep + 1}/{REPS}")
                out = call_gemini(prompt)
                results[key].append(score(out))
                time.sleep(THROTTLE_S)

    # --- report ---
    print("\n" + "=" * 72)
    print(f"TABLE CONTINUATION HINT -- model: {MODEL_NAME}  reps/cell: {REPS}")
    print(f"expected: {EXPECTED_DATA_ROWS} data rows x {EXPECTED_COLS} cols "
          f"(headerless: no separator; full: separator present)")
    print("=" * 72)
    header = (f"{'direction':<8} {'style':<11} {'pipe?':<6} {'datarows':<9} "
              f"{'2col?':<6} {'sep?':<6} {'hatch':<6} {'leak':<6}")
    print(header)
    print("-" * 72)
    for direction in directions:
        for style in styles:
            for i, r in enumerate(results[(direction, style)]):
                tag = f"{direction:<8} {style:<11}" if i == 0 else f"{'':<8} {'':<11}"
                print(f"{tag} "
                      f"{('Y' if r['emitted_pipe_rows'] else 'n'):<6} "
                      f"{r['n_data_rows']:<9} "
                      f"{('Y' if r['all_two_col'] else 'n'):<6} "
                      f"{('Y' if r['has_separator'] else 'n'):<6} "
                      f"{('Y' if r['hatch_fired'] else 'n'):<6} "
                      f"{('LEAK' if r['hint_leaked'] else '-'):<6}")
            print("-" * 72)

    # --- raw dump: first rep of each cell, so cell-text correctness is eyeballable ---
    print("\nRAW OUTPUT (first rep per cell) -- inspect fused-row split + cell text:")
    for direction in directions:
        for style in styles:
            r0 = results[(direction, style)][0]
            print("\n" + "-" * 72)
            print(f"[{direction} / {style}] raw:")
            print(r0["raw"])


if __name__ == "__main__":
    run()