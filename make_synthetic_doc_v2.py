"""
make_synthetic_doc_v2.py

Generates a synthetic English divorce decree PNG (200 DPI) engineered with
OVERLAP STRESS CASES to test whether region_scan_v1.py's dilation step
wrongly fuses stamps/signatures into neighboring text regions.

All content is fabricated. No real PII. Throwaway test data only.

Elements (top to bottom):
  - body_text          : clean control paragraph, no overlap
  - stamp_isolated     : 15deg stamp floating in margin whitespace (v1 replay)
  - stamp_light_overlap: 15deg stamp edge just clipping end of a text line
  - stamp_heavy_overlap: 15deg stamp centered over a paragraph
  - signature          : script-style signature crossing a printed sig line

Outputs:
  - synthetic_doc_v2.png          (the rendered page)
  - synthetic_doc_v2_manifest.json (ground-truth boxes in PIXEL space)

Manifest boxes are in TOP-LEFT pixel coordinates to match region_scan_v1.py
output. ReportLab's bottom-left point origin is converted here.
"""

import json
import math
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from pdf2image import convert_from_bytes
from io import BytesIO

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
DPI = 200
PAGE_W_PT, PAGE_H_PT = letter          # 612 x 792 points (8.5 x 11 in)
PT_TO_PX = DPI / 72.0                   # points -> pixels at target DPI

PNG_OUT = "synthetic_doc_v2.png"
MANIFEST_OUT = "synthetic_doc_v2_manifest.json"

# Manifest accumulates ground-truth element boxes as we draw.
manifest = {
    "dpi": DPI,
    "page_width_px": round(PAGE_W_PT * PT_TO_PX),
    "page_height_px": round(PAGE_H_PT * PT_TO_PX),
    "elements": []
}


def pt_box_to_px(x, y, w, h):
    """
    Convert a ReportLab box (bottom-left origin, points) to a top-left
    pixel box: (left, top, right, bottom). This is the flip that keeps
    the manifest aligned with region_scan_v1.py's coordinate space.
    """
    left = x * PT_TO_PX
    right = (x + w) * PT_TO_PX
    # ReportLab y is measured from the bottom; convert to top-down.
    top = (PAGE_H_PT - (y + h)) * PT_TO_PX
    bottom = (PAGE_H_PT - y) * PT_TO_PX
    return [round(left), round(top), round(right), round(bottom)]


def record(elem_id, elem_type, x, y, w, h, expected_unaccounted):
    manifest["elements"].append({
        "id": elem_id,
        "type": elem_type,
        "box_px": pt_box_to_px(x, y, w, h),
        "expected_unaccounted": expected_unaccounted
    })


def rotated_stamp(c, cx, cy, angle_deg, text_lines, radius=54):
    """
    Draw a circular 'official' stamp centered at (cx, cy) in points,
    rotated by angle_deg. Fabricated text only. Returns the axis-aligned
    bounding box (x, y, w, h) in points that encloses the rotated stamp,
    which is what ground truth records (a distinct object regardless of
    the text it overlaps).
    """
    c.saveState()
    c.translate(cx, cy)
    c.rotate(angle_deg)
    c.setLineWidth(2)
    c.circle(0, 0, radius, stroke=1, fill=0)
    c.circle(0, 0, radius - 8, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 7)
    n = len(text_lines)
    for i, line in enumerate(text_lines):
        ty = (n - 1) * 4.5 - i * 9
        c.drawCentredString(0, ty, line)
    c.restoreState()
    # Axis-aligned bbox of a rotated circle == the circle's bbox,
    # so the rotation doesn't enlarge it; radius is the half-extent.
    return (cx - radius, cy - radius, 2 * radius, 2 * radius)


def draw_paragraph(c, x, y_top, width, lines, leading=14, font="Times-Roman", size=11):
    """
    Draw wrapped lines from the top down. Returns (x, y_bottom, width, height)
    box in points enclosing the drawn text block.
    """
    c.setFont(font, size)
    y = y_top
    for line in lines:
        c.drawString(x, y, line)
        y -= leading
    height = y_top - y + leading
    return (x, y + leading - (leading - size), width, height)


