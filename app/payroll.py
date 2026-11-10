"""Oylik hisob: davr (oyning so'nggi kuni 14:00), o'qituvchi ulushi, oklad, moliya 2%, xizmat rag'bati,
avans, bonus/jarima ballari (tushumning 1% i), kassa majburiyatlari."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.db.models import (Attendance, AuditLog, BonusLedger, CashClose, Enrollment, Expense, Group, Lead, Lesson,
                           Payment, Payout, Payroll, ServiceSale, Setting, Staff, StaffPoint)
from app.logic.money import Discount, teacher_share_base

UTC_SHIFT = timedelta(hours=5)          # Toshkent = UTC+5 (yozgi vaqt yo'q)
CLOSE_HOUR = 14
MX, MN = 20, -15
BONUS_SHARE = 0.01
MOLIYA_PCT = 2.0
DEDUCT_CAP = 0.10


# ---------------- davrlar ----------------
def close_moment(year: int, month: int) -> datetime:
    """Oyning so'nggi kuni 14:00 (yakshanba bo'lsa — shanba), mahalliy vaqt."""
    d = date(year, month, monthrange(year, month)[1])
    if d.weekday() == 6:
        d -= timedelta(days=1)
    return datetime(d.year, d.month, d.day, CLOSE_HOUR)


def prev_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - (m == 1)}-{(m - 2) % 12 + 1:02d}"


def next_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y + (m == 12)}-{m % 12 + 1:02d}"


def period(month: str) -> tuple[datetime, datetime]:
    """(boshi, oxiri] — UTC (bazadagi created_at bilan solishtirish uchun)."""
    y, m = map(int, month.split("-"))
    py, pm = map(int, prev_month(month).split("-"))
    return close_moment(py, pm) - UTC_SHIFT, close_moment(y, m) - UTC_SHIFT


def current_month(now_local: datetime) -> str:
    """Hozirgi tushum qaysi oyga yoziladi (14:00 dan keyin — keyingi oy)."""
    m = f"{now_local:%Y-%m}"
    return next_month(m) if now_local.replace(tzinfo=None) > close_moment(now_local.year, now_local.month) else m


# ---------------- tushum va ulushlar ----------------
async def income(s, start: datetime, end: datetime) -> int:
    """Markaz tushumi: naqd + karta o'quv to'lovlari + xizmatlar (ijtimoiy shartli to'lovlar kirmaydi)."""
    pays = (await s.execute(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.status == "tasdiq", Payment.method.in_(("naqd", "karta")),
        Payment.created_at > start, Payment.created_at <= end))).scalar()
    sales = (await s.execute(select(func.coalesce(func.sum(ServiceSale.amount), 0)).where(
        ServiceSale.status == "tasdiq", ServiceSale.created_at > start, ServiceSale.created_at <= end))).scalar()
    return int(pays) + int(sales)


async def teacher_share(s, st: Staff, start: datetime, end: datetime) -> tuple[int, list[tuple[str, int]]]:
    """O'qituvchi guruhlaridan shu davrda tushgan to'lovlar × foiz (chegirma qoidalari bilan)."""
    if not st.percent:
        return 0, []
    from app.db.models import GroupTeacher
    hist_gids = set((await s.execute(select(GroupTeacher.group_id).where(GroupTeacher.teacher_id == st.id))).scalars())
    groups = (await s.execute(select(Group).where((Group.teacher_id == st.id) | Group.id.in_(hist_gids or {0})))).scalars().all()
    total, lines = 0, []
    for g in groups:
        spans = await teacher_spans(s, g, st.id, start, end)
        if not spans:
            continue
        cond = None
        for a_, b_ in spans:
            c_ = (Payment.created_at > a_) & (Payment.created_at <= b_)
            cond = c_ if cond is None else (cond | c_)
        rows = (await s.execute(select(Payment, Enrollment).join(Enrollment, Enrollment.id == Payment.enrollment_id)
                                .where(Enrollment.group_id == g.id, Payment.status == "tasdiq", Payment.amount > 0,
                                       cond))).all()
        base = 0
        for p, e in rows:
            disc = Discount(e.discount_kind, e.discount_percent)
            if e.discount_kind == "markaz" and e.discount_percent:
                payable_full = 100 - e.discount_percent
                base += round(p.amount * 100 / payable_full) if payable_full > 0 else p.amount
            else:
                base += p.amount        # ijtimoiy: shartli to'lov to'liq narxda yozilgan
        # qaytarilgan pullar (manfiy) ulushdan ayiriladi
        refunds = (await s.execute(select(func.coalesce(func.sum(Payment.amount), 0)).join(
            Enrollment, Enrollment.id == Payment.enrollment_id).where(
            Enrollment.group_id == g.id, Payment.amount < 0, Payment.status == "tasdiq", cond))).scalar()
        share = round((base + int(refunds)) * st.percent / 100)
        if share:
            lines.append((f"{g.name}: {st.percent:g}% ulush", share))
            total += share
    return total, lines


