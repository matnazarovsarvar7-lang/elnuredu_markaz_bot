"""Bot va baza orasidagi asosiy amallar (handlerlardan mustaqil, test qilinadi)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (Attendance, AuditLog, CoinTx, Enrollment, Group, Lesson, Parent, Payment,
                           Setting, Staff, Student)
from app.logic import coins as C
from app.logic.calendar import is_lesson_day
from app.logic.money import round_to, DEFAULT_TIER_PRICES, Discount, month_due

ROLE_TITLES = {"rahbar": "Rahbar", "moliya": "Moliya", "admin": "Admin",
               "oqituvchi": "O'qituvchi", "yordamchi": "Yordamchi o'qituvchi"}


def norm_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    d = re.sub(r"\D", "", raw)
    if len(d) == 9:
        d = "998" + d
    if len(d) != 12 or not d.startswith("998"):
        return None
    return "+" + d


def fmt_som(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " so'm"


async def get_rules(s: AsyncSession) -> dict:
    row = await s.get(Setting, "coin_rules")
    return {**C.DEFAULT_RULES, **(row.value if row else {})}


async def tier_price_history(s: AsyncSession) -> list[dict]:
    """[{"from": "2026-01", "prices": {"1": 200000, ...}}, ...] — eskidan yangiga."""
    row = await s.get(Setting, "tier_prices")
    hist = (row.value or {}).get("history") if row else None
    return hist or [{"from": "2000-01", "prices": {str(k): v for k, v in DEFAULT_TIER_PRICES.items()}}]


async def get_tier_prices(s: AsyncSession, year: int | None = None, month: int | None = None) -> dict[int, int]:
    """Berilgan oy uchun amaldagi toifa narxlari (narx o'zgarsa o'tgan oylar eski narxda qoladi)."""
    hist = await tier_price_history(s)
    key = f"{year:04d}-{month:02d}" if year else "9999-12"
    cur = hist[0]
    for h in hist:
        if h["from"] <= key:
            cur = h
    return {int(k): v for k, v in cur["prices"].items()}


async def set_tier_price(s: AsyncSession, tier: int, price: int, from_month: str, by: Staff | None) -> None:
    hist = await tier_price_history(s)
    base = dict(hist[-1]["prices"])
    base[str(tier)] = price
    hist = [h for h in hist if h["from"] != from_month] + [{"from": from_month, "prices": base}]
    hist.sort(key=lambda h: h["from"])
    row = await s.get(Setting, "tier_prices") or Setting(key="tier_prices", value={})
    row.value = {"history": hist}
    s.add(row)
    await audit(s, by, "narx_toifa", tier=tier, price=price, from_month=from_month)


async def audit(s: AsyncSession, actor: Staff | None, action: str, /, **details) -> None:
    s.add(AuditLog(staff_id=actor.id if actor else None, role=actor.current_role if actor else None,
                   action=action, details=details))


# ---------- kim kirdi ----------
@dataclass
class Who:
    staff: Staff | None = None
    parent: Parent | None = None
    student: Student | None = None


async def who_by_tg(s: AsyncSession, tg_id: int) -> Who:
    st = (await s.execute(select(Staff).where(Staff.tg_id == tg_id, Staff.active))).scalar_one_or_none()
    if st:
        return Who(staff=st)
    p = (await s.execute(select(Parent).where(Parent.tg_id == tg_id)
                         .options(selectinload(Parent.children)))).scalar_one_or_none()
    if p:
        return Who(parent=p)
    stu = (await s.execute(select(Student).where(Student.tg_id == tg_id))).scalar_one_or_none()
    return Who(student=stu)


async def link_by_phone(s: AsyncSession, tg_id: int, phone: str) -> Who:
    """Telefon raqam bo'yicha topib, Telegram akkauntiga bog'laydi.
    Ma'lumot kartada qoladi — yangi akkauntdan kirilsa ham hammasi qaytadi."""
    st = (await s.execute(select(Staff).where(Staff.phone == phone, Staff.active))).scalar_one_or_none()
    if st:
        st.tg_id = tg_id
        if not st.current_role and st.role_list():
            st.current_role = st.role_list()[0]
        return Who(staff=st)
    p = (await s.execute(select(Parent).where(Parent.phone == phone)
                         .options(selectinload(Parent.children)))).scalar_one_or_none()
    if p:
        p.tg_id, p.bot_blocked_at = tg_id, None
        return Who(parent=p)
    stu = (await s.execute(select(Student).where(Student.phone == phone))).scalar_one_or_none()
    if stu:
        stu.tg_id, stu.bot_blocked_at = tg_id, None
        return Who(student=stu)
    return Who()


