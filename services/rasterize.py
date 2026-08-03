"""
rasterize.py

Single source of truth for turning an uploaded document into page pixels.

WHY THIS EXISTS
---------------
/ocr measures word boxes in the pixel space of a PDF rasterized at DPI=300.
/redact must draw black rectangles on THOSE SAME pixels -- if it rasterized at
a different DPI (or with any different parameter), every box would land in the
wrong place and PII would leak. The only way to guarantee the two services
agree on the pixel space is for them to run the IDENTICAL rasterization code.
So both import from here. Tune the DPI or the poppler call in ONE place and
both services move together; they cannot silently drift.

WHAT'S HERE
-----------
    sniff_format(head)            -> "pdf" | "image"   (magic bytes, not ext)
    rasterize_pdf_all(pdf_bytes)  -> [png_bytes, ...]  (every page, in order)
    rasterize_pdf_page(pdf_bytes, page_index) -> png_bytes  (one page)

rasterize_pdf_page is just rasterize_pdf_all indexed: there is ONE rendering
routine, so page N is byte-identical whether you asked for all pages or one.

RASTERIZATION CONTRACT (must not change without changing it for BOTH services)
    - DPI = 300
    - convert_from_path (PDF written to a temp file first -- matches the
      original /ocr path exactly; kept deliberately over convert_from_bytes so
      the wired /ocr path stays byte-for-byte unchanged)
    - each PIL page saved to a temp PNG and read back to bytes, so the bytes a
      caller draws on are the same bytes that were OCR'd
    - page_index == list position from convert_from_path

PRIVACY POSTURE
    PDF page rasterization writes full-page, PII-bearing PNGs (and the PDF
    itself) to disk transiently. Every function here deletes EVERY temp
    artifact in a finally block, on success or failure. Callers that receive
    the returned bytes are responsible for those bytes; nothing lingers on disk
    inside this module.
"""

import os
import tempfile

from fastapi import HTTPException
from pdf2image import convert_from_path

# Canonical rasterization resolution. This is THE number that must match across
# /scan-regions, /ocr, and /redact. Changing it re-spaces every box everywhere.
DPI = 300

# Magic-byte signatures: identify by leading bytes, never by filename.
_PDF_MAGIC = b"%PDF"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def sniff_format(head: bytes) -> str:
    """
    Decide input type from leading bytes, NOT the filename, so a mislabeled
    upload cannot route a PDF into the raster path or vice versa.

    Returns "pdf" or "image". Raises 400 for anything unsupported.
    """
    if head.startswith(_PDF_MAGIC):
        return "pdf"
    if head.startswith(_PNG_MAGIC) or head.startswith(_JPEG_MAGIC):
        return "image"
    raise HTTPException(
        status_code=400,
        detail="Unsupported file type. Upload a PDF, PNG, or JPEG.",
    )


def rasterize_pdf_all(pdf_bytes: bytes, dpi: int = DPI) -> list:
    """
    Rasterize every page of a PDF to PNG bytes at the canonical DPI, in order.

    Returns a list of PNG byte strings, one per page (index == page_index).
    Raises 400 if the PDF has no rasterizable pages.

    Deletes the temp PDF and every temp page PNG before returning, on success
    or failure.
    """
    temp_paths = []
    try:
        fd, pdf_path = tempfile.mkstemp(suffix=".pdf")
        temp_paths.append(pdf_path)
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(pdf_bytes)

        page_images = convert_from_path(pdf_path, dpi=dpi)
        if not page_images:
            raise HTTPException(
                status_code=400,
                detail="PDF contained no rasterizable pages.",
            )

        out = []
        for pil_page in page_images:
            fd, page_png = tempfile.mkstemp(suffix=".png")
            temp_paths.append(page_png)
            os.close(fd)              # PIL.save reopens by path; just need name
            pil_page.save(page_png)
            with open(page_png, "rb") as f:
                out.append(f.read())
        return out

    finally:
        # Privacy-critical: delete every artifact written, success or failure.
        for p in temp_paths:
            if p and os.path.exists(p):
                os.remove(p)


def rasterize_pdf_page(pdf_bytes: bytes, page_index: int,
                       dpi: int = DPI) -> bytes:
    """
    Rasterize ONE page of a PDF to PNG bytes at the canonical DPI.

    Byte-identical to rasterize_pdf_all(pdf_bytes)[page_index] -- there is a
    single rendering routine, so /redact's page N is exactly the page /ocr
    measured. Raises 400 if page_index is out of range for this PDF.
    """
    pages = rasterize_pdf_all(pdf_bytes, dpi=dpi)
    if page_index < 0 or page_index >= len(pages):
        raise HTTPException(
            status_code=400,
            detail=(f"page_index {page_index} out of range; PDF has "
                    f"{len(pages)} page(s)."),
        )
    return pages[page_index]