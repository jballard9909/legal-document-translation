# Certified Legal Document Translation Pipeline

**Turkish ⇄ English certified translation with architecturally enforced PII protection.**

An n8n workflow that takes a scanned legal document, strips every piece of
personally identifiable information *before* any cloud API call, translates the
anonymized text, restores the PII locally, and reassembles the result as a
structure-preserving certified translation package: Translated document, sworn
affidavit, and original source pages all delivered after human review.

Behind the workflow sits a pipeline of five local FastAPI microservices handling
OCR, PII detection, redaction, anonymization, and document assembly.

![n8n](https://img.shields.io/badge/n8n-orchestration-EA4B71?logo=n8n&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-5_microservices-009688?logo=fastapi&logoColor=white)
![Presidio](https://img.shields.io/badge/Presidio-PII_detection-0078D4)
![Gemini](https://img.shields.io/badge/Gemini_2.5_Flash-translation-4285F4?logo=google&logoColor=white)

> Built for a legal services client under NDA. All examples, screenshots, and test
> documents in this repository use synthetic data. Implementation code was written
> with AI assistance — see [How This Was Built](#how-this-was-built).
## Demo

<p align="center">
  <img src="docs/assets/demo.gif" alt="End-to-end run: upload through delivered package" width="800">
</p>

<p align="center"><em>
Synthetic divorce decree, English → Turkish. Upload through delivered package.
</em></p>

## The Problem

Certified translation of legal documents is a slow, manual, high-stakes process.
A single divorce decree, birth certificate, or court record must be transcribed,
translated, formatted to mirror the original, and accompanied by a signed
certificate of accuracy. Under 8 CFR 103.2(b)(3), U.S. immigration filings require
a *complete* English translation of any foreign-language document, together with
the translator's signed attestation of accuracy and competence — where "complete"
means every element on the page, including stamps, seals, and marginal notations,
not merely the body text.

Machine translation solves the wrong half of this. Pasting a legal document into
a translation API returns text in reading order, stripped of the structure that
made it a legal instrument, and, more seriously, transmits names, addresses,
case numbers, and national identity numbers to a third-party service. For
documents whose entire purpose is to record who someone is, that is not an
acceptable trade.

This pipeline was built to solve both halves at once:

- **No PII leaves the local environment.** Detection, redaction, and anonymization
  all run locally. The cloud translation model receives placeholder tokens
  (`[PERSON_1]`, `[CASE_NUMBER_2]`) and never sees the underlying values.
- **Structure is preserved, not flattened.** Output mirrors the source document's
  headings, tables, and layout rather than returning a wall of prose.
- **A human approves before anything is delivered.** Translation is drafted
  automatically; certification is not automatic.

## How It Works

<p align="center">
  <img src="docs/assets/architecture.jpg" alt="Pipeline architecture" width="900">
</p>

A single n8n workflow orchestrates five local FastAPI microservices and a set of
Google services. A browser front end posts a document to the workflow's webhook;
every processing stage between OCR and translation runs locally.

**1) Intake.** A webhook receives the uploaded document. The shared glossary is
loaded from Google Sheets at the start of the run, before OCR, so approved
terminology is available to the run rather than only written at the end of it.

**2) OCR.** `/ocr` (Tesseract) extracts text along with per-word character spans
and pixel bounding boxes. Character spans are built in the same pass as the full
text, so string and offsets cannot drift apart.

**3) PII detection.** The document is split per page. Each page goes to
`/detect-pii` (Microsoft Presidio) while the OCR result is held on a parallel
branch. The two are recombined by matching `page_index` rather than by arrival
order, so pairing is order-independent.

> **A note on `/redact`.** A pixel-level redaction service (`/redact`, port 8003)
> is built and verified: it maps detected PII character spans to the corresponding
> OCR word bounding boxes and draws filled rectangles over them, with a raster-parity
> assertion that fails loudly rather than mis-placing boxes. It runs in the current
> workflow but its output is not yet consumed downstream. The assembled package
> includes the untouched original pages. Wiring redacted images into a
> reviewer-facing path is planned for phase 2.

**4) Anonymization.** `/anonymize` replaces detected entities with typed
placeholders (`[PERSON_1]`, `[CASE_NUMBER_2]`), resolving overlapping spans by
confidence score and trimming boundary artifacts. The mapping is retained locally.

