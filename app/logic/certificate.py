"""Sertifikat (A4 albom, 300 dpi): guilloche fon, ramka, medal/daraja muhri, natija muhri, imzolar, QR tekshiruv.

Bitta chizish — ikki chiqish: Telegram uchun PNG (ko'rinish) va chop etish uchun PDF.
Shriftlar: app/assets/fonts (Cinzel, Great Vibes, Cormorant Garamond — SIL Open Font License).
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"
W, H = 3508, 2480
NAVY = (14, 42, 71)
NAVY2 = (31, 66, 104)
INK = (40, 46, 58)
MUTED = (110, 116, 128)
IVORY = (252, 249, 241)
PALETTE = {
    "gold": {"base": (201, 158, 52), "light": (246, 222, 140), "dark": (135, 98, 22)},
    "silver": {"base": (150, 162, 178), "light": (232, 237, 243), "dark": (88, 99, 115)},
    "bronze": {"base": (186, 116, 58), "light": (238, 182, 128), "dark": (118, 66, 28)},
    "navy": {"base": (40, 84, 140), "light": (150, 186, 228), "dark": (14, 42, 71)},
}


def _font(name: str, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONTS / name), size)
    if weight is not None:
        try:
            f.set_variation_by_axes([weight])
        except (OSError, ValueError):
            pass
    return f


def uz(text: str) -> str:
    """O'zbek tutuq belgilari chiroyli tipografik ko'rinishda: o‘, g‘ — ‘ ; boshqa joylarda — ’."""
    out = []
    for i, ch in enumerate(text):
        if ch in "'`ʻʼ’‘":
            prev = text[i - 1].lower() if i else ""
            out.append("‘" if prev in ("o", "g") else "’")
        else:
            out.append(ch)
    return "".join(out)


@dataclass
class Cert:
    kind: str                    # group_place | month_place | moc_level | moc_place | perfect | star | growth
    name: str
    category: str                # «GURUH NAZORATI · 1-O‘RIN»
    body: str                    # asosiy matn
    praise: str                  # maqtov so'zlari
    tone: str = "gold"           # gold | silver | bronze | navy
    emblem_big: str = "1"        # medal ichidagi katta yozuv: «1», «A+», «100%», «★»
    emblem_small: str = "O‘RIN"
    score_big: str = ""          # o'ng muhr: «96%», «72.4»
    score_small: str = "NATIJA"
    date_txt: str = ""
    number: str = ""
    signers: list[tuple[str, str]] = field(default_factory=list)   # (ism, lavozim)
    verify_url: str = ""
    center: str = "ELNUR EDU"


# ---------------- yordamchi chizmalar ----------------
def _spaced(d: ImageDraw.ImageDraw, cx: float, y: float, text: str, font, fill, spacing: float) -> float:
    widths = [font.getlength(ch) for ch in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = cx - total / 2
    for ch, w in zip(text, widths):
        d.text((x, y), ch, font=font, fill=fill)
        x += w + spacing
    return total


def _wrap(text: str, font, maxw: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if font.getlength(t) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _guilloche(im: Image.Image, tone: dict) -> None:
    """Xavfsizlik naqshi: nozik to'lqinlar va markazdagi rozetka."""
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    col = (*tone["base"], 26)
    for k in range(-6, 42):
        pts = []
        for x in range(0, W + 40, 40):
            y = 140 + k * 62 + 34 * math.sin(x / 210 + k * 0.6) + 16 * math.sin(x / 77 + k)
            pts.append((x, y))
        d.line(pts, fill=col, width=2)
    cx, cy = W / 2, H / 2 + 80
    rc = (*tone["base"], 30)
    for j in range(36):
        a0 = j * math.pi / 18
        pts = []
        for t in range(0, 361, 3):
            t_ = math.radians(t)
            r = 560 + 140 * math.sin(6 * t_ + a0)
            pts.append((cx + r * math.cos(t_ + a0 / 6), cy + r * 0.62 * math.sin(t_ + a0 / 6)))
        d.line(pts, fill=rc, width=2)
    im.alpha_composite(layer)


def _frame(d: ImageDraw.ImageDraw, tone: dict) -> None:
    m = 70
    d.rectangle([m, m, W - m, H - m], outline=NAVY, width=44)
    d.rectangle([m + 62, m + 62, W - m - 62, H - m - 62], outline=tone["base"], width=7)
    d.rectangle([m + 82, m + 82, W - m - 82, H - m - 82], outline=tone["base"], width=2)
    # burchak bezaklari
    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        x0 = m + 62 if sx > 0 else W - m - 62
        y0 = m + 62 if sy > 0 else H - m - 62
        for k, ln in enumerate((210, 150)):
            o = 24 + k * 22
            d.line([(x0 + sx * o, y0 + sy * o), (x0 + sx * (o + ln), y0 + sy * o)], fill=tone["dark"], width=5)
            d.line([(x0 + sx * o, y0 + sy * o), (x0 + sx * o, y0 + sy * (o + ln))], fill=tone["dark"], width=5)
        cx, cy = x0 + sx * 24, y0 + sy * 24
        r = 26
        d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=tone["base"], outline=tone["dark"])
        d.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], fill=IVORY)


