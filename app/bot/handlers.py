from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from aiogram import Bot, F, Router
from aiogram.types import InlineKeyboardButton as IB, InlineKeyboardMarkup as IM
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.orm import selectinload

from app.bot import keyboards as K
from app.config import settings
from app.db.models import Group, Lesson, Student
from app.db.session import Session
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
# Tasdiqlanmagan belgilar (dars davomida xotirada)
DRAFT_ATT: dict[int, dict[int, str]] = {}
DRAFT_HW: dict[int, dict[int, str]] = {}
NEXT_MARK = {"k": "y", "y": "l", "l": "k"}
NEXT_HW = {"done": "part", "part": "none", "none": "done"}


def today() -> date:
    return datetime.now(TZ).date()


async def show_menu(m: Message, who: S.Who) -> None:
    if who.staff:
        roles = who.staff.role_list()
        role = who.staff.current_role or roles[0]
        await m.answer(f"Assalomu alaykum, {who.staff.full_name}!\nRol: {S.ROLE_TITLES[role]}",
                       reply_markup=K.staff_menu(role, len(roles) > 1))
    elif who.parent:
        if not who.parent.children:
            await m.answer("Farzandingiz hali biriktirilmagan. Admin bilan bog'laning.")
        await m.answer(f"{settings.center_name} botiga xush kelibsiz!", reply_markup=K.parent_menu())
    elif who.student:
        await m.answer(f"Salom, {who.student.full_name}!", reply_markup=K.student_menu())


@router.message(CommandStart())
async def start(m: Message, state: FSMContext | None = None) -> None:
    if state:
        await state.clear()   # boshlangan har qanday amal bekor bo'ladi
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
    if who.staff or who.parent or who.student:
        await show_menu(m, who)
    else:
        await m.answer(f"{settings.center_name} botiga xush kelibsiz!\n\n"
                       "• Farzandingiz markazimizda o'qisa — «📱 Raqamni yuborish» tugmasini bosing.\n"
                       "• Kursga yozilmoqchi bo'lsangiz — «✍️ Kursga yozilish» tugmasini bosing.",
                       reply_markup=K.guest_kb())


@router.message(F.contact)
async def got_contact(m: Message) -> None:
    if m.contact.user_id != m.from_user.id:
        await m.answer("Iltimos, o'zingizning raqamingizni tugma orqali yuboring.", reply_markup=K.contact_kb())
        return
    phone = S.norm_phone(m.contact.phone_number)
    async with Session() as s:
        who = await S.link_by_phone(s, m.from_user.id, phone or "")
        await s.commit()
    if not (who.staff or who.parent or who.student):
        await m.answer("Bu raqam markaz ro'yxatida topilmadi.\n\n"
                       "• Kursga yozilmoqchi bo'lsangiz — «✍️ Kursga yozilish» tugmasini bosing.\n"
                       "• Farzandingiz markazda o'qisa — admin raqamingizni qo'shgach, qayta /start bosing.",
                       reply_markup=K.guest_kb())
        return
    if who.parent and not any(c.consent_at for c in who.parent.children):
        await m.answer("Farzandingiz ma'lumotlari (davomat, to'lov, baholar) markaz tomonidan saqlanadi va "
                       "faqat siz, farzandingiz va markaz xodimlariga ko'rinadi. Rozimisiz?",
                       reply_markup=K.consent_kb())
    await show_menu(m, who)


@router.callback_query(F.data == "consent")
async def consent(c: CallbackQuery) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if who.parent:
            for ch in who.parent.children:
                ch.consent_at = ch.consent_at or datetime.now()
            await s.commit()
    await c.message.edit_text("Rahmat! Rozilik qayd etildi.")


# ---------- rolni almashtirish ----------
@router.message(F.text == K.B_SWITCH)
async def switch(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
    if who.staff:
        await m.answer("Qaysi rolda ishlaysiz?", reply_markup=K.roles_kb(who.staff.role_list()))


@router.callback_query(F.data.startswith("role:"))
async def set_role(c: CallbackQuery) -> None:
    role = c.data.split(":")[1]
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not who.staff or role not in who.staff.role_list():
            await c.answer("Bu rol sizga berilmagan", show_alert=True)
            return
        who.staff.current_role = role
        await s.commit()
    await c.message.delete()
    await show_menu(c.message, who)


# ---------- o'qituvchi: davomat va vazifa ----------
@router.message(F.text == K.B_TODAY)
async def today_lessons(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not who.staff:
            return
        groups = S.todays_groups(await S.teacher_groups(s, who.staff, who.staff.current_role), today())
    if not groups:
        await m.answer("Bugun sizda dars yo'q.")
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"{g.start_time:%H:%M} · {g.name}" if g.start_time else g.name, callback_data=f"grp:{g.id}")]
        for g in groups])
    await m.answer("Bugungi darslaringiz. Guruhni tanlang:", reply_markup=kb)


