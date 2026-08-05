"""
order_signature_block_stability_test_v1.py

THROWAWAY DIAGNOSTIC — not a pipeline component. Same convention as
placeholder_survival_test_v1.py / v2.py and table_continuation_hint_test:
scoped narrowly to one open question, makes live calls to the production
translation model, writes no files, imports no versioned core.

QUESTION THIS ANSWERS
----------------------
Is the "IX. ORDER" / "APPROVED AS TO FORM AND CONTENT:" signature block
(page_index=4 of the synthetic divorce decree) stable across independent
calls against the UNMODIFIED production prompt and BYTE-IDENTICAL input?

Three real prior outputs against this exact block disagreed:
  - July 29 PDF: uncertainty escape hatch fired (flagged plain text) — but
    even that flagged output had the row pairing already scrambled
    underneath the flag.
  - A separately-triggered call: unflagged plain paragraphs, no table.
  - Today's delivered PDF: unflagged, confidently formatted as a table with
    swapped Petitioner/Respondent attribution.

This is confirmed unrelated to the continuation-hint/splice work — this page
never fires table_continuation (table_continuation.fires == false for this
page in production execution data), and its prompt input hasn't changed.

WHAT THIS SCRIPT DOES
----------------------
Feeds the exact page_index=4 structured_text (verbatim, from a real n8n
execution) through the unmodified production prompt template N=15 times and
classifies each output into one of three buckets:
    hatch   — the [TABLE — STRUCTURE UNCERTAIN, REVIEW] marker fired
    table   — no hatch marker, but the output contains a block that would
              actually render as a Word table (uses md_render_v1.py's own
              _looks_like_table / _SEP_RE logic, copied verbatim, so "table"
              here means what production's renderer would treat as a table —
              not a re-guessed approximation of it)
    prose   — neither of the above

For every "table" outcome, a SECOND check scores attribution: does
[PERSON_2] end up correctly grouped with the Petitioner side (Ashworth &
Doyle, LLP) and [PERSON_3] with the Respondent side (Whitfield Family Law
Group)? Ground truth confirmed directly by the project owner:
    PERSON_2 = Ashworth & Doyle, LLP  -> Attorney for Petitioner
    PERSON_3 = Whitfield Family Law Group -> Attorney for Respondent

ATTRIBUTION SCORING IS A HEURISTIC — NOT A CERTAINTY
------------------------------------------------------
The scorer looks for the Turkish stems "davac" (Davacı = Petitioner) and
"daval" (Davalı = Respondent) in the row containing each PERSON placeholder,
cross-checked against the firm names (which are proper nouns and likely to
survive untranslated). This has NOT been validated against every possible
Turkish inflection or phrasing Gemini might produce. Any case the heuristic
can't resolve cleanly is reported as "ambiguous" rather than forced into
correct/incorrect. The FULL raw row text is logged for every table outcome
so a human can spot-check the automated verdict rather than trust it blind.

100% synthetic (fabricated specimen document). Placeholders present —
already anonymized, consistent with the rest of the pipeline's privacy
posture. No real PII in this file or in any output it produces.

Run:  python order_signature_block_stability_test_v1.py
Requires: GEMINI_API_KEY in env, google-genai installed in the abclink env.
"""

import os
import re
import sys
import time

try:
    from google import genai
except ImportError:
    sys.exit("google-genai not installed. Run: pip install google-genai")

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    sys.exit("GEMINI_API_KEY not set. Export it in the abclink env before running.")

client = genai.Client(api_key=API_KEY)

# Confirmed production model (Jacob, Aug 2026) — do not swap without
# re-confirming against the live n8n node config. A model mismatch would
# make this diagnostic answer a different question than the one asked.
MODEL_NAME = "gemini-2.5-flash"

N_REPS = 15

# On a paid tier now — no artificial throttle needed. Light courtesy spacer
# only; the retry/backoff loop below is the real safety net against 429s.
CALL_SPACER_SECONDS = 2.0

