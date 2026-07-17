"""
anonymize_core_v2.py

Importable core for the /anonymize step. Successor to anonymize_core_v1.py,
which ships UNCHANGED alongside this file (versioning convention: cores are
versioned and never overwritten; the service wrapper switches its import, so
rollback is a one-line change).

WHAT V2 CHANGES, AND WHY
------------------------
V1 replaced each Presidio span VERBATIM. But Presidio emits spans whose
boundaries don't align with the entity, so junk got swallowed into the
placeholder AND into the restore map. Confirmed empirically on a full 5-page
run of the synthetic divorce decree:

    "[PERSON_6]"    : "Riley\\nQ. Millbrook"     internal newline (line wrap)
    "[DATE_TIME_2]" : "6:00 p.m. through"        trailing preposition
    "[DATE_TIME_1]" : "date July 1"              leading noun
    "[DATE_TIME_5]" : "about January 9, 2024"    leading preposition

These are not cosmetic. Restore substitutes values back VERBATIM (locked design
rule: names/numbers/dates must match the source exactly). So anything swallowed
into a span is PERMANENTLY EXCLUDED FROM TRANSLATION and reappears, in the
source language, in the target-language deliverable:

    v1:  "...from Friday at [DATE_TIME_2] Sunday at..."
         -> Gemini translates -> restore -> the ENGLISH word "through" lands in
            the Turkish decree, and (because the translate prompt permits token
            repositioning for target grammar) it travels wherever Turkish
            grammar puts the placeholder, silently breaking the clause.

    v2:  "...from Friday at [DATE_TIME_2] through Sunday at..."
         -> "through" is now free-standing text OUTSIDE the span, so Gemini
            translates it -> restore puts back only "6:00 p.m.". Correct.

So trimming is not tidying the restore map. Trimming RETURNS WORDS TO THE
TRANSLATOR. That is the point.

Second, independent consequence (the one that surfaced the bug): the internal
newline desynchronized line counts. Build Structured Text zips
anonymized_text.split("\\n") against /ocr's lines[] by index; page 0 came back
ocr_line_count 33 vs anon_line_count 32 and correctly soft-failed. v2 preserves
newline count BY CONSTRUCTION, so Stage 1 should pass with its code untouched.

FOUR CHANGES, ENUMERATED
------------------------
1. NEW normalize_boundaries() pass, between resolve_overlaps and dedupe.
2. Substitution records gain a "replacement" field (see NEWLINE POLICY).
3. apply_anonymization writes s["replacement"], falling back to
   s["placeholder"] when absent -- so a human-edited mapping from the
   propose/apply seam still applies unchanged.
4. warnings records gain a "kind" discriminator. resolve_overlaps' existing
   records are retrofitted to kind="overlap_exposure"; the new boundary records
   are kind="boundary_normalization". Nothing consumed warnings yet (it was []
   on all pages), so this shape change is free now and expensive later.

WHERE THE PASS SITS (locked with Jacob)
---------------------------------------
    apply_score_floor -> resolve_overlaps -> [normalize_boundaries] -> dedupe+number

  - AFTER resolve_overlaps: _priority_key ranks by span length, so trimming
    first would change overlap winners -- a behavior change outside this
    thread's scope.
  - Trimming only SHRINKS spans, and shrinking can only remove overlaps, never
    create them. So `resolved` stays pairwise non-overlapping and
    apply_anonymization's guard remains satisfied. That is an invariant, not a
    hope.
  - BEFORE dedupe: dedupe keys off text[start:end], so it must see the
    normalized value. Two bonuses fall out of this ordering -- see DEDUPE below.

NEWLINE POLICY -- Option B, re-emit AFTER (locked with Jacob)
-------------------------------------------------------------
When a span crosses a newline, we do NOT split it into two placeholders. We keep
ONE placeholder and re-emit the swallowed newline(s) immediately after it:

    text        : "...and Riley\\nQ. Millbrook (date of birth..."
    span        : covers "Riley\\nQ. Millbrook"        <- UNCHANGED
    replacement : "[PERSON_6]\\n"
    value       : "Riley Q. Millbrook"                 <- wrap collapsed to space
    result      : "...and [PERSON_6]\\n (date of birth..."

Why not split (Option A): elsewhere in the document "Riley Q. Millbrook" appears
unbroken and earns its own placeholder. Splitting would give one human three
placeholder identities and silently disable per-entity-type dedupe for exactly
the entities that happen to land on a wrap. Worse, the translate prompt permits
repositioning, so the model could reorder the two halves independently and
restore would yield "Q. Millbrook Riley".

Why the value collapse is not mutation: that newline is an OCR line wrap, never
part of the name. Collapsing it RECOVERS the value rather than altering it.
Downstream, Build Structured Text collapses wraps into spaces anyway.

Why span needs no adjustment here: [s, e) still exactly covers the original
span, which keeps substitutions[].span -- the load-bearing record -- trivially
true. Only the TRIMS adjust offsets.

Why "after" rather than "before": the entity stays associated with the OCR line
it began on, and it is order-preserving if a span ever swallows two newlines.
Cost: the following line begins with the space that trailed the span, inside a
wrapped paragraph that gets collapsed anyway.

NOT HANDLED (deliberate): a span crossing a PARAGRAPH boundary. Count is still
preserved, but the entity lands in the wrong paragraph. Scoped out; warn-only
falls out of the newline_reemit action being visible in warnings.

BLACKLIST POLICY -- per entity type, evidence-driven (locked with Jacob)
------------------------------------------------------------------------
The handoff said "stopword" swallow, but "date July 1" swallowed *date* -- a
noun. spaCy's is_stop would miss it. So: a curated junk-word blacklist, keyed
BY ENTITY TYPE, trimmed inward from both ends, looping (so "on or about" peels
in sequence).

  UNDER-TRIM, NEVER OVER-TRIM. This is the text-path mirror of /redact's
  over-redact rule, and for the same reason: the recoverable failure is the safe
  one. A blacklist under-trims -- an unlisted junk word stays swallowed, which
  is exactly today's behavior, plus a warning, plus the human review gate. The
  rejected alternative, a shape whitelist ("a DATE_TIME must start with a month
  or a digit"), OVER-trims: "van Beethoven" loses "van" and restore writes a
  wrong name into a legal document. Silent corruption. Wrong direction.

Over-trim risk lives almost entirely in PERSON, not DATE_TIME -- dates do not
begin with prepositions. That is exactly what per-entity-type buys us:

    DATE_TIME : populated (all four confirmed defects live here)
    everything else (PERSON, LOCATION, CASE_NUMBER, ...) : EMPTY -- NO TRIMMING

Add to a list on EVIDENCE ONLY.

Trimming fixes SWALLOW, not RECALL. "date July 1" trims to "July 1"; the ", 2025"
that Presidio failed to include is a detection gap, out of scope, and stays
covered by the mandatory human review gate.

CASING / LANGUAGE -- read before adding a Turkish term
------------------------------------------------------
Blacklist matching folds the token with str.lower(). Python has NO equivalent of
JavaScript's toLocaleLowerCase("tr") -- str.lower() is locale-independent by
design, and the `locale` module does not reach into string casing. It gets
Turkish wrong two ways:

    "I".lower()    -> "i"            (Turkish wants "i" dotless -> "\u0131")
    "\u0130".lower()  -> "i" + U+0307   TWO codepoints; compares unequal to a
                                     normally-typed "i". Length changes.
                                     casefold() does the same. NFC will not fix
                                     it (no precomposed lowercase i-with-dot).

But note the trap in the other direction: a Turkish folder applied to ENGLISH
text is ALSO wrong -- it maps "IN" -> "\u0131n", which fails to match the entry
"in". These decrees are full of all-caps headings, so that would fire for real.

  ==> The casing function is a property of the LIST'S LANGUAGE, not a global
      utility. There is no single folder that serves both.

Today this is a non-issue: the blacklist is English-only, so on a Turkish
document the English terms simply never match -- under-trim, the safe direction.

  TRIGGER CONDITION: the day a Turkish term is added to any blacklist,
  /anonymize needs a `language` field on its request model (it has none today --
  /detect-pii takes one, /anonymize does not) plus a per-language folder. That
  is a CONTRACT change, not a tweak. Do not add a Turkish term without it; the
  failure is a silent no-match.

  For reference, the two real options when that day comes:
      PyICU (true equivalent -- same ICU implementation JS uses; required if the
      keys must match the glossary node's toLocaleLowerCase("tr")):
          str(icu.UnicodeString(s).toLower(icu.Locale("tr")))
      Pre-map, no dependency:
          s.replace("\u0130", "i").replace("I", "\u0131").lower()

MID-WORD TRUNCATION -- warn-only, do NOT fix (locked with Jacob)
----------------------------------------------------------------
Zero confirmed instances in the 5-page run. The parity argument with /redact's
whole-word-box policy does not survive contact: /redact EXPANDS to whole words
because over-covering pixels is safe. Here there is no way to distinguish
"Presidio truncated the entity" from "the entity genuinely ends here" without
guessing, and guessing wrong EXPANDS a span -- swallowing MORE text out of
translation. Wrong direction. So: detect, warn, wait for evidence.

DEDUPE (two bonuses that fall out of normalizing before dedupe)
---------------------------------------------------------------
1. Dedupe now works ACROSS WRAPS. "Riley\\nQ. Millbrook" normalizes to the value
   "Riley Q. Millbrook", which matches the unbroken occurrence's key -- same
   placeholder, one restore entry, consistent translation across pages. V1 gave
   those two mentions separate identities.
2. A latent v1 bug closes. V1 keyed dedupe on raw_value.strip() but stored
   raw_value UNSTRIPPED in the restore map, so a span with edge whitespace
   restored that whitespace into the document. After v2's whitespace trim no
   span has edge whitespace, and the two converge by construction.

WHITESPACE INVARIANT WE RELY ON
-------------------------------
ocr_extract.py builds text as " ".join(words) per line and "\\n".join(lines),
with every word .strip()ed. So inside any span, whitespace is EXACTLY single
spaces and single newlines -- never runs. value.replace("\\n", " ") therefore
cannot produce a double space.

SCOPE / BLAST RADIUS
--------------------
/redact consumes /detect-pii spans DIRECTLY off the same Merge, not /anonymize's
output. Nothing here touches the image path. One detection, two consumers -- and
only one of them normalizes.

100% synthetic in the self-test. No real PII. No FastAPI, no disk, no network,
no globals mutated. Self-test behind __main__; importing runs definitions only.
"""

