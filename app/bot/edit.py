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
EDIT_ROLES = ("admin", "moliya", "rahbar")
DISC = {"none": "Chegirmasiz", "markaz": "Markaz hisobidan", "umumiy": "Umumiy", "ijtimoiy": "Ijtimoiy (100%)"}


class Ed(StatesGroup):
    g_time = State()
    g_room = State()
    st_search = State()
    st_phone = State()
    par_phone = State()
    disc_pct = State()
    staff_add = State()
    st_name = State()
    st_region = State()
    par_replace = State()


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
    await c.answer()


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
    grade = "—" if not stu.grade else ("bitirgan / katta" if stu.grade > 11 else f"{stu.grade}-sinf")
    text = (f"<b>{stu.full_name}</b>\nSinf: {grade} · Hudud: {stu.region or '—'}\n"
            f"Holat: {stu.status}\nCoin: {stu.coins}\nOta-ona telefoni: {par}\n"
            f"O'quvchi telefoni: {stu.phone or '—'}\n\nGuruhlar:\n{grp}")
    rows = [[IB(text="✏️ Tahrirlash", callback_data=f"sx:{sid}")]] if role in EDIT_ROLES else []
    rows += [[IB(text="📞 Ota-ona raqami qo'shish", callback_data=f"spa:{sid}")],
            [IB(text="📱 O'quvchi raqami", callback_data=f"sph:{sid}")],
            [IB(text="➕ Guruhga qo'shish", callback_data=f"sag:{sid}")]]
    if role in ("moliya", "rahbar"):
        rows.append([IB(text="🏷 Chegirma", callback_data=f"sd:{sid}"), IB(text="🪙 Qo'lda coin", callback_data=f"mcn:{sid}")])
    if role in ("rahbar", "admin", "moliya", "oqituvchi"):
        rows += [[IB(text=f"⚙️ {e.group.name}: muzlatish / chiqarish", callback_data=f"en:{e.id}")] for e in enrs]
    return text, IM(inline_keyboard=rows)


@router.callback_query(F.data.startswith("st:"))
async def student_card(c: CallbackQuery) -> None:
    _, role = await _role(c.from_user.id)
    text, kb = await _student_card(int(c.data.split(":")[1]), role)
    await c.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith(("spa:", "sph:")))
async def phone_ask(c: CallbackQuery, state: FSMContext) -> None:
    kind, sid = c.data.split(":")
    await state.set_state(Ed.par_phone if kind == "spa" else Ed.st_phone)
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


# ---------- ✏️ o'quvchini tahrirlash ----------
async def _editor(tg_id: int):
    async with Session() as s:
        who = await S.who_by_tg(s, tg_id)
    st = who.staff
    return st if st and st.current_role in EDIT_ROLES else None


def _edit_kb(sid: int) -> IM:
    return IM(inline_keyboard=[
        [IB(text="👤 Ism-familiya", callback_data=f"sxn:{sid}"), IB(text="🎓 Sinf", callback_data=f"sxg:{sid}")],
        [IB(text="📍 Hudud", callback_data=f"sxr:{sid}"), IB(text="📞 Ota-ona raqamlari", callback_data=f"sxp:{sid}")],
        [IB(text="⬅️ Kartaga qaytish", callback_data=f"st:{sid}")]])