# --- exact production input, verbatim from a real n8n execution ------------
# page_index=4, direction=en>tr, table_continuation.fires=false,
# continuation_hint="" (empty — this page is unaffected by the continuation-
# hint work, confirming the instability predates and is unrelated to it).
STRUCTURED_TEXT = "IX. ORDER\n\nIT IS SO ORDERED, ADJUDGED, AND DECREED this June 24, [DATE_TIME_1], at [LOCATION_1], State of Franklin.\n\nThe Honorable [PERSON_1] Judge, COURT OF COMMON PLEAS OF [LOCATION_2]\n\nAPPROVED AS TO FORM AND CONTENT:\n\n[PERSON_2], Esquire [PERSON_3], Esquire\n\nAshworth & Doyle, LLP Whitfield Family Law Group\n\n100 [LOCATION_3], Suite 300, Rivergrove, FR 200 Sample Street, Suite 12, Rivergrove, FR 00000 00000 Attorney for Respondent\n\nAttorney for Petitioner\n\nCLERK'S CERTIFICATION\n\nI hereby certify that the foregoing is a true and correct copy of the Final Decree of Divorce entered in Case No. FD-[CASE_NUMBER_1] on [DATE_TIME_2], as reflected in the fabricated test records of this specimen document.\n\nClerk of Courts, Fictional County (Specimen Seal)\n\nNOTICE: This is a synthetic specimen document generated for software quality assurance and workflow-testing purposes only. All names, dates, addresses, case numbers, financial figures, and identifiers appearing above are entirely fictional and were fabricated for testing. This document does not describe any real marriage, legal proceeding, court, judge, attorney, or individual, whether living or deceased, and must not be used, filed, submitted, or relied upon as an actual legal record for any purpose.\n\nPage 5 — Synthetic Test Data Only — Case No. FD-[CASE_NUMBER_1] FABRICATED DOCUMENT FOR SOFTWARE TESTING — NO REAL PERSONS, EVENTS, OR RECORDS DEPICTED"

# direction = en>tr -> from English into Turkish (resolved from the
# production template's conditional, not re-derived)
PROMPT = f"""Translate the document text below from English into Turkish.

CONTENT RULES
- Translate everything. Do not summarize, explain, comment, or omit anything. Do not add content that is not in the source.
- Use formal legal register appropriate to the target language.
- Preserve verbatim, without translating: case numbers, docket numbers, account numbers, monetary figures, alphanumeric identifiers, form codes (e.g. FL-190), and URLs.
- For statutory and legal citations, preserve the numbers verbatim but translate the descriptive words around them. Example: "Family Code, §§ 2330, 7636, 7637" becomes "Medeni Kanun 2330, 7636, 7637 maddeleri". The section numbers never change; the label and the § symbol are rendered in the target language.
- Mark any genuinely illegible passage as [ILLEGIBLE]. Keep this marker exactly, in English, regardless of direction.

PLACEHOLDER TOKENS
- The text contains tokens in square brackets with an underscore and number, like [PERSON_1], [LOCATION_2], [DATE_TIME_3]. These are opaque identifiers standing in for redacted content.
- Reproduce each token exactly — same brackets, same word, same number. Never translate, reformat, renumber, space, or alter them in any way.
- You may reposition a token within its sentence so the surrounding translation reads naturally in the target language. The token's internal text must remain untouched.

OUTPUT FORMAT — MARKDOWN
Produce the translation as Markdown that mirrors the structure of the source.
- Use # for the document's main title and ## for section headings.
- Separate paragraphs with a blank line, exactly as they appear in the input.
- Section and clause numbers (for example I., VIII., 1.1., 2.3.) are LITERAL TEXT. Reproduce them exactly as written. Do NOT convert them into Markdown numbered or ordered lists, and do NOT use Markdown list syntax (1., -, *) anywhere. A heading keeps its number inline, e.g. "## I. FINDINGS OF FACT".
- Render selection marks textually: a checked box as (X), an unchecked box as ( ).
- For a stamp or seal containing readable text, translate that text in place. For a purely graphical mark with no readable text, insert a bracketed note in the target language describing it, e.g. [İmza] / [signature], [mühür] / [seal].
- Mirror page-number lines in the target language, e.g. "Page 1 of 1" becomes "Sayfa 1/1".

TABLES — WITH AN UNCERTAINTY ESCAPE HATCH
Some input arrives as tabular content whose row and column structure was damaged by OCR (missing delimiters, fused rows, glued words).
- If you can confidently reconstruct the rows and columns, render a proper Markdown table: header row, separator row, one row per record.
- If the structure is genuinely ambiguous and you cannot reconstruct it with confidence, DO NOT GUESS. Translate the content as plain text lines and prefix the whole block, on its own line, with this exact marker: [TABLE — STRUCTURE UNCERTAIN, REVIEW]. Keep this marker exactly, in English, regardless of direction.
- Never invent a column assignment to make a table look clean. A wrong cell placement in a legal table is a substantive error; a flagged uncertain block is not.

Text to translate:
{STRUCTURED_TEXT}

Output only the translated Markdown. No preamble, no notes, no code fences, no surrounding quotation marks."""

# --- classifier constants, copied verbatim from md_render_v1.py ------------
# (so "table" here means what production's renderer would actually treat as
# a table, not a re-guessed approximation of it)
REVIEW_MARKER = "[TABLE \u2014 STRUCTURE UNCERTAIN, REVIEW]"  # em-dash inside
_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")


