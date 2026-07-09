# presidio_test_v3.py  (v3 — context-boosted CASE_NUMBER that beats DATE_TIME)
# Still 100% synthetic text. No real PII.

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.context_aware_enhancers import LemmaContextAwareEnhancer
from presidio_anonymizer import AnonymizerEngine

# --- 1. Define the custom recognizer ---------------------------------------
# Strategy: split patterns by how DISTINCTIVE they are.
#   - Distinctive (has a letter block) -> safe to score HIGH (can't be a date).
#   - Ambiguous (all digits) -> score LOW, let context words lift it.

case_number_patterns = [
    # HIGH confidence: digits - LETTERS - digits. The letter block (CV, FAM,...)
    # means this can't be a date, so 0.85 outright is safe.
    Pattern(
        name="case_alpha_block",
        regex=r"\b\d{2,4}[-/][A-Z]{2,4}[-/]\d{3,6}\b",
        score=0.85,
    ),

    # LOW confidence: all-numeric, hyphen OR slash separated (##-##-#### etc.).
    # These COLLIDE with dates by shape, so we keep the base score low (0.40)
    # and rely on context words to boost them above DATE_TIME (0.85) when a
    # legal label is nearby.
    Pattern(
        name="case_all_numeric",
        regex=r"\b\d{2,4}[-/]\d{2,4}[-/]\d{3,6}\b",
        score=0.40,
    ),
]

# Context words — English AND Turkish legal labels. "Esas No" and "Dosya No"
# are the real labels on Turkish court documents; these are what make the
# ambiguous all-numeric pattern win against DATE_TIME.
case_number_context = [
    "case", "case no", "case number", "docket", "file no",
    "esas", "esas no", "dosya", "dosya no", "karar",
]

case_number_recognizer = PatternRecognizer(
    supported_entity="CASE_NUMBER",
    patterns=case_number_patterns,
    context=case_number_context,
)

# --- 2. Configure a context enhancer with a LARGER boost -------------------
# Default boost is small. We raise it so a context hit on the LOW (0.40)
# pattern clears DATE_TIME's 0.85. 0.40 + 0.50 = 0.90 > 0.85. We can SEE the math.
enhancer = LemmaContextAwareEnhancer(
    context_similarity_factor=0.50,   # how much a context hit adds
    min_score_with_context_similarity=0.55,  # floor once context matches
)

analyzer = AnalyzerEngine(context_aware_enhancer=enhancer)
analyzer.registry.add_recognizer(case_number_recognizer)

# --- 3. Test text — same fake formats as v2 plus an UNLABELED ambiguous one -
text = (
    "Jacob Miller, born 04/12/1988, filed case no. 12-34-5678 in Ankara. "
    "A related US matter, case number 2024-CV-00891, references docket 24-FAM-12345. "
    "Esas No: 2023/CV/451 was also cited. "
    "An unlabeled number 55-66-7890 appears with no context."  # should stay LOW
)

results = analyzer.analyze(text=text, language="en")

print("=== DETECTED ENTITIES ===")
for r in sorted(results, key=lambda x: x.start):
    print(f"{r.entity_type:15} | {text[r.start:r.end]:22} | score={r.score:.2f}")

# --- 4. Anonymize ----------------------------------------------------------
anonymizer = AnonymizerEngine()
anonymized = anonymizer.anonymize(text=text, analyzer_results=results)

print("\n=== ANONYMIZED TEXT ===")
print(anonymized.text)