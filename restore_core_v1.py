"""
restore_core_v1.py

Importable core for the /restore step: the inverse of /anonymize. After the
anonymized text crosses to the cloud translator and comes back, this puts the
real PII values back -- LOCALLY, post-cloud -- driven entirely by the per-page
restore_map the anonymizer produced.

WHERE THIS SITS
---------------
Assembly branch only: Wrap in JSON -> /restore -> Aggregate -> /assemble. It
lives on the assembly side so restored PII is structurally incapable of reaching
the glossary branch, which keeps consuming anonymized text. Same trust-boundary
posture as the rest of the local chain: this runs local, never crosses to cloud.

WHY A SEPARATE CORE (not an extension of anonymize_core)
--------------------------------------------------------
Restore needs ZERO knowledge of the placeholder format. Substitution is driven
entirely by the map's keys: iterate restore_map, replace each key string with
its value. The map is the single source of truth and the anonymizer already
produced it -- there is nothing to import and no format to share. So restore is
its own versioned core with its own self-test, the clean complement to the
anonymize core: one takes PII out, one puts it back, both keyed on the same map.
(May fold into anonymize_core later; kept separate now for isolated testing.)

WHY PLAIN str.replace IS PROVABLY SAFE HERE
-------------------------------------------
Substitution is literal string replacement, NOT regex -- so square brackets are
just characters and there is no metacharacter/escaping concern. Two properties
make plain replace correct:

  1. NO PREFIX COLLISION. "[PERSON_1]" is not a substring of "[PERSON_10]": the
     closing "]" terminates the token, so after "[PERSON_1" comes "0", not "]".
     The bracket protects against partial matches.
  2. NO CASCADE. Restored values are real names/dates/places; they never contain
     "[TYPE_N]"-shaped strings, so replacing one key cannot create or corrupt
     another. Replacement is therefore ORDER-INDEPENDENT.

VERBATIM RULE (locked with Jacob)
---------------------------------
Values are substituted back exactly as stored -- no date reformatting, no name
normalization -- because a certified translation requires names, numbers, and
dates to match the original exactly. Restore is also STRUCTURE-BLIND: it treats
the (Markdown) text as a flat string and never parses it. Placeholders are
literal tokens sitting in the text; swapping them is Markdown-agnostic.

INTEGRITY DETECTION (both detectors, locked with Jacob)
-------------------------------------------------------
Detector 1 -- MISSING placeholders (primary). Before substituting, count each
  map key's occurrences in the translated text. A key with ZERO occurrences was
  dropped or corrupted in translation -- it never came back. This is the real
  integrity guarantee: did everything anonymized survive the round trip? Robust
  to any corruption, because it checks the authoritative list (map keys) against
  presence -- even a mangled "[PERSON3]" is caught, since the key "[PERSON_3]"
  won't be found. Maps are per-page, so every key should appear on its page:
  no false positives.

Detector 2 -- STRAY survivors (secondary). After substituting, scan for anything
  still matching the placeholder grammar that was NOT a map key -- a hallucinated
  or duplicated token. Catches what Detector 1 cannot (a token that should not be
  there at all). The review markers [ILLEGIBLE] and
  [TABLE - STRUCTURE UNCERTAIN, REVIEW] do NOT match the grammar (no _<digits>
  suffix), so they are excluded by construction -- asserted in the self-test.

Neither detector catches a spurious DUPLICATION of a valid token in the wrong
place -- that is a pure human-review catch. On any flag we FLAG AND SUCCEED,
never error: the pipeline continues, flags ride downstream, and the mandatory
human-review gate acts on them. Same philosophy as Stage 1's soft-fail.

100% synthetic in the self-test. No real PII.
"""

import re
from typing import Dict, List

# Placeholder grammar for Detector 2. Matches the anonymizer's [TYPE_N] format:
# an uppercase/underscore type, then "_" and one or more digits, in brackets.
# Review markers have no _<digits> suffix and so are excluded by construction.
_PLACEHOLDER_RE = re.compile(r"\[[A-Z_]+_\d+\]")


def restore(translated_text: str, restore_map: Dict[str, str]) -> Dict:
    """
    Substitute real PII values back into translated text, with integrity checks.

    Args:
        translated_text: the (Markdown) translation containing [TYPE_N] tokens.
        restore_map:     { "[PERSON_1]": "Jordan A. Millbrook", ... } for THIS
                         page. Keys are placeholders, values are raw originals.

    Returns:
        {
          "restored_text":        str,        # values substituted back
          "restored_count":       int,        # total token OCCURRENCES replaced
          "missing_placeholders": [str, ...],  # Detector 1: keys absent from text
          "unresolved_tokens":    [str, ...],  # Detector 2: stray [TYPE_N] left
        }

    Never raises on a missing/stray token: flags and succeeds.
    """
    text = translated_text or ""

    # --- Detector 1: missing placeholders + occurrence count (pre-substitution)
    missing: List[str] = []
    restored_count = 0
    for placeholder in restore_map:
        n = text.count(placeholder)
        if n == 0:
            missing.append(placeholder)
        else:
            restored_count += n

    # --- Substitute (literal, order-independent) ---------------------------
    restored = text
    for placeholder, value in restore_map.items():
        restored = restored.replace(placeholder, value)

    # --- Detector 2: stray survivors (post-substitution) -------------------
    # All real keys are now gone, so any grammar match remaining is a token that
    # was never a map key -- hallucinated or duplicated. Values are real PII and
    # never contain bracketed [TYPE_N] strings, so they add no false matches.
    unresolved = sorted(set(_PLACEHOLDER_RE.findall(restored)))

    return {
        "restored_text": restored,
        "restored_count": restored_count,
        "missing_placeholders": sorted(missing),
        "unresolved_tokens": unresolved,
    }