from typing import List, Dict, Tuple, Callable, Optional


# --- tunables (locked with Jacob) ---
SCORE_FLOOR = 0.30   # drop entities below this before anything else

# Junk-word blacklists, keyed BY ENTITY TYPE. Entries MUST be lowercase (they
# are compared against _fold_for_match(token)). An entity type absent from a
# dict gets NO trimming on that end -- that absence is the safety default, not
# an oversight. Add on evidence only. Read the CASING / LANGUAGE note in the
# module docstring before adding any non-English term.
LEADING_BLACKLIST: Dict[str, frozenset] = {
    "DATE_TIME": frozenset({
        "on", "or", "about", "at", "from", "through", "until", "since",
        "by", "before", "after", "in", "within", "commencing", "effective",
        "date", "dated", "this", "beginning", "starting", "the", "of", "as",
    }),
}

TRAILING_BLACKLIST: Dict[str, frozenset] = {
    "DATE_TIME": frozenset({
        "through", "until", "to", "and", "or", "at", "on", "from",
        "the", "of", "in", "by", "as",
    }),
}


def square_format(entity_type: str, index: int) -> str:
    """Default placeholder format: [PERSON_1]. Swap this one function to change
    the token style everywhere. Unchanged from v1.

    NOTE: placeholder_survival_test_v1.py must be re-run against v2 output
    before trusting it -- v2 changes what gets emitted around the placeholder
    (the re-emitted newline), so v1's byte-identical survival result does not
    automatically carry over."""
    return f"[{entity_type}_{index}]"


