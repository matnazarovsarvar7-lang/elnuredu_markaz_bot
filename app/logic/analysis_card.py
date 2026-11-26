"""Tahlil kartalari (PNG): auditoriya hisoboti (ustoz/rahbar) va shaxsiy mavzular kartasi (o'quvchi/ota-ona)."""
from __future__ import annotations

import io

from PIL import Image, ImageDraw

from app.logic.itemstats import MIN_ITEM_N, OTHER, Report, strong_weak
from app.logic.rating_card import (CREAM, GOLD, GREEN, INK, LINE, MUTED, NAVY, NAVY2, RED, S, W, WHITE, _f, _fit,
                                   _logo)

AMBER = (224, 138, 46)
PAD = 22 * S
TINT = {"g": (226, 243, 234), "a": (252, 238, 218), "r": (250, 226, 226)}
LVL = {"oson": "Oson", "orta": "O'rta", "qiyin": "Qiyin"}


def zcol(pct: float):
    return GREEN if pct >= 80 else (AMBER if pct >= 50 else RED)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _head(d, title: str, name: str, sub: str, date_txt: str) -> int:
    h = 124 * S
    d.rectangle([0, 0, W, h], fill=NAVY)
    d.rectangle([0, h - 5 * S, W, h], fill=GOLD)
    _logo(d, 52 * S, 60 * S, 30 * S)
    d.text((100 * S, 22 * S), title, font=_f(24, True), fill=WHITE)
    d.text((100 * S, 58 * S), _fit(d, name, _f(15, True), 330 * S), font=_f(15, True), fill=GOLD)
    d.text((100 * S, 84 * S), _fit(d, sub, _f(11), 330 * S), font=_f(11), fill=(190, 204, 220))
    tw = d.textlength(date_txt, font=_f(11))
    d.text((W - 24 * S - tw, 28 * S), date_txt, font=_f(11), fill=(190, 204, 220))
    tw = d.textlength("ELNUR EDU", font=_f(12, True))
    d.text((W - 24 * S - tw, 46 * S), "ELNUR EDU", font=_f(12, True), fill=WHITE)
    return h


def _section(d, y: int, text: str, note: str = "") -> int:
    d.text((PAD, y), text, font=_f(11, True), fill=MUTED)
    if note:
        tw = d.textlength(note, font=_f(9))
        d.text((W - PAD - tw, y + 2 * S), note, font=_f(9), fill=MUTED)
    return y + 24 * S


def _wrap(d, text: str, font, maxw: int) -> list[str]:
    out, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) <= maxw:
            cur = t
        else:
            if cur:
                out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out


def _tile(d, x, y, w, h, value: str, label: str, color) -> None:
    d.rounded_rectangle([x, y, x + w, y + h], radius=14 * S, fill=WHITE, outline=LINE, width=S)
    size = 20
    while size > 11 and d.textlength(value, font=_f(size, True)) > w - 16 * S:
        size -= 1
    f = _f(size, True)
    v = _fit(d, value, f, w - 16 * S)
    d.text((x + w / 2 - d.textlength(v, font=f) / 2, y + 12 * S + (20 - size) * S * 0.6), v, font=f, fill=color)
    l2 = _fit(d, label, _f(9), w - 12 * S)
    d.text((x + w / 2 - d.textlength(l2, font=_f(9)) / 2, y + 46 * S), l2, font=_f(9), fill=MUTED)


