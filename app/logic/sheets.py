"""PDF: o'quvchi savollar varag'i va skanerlanadigan javob varag'i (bir xil o'lchamlar OMR bilan)."""
from __future__ import annotations

import io
import math
import os
from dataclasses import dataclass

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

W, H = A4                     # 595.28 x 841.89 pt
MARK = 24                     # burchak belgisi tomoni
MARK_C = [(40, 40), (W - 40, 40), (40, H - 40), (W - 40, H - 40)]   # markazlar (yuqori-chap koordinata)
ROWS = 20
ROW_H = 24
TOP = 268
COL_W = 165
COL_X0 = 62
BUB_DX = 27
BUB_X0 = 34
BUB_R = 8
QR = (448, 66, 96)            # x, y (yuqori-chap), o'lcham

_FONT = "Helvetica"
_BOLD = "Helvetica-Bold"
for path, name in (("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DV"),
                   ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DVB")):
    if os.path.exists(path):
        pdfmetrics.registerFont(TTFont(name, path))
        if name == "DV":
            _FONT = "DV"
        else:
            _BOLD = "DVB"


def bubble_centers(n: int) -> list[list[tuple[float, float]]]:
    """Har savol uchun A,B,C,D doirachalar markazi (yuqori-chap koordinatada, pt)."""
    cols = max(1, math.ceil(n / ROWS))
    span = cols * COL_W
    x_start = (W - span) / 2 + 10 if cols < 3 else COL_X0
    out = []
    for i in range(n):
        c, r = divmod(i, ROWS)
        y = TOP + r * ROW_H
        x = x_start + c * COL_W + BUB_X0
        out.append([(x + k * BUB_DX, y) for k in range(4)])
    return out


@dataclass
class SheetInfo:
    paper_id: int
    student: str
    group: str
    date: str
    code: str
    variant: str
    n: int
    title: str


def answer_sheets_pdf(items: list[SheetInfo], center: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for it in items:
        _answer_sheet(c, it, center)
        c.showPage()
    c.save()
    return buf.getvalue()


def _answer_sheet(c: canvas.Canvas, it: SheetInfo, center: str) -> None:
    Y = lambda y: H - y       # yuqori-chapdan pastki-chapga
    c.setFillColorRGB(0, 0, 0)
    for x, y in MARK_C:
        c.rect(x - MARK / 2, Y(y) - MARK / 2, MARK, MARK, stroke=0, fill=1)
    c.setFont(_BOLD, 13)
    c.drawString(70, Y(80), f"{center} · JAVOB VARAG'I")
    c.setFont(_FONT, 10.5)
    c.drawString(70, Y(102), it.title[:60])
    c.drawString(70, Y(128), f"O'quvchi: {it.student}")
    c.drawString(70, Y(148), f"Guruh: {it.group}    Sana: {it.date}")
    c.setFont(_BOLD, 11)
    c.drawString(70, Y(170), f"Variant: {it.variant}     Kod: {it.code}")
    c.setFont(_FONT, 8.5)
    c.drawString(70, Y(196), "Har bir savolga BITTA doirachani ruchka bilan TO'LIQ bo'yang.")
    c.drawString(70, Y(209), "Varaqni bukmang, belgilardan tashqariga yozmang.")
    # namuna
    c.drawString(70, Y(230), "To'g'ri:")
    c.circle(115, Y(227), 6, stroke=1, fill=1)
    c.drawString(140, Y(230), "Noto'g'ri:")
    c.circle(192, Y(227), 6, stroke=1, fill=0)
    c.line(186, Y(221), 198, Y(233))
    qr = QrCodeWidget(f"ELN:{it.paper_id}")
    b = qr.getBounds()
    x, y, s = QR
    d = Drawing(s, s, transform=[s / (b[2] - b[0]), 0, 0, s / (b[3] - b[1]), 0, 0])
    d.add(qr)
    renderPDF.draw(d, c, x, Y(y + s))
    for i, row in enumerate(bubble_centers(it.n)):
        x0, y0 = row[0]
        c.setFont(_BOLD, 10)
        c.drawRightString(x0 - 14, Y(y0) - 3.5, f"{i + 1}.")
        for k, (bx, by) in enumerate(row):
            c.setLineWidth(1)
            c.circle(bx, Y(by), BUB_R, stroke=1, fill=0)
            c.setFont(_FONT, 7.5)
            c.drawCentredString(bx, Y(by) - 2.6, "ABCD"[k])


@dataclass
class BookletItem:
    student: str
    group: str
    date: str
    code: str
    variant: str
    title: str
    questions: list[tuple[str, list[str]]]   # (savol, [A,B,C,D] varaqdagi tartibda)


def booklets_pdf(items: list[BookletItem], center: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm, title="Savollar")
    h = ParagraphStyle("h", fontName=_BOLD, fontSize=11.5, leading=15)
    meta = ParagraphStyle("m", fontName=_FONT, fontSize=9.5, leading=13)
    qs = ParagraphStyle("q", fontName=_BOLD, fontSize=10.5, leading=14, spaceBefore=6)
    op = ParagraphStyle("o", fontName=_FONT, fontSize=10, leading=13, leftIndent=14)
    story = []
    for n, it in enumerate(items):
        if n:
            story.append(PageBreak())
        story.append(Paragraph(f"{center} · {it.title}", h))
        t = Table([[Paragraph(f"O'quvchi: <b>{_e(it.student)}</b>", meta),
                    Paragraph(f"Variant: <b>{it.variant}</b> · Kod: <b>{it.code}</b>", meta)],
                   [Paragraph(f"Guruh: {_e(it.group)} · Sana: {it.date}", meta),
                    Paragraph("Javoblarni JAVOB VARAG'IGA belgilang", meta)]], colWidths=[105 * mm, 73 * mm])
        t.setStyle(TableStyle([("LINEBELOW", (0, 1), (-1, 1), 0.8, "#16202c"), ("BOTTOMPADDING", (0, 1), (-1, 1), 6)]))
        story += [t, Spacer(1, 4)]
        for i, (q, opts) in enumerate(it.questions, 1):
            block = [Paragraph(f"{i}. {_e(q)}", qs)]
            block += [Paragraph(f"{x}) {_e(o)}", op) for x, o in zip("ABCD", opts)]
            story.append(KeepTogether(block))
    doc.build(story)
    return buf.getvalue()


def review_pdf(title: str, rows: list[tuple[int, str, str, str, str, list[str], str]]) -> bytes:
    """O'qituvchi ko'rib chiqishi uchun: (id, variant, qiyinlik, manba, savol, javoblar, to'g'ri)."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm, title=title)
    h = ParagraphStyle("h", fontName=_BOLD, fontSize=12, leading=16)
    qs = ParagraphStyle("q", fontName=_BOLD, fontSize=10, leading=13.5, spaceBefore=7)
    op = ParagraphStyle("o", fontName=_FONT, fontSize=9.5, leading=12.5, leftIndent=14)
    ok = ParagraphStyle("ok", parent=op, fontName=_BOLD, textColor="#0f7c74")
    small = ParagraphStyle("s", fontName=_FONT, fontSize=8, leading=10, textColor="#5b6675", leftIndent=14)
    story = [Paragraph(_e(title), h), Spacer(1, 4)]
    for qid, var, lvl, src, q, opts, ans in rows:
        block = [Paragraph(f"#{qid} · {var} · {lvl}", small), Paragraph(_e(q), qs)]
        block += [Paragraph(f"{x}) {_e(o)}" + ("  — to'g'ri javob" if x == ans else ""), ok if x == ans else op)
                  for x, o in zip("ABCD", opts)]
        if src:
            block.append(Paragraph(f"Manba: {_e(src)}", small))
        story.append(KeepTogether(block))
    doc.build(story)
    return buf.getvalue()


def _e(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
