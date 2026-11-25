"""Xodimlar uchun kunlik/haftalik natija: ball hisoblash, rag'bat matnlari va chiroyli karta (PNG).

Sof mantiq — bazaga bog'liq emas, shuning uchun to'liq test qilinadi.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw

from app.logic.rating_card import (CREAM, GOLD, GREEN, INK, LINE, MUTED, NAVY, NAVY2, S, W, WHITE,
                                   _f, _fit, _logo)

SOFT = (92, 107, 192)          # past natija — qizil emas, xotirjam ko'k-binafsha (uyaltirmaydi)
AMBER = (224, 138, 46)
WD = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
WD2 = ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"]

# kalit: (nom, og'irlik, maslahat)
KINDS = {
    "att": ("Davomat o'z vaqtida", 3, "davomatni dars tugashi bilan (1 soat ichida) kiriting"),
    "hw": ("Uy vazifasi berildi", 2, "uy vazifasini dars yakunida botga kiriting"),
    "hwc": ("Uy vazifasi tekshirildi", 2, "dars boshida uy vazifasini tekshirib, botda belgilang"),
    "exam": ("Nazorat o'tkazildi", 3, "rejadagi nazoratni o'z kunida o'tkazib, natijasini kiriting"),
    "mjt": ("Ota-onaga javob (24 soat)", 2, "ota-onalar murojaatiga 24 soat ichida javob bering"),
    "absent": ("Kelmaganlar sababi aniqlandi", 2, "darsga kelmaganlar uyiga qo'ng'iroq qilib, sababini belgilang"),
    "lead": ("Yangi arizalar bilan bog'lanildi", 2, "yangi arizalarga ertasi kungacha qo'ng'iroq qiling"),
    "mja": ("Murojaatlarga javob (24 soat)", 2, "murojaatlarga 24 soat ichida javob bering"),
    "print": ("Chop etish navbati bajarildi", 2, "chop etish so'rovlarini shu kunning o'zida bajaring"),
    "pay": ("To'lovlar tasdiqlandi", 3, "kutilayotgan to'lovlarni shu kunning o'zida tasdiqlang"),
    "cash": ("Kassa yopildi", 3, "kun oxirida kassani yopishni unutmang"),
    "task": ("Vazifalar muddatida", 2, "vazifalarni muddatida yopib boring («✅ Bajarildi»)"),
}


@dataclass
class Item:
    key: str
    done: int
    total: int

    @property
    def label(self) -> str:
        return KINDS[self.key][0]

    @property
    def weight(self) -> int:
        return KINDS[self.key][1]

    @property
    def ratio(self) -> float:
        return self.done / self.total if self.total else 1.0


def merge(groups: list[list[Item]]) -> list[Item]:
    """Bir necha kun (yoki rol) ko'rsatkichlarini bitta ro'yxatga qo'shadi, tartib saqlanadi."""
    out: dict[str, Item] = {}
    for items in groups:
        for it in items:
            if it.total <= 0:
                continue
            cur = out.setdefault(it.key, Item(it.key, 0, 0))
            cur.done += min(it.done, it.total)
            cur.total += it.total
    return sorted(out.values(), key=lambda i: list(KINDS).index(i.key))


def score(items: list[Item]) -> float | None:
    """Og'irlikli foiz: har bir yo'nalish o'z og'irligi bilan; o'lchanadigan ish bo'lmasa — None."""
    xs = [i for i in items if i.total > 0]
    if not xs:
        return None
    w = sum(i.weight for i in xs)
    return round(100 * sum(i.weight * min(1.0, i.ratio) for i in xs) / w, 1)


def tier(p: float) -> str:
    if p >= 90:
        return "zor"
    if p >= 75:
        return "yaxshi"
    if p >= 55:
        return "orta"
    return "past"


TIER_UI = {               # (karta sarlavhasi, rang)
    "zor": ("A'LO NATIJA!", GREEN),
    "yaxshi": ("YAXSHI NATIJA", NAVY2),
    "orta": ("BIZ SIZGA ISHONAMIZ", AMBER),
    "past": ("BIZ SIZGA ISHONAMIZ", SOFT),
}


def tip(items: list[Item]) -> str:
    """Eng ko'p ta'sir qiladigan kamchilik bo'yicha bitta aniq maslahat."""
    miss = [i for i in items if i.total and i.done < i.total]
    if not miss:
        return "shu ritmni saqlang"
    worst = max(miss, key=lambda i: i.weight * (1 - i.ratio))
    return KINDS[worst.key][2]