def _looks_like_table(lines):
    return (len(lines) >= 2
            and lines[0].lstrip().startswith("|")
            and _SEP_RE.match(lines[1]) is not None)


def classify_response(text):
    if REVIEW_MARKER in text:
        return "hatch"
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip() != ""]
        if _looks_like_table(lines):
            return "table"
    return "prose"


def score_attribution(text):
    """Heuristic only. Returns (verdict, note, person2_row, person3_row).
    verdict in {"correct", "incorrect", "ambiguous"}.
    Ground truth: PERSON_2 = Ashworth & Doyle, LLP = Attorney for Petitioner
                  PERSON_3 = Whitfield Family Law Group = Attorney for Respondent
    """
    rows = [r for r in text.split("\n") if r.strip()]
    person2_row = next((r for r in rows if "[PERSON_2]" in r), None)
    person3_row = next((r for r in rows if "[PERSON_3]" in r), None)

    if person2_row is None or person3_row is None:
        return ("ambiguous",
                "could not locate one or both PERSON placeholders as a distinct row",
                person2_row, person3_row)

    def side(row):
        r = row.lower()
        petitioner_signal = ("davac" in r) or ("ashworth" in r)
        respondent_signal = ("daval" in r) or ("whitfield" in r)
        if petitioner_signal and not respondent_signal:
            return "petitioner"
        if respondent_signal and not petitioner_signal:
            return "respondent"
        return "unclear"

    side2, side3 = side(person2_row), side(person3_row)

    if side2 == "petitioner" and side3 == "respondent":
        verdict = "correct"
    elif side2 == "respondent" and side3 == "petitioner":
        verdict = "incorrect"
    else:
        verdict = "ambiguous"

    return verdict, f"PERSON_2 read as {side2}, PERSON_3 read as {side3}", person2_row, person3_row


def call_gemini(prompt, max_retries=6):
    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            return resp.text
        except Exception as e:
            msg = str(e)
            is_rate_limit = "429" in msg or "RESOURCE_EXHAUSTED" in msg or "rate" in msg.lower()
            if is_rate_limit and attempt < max_retries - 1:
                m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+)", msg)
                wait = float(m.group(1)) + 1 if m else 20.0
                print(f"  [rate-limited] waiting {wait:.0f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            print(f"[API error] {e}")
            return None
    return None


def run():
    buckets = {"hatch": [], "table": [], "prose": []}
    attribution = {"correct": [], "incorrect": [], "ambiguous": []}

    for i in range(N_REPS):
        print(f"[{i + 1}/{N_REPS}] calling {MODEL_NAME}...")
        text = call_gemini(PROMPT)
        if text is None:
            print(f"  [skipped] rep {i + 1} — no response after retries")
            continue

        outcome = classify_response(text)
        buckets[outcome].append(text)
        print(f"  -> {outcome}")

        if outcome == "table":
            verdict, note, p2_row, p3_row = score_attribution(text)
            attribution[verdict].append({
                "rep": i + 1, "note": note,
                "person2_row": p2_row, "person3_row": p3_row,
            })
            print(f"     attribution: {verdict} ({note})")

        time.sleep(CALL_SPACER_SECONDS)

    print("\n" + "=" * 70)
    print(f"ORDER SIGNATURE BLOCK STABILITY — model: {MODEL_NAME}, N={N_REPS}")
    print("=" * 70)
    total_classified = sum(len(v) for v in buckets.values())
    for name in ("hatch", "table", "prose"):
        n = len(buckets[name])
        pct = (n / total_classified * 100) if total_classified else 0
        print(f"  {name:<8} {n}/{total_classified}  ({pct:.0f}%)")

    if buckets["table"]:
        print("\nAttribution breakdown within 'table' outcomes:")
        for name in ("correct", "incorrect", "ambiguous"):
            n = len(attribution[name])
            print(f"  {name:<10} {n}/{len(buckets['table'])}")

        print("\nFull row detail for every table outcome (for manual spot-check):")
        print("-" * 70)
        for name in ("correct", "incorrect", "ambiguous"):
            for entry in attribution[name]:
                print(f"\n[{name.upper()}] rep {entry['rep']} — {entry['note']}")
                print(f"  PERSON_2 row: {entry['person2_row']!r}")
                print(f"  PERSON_3 row: {entry['person3_row']!r}")

    print("\nFull raw outputs by bucket (for anything the summary doesn't answer):")
    print("-" * 70)
    for name in ("hatch", "table", "prose"):
        for idx, text in enumerate(buckets[name], 1):
            print(f"\n--- {name} #{idx} ---")
            print(text)


if __name__ == "__main__":
    run()