"""Dars kunlari va darslar sonini hisoblash (toq / juft / har kuni)."""
from __future__ import annotations

import calendar as _cal
from datetime import date, timedelta

# Python: dushanba = 0 ... yakshanba = 6
PATTERNS: dict[str, frozenset[int]] = {
    "toq": frozenset({0, 2, 4}),        # Du, Chor, Ju
    "juft": frozenset({1, 3, 5}),       # Se, Pay, Sha
    "har": frozenset({0, 1, 2, 3, 4, 5}),  # Du–Sha
}


def days_in_month(year: int, month: int) -> int:
    return _cal.monthrange(year, month)[1]


def is_lesson_day(d: date, pattern: str, no_lesson: set[date] | frozenset[date] = frozenset()) -> bool:
    return d.weekday() in PATTERNS[pattern] and d not in no_lesson


def lesson_dates(year: int, month: int, pattern: str, from_day: int = 1,
                 no_lesson: set[date] | frozenset[date] = frozenset()) -> list[date]:
    last = days_in_month(year, month)
    return [date(year, month, d) for d in range(max(1, from_day), last + 1)
            if is_lesson_day(date(year, month, d), pattern, no_lesson)]


def lessons_in_month(year: int, month: int, pattern: str, from_day: int = 1,
                     no_lesson: set[date] | frozenset[date] = frozenset()) -> int:
    return len(lesson_dates(year, month, pattern, from_day, no_lesson))


def nth_lesson_on_or_after(start: date, pattern: str, n: int,
                           no_lesson: set[date] | frozenset[date] = frozenset()) -> date:
    """`start` kunidan (shu kun ham kiradi) boshlab n-darsning sanasi."""
    d, seen = start, 0
    for _ in range(400):
        if is_lesson_day(d, pattern, no_lesson):
            seen += 1
            if seen == n:
                return d
        d += timedelta(days=1)
    raise ValueError("dars kuni topilmadi")
