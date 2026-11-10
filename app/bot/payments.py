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
    await m.answer("O'quvchining ismi yoki familiyasidan kamida 2 harf yozing:")


@router.message(Pay.search)
async def pay_search(m: Message, state: FSMContext) -> None:
    async with Session() as s:
        found = await S.search_students(s, m.text or "")
    if not found:
        await m.answer("Topilmadi. Boshqacha yozib ko'ring yoki /start bosing.")
        return
    kb = IM(inline_keyboard=[[IB(text=x.full_name, callback_data=f"ps:{x.id}")] for x in found])
    await m.answer("O'quvchini tanlang:", reply_markup=kb)


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
    kb = IM(inline_keyboard=[[IB(text="💵 Naqd", callback_data="pm:naqd"),
                              IB(text="💳 Kartaga o'tkazma", callback_data="pm:karta")]])
    await msg.answer(f"{S.fmt_som(amount)} — qanday to'landi?", reply_markup=kb)


@router.message(Pay.amount)
async def pay_amount(m: Message, state: FSMContext) -> None:
    digits = "".join(ch for ch in (m.text or "") if ch.isdigit())
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
    await state.update_data(paid=d.isoformat(), month=month_key(d))
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
        y, m = map(int, d["month"].split("-"))
        sd = await S.student_debt(s, d["sid"], y, m)
        parents = await S.parent_tg_ids(s, d["sid"])
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        items = [(e.group.name, p.amount) for e, p in res]
        coins = sum(p.coins_given for _, p in res)
        await s.commit()
    await state.clear()
    paid = date.fromisoformat(d["paid"])
    await msg.answer(f"✅ Saqlandi: {d['name']} · {S.fmt_som(d['amount'])} "
                     f"({METHOD[d['method']].lower()}, {paid:%d.%m}).{_breakdown(items)}")
    await _send_receipt(bot, parents, d["name"], d["amount"], d["method"], d["month"], items, sd.debt, coins)
    for b in bosses:
        if b != tg_id:
            await send(bot, b, f"💵 {METHOD[d['method']]}: {d['name']} · {S.fmt_som(d['amount'])} "
                               f"({who.staff.full_name})")


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
        await s.commit()
    await state.clear()
    caption = _pending_caption(d["name"], d["amount"], first.paid_date, items)
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
    async with Session() as s:
        if await S.duplicate_receipt(s, uid):
            await m.answer("⚠️ Bu chek avval yuborilgan. Boshqa chek rasmini yuboring yoki admin bilan bog'laning.")
            return
    await _save_pending(m, state, bot, m.from_user.id, fid, uid)
    await m.answer("Chek qabul qilindi. Rahbar tasdiqlagach, to'lov hisobga olinadi va xabar keladi.")


@router.callback_query(F.data == "nochek")
async def pay_no_receipt(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await _save_pending(c.message, state, bot, c.from_user.id, None, None)
    await c.message.edit_text("Saqlandi. Rahbar bank ilovasida tekshirib tasdiqlagach hisobga olinadi.")


@router.message(Pay.receipt)
async def pay_receipt_wrong(m: Message) -> None:
    await m.answer("Iltimos, chekni rasm (skrinshot) yoki PDF fayl qilib yuboring. Bekor qilish: /start")


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
            views.append((head, name, sum(x.amount for x in pays), items))
    if not views:
        await m.answer("Tasdiq kutayotgan narsa yo'q ✅")
        return
    for head, name, total, items in views:
        cap = _pending_caption(name, total, head.paid_date, items)
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
        y, m = map(int, pay.month.split("-"))
        left = (await S.student_debt(s, sid, y, m)).debt
        parents = await S.parent_tg_ids(s, sid)
        total, method, month = sum(p.amount for p in pays), pay.method, pay.month
        enterer = await s.get(Staff, pay.entered_by) if pay.entered_by else None
        enterer_tg = enterer.tg_id if enterer and enterer.tg_id and enterer.tg_id != c.from_user.id else None
        await s.commit()
    mark = "\n\n✅ Tasdiqlandi" if ok else "\n\n❌ Rad etildi"
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
    if not items:
        await m.answer(f"{t:%m.%Y}: qarzdor yo'q ✅")
        return
    out = [f"<b>Qarzdorlar · {t:%m.%Y}</b>"]
    for sd in items:
        groups = ", ".join(p.enrollment.group.name for p in sd.parts)
        out.append(f"• <b>{sd.student.full_name}</b> — {S.fmt_som(sd.debt)}\n   {groups}")
    if who.staff.current_role == "rahbar" or t.day > 25:
        out.append(f"\nJami: {len(items)} o'quvchi, {S.fmt_som(sum(x.debt for x in items))}")
    text = "\n".join(out)
    while text:
        cut = text.rfind("\n", 0, 3800) if len(text) > 3800 else len(text)
        await m.answer(text[:cut])
        text = text[cut:].lstrip("\n")
