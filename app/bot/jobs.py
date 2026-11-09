"""Avtomatik vazifalar: qarz eslatmalari, davomat eslatmasi, rahbarga kechki hisobot."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.bot.notify import send
from app.config import settings
from app.db.models import Attendance, Group, Lesson, Payment, Setting, Staff
from app.db.session import Session
from app.logic.calendar import is_lesson_day
from app.logic.money import reminder_day
from app import services as S

TZ = ZoneInfo(settings.timezone)


def now() -> datetime:
    return datetime.now(TZ)


async def debt_reminders(bot: Bot) -> None:
    """Bitta o'quvchiga bitta eslatma — barcha guruhlari bo'yicha umumiy qarz bilan."""
    t = now().date()
    if not reminder_day(t.day):
        return
    async with Session() as s:
        jobs = []
        for sd in await S.student_debtors(s, t.year, t.month):
            parts = [p for p in sd.parts if p.enrollment.discount_kind != "ijtimoiy"]
            if not parts:
                continue
            lines = "\n".join(f"• {p.enrollment.group.name}: {S.fmt_som(p.debt)}" for p in parts)
            total = sum(p.debt for p in parts)
            for tg in await S.parent_tg_ids(s, sd.student.id):
                jobs.append((tg, f"🔔 Eslatma: {sd.student.full_name}\n{t:%m.%Y} uchun to'lanmagan:\n{lines}\n"
                                 f"<b>Jami: {S.fmt_som(total)}</b>\n"
                                 "To'lov qilgan bo'lsangiz, chekni botga yuboring."))
    for tg, text in jobs:
        await send(bot, tg, text)


async def attendance_watch(bot: Bot) -> None:
    """Dars tugaganidan 1 soat o'tib davomat olinmagan bo'lsa — mas'ulga eslatma (bir marta)."""
    n = now()
    t = n.date()
    async with Session() as s:
        groups = (await s.execute(select(Group).where(Group.active, Group.end_time.is_not(None))
                                  .options(selectinload(Group.teacher)))).scalars().all()
        todo = []
        for g in groups:
            if not is_lesson_day(t, g.days):
                continue
            end = datetime.combine(t, g.end_time, TZ)
            if not (end + timedelta(hours=1) <= n < end + timedelta(hours=1, minutes=10)):
                continue
            les = (await s.execute(select(Lesson).where(Lesson.group_id == g.id, Lesson.date == t))).scalar_one_or_none()
            if les and (les.confirmed_at or les.cancelled):
                continue
            resp_id = g.assistant_id if (g.delegated or {}).get("attendance") and g.assistant_id else g.teacher_id
            if await S.group_paused(s, g.id, t):
                continue
            resp = await s.get(Staff, resp_id) if resp_id else None
            from app.bot.exams import on_leave
            if resp and await on_leave(s, resp.id, t):
                continue
            if resp and resp.tg_id:
                todo.append((resp.tg_id, g.name))
    for tg, name in todo:
        await send(bot, tg, f"⏰ «{name}» darsi tugaganiga 1 soat bo'ldi, davomat hali olinmagan. "
                            "«📋 Bugungi darslar» orqali hoziroq kiriting.")


async def evening_report(bot: Bot) -> None:
    t = now().date()
    async with Session() as s:
        cash = (await s.execute(select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.paid_date == t, Payment.status == "tasdiq", Payment.method == "naqd"))).scalar()
        card = (await s.execute(select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.paid_date == t, Payment.status == "tasdiq", Payment.method == "karta"))).scalar()
        pending = len(await S.pending_payments(s))
        les = (await s.execute(select(Lesson).where(Lesson.date == t))).scalars().all()
        absent = (await s.execute(select(func.count(Attendance.id)).join(Lesson).where(
            Lesson.date == t, Attendance.mark == "y"))).scalar()
        groups_today = [g for g in (await s.execute(select(Group).where(Group.active))).scalars() if is_lesson_day(t, g.days)]
        missing = len(groups_today) - sum(1 for l in les if l.confirmed_at)
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        from app.bot.homework import missing_today
        no_hw = await missing_today(s, t)
        from app.bot.cash import early_lessons, expected_cash
        from app.db.models import CashClose
        early = await early_lessons(s, t)
        cl = (await s.execute(select(CashClose).where(CashClose.date == t).order_by(CashClose.id.desc()))).scalars().first()
        exp_cash = await expected_cash(s)
    text = (f"🌙 <b>Kunlik hisobot · {t:%d.%m.%Y}</b>\n"
            f"Tushum: naqd {S.fmt_som(cash)}, karta {S.fmt_som(card)}\n"
            f"Tasdiq kutayotgan to'lovlar: {pending}\n"
            f"Darslar: {len(groups_today)}, davomat olinmagan: {max(0, missing)}\n"
            f"Darsga kelmaganlar: {absent}\n"
            f"Uy vazifasi berilmagan: {len(no_hw)}" + (f" ({', '.join(no_hw)})" if no_hw else " ✅")
            + (f"\nErta tugagan darslar: {'; '.join(early)}" if early else "")
            + "\nKassa: " + ({"mos": "✅ yopildi, mos", "farq": "⚠️ yopildi, farq bor", "boshlang": "▫️ boshlang'ich qoldiq"}
                             .get(cl.status, "") if cl else "❌ yopilmadi")
            + (f" · kassada bo'lishi kerak {S.fmt_som(exp_cash)}" if exp_cash is not None else ""))
    for b in bosses:
        await send(bot, b, text)


