"""Kassa: kunlik "ko'r" yopish, inkassatsiya; ijtimoiy o'quvchilar to'lovi; "Dars tugadi" xabari."""
from __future__ import annotations

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
from app.db.models import (Attendance, CashClose, CashHandover, Enrollment, Group, Lesson, Payment, Setting, Staff,
                           StaffPoint, Student)
from app.db.session import Session
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
EARLY_MIN = 15


def now() -> datetime:
    return datetime.now(TZ)


def utcnow() -> datetime:
    return datetime.utcnow()


def _amount(text: str | None) -> int | None:
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    return int(digits) if digits else None


# ================= kassa qoldig'i (faqat bot ichida, moliyaga ko'rsatilmaydi) =================
async def _opening(s) -> dict | None:
    row = await s.get(Setting, "cash_opening")
    return row.value if row else None


async def expected_cash(s) -> int | None:
    op = await _opening(s)
    if not op:
        return None
    since = datetime.fromisoformat(op["at"])
    # kassa sanalgan kundan oldingi sana bilan kiritilgan (orqaga qaytib kiritilgan) to'lov va xarajatlar
    # kassaga ta'sir qilmaydi: ular allaqachon sanalgan pulning ichida yoki ilgari ishlatilgan
    open_day = date.fromisoformat(op["day"]) if op.get("day") else (since + timedelta(hours=5)).date()
    pays = (await s.execute(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.method == "naqd", Payment.status == "tasdiq", Payment.created_at >= since,
        Payment.paid_date >= open_day))).scalar()
    outs = (await s.execute(select(func.coalesce(func.sum(CashHandover.amount), 0)).where(
        CashHandover.status != "rad", CashHandover.created_at >= since))).scalar()
    from app.db.models import Expense, ServiceSale
    sales = (await s.execute(select(func.coalesce(func.sum(ServiceSale.amount), 0)).where(
        ServiceSale.method == "naqd", ServiceSale.status == "tasdiq", ServiceSale.created_at >= since))).scalar()
    spent = (await s.execute(select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.method == "naqd", Expense.status == "tasdiq", Expense.created_at >= since,
        Expense.date >= open_day))).scalar()
    from app.db.models import Payout
    paid_out = (await s.execute(select(func.coalesce(func.sum(Payout.amount), 0)).where(
        Payout.method == "naqd", Payout.status.in_(("berildi", "olindi")), Payout.created_at >= since))).scalar()
    return int(op["amount"]) + int(pays) + int(sales) - int(outs) - int(spent) - int(paid_out)


async def _set_opening(s, amount: int) -> None:
    row = await s.get(Setting, "cash_opening")
    db_now = (await s.execute(select(func.now()))).scalar()          # bazaning o'z vaqti (created_at bilan bir xil)
    if isinstance(db_now, str):
        db_now = datetime.fromisoformat(db_now)
    val = {"amount": amount, "at": db_now.replace(tzinfo=None, microsecond=0).isoformat(),
           "day": datetime.now(TZ).date().isoformat()}
    if row:
        row.value = val
    else:
        s.add(Setting(key="cash_opening", value=val))


# ================= 🔐 kunlik yopish =================
class Cl(StatesGroup):
    counted = State()


@router.message(F.text == K.B_CASH_CLOSE)
async def close_start(m: Message, state: FSMContext) -> None:
    t = now().date()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not (who.staff and who.staff.current_role in ("moliya", "rahbar")):
            return
        done = (await s.execute(select(CashClose).where(CashClose.date == t, CashClose.decision != "qayta")
                                .order_by(CashClose.id.desc()))).scalars().first()
    if done:
        await m.answer("Bugungi kassa allaqachon yopilgan." + (" Natija: ✅ mos." if done.status != "farq" else
                       " Natija: ⚠️ farq — rahbar ko'rib chiqadi."))
        return
    await state.set_state(Cl.counted)
    await m.answer("🔐 <b>Kassani yopish</b>\nKassadagi naqd pulni sanang va summani yozing (masalan: 3 450 000).\n"
                   "Bot faqat «mos» yoki «farq bor» deb javob beradi.")


