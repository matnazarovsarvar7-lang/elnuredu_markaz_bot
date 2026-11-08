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
            resp = await s.get(Staff, resp_id) if resp_id else None
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
    text = (f"🌙 <b>Kunlik hisobot · {t:%d.%m.%Y}</b>\n"
            f"Tushum: naqd {S.fmt_som(cash)}, karta {S.fmt_som(card)}\n"
            f"Tasdiq kutayotgan to'lovlar: {pending}\n"
            f"Darslar: {len(groups_today)}, davomat olinmagan: {max(0, missing)}\n"
            f"Darsga kelmaganlar: {absent}")
    for b in bosses:
        await send(bot, b, text)


def setup(bot: Bot) -> AsyncIOScheduler:
    sch = AsyncIOScheduler(timezone=TZ)
    sch.add_job(debt_reminders, "cron", hour=10, minute=0, args=[bot])
    sch.add_job(attendance_watch, "cron", minute="*/10", args=[bot])
    sch.add_job(evening_report, "cron", hour=21, minute=0, args=[bot])
    sch.start()
    return sch
