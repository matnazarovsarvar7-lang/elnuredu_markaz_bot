"""Rahbar: sozlamalar (oyliklar, coin miqdorlari, ruxsatlar) va xodimlarga vazifa berish."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import select

from app.bot import keyboards as K
from app.bot.notify import send
from app.config import settings
from app.db.models import Setting, Staff, Task
from app.db.session import Session
from app.logic.coins import DEFAULT_RULES
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
PAY_TYPES = {"foiz": "Faqat foiz", "fiksal": "Faqat oklad", "ikkalasi": "Oklad + foiz"}
RULE_TITLES = {
    "attend_k": "Darsga keldi", "attend_l": "Kechikdi", "attend_y": "Kelmadi",
    "hw_done": "Vazifa bajardi", "hw_part": "Vazifa qisman", "hw_none": "Vazifa bajarmadi",
    "grade_alo": "Baho «a'lo»", "pay_unit": "To'lov: necha so'mga", "pay_coins": "To'lov: necha coin",
    "pay_bonus": "5-sanagacha to'lov bonusi", "achievement": "Yutuq (sertifikat, OTM)",
}


def today() -> date:
    return datetime.now(TZ).date()


class M(StatesGroup):
    pct = State()
    oklad = State()
    rule = State()
    task_text = State()
    task_date = State()
    task_note = State()


async def _boss(tg_id: int) -> Staff | None:
    async with Session() as s:
        who = await S.who_by_tg(s, tg_id)
    return who.staff if who.staff and who.staff.current_role == "rahbar" else None


# =================== SOZLAMALAR ===================
@router.message(F.text == K.B_SETTINGS)
async def settings_menu(m: Message, state: FSMContext) -> None:
    if not await _boss(m.from_user.id):
        return
    await state.clear()
    async with Session() as s:
        row = await s.get(Setting, "admin_sees_debts")
    on = bool(row and row.value.get("on"))
    kb = IM(inline_keyboard=[
        [IB(text="💰 Xodimlar oyligi", callback_data="set:pay")],
        [IB(text="🪙 Coin miqdorlari", callback_data="set:coins")],
        [IB(text=f"👁 Admin qarzdorlarni ko'radi: {'HA' if on else 'YO`Q'}", callback_data="set:admdebt")],
    ])
    await m.answer("🛠 <b>Sozlamalar</b>\nHar bir o'zgarish kiritilgan paytdan kuchga kiradi va tarixga yoziladi.",
                   reply_markup=kb)


@router.callback_query(F.data == "set:admdebt")
async def toggle_admin_debts(c: CallbackQuery) -> None:
    boss = await _boss(c.from_user.id)
    if not boss:
        return
    async with Session() as s:
        row = await s.get(Setting, "admin_sees_debts") or Setting(key="admin_sees_debts", value={"on": False})
        row.value = {"on": not row.value.get("on")}
        s.add(row)
        await S.audit(s, boss, "sozlama", admin_sees_debts=row.value["on"])
        await s.commit()
    await c.answer(f"Admin qarzdorlarni ko'radi: {'HA' if row.value['on'] else 'YO`Q'}", show_alert=True)


# ---------- xodimlar oyligi ----------
@router.callback_query(F.data == "set:pay")
async def pay_list(c: CallbackQuery) -> None:
    async with Session() as s:
        rows = (await s.execute(select(Staff).where(Staff.active).order_by(Staff.full_name))).scalars().all()
    await c.message.edit_text("Kimning oylik shartini o'zgartirasiz?", reply_markup=IM(
        inline_keyboard=[[IB(text=x.full_name, callback_data=f"sp:{x.id}")] for x in rows]))


def _pay_text(x: Staff) -> str:
    return (f"<b>{x.full_name}</b>\nLavozimlar: {', '.join(S.ROLE_TITLES[r] for r in x.role_list())}\n"
            f"Oylik turi: {PAY_TYPES.get(x.pay_type or '', 'belgilanmagan')}\n"
            f"Foiz: {x.percent or 0}%\nOklad: {S.fmt_som(x.oklad or 0)}")


def _pay_kb(sid: int) -> IM:
    return IM(inline_keyboard=[[IB(text="📊 Foiz", callback_data=f"spp:{sid}"),
                                IB(text="💵 Oklad", callback_data=f"spo:{sid}")],
                               [IB(text="🔁 Oylik turi", callback_data=f"spt:{sid}")]])


@router.callback_query(F.data.startswith("sp:"))
async def pay_card(c: CallbackQuery) -> None:
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        x = await s.get(Staff, sid)
    await c.message.edit_text(_pay_text(x), reply_markup=_pay_kb(sid))


@router.callback_query(F.data.startswith(("spp:", "spo:")))
async def pay_ask(c: CallbackQuery, state: FSMContext) -> None:
    kind, sid = c.data.split(":")
    await state.update_data(sid=int(sid))
    await state.set_state(M.pct if kind == "spp" else M.oklad)
    await c.message.answer("Yangi foizni yozing (0–100):" if kind == "spp" else
                           "Yangi oklad summasini yozing (masalan 700000, yo'q bo'lsa 0):")


async def _save_pay(m: Message, state: FSMContext, field: str, value: int) -> None:
    d = await state.get_data()
    async with Session() as s:
        x = await s.get(Staff, d["sid"])
        old = getattr(x, field)
        setattr(x, field, value)
        if x.percent and x.oklad:
            x.pay_type = "ikkalasi"
        elif x.oklad:
            x.pay_type = "fiksal"
        elif x.percent:
            x.pay_type = "foiz"
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "oylik_shart", staff=x.full_name, field=field, old=old, new=value)
        await s.commit()
    await state.clear()
    await m.answer("Saqlandi ✅ Keyingi hisob-kitoblarda yangi shart ishlatiladi.\n\n" + _pay_text(x),
                   reply_markup=_pay_kb(x.id))


@router.message(M.pct)
async def pay_pct(m: Message, state: FSMContext) -> None:
    digits = "".join(ch for ch in (m.text or "") if ch.isdigit())
    if not digits or int(digits) > 100:
        await m.answer("0 dan 100 gacha raqam yozing.")
        return
    await _save_pay(m, state, "percent", int(digits))


@router.message(M.oklad)
async def pay_oklad(m: Message, state: FSMContext) -> None:
    digits = "".join(ch for ch in (m.text or "") if ch.isdigit())
    if not digits:
        await m.answer("Faqat raqam yozing, masalan 700000.")
        return
    await _save_pay(m, state, "oklad", int(digits))


@router.callback_query(F.data.startswith("spt:"))
async def pay_type_pick(c: CallbackQuery) -> None:
    sid = int(c.data.split(":")[1])
    await c.message.edit_reply_markup(reply_markup=IM(
        inline_keyboard=[[IB(text=v, callback_data=f"spts:{sid}:{k}")] for k, v in PAY_TYPES.items()]))


@router.callback_query(F.data.startswith("spts:"))
async def pay_type_set(c: CallbackQuery) -> None:
    _, sid, t = c.data.split(":")
    async with Session() as s:
        x = await s.get(Staff, int(sid))
        old = x.pay_type
        x.pay_type = t
        if t == "foiz":
            x.oklad = 0
        if t == "fiksal":
            x.percent = 0
        who = await S.who_by_tg(s, c.from_user.id)
        await S.audit(s, who.staff, "oylik_shart", staff=x.full_name, field="pay_type", old=old, new=t)
        await s.commit()
    await c.message.edit_text("Saqlandi ✅\n\n" + _pay_text(x), reply_markup=_pay_kb(x.id))


# ---------- coin miqdorlari ----------
async def _coins_view() -> tuple[str, IM]:
    async with Session() as s:
        rules = await S.get_rules(s)
    lines = ["🪙 <b>Coin miqdorlari</b> (o'zgartirish uchun bosing)"]
    kb = []
    for k, title in RULE_TITLES.items():
        v = rules.get(k, DEFAULT_RULES.get(k, 0))
        shown = S.fmt_som(v) if k == "pay_unit" else f"{v:+d}" if k.startswith(("attend", "hw")) else str(v)
        kb.append([IB(text=f"{title}: {shown}", callback_data=f"cr:{k}")])
    return "\n".join(lines), IM(inline_keyboard=kb)


@router.callback_query(F.data == "set:coins")
async def coins_view(c: CallbackQuery) -> None:
    text, kb = await _coins_view()
    await c.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cr:"))
async def coin_ask(c: CallbackQuery, state: FSMContext) -> None:
    key = c.data.split(":")[1]
    await state.set_state(M.rule)
    await state.update_data(key=key)
    hint = "masalan 20000" if key == "pay_unit" else "manfiy bo'lsa minus bilan, masalan -1" \
        if key.startswith(("attend", "hw")) else "masalan 5; o'chirish uchun 0"
    await c.message.answer(f"«{RULE_TITLES[key]}» uchun yangi qiymat ({hint}):")


@router.message(M.rule)
async def coin_set(m: Message, state: FSMContext) -> None:
    mt = re.fullmatch(r"\s*(-?\d+)\s*", (m.text or "").replace(" ", ""))
    d = await state.get_data()
    if not mt or (d["key"] == "pay_unit" and int(mt.group(1)) <= 0):
        await m.answer("Faqat butun son yozing.")
        return
    async with Session() as s:
        row = await s.get(Setting, "coin_rules") or Setting(key="coin_rules", value={})
        old = {**DEFAULT_RULES, **row.value}.get(d["key"])
        row.value = {**row.value, d["key"]: int(mt.group(1))}
        s.add(row)
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "coin_qoida", rule=d["key"], old=old, new=int(mt.group(1)))
        await s.commit()
    await state.clear()
    text, kb = await _coins_view()
    await m.answer("Saqlandi ✅ Oldin berilgan coinlar o'zgarmaydi.\n\n" + text, reply_markup=kb)


# =================== VAZIFALAR ===================
@router.message(F.text == K.B_TASK_NEW)
async def task_new(m: Message, state: FSMContext) -> None:
    if not await _boss(m.from_user.id):
        return
    await state.clear()
    async with Session() as s:
        rows = (await s.execute(select(Staff).where(Staff.active).order_by(Staff.full_name))).scalars().all()
    await m.answer("Vazifa kimga?", reply_markup=IM(
        inline_keyboard=[[IB(text=x.full_name, callback_data=f"tn:{x.id}")] for x in rows]))


@router.callback_query(F.data.startswith("tn:"))
async def task_who(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(sid=int(c.data.split(":")[1]))
    await state.set_state(M.task_text)
    await c.message.edit_text("Vazifani yozing (aniq va qisqa):")


@router.message(M.task_text)
async def task_text(m: Message, state: FSMContext) -> None:
    if not (m.text or "").strip():
        return
    await state.update_data(text=m.text.strip())
    t = today()
    opts = [("Bugun", t), ("Ertaga", t + timedelta(days=1)), ("3 kunda", t + timedelta(days=3)),
            ("1 haftada", t + timedelta(days=7))]
    kb = IM(inline_keyboard=[[IB(text=f"{n} ({d:%d.%m})", callback_data=f"td:{d.isoformat()}")] for n, d in opts]
            + [[IB(text="📅 Boshqa sana", callback_data="td:other")]])
    await m.answer("Muddat?", reply_markup=kb)


@router.callback_query(F.data.startswith("td:"))
async def task_due(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    v = c.data.split(":", 1)[1]
    if v == "other":
        await state.set_state(M.task_date)
        await c.message.edit_text("Sanani kun va oy bilan yozing, masalan: 20 11")
        return
    await _create_task(c.message, state, bot, c.from_user.id, date.fromisoformat(v))


@router.message(M.task_date)
async def task_due_custom(m: Message, state: FSMContext, bot: Bot) -> None:
    nums = [int(x) for x in re.findall(r"\d+", m.text or "")]
    try:
        d = date(today().year, nums[1], nums[0])
        if d < today():
            d = date(today().year + 1, nums[1], nums[0])
    except (IndexError, ValueError):
        await m.answer("Tushunmadim. Masalan: 20 11")
        return
    await _create_task(m, state, bot, m.from_user.id, d)


async def _create_task(msg: Message, state: FSMContext, bot: Bot, tg_id: int, due: date) -> None:
    d = await state.get_data()
    async with Session() as s:
        who = await S.who_by_tg(s, tg_id)
        x = await s.get(Staff, d["sid"])
        t = Task(staff_id=x.id, created_by=who.staff.id, text=d["text"], due=due)
        s.add(t)
        await S.audit(s, who.staff, "vazifa_berildi", staff=x.full_name, due=due.isoformat())
        await s.commit()
    await state.clear()
    await msg.answer(f"✅ Vazifa berildi: {x.full_name}, muddat {due:%d.%m}.")
    if x.tg_id:
        await send(bot, x.tg_id, f"📝 <b>Yangi vazifa</b> (rahbardan)\n{t.text}\nMuddat: {due:%d.%m.%Y}",
                   reply_markup=_done_kb(t.id))
    else:
        await msg.answer("⚠️ Bu xodim hali botga kirmagan — vazifani u kirganda «Vazifalarim»da ko'radi.")


def _done_kb(tid: int) -> IM:
    return IM(inline_keyboard=[[IB(text="✅ Bajarildi", callback_data=f"tdone:{tid}")]])


@router.message(F.text == K.B_MY_TASKS)
async def my_tasks(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not who.staff:
            return
        rows = (await s.execute(select(Task).where(Task.staff_id == who.staff.id, Task.status == "ochiq")
                                .order_by(Task.due))).scalars().all()
    if not rows:
        await m.answer("Ochiq vazifa yo'q ✅")
        return
    for t in rows:
        late = " ⚠️ muddati o'tgan" if t.due < today() else ""
        await m.answer(f"📝 {t.text}\nMuddat: {t.due:%d.%m}{late}", reply_markup=_done_kb(t.id))


@router.callback_query(F.data.startswith("tdone:"))
async def task_done(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(M.task_note)
    await state.update_data(tid=int(c.data.split(":")[1]))
    await c.message.answer("Qisqa izoh yozing (nima qilindi). Izohsiz bo'lsa «-» yozing:")


@router.message(M.task_note)
async def task_note(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    note = (m.text or "").strip()
    async with Session() as s:
        t = await s.get(Task, d["tid"])
        if t.status != "ochiq":
            await state.clear()
            await m.answer("Bu vazifa allaqachon yopilgan.")
            return
        t.status, t.done_at, t.done_note = "bajarildi", datetime.now(), None if note == "-" else note
        x = await s.get(Staff, t.staff_id)
        boss = await s.get(Staff, t.created_by) if t.created_by else None
        await S.audit(s, x, "vazifa_bajarildi", task_id=t.id)
        await s.commit()
    await state.clear()
    await m.answer("Rahmat! Vazifa bajarildi deb belgilandi ✅")
    if boss and boss.tg_id:
        late = " (muddatidan kech)" if t.done_at.date() > t.due else ""
        await send(bot, boss.tg_id, f"✅ {x.full_name} vazifani bajardi{late}:\n{t.text}"
                                    + (f"\nIzoh: {t.done_note}" if t.done_note else ""))


@router.message(F.text == K.B_TASKS_ALL)
async def tasks_all(m: Message) -> None:
    if not await _boss(m.from_user.id):
        return
    since = today() - timedelta(days=7)
    async with Session() as s:
        rows = (await s.execute(select(Task, Staff).join(Staff, Staff.id == Task.staff_id)
                                .where((Task.status == "ochiq") | (Task.done_at >= since))
                                .order_by(Task.status, Task.due))).all()
    if not rows:
        await m.answer("Vazifalar yo'q.")
        return
    out = ["<b>Vazifalar</b>"]
    for t, x in rows:
        if t.status == "ochiq":
            icon = "🔴" if t.due < today() else "🟡"
            out.append(f"{icon} {x.full_name}: {t.text[:60]} — {t.due:%d.%m}")
        elif t.status == "bajarildi":
            out.append(f"🟢 {x.full_name}: {t.text[:60]} — bajarildi {t.done_at:%d.%m}")
    out.append("\n🔴 muddati o'tgan · 🟡 jarayonda · 🟢 oxirgi 7 kunda bajarilgan")
    await m.answer("\n".join(out))


async def tasks_job(bot: Bot) -> None:
    """Har kuni 9:00: bugun muddati tugaydiganlar — xodimga eslatma; muddati o'tganlar — rahbarga bir marta."""
    t = today()
    async with Session() as s:
        rows = (await s.execute(select(Task).where(Task.status == "ochiq"))).scalars().all()
        msgs = []
        for task in rows:
            x = await s.get(Staff, task.staff_id)
            if task.due == t and x.tg_id:
                msgs.append((x.tg_id, f"⏰ Bugun muddati tugaydi:\n{task.text}", _done_kb(task.id)))
            if task.due < t and not task.overdue_notified:
                boss = await s.get(Staff, task.created_by) if task.created_by else None
                if boss and boss.tg_id:
                    msgs.append((boss.tg_id, f"🔴 Muddati o'tdi ({task.due:%d.%m}): {x.full_name}\n{task.text}", None))
                if x.tg_id:
                    msgs.append((x.tg_id, f"🔴 Vazifa muddati o'tdi:\n{task.text}", _done_kb(task.id)))
                task.overdue_notified = True
        await s.commit()
    for tg, text, kb in msgs:
        await send(bot, tg, text, reply_markup=kb) if kb else await send(bot, tg, text)