# ---------- guruh va darslar ----------
async def teacher_groups(s: AsyncSession, staff: Staff, role: str) -> list[Group]:
    q = select(Group).where(Group.active)
    if role == "yordamchi":
        q = q.where(Group.assistant_id == staff.id)
    elif role != "rahbar":
        q = q.where(Group.teacher_id == staff.id)
    return list((await s.execute(q.order_by(Group.start_time))).scalars())


def todays_groups(groups: list[Group], today: date) -> list[Group]:
    return [g for g in groups if is_lesson_day(today, g.days)]


async def group_roster(s: AsyncSession, group_id: int, on: date) -> list[Enrollment]:
    """Shu kuni darsga kelishi kerak bo'lganlar: faol, qo'shilgan, muzlatilmagan (guruh to'xtatilmagan)."""
    from app.db.models import Freeze
    if await group_paused(s, group_id, on):
        return []
    q = (select(Enrollment).join(Student)
         .where(Enrollment.group_id == group_id, Enrollment.active, Enrollment.join_date <= on,
                Student.status != "ketgan")
         .options(selectinload(Enrollment.student)).order_by(Student.full_name))
    rows = list((await s.execute(q)).scalars())
    frozen = set((await s.execute(select(Freeze.enrollment_id).where(
        Freeze.enrollment_id.in_([e.id for e in rows] or [0]), Freeze.start <= on, Freeze.end >= on))).scalars())
    return [e for e in rows if e.id not in frozen]


async def get_or_create_lesson(s: AsyncSession, group_id: int, on: date) -> Lesson:
    les = (await s.execute(select(Lesson).where(Lesson.group_id == group_id, Lesson.date == on))).scalar_one_or_none()
    if not les:
        les = Lesson(group_id=group_id, date=on)
        s.add(les)
        await s.flush()
    return les


async def add_coins(s: AsyncSession, student: Student, amount: int, kind: str, reason: str,
                    by: Staff | None = None) -> None:
    if not amount:
        return
    student.coins += amount
    s.add(CoinTx(student_id=student.id, amount=amount, kind=kind, reason=reason,
                 by_staff_id=by.id if by else None))


async def save_attendance(s: AsyncSession, lesson: Lesson, marks: dict[int, str], by: Staff) -> dict[str, int]:
    """marks: {enrollment_id: 'k'|'l'|'y'}. Coinlar farq bo'yicha beriladi (qayta saqlansa ham to'g'ri)."""
    rules = await get_rules(s)
    counts = {"k": 0, "l": 0, "y": 0}
    for enr_id, mark in marks.items():
        att = (await s.execute(select(Attendance).where(Attendance.lesson_id == lesson.id,
                                                        Attendance.enrollment_id == enr_id))).scalar_one_or_none()
        old = att.mark if att else None
        if not att:
            att = Attendance(lesson_id=lesson.id, enrollment_id=enr_id)
            s.add(att)
        att.mark = mark
        counts[mark] += 1
        delta = C.attendance_coin(mark, rules) - C.attendance_coin(old, rules)
        if delta:
            enr = await s.get(Enrollment, enr_id, options=[selectinload(Enrollment.student)])
            if enr.student.status == "faol" and await coins_allowed(s, enr.student, lesson.date):
                await add_coins(s, enr.student, delta, "davomat", f"Davomat {lesson.date:%d.%m}", by)
    lesson.confirmed_at = lesson.confirmed_at or datetime.now()
    await audit(s, by, "davomat", lesson_id=lesson.id, **counts)
    return counts