async def _roster_rows(s, lesson: Lesson, draft: dict[int, str], default: str):
    roster = await S.group_roster(s, lesson.group_id, lesson.date)
    for e in roster:
        draft.setdefault(e.id, default)
    return [(e.id, e.student.full_name, draft[e.id]) for e in roster]


@router.callback_query(F.data.startswith("grp:"))
async def open_group(c: CallbackQuery) -> None:
    gid = int(c.data.split(":")[1])
    async with Session() as s:
        g = await s.get(Group, gid)
        les = await S.get_or_create_lesson(s, gid, today())
        await s.commit()
        if les.confirmed_at and les.id not in DRAFT_HW:
            from app.bot.homework import lesson_status_text, prompt_kb
            text, given = await lesson_status_text(les.id)
            await c.message.edit_text("Davomat olingan ✅\n\n" + text, reply_markup=prompt_kb(les.id, given))
            return
        draft = DRAFT_ATT.setdefault(les.id, {})
        rows = await _roster_rows(s, les, draft, "k")
    if not rows:
        await c.message.edit_text(f"{g.name}: guruhda faol o'quvchi yo'q.")
        return
    await c.message.edit_text(
        f"<b>{g.name}</b> · {les.date:%d.%m.%Y}\nHammasi «keldi» deb belgilangan. Kelmaganni bir marta, "
        "kechikkanni ikki marta bosing, keyin «Tasdiqlash».", reply_markup=K.attendance_kb(les.id, rows))


@router.callback_query(F.data.startswith("att:"))
async def toggle_att(c: CallbackQuery) -> None:
    _, lid, eid = c.data.split(":")
    lid, eid = int(lid), int(eid)
    draft = DRAFT_ATT.setdefault(lid, {})
    draft[eid] = NEXT_MARK[draft.get(eid, "k")]
    async with Session() as s:
        les = await s.get(Lesson, lid)
        rows = await _roster_rows(s, les, draft, "k")
    await c.message.edit_reply_markup(reply_markup=K.attendance_kb(lid, rows))
    await c.answer()


REASONS = {"kasal": "Kasal", "oila": "Oilaviy sabab", "maktab": "Maktab tadbiri", "sababsiz": "Sababsiz",
           "javobsiz": "Telefonni ko'tarmadi", "boshqa": "Boshqa"}


async def notify_absent(bot: Bot, lesson_id: int) -> None:
    """Kelmaganlar ro'yxati adminga — sababni tanlab yozib qo'yadi."""
    from sqlalchemy import select as _select
    from app.db.models import Attendance, Enrollment, Group as _G
    from app.bot.notify import send
    async with Session() as s:
        les = await s.get(Lesson, lesson_id)
        g = await s.get(_G, les.group_id)
        rows = (await s.execute(_select(Attendance).where(Attendance.lesson_id == lesson_id, Attendance.mark == "y"))).scalars().all()
        items = []
        for a in rows:
            e = await s.get(Enrollment, a.enrollment_id, options=[selectinload(Enrollment.student).selectinload(Student.parents)])
            phones = ", ".join(p.phone for p in e.student.parents) or (e.student.phone or "raqam yo'q")
            items.append((a.id, e.student.full_name, phones))
        admins = [x.tg_id for x in await S.staff_with_role(s, "admin") if x.tg_id]
    for aid, name, phones in items:
        kb = IM(inline_keyboard=[[IB(text=v, callback_data=f"ab:{aid}:{k}")] for k, v in REASONS.items()])
        for adm in admins:
            await send(bot, adm, f"📞 Darsga kelmadi: <b>{name}</b>\n{g.name} · {les.date:%d.%m}\nTel: {phones}\n"
                                 "Qo'ng'iroq qilib, sababini tanlang (bugun 20:00 gacha):", reply_markup=kb)


@router.callback_query(F.data.startswith("ab:"))
async def absent_reason(c: CallbackQuery) -> None:
    from app.db.models import Attendance
    _, aid, code = c.data.split(":")
    async with Session() as s:
        a = await s.get(Attendance, int(aid))
        a.absence_reason = REASONS[code]
        who = await S.who_by_tg(s, c.from_user.id)
        await S.audit(s, who.staff, "kelmadi_sabab", attendance_id=a.id, reason=REASONS[code])
        await s.commit()
    await c.message.edit_text(c.message.html_text.split("\nQo'ng'iroq")[0] + f"\nSabab: <b>{REASONS[code]}</b> ✅")


