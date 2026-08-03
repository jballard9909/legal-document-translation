"""
anonymize_core_v1.py

Importable core for the /anonymize step: the TEXT-side complement to /redact.
/detect-pii returns PII spans; /redact used them to black out image pixels; this
uses the SAME spans to replace PII in the TEXT with typed, numbered placeholders
so the text can safely cross to the cloud translator. Restoration puts the real
values back locally after translation returns.

Bytes/strings in, strings + maps out. No FastAPI, no disk, no network, no
globals. anonymize_service.py (port 8004) will wrap this; both import the SAME
functions (single source of truth), same pattern as redact_core.py.

THE PROPOSE / APPLY SEAM (design decision, locked with Jacob)
------------------------------------------------------------
The core is split into two halves with a MAPPING as the handoff:

  PROPOSE  build_proposal(text, entities)  -> proposal
      score-floor -> resolve overlaps -> dedupe+number -> proposal object.
      Nothing substituted yet; this is pure inspection output.

  APPLY    apply_anonymization(text, substitutions) -> anonymized_text
      position-based right-to-left substitution driven by the proposal's
      substitution list.

Why split: option (b), a local human-in-the-loop review gate BEFORE the cloud
call, slots in between propose and apply without touching either. The human
edits the proposal (fix a missed entity, drop a false positive, correct an
overlap winner); apply consumes whatever mapping it's handed. For the option (a)
demo, anonymize() just calls propose then apply back-to-back. Apply works from
the mapping's SPANS alone -- never re-derived from entities -- so a
human-corrected mapping that no propose step would produce still applies cleanly.

TWO VIEWS OF THE MAPPING (both emitted by propose)
--------------------------------------------------
  substitutions : list of {span:[s,e], placeholder, entity_type, value}
                  -- for taking PII OUT. Position-based, so substitution stays
                  the clean right-to-left offset operation resolve_overlaps set
                  up. Same value appears once per occurrence (distinct spans),
                  all sharing one placeholder.
  restore_map   : { placeholder -> real value } -- for putting values BACK after
                  translation. One entry per UNIQUE value (dedupe), regardless
                  of occurrence count.

DEDUPE (locked with Jacob): exact match after .strip(). Same value -> same
placeholder -> same number. "Jordan A. Millbrook" seen 3x = [PERSON_1] 3x, one
restore entry. Case-folding / fuzzy matching deferred (documented next step).

PLACEHOLDER FORMAT (locked with Jacob): square, e.g. [PERSON_1]. Chosen for
reviewer readability + matches the project spec's own examples. Survival vs.
the cloud translator to be confirmed by placeholder_survival_test; format is a
swappable format_fn so a later switch is one line. NOTE: square brackets are
regex metacharacters -- the RESTORE step (built later) must re.escape() the
placeholder before searching translated text. Substitution here is plain string
work and is unaffected.

SCORE FLOOR (locked with Jacob): 0.30, reused from /redact for consistency.

100% synthetic in the self-test. No real PII.
"""

from typing import List, Dict, Tuple, Callable


# --- tunables (locked with Jacob) ---
SCORE_FLOOR = 0.30   # drop entities below this before anything else


def square_format(entity_type: str, index: int) -> str:
    """Default placeholder format: [PERSON_1]. Swap this one function to change
    the token style everywhere (e.g. after the survival test picks a winner)."""
    return f"[{entity_type}_{index}]"


# ===========================================================================
# STEP 1 -- SCORE FLOOR
# ===========================================================================
def apply_score_floor(entities: List[Dict],
                      floor: float = SCORE_FLOOR) -> List[Dict]:
    """Drop entities whose score is strictly below the floor. Mirrors /redact so
    the two consumers of /detect-pii spans agree on what counts as real."""
    return [e for e in entities if e.get("score", 0) >= floor]


# ===========================================================================
# STEP 2 -- RESOLVE OVERLAPS  (approved as resolve_overlaps_v1.py; unchanged)
# ===========================================================================
def _spans_overlap(a_start: int, a_end: int,
                   b_start: int, b_end: int) -> bool:
    """Half-open [start, end) overlap -- identical to redact_core semantics.
    Touching (a_end == b_start) is NOT overlap."""
    return not (a_end <= b_start or a_start >= b_end)


