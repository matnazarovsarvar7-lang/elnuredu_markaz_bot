"""Yangi o'quvchini ro'yxatga olish va mavjud o'quvchini yana bir guruhga qo'shish."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.bot import keyboards as K
from app.bot.notify import send
from app.config import settings
from app.db.models import Enrollment, Group, Lead, Parent, Student
from app.db.session import Session
from app.logic.calendar import nth_lesson_on_or_after
from app.logic.money import first_month_due, new_student_deadline, tier_of_grade
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
ROLES = ("admin", "moliya", "rahbar")
BACK_DAYS, AHEAD_DAYS = 7, 30
DAYS = {"toq": "Du-Chor-Ju", "juft": "Se-Pay-Sha", "har": "Du-Sha"}


def today() -> date:
    return datetime.now(TZ).date()


class Rg(StatesGroup):
    name = State()
    grade = State()
    phone = State()
    group = State()
    date = State()


async def _staff(tg_id: int):
    async with Session() as s:
        who = await S.who_by_tg(s, tg_id)
    return who.staff if who.staff and who.staff.current_role in ROLES else None


# ---------- boshlash ----------
@router.callback_query(F.data == "rgnew")
async def reg_again(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await reg_start(c.message, state, tg_id=c.from_user.id)


@router.message(F.text == K.B_NEW_STUDENT)
async def reg_start(m: Message, state: FSMContext, tg_id: int | None = None) -> None:
    if not await _staff(tg_id or m.from_user.id):
        return
    await state.clear()
    await state.set_state(Rg.name)
    await m.answer("➕ <b>Yangi o'quvchi</b>\n\n1/5. O'quvchining familiyasi va ismini yozing:\n"
                   "<i>Masalan: Karimov Jasur</i>\n\nBekor qilish: /start")


@router.callback_query(F.data.startswith("rgl:"))
async def reg_from_lead(c: CallbackQuery, state: FSMContext) -> None:
    if not await _staff(c.from_user.id):
        return
    async with Session() as s:
        lead = await s.get(Lead, int(c.data.split(":")[1]))
    await state.clear()
    await state.update_data(name=lead.child_name, phone=lead.phone, lead=lead.id)
    await c.answer()
    await _ask_grade(c.message, state, prefix=f"Arizadan: <b>{lead.child_name}</b>, tel {lead.phone}\n\n")


async def _back_to_confirm(msg: Message, state: FSMContext) -> bool:
    """Tekshiruv sahifasidan bitta maydon tuzatilayotgan bo'lsa — qayta tekshiruvga qaytadi."""
    d = await state.get_data()
    if d.get("fixing") and d.get("join") and d.get("gid"):
        await state.update_data(fixing=False)
        await _confirm(msg, state, date.fromisoformat(d["join"]))
        return True
    return False


@router.message(Rg.name)
async def reg_name(m: Message, state: FSMContext) -> None:
    name = " ".join((m.text or "").split())
    if len(name) < 5 or " " not in name:
        await m.answer("Familiya va ismni to'liq yozing. Masalan: Karimov Jasur")
        return
    async with Session() as s:
        same = await S.similar_students(s, name)
        labels = await S.student_labels(s, same) if same else {}
    await state.update_data(name=name[:120])
    if same:
        lst = "\n".join(f"• {labels[x.id]} ({x.status})" for x in same)
        await m.answer(f"⚠️ Bazada shunga o'xshash ismli o'quvchi bor:\n{lst}\n\n"
                       "Agar u o'sha bola bo'lsa — yangisini ochmang: «👤 O'quvchi» → kartasi → «➕ Guruhga qo'shish».\n"
                       "Boshqa bola bo'lsa — davom eting (ota-ona telefoni bilan ajratiladi).")
    if await _back_to_confirm(m, state):
        return
    await _ask_grade(m, state)


