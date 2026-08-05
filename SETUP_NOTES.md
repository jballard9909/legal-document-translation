# Setup Notes

Running log of validation steps, design decisions, and scoping calls made
during development of the ABC Link legal document translation pipeline. This
file exists to keep deferred scope and verification work visible rather than
silently dropped — see the project's own principle: architecture diagrams,
setup notes, and design docs should show phase-2 items and validation history
explicitly rather than hiding gaps.

---

## Placeholder Format Validation (anonymize_core_v2.py)

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

## INTAKE — EMAIL ADDRESS SCOPE DECISION

The client email address submitted through the intake form is deliberately
excluded from the PII privacy chain. It is collected as plain form data and
travels through the workflow in the clear to the delivery node.

This is intentional, not an oversight. The email address is the delivery
destination — anonymizing it would make the package undeliverable. It is the
one field whose function requires it to remain in plaintext.

Scope of collection is minimized accordingly: the form collects only the
document and the delivery address. Client name was considered and deliberately
dropped — nothing in the pipeline consumes it, so collecting it would have
expanded PII surface with no functional justification.

The document itself receives full privacy-chain treatment (local OCR → local
PII detection → text anonymization + image redaction before any cloud call).
The distinction is: document contents are protected by architecture; the
delivery address is protected by minimization.

---

## Root-cause correction: the "fused rows" are not an OCR defect

The original handoff attributed rows like `Payment method ... Health insurance ... Uninsured medical/dental ...` running together on one line to OCR quality. Verified false: `/ocr`'s own `text` and `lines[]` output preserve all rows as separate lines (distinct `line_id` values, e.g. `10:4:1` / `10:4:2` / `10:4:3`). The fusion is introduced by Build Structured Text's paragraph-join rule — lines sharing a `block:par` prefix get joined with a space — meeting a table, where Tesseract grouped multiple table rows into one paragraph. Confirmed against all five pages; the same mechanism also fused the asset/liability table's first two rows.

---

## Known model behavior: confident-wrong correction of OCR garble (two confirmed instances)

- Diagnostic testing (`table_continuation_hint_test_v2.py`, 20 reps): 1-in-5 reps silently "corrected" `5596`/`4596` to `55%`/`45%` despite the prompt instructing verbatim preservation of OCR-damaged figures. Reps 1, 2, 4, 5 carried the garble through correctly; rep 3 fixed it.
- Confirmed again in a delivered PDF's asset table: the OCR fragment `Rivergrove,Hé&pondent` was split by Gemini into "Rivergrove" + "Respondent," dropping the garbled "Hé&" — a plausible but unverified interpretation.

Both are the same failure mode: the model resolving ambiguous/garbled source text with unwarranted confidence instead of flagging it.

---

## Table-boundary geometry detection: derivation and documented limits

Threshold (150px column-gap) derived from a sweep (60–420px) against this document's real `/ocr` coordinates — stable across 60–320px, confirmed via `diagnostics/results/table_boundary_geometry_test_v1_gap150.txt`. Explicitly scoped limits:

- Validated against **one document's layout only**; re-run the sweep before trusting these constants on a different document.
- The rule never got tested against a true two-different-tables-adjacent case in this document (the asset table sits mid-page, so the trailing-run walk never reached it). Band-count discrimination (2 vs. 3 columns) is inference, not a tested result.
- Page 5's signature block is a genuine multi-line 2-band structure that did *not* false-positive only because the page opens with a non-grid-like heading, breaking the leading run before it started. Band-position tolerance (45px delta vs. 25px tolerance) would have been the actual discriminator had that heading not existed — untested.

---

## md_render_v2: footer position after a spliced table (accepted tradeoff, not a defect)

When a table is spliced across a page boundary, the source page's original footer caption (e.g. "Sayfa 2 —...") now renders after the *complete* merged table rather than after the table's original first row. This is a direct consequence of `render_body`'s pre-existing "no forced page breaks, content flows naturally" design decision — adding rows pushes later content further down, so the DOCX's physical page break lands differently, and source/translation page alignment was already an accepted drift. Confirmed via direct render inspection, not just reasoning.

---

## Pre-existing asymmetry: footer rendering by direction

`_FOOTER_RE` matches only `^Sayfa\s+\d+` (Turkish). In the `tr>en` direction, an English footer ("Page N —...") does not match, so it renders as a justified body paragraph instead of a centered caption. Pre-existing in v1; not touched by the v2 splice, which uses its own bilingual `_SPLICE_SKIP_RE` only to *locate* footers, not to style them.

---

## Not a pipeline issue: Google Drive's own OCR on appended source pages

When inspecting delivered PDFs via Drive's text extraction, the appended original-English source pages showed their own unrelated garble (e.g. "Rivergrove, FR 00000" → "Rivergrove, FRRespondent"). This is Google's OCR of the rasterized source images at read-time, entirely outside this pipeline — noting it so it isn't later mistaken for a regression when someone diffs text extractions.

---

## Known issue: signature block instability

The "IX. ORDER" / `APPROVED AS TO FORM AND CONTENT:` signature block shows unstable structure across independent Gemini calls on byte-identical input — confirmed three distinct outcomes across three real runs: escape-hatch-flagged plain text, unflagged plain paragraphs, and an unflagged table with swapped Petitioner/Respondent attribution. Root cause not yet diagnosed.