@router.callback_query(F.data.startswith("attok:"))
async def confirm_att(c: CallbackQuery, bot: Bot) -> None:
    lid = int(c.data.split(":")[1])
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        les = await s.get(Lesson, lid)
        draft = DRAFT_ATT.get(lid, {})
        await _roster_rows(s, les, draft, "k")
        counts = await S.save_attendance(s, les, draft, who.staff)
        await s.commit()
        hw_draft = DRAFT_HW.setdefault(lid, {})
        present = {e: m for e, m in draft.items() if m != "y"}
        # oldingi darsda vazifa berilgan bo'lsagina tekshiramiz
        prev = (await s.execute(select(Lesson).where(Lesson.group_id == les.group_id, Lesson.date < les.date,
                                                    Lesson.homework_at.is_not(None))
                                .order_by(Lesson.date.desc()).limit(1))).scalar_one_or_none()
        has_prev = bool(prev and not prev.no_homework_reason)
        rows = [r for r in await _roster_rows(s, les, hw_draft, "done") if r[0] in present] if has_prev else []
        if not has_prev:
            DRAFT_HW.pop(lid, None)
    await c.message.edit_text(f"Davomat saqlandi: keldi {counts['k']}, kechikdi {counts['l']}, "
                              f"kelmadi {counts['y']}.")
    if counts["y"]:
        await notify_absent(bot, lid)
    if rows:
        await c.message.answer("Oldingi uy vazifasi tekshiruvi: hammasi «bajardi». Qisman — bir marta, "
                               "bajarmagan — ikki marta bosing.", reply_markup=K.homework_kb(lid, rows))
    else:
        from app.bot.homework import ask_homework
        await ask_homework(c.message, lid)


@router.callback_query(F.data.startswith("hw:"))
async def toggle_hw(c: CallbackQuery) -> None:
    _, lid, eid = c.data.split(":")
    lid, eid = int(lid), int(eid)
    draft = DRAFT_HW.setdefault(lid, {})
    draft[eid] = NEXT_HW[draft.get(eid, "done")]
    present = set(DRAFT_ATT.get(lid, {}) and {e for e, m in DRAFT_ATT[lid].items() if m != "y"})
    async with Session() as s:
        les = await s.get(Lesson, lid)
        rows = [r for r in await _roster_rows(s, les, draft, "done") if not present or r[0] in present]
    await c.message.edit_reply_markup(reply_markup=K.homework_kb(lid, rows))
    await c.answer()


@router.callback_query(F.data.startswith("hwok:"))
async def confirm_hw(c: CallbackQuery) -> None:
    lid = int(c.data.split(":")[1])
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        les = await s.get(Lesson, lid)
        await S.save_homework(s, les, DRAFT_HW.get(lid, {}), who.staff)
        await s.commit()
    DRAFT_ATT.pop(lid, None); DRAFT_HW.pop(lid, None)
    await c.message.edit_text("Vazifa natijalari saqlandi. Coinlar hisoblandi.")
    from app.bot.homework import ask_homework
    await ask_homework(c.message, lid)


# ---------- ota-ona va o'quvchi ----------
async def student_summary(s, stu: Student) -> str:
    t = today()
    lines = [f"<b>{stu.full_name}</b>", f"Coin: <b>{stu.coins}</b>"]
    total = 0
    for e in await S.student_enrollments(s, stu.id):
        st = await S.month_status(s, e, t.year, t.month)
        g = e.group
        when = f"{g.start_time:%H:%M}" if g.start_time else ""
        days = {"toq": "Du-Chor-Ju", "juft": "Se-Pay-Sha", "har": "Du-Sha"}[g.days]
        pay = "✅ to'langan" if st.debt == 0 else f"qoldi {S.fmt_som(st.debt)}"
        lines.append(f"\n📘 {g.name} · {days} {when}\n   {t:%m.%Y} uchun: {S.fmt_som(st.payable)}, {pay}")
        total += st.debt
    lines.append("\n✅ Bu oy uchun qarz yo'q" if total == 0 else f"\n<b>Umumiy qarz: {S.fmt_som(total)}</b>")
    from app.db.models import Attendance, Enrollment as _E
    grades = (await s.execute(select(Attendance.grade, Lesson.date).join(Lesson, Lesson.id == Attendance.lesson_id)
                              .join(_E, _E.id == Attendance.enrollment_id)
                              .where(_E.student_id == stu.id, Attendance.grade.is_not(None))
                              .order_by(Lesson.date.desc()).limit(5))).all()
    if grades:
        lines.append("\n⭐ Oxirgi baholar: " + ", ".join(f"{S.GRADE_NAMES.get(g, g)} ({d:%d.%m})" for g, d in grades))
    return "\n".join(lines)


@router.message(F.text == K.B_CHILD)
async def child(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not who.parent:
            return
        for ch in who.parent.children:
            await m.answer(await student_summary(s, ch))


@router.message(F.text == K.B_ME)
async def me(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if who.student:
            await m.answer(await student_summary(s, who.student))


@router.message(F.text == K.B_HELP)
async def help_(m: Message) -> None:
    await m.answer("Savol bo'lsa, markaz administratoriga murojaat qiling. Menyuni qayta ochish: /start")