def _fold_for_match(token: str) -> str:
    """
    Fold a token for blacklist comparison. ENGLISH ONLY.

    This is the single place to change if the blacklist ever goes multilingual --
    but read the CASING / LANGUAGE note in the module docstring first: you cannot
    just swap this for a Turkish folder, because a Turkish folder breaks
    all-caps English ("IN" -> "\u0131n"). The folder must be selected per list
    language, which requires a `language` field /anonymize does not yet have.
    """
    return token.lower()


# ===========================================================================
# STEP 1 -- SCORE FLOOR  (unchanged from v1)
# ===========================================================================
def apply_score_floor(entities: List[Dict],
                      floor: float = SCORE_FLOOR) -> List[Dict]:
    """Drop entities whose score is strictly below the floor. Mirrors /redact so
    the two consumers of /detect-pii spans agree on what counts as real."""
    return [e for e in entities if e.get("score", 0) >= floor]


# ===========================================================================
# STEP 2 -- RESOLVE OVERLAPS  (logic unchanged from v1; warnings gain "kind")
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

    Logic identical to v1. ONLY change: each warning now carries
    kind="overlap_exposure", so a consumer can discriminate these from the new
    boundary_normalization records in the same list.

    Returns (resolved, warnings).
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
                    "kind": "overlap_exposure",          # v2: discriminator
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
# STEP 3 -- NORMALIZE BOUNDARIES  (NEW in v2)
# ===========================================================================
def _trim_whitespace(text: str, start: int, end: int) -> Tuple[int, int]:
    """Move both offsets inward past any whitespace. May return start == end if
    the span was whitespace-only; the caller decides what to do about that."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _first_token(text: str, start: int, end: int) -> Tuple[str, int]:
    """First whitespace-delimited token in [start, end), plus the offset just
    past it. Assumes text[start] is not whitespace (caller trims first).

    No punctuation stripping: matching is on the exact whitespace-delimited
    token. All four confirmed defects swallow clean tokens ("date", "about",
    "through"), so the simplest rule covers the evidence. Anything fancier is
    unwarranted complexity in the over-trim direction."""
    i = start
    while i < end and not text[i].isspace():
        i += 1
    return text[start:i], i


def _last_token(text: str, start: int, end: int) -> Tuple[str, int]:
    """Last whitespace-delimited token in [start, end), plus its start offset."""
    i = end
    while i > start and not text[i - 1].isspace():
        i -= 1
    return text[i:end], i


def _starts_mid_word(text: str, start: int) -> bool:
    """
    True when the span's start splits two ALPHANUMERIC characters.

    Deliberately conservative: it does NOT fire on punctuation adjacency, so
    "24-FAM-12345" split at "FAM" is MISSED (the neighbor is "-"). That miss is
    the price of not firing on every "Millbrook," and "Millbrook." in the
    document -- which is most of them. A warnings list that cries wolf trains
    the reviewer to ignore it, which is worse than a miss on a case with zero
    confirmed instances. Revisit if evidence appears.
    """
    return (0 < start < len(text)
            and text[start - 1].isalnum()
            and text[start].isalnum())


def _ends_mid_word(text: str, end: int) -> bool:
    """Mirror of _starts_mid_word for the span's end. Same conservatism."""
    return (0 < end < len(text)
            and text[end - 1].isalnum()
            and text[end].isalnum())