async def save_homework(s: AsyncSession, lesson: Lesson, marks: dict[int, str], by: Staff) -> None:
    """Kelmaganlarga vazifa belgilanmaydi."""
    rules = await get_rules(s)
    for enr_id, hw in marks.items():
        att = (await s.execute(select(Attendance).where(Attendance.lesson_id == lesson.id,
                                                        Attendance.enrollment_id == enr_id))).scalar_one_or_none()
        if not att or att.mark == "y":
            continue
        delta = C.homework_coin(hw, rules) - C.homework_coin(att.homework, rules)
        att.homework = hw
        if delta:
            enr = await s.get(Enrollment, enr_id, options=[selectinload(Enrollment.student)])
            if await coins_allowed(s, enr.student, lesson.date):
                await add_coins(s, enr.student, delta, "vazifa", f"Vazifa {lesson.date:%d.%m}", by)
    await audit(s, by, "vazifa", lesson_id=lesson.id)


GRADES = ("alo", "yaxshi", "qoniqarli", "yomon")
GRADE_NAMES = {"alo": "A'lo", "yaxshi": "Yaxshi", "qoniqarli": "Qoniqarli", "yomon": "Yomon"}


async def save_grades(s: AsyncSession, lesson: Lesson, marks: dict[int, str | None], by: Staff) -> list[tuple[int, str]]:
    """Baholarni saqlaydi; «a'lo» uchun coin (qayta o'zgartirilsa farqi hisoblanadi). Yangi qo'yilgan baholarni qaytaradi."""
    rules = await get_rules(s)
    alo = int(rules.get("grade_alo", 1))
    changed = []
    for enr_id, g in marks.items():
        att = (await s.execute(select(Attendance).where(Attendance.lesson_id == lesson.id,
                                                        Attendance.enrollment_id == enr_id))).scalar_one_or_none()
        if not att or att.mark == "y" or att.grade == g:
            continue
        delta = (alo if g == "alo" else 0) - (alo if att.grade == "alo" else 0)
        att.grade = g
        enr = await s.get(Enrollment, enr_id, options=[selectinload(Enrollment.student)])
        if delta and await coins_allowed(s, enr.student, lesson.date):
            await add_coins(s, enr.student, delta, "baho", f"Baho {lesson.date:%d.%m}", by)
        if g:
            changed.append((enr.student_id, g))
    await audit(s, by, "baho", lesson_id=lesson.id)
    return changed


# ---------- to'lov va qarz ----------
async def enrollment_fee(s: AsyncSession, enr: Enrollment, year: int | None = None, month: int | None = None) -> int:
    if enr.price_override:
        return enr.price_override
    prices = await get_tier_prices(s, year, month)
    return prices[enr.tier or enr.student.tier]


async def paid_for_month(s: AsyncSession, enr_id: int, month: str) -> int:
    rows = (await s.execute(select(Payment.amount).where(Payment.enrollment_id == enr_id, Payment.month == month,
                                                         Payment.status == "tasdiq"))).scalars()
    return sum(rows)


@dataclass
class MonthStatus:
    enrollment: Enrollment
    gross: int
    payable: int
    paid: int

    @property
    def debt(self) -> int:
        return max(0, self.payable - self.paid)


async def freezes_for(s: AsyncSession, enr: Enrollment, only_fee: bool = False) -> list:
    """Shu o'quvchi-guruhga ta'sir qiladigan muzlatishlar (o'zi yoki butun guruh)."""
    from app.db.models import Freeze
    q = select(Freeze).where((Freeze.enrollment_id == enr.id) | (Freeze.group_id == enr.group_id))
    if only_fee:
        q = q.where(Freeze.recalc.in_(("auto", "tasdiq")))
    return list((await s.execute(q)).scalars())


