"""
assemble_service.py

FastAPI wrapper exposing the assembly-phase cores as local HTTP endpoints for
n8n: /render-body (md_render_v1.render_body) and /assemble (assemble_core.
assemble_package). Logic lives in the imported cores and is IMPORTED (single
source of truth, same pattern as every other service in this project) --
importing them runs nothing but definitions (each core's self-test is behind
its own __main__ guard).

WHERE THIS SITS
---------------
Assembly branch, after the human-review gate splits the flow in two:

    Aggregate -> /render-body -> [reviewer edits in Word, re-uploads] ->
    Wait-node review gate -> /assemble -> [deliver]

/render-body runs BEFORE the gate (produces the draft the reviewer edits).
/assemble runs AFTER it (the reviewer's approved DOCX is one of its inputs).

PORT: 8005. (8001 detect-pii, 8002 ocr, 8003 redact, 8004 anonymize/restore
are taken.)

STATELESS BY DESIGN
--------------------
Both endpoints take their file inputs as multipart bytes and never read from
Google Drive or any external store directly -- n8n's Drive/disk nodes fetch
bytes and hand them to this service, the same pattern /redact already uses for
the original PDF. No Drive credentials live in this service.

TRANSLATOR PROFILE IS SERVICE-SIDE CONFIG, NOT A PER-CALL INPUT
-------------------------------------------------------------------
translator_full_name / ata_member_number / state_name / city are constant for
a given translator (the signature is already baked into the affidavit
template) -- see assemble_core.py's docstring. They are defined ONCE below as
TRANSLATOR_PROFILE and are NOT accepted as request fields, so a caller cannot
accidentally vary the translator's identity per job. Edit the dict below (or
swap it for an env-var load) to configure the real translator; nothing else
in this file needs to change.

Run locally with:
    uvicorn assemble_service:app --port 8005 --reload
"""

import json
import os
import shutil
import tempfile
from io import BytesIO
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from assemble_core import assemble_package
from md_render_v2 import render_body

app = FastAPI(title="ABC Link -- Assembly Service")

# --- EDIT THESE VALUES for the real translator; nothing else needs to change.
TRANSLATOR_PROFILE = {
    "translator_full_name": "Jamie L. Rivera",
    "ata_member_number": "265014",
    "state_name": "Franklin",
    "city": "Rivergrove",
}


# ---------------------------------------------------------------------------
# /render-body -- Aggregate's item in, translated-body DOCX out.
# ---------------------------------------------------------------------------
class PageItem(BaseModel):
    """Mirrors one entry of Aggregate Pages' `pages[]`."""
    page_index: int
    restored_text: str
    missing_placeholders: List[str] = Field(default_factory=list)
    unresolved_tokens: List[str] = Field(default_factory=list)


class RenderBodyRequest(BaseModel):
    """Mirrors the Aggregate Pages node's output item exactly, so it can be
    forwarded as this endpoint's body with no reshaping in n8n."""
    page_count: int
    pages: List[PageItem]
    has_integrity_flags: bool = False
    flagged_pages: List[int] = Field(default_factory=list)


@app.post("/render-body")
def render_body_endpoint(req: RenderBodyRequest):
    """
    Render the aggregated, restored pages into the structural-mirror DOCX body
    (Stage 5a). Returns the .docx file; integrity flags ride along in headers
    so n8n can log or surface them without parsing the binary body.
    """
    pages = [p.model_dump() for p in req.pages]
    doc = render_body(pages)

    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)

    headers = {
        "X-Page-Count": str(req.page_count),
        "X-Has-Integrity-Flags": str(req.has_integrity_flags).lower(),
        "X-Flagged-Pages": json.dumps(req.flagged_pages),
        "Content-Disposition": "attachment; filename=translated_body.docx",
    }
    return StreamingResponse(
        buf,
        media_type=(
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        ),
        headers=headers,
    )


