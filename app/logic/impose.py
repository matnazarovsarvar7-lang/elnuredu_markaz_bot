"""Chop etish uchun PDF tayyorlash: buklet joylashuvi, qo'lda ikki tomonlama (old / orqa), nusxalar.

Printerlar (Canon LBP2900, Epson L850) o'zi ikki tomonga chop eta olmaydi, shuning uchun:
  1) «old» fayl chiqadi → admin varaqlarni qayta soladi → 2) «orqa» fayl chiqadi.
Orqa tomonning tartibi (teskari) va burilishi (180°) har printer uchun sinov varag'i bilan aniqlanadi.
"""
from __future__ import annotations

import io

from pypdf import PageObject, PdfReader, PdfWriter, Transformation

A4 = (595.28, 841.89)          # portret (pt)
A4L = (841.89, 595.28)         # albom


def image_to_pdf(data: bytes) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, "PDF", resolution=150)
    return out.getvalue()


def _fit(dst: PageObject, src: PageObject, x0: float, y0: float, w: float, h: float) -> None:
    sw, sh = float(src.mediabox.width), float(src.mediabox.height)
    k = min(w / sw, h / sh)
    tx = x0 + (w - sw * k) / 2 - float(src.mediabox.left) * k
    ty = y0 + (h - sh * k) / 2 - float(src.mediabox.bottom) * k
    dst.merge_transformed_page(src, Transformation().scale(k, k).translate(tx, ty))


def _blank(size) -> PageObject:
    return PageObject.create_blank_page(width=size[0], height=size[1])


def booklet_sides(pages: list[PageObject]) -> tuple[list[PageObject], list[PageObject]]:
    """Buklet: har varaqda 4 bet (A4 albom, yonma-yon 2 ta A5). Qaytaradi: (old tomonlar, orqa tomonlar)."""
    n = len(pages)
    total = (n + 3) // 4 * 4
    get = (lambda i: pages[i - 1] if i <= n else None)
    fronts, backs = [], []
    for s in range(total // 4):
        pairs = [((total - 2 * s), (2 * s + 1)), ((2 * s + 2), (total - 2 * s - 1))]
        for side, (left, right) in zip((fronts, backs), pairs):
            sheet = _blank(A4L)
            for idx, x in ((left, 0), (right, A4L[0] / 2)):
                p = get(idx)
                if p is not None:
                    _fit(sheet, p, x, 0, A4L[0] / 2, A4L[1])
            side.append(sheet)
    return fronts, backs


def duplex_sides(pages: list[PageObject]) -> tuple[list[PageObject], list[PageObject]]:
    """Oddiy A4 ikki tomonlama: toq betlar — old, juft betlar — orqa (oxiri bo'sh bo'lishi mumkin)."""
    fronts = pages[0::2]
    backs = pages[1::2]
    if len(backs) < len(fronts):
        p = fronts[-1]
        backs = backs + [_blank((float(p.mediabox.width), float(p.mediabox.height)))]
    return fronts, backs


def build(data: bytes, layout: str, copies: int, side: str, reverse_back: bool = False,
          rotate_back: bool = False) -> tuple[bytes, int]:
    """side: all | front | back. Qaytaradi: (pdf, sahifalar soni)."""
    src = list(PdfReader(io.BytesIO(data)).pages)
    if layout == "buklet":
        fronts, backs = booklet_sides(src)
    elif layout == "ikki" and len(src) > 1:
        fronts, backs = duplex_sides(src)
    else:
        fronts, backs = src, []
    w = PdfWriter()
    if side == "back":
        seq = backs * copies
        if reverse_back:
            seq = list(reversed(seq))
        for p in seq:
            w.add_page(p)
            if rotate_back:
                w.pages[-1].rotate(180)
    else:
        seq = fronts * copies if side == "front" else (src * copies if layout == "bir" or not backs else
                                                       [x for pr in zip(fronts, backs) for x in pr] * copies)
        for p in seq:
            w.add_page(p)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue(), len(w.pages)


def test_sheets(side: str) -> bytes:
    """Ikki tomon sinovi: 2 varaq. Old: «1-VARAQ OLD ↑», orqa: «1-VARAQ ORQA ↑»."""
    from reportlab.lib.pagesizes import A4 as RA4
    from reportlab.pdfgen import canvas
    from app.logic import sheets as SH
    reg, bold = SH._FONT, SH._BOLD
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=RA4)
    W, H = RA4
    for n in (1, 2):
        c.setFont(bold, 54)
        c.drawCentredString(W / 2, H - 200, f"{n}-VARAQ")
        c.setFont(bold, 40)
        c.drawCentredString(W / 2, H - 270, "OLD TOMON" if side == "front" else "ORQA TOMON")
        c.setFont(bold, 120)
        c.drawCentredString(W / 2, H / 2 - 60, "↑")
        c.setFont(reg, 16)
        c.drawCentredString(W / 2, 120, "Strelka yuqoriga qarasa — to'g'ri. ELNUR EDU printer sinovi")
        c.showPage()
    c.save()
    return out.getvalue()
