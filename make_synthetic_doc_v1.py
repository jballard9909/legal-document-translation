#Generates a SYNTHETIC English legal document (a fake divorce decree) for OCR
#testing. ALL content is fabricated. No real client data is used anywhere.

#Purpose (step 6, first OCR pass):
  #Build a document that deliberately contains the hard elements ABC Link cares
  #about, so we can watch how Tesseract handles each one:
    #- clean printed heading + body   -> high-confidence baseline
    #- a case number (2023/CV/451)    -> ties to the CASE_NUMBER recognizer work
    #- synthetic date / names / address -> Presidio fodder downstream
    #- a small-print footnote          -> watch confidence dip with font size
    #- a rotated bordered "stamp"      -> watch confidence crater

#Pipeline:
  #ReportLab builds a vector PDF  ->  pdf2image rasterizes it to a PNG at 200 DPI.
  #Rasterizing FLATTENS the text layer, so Tesseract must do real OCR on pixels
  #instead of reading an embedded digital text layer. A slight skew is added to
  #mimic a scan.

#Outputs (versioned):
  #synthetic_decree_v1.pdf
  #synthetic_decree_v1.png
  #synthetic_decree_v1_ground_truth.txt   (exact text we placed, for scoring OCR)

import os

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from pdf2image import convert_from_path
from PIL import Image

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
PDF_PATH = os.path.join(OUT_DIR, "synthetic_decree_v1.pdf")
PNG_PATH = os.path.join(OUT_DIR, "synthetic_decree_v1.png")
GROUND_TRUTH_PATH = os.path.join(OUT_DIR, "synthetic_decree_v1_ground_truth.txt")

DPI = 200          # scan-like resolution
SKEW_DEGREES = 0.7 # very slight rotation to mimic an imperfect scan

# ---------------------------------------------------------------------------
# The exact text we place on the page. We keep this as structured data so the
# ground-truth file is generated from the SAME source as the PDF — they can
# never drift apart.
# ---------------------------------------------------------------------------
CONTENT = {
    "heading": "IN THE FAMILY COURT OF THE STATE OF EXAMPLE",
    "subheading": "COUNTY OF SPECIMEN",
    "case_line": "Case No. 2023/CV/451",
    "title": "FINAL DECREE OF DISSOLUTION OF MARRIAGE",
    "body": [
        "This matter came before the Court on the petition of Jane A. Doe,",
        "Petitioner, against John B. Roe, Respondent, for dissolution of the",
        "marriage of the parties. The Court, having reviewed the record and",
        "being fully advised, finds that it has jurisdiction over the parties",
        "and the subject matter of this action.",
        "",
        "IT IS HEREBY ORDERED, ADJUDGED, AND DECREED that the bonds of",
        "matrimony between the Petitioner and the Respondent are dissolved,",
        "and each party is restored to the status of a single person.",
    ],
    "date_line": "Entered this 14th day of March, 2023.",
    "signature_line": "_______________________________",
    "judge_line": "Hon. Pat M. Sample, Presiding Judge",
    "party_address": "Petitioner address of record: 742 Sample Avenue, Specimen City.",
    "footnote": (
        "This is a synthetic document generated for OCR testing only. It "
        "contains no real personal data. Small print is included here to test "
        "how optical character recognition confidence varies with font size."
    ),
    "stamp": ["CERTIFIED", "TRUE COPY", "CLERK OF COURT"],
}