async def teacher_spans(s, g: Group, staff_id: int, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Davr ichida shu xodim guruh o'qituvchisi bo'lgan oraliqlar (o'qituvchi almashgan bo'lsa — tarix bo'yicha)."""
    from app.db.models import GroupTeacher
    hist = (await s.execute(select(GroupTeacher).where(GroupTeacher.group_id == g.id)
                            .order_by(GroupTeacher.since))).scalars().all()
    if not hist:
        return [(start, end)] if g.teacher_id == staff_id else []
    out = []
    for i, h in enumerate(hist):
        if h.teacher_id != staff_id:
            continue
        a_ = max(start, h.since)
        b_ = min(end, hist[i + 1].since) if i + 1 < len(hist) else end
        if a_ < b_:
            out.append((a_, b_))
    return out


def has_role(st: Staff, role: str) -> bool:
    return role in (st.roles or "").split(",")


async def earned(s, st: Staff, start: datetime, end: datetime, prorate: float = 1.0) -> tuple[int, list]:
    """Davr ichida ishlab topilgan (bonusdan tashqari)."""
    from app.bot.finance import service_rewards
    lines = []
    share, sl = await teacher_share(s, st, start, end)
    lines += sl
    if st.oklad:
        lines.append(("Oklad" + (" (o'tgan kunlar ulushi)" if prorate < 1 else ""), round(st.oklad * prorate)))
    if has_role(st, "moliya"):
        pct = await _setting_num(s, "moliya_pct", MOLIYA_PCT)
        lines.append((f"Moliya bo'limi: barcha tushumning {pct:g}%", round(await income(s, start, end) * pct / 100)))
    rew = await service_rewards(s, st.id, start, end)
    if rew:
        lines.append(("Xizmatlar sotuvidan 15% rag'bat", rew))
    return sum(v for _, v in lines), lines


async def _setting_num(s, key: str, default: float) -> float:
    row = await s.get(Setting, key)
    try:
        return float((row.value or {}).get("v", default)) if row else default
    except (TypeError, ValueError):
        return default


async def payouts(s, staff_id: int, month: str, kind: str | None = None) -> int:
    q = select(func.coalesce(func.sum(Payout.amount), 0)).where(Payout.staff_id == staff_id, Payout.month == month,
                                                                Payout.status.in_(("tasdiq", "berildi", "olindi")))
    if kind:
        q = q.where(Payout.kind == kind)
    return int((await s.execute(q)).scalar())


# ---------------- kassa majburiyatlari (kassa qulfi uchun) ----------------
async def obligations(s, now_local: datetime | None = None) -> int:
    """Shu kungacha xodimlarga hisoblangan, lekin berilmagan pul + shu oyning to'lanmagan doimiy xarajatlari
    + yig'ilgan bonuslar + kelgusi oylar uchun oldindan to'langan pullar."""
    now_local = now_local or datetime.utcnow() + UTC_SHIFT
    month = current_month(now_local)
    start, _ = period(month)
    end = now_local.replace(tzinfo=None) - UTC_SHIFT
    y, m = map(int, month.split("-"))
    days = monthrange(y, m)[1]
    prorate = min(1.0, now_local.day / days)
    owed = 0
    for st in (await s.execute(select(Staff).where(Staff.active))).scalars():
        if has_role(st, "rahbar"):
            continue
        e, _ = await earned(s, st, start, end, prorate)
        owed += max(0, e - await payouts(s, st.id, month))
    fixed = await _setting_num(s, "fixed_costs", 2_000_000)           # ijara+kommunal+boshqa (oyiga)
    first = date(y, m, 1)
    paid_fixed = int((await s.execute(select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.status == "tasdiq", Expense.date >= first,
        Expense.category.in_(("Ijara", "Kommunal", "Internet"))))).scalar())
    owed += max(0, int(fixed) - paid_fixed)
    owed += int((await s.execute(select(func.coalesce(func.sum(BonusLedger.amount), 0)).where(
        BonusLedger.status == "yigilmoqda", BonusLedger.amount > 0))).scalar())
    owed += int((await s.execute(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.status == "tasdiq", Payment.method == "naqd", Payment.month > month))).scalar())
    return owed