def _bar_row(d, y: int, label: str, pct: float, sub: str = "", avg: float | None = None,
             faded: bool = False, trend: int | None = None) -> int:
    lx, lw = PAD, 170 * S
    bx = lx + lw + 10 * S
    bw = W - PAD - bx - 64 * S
    f = _f(12, True)
    d.text((lx, y + 4 * S), _fit(d, label, f, lw), font=f, fill=MUTED if faded else INK)
    if sub:
        d.text((lx, y + 22 * S), _fit(d, sub, _f(9), lw), font=_f(9), fill=MUTED)
    d.rounded_rectangle([bx, y + 9 * S, bx + bw, y + 23 * S], radius=7 * S, fill=LINE)
    c = zcol(pct)
    if faded:
        c = tuple(int(v * 0.45 + 255 * 0.55) for v in c)
    if pct > 0:
        d.rounded_rectangle([bx, y + 9 * S, bx + max(14 * S, bw * pct / 100), y + 23 * S], radius=7 * S, fill=c)
    if avg is not None:
        ax = bx + bw * avg / 100
        d.line([(ax, y + 4 * S), (ax, y + 28 * S)], fill=NAVY, width=2 * S)
    t = f"{round(pct)}%"
    fs = _f(14, True)
    d.text((W - PAD - d.textlength(t, font=fs), y + 5 * S), t, font=fs, fill=zcol(pct) if not faded else MUTED)
    if trend:
        tt = f"▲{trend}" if trend > 0 else f"▼{-trend}"
        d.text((W - PAD - d.textlength(t, font=fs) - d.textlength(tt, font=_f(9, True)) - 6 * S, y + 9 * S), tt,
               font=_f(9, True), fill=GREEN if trend > 0 else RED)
    return y + 38 * S


def _footer(d, h: int, text: str) -> None:
    fy = h - 44 * S
    d.line([(PAD, fy), (W - PAD, fy)], fill=LINE, width=S)
    x = PAD
    for c, lab in ((GREEN, "80%+ yaxshi"), (AMBER, "50–79% o'rta"), (RED, "50% dan past")):
        d.ellipse([x, fy + 15 * S, x + 9 * S, fy + 24 * S], fill=c)
        d.text((x + 13 * S, fy + 12 * S), lab, font=_f(9), fill=MUTED)
        x += d.textlength(lab, font=_f(9)) + 30 * S
    tw = d.textlength(text, font=_f(9, True))
    d.text((W - PAD - tw, fy + 12 * S), text, font=_f(9, True), fill=NAVY)