async def is_frozen(s: AsyncSession, enr: Enrollment, d: date) -> bool:
    return any(f.start <= d <= f.end for f in await freezes_for(s, enr))


async def group_paused(s: AsyncSession, group_id: int, d: date) -> bool:
    from app.db.models import Freeze
    return (await s.execute(select(Freeze.id).where(Freeze.group_id == group_id, Freeze.start <= d,
                                                    Freeze.end >= d))).first() is not None


def _frozen_share(enr: Enrollment, year: int, month: int, periods: list, until: date | None = None) -> tuple[int, int]:
    """(hisobga kiradigan darslar, oydagi darslar) — qo'shilgan sanadan, muzlatilganlarsiz, ketgan sanagacha."""
    from app.logic.calendar import lesson_dates
    start = enr.join_date.day if (year, month) == (enr.join_date.year, enr.join_date.month) else 1
    days = lesson_dates(year, month, enr.group.days, from_day=start)
    count = [d for d in days if not any(p.start <= d <= p.end for p in periods) and (until is None or d <= until)]
    return len(count), len(days)


def discount_pct(enr: Enrollment, fee: int) -> float:
    """Chegirma foizi. Summa bilan berilgan bo'lsa — oylik narxga nisbatan (to'liq oyda aynan shu summa ayiriladi,
    oy chala bo'lsa — mutanosib)."""
    if enr.discount_kind in ("markaz", "umumiy") and enr.discount_amount:
        return min(100.0, enr.discount_amount * 100 / fee) if fee else 0
    return enr.discount_percent or 0


def discount_label(enr: Enrollment) -> str:
    if enr.discount_kind == "none":
        return ""
    if enr.discount_kind == "ijtimoiy":
        return "ijtimoiy (100%)"
    name = {"markaz": "markaz hisobidan", "umumiy": "umumiy"}.get(enr.discount_kind, enr.discount_kind)
    return f"{name} −{fmt_som(enr.discount_amount)}" if enr.discount_amount else f"{name} {enr.discount_percent}%"


async def month_status(s: AsyncSession, enr: Enrollment, year: int, month: int) -> MonthStatus:
    fee = await enrollment_fee(s, enr, year, month)
    gross = month_due(fee, enr.join_date, year, month, enr.group.days)
    periods = await freezes_for(s, enr, only_fee=True)
    until = enr.left_date if enr.left_date and (enr.left_date.year, enr.left_date.month) == (year, month) else None
    if gross and (periods or until):
        keep, total = _frozen_share(enr, year, month, periods, until)
        if total and keep < total:
            gross = round_to(gross * keep / total)
    payable = Discount(enr.discount_kind, discount_pct(enr, fee)).payable(gross)
    paid = await paid_for_month(s, enr.id, f"{year:04d}-{month:02d}")
    return MonthStatus(enr, gross, payable, paid)


async def has_closed_month_debt(s: AsyncSession, student: Student, today: date) -> bool:
    """Oldingi oylardan qarz bormi (oy qarz bilan yopilgan bo'lsa coin berilmaydi)."""
    for enr in student.enrollments if student.enrollments else []:
        if not enr.active:
            continue
        y, m = enr.join_date.year, enr.join_date.month
        while (y, m) < (today.year, today.month):
            st = await month_status(s, enr, y, m)
            if st.debt > 0:
                return True
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return False


async def coins_allowed(s: AsyncSession, student: Student, on: date) -> bool:
    await s.refresh(student, ["enrollments"])
    for e in student.enrollments:
        await s.refresh(e, ["group", "student"])
    return not await has_closed_month_debt(s, student, on)


