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
