from datetime import date

from app.logic.calendar import lessons_in_month
from app.logic.money import (Discount, first_month_due, is_market_debtor, month_due,
                             new_student_deadline, reminder_day, teacher_share_base, tier_of_grade)
from app.logic.coins import attendance_coin, change_delta, payment_coins
from app.logic.salary import avans_available, bonus_fund, penalty_amount, point_value


def test_lessons_november_2026():
    assert lessons_in_month(2026, 11, "toq") == 13
    assert lessons_in_month(2026, 11, "toq", from_day=16) == 7
    assert lessons_in_month(2026, 11, "juft") == 12


def test_first_month_due():
    assert first_month_due(300_000, date(2026, 11, 16), "toq") == 162_000
    assert first_month_due(300_000, date(2026, 11, 5), "toq") == 300_000   # 1–5: to'liq
    assert first_month_due(300_000, date(2026, 11, 6), "toq") < 300_000    # 6: to'liq emas
    assert month_due(300_000, date(2026, 11, 16), 2026, 12, "toq") == 300_000
    assert month_due(300_000, date(2026, 11, 16), 2026, 10, "toq") == 0


def test_tiers():
    assert [tier_of_grade(g) for g in (1, 4, 5, 8, 9, 11)] == [1, 1, 2, 2, 3, 3]


def test_discounts_and_teacher_base():
    full = 300_000
    markaz = Discount("markaz", 50)
    assert markaz.payable(full) == 150_000
    assert teacher_share_base(full, 150_000, markaz) == 300_000
    assert teacher_share_base(full, 75_000, markaz) == 150_000
    umumiy = Discount("umumiy", 50)
    assert teacher_share_base(full, 150_000, umumiy) == 150_000
    ijt = Discount("ijtimoiy", 100)
    assert ijt.payable(full) == 0 and teacher_share_base(full, 0, ijt) == 300_000


def test_new_student_deadline():
    # 24-noyabr (se): toq darslar 25, 27, 30-noyabr — 3-dars shu oyda
    assert new_student_deadline(date(2026, 11, 24), "toq") == date(2026, 11, 30)
    # 26-noyabr: toq darslar 27, 30, 2-dekabr -> keyingi oy -> 5-dekabr
    assert new_student_deadline(date(2026, 11, 26), "toq") == date(2026, 12, 5)


def test_market_debtor_and_reminders():
    assert not is_market_debtor(date(2026, 11, 5), 300_000, 0)
    assert is_market_debtor(date(2026, 11, 6), 300_000, 0)
    assert not is_market_debtor(date(2026, 11, 20), 300_000, 300_000)
    assert not is_market_debtor(date(2026, 11, 26), 300_000, 0, date(2026, 12, 5))
    assert [d for d in range(1, 31) if reminder_day(d)] == [5, 8, 11, 14] + list(range(16, 31))


def test_coins():
    assert payment_coins(0, 300_000) == 15
    assert payment_coins(0, 150_000) == 7
    assert payment_coins(10_000, 10_000) == 1
    assert attendance_coin("y") == -1 and change_delta(attendance_coin("y"), attendance_coin("k")) == 2


def test_salary():
    assert avans_available(date(2026, 11, 15), oklad=3_000_000) == 600_000
    assert avans_available(date(2026, 11, 15), oklad=3_000_000, percent=30,
                           collected_so_far=8_000_000, taken_this_month=500_000) == 1_060_000
    assert avans_available(date(2026, 11, 26), oklad=3_000_000) == 0
    assert avans_available(date(2026, 11, 15), oklad=3_000_000, requests_this_month=2) == 0
    assert bonus_fund(4_600_000) == 920_000 and bonus_fund(-100) == 0
    assert round(point_value(920_000, 105, 50)) == 8762
    assert point_value(920_000, 105, 140) == 920_000 / 140
    assert penalty_amount(5, 700_000) == 50_000 and penalty_amount(20, 700_000) == 70_000
