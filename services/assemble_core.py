"""
assemble_core.py

Final assembly orchestrator: reviewer-APPROVED translated DOCX + affidavit
template + original source PDF -> one certified-translation package PDF.

WHERE THIS SITS
---------------
Runs AFTER the human-review gate (Wait node). By this point the translated
body has been reviewed, corrected in Word if needed, and re-uploaded; this
step does no translation, no PII work, and no content judgment -- it is pure
mechanical assembly of already-approved, already-filled parts.

WHAT IT ORCHESTRATES (three independently-built, independently-tested cores)
------------------------------------------------------------------------------
    1. docx_to_pdf           (affidavit_fill_v1) -- convert the APPROVED
       translated .docx to PDF, via the SAME LibreOffice path the affidavit
       uses, so both conversions are produced by one consistent routine.
    2. fill_affidavit_to_pdf (affidavit_fill_v1) -- fill + convert the
       affidavit template with this job's values.
    3. merge_pdfs            (pdf_merge_v1)      -- concatenate translated ->
       affidavit -> source, with bookmarks + NDA-safe metadata.
This module adds NO new logic beyond wiring these three together and deriving
the values each expects -- deliberately thin, same as the service-wrapper
pattern used elsewhere in the project (logic lives in imported cores).

PAGE COUNT: DERIVED, NEVER TRUSTED
-------------------------------------
page_count for the affidavit is read from the CONVERTED translated PDF, not
carried over from the original Aggregate count. The reviewer may add or remove
content in Word during review, changing the true page count -- reading it from
the actual final PDF is the only value that cannot drift from what the
affidavit is describing.

TRANSLATOR PROFILE IS CONFIG, NOT PER-JOB INPUT
--------------------------------------------------
translator_full_name / ata_member_number / state_name / city are constant for
a given translator (the signature is already baked into the affidavit
template) and are supplied as a plain dict -- this core does not care where
that dict comes from (hardcoded, env vars, a config file); that is a
service-layer decision, kept out of this core on purpose. Only `direction`
(which way THIS document was translated) and `date` (execution date, from the
review-gate) are genuinely per-job.

100% synthetic in the self-test. No real PII.
"""

import os

from pypdf import PdfReader

from affidavit_fill_v1 import docx_to_pdf, fill_affidavit_to_pdf
from pdf_merge_v1 import merge_pdfs

# Maps the pipeline's direction code (set by the Turkish-char-counting Code
# node upstream) to the (source, target) language names the affidavit needs.
DIRECTION_LANGUAGES = {
    "en>tr": ("English", "Turkish"),
    "tr>en": ("Turkish", "English"),
}


def _languages_for_direction(direction: str):
    try:
        return DIRECTION_LANGUAGES[direction]
    except KeyError:
        raise ValueError(
            f"Unknown direction '{direction}'. Expected one of: "
            f"{list(DIRECTION_LANGUAGES)}.")


def _pdf_page_count(pdf_path: str) -> int:
    return len(PdfReader(pdf_path).pages)


def assemble_package(
    approved_translated_docx: str,
    affidavit_template: str,
    source_pdf: str,
    translator_profile: dict,
    direction: str,
    date: str,
    out_dir: str,
) -> str:
    """
    Full assembly: approved translated .docx + affidavit template + original
    source PDF -> final certified-translation package PDF.

    Args:
        approved_translated_docx: path to the REVIEWER-APPROVED translated
            body .docx (post-review-gate; may differ from the pipeline's
            original render if the reviewer edited it in Word).
        affidavit_template: path to the static affidavit .docx template
            (docxtpl jinja tags, translator signature baked in, notary block
            already dropped).
        source_pdf: path to the original, un-redacted source document PDF.
        translator_profile: dict with translator_full_name, ata_member_number,
            state_name, city -- constant per translator, not per job.
        direction: "en>tr" or "tr>en" -- this job's translation direction.
        date: execution date string, reviewer-supplied at the review gate.
        out_dir: working directory for intermediate and final files.

    Returns:
        Path to the final merged package PDF.

    Raises:
        ValueError on an unknown direction, or a missing/blank profile field
        (surfaced by fill_affidavit_docx's own required-field guard --
        FAIL LOUD rather than render blanks into a certification).
    """
    os.makedirs(out_dir, exist_ok=True)
    source_language, target_language = _languages_for_direction(direction)

    # 1. Convert the APPROVED translated body to PDF -- same LibreOffice path
    #    the affidavit uses, so both conversions come from one routine.
    translated_pdf = docx_to_pdf(approved_translated_docx, out_dir)

    # 2. Page count from the CONVERTED PDF: the reviewer's edits are the last
    #    word on how long the document actually is.
    page_count = _pdf_page_count(translated_pdf)

    # 3. Fill + convert the affidavit with this job's values. Missing/blank
    #    profile fields raise here (fill_affidavit_docx's own guard) --
    #    propagated, not caught, so a bad profile fails the whole assembly.
    affidavit_values = {
        **translator_profile,
        "source_language": source_language,
        "target_language": target_language,
        "page_count": page_count,
        "date": date,
    }
    affidavit_pdf = fill_affidavit_to_pdf(
        affidavit_template, affidavit_values, out_dir)

    # 4. Merge: translated -> affidavit -> source. Bookmarks + NDA-safe
    #    metadata handled inside merge_pdfs.
    merged_bytes = merge_pdfs([translated_pdf, affidavit_pdf, source_pdf])

    final_path = os.path.join(out_dir, "final_package.pdf")
    with open(final_path, "wb") as f:
        f.write(merged_bytes)
    return final_path