async def _ask_grade(msg: Message, state: FSMContext, prefix: str = "") -> None:
    await state.set_state(Rg.grade)
    nums = [IB(text=str(i), callback_data=f"rgc:{i}") for i in range(1, 12)]
    kb = IM(inline_keyboard=[nums[0:4], nums[4:8], nums[8:11], [IB(text="Bitirgan / kattalar", callback_data="rgc:12")]])
    await msg.answer(prefix + "2/5. Nechanchi sinfda o'qiydi?", reply_markup=kb)


@router.callback_query(Rg.grade, F.data.startswith("rgc:"))
async def reg_grade(c: CallbackQuery, state: FSMContext) -> None:
    grade = int(c.data.split(":")[1])
    await state.update_data(grade=grade)
    d = await state.get_data()
    await c.message.edit_text(f"Sinf: {grade if grade <= 11 else 'bitirgan'} ✅")
    if await _back_to_confirm(c.message, state):
        return
    if d.get("phone"):
        await _ask_group(c.message, state)
        return
    await state.set_state(Rg.phone)
    await c.message.answer("3/5. Ota-ona (yoki vasiy) telefon raqamini yozing: +998901234567\n"
                           "Shu raqam bilan ota-ona botga kiradi.")


@router.message(Rg.phone)
async def reg_phone(m: Message, state: FSMContext) -> None:
    phone = S.norm_phone(m.text)
    if not phone:
        await m.answer("Raqam noto'g'ri. Masalan: +998901234567")
        return
    await state.update_data(phone=phone)
    if await _back_to_confirm(m, state):
        return
    await _ask_group(m, state)


# ---------- guruh va sana (yangi o'quvchi ham, mavjud o'quvchi ham) ----------
@router.callback_query(F.data.startswith("sag:"))
async def add_group_start(c: CallbackQuery, state: FSMContext) -> None:
    if not await _staff(c.from_user.id):
        return
    sid = int(c.data.split(":")[1])
    await state.clear()
    await state.update_data(sid=sid)
    await c.answer()
    await _ask_group(c.message, state)


async def _ask_group(msg: Message, state: FSMContext) -> None:
    d = await state.get_data()
    t = today()
    async with Session() as s:
        skip = set()
        if d.get("sid"):
            skip = {e.group_id for e in await S.student_enrollments(s, d["sid"])}
        gs = (await s.execute(select(Group).where(Group.active).order_by(Group.subject, Group.name))).scalars().all()
        rows = []
        for g in gs:
            if g.id in skip:
                continue
            n = len(await S.group_roster(s, g.id, t))
            full = g.capacity and n >= g.capacity
            when = f" {g.start_time:%H:%M}" if g.start_time else ""
            rows.append([IB(text=f"{'🔴 ' if full else ''}{g.name} · {DAYS[g.days]}{when} · {n} ta",
                            callback_data=f"rgg:{g.id}")])
    await state.set_state(Rg.group)
    step = "" if d.get("sid") else "4/5. "
    await msg.answer(f"{step}Qaysi guruhga qo'shiladi?", reply_markup=IM(inline_keyboard=rows or
                     [[IB(text="Bo'sh guruh yo'q", callback_data="rgx")]]))


@router.callback_query(Rg.group, F.data.startswith("rgg:"))
async def reg_group(c: CallbackQuery, state: FSMContext) -> None:
    gid = int(c.data.split(":")[1])
    async with Session() as s:
        g = await s.get(Group, gid)
    await state.update_data(gid=gid)
    await state.set_state(Rg.date)
    t = today()
    wd = ["Du", "Se", "Chor", "Pay", "Ju", "Sha", "Yak"]
    days = [nth_lesson_on_or_after(t, g.days, i) for i in (1, 2, 3)]
    kb = IM(inline_keyboard=[[IB(text=("Bugun" if x == t else f"{wd[x.weekday()]}") + f" {x:%d.%m}",
                                 callback_data=f"rgd:{x.isoformat()}") for x in days],
                             [IB(text="⬅️ Guruhni o'zgartirish", callback_data="rgbg")]])
    d = await state.get_data()
    step = "" if d.get("sid") else "5/5. "
    await c.message.edit_text(f"Guruh: <b>{g.name}</b> ✅\n\n{step}Birinchi darsga qaysi kuni keladi (keldi)?\nPastda shu guruhning eng yaqin dars kunlari.\n"
                              f"Boshqa sana bo'lsa yozing: <i>12.11</i>\n"
                              f"(ko'pi bilan {BACK_DAYS} kun oldingi sana)", reply_markup=kb)