def short_name(full: str) -> str:
    """«Karimova Dilnoza Akmalovna» → «Karimova Dilnoza» (otasining ismi tushiriladi)."""
    p = full.split()
    if len(p) >= 3 and p[-1].lower().endswith(("ovna", "evna", "ovich", "evich", "qizi", "o'g'li", "o‘g‘li", "ogli")):
        p = p[:-1]
    return " ".join(p[:2])


DAY_MSG = {
    "zor": [
        "🌟 <b>{n}, bugun a'lo ishladingiz!</b>\nRejadagi ishlar o'z vaqtida bajarildi. Sizning intizomingiz — "
        "butun jamoa uchun namuna. Katta rahmat!",
        "🏆 <b>Ajoyib kun, {n}!</b>\nHar bir ishni puxta va vaqtida qildingiz. O'quvchilar va ota-onalar aynan "
        "shunday mehnatni qadrlashadi. Rahmat sizga!",
        "✨ <b>{n}, bugungi natijangiz — {p}%!</b>\nBu shunchaki raqam emas, bu sizning mas'uliyatingiz. "
        "ELNUR EDU siz bilan faxrlanadi!",
    ],
    "yaxshi": [
        "👏 <b>Yaxshi kun bo'ldi, {n}!</b>\nNatija — {p}%. A'loga juda oz qoldi: {t}. Ertaga albatta uddalaysiz!",
        "💪 <b>{n}, bugun yaxshi ishladingiz ({p}%).</b>\nBitta kichik qadam — {t} — va ertangi a'lo natija sizniki!",
    ],
    "orta": [
        "🤝 <b>{n}, biz sizga ishonamiz!</b>\nBugungi natija — {p}%. Ertaga bitta narsaga e'tibor bering: {t}. "
        "Shunda natija o'zi ko'tariladi.",
        "🌱 <b>{n}, har bir kun — yangi imkoniyat.</b>\nBugun {p}% bo'ldi, ertaga undan yuqori bo'lishiga "
        "ishonamiz. Maslahat: {t}.",
    ],
    "past": [
        "💙 <b>{n}, biz sizga ishonamiz.</b>\nBugun reja to'liq bajarilmadi — bunday kunlar hammada bo'ladi. "
        "Ertaga bitta qadamdan boshlang: {t}. Yordam kerak bo'lsa, rahbarga bemalol yozing — birga hal qilamiz.",
        "🤗 <b>{n}, qiyin kun bo'ldimi? Hechqisi yo'q.</b>\nSizning tajribangiz va mehnatingiz biz uchun juda qadrli. "
        "Ertaga: {t} — va natija albatta o'zgaradi. Biz sizga ishonamiz!",
    ],
}

WEEK_MSG = {
    "zor": [
        "🏆 <b>{n}, bu hafta siz a'lo natija ko'rsatdingiz — {p}%!</b>\nButun hafta intizom va mas'uliyat bilan "
        "ishladingiz. Jamoamiz siz bilan faxrlanadi. Dam olish kuningiz maroqli o'tsin!",
        "🌟 <b>Zo'r hafta, {n}! Natija — {p}%.</b>\nSiz kabi xodimlar tufayli ELNUR EDU har kuni o'sib bormoqda. "
        "Chin dildan rahmat!",
    ],
    "yaxshi": [
        "👏 <b>{n}, haftalik natijangiz — {p}%.</b>\nBu yaxshi ko'rsatkich! Keyingi haftada {t} — va a'lo natija "
        "sizniki. Mehnatingiz uchun rahmat!",
    ],
    "orta": [
        "🤝 <b>{n}, biz sizga ishonamiz!</b>\nHaftalik natija — {p}%. Sizda bundan ham yuqori natija ko'rsatish "
        "salohiyati bor. Keyingi hafta uchun bitta maqsad: {t}. Biz doim yordamga tayyormiz.",
    ],
    "past": [
        "💙 <b>{n}, biz sizga ishonamiz.</b>\nBu hafta oson kechmadi ({p}%), lekin bir hafta hech narsani hal "
        "qilmaydi. Keyingi haftani yangi kuch bilan boshlaymiz: {t}. Qanday yordam kerak bo'lsa, rahbarga "
        "ayting — birga hal qilamiz.",
    ],
}

