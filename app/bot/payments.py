"""To'lovlar: naqd kiritish, karta cheki, rahbar tasdig'i, qarzdorlar.

Odatiy holat — o'quvchi bo'yicha UMUMIY to'lov: summa uning barcha guruhlariga qarziga mutanosib
taqsimlanadi (qarzlar teng bo'lsa — teng). Moliya xohlasa bitta guruhga alohida to'lov kiritadi.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy.orm import selectinload

from app.bot import keyboards as K
from app.bot.notify import send, send_photo
from app.config import settings
from app.db.models import Staff, Enrollment, Payment, Student
from app.db.session import Session
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
METHOD = {"naqd": "Naqd", "karta": "Kartaga o'tkazma", "ijtimoiy": "Ijtimoiy", "kochirish": "Guruhdan ko'chirilgan"}


def today() -> date:
    return datetime.now(TZ).date()


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


class Pay(StatesGroup):
    search = State()
    amount = State()
    date = State()
    receipt = State()


async def _who(tg_id: int):
    async with Session() as s:
        return await S.who_by_tg(s, tg_id)


def _is(who, *roles) -> bool:
    return bool(who.staff and who.staff.current_role in roles)


# ---------- 1. o'quvchini tanlash ----------
@router.message(F.text == K.B_PAY_IN)
async def pay_start(m: Message, state: FSMContext) -> None:
    who = await _who(m.from_user.id)
    if not _is(who, "moliya", "rahbar"):
        return
    await state.clear()
    await state.set_state(Pay.search)
    await m.answer("💵 O'quvchi ismi yoki familiyasidan kamida 2 harf yozing.\n"
                   "Saqlagandan keyin keyingi ismni darhol yozaverasiz — boshidan boshlash shart emas.")


@router.message(Pay.search)
async def pay_search(m: Message, state: FSMContext, text: str | None = None) -> None:
    async with Session() as s:
        found = await S.search_students(s, text if text is not None else (m.text or ""))
        labels = await S.student_labels(s, found) if len(found) > 1 else {}
    if not found:
        await m.answer("Topilmadi. Boshqacha yozib ko'ring (kamida 2 harf).")
        return
    if len(found) == 1:                      # bitta topilsa — darhol ochiladi
        await _open_student(m, state, found[0].id, edit=False)
        return
    kb = IM(inline_keyboard=[[IB(text=labels[x.id], callback_data=f"ps:{x.id}")] for x in found])
    await m.answer("O'quvchini tanlang (guruhi va ota-ona telefoni oxiriga qarang):", reply_markup=kb)


def _last_text(last: dict | None) -> str | None:
    if not last:
        return None
    d = date.fromisoformat(last["paid"])
    return f"{'💵 naqd' if last['method'] == 'naqd' else '💳 karta'} · {d:%d.%m}"


async def _open_student(msg: Message, state: FSMContext, sid: int, edit: bool) -> None:
    """Xodim uchun bitta ekran: qarz + summa tugmalari (yoki summani yozish)."""
    t = today()
    async with Session() as s:
        sd = await S.student_debt(s, sid, t.year, t.month)
        old = await S.left_debts(s, sid, before=date(t.year, t.month, 1))
        if not sd.parts and not old:
            await s.commit()
            await msg.answer("Bu o'quvchi hech qaysi guruhda emas va qarzi yo'q.")
            return
        label = (await S.student_labels(s, [sd.student]))[sid]
        await s.commit()
    if old and not sd.parts:                  # guruhdan ketgan, eski oydan qarzi qolgan
        await _open_left(msg, state, sd.student.full_name, sid, old, label)
        return
    tail = label.split("☎")[-1] if "☎" in label else ""
    lines = [f"<b>{sd.student.full_name}</b> · {t:%m.%Y}" + (f" · ota-ona ☎{tail}" if tail else "")]
    for p in sd.parts:
        lines.append(f"• {p.enrollment.group.name}: {S.fmt_som(p.payable)}, "
                     + ("to'langan ✅" if p.debt == 0 else f"qoldi {S.fmt_som(p.debt)}"))
    rows = []
    if sd.debt:
        rows.append([IB(text=f"✅ Qarzini to'liq: {S.fmt_som(sd.debt)}", callback_data=f"pq:{sid}:a:{sd.debt}")])
    if len(sd.parts) > 1:
        rows += [[IB(text=f"Faqat {p.enrollment.group.name}: {S.fmt_som(p.debt)}",
                     callback_data=f"pq:{sid}:{p.enrollment.id}:{p.debt}")] for p in sd.parts if p.debt]
    for st in old:                            # boshqa guruhdan ketgandagi eski qarz
        e = st.enrollment
        lines.append(f"🚪 {e.group.name} (ketgan {e.left_date:%d.%m.%Y}): eski qarz {S.fmt_som(st.debt)}")
        rows.append([IB(text=f"🚪 Eski qarz · {e.group.name}: {S.fmt_som(st.debt)}",
                        callback_data=f"pq:{sid}:{e.id}:{st.debt}")])
    rows.append([IB(text="👤 Kartasi", callback_data=f"st:{sid}")])
    d = await state.get_data()
    await state.set_state(Pay.amount)
    await state.update_data(mode="all", sid=sid, eid=None, month=month_key(t), name=sd.student.full_name,
                            last=d.get("last"), lock_month=False)
    text = "\n".join(lines) + ("\n\nTugmani bosing yoki summani yozing (masalan 300000):" if sd.debt else
                               "\n\nQarzi yo'q ✅ Oldindan to'lov bo'lsa, summani yozing:")
    if edit:
        await msg.edit_text(text, reply_markup=IM(inline_keyboard=rows))
    else:
        await msg.answer(text, reply_markup=IM(inline_keyboard=rows))


async def _open_left(msg: Message, state: FSMContext, name: str, sid: int, old: list, label: str) -> None:
    tail = label.split("☎")[-1] if "☎" in label else ""
    lines = [f"<b>{name}</b> · 🚪 guruhdan ketgan" + (f" · ota-ona ☎{tail}" if tail else ""), ""]
    for st in old:
        e = st.enrollment
        lines.append(f"• {e.group.name} ({e.left_date:%m.%Y}, ketgan {e.left_date:%d.%m}): "
                     f"qarz <b>{S.fmt_som(st.debt)}</b>")
    rows = [[IB(text=f"✅ {st.enrollment.group.name}: {S.fmt_som(st.debt)}",
                callback_data=f"pq:{sid}:{st.enrollment.id}:{st.debt}")] for st in old]
    first = old[0].enrollment
    d = await state.get_data()
    await state.set_state(Pay.amount)
    await state.update_data(mode="one", sid=sid, eid=first.id, month=month_key(first.left_date), lock_month=True,
                            name=name, last=d.get("last"))
    await msg.answer("\n".join(lines) + "\n\nTugmani bosing yoki summani yozing:",
                     reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data.startswith("pq:"))
async def pay_quick_amount(c: CallbackQuery, state: FSMContext) -> None:
    _, sid, target, amount = c.data.split(":")
    d = await state.get_data()
    if d.get("sid") != int(sid):
        await _open_student(c.message, state, int(sid), edit=False)
        d = await state.get_data()
    if target == "a":
        await state.update_data(mode="all", eid=None, month=month_key(today()), lock_month=False)
    else:
        async with Session() as s:
            e = await s.get(Enrollment, int(target), options=[selectinload(Enrollment.group)])
        await state.update_data(mode="one", eid=e.id)
        if not e.active and e.left_date and month_key(e.left_date) != month_key(today()):
            await state.update_data(month=month_key(e.left_date), lock_month=True)   # ketgan oyi hisobiga
    await c.answer()
    await _got_amount(c.message, state, int(amount), c.from_user.id)


async def _student_kb(s, sid: int, t: date) -> tuple[str, IM]:
    sd = await S.student_debt(s, sid, t.year, t.month)
    lines = [f"<b>{sd.student.full_name}</b> · {t:%m.%Y}"]
    for p in sd.parts:
        tail = "to'langan ✅" if p.debt == 0 else f"qoldi {S.fmt_som(p.debt)}"
        lines.append(f"• {p.enrollment.group.name}: {S.fmt_som(p.payable)}, {tail}")
    lines.append(f"<b>Umumiy qarz: {S.fmt_som(sd.debt)}</b>")
    rows = [[IB(text=f"💰 Umumiy to'lov (qarz {S.fmt_som(sd.debt)})", callback_data=f"pall:{sid}")]]
    if len(sd.parts) > 1:
        rows += [[IB(text=f"Faqat: {p.enrollment.group.name}", callback_data=f"pe:{p.enrollment.id}")]
                 for p in sd.parts]
    return "\n".join(lines), IM(inline_keyboard=rows)


@router.callback_query(F.data.startswith("ps:"))
async def pay_pick_student(c: CallbackQuery, state: FSMContext) -> None:
    sid = int(c.data.split(":")[1])
    who = await _who(c.from_user.id)
    if who.staff:
        await c.answer()
        await _open_student(c.message, state, sid, edit=True)
        return
    async with Session() as s:
        if not await S.student_enrollments(s, sid):
            await c.message.edit_text("Bu o'quvchi hech qaysi guruhda emas.")
            return
        text, kb = await _student_kb(s, sid, today())
    await c.message.edit_text(text + "\n\nTo'lov qanday kiritiladi?", reply_markup=kb)


# ota-ona: farzandini tanlaydi, keyin umumiy yoki alohida
@router.message(F.text == K.B_PARENT_PAY)
async def parent_pay(m: Message, state: FSMContext) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not who.parent:
            return
        kids = [ch for ch in who.parent.children if await S.student_enrollments(s, ch.id)]
    await state.clear()
    if not kids:
        await m.answer("Farzandingiz hech qaysi guruhda emas.")
        return
    if len(kids) == 1:
        async with Session() as s:
            text, kb = await _student_kb(s, kids[0].id, today())
        await m.answer(text + "\n\nKartaga o'tkazgan to'lovingiz qaysi biri uchun?", reply_markup=kb)
        return
    await m.answer("Qaysi farzandingiz uchun?", reply_markup=IM(
        inline_keyboard=[[IB(text=k.full_name, callback_data=f"ps:{k.id}")] for k in kids]))


async def _ask_amount(msg: Message, state: FSMContext, title: str, debt: int, edit: bool) -> None:
    await state.set_state(Pay.amount)
    kb = IM(inline_keyboard=[[IB(text=f"Qarzini to'liq: {S.fmt_som(debt)}", callback_data=f"pa:{debt}")]]) \
        if debt else None
    text = f"{title}\n\nSummani raqam bilan yozing (masalan 300000):"
    if edit:
        await msg.edit_text(text, reply_markup=kb)
    else:
        await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("pall:"))
async def pay_all(c: CallbackQuery, state: FSMContext) -> None:
    sid = int(c.data.split(":")[1])
    t = today()
    async with Session() as s:
        sd = await S.student_debt(s, sid, t.year, t.month)
    await state.update_data(mode="all", sid=sid, eid=None, month=month_key(t), name=sd.student.full_name,
                            group="barcha guruhlar" if len(sd.parts) > 1 else sd.parts[0].enrollment.group.name)
    note = "\nSumma guruhlarga qarziga qarab avtomatik taqsimlanadi." if len(sd.parts) > 1 else ""
    await _ask_amount(c.message, state, f"<b>{sd.student.full_name}</b> · umumiy qarz {S.fmt_som(sd.debt)}{note}",
                      sd.debt, edit=True)


@router.callback_query(F.data.startswith("pe:"))
async def pay_pick_enr(c: CallbackQuery, state: FSMContext) -> None:
    eid = int(c.data.split(":")[1])
    t = today()
    async with Session() as s:
        e = await s.get(Enrollment, eid, options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
        st = await S.month_status(s, e, t.year, t.month)
    await state.update_data(mode="one", eid=eid, sid=e.student_id, month=month_key(t), name=e.student.full_name,
                            group=e.group.name)
    await _ask_amount(c.message, state, f"<b>{e.student.full_name}</b> · faqat {e.group.name}: "
                                        f"qarz {S.fmt_som(st.debt)}", st.debt, edit=True)


# ---------- 2. summa, usul, sana ----------
async def _got_amount(msg: Message, state: FSMContext, amount: int, tg_id: int) -> None:
    if amount <= 0:
        await msg.answer("Summa noto'g'ri. Faqat raqam yozing, masalan 300000.")
        return
    await state.update_data(amount=amount)
    who = await _who(tg_id)
    if who.parent:
        await state.update_data(method="karta", paid=today().isoformat())
        await state.set_state(Pay.receipt)
        await msg.answer("O'tkazma chekini yuboring: skrinshot yoki PDF fayl (Click, Payme, Xazna, bank ilovasi).")
        return
    d = await state.get_data()
    t = today()
    rows = []
    last = d.get("last")
    if last and not (last["method"] == "naqd" and last["paid"] == t.isoformat()):
        rows.append([IB(text=f"🔁 Oldingidek: {_last_text(last)}", callback_data="pmd:last")])
    rows.append([IB(text="💵 Naqd · bugun", callback_data="pmd:naqd:0"),
                 IB(text="💳 Karta · bugun", callback_data="pmd:karta:0")])
    rows.append([IB(text="💵 Naqd · boshqa sana", callback_data="pmd:naqd:x"),
                 IB(text="💳 Karta · boshqa sana", callback_data="pmd:karta:x")])
    rows.append([IB(text="✏️ Summani o'zgartirish", callback_data="pfix:amt"),
                 IB(text="❌ Bekor", callback_data="pfix:no")])
    await msg.answer(f"<b>{d.get('name', '')}</b> · {S.fmt_som(amount)} — qanday va qachon to'landi?",
                     reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data.startswith("pfix:"))
async def pay_fix_before(c: CallbackQuery, state: FSMContext) -> None:
    """Saqlashdan oldin: summani qayta yozish yoki to'lovni umuman bekor qilish."""
    d = await state.get_data()
    await c.answer()
    if c.data == "pfix:no" or not d.get("sid"):
        last = d.get("last")
        await state.clear()
        await state.set_state(Pay.search)
        if last:
            await state.update_data(last=last)
        await c.message.edit_text("❌ Bekor qilindi — hech narsa saqlanmadi.\n➡️ O'quvchi ismini yozing.")
        return
    await c.message.edit_text("✏️ Summa o'zgartiriladi.")
    await _open_student(c.message, state, d["sid"], edit=False)