# ---------------- ballar (KPI) ----------------
def _month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


async def _att_rates(s, group_ids: list[int], d1: date, d2: date) -> tuple[float | None, float | None]:
    if not group_ids:
        return None, None
    rows = (await s.execute(select(Attendance.mark, Attendance.homework).join(Lesson, Lesson.id == Attendance.lesson_id)
                            .where(Lesson.group_id.in_(group_ids), Lesson.date >= d1, Lesson.date <= d2))).all()
    marks = [r[0] for r in rows if r[0]]
    hws = [r[1] for r in rows if r[1]]
    att = sum(1 for x in marks if x != "y") / len(marks) if marks else None
    hw = sum(1 for x in hws if x == "done") / len(hws) if hws else None
    return att, hw


async def penalties(s, st: Staff, role: str, d1: date, d2: date) -> int:
    q = select(func.coalesce(func.sum(StaffPoint.points), 0)).where(
        StaffPoint.staff_id == st.id, StaffPoint.date >= d1, StaffPoint.date <= d2, StaffPoint.points < 0)
    roles = [r for r in (st.roles or "").split(",") if r != "rahbar"]
    if len(roles) > 1:
        q = q.where((StaffPoint.role == role) | ((StaffPoint.role.is_(None)) & (role == roles[0])))
    return int((await s.execute(q)).scalar())