QUOTES = [
    "Yaxshi ustoz — o'quvchining yuragida yoqilgan chiroq.",
    "Kichik ishni o'z vaqtida qilish — katta natijaning siri.",
    "Bugungi mehnat — ertangi faxr.",
    "Intizom — maqsad bilan natija orasidagi ko'prik.",
    "Har bir o'quvchi — ishonib topshirilgan omonat.",
    "Muvaffaqiyat — har kuni takrorlanadigan kichik harakatlar yig'indisi.",
    "Sabr va izchillik har qanday cho'qqini zabt etadi.",
    "Jamoa kuchli bo'lsa — har bir a'zo kuchli.",
    "O'qituvchining bir kuni — o'quvchining bir umri.",
    "Eng yaxshi reklama — mamnun ota-ona.",
    "Rejali kun — xotirjam kun.",
    "Ishonch — so'z bilan emas, har kungi ish bilan qoziniladi.",
    "Bugun boshlagan ishingiz ertaga odatingizga aylanadi.",
    "Siz qilgan har bir yaxshi ish — markazning obro'si.",
]


def pick(options: list[str], seed: int) -> str:
    return options[seed % len(options)]


def day_text(name: str, p: float, items: list[Item], seed: int) -> str:
    tr = tier(p)
    return pick(DAY_MSG[tr], seed).format(n=short_name(name), p=f"{p:g}", t=tip(items))


def week_text(name: str, p: float, items: list[Item], seed: int, prev: float | None, best: bool) -> str:
    tr = tier(p)
    txt = pick(WEEK_MSG[tr], seed).format(n=short_name(name), p=f"{p:g}", t=tip(items))
    if prev is not None:
        dlt = round(p - prev)
        if dlt >= 3:
            txt += f"\n📈 O'tgan haftaga nisbatan <b>+{dlt}%</b> — o'sish ko'rinib turibdi!"
        elif dlt <= -3 and tr in ("zor", "yaxshi"):
            txt += "\n📊 O'tgan haftadan biroz pastroq, lekin baribir yaxshi daraja."
    if best:
        txt += "\n🥇 <b>Siz — haftaning eng yaxshi xodimisiz!</b>"
    return txt


# ===================================== karta =====================================
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


def _head(d, title: str, name: str, sub: str, date_txt: str) -> int:
    h = 118 * S
    d.rectangle([0, 0, W, h], fill=NAVY)
    d.rectangle([0, h - 5 * S, W, h], fill=GOLD)
    _logo(d, 52 * S, 58 * S, 30 * S)
    d.text((100 * S, 24 * S), title, font=_f(26, True), fill=WHITE)
    d.text((100 * S, 62 * S), _fit(d, name, _f(15, True), 300 * S), font=_f(15, True), fill=GOLD)
    d.text((100 * S, 86 * S), _fit(d, sub, _f(11), 300 * S), font=_f(11), fill=(190, 204, 220))
    tw = d.textlength(date_txt, font=_f(11))
    d.text((W - 24 * S - tw, 30 * S), date_txt, font=_f(11), fill=(190, 204, 220))
    d.text((W - 24 * S - d.textlength("ELNUR EDU", font=_f(12, True)), 48 * S), "ELNUR EDU",
           font=_f(12, True), fill=WHITE)
    return h


def _big_ring(d, cx, cy, r, pct, color):
    w = 12 * S
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=LINE, width=w)
    if pct > 0:
        d.arc([cx - r, cy - r, cx + r, cy + r], -90, -90 + 360 * min(pct, 100) / 100, fill=color, width=w)
    t = f"{round(pct)}%"
    f = _f(26, True)
    d.text((cx - d.textlength(t, font=f) / 2, cy - 17 * S), t, font=f, fill=INK)


def _check(d, cx, cy, r, ok: float):
    col = GREEN if ok >= 1 else (AMBER if ok > 0 else LINE)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    if ok >= 1:
        d.line([(cx - r * 0.45, cy), (cx - r * 0.1, cy + r * 0.38), (cx + r * 0.5, cy - r * 0.35)],
               fill=WHITE, width=max(2, int(r * 0.28)), joint="curve")
    else:
        f = _f(11, True)
        t = "!" if ok == 0 else "½"
        d.text((cx - d.textlength(t, font=f) / 2, cy - 8 * S), t, font=f, fill=WHITE if ok else MUTED)