@router.callback_query(F.data.startswith("sx:"))
async def st_edit_menu(c: CallbackQuery, state: FSMContext) -> None:
    if not await _editor(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.clear()
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        stu = await s.get(Student, sid)
    await c.answer()
    await c.message.edit_text(f"✏️ <b>{stu.full_name}</b> — nimani o'zgartiramiz?\n\n"
                              "Guruhni almashtirish: kartadagi «⚙️ guruh» tugmasi → «🔀 Boshqa guruhga ko'chirish».",
                              reply_markup=_edit_kb(sid))


async def _back_to_card(m: Message, sid: int, tg_id: int, note: str) -> None:
    _, role = await _role(tg_id)
    text, kb = await _student_card(sid, role)
    await m.answer(note + "\n\n" + text, reply_markup=kb)


@router.callback_query(F.data.startswith(("sxn:", "sxr:")))
async def st_edit_text_ask(c: CallbackQuery, state: FSMContext) -> None:
    if not await _editor(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    kind, sid = c.data.split(":")
    await state.set_state(Ed.st_name if kind == "sxn" else Ed.st_region)
    await state.update_data(sid=int(sid))
    await c.answer()
    await c.message.answer("Yangi familiya va ismni to'liq yozing (masalan: <code>Karimov Jasur</code>):" if kind == "sxn"
                           else "Hududni yozing (masalan: <code>Xorazm, Urganch tumani</code>):")


@router.message(Ed.st_name)
async def st_name_set(m: Message, state: FSMContext) -> None:
    name = " ".join((m.text or "").split())
    if len(name) < 3 or len(name) > 120 or len(name.split()) < 2 or any(ch.isdigit() for ch in name):
        await m.answer("Familiya va ismni harflar bilan, kamida ikki so'z qilib yozing. Masalan: Karimov Jasur")
        return
    d = await state.get_data()
    async with Session() as s:
        stu = await s.get(Student, d["sid"])
        same = [x for x in await S.search_students(s, name) if x.id != stu.id and x.full_name.lower() == name.lower()]
        old, stu.full_name = stu.full_name, name
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "oquvchi_ism", old=old, new=name)
        await s.commit()
    await state.clear()
    warn = "\n⚠️ Bazada shu ismli boshqa o'quvchi ham bor — adashmaslik uchun tekshiring." if same else ""
    await _back_to_card(m, d["sid"], m.from_user.id, f"✅ Ism o'zgartirildi: {old} → <b>{name}</b>{warn}")


@router.message(Ed.st_region)
async def st_region_set(m: Message, state: FSMContext) -> None:
    reg = " ".join((m.text or "").split())
    if len(reg) < 3 or len(reg) > 80:
        await m.answer("Hududni 3–80 harf oralig'ida yozing. Masalan: Xorazm, Urganch tumani")
        return
    d = await state.get_data()
    async with Session() as s:
        stu = await s.get(Student, d["sid"])
        old, stu.region = stu.region, reg
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "oquvchi_hudud", student=stu.full_name, old=old, new=reg)
        await s.commit()
    await state.clear()
    await _back_to_card(m, d["sid"], m.from_user.id, "✅ Hudud saqlandi.")


@router.callback_query(F.data.startswith("sxg:"))
async def st_grade_ask(c: CallbackQuery) -> None:
    sid = c.data.split(":")[1]
    nums = [IB(text=str(i), callback_data=f"sxgs:{sid}:{i}") for i in range(1, 12)]
    await c.answer()
    await c.message.edit_text("🎓 Nechanchi sinfda o'qiydi?", reply_markup=IM(inline_keyboard=[
        nums[0:4], nums[4:8], nums[8:11], [IB(text="Bitirgan / kattalar", callback_data=f"sxgs:{sid}:12")],
        [IB(text="⬅️ Orqaga", callback_data=f"sx:{sid}")]]))


@router.callback_query(F.data.startswith("sxgs:"))
async def st_grade_set(c: CallbackQuery) -> None:
    from app.logic.money import tier_of_grade
    st = await _editor(c.from_user.id)
    if not st:
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    _, sid, g = c.data.split(":")
    sid, g = int(sid), int(g)
    async with Session() as s:
        stu = await s.get(Student, sid)
        old_g, stu.grade = stu.grade, g
        new_tier = tier_of_grade(g)
        enrs = await S.student_enrollments(s, sid)
        differ = [e for e in enrs if (e.tier or stu.tier) != new_tier and not e.price_override]
        stu.tier = new_tier
        await S.audit(s, st, "oquvchi_sinf", student=stu.full_name, old=old_g, new=g)
        await s.commit()
    gtxt = "bitirgan / katta" if g > 11 else f"{g}-sinf"
    await c.answer("Saqlandi")
    if differ:
        note = (f"✅ Sinf: {gtxt}.\n\nℹ️ Yangi sinf boshqa narx toifasiga to'g'ri keladi, lekin guruhdagi narx "
                "o'zgarmadi. Narxni o'zgartirishni faqat rahbar hal qiladi.")
        kb = [[IB(text="💱 Narxni yangi toifaga o'tkazish", callback_data=f"sxt:{sid}")]] if st.current_role == "rahbar" else []
        kb.append([IB(text="⬅️ Kartaga qaytish", callback_data=f"st:{sid}")])
        await c.message.edit_text(note, reply_markup=IM(inline_keyboard=kb))
        if st.current_role != "rahbar":
            async with Session() as s:
                bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
            for b in bosses:
                await send(c.bot, b, f"🎓 {st.full_name} «{stu.full_name}» sinfini {gtxt} ga o'zgartirdi. "
                                     "Narx toifasi farq qiladi — kerak bo'lsa o'quvchi kartasida hal qiling.",
                           reply_markup=IM(inline_keyboard=[[IB(text="💱 Narxni yangi toifaga o'tkazish",
                                                                callback_data=f"sxt:{sid}")]]))
    else:
        await c.message.edit_text(f"✅ Sinf: {gtxt}.", reply_markup=IM(inline_keyboard=[
            [IB(text="⬅️ Kartaga qaytish", callback_data=f"st:{sid}")]]))