async def record_payment(s: AsyncSession, enr: Enrollment, amount: int, method: str, paid_date: date,
                         month: str, by: Staff | None, status: str = "tasdiq",
                         receipt_file_id: str | None = None, receipt_hash: str | None = None,
                         batch: str | None = None) -> Payment:
    pay = Payment(enrollment_id=enr.id, month=month, amount=amount, method=method, status=status,
                  paid_date=paid_date, entered_by=by.id if by else None,
                  receipt_file_id=receipt_file_id, receipt_hash=receipt_hash, batch=batch)
    s.add(pay)
    await s.flush()
    if status == "tasdiq":
        await apply_payment_coins(s, pay, enr)
    await audit(s, by, "tolov", payment_id=pay.id, amount=amount, method=method)
    return pay


async def apply_payment_coins(s: AsyncSession, pay: Payment, enr: Enrollment) -> int:
    """Shu oy uchun shu oy ichida to'langan bo'lsa — coin (har 20 000 so'm).
    +5 bonus o'quvchiga oyiga BIR MARTA: barcha guruhlari bo'yicha qarz 5-sanagacha to'liq yopilsa."""
    rules = await get_rules(s)
    y, m = map(int, pay.month.split("-"))
    if (pay.paid_date.year, pay.paid_date.month) > (y, m):
        return 0  # keyingi oyga o'tgan qarz — coin yo'q
    # coin o'quvchining shu oydagi BARCHA to'lovlari yig'indisidan (guruhlarga bo'linganda kasr yo'qolmasin)
    enr_ids = list((await s.execute(select(Enrollment.id).where(Enrollment.student_id == enr.student_id))).scalars())
    total = sum([await paid_for_month(s, eid, pay.month) for eid in enr_ids])
    before = total - pay.amount
    got = C.payment_coins(before, pay.amount, rules)
    await s.refresh(enr, ["student"])
    await add_coins(s, enr.student, got, "tolov", f"To'lov {fmt_som(pay.amount)}", None)
    bonus = 0
    if pay.paid_date.day <= 5 and rules["pay_bonus"]:
        already = (await s.execute(select(CoinTx.id).where(CoinTx.student_id == enr.student_id, CoinTx.kind == "bonus",
                                                           CoinTx.reason == f"Bonus {pay.month}"))).first()
        if not already:
            enrs = (await s.execute(select(Enrollment).where(Enrollment.student_id == enr.student_id,
                                                             Enrollment.active)
                                    .options(selectinload(Enrollment.group)))).scalars().all()
            debt = 0
            for e in enrs:
                debt += (await month_status(s, e, y, m)).debt
            if debt == 0:
                bonus = rules["pay_bonus"]
                await add_coins(s, enr.student, bonus, "bonus", f"Bonus {pay.month}", None)
    pay.coins_given = got + bonus
    return got + bonus


# ---------- qidiruv, karta tasdig'i, qarzdorlar ----------
async def search_students(s: AsyncSession, q: str, limit: int = 10) -> list[Student]:
    q = q.strip()
    if len(q) < 2:
        return []
    rows = (await s.execute(select(Student).where(Student.status != "ketgan", Student.full_name.ilike(f"%{q}%"))
                            .order_by(Student.full_name).limit(limit))).scalars()
    return list(rows)


def name_key(name: str) -> str:
    """Ism yozilishidagi farqlarni tekislaydi: x/h, o'/o, g'/g, yo/e, so'zlar tartibi (Hasanov = Xasanov)."""
    n = (name or "").lower()
    for ch in "ʻ‘’`ʼ":
        n = n.replace(ch, "'")
    n = n.replace("x", "h").replace("o'", "o").replace("g'", "g").replace("'", "").replace("yo", "e").replace("iy", "i")
    return " ".join(sorted(n.split()))


async def similar_students(s: AsyncSession, name: str) -> list[Student]:
    key = name_key(name)
    rows = (await s.execute(select(Student))).scalars().all()
    return [x for x in rows if name_key(x.full_name) == key]


