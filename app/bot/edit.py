"""Tahrirlash: guruh vaqti/xonasi/kunlari, o'quvchi va ota-ona telefoni, chegirma, xodimlar."""
from __future__ import annotations

import re
from datetime import time

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.bot import keyboards as K
from app.bot.notify import send
from app.db.models import Enrollment, Group, Parent, Staff, Student
from app.db.session import Session
from app import services as S

router = Router()
DAYS = {"toq": "Toq (Du-Chor-Ju)", "juft": "Juft (Se-Pay-Sha)", "har": "Har kuni (Du-Sha)"}
DISC = {"none": "Chegirmasiz", "markaz": "Markaz hisobidan", "umumiy": "Umumiy", "ijtimoiy": "Ijtimoiy (100%)"}


class Ed(StatesGroup):
    g_time = State()
    g_room = State()
    st_search = State()
    st_phone = State()
    par_phone = State()
    disc_pct = State()
    staff_add = State()


async def _role(tg_id: int) -> tuple[Staff | None, str | None]:
    async with Session() as s:
        who = await S.who_by_tg(s, tg_id)
    return (who.staff, who.staff.current_role) if who.staff else (None, None)


# ---------- guruhlar ----------
@router.message(F.text == K.B_GROUPS)
async def groups_list(m: Message) -> None:
    _, role = await _role(m.from_user.id)
    if role not in ("moliya", "rahbar"):
        return
    async with Session() as s:
        gs = (await s.execute(select(Group).where(Group.active).order_by(Group.name))).scalars().all()
    kb = IM(inline_keyboard=[[IB(text=f"{g.name}{'' if g.start_time else ' ⚠️ vaqt yo`q'}", callback_data=f"g:{g.id}")]
                             for g in gs])
    await m.answer("Guruhni tanlang (⚠️ — dars vaqti kiritilmagan):", reply_markup=kb)


async def _group_card(gid: int) -> tuple[str, IM]:
    async with Session() as s:
        g = await s.get(Group, gid, options=[selectinload(Group.teacher)])
        n = len(await S.group_roster(s, gid, __import__("datetime").date.today()))
    t = f"{g.start_time:%H:%M}–{g.end_time:%H:%M}" if g.start_time and g.end_time else "kiritilmagan"
    text = (f"<b>{g.name}</b>\nO'qituvchi: {g.teacher.full_name if g.teacher else '—'}\nKunlar: {DAYS[g.days]}\n"
            f"Vaqt: {t}\nXona: {g.room or '—'}\nO'quvchilar: {n}")
    kb = IM(inline_keyboard=[[IB(text="🕒 Vaqt", callback_data=f"gt:{gid}"), IB(text="🚪 Xona", callback_data=f"gr:{gid}")],
                             [IB(text="📅 Kunlar", callback_data=f"gd:{gid}")],
                             [IB(text="⏸ Guruhni to'xtatish (yoz / uzrli)", callback_data=f"gpz:{gid}")]])
    return text, kb


@router.callback_query(F.data.startswith("g:"))
async def group_card(c: CallbackQuery) -> None:
    text, kb = await _group_card(int(c.data.split(":")[1]))
    await c.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("gt:"))
async def group_time(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Ed.g_time)
    await state.update_data(gid=int(c.data.split(":")[1]))
    await c.message.answer("Dars vaqtini shunday yozing: 14 00 15 30\n(boshlanishi va tugashi, faqat raqamlar)")


@router.message(Ed.g_time)
async def group_time_set(m: Message, state: FSMContext) -> None:
    nums = [int(x) for x in re.findall(r"\d+", m.text or "")]
    if len(nums) != 4 or not (0 <= nums[0] < 24 and 0 <= nums[2] < 24 and nums[1] < 60 and nums[3] < 60):
        await m.answer("Tushunmadim. Masalan: 14 00 15 30")
        return
    st, en = time(nums[0], nums[1]), time(nums[2], nums[3])
    if en <= st:
        await m.answer("Tugash vaqti boshlanishidan keyin bo'lishi kerak.")
        return
    d = await state.get_data()
    async with Session() as s:
        g = await s.get(Group, d["gid"])
        g.start_time, g.end_time = st, en
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "guruh_vaqt", group=g.name, time=f"{st:%H:%M}-{en:%H:%M}")
        await s.commit()
    await state.clear()
    text, kb = await _group_card(d["gid"])
    await m.answer("Saqlandi ✅\n\n" + text, reply_markup=kb)