def build_pdf(path: str) -> None:
    """Lay out the synthetic decree as a vector PDF with ReportLab."""
    c = canvas.Canvas(path, pagesize=letter)
    width, height = letter

    # --- Heading block (clean, large -> high-confidence baseline) ---
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(width / 2, height - 1.0 * inch, CONTENT["heading"])
    c.setFont("Helvetica", 12)
    c.drawCentredString(width / 2, height - 1.25 * inch, CONTENT["subheading"])

    # --- Case number (right-aligned, mimics a real filing) ---
    c.setFont("Helvetica", 11)
    c.drawRightString(width - 1.0 * inch, height - 1.7 * inch, CONTENT["case_line"])

    # --- Document title ---
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(width / 2, height - 2.2 * inch, CONTENT["title"])

    # --- Body paragraphs (baseline printed text) ---
    c.setFont("Helvetica", 11)
    y = height - 2.7 * inch
    for line in CONTENT["body"]:
        c.drawString(1.0 * inch, y, line)
        y -= 0.22 * inch

    # --- Date + signature block ---
    y -= 0.2 * inch
    c.drawString(1.0 * inch, y, CONTENT["date_line"])
    y -= 0.5 * inch
    c.drawString(1.0 * inch, y, CONTENT["signature_line"])
    y -= 0.2 * inch
    c.drawString(1.0 * inch, y, CONTENT["judge_line"])

    # --- Party address (a line Presidio should later catch) ---
    y -= 0.4 * inch
    c.setFont("Helvetica", 10)
    c.drawString(1.0 * inch, y, CONTENT["party_address"])

    # --- Small-print footnote (smaller font -> confidence should dip) ---
    c.setFont("Helvetica-Oblique", 7)
    footnote_y = 0.9 * inch
    # naive wrap
    words = CONTENT["footnote"].split()
    line, lines = "", []
    for w in words:
        if len(line) + len(w) + 1 > 95:
            lines.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        lines.append(line)
    for ln in lines:
        c.drawString(1.0 * inch, footnote_y, ln)
        footnote_y -= 0.14 * inch

    # --- Rotated "stamp" (rotation -> OCR should struggle / crater) ---
    c.saveState()
    c.translate(width - 2.2 * inch, 2.2 * inch)
    c.rotate(15)
    c.setLineWidth(1.5)
    c.rect(-0.9 * inch, -0.6 * inch, 1.8 * inch, 1.2 * inch)
    c.setFont("Helvetica-Bold", 11)
    stamp_y = 0.28 * inch
    for word in CONTENT["stamp"]:
        c.drawCentredString(0, stamp_y, word)
        stamp_y -= 0.28 * inch
    c.restoreState()

    c.showPage()
    c.save()


def rasterize(pdf_path: str, png_path: str, dpi: int, skew_deg: float) -> None:
    """Flatten the PDF to a PNG so OCR runs on pixels, not a text layer."""
    pages = convert_from_path(pdf_path, dpi=dpi)
    img = pages[0].convert("RGB")
    if skew_deg:
        # expand=True keeps corners; white fill mimics paper background
        img = img.rotate(skew_deg, expand=True, fillcolor=(255, 255, 255))
    img.save(png_path)


def write_ground_truth(path: str) -> None:
    """Dump the exact text we placed, so OCR output can be scored against it."""
    lines = []
    lines.append(CONTENT["heading"])
    lines.append(CONTENT["subheading"])
    lines.append(CONTENT["case_line"])
    lines.append(CONTENT["title"])
    lines.extend([b for b in CONTENT["body"] if b])
    lines.append(CONTENT["date_line"])
    lines.append(CONTENT["judge_line"])
    lines.append(CONTENT["party_address"])
    lines.append(CONTENT["footnote"])
    lines.append(" ".join(CONTENT["stamp"]))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> None:
    print("Building synthetic PDF ...")
    build_pdf(PDF_PATH)
    print(f"  -> {PDF_PATH}")

    print(f"Rasterizing to PNG at {DPI} DPI (skew {SKEW_DEGREES} deg) ...")
    rasterize(PDF_PATH, PNG_PATH, DPI, SKEW_DEGREES)
    print(f"  -> {PNG_PATH}")

    print("Writing ground-truth text ...")
    write_ground_truth(GROUND_TRUTH_PATH)
    print(f"  -> {GROUND_TRUTH_PATH}")

    print("\nDone. All content is synthetic — no real client data was used.")


if __name__ == "__main__":
    main()