# ================================ auditoriya ================================
def audience(rep: Report, title: str, sub: str, date_txt: str, max_tiles: int = 96) -> bytes:
    items = [x for x in rep.items if x.n >= MIN_ITEM_N] or rep.items
    if len(items) > max_tiles:              # ko'p savol (bank): eng osonlarning bir qismi + eng qiyinlarning hammasi
        k = max_tiles // 3
        shown = items[:k] + [None] + items[-(max_tiles - k - 1):]
    else:
        shown = list(items)
    cols = 8
    gap = 8 * S
    tw_ = (W - 2 * PAD - (cols - 1) * gap) / cols
    th = 54 * S
    rows = -(-len(shown) // cols)
    topics = [t for t in rep.topics if t.topic != OTHER or len(rep.topics) > 1] if rep.has_topics else []
    levels = rep.levels if len(rep.levels) > 1 else []
    attn = [x for x in rep.items if x.suspicious] + [x for x in rep.items if not x.suspicious and x.p < 0.4
                                                     and x.n >= MIN_ITEM_N][::-1]
    attn = attn[:8]
    h = (124 + 18 + 84 + 18) * S
    h += (24 * S + len(topics) * 38 * S + 14 * S) if topics else 0
    h += (24 * S + len(levels) * 38 * S + 14 * S) if levels else 0
    h += 24 * S + rows * (th + gap) + 14 * S
    h += (24 * S + len(attn) * 50 * S + 10 * S) if attn else 0
    h += 60 * S
    img = Image.new("RGB", (W, int(h)), CREAM)
    d = ImageDraw.Draw(img)
    y = _head(d, "SAVOLLAR TAHLILI", title, sub, date_txt) + 18 * S

    # --- umumiy ko'rsatkichlar
    tw4 = (W - 2 * PAD - 3 * 10 * S) / 4
    if topics:
        rel = [t for t in topics if t.reliable and t.topic != OTHER] or topics
        best, worst = rel[0], rel[-1]
        tiles = [(f"{rep.mean:g}%", "O'rtacha natija", zcol(rep.mean)), (str(rep.n), "Qatnashchi", NAVY),
                 (best.topic, f"Eng kuchli · {round(best.pct)}%", GREEN),
                 (worst.topic, f"Eng zaif · {round(worst.pct)}%", RED if worst.pct < 50 else AMBER)]
    else:
        easy = sum(1 for x in items if x.p >= 0.8)
        hard = sum(1 for x in items if x.p < 0.5)
        tiles = [(f"{rep.mean:g}%", "O'rtacha natija", zcol(rep.mean)), (str(rep.n), "Qatnashchi", NAVY),
                 (str(easy), "Yaxshi ishlangan savol", GREEN), (str(hard), "Qiyin bo'lgan savol", RED)]
    for i, (v, lab, c) in enumerate(tiles):
        _tile(d, PAD + i * (tw4 + 10 * S), y, tw4, 72 * S, v, lab, c)
    y += 84 * S + 18 * S

    if topics:
        y = _section(d, y, "MAVZULAR BO'YICHA", "* — savol kam, xulosa taxminiy")
        for t in topics:
            y = _bar_row(d, y, t.topic + ("" if t.reliable else " *"), t.pct, f"{t.n_q} ta savol",
                         faded=not t.reliable)
        y += 14 * S
    if levels:
        y = _section(d, y, "QIYINLIK DARAJASI BO'YICHA")
        for t in levels:
            y = _bar_row(d, y, LVL.get(t.topic, t.topic), t.pct, f"{t.n_q} ta savol")
        y += 14 * S

    note = (f"ko'rsatilgan: {len(shown) - 1} / {len(items)}" if None in shown else f"{len(items)} ta savol")
    if any(x and x.suspicious for x in shown):
        note = "ramkali — kalitni tekshiring · " + note
    y = _section(d, y, "SAVOLLAR · eng ko'p ishlanganidan eng kamigacha", note)
    for i, x in enumerate(shown):
        r, c = divmod(i, cols)
        tx, ty = PAD + c * (tw_ + gap), y + r * (th + gap)
        if x is None:
            d.rounded_rectangle([tx, ty, tx + tw_, ty + th], radius=10 * S, fill=LINE)
            d.text((tx + tw_ / 2 - d.textlength("…", font=_f(18, True)) / 2, ty + 14 * S), "…", font=_f(18, True),
                   fill=MUTED)
            continue
        col = zcol(100 * x.p)
        d.rounded_rectangle([tx, ty, tx + tw_, ty + th], radius=10 * S, fill=col)
        if x.suspicious:
            d.rounded_rectangle([tx, ty, tx + tw_, ty + th], radius=10 * S, outline=NAVY, width=3 * S)
        lab = f"№{x.label}" if not str(x.label).startswith("#") else str(x.label)
        d.text((tx + 8 * S, ty + 6 * S), _fit(d, lab, _f(9, True), tw_ - 12 * S), font=_f(9, True), fill=WHITE)
        v = f"{round(100 * x.p)}%"
        fv = _f(15, True)
        d.text((tx + tw_ / 2 - d.textlength(v, font=fv) / 2, ty + 24 * S), v, font=fv, fill=WHITE)
    y += rows * (th + gap) + 14 * S

    if attn:
        y = _section(d, y, "E'TIBOR TALAB SAVOLLAR")
        for x in attn:
            d.rounded_rectangle([PAD, y, W - PAD, y + 42 * S], radius=10 * S,
                                fill=TINT["r"] if x.suspicious else WHITE)
            lab = f"№{x.label}" if not str(x.label).startswith("#") else str(x.label)
            d.text((PAD + 12 * S, y + 6 * S), lab, font=_f(12, True), fill=NAVY)
            pv = f"{round(100 * x.p)}% to'g'ri"
            d.text((PAD + 70 * S, y + 6 * S), pv, font=_f(12, True), fill=zcol(100 * x.p))
            parts = []
            if x.trap and x.trap_share >= 0.2:
                parts.append(f"ko'pchilik «{x.trap}» ni tanlagan ({round(100 * x.trap_share)}%)")
            if x.blank >= 0.2:
                parts.append(f"{round(100 * x.blank)}% javob bermagan")
            if x.topic and x.topic != OTHER:
                parts.append(x.topic)
            line = " · ".join(parts) or "ko'pchilik xato qilgan"
            d.text((PAD + 12 * S, y + 24 * S), _fit(d, line, _f(10), W - 2 * PAD - 170 * S), font=_f(10), fill=MUTED)
            if x.suspicious:
                tag = "KALITNI TEKSHIRING"
                tw2 = d.textlength(tag, font=_f(9, True)) + 16 * S
                d.rounded_rectangle([W - PAD - 10 * S - tw2, y + 11 * S, W - PAD - 10 * S, y + 31 * S], radius=10 * S,
                                    fill=RED)
                d.text((W - PAD - 10 * S - tw2 + 8 * S, y + 14 * S), tag, font=_f(9, True), fill=WHITE)
            y += 50 * S
    _footer(d, int(h), "ELNUR EDU")
    return _png(img)


# ================================ shaxsiy ================================
def personal(name: str, title: str, date_txt: str, pct: float, topics: dict[str, list],
             group_avg: dict[str, float] | None = None, group_mean: float | None = None,
             prev: dict[str, float] | None = None) -> bytes:
    rows = [(t, 100 * ok / n, n) for t, (ok, n) in topics.items() if n]
    rows.sort(key=lambda r: (r[0] == OTHER, -r[1]))
    strong, weak = strong_weak(topics)
    fw = _f(11)
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    bw = (W - 2 * PAD - 12 * S) / 2 - 28 * S
    sl = _wrap(tmp, ", ".join(strong) or "—", fw, bw)
    wl = _wrap(tmp, ", ".join(weak) or "Bunday mavzu yo'q — barakalla!", fw, bw)
    box_h = (44 + 18 * max(len(sl), len(wl), 1)) * S
    h = (124 + 18 + 120 + 16) * S + 24 * S + len(rows) * 38 * S + 16 * S + box_h + 70 * S
    img = Image.new("RGB", (W, int(h)), CREAM)
    d = ImageDraw.Draw(img)
    y = _head(d, "MAVZULAR TAHLILI", name, title, date_txt) + 18 * S

    d.rounded_rectangle([PAD, y, W - PAD, y + 106 * S], radius=16 * S, fill=WHITE, outline=zcol(pct), width=2 * S)
    cx, cy, r = PAD + 66 * S, y + 53 * S, 40 * S
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=LINE, width=10 * S)
    if pct > 0:
        d.arc([cx - r, cy - r, cx + r, cy + r], -90, -90 + 360 * min(pct, 100) / 100, fill=zcol(pct), width=10 * S)
    t = f"{round(pct)}%"
    d.text((cx - d.textlength(t, font=_f(20, True)) / 2, cy - 13 * S), t, font=_f(20, True), fill=INK)
    tx = PAD + 128 * S
    head = "A'lo natija!" if pct >= 85 else "Yaxshi natija" if pct >= 70 else "O'sish imkoniyati bor" \
        if pct >= 50 else "Mavzularni takrorlash kerak"
    d.text((tx, y + 18 * S), head, font=_f(16, True), fill=zcol(pct))
    if group_mean is not None:
        d.text((tx, y + 46 * S), f"Guruh o'rtachasi: {round(group_mean)}%", font=_f(11), fill=MUTED)
    d.text((tx, y + 66 * S), f"Kuchli: {len(strong)} ta mavzu   ·   Takrorlash: {len(weak)} ta", font=_f(11, True),
           fill=NAVY)
    y += 120 * S + 16 * S

    y = _section(d, y, "MAVZULAR BO'YICHA", "| — guruh o'rtachasi" if group_avg else "")
    for t, p, n in rows:
        tr = None
        if prev and t in prev:
            tr = round(p - prev[t]) or None
        y = _bar_row(d, y, t, p, f"{n} ta savol", avg=(group_avg or {}).get(t), faded=n < 2, trend=tr)
    y += 16 * S

    half = (W - 2 * PAD - 12 * S) / 2
    for i, (lab, lines, tint, col) in enumerate((("KUCHLI MAVZULAR", sl, TINT["g"], GREEN),
                                                 ("TAKRORLASH KERAK", wl, TINT["a"], AMBER))):
        x = PAD + i * (half + 12 * S)
        d.rounded_rectangle([x, y, x + half, y + box_h], radius=14 * S, fill=tint)
        d.rounded_rectangle([x, y, x + 6 * S, y + box_h], radius=3 * S, fill=col)
        d.text((x + 18 * S, y + 12 * S), lab, font=_f(10, True), fill=col)
        for j, ln in enumerate(lines):
            d.text((x + 18 * S, y + 34 * S + j * 18 * S), ln, font=fw, fill=INK)
    _footer(d, int(h), "Har bir mavzu — bitta qadam")
    return _png(img)