@router.message(Cl.counted)
async def close_save(m: Message, state: FSMContext, bot: Bot) -> None:
    counted = _amount(m.text)
    if counted is None:
        await m.answer("Summani raqam bilan yozing: 3450000")
        return
    await state.clear()
    t = now().date()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        exp = await expected_cash(s)
        if exp is None:
            await _set_opening(s, counted)
            s.add(CashClose(date=t, expected=counted, counted=counted, diff=0, status="boshlang", by_id=who.staff.id))
            status, diff, exp = "boshlang", 0, counted
        else:
            diff = counted - exp
            status = "mos" if diff == 0 else "farq"
            s.add(CashClose(date=t, expected=exp, counted=counted, diff=diff, status=status, by_id=who.staff.id))
        await S.audit(s, who.staff, "kassa_yopish", status=status)
        await s.commit()
        cid = (await s.execute(select(CashClose.id).order_by(CashClose.id.desc()))).scalars().first()
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        name = who.staff.full_name
    if status == "boshlang":
        await m.answer("✅ Kassa boshlang'ich qoldig'i qayd etildi. Ertadan bot har kuni solishtiradi.")
    elif status == "mos":
        await m.answer("✅ Kassa mos. Rahmat!")
    else:
        await m.answer("⚠️ Farq bor. Rahbarga xabar berildi.")
    sign = "+" if diff > 0 else ""
    text = (f"🔐 <b>Kassa yopildi · {t:%d.%m}</b> ({name})\nKutilgan: {S.fmt_som(exp)}\nSanalgan: {S.fmt_som(counted)}\n"
            + ("Boshlang'ich qoldiq sifatida qayd etildi." if status == "boshlang" else
               "✅ Mos" if status == "mos" else f"⚠️ Farq: <b>{sign}{S.fmt_som(diff)}</b>"))
    kb = None
    if status == "farq":
        kb = IM(inline_keyboard=[[IB(text="−2 ball moliyaga", callback_data=f"cf:{cid}:pen")],
                                 [IB(text="Sabab aniqlandi — qoldiqni tuzatish", callback_data=f"cf:{cid}:fix")],
                                 [IB(text="🔄 Qayta sanashga ruxsat", callback_data=f"cf:{cid}:re")]])
    for b in bosses:
        await send(bot, b, text, reply_markup=kb)