def _priority_key(e: Dict):
    """Highest-priority entity sorts FIRST (smallest tuple). Ascending sort, so
    negate 'bigger is better' fields:
      higher score -> longer span -> lower start -> type alphabetical."""
    return (-e["score"], -(e["end"] - e["start"]), e["start"], e["entity_type"])


def _wins(a: Dict, b: Dict) -> Dict:
    """Winner of a head-to-head overlap by the locked priority."""
    return a if _priority_key(a) <= _priority_key(b) else b


def _uncovered_fragments(inner_s: int, inner_e: int,
                         cover_s: int, cover_e: int) -> List[List[int]]:
    """Parts of [inner_s, inner_e) NOT covered by [cover_s, cover_e). Up to two
    fragments (left/right). Empty if cover fully contains inner. This detects a
    dropped loser's exposed tail."""
    frags = []
    if inner_s < cover_s:
        frags.append([inner_s, min(inner_e, cover_s)])
    if inner_e > cover_e:
        frags.append([max(inner_s, cover_e), inner_e])
    return frags


def resolve_overlaps(entities: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    Reduce entities to a pairwise non-overlapping survivor set (winner-takes-all)
    and flag any exposure a drop creates.

    Returns (resolved, warnings):
      resolved : survivors, pairwise non-overlapping, sorted by start.
      warnings : one record per drop that left uncovered characters, each with
                 dropped/winner type+span+score and the exposed fragments, so a
                 reviewer can triage by confidence.
    """
    if not entities:
        return [], []

    ordered = sorted(entities, key=lambda e: (e["start"], _priority_key(e)))

    resolved: List[Dict] = []
    warnings: List[Dict] = []

    current = ordered[0]
    for nxt in ordered[1:]:
        if _spans_overlap(current["start"], current["end"],
                          nxt["start"], nxt["end"]):
            winner = _wins(current, nxt)
            loser = nxt if winner is current else current
            exposed = _uncovered_fragments(
                loser["start"], loser["end"],
                winner["start"], winner["end"],
            )
            if exposed:
                warnings.append({
                    "dropped_type": loser["entity_type"],
                    "dropped_span": [loser["start"], loser["end"]],
                    "dropped_score": loser["score"],
                    "winner_type": winner["entity_type"],
                    "winner_span": [winner["start"], winner["end"]],
                    "winner_score": winner["score"],
                    "exposed_spans": exposed,
                })
            current = winner
        else:
            resolved.append(current)
            current = nxt

    resolved.append(current)
    resolved.sort(key=lambda e: e["start"])
    return resolved, warnings


# ===========================================================================
# STEP 3 -- BUILD PROPOSAL  (the PROPOSE half)
# ===========================================================================
def build_proposal(text: str,
                   entities: List[Dict],
                   format_fn: Callable[[str, int], str] = square_format,
                   floor: float = SCORE_FLOOR) -> Dict:
    """
    Produce a review-ready anonymization proposal WITHOUT modifying text.

    Chains: score-floor -> resolve overlaps -> dedupe + number -> assemble.

    Dedupe: exact match after .strip(), scoped PER entity_type. Same value seen
    again gets the SAME placeholder + number. Numbering is assigned in reading
    order (first appearance by start offset) so [PERSON_1] is the first person
    the reader meets -- stable, intuitive, and deterministic.

    Returns a proposal dict:
      {
        "substitutions": [   # one per OCCURRENCE (distinct span), for APPLY
           {"span":[s,e], "placeholder":"[PERSON_1]",
            "entity_type":"PERSON", "value":"Jordan A. Millbrook"}, ...
        ],
        "restore_map": {     # one per UNIQUE value, for post-translation restore
           "[PERSON_1]": "Jordan A. Millbrook", ...
        },
        "warnings": [ ...overlap exposures... ],
      }

    The substitutions list is what apply_anonymization consumes. restore_map is
    carried alongside for the (much later) restore step. Both come from the same
    resolved survivors, so they cannot disagree.
    """
    floored = apply_score_floor(entities, floor=floor)
    resolved, warnings = resolve_overlaps(floored)

    # Assign placeholders. Dedupe by (entity_type, stripped value). Numbering
    # per type, in reading order (resolved is already sorted by start).
    # value_to_placeholder: (type, stripped_value) -> placeholder
    value_to_placeholder: Dict[Tuple[str, str], str] = {}
    type_counters: Dict[str, int] = {}
    restore_map: Dict[str, str] = {}
    substitutions: List[Dict] = []

    for e in resolved:
        etype = e["entity_type"]
        raw_value = text[e["start"]:e["end"]]
        key = (etype, raw_value.strip())

        placeholder = value_to_placeholder.get(key)
        if placeholder is None:
            # first time we've seen this value for this type -> new number
            type_counters[etype] = type_counters.get(etype, 0) + 1
            placeholder = format_fn(etype, type_counters[etype])
            value_to_placeholder[key] = placeholder
            # restore_map keyed by placeholder; store the raw (unstripped) value
            # so restoration reproduces exactly what was in the source text.
            restore_map[placeholder] = raw_value

        substitutions.append({
            "span": [e["start"], e["end"]],
            "placeholder": placeholder,
            "entity_type": etype,
            "value": raw_value,
        })

    return {
        "substitutions": substitutions,
        "restore_map": restore_map,
        "warnings": warnings,
    }


# ===========================================================================
# STEP 4 -- APPLY ANONYMIZATION  (the APPLY half)
# ===========================================================================
def apply_anonymization(text: str, substitutions: List[Dict]) -> str:
    """
    Replace each substitution's span with its placeholder, RIGHT-TO-LEFT so
    earlier offsets stay valid as the string length changes.

    Driven ONLY by the substitutions list (span + placeholder). Does NOT read
    the original entities -- so a human-edited substitution list (option b)
    applies exactly the same way. This is the seam that makes review possible.

    Assumes spans are non-overlapping (guaranteed by resolve_overlaps, or by the
    reviewer's own editing). Overlapping spans would corrupt the output; we
    assert non-overlap defensively rather than silently produce garbage.
    """
    # Sort by start DESCENDING: apply from the end of the string backward.
    ordered = sorted(substitutions, key=lambda s: s["span"][0], reverse=True)

    # Defensive non-overlap check (cheap insurance against a bad edited mapping).
    prev_start = None
    for s in ordered:
        st, en = s["span"]
        if prev_start is not None and en > prev_start:
            raise ValueError(
                f"overlapping substitution spans: span ending {en} overlaps a "
                f"later span starting {prev_start}; apply requires "
                f"non-overlapping spans."
            )
        prev_start = st

    out = text
    for s in ordered:
        st, en = s["span"]
        out = out[:st] + s["placeholder"] + out[en:]
    return out


# ===========================================================================
# STEP 5 -- CONVENIENCE WRAPPER  (option (a) demo path: propose then apply)
# ===========================================================================
def anonymize(text: str,
              entities: List[Dict],
              format_fn: Callable[[str, int], str] = square_format,
              floor: float = SCORE_FLOOR) -> Dict:
    """
    Full option-(a) path: propose then immediately apply. For option (b), call
    build_proposal, let a human edit the proposal, then call apply_anonymization
    on the edited substitutions instead of using this wrapper.

    Returns:
      {
        "anonymized_text": str,
        "restore_map": {placeholder -> value},
        "warnings": [...],
        "substitutions": [...],   # included for logging/audit + as the object
                                  # a reviewer would edit in option (b)
      }
    """
    proposal = build_proposal(text, entities, format_fn=format_fn, floor=floor)
    anonymized_text = apply_anonymization(text, proposal["substitutions"])
    return {
        "anonymized_text": anonymized_text,
        "restore_map": proposal["restore_map"],
        "warnings": proposal["warnings"],
        "substitutions": proposal["substitutions"],
    }


# ===========================================================================
# SELF-TEST (behind __main__): synthetic only, no real PII, no network.
# Covers: round-trip propose->apply, dedupe (same value -> same placeholder),
# reading-order numbering, overlap resolution feeding through, and the
# restore_map shape. Also asserts apply's non-overlap guard fires.
# ===========================================================================
def _check(label: str, got, expected) -> bool:
    ok = got == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"        got     : {got!r}")
        print(f"        expected: {expected!r}")
    return ok


def _main():
    all_ok = True
    print("=== round-trip: dedupe + numbering + apply ===")

    # Synthetic sentence. Offsets chosen so entities land on the right words.
    # "Jordan filed; later Jordan appeared. Case 12-34-5678 before Reese."
    text = "Jordan filed; later Jordan appeared. Case 12-34-5678 before Reese."
    #        0        1                                                       6
    # index the two "Jordan" occurrences, the case number, and "Reese".
    j1 = text.index("Jordan")                       # 0
    j2 = text.index("Jordan", j1 + 1)               # second occurrence
    case_s = text.index("12-34-5678")
    case_e = case_s + len("12-34-5678")
    reese_s = text.index("Reese")
    reese_e = reese_s + len("Reese")

    entities = [
        {"entity_type": "PERSON", "start": j1, "end": j1 + 6, "score": 0.85},
        {"entity_type": "PERSON", "start": j2, "end": j2 + 6, "score": 0.85},
        {"entity_type": "CASE_NUMBER", "start": case_s, "end": case_e, "score": 0.90},
        {"entity_type": "PERSON", "start": reese_s, "end": reese_e, "score": 0.80},
        # a below-floor false positive that must be dropped:
        {"entity_type": "US_DRIVER_LICENSE", "start": case_s, "end": case_e, "score": 0.01},
    ]

    result = anonymize(text, entities)

    # Both "Jordan"s share [PERSON_1]; Reese is [PERSON_2]; case is [CASE_NUMBER_1].
    all_ok &= _check(
        "anonymized text",
        result["anonymized_text"],
        "[PERSON_1] filed; later [PERSON_1] appeared. "
        "Case [CASE_NUMBER_1] before [PERSON_2].",
    )
    all_ok &= _check(
        "restore_map",
        result["restore_map"],
        {"[PERSON_1]": "Jordan", "[CASE_NUMBER_1]": "12-34-5678",
         "[PERSON_2]": "Reese"},
    )
    all_ok &= _check("no warnings (clean, non-overlapping real entities)",
                     result["warnings"], [])
    all_ok &= _check(
        "substitution count = 4 occurrences (Jordan x2 + case + Reese); "
        "dedupe collapses VALUES not OCCURRENCES, so both Jordan spans remain",
        len(result["substitutions"]), 4)

    # --- dedupe restore is one-per-value even with two occurrences ---
    all_ok &= _check("restore_map has 3 unique placeholders",
                     len(result["restore_map"]), 3)

    # --- apply's non-overlap guard should fire on a bad edited mapping ---
    print("\n=== apply non-overlap guard ===")
    bad = [
        {"span": [0, 10], "placeholder": "[A_1]"},
        {"span": [5, 15], "placeholder": "[B_1]"},
    ]
    try:
        apply_anonymization("x" * 20, bad)
        all_ok &= _check("guard raised on overlap", False, True)
    except ValueError:
        all_ok &= _check("guard raised on overlap", True, True)

    # --- overlap resolution flows through build_proposal + warnings surface ---
    print("\n=== overlap flows through to proposal ===")
    text2 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # 30 chars, values irrelevant here
    ents2 = [
        {"entity_type": "PERSON", "start": 10, "end": 18, "score": 0.85},
        {"entity_type": "LOCATION", "start": 15, "end": 25, "score": 0.85},
    ]
    prop2 = build_proposal(text2, ents2)
    all_ok &= _check("overlap: one survivor (LOCATION, longer)",
                     [s["entity_type"] for s in prop2["substitutions"]],
                     ["LOCATION"])
    all_ok &= _check("overlap: exposure warning surfaced with scores",
                     (len(prop2["warnings"]) == 1
                      and prop2["warnings"][0]["dropped_type"] == "PERSON"
                      and prop2["warnings"][0]["dropped_score"] == 0.85
                      and prop2["warnings"][0]["exposed_spans"] == [[10, 15]]),
                     True)

    print("\n" + "=" * 60)
    print(f"OVERALL: {'ALL PASS' if all_ok else 'SOME FAILED'}")
    print("=" * 60)


if __name__ == "__main__":
    _main()