def render(kind: str, name: str, role: str, date_txt: str, pct: float, items: list[Item],
           days: list[tuple[str, float | None]] | None = None, prev: float | None = None,
           best: bool = False) -> bytes:
    """kind: 'day' | 'week'.  days: [("Du", 92.0), ...] — haftalik ustunlar."""
    tr = tier(pct)
    head, col = TIER_UI[tr]
    pad = 20 * S
    row_h = 44 * S
    chart_h = 166 * S if days else 0
    h = 118 * S + 18 * S + 150 * S + 34 * S + len(items) * row_h + chart_h + 64 * S
    img = Image.new("RGB", (W, h), CREAM)
    d = ImageDraw.Draw(img)
    y = _head(d, "KUNLIK NATIJA" if kind == "day" else "HAFTALIK NATIJA", name, role, date_txt) + 18 * S

    # --- asosiy blok: halqa + sarlavha
    d.rounded_rectangle([pad, y, W - pad, y + 136 * S], radius=16 * S, fill=WHITE, outline=col, width=2 * S)
    _big_ring(d, pad + 78 * S, y + 68 * S, 48 * S, pct, col)
    tx = pad + 150 * S
    fh = _f(17, True)
    d.text((tx, y + 22 * S), _fit(d, head, fh, W - pad - tx - 14 * S), font=fh, fill=col)
    done = sum(i.done for i in items)
    tot = sum(i.total for i in items)
    d.text((tx, y + 52 * S), f"{done} / {tot} ish bajarildi", font=_f(13, True), fill=INK)
    if best:
        d.rounded_rectangle([tx, y + 82 * S, tx + 176 * S, y + 104 * S], radius=11 * S, fill=GOLD)
        d.text((tx + 12 * S, y + 86 * S), "★ HAFTA XODIMI", font=_f(11, True), fill=WHITE)
    elif prev is not None:
        dl = round(pct - prev)
        lab = "kechaga" if kind == "day" else "o'tgan haftaga"
        if dl > 0:
            d.text((tx, y + 84 * S), f"▲ +{dl}%  {lab} nisbatan", font=_f(11, True), fill=GREEN)
        elif dl < 0:
            d.text((tx, y + 84 * S), f"{lab.capitalize()}: {round(prev)}%", font=_f(11), fill=MUTED)
        else:
            d.text((tx, y + 84 * S), f"Barqaror: {round(prev)}%", font=_f(11), fill=MUTED)
    y += 150 * S

    # --- haftalik ustunlar
    if days:
        d.text((pad, y), "KUNLAR KESIMIDA", font=_f(11, True), fill=MUTED)
        y += 40 * S
        ch = 92 * S
        n = len(days)
        slot = (W - 2 * pad) / max(n, 1)
        bw = min(46 * S, slot * 0.55)
        d.line([(pad, y + ch), (W - pad, y + ch)], fill=LINE, width=S)
        for i, (lab, v) in enumerate(days):
            cx = pad + slot * i + slot / 2
            if v is not None:
                bh = max(4 * S, ch * v / 100)
                c2 = TIER_UI[tier(v)][1]
                d.rounded_rectangle([cx - bw / 2, y + ch - bh, cx + bw / 2, y + ch], radius=6 * S, fill=c2)
                t = f"{round(v)}"
                d.text((cx - d.textlength(t, font=_f(10, True)) / 2, y + ch - bh - 16 * S), t,
                       font=_f(10, True), fill=INK)
            else:
                d.text((cx - d.textlength("—", font=_f(10)) / 2, y + ch - 16 * S), "—", font=_f(10), fill=MUTED)
            d.text((cx - d.textlength(lab, font=_f(10)) / 2, y + ch + 6 * S), lab, font=_f(10), fill=MUTED)
        y += chart_h - 40 * S

    # --- ko'rsatkichlar
    d.text((pad, y + 4 * S), "KO'RSATKICHLAR", font=_f(11, True), fill=MUTED)
    y += 30 * S
    for it in items:
        _check(d, pad + 14 * S, y + 16 * S, 12 * S, it.ratio)
        lx = pad + 38 * S
        d.text((lx, y + 6 * S), _fit(d, it.label, _f(12, True), 300 * S), font=_f(12, True), fill=INK)
        bx, bwid = lx, W - pad - lx - 70 * S
        d.rounded_rectangle([bx, y + 26 * S, bx + bwid, y + 32 * S], radius=3 * S, fill=LINE)
        if it.done:
            d.rounded_rectangle([bx, y + 26 * S, bx + max(6 * S, bwid * it.ratio), y + 32 * S], radius=3 * S,
                                fill=GREEN if it.ratio >= 1 else AMBER)
        t = f"{it.done}/{it.total}"
        fs = _f(14, True)
        d.text((W - pad - d.textlength(t, font=fs), y + 9 * S), t, font=fs, fill=NAVY)
        y += row_h

    fy = h - 46 * S
    d.line([(pad, fy), (W - pad, fy)], fill=LINE, width=S)
    d.text((pad, fy + 12 * S), "Har bir kichik ish — katta natija sari qadam", font=_f(9), fill=MUTED)
    tw = d.textlength("elnuredu", font=_f(9, True))
    d.text((W - pad - tw, fy + 12 * S), "elnuredu", font=_f(9, True), fill=NAVY)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


