"""To'lovlar: naqd kiritish, karta cheki, rahbar tasdig'i, qarzdorlar."""
from __future__ import annotations

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
from app.db.models import Enrollment, Payment
from app.db.session import Session
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
METHOD = {"naqd": "Naqd", "karta": "Kartaga o'tkazma", "ijtimoiy": "Ijtimoiy"}


def today() -> date:
    return datetime.now(TZ).date()


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


class Pay(StatesGroup):
    search = State()
    amount = State()
    receipt = State()


async def _who(tg_id: int):
    async with Session() as s:
        return await S.who_by_tg(s, tg_id)


def _is(who, *roles) -> bool:
    return bool(who.staff and who.staff.current_role in roles)


# ---------- 1. qidirish ----------
@router.message(F.text == K.B_PAY_IN)
async def pay_start(m: Message, state: FSMContext) -> None:
    who = await _who(m.from_user.id)
    if not (_is(who, "moliya", "rahbar") or who.parent):
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


async def _enr_kb(s, enrs, t: date, prefix: str) -> IM:
    rows = []
    for e in enrs:
        st = await S.month_status(s, e, t.year, t.month)
        tail = "to'langan" if st.debt == 0 else f"qoldi {S.fmt_som(st.debt)}"
        rows.append([IB(text=f"{e.group.name} · {tail}", callback_data=f"{prefix}:{e.id}")])
    return IM(inline_keyboard=rows)


@router.callback_query(F.data.startswith("ps:"))
async def pay_pick_student(c: CallbackQuery, state: FSMContext) -> None:
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        enrs = await S.student_enrollments(s, sid)
        kb = await _enr_kb(s, enrs, today(), "pe")
    if not enrs:
        await c.message.edit_text("Bu o'quvchi hech qaysi guruhda emas.")
        return
    await c.message.edit_text("Qaysi guruh uchun to'lov?", reply_markup=kb)


# ota-ona: o'z farzandlari ro'yxatidan
@router.message(F.text == K.B_PARENT_PAY)
async def parent_pay(m: Message, state: FSMContext) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not who.parent:
            return
        enrs = [e for ch in who.parent.children for e in await S.student_enrollments(s, ch.id)]
        kb = await _enr_kb(s, enrs, today(), "pe")
    await state.clear()
    await m.answer("Kartaga o'tkazgan to'lovingiz qaysi guruh uchun?", reply_markup=kb)


