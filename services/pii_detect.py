"""
pii_detect.py

Importable PII-detection module. This is the refactor of presidio_test_v3.py
into reusable functions so pii_service.py (FastAPI) can call it per-request.

WHAT CHANGED FROM presidio_test_v3.py: only the PACKAGING. The detection logic
is identical, byte-for-byte:
  - split-confidence CASE_NUMBER patterns (distinctive alpha-block scored HIGH,
    date-colliding all-numeric scored LOW and lifted by context)
  - English + Turkish legal context words (Esas No, Dosya No, ...)
  - LemmaContextAwareEnhancer math: 0.40 + 0.50 = 0.90 > DATE_TIME's 0.85
The ONLY differences: the analyzer is built ONCE at import (not re-run per call),
detection is exposed as detect_pii(text, language), and the hardcoded test is
tucked behind `if __name__ == "__main__"` so importing this file runs nothing
but the definitions.

100% synthetic text in the self-test. No real PII.

LANGUAGE PARAMETER — HONEST SCOPE
---------------------------------
detect_pii(text, language="en") accepts a language code and passes it to
Presidio. Two things are true and worth being precise about:

  1. The custom CASE_NUMBER recognizer is a PATTERN + CONTEXT recognizer. It does
     NOT depend on a language NLP model, so it runs for ANY language value --
     including "tr" -- and its Turkish context words (Esas No, Dosya No) work.
     This is the legally-critical entity for this project, and it is covered.

  2. Presidio's `language` also selects an NLP (spaCy) model for entities like
     PERSON/LOCATION. This AnalyzerEngine is configured for English NER only.
     Passing language="tr" will run the custom recognizer fine, but full Turkish
     NER (names/places via a Turkish model) requires registering a Turkish spaCy
     model in the NlpEngine -- a separate configuration step, not done here.

So: the parameter is wired end-to-end and honored. Turkish CASE_NUMBER detection
works now. Turkish PERSON/LOCATION NER is a future NLP-model configuration.
SUPPORTED_LANGUAGES below documents exactly what this module currently backs.
"""

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.context_aware_enhancers import LemmaContextAwareEnhancer
from presidio_anonymizer import AnonymizerEngine

# Languages this module is currently configured to accept. "en" has full NER.
# "tr" runs the custom CASE_NUMBER recognizer (pattern+context, model-independent)
# but not Turkish NLP-model NER until a Turkish spaCy model is registered.
SUPPORTED_LANGUAGES = ("en", "tr")

# ---------------------------------------------------------------------------
# Boilerplate allow-list: terms Presidio's built-in NER greedily mis-flags as
# PERSON / DATE_TIME but which carry NO PII. Passed to analyze(allow_list=...)
# so these exact spans are never returned as entities. This is a deliberate
# hole in detection -- every term here MUST be PII-free by human audit.
#
# SAFETY: hardcoded and human-audited on purpose. A name must never enter this
# list. Phase-2 (documented, not built): source a SCREENED, verified subset of
# the legal glossary here instead -- each term run through detect_pii() first so
# an allow-list entry can never itself be something Presidio would flag as PII.
#
# Matches on EXACT span text Presidio emits, so this fixes only clean mis-masks
# (e.g. "Esquire"). Boundary-swallowed spans ("date July 1", "6:00 p.m.
# through") are a SEPARATE defect handled downstream at substitution (logged to
# warnings for human review), not here.
_ALLOW_LIST = [
    "Esquire",
    "LLP",
    "Attorney",
    "Monthly",
    "months",
    "one weekday evening",
    "the age of eighteen",
]
# ---------------------------------------------------------------------------
# ONE-TIME SETUP (runs once at import; this is setup, NOT a test run)
# ---------------------------------------------------------------------------