async def kpi_points(s, st: Staff, role: str, month: str) -> tuple[int, list[str]]:
    """Bonus ballari (8-bo'lim jadvali bo'yicha)."""
    d1, d2 = _month_bounds(month)
    pen = await penalties(s, st, role, d1, d2)
    pts, why = 0, []

    def add(ok: bool, n: int, text: str) -> None:
        nonlocal pts
        if ok:
            pts += n
            why.append(f"+{n} {text}")

    if role == "oqituvchi":
        gids = list((await s.execute(select(Group.id).where(Group.teacher_id == st.id))).scalars())
        att, hw = await _att_rates(s, gids, d1, d2)
        add(att is not None and att >= 0.90, 4, "davomat 90%+")
        add(hw is not None and hw >= 0.85, 4, "vazifa 85%+")
        names = set((await s.execute(select(Group.name).where(Group.id.in_(gids or [0])))).scalars())
        unhappy_mine = 0
        for a in (await s.execute(select(AuditLog).where(
                AuditLog.action == "ketdi", AuditLog.created_at >= datetime.combine(d1, datetime.min.time()),
                AuditLog.created_at <= datetime.combine(d2, datetime.max.time())))).scalars():
            det = a.details or {}
            if "norozi" in str(det.get("reason", "")).lower() and det.get("group") in names:
                unhappy_mine += 1
        add(unhappy_mine == 0, 4, "norozi ketgan o'quvchi yo'q")
        late = (await s.execute(select(func.count(StaffPoint.id)).where(
            StaffPoint.staff_id == st.id, StaffPoint.date >= d1, StaffPoint.date <= d2,
            StaffPoint.reason.like("%o'z vaqtida%")))).scalar()
        add(late == 0, 3, "davomat va vazifa o'z vaqtida")
        from app.bot.extra import survey_avg
        avg, cnt = await survey_avg(s, st.id, month)
        add(avg is not None and cnt >= 3 and avg >= 9, 2, f"so'rovnoma o'rtachasi {avg:.1f}" if avg else "so'rovnoma 9+")
        add(pen == 0, 3, "jarimasiz oy")
    elif role == "yordamchi":
        gids = list((await s.execute(select(Group.id).where(Group.assistant_id == st.id))).scalars())
        att, hw = await _att_rates(s, gids, d1, d2)
        add(pen == 0, 8, "yuklangan ishlar o'z vaqtida")
        add(att is not None and att >= 0.90, 5, "guruhlar davomati 90%+")
        add(hw is not None and hw >= 0.85, 4, "vazifa 85%+")
        add(pen == 0, 3, "jarimasiz oy")
    elif role == "admin":
        absents = (await s.execute(select(Attendance.absence_reason).join(Lesson, Lesson.id == Attendance.lesson_id)
                                   .where(Attendance.mark == "y", Lesson.date >= d1, Lesson.date <= d2))).scalars().all()
        called = sum(1 for r in absents if r) / len(absents) if absents else 1.0
        add(called >= 0.95, 6, "kelmaganlarga qo'ng'iroq")
        leads = (await s.execute(select(Lead).where(Lead.created_at >= datetime.combine(d1, datetime.min.time()),
                                                    Lead.created_at <= datetime.combine(d2, datetime.max.time())))).scalars().all()
        conv = sum(1 for l in leads if l.status == "oquvchi") / len(leads) if leads else 1.0
        add(conv >= 0.5, 5, "arizalarning 50%+ i o'quvchi bo'ldi")
        open_old = sum(1 for l in leads if l.status == "yangi")
        add(open_old == 0, 4, "arizalarga javob berildi")
        regs = (await s.execute(select(Enrollment.registered_at, Enrollment.join_date).where(
            Enrollment.registered_at >= d1, Enrollment.registered_at <= d2))).all()
        late_reg = sum(1 for r, j in regs if r and j and (r - j).days > 5)
        add(late_reg == 0, 2, "yangi o'quvchilar o'z vaqtida ro'yxatda")
        add(pen == 0, 3, "jarimasiz oy")
    elif role == "moliya":
        from app import services as S
        enrs = (await s.execute(select(Enrollment).where(Enrollment.active, Enrollment.discount_kind != "ijtimoiy")
                                .options(selectinload(Enrollment.group), selectinload(Enrollment.student)))).scalars().all()
        y, m = d1.year, d1.month
        early, payable, debt = 0, 0, 0
        for e in enrs:
            stt = await S.month_status(s, e, y, m)
            payable += stt.payable
            debt += stt.debt
            paid5 = int((await s.execute(select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.enrollment_id == e.id, Payment.month == f"{y}-{m:02d}", Payment.status == "tasdiq",
                Payment.paid_date <= date(y, m, 5)))).scalar())
            early += paid5 >= stt.payable > 0
        n = sum(1 for e in enrs)
        add(n and early / n >= 0.70, 6, "5-sanagacha to'laganlar 70%+")
        add(payable and debt / payable < 0.10, 6, "oy oxirida qarz 10% dan kam")
        farq = (await s.execute(select(func.count(CashClose.id)).where(
            CashClose.date >= d1, CashClose.date <= d2, CashClose.status == "farq"))).scalar()
        add(farq == 0, 3, "kassada farq yo'q")
        nocheck = (await s.execute(select(func.count(Expense.id)).where(
            Expense.date >= d1, Expense.date <= d2, Expense.by_id == st.id, Expense.receipt_file_id.is_(None)))).scalar()
        add(nocheck == 0, 2, "xarajatlar cheki bilan")
        add(pen == 0, 3, "jarimasiz oy")
    return max(MN, min(MX, pts + pen)), why + ([f"{pen} jarima ballari"] if pen else [])