def _blacklist_trim(text: str, start: int, end: int,
                    entity_type: str) -> Tuple[int, int, List[Dict]]:
    """
    Peel blacklisted tokens off BOTH ends, looping until neither end matches,
    so "on or about January 9, 2024" peels on -> or -> about in sequence.

    An entity type with no blacklist entry returns immediately, untouched --
    that is the PERSON/LOCATION/CASE_NUMBER path, and it is a no-op by design.

    MAY return start >= end (e.g. a span that is nothing but "about"). The
    caller reverts in that case; this function does not decide policy.

    Terminates because every iteration either strictly shrinks the span or
    breaks.
    """
    leading = LEADING_BLACKLIST.get(entity_type, frozenset())
    trailing = TRAILING_BLACKLIST.get(entity_type, frozenset())
    actions: List[Dict] = []
    if not leading and not trailing:
        return start, end, actions

    while start < end:
        start, end = _trim_whitespace(text, start, end)
        if start >= end:
            break

        token, token_end = _first_token(text, start, end)
        if _fold_for_match(token) in leading:
            actions.append({"action": "leading_trim", "token": token,
                            "fixed": True})
            start = token_end
            continue

        token, token_start = _last_token(text, start, end)
        if _fold_for_match(token) in trailing:
            actions.append({"action": "trailing_trim", "token": token,
                            "fixed": True})
            end = token_start
            continue

        break   # neither end matched -> done

    start, end = _trim_whitespace(text, start, end)
    return start, end, actions


def _normalize_one(text: str, entity: Dict) -> Tuple[Dict, Optional[Dict]]:
    """
    Normalize ONE entity's span. Returns (new_entity, warning_or_None).

    Never mutates the input entity -- returns a copy with adjusted start/end.
    Never emits an empty span: if normalization would empty it, we revert and
    warn instead. An entity we cannot clean is passed through visible to the
    reviewer, not silently dropped.
    """
    s0, e0 = entity["start"], entity["end"]
    etype = entity["entity_type"]
    actions: List[Dict] = []

    # --- diagnostics on the span AS PRESIDIO EMITTED IT (warn-only) ---------
    # Evaluated before any trim: this is a report about the detector's output,
    # and trimming can never create a mid-word boundary anyway (it always lands
    # on whitespace).
    if _starts_mid_word(text, s0):
        actions.append({"action": "mid_word_start", "fixed": False})
    if _ends_mid_word(text, e0):
        actions.append({"action": "mid_word_end", "fixed": False})

    # --- 1. whitespace trim -------------------------------------------------
    s, e = _trim_whitespace(text, s0, e0)
    whitespace_trimmed = (s, e) != (s0, e0)

    if s >= e:
        # Degenerate: whitespace-only (or empty) span. Revert; never emit empty.
        actions.append({"action": "abort_empty_span",
                        "reason": "span is whitespace-only", "fixed": False})
        s, e = s0, e0
    else:
        if whitespace_trimmed:
            actions.append({"action": "whitespace_trim", "fixed": True})

        # --- 2. blacklist trim ---------------------------------------------
        s2, e2, trims = _blacklist_trim(text, s, e, etype)
        if s2 >= e2:
            # Every token was blacklisted -- e.g. a DATE_TIME whose whole span
            # is "about". That is a DETECTION failure, not a boundary failure;
            # it is out of scope to fix. Keep the whitespace-trimmed span and
            # surface it to the reviewer.
            actions.append({"action": "abort_empty_span",
                            "reason": "every token blacklisted",
                            "fixed": False})
        else:
            s, e = s2, e2
            actions.extend(trims)

    # --- 3. newline accounting (Option B) -----------------------------------
    # Counted on the FINAL span: a newline that trimming pushed outside the span
    # is already back in the text as itself and needs no re-emission.
    newline_count = text.count("\n", s, e)
    if newline_count:
        actions.append({"action": "newline_reemit", "count": newline_count,
                        "fixed": True})

    out = dict(entity)
    out["start"] = s
    out["end"] = e

    warning = None
    if actions:
        warning = {
            "kind": "boundary_normalization",       # v2: discriminator
            "entity_type": etype,
            "score": entity.get("score"),
            "original_span": [s0, e0],
            "original_value": text[s0:e0],
            "normalized_span": [s, e],
            "normalized_value": text[s:e].replace("\n", " "),
            "actions": actions,
        }
    return out, warning