@router.callback_query(F.data == "rgbg")
async def reg_back_group(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    if not (d.get("name") or d.get("sid")):
        await c.answer("Ma'lumot eskirgan, qaytadan boshlang.", show_alert=True)
        return
    await c.answer()
    await c.message.edit_text("Guruh qayta tanlanmoqda…")
    await _ask_group(c.message, state)


@router.callback_query(F.data.startswith("rgf:"))
async def reg_fix(c: CallbackQuery, state: FSMContext) -> None:
    """Tekshiruv sahifasida: ism, sinf, telefon, guruh yoki sanani tuzatish."""
    what = c.data.split(":")[1]
    d = await state.get_data()
    if not d.get("gid"):
        await c.answer("Ma'lumot eskirgan, qaytadan boshlang.", show_alert=True)
        return
    await c.answer()
    await state.update_data(fixing=True)
    if what == "n":
        await state.set_state(Rg.name)
        await c.message.answer("Familiya va ismni qaytadan yozing:")
    elif what == "c":
        await _ask_grade(c.message, state)
    elif what == "p":
        await state.set_state(Rg.phone)
        await c.message.answer("Ota-ona telefon raqamini qaytadan yozing: +998901234567")
    elif what == "g":
        await state.update_data(fixing=False)     # guruhdan keyin sana ham qayta so'raladi
        await _ask_group(c.message, state)
    else:
        await state.update_data(fixing=False)
        await state.set_state(Rg.date)
        await c.message.answer("Birinchi dars sanasini yozing: <i>12.11</i>")


@router.callback_query(Rg.date, F.data.startswith("rgd:"))
async def reg_date_btn(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await _confirm(c.message, state, date.fromisoformat(c.data.split(":")[1]))


@router.message(Rg.date)
async def reg_date_text(m: Message, state: FSMContext) -> None:
    x = re.findall(r"\d+", m.text or "")
    t = today()
    try:
        d = date(t.year, int(x[1]), int(x[0]))
    except (IndexError, ValueError):
        await m.answer("Sanani kun.oy ko'rinishida yozing. Masalan: 12.11")
        return
    if (d - t).days > 180:
        d = d.replace(year=t.year - 1)
    elif (t - d).days > 180:
        d = d.replace(year=t.year + 1)
    await _confirm(m, state, d)


async def _confirm(msg: Message, state: FSMContext, join: date) -> None:
    t = today()
    if (t - join).days > BACK_DAYS:
        await msg.answer(f"⚠️ {BACK_DAYS} kundan oldingi sana kiritib bo'lmaydi. Bunday holatda rahbarga ayting.")
        return
    if (join - t).days > AHEAD_DAYS:
        await msg.answer(f"⚠️ Sana juda uzoq. {AHEAD_DAYS} kun ichidagi sanani kiriting.")
        return
    await state.update_data(join=join.isoformat())
    d = await state.get_data()
    async with Session() as s:
        g = await s.get(Group, d["gid"])
        if d.get("sid"):
            stu = await s.get(Student, d["sid"])
            name, tier = stu.full_name, stu.tier
        else:
            name, tier = d["name"], tier_of_grade(d["grade"])
        fee = (await S.get_tier_prices(s, join.year, join.month))[tier]
    first = first_month_due(fee, join, g.days)
    deadline = new_student_deadline(max(join, t), g.days)
    lines = ["<b>Tekshiring:</b>", f"O'quvchi: {name}"]
    if not d.get("sid"):
        grade = d["grade"]
        lines += [f"Sinf: {grade if grade <= 11 else 'bitirgan'}", f"Ota-ona tel: {d['phone']}"]
    lines += [f"Guruh: {g.name} ({DAYS[g.days]})", f"Birinchi dars: {join:%d.%m.%Y}",
              f"Oylik narx: {S.fmt_som(fee)}",
              f"{join:%m}-oy uchun to'lov: {S.fmt_som(first)}" + (" (qolgan darslar bo'yicha)" if first != fee else ""),
              f"To'lov muddati: {deadline:%d.%m.%Y} gacha"]
    fix = [IB(text="✏️ Guruh", callback_data="rgf:g"), IB(text="✏️ Sana", callback_data="rgf:d")]
    if not d.get("sid"):
        fix = [IB(text="✏️ Ism", callback_data="rgf:n"), IB(text="✏️ Sinf", callback_data="rgf:c"),
               IB(text="✏️ Telefon", callback_data="rgf:p")], fix
    else:
        fix = (fix,)
    await state.set_state(Rg.date)     # sanani yozib ham o'zgartirsa bo'ladi
    await msg.answer("\n".join(lines) + "\n\n<i>Xato bo'lsa — pastdan tuzating.</i>", reply_markup=IM(inline_keyboard=[
        *fix, [IB(text="✅ Saqlash", callback_data="rgok"), IB(text="❌ Bekor qilish", callback_data="rgx")]]))


@router.callback_query(F.data == "rgx")
async def reg_cancel(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await c.message.edit_text("Bekor qilindi.")


@router.callback_query(F.data == "rgok")
async def reg_save(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    if not d.get("gid") or not d.get("join"):
        await c.answer("Ma'lumot eskirgan, qaytadan boshlang.", show_alert=True)
        return
    join, t = date.fromisoformat(d["join"]), today()
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        g = await s.get(Group, d["gid"], options=[selectinload(Group.teacher)])
        if d.get("sid"):
            stu = await s.get(Student, d["sid"], options=[selectinload(Student.parents)])
            stu.status = "faol"
            new = False
        else:
            par = (await s.execute(select(Parent).where(Parent.phone == d["phone"]))).scalar_one_or_none()
            stu = Student(full_name=d["name"], grade=d["grade"] if d["grade"] <= 11 else None,
                          tier=tier_of_grade(d["grade"]), source="bot", parents=[par or Parent(phone=d["phone"])])
            s.add(stu)
            new = True
            if d.get("lead"):
                lead = await s.get(Lead, d["lead"])
                lead.status, lead.handled_by = "oquvchi", who.staff.id
        await s.flush()
        enr = Enrollment(student_id=stu.id, group_id=g.id, join_date=join, registered_at=t, tier=stu.tier)
        s.add(enr)
        await S.audit(s, who.staff, "royxat" if new else "guruhga_qoshdi", student=stu.full_name, group=g.name,
                      join=join.isoformat())
        await s.commit()
        eid = enr.id
        parent_tgs = [p.tg_id for p in stu.parents if p.tg_id]
        teacher_tg = g.teacher.tg_id if g.teacher else None
        name, gname, sid = stu.full_name, g.name, stu.id
    await state.clear()
    kb = IM(inline_keyboard=[[IB(text="➕ Yana yangi o'quvchi", callback_data="rgnew")],
                             [IB(text="➕ Shu o'quvchini yana bir guruhga", callback_data=f"sag:{sid}")],
                             [IB(text="🔀 Guruh xato bo'lsa — almashtirish", callback_data=f"rgfx:{eid}")],
                             [IB(text="👤 Kartasini ochish", callback_data=f"st:{sid}")]])
    await c.message.edit_text(f"✅ Saqlandi: <b>{name}</b> → {gname}, {join:%d.%m} dan." +
                              ("\nOta-ona endi o'z raqami bilan botga kira oladi." if new else ""), reply_markup=kb)
    if teacher_tg:
        await send(bot, teacher_tg, f"🆕 Guruhingizga yangi o'quvchi: <b>{name}</b> ({gname}), {join:%d.%m} dan.")
    for p in parent_tgs:
        await send(bot, p, f"✅ {name} «{gname}» guruhiga qo'shildi. Birinchi dars: {join:%d.%m.%Y}.")


# ---------- xato guruhga yozilganini tuzatish (hali dars/to'lov bo'lmasa — oddiy almashtirish) ----------
async def _enr_clean(s, eid: int) -> bool:
    from app.db.models import Attendance, Payment
    att = (await s.execute(select(Attendance.id).where(Attendance.enrollment_id == eid))).first()
    pay = (await s.execute(select(Payment.id).where(Payment.enrollment_id == eid))).first()
    return not att and not pay


@router.callback_query(F.data.startswith("rgfx:"))
async def reg_fix_group(c: CallbackQuery) -> None:
    eid = int(c.data.split(":")[1])
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role in ("admin", "rahbar", "moliya")):
            await c.answer()
            return
        e = await s.get(Enrollment, eid, options=[selectinload(Enrollment.student), selectinload(Enrollment.group)])
        if not await _enr_clean(s, eid):
            await c.answer()
            await c.message.answer("Bu guruhda davomat yoki to'lov bor — oddiy almashtirib bo'lmaydi. "
                                   "Ko'chirish orqali qiling:", reply_markup=IM(inline_keyboard=[[
                                       IB(text="🔀 Boshqa guruhga ko'chirish", callback_data=f"mv:{eid}")]]))
            return
        busy = set((await s.execute(select(Enrollment.group_id).where(
            Enrollment.student_id == e.student_id, Enrollment.active))).scalars())
        gs = (await s.execute(select(Group).where(Group.active).order_by(Group.subject, Group.name))).scalars().all()
    rows = [[IB(text=f"{g.name} · {DAYS[g.days]}", callback_data=f"rgfy:{eid}:{g.id}")] for g in gs if g.id not in busy]
    await c.answer()
    await c.message.answer(f"🔀 <b>{e.student.full_name}</b> xato «{e.group.name}» ga yozilgan. To'g'ri guruhni tanlang:",
                           reply_markup=IM(inline_keyboard=rows or [[IB(text="Boshqa guruh yo'q", callback_data="rgx")]]))


@router.callback_query(F.data.startswith("rgfy:"))
async def reg_fix_group_save(c: CallbackQuery, bot: Bot) -> None:
    _, eid, gid = c.data.split(":")
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        e = await s.get(Enrollment, int(eid), options=[selectinload(Enrollment.student), selectinload(Enrollment.group)])
        if not await _enr_clean(s, e.id):
            await c.answer("Guruhda davomat/to'lov paydo bo'ldi — ko'chirish orqali qiling.", show_alert=True)
            return
        from app.db.models import Staff
        g = await s.get(Group, int(gid))
        old_g = e.group
        old_t = await s.get(Staff, old_g.teacher_id) if old_g.teacher_id else None
        new_t = await s.get(Staff, g.teacher_id) if g.teacher_id else None
        old = e.group.name
        t = today()
        join = e.join_date if e.join_date >= t else t
        if g.days:                    # yangi guruhning eng yaqin dars kuniga
            join = nth_lesson_on_or_after(join, g.days, 1)
        name, gname = e.student.full_name, g.name
        tell = []
        if old_t and old_t.tg_id and old_g.teacher_id != g.teacher_id:
            tell.append((old_t.tg_id, f"↩️ {name} «{old}» guruhiga xato yozilgan edi — olib tashlandi."))
        if new_t and new_t.tg_id:
            tell.append((new_t.tg_id, f"🆕 Guruhingizga yangi o'quvchi: <b>{name}</b> ({gname}), {join:%d.%m} dan."))
        e.group_id, e.join_date = g.id, join
        await S.audit(s, who.staff, "guruh_tuzatish", student=e.student.full_name, old=old, new=g.name)
        await s.commit()
    for t_, txt in tell:
        await send(bot, t_, txt)
    await c.answer("Tuzatildi")
    await c.message.edit_text(f"✅ {name}: «{old}» → <b>«{gname}»</b>. Birinchi dars: {join:%d.%m.%Y}.\n"
                              "Sana boshqa bo'lsa — o'quvchi kartasidan o'zgartiring.")