def role_slots(staff: list[Staff]) -> list[tuple[Staff, str]]:
    out = []
    for st in staff:
        for r in (st.roles or "").split(","):
            if r and r != "rahbar":
                out.append((st, r))
    return out


# ---------------- oyni yopish ----------------
async def close_month(s, month: str) -> list[Payroll]:
    """Oylik hisobni tuzadi (qayta chaqirilsa — yangilaydi). Bonus 6 oyda (dekabr/iyun) yoki xodim tanlasa har oy."""
    start, end = period(month)
    tushum = await income(s, start, end)
    staff = [x for x in (await s.execute(select(Staff).where(Staff.active))).scalars()]
    slots = role_slots(staff)
    value = int(tushum * BONUS_SHARE / (MX * max(1, len(slots)))) if slots else 0
    # ballar -> daftar
    # faqat shu oyning rol ballari qayta yoziladi (yutuq va o'tgan oydan qolgan qoldiq saqlanadi)
    await s.execute(BonusLedger.__table__.delete().where(
        BonusLedger.month == month, BonusLedger.status == "yigilmoqda",
        BonusLedger.role.notin_(("yutuq", "qoldiq"))))
    for st, r in slots:
        pts, _ = await kpi_points(s, st, r, month)
        s.add(BonusLedger(staff_id=st.id, month=month, role=r, points=pts, value=value, amount=pts * value))
    await s.flush()
    out = []
    y, m = map(int, month.split("-"))
    pay_bonus_now = m in (6, 12)
    for st in staff:
        if has_role(st, "rahbar") and not st.percent:
            continue
        gross, lines = await earned(s, st, start, end)
        avans = await payouts(s, st.id, month, "avans")
        monthly = await _pref_monthly(s, st.id)
        bonus = penalty = 0
        if pay_bonus_now or monthly:
            rows = (await s.execute(select(BonusLedger).where(BonusLedger.staff_id == st.id,
                                                              BonusLedger.status == "yigilmoqda"))).scalars().all()
            total = sum(r.amount for r in rows)
            if total >= 0:
                bonus = total
                for r in rows:
                    r.status = "tolandi"
            else:
                penalty = min(-total, round(gross * DEDUCT_CAP))
                for r in rows:
                    r.status = "ushlandi"
                if -total > penalty:      # chegaradan ortig'i keyingi davrga o'tadi
                    s.add(BonusLedger(staff_id=st.id, month=next_month(month), role="qoldiq", points=0, value=0,
                                      amount=total + penalty))
        net = gross - avans + bonus - penalty
        if net < 0:   # berilgan avans ishlab topilgandan ko'p — farqi keyingi oy oyligidan ushlanadi
            carry = -net
            lines = list(lines) + [("Keyingi oyga o'tgan avans qarzi", carry)]
            s.add(Payout(staff_id=st.id, month=next_month(month), kind="avans", amount=carry, method="qarz",
                         status="berildi", given_at=datetime.now()))
            net = 0
        pr = (await s.execute(select(Payroll).where(Payroll.staff_id == st.id, Payroll.month == month))).scalar_one_or_none()
        if not pr:
            pr = Payroll(staff_id=st.id, month=month)
            s.add(pr)
        pr.lines, pr.gross, pr.avans, pr.bonus, pr.penalty, pr.net = [list(x) for x in lines], gross, avans, bonus, penalty, net
        out.append(pr)
    await s.flush()
    return out