**5) Translation.** Language direction is detected from character frequency.
Gemini receives placeholder-bearing text only.

**6) Restoration.** `/restore` reinserts the original PII values into the
translated text at their mapped positions, locally.

**7) Glossary harvest.** A parallel branch extracts terminology from the
*anonymized* translation, deduplicates it with Turkish-locale-aware casing, and
appends or updates rows in the shared glossary. Verified entries are structurally
unreachable by the write path.

**8) Rendering.** `/render-body` converts the reassembled Markdown into a DOCX
that mirrors the source document's headings, tables, and layout, flagging
uncertain regions for reviewer attention.

**9) Human review.** The draft is uploaded to Drive and shared. The workflow
halts at a Wait node until a reviewer submits the approval form. The reviewer may
edit the DOCX in Word and re-upload before approving.

**10) Assembly and delivery.** `/assemble` converts the approved document,
merges it with the signed affidavit and the original source pages into a single
bookmarked PDF with cleared metadata, then delivers via Drive and Gmail.

### Services

| Port | Endpoint | Responsibility |
|------|----------|----------------|
| 8001 | `/detect-pii` | Presidio entity detection (EN + TR) |
| 8002 | `/ocr` | Text extraction with word character spans and pixel boxes |
| 8003 | `/redact` | Pixel-level redaction of source images |
| 8004 | `/anonymize`, `/restore` | Placeholder substitution and reversal |
| 8005 | `/render-body`, `/assemble` | DOCX rendering, PDF assembly |

## The Privacy Chain

The constraint driving this architecture: legal documents exist to record who
someone is. A divorce decree is names, addresses, national identity numbers, and
case numbers — the document is the PII. Sending one to a translation API means
transmitting all of it to a third party.

This pipeline is built so that cannot happen, as a property of the graph rather
than a matter of policy.

**Detection, anonymization, and restoration all run locally.** The translation
model receives text in which every detected entity has been replaced by a typed
placeholder. It never receives a name, an address, or a case number. Restoration
happens on the local machine after the translated text returns.

**The glossary branch is also PII-free.** Terminology extraction runs on the
anonymized translation, before restoration — so the shared glossary in Google
Sheets can never accumulate PII as a side effect.

**What this claim does and does not cover.** The completed translation contains
the original PII by necessity — it is the deliverable. It is uploaded to Google
Drive for review and sent by Gmail on approval. The guarantee is specific and
worth stating precisely: *no personally identifiable information is exposed to
the third-party translation model.* Delivery to the client's own document
infrastructure is a separate trust boundary, governed by the client's existing
data agreements rather than by this pipeline.

**Verified behavior.** Placeholder survival through translation was tested across
both language directions, including entities spanning line breaks: 12/12 tokens
survived intact. Structure recovery succeeded on 5/5 pages of the synthetic
five-page test document.

## Structural Fidelity

The output must be a structure-preserving copy of the source, not its text in reading order. 
A translated decree that loses its headings, tables, and clause numbering is no 
longer usable as a legal instrument, regardless of how accurate the translation is.

| Source - English | Rendered translation - Turkish |
|:---:|:---:|
| <img src="docs/assets/source-en.png" width="420"> | <img src="docs/assets/translated-tr.png" width="420"> |
<p align="center"><em>
Heading hierarchy, section structure, and legal clause numbering are preserved exactly.
The two-column case caption is flattened to reading order at the OCR stage, and
the source page footer appears inline rather than at the page foot — a consequence 
  of the natural-flow design described above. Both are improvements planned for Phase 2.
</em></p>

Translation output is Markdown with a deliberately narrow grammar — only what the
translation stage is instructed to emit. `md_render_v1.py` maps it to Word:

| Markdown | Rendered as |
|----------|-------------|
| `# Heading` | Heading 1 |
| `## Heading` | Heading 2 |
| `\| a \| b \|` with separator row | Real Word table, computed column widths |
| Case caption (pre-heading text, page 1) | Centered caption block |
| Page footer boilerplate | Centered small caption |
| Everything else | Justified body paragraph |

**Clause numbers are plain text.** Legal numbering (`1.1.`, `2.3.`,
`VIII.`) is important to recreate accurately. Rendering it as a Word list would hand
renumbering control to Word, and a decree whose clause numbers silently shift is
a corrupted document. The translation stage is instructed never to emit Markdown
list syntax, so the risk is removed at the source rather than patched downstream.