# ---------------------------------------------------------------------------
# SELF-TEST -- runs ONLY when executed directly, never on import.
# 100% synthetic. No network, no disk.
# ---------------------------------------------------------------------------
def _check(label: str, got, expected) -> bool:
    ok = got == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"        got     : {got!r}")
        print(f"        expected: {expected!r}")
    return ok


def _main() -> None:
    all_ok = True

    # --- 1. clean round-trip ---------------------------------------------
    print("=== clean round-trip ===")
    text = "Davac\u0131 [PERSON_1], Dava No.: FD-[CASE_NUMBER_1]."
    rmap = {"[PERSON_1]": "Jordan A. Millbrook",
            "[CASE_NUMBER_1]": "2025-TEST-004471"}
    r = restore(text, rmap)
    all_ok &= _check("restored_text",
                     r["restored_text"],
                     "Davac\u0131 Jordan A. Millbrook, Dava No.: FD-2025-TEST-004471.")
    all_ok &= _check("restored_count = 2", r["restored_count"], 2)
    all_ok &= _check("no missing", r["missing_placeholders"], [])
    all_ok &= _check("no unresolved", r["unresolved_tokens"], [])

    # --- 2. repeated placeholder (one key, multiple occurrences) ---------
    print("\n=== repeated placeholder ===")
    text = "[PERSON_1] filed; later [PERSON_1] appeared."
    rmap = {"[PERSON_1]": "Jordan"}
    r = restore(text, rmap)
    all_ok &= _check("both occurrences restored",
                     r["restored_text"],
                     "Jordan filed; later Jordan appeared.")
    all_ok &= _check("restored_count = 2 (occurrences, not keys)",
                     r["restored_count"], 2)

    # --- 3. Detector 1: dropped key --------------------------------------
    print("\n=== Detector 1: dropped placeholder ===")
    text = "Only [PERSON_1] survived translation."   # [PERSON_2] absent
    rmap = {"[PERSON_1]": "Jordan", "[PERSON_2]": "Casey"}
    r = restore(text, rmap)
    all_ok &= _check("missing lists the dropped key",
                     r["missing_placeholders"], ["[PERSON_2]"])
    all_ok &= _check("present key still restored",
                     r["restored_text"], "Only Jordan survived translation.")
    all_ok &= _check("restored_count counts only what was present",
                     r["restored_count"], 1)

    # --- 4. order-independence / no prefix collision ---------------------
    print("\n=== order-independence: [PERSON_1] vs [PERSON_10] ===")
    text = "[PERSON_1] and [PERSON_10] are distinct."
    rmap = {"[PERSON_1]": "Alice", "[PERSON_10]": "Bob"}
    r = restore(text, rmap)
    all_ok &= _check("[PERSON_1] did not corrupt [PERSON_10]",
                     r["restored_text"], "Alice and Bob are distinct.")
    all_ok &= _check("no stray left behind", r["unresolved_tokens"], [])
    # prove it the other way too: reversed insertion order, same result
    rmap_rev = {"[PERSON_10]": "Bob", "[PERSON_1]": "Alice"}
    r2 = restore(text, rmap_rev)
    all_ok &= _check("result identical regardless of map order",
                     r2["restored_text"], r["restored_text"])

    # --- 5. Detector 2: stray survivor -----------------------------------
    print("\n=== Detector 2: stray token not in map ===")
    text = "[PERSON_1] met [PERSON_7] who was never anonymized."
    rmap = {"[PERSON_1]": "Jordan"}
    r = restore(text, rmap)
    all_ok &= _check("stray flagged", r["unresolved_tokens"], ["[PERSON_7]"])
    all_ok &= _check("mapped key still restored",
                     r["restored_text"],
                     "Jordan met [PERSON_7] who was never anonymized.")

    # --- 6. review markers must NOT be flagged as unresolved -------------
    print("\n=== review markers excluded from Detector 2 ===")
    text = ("Signature line [ILLEGIBLE] and a block "
            "[TABLE \u2014 STRUCTURE UNCERTAIN, REVIEW] with [PERSON_1].")
    rmap = {"[PERSON_1]": "Jordan"}
    r = restore(text, rmap)
    all_ok &= _check("[ILLEGIBLE] and table marker NOT in unresolved",
                     r["unresolved_tokens"], [])
    all_ok &= _check("markers preserved verbatim in output",
                     "[ILLEGIBLE]" in r["restored_text"]
                     and "[TABLE \u2014 STRUCTURE UNCERTAIN, REVIEW]" in r["restored_text"],
                     True)

    # --- 7. empty map / empty text ---------------------------------------
    print("\n=== edge: empty map, empty text ===")
    r = restore("some text no tokens", {})
    all_ok &= _check("empty map: text unchanged",
                     r["restored_text"], "some text no tokens")
    all_ok &= _check("empty map: nothing restored", r["restored_count"], 0)
    r = restore("", {"[PERSON_1]": "Jordan"})
    all_ok &= _check("empty text: key reported missing",
                     r["missing_placeholders"], ["[PERSON_1]"])

    print("\n" + "=" * 60)
    print(f"OVERALL: {'ALL PASS' if all_ok else 'SOME FAILED'}")
    print("=" * 60)


if __name__ == "__main__":
    _main()