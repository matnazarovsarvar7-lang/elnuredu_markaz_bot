"""Avans va bonus fondi."""
from __future__ import annotations

from datetime import date

from .calendar import days_in_month
from .money import round_to

AVANS_RATIO = 0.40
AVANS_LAST_DAY = 25
AVANS_MAX_PER_MONTH = 2


def earned_so_far(today: date, oklad: int = 0, percent: float = 0, collected_so_far: int = 0) -> int:
    fixed = oklad * today.day / days_in_month(today.year, today.month) if oklad else 0
    share = collected_so_far * percent / 100 if percent else 0
    return round(fixed + share)


def avans_available(today: date, oklad: int = 0, percent: float = 0, collected_so_far: int = 0,
                    taken_this_month: int = 0, requests_this_month: int = 0,
                    ratio: float = AVANS_RATIO, step: int = 10_000) -> int:
    if today.day > AVANS_LAST_DAY or requests_this_month >= AVANS_MAX_PER_MONTH:
        return 0
    limit = earned_so_far(today, oklad, percent, collected_so_far) * ratio - taken_this_month
    if limit <= 0:
        return 0
    return int(limit // step * step)  # pastga yaxlitlanadi — limitdan oshmasin


def bonus_fund(profit_before_bonus: int, share: float = 0.20) -> int:
    return max(0, round(profit_before_bonus * share))


def point_value(fund: int, max_points: int, earned_points: int) -> float:
    """1 ball qiymati = fond ÷ eng ko'p ballar; olingan ballar maksimumdan oshsa — fond ÷ olingan."""
    if fund <= 0 or max_points <= 0:
        return 0.0
    return fund / max(max_points, earned_points)


def penalty_amount(points: int, salary: int, per_point: int = 10_000, cap_ratio: float = 0.10) -> int:
    return min(points * per_point, round(salary * cap_ratio))