**Uncertainty is surfaced and flagged.** A table whose rows disagree with its
header still renders, with an amber review flag placed above it. A pipe-delimited
block with no separator row is not treated as a table at all and falls through to
paragraphs. OCR artifacts (fused captions, garbled cells) are reproduced
faithfully rather than cleaned up. Guessing at what a damaged cell *meant* is the
confident-wrong failure mode, and in a certified translation it is worse than
visibly flagging the problem for the reviewer who is there to catch it.

**One design decision was reversed after production.** Earlier builds forced a
hard page break between source pages, so translation page N matched source page N.
In practice that made translation length on any one page load-bearing for layout
on every later page: Turkish and English differ in length, content pushed past its
natural boundary, collided with the next forced break, and stranded sections alone
on near-empty pages. Content now flows naturally. Page-number alignment between
source and translation drifts as a result and the page caption already present in each 
page's text remains the visible marker of where one source page ends and the next begins.

**Raster parity is enforced.** `/ocr` and `/redact` import the same
rasterization module, and `/redact` asserts that image dimensions match those
reported by `/ocr` before drawing any box. A mismatch raises (rather than placing)
redaction boxes at wrong coordinates.

## Human Review Gate

Certification is a legal act. The translator signs a statement that the
translation is complete and accurate, so the pipeline is built in a way that no
document can reach a client without a human having approved it.

**How the gate works.** After rendering, the draft DOCX is uploaded to a Drive
folder and shared with the reviewer, who receives an email with a link. The
workflow then stops at a Wait node configured to resume on form submission. It
does not poll, time out, or proceed on a default. Execution is suspended until a
human submits the form.

**The reviewer edits in Word.** Legal translators already work in Word, with track 
changes, comments, and their own terminology tooling. Asking them to review a legal 
document inside a browser text field would trade their entire working environment for 
a novelty. The reviewer downloads the draft, edits it however they normally would, 
and re-uploads the approved `.docx` through the form. By submitting the form, the
reviewer is also approving the translation of the document.

**The affidavit is static and pre-signed.** It is fetched from Drive as a fixed
template and filled with per-job values. The translator's signature is on the
certificate of accuracy, which under 8 CFR 103.2(b)(3) is what a certified
translation requires. No notary block is included: notarization is a separate,
downstream requirement imposed by particular receiving bodies, not part of
certification itself.

## Verified Results

Every claim below is backed by a committed artifact or by code in this
repository. Where something is designed but not yet measured, it is listed under
Phase 2 instead.

**Glossary additions from AI cannot overwrite verified entries.** Rows marked verified
are unreachable by the workflow's write branch, so reviewer-approved terminology
cannot be silently replaced by a later automated addition.

**Placeholder survival through translation — 12/12 (100%).**
`diagnostics/placeholder_survival_test_v2.py`, output committed at
`diagnostics/results/placeholder_survival_v2.txt`. Eight synthetic cases across
both language directions, stressing Turkish inflectional suffixes, repeated
tokens, placeholder-adjacent-to-placeholder, and entities whose spans cross a
line break. Run against `gemini-2.5-flash`; the model name is recorded in the
output file itself.

**Placeholder format selection.**
`diagnostics/placeholder_survival_test_v1.py` tested four candidate formats
(`[PERSON_1]`, `⟦PERSON_1⟧`, `<<PERSON_1>>`, `XPIIX_PERSON_1`) under the same
stresses before square brackets were adopted. The convention in the shipping
anonymizer is the result of that test.

**Raster parity is enforced in code.** `/ocr` and `/redact` import the same
rasterization module, so both render PDF pages at identical DPI by construction.
`/redact` additionally asserts that the image it receives matches the dimensions
reported by `/ocr` before drawing any box, and raises rather than proceeding on
mismatch. A coordinate-space drift cannot silently place redaction boxes over
the wrong words.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Orchestration | n8n Self-Hosted v2.27.4 (Docker) |
| Services | Python 3.11, FastAPI, Uvicorn |
| OCR | Tesseract 5.5, pytesseract, pdf2image, poppler |
| PII detection | Microsoft Presidio 2.2.363, spaCy 3.8.13 (`en_core_web_lg`) |
| Translation | Gemini 2.5 Flash |
| Imaging | OpenCV, Pillow, NumPy |
| Documents | python-docx, docxtpl, pypdf, LibreOffice |
| Storage & delivery | Google Drive, Google Sheets, Gmail |
| Front end | Lovable |

