"""
scan_service.py

FastAPI wrapper that exposes the detect-before-read privacy layer
(region_scan_v1.py) as an HTTP endpoint so n8n can call it.

WHAT THIS CHANGES ABOUT region_scan_v1.py: nothing about the logic. The three
detection functions are IMPORTED, not copied, so there is one source of truth --
tune the dilation kernel in region_scan_v1.py and this service picks it up
automatically. All this file adds is the HTTP plumbing:
    - trigger: an HTTP POST instead of `python region_scan_v1.py`
    - input:   the uploaded image instead of a hardcoded INPUT_PNG path
    - output:  JSON instead of a printed table + saved overlay

PRIVACY POSTURE (unchanged): detection is fully local, no AI, no network. The
uploaded bytes are written to a LOCAL temp file (so the imported functions,
which expect a file path, work untouched), processed, and the temp file is
deleted immediately in a finally block. Nothing leaves the machine.

CONTRACT
--------
Request:
    POST /scan-regions
    Content-Type: multipart/form-data
        file: <image bytes>

Response (JSON):
    {
      "image_dimensions": {"width": int, "height": int},
      "unaccounted_regions": [{"x": int, "y": int, "w": int, "h": int}, ...],
      "accounted_regions":   [{"x": int, "y": int, "w": int, "h": int}, ...],
      "counts": {"accounted": int, "unaccounted": int}
    }

unaccounted_regions is the load-bearing payload: presumptive-PII blackout
candidates that /redact will consume downstream. All boxes are (x, y, w, h) in
pixel space, matching region_scan_v1.py exactly.

Run locally with:
    uvicorn scan_service:app --reload
"""

import os
import tempfile

from fastapi import FastAPI, UploadFile, File, HTTPException
from PIL import Image

# Single source of truth: reuse the proven detection functions untouched.
# region_scan_v1.py's `if __name__ == "__main__"` guard means importing it does
# NOT run its main() -- only the function defs load.
from region_scan_v1 import find_ink_regions, get_ocr_word_boxes, reconcile, LANG

app = FastAPI(title="ABC Link -- Region Scan Service")


def _boxes_to_json(boxes):
    """Convert (x, y, w, h) tuples into the named-object form the contract uses."""
    return [{"x": int(x), "y": int(y), "w": int(w), "h": int(h)}
            for (x, y, w, h) in boxes]


@app.get("/health")
def health():
    """Trivial liveness check -- lets you confirm the service is up before wiring n8n."""
    return {"status": "ok"}


@app.post("/scan-regions")
async def scan_regions(file: UploadFile = File(...)):
    """
    Run the detect-before-read privacy verdict on an uploaded image.

    Mirrors region_scan_v1.main(): detect ink regions (local CV) -> collect OCR
    word-boxes -> reconcile into accounted vs. unaccounted. Returns the verdict
    as JSON. No overlay, no printing.
    """
    # Write the upload to a local temp file so the imported functions -- which
    # take a path and read from disk -- work without modification. Preserve the
    # original extension so cv2/PIL sniff the format correctly.
    suffix = os.path.splitext(file.filename or "")[1] or ".png"
    tmp_path = None
    try:
        contents = await file.read()
        if not contents:
            raise HTTPException(status_code=400, detail="Empty file upload.")

        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(contents)

        # --- identical pipeline to region_scan_v1.main(), sans print/overlay ---
        regions = find_ink_regions(tmp_path)
        word_boxes = get_ocr_word_boxes(tmp_path, LANG)
        accounted, unaccounted = reconcile(regions, word_boxes)

        # Image dimensions travel with the response so every downstream consumer
        # of these pixel-space boxes agrees on the coordinate space they live in.
        with Image.open(tmp_path) as im:
            width, height = im.size

        return {
            "image_dimensions": {"width": width, "height": height},
            "unaccounted_regions": _boxes_to_json(unaccounted),
            "accounted_regions": _boxes_to_json(accounted),
            "counts": {
                "accounted": len(accounted),
                "unaccounted": len(unaccounted),
            },
        }
    finally:
        # Temp file is deleted whether we succeeded or errored -- no PII at rest.
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)