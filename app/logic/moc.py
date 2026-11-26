"""MOC / tayyor PDF testlar: imtihon formati (shablon), javob kaliti va baholash.

Format matni (o'qituvchi o'zi ham yozishi mumkin):

    nom: DTM (abituriyent)
    1-10: Ona tili | test 4 | 1.1
    11-20: Matematika | test 4 | 1.1
    21-90: ...
    Writing | baho 75
    natija: ball            # ball | foiz | shkala 75 | cefr | ielts
    daraja: 70 A+; 65 A; 60 B+; 55 B; 50 C+; 46 C

Bo'lim turlari:
  test K      — K variantli (2..6: A..F) yopiq savol; to'g'ri javob = ball
  yozma M     — ochiq savol: o'qituvchi varaqdagi 1..M doirachadan birini bo'yaydi (0 — hech biri); ball = M × narx
  baho M      — varaqda yo'q: bo'lim balini o'qituvchi botga yozadi (Writing/Speaking: 0..75 yoki IELTS band 0..9)
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

LETTERS = "ABCDEF"


@dataclass
class Section:
    name: str
    kind: str                 # test | yozma | baho
    n: int = 0                # savollar soni (test/yozma)
    opts: int = 4             # test: variantlar soni
    mx: float = 1             # yozma: 1 savoldagi eng katta belgi; baho: eng katta ball
    pts: float = 1.0          # test: 1 to'g'ri javob bali; yozma: 1 birlik bali
    start: int = 0            # 1-savol raqami (test/yozma)

    @property
    def on_sheet(self) -> bool:
        return self.kind in ("test", "yozma")

    @property
    def max_score(self) -> float:
        if self.kind == "test":
            return self.n * self.pts
        if self.kind == "yozma":
            return self.n * self.mx * self.pts
        return float(self.mx)


@dataclass
class Spec:
    name: str
    sections: list[Section]
    mode: str = "ball"        # ball | foiz | shkala | rasch | cefr | ielts
    scale: float = 100.0      # shkala uchun eng katta ball
    levels: list[tuple[float, str]] = field(default_factory=list)   # kamayish tartibida
    errors: list[str] = field(default_factory=list)
    topics: dict[int, str] = field(default_factory=dict)            # savol raqami -> mavzu (tahlil uchun)

    @property
    def n_questions(self) -> int:
        return sum(s.n for s in self.sections if s.on_sheet)

    @property
    def n_test(self) -> int:
        return sum(s.n for s in self.sections if s.kind == "test")

    def bubbles(self) -> list[tuple[str, int]]:
        """Har savol uchun (tur, doirachalar soni): ('t', 4) yoki ('y', 2)."""
        out = []
        for s in self.sections:
            if s.kind == "test":
                out += [("t", s.opts)] * s.n
            elif s.kind == "yozma":
                out += [("y", int(s.mx))] * s.n
        return out

    def layout(self) -> str:
        """QR uchun ixcham yozuv: 't4x32.t6x3.y2x10'."""
        runs: list[list] = []
        for b in self.bubbles():
            if runs and runs[-1][0] == b:
                runs[-1][1] += 1
            else:
                runs.append([b, 1])
        return ".".join(f"{k}{c}x{n}" for (k, c), n in runs)

    def section_of(self, q: int) -> Section | None:
        for s in self.sections:
            if s.on_sheet and s.start <= q < s.start + s.n:
                return s
        return None

    def to_text(self) -> str:
        lines = [f"nom: {self.name}"]
        for s in self.sections:
            if s.kind == "test":
                lines.append(f"{s.start}-{s.start + s.n - 1}: {s.name} | test {s.opts} | {_num(s.pts)}")
            elif s.kind == "yozma":
                lines.append(f"{s.start}-{s.start + s.n - 1}: {s.name} | yozma {int(s.mx)} | {_num(s.pts)}")
            else:
                lines.append(f"{s.name} | baho {_num(s.mx)}")
        lines.append("natija: " + ({"shkala": f"shkala {_num(self.scale)}",
                                    "rasch": f"rasch {_num(self.scale)}"}.get(self.mode, self.mode)))
        if self.levels:
            lines.append("daraja: " + "; ".join(f"{_num(a)} {b}" for a, b in self.levels))
        return "\n".join(lines)

    def summary(self) -> str:
        """Odam uchun qisqa tavsif."""
        out = [f"<b>{self.name}</b>"]
        for s in self.sections:
            if s.kind == "test":
                out.append(f"• {s.start}–{s.start + s.n - 1}: {s.name} — test ({LETTERS[:s.opts]}), {_num(s.pts)} ball")
            elif s.kind == "yozma":
                out.append(f"• {s.start}–{s.start + s.n - 1}: {s.name} — yozma, o'qituvchi 0–{int(s.mx)} "
                           f"belgilaydi, birligi {_num(s.pts)} ball")
            else:
                out.append(f"• {s.name} — o'qituvchi botga yozadi (0–{_num(s.mx)})")
        res = {"ball": f"jami ball (maks. {_num(sum(s.max_score for s in self.sections))})", "foiz": "foiz",
               "shkala": f"{_num(self.scale)} ballik shkala (taxminiy)",
               "rasch": f"Rasch modeli, {_num(self.scale)} ballik shkala", "cefr": "CEFR: har bo'lim 75 ballik, daraja o'rtachadan",
               "ielts": "IELTS band (0–9), umumiy band o'rtachadan"}[self.mode]
        out.append(f"Natija: {res}")
        if self.levels:
            out.append("Darajalar: " + ", ".join(f"{b} ≥{_num(a)}" for a, b in self.levels))
        if self.topics:
            out.append(f"Mavzular: {len(set(self.topics.values()))} ta ({len(self.topics)} savolga) — tahlil kartasi bo'ladi")
        return "\n".join(out)


def _num(x: float) -> str:
    return f"{x:g}"


# ---------------- tayyor shablonlar ----------------
MS_LEVELS = "70 A+; 65 A; 60 B+; 55 B; 50 C+; 46 C"
PRESETS: dict[str, tuple[str, str]] = {
    "oddiy": ("📝 Oddiy test", "nom: Test\n1-20: Test | test 4 | 1\nnatija: foiz"),
    "ms": ("🏅 Milliy sertifikat (matematika)", "nom: Milliy sertifikat (matematika)\n1-35: Yopiq savollar | test 4 | 1\n"
                                   "36-45: Ochiq savollar (a, b) | yozma 2 | 1\nnatija: rasch 75\n"
                                   f"daraja: {MS_LEVELS}"),
    "msbio": ("🧬 Milliy sertifikat (biologiya)",
              "nom: Milliy sertifikat (biologiya)\n1-32: Yopiq test | test 4 | 1\n33-35: Moslashtirish | yozma 2 | 1\n"
              "36-40: Qisqa javob | yozma 1 | 1\n41-topshiriq (yozma ish) | baho 30\n42-topshiriq (yozma ish) | baho 35\n"
              f"43-topshiriq (yozma ish) | baho 10\nnatija: rasch 75\ndaraja: {MS_LEVELS}"),
    "msona": ("📖 Milliy sertifikat (ona tili)",
              "nom: Milliy sertifikat (ona tili va adabiyot)\n1-32: Yopiq test | test 4 | 1\n33-35: Moslashtirish | yozma 2 | 1\n"
              f"36-44: Qisqa javob | yozma 1 | 1\nEsse (45-topshiriq) | baho 20\nnatija: rasch 75\ndaraja: {MS_LEVELS}"),
    "dtm": ("🎓 DTM (abituriyent)", "nom: DTM (abituriyent)\n1-10: Ona tili | test 4 | 1.1\n11-20: Matematika | test 4 | 1.1\n"
                                    "21-30: O'zbekiston tarixi | test 4 | 1.1\n31-60: 1-fan | test 4 | 3.1\n"
                                    "61-90: 2-fan | test 4 | 2.1\nnatija: ball"),
    "cefr": ("🇬🇧 CEFR (multilevel)", "nom: CEFR (multilevel)\n1-35: Listening | test 6 | 1\n36-70: Reading | test 6 | 1\n"
                                      "Writing | baho 75\nSpeaking | baho 75\nnatija: cefr"),
    "ielts": ("🇬🇧 IELTS", "nom: IELTS (mock)\n1-40: Listening | yozma 1 | 1\n41-80: Reading | yozma 1 | 1\n"
                          "Writing | baho 9\nSpeaking | baho 9\nnatija: ielts"),
}
CEFR_LEVELS = [(65, "C1"), (51, "B2"), (38, "B1"), (0, "B1 dan past")]
CEFR_ANCHORS = [(0, 0), (10 / 35, 38), (18 / 35, 51), (28 / 35, 65), (1, 75)]
# IELTS: to'g'ri javoblar (40 tadan) → band
IELTS_L = [(39, 9), (37, 8.5), (35, 8), (32, 7.5), (30, 7), (26, 6.5), (23, 6), (18, 5.5), (16, 5), (13, 4.5),
           (10, 4), (8, 3.5), (6, 3), (4, 2.5), (2, 2), (1, 1), (0, 0)]
IELTS_R = [(39, 9), (37, 8.5), (35, 8), (33, 7.5), (30, 7), (27, 6.5), (23, 6), (19, 5.5), (15, 5), (13, 4.5),
           (10, 4), (8, 3.5), (6, 3), (4, 2.5), (2, 2), (1, 1), (0, 0)]


def simple_spec(n: int, opts: int = 4) -> Spec:
    return parse_spec(f"nom: Test\n1-{n}: Test | test {opts} | 1\nnatija: foiz")


_RANGE = re.compile(r"^\s*(\d+)\s*[-–]\s*(\d+)\s*:\s*(.+)$")


def parse_spec(text: str) -> Spec:
    sp = Spec(name="Imtihon", sections=[])
    nxt = 1
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("nom:"):
            sp.name = line.split(":", 1)[1].strip()[:80] or sp.name
            continue
        if low.startswith("natija:"):
            v = low.split(":", 1)[1].split()
            if not v or v[0] not in ("ball", "foiz", "shkala", "rasch", "cefr", "ielts"):
                sp.errors.append(f"«{line}» — natija: ball, foiz, shkala 75, rasch 75, cefr yoki ielts bo'lishi kerak")
                continue
            sp.mode = v[0]
            if sp.mode == "rasch" and len(v) < 2:
                sp.scale = 75.0
            elif sp.mode in ("shkala", "rasch"):
                try:
                    sp.scale = float(v[1].replace(",", "."))
                except (IndexError, ValueError):
                    sp.errors.append("shkala uchun eng katta ballni yozing: natija: shkala 75")
            continue
        if low.startswith("mavzu:"):
            for part in line.split(":", 1)[1].split(";"):
                mm = re.match(r"^\s*([\d\s,\-–]+?)\s+([^\d\s].*)$", part)
                if not mm:
                    if part.strip():
                        sp.errors.append(f"mavzu «{part.strip()}» — avval savollar, keyin nomi: 1-5 Kasrlar")
                    continue
                for chunk in re.split(r"[,\s]+", mm.group(1).replace("–", "-").strip()):
                    if not chunk:
                        continue
                    a_, _, b_ = chunk.partition("-")
                    try:
                        lo_, hi_ = int(a_), int(b_ or a_)
                    except ValueError:
                        sp.errors.append(f"mavzu «{part.strip()}» — raqamlar noto'g'ri")
                        continue
                    for q in range(lo_, hi_ + 1):
                        sp.topics[q] = mm.group(2).strip()[:60]
            continue
        if low.startswith("daraja:"):
            lv = []
            for part in line.split(":", 1)[1].split(";"):
                bits = part.split()
                if len(bits) < 2:
                    continue
                try:
                    lv.append((float(bits[0].replace(",", ".")), " ".join(bits[1:])))
                except ValueError:
                    sp.errors.append(f"daraja «{part.strip()}» — avval ball, keyin nomi: 70 A+")
            sp.levels = sorted(lv, key=lambda x: -x[0])
            continue
        m = _RANGE.match(line)
        if m:
            a, b, rest = int(m.group(1)), int(m.group(2)), m.group(3)
        else:
            a = b = None
            rest = line
        parts = [p.strip() for p in rest.split("|")]
        if len(parts) < 2:
            sp.errors.append(f"«{line}» — tushunilmadi. Namuna: 1-30: Matematika | test 4 | 1")
            continue
        name, kind_s = parts[0] or "Bo'lim", parts[1].lower().split()
        pts = 1.0
        if len(parts) > 2:
            try:
                pts = float(parts[2].replace(",", "."))
            except ValueError:
                sp.errors.append(f"«{line}» — ball raqam bo'lishi kerak")
        kind = kind_s[0] if kind_s else ""
        arg = kind_s[1] if len(kind_s) > 1 else ""
        if kind == "baho":
            try:
                mx = float(arg.replace(",", ".")) if arg else 75.0
            except ValueError:
                mx = 75.0
            sp.sections.append(Section(name=name[:40], kind="baho", mx=mx, pts=1))
            continue
        if kind not in ("test", "yozma"):
            sp.errors.append(f"«{line}» — turi test, yozma yoki baho bo'lishi kerak")
            continue
        if a is None:
            sp.errors.append(f"«{line}» — savollar oralig'ini yozing: 1-30: ...")
            continue
        if a != nxt:
            sp.errors.append(f"«{line}» — savollar {nxt} dan davom etishi kerak edi")
        if b < a:
            sp.errors.append(f"«{line}» — oraliq noto'g'ri")
            continue
        n = b - a + 1
        if kind == "test":
            k = int(arg) if arg.isdigit() else 4
            if not 2 <= k <= 6:
                sp.errors.append(f"«{line}» — variantlar soni 2 dan 6 gacha (A–F)")
                k = max(2, min(6, k))
            sp.sections.append(Section(name=name[:40], kind="test", n=n, opts=k, pts=pts, start=a))
        else:
            mx = int(arg) if arg.isdigit() else 1
            if not 1 <= mx <= 5:
                sp.errors.append(f"«{line}» — yozma savol uchun 1 dan 5 gacha belgi")
                mx = max(1, min(5, mx))
            sp.sections.append(Section(name=name[:40], kind="yozma", n=n, mx=mx, pts=pts, start=a))
        nxt = b + 1
    if not sp.sections:
        sp.errors.append("Birorta bo'lim yo'q")
    if sp.n_questions > 100:
        sp.errors.append(f"Varaqqa ko'pi bilan 100 ta savol sig'adi (hozir {sp.n_questions})")
    if sp.mode == "cefr" and not sp.levels:
        sp.levels = list(CEFR_LEVELS)
    bad = sorted(q for q in sp.topics if not 1 <= q <= sp.n_questions)
    if bad and sp.sections:
        sp.errors.append(f"mavzu: {bad[0]}-savol varaqda yo'q (savollar 1–{sp.n_questions})")
    return sp


def preset(key: str) -> Spec:
    return parse_spec(PRESETS[key][1])


# ---------------- javob kaliti ----------------
def parse_key(text: str, sp: Spec) -> tuple[str | None, str | None]:
    """Kalit: «ABCD…», «1A 2B 3C», «1-a, 2-b», qatorma-qator. Faqat test savollari uchun harf kerak;
    yozma savollar o'rnida '.' saqlanadi. Qaytaradi: (kalit, xato)."""
    t = (text or "").upper().replace("А", "A").replace("В", "B").replace("С", "C").replace("Е", "E")
    pairs = re.findall(r"(\d+)\s*[\).:\-–]?\s*([A-F])\b", t)
    kinds = sp.bubbles()
    tests = [i for i, (k, _) in enumerate(kinds, 1) if k == "t"]
    ans: dict[int, str] = {}
    if len(pairs) >= max(1, len(tests) // 2):
        for q, a in pairs:
            ans[int(q)] = a
    else:
        letters = re.sub(r"[^A-F]", "", t)
        if len(letters) != len(tests):
            return None, (f"Kalitda {len(letters)} ta harf bor, test savollari esa {len(tests)} ta. "
                          "Masalan: ABCDA… yoki 1A 2B 3C …")
        ans = dict(zip(tests, letters))
    missing = [q for q in tests if q not in ans]
    if missing:
        return None, "Javobi yo'q savollar: " + ", ".join(map(str, missing[:15])) + ("…" if len(missing) > 15 else "")
    extra = [q for q in ans if q not in tests]
    if extra and any(1 <= q <= len(kinds) for q in extra):
        return None, ("Bu savollar yozma (ochiq) — ularga harf kerak emas: "
                      + ", ".join(map(str, sorted(q for q in extra if 1 <= q <= len(kinds)))[:15]))
    out = []
    for i, (k, c) in enumerate(kinds, 1):
        if k == "t":
            a = ans[i]
            if LETTERS.index(a) >= c:
                return None, f"{i}-savolda faqat {LETTERS[:c]} bo'lishi mumkin, kalitda {a}"
            out.append(a)
        else:
            out.append(".")
    return "".join(out), None


# ---------------- baholash ----------------
def _interp(x: float, pts: list[tuple[float, float]]) -> float:
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 > x0 else y1
    return pts[-1][1]


def _band(raw: float, n: int, table) -> float:
    r = raw * 40 / n if n else 0
    for lo, b in table:
        if r >= lo - 1e-9:
            return b
    return 0.0


def level_of(sp: Spec, total: float) -> str | None:
    for lo, name in sp.levels:
        if total >= lo - 1e-9:
            return name
    if sp.levels:
        return "Sertifikatsiz" if sp.mode in ("shkala", "rasch") else (sp.levels[-1][1] if sp.levels[-1][0] <= 0 else "—")
    return None


def score(sp: Spec, key: str, answers: str, baho: dict | None = None) -> dict:
    """answers: har savol uchun bitta belgi ('A'..'F', '1'..'5', '-' bo'sh, '*' ikkita, '?' noaniq).
    Qaytaradi: bo'limlar, jami, foiz, daraja, savol bo'yicha to'g'ri/noto'g'ri."""
    baho = baho or {}
    answers = (answers or "").ljust(sp.n_questions, "-")
    per_q: list[float] = []
    secs = []
    for s in sp.sections:
        if s.kind == "test":
            ok = sum(1 for q in range(s.start, s.start + s.n) if answers[q - 1] == key[q - 1])
            raw, rmax = ok, s.n
            per_q += [1.0 if answers[q - 1] == key[q - 1] else 0.0 for q in range(s.start, s.start + s.n)]
            pts = ok * s.pts
        elif s.kind == "yozma":
            vals = [int(answers[q - 1]) if answers[q - 1].isdigit() else 0 for q in range(s.start, s.start + s.n)]
            vals = [min(v, int(s.mx)) for v in vals]
            raw, rmax = sum(vals), s.n * int(s.mx)
            per_q += [v / s.mx for v in vals]
            pts = raw * s.pts
        else:
            v = baho.get(s.name)
            raw = None if v is None else max(0.0, min(float(v), s.mx))
            rmax, pts = s.mx, raw
        secs.append({"name": s.name, "kind": s.kind, "raw": raw, "raw_max": rmax, "score": pts,
                     "max": s.max_score, "n": s.n})
    res = {"sections": secs, "per_q": per_q}
    missing = [x["name"] for x in secs if x["kind"] == "baho" and x["raw"] is None]
    res["missing"] = missing
    if sp.mode == "cefr":
        vals = []
        for x in secs:
            if x["kind"] == "baho":
                v = x["raw"] if x["raw"] is not None else 0.0
                x["scaled"] = round(v * 75 / x["raw_max"], 1) if x["raw_max"] else 0.0
            else:
                x["scaled"] = round(_interp(x["raw"] / x["raw_max"] if x["raw_max"] else 0, CEFR_ANCHORS), 1)
            vals.append(x["scaled"])
        total = round(sum(vals) / len(vals), 1) if vals else 0.0
        res.update(total=total, total_max=75.0)
    elif sp.mode == "ielts":
        bands = []
        for x in secs:
            if x["kind"] == "baho":
                b = x["raw"] if x["raw"] is not None else 0.0
            else:
                tbl = IELTS_R if "read" in x["name"].lower() else IELTS_L
                b = _band(x["raw"], x["raw_max"], tbl)
            x["scaled"] = b
            bands.append(b)
        mean = sum(bands) / len(bands) if bands else 0
        res.update(total=math.floor(mean * 2 + 0.5) / 2, total_max=9.0)
    else:
        prim = sum(x["score"] or 0 for x in secs)
        pmax = sum(x["max"] for x in secs) or 1
        if sp.mode == "foiz":
            res.update(total=round(prim / pmax * 100, 1), total_max=100.0)
        elif sp.mode in ("shkala", "rasch"):        # rasch: yakunda hamma qatnashchilar bilan qayta hisoblanadi
            res.update(total=round(prim / pmax * sp.scale, 1), total_max=sp.scale)
        else:
            res.update(total=round(prim, 2), total_max=round(pmax, 2))
        res["primary"], res["primary_max"] = prim, pmax
    res["percent"] = round(res["total"] / res["total_max"] * 100, 1) if res["total_max"] else 0.0
    res["level"] = level_of(sp, res["total"])
    return res


def fmt_total(sp: Spec, res: dict) -> str:
    t = res["total"]
    if sp.mode == "foiz":
        return f"{t:g}%"
    if sp.mode == "ielts":
        return f"Band {t:g}"
    return f"{t:g} / {res['total_max']:g}"