async def _pref_monthly(s, staff_id: int) -> bool:
    row = await s.get(Setting, f"bonus_pref_{staff_id}")
    return bool(row and (row.value or {}).get("monthly"))


async def avans_info(s, st: Staff, now_local: datetime) -> tuple[int, int, int]:
    """(ishlab topilgan, olingan avans, mumkin bo'lgan avans)."""
    from app.logic.salary import AVANS_LAST_DAY, AVANS_MAX_PER_MONTH, AVANS_RATIO
    month = current_month(now_local)
    start, _ = period(month)
    y, m = map(int, month.split("-"))
    prorate = min(1.0, now_local.day / monthrange(y, m)[1])
    e, _ = await earned(s, st, start, now_local.replace(tzinfo=None) - UTC_SHIFT, prorate)
    taken = await payouts(s, st.id, month, "avans")
    reqs = int((await s.execute(select(func.count(Payout.id)).where(
        Payout.staff_id == st.id, Payout.month == month, Payout.kind == "avans", Payout.status != "rad"))).scalar())
    # shu oy ushlanadigan jarima avansni kamaytiradi
    pen = -int((await s.execute(select(func.coalesce(func.sum(BonusLedger.amount), 0)).where(
        BonusLedger.staff_id == st.id, BonusLedger.status == "yigilmoqda", BonusLedger.amount < 0))).scalar())
    if now_local.day > AVANS_LAST_DAY or reqs >= AVANS_MAX_PER_MONTH:
        return e, taken, 0
    pending = int((await s.execute(select(func.coalesce(func.sum(Payout.amount), 0)).where(
        Payout.staff_id == st.id, Payout.month == month, Payout.kind == "avans",
        Payout.status == "kutilmoqda"))).scalar())
    can = int(max(0, e * AVANS_RATIO - taken - pending - pen) // 10_000 * 10_000)
    return e, taken, can


async def avans_reason(s, st: Staff, now_local: datetime) -> str:
    """Avans nega hozir mumkin emasligini aniq tushuntiradi."""
    from app.logic.salary import AVANS_LAST_DAY, AVANS_MAX_PER_MONTH, AVANS_RATIO
    month = current_month(now_local)
    e, taken, _ = await avans_info(s, st, now_local)
    reqs = int((await s.execute(select(func.count(Payout.id)).where(
        Payout.staff_id == st.id, Payout.month == month, Payout.kind == "avans", Payout.status != "rad"))).scalar())
    if now_local.day > AVANS_LAST_DAY:
        return f"Avans har oyning {AVANS_LAST_DAY}-sanasigacha so'raladi. Oylik oy oxirida beriladi."
    if reqs >= AVANS_MAX_PER_MONTH:
        return f"Bu oy avans {AVANS_MAX_PER_MONTH} marta so'ralgan — oyiga ko'pi bilan {AVANS_MAX_PER_MONTH} marta."
    if e <= 0:
        return ("Shu oy hali ishlab topilgan summa yo'q: guruhlaringizdan bu oy botga kiritilgan to'lov yo'q. "
                f"Avans ishlab topilganning {int(AVANS_RATIO * 100)}% igacha beriladi — to'lovlar kiritilishi bilan ochiladi.")
    pending = int((await s.execute(select(func.coalesce(func.sum(Payout.amount), 0)).where(
        Payout.staff_id == st.id, Payout.month == month, Payout.kind == "avans",
        Payout.status == "kutilmoqda"))).scalar())
    if pending:
        return f"{pending:,} so'mlik avans so'rovingiz rahbar tasdig'ini kutmoqda.".replace(",", " ")
    return (f"Ishlab topilgan {e:,} so'mning {int(AVANS_RATIO * 100)}% i ({int(e * AVANS_RATIO):,}) olingan avans "
            f"({taken:,}) va jarimalardan qolmadi yoki 10 000 so'mdan kam.").replace(",", " ")