@router.callback_query(F.data.startswith("pe:"))
async def pay_pick_enr(c: CallbackQuery, state: FSMContext) -> None:
    eid = int(c.data.split(":")[1])
    t = today()
    async with Session() as s:
        e = await s.get(Enrollment, eid, options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
        st = await S.month_status(s, e, t.year, t.month)
    await state.update_data(eid=eid, month=month_key(t), name=e.student.full_name, group=e.group.name,
                            sid=e.student_id)
    await state.set_state(Pay.amount)
    kb = IM(inline_keyboard=[[IB(text=f"Qarzini to'liq: {S.fmt_som(st.debt)}", callback_data=f"pa:{st.debt}")]]) \
        if st.debt else None
    await c.message.edit_text(
        f"<b>{e.student.full_name}</b> · {e.group.name}\n{t:%m.%Y}: to'lashi kerak {S.fmt_som(st.payable)}, "
        f"to'langan {S.fmt_som(st.paid)}.\n\nSummani raqam bilan yozing (masalan 300000):", reply_markup=kb)


async def _got_amount(msg: Message, state: FSMContext, amount: int, tg_id: int) -> None:
    if amount <= 0:
        await msg.answer("Summa noto'g'ri. Faqat raqam yozing, masalan 300000.")
        return
    await state.update_data(amount=amount)
    who = await _who(tg_id)
    if who.parent:
        await state.set_state(Pay.receipt)
        await msg.answer("O'tkazma chekining rasmini (skrinshot) yuboring.")
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
async def pay_method(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    method = c.data.split(":")[1]
    if method == "karta":
        await state.set_state(Pay.receipt)
        await c.message.edit_text("O'tkazma chekining rasmini yuboring. To'lov rahbar tasdiqlagach hisobga olinadi.")
        return
    d = await state.get_data()
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        e = await s.get(Enrollment, d["eid"], options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
        pay = await S.record_payment(s, e, d["amount"], "naqd", today(), d["month"], who.staff)
        st = await S.month_status(s, e, today().year, today().month)
        parents = await S.parent_tg_ids(s, e.student_id)
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        await s.commit()
    await state.clear()
    # moliya faqat shu to'lovni ko'radi
    await c.message.edit_text(f"✅ Saqlandi: {d['name']} · {d['group']} · {S.fmt_som(d['amount'])} (naqd).")
    await _send_receipt(bot, parents, d, pay, st)
    for b in bosses:
        if b != c.from_user.id:
            await send(bot, b, f"💵 Naqd to'lov: {d['name']} · {d['group']} · {S.fmt_som(d['amount'])} "
                               f"({who.staff.full_name})")


async def _send_receipt(bot: Bot, tg_ids: list[int], d: dict, pay: Payment, st) -> None:
    left = "To'liq to'langan ✅" if st.debt == 0 else f"Qoldi: {S.fmt_som(st.debt)}"
    coins = f"\nCoin: +{pay.coins_given}" if pay.coins_given else ""
    text = (f"🧾 <b>{settings.center_name}</b> — to'lov qabul qilindi\n{d['name']} · {d['group']}\n"
            f"Summa: {S.fmt_som(pay.amount)} ({METHOD[pay.method]})\nOy: {pay.month}\n{left}{coins}")
    for t in tg_ids:
        await send(bot, t, text)


@router.message(Pay.receipt, F.photo)
async def pay_receipt(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    photo = m.photo[-1]
    async with Session() as s:
        if await S.duplicate_receipt(s, photo.file_unique_id):
            await m.answer("⚠️ Bu chek avval yuborilgan. Boshqa chek rasmini yuboring yoki admin bilan bog'laning.")
            return
        who = await S.who_by_tg(s, m.from_user.id)
        e = await s.get(Enrollment, d["eid"], options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
        pay = await S.record_payment(s, e, d["amount"], "karta", today(), d["month"], who.staff, status="kutilmoqda",
                                     receipt_file_id=photo.file_id, receipt_hash=photo.file_unique_id)
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        await s.commit()
    await state.clear()
    await m.answer("Chek qabul qilindi. Rahbar tasdiqlagach, to'lov hisobga olinadi va xabar keladi.")
    for b in bosses:
        await send_photo(bot, b, photo.file_id, _pending_caption(d, pay), reply_markup=_confirm_kb(pay.id))


def _pending_caption(d: dict, pay: Payment) -> str:
    return (f"💳 Kartaga o'tkazma — tasdiq kutilmoqda\n{d['name']} · {d['group']}\n"
            f"Summa: {S.fmt_som(pay.amount)}\nSana: {pay.paid_date:%d.%m.%Y}\n"
            "Bank ilovasida pul tushganini tekshiring.")


def _confirm_kb(pid: int) -> IM:
    return IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=f"pc:{pid}"),
                                IB(text="❌ Rad etish", callback_data=f"pr:{pid}")]])


@router.message(Pay.receipt)
async def pay_receipt_wrong(m: Message) -> None:
    await m.answer("Iltimos, chekni rasm (skrinshot) qilib yuboring.")


# ---------- 2. rahbar tasdig'i ----------
@router.message(F.text == K.B_PENDING)
async def pending(m: Message, bot: Bot) -> None:
    who = await _who(m.from_user.id)
    if not _is(who, "rahbar"):
        return
    async with Session() as s:
        items = await S.pending_payments(s)
        rows = []
        for p in items:
            e = await s.get(Enrollment, p.enrollment_id, options=[selectinload(Enrollment.group),
                                                                 selectinload(Enrollment.student)])
            rows.append((p, {"name": e.student.full_name, "group": e.group.name}))
    if not rows:
        await m.answer("Tasdiq kutayotgan narsa yo'q ✅")
        return
    for p, d in rows:
        await bot.send_photo(m.chat.id, p.receipt_file_id, caption=_pending_caption(d, p), reply_markup=_confirm_kb(p.id))


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
        e = await s.get(Enrollment, pay.enrollment_id, options=[selectinload(Enrollment.group),
                                                               selectinload(Enrollment.student)])
        d = {"name": e.student.full_name, "group": e.group.name}
        if ok:
            await S.confirm_payment(s, pay, who.staff)
        else:
            await S.reject_payment(s, pay, who.staff)
        st = await S.month_status(s, e, pay.paid_date.year, pay.paid_date.month)
        parents = await S.parent_tg_ids(s, e.student_id)
        await s.commit()
    await c.message.edit_caption(caption=c.message.caption + ("\n\n✅ Tasdiqlandi" if ok else "\n\n❌ Rad etildi"))
    if ok:
        await _send_receipt(bot, parents, d, pay, st)
    else:
        for t in parents:
            await send(bot, t, f"❌ {d['name']} · {d['group']}: {S.fmt_som(pay.amount)} lik o'tkazma tasdiqlanmadi. "
                               "Iltimos, markaz bilan bog'laning.")


@router.callback_query(F.data.startswith("pc:"))
async def pay_confirm(c: CallbackQuery, bot: Bot) -> None:
    await _decide(c, bot, True)


@router.callback_query(F.data.startswith("pr:"))
async def pay_reject(c: CallbackQuery, bot: Bot) -> None:
    await _decide(c, bot, False)


# ---------- 3. qarzdorlar ----------
@router.message(F.text == K.B_DEBTS)
async def debts(m: Message) -> None:
    who = await _who(m.from_user.id)
    if not (_is(who, "moliya", "rahbar") or (_is(who, "admin") and await S.admin_may_see_debts())):
        return
    t = today()
    async with Session() as s:
        items = await S.debtors(s, t.year, t.month)
    if not items:
        await m.answer(f"{t:%m.%Y}: qarzdor yo'q ✅")
        return
    show_total = who.staff.current_role == "rahbar" or t.day > 25
    out, cur = [f"<b>Qarzdorlar · {t:%m.%Y}</b>"], None
    for st in items:
        g = st.enrollment.group.name
        if g != cur:
            out.append(f"\n<b>{g}</b>")
            cur = g
        out.append(f"• {st.enrollment.student.full_name} — {S.fmt_som(st.debt)}")
    if show_total:
        out.append(f"\nJami: {len(items)} ta, {S.fmt_som(sum(x.debt for x in items))}")
    text = "\n".join(out)
    for i in range(0, len(text), 3800):
        await m.answer(text[i:i + 3800])