async def student_labels(s: AsyncSession, students: list[Student]) -> dict[int, str]:
    """Bir xil ismli o'quvchilarni adashtirmaslik uchun tugma yozuvi: ism · guruh(lar) · ota-ona tel. oxirgi 4 raqami."""
    from app.db.models import StudentParent
    ids = [x.id for x in students] or [0]
    groups: dict[int, list[str]] = {}
    for sid, gname in (await s.execute(select(Enrollment.student_id, Group.name).join(Group, Group.id == Enrollment.group_id)
                                       .where(Enrollment.student_id.in_(ids), Enrollment.active))).all():
        groups.setdefault(sid, []).append(gname)
    phones: dict[int, str] = {}
    for sid, ph in (await s.execute(select(StudentParent.student_id, Parent.phone).join(
            Parent, Parent.id == StudentParent.parent_id).where(StudentParent.student_id.in_(ids)))).all():
        phones.setdefault(sid, ph)
    names = [x.full_name.strip().lower() for x in students]
    out = {}
    for x in students:
        g = ", ".join(groups.get(x.id, [])) or "guruhsiz"
        tail = (phones.get(x.id) or x.phone or "")[-4:]
        extra = f" · {x.grade}-sinf" if names.count(x.full_name.strip().lower()) > 1 and x.grade and x.grade <= 11 else ""
        out[x.id] = f"{x.full_name} · {g}" + (f" · ☎…{tail}" if tail else "") + extra
    return out


async def student_enrollments(s: AsyncSession, student_id: int) -> list[Enrollment]:
    q = (select(Enrollment).where(Enrollment.student_id == student_id, Enrollment.active)
         .options(selectinload(Enrollment.group), selectinload(Enrollment.student))
         .execution_options(populate_existing=True))
    return list((await s.execute(q)).scalars())


async def duplicate_receipt(s: AsyncSession, receipt_hash: str) -> bool:
    return (await s.execute(select(Payment.id).where(Payment.receipt_hash == receipt_hash,
                                                     Payment.status != "rad"))).first() is not None


async def confirm_payment(s: AsyncSession, pay: Payment, by: Staff) -> int:
    enr = await s.get(Enrollment, pay.enrollment_id, options=[selectinload(Enrollment.group),
                                                             selectinload(Enrollment.student)])
    pay.status, pay.confirmed_by = "tasdiq", by.id
    await s.flush()
    got = await apply_payment_coins(s, pay, enr)
    await audit(s, by, "karta_tasdiq", payment_id=pay.id)
    return got


async def reject_payment(s: AsyncSession, pay: Payment, by: Staff, reason: str = "") -> None:
    pay.status, pay.confirmed_by = "rad", by.id
    await audit(s, by, "karta_rad", payment_id=pay.id, reason=reason)


async def pending_payments(s: AsyncSession) -> list[Payment]:
    return list((await s.execute(select(Payment).where(Payment.status == "kutilmoqda")
                                 .order_by(Payment.created_at))).scalars())


async def debtors(s: AsyncSession, year: int, month: int) -> list[MonthStatus]:
    first = date(year, month, 1)
    q = (select(Enrollment).join(Student)
         .where(Enrollment.active | (Enrollment.left_date >= first))      # oy ichida ketganlar ham (qarzi qolsa)
         .options(selectinload(Enrollment.group), selectinload(Enrollment.student)))
    out = []
    for e in (await s.execute(q)).scalars():
        st = await month_status(s, e, year, month)
        if st.debt > 0:
            out.append(st)
    out.sort(key=lambda x: (x.enrollment.group.name, x.enrollment.student.full_name))
    return out


