"""Universal javob varag'i (MOC / tayyor PDF testlar): 100 tagacha savol, A–F variantlar, yozma savollar uchun
o'qituvchi baho doirachalari, bo'sh varaqlar uchun «Variant» qatori.

QR: «E2:<o'rin id>:<joylashuv>», masalan «E2:57:t4x35.y2x10» yoki bo'sh varaq «E2:58:t4x90.V4».
Geometriya faqat joylashuv yozuvidan hisoblanadi — docs/omr.js dagi geom2() aynan shu formulani takrorlaydi.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.pdfgen import canvas

from app.logic import sheets as SH

W, H, MARK, MARK_C, QR = SH.W, SH.H, SH.MARK, SH.MARK_C, SH.QR
TOP2, BOT2, GX0, GW, LBL = 236.0, 785.0, 52.0, 491.0, 36.0
VAR_Y, VAR_X0, VAR_DX, VAR_R = 204.0, 150.0, 26.0, 7.5
LETTERS = "ABCDEF"


def parse_layout(code: str) -> tuple[list[tuple[str, int]], int]:
    """'t4x35.y2x10.V4' → ([('t',4)]*35 + [('y',2)]*10, variantlar=4)."""
    qs, nv = [], 0
    for part in (code or "").split("."):
        if not part:
            continue
        if part[0] == "V":
            nv = int(part[1:])
            continue
        kind, rest = part[0], part[1:]
        c, n = rest.split("x")
        qs += [(kind, int(c))] * int(n)
    return qs, nv


def geom2(code: str) -> dict:
    """Doirachalar markazlari (pt, yuqori-chap koordinatada), radius va ustunlar."""
    qs, nv = parse_layout(code)
    n = len(qs)
    maxb = max((c for _, c in qs), default=4)
    cmax = 4 if maxb <= 5 else 3
    best = None
    for C in range(1, cmax + 1):
        R = math.ceil(n / C) if n else 1
        rh = min(24.0, (BOT2 - TOP2) / (R - 1)) if R > 1 else 24.0
        dx = min(26.0, (GW / C - 40.0) / maxb)
        r = min(8.0, rh * 0.36, dx * 0.4)
        if best is None or r > best[0] + 1e-9:
            best = (r, C, R, rh, dx)
    r, C, R, rh, dx = best
    colw = min(GW / C, 40.0 + maxb * dx + 30.0)
    x0 = GX0 + (GW - C * colw) / 2.0
    rows = []
    for i, (kind, nb) in enumerate(qs):
        c, k = i // R, i % R
        y = TOP2 + k * rh
        bx = x0 + c * colw + LBL
        rows.append([(bx + j * dx, y) for j in range(nb)])
    var = [(VAR_X0 + j * VAR_DX, VAR_Y) for j in range(nv)]
    return {"rows": rows, "kinds": qs, "r": r, "dx": dx, "nv": nv, "var": var}


@dataclass
class Sheet2:
    seat_id: int
    layout: str               # joylashuv (variant qatori bo'lsa .V<n> bilan)
    title: str
    student: str | None       # None — bo'sh varaq
    group: str
    date: str
    code: str
    variant: str | None
    legend: str = ""          # bo'limlar: «1–10 Ona tili · 11–20 Matematika …»


def _qr(c: canvas.Canvas, text: str) -> None:
    qr = QrCodeWidget(text)
    b = qr.getBounds()
    x, y, s = QR
    d = Drawing(s, s, transform=[s / (b[2] - b[0]), 0, 0, s / (b[3] - b[1]), 0, 0])
    d.add(qr)
    renderPDF.draw(d, c, x, H - (y + s))


def _sheet(c: canvas.Canvas, it: Sheet2, center: str) -> None:
    Y = lambda y: H - y
    F, B = SH._FONT, SH._BOLD
    c.setFillColorRGB(0, 0, 0)
    for x, y in MARK_C:
        c.rect(x - MARK / 2, Y(y) - MARK / 2, MARK, MARK, stroke=0, fill=1)
    c.setFont(B, 12.5)
    c.drawString(70, Y(78), f"{center} · JAVOB VARAG'I")
    c.setFont(F, 10)
    c.drawString(70, Y(98), it.title[:62])
    if it.student:
        c.setFont(B, 11)
        c.drawString(70, Y(120), it.student[:44])
        c.setFont(F, 9.5)
        c.drawString(70, Y(138), f"{it.group}    Sana: {it.date}")
        c.setFont(B, 11)
        c.drawString(70, Y(158), f"Variant: {it.variant or '—'}     Kod: {it.code}")
    else:
        c.setFont(F, 10)
        c.drawString(70, Y(122), "F.I.Sh: ________________________________")
        c.drawString(70, Y(142), "Telefon: ______________________   Kod: " + it.code)
        c.drawString(70, Y(162), f"Sana: {it.date}")
    c.setFont(F, 7.8)
    c.drawString(70, Y(178), "Har savolga BITTA doirachani ruchka bilan to'liq bo'yang. Varaqni bukmang.")
    if it.legend:
        c.drawString(70, Y(190), it.legend[:110])
    _qr(c, f"E2:{it.seat_id}:{it.layout}")
    g = geom2(it.layout)
    if g["nv"]:
        c.setFont(B, 10)
        c.drawString(70, Y(VAR_Y) - 3.5, "Variant:")
        for j, (bx, by) in enumerate(g["var"]):
            c.setLineWidth(1)
            c.circle(bx, Y(by), VAR_R, stroke=1, fill=0)
            c.setFont(F, 8)
            c.drawCentredString(bx, Y(by) - 2.8, LETTERS[j])
    r = g["r"]
    yozma_hint = False
    for i, row in enumerate(g["rows"]):
        kind, nb = g["kinds"][i]
        x0, y0 = row[0]
        c.setFont(B, 9 if r >= 7 else 8)
        c.drawRightString(x0 - r - 4, Y(y0) - 3.2, f"{i + 1}.")
        for j, (bx, by) in enumerate(row):
            c.setLineWidth(0.9)
            if kind == "y":
                yozma_hint = True
            c.circle(bx, Y(by), r, stroke=1, fill=0)
            c.setFont(F, max(6.0, r * 0.95))
            lab = LETTERS[j] if kind == "t" else ("✓" if nb == 1 else str(j + 1))
            c.drawCentredString(bx, Y(by) - r * 0.34, lab)
    if yozma_hint:
        c.setFont(F, 7)
        c.drawString(70, Y(812), "Raqamli doirachalar (1, 2 …) — o'qituvchi uchun: yozma javob balini bo'yaydi, "
                                 "0 ball bo'lsa hech biri bo'yalmaydi.")


def sheets_pdf(items: list[Sheet2], center: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=SH.A4)
    for it in items:
        _sheet(c, it, center)
        c.showPage()
    c.save()
    return buf.getvalue()


@dataclass
class Ticket:
    title: str
    name: str | None
    code: str
    variant: str | None
    date: str
    link: str


def tickets_pdf(items: list[Ticket], center: str) -> bytes:
    """Qatnashchi chiptasi (A4 da 8 ta): natijani Telegramda olish uchun QR havola."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=SH.A4)
    F, B = SH._FONT, SH._BOLD
    cw, ch = W / 2, H / 4
    for i, t in enumerate(items):
        if i and i % 8 == 0:
            c.showPage()
        k = i % 8
        x, y = (k % 2) * cw, H - (k // 2 + 1) * ch
        c.setDash(3, 3)
        c.setLineWidth(0.5)
        c.rect(x + 6, y + 6, cw - 12, ch - 12)
        c.setDash()
        c.setFont(B, 11)
        c.drawString(x + 18, y + ch - 30, center)
        c.setFont(F, 8.5)
        c.drawString(x + 18, y + ch - 46, t.title[:40])
        c.setFont(B, 10.5)
        c.drawString(x + 18, y + ch - 68, (t.name or "F.I.Sh: ______________")[:30])
        c.setFont(F, 9)
        c.drawString(x + 18, y + ch - 86, f"Kod: {t.code}" + (f"   Variant: {t.variant}" if t.variant else ""))
        c.drawString(x + 18, y + ch - 100, f"Sana: {t.date}")
        c.setFont(F, 8)
        c.drawString(x + 18, y + 40, "Natijani Telegramda olish uchun")
        c.drawString(x + 18, y + 28, "QR kodni telefoningiz bilan skanerlang.")
        qr = QrCodeWidget(t.link)
        b = qr.getBounds()
        s = 92
        d = Drawing(s, s, transform=[s / (b[2] - b[0]), 0, 0, s / (b[3] - b[1]), 0, 0])
        d.add(qr)
        renderPDF.draw(d, c, x + cw - s - 16, y + 22)
    c.save()
    return buf.getvalue()
