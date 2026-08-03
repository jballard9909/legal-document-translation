"""
affidavit_fill_v1.py

Fill the static Certification-of-Accuracy affidavit template with per-job values
and render it to PDF, ready for pypdf to concatenate into the final package.

WHERE THIS SITS
---------------
Final-assembly stage, AFTER the human-review gate. The affidavit is a fixed Drive
artifact -- translator signature baked in, NOTARY block dropped (notarization, when
a receiving body requires it, is a downstream step outside this pipeline). Only a
few TYPED fields vary per job, so this step does no image insertion: it fills text
via docxtpl and converts to PDF. The Drive master is only ever READ; every run
writes a new filled copy, so nothing is saved back over the template.

WHY docxtpl (not python-docx find-replace)
------------------------------------------
Word often splits a typed placeholder across multiple runs, which breaks naive
string replacement. docxtpl renders jinja {{ tags }} correctly regardless of run
boundaries. The template must therefore carry {{ }} jinja tags, not [FIELD] text.

FIELD SOURCES (for the caller)
------------------------------
  auto-derived : page_count (from Aggregate), source_language / target_language
                 (from the translation direction)
  reviewer     : date (execution date, from the review-gate form)
  translator   : translator_full_name, ata_member_number, state_name, city
                 (profile config -- constant for a given translator)
All are required here; deriving/defaulting them is the caller's job. Missing a
field FAILS LOUDLY rather than rendering a blank into a legal document.

100% synthetic in the self-test. No real PII.
"""

import os
import shutil
import subprocess
import tempfile

from docxtpl import DocxTemplate

REQUIRED_FIELDS = (
    "translator_full_name",
    "ata_member_number",
    "source_language",
    "target_language",
    "page_count",
    "date",
    "state_name",
    "city",
)

# LibreOffice binary: "soffice" on most installs, "libreoffice" on some distros.
_SOFFICE_CANDIDATES = ("soffice", "libreoffice")


def fill_affidavit_docx(template_path: str, values: dict,
                        out_docx_path: str) -> str:
    """Render the docxtpl template with `values` -> filled .docx. Raises on a
    missing required field."""
    missing = [f for f in REQUIRED_FIELDS if f not in values or values[f] in (None, "")]
    if missing:
        raise ValueError(
            f"affidavit fill missing required field(s): {missing}. "
            f"Refusing to render blanks into a certification.")
    doc = DocxTemplate(template_path)
    doc.render(values)
    doc.save(out_docx_path)
    return out_docx_path


def _soffice_bin() -> str:
    for name in _SOFFICE_CANDIDATES:
        if shutil.which(name):
            return name
    raise RuntimeError(
        "LibreOffice not found (looked for: "
        f"{', '.join(_SOFFICE_CANDIDATES)}). Install it for DOCX->PDF.")


def docx_to_pdf(docx_path: str, out_dir: str) -> str:
    """Convert a .docx to PDF via headless LibreOffice. Returns the PDF path.
    Uses an isolated profile dir so concurrent conversions don't collide."""
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(
            [_soffice_bin(), "--headless",
             f"-env:UserInstallation=file://{profile}",
             "--convert-to", "pdf", "--outdir", out_dir, docx_path],
            check=True, capture_output=True,
        )
    pdf_path = os.path.join(
        out_dir,
        os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
    if not os.path.exists(pdf_path):
        raise RuntimeError(f"LibreOffice did not produce {pdf_path}")
    return pdf_path


def fill_affidavit_to_pdf(template_path: str, values: dict,
                          out_dir: str) -> str:
    """Full step: fill template -> .docx -> .pdf. Returns the affidavit PDF path.
    The intermediate .docx is written into out_dir alongside the PDF."""
    os.makedirs(out_dir, exist_ok=True)
    filled_docx = fill_affidavit_docx(
        template_path, values, os.path.join(out_dir, "affidavit_filled.docx"))
    return docx_to_pdf(filled_docx, out_dir)


# ---------------------------------------------------------------------------
# SELF-TEST -- synthetic values only, behind __main__.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    template = os.path.join(here, "affidavit_template.docx")

    # missing-field guard fires
    try:
        fill_affidavit_docx(template, {"date": "x"}, "/tmp/x.docx")
        print("[FAIL] missing-field guard did not fire")
    except ValueError:
        print("[PASS] missing-field guard fires")

    # full synthetic fill (auto-derived + reviewer + profile values)
    values = {
        "translator_full_name": "Jamie L. Rivera",
        "ata_member_number": "265014",
        "source_language": "English",
        "target_language": "Turkish",
        "page_count": 5,                # auto-derived from Aggregate
        "date": "July 22, 2026",        # reviewer-supplied
        "state_name": "Franklin",
        "city": "Rivergrove",
    }
    out = os.path.join(here, "affidavit_out")
    pdf = fill_affidavit_to_pdf(template, values, out)
    print(f"[PASS] filled affidavit rendered -> {pdf}")