"""
placeholder_survival_test_v1.py

THROWAWAY DIAGNOSTIC — not a pipeline component.
Purpose: determine which placeholder format survives Gemini translation
byte-intact (or intact-core + trailing Turkish inflection), in both
EN->TR and TR->EN directions, so the /anonymize step can standardize
on the most survivable format.

Writes no files. Imports no versioned core. Reads GEMINI_API_KEY from env.
All test sentences are fully synthetic — no real PII.

Survival definition (per design decisions):
  - SURVIVED: token core present, exact OR followed immediately by a
    Turkish suffix (inflection-tolerant match).
  - FAILED: core translated, delimiters mangled, token split, or missing.

"""

import os
import re
import sys
import json
import time

# --- Gemini client -----------------------------------------------------------
# Uses the current google-genai SDK. Install into abclink if absent:
#   pip install google-genai
try:
    from google import genai
except ImportError:
    sys.exit("google-genai not installed. Run: pip install google-genai")

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    sys.exit("GEMINI_API_KEY not set. Export it in the abclink env before running.")

client = genai.Client(api_key=API_KEY)
MODEL_NAME = "gemini-2.5-flash"  # free-tier friendly; swap if your production model differs

# --- Placeholder formats under test ------------------------------------------
# Each is a function: entity_type, index -> rendered token string.
FORMATS = {
    "square":   lambda t, i: f"[{t}_{i}]",
    "math_wb":  lambda t, i: f"\u27e6{t}_{i}\u27e7",      # ⟦PERSON_1⟧
    "angle":    lambda t, i: f"<<{t}_{i}>>",
    "sentinel": lambda t, i: f"XPIIX_{t}_{i}",
}

# --- Synthetic test cases ----------------------------------------------------
# Each case: direction, template with {p1},{p2},... slots, and the (type,index)
# each slot should carry. Slots reusing the same (type,index) test dedupe survival.
# Sentences are fabricated; placeholders stand in for names/cases/dates.

CASES = [
    # EN->TR, inflection stress: token in a position Turkish will case-mark
    {
        "id": "en_tr_inflect",
        "direction": "EN->TR",
        "stress": "inflection",
        "template": "The court awarded custody to {p1} under the terms of the decree.",
        "slots": {"p1": ("PERSON", 1)},
    },
    # EN->TR, dedupe stress: same token twice
    {
        "id": "en_tr_dedupe",
        "direction": "EN->TR",
        "stress": "dedupe",
        "template": "{p1} filed the petition, and {p1} appeared before the judge in person.",
        "slots": {"p1": ("PERSON", 1)},
    },
    # EN->TR, adjacency stress: two different tokens close together
    {
        "id": "en_tr_adjacent",
        "direction": "EN->TR",
        "stress": "adjacency",
        "template": "In case {p1}, the petitioner {p2} was granted a dissolution of marriage.",
        "slots": {"p1": ("CASE_NUMBER", 1), "p2": ("PERSON", 1)},
    },
    # TR->EN, inflection stress
    {
        "id": "tr_en_inflect",
        "direction": "TR->EN",
        "stress": "inflection",
        "template": "Mahkeme, velayeti {p1} tarafina karar metnine gore verdi.",
        "slots": {"p1": ("PERSON", 1)},
    },
    # TR->EN, dedupe stress
    {
        "id": "tr_en_dedupe",
        "direction": "TR->EN",
        "stress": "dedupe",
        "template": "{p1} dilekceyi sundu ve {p1} durusmada bizzat hazir bulundu.",
        "slots": {"p1": ("PERSON", 1)},
    },
    # TR->EN, adjacency stress
    {
        "id": "tr_en_adjacent",
        "direction": "TR->EN",
        "stress": "adjacency",
        "template": "{p1} sayili dosyada, davaci {p2} icin bosanma karari verildi.",
        "slots": {"p1": ("CASE_NUMBER", 1), "p2": ("PERSON", 1)},
    },
]

# --- Production-like translation prompt --------------------------------------
# Modeled on the project's translation rules so survival reflects real conditions.
def build_prompt(direction, text):
    if direction == "EN->TR":
        src, tgt = "English", "Turkish"
    else:
        src, tgt = "Turkish", "English"
    return (
        f"You are a certified legal translator. Translate the following {src} "
        f"legal text into {tgt}.\n"
        "Rules:\n"
        "- Translate everything. Do not summarize, explain, or add commentary.\n"
        "- Do not add headers or omit information.\n"
        "- Preserve names, court names, case numbers, dates, and legal references exactly.\n"
        "- Use formal legal language appropriate to the target language.\n"
        "- Output only the translation, nothing else.\n\n"
        f"Text:\n{text}"
    )

