"""
redact_service.py

FastAPI wrapper exposing redact_core as a local HTTP endpoint for n8n. This is
the LAST service before the cloud boundary: it blacks out PII pixels on a page
image so the source image can travel onward without leaking what /detect-pii
removed from the text.

WHY THE INPUT SHAPE IS DIFFERENT FROM THE OTHER SERVICES
--------------------------------------------------------
/redact needs THREE things that were produced at different points:
    - the original PDF bytes        (to re-rasterize the exact page pixels)
    - that page's OCR word-boxes    (char_span + box, from /ocr)
    - that page's PII spans         (start/end/score, from /detect-pii)
The word-boxes + PII spans arrive already merged into one per-page JSON item
(n8n Merge-by-page_index). So the request is:

    multipart/form-data
        file    : the ORIGINAL PDF bytes (whole document)
        payload : JSON string for THIS page, containing:
                    { "page_index": int,
                      "image_dimensions": {"width":int,"height":int},
                      "words":    [ {box, char_span, ...}, ... ],
                      "entities": [ {start, end, score, entity_type}, ... ] }

The service re-rasterizes ONLY page `page_index` via the SHARED rasterize.py
helper -- the identical DPI/code path /ocr used -- so the pixels it draws on are
byte-identical to the ones the word-boxes were measured against. redact_core
additionally asserts image_dimensions match before drawing (raster-parity
tripwire): a mismatch aborts rather than mis-placing boxes.

WHY RE-RASTERIZE INSTEAD OF CARRYING THE IMAGE THROUGH n8n
    The page image kept getting dropped in n8n's Split Out / Merge plumbing.
    Re-rasterizing from the PDF makes /redact self-contained and guarantees
    raster parity BY CONSTRUCTION (same code, same DPI) rather than hoping the
    right binary survived. The PDF is one small file for the whole document.

PRIVACY POSTURE
    rasterize.py deletes its temp PDF + temp page PNGs in a finally block.
    This service holds page bytes only in memory and returns them; nothing
    PII-bearing is written to disk here.

RESPONSE
    Returns the redacted page as image/png (StreamingResponse), with the
    redaction audit (what was blacked out + why) in response HEADERS so the
    body stays a clean image n8n can pass to PDF assembly. Header:
        X-Redacted-Count : number of word boxes blacked out
        X-Redact-Audit   : JSON list of {box, text, entity_types}   (for logs)

Run locally with:
    uvicorn redact_service:app --port 8003 --reload
"""

import json

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import StreamingResponse
from io import BytesIO

from rasterize import sniff_format, rasterize_pdf_page
from redact_core import redact_page

app = FastAPI(title="ABC Link -- Redact Service")


@app.get("/health")
def health():
    """Liveness check -- confirm the service is up before wiring n8n."""
    return {"status": "ok"}


@app.post("/redact")
async def redact(
    file: UploadFile = File(...),
    payload: str = Form(...),
):
    """
    Black out PII pixels on one page and return the redacted page image.

    file    : original PDF bytes (whole document)
    payload : JSON string for THIS page (page_index, image_dimensions,
              words, entities) -- the merged /ocr + /detect-pii item.
    """
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty file upload.")

    # Parse the per-page payload.
    try:
        page = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=422, detail="'payload' is not valid JSON.")

    # Required fields -- fail loudly rather than redact against a bad payload.
    for key in ("page_index", "words", "entities"):
        if key not in page:
            raise HTTPException(
                status_code=422,
                detail=f"payload missing required field '{key}'.")
    page_index = page["page_index"]
    if not isinstance(page_index, int):
        raise HTTPException(
            status_code=422, detail="payload 'page_index' must be an integer.")

    # This service redacts PDF pages. A bare image upload has no page_index to
    # rasterize by; reject clearly rather than guess.
    kind = sniff_format(contents[:16])
    if kind != "pdf":
        raise HTTPException(
            status_code=400,
            detail="/redact expects the original PDF (multi-page). For a "
                   "single-image document, page_index 0 still requires the "
                   "document to be supplied as a one-page PDF.")

    # Shared rasterizer: the exact page /ocr measured (same DPI, same code).
    # Raises 400 if page_index is out of range for this PDF.
    page_png = rasterize_pdf_page(contents, page_index)

    # redact_core asserts image_dimensions parity (if provided) before drawing.
    # A parity mismatch raises ValueError -- convert it to a clear 422 (rather
    # than letting it surface as an opaque 500) so the n8n execution log shows
    # WHY redaction refused. The abort itself is the safe behavior; this just
    # makes the reason legible.
    try:
        result = redact_page(
            image_bytes=page_png,
            words=page["words"],
            entities=page["entities"],
            expected_dims=page.get("image_dimensions"),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Audit goes in headers so the body is a clean PNG for downstream assembly.
    headers = {
        "X-Redacted-Count": str(result["redacted_count"]),
        "X-Redact-Audit": json.dumps(result["audit"]),
        "X-Page-Index": str(page_index),
    }
    return StreamingResponse(
        BytesIO(result["image_bytes"]),
        media_type="image/png",
        headers=headers,
    )