@dataclass
class TeamRow:
    name: str
    role: str
    pct: float | None
    prev: float | None = None
    weak: str = ""


def render_team(title: str, date_txt: str, rows: list[TeamRow]) -> bytes:
    rows = sorted(rows, key=lambda r: (r.pct is None, -(r.pct or 0), r.name))
    pad = 20 * S
    row_h = 52 * S
    scored = [r.pct for r in rows if r.pct is not None]
    avg = round(sum(scored) / len(scored)) if scored else None
    h = 118 * S + 18 * S + 96 * S + 30 * S + len(rows) * row_h + 64 * S
    img = Image.new("RGB", (W, h), CREAM)
    d = ImageDraw.Draw(img)
    y = _head(d, "JAMOA NATIJASI", title, f"{len(rows)} xodim", date_txt) + 18 * S
    # umumiy bloklar
    cnt = {k: sum(1 for p in scored if tier(p) == k) for k in TIER_UI}
    boxes = [("O'rtacha", f"{avg}%" if avg is not None else "—", NAVY), ("A'lo", str(cnt["zor"]), GREEN),
             ("Yaxshi", str(cnt["yaxshi"]), NAVY2), ("Yordam kerak", str(cnt["orta"] + cnt["past"]), AMBER)]
    bw = (W - 2 * pad - 3 * 10 * S) / 4
    for i, (lab, val, c) in enumerate(boxes):
        x = pad + i * (bw + 10 * S)
        d.rounded_rectangle([x, y, x + bw, y + 80 * S], radius=14 * S, fill=WHITE, outline=LINE, width=S)
        f = _f(22, True)
        d.text((x + bw / 2 - d.textlength(val, font=f) / 2, y + 14 * S), val, font=f, fill=c)
        d.text((x + bw / 2 - d.textlength(lab, font=_f(10)) / 2, y + 52 * S), lab, font=_f(10), fill=MUTED)
    y += 96 * S
    d.text((pad, y), "XODIMLAR", font=_f(11, True), fill=MUTED)
    y += 30 * S
    for i, r in enumerate(rows):
        c = TIER_UI[tier(r.pct)][1] if r.pct is not None else LINE
        d.rounded_rectangle([pad, y, W - pad, y + row_h - 8 * S], radius=12 * S, fill=WHITE)
        d.rounded_rectangle([pad + 6 * S, y + 10 * S, pad + 10 * S, y + row_h - 18 * S], radius=2 * S, fill=c)
        d.text((pad + 18 * S, y + 6 * S), _fit(d, short_name(r.name), _f(12, True), 250 * S), font=_f(12, True),
               fill=INK)
        sub = r.role + (f" · {r.weak}" if r.weak else "")
        d.text((pad + 18 * S, y + 25 * S), _fit(d, sub, _f(9), 300 * S), font=_f(9), fill=MUTED)
        t = f"{round(r.pct)}%" if r.pct is not None else "—"
        fs = _f(17, True)
        d.text((W - pad - 16 * S - d.textlength(t, font=fs), y + 10 * S), t, font=fs, fill=c if r.pct is not None else MUTED)
        if r.pct is not None and r.prev is not None and round(r.pct - r.prev):
            dl = round(r.pct - r.prev)
            at = f"▲{dl}" if dl > 0 else f"▼{-dl}"
            d.text((W - pad - 96 * S, y + 14 * S), at, font=_f(10, True), fill=GREEN if dl > 0 else MUTED)
        y += row_h
    fy = h - 46 * S
    d.line([(pad, fy), (W - pad, fy)], fill=LINE, width=S)
    d.text((pad, fy + 12 * S), "90%+ a'lo · 75%+ yaxshi · undan pasti — qo'llab-quvvatlash kerak", font=_f(9),
           fill=MUTED)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
