"""Kursga yozilish arizalari (lidlar) va rahbar uchun «Hozirgi holat»."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM,
                           KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove)
from sqlalchemy import func, select

from app.bot import keyboards as K
from app.bot.notify import send
from app.config import settings
from app.db.models import Attendance, Group, Lead, Lesson, Payment, Staff, Task
from app.db.session import Session
from app.logic.calendar import is_lesson_day
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
STATUS = {"yangi": "🆕 Yangi", "boglanildi": "📞 Bog'lanildi", "oquvchi": "✅ O'quvchi bo'ldi", "rad": "❌ Rad etildi"}


def now() -> datetime:
    return datetime.now(TZ)


class Ap(StatesGroup):
    name = State()
    subject = State()
    phone = State()


# ---------- ariza (mehmon) ----------
@router.message(F.text == K.B_APPLY)
async def apply_start(m: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Ap.name)
    await m.answer("O'quvchining ism-familiyasini yozing:", reply_markup=ReplyKeyboardRemove())


@router.message(Ap.name)
async def apply_name(m: Message, state: FSMContext) -> None:
    name = (m.text or "").strip()
    if len(name) < 3:
        await m.answer("Ism-familiyani to'liq yozing.")
        return
    await state.update_data(name=name[:120])
    async with Session() as s:
        subjects = sorted({g.subject for g in (await s.execute(select(Group).where(Group.active))).scalars()
                           if g.subject})
    kb = IM(inline_keyboard=[[IB(text=x, callback_data=f"aps:{x[:40]}")] for x in subjects]
            + [[IB(text="Boshqa / bilmayman", callback_data="aps:-")]])
    await state.set_state(Ap.subject)
    await m.answer("Qaysi fanga qiziqasiz?", reply_markup=kb)


@router.callback_query(Ap.subject, F.data.startswith("aps:"))
async def apply_subject(c: CallbackQuery, state: FSMContext) -> None:
    subj = c.data.split(":", 1)[1]
    await state.update_data(subject=None if subj == "-" else subj)
    await state.set_state(Ap.phone)
    await c.message.edit_text("Rahmat! Endi siz bilan bog'lanishimiz uchun telefon raqamingizni yuboring:")
    await c.message.answer("Pastdagi tugmani bosing 👇", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=K.B_CONTACT, request_contact=True)]], resize_keyboard=True,
        one_time_keyboard=True))


@router.message(Ap.phone)
async def apply_phone(m: Message, state: FSMContext, bot: Bot) -> None:
    phone = S.norm_phone(m.contact.phone_number if m.contact else m.text)
    if not phone:
        await m.answer("Raqamni tugma orqali yuboring yoki +998901234567 ko'rinishida yozing.")
        return
    d = await state.get_data()
    async with Session() as s:
        lead = Lead(tg_id=m.from_user.id, phone=phone, child_name=d["name"], subject=d.get("subject"))
        s.add(lead)
        await s.flush()
        admins = [x.tg_id for x in await S.staff_with_role(s, "admin") if x.tg_id]
        await s.commit()
    await state.clear()
    await m.answer(f"✅ Arizangiz qabul qilindi! {settings.center_name} administratori tez orada siz bilan "
                   "bog'lanadi.", reply_markup=K.guest_kb())
    for a in admins:
        await send(bot, a, f"🧲 <b>Yangi ariza</b>\n{lead.child_name}\nFan: {lead.subject or '—'}\nTel: {phone}\n"
                           "Bog'lanib, holatini belgilang.", reply_markup=_lead_kb(lead.id))


def _lead_kb(lid: int) -> IM:
    return IM(inline_keyboard=[[IB(text="📞 Bog'lanildi", callback_data=f"ld:{lid}:boglanildi"),
                                IB(text="✅ O'quvchi bo'ldi", callback_data=f"ld:{lid}:oquvchi")],
                               [IB(text="❌ Rad etildi", callback_data=f"ld:{lid}:rad")]])


@router.callback_query(F.data.startswith("ld:"))
async def lead_status(c: CallbackQuery) -> None:
    _, lid, st = c.data.split(":")
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not who.staff:
            return
        lead = await s.get(Lead, int(lid))
        lead.status, lead.handled_by = st, who.staff.id
        await S.audit(s, who.staff, "ariza", name=lead.child_name, status=st)
        await s.commit()
    await c.answer(STATUS[st])
    if st == "oquvchi":
        await c.message.edit_text(f"{c.message.html_text}\n\nHolat: <b>{STATUS[st]}</b>",
                                  reply_markup=IM(inline_keyboard=[[IB(text="➕ Ro'yxatga olish",
                                                                       callback_data=f"rgl:{lid}")]]))
    elif st == "rad":
        await c.message.edit_text(f"{c.message.html_text}\n\nHolat: <b>{STATUS[st]}</b>")


@router.message(F.text == K.B_LEADS)
async def leads_list(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not (who.staff and who.staff.current_role in ("admin", "rahbar")):
            return
        rows = (await s.execute(select(Lead).where(Lead.status.in_(("yangi", "boglanildi")))
                                .order_by(Lead.created_at))).scalars().all()
        month_start = now().date().replace(day=1)
        total = (await s.execute(select(func.count(Lead.id)).where(Lead.created_at >= month_start))).scalar()
        conv = (await s.execute(select(func.count(Lead.id)).where(Lead.created_at >= month_start,
                                                                   Lead.status == "oquvchi"))).scalar()
    await m.answer(f"🧲 <b>Arizalar</b>\nShu oy: {total} ta, o'quvchi bo'lgani: {conv} ta\n"
                   f"Ochiq: {len(rows)} ta" + ("" if rows else " ✅"))
    for lead in rows:
        await m.answer(f"{STATUS[lead.status]} · {lead.created_at:%d.%m}\n<b>{lead.child_name}</b>\n"
                       f"Fan: {lead.subject or '—'}\nTel: {lead.phone}", reply_markup=_lead_kb(lead.id))


# ---------- 📊 hozirgi holat (rahbar) ----------
@router.message(F.text == K.B_NOW)
async def now_status(m: Message) -> None:
    t = now().date()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            return
        groups = [g for g in (await s.execute(select(Group).where(Group.active).order_by(Group.start_time)))
                  .scalars() if is_lesson_day(t, g.days)]
        lines = [f"📊 <b>Hozirgi holat · {now():%d.%m %H:%M}</b>", "", "<b>Bugungi darslar</b>"]
        for g in groups:
            les = (await s.execute(select(Lesson).where(Lesson.group_id == g.id, Lesson.date == t))).scalar_one_or_none()
            when = f"{g.start_time:%H:%M}" if g.start_time else "vaqt yo'q"
            if les and les.confirmed_at:
                absent = (await s.execute(select(func.count(Attendance.id)).where(
                    Attendance.lesson_id == les.id, Attendance.mark == "y"))).scalar()
                lines.append(f"✅ {when} {g.name} — davomat olindi, kelmagan {absent}")
            else:
                lines.append(f"⏳ {when} {g.name} — davomat hali yo'q")
        if not groups:
            lines.append("Bugun dars yo'q")
        pays = (await s.execute(select(Payment).where(Payment.paid_date == t, Payment.status == "tasdiq"))).scalars().all()
        cash = sum(p.amount for p in pays if p.method == "naqd")
        card = sum(p.amount for p in pays if p.method == "karta")
        pending = len({p.batch or p.id for p in await S.pending_payments(s)})
        open_tasks = (await s.execute(select(func.count(Task.id)).where(Task.status == "ochiq"))).scalar()
        late_tasks = (await s.execute(select(func.count(Task.id)).where(Task.status == "ochiq", Task.due < t))).scalar()
        new_leads = (await s.execute(select(func.count(Lead.id)).where(Lead.status == "yangi"))).scalar()
    lines += ["", "<b>Bugungi tushum</b>", f"Naqd: {S.fmt_som(cash)} · Karta: {S.fmt_som(card)}",
              f"Tasdiq kutayotgan to'lovlar: {pending}", "",
              f"📝 Ochiq vazifalar: {open_tasks} (muddati o'tgan: {late_tasks})",
              f"🧲 Yangi arizalar: {new_leads}"]
    await m.answer("\n".join(lines))
