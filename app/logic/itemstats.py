"""Savollar va mavzular tahlili (item analysis) — sof mantiq.

Kirish: har bir qatnashchining javoblari (savol kaliti → to'g'rimi, qaysi variantni tanlagan) va savollar haqida
ma'lumot (raqam/belgi, mavzu, qiyinlik, to'g'ri javob). Chiqish: savollar qiyinligi (to'g'ri ishlaganlar foizi),
ajratish kuchi (kuchli va kuchsiz o'quvchilar farqi), eng ko'p tanlangan xato javob, mavzular kesimi va har bir
o'quvchining mavzular bo'yicha natijasi.
"""
from __future__ import annotations

from dataclasses import dataclass, field

OTHER = "Boshqa"
MIN_ITEM_N = 3          # savol statistikasi uchun kamida shuncha javob
MIN_TOPIC_Q = 3         # mavzu xulosasi uchun kamida shuncha savol


@dataclass
class QMeta:
    label: str                 # varaqdagi raqam («17») yoki bank belgisi («#57»)
    topic: str = ""
    level: str = ""
    answer: str = ""           # to'g'ri variant harfi (asl tartibda)
    text: str = ""


@dataclass
class Resp:
    key: int | str             # o'quvchi/qatnashchi
    name: str
    items: list[tuple[str, float, str | None]]   # (savol kaliti, to'g'rilik 0..1, tanlangan harf yoki None)


@dataclass
class ItemStat:
    key: str
    label: str
    topic: str
    level: str
    n: int
    p: float                    # to'g'ri ishlaganlar ulushi 0..1
    disc: float | None          # ajratish kuchi: yuqori 27% − quyi 27% (kam javobda None)
    trap: str | None = None     # eng ko'p tanlangan xato harf
    trap_share: float = 0.0
    blank: float = 0.0          # javob bermaganlar ulushi

    @property
    def suspicious(self) -> bool:
        """Kuchli o'quvchilar kuchsizlardan yomon ishlagan — odatda kalit xatosi yoki noaniq savol."""
        return self.disc is not None and self.disc < -0.05


@dataclass
class TopicStat:
    topic: str
    ok: float
    n: int                      # javoblar soni (savol × o'quvchi)
    n_q: int                    # mavzudagi savollar soni

    @property
    def pct(self) -> float:
        return round(100 * self.ok / self.n, 1) if self.n else 0.0

    @property
    def reliable(self) -> bool:
        return self.n_q >= MIN_TOPIC_Q


@dataclass
class Report:
    n: int                                   # qatnashchilar
    mean: float                              # o'rtacha foiz
    items: list[ItemStat]                    # p bo'yicha kamayish tartibida
    topics: list[TopicStat]                  # foiz bo'yicha kamayish tartibida
    levels: list[TopicStat]
    students: dict = field(default_factory=dict)   # key -> {"pct": .., "topics": {t: [ok, n]}}

    @property
    def has_topics(self) -> bool:
        return any(t.topic != OTHER for t in self.topics)


def analyze(resps: list[Resp], meta: dict[str, QMeta]) -> Report:
    resps = [r for r in resps if r.items]
    totals = {r.key: (sum(ok for _, ok, _ in r.items) / len(r.items)) for r in resps}
    order = sorted(resps, key=lambda r: totals[r.key])
    g = max(1, round(len(order) * 0.27))
    low = {r.key for r in order[:g]}
    high = {r.key for r in order[-g:]}
    per: dict[str, list[tuple[float, str | None, int | str]]] = {}
    for r in resps:
        for k, ok, ch in r.items:
            per.setdefault(k, []).append((ok, ch, r.key))
    items = []
    for k, xs in per.items():
        m = meta.get(k) or QMeta(label=str(k))
        n = len(xs)
        p = sum(ok for ok, _, _ in xs) / n
        hi = [ok for ok, _, who in xs if who in high]
        lo = [ok for ok, _, who in xs if who in low]
        disc = (sum(hi) / len(hi) - sum(lo) / len(lo)) if len(xs) >= 8 and hi and lo else None
        wrong: dict[str, int] = {}
        blank = 0
        for ok, ch, _ in xs:
            if ok >= 1:
                continue
            if ch and ch in "ABCD" and ch != m.answer:
                wrong[ch] = wrong.get(ch, 0) + 1
            elif not ch or ch not in "ABCD":
                blank += 1
        trap = max(wrong, key=wrong.get) if wrong else None
        items.append(ItemStat(k, m.label, m.topic or OTHER, m.level, n, round(p, 4),
                              round(disc, 3) if disc is not None else None,
                              trap, round(wrong.get(trap, 0) / n, 3) if trap else 0.0, round(blank / n, 3)))
    items.sort(key=lambda x: (-x.p, _num(x.label)))

    def group(attr: str) -> list[TopicStat]:
        acc: dict[str, list] = {}
        for r in resps:
            for k, ok, _ in r.items:
                m = meta.get(k)
                t = (getattr(m, attr) if m else "") or (OTHER if attr == "topic" else "")
                if not t:
                    continue
                a = acc.setdefault(t, [0.0, 0, set()])
                a[0] += ok
                a[1] += 1
                a[2].add(k)
        out = [TopicStat(t, a[0], a[1], len(a[2])) for t, a in acc.items()]
        return sorted(out, key=lambda x: -x.pct)

    students = {}
    for r in resps:
        tp: dict[str, list] = {}
        for k, ok, _ in r.items:
            m = meta.get(k)
            t = (m.topic if m else "") or OTHER
            a = tp.setdefault(t, [0.0, 0])
            a[0] += ok
            a[1] += 1
        students[r.key] = {"name": r.name, "pct": round(100 * totals[r.key], 1),
                           "topics": {t: [round(a[0], 2), a[1]] for t, a in tp.items()}}
    mean = round(100 * sum(totals.values()) / len(totals), 1) if totals else 0.0
    lv = sorted(group("level"), key=lambda x: ["oson", "orta", "qiyin"].index(x.topic)
                if x.topic in ("oson", "orta", "qiyin") else 9)
    return Report(len(resps), mean, items, group("topic"), lv, students)


def _num(label: str) -> float:
    try:
        return float(str(label).lstrip("#"))
    except ValueError:
        return 1e9


def zone(pct: float) -> str:
    return "kuchli" if pct >= 80 else ("orta" if pct >= 50 else "zaif")


def strong_weak(topics: dict[str, list], min_n: int = 2) -> tuple[list[str], list[str]]:
    """O'quvchi uchun: kuchli (≥80%) va takrorlash kerak (<50%) mavzular; juda kam savolli mavzular chetda."""
    s, w = [], []
    for t, (ok, n) in sorted(topics.items(), key=lambda kv: -(kv[1][0] / kv[1][1] if kv[1][1] else 0)):
        if t == OTHER or n < min_n:
            continue
        p = 100 * ok / n
        if p >= 80:
            s.append(t)
        elif p < 50:
            w.append(t)
    return s, w
