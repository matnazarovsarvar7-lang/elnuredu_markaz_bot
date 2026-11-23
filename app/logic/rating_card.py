"""Reyting kartasi — ota-ona va o'quvchiga yuboriladigan chiroyli rasm (PNG).

ELNUR EDU brend ranglari: to'q ko'k #0E2A47, oltin #E9A23B, krem #FBF7EF.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

NAVY = (14, 42, 71)
NAVY2 = (24, 62, 102)
GOLD = (233, 162, 59)
CREAM = (251, 247, 239)
INK = (22, 32, 44)
MUTED = (107, 122, 140)
LINE = (230, 224, 212)
GREEN = (38, 150, 100)
RED = (214, 69, 69)
SILVER = (168, 178, 190)
BRONZE = (196, 128, 74)
WHITE = (255, 255, 255)

_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
S = 2                                   # retina: hamma o'lchamlar 2 baravar
W = 540 * S


def _f(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(_BOLD if bold else _REG, size * S)
    except OSError:
        return ImageFont.load_default()


@dataclass
class CardRow:
    rank: int | None
    name: str
    score: float | None
    trend: int | None = None          # o'tgan oyga nisbatan
    me: bool = False
    partial: bool = False


def _level(score: float) -> tuple[str, tuple]:
    if score >= 85:
        return "A'LO", GREEN
    if score >= 70:
        return "YAXSHI", NAVY2
    if score >= 55:
        return "O'RTA", GOLD
    return "E'TIBOR KERAK", RED


def _logo(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    """Brend belgisi: kitob ustida chiqayotgan quyosh va nurlar."""
    for i in range(7):
        a = math.radians(180 + 22.5 + i * 22.5)
        x1, y1 = cx + math.cos(a) * r * 0.62, cy + math.sin(a) * r * 0.62
        x2, y2 = cx + math.cos(a) * r * (1.0 if i == 3 else 0.9), cy + math.sin(a) * r * (1.0 if i == 3 else 0.9)
        d.line([(x1, y1), (x2, y2)], fill=GOLD, width=max(2, r // 9))
    d.pieslice([cx - r * 0.45, cy - r * 0.45, cx + r * 0.45, cy + r * 0.45], 180, 360, fill=GOLD)
    w = r * 1.25
    d.polygon([(cx - 3, cy + r * 0.12), (cx - w, cy + r * 0.02), (cx - w, cy + r * 0.62), (cx - 3, cy + r * 0.72)], fill=CREAM)
    d.polygon([(cx + 3, cy + r * 0.12), (cx + w, cy + r * 0.02), (cx + w, cy + r * 0.62), (cx + 3, cy + r * 0.72)], fill=CREAM)


def _fit(d: ImageDraw.ImageDraw, text: str, font, maxw: int) -> str:
    if d.textlength(text, font=font) <= maxw:
        return text
    while text and d.textlength(text + "…", font=font) > maxw:
        text = text[:-1]
    return text + "…"


def _header(d: ImageDraw.ImageDraw, title: str, sub: str, date_txt: str) -> int:
    h = 118 * S
    d.rectangle([0, 0, W, h], fill=NAVY)
    d.rectangle([0, h - 5 * S, W, h], fill=GOLD)
    _logo(d, 52 * S, 58 * S, 30 * S)
    d.text((100 * S, 26 * S), "REYTING", font=_f(30, True), fill=WHITE)
    d.text((100 * S, 66 * S), _fit(d, title, _f(15, True), 300 * S), font=_f(15, True), fill=GOLD)
    d.text((100 * S, 88 * S), sub, font=_f(11), fill=(190, 204, 220))
    tw = d.textlength(date_txt, font=_f(11))
    d.text((W - 24 * S - tw, 30 * S), date_txt, font=_f(11), fill=(190, 204, 220))
    d.text((W - 24 * S - d.textlength("ELNUR EDU", font=_f(12, True)), 48 * S), "ELNUR EDU",
           font=_f(12, True), fill=WHITE)
    return h


def _ring(d, cx, cy, r, pct, color, label):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=LINE, width=9 * S // 2)
    d.arc([cx - r, cy - r, cx + r, cy + r], -90, -90 + 360 * max(0, min(pct, 100)) / 100, fill=color, width=9 * S // 2)
    t = f"{round(pct)}"
    f = _f(24, True)
    d.text((cx - d.textlength(t, font=f) / 2, cy - 17 * S), t, font=f, fill=INK)
    fl = _f(9)
    d.text((cx - d.textlength(label, font=fl) / 2, cy + 12 * S), label, font=fl, fill=MUTED)


def _bar(d, x, y, w, h, pct, color, bg=LINE):
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=bg)
    if pct > 0:
        d.rounded_rectangle([x, y, x + max(h, w * min(pct, 100) / 100), y + h], radius=h // 2, fill=color)


def render(group: str, date_txt: str, rows: list[CardRow], me: dict | None, avg: int | None,
           pending: list[str]) -> bytes:
    """me: {"name", "rank", "total", "score", "level", "ex", "hw", "trend", "partial"} yoki None."""
    scored = [r for r in rows if r.score is not None]
    row_h = 46 * S
    me_h = 170 * S if me else 0
    pend_h = (40 + 16 * math.ceil(len(pending) / 2)) * S if pending else 0
    h = 118 * S + 18 * S + me_h + (40 * S + len(scored) * row_h if scored else 0) + pend_h + 70 * S
    img = Image.new("RGB", (W, h), CREAM)
    d = ImageDraw.Draw(img)
    y = _header(d, group, "oxirgi 30 kun · nazorat 60% + uy vazifasi 40%", date_txt) + 18 * S
    pad = 20 * S

    if me:                                          # farzand kartasi
        d.rounded_rectangle([pad, y, W - pad, y + me_h - 14 * S], radius=16 * S, fill=WHITE, outline=GOLD, width=2 * S)
        d.text((pad + 18 * S, y + 14 * S), _fit(d, me["name"], _f(15, True), 300 * S), font=_f(15, True), fill=INK)
        if me.get("score") is not None:
            col = _level(me["score"])[1]
            d.text((pad + 18 * S, y + 42 * S), f"{me['rank']}", font=_f(44, True), fill=NAVY)
            rw = d.textlength(f"{me['rank']}", font=_f(44, True))
            d.text((pad + 22 * S + rw, y + 58 * S), f"-o'rin / {me['total']}", font=_f(15, True), fill=MUTED)
            lvl, lc = _level(me["score"])
            fl = _f(10, True)
            bw = d.textlength(lvl, font=fl) + 20 * S
            d.rounded_rectangle([pad + 18 * S, y + 104 * S, pad + 18 * S + bw, y + 124 * S], radius=10 * S, fill=lc)
            d.text((pad + 28 * S, y + 108 * S), lvl, font=fl, fill=WHITE)
            if me.get("trend"):
                t = me["trend"]
                tx = pad + 28 * S + bw
                d.text((tx, y + 106 * S), f"▲ +{t}" if t > 0 else f"▼ {t}",
                       font=_f(11, True), fill=GREEN if t > 0 else RED)
            # o'ng tomon: halqa va ko'rsatkichlar
            _ring(d, W - pad - 70 * S, y + 64 * S, 40 * S, me["score"], col, "ball")
            sx = pad + 190 * S
            for i, (lab, val) in enumerate((("Nazoratlar", me.get("ex")), ("Uy vazifasi", me.get("hw")))):
                yy = y + 50 * S + i * 36 * S
                d.text((sx, yy), lab, font=_f(10), fill=MUTED)
                txt = "—" if val is None else f"{round(val)}%"
                d.text((sx + 150 * S - d.textlength(txt, font=_f(10, True)), yy), txt, font=_f(10, True), fill=INK)
                _bar(d, sx, yy + 16 * S, 150 * S, 7 * S, val or 0, NAVY2)
            if avg is not None:
                d.text((sx, y + 122 * S), f"Guruh o'rtachasi: {avg}", font=_f(10), fill=MUTED)
            if me.get("partial"):
                d.text((pad + 18 * S, y + 132 * S), "dastlabki baho", font=_f(9), fill=MUTED)
        else:
            d.text((pad + 18 * S, y + 50 * S), "Reyting hali shakllanmoqda", font=_f(17, True), fill=NAVY)
            d.text((pad + 18 * S, y + 80 * S), "Birinchi uy vazifasi tekshirilishi yoki nazorat o'tkazilishi bilan",
                   font=_f(10), fill=MUTED)
            d.text((pad + 18 * S, y + 96 * S), "farzandingizning o'rni shu yerda paydo bo'ladi.", font=_f(10), fill=MUTED)
        y += me_h

    if scored:
        d.text((pad, y + 6 * S), "GURUH REYTINGI", font=_f(11, True), fill=MUTED)
        n_txt = f"{len(scored)} o'quvchi"
        d.text((W - pad - d.textlength(n_txt, font=_f(10)), y + 7 * S), n_txt, font=_f(10), fill=MUTED)
        y += 32 * S
        for r in scored:
            if r.me:
                d.rounded_rectangle([pad - 6 * S, y + 2 * S, W - pad + 6 * S, y + row_h - 4 * S], radius=12 * S,
                                    fill=(255, 241, 214), outline=GOLD, width=2 * S)
            cx, cy, cr = pad + 18 * S, y + row_h // 2 - 1 * S, 15 * S
            medal = {1: GOLD, 2: SILVER, 3: BRONZE}.get(r.rank)
            d.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=medal or WHITE, outline=None if medal else LINE,
                      width=2 * S)
            rt = str(r.rank)
            fr = _f(12, True)
            d.text((cx - d.textlength(rt, font=fr) / 2, cy - 8 * S), rt, font=fr, fill=WHITE if medal else INK)
            nx = pad + 44 * S
            d.text((nx, y + 8 * S), _fit(d, r.name, _f(12, r.me), 230 * S), font=_f(12, r.me), fill=INK)
            _bar(d, nx, y + 28 * S, 230 * S, 6 * S, r.score, GOLD if r.me else (NAVY2 if r.rank and r.rank <= 3 else SILVER))
            st = f"{round(r.score)}"
            fs = _f(16, True)
            sx = W - pad - 60 * S
            d.text((sx + 40 * S - d.textlength(st, font=fs), y + 12 * S), st, font=fs, fill=NAVY)
            if r.trend:
                arrow = "▲" if r.trend > 0 else "▼"
                d.text((sx + 44 * S, y + 15 * S), arrow, font=_f(10), fill=GREEN if r.trend > 0 else RED)
            y += row_h

    if pending:
        y += 8 * S
        d.text((pad, y), "MA'LUMOT YIG'ILMOQDA", font=_f(10, True), fill=MUTED)
        y += 20 * S
        colw = (W - 2 * pad) // 2
        for i, nm in enumerate(pending):
            d.text((pad + (i % 2) * colw, y + (i // 2) * 16 * S), "• " + _fit(d, nm, _f(10), colw - 20 * S),
                   font=_f(10), fill=MUTED)
        y += 16 * S * math.ceil(len(pending) / 2) + 6 * S

    fy = h - 46 * S
    d.line([(pad, fy), (W - pad, fy)], fill=LINE, width=S)
    d.text((pad, fy + 12 * S), "Ball: nazoratlar 60% + uy vazifasi 40%  ·  har yakshanba yangilanadi",
           font=_f(9), fill=MUTED)
    tw = d.textlength("elnuredu", font=_f(9, True))
    d.text((W - pad - tw, fy + 12 * S), "elnuredu", font=_f(9, True), fill=NAVY)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
