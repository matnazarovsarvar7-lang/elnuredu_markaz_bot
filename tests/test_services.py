import asyncio
from datetime import date, time

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Enrollment, Group, Parent, Staff, Student
from app import services as S


def test_phone():
    assert S.norm_phone("90 123 45 67") == "+998901234567"
    assert S.norm_phone("+998 (90) 123-45-67") == "+998901234567"
    assert S.norm_phone("123") is None


@pytest.mark.asyncio
async def test_flow():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    s = async_sessionmaker(eng, expire_on_commit=False)()
    t = Staff(full_name="Ustoz", phone="+998900000001", roles="oqituvchi")
    g = Group(name="Ingliz A1", days="toq", start_time=time(14), teacher=t)
    stu = Student(full_name="Aziza", tier=2)
    par = Parent(phone="+998900000002", children=[stu])
    s.add_all([t, g, stu, par]); await s.flush()
    enr = Enrollment(student=stu, group=g, join_date=date(2026, 11, 16), registered_at=date(2026, 11, 16))
    s.add(enr); await s.flush()

    who = await S.link_by_phone(s, 555, "+998900000002")
    assert who.parent and who.parent.children[0].full_name == "Aziza"
    who = await S.link_by_phone(s, 556, "+998900000001")
    assert who.staff and who.staff.current_role == "oqituvchi"

    st = await S.month_status(s, enr, 2026, 11)
    assert (st.gross, st.debt) == (162_000, 162_000)

    les = await S.get_or_create_lesson(s, g.id, date(2026, 11, 18))
    await S.save_attendance(s, les, {enr.id: "y"}, t)
    assert stu.coins == -1
    await S.save_attendance(s, les, {enr.id: "k"}, t)   # tuzatildi: -1 -> +1
    assert stu.coins == 1
    await S.save_homework(s, les, {enr.id: "done"}, t)
    assert stu.coins == 3

    await S.record_payment(s, enr, 162_000, "naqd", date(2026, 11, 20), "2026-11", t)
    assert stu.coins == 3 + 8          # 162 000 // 20 000 = 8, 5-sanadan keyin — bonus yo'q
    st = await S.month_status(s, enr, 2026, 11)
    assert st.debt == 0

    await S.record_payment(s, enr, 300_000, "naqd", date(2026, 12, 3), "2026-12", t)
    assert stu.coins == 11 + 15 + 5    # dekabr: 15 + 5-sanagacha bonus

    await s.close(); await eng.dispose()


async def test_card_payment_and_debtors():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    s = async_sessionmaker(eng, expire_on_commit=False)()
    boss = Staff(full_name="Rahbar", phone="+998900000009", roles="rahbar,oqituvchi")
    g = Group(name="G", days="juft", teacher=boss)
    a, b = Student(full_name="Ali", tier=1), Student(full_name="Vali", tier=3)
    s.add_all([boss, g, a, b]); await s.flush()
    ea = Enrollment(student=a, group=g, join_date=date(2026, 11, 1), registered_at=date(2026, 11, 8), price_override=200_000)
    eb = Enrollment(student=b, group=g, join_date=date(2026, 11, 1), registered_at=date(2026, 11, 8), price_override=350_000)
    s.add_all([ea, eb]); await s.flush()
    assert len(await S.debtors(s, 2026, 12)) == 2
    pay = await S.record_payment(s, ea, 200_000, "karta", date(2026, 12, 4), "2026-12", None,
                                 status="kutilmoqda", receipt_hash="abc")
    assert a.coins == 0 and len(await S.pending_payments(s)) == 1
    assert await S.duplicate_receipt(s, "abc")
    await S.confirm_payment(s, pay, boss)
    assert a.coins == 10 + 5          # 200 000 / 20 000 = 10, 4-dekabr -> bonus
    left = await S.debtors(s, 2026, 12)
    assert [x.enrollment.student.full_name for x in left] == ["Vali"]
    found = await S.search_students(s, "ali")
    assert {x.full_name for x in found} == {"Ali", "Vali"}
    await s.close(); await eng.dispose()


async def test_student_level_payment_split():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    s = async_sessionmaker(eng, expire_on_commit=False)()
    boss = Staff(full_name="Rahbar", phone="+998900000009", roles="rahbar")
    g1, g2 = Group(name="Fizika", days="juft", teacher=boss), Group(name="Milliy", days="toq", teacher=boss)
    k = Student(full_name="Kamron", tier=3)
    s.add_all([boss, g1, g2, k]); await s.flush()
    for g in (g1, g2):
        s.add(Enrollment(student=k, group=g, join_date=date(2026, 11, 1), registered_at=date(2026, 11, 8),
                         price_override=350_000))
    await s.flush()
    res = await S.record_student_payment(s, k.id, 400_000, "naqd", date(2026, 11, 8), "2026-11", boss)
    assert sorted(p.amount for _, p in res) == [200_000, 200_000]
    sd = await S.student_debt(s, k.id, 2026, 11)
    assert sd.debt == 300_000 and len({p.batch for _, p in res}) == 1
    res = await S.record_student_payment(s, k.id, 300_000, "karta", date(2026, 11, 9), "2026-11", boss,
                                         status="kutilmoqda")
    assert k.coins == 20 and len(await S.batch_payments(s, res[0][1])) == 2
    for p in await S.batch_payments(s, res[0][1]):
        await S.confirm_payment(s, p, boss)
    assert (await S.student_debt(s, k.id, 2026, 11)).debt == 0
    assert k.coins == 35                 # jami 700 000 / 20 000 = 35 (8-noyabr — bonus yo'q)
    assert len(await S.student_debtors(s, 2026, 11)) == 0
    await s.close(); await eng.dispose()


async def test_bonus_once_per_student():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    s = async_sessionmaker(eng, expire_on_commit=False)()
    boss = Staff(full_name="Rahbar", phone="+998900000009", roles="rahbar")
    g1, g2 = Group(name="A", days="juft", teacher=boss), Group(name="B", days="toq", teacher=boss)
    k = Student(full_name="K", tier=3)
    s.add_all([boss, g1, g2, k]); await s.flush()
    for g in (g1, g2):
        s.add(Enrollment(student=k, group=g, join_date=date(2026, 11, 1), registered_at=date(2026, 11, 1),
                         price_override=350_000))
    await s.flush()
    await S.record_student_payment(s, k.id, 350_000, "naqd", date(2026, 12, 2), "2026-12", boss)
    assert k.coins == 17                       # hali to'liq emas — bonus yo'q
    await S.record_student_payment(s, k.id, 350_000, "naqd", date(2026, 12, 4), "2026-12", boss)
    assert k.coins == 17 + 18 + 5               # 700k jami: 35 coin, +5 bitta marta
    await s.close(); await eng.dispose()