def _logo(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, tone: dict) -> None:
    for i in range(7):
        a = math.radians(180 + 22.5 + i * 22.5)
        x1, y1 = cx + math.cos(a) * r * 0.62, cy + math.sin(a) * r * 0.62
        x2, y2 = cx + math.cos(a) * r * (1.0 if i == 3 else 0.9), cy + math.sin(a) * r * (1.0 if i == 3 else 0.9)
        d.line([(x1, y1), (x2, y2)], fill=tone["base"], width=max(3, int(r / 9)))
    d.pieslice([cx - r * 0.45, cy - r * 0.45, cx + r * 0.45, cy + r * 0.45], 180, 360, fill=tone["base"])
    w = r * 1.25
    d.polygon([(cx - 4, cy + r * 0.12), (cx - w, cy + r * 0.02), (cx - w, cy + r * 0.62), (cx - 4, cy + r * 0.72)], fill=NAVY)
    d.polygon([(cx + 4, cy + r * 0.12), (cx + w, cy + r * 0.02), (cx + w, cy + r * 0.62), (cx + 4, cy + r * 0.72)], fill=NAVY)


def _radial(size: int, inner: tuple, outer: tuple) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i in range(size // 2, 0, -2):
        t = i / (size / 2)
        col = tuple(int(inner[k] * (1 - t) + outer[k] * t) for k in range(3))
        c = size / 2
        d.ellipse([c - i, c - i, c + i, c + i], fill=(*col, 255))
    return im


def _star(cx, cy, r1, r2, n=5, rot=-90):
    pts = []
    for i in range(n * 2):
        r = r1 if i % 2 == 0 else r2
        a = math.radians(rot + i * 180 / n)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _leaf(cx: float, cy: float, ang: float, L: float, wd: float) -> list[tuple[float, float]]:
    pts = []
    for k in range(0, 21):
        tt = k / 20
        pts.append(((tt - 0.5) * L, wd / 2 * math.sin(math.pi * tt)))
    for k in range(20, -1, -1):
        tt = k / 20
        pts.append(((tt - 0.5) * L, -wd / 2 * math.sin(math.pi * tt)))
    ca, sa = math.cos(ang), math.sin(ang)
    return [(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in pts]


def _laurel(d: ImageDraw.ImageDraw, cx: float, cy: float, R: float, col: tuple, light: tuple) -> None:
    """Dafna gulchambari: pastdan ikki tomonga egilib chiqadigan shoxlar va juft barglar."""
    for side in (-1, 1):
        a0, a1 = (math.radians(108), math.radians(236)) if side < 0 else (math.radians(72), math.radians(-56))
        n = 10
        d.arc([cx - R, cy - R, cx + R, cy + R], start=math.degrees(min(a0, a1)), end=math.degrees(max(a0, a1)),
              fill=col, width=7)
        for i in range(n):
            a = a0 + (a1 - a0) * (i + 0.5) / n
            px, py = cx + R * math.cos(a), cy + R * math.sin(a)
            tang = a + math.pi / 2 if side < 0 else a - math.pi / 2
            size = 1.0 - 0.35 * i / n
            for off, sgn in ((0.55, 1), (-0.55, -1)):
                ang = tang + off
                lx = px + 30 * size * math.cos(ang)
                ly = py + 30 * size * math.sin(ang)
                d.polygon(_leaf(lx, ly, ang, 78 * size, 30 * size), fill=col if sgn > 0 else light)


def _medal(im: Image.Image, cx: int, cy: int, R: int, tone: dict, big: str, small: str, laurel: bool = False) -> None:
    d = ImageDraw.Draw(im)
    if laurel:
        _laurel(d, cx, cy + 6, R * 1.3, tone["dark"], tone["base"])
    # lentalar
    for sgn in (-1, 1):
        x = cx + sgn * R * 0.42
        poly = [(x - 70, cy + R * 0.55), (x + 70, cy + R * 0.55), (x + 70 + sgn * 40, cy + R * 1.75),
                (x + sgn * 30, cy + R * 1.55), (x - 70 + sgn * 40, cy + R * 1.75)]
        d.polygon(poly, fill=NAVY)
        d.line([(x - 18, cy + R * 0.6), (x - 18 + sgn * 28, cy + R * 1.62)], fill=tone["base"], width=14)
        d.line([(x + 18, cy + R * 0.6), (x + 18 + sgn * 28, cy + R * 1.62)], fill=tone["base"], width=14)
    # tishli rozetka
    shadow = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon(_star(cx + 10, cy + 14, R * 1.04, R * 0.94, n=36), fill=(0, 0, 0, 70))
    im.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(12)))
    d = ImageDraw.Draw(im)
    d.polygon(_star(cx, cy, R * 1.04, R * 0.94, n=36), fill=tone["dark"])
    disc = _radial(int(R * 1.84), tone["light"], tone["base"])
    im.alpha_composite(disc, (int(cx - R * 0.92), int(cy - R * 0.92)))
    d = ImageDraw.Draw(im)
    d.ellipse([cx - R * 0.78, cy - R * 0.78, cx + R * 0.78, cy + R * 0.78], outline=tone["dark"], width=6)
    d.ellipse([cx - R * 0.72, cy - R * 0.72, cx + R * 0.72, cy + R * 0.72], outline=(255, 255, 255, 160), width=3)
    for i in range(24):                                     # mayda yulduzchalar halqasi
        a = math.radians(i * 15)
        d.polygon(_star(cx + R * 0.86 * math.cos(a), cy + R * 0.86 * math.sin(a), 9, 4), fill=tone["dark"])
    if big == "★":
        d.polygon(_star(cx, cy - R * 0.08, R * 0.5, R * 0.21), fill=NAVY)
    else:
        size = int(R * (0.95 if len(big) <= 1 else 0.7 if len(big) <= 2 else 0.5))
        f = _font("Cinzel.ttf", size, 800)
        bb = d.textbbox((0, 0), big, font=f)
        d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], cy - R * 0.12 - (bb[3] - bb[1]) / 2 - bb[1]), big, font=f, fill=NAVY)
    fsz = int(R * 0.15)
    fs = _font("Cinzel.ttf", fsz, 700)
    while sum(fs.getlength(ch) for ch in small) + 6 * len(small) > R * 1.15 and fsz > 18:
        fsz -= 2
        fs = _font("Cinzel.ttf", fsz, 700)
    _spaced(d, cx, cy + R * 0.38, small, fs, NAVY, 6)