@router.callback_query(F.data.startswith("cf:"))
async def close_decide(c: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    from app.bot.pin import need_pin
    if await need_pin(c, state):
        return
    _, cid, act = c.data.split(":")
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            await c.answer("Faqat rahbar.", show_alert=True)
            return
        cl = await s.get(CashClose, int(cid))
        mol = await s.get(Staff, cl.by_id) if cl.by_id else None
        if act == "pen" and mol:
            s.add(StaffPoint(staff_id=mol.id, points=-2, role="moliya", date=cl.date,
                             reason=f"Kassa yopishda farq {cl.date:%d.%m}: {S.fmt_som(cl.diff)}"))
            cl.decision, note = "jarima", "−2 ball yozildi"
        elif act == "fix":
            await _set_opening(s, cl.counted)
            cl.decision, note = "tuzatildi", "Qoldiq sanalgan summaga tenglashtirildi"
        else:
            cl.decision, note = "qayta", "Qayta sanashga ruxsat berildi"
        await S.audit(s, who.staff, "kassa_qaror", close=cl.id, decision=cl.decision)
        await s.commit()
        mtg = mol.tg_id if mol else None
    await c.message.edit_text(c.message.html_text + f"\n\n{note}.")
    if act == "re" and mtg:
        await send(bot, mtg, "🔄 Rahbar kassani qayta sanashga ruxsat berdi — «🔐 Kassani yopish» ni qayta bosing.")
    if act == "pen" and mtg:
        await send(bot, mtg, "⚠️ Kassa yopishdagi farq uchun −2 ball yozildi.")


async def close_reminder(bot: Bot) -> None:
    """19:30 — kassa yopilmagan bo'lsa moliyaga eslatma."""
    t = now().date()
    async with Session() as s:
        done = (await s.execute(select(CashClose.id).where(CashClose.date == t))).first()
        to = [x.tg_id for x in await S.staff_with_role(s, "moliya") if x.tg_id]
    if not done:
        for x in to:
            await send(bot, x, "🔐 Kun oxiri: kassani yopishni unutmang — «🔐 Kassani yopish».")


# ================= 💼 inkassatsiya =================
class Ho(StatesGroup):
    amount = State()
    receipt = State()


@router.message(F.text == K.B_HANDOVER)
async def ho_start(m: Message, state: FSMContext) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
    if not (who.staff and who.staff.current_role == "moliya"):
        return
    await state.set_state(Ho.amount)
    await m.answer("💼 <b>Kassani topshirish</b>\nQancha naqd pul topshiryapsiz? Summani yozing:")


@router.message(Ho.amount)
async def ho_amount(m: Message, state: FSMContext) -> None:
    amt = _amount(m.text)
    if not amt:
        await m.answer("Summani raqam bilan yozing: 2000000")
        return
    await state.update_data(amount=amt)
    await m.answer(f"{S.fmt_som(amt)} — kimga?", reply_markup=IM(inline_keyboard=[[
        IB(text="👤 Rahbarga (muhrlangan konvertda)", callback_data="hot:rahbar")],
        [IB(text="🏦 Bankka", callback_data="hot:bank")]]))


@router.callback_query(Ho.amount, F.data.startswith("hot:"))
async def ho_to(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    to = c.data.split(":")[1]
    if to == "bank":
        await state.set_state(Ho.receipt)
        await c.message.edit_text("🏦 Bank kvitansiyasini yuboring (rasm yoki PDF):")
        return
    await _ho_save(c.message, state, bot, c.from_user.id, "rahbar", None)


@router.message(Ho.receipt, F.photo | F.document)
async def ho_receipt(m: Message, state: FSMContext, bot: Bot) -> None:
    from app.bot.notify import receipt_of
    rc = receipt_of(m)
    if not rc:
        await m.answer("Kvitansiyani rasm yoki PDF fayl qilib yuboring.")
        return
    await _ho_save(m, state, bot, m.from_user.id, "bank", rc[0])


async def _ho_save(msg: Message, state: FSMContext, bot: Bot, tg: int, to: str, fid: str | None) -> None:
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        who = await S.who_by_tg(s, tg)
        h = CashHandover(amount=d["amount"], to_kind=to, receipt_file_id=fid, by_id=who.staff.id)
        s.add(h)
        await s.flush()
        await S.audit(s, who.staff, "inkassatsiya", amount=d["amount"], to=to)
        await s.commit()
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        hid, name = h.id, who.staff.full_name
    await msg.answer(f"✅ {S.fmt_som(d['amount'])} topshirildi deb qayd etildi. Rahbar tasdiqlaydi.")
    kb = IM(inline_keyboard=[[IB(text="✅ Qabul qildim", callback_data=f"hoy:{hid}"),
                              IB(text="❌ Mos emas", callback_data=f"hon:{hid}")]])
    text = (f"💼 <b>Inkassatsiya</b>\n{name}: {S.fmt_som(d['amount'])} → "
            + ("sizga (konvertda)" if to == "rahbar" else "bankka (kvitansiya ilova)"))
    for b in bosses:
        if fid:
            from app.bot.notify import send_photo
            await send_photo(bot, b, fid, text)
        await send(bot, b, text if not fid else "Tasdiqlang:", reply_markup=kb)


@router.callback_query(F.data.startswith(("hoy:", "hon:")))
async def ho_decide(c: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    from app.bot.pin import need_pin
    if await need_pin(c, state):
        return
    kind, hid = c.data.split(":")
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            await c.answer("Faqat rahbar.", show_alert=True)
            return
        h = await s.get(CashHandover, int(hid))
        h.status, h.confirmed_by = ("tasdiq" if kind == "hoy" else "rad"), who.staff.id
        await S.audit(s, who.staff, "inkassatsiya_qaror", id=h.id, status=h.status)
        await s.commit()
        mol = await s.get(Staff, h.by_id) if h.by_id else None
        amt = h.amount
    await c.message.edit_text(c.message.html_text + ("\n\n✅ Qabul qilindi" if kind == "hoy" else "\n\n❌ Mos emas"))
    if mol and mol.tg_id:
        await send(bot, mol.tg_id, f"💼 {S.fmt_som(amt)} topshirish: " +
                   ("✅ rahbar qabul qildi." if kind == "hoy" else "❌ rahbar «mos emas» dedi — bog'laning."))


# ================= 💼 Kassa (rahbar) =================
@router.message(F.text == K.B_CASH)
async def cash_view(m: Message) -> None:
    t = now().date()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            return
        exp = await expected_cash(s)
        closes = (await s.execute(select(CashClose).order_by(CashClose.date.desc(), CashClose.id.desc())
                                  .limit(7))).scalars().all()
        pend = (await s.execute(select(CashHandover).where(CashHandover.status == "kutilmoqda"))).scalars().all()
        social = (await s.execute(select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.method == "ijtimoiy", Payment.month == f"{t:%Y-%m}"))).scalar()
    lines = ["💼 <b>Kassa</b>",
             f"Kassada bo'lishi kerak: <b>{S.fmt_som(exp)}</b>" if exp is not None else
             "Boshlang'ich qoldiq hali kiritilmagan (moliya birinchi «🔐 Kassani yopish» da kiritadi).",
             f"Tasdiq kutayotgan topshirishlar: {len(pend)} ta" + (f" ({S.fmt_som(sum(h.amount for h in pend))})" if pend else ""),
             f"Ijtimoiy to'lovlar (kassaga kirmaydi) shu oy: {S.fmt_som(int(social))}", "", "<b>Oxirgi yopishlar</b>"]
    for cl in closes:
        mark = {"mos": "✅", "farq": "⚠️", "boshlang": "▫️"}[cl.status]
        lines.append(f"{mark} {cl.date:%d.%m}: {S.fmt_som(cl.counted)}" +
                     (f" (farq {'+' if cl.diff > 0 else ''}{S.fmt_som(cl.diff)})" if cl.diff else ""))
    if not closes:
        lines.append("—")
    await m.answer("\n".join(lines))


# ================= ijtimoiy o'quvchilar: har oy avtomatik "to'landi" =================
async def social_job(bot: Bot | None = None) -> int:
    """Har kuni 07:00: ijtimoiy chegirmali o'rinlarga shu oy uchun shartli to'lov (kassaga kirmaydi) va coin."""
    t = now().date()
    month = f"{t:%Y-%m}"
    made = 0
    async with Session() as s:
        enrs = (await s.execute(select(Enrollment).where(Enrollment.active, Enrollment.discount_kind == "ijtimoiy")
                                .options(selectinload(Enrollment.group), selectinload(Enrollment.student)))).scalars().all()
        for e in enrs:
            has = (await s.execute(select(Payment.id).where(Payment.enrollment_id == e.id, Payment.month == month,
                                                            Payment.method == "ijtimoiy"))).first()
            if has:
                continue
            st = await S.month_status(s, e, t.year, t.month)
            if st.gross <= 0:
                continue
            # bot "5-sanagacha to'liq to'ladi" deb belgilaydi: to'liq narxdan coin + bonus
            pay = Payment(enrollment_id=e.id, month=month, amount=st.gross, method="ijtimoiy", status="tasdiq",
                          paid_date=max(t.replace(day=1), e.join_date if e.join_date <= t else t))
            s.add(pay)
            await s.flush()
            await S.apply_payment_coins(s, pay, e)
            made += 1
        await s.commit()
    return made


# ================= 🔔 Dars tugadi =================
@router.callback_query(F.data.startswith("dz:"))
async def lesson_end(c: CallbackQuery, bot: Bot) -> None:
    lid = int(c.data.split(":")[1])
    n = now()
    async with Session() as s:
        les = await s.get(Lesson, lid)
        if les.ended_at:
            await c.answer(f"Allaqachon belgilangan: {les.ended_at:%H:%M}", show_alert=True)
            return
        g = await s.get(Group, les.group_id)
        les.ended_at = n.replace(tzinfo=None)
        present = (await s.execute(select(Attendance).where(Attendance.lesson_id == lid,
                                                            Attendance.mark != "y"))).scalars().all()
        msgs = []
        for a in present:
            e = await s.get(Enrollment, a.enrollment_id, options=[selectinload(Enrollment.student)])
            for t in await S.parent_tg_ids(s, e.student_id):
                msgs.append((t, f"🔔 {e.student.full_name}: «{g.name}» darsi {n:%H:%M} da tugadi, uyga chiqdi."))
        await s.commit()
        early = g.end_time and (datetime.combine(n.date(), g.end_time, TZ) - n) > timedelta(minutes=EARLY_MIN)
    await c.answer("Ota-onalarga xabar yuborildi")
    await c.message.answer(f"🔔 Dars tugadi ({n:%H:%M}). {len(msgs)} ta ota-onaga xabar yuborildi."
                           + (f"\n⚠️ Jadvaldagidan {EARLY_MIN} daqiqadan ko'proq oldin tugadi — rahbar hisobotida "
                              "ko'rinadi." if early else ""))
    for t, text in msgs:
        await send(bot, t, text)


async def early_lessons(s, d: date) -> list[str]:
    out = []
    les = (await s.execute(select(Lesson).where(Lesson.date == d, Lesson.ended_at.is_not(None)))).scalars().all()
    for l in les:
        g = await s.get(Group, l.group_id)
        if g.end_time and datetime.combine(d, g.end_time) - l.ended_at > timedelta(minutes=EARLY_MIN):
            out.append(f"{g.name} ({l.ended_at:%H:%M}, jadval {g.end_time:%H:%M})")
    return out