## Repository Structure

```
Legal Document Translation Workflow.json   n8n workflow export
requirements.txt
SETUP.md                                   installation and run instructions
SETUP_NOTES.md                             design decisions and scoping log

services/
  ocr_service.py         ocr_extract.py          /ocr — 8002
  pii_service.py         pii_detect.py           /detect-pii — 8001
  redact_service.py      redact_core.py          /redact — 8003
  anonymize_service.py   anonymize_core_v2.py    /anonymize, /restore — 8004
                         restore_core_v1.py
  assemble_service.py    assemble_core.py        /render-body, /assemble — 8005
                         md_render_v1.py         Markdown → structure-preserving DOCX
                         affidavit_fill_v1.py    affidavit template fill
                         pdf_merge_v1.py         bookmarked PDF assembly
  rasterize.py                                   shared PDF→pixels (/ocr + /redact)
  scan_service.py        region_scan_v1.py       /scan-regions — see Phase 2

tools/                   synthetic test document generation
diagnostics/             diagnostic scripts and committed results
docs/assets/             architecture diagram and screenshots
```

Every microservice follows the same pattern: a versioned pure core module with no
web framework dependency, plus a thin FastAPI wrapper. Cores are never
overwritten — `anonymize_core_v1.py` remains alongside v2, and the service's
import line is the only thing that moves.

`rasterize.py` is imported by both `/ocr` and `/redact`, so the two render PDF
pages at identical DPI by construction rather than by convention.

## Running It Yourself

**Prerequisites:** WSL2 or Linux · Python 3.11 (conda) · Docker · Tesseract 5.5
with `tesseract-ocr-tur` · poppler-utils · LibreOffice · a Gemini API key ·
Google Drive, Sheets, and Gmail credentials.

Full instructions, including service startup and workflow import, are in
**[SETUP.md](SETUP.md)**.

No test document is included in this repository — generate one with
`tools/python make_synthetic_doc_v2.py`.

## Phase 2

- **Redacted images into the review path.** `/redact` is built and verified;
  wiring its output into a reviewer-facing document is the next step.
- **Visual-element PII.** `/scan-regions` performs pre-OCR region detection to
  catch PII in rotated stamps, seals, and handwriting that OCR cannot read.
  Built, not yet in the active pipeline. This is also a completeness question
  under 8 CFR 103.2(b)(3), which requires translation of everything on the page.
- **Turkish NER.** Presidio currently runs English models against Turkish text —
  over-detecting rather than under-detecting, which errs safe. Full Turkish NER
  needs `tr_core_news_lg`, pinned to spaCy <3.5 against this project's 3.8.13.
  Planned as an isolated microservice on its own spaCy version.
- **Turkish-source end-to-end run.** Both directions are verified at the
  translation stage; full-pipeline verification of a Turkish-source document
  is outstanding.
- **Table fragmentation.** Stray blank lines in model output can split a
  Markdown table so only its first row renders as a Word table.
- **Glossary into the translation prompt.** The glossary is read at the start of
  each run and written at the end; feeding approved terms into the translation
  prompt closes the loop.
- **Turkish OCR accuracy.** Ubuntu's packaged `tesseract-ocr-tur` traineddata is
  older than the 5.5 engine. Impact unmeasured.

## Data Handling

Every document, screenshot, and test result in this repository is synthetic. No 
client-provided document has been used in development, and none will be committed. 
Test PDFs are gitignored.

## How This Was Built

Implementation code in this repository was written with AI assistance (Claude).
The architecture, the scoping decisions, and the diagnostics behind every claim
under Verified Results are mine, as is responsibility for any defect in the
result. Where that judgment shows: anonymizing before any cloud call rather than
relying on a provider agreement, scoping the privacy claim to the translation
model rather than the whole pipeline, and reversing the forced page-break design
after it stranded sections on near-empty pages. Reasoning is logged in
[SETUP_NOTES.md](SETUP_NOTES.md); diagnostic output is committed under `diagnostics/`.

## Author

Jacob Ballard — [GitHub](https://github.com/jballard9909) · [LinkedIn](https://www.linkedin.com/in/jacob-ballard-)
