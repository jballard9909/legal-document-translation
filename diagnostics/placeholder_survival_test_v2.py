"""
placeholder_survival_test_v2.py

THROWAWAY DIAGNOSTIC — not a pipeline component. Successor to
placeholder_survival_test_v1.py, which ships UNCHANGED alongside this file.

WHAT CHANGED FROM V1, AND WHY
------------------------------
v1 was built to decide BETWEEN FOUR placeholder formats (square, math_wb,
angle, sentinel). That question is no longer open: anonymize_core_v2.py has
square_format locked in as the shipping format. Re-running all four now would
spend quota re-deciding a decision that's already made, so FORMATS is trimmed
to "square" only — 1 format instead of 4.

That budget goes toward testing something v1 never could: anonymize_core_v2.py
introduces a NEW placeholder shape that didn't exist when v1 was written. For
an entity whose span crosses a newline (Option B, locked with Jacob), v2 emits

    replacement = placeholder + ("\\n" * newline_count)

i.e. the token is followed immediately by a LINE BREAK instead of a space, e.g.
"...and [PERSON_1]\\n Q. Millbrook per the decree...". v1's CASES only ever
placed placeholders next to spaces and punctuation. Whether a token survives
translation intact when it's adjacent to whitespace it's never been tested
against (a newline instead of a space) is exactly the kind of stress v1's
design was built to catch for OTHER adjacency types (inflection, dedupe,
adjacency-to-another-token) — this file extends that same method to the one
new adjacency v2 introduces.

WHAT DIDN'T CHANGE
------------------
survived() is UNTOUCHED. It already matches the placeholder CORE plus an
optional trailing Turkish-inflection suffix, and was never sensitive to what
precedes or follows the token in the sentence. So the newline stress needs no
new matching logic — only new CASES whose TEMPLATE puts a newline where a
space would normally sit, immediately after the placeholder slot. That is the
literal shape v2 emits; embedding the newline in the template reproduces it
exactly rather than approximating it.

Everything else — the CASES structure, build_prompt (the production-like
translation prompt), the retry/backoff loop, the report format — is identical
to v1.

Run:  python placeholder_survival_test_v2.py <model-name>
     e.g. python placeholder_survival_test_v2.py gemini-2.5-flash

The model is a REQUIRED argument with no default. A result file whose model
was assumed rather than stated is not evidence — and this test exists to
produce evidence about the model that ships, which has changed once already.
Requires: GEMINI_API_KEY in env, google-genai installed in the abclink env.
100% synthetic. No real PII.
"""

import os
import re
import sys
import json
import time

try:
     from google import genai
except ImportError:
     sys.exit("google-genai not installed. Run: pip install google-genai")
 
if len(sys.argv) != 2:
    sys.exit(
        "Usage: python placeholder_survival_test_v2.py <model-name>\n"
        "  e.g. python placeholder_survival_test_v2.py gemini-2.5-flash\n"
        "Model is required, not defaulted — see module docstring."
    )
MODEL_NAME = sys.argv[1]

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    sys.exit("GEMINI_API_KEY not set. Export it in the abclink env before running.")

client = genai.Client(api_key=API_KEY)
print(f"Running placeholder survival against model: {MODEL_NAME}\n")

# --- Placeholder formats under test ------------------------------------------
# v1 tested four candidate formats to CHOOSE one. That choice is made --
# anonymize_core_v2.py ships square_format. Testing only what ships.
FORMATS = {
    "square": lambda t, i: f"[{t}_{i}]",
}

# --- Synthetic test cases -----------------------------------------------------
# First six cases are UNCHANGED from v1 (inflection, dedupe, adjacency stress,
# both directions). The two NEWLINE cases are new: the template embeds a literal
# "\n" immediately after the placeholder slot, in place of the space v1 always
# used there -- reproducing v2's replacement = placeholder + "\n" shape exactly.