# --- Survival matching -------------------------------------------------------
# A token core survives if it appears intact, optionally followed by letters
# (Turkish suffix) or an apostrophe+letters (e.g. [PERSON_1]'in).
# We escape the exact rendered token, then allow an optional trailing
# inflection cluster.
def survived(rendered_token, translated_text):
    core = re.escape(rendered_token)
    # allow optional Turkish inflection: apostrophe(s) and/or trailing letters
    pattern = core + r"(?:['\u2019]?[a-zA-Z\u00c7\u00e7\u011e\u011f\u0130\u0131\u00d6\u00f6\u015e\u015f\u00dc\u00fc]*)?"
    return re.search(pattern, translated_text) is not None

def count_expected(slots, fmt_fn, template):
    """Return list of rendered tokens expected to survive (one entry per
    distinct slot occurrence in the template)."""
    rendered = {name: fmt_fn(t, i) for name, (t, i) in slots.items()}
    # count occurrences of each slot in template
    expected = []
    for name, tok in rendered.items():
        n = template.count("{" + name + "}")
        expected.extend([tok] * n)
    return expected, rendered

def render_sentence(template, rendered):
    out = template
    for name, tok in rendered.items():
        out = out.replace("{" + name + "}", tok)
    return out

# --- Run ---------------------------------------------------------------------
def run():
    results = {fmt: {"survived": 0, "total": 0, "failures": []} for fmt in FORMATS}

    for case in CASES:
        for fmt_name, fmt_fn in FORMATS.items():
            expected, rendered = count_expected(case["slots"], fmt_fn, case["template"])
            sentence = render_sentence(case["template"], rendered)
            prompt = build_prompt(case["direction"], sentence)

            # Call with retry on 429 rate-limit. Free tier is ~5 req/min for
            # gemini-2.5-flash, so throttling is expected; we honor the delay
            # Google returns rather than counting a throttled call as a failure.
            translated = ""
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    resp = client.models.generate_content(
                        model=MODEL_NAME, contents=prompt
                    )
                    translated = resp.text or ""
                    break
                except Exception as e:
                    msg = str(e)
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                        # try to read the retry delay Google suggests; default 15s
                        m = re.search(r"retry in ([\d.]+)s", msg) or \
                            re.search(r"retryDelay['\"]?:\s*['\"]?(\d+)", msg)
                        wait = float(m.group(1)) + 1 if m else 15.0
                        print(f"  [rate-limited] {case['id']}/{fmt_name}, "
                              f"waiting {wait:.0f}s (attempt {attempt+1}/{max_retries})")
                        time.sleep(wait)
                        continue
                    else:
                        print(f"[API error] {case['id']} / {fmt_name}: {e}")
                        break

            # score each expected token occurrence
            for tok in expected:
                results[fmt_name]["total"] += 1
                if translated and survived(tok, translated):
                    results[fmt_name]["survived"] += 1
                else:
                    results[fmt_name]["failures"].append(
                        {"case": case["id"], "stress": case["stress"],
                         "token": tok, "got": translated[:160]}
                    )
            time.sleep(13)  # free tier is ~5 req/min; ~13s/call keeps us under it

    # --- report ---
    print("\n" + "=" * 60)
    print(f"PLACEHOLDER SURVIVAL — model: {MODEL_NAME}")
    print("=" * 60)
    print(f"{'format':<10} {'survived/total':<16} {'rate':<8}")
    print("-" * 60)
    for fmt_name, r in results.items():
        rate = (r["survived"] / r["total"] * 100) if r["total"] else 0
        print(f"{fmt_name:<10} {str(r['survived'])+'/'+str(r['total']):<16} {rate:>5.1f}%")

    print("\nFailure detail (which stress broke each format):")
    print("-" * 60)
    for fmt_name, r in results.items():
        if r["failures"]:
            print(f"\n[{fmt_name}]")
            for f in r["failures"]:
                print(f"  {f['case']} ({f['stress']}): token {f['token']} not found")
                print(f"    got: {f['got']!r}")
        else:
            print(f"\n[{fmt_name}] — all survived")

if __name__ == "__main__":
    run()