@router.callback_query(F.data.startswith("gr:"))
async def group_room(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Ed.g_room)
    await state.update_data(gid=int(c.data.split(":")[1]))
    await c.message.answer("Xona nomini yozing (masalan: 2-xona):")


@router.message(Ed.g_room)
async def group_room_set(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    async with Session() as s:
        g = await s.get(Group, d["gid"])
        g.room = (m.text or "").strip()[:30]
        await s.commit()
    await state.clear()
    text, kb = await _group_card(d["gid"])
    await m.answer("Saqlandi ✅\n\n" + text, reply_markup=kb)


@router.callback_query(F.data.startswith("gd:"))
async def group_days(c: CallbackQuery) -> None:
    gid = int(c.data.split(":")[1])
    kb = IM(inline_keyboard=[[IB(text=v, callback_data=f"gds:{gid}:{k}")] for k, v in DAYS.items()])
    await c.message.edit_reply_markup(reply_markup=kb)


@router.callback_query(F.data.startswith("gds:"))
async def group_days_set(c: CallbackQuery) -> None:
    _, gid, days = c.data.split(":")
    async with Session() as s:
        g = await s.get(Group, int(gid))
        g.days = days
        who = await S.who_by_tg(s, c.from_user.id)
        await S.audit(s, who.staff, "guruh_kunlar", group=g.name, days=days)
        await s.commit()
    text, kb = await _group_card(int(gid))
    await c.message.edit_text("Saqlandi ✅\n\n" + text, reply_markup=kb)


# ---------- o'quvchi kartasi ----------
@router.message(F.text == K.B_STUDENT)
async def student_find(m: Message, state: FSMContext) -> None:
    _, role = await _role(m.from_user.id)
    if role not in ("admin", "moliya", "rahbar"):
        return
    await state.set_state(Ed.st_search)
    await m.answer("O'quvchi ismidan kamida 2 harf yozing:")


@router.message(Ed.st_search)
async def student_search(m: Message, state: FSMContext) -> None:
    async with Session() as s:
        found = await S.search_students(s, m.text or "")
    if not found:
        await m.answer("Topilmadi. Boshqacha yozib ko'ring.")
        return
    await state.clear()
    await m.answer("Tanlang:", reply_markup=IM(inline_keyboard=[[IB(text=x.full_name, callback_data=f"st:{x.id}")]
                                                              for x in found]))


async def _student_card(sid: int, role: str) -> tuple[str, IM]:
    async with Session() as s:
        enrs = await S.student_enrollments(s, sid)
        fees = {e.id: await S.enrollment_fee(s, e) for e in enrs}
        stu = await s.get(Student, sid, options=[selectinload(Student.parents)], populate_existing=True)
        par = ", ".join(p.phone + (" (botda)" if p.tg_id else "") for p in stu.parents) or "—"
    grp = "\n".join(f"• {e.group.name} — {S.fmt_som(fees[e.id])}" + (" (istisno narx)" if e.price_override else "")
                    + (f", {DISC[e.discount_kind]} {e.discount_percent}%" if e.discount_kind != "none" else "")
                    for e in enrs) or "—"
    text = (f"<b>{stu.full_name}</b>\nHolat: {stu.status}\nCoin: {stu.coins}\nOta-ona telefoni: {par}\n"
            f"O'quvchi telefoni: {stu.phone or '—'}\n\nGuruhlar:\n{grp}")
    rows = [[IB(text="📞 Ota-ona raqami qo'shish", callback_data=f"spp:{sid}")],
            [IB(text="📱 O'quvchi raqami", callback_data=f"sph:{sid}")],
            [IB(text="➕ Guruhga qo'shish", callback_data=f"sag:{sid}")]]
    if role in ("moliya", "rahbar"):
        rows.append([IB(text="🏷 Chegirma", callback_data=f"sd:{sid}")])
    if role in ("rahbar", "admin", "moliya", "oqituvchi"):
        rows += [[IB(text=f"⚙️ {e.group.name}: muzlatish / chiqarish", callback_data=f"en:{e.id}")] for e in enrs]
    return text, IM(inline_keyboard=rows)


@router.callback_query(F.data.startswith("st:"))
async def student_card(c: CallbackQuery) -> None:
    _, role = await _role(c.from_user.id)
    text, kb = await _student_card(int(c.data.split(":")[1]), role)
    await c.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith(("spp:", "sph:")))