def build():
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)

    # --- Title -------------------------------------------------------
    c.setFont("Times-Bold", 15)
    c.drawCentredString(PAGE_W_PT / 2, 740, "IN THE FAMILY COURT OF WILLOW COUNTY")
    c.setFont("Times-Roman", 11)
    c.drawCentredString(PAGE_W_PT / 2, 724, "DECREE OF DISSOLUTION OF MARRIAGE")
    c.drawCentredString(PAGE_W_PT / 2, 710, "Case No. 24-FAM-88213")

    # --- 1. Clean control paragraph (no overlap) --------------------
    control_lines = [
        "This matter came before the Court upon the petition of the Petitioner,",
        "Dana Marlow, and the Respondent, Kerry Alcott, both having appeared and",
        "the Court being fully advised finds that the marriage is irretrievably broken.",
        "It is therefore ORDERED that the bonds of matrimony are hereby dissolved.",
    ]
    box = draw_paragraph(c, 72, 680, 468, control_lines)
    record("body_control", "body_text", *box, expected_unaccounted=False)

    # --- 2. Isolated stamp in margin whitespace (v1 replay) ---------
    # Placed in open right margin, well away from any text.
    sx, sy, sw, sh = rotated_stamp(
        c, cx=520, cy=610, angle_deg=15,
        text_lines=["WILLOW COUNTY", "FILED", "CLERK OF COURT"]
    )
    record("stamp_isolated", "stamp_isolated", sx, sy, sw, sh,
           expected_unaccounted=True)

    # --- 3. Light-overlap stamp (edge clips end of a text line) -----
    light_lines = [
        "The Court further finds that all matters of property division have been",
        "resolved by the written and duly executed agreement of both parties named,",
    ]
    box = draw_paragraph(c, 72, 560, 468, light_lines)
    record("body_light_context", "body_text", *box, expected_unaccounted=False)
    # Stamp fully on-canvas, left edge clipping the tail of the text lines
    # mid-page -- minimal contact, no page-boundary confound.
    sx, sy, sw, sh = rotated_stamp(
        c, cx=470, cy=548, angle_deg=15,
        text_lines=["NOTARY", "PUBLIC", "SEAL"]
    )
    record("stamp_light_overlap", "stamp_light_overlap", sx, sy, sw, sh,
           expected_unaccounted=True)

    # --- 4. Heavy-overlap stamp (centered over a paragraph) ---------
    heavy_lines = [
        "The Respondent is hereby restored to the former name of record as",
        "requested in open court, and the Clerk is directed to enter this decree",
        "upon the official register of the county without further delay or notice.",
    ]
    box = draw_paragraph(c, 72, 470, 468, heavy_lines)
    record("body_heavy_context", "body_text", *box, expected_unaccounted=False)
    # Stamp centered directly over the middle of the paragraph.
    sx, sy, sw, sh = rotated_stamp(
        c, cx=300, cy=452, angle_deg=15,
        text_lines=["OFFICIAL", "COURT SEAL", "ENTERED"]
    )
    record("stamp_heavy_overlap", "stamp_heavy_overlap", sx, sy, sw, sh,
           expected_unaccounted=True)

    # --- 5. Signature crossing a printed signature line -------------
    # Printed rule + label.
    c.setLineWidth(1)
    c.line(72, 300, 300, 300)
    c.setFont("Times-Roman", 10)
    c.drawString(72, 286, "Signature of Petitioner")
    # Fake handwriting: a script-style flourish drawn as a bezier squiggle
    # sitting ON the line (crossing y=300).
    c.saveState()
    c.setLineWidth(2)
    p = c.beginPath()
    p.moveTo(90, 298)
    p.curveTo(110, 320, 130, 285, 150, 306)
    p.curveTo(170, 322, 190, 288, 215, 304)
    p.curveTo(230, 314, 245, 296, 262, 305)
    c.drawPath(p, stroke=1, fill=0)
    c.restoreState()
    # Ground-truth box around the signature stroke extent (points).
    record("signature", "signature",
           x=88, y=284, w=178, h=40, expected_unaccounted=True)

    c.showPage()
    c.save()
    buf.seek(0)

    # --- Rasterize to PNG at target DPI -----------------------------
    images = convert_from_bytes(buf.read(), dpi=DPI)
    images[0].save(PNG_OUT, "PNG")

    with open(MANIFEST_OUT, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {PNG_OUT} ({images[0].width}x{images[0].height} px)")
    print(f"Wrote {MANIFEST_OUT} with {len(manifest['elements'])} elements:")
    for e in manifest["elements"]:
        flag = "UNACCOUNTED" if e["expected_unaccounted"] else "accounted "
        print(f"   [{flag}] {e['id']:22s} {e['type']:20s} {e['box_px']}")


if __name__ == "__main__":
    build()