# ---------------------------------------------------------------------------
# /assemble -- approved DOCX + affidavit template + source PDF -> package PDF.
# ---------------------------------------------------------------------------
VALID_DIRECTIONS = ("en>tr", "tr>en")


@app.get("/health")
def health():
    """Liveness check -- confirm the service is up before wiring n8n."""
    return {"status": "ok", "translator_configured": bool(
        TRANSLATOR_PROFILE.get("translator_full_name"))}


@app.post("/assemble")
async def assemble_endpoint(
    translated_docx: UploadFile = File(
        ..., description="Reviewer-approved translated body .docx"),
    affidavit_template: UploadFile = File(
        ..., description="Static affidavit .docx template (from Drive)"),
    source_pdf: UploadFile = File(
        ..., description="Original, un-redacted source document PDF"),
    direction: str = Form(
        ..., description="'en>tr' or 'tr>en' -- this job's translation direction"),
    date: str = Form(
        ..., description="Execution date, reviewer-supplied at the review gate"),
):
    """
    Assemble the final certified-translation package: convert the approved
    translated .docx to PDF, fill + convert the affidavit with this job's
    direction/date (translator identity from TRANSLATOR_PROFILE above), then
    merge translated -> affidavit -> source into one bookmarked PDF.

    All three file inputs arrive as multipart bytes (stateless; no Drive
    access from this service). Written to an isolated temp directory that is
    always cleaned up, success or failure.
    """
    if direction not in VALID_DIRECTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"'direction' must be one of {VALID_DIRECTIONS}, got "
                   f"'{direction}'.")

    work_dir = tempfile.mkdtemp(prefix="assemble_")
    try:
        docx_path = os.path.join(work_dir, "translated_body.docx")
        affidavit_path = os.path.join(work_dir, "affidavit_template.docx")
        source_path = os.path.join(work_dir, "source.pdf")

        for upload, path in (
            (translated_docx, docx_path),
            (affidavit_template, affidavit_path),
            (source_pdf, source_path),
        ):
            contents = await upload.read()
            if not contents:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{upload.filename or upload.field_name}' was "
                           f"empty.")
            with open(path, "wb") as f:
                f.write(contents)

        try:
            final_path = assemble_package(
                approved_translated_docx=docx_path,
                affidavit_template=affidavit_path,
                source_pdf=source_path,
                translator_profile=TRANSLATOR_PROFILE,
                direction=direction,
                date=date,
                out_dir=work_dir,
            )
        except ValueError as e:
            # Unknown direction or incomplete translator profile -- surfaced
            # as a clear 422 rather than an opaque 500.
            raise HTTPException(status_code=422, detail=str(e))
        except RuntimeError as e:
            # Infrastructure gap (e.g. LibreOffice missing from PATH in this
            # environment) -- a 500 is correct (it's not the caller's fault),
            # but it must NOT be the bare, detail-free 500 FastAPI's default
            # handler produces. Surfacing the message here is what makes this
            # class of failure diagnosable from n8n's error panel directly,
            # instead of requiring a trip to the uvicorn terminal.
            raise HTTPException(
                status_code=500,
                detail=f"Assembly environment error: {e}")
        except Exception as e:
            # Catch-all: a corrupt/unreadable DOCX or PDF, a docxtpl template
            # error, or anything else not anticipated above. Still a 500 (not
            # the caller's fault), but WITH a message and the failing stage
            # named, rather than silently swallowed. Fail loud, same posture
            # as every other core in this project.
            raise HTTPException(
                status_code=500,
                detail=f"Assembly failed unexpectedly ({type(e).__name__}): {e}")

        with open(final_path, "rb") as f:
            final_bytes = f.read()

    finally:
        # Privacy/hygiene: delete every artifact written, success or failure --
        # same posture as rasterize.py's temp-file cleanup.
        shutil.rmtree(work_dir, ignore_errors=True)

    return StreamingResponse(
        BytesIO(final_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": "attachment; filename=certified_translation_package.pdf",
        },
    )