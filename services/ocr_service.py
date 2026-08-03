"""
ocr_service.py

FastAPI wrapper exposing the OCR core (ocr_extract.extract) as a local HTTP
endpoint for n8n. Accepts PNG, JPEG, or (multi-page) PDF and returns per-page
OCR results in the SAME uniform envelope shape as scan_service_v2.py, so every
downstream consumer always handles "a list of pages" regardless of input format.

WHY THIS MIRRORS scan_service_v2.py
-----------------------------------
/scan-regions and /ocr must agree on what "a document" is. Both:
    - sniff format by MAGIC BYTES, not filename extension
    - rasterize PDFs locally at the SAME DPI (300) so a given page lands in the
      SAME pixel space in both services -- otherwise region boxes and OCR word
      boxes wouldn't line up and reconciliation would silently break
    - return {page_count, pages: [...]} with per-page page_index +
      image_dimensions, so "regions on page N" pairs with "OCR words on page N"

Format sniffing and PDF rasterization now live in rasterize.py (shared with
/redact, so all services render pixels identically -- see that module). This
service is transport + per-page shaping only; ocr_extract.extract() stays a
pure bytes-in core and is UNCHANGED.

PRIVACY POSTURE
---------------
OCR output is the document's full text and DOES cross into n8n -- expected and
fine (n8n is local; the guarantee comes from /detect-pii + /redact running
after, in order, before any cloud call). PDF rasterization writes full-page,
PII-bearing PNGs to disk transiently; rasterize.py deletes every such artifact
in its own finally block. The image path writes nothing to disk at all.

CONTRACT
--------
Request:
    POST /ocr
    Content-Type: multipart/form-data
        file: <PNG | JPEG | PDF bytes>       (required)
        lang: <str>                          (optional, default "tur+eng")

Response (JSON):
    {
      "page_count": int,
      "pages": [
        {
          "page_index": 0,                    # 0-based; pairs OCR output with
                                              # its page image + the matching
                                              # /scan-regions page object
          "image_dimensions": {"width": int, "height": int},
          "text":  "<full page text, reading order>",
          "lines": [ {line_id, text, box}, ... ],
          "words": [ {text, box, conf, line_id, char_span}, ... ]
        },
        ...
      ]
    }

    A single image upload returns page_count == 1 with one page object at
    page_index 0 -- uniform shape regardless of format.

Errors:
    400 : empty upload, unsupported file type, or PDF with no rasterizable pages
    422 : bytes present but not decodable by the OCR core (UnidentifiedImageError)

Run locally with:
    uvicorn ocr_service:app --port 8002 --reload
"""

from io import BytesIO

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from PIL import Image, UnidentifiedImageError

from ocr_extract import extract, DEFAULT_LANG
from rasterize import sniff_format, rasterize_pdf_all

app = FastAPI(title="ABC Link -- OCR Service (multi-format)")


def _dims(image_bytes: bytes) -> dict:
    """Read page pixel dimensions without disturbing extract()'s own decode."""
    with Image.open(BytesIO(image_bytes)) as im:
        width, height = im.size
    return {"width": width, "height": height}


def _ocr_one_page(image_bytes: bytes, page_index: int, lang: str) -> dict:
    """
    Run extract() on one page's bytes and shape it into a page object carrying
    page_index + image_dimensions, matching the scan_service_v2 page contract.
    """
    result = extract(image_bytes, lang=lang)
    return {
        "page_index": page_index,
        "image_dimensions": _dims(image_bytes),
        "text": result["text"],
        "lines": result["lines"],
        "words": result["words"],
    }


@app.get("/health")
def health():
    """Liveness check -- confirm the service is up before wiring n8n."""
    return {"status": "ok"}


@app.post("/ocr")
async def ocr(
    file: UploadFile = File(...),
    lang: str = Form(DEFAULT_LANG),
):
    """
    Accept a PNG, JPEG, or (multi-page) PDF and return per-page OCR results.

    Flow:
      1. Read bytes, sniff format by magic bytes (shared rasterize.sniff_format).
      2. image -> extract() directly on the uploaded bytes (no disk write).
         pdf   -> rasterize.rasterize_pdf_all -> per-page PNG bytes -> extract()
                  each in order. (Rasterization + its temp-file cleanup live in
                  rasterize.py, shared with /redact.)
      3. Assemble the uniform {page_count, pages: [...]} contract.
    """
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty file upload.")

    kind = sniff_format(contents[:16])
    pages = []

    if kind == "image":
        # extract is bytes-in, so the common case never touches disk.
        try:
            pages.append(_ocr_one_page(contents, page_index=0, lang=lang))
        except UnidentifiedImageError:
            raise HTTPException(
                status_code=422,
                detail="Uploaded 'file' could not be decoded as an image.",
            )

    else:  # kind == "pdf"
        # Shared rasterizer: same DPI/code path /redact uses. It owns its own
        # temp-file cleanup, so no PII-bearing PNG survives the call.
        page_byte_list = rasterize_pdf_all(contents)
        for idx, page_bytes in enumerate(page_byte_list):
            pages.append(_ocr_one_page(page_bytes, page_index=idx, lang=lang))

    return {
        "page_count": len(pages),
        "pages": pages,
    }