def _seal(d: ImageDraw.ImageDraw, cx: int, cy: int, R: int, tone: dict, big: str, small: str) -> None:
    d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=NAVY)
    d.ellipse([cx - R + 16, cy - R + 16, cx + R - 16, cy + R - 16], outline=tone["base"], width=5)
    d.ellipse([cx - R + 34, cy - R + 34, cx + R - 34, cy + R - 34], outline=tone["base"], width=2)
    for i in range(40):
        a = math.radians(i * 9)
        x, y = cx + (R - 25) * math.cos(a), cy + (R - 25) * math.sin(a)
        d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=tone["light"])
    size = int(R * (0.62 if len(big) <= 3 else 0.5 if len(big) <= 4 else 0.4))
    f = _font("Cinzel.ttf", size, 800)
    bb = d.textbbox((0, 0), big, font=f)
    d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], cy - R * 0.12 - (bb[3] - bb[1]) / 2 - bb[1]), big, font=f, fill=tone["light"])
    _spaced(d, cx, cy + R * 0.3, small, _font("Cinzel.ttf", int(R * 0.15), 700), tone["light"], 6)


def _qr(d: ImageDraw.ImageDraw, x: int, y: int, size: int, data: str) -> None:
    from reportlab.graphics.barcode.qr import QrCodeWidget
    w = QrCodeWidget(data)
    w.qr.make()
    n = w.qr.getModuleCount()
    s = size / (n + 2)
    d.rectangle([x, y, x + size, y + size], fill=(255, 255, 255))
    for r, row in enumerate(w.qr.modules):
        for c, v in enumerate(row):
            if v:
                d.rectangle([x + (c + 1) * s, y + (r + 1) * s, x + (c + 2) * s, y + (r + 2) * s], fill=NAVY)