async def parent_tg_ids(s: AsyncSession, student_id: int) -> list[int]:
    """Ota-ona(lar) va o'quvchining o'zi (botga ulangan bo'lsa). Aniq so'rov — sessiyadagi eski obyektga bog'liq emas."""
    from app.db.models import StudentParent
    ids = list((await s.execute(select(Parent.tg_id).join(StudentParent, StudentParent.parent_id == Parent.id)
                                .where(StudentParent.student_id == student_id, Parent.tg_id.is_not(None)))).scalars())
    own = (await s.execute(select(Student.tg_id).where(Student.id == student_id))).scalar()
    if own:
        ids.append(own)
    return ids


async def staff_with_role(s: AsyncSession, role: str) -> list[Staff]:
    rows = (await s.execute(select(Staff).where(Staff.active, Staff.roles.like(f"%{role}%")))).scalars()
    return [x for x in rows if role in x.role_list()]


async def admin_may_see_debts() -> bool:
    from app.db.session import Session
    async with Session() as s:
        row = await s.get(Setting, "admin_sees_debts")
        return bool(row and row.value.get("on"))



# ---------- o'quvchi bo'yicha umumiy qarz va to'lovni taqsimlash ----------
@dataclass
class StudentDebt:
    student: Student
    parts: list[MonthStatus]

    @property
    def debt(self) -> int:
        return sum(p.debt for p in self.parts)

    @property
    def payable(self) -> int:
        return sum(p.payable for p in self.parts)


async def debt_enrollments(s: AsyncSession, student_id: int, year: int, month: int) -> list[Enrollment]:
    """Shu oy hisobiga kiradigan o'rinlar: faollar + shu oy ichida guruhdan chiqqanlar."""
    first = date(year, month, 1)
    q = (select(Enrollment).where(Enrollment.student_id == student_id,
                                  Enrollment.active | (Enrollment.left_date >= first))
         .options(selectinload(Enrollment.group), selectinload(Enrollment.student))
         .execution_options(populate_existing=True))
    return list((await s.execute(q)).scalars())


async def student_debt(s: AsyncSession, student_id: int, year: int, month: int) -> StudentDebt:
    enrs = await debt_enrollments(s, student_id, year, month)
    parts = [await month_status(s, e, year, month) for e in enrs]
    stu = enrs[0].student if enrs else await s.get(Student, student_id)
    return StudentDebt(stu, parts)


async def record_student_payment(s: AsyncSession, student_id: int, amount: int, method: str, paid_date: date,
                                 month: str, by: Staff | None, status: str = "tasdiq",
                                 receipt_file_id: str | None = None, receipt_hash: str | None = None
                                 ) -> list[tuple[Enrollment, Payment]]:
    """Bitta to'lov — o'quvchining barcha guruhlariga qarziga mutanosib taqsimlanadi."""
    import uuid
    from app.logic.money import split_payment
    y, m = map(int, month.split("-"))
    sd = await student_debt(s, student_id, y, m)
    parts = [p for p in sd.parts if p.payable > 0] or sd.parts
    shares = split_payment(amount, [p.debt for p in parts])
    batch = str(uuid.uuid4())
    out = []
    for p, share in zip(parts, shares):
        if share <= 0:
            continue
        pay = await record_payment(s, p.enrollment, share, method, paid_date, month, by, status,
                                   receipt_file_id, receipt_hash if not out else None, batch)
        out.append((p.enrollment, pay))
    return out


async def batch_payments(s: AsyncSession, pay: Payment) -> list[Payment]:
    if not pay.batch:
        return [pay]
    return list((await s.execute(select(Payment).where(Payment.batch == pay.batch))).scalars())


async def student_debtors(s: AsyncSession, year: int, month: int) -> list[StudentDebt]:
    by_student: dict[int, list[MonthStatus]] = {}
    for st in await debtors(s, year, month):
        by_student.setdefault(st.enrollment.student_id, []).append(st)
    out = [StudentDebt(v[0].enrollment.student, v) for v in by_student.values()]
    out.sort(key=lambda x: x.student.full_name)
    return out