@router.callback_query(F.data.startswith("sxt:"))
async def st_tier_apply(c: CallbackQuery) -> None:
    st = await _editor(c.from_user.id)
    if not st or st.current_role != "rahbar":
        await c.answer("Faqat rahbar", show_alert=True)
        return
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        stu = await s.get(Student, sid)
        n = 0
        for e in await S.student_enrollments(s, sid):
            if not e.price_override and e.tier != stu.tier:
                e.tier, n = stu.tier, n + 1
        await S.audit(s, st, "oquvchi_toifa", student=stu.full_name, tier=stu.tier)
        await s.commit()
    await c.answer("Saqlandi")
    await c.message.edit_text(f"✅ {n} ta guruhda narx yangi toifaga o'tkazildi (shu oy hisobidan).")


@router.callback_query(F.data.startswith("sxp:"))
async def st_parents(c: CallbackQuery) -> None:
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        stu = await s.get(Student, sid, options=[selectinload(Student.parents)], populate_existing=True)
        pars = list(stu.parents)
    rows = []
    for p in pars:
        rows.append([IB(text=f"🔁 {p.phone}", callback_data=f"sxpr:{sid}:{p.id}"),
                     IB(text="🗑 O'chirish", callback_data=f"sxpd:{sid}:{p.id}")])
    rows.append([IB(text="➕ Yangi raqam qo'shish", callback_data=f"spa:{sid}")])
    rows.append([IB(text="⬅️ Orqaga", callback_data=f"sx:{sid}")])
    await c.answer()
    await c.message.edit_text(f"📞 <b>{stu.full_name}</b> — ota-ona raqamlari.\n🔁 — raqamni almashtirish, 🗑 — o'chirish."
                              + ("" if pars else "\n\nHozircha raqam yo'q."), reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data.startswith("sxpr:"))
async def st_parent_replace_ask(c: CallbackQuery, state: FSMContext) -> None:
    if not await _editor(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    _, sid, pid = c.data.split(":")
    await state.set_state(Ed.par_replace)
    await state.update_data(sid=int(sid), pid=int(pid))
    await c.answer()
    await c.message.answer("Yangi raqamni yozing: +998901234567")


async def _unlink_parent(s, stu: Student, par: Parent) -> None:
    """Ota-onani shu o'quvchidan ajratadi; boshqa farzandi qolmasa botga kirishi yopiladi."""
    if par in stu.parents:
        stu.parents.remove(par)
    await s.flush()
    await s.refresh(par, ["children"])
    if not par.children:
        par.tg_id = None


@router.message(Ed.par_replace)
async def st_parent_replace(m: Message, state: FSMContext) -> None:
    phone = S.norm_phone(m.text)
    if not phone:
        await m.answer("Raqam noto'g'ri. Masalan: +998901234567")
        return
    d = await state.get_data()
    async with Session() as s:
        stu = await s.get(Student, d["sid"], options=[selectinload(Student.parents)])
        old = await s.get(Parent, d["pid"])
        if old.phone == phone:
            await state.clear()
            await m.answer("Bu raqam allaqachon shu. O'zgarish yo'q.")
            return
        new = (await s.execute(select(Parent).where(Parent.phone == phone))).scalar_one_or_none() or Parent(phone=phone)
        if new not in stu.parents:
            stu.parents.append(new)
        old_phone = old.phone
        await _unlink_parent(s, stu, old)
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "otaona_tel_almashdi", student=stu.full_name, old=old_phone, new=phone)
        await s.commit()
    await state.clear()
    await _back_to_card(m, d["sid"], m.from_user.id,
                        f"✅ Raqam almashtirildi: {old_phone} → {phone}. Ota-ona yangi raqam bilan botga kiradi.")