def setup(bot: Bot) -> AsyncIOScheduler:
    sch = AsyncIOScheduler(timezone=TZ)
    sch.add_job(debt_reminders, "cron", hour=10, minute=0, args=[bot])
    sch.add_job(attendance_watch, "cron", minute="*/10", args=[bot])
    sch.add_job(evening_report, "cron", hour=21, minute=0, args=[bot])
    from app.bot.homework import homework_watch, tomorrow_reminder
    sch.add_job(homework_watch, "cron", minute="*/10", args=[bot])
    sch.add_job(tomorrow_reminder, "cron", hour=18, minute=0, args=[bot])
    from app.bot.exams import weekly_plan_job, exam_reminder_job, missed_job
    sch.add_job(weekly_plan_job, "cron", day_of_week="sun", hour=19, minute=0, args=[bot])
    sch.add_job(exam_reminder_job, "cron", hour=18, minute=5, args=[bot])
    sch.add_job(missed_job, "cron", day_of_week="sat", hour=20, minute=30, args=[bot])
    from app.bot.status import risk_job
    sch.add_job(risk_job, "cron", hour=20, minute=40, args=[bot])
    from app.bot.cash import social_job, close_reminder
    sch.add_job(social_job, "cron", hour=7, minute=0, args=[bot])
    sch.add_job(close_reminder, "cron", hour=19, minute=30, args=[bot])
    from app.bot.salary import payroll_job, negative_notice
    from app.bot.finance import petty_reminder, printer_reminder
    sch.add_job(payroll_job, "cron", hour=14, minute=1, args=[bot])
    sch.add_job(negative_notice, "cron", day=25, hour=10, minute=15, args=[bot])
    sch.add_job(petty_reminder, "cron", day=1, hour=9, minute=0, args=[bot])
    sch.add_job(printer_reminder, "cron", day_of_week="mon", hour=10, minute=5, args=[bot])
    sch.add_job(late_penalties, "cron", hour=20, minute=5, args=[bot])
    from app.bot.achieve import alumni_job
    sch.add_job(alumni_job, "cron", month=8, day=5, hour=10, minute=0, args=[bot])
    from app.bot.manage import recurring_job, tasks_job
    sch.add_job(recurring_job, "cron", hour=8, minute=30, args=[bot])
    sch.add_job(tasks_job, "cron", hour=9, minute=0, args=[bot])
    sch.start()
    return sch


async def late_penalties(bot: Bot) -> None:
    """20:05 — bugun davomat yoki uy vazifasi o'z vaqtida kiritilmagan bo'lsa mas'ulga −1 ball (ruxsatda emas)."""
    from app.db.models import StaffPoint
    from app.bot.exams import on_leave
    from app.bot.homework import responsible_id
    t = now().date()
    msgs = []
    async with Session() as s:
        for g in (await s.execute(select(Group).where(Group.active))).scalars():
            if not is_lesson_day(t, g.days) or await S.group_paused(s, g.id, t):
                continue
            les = (await s.execute(select(Lesson).where(Lesson.group_id == g.id, Lesson.date == t))).scalar_one_or_none()
            if les and les.cancelled:
                continue
            checks = [("attendance", not (les and les.confirmed_at), "Davomat o'z vaqtida olinmadi"),
                      ("homework", not (les and les.homework_at), "Uy vazifasi o'z vaqtida berilmadi")]
            for job, missing, why in checks:
                if not missing:
                    continue
                sid = responsible_id(g, job)
                st = await s.get(Staff, sid) if sid else None
                if not st or await on_leave(s, st.id, t):
                    continue
                role = "yordamchi" if sid == g.assistant_id and sid != g.teacher_id else "oqituvchi"
                s.add(StaffPoint(staff_id=st.id, points=-1, role=role, date=t, reason=f"{why}: {g.name} {t:%d.%m}"))
                if st.tg_id:
                    msgs.append((st.tg_id, f"⚠️ {why} («{g.name}»): −1 ball."))
        await s.commit()
    for tg, text in msgs:
        await send(bot, tg, text)