async def phone_ask(c: CallbackQuery, state: FSMContext) -> None:
    kind, sid = c.data.split(":")
    await state.set_state(Ed.par_phone if kind == "spp" else Ed.st_phone)
    await state.update_data(sid=int(sid))
    await c.message.answer("Telefon raqamini yozing: +998901234567")


@router.message(Ed.par_phone)
async def parent_phone_set(m: Message, state: FSMContext) -> None:
    phone = S.norm_phone(m.text)
    if not phone:
        await m.answer("Raqam noto'g'ri. Masalan: +998901234567")
        return
    d = await state.get_data()
    async with Session() as s:
        stu = await s.get(Student, d["sid"], options=[selectinload(Student.parents)])
        par = (await s.execute(select(Parent).where(Parent.phone == phone))).scalar_one_or_none() or Parent(phone=phone)
        if par not in stu.parents:
            stu.parents.append(par)
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "otaona_tel", student=stu.full_name, phone=phone)
        await s.commit()
        role = who.staff.current_role
    await state.clear()
    text, kb = await _student_card(d["sid"], role)
    await m.answer("Saqlandi ✅ Ota-ona endi shu raqam bilan botga kira oladi.\n\n" + text, reply_markup=kb)


@router.message(Ed.st_phone)
async def student_phone_set(m: Message, state: FSMContext) -> None:
    phone = S.norm_phone(m.text)
    if not phone:
        await m.answer("Raqam noto'g'ri. Masalan: +998901234567")
        return
    d = await state.get_data()
    async with Session() as s:
        stu = await s.get(Student, d["sid"])
        stu.phone = phone
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "oquvchi_tel", student=stu.full_name, phone=phone)
        await s.commit()
        role = who.staff.current_role
    await state.clear()
    text, kb = await _student_card(d["sid"], role)
    await m.answer("Saqlandi ✅\n\n" + text, reply_markup=kb)


# ---------- chegirma ----------
@router.callback_query(F.data.startswith("sd:"))
async def disc_pick_enr(c: CallbackQuery) -> None:
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        enrs = await S.student_enrollments(s, sid)
    kb = IM(inline_keyboard=[[IB(text=e.group.name, callback_data=f"sde:{e.id}")] for e in enrs])
    await c.message.edit_reply_markup(reply_markup=kb)


@router.callback_query(F.data.startswith("sde:"))
async def disc_kind(c: CallbackQuery) -> None:
    eid = int(c.data.split(":")[1])
    staff, role = await _role(c.from_user.id)
    async with Session() as s:
        e = await s.get(Enrollment, eid, options=[selectinload(Enrollment.group)])
        if role != "rahbar" and e.group.teacher_id == staff.id:
            await c.answer("Bu o'z guruhingiz o'quvchisi — chegirmani faqat rahbar beradi.", show_alert=True)
            return
    kb = IM(inline_keyboard=[[IB(text=v, callback_data=f"sdk:{eid}:{k}")] for k, v in DISC.items()])
    await c.message.edit_reply_markup(reply_markup=kb)


@router.callback_query(F.data.startswith("sdk:"))
async def disc_kind_set(c: CallbackQuery, state: FSMContext) -> None:
    _, eid, kind = c.data.split(":")
    if kind in ("none", "ijtimoiy"):
        await _save_disc(c.message, c.from_user.id, int(eid), kind, 100 if kind == "ijtimoiy" else 0)
        return
    await state.set_state(Ed.disc_pct)
    await state.update_data(eid=int(eid), kind=kind)
    await c.message.answer("Chegirma foizini yozing (masalan 20):")


@router.message(Ed.disc_pct)
async def disc_pct(m: Message, state: FSMContext) -> None:
    digits = "".join(ch for ch in (m.text or "") if ch.isdigit())
    pct = int(digits) if digits else -1
    if not 1 <= pct <= 100:
        await m.answer("1 dan 100 gacha raqam yozing.")
        return
    d = await state.get_data()
    await state.clear()
    await _save_disc(m, m.from_user.id, d["eid"], d["kind"], pct)