@router.callback_query(F.data.startswith("sxpd:"))
async def st_parent_del_ask(c: CallbackQuery) -> None:
    _, sid, pid = c.data.split(":")
    async with Session() as s:
        stu = await s.get(Student, int(sid), options=[selectinload(Student.parents)])
        p = await s.get(Parent, int(pid))
        last = len(stu.parents) <= 1
    if last:
        await c.answer("Bu yagona raqam. Avval yangi raqam qo'shing yoki 🔁 bilan almashtiring.", show_alert=True)
        return
    await c.answer()
    await c.message.edit_text(f"🗑 {p.phone} raqami {stu.full_name} kartasidan o'chirilsinmi?\n"
                              "Bu raqam endi shu o'quvchi haqida xabar olmaydi.",
                              reply_markup=IM(inline_keyboard=[[IB(text="✅ Ha, o'chirish", callback_data=f"sxpy:{sid}:{pid}"),
                                                                IB(text="❌ Yo'q", callback_data=f"sxp:{sid}")]]))


@router.callback_query(F.data.startswith("sxpy:"))
async def st_parent_del(c: CallbackQuery) -> None:
    st = await _editor(c.from_user.id)
    if not st:
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    _, sid, pid = c.data.split(":")
    async with Session() as s:
        stu = await s.get(Student, int(sid), options=[selectinload(Student.parents)])
        p = await s.get(Parent, int(pid))
        if len(stu.parents) <= 1:
            await c.answer("Yagona raqamni o'chirib bo'lmaydi.", show_alert=True)
            return
        phone = p.phone
        await _unlink_parent(s, stu, p)
        await S.audit(s, st, "otaona_tel_ochirildi", student=stu.full_name, phone=phone)
        await s.commit()
    await c.answer("O'chirildi")
    await c.message.edit_text(f"✅ {phone} o'chirildi.", reply_markup=IM(inline_keyboard=[
        [IB(text="📞 Raqamlar", callback_data=f"sxp:{sid}"), IB(text="⬅️ Kartaga", callback_data=f"st:{sid}")]]))


# ---------- chegirma ----------
@router.callback_query(F.data.startswith("sd:"))
async def disc_pick_enr(c: CallbackQuery) -> None:
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        enrs = await S.student_enrollments(s, sid)
    kb = IM(inline_keyboard=[[IB(text=e.group.name, callback_data=f"sde:{e.id}")] for e in enrs])
    await c.message.edit_reply_markup(reply_markup=kb)
    await c.answer()


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
    await c.answer()


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
async def staff_off_ask(c: CallbackQuery) -> None:
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await s.get(Staff, sid)
    await c.answer()
    await c.message.answer(
        f"⚠️ <b>{st.full_name}</b> botdan chiqarilsinmi? U endi botga kira olmaydi.",
        reply_markup=IM(inline_keyboard=[[IB(text="✅ Ha, chiqarish", callback_data=f"sfy:{sid}"),
                                          IB(text="❌ Yo'q", callback_data="sfn")]]))


@router.callback_query(F.data == "sfn")
async def staff_off_no(c: CallbackQuery) -> None:
    await c.answer("Bekor qilindi")
    await c.message.edit_text("Bekor qilindi.")


@router.callback_query(F.data.startswith("sfy:"))
async def staff_off(c: CallbackQuery, bot: Bot) -> None:
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not who.staff or who.staff.current_role != "rahbar":
            await c.answer("Ruxsat yo'q", show_alert=True)
            return
        st = await s.get(Staff, sid)
        st.active = False
        tg = st.tg_id
        st.tg_id = None
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