# ---------------- asosiy ----------------
def render(c: Cert) -> Image.Image:
    tone = PALETTE.get(c.tone, PALETTE["gold"])
    im = Image.new("RGBA", (W, H), (*IVORY, 255))
    _guilloche(im, tone)
    d = ImageDraw.Draw(im)
    _frame(d, tone)
    cx = W // 2
    # brend
    _logo(d, cx - 290, 268, 62, tone)
    d.text((cx - 200, 222), c.center, font=_font("Cinzel.ttf", 84, 700), fill=NAVY)
    _spaced(d, cx + 52, 318, "O‘QUV MARKAZI", _font("Cinzel.ttf", 34, 600), tone["dark"], 14)
    # sarlavha
    _spaced(d, cx, 410, "SERTIFIKAT", _font("Cinzel.ttf", 236, 800), NAVY, 30)
    cat = uz(c.category).upper()
    fc = _font("Cinzel.ttf", 62, 700)
    wcat = _spaced(d, cx, 712, cat, fc, tone["dark"], 8)
    for sgn in (-1, 1):
        x0 = cx + sgn * (wcat / 2 + 40)
        d.line([(x0, 752), (x0 + sgn * 260, 752)], fill=tone["base"], width=4)
        d.polygon([(x0 + sgn * 270, 740), (x0 + sgn * 282, 752), (x0 + sgn * 270, 764), (x0 + sgn * 258, 752)],
                  fill=tone["base"])
    # kimga
    fi = _font("Cormorant-Italic.ttf", 66, 500)
    t = "Ushbu sertifikat bilan taqdirlanadi"
    d.text((cx - fi.getlength(t) / 2, 870), t, font=fi, fill=MUTED)
    name = uz(c.name)
    size = 250
    fn = _font("GreatVibes.ttf", size)
    while fn.getlength(name) > 1640 and size > 130:
        size -= 10
        fn = _font("GreatVibes.ttf", size)
    asc = fn.getmetrics()[0]
    tw = fn.getlength(name)
    d.text((cx - tw / 2, 1235 - asc), name, font=fn, fill=NAVY)
    d.line([(cx - 880, 1283), (cx - 40, 1283)], fill=tone["base"], width=4)
    d.line([(cx + 40, 1283), (cx + 880, 1283)], fill=tone["base"], width=4)
    d.polygon([(cx, 1265), (cx + 18, 1283), (cx, 1301), (cx - 18, 1283)], fill=tone["base"])
    # matn
    fb = _font("Cormorant.ttf", 70, 600)
    y = 1350
    for line in _wrap(uz(c.body), fb, 1820):
        d.text((cx - fb.getlength(line) / 2, y), line, font=fb, fill=INK)
        y += 92
    fp = _font("Cormorant-Italic.ttf", 66, 600)
    y += 26
    for line in _wrap(uz(c.praise), fp, 1780):
        d.text((cx - fp.getlength(line) / 2, y), line, font=fp, fill=tone["dark"])
        y += 84
    # medal va natija muhri
    _medal(im, 560, 1350, 250 if c.kind != "month_place" else 232, tone, uz(c.emblem_big),
           uz(c.emblem_small).upper(), laurel=c.kind == "month_place")
    d = ImageDraw.Draw(im)
    if c.score_big:
        _seal(d, W - 540, 1350, 215, tone, c.score_big, uz(c.score_small).upper())
    # imzolar
    fs_n = _font("Cormorant.ttf", 58, 700)
    fs_r = _font("Cinzel.ttf", 34, 600)
    slots = [(740, 2050), (W - 960, 2050)] if len(c.signers) > 1 else [(740, 2050)]
    for (nm, role), (sx, sy) in zip(c.signers[:2], slots):
        d.line([(sx - 300, sy), (sx + 300, sy)], fill=INK, width=3)
        nm = uz(nm)
        d.text((sx - fs_n.getlength(nm) / 2, sy + 14), nm, font=fs_n, fill=INK)
        _spaced(d, sx, sy + 92, uz(role).upper(), fs_r, MUTED, 8)
    # sana va raqam
    fd = _font("Cormorant.ttf", 56, 600)
    dt = uz(c.date_txt)
    d.text((cx - fd.getlength(dt) / 2, 2040), dt, font=fd, fill=INK)
    if c.number:
        fnum = _font("Cinzel.ttf", 30, 600)
        _spaced(d, cx, 2120, f"NO. {c.number}", fnum, MUTED, 6)
    if c.verify_url:
        qx, qy, qs = W - 440, 1925, 240
        _qr(d, qx, qy, qs, c.verify_url)
        ft = _font("Cormorant-Italic.ttf", 32, 600)
        tt = "Haqiqiyligini tekshirish"
        d.text((qx + qs / 2 - ft.getlength(tt) / 2, qy - 44), tt, font=ft, fill=MUTED)
    return im.convert("RGB")


def to_png(im: Image.Image, width: int = 1600) -> bytes:
    out = io.BytesIO()
    im.resize((width, int(width * H / W)), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()


def to_pdf(images: list[Image.Image]) -> bytes:
    out = io.BytesIO()
    first, rest = images[0], images[1:]
    first.save(out, "PDF", resolution=300, save_all=True, append_images=rest, quality=92)
    return out.getvalue()