def normalize_boundaries(text: str,
                         entities: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    Clean span boundaries for every entity. Returns (normalized, warnings).

    Preserves the pairwise non-overlap invariant established by
    resolve_overlaps: every normalized span is a SUBSET of its original, and
    subsets of disjoint sets stay disjoint. So apply_anonymization's guard
    remains satisfied without re-checking.

    Order is preserved (resolved is already sorted by start, and shrinking a
    span cannot move it past a neighbor), so downstream reading-order numbering
    is unaffected.
    """
    normalized: List[Dict] = []
    warnings: List[Dict] = []
    for entity in entities:
        new_entity, warning = _normalize_one(text, entity)
        normalized.append(new_entity)
        if warning is not None:
            warnings.append(warning)
    return normalized, warnings


# ===========================================================================
# STEP 4 -- BUILD PROPOSAL  (the PROPOSE half)
# ===========================================================================
def build_proposal(text: str,
                   entities: List[Dict],
                   format_fn: Callable[[str, int], str] = square_format,
                   floor: float = SCORE_FLOOR) -> Dict:
    """
    Produce a review-ready anonymization proposal WITHOUT modifying text.

    Chains: score-floor -> resolve overlaps -> NORMALIZE BOUNDARIES ->
            dedupe + number -> assemble.

    Signature and return shape are IDENTICAL to v1 (plus the new "replacement"
    key inside each substitution), so anonymize_service.py changes only its
    import line.

    Dedupe: exact match on the NORMALIZED value, scoped PER entity_type.
    Numbering is assigned in reading order (first appearance by start offset).
    Because normalization runs first, a wrapped occurrence and an unbroken one
    now share a key -- and share a placeholder.

    Returns:
      {
        "substitutions": [   # one per OCCURRENCE (distinct span), for APPLY
           {"span":[s,e], "placeholder":"[PERSON_1]",
            "replacement":"[PERSON_1]\\n",          # <- v2: what APPLY writes
            "entity_type":"PERSON", "value":"Riley Q. Millbrook"}, ...
        ],
        "restore_map": {"[PERSON_1]": "Riley Q. Millbrook", ...},
        "warnings": [ {kind: "overlap_exposure"|"boundary_normalization", ...} ],
      }
    """
    floored = apply_score_floor(entities, floor=floor)
    resolved, overlap_warnings = resolve_overlaps(floored)
    normalized, boundary_warnings = normalize_boundaries(text, resolved)
    warnings = overlap_warnings + boundary_warnings

    value_to_placeholder: Dict[Tuple[str, str], str] = {}
    type_counters: Dict[str, int] = {}
    restore_map: Dict[str, str] = {}
    substitutions: List[Dict] = []

    for e in normalized:
        etype = e["entity_type"]
        start, end = e["start"], e["end"]

        # The VALUE is the normalized span with wraps collapsed. This is what
        # restore writes back, so it is the real name/date and nothing else.
        # No .strip() needed -- normalization guarantees no edge whitespace
        # (and that convergence closes v1's key-stripped/store-unstripped bug).
        value = text[start:end].replace("\n", " ")
        key = (etype, value)

        placeholder = value_to_placeholder.get(key)
        if placeholder is None:
            type_counters[etype] = type_counters.get(etype, 0) + 1
            placeholder = format_fn(etype, type_counters[etype])
            value_to_placeholder[key] = placeholder
            restore_map[placeholder] = value

        # Option B: re-emit every newline the span still swallows, AFTER the
        # placeholder, so line count is preserved by construction while the
        # entity stays atomic.
        newline_count = text.count("\n", start, end)
        substitutions.append({
            "span": [start, end],
            "placeholder": placeholder,
            "replacement": placeholder + ("\n" * newline_count),
            "entity_type": etype,
            "value": value,
        })

    return {
        "substitutions": substitutions,
        "restore_map": restore_map,
        "warnings": warnings,
    }


# ===========================================================================
# STEP 5 -- APPLY ANONYMIZATION  (the APPLY half)
# ===========================================================================
def apply_anonymization(text: str, substitutions: List[Dict]) -> str:
    """
    Replace each substitution's span with its replacement, RIGHT-TO-LEFT so
    earlier offsets stay valid as the string length changes.

    v2 writes s["replacement"], FALLING BACK to s["placeholder"] when the key is
    absent. That fallback is what keeps the propose/apply seam intact: a human
    who hand-edits the substitution list (option b) and omits "replacement" still
    gets correct output -- just without newline re-emission, which only matters
    for spans that cross a wrap.

    Driven ONLY by the substitutions list. Does NOT read the original entities.

    Assumes spans are non-overlapping (guaranteed by resolve_overlaps, preserved
    by normalize_boundaries, or supplied by the reviewer). Asserts it defensively
    rather than silently producing garbage.
    """
    ordered = sorted(substitutions, key=lambda s: s["span"][0], reverse=True)

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
        out = out[:st] + s.get("replacement", s["placeholder"]) + out[en:]
    return out


# ===========================================================================
# STEP 6 -- CONVENIENCE WRAPPER  (option (a) demo path: propose then apply)
# ===========================================================================
def anonymize(text: str,
              entities: List[Dict],
              format_fn: Callable[[str, int], str] = square_format,
              floor: float = SCORE_FLOOR) -> Dict:
    """
    Full option-(a) path: propose then immediately apply. Return shape identical
    to v1, so anonymize_service.py needs no change beyond its import line.
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
# ===========================================================================
def _check(label: str, got, expected) -> bool:
    ok = got == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"        got     : {got!r}")
        print(f"        expected: {expected!r}")
    return ok


