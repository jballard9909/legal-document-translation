"""
pii_service.py

FastAPI wrapper exposing the PII-detection logic (pii_detect.py) as an HTTP
endpoint so n8n can call it.

HOW THIS DIFFERS FROM scan_service.py: /detect-pii is the odd one out among the
four endpoints -- it takes TEXT, not an image. So there is NO file upload, NO
temp file, NO multipart. It is plain JSON in, JSON out. That makes it the
simplest wrapper of the four.

Logic lives in pii_detect.py and is IMPORTED (single source of truth) -- tune a
pattern or context word there and this service picks it up. Importing pii_detect
runs nothing but its definitions (its self-test is behind an __main__ guard).

PORT: run this on 8001, because scan_service.py already uses 8000. Two services,
two ports.

CONTRACT
--------
Request:
    POST /detect-pii
    Content-Type: application/json
    { "text": "...", "language": "en", "page_index": 0 }
        - language   optional, defaults "en"
        - page_index optional, defaults null. A pure pass-through identifier:
          the service does NOTHING with it except echo it back in the response.
          It exists so a caller that fans a multi-page document into per-page
          requests (n8n Split Out) can re-pair each page's PII result with that
          page's OCR word-boxes downstream by MATCHING page_index, rather than
          relying on item ordering. page_index is a real document-structural
          fact (which page this text came from), already present in the /ocr
          envelope -- echoing it keeps that fact attached to the data as it
          crosses the service boundary.

    - empty "text" string           -> 200 with empty entities list
    - missing "text" field entirely -> 422 (FastAPI validation rejects it)

Response (JSON):
    {
      "entities": [
        {"entity_type": "CASE_NUMBER", "start": 45, "end": 55,
         "score": 0.90, "text": "12-34-5678"}
      ],
      "count": 1,
      "language": "en",
      "page_index": 0          # echoed back; null if the caller omitted it
    }

start/end are CHARACTER OFFSETS into the submitted text. They are load-bearing:
/redact will later map these spans -> pixels using the OCR word-boxes. Carried
through exactly as Presidio emits them.

Run locally with:
    uvicorn pii_service:app --reload --port 8001
"""

from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from pii_detect import detect_pii, results_to_dicts, SUPPORTED_LANGUAGES

app = FastAPI(title="ABC Link -- PII Detection Service")


class DetectRequest(BaseModel):
    """
    Request body shape. Because `text` has no default, FastAPI will reject a
    request that OMITS it with a 422 -- exactly the "missing field is an error"
    behavior we want. An empty STRING, by contrast, is allowed and yields an
    empty result (handled in detect_pii).

    page_index is OPTIONAL (defaults None) so existing callers and the direct
    self-tests keep working unchanged -- when it's absent the response simply
    echoes null. It is never used in detection; it is transport plumbing only.
    """
    text: str = Field(..., description="Text to scan for PII (may be empty).")
    language: str = Field("en", description="Language code; defaults to 'en'.")
    page_index: Optional[int] = Field(
        None,
        description="Pass-through page identifier; echoed back for downstream "
                    "re-pairing. Not used in detection.",
    )


@app.get("/health")
def health():
    """Liveness check -- confirm the service is up before wiring n8n."""
    return {"status": "ok", "supported_languages": list(SUPPORTED_LANGUAGES)}


@app.post("/detect-pii")
def detect_pii_endpoint(req: DetectRequest):
    """
    Detect PII spans in the submitted text. Detect-only: returns spans, not
    anonymized text.
    """
    results = detect_pii(req.text, language=req.language)
    entities = results_to_dicts(req.text, results)
    return {
        "entities": entities,
        "count": len(entities),
        "language": req.language,
        "page_index": req.page_index,   # pure echo; null when caller omits it
    }