"""MOC / imtihon natijasi kartasi (PNG): ism, umumiy ball, daraja, bo'limlar, o'rin."""
from __future__ import annotations

import io

from PIL import Image, ImageDraw

from app.logic.rating_card import (CREAM, GOLD, GREEN, INK, LINE, MUTED, NAVY, RED, S, W, WHITE, _bar, _f, _fit,
                                   _logo)

BLUE = (52, 112, 190)


def _lvl_color(pct: float) -> tuple:
    if pct >= 85:
        return GREEN
    if pct >= 70:
        return BLUE
    if pct >= 55:
        return GOLD
    return RED


def render(title: str, date_txt: str, name: str, total_txt: str, level: str | None, pct: float,
           sections: list[tuple[str, str, float]], rank: int | None, n: int | None, note: str = "") -> bytes:
    """sections: (nomi, «ball / maks», foiz)."""
    hgt = (118 + 210 + 34 + 46 * len(sections) + 70) * S
    im = Image.new("RGB", (W, hgt), CREAM)
    d = ImageDraw.Draw(im)
    h = 118 * S
    d.rectangle([0, 0, W, h], fill=NAVY)
    d.rectangle([0, h - 5 * S, W, h], fill=GOLD)
    _logo(d, 52 * S, 58 * S, 30 * S)
    d.text((100 * S, 24 * S), "NATIJA", font=_f(30, True), fill=WHITE)
    d.text((100 * S, 64 * S), _fit(d, title, _f(15, True), 330 * S), font=_f(15, True), fill=GOLD)
    d.text((100 * S, 88 * S), date_txt, font=_f(11), fill=(190, 204, 220))
    d.text((W - 24 * S - d.textlength("ELNUR EDU", font=_f(12, True)), 30 * S), "ELNUR EDU", font=_f(12, True), fill=WHITE)
    y = h + 18 * S
    # asosiy karta
    d.rounded_rectangle([18 * S, y, W - 18 * S, y + 190 * S], radius=16 * S, fill=WHITE, outline=LINE, width=S)
    d.text((36 * S, y + 18 * S), _fit(d, name, _f(17, True), 300 * S), font=_f(17, True), fill=INK)
    col = _lvl_color(pct)
    d.text((36 * S, y + 52 * S), total_txt, font=_f(40, True), fill=NAVY)
    if level:
        f = _f(15, True)
        tw = d.textlength(level, font=f)
        d.rounded_rectangle([36 * S, y + 112 * S, 36 * S + tw + 28 * S, y + 142 * S], radius=15 * S, fill=col)
        d.text((50 * S, y + 117 * S), level, font=f, fill=WHITE)
    if rank and n:
        t = f"{rank}-o'rin"
        f = _f(26, True)
        d.text((W - 36 * S - d.textlength(t, font=f), y + 50 * S), t, font=f, fill=GOLD if rank <= 3 else INK)
        t2 = f"{n} ta qatnashchi orasida"
        d.text((W - 36 * S - d.textlength(t2, font=_f(10)), y + 88 * S), t2, font=_f(10), fill=MUTED)
    _bar(d, 36 * S, y + 160 * S, W - 72 * S, 12 * S, pct, col)
    d.text((W - 36 * S - d.textlength(f"{pct:.0f}%", font=_f(10, True)), y + 142 * S), f"{pct:.0f}%",
           font=_f(10, True), fill=MUTED)
    y += 210 * S
    d.text((24 * S, y), "BO'LIMLAR", font=_f(11, True), fill=MUTED)
    y += 26 * S
    for nm, val, p in sections:
        d.text((24 * S, y), _fit(d, nm, _f(12, True), 230 * S), font=_f(12, True), fill=INK)
        d.text((W - 24 * S - d.textlength(val, font=_f(12)), y), val, font=_f(12), fill=INK)
        _bar(d, 24 * S, y + 22 * S, W - 48 * S, 9 * S, p, _lvl_color(p))
        y += 46 * S
    if note:
        d.text((24 * S, y + 6 * S), _fit(d, note, _f(9), W - 48 * S), font=_f(9), fill=MUTED)
    d.text((24 * S, hgt - 30 * S), "ELNUR EDU · natijalar bot orqali avtomatik tekshirildi", font=_f(9), fill=MUTED)
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()