async def _save_disc(msg: Message, tg_id: int, eid: int, kind: str, pct: int) -> None:
    async with Session() as s:
        e = await s.get(Enrollment, eid, options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
        e.discount_kind, e.discount_percent = kind, pct
        who = await S.who_by_tg(s, tg_id)
        await S.audit(s, who.staff, "chegirma", student=e.student.full_name, group=e.group.name, kind=kind, pct=pct)
        await s.commit()
    await msg.answer(f"Saqlandi ✅ {e.student.full_name} · {e.group.name}: {DISC[kind]}"
                     + (f" {pct}%" if kind in ("markaz", "umumiy") else ""))


# ---------- xodimlar (rahbar) ----------
@router.message(F.text == K.B_STAFF)
async def staff_list(m: Message, state: FSMContext) -> None:
    _, role = await _role(m.from_user.id)
    if role != "rahbar":
        return
    async with Session() as s:
        rows = (await s.execute(select(Staff).order_by(Staff.full_name))).scalars().all()
    lines = ["<b>Xodimlar</b>"] + [
        f"{'✅' if x.active else '⛔'} {x.full_name} · {', '.join(S.ROLE_TITLES[r] for r in x.role_list())} · "
        f"{x.phone or 'raqam yo`q'}{' · botda' if x.tg_id else ''}" for x in rows]
    kb = IM(inline_keyboard=[[IB(text="➕ Xodim qo'shish", callback_data="sfa")]] +
            [[IB(text=f"⛔ {x.full_name} — chiqarish", callback_data=f"sfo:{x.id}")] for x in rows
             if x.active and "rahbar" not in x.role_list()])
    await m.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data == "sfa")
async def staff_add(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Ed.staff_add)
    await c.message.answer("Shunday yozing (nuqtali vergul bilan):\nIsm Familiya; +998901234567; lavozimlar\n"
                           "Lavozimlar: rahbar, moliya, admin, oqituvchi, yordamchi (vergul bilan)")


@router.message(Ed.staff_add)
async def staff_add_set(m: Message, state: FSMContext) -> None:
    parts = [p.strip() for p in (m.text or "").split(";")]
    phone = S.norm_phone(parts[1]) if len(parts) == 3 else None
    roles = [r.strip().lower().replace("'", "") for r in parts[2].split(",")] if len(parts) == 3 else []
    if not phone or not roles or any(r not in S.ROLE_TITLES for r in roles):
        await m.answer("Tushunmadim. Masalan: Ali Valiyev; +998901234567; oqituvchi, moliya")
        return
    async with Session() as s:
        st = (await s.execute(select(Staff).where(Staff.phone == phone))).scalar_one_or_none() or Staff(phone=phone)
        st.full_name, st.roles, st.active, st.current_role = parts[0], ",".join(roles), True, roles[0]
        s.add(st)
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "xodim_qoshish", name=parts[0], roles=roles)
        await s.commit()
    await state.clear()
    await m.answer(f"Saqlandi ✅ {parts[0]} endi {phone} raqami bilan botga kira oladi.")


@router.callback_query(F.data.startswith("sfo:"))
async def staff_off(c: CallbackQuery, bot: Bot) -> None:
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await s.get(Staff, sid)
        st.active = False
        tg = st.tg_id
        st.tg_id = None
        who = await S.who_by_tg(s, c.from_user.id)
        await S.audit(s, who.staff, "xodim_chiqarish", name=st.full_name)
        await s.commit()
    await c.answer(f"{st.full_name} botdan chiqarildi", show_alert=True)
    if tg:
        await send(bot, tg, "Sizning botdagi xodim huquqingiz o'chirildi.")


# ---------- boshlang'ich ma'lumotlarni yuklash (faqat rahbar, faqat bo'sh bazaga) ----------
@router.message(F.document.file_name.endswith(".json"))
async def import_json(m: Message, bot: Bot) -> None:
    import json
    from app.importer.bundle import import_bundle
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
    if not (who.staff and "rahbar" in who.staff.role_list()):
        return
    f = await bot.download(m.document)
    try:
        data = json.loads(f.read().decode("utf-8"))
        async with Session() as s:
            res = await import_bundle(s, data)
            await S.audit(s, who.staff, "import", **res)
            await s.commit()
    except Exception as e:  # noqa: BLE001
        await m.answer(f"Yuklab bo'lmadi: {e}")
        return
    await m.answer("✅ Ma'lumotlar yuklandi:\n" + "\n".join(f"{k}: {v}" for k, v in res.items()))