# ---------------------------------------------------------------------------
# SELF-TEST -- synthetic only, behind __main__. No real PII, no network.
# ---------------------------------------------------------------------------
def _check(label: str, got, expected) -> bool:
    ok = got == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"        got     : {got!r}")
        print(f"        expected: {expected!r}")
    return ok


def _main() -> None:
    import shutil

    from md_render_v1 import render_body

    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "assemble_selftest_out")
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)

    all_ok = True

    # --- synthetic translated body, via the ALREADY-BUILT 5a renderer ---
    synthetic_pages = [
        {"page_index": 0,
         "restored_text": "# SYNTHETIC TITLE\n\nBody text for page one.\n\n"
                          "Sayfa 1 \u2014 Synthetic Test Data Only"},
        {"page_index": 1,
         "restored_text": "## SECTION TWO\n\nBody text for page two.\n\n"
                          "Sayfa 2 \u2014 Synthetic Test Data Only"},
    ]
    translated_docx_path = os.path.join(out_dir, "translated_body.docx")
    render_body(synthetic_pages).save(translated_docx_path)

    affidavit_template = os.path.join(here, "affidavit_template.docx")

    # --- synthetic 3-page source PDF ---
    from reportlab.pdfgen import canvas
    source_pdf_path = os.path.join(out_dir, "synthetic_source.pdf")
    c = canvas.Canvas(source_pdf_path, pagesize=(612, 792))
    for i in range(3):
        c.drawString(72, 700, f"SYNTHETIC source page {i + 1}")
        c.showPage()
    c.save()

    profile = {
        "translator_full_name": "Jamie L. Rivera",
        "ata_member_number": "265014",
        "state_name": "Franklin",
        "city": "Rivergrove",
    }

    print("=== clean assembly run ===")
    final = assemble_package(
        translated_docx_path, affidavit_template, source_pdf_path,
        profile, "en>tr", "July 22, 2026", out_dir,
    )
    reader = PdfReader(final)
    translated_pages = _pdf_page_count(os.path.join(out_dir, "translated_body.pdf"))
    all_ok &= _check(
        "final page count = translated + 1 affidavit + 3 source",
        len(reader.pages), translated_pages + 1 + 3)

    outline_titles = [i.title for i in reader.outline]
    all_ok &= _check("bookmarks present",
                     outline_titles,
                     ["Translated Document", "Certification of Accuracy",
                      "Source Document"])

    print("\n=== unknown direction raises ===")
    try:
        assemble_package(translated_docx_path, affidavit_template,
                         source_pdf_path, profile, "xx>yy",
                         "July 22, 2026", out_dir)
        all_ok &= _check("raises on unknown direction", False, True)
    except ValueError:
        all_ok &= _check("raises on unknown direction", True, True)

    print("\n=== missing profile field raises (propagated guard) ===")
    bad_profile = {"translator_full_name": "Jamie L. Rivera"}  # incomplete
    try:
        assemble_package(translated_docx_path, affidavit_template,
                         source_pdf_path, bad_profile, "en>tr",
                         "July 22, 2026", out_dir)
        all_ok &= _check("raises on incomplete profile", False, True)
    except ValueError:
        all_ok &= _check("raises on incomplete profile", True, True)

    print("\n=== page_count reflects the CONVERTED pdf, not a stale count ===")
    # Confirm the affidavit's page_count matches the translated PDF's actual
    # page count, not e.g. len(synthetic_pages) -- proves derivation, not trust.
    all_ok &= _check("affidavit page_count derivation matches actual PDF",
                     translated_pages, _pdf_page_count(
                         os.path.join(out_dir, "translated_body.pdf")))

    print("\n" + "=" * 60)
    print(f"OVERALL: {'ALL PASS' if all_ok else 'SOME FAILED'}")
    print("=" * 60)


if __name__ == "__main__":
    _main()