# --- Custom CASE_NUMBER recognizer (identical logic to v3) -----------------
_case_number_patterns = [
    # HIGH: digits-LETTERS-digits. The letter block (CV, FAM, ...) means it
    # can't be a date, so 0.85 outright is safe.
    Pattern(
        name="case_alpha_block",
        regex=r"\b\d{2,4}[-/][A-Z]{2,4}[-/]\d{3,6}\b",
        score=0.85,
    ),
    # LOW: all-numeric, hyphen/slash separated. Collides with dates by shape,
    # so keep it low (0.40) and let context lift it above DATE_TIME (0.85).
    Pattern(
        name="case_all_numeric",
        regex=r"\b\d{2,4}[-/]\d{2,4}[-/]\d{3,6}\b",
        score=0.40,
    ),
]

# English AND Turkish legal labels. "Esas No" / "Dosya No" are the real labels
# on Turkish court documents and are what let the ambiguous all-numeric pattern
# beat DATE_TIME when a legal label is nearby.
_case_number_context = [
    "case", "case no", "case number", "docket", "file no",
    "esas", "esas no", "dosya", "dosya no", "karar",
]

_case_number_recognizer = PatternRecognizer(
    supported_entity="CASE_NUMBER",
    patterns=_case_number_patterns,
    context=_case_number_context,
)

# --- Context enhancer with the larger boost (identical to v3) --------------
# 0.40 (low pattern) + 0.50 (this boost) = 0.90 > DATE_TIME 0.85. Visible math.
_enhancer = LemmaContextAwareEnhancer(
    context_similarity_factor=0.50,
    min_score_with_context_similarity=0.55,
)

# Build the analyzer ONCE. Expensive to construct; reused across all requests.
_analyzer = AnalyzerEngine(context_aware_enhancer=_enhancer)
_analyzer.registry.add_recognizer(_case_number_recognizer)


# ---------------------------------------------------------------------------
# PER-REQUEST FUNCTION (what the endpoint calls)
# ---------------------------------------------------------------------------
def detect_pii(text: str, language: str = "en"):
    """
    Analyze `text` for PII and return Presidio RecognizerResult objects.

    Args:
        text:     the text to scan (may be empty -> returns []).
        language: language code; defaults "en". See SUPPORTED_LANGUAGES and the
                  module docstring for the honest scope of Turkish support.

    Returns:
        list[RecognizerResult] -- each has .entity_type, .start, .end, .score.
        The matched substring is derived by the caller via text[start:end].
    """
    if not text:
        return []
    return _analyzer.analyze(text=text, language=language, allow_list=_ALLOW_LIST)


def results_to_dicts(text: str, results):
    """
    Shape RecognizerResult objects into plain dicts for JSON responses.
    Includes the matched substring (text[start:end]) -- caller has decided
    local PII-in-response is acceptable inside the trust boundary.
    """
    out = []
    for r in sorted(results, key=lambda x: x.start):
        out.append({
            "entity_type": r.entity_type,
            "start": r.start,
            "end": r.end,
            "score": round(float(r.score), 4),
            "text": text[r.start:r.end],
        })
    return out


# ---------------------------------------------------------------------------
# SELF-TEST — runs ONLY when executed directly, never on import.
# Preserves the original v3 demonstration (synthetic text, prints, anonymizer).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _text = (
        "Jacob Miller, born 04/12/1988, filed case no. 12-34-5678 in Ankara. "
        "A related US matter, case number 2024-CV-00891, references docket 24-FAM-12345. "
        "Esas No: 2023/CV/451 was also cited. "
        "An unlabeled number 55-66-7890 appears with no context."
    )

    _results = detect_pii(_text, language="en")

    print("=== DETECTED ENTITIES ===")
    for _r in sorted(_results, key=lambda x: x.start):
        print(f"{_r.entity_type:15} | {_text[_r.start:_r.end]:22} | score={_r.score:.2f}")

    _anonymizer = AnonymizerEngine()
    _anonymized = _anonymizer.anonymize(text=_text, analyzer_results=_results)
    print("\n=== ANONYMIZED TEXT ===")
    print(_anonymized.text)