# Setup Notes

Running log of validation steps, design decisions, and scoping calls made
during development of the ABC Link legal document translation pipeline. This
file exists to keep deferred scope and verification work visible rather than
silently dropped — see the project's own principle: architecture diagrams,
setup notes, and design docs should show phase-2 items and validation history
explicitly rather than hiding gaps.

---

## Placeholder Format Validation (anonymize_core_v2.py)

**Date:** July 2026

Before switching the anonymization service from `anonymize_core_v1.py` to
`anonymize_core_v2.py`, placeholder survival through translation was
re-verified against the production translation model.

**Why re-verification was necessary:** v2 changes what gets emitted around a
placeholder for entities whose span crosses a newline (a line-wrap artifact
from OCR, not a real paragraph break). Where v1 emitted the placeholder alone
(e.g. `[PERSON_6]`), v2 emits the placeholder followed by a re-inserted
newline (e.g. `[PERSON_6]\n`) to preserve line-count parity with the OCR
output while keeping the entity atomic (see `anonymize_core_v2.py` module
docstring for the full design rationale — "Option B"). That is new surface
area the translation model could mangle or reposition, so v1's earlier
byte-identical survival result does not automatically carry over to v2.

**What was tested:** Survival of the `square` placeholder format
(`[ENTITY_TYPE_N]`) across both translation directions (EN→TR and TR→EN),
including a case built specifically to reproduce the newline-adjacent shape
v2 introduces — a placeholder immediately followed by a line break rather
than a space, matching v2's real output byte-for-byte.

**Model:** Gemini 3.5 Flash

**Result:** 100% survival (12/12 token checks), including both
newline-adjacency cases.

**Verified via:** a standalone diagnostic script
(`placeholder_survival_test_v2.py`), scoped narrowly to this question. It is
**not part of the deployed pipeline** — no service imports it, and it makes
live calls to the translation API rather than running offline. Kept out of
the shipped repo path for that reason; this note is the durable record of
what it confirmed and why the check was needed.

---
