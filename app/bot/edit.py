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
    kb = IM(inline_keyboard=[[IB(text="➕ Yangi guruh ochish", callback_data="gnew")]] +
            [[IB(text=f"{g.name}{'' if g.start_time else ' ⚠️ vaqt yo`q'}", callback_data=f"g:{g.id}")] for g in gs])
    await m.answer("Guruhni tanlang (⚠️ — dars vaqti kiritilmagan):", reply_markup=kb)


async def _group_card(gid: int) -> tuple[str, IM]:
    async with Session() as s:
        g = await s.get(Group, gid, options=[selectinload(Group.teacher)])
        n = len(await S.group_roster(s, gid, __import__("datetime").date.today()))
    t = f"{g.start_time:%H:%M}–{g.end_time:%H:%M}" if g.start_time and g.end_time else "kiritilmagan"
    async with Session() as s:
        asst = await s.get(Staff, g.assistant_id) if g.assistant_id else None
    text = (f"<b>{g.name}</b>\nFan: {g.subject or '—'}\nO'qituvchi: {g.teacher.full_name if g.teacher else '—'}\n"
            f"Yordamchi: {asst.full_name if asst else '—'}\nKunlar: {DAYS[g.days]}\n"
            f"Vaqt: {t}\nXona: {g.room or '—'}\nO'quvchilar: {n}")
    kb = IM(inline_keyboard=[[IB(text="🕒 Vaqt", callback_data=f"gt:{gid}"), IB(text="🚪 Xona", callback_data=f"gr:{gid}")],
                             [IB(text="📅 Kunlar", callback_data=f"gd:{gid}"), IB(text="✏️ Nomi", callback_data=f"gnm:{gid}"),
                              IB(text="📚 Fan", callback_data=f"gsb:{gid}")],
                             [IB(text="👨‍🏫 O'qituvchi", callback_data=f"gtc:{gid}"),
                              IB(text="🤝 Yordamchi", callback_data=f"gas:{gid}")],
                             [IB(text="👥 O'quvchilar ro'yxati", callback_data=f"gls:{gid}"),
                              IB(text="🗂 O'tgan darslar", callback_data=f"lsn:{gid}")],
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
    await m.answer("O'quvchi ismidan kamida 2 harf yozing yoki ro'yxatni oching:", reply_markup=IM(inline_keyboard=[
        [IB(text="📋 Guruh bo'yicha ro'yxat", callback_data="gla")],
        [IB(text="📥 Barcha o'quvchilar — Excel", callback_data="stx")]]))


@router.message(Ed.st_search)
async def student_search(m: Message, state: FSMContext) -> None:
    async with Session() as s:
        found = await S.search_students(s, m.text or "")
    if not found:
        await m.answer("Topilmadi. Boshqacha yozib ko'ring.")
        return
    await state.clear()
    async with Session() as s:
        labels = await S.student_labels(s, found)
    await m.answer("Tanlang (guruhi va ota-ona telefoni oxiriga qarang):", reply_markup=IM(inline_keyboard=[
        [IB(text=labels[x.id], callback_data=f"st:{x.id}")] for x in found]))


async def _student_card(sid: int, role: str) -> tuple[str, IM]:
    async with Session() as s:
        enrs = await S.student_enrollments(s, sid)
        fees = {e.id: await S.enrollment_fee(s, e) for e in enrs}
        stu = await s.get(Student, sid, options=[selectinload(Student.parents)], populate_existing=True)
        par = ", ".join(p.phone + (" (botda)" if p.tg_id else "") for p in stu.parents) or "—"
    grp = "\n".join(f"• {e.group.name} — {S.fmt_som(fees[e.id])}" + (" (istisno narx)" if e.price_override else "")
                    + (f", chegirma: {S.discount_label(e)}" if e.discount_kind != "none" else "")
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
        rows.append([IB(text="💵 To'lovlar (bekor qilish)", callback_data=f"pl:{sid}")])
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
    async with Session() as s:
        e = await s.get(Enrollment, int(eid), options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
        fee = await S.enrollment_fee(s, e)
    await state.set_state(Ed.disc_pct)
    await state.update_data(eid=int(eid), kind=kind, fee=fee)
    await c.message.answer(f"Oylik narx: {S.fmt_som(fee)}.\nChegirmani yozing:\n"
                           "• <b>foiz</b> bilan — masalan <code>20%</code>\n"
                           "• <b>summa</b> bilan (oyiga necha so'm kam to'laydi) — masalan <code>50000</code>")


def parse_discount(text: str, fee: int) -> tuple[str, int] | str:
    """('pct', 20) yoki ('sum', 50000) yoki xato matni. 100 gacha son — foiz, 1000 dan katta — summa."""
    t = (text or "").replace(" ", "").replace(",", "").lower()
    digits = "".join(ch for ch in t if ch.isdigit())
    if not digits:
        return "Raqam yozing: 20% yoki 50000"
    n = int(digits)
    if "%" in t or n <= 100:
        return ("pct", n) if 1 <= n <= 100 else "Foiz 1 dan 100 gacha bo'lsin."
    if n < 1000:
        return "Summani to'liq yozing (masalan 50000) yoki foiz uchun % qo'ying (masalan 20%)."
    if fee and n >= fee:
        return f"Chegirma oylik narxdan ({S.fmt_som(fee)}) kam bo'lsin. To'liq ozod qilish uchun «Ijtimoiy (100%)» ni tanlang."
    return ("sum", n)


@router.message(Ed.disc_pct)
async def disc_pct(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    res = parse_discount(m.text or "", d.get("fee") or 0)
    if isinstance(res, str):
        await m.answer(res)
        return
    await state.clear()
    kind_v, val = res
    await _save_disc(m, m.from_user.id, d["eid"], d["kind"], val if kind_v == "pct" else 0,
                     amount=val if kind_v == "sum" else None)


async def _save_disc(msg: Message, tg_id: int, eid: int, kind: str, pct: int, amount: int | None = None) -> None:
    async with Session() as s:
        e = await s.get(Enrollment, eid, options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
        e.discount_kind, e.discount_percent, e.discount_amount = kind, pct, amount
        who = await S.who_by_tg(s, tg_id)
        await S.audit(s, who.staff, "chegirma", student=e.student.full_name, group=e.group.name, kind=kind, pct=pct,
                      amount=amount)
        await s.commit()
        fee = await S.enrollment_fee(s, e)
        label = S.discount_label(e)
    pay = fee - amount if amount else round(fee * (100 - pct) / 100) if kind in ("markaz", "umumiy") else (0 if kind == "ijtimoiy" else fee)
    await msg.answer(f"Saqlandi ✅ {e.student.full_name} · {e.group.name}: {label or DISC[kind]}\n"
                     f"To'liq oy uchun to'lov: <b>{S.fmt_som(pay)}</b> (narx {S.fmt_som(fee)}).")


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
            [[IB(text=f"✏️ {x.full_name}", callback_data=f"sfe:{x.id}")] for x in rows if x.active] +
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


# ================= 👥 xodimni tahrirlash (rahbar) =================
class Sf(StatesGroup):
    name = State()
    phone = State()


ROLE_DRAFT: dict[tuple[int, int], set[str]] = {}


async def _boss_staff(tg: int) -> Staff | None:
    st, role = await _role(tg)
    return st if role == "rahbar" else None


def _staff_edit_kb(sid: int) -> IM:
    return IM(inline_keyboard=[[IB(text="👤 Ism-familiya", callback_data=f"sfen:{sid}"),
                                IB(text="📞 Telefon", callback_data=f"sfep:{sid}")],
                               [IB(text="🎭 Lavozimlar", callback_data=f"sfer:{sid}")]])


@router.callback_query(F.data.startswith("sfe:"))
async def staff_edit(c: CallbackQuery) -> None:
    if not await _boss_staff(c.from_user.id):
        await c.answer("Faqat rahbar", show_alert=True)
        return
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        x = await s.get(Staff, sid)
    await c.answer()
    await c.message.answer(f"✏️ <b>{x.full_name}</b>\nTelefon: {x.phone or '—'}\n"
                           f"Lavozimlar: {', '.join(S.ROLE_TITLES[r] for r in x.role_list())}",
                           reply_markup=_staff_edit_kb(sid))


@router.callback_query(F.data.startswith(("sfen:", "sfep:")))
async def staff_field_ask(c: CallbackQuery, state: FSMContext) -> None:
    if not await _boss_staff(c.from_user.id):
        await c.answer("Faqat rahbar", show_alert=True)
        return
    kind, sid = c.data.split(":")
    await state.set_state(Sf.name if kind == "sfen" else Sf.phone)
    await state.update_data(sid=int(sid))
    await c.answer()
    await c.message.answer("Yangi ism-familiyani yozing:" if kind == "sfen" else
                           "Yangi telefon raqamini yozing: +998901234567\n"
                           "Xodim eski raqamdan chiqariladi va yangi raqam bilan /start qilib kiradi.")


@router.message(Sf.name)
async def staff_name_set(m: Message, state: FSMContext) -> None:
    name = " ".join((m.text or "").split())
    if len(name) < 3 or len(name.split()) < 2 or any(ch.isdigit() for ch in name):
        await m.answer("Ism va familiyani yozing. Masalan: Ali Valiyev")
        return
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        x = await s.get(Staff, d["sid"])
        old, x.full_name = x.full_name, name
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "xodim_ism", old=old, new=name)
        await s.commit()
    await m.answer(f"✅ {old} → <b>{name}</b>. Ballar, oylik va tarix saqlanadi.")


@router.message(Sf.phone)
async def staff_phone_set(m: Message, state: FSMContext, bot: Bot) -> None:
    phone = S.norm_phone(m.text or "")
    if not phone:
        await m.answer("Raqam noto'g'ri. Masalan: +998901234567")
        return
    d = await state.get_data()
    async with Session() as s:
        busy = (await s.execute(select(Staff).where(Staff.phone == phone, Staff.id != d["sid"]))).scalar_one_or_none()
        if busy:
            await m.answer(f"Bu raqam boshqa xodimda ({busy.full_name}). Boshqa raqam yozing.")
            return
        await state.clear()
        x = await s.get(Staff, d["sid"])
        old, old_tg = x.phone, x.tg_id
        if old == phone:
            await m.answer("Raqam o'zgarmadi.")
            return
        x.phone, x.tg_id = phone, None
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "xodim_tel", staff=x.full_name, old=old, new=phone)
        await s.commit()
        name = x.full_name
    if old_tg and old_tg != m.from_user.id:
        await send(bot, old_tg, f"ℹ️ Botdagi raqamingiz {phone} ga o'zgartirildi. Yangi raqamdagi Telegram bilan /start qiling.")
    await m.answer(f"✅ {name}: {old or '—'} → {phone}. Xodim yangi raqam bilan /start qilib kiradi; "
                   "ballari, oyligi va tarixi saqlanadi.")


def _roles_kb(sid: int, roles: set[str]) -> IM:
    kb = [[IB(text=("✅ " if r in roles else "▫️ ") + t, callback_data=f"sfrt:{sid}:{r}")] for r, t in S.ROLE_TITLES.items()]
    kb.append([IB(text="💾 Saqlash", callback_data=f"sfrk:{sid}")])
    return IM(inline_keyboard=kb)


@router.callback_query(F.data.startswith("sfer:"))
async def staff_roles(c: CallbackQuery) -> None:
    if not await _boss_staff(c.from_user.id):
        await c.answer("Faqat rahbar", show_alert=True)
        return
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        x = await s.get(Staff, sid)
    ROLE_DRAFT[(c.from_user.id, sid)] = set(x.role_list())
    await c.answer()
    await c.message.answer(f"🎭 <b>{x.full_name}</b> lavozimlari (bosib yoqing/o'chiring):",
                           reply_markup=_roles_kb(sid, ROLE_DRAFT[(c.from_user.id, sid)]))


@router.callback_query(F.data.startswith("sfrt:"))
async def staff_role_toggle(c: CallbackQuery) -> None:
    _, sid, r = c.data.split(":")
    key = (c.from_user.id, int(sid))
    if key not in ROLE_DRAFT:
        await c.answer("Qaytadan oching.", show_alert=True)
        return
    ROLE_DRAFT[key] ^= {r}
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=_roles_kb(int(sid), ROLE_DRAFT[key]))


@router.callback_query(F.data.startswith("sfrk:"))
async def staff_roles_save(c: CallbackQuery, bot: Bot) -> None:
    sid = int(c.data.split(":")[1])
    me = await _boss_staff(c.from_user.id)
    roles = ROLE_DRAFT.get((c.from_user.id, sid))
    if not me or roles is None:
        await c.answer("Qaytadan oching.", show_alert=True)
        return
    if not roles:
        await c.answer("Kamida bitta lavozim qolsin.", show_alert=True)
        return
    if sid == me.id and "rahbar" not in roles:
        await c.answer("O'zingizdan rahbar lavozimini olib bo'lmaydi.", show_alert=True)
        return
    ROLE_DRAFT.pop((c.from_user.id, sid), None)
    order = [r for r in S.ROLE_TITLES if r in roles]
    async with Session() as s:
        x = await s.get(Staff, sid)
        old = x.role_list()
        x.roles = ",".join(order)
        if x.current_role not in order:
            x.current_role = order[0]
        await S.audit(s, me, "xodim_lavozim", staff=x.full_name, old=old, new=order)
        await s.commit()
        tg, name = x.tg_id, x.full_name
    titles = ", ".join(S.ROLE_TITLES[r] for r in order)
    await c.answer("Saqlandi")
    await c.message.edit_text(f"✅ {name}: {titles}.\nBallar shu oy saqlanadi; yangi lavozim KPI si ertadan hisoblanadi.")
    if tg and tg != c.from_user.id:
        await send(bot, tg, f"ℹ️ Lavozimlaringiz yangilandi: {titles}. Menyuni yangilash uchun /start bosing.")


# ================= ⚙️ guruh: yangi guruh, nom, fan, o'qituvchi, yordamchi =================
class Gm(StatesGroup):
    name = State()
    subject = State()
    time = State()
    room = State()
    rename = State()
    resubj = State()


async def _group_editor(tg: int) -> Staff | None:
    st, role = await _role(tg)
    return st if role in ("rahbar", "moliya") else None


def _parse_time(text: str) -> tuple[time, time] | None:
    nums = [int(x) for x in re.findall(r"\d+", text or "")]
    if len(nums) != 4 or not (0 <= nums[0] < 24 and 0 <= nums[2] < 24 and nums[1] < 60 and nums[3] < 60):
        return None
    a, b = time(nums[0], nums[1]), time(nums[2], nums[3])
    return (a, b) if b > a else None


@router.callback_query(F.data == "gnew")
async def gnew(c: CallbackQuery, state: FSMContext) -> None:
    if not await _group_editor(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.clear()
    await state.set_state(Gm.name)
    await c.answer()
    await c.message.answer("➕ <b>Yangi guruh</b>\n1/6. Guruh nomi (masalan: Matematika toq 2):")


@router.message(Gm.name)
async def gnew_name(m: Message, state: FSMContext) -> None:
    name = " ".join((m.text or "").split())[:120]
    if len(name) < 2:
        await m.answer("Nomini yozing.")
        return
    async with Session() as s:
        if (await s.execute(select(Group.id).where(Group.name == name))).first():
            await m.answer("Bu nomli guruh bor. Boshqa nom yozing.")
            return
    await state.update_data(name=name)
    await state.set_state(Gm.subject)
    await m.answer("2/6. Fan (masalan: Matematika):")


@router.message(Gm.subject)
async def gnew_subject(m: Message, state: FSMContext) -> None:
    await state.update_data(subject=" ".join((m.text or "").split())[:60] or None)
    await state.set_state(None)
    await m.answer("3/6. Dars kunlari:", reply_markup=IM(inline_keyboard=[[IB(text=v, callback_data=f"gnd:{k}")]
                                                                       for k, v in DAYS.items()]))


@router.callback_query(F.data.startswith("gnd:"))
async def gnew_days(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    if "name" not in d:
        await c.answer("Qaytadan boshlang.", show_alert=True)
        return
    await state.update_data(days=c.data.split(":")[1])
    await state.set_state(Gm.time)
    await c.answer()
    await c.message.edit_text(f"Kunlar: {DAYS[c.data.split(':')[1]]} ✅\n4/6. Dars vaqti: 14 00 15 30 (boshlanishi va tugashi)")


@router.message(Gm.time)
async def gnew_time(m: Message, state: FSMContext) -> None:
    tt = _parse_time(m.text or "")
    if not tt:
        await m.answer("Masalan: 14 00 15 30 (tugashi boshlanishidan keyin)")
        return
    await state.update_data(t1=tt[0].isoformat(), t2=tt[1].isoformat())
    await state.set_state(Gm.room)
    await m.answer("5/6. Xona (masalan: 2-xona) yoki «-»:")


@router.message(Gm.room)
async def gnew_room(m: Message, state: FSMContext) -> None:
    room = (m.text or "").strip()[:30]
    await state.update_data(room=None if room in ("-", "") else room)
    await state.set_state(None)
    async with Session() as s:
        ts = [x for x in (await s.execute(select(Staff).where(Staff.active).order_by(Staff.full_name))).scalars()
              if "oqituvchi" in x.role_list()]
    kb = [[IB(text=x.full_name, callback_data=f"gnt:{x.id}")] for x in ts]
    kb.append([IB(text="Keyin belgilayman", callback_data="gnt:0")])
    await m.answer("6/6. O'qituvchi:", reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("gnt:"))
async def gnew_save(c: CallbackQuery, state: FSMContext) -> None:
    st = await _group_editor(c.from_user.id)
    d = await state.get_data()
    if not st or "t1" not in d:
        await c.answer("Qaytadan boshlang.", show_alert=True)
        return
    tid = int(c.data.split(":")[1]) or None
    await state.clear()
    async with Session() as s:
        if (await s.execute(select(Group.id).where(Group.name == d["name"]))).first():
            await c.answer("Bu nomli guruh allaqachon bor.", show_alert=True)
            return
        g = Group(name=d["name"], subject=d.get("subject"), days=d["days"], start_time=time.fromisoformat(d["t1"]),
                  end_time=time.fromisoformat(d["t2"]), room=d.get("room"), teacher_id=tid, active=True)
        s.add(g)
        await s.flush()
        if tid:
            from app.db.models import GroupTeacher
            s.add(GroupTeacher(group_id=g.id, teacher_id=tid, since=_utcnow()))
        await S.audit(s, st, "guruh_ochildi", group=g.name, teacher=tid)
        await s.commit()
        gid = g.id
    await c.answer("Guruh ochildi")
    text, kb = await _group_card(gid)
    await c.message.edit_text("✅ Yangi guruh ochildi. Endi o'quvchilarni «➕ Yangi o'quvchi» yoki kartadagi "
                              "«➕ Guruhga qo'shish» orqali qo'shing.\n\n" + text, reply_markup=kb)


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.callback_query(F.data.startswith(("gnm:", "gsb:")))
async def group_text_ask(c: CallbackQuery, state: FSMContext) -> None:
    if not await _group_editor(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    kind, gid = c.data.split(":")
    await state.set_state(Gm.rename if kind == "gnm" else Gm.resubj)
    await state.update_data(gid=int(gid))
    await c.answer()
    await c.message.answer("Guruhning yangi nomini yozing:" if kind == "gnm" else "Fan nomini yozing:")


@router.message(Gm.rename)
async def group_rename(m: Message, state: FSMContext) -> None:
    name = " ".join((m.text or "").split())[:120]
    d = await state.get_data()
    async with Session() as s:
        if len(name) < 2 or (await s.execute(select(Group.id).where(Group.name == name, Group.id != d["gid"]))).first():
            await m.answer("Bu nom bo'sh yoki boshqa guruhda bor. Boshqa nom yozing.")
            return
        await state.clear()
        g = await s.get(Group, d["gid"])
        old, g.name = g.name, name
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "guruh_nomi", old=old, new=name)
        await s.commit()
    text, kb = await _group_card(d["gid"])
    await m.answer(f"✅ {old} → {name}\n\n" + text, reply_markup=kb)


@router.message(Gm.resubj)
async def group_subject(m: Message, state: FSMContext) -> None:
    subj = " ".join((m.text or "").split())[:60]
    if len(subj) < 2:
        await m.answer("Fan nomini yozing.")
        return
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        g = await s.get(Group, d["gid"])
        old, g.subject = g.subject, subj
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "guruh_fan", group=g.name, old=old, new=subj)
        await s.commit()
    text, kb = await _group_card(d["gid"])
    await m.answer("✅ Saqlandi\n\n" + text, reply_markup=kb)


@router.callback_query(F.data.startswith("gtc:"))
async def group_teacher_list(c: CallbackQuery) -> None:
    if not await _boss_staff(c.from_user.id):
        await c.answer("O'qituvchini faqat rahbar almashtiradi", show_alert=True)
        return
    gid = int(c.data.split(":")[1])
    async with Session() as s:
        g = await s.get(Group, gid)
        ts = [x for x in (await s.execute(select(Staff).where(Staff.active).order_by(Staff.full_name))).scalars()
              if "oqituvchi" in x.role_list()]
    await c.answer()
    await c.message.answer(f"👨‍🏫 «{g.name}» guruhiga yangi o'qituvchi.\nBugundan keyingi to'lovlar ulushi yangi "
                           "o'qituvchiga hisoblanadi, avvalgilari eski o'qituvchida qoladi.",
                           reply_markup=IM(inline_keyboard=[[IB(text=("✅ " if x.id == g.teacher_id else "") + x.full_name,
                                                                callback_data=f"gtcs:{gid}:{x.id}")] for x in ts]))


@router.callback_query(F.data.startswith("gtcs:"))
async def group_teacher_set(c: CallbackQuery, bot: Bot) -> None:
    me = await _boss_staff(c.from_user.id)
    if not me:
        await c.answer("Faqat rahbar", show_alert=True)
        return
    _, gid, tid = c.data.split(":")
    from app.db.models import GroupTeacher
    async with Session() as s:
        g = await s.get(Group, int(gid))
        if g.teacher_id == int(tid):
            await c.answer("Bu o'qituvchi allaqachon shu guruhda.")
            return
        old = await s.get(Staff, g.teacher_id) if g.teacher_id else None
        new = await s.get(Staff, int(tid))
        hist = (await s.execute(select(GroupTeacher.id).where(GroupTeacher.group_id == g.id))).first()
        if not hist and g.teacher_id:
            from datetime import datetime
            s.add(GroupTeacher(group_id=g.id, teacher_id=g.teacher_id, since=datetime(2000, 1, 1)))
        s.add(GroupTeacher(group_id=g.id, teacher_id=new.id, since=_utcnow()))
        g.teacher_id = new.id
        await S.audit(s, me, "guruh_oqituvchi", group=g.name, old=old.full_name if old else None, new=new.full_name)
        await s.commit()
        gname = g.name
    await c.answer("Saqlandi")
    await c.message.edit_text(f"✅ «{gname}»: o'qituvchi {old.full_name if old else '—'} → <b>{new.full_name}</b>.")
    if new.tg_id:
        await send(bot, new.tg_id, f"👨‍🏫 Sizga «{gname}» guruhi biriktirildi. 📋 Bugungi darslar menyusida ko'rinadi.")
    if old and old.tg_id:
        await send(bot, old.tg_id, f"ℹ️ «{gname}» guruhi endi {new.full_name} ga o'tkazildi.")


@router.callback_query(F.data.startswith("gas:"))
async def group_assistant_list(c: CallbackQuery) -> None:
    gid = int(c.data.split(":")[1])
    st, role = await _role(c.from_user.id)
    async with Session() as s:
        g = await s.get(Group, gid)
        if not st or not (role == "rahbar" or st.id == g.teacher_id):
            await c.answer("Faqat rahbar yoki guruh o'qituvchisi", show_alert=True)
            return
        xs = [x for x in (await s.execute(select(Staff).where(Staff.active).order_by(Staff.full_name))).scalars()
              if "yordamchi" in x.role_list()]
    kb = [[IB(text=("✅ " if x.id == g.assistant_id else "") + x.full_name, callback_data=f"gass:{gid}:{x.id}")] for x in xs]
    kb.append([IB(text="Yordamchisiz", callback_data=f"gass:{gid}:0")])
    await c.answer()
    await c.message.answer(f"🤝 «{g.name}» guruhi yordamchisi:", reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("gass:"))
async def group_assistant_set(c: CallbackQuery, bot: Bot) -> None:
    _, gid, aid = c.data.split(":")
    st, role = await _role(c.from_user.id)
    async with Session() as s:
        g = await s.get(Group, int(gid))
        if not st or not (role == "rahbar" or st.id == g.teacher_id):
            await c.answer("Ruxsat yo'q", show_alert=True)
            return
        old = await s.get(Staff, g.assistant_id) if g.assistant_id else None
        new = await s.get(Staff, int(aid)) if int(aid) else None
        g.assistant_id = new.id if new else None
        if not new:
            g.delegated = {}
        await S.audit(s, st, "guruh_yordamchi", group=g.name, old=old.full_name if old else None,
                      new=new.full_name if new else None)
        await s.commit()
        gname = g.name
    await c.answer("Saqlandi")
    await c.message.edit_text(f"✅ «{gname}» yordamchisi: <b>{new.full_name if new else 'yo`q'}</b>."
                              + ("\nYuklangan ishlarni «🤝 Yordamchi» menyusidan belgilang." if new else ""))
    if new and new.tg_id:
        await send(bot, new.tg_id, f"🤝 Siz «{gname}» guruhiga yordamchi o'qituvchi etib biriktirildingiz.")
    if old and old.tg_id and (not new or old.id != new.id):
        await send(bot, old.tg_id, f"ℹ️ «{gname}» guruhidagi yordamchilik vazifangiz yakunlandi.")


# ================= 👥 o'quvchilar ro'yxati va Excel =================
async def _list_role(tg: int) -> str | None:
    _, role = await _role(tg)
    return role if role in ("admin", "moliya", "rahbar") else None


@router.callback_query(F.data == "gla")
async def groups_for_list(c: CallbackQuery, state: FSMContext) -> None:
    if not await _list_role(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.clear()
    async with Session() as s:
        gs = (await s.execute(select(Group).where(Group.active).order_by(Group.name))).scalars().all()
        counts = {g.id: len(await S.group_roster(s, g.id, __import__("datetime").date.today())) for g in gs}
    await c.answer()
    await c.message.answer("Qaysi guruh?", reply_markup=IM(inline_keyboard=[
        [IB(text=f"{g.name} ({counts[g.id]})", callback_data=f"gls:{g.id}")] for g in gs]))


@router.callback_query(F.data.startswith("gls:"))
async def group_students(c: CallbackQuery) -> None:
    role = await _list_role(c.from_user.id)
    if not role:
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    from datetime import date as _d
    gid = int(c.data.split(":")[1])
    t = _d.today()
    async with Session() as s:
        g = await s.get(Group, gid)
        enrs = (await s.execute(select(Enrollment).join(Student).where(Enrollment.group_id == gid, Enrollment.active)
                                .options(selectinload(Enrollment.student), selectinload(Enrollment.group))
                                .order_by(Student.full_name))).scalars().all()
        labels = await S.student_labels(s, [e.student for e in enrs])
        rows = []
        for e in enrs:
            st = await S.month_status(s, e, t.year, t.month)
            frozen = await S.is_frozen(s, e, t)
            mark = "❄️ muzlatilgan" if frozen else ("✅ to'lagan" if st.debt == 0 else f"qarz {S.fmt_som(st.debt)}")
            tail = labels[e.student_id].split("☎")[-1] if "☎" in labels[e.student_id] else ""
            rows.append((e.student_id, e.student.full_name, tail, mark, e.student.coins))
    paid = sum(1 for r in rows if r[3].startswith("✅"))
    lines = [f"👥 <b>{g.name}</b> — {len(rows)} ta o'quvchi (to'lagan {paid}, qarzdor {len(rows) - paid})", ""]
    lines += [f"{i}. {n}" + (f" · ☎{tl}" if tl else "") + f" · {mk} · 🪙{cn}" for i, (_, n, tl, mk, cn) in enumerate(rows, 1)]
    kb = [[IB(text=f"{i}. {n}", callback_data=f"st:{sid}")] for i, (sid, n, *_r) in enumerate(rows, 1)][:60]
    await c.answer()
    await c.message.answer("\n".join(lines) + "\n\nKartani ochish uchun pastdan tanlang:" if rows else
                           f"{g.name}: faol o'quvchi yo'q.", reply_markup=IM(inline_keyboard=kb) if kb else None)


@router.callback_query(F.data == "stx")
async def students_excel(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not await _list_role(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.clear()
    await c.answer("Tayyorlanmoqda…")
    import io
    from datetime import date as _d
    from aiogram.types import BufferedInputFile
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from app.db.models import StudentParent
    t = _d.today()
    async with Session() as s:
        studs = (await s.execute(select(Student).where(Student.status != "ketgan").order_by(Student.full_name))).scalars().all()
        phones: dict[int, list[str]] = {}
        for sid, ph in (await s.execute(select(StudentParent.student_id, Parent.phone).join(
                Parent, Parent.id == StudentParent.parent_id))).all():
            phones.setdefault(sid, []).append(ph)
        enrs = (await s.execute(select(Enrollment).where(Enrollment.active).options(
            selectinload(Enrollment.group), selectinload(Enrollment.student)))).scalars().all()
        by: dict[int, list] = {}
        for e in enrs:
            st = await S.month_status(s, e, t.year, t.month)
            by.setdefault(e.student_id, []).append((e, st))
    wb = Workbook()
    ws = wb.active
    ws.title = "O'quvchilar"
    head = ["№", "O'quvchi", "Sinf", "Hudud", "Guruh(lar)", "Ota-ona telefoni", "O'quvchi telefoni", "Holat", "Coin",
            "Qo'shilgan", f"{t:%m.%Y} hisob", "To'langan", "Qarz", "Chegirma"]
    ws.append(head)
    for cell in ws[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0D2440")
    for i, x in enumerate(studs, 1):
        parts = by.get(x.id, [])
        ws.append([i, x.full_name, ("bitirgan" if x.grade and x.grade > 11 else x.grade), x.region,
                   ", ".join(e.group.name for e, _ in parts), ", ".join(phones.get(x.id, [])), x.phone, x.status, x.coins,
                   min((e.join_date for e, _ in parts), default=None), sum(st.payable for _, st in parts),
                   sum(st.paid for _, st in parts), sum(st.debt for _, st in parts),
                   "; ".join(f"{e.group.name}: {S.discount_label(e)}" for e, _ in parts if e.discount_kind != "none")])
    for col in (11, 12, 13):
        for row in ws.iter_rows(min_row=2, min_col=col, max_col=col):
            for cell in row:
                cell.number_format = "#,##0"
    for row in ws.iter_rows(min_row=2, min_col=10, max_col=10):
        for cell in row:
            cell.number_format = "DD.MM.YYYY"
    for i, w in enumerate([5, 30, 8, 22, 30, 28, 16, 12, 7, 12, 14, 14, 14, 30], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{len(studs) + 1}"
    buf = io.BytesIO()
    wb.save(buf)
    await bot.send_document(c.message.chat.id, BufferedInputFile(buf.getvalue(), f"ELNUR_oquvchilar_{t:%d.%m.%Y}.xlsx"),
                            caption=f"📥 Barcha o'quvchilar: {len(studs)} ta. Filtr bilan guruh, qarz yoki sinf bo'yicha saralang.")
