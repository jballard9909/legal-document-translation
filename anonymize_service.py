"""
anonymize_service.py

FastAPI wrapper exposing the anonymization core (anonymize_core_v1.py) as a
local HTTP endpoint for n8n. The TEXT-side complement to /redact: /redact blacks
out PII pixels on the image; /anonymize replaces PII in the TEXT with typed,
numbered placeholders so the text can safely cross to the cloud translator.
Both consume the SAME /detect-pii spans -- one detection, two consumers.

Logic lives in anonymize_core_v1.py and is IMPORTED (single source of truth),
same pattern as redact_service -> redact_core. Importing the core runs nothing
but definitions (its self-test is behind an __main__ guard).

PORT: 8004. (8001 detect-pii, 8002 ocr, 8003 redact are taken.)

WHY THE INPUT MIRRORS /redact (Option 1, locked with Jacob)
-----------------------------------------------------------
By this point in the n8n flow the merged per-page item already carries
page_index + image_dimensions + words + entities together (the same item
/redact consumes). /anonymize accepts that SAME payload and simply reads the
two fields it needs -- `text` and `entities` -- ignoring `words` and
`image_dimensions` (those are the image path's concern). One consistent payload
flows to both services; each takes what it needs.

THE CORRECTNESS CONSTRAINT ON `text`
------------------------------------
/detect-pii returns entity spans as CHARACTER OFFSETS into the text it was
given. Those offsets are only valid against that EXACT string. So `text` here
MUST be byte-identical to what /detect-pii analyzed (the page `text` from the
/ocr envelope). We take `text` DIRECTLY from the payload -- we never re-derive
or re-join it from `words`, which could drift by a space or newline and
invalidate every offset. If `text` is missing we FAIL LOUDLY (422) rather than
anonymize an empty string and silently pass un-anonymized content downstream.

PRIVACY POSTURE
---------------
This service runs locally and returns a `restore_map` that BY DESIGN contains
the real PII values (that is how values are put back after translation). It is
PII-bearing and stays entirely local: response goes back into local n8n, the
map persists locally until restoration, and it NEVER crosses to the cloud. Same
trust-boundary posture as /detect-pii returning matched text and /redact
returning its audit. No disk writes here; the map lives in the response item.

CONTRACT
--------
Request:
    POST /anonymize
    Content-Type: application/json
    {
      "text": "<exact page text /detect-pii analyzed>",   (required)
      "entities": [ {entity_type, start, end, score, ...}, ... ],  (required)
      "image_dimensions": {...},   (ignored; may ride along from the merge)
      "words": [...],              (ignored; may ride along from the merge)
      "page_index": 0              (optional; echoed back for downstream pairing)
    }

    - missing "text"      -> 422 (fail loud; never anonymize nothing)
    - missing "entities"  -> 422
    - empty "entities" [] -> 200, text returned unchanged, empty maps

Response (JSON):
    {
      "anonymized_text": "...",
      "restore_map":   { "[PERSON_1]": "Jordan A. Millbrook", ... },
      "substitutions": [ {span, placeholder, entity_type, value}, ... ],
      "warnings":      [ ...overlap exposures with scores... ],
      "page_index":    0
    }

Run locally with:
    uvicorn anonymize_service:app --port 8004 --reload
"""

from typing import List, Dict, Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from anonymize_core_v1 import anonymize, SCORE_FLOOR

app = FastAPI(title="ABC Link -- Anonymization Service")


class AnonymizeRequest(BaseModel):
    """
    Mirrors the merged per-page item (Option 1). Only `text` and `entities` are
    used; `words`, `image_dimensions`, and any extras are accepted and ignored
    so the same payload can flow to both /anonymize and /redact.

    `text` and `entities` have NO defaults -> FastAPI 422s if either is omitted,
    which is the "missing is an error" behavior we want. An empty entities list
    is allowed and returns the text unchanged.
    """
    text: str = Field(..., description="Exact page text /detect-pii analyzed.")
    entities: List[Dict[str, Any]] = Field(
        ..., description="Detected PII spans (entity_type, start, end, score).")
    page_index: Optional[int] = Field(
        None, description="Pass-through page identifier; echoed back.")

    # Accept-and-ignore fields so /redact's payload validates here too. Declared
    # optional so their presence or absence never causes a validation error.
    words: Optional[List[Dict[str, Any]]] = Field(default=None)
    image_dimensions: Optional[Dict[str, Any]] = Field(default=None)

    model_config = {"extra": "ignore"}  # tolerate any other merge-carried keys


@app.get("/health")
def health():
    """Liveness check -- confirm the service is up before wiring n8n."""
    return {"status": "ok", "score_floor": SCORE_FLOOR}


@app.post("/anonymize")
def anonymize_endpoint(req: AnonymizeRequest):
    """
    Replace PII spans in `text` with typed, numbered placeholders and return the
    anonymized text plus the restore map, substitutions (audit / option-b review
    object), and any overlap-exposure warnings.

    Detect-only upstream: this is where detection RESULTS become anonymized text.
    Runs the full option-(a) path (propose then apply) via the core's anonymize().
    """
    # `text` being an empty string is suspicious for a real page but not
    # inherently invalid; the core handles it (no entities can match empties).
    # We only hard-fail on the field being ABSENT, which pydantic already did.
    result = anonymize(req.text, req.entities)
    return {
        "anonymized_text": result["anonymized_text"],
        "restore_map": result["restore_map"],
        "substitutions": result["substitutions"],
        "warnings": result["warnings"],
        "page_index": req.page_index,
    }