CASES = [
    # --- carried from v1, unchanged ---
    {
        "id": "en_tr_inflect",
        "direction": "EN->TR",
        "stress": "inflection",
        "template": "The court awarded custody to {p1} under the terms of the decree.",
        "slots": {"p1": ("PERSON", 1)},
    },
    {
        "id": "en_tr_dedupe",
        "direction": "EN->TR",
        "stress": "dedupe",
        "template": "{p1} filed the petition, and {p1} appeared before the judge in person.",
        "slots": {"p1": ("PERSON", 1)},
    },
    {
        "id": "en_tr_adjacent",
        "direction": "EN->TR",
        "stress": "adjacency",
        "template": "In case {p1}, the petitioner {p2} was granted a dissolution of marriage.",
        "slots": {"p1": ("CASE_NUMBER", 1), "p2": ("PERSON", 1)},
    },
    {
        "id": "tr_en_inflect",
        "direction": "TR->EN",
        "stress": "inflection",
        "template": "Mahkeme, velayeti {p1} tarafina karar metnine gore verdi.",
        "slots": {"p1": ("PERSON", 1)},
    },
    {
        "id": "tr_en_dedupe",
        "direction": "TR->EN",
        "stress": "dedupe",
        "template": "{p1} dilekceyi sundu ve {p1} durusmada bizzat hazir bulundu.",
        "slots": {"p1": ("PERSON", 1)},
    },
    {
        "id": "tr_en_adjacent",
        "direction": "TR->EN",
        "stress": "adjacency",
        "template": "{p1} sayili dosyada, davaci {p2} icin bosanma karari verildi.",
        "slots": {"p1": ("CASE_NUMBER", 1), "p2": ("PERSON", 1)},
    },
    # --- NEW: newline-adjacency stress, reproducing v2's Option B shape ---
    # Note the "\n" sits directly in the template, right after {p1} and before
    # the next word -- exactly where v2's apply_anonymization would have
    # inserted it, in place of the space v1's templates always used there.
    {
        "id": "en_tr_newline",
        "direction": "EN->TR",
        "stress": "newline_adjacency",
        "template": "The court hereby recognizes {p1}\nas legal guardian of the minor children.",
        "slots": {"p1": ("PERSON", 1)},
    },
    {
        "id": "tr_en_newline",
        "direction": "TR->EN",
        "stress": "newline_adjacency",
        "template": "Mahkeme, {p1}\nkisisinin veli olarak atanmasina karar verdi.",
        "slots": {"p1": ("PERSON", 1)},
    },
]

# --- Production-like translation prompt (unchanged from v1) ------------------
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

# --- Survival matching (unchanged from v1) ------------------------------------
def survived(rendered_token, translated_text):
    core = re.escape(rendered_token)
    pattern = core + r"(?:['\u2019]?[a-zA-Z\u00c7\u00e7\u011e\u011f\u0130\u0131\u00d6\u00f6\u015e\u015f\u00dc\u00fc]*)?"
    return re.search(pattern, translated_text) is not None

def count_expected(slots, fmt_fn, template):
    rendered = {name: fmt_fn(t, i) for name, (t, i) in slots.items()}
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

            for tok in expected:
                results[fmt_name]["total"] += 1
                if translated and survived(tok, translated):
                    results[fmt_name]["survived"] += 1
                else:
                    results[fmt_name]["failures"].append(
                        {"case": case["id"], "stress": case["stress"],
                         "token": tok, "got": translated[:160]}
                    )

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

    # v2 addition: call out the newline cases specifically, since that's the
    # whole reason this run exists -- easy to miss in the aggregate rate above.
    print("\n" + "=" * 60)
    print("NEWLINE-ADJACENCY CASES (new in v2) — pass/fail at a glance")
    print("=" * 60)
    newline_case_ids = {c["id"] for c in CASES if c["stress"] == "newline_adjacency"}
    for fmt_name, r in results.items():
        failed_ids = {f["case"] for f in r["failures"]} & newline_case_ids
        for cid in newline_case_ids:
            status = "FAIL" if cid in failed_ids else "PASS"
            print(f"  [{status}] {fmt_name} / {cid}")


if __name__ == "__main__":
    run()