@router.callback_query(F.data.startswith("pmd:"))
async def pay_method_date(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    parts = c.data.split(":")
    d = await state.get_data()
    if "amount" not in d:
        await c.answer("Qaytadan boshlang: o'quvchi ismini yozing.", show_alert=True)
        return
    await c.answer()
    if parts[1] == "last":
        last = d.get("last")
        if not last:
            return
        paid = date.fromisoformat(last["paid"])
        await state.update_data(method=last["method"], paid=paid.isoformat(),
                                **({} if d.get("lock_month") else {"month": month_key(paid)}))
        await c.message.edit_text(f"{_last_text(last)} ✅")
        await _after_date(c.message, state, bot, c.from_user.id)
        return
    await state.update_data(method=parts[1])
    if parts[2] == "x":
        await state.set_state(Pay.date)
        await c.message.edit_text("Sanani kun va oy bilan yozing, masalan: 3 11")
        return
    await state.update_data(paid=today().isoformat())
    await c.message.edit_text(f"{'💵 Naqd' if parts[1] == 'naqd' else '💳 Karta'} · bugun ✅")
    await _after_date(c.message, state, bot, c.from_user.id)


@router.message(Pay.amount)
async def pay_amount(m: Message, state: FSMContext) -> None:
    digits = "".join(ch for ch in (m.text or "") if ch.isdigit())
    who = await _who(m.from_user.id)
    if not digits and who.staff and len((m.text or "").strip()) >= 2:
        await pay_search(m, state)           # summa o'rniga ism yozildi — boshqa o'quvchini qidiramiz
        return
    await _got_amount(m, state, int(digits) if digits else 0, m.from_user.id)


@router.callback_query(F.data.startswith("pa:"))
async def pay_amount_btn(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await _got_amount(c.message, state, int(c.data.split(":")[1]), c.from_user.id)


@router.callback_query(F.data.startswith("pm:"))
async def pay_method(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(method=c.data.split(":")[1])
    kb = IM(inline_keyboard=[[IB(text=f"Bugun ({today():%d.%m})", callback_data="pdt:0")],
                             [IB(text="📅 Boshqa sana", callback_data="pdt:other")]])
    await c.message.edit_text("To'lov qaysi kuni qilingan?", reply_markup=kb)


@router.callback_query(F.data.startswith("pdt:"))
async def pay_date(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if c.data == "pdt:other":
        await state.set_state(Pay.date)
        await c.message.edit_text("Sanani kun va oy bilan yozing, masalan: 3 11")
        return
    await state.update_data(paid=today().isoformat())
    await _after_date(c.message, state, bot, c.from_user.id)


@router.message(Pay.date)
async def pay_date_custom(m: Message, state: FSMContext, bot: Bot) -> None:
    nums = [int(x) for x in re.findall(r"\d+", m.text or "")]
    t = today()
    try:
        d = date(t.year, nums[1], nums[0])
        if d > t:
            d = date(t.year - 1, nums[1], nums[0])
    except (IndexError, ValueError):
        await m.answer("Tushunmadim. Masalan: 3 11 (3-noyabr)")
        return
    if (t - d).days > 62:
        await m.answer("Juda eski sana. Oxirgi 2 oy ichidagi sanani yozing.")
        return
    lock = (await state.get_data()).get("lock_month")
    await state.update_data(paid=d.isoformat(), **({} if lock else {"month": month_key(d)}))
    await _after_date(m, state, bot, m.from_user.id)


async def _after_date(msg: Message, state: FSMContext, bot: Bot, tg_id: int) -> None:
    d = await state.get_data()
    if d["method"] == "karta":
        who = await _who(tg_id)
        if who.staff and who.staff.current_role == "rahbar":
            await _save(msg, state, bot, tg_id)          # rahbar bankda o'zi ko'rgan — darhol tasdiq
            return
        await state.set_state(Pay.receipt)
        kb = IM(inline_keyboard=[[IB(text="Chek yo'q — rahbar tekshirsin", callback_data="nochek")]])
        await msg.answer("O'tkazma chekini yuboring (skrinshot yoki PDF fayl). To'lov rahbar tasdiqlagach hisobga olinadi.",
                         reply_markup=kb)
        return
    await _save(msg, state, bot, tg_id)


# ---------- 3. saqlash ----------
async def _record(s, d: dict, who, status: str, file_id: str | None = None,
                  file_hash: str | None = None) -> list[tuple[Enrollment, Payment]]:
    paid = date.fromisoformat(d.get("paid") or today().isoformat())
    if d["mode"] == "all":
        return await S.record_student_payment(s, d["sid"], d["amount"], d["method"], paid, d["month"], who.staff,
                                              status, file_id, file_hash)
    e = await s.get(Enrollment, d["eid"], options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
    pay = await S.record_payment(s, e, d["amount"], d["method"], paid, d["month"], who.staff, status,
                                 file_id, file_hash)
    return [(e, pay)]


def _breakdown(items: list[tuple[str, int]]) -> str:
    if len(items) == 1:
        return ""
    return "\n" + "\n".join(f"  • {g}: {S.fmt_som(a)}" for g, a in items)


async def _save(msg: Message, state: FSMContext, bot: Bot, tg_id: int) -> None:
    d = await state.get_data()
    async with Session() as s:
        who = await S.who_by_tg(s, tg_id)
        res = await _record(s, d, who, "tasdiq")
        await s.flush()
        closed = await S.settle_left_debts(s, d["sid"])
        y, m = map(int, d["month"].split("-"))
        sd = await S.student_debt(s, d["sid"], y, m)
        parents = await S.parent_tg_ids(s, d["sid"])
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        items = [(e.group.name, p.amount) for e, p in res]
        coins = sum(p.coins_given for _, p in res)
        await s.commit()
        pid = res[0][1].id if res else None
    paid = date.fromisoformat(d["paid"])
    await _next_series(state, d)
    await msg.answer(f"✅ Saqlandi: {d['name']} · {S.fmt_som(d['amount'])} "
                     f"({METHOD[d['method']].lower()}, {paid:%d.%m}).{_breakdown(items)}\n"
                     + (f"🚪 Ketgan o'quvchi qarzi yopildi: {', '.join(closed)} ✅\n" if closed else "") + "\n"
                     "➡️ <b>Keyingi o'quvchi ismini yozing</b> (tugatish — menyudagi istalgan tugma).",
                     reply_markup=IM(inline_keyboard=[[IB(text=f"↩️ Xato kiritildi — bekor qilish ({UNDO_MIN} daqiqa)",
                                                          callback_data=f"pxu:{pid}")]]) if pid else None)
    await _send_receipt(bot, parents, d["name"], d["amount"], d["method"], d["month"], items, sd.debt, coins)
    for b in bosses:
        if b != tg_id:
            await send(bot, b, f"💵 {METHOD[d['method']]}: {d['name']} · {S.fmt_som(d['amount'])} "
                               f"({who.staff.full_name})")


UNDO_MIN = 15


@router.callback_query(F.data.startswith("pxu:"))
async def pay_undo(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Kirituvchi o'z to'lovini 15 daqiqa ichida rahbarsiz bekor qiladi (teskari yozuv, rahbarga xabar)."""
    from datetime import datetime, timedelta
    from app.bot.fixes import apply_void
    pid = int(c.data.split(":")[1])
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        p = await s.get(Payment, pid)
        if not (who.staff and p and p.entered_by == who.staff.id):
            await c.answer("Faqat to'lovni kiritgan xodim bekor qila oladi.", show_alert=True)
            return
        if p.voided_at or p.void_of is not None:
            await c.answer("Allaqachon bekor qilingan.", show_alert=True)
            return
        if datetime.utcnow() - p.created_at > timedelta(minutes=UNDO_MIN):
            await c.answer(f"{UNDO_MIN} daqiqa o'tdi. Endi o'quvchi kartasidan «💵 To'lovlar (bekor qilish)» orqali "
                           "rahbar tasdig'i bilan bekor qilinadi.", show_alert=True)
            return
        desc, family = await apply_void(s, "p", p, who.staff, "Xato kiritildi — kirituvchi darhol bekor qildi")
        await S.audit(s, who.staff, "bekor_qilindi", kind="To'lov", id=pid, reason="darhol (xato kiritildi)")
        await s.commit()
        e = await s.get(Enrollment, p.enrollment_id)
        sid = e.student_id
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        name = who.staff.full_name
    await c.answer("Bekor qilindi")
    await c.message.edit_text(c.message.html_text.split("\n\n➡️")[0] + "\n\n↩️ <b>Bekor qilindi</b> (teskari yozuv qo'shildi).")
    for b in bosses:
        if b != c.from_user.id:
            await send(bot, b, f"↩️ To'lov darhol bekor qilindi ({name}):\n{desc}")
    for tg, text in family:
        await send(bot, tg, text)
    await c.message.answer("✏️ To'g'ri summani kiriting:")
    await _open_student(c.message, state, sid, edit=False)


async def _next_series(state: FSMContext, d: dict) -> None:
    """Xodim to'lovdan keyin yana ism yozib keyingi to'lovni kiritadi; usul va sana eslab qolinadi."""
    await state.clear()
    await state.set_state(Pay.search)
    await state.update_data(last={"method": d["method"], "paid": d["paid"]})


async def _send_receipt(bot: Bot, tg_ids: list[int], name: str, amount: int, method: str, month: str,
                        items: list[tuple[str, int]], left: int, coins: int) -> None:
    rest = "To'liq to'langan ✅" if left == 0 else f"Qolgan qarz: {S.fmt_som(left)}"
    text = (f"🧾 <b>{settings.center_name}</b> — to'lov qabul qilindi\n{name}\n"
            f"Summa: {S.fmt_som(amount)} ({METHOD[method]}){_breakdown(items)}\nOy: {month}\n{rest}"
            + (f"\nCoin: +{coins}" if coins else ""))
    for t in tg_ids:
        await send(bot, t, text)


def _pending_caption(name: str, amount: int, paid: date, items: list[tuple[str, int]]) -> str:
    return (f"💳 Kartaga o'tkazma — tasdiq kutilmoqda\n{name}\nSumma: {S.fmt_som(amount)}{_breakdown(items)}\n"
            f"Sana: {paid:%d.%m.%Y}\nBank ilovasida pul tushganini tekshiring.")


def _confirm_kb(pid: int) -> IM:
    return IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=f"pc:{pid}"),
                                IB(text="❌ Rad etish", callback_data=f"pr:{pid}")]])


async def _save_pending(msg_or_cb_msg: Message, state: FSMContext, bot: Bot, tg_id: int,
                        file_id: str | None, file_hash: str | None) -> None:
    d = await state.get_data()
    async with Session() as s:
        who = await S.who_by_tg(s, tg_id)
        res = await _record(s, d, who, "kutilmoqda", file_id, file_hash)
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        items = [(e.group.name, p.amount) for e, p in res]
        first = res[0][1]
        staff = bool(who.staff)
        shared = ""
        if file_hash:
            uses = await S.receipt_uses(s, file_hash)
            if len(uses) > 1:
                shared = ("\n\n📎 <b>Bitta chek — bir necha o'quvchi:</b>\n"
                          + "\n".join(f"• {n}: {S.fmt_som(a)}" for _, n, a in uses)
                          + f"\n<b>Jami: {S.fmt_som(sum(a for *_, a in uses))}</b> — chekdagi summa bilan solishtiring.")
        await s.commit()
    if staff:
        await _next_series(state, d)
    else:
        await state.clear()
    caption = _pending_caption(d["name"], d["amount"], first.paid_date, items) + shared
    for b in bosses:
        if file_id:
            await send_photo(bot, b, file_id, caption, reply_markup=_confirm_kb(first.id))
        else:
            await send(bot, b, caption + "\n(chek rasmi yo'q)", reply_markup=_confirm_kb(first.id))


@router.message(Pay.receipt, F.photo | F.document)
async def pay_receipt(m: Message, state: FSMContext, bot: Bot) -> None:
    from app.bot.notify import receipt_of
    rc = receipt_of(m)
    if not rc:
        await m.answer("Chekni rasm yoki PDF fayl qilib yuboring (Click, Payme, Xazna, bank ilovasi cheki).")
        return
    fid, uid = rc
    d = await state.get_data()
    async with Session() as s:
        uses = await S.receipt_uses(s, uid)
        others = [u for u in uses if u[0] != d.get("sid")]
        same_student = [u for u in uses if u[0] == d.get("sid")]
        sib = bool(others) and all([await S.are_siblings(s, u[0], d["sid"]) for u in others])
    if same_student and not others:
        await m.answer(f"⚠️ Bu chek shu o'quvchi uchun avval kiritilgan ({S.fmt_som(same_student[0][2])}). "
                       "Ikki marta kiritilmasin — boshqa chek yuboring yoki menyudan chiqing.")
        return
    if others and not sib:
        used = ", ".join(f"{n} — {S.fmt_som(a)}" for _, n, a in others)
        await state.update_data(rc_fid=fid, rc_uid=uid)
        await m.answer(f"⚠️ Bu chek avval boshqa o'quvchi uchun ishlatilgan: {used}.\n"
                       "Agar bitta o'tkazma bilan bir necha o'quvchi uchun to'langan bo'lsa — davom eting, "
                       "rahbar chekdagi umumiy summani tekshiradi.",
                       reply_markup=IM(inline_keyboard=[
                           [IB(text="✅ Ha, bitta chek — bir necha o'quvchi", callback_data="prcu")],
                           [IB(text="❌ Yo'q, boshqa chek yuboraman", callback_data="prcn")]]))
        return
    await _save_pending(m, state, bot, m.from_user.id, fid, uid)
    tail = ("\n\n➡️ <b>Keyingi o'quvchi ismini yozing</b> (tugatish — menyudagi istalgan tugma)."
            if await state.get_state() == Pay.search.state else "")
    await m.answer("✅ Chek qabul qilindi. Rahbar tasdiqlagach, to'lov hisobga olinadi va xabar keladi." + tail)


@router.callback_query(F.data.in_({"prcu", "prcn"}))
async def pay_receipt_shared(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    if not d.get("rc_uid") or await state.get_state() != Pay.receipt.state:
        await c.answer("Eskirgan. Chekni qayta yuboring.", show_alert=True)
        return
    await c.answer()
    if c.data == "prcn":
        await state.update_data(rc_fid=None, rc_uid=None)
        await c.message.edit_text("Boshqa chekni yuboring (rasm yoki PDF).")
        return
    await c.message.edit_text("✅ Bitta chek bir necha o'quvchi uchun — rahbar umumiy summani tekshiradi.")
    await _save_pending(c.message, state, bot, c.from_user.id, d["rc_fid"], d["rc_uid"])
    tail = ("\n\n➡️ <b>Keyingi o'quvchi ismini yozing</b> (tugatish — menyudagi istalgan tugma)."
            if await state.get_state() == Pay.search.state else "")
    await c.message.answer("✅ Chek qabul qilindi. Rahbar tasdiqlagach hisobga olinadi." + tail)


@router.callback_query(F.data == "nochek")
async def pay_no_receipt(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await _save_pending(c.message, state, bot, c.from_user.id, None, None)
    await c.message.edit_text("✅ Saqlandi. Rahbar bank ilovasida tekshirib tasdiqlagach hisobga olinadi.\n\n"
                              "➡️ <b>Keyingi o'quvchi ismini yozing</b> (tugatish — menyudagi istalgan tugma).")


@router.message(Pay.receipt)
async def pay_receipt_wrong(m: Message, state: FSMContext) -> None:
    who = await _who(m.from_user.id)
    txt = (m.text or "").strip()
    if who.staff and len(txt) >= 2 and not any(ch.isdigit() for ch in txt):
        d = await state.get_data()
        await state.update_data(skip_to=txt[:60])
        await m.answer(f"⏳ {d.get('name', '')} · {S.fmt_som(d.get('amount', 0))} — hali saqlanmadi, chek kutilmoqda.\n"
                       "Nima qilamiz?", reply_markup=IM(inline_keyboard=[
                           [IB(text="📎 Chek yo'q — rahbar tekshirsin (saqlash)", callback_data="nochek")],
                           [IB(text=f"⏭ Saqlamasdan «{txt[:20]}» ni qidirish", callback_data="prskip")]]))
        return
    await m.answer("Chekni rasm (skrinshot) yoki PDF fayl qilib yuboring.\n"
                   "Chek bo'lmasa — «Chek yo'q — rahbar tekshirsin» tugmasi; chiqish — menyudagi istalgan tugma.")


@router.callback_query(F.data == "prskip")
async def pay_skip(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    txt = d.get("skip_to")
    await c.answer()
    await state.clear()
    await state.set_state(Pay.search)
    if d.get("last"):
        await state.update_data(last=d["last"])
    await c.message.edit_text(f"⏭ {d.get('name', '')} uchun to'lov saqlanmadi.")
    if txt:
        await pay_search(c.message, state, text=txt)


# ---------- 4. rahbar tasdig'i ----------
async def _batch_view(s, pay: Payment) -> tuple[str, list[Payment], list[tuple[str, int]], int]:
    pays = await S.batch_payments(s, pay)
    items, name, sid = [], "", None
    for p in pays:
        e = await s.get(Enrollment, p.enrollment_id, options=[selectinload(Enrollment.group),
                                                             selectinload(Enrollment.student)])
        items.append((e.group.name, p.amount))
        name, sid = e.student.full_name, e.student_id
    return name, pays, items, sid


@router.message(F.text == K.B_PENDING)
async def pending(m: Message, bot: Bot) -> None:
    who = await _who(m.from_user.id)
    if not _is(who, "rahbar"):
        return
    async with Session() as s:
        seen, views = set(), []
        for p in await S.pending_payments(s):
            key = p.batch or p.id
            if key in seen:
                continue
            seen.add(key)
            name, pays, items, _ = await _batch_view(s, p)
            head = next((x for x in pays if x.receipt_file_id), pays[0])
            shared = ""
            if head.receipt_hash:
                uses = await S.receipt_uses(s, head.receipt_hash)
                if len(uses) > 1:
                    shared = ("\n\n📎 Bitta chek: " + ", ".join(f"{n} {S.fmt_som(a)}" for _, n, a in uses)
                              + f" — jami {S.fmt_som(sum(a for *_, a in uses))}")
            views.append((head, name, sum(x.amount for x in pays), items, shared))
    other = await pending_other(bot, m.chat.id)
    if not views and not other:
        await m.answer("Tasdiq kutayotgan narsa yo'q ✅")
        return
    for head, name, total, items, shared in views:
        cap = _pending_caption(name, total, head.paid_date, items) + shared
        if head.receipt_file_id:
            await send_photo(bot, m.chat.id, head.receipt_file_id, cap, reply_markup=_confirm_kb(head.id))
        else:
            await m.answer(cap + "\n(chek rasmi yo'q)", reply_markup=_confirm_kb(head.id))


async def _decide(c: CallbackQuery, bot: Bot, ok: bool) -> None:
    pid = int(c.data.split(":")[1])
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and "rahbar" in who.staff.role_list()):
            await c.answer("Faqat rahbar tasdiqlaydi", show_alert=True)
            return
        pay = await s.get(Payment, pid)
        if pay.status != "kutilmoqda":
            await c.answer("Bu to'lov allaqachon ko'rib chiqilgan", show_alert=True)
            return
        name, pays, items, sid = await _batch_view(s, pay)
        coins = 0
        for p in pays:
            if ok:
                coins += await S.confirm_payment(s, p, who.staff)
            else:
                await S.reject_payment(s, p, who.staff)
        if ok:
            await s.flush()
            await S.settle_left_debts(s, sid)
        y, m = map(int, pay.month.split("-"))
        left = (await S.student_debt(s, sid, y, m)).debt
        parents = await S.parent_tg_ids(s, sid)
        total, method, month = sum(p.amount for p in pays), pay.method, pay.month
        enterer = await s.get(Staff, pay.entered_by) if pay.entered_by else None
        enterer_tg = enterer.tg_id if enterer and enterer.tg_id and enterer.tg_id != c.from_user.id else None
        await s.commit()
        from app.bot.vault import money_line
        ml = await money_line(s) if ok and method in ("karta", "kochirish") else ""
    mark = ("\n\n✅ Tasdiqlandi" + (f"\n{ml}" if ml else "")) if ok else "\n\n❌ Rad etildi"
    if c.message.photo or c.message.document:
        await c.message.edit_caption(caption=(c.message.caption or "") + mark)
    else:
        await c.message.edit_text((c.message.text or "") + mark)
    if ok:
        await _send_receipt(bot, parents, name, total, method, month, items, left, coins)
    else:
        for t in parents:
            await send(bot, t, f"❌ {name}: {S.fmt_som(total)} lik o'tkazma tasdiqlanmadi. "
                               "Iltimos, markaz bilan bog'laning.")
    if enterer_tg:
        await send(bot, enterer_tg, (f"✅ Karta to'lovi tasdiqlandi: {name} · {S.fmt_som(total)} — hisobga olindi."
                                     if ok else
                                     f"❌ Karta to'lovi rad etildi: {name} · {S.fmt_som(total)}.\n"
                                     "Hisobga olinmadi. Pul bankka tushganini tekshiring; chek boshqa bo'lsa — qayta kiriting."))


@router.callback_query(F.data.startswith("pc:"))
async def pay_confirm(c: CallbackQuery, bot: Bot) -> None:
    await _decide(c, bot, True)


@router.callback_query(F.data.startswith("pr:"))
async def pay_reject(c: CallbackQuery, bot: Bot) -> None:
    await _decide(c, bot, False)


# ---------- 5. qarzdorlar (o'quvchi bo'yicha umumiy) ----------
@router.message(F.text == K.B_DEBTS)
async def debts(m: Message) -> None:
    who = await _who(m.from_user.id)
    if _is(who, "admin") and not await S.admin_may_see_debts():
        await m.answer("Qarzdorlarni ko'rish uchun rahbar ruxsat berishi kerak.")
        return
    if not _is(who, "moliya", "rahbar", "admin"):
        return
    t = today()
    async with Session() as s:
        items = await S.student_debtors(s, t.year, t.month)
        old = await S.left_debts(s, before=date(t.year, t.month, 1))
        await s.commit()
    if not items and not old:
        await m.answer(f"{t:%m.%Y}: qarzdor yo'q ✅")
        return
    out = [f"<b>Qarzdorlar · {t:%m.%Y}</b>"]
    for sd in items:
        groups = ", ".join(p.enrollment.group.name for p in sd.parts)
        out.append(f"• <b>{sd.student.full_name}</b> — {S.fmt_som(sd.debt)}\n   {groups}")
    if not items:
        out.append("Bu oy qarzdor yo'q ✅")
    if old:
        out.append("\n<b>🚪 Guruhdan ketganlar qarzi</b>")
        for st in old:
            e = st.enrollment
            out.append(f"• <b>{e.student.full_name}</b> — {S.fmt_som(st.debt)}\n   {e.group.name}, ketgan {e.left_date:%d.%m.%Y}")
    if who.staff.current_role == "rahbar" or t.day > 25:
        out.append(f"\nJami: {len(items) + len(old)} ta, "
                   f"{S.fmt_som(sum(x.debt for x in items) + sum(x.debt for x in old))}")
    text = "\n".join(out)
    while text:
        cut = text.rfind("\n", 0, 3800) if len(text) > 3800 else len(text)
        await m.answer(text[:cut])
        text = text[cut:].lstrip("\n")



def _ho_kb(hid: int):
    from app.bot.cash import _decide_kb
    return _decide_kb(hid)


async def pending_other(bot: Bot, chat: int) -> int:
    """Kutilayotganlar: to'lovlardan tashqari — topshirilgan pul, avans so'rovlari, xarajatlar, test so'rovlari."""
    from sqlalchemy import select
    from app.db.models import CashHandover, Expense, Payout
    async with Session() as s:
        hos = (await s.execute(select(CashHandover, Staff.full_name).outerjoin(Staff, Staff.id == CashHandover.by_id)
                               .where(CashHandover.status == "kutilmoqda", CashHandover.to_kind == "rahbar"))).all()
        avs = (await s.execute(select(Payout, Staff.full_name).join(Staff, Staff.id == Payout.staff_id)
                               .where(Payout.status == "kutilmoqda", Payout.kind == "avans"))).all()
        exs = (await s.execute(select(Expense, Staff.full_name).outerjoin(Staff, Staff.id == Expense.by_id)
                               .where(Expense.status == "kutilmoqda"))).all()
    n = 0
    for h, nm in hos:
        n += 1
        await send(bot, chat, f"💼 <b>Sizga topshirilgan pul</b>\n{nm or ''}: {S.fmt_som(h.amount)} — konvertni sanab tasdiqlang",
                   reply_markup=_ho_kb(h.id))
    for p, nm in avs:
        n += 1
        await send(bot, chat, f"💸 <b>Avans so'rovi</b>\n{nm}: {S.fmt_som(p.amount)}",
                   reply_markup=IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=f"avy:{p.id}"),
                                                     IB(text="❌ Rad", callback_data=f"avn:{p.id}")]]))
    for e, nm in exs:
        n += 1
        ok = f"xk:{e.id}" if e.source == "kichik" else f"xy:{e.id}"
        await send(bot, chat, f"🧾 <b>Xarajat</b> ({nm or ''})\n{e.category}: {S.fmt_som(e.amount)}\n{e.note or ''}",
                   reply_markup=IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=ok),
                                                     IB(text="❌ Rad", callback_data=f"xn:{e.id}")]]))
    from app.bot.tests_flow import send_request_cards
    n += await send_request_cards(bot, chat)
    return n
