"""To'lov, qarz, chegirma va o'qituvchi ulushi qoidalari (texnik topshiriq bo'yicha)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .calendar import lessons_in_month, nth_lesson_on_or_after

DEFAULT_TIER_PRICES = {1: 200_000, 2: 300_000, 3: 350_000}
FULL_FEE_LAST_JOIN_DAY = 5   # 1–5-sanalarda qo'shilsa to'liq summa
PAY_DEADLINE_DAY = 5         # 5-sanagacha to'lasa bonus
NEW_STUDENT_LESSONS = 3      # yangi o'quvchi 3 dars ichida to'laydi


def tier_of_grade(grade: int) -> int:
    if grade <= 4:
        return 1
    if grade <= 8:
        return 2
    return 3


def round_to(amount: float, step: int = 1000) -> int:
    """Butun so'mga, `step` qadam bilan yaxlitlash (yarmidan yuqoriga)."""
    step = max(1, int(step))
    return int((amount + step / 2) // step * step)


def first_month_due(fee: int, join: date, pattern: str, step: int = 1000) -> int:
    """Qo'shilgan oy uchun to'lov: 1–5-sanada to'liq, keyin qolgan darslar ulushi."""
    if join.day <= FULL_FEE_LAST_JOIN_DAY:
        return fee
    total = lessons_in_month(join.year, join.month, pattern)
    left = lessons_in_month(join.year, join.month, pattern, from_day=join.day)
    if total == 0 or left >= total:
        return fee
    return round_to(fee * left / total, step)


def month_due(fee: int, join: date, year: int, month: int, pattern: str, step: int = 1000) -> int:
    """Berilgan oy uchun chegirmagacha bo'lgan summa."""
    if (year, month) < (join.year, join.month):
        return 0
    if (year, month) == (join.year, join.month):
        return first_month_due(fee, join, pattern, step)
    return fee


@dataclass(frozen=True)
class Discount:
    kind: str = "none"   # none | markaz | umumiy | ijtimoiy
    percent: float = 0   # 0–100 (summa chegirmasi oylik narxga nisbatan foizga aylantiriladi)

    def payable(self, gross: int, step: int = 1000) -> int:
        if self.kind == "ijtimoiy":
            return 0
        if self.kind == "none" or not self.percent:
            return gross
        return round_to(gross * (100 - self.percent) / 100, step)


def teacher_share_base(gross: int, paid: int, discount: Discount) -> int:
    """O'qituvchi foizi hisoblanadigan summa.

    - chegirmasiz / umumiy chegirma: haqiqatda to'langan summa;
    - markaz chegirmasi: to'liq narxdan, to'langan ulush nisbatida;
    - ijtimoiy: to'liq narx (to'lagan o'quvchidek).
    """
    if discount.kind == "ijtimoiy":
        return gross
    if discount.kind == "markaz" and discount.percent:
        payable = discount.payable(gross)
        if payable <= 0:
            return gross
        return round(gross * min(paid, payable) / payable)
    return paid


def new_student_deadline(registered: date, pattern: str) -> date:
    """Yangi o'quvchi to'lov muddati: botga kiritilgan kundan 3-dars.
    3-dars keyingi oyga tushsa — keyingi oyning 5-sanasi."""
    third = nth_lesson_on_or_after(registered, pattern, NEW_STUDENT_LESSONS)
    if (third.year, third.month) != (registered.year, registered.month):
        return date(third.year, third.month, PAY_DEADLINE_DAY)
    return third


def is_market_debtor(today: date, due: int, paid: int, new_student_deadline_date: date | None = None) -> bool:
    """Marketdan xarid qila olmaydimi: oddiy o'quvchi 6-sanadan, yangi o'quvchi muddatidan keyin."""
    if paid >= due:
        return False
    if new_student_deadline_date and today <= new_student_deadline_date:
        return False
    return today.day > PAY_DEADLINE_DAY


def reminder_day(day: int) -> bool:
    """Qarz eslatmasi yuboriladigan kunmi: 5; 8, 11, 14; 16-dan oy oxirigacha har kuni."""
    return day == 5 or day in (8, 11, 14) or day >= 16


def split_payment(amount: int, debts: list[int], step: int = 1000) -> list[int]:
    """Umumiy to'lovni guruhlar qarziga mutanosib taqsimlash (qarzlar teng bo'lsa — teng).
    Har guruhga qarzidan ko'p tushmaydi; ortiqchasi oxirgi guruhga (keyingi oy hisobiga) qoladi."""
    if not debts:
        return []
    total = sum(debts)
    if total <= 0:
        out = [0] * len(debts)
        out[-1] = amount
        return out
    if amount >= total:
        out = list(debts)
        out[-1] += amount - total
        return out
    out = [min(d, int(amount * d / total // step * step)) for d in debts]
    rest = amount - sum(out)
    for i in sorted(range(len(debts)), key=lambda i: debts[i] - out[i], reverse=True):  # qoldiqni taqsimlash
        add = min(rest, debts[i] - out[i])
        out[i] += add
        rest -= add
    out[-1] += rest
    return out
