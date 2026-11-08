"""Coin qoidalari. Miqdorlar standart — rahbar sozlamalarda o'zgartiradi."""
from __future__ import annotations

DEFAULT_RULES = {
    "attend_k": 1,     # keldi
    "attend_l": 0,     # kechikdi
    "attend_y": -1,    # kelmadi
    "hw_done": 2,
    "hw_part": 1,
    "hw_none": -1,
    "grade_alo": 1,
    "pay_unit": 20_000,
    "pay_coins": 1,
    "pay_bonus": 5,
    "achievement": 10,
}


def attendance_coin(mark: str | None, rules: dict = DEFAULT_RULES) -> int:
    return {"k": rules["attend_k"], "l": rules["attend_l"], "y": rules["attend_y"]}.get(mark or "", 0)


def homework_coin(mark: str | None, rules: dict = DEFAULT_RULES) -> int:
    return {"done": rules["hw_done"], "part": rules["hw_part"], "none": rules["hw_none"]}.get(mark or "", 0)


def change_delta(old: int, new: int) -> int:
    """Belgi o'zgarganda beriladigan farq (masalan, kelmadi → keldi: +2)."""
    return new - old


def payment_coins(paid_before_in_month: int, amount: int, rules: dict = DEFAULT_RULES) -> int:
    """Oy ichidagi to'lovlar qo'shib hisoblanadi, kasr tashlanadi."""
    unit, per = rules["pay_unit"], rules["pay_coins"]
    if unit <= 0 or amount <= 0:
        return 0
    return ((paid_before_in_month + amount) // unit - paid_before_in_month // unit) * per
