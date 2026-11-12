"""Har bir o'quvchi uchun varaq: variant, savollar tartibi, javoblar tartibi, kalit; tekshirish."""
from __future__ import annotations

import random
from dataclasses import dataclass

LETTERS = "ABCD"


@dataclass
class Paper:
    variant: str
    qids: list[int]          # varaqdagi tartib bo'yicha savol id lari
    perms: list[list[int]]   # har savol uchun: varaqdagi A,B,C,D -> asl javob indeksi
    key: str                 # varaqdagi to'g'ri javob harflari


def make_paper(variant: str, questions: list, seed: int) -> Paper:
    """questions: shu variant savollari (qid, answer atributlari bilan)."""
    rnd = random.Random(seed)
    qs = list(questions)
    rnd.shuffle(qs)
    perms, key = [], []
    for q in qs:
        p = [0, 1, 2, 3]
        rnd.shuffle(p)
        perms.append(p)
        key.append(LETTERS[p.index(LETTERS.index(q.answer))])
    return Paper(variant, [q.qid for q in qs], perms, "".join(key))


def assign_variants(n_students: int, n_variants: int) -> list[str]:
    """Ro'yxat tartibida navbat bilan: A, B, C, D, A, ... (yonma-yon o'tirganlar har xil)."""
    return [LETTERS[i % n_variants] for i in range(n_students)]


def clean_answers(raw: str, n: int) -> str | None:
    """'ABDCA BBC..' -> 'ABDCABBC..'. Bo'sh javob: '-' yoki '0'. Uzunlik mos kelmasa — None."""
    s = "".join(ch for ch in raw.upper() if ch in "ABCD-0*?")
    s = s.replace("0", "-")
    return s if len(s) == n else None


def score(answers: str, key: str) -> int:
    return sum(1 for a, k in zip(answers, key) if a == k)


# ================= Bank rejimi: har bir o'quvchiga shaxsiy savollar to'plami =================
# Talab: har o'quvchiga n ta savol; istalgan ikki o'quvchida ko'pi bilan k ta bir xil savol.
# Bank hajmining matematik pastki chegarasi: jami o'rinlar T = N·n, juftliklar P = N(N−1)/2,
# ruxsat etilgan takrorlar R = k·P. Savol r ta o'quvchiga tushsa C(r,2) takror beradi;
# Σ C(r_i,2) ≤ R shartini qanoatlantiradigan eng kichik M — chegara. Amalda +12% zaxira.
import math

BANK_MARGIN = 1.12
LEVEL_SHARE = {"oson": 0.5, "orta": 0.3, "qiyin": 0.2}


def overlap_limit(n: int) -> int:
    """Ikki o'quvchidagi bir xil savollar chegarasi: savollar sonining 1/6 qismi (30 → 5)."""
    return max(1, round(n / 6))


def bank_min(n_students: int, n: int, k: int) -> int:
    if n_students <= 1:
        return n
    total, allowed = n_students * n, k * n_students * (n_students - 1) // 2
    m = n
    while True:
        lo, extra = divmod(total, m)
        if lo == 0:
            return m
        if (m - extra) * lo * (lo - 1) // 2 + extra * (lo + 1) * lo // 2 <= allowed:
            return m
        m += 1


def bank_need(n_students: int, n: int, k: int | None = None) -> dict:
    """Tavsiya etilgan bank hajmi va darajalar bo'yicha bo'linishi."""
    k = overlap_limit(n) if k is None else k
    total = max(n, math.ceil(bank_min(n_students, n, k) * BANK_MARGIN))
    oson = round(total * LEVEL_SHARE["oson"])
    orta = round(total * LEVEL_SHARE["orta"])
    return {"total": total, "oson": oson, "orta": orta, "qiyin": total - oson - orta, "k": k,
            "min": bank_min(n_students, n, k)}


def level_quotas(n: int, counts: dict[str, int]) -> dict[str, int]:
    """Bir o'quvchiga har darajadan nechta: bankdagi nisbatga mos (odatda 50/30/20)."""
    tot = sum(counts.values())
    q = {L: math.floor(n * c / tot) for L, c in counts.items()}
    for L in sorted(counts, key=lambda L: -(n * counts[L] / tot - q[L]))[:n - sum(q.values())]:
        q[L] += 1
    return q


def assign_bank(levels: dict[int, str], n_students: int, n: int, k: int, seed: int = 1,
                iters: int = 80000) -> list[list[int]] | None:
    """Har o'quvchiga n ta savol (darajalar nisbati saqlanadi), istalgan ikki o'quvchida ≤ k ta umumiy.
    Muvozanatli boshlang'ich taqsimot + mahalliy qidiruv (buzilgan juftliklarni tuzatish). Topilmasa — None."""
    by: dict[str, list[int]] = {}
    for qid, L in levels.items():
        by.setdefault(L, []).append(qid)
    quota = level_quotas(n, {L: len(v) for L, v in by.items()})
    if any(quota[L] > len(by[L]) for L in quota):
        return None
    rnd = random.Random(seed)
    holders: dict[int, set[int]] = {q: set() for q in levels}
    sets: list[set[int]] = []
    for s in range(n_students):
        mine: set[int] = set()
        for L, need in quota.items():
            mine.update(sorted(by[L], key=lambda q: (len(holders[q]), rnd.random()))[:need])
        for q in mine:
            holders[q].add(s)
        sets.append(mine)
    N = n_students
    ov = [[0] * N for _ in range(N)]
    for h in holders.values():
        h = list(h)
        for i in range(len(h)):
            for j in range(i + 1, len(h)):
                ov[h[i]][h[j]] += 1
                ov[h[j]][h[i]] += 1
    viol = sum(max(0, ov[a][b] - k) for a in range(N) for b in range(a + 1, N))
    it = 0
    while viol and it < iters:
        it += 1
        bad = [(a, b) for a in range(N) for b in range(a + 1, N) if ov[a][b] > k]
        a, b = rnd.choice(bad)
        s, o = (a, b) if rnd.random() < 0.5 else (b, a)
        q = rnd.choice([x for x in sets[s] if o in holders[x]])
        cands = [x for x in by[levels[q]] if x not in sets[s]]
        rnd.shuffle(cands)
        best, bkey = None, None
        for q2 in cands[:60]:
            d = -sum(1 for t in holders[q] if t != s and ov[s][t] > k) + sum(1 for t in holders[q2] if ov[s][t] >= k)
            key = (d, len(holders[q2]))
            if bkey is None or key < bkey:
                best, bkey = q2, key
        if best is None or (bkey[0] > 0 and rnd.random() > 0.05):
            continue
        for t in holders[q]:
            if t != s:
                viol -= ov[s][t] > k
                ov[s][t] -= 1
                ov[t][s] -= 1
        for t in holders[best]:
            viol += ov[s][t] >= k
            ov[s][t] += 1
            ov[t][s] += 1
        holders[q].discard(s)
        holders[best].add(s)
        sets[s].discard(q)
        sets[s].add(best)
    return None if viol else [sorted(x) for x in sets]


def max_overlap(sets: list[list[int]]) -> int:
    m = 0
    for a in range(len(sets)):
        sa = set(sets[a])
        for b in range(a + 1, len(sets)):
            m = max(m, len(sa & set(sets[b])))
    return m
