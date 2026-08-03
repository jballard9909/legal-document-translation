"""
pdf_merge_v1.py

Final PDF-assembly core: concatenate the translated-body PDF, the filled
affidavit PDF, and the original source-document PDF into one certified
translation package. Pure concatenation only -- no DOCX->PDF conversion here
(that stays in the orchestrator, which produces the two source PDFs and then
calls this). Bytes/paths in, bytes out. No FastAPI, no disk requirement beyond
what the caller supplies.

WHERE THIS SITS
----------------
Last step of assembly, run AFTER the human-review gate has approved the
translated body and the affidavit has been filled (affidavit_fill_v1.py).
pypdf's PdfWriter.append() copies pages losslessly -- no rasterization, so the
translated text stays selectable and the original source pages are untouched
bytes, not re-rendered images.

ORDER (locked with Jacob, matches the project spec's page structure)
----------------------------------------------------------------------
    1. Translated document (the structural-mirror body, Stage 5a)
    2. Certification of Accuracy (the filled affidavit, Stage 5b)
    3. Original source document (the untouched original PDF)
The affidavit's "consisting of N pages" statement refers to the TRANSLATION
(page_count from Aggregate), which is correct under this ordering.

BOOKMARKS
---------
Three top-level outline entries are added, one per section, pointing at the
first page of each part -- so a reviewer can jump between sections in any PDF
viewer without hunting.

METADATA / NDA POSTURE
-----------------------
PDF metadata (author, producer, originating filename) can leak identifying
strings into file properties even when the visible content is clean. Every
merge sets a generic title and clears author/producer/subject fields --
deliberate, not an oversight, given the NDA constraint against exposing
client-identifying information in portfolio artifacts.

FAIL-LOUD
---------
Each source must be a readable, non-empty PDF. A missing, corrupt, or
zero-page input raises immediately rather than silently producing a short or
malformed package -- a dropped section in a legal deliverable is a correctness
failure, not a cosmetic one.

100% synthetic in the self-test. No real PII.
"""

import io
import os
from typing import List, Optional, Union

from pypdf import PdfReader, PdfWriter

SECTION_LABELS = (
    "Translated Document",
    "Certification of Accuracy",
    "Source Document",
)

GENERIC_TITLE = "Certified Translation Package"

PdfSource = Union[str, bytes, io.BytesIO]


def _as_reader(source: PdfSource, label: str) -> PdfReader:
    """Load one source as a PdfReader, failing loudly on anything unreadable
    or empty. Accepts a file path, raw bytes, or a BytesIO stream."""
    try:
        if isinstance(source, (bytes, bytearray)):
            reader = PdfReader(io.BytesIO(source))
        elif isinstance(source, io.BytesIO):
            reader = PdfReader(source)
        elif isinstance(source, str):
            if not os.path.exists(source):
                raise FileNotFoundError(f"'{label}' source not found: {source}")
            reader = PdfReader(source)
        else:
            raise TypeError(
                f"'{label}' source must be a path, bytes, or BytesIO; "
                f"got {type(source)}")
    except FileNotFoundError:
        raise
    except Exception as e:
        raise ValueError(f"'{label}' source is not a readable PDF: {e}") from e

    if len(reader.pages) == 0:
        raise ValueError(f"'{label}' source PDF has zero pages; refusing to "
                         f"merge an empty section.")
    return reader


