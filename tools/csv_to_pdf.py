#!/usr/bin/env python3
"""
Turn a results CSV into a neat, paginated PDF table.

Usage:  python tools/csv_to_pdf.py <input.csv> <output.pdf> "Report Title"

Pure-pip (reportlab) — no system packages needed. Uses DejaVuSans if present
so names with accents/curly quotes render cleanly, else falls back to Helvetica.
"""
import csv
import os
import sys
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape, portrait
from reportlab.lib.units import inch
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                Spacer)

NAVY = colors.HexColor("#1f4e79")
STRIPE = colors.HexColor("#eef3fa")
GRIDC = colors.HexColor("#c8c8c8")

# Use a Unicode font if we can find one (so → “ ” é etc. render).
FONT, FONT_B = "Helvetica", "Helvetica-Bold"
for reg, bold, name, bname in [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVu", "DejaVu-Bold"),
]:
    if os.path.exists(reg) and os.path.exists(bold):
        try:
            pdfmetrics.registerFont(TTFont(name, reg))
            pdfmetrics.registerFont(TTFont(bname, bold))
            FONT, FONT_B = name, bname
        except Exception:
            pass


def esc(t):
    t = (t or "")[:200]                       # never let one cell explode the layout
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return t if t.strip() else " "


def main():
    inp, outp = sys.argv[1], sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else "Report"
    rows = list(csv.reader(open(inp, encoding="utf-8")))
    header = rows[0] if rows else ["(no data)"]
    body = rows[1:]
    ncol = len(header)

    cell = ParagraphStyle("c", fontName=FONT, fontSize=7, leading=8.5)
    hcell = ParagraphStyle("h", fontName=FONT_B, fontSize=7.5, leading=9,
                           textColor=colors.white)
    tstyle = ParagraphStyle("t", fontName=FONT_B, fontSize=15, textColor=NAVY)
    sstyle = ParagraphStyle("s", fontName=FONT, fontSize=9, textColor=colors.grey)

    data = [[Paragraph(esc(h), hcell) for h in header]]
    for r in body:
        r = (r + [""] * ncol)[:ncol]
        data.append([Paragraph(esc(c), cell) for c in r])

    # Narrow tables look better in portrait; wide ones in landscape.
    page = portrait(letter) if ncol <= 4 else landscape(letter)
    avail = page[0] - 48

    # Column widths proportional to the longest text in each column (clamped).
    maxlen = [max([len(header[c])] + [len(rr[c]) for rr in body if c < len(rr)] or [1])
              for c in range(ncol)]
    tot = sum(maxlen) or ncol
    widths = [max(0.5 * inch, avail * m / tot) for m in maxlen]
    scale = avail / sum(widths)
    widths = [w * scale for w in widths]

    tbl = Table(data, colWidths=widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.25, GRIDC),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, STRIPE]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 3.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3.5),
    ]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(FONT, 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawRightString(page[0] - 24, 16, f"Page {doc.page}")
        canvas.drawString(24, 16, title)
        canvas.restoreState()

    doc = SimpleDocTemplate(outp, pagesize=page, leftMargin=24, rightMargin=24,
                            topMargin=34, bottomMargin=28, title=title)
    sub = f"{len(body)} rows · generated {datetime.now(timezone.utc):%Y-%m-%d}"
    doc.build([Paragraph(esc(title), tstyle), Spacer(1, 4),
               Paragraph(sub, sstyle), Spacer(1, 8), tbl],
              onFirstPage=footer, onLaterPages=footer)
    print(f"PDF written: {outp} ({len(body)} rows)")


if __name__ == "__main__":
    main()