def _span_of(text: str, needle: str, start_at: int = 0):
    s = text.index(needle, start_at)
    return s, s + len(needle)


def _actions(warning: Dict):
    return [a["action"] for a in warning["actions"]]


def _main():
    all_ok = True

    # =======================================================================
    # 1. REGRESSION: v2 == v1 byte-for-byte on entities with no boundary junk.
    #    This is the check that proves we changed only what we meant to.
    # =======================================================================
    print("=== 1. v1 parity on clean entities (regression) ===")
    text = "Jordan filed; later Jordan appeared. Case 12-34-5678 before Reese."
    j1 = text.index("Jordan")
    j2 = text.index("Jordan", j1 + 1)
    case_s, case_e = _span_of(text, "12-34-5678")
    reese_s, reese_e = _span_of(text, "Reese")

    clean_entities = [
        {"entity_type": "PERSON", "start": j1, "end": j1 + 6, "score": 0.85},
        {"entity_type": "PERSON", "start": j2, "end": j2 + 6, "score": 0.85},
        {"entity_type": "CASE_NUMBER", "start": case_s, "end": case_e, "score": 0.90},
        {"entity_type": "PERSON", "start": reese_s, "end": reese_e, "score": 0.80},
        {"entity_type": "US_DRIVER_LICENSE", "start": case_s, "end": case_e, "score": 0.01},
    ]
    v2_result = anonymize(text, clean_entities)

    try:
        import anonymize_core_v1 as v1
        v1_result = v1.anonymize(text, clean_entities)
        all_ok &= _check("v2 anonymized_text == v1 anonymized_text",
                         v2_result["anonymized_text"], v1_result["anonymized_text"])
        all_ok &= _check("v2 restore_map == v1 restore_map",
                         v2_result["restore_map"], v1_result["restore_map"])
        all_ok &= _check("v2 warnings == v1 warnings (both empty here)",
                         v2_result["warnings"], v1_result["warnings"])
    except ImportError:
        print("  [SKIP] anonymize_core_v1.py not importable; "
              "falling back to hardcoded v1 expectations")
        all_ok &= _check(
            "anonymized text matches v1's known output",
            v2_result["anonymized_text"],
            "[PERSON_1] filed; later [PERSON_1] appeared. "
            "Case [CASE_NUMBER_1] before [PERSON_2].")

    # =======================================================================
    # 2. DEFECT 1 -- internal newline. Option B + dedupe across the wrap.
    # =======================================================================
    print("\n=== 2. defect: internal newline (Option B, re-emit after) ===")
    # Whitespace honors the /ocr invariant: single spaces within a line,
    # single newlines between lines.
    text_nl = ("The Minor Children are Avery J. Millbrook and Riley\n"
               "Q. Millbrook per the decree. Petitioner shall claim Riley Q. Millbrook\n"
               "as a dependent for tax purposes.")
    w_s, w_e = _span_of(text_nl, "Riley\nQ. Millbrook")
    u_s, u_e = _span_of(text_nl, "Riley Q. Millbrook")
    ents_nl = [
        {"entity_type": "PERSON", "start": w_s, "end": w_e, "score": 0.85},
        {"entity_type": "PERSON", "start": u_s, "end": u_e, "score": 0.85},
    ]
    r_nl = anonymize(text_nl, ents_nl)

    all_ok &= _check(
        "anonymized text (newline re-emitted AFTER placeholder)",
        r_nl["anonymized_text"],
        "The Minor Children are Avery J. Millbrook and [PERSON_1]\n"
        " per the decree. Petitioner shall claim [PERSON_1]\n"
        "as a dependent for tax purposes.")
    all_ok &= _check("NEWLINE COUNT PRESERVED (the Stage 1 desync fix)",
                     r_nl["anonymized_text"].count("\n"), text_nl.count("\n"))
    all_ok &= _check("dedupe collapses ACROSS the wrap -> one placeholder",
                     r_nl["restore_map"], {"[PERSON_1]": "Riley Q. Millbrook"})
    all_ok &= _check("restore value has the wrap collapsed to a space",
                     r_nl["restore_map"]["[PERSON_1]"], "Riley Q. Millbrook")
    all_ok &= _check("span UNCHANGED for the newline case (v1 offsets stand)",
                     r_nl["substitutions"][0]["span"], [w_s, w_e])
    all_ok &= _check("replacement carries the newline, placeholder does not",
                     (r_nl["substitutions"][0]["replacement"],
                      r_nl["substitutions"][0]["placeholder"]),
                     ("[PERSON_1]\n", "[PERSON_1]"))
    nl_warn = [w for w in r_nl["warnings"] if w["kind"] == "boundary_normalization"]
    all_ok &= _check("newline_reemit warning raised once",
                     [_actions(w) for w in nl_warn], [["newline_reemit"]])
    all_ok &= _check("un-detected 'Avery J. Millbrook' left untouched",
                     "Avery J. Millbrook" in r_nl["anonymized_text"], True)

    # =======================================================================
    # 3. DEFECT 2 -- trailing swallow. The word RETURNS to the translator.
    # =======================================================================
    print("\n=== 3. defect: trailing swallow ('6:00 p.m. through') ===")
    text_tt = "Parenting time runs from Friday at 6:00 p.m. through Sunday at 8:00 a.m."
    t_s, t_e = _span_of(text_tt, "6:00 p.m. through")
    r_tt = anonymize(text_tt, [{"entity_type": "DATE_TIME",
                                "start": t_s, "end": t_e, "score": 0.85}])
    all_ok &= _check(
        "'through' is now OUTSIDE the span -> stays in text -> gets translated",
        r_tt["anonymized_text"],
        "Parenting time runs from Friday at [DATE_TIME_1] through Sunday at 8:00 a.m.")
    all_ok &= _check("restore value is the date alone",
                     r_tt["restore_map"], {"[DATE_TIME_1]": "6:00 p.m."})
    all_ok &= _check("trailing_trim warning names the trimmed token",
                     r_tt["warnings"][0]["actions"],
                     [{"action": "trailing_trim", "token": "through", "fixed": True}])
    all_ok &= _check("span was adjusted inward (offsets stay load-bearing)",
                     r_tt["substitutions"][0]["span"],
                     [t_s, t_s + len("6:00 p.m.")])

    # =======================================================================
    # 4. DEFECT 3 -- leading swallow of a NOUN (what a stopword list misses).
    # =======================================================================
    print("\n=== 4. defect: leading swallow ('date July 1') ===")
    text_lt = "Effective date July 1, 2025, payments commence monthly."
    l_s, l_e = _span_of(text_lt, "date July 1")
    r_lt = anonymize(text_lt, [{"entity_type": "DATE_TIME",
                                "start": l_s, "end": l_e, "score": 0.85}])
    all_ok &= _check("'date' trimmed off the front (spaCy is_stop would miss it)",
                     r_lt["restore_map"], {"[DATE_TIME_1]": "July 1"})
    all_ok &= _check("anonymized text",
                     r_lt["anonymized_text"],
                     "Effective date [DATE_TIME_1], 2025, payments commence monthly.")
    # Honest note: ", 2025" staying outside is a RECALL gap in /detect-pii, not a
    # boundary defect. Out of scope; the human review gate is the backstop.

    # =======================================================================
    # 5. DEFECT 4 -- leading swallow, LOOPING ("on or about ...").
    # =======================================================================
    print("\n=== 5. defect: looping leading trim ('on or about January 9, 2024') ===")
    text_loop = "The parties separated on or about January 9, 2024, and lived apart."
    lo_s, lo_e = _span_of(text_loop, "on or about January 9, 2024")
    r_loop = anonymize(text_loop, [{"entity_type": "DATE_TIME",
                                    "start": lo_s, "end": lo_e, "score": 0.85}])
    all_ok &= _check("all three leading tokens peeled in sequence",
                     r_loop["restore_map"], {"[DATE_TIME_1]": "January 9, 2024"})
    all_ok &= _check("anonymized text",
                     r_loop["anonymized_text"],
                     "The parties separated on or about [DATE_TIME_1], and lived apart.")
    all_ok &= _check("one warning per trimmed token, in peel order",
                     [(a["action"], a["token"]) for a in r_loop["warnings"][0]["actions"]],
                     [("leading_trim", "on"), ("leading_trim", "or"),
                      ("leading_trim", "about")])

    # =======================================================================
    # 6. NO TRIMMING for types with no blacklist (the over-trim guard).
    # =======================================================================
    print("\n=== 6. PERSON/LOCATION are never trimmed (under-trim, never over-trim) ===")
    text_van = "The estate of van Beethoven at the Ludwig residence."
    v_s, v_e = _span_of(text_van, "van Beethoven")
    r_van = anonymize(text_van, [{"entity_type": "PERSON",
                                  "start": v_s, "end": v_e, "score": 0.85}])
    all_ok &= _check("'van' survives -- PERSON has no blacklist, so no trimming",
                     r_van["restore_map"], {"[PERSON_1]": "van Beethoven"})
    all_ok &= _check("no boundary warning raised for an untouched PERSON",
                     r_van["warnings"], [])
    # Same token, DATE_TIME type: proves the trim is TYPE-SCOPED, not global.
    text_the = "Filed the 3rd of May by the clerk."
    th_s, th_e = _span_of(text_the, "the 3rd of May")
    r_the = anonymize(text_the, [{"entity_type": "DATE_TIME",
                                  "start": th_s, "end": th_e, "score": 0.85}])
    all_ok &= _check("same words WOULD trim under DATE_TIME -> type-scoped",
                     r_the["restore_map"], {"[DATE_TIME_1]": "3rd of May"})

    # =======================================================================
    # 7. ABORT GUARD -- never emit an empty span.
    # =======================================================================
    print("\n=== 7. abort guard: all-junk span reverts, never empties ===")
    text_ab = "The hearing was set for about noon today."
    a_s, a_e = _span_of(text_ab, "about")
    r_ab = anonymize(text_ab, [{"entity_type": "DATE_TIME",
                                "start": a_s, "end": a_e, "score": 0.85}])
    all_ok &= _check("span reverted, not emptied",
                     r_ab["substitutions"][0]["span"], [a_s, a_e])
    all_ok &= _check("value preserved as-is",
                     r_ab["restore_map"], {"[DATE_TIME_1]": "about"})
    all_ok &= _check("abort_empty_span warning surfaced for the reviewer",
                     "abort_empty_span" in _actions(r_ab["warnings"][0]), True)

    # =======================================================================
    # 8. MID-WORD TRUNCATION -- warn-only, span untouched.
    # =======================================================================
    print("\n=== 8. mid-word truncation is detected but NOT fixed ===")
    text_mw = "Payor JordanA. Millbrook remitted the sum."
    m_s, m_e = _span_of(text_mw, "Jordan")
    r_mw = anonymize(text_mw, [{"entity_type": "PERSON",
                                "start": m_s, "end": m_e, "score": 0.85}])
    all_ok &= _check("mid_word_end detected", 
                     "mid_word_end" in _actions(r_mw["warnings"][0]), True)
    all_ok &= _check("warn-only: fixed == False",
                     [a["fixed"] for a in r_mw["warnings"][0]["actions"]], [False])
    all_ok &= _check("span left exactly as Presidio emitted it",
                     r_mw["substitutions"][0]["span"], [m_s, m_e])
    # And the conservatism check: normal punctuation must NOT cry wolf.
    text_pn = "Petitioner, Jordan A. Millbrook, appeared in person."
    p_s, p_e = _span_of(text_pn, "Jordan A. Millbrook")
    r_pn = anonymize(text_pn, [{"entity_type": "PERSON",
                                "start": p_s, "end": p_e, "score": 0.85}])
    all_ok &= _check("no false mid-word warning on a trailing comma",
                     r_pn["warnings"], [])

    # =======================================================================
    # 9. SEAM INTEGRITY -- a human-edited mapping with no "replacement" applies.
    # =======================================================================
    print("\n=== 9. propose/apply seam: replacement-less mapping still applies ===")
    all_ok &= _check(
        "apply falls back to placeholder when 'replacement' is absent",
        apply_anonymization("Jordan filed the petition.",
                            [{"span": [0, 6], "placeholder": "[PERSON_1]"}]),
        "[PERSON_1] filed the petition.")

    print("\n=== 10. apply non-overlap guard still fires (carried from v1) ===")
    try:
        apply_anonymization("x" * 20, [{"span": [0, 10], "placeholder": "[A_1]"},
                                       {"span": [5, 15], "placeholder": "[B_1]"}])
        all_ok &= _check("guard raised on overlap", False, True)
    except ValueError:
        all_ok &= _check("guard raised on overlap", True, True)

    # =======================================================================
    # 11. WARNING KIND discriminator on the overlap path.
    # =======================================================================
    print("\n=== 11. overlap warnings carry kind='overlap_exposure' ===")
    text2 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    prop2 = build_proposal(text2, [
        {"entity_type": "PERSON", "start": 10, "end": 18, "score": 0.85},
        {"entity_type": "LOCATION", "start": 15, "end": 25, "score": 0.85},
    ])
    all_ok &= _check("overlap: one survivor (LOCATION, longer)",
                     [s["entity_type"] for s in prop2["substitutions"]], ["LOCATION"])
    all_ok &= _check("overlap warning kind + payload intact",
                     (prop2["warnings"][0]["kind"],
                      prop2["warnings"][0]["dropped_type"],
                      prop2["warnings"][0]["exposed_spans"]),
                     ("overlap_exposure", "PERSON", [[10, 15]]))
    all_ok &= _check("every warning is self-describing via 'kind'",
                     all("kind" in w for w in prop2["warnings"]), True)

    # =======================================================================
    # 12. EMPTY / no-entity paths.
    # =======================================================================
    print("\n=== 12. degenerate inputs ===")
    all_ok &= _check("no entities -> text unchanged, empty maps",
                     anonymize("Nothing to see here.", []),
                     {"anonymized_text": "Nothing to see here.", "restore_map": {},
                      "warnings": [], "substitutions": []})
    all_ok &= _check("all entities below floor -> text unchanged",
                     anonymize("Jordan filed.",
                               [{"entity_type": "PERSON", "start": 0, "end": 6,
                                 "score": 0.01}])["anonymized_text"],
                     "Jordan filed.")

    print("\n" + "=" * 62)
    print(f"OVERALL: {'ALL PASS' if all_ok else 'SOME FAILED'}")
    print("=" * 62)
    return all_ok


if __name__ == "__main__":
    _main()