def merge_pdfs(sources: List[PdfSource],
              labels: Optional[List[str]] = None,
              outline: bool = True,
              title: str = GENERIC_TITLE) -> bytes:
    """
    Concatenate PDFs in order into one package.

    Args:
        sources: ordered list of PDF paths, raw bytes, or BytesIO streams.
                 Default expected order: [translated, affidavit, source].
        labels:  section names for bookmarks, same length as sources. Defaults
                 to SECTION_LABELS when len(sources) == 3; required otherwise.
        outline: add a top-level bookmark per section (default True).
        title:   PDF Title metadata; generic by default (NDA posture -- no
                 client-identifying strings in file properties).

    Returns:
        Merged PDF as bytes.

    Raises:
        ValueError on an empty/unreadable source, or on a labels/sources
        length mismatch. FileNotFoundError if a path source doesn't exist.
    """
    if not sources:
        raise ValueError("merge_pdfs requires at least one source PDF.")

    if labels is None:
        if len(sources) == len(SECTION_LABELS):
            labels = list(SECTION_LABELS)
        else:
            raise ValueError(
                "labels must be supplied explicitly when sources does not "
                f"have exactly {len(SECTION_LABELS)} entries "
                f"(got {len(sources)}).")
    if len(labels) != len(sources):
        raise ValueError(
            f"labels length ({len(labels)}) must match sources length "
            f"({len(sources)}).")

    writer = PdfWriter()
    page_cursor = 0
    for source, label in zip(sources, labels):
        reader = _as_reader(source, label)
        writer.append(reader, import_outline=False)
        if outline:
            writer.add_outline_item(label, page_cursor)
        page_cursor += len(reader.pages)

    # NDA-safe metadata: generic title, cleared identifying fields.
    writer.add_metadata({
        "/Title": title,
        "/Author": "",
        "/Producer": "",
        "/Creator": "",
        "/Subject": "",
    })

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# SELF-TEST -- synthetic PDFs built in-memory, behind __main__. No real PII.
# ---------------------------------------------------------------------------
def _make_synthetic_pdf(text: str, n_pages: int = 1) -> bytes:
    """Tiny synthetic PDF via reportlab, for isolated testing without any
    dependency on the real pipeline's output."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))  # US Letter points
    for i in range(n_pages):
        c.drawString(72, 700, f"{text} (page {i + 1}/{n_pages})")
        c.showPage()
    c.save()
    return buf.getvalue()


def _check(label: str, got, expected) -> bool:
    ok = got == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"        got     : {got!r}")
        print(f"        expected: {expected!r}")
    return ok


def _main() -> None:
    all_ok = True

    print("=== clean 3-part merge ===")
    translated = _make_synthetic_pdf("SYNTHETIC translated body", n_pages=2)
    affidavit = _make_synthetic_pdf("SYNTHETIC affidavit", n_pages=1)
    source = _make_synthetic_pdf("SYNTHETIC source document", n_pages=3)

    merged = merge_pdfs([translated, affidavit, source])
    reader = PdfReader(io.BytesIO(merged))
    all_ok &= _check("total page count = 2+1+3", len(reader.pages), 6)

    outline = reader.outline
    titles = [item.title for item in outline] if outline else []
    all_ok &= _check("bookmarks present with correct labels",
                     titles, list(SECTION_LABELS))

    meta = reader.metadata
    all_ok &= _check("generic title set", meta.title, GENERIC_TITLE)
    all_ok &= _check("author cleared", meta.author or "", "")

    print("\n=== fail-loud: empty source ===")
    empty = io.BytesIO()
    from pypdf import PdfWriter as _W
    _W().write(empty)  # a technically-valid but zero-page PDF
    empty.seek(0)
    try:
        merge_pdfs([translated, empty, source])
        all_ok &= _check("raises on zero-page source", False, True)
    except ValueError:
        all_ok &= _check("raises on zero-page source", True, True)

    print("\n=== fail-loud: missing file path ===")
    try:
        merge_pdfs(["/tmp/does_not_exist_12345.pdf", affidavit, source])
        all_ok &= _check("raises on missing path", False, True)
    except FileNotFoundError:
        all_ok &= _check("raises on missing path", True, True)

    print("\n=== fail-loud: labels/sources length mismatch ===")
    try:
        merge_pdfs([translated, affidavit], labels=["only", "two", "labels"])
        all_ok &= _check("raises on labels mismatch", False, True)
    except ValueError:
        all_ok &= _check("raises on labels mismatch", True, True)

    print("\n=== custom sources count needs explicit labels ===")
    try:
        merge_pdfs([translated, affidavit])  # only 2, no labels given
        all_ok &= _check("raises when count != 3 and no labels given", False, True)
    except ValueError:
        all_ok &= _check("raises when count != 3 and no labels given", True, True)
    # ...but works fine with explicit labels
    two_part = merge_pdfs([translated, affidavit], labels=["Translated", "Affidavit"])
    two_reader = PdfReader(io.BytesIO(two_part))
    all_ok &= _check("2-part merge with explicit labels works",
                     len(two_reader.pages), 3)

    print("\n" + "=" * 60)
    print(f"OVERALL: {'ALL PASS' if all_ok else 'SOME FAILED'}")
    print("=" * 60)


if __name__ == "__main__":
    _main()