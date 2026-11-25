"""Tashqi tekshiruv natijalarini o'qish: kanal/bot yuborgan ro'yxat (matn, Excel, CSV, PDF) → (ism, ball).

Ismlar lotin yoki kirill, «Familiya Ism» yoki «Ism Familiya», otasining ismi bilan yoki bo'lmasa ham
o'quvchilar ro'yxatiga moslanadi. Sof mantiq — bazaga bog'liq emas.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

CYR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i",
       "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
       "у": "u", "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "",
       "э": "e", "ю": "yu", "я": "ya", "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h"}
APOS = "'ʻʼ’‘`´"
PATRO = ("ovna", "evna", "ovich", "evich", "qizi", "ogli", "o'g'li", "ugli")
STOP = {"ball", "bal", "ta", "foiz", "daraja", "natija", "natijasi", "togri", "javob", "jami", "sertifikat",
        "fish", "fio", "ism", "familiya", "ishtirokchi", "oquvchi", "guruh", "variant", "ball:", "umumiy",
        "kelmadi", "qatnashmadi", "kelmagan", "yoq", "reyting", "orin", "test", "baho", "max", "maks", "dan",
        "javoblar", "javobi", "togrilar", "savol", "savollar", "ballar", "umumiy", "foizi", "sertifikati", "xato",
        "xatolar", "natijalar", "vaqt", "sana", "fan", "natijangiz", "tabriklaymiz", "darajasi", "ishtirokchilar", "soni", "id", "qism",
        "bosqich", "pers", "persentil", "reyting", "joy", "viloyat", "tuman", "maktab", "sinf", "kod"}
ABSENT = ("kelmadi", "qatnashmadi", "kelmagan", "ishtirok etmadi", "н/я", "неявка")
LEVEL = re.compile(r"^(?:[ABC][12]?\+?|[ABC]\+|PRE-?A1|A0)$", re.I)
LVL_AFTER = re.compile(r"(\d+(?:[.,]\d+)?)\s*[|;,]?\s*\(?\b([ABC][12]?\+?)(?=[\s|;,)]|$)")
PCT = re.compile(r"\d+(?:[.,]\d+)?\s*%")
NUM = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)(?:\s*/\s*(\d+(?:[.,]\d+)?))?(?![\w])")
PHONE = re.compile(r"\+?998[\d\s\-()]{9,14}|\b\d{2}[\s\-]\d{3}[\s\-]\d{2}[\s\-]\d{2}\b|\b\d{9}\b")
DATE = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b|\b\d{1,2}:\d{2}\b")
INDEX = re.compile(r"^\s*(?:№\s*)?\d{1,4}\s*[.)\-–:|]\s*")


def latin(s: str) -> str:
    out = []
    for ch in s:
        lo = ch.lower()
        if lo in CYR:
            t = CYR[lo]
            out.append(t.capitalize() if ch != lo and t else t)
        else:
            out.append(ch)
    return "".join(out)


def tokens(name: str) -> list[str]:
    s = latin(name).lower()
    for a in APOS:
        s = s.replace(a, "")
    words = re.findall(r"[a-z]+\.?", s)
    out = []
    for w in words:
        w2 = w.rstrip(".")
        if not w2 or w2 in STOP or w2.endswith(PATRO):
            continue
        out.append(w2)
    return out


def _tsim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if len(a) == 1 or len(b) == 1:                  # bosh harf: «Aliyev V.»
        return 0.9 if (b.startswith(a) or a.startswith(b)) else 0.0
    return SequenceMatcher(None, a, b).ratio()


def name_sim(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    used, total = set(), 0.0
    for t in short:
        best, bj = 0.0, None
        for j, u in enumerate(long_):
            if j in used:
                continue
            v = _tsim(t, u)
            if v > best:
                best, bj = v, j
        if bj is not None:
            used.add(bj)
        total += best
    sc = total / len(short)
    if len(short) == 1 and len(long_) >= 2:         # faqat familiya — ishonch pastroq
        sc *= 0.85
    return round(sc, 3)


@dataclass
class Row:
    name: str
    value: float | None          # None — kelmagan
    raw: str
    level: str | None = None     # kanal bergan daraja (C, B+, …)
    rank: int | None = None      # umumiy ro'yxatdagi o'rin
    seat: int | None = None
    sim: float = 0.0
    cands: list[int] = field(default_factory=list)


def _num(x: str) -> float:
    return float(x.replace(",", "."))


def parse_line(line: str, mx: float) -> Row | str | None:
    """Row — natija; str — tushunarsiz qator (xato sifatida ko'rsatiladi); None — e'tiborsiz (sarlavha, bo'sh)."""
    s = " ".join(line.replace("\t", " ").split())
    if len(s) < 3:
        return None
    s = PCT.sub(" ", PHONE.sub(" ", DATE.sub(" ", s)))           # foizlar ball emas
    nums = NUM.findall(s)
    body = INDEX.sub("", s, count=1)
    rank = None
    if body != s:
        rank = int(re.match(r"\D*(\d+)", s).group(1))
        nums = NUM.findall(body)
    elif len(nums) >= 2 and re.match(r"^\s*\d{1,5}\s+[^\d\s]", s):      # «610 Aliyev Vali 47.39 C»
        rank = int(re.match(r"\s*(\d+)", s).group(1))
        body = re.sub(r"^\s*\d{1,5}\s+", "", s)
        nums = NUM.findall(body)
    words = [w for w in re.findall(r"[^\W\d_][^\s\d|,;:—–\-()]*", body) if not LEVEL.match(w.strip(".+"))]
    name = " ".join(w for w in words if tokens(w))
    if not tokens(name):
        return None
    low = latin(body).lower()
    if any(k in low for k in ABSENT):
        return Row(name, None, line.strip(), rank=rank)
    # 0) daraja oldidagi son — umumiy ball («… 47.39 C 72.9% …»)
    for a, lv in LVL_AFTER.findall(body):
        v = _num(a)
        if 0 <= v <= mx:
            return Row(name, v, line.strip(), lv.upper(), rank)
    # 1) «Ball: 62» yoki «62.5 ball» — yorliqli son ustun
    for rx in (r"(?:ball|natija|umumiy|jami)[^\d\n]{0,8}?(\d+(?:[.,]\d+)?)(?!\s*/)", r"(\d+(?:[.,]\d+)?)\s*(?:ball|bal\b)"):
        hit = [_num(x) for x in re.findall(rx, low)]
        hit = [v for v in hit if 0 <= v <= mx]
        if hit:
            return Row(name, hit[-1], line.strip(), rank=rank)
    # 2) «33/45» — to'g'ri javoblar soni; boshqa son bo'lsa o'shani olamiz
    plain = [_num(a) for a, b in nums if not b]
    frac = [_num(a) for a, b in nums if b]
    ok = [v for v in plain if 0 <= v <= mx] or [v for v in frac if 0 <= v <= mx]
    if ok:
        return Row(name, ok[-1], line.strip(), rank=rank)
    if not nums:
        return None                                   # sarlavha, izoh yoki natijasiz ism
    return line.strip()


def _is_name(t: str) -> bool:
    words = [w for w in re.findall(r"[^\W\d_][^\s\d]*", t) if not LEVEL.match(w.strip(".+()"))]
    return any(len(x) >= 3 for x in tokens(" ".join(words)))


def _stream(lines: list[str]) -> list[str]:
    """Jadval ustunlari alohida qatorlarga tushgan PDF: [o'rin] + ism qatori + keyingi raqam qatorlari = bitta yozuv."""
    ls = [x.strip() for x in lines if x.strip()]
    out, cur = [], ""
    i = 0
    while i < len(ls):
        t = ls[i]
        if re.fullmatch(r"\d{1,5}", t) and i + 1 < len(ls) and _is_name(ls[i + 1]):
            if cur:
                out.append(cur)
            cur = t + " " + ls[i + 1]
            i += 2
            continue
        if _is_name(t):
            if cur:
                out.append(cur)
            cur = t
        elif cur:
            cur += " " + t
        i += 1
    if cur:
        out.append(cur)
    return out


def parse_text(text: str | list[str], mx: float) -> tuple[list[Row], list[str]]:
    """Har bir bo'lak (xabar yoki fayl) alohida: qatorma-qator yoki «ism + keyingi qatorlar» — qaysi biri ko'p bersa."""
    if isinstance(text, list):
        rows, bad = [], []
        for chunk in text:
            r, b = parse_text(chunk, mx)
            rows += r
            bad += b
        return rows, bad
    lines = text.splitlines()

    def run(ls):
        rows, bad = [], []
        for ln in ls:
            r = parse_line(ln, mx)
            if isinstance(r, Row):
                rows.append(r)
            elif r:
                bad.append(r)
        return rows, bad
    a = run(lines)
    b = run(_stream(lines))
    return a if len(a[0]) >= len(b[0]) else b


def text_from_file(data: bytes, fname: str) -> str:
    n = (fname or "").lower()
    if n.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [(f"{c:g}" if isinstance(c, float) else str(c)) for c in row if c not in (None, "")]
                if cells:
                    out.append(" | ".join(cells))
        return "\n".join(out)
    if n.endswith(".csv"):
        txt = data.decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(txt[:2000], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return "\n".join(" | ".join(c for c in r if c.strip()) for r in csv.reader(io.StringIO(txt), dialect))
    if n.endswith(".pdf"):
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    return data.decode("utf-8", errors="replace")


THRESH, GAP = 0.8, 0.06


def big_list(rows: list[Row], seats: list[tuple[int, str]]) -> bool:
    """Kanalning umumiy (respublika) ro'yxati — bizdan boshqa markazlar ham bor."""
    return len(rows) > 2 * len(seats) + 10


def match(rows: list[Row], seats: list[tuple[int, str]]) -> int:
    """Har bir qatorga eng mos qatnashchini biriktiradi (bir qatnashchi — bitta qator). Noaniqlar — cands bilan.
    Umumiy ro'yxatda bizga o'xshamagan qatorlar (boshqa markazlar) olib tashlanadi; ularning soni qaytariladi."""
    big = big_list(rows, seats)
    pairs, foreign = [], []
    for i, r in enumerate(rows):
        sims = sorted(((name_sim(r.name, nm), sid) for sid, nm in seats), reverse=True)
        if big and (not sims or sims[0][0] < 0.7):
            foreign.append(i)
            continue
        r.cands = [sid for v, sid in sims[:6] if v >= 0.45]
        if sims and sims[0][0] >= THRESH and (len(sims) == 1 or sims[1][0] < sims[0][0] - GAP):
            pairs.append((sims[0][0], i, sims[0][1]))
    taken = set()
    for v, i, sid in sorted(pairs, reverse=True):
        if sid in taken:
            continue
        rows[i].seat, rows[i].sim = sid, v
        taken.add(sid)
    for i in reversed(foreign):
        del rows[i]
    return len(foreign)
