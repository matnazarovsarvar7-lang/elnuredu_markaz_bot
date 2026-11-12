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
BOSS_REFUND = "qaytarish:rahbar"   # Payment.void_reason: rahbar o'z qo'lidan qaytargan pul
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
    """Moliyadagi o'quvchilar puli hisobining boshi. Yo'q bo'lsa — kassalar bilan birga 0 dan avtomatik."""
    row = await s.get(Setting, "cash_opening")
    if row:
        return row.value
    from app.bot.vault import ensure_started
    await ensure_started()
    async with Session() as s2:
        row = await s2.get(Setting, "cash_opening")
        if not row:
            await _set_opening(s2, 0)
            await s2.commit()
            row = await s2.get(Setting, "cash_opening")
        return row.value


async def cash_breakdown(s) -> dict | None:
    """Kutilgan naqd qanday hisoblangani: boshlang'ich qoldiq + kirim − chiqim, va hisobga kirmaganlar (izoh uchun)."""
    op = await _opening(s)
    if not op:
        return None
    from app.db.models import ServiceSale
    since = datetime.fromisoformat(op["at"])
    # kassa sanalgan kundan oldingi sana bilan kiritilgan (orqaga qaytib kiritilgan) to'lov va xarajatlar
    # kassaga ta'sir qilmaydi: ular allaqachon sanalgan pulning ichida yoki ilgari ishlatilgan
    open_day = date.fromisoformat(op["day"]) if op.get("day") else (since + timedelta(hours=5)).date()

    async def total(q):
        return int((await s.execute(q)).scalar() or 0)

    # rahbar o'z qo'lidan qaytargan pul moliyadagi o'quvchilar puliga tegmaydi
    naqd = (Payment.method == "naqd") & (func.coalesce(Payment.void_reason, "") != BOSS_REFUND)
    ok = Payment.status == "tasdiq"
    pays = await total(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        naqd, ok, Payment.created_at >= since, Payment.paid_date >= open_day))
    n_pays = await total(select(func.count(Payment.id)).where(
        naqd, ok, Payment.created_at >= since, Payment.paid_date >= open_day, Payment.amount > 0))
    back = await total(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        naqd, ok, Payment.created_at >= since, Payment.paid_date < open_day))
    before = await total(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        naqd, ok, Payment.created_at < since, Payment.paid_date == open_day))
    outs = await total(select(func.coalesce(func.sum(CashHandover.amount), 0)).where(
        CashHandover.status != "rad", CashHandover.created_at >= since))
    sales = await total(select(func.coalesce(func.sum(ServiceSale.amount), 0)).where(
        ServiceSale.method == "naqd", ServiceSale.status == "tasdiq", ServiceSale.created_at >= since))
    # xarajat va oylik/avans qabul kassasidan qilinmaydi: xarajat — kichik kassadan, oylik/avans — rahbar kassasidan
    spent = paid_out = 0
    opening = int(op["amount"])
    return {"opening": opening, "since": since + timedelta(hours=5), "open_day": open_day, "pays": pays,
            "n_pays": n_pays, "sales": sales, "outs": outs, "spent": spent, "paid_out": paid_out,
            "back": back, "before": before,
            "expected": opening + pays + sales - outs - spent - paid_out}


def breakdown_text(b: dict) -> str:
    lines = [f"Boshlang'ich qoldiq ({b['since']:%d.%m %H:%M} da sanalgan): {S.fmt_som(b['opening'])}",
             f"+ Naqd to'lovlar (shundan keyin kiritilgan): {S.fmt_som(b['pays'])} ({b['n_pays']} ta)"]
    if b["sales"]:
        lines.append(f"+ Naqd xizmat sotuvi: {S.fmt_som(b['sales'])}")
    if b["outs"]:
        lines.append(f"− Kassadan topshirilgan: {S.fmt_som(b['outs'])}")
    if b["spent"]:
        lines.append(f"− Naqd xarajatlar: {S.fmt_som(b['spent'])}")
    if b["paid_out"]:
        lines.append(f"− Naqd oylik/avans: {S.fmt_som(b['paid_out'])}")
    lines.append(f"= <b>Kutilgan: {S.fmt_som(b['expected'])}</b>")
    notes = []
    if b["before"]:
        notes.append(f"• {b['open_day']:%d.%m} kuni boshlang'ich qoldiq sanalishidan <b>oldin</b> kiritilgan naqd to'lovlar: "
                     f"{S.fmt_som(b['before'])} — ular boshlang'ich qoldiq ichida hisoblangan")
    if b["back"]:
        notes.append(f"• Orqaga sana bilan kiritilgan naqd to'lovlar: {S.fmt_som(b['back'])} — kassaga qo'shilmaydi "
                     "(pul o'sha kunlari olingan)")
    if notes:
        lines += ["", "ℹ️ Hisobga kirmaganlar:"] + notes
    return "\n".join(lines)


async def expected_cash(s) -> int | None:
    b = await cash_breakdown(s)
    return b["expected"] if b else None


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
    from app.bot.vault import petty_balance
    async with Session() as s:
        pb = await petty_balance(s)
    await m.answer("🔐 <b>Kassani yopish</b>\n📥 Faqat <b>o'quvchilar to'lagan naqd pulni</b> sanang va summani yozing "
                   "(masalan: 3 450 000).\n"
                   f"💳 Xarajat pulini (sizda {S.fmt_som(pb or 0)}) <b>qo'shmang</b> — u alohida konvertda turadi.\n"
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
    else:
        give = min(counted, exp)        # ortiqcha (sababi noma'lum) pul avtomatik topshirilmaydi; kam bo'lsa — borini
        hand = IM(inline_keyboard=[[IB(text=f"💼 Rahbarga topshirish — {S.fmt_som(give)}",
                                       callback_data=f"hoq:{give}")]]) if give > 0 else None
        await m.answer(("✅ Kassa mos. Rahmat!" if status == "mos" else "⚠️ Farq bor. Rahbarga xabar berildi.")
                       + ("\n\nPulni konvertga solib rahbarga bering va pastdagi tugmani bosing:" if hand else ""),
                       reply_markup=hand)
    sign = "+" if diff > 0 else ""
    text = (f"🔐 <b>Kassa yopildi · {t:%d.%m}</b> ({name})\nBo'lishi kerak: {S.fmt_som(exp)}\nSanadi: {S.fmt_som(counted)}\n"
            + ("Boshlang'ich qoldiq sifatida qayd etildi." if status == "boshlang" else
               "✅ Mos" if status == "mos" else f"⚠️ Farq: <b>{sign}{S.fmt_som(diff)}</b>")
            )
    kb = None
    if status == "farq":
        kb = IM(inline_keyboard=[[IB(text="−2 ball moliyaga", callback_data=f"cf:{cid}:pen")],
                                 [IB(text="Sabab aniqlandi — qoldiqni tuzatish", callback_data=f"cf:{cid}:fix")],
                                 [IB(text="🔄 Qayta sanashga ruxsat", callback_data=f"cf:{cid}:re")],
                                 [IB(text="📋 Nega? Tafsilot", callback_data="kv:det")]])
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
    async with Session() as s:
        exp = await expected_cash(s)
    await state.set_state(Ho.amount)
    await m.answer("💼 <b>Kassani topshirish</b>\n"
                   + (f"Sizda o'quvchilar puli: <b>{S.fmt_som(exp)}</b> bo'lishi kerak.\n" if exp is not None else "")
                   + "Qancha topshiryapsiz? Summani yozing:",
                   reply_markup=IM(inline_keyboard=[[IB(text=f"Hammasini — {S.fmt_som(exp)}", callback_data=f"hoall:{exp}")]])
                   if exp and exp > 0 else None)


_HOQ: dict[int, float] = {}


@router.callback_query(F.data.startswith("hoq:"))
async def ho_quick(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Kun yakuni: kassani yopgan zahoti sanalgan pulni bitta tugma bilan rahbarga topshirish."""
    import time
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
    if not (who.staff and who.staff.current_role == "moliya"):
        await c.answer()
        return
    if time.time() - _HOQ.get(c.from_user.id, 0) < 60:
        await c.answer("Topshirish yuborilgan.", show_alert=True)
        return
    async with Session() as s:
        exp = await expected_cash(s)
    amt = min(int(c.data.split(":")[1]), exp or 0)
    if amt <= 0:
        await c.answer("Topshiriladigan pul yo'q.", show_alert=True)
        return
    _HOQ[c.from_user.id] = time.time()
    await state.clear()
    await state.update_data(amount=amt)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    await _ho_save(c.message, state, bot, c.from_user.id, "rahbar", None)


@router.callback_query(Ho.amount, F.data.startswith("hoall:"))
async def ho_all(c: CallbackQuery, state: FSMContext) -> None:
    amt = int(c.data.split(":")[1])
    await state.update_data(amount=amt)
    await c.answer()
    await c.message.answer(f"{S.fmt_som(amt)} — kimga?", reply_markup=IM(inline_keyboard=[[
        IB(text="👤 Rahbarga (muhrlangan konvertda)", callback_data="hot:rahbar")],
        [IB(text="🏦 Bankka", callback_data="hot:bank")]]))


@router.message(Ho.amount)
async def ho_amount(m: Message, state: FSMContext) -> None:
    amt = _amount(m.text)
    if not amt:
        await m.answer("Summani raqam bilan yozing: 2000000")
        return
    async with Session() as s:
        exp = await expected_cash(s)
    if exp is not None and amt > exp:
        await m.answer(f"⚠️ Sizda o'quvchilar puli {S.fmt_som(exp)} bo'lishi kerak — {S.fmt_som(amt)} ko'p.\n"
                       "Xarajat pulini topshirmang. Agar haqiqatan ortiqcha pul bo'lsa — rahbarga ayting: "
                       "«🧮 Qayta sanash» qiladi.")
        return
    await state.update_data(amount=amt)
    await m.answer(f"{S.fmt_som(amt)} — kimga?", reply_markup=IM(inline_keyboard=[[
        IB(text="👤 Rahbarga (muhrlangan konvertda)", callback_data="hot:rahbar")],
        [IB(text="🏦 Bankka", callback_data="hot:bank")]]))


@router.callback_query(Ho.amount, F.data.startswith("hot:"))
async def ho_to(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    to = c.data.split(":")[1]
    if not (await state.get_data()).get("amount"):
        await c.answer("Avval summani yozing.", show_alert=True)
        return
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
    kind, hid = c.data.split(":")
    async with Session() as s:
        h0 = await s.get(CashHandover, int(hid))
        if not h0 or h0.status != "kutilmoqda":
            await c.answer("Bu topshirish allaqachon hal qilingan.", show_alert=True)
            return
        amt0 = h0.amount
    if await need_pin(c, state, amt0):
        return
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
        from app.bot.vault import money_line
        line = await money_line(s)
    await c.message.edit_text(c.message.html_text + ("\n\n✅ Qabul qilindi" if kind == "hoy" else "\n\n❌ Mos emas")
                              + (f"\n{line}" if kind == "hoy" and line else ""))
    if mol and mol.tg_id:
        await send(bot, mol.tg_id, f"💼 {S.fmt_som(amt)} topshirish: " +
                   ("✅ rahbar qabul qildi." if kind == "hoy" else "❌ rahbar «mos emas» dedi — bog'laning."))


# ================= 💼 Kassa (rahbar) =================
@router.message(F.text == K.B_CASH)
async def cash_view(m: Message) -> None:
    """Rahbar: oddiy kassa ekrani — 3 ta raqam. Tafsilot alohida tugmada."""
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            return
    text, kb = await simple_view()
    await m.answer(text, reply_markup=kb)


async def simple_view() -> tuple[str, IM]:
    from app.bot import vault as V
    from app.bot.golive import is_test
    t = now().date()
    async with Session() as s:
        bi = await V.boss_month_info(s)
        pb = await V.petty_balance(s)
        cbal = await V.card_balance(s)
        exp = await expected_cash(s)
        test = await is_test(s)
        pend = (await s.execute(select(func.coalesce(func.sum(CashHandover.amount), 0)).where(
            CashHandover.status == "kutilmoqda"))).scalar()
        start = datetime.combine(t, datetime.min.time()) - timedelta(hours=5)
        today_in = (await s.execute(select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.method == "naqd", Payment.status == "tasdiq", Payment.created_at >= start, Payment.amount > 0))).scalar()
    lines = [f"💼 <b>Kassa · {t:%d.%m}</b>"]
    if test:
        lines.append("🧪 <i>Sinov rejimi — summalar haqiqiy emas</i>")
    lines.append("")
    if bi is None:  # endi bo'lmaydi (avtomatik boshlanadi), eski holat uchun
        lines += ["Kassa hali ochilmagan.", "",
                  "«🧮 Pulni sanab kiritish» ni bosing — bot 4 ta savol beradi:",
                  "1. Qo'lingizda qancha naqd bor?", "2. Markaz kartasida qancha bor?",
                  "3. Moliyada o'quvchilar to'lagan pul qancha?", "4. Moliyada xarajat uchun pul qancha?"]
    else:
        lines += [f"👤 <b>Qo'lingizda (naqd):</b> {S.fmt_som(bi['balance'])}",
                  f"🏦 <b>Kartada:</b> {S.fmt_som(cbal or 0)}",
                  f"💰 <b>Jami sizda:</b> {S.fmt_som(bi['balance'] + (cbal or 0))}", "",
                  f"📥 <b>Moliyada — o'quvchilar puli:</b> {S.fmt_som(exp or 0)}"
                  + ("  <i>(sizga topshiriladi)</i>" if exp else ""),
                  f"💳 <b>Moliyada — xarajat puli:</b> {S.fmt_som(pb)}", "",
                  f"Bugun o'quvchilardan naqd: {S.fmt_som(int(today_in))}"]
        if pend:
            lines.append(f"⏳ Sizga topshirilgan, tasdiqlanmagan: <b>{S.fmt_som(int(pend))}</b>")
        lines += ["", "<i>Oylik, avans, xarajat — naqd bersangiz qo'lingizdagidan, kartadan bersangiz kartadan ayiriladi. "
                  "Moliya xarajati — xarajat pulidan. Yetmasa, bot to'xtatadi.</i>"]
    kb = V.kassa_kb(bi is not None)
    if test:
        kb.inline_keyboard.append([IB(text="🚀 Haqiqiy ishni boshlash", callback_data="gl")])
    return "\n".join(lines), kb


@router.callback_query(F.data == "kv:det")
async def cash_detail(c: CallbackQuery) -> None:
    """📋 Tafsilot: har bir raqam qanday hisoblangani."""
    from app.bot import vault as V
    t = now().date()
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            await c.answer()
            return
        br = await cash_breakdown(s)
        bi = await V.boss_month_info(s)
        pb = await V.petty_balance(s)
        closes = (await s.execute(select(CashClose).order_by(CashClose.date.desc(), CashClose.id.desc())
                                  .limit(5))).scalars().all()
        social = (await s.execute(select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.method == "ijtimoiy", Payment.month == f"{t:%Y-%m}"))).scalar()
    lines = ["📋 <b>Tafsilot</b>", ""]
    ci = None
    async with Session() as s:
        ci = await V.card_info(s)
    if bi:
        lines += ["👤 <b>Qo'lingizda (naqd)</b>", V.boss_text(bi), ""]
    if ci:
        lines += ["🏦 <b>Kartada</b>", f"Boshida/tuzatish: {S.fmt_som(ci['base'])}",
                  f"+ Karta/o'tkazma to'lovlar: {S.fmt_som(ci['inc'])}"]
        if ci["bank"]:
            lines.append(f"+ Moliya bankka topshirgan: {S.fmt_som(ci['bank'])}")
        if ci["paid"]:
            lines.append(f"− Kartadan oylik/avans: {S.fmt_som(ci['paid'])}")
        if ci["spent"]:
            lines.append(f"− Bank orqali xarajatlar: {S.fmt_som(ci['spent'])}")
        if ci["to_funds"]:
            lines.append(f"− Fondlarga taqsimlangan: {S.fmt_som(ci['to_funds'])}")
        lines += [f"= <b>Kartada: {S.fmt_som(ci['balance'])}</b>", ""]
        lines += [f"💳 <b>Xarajat puli:</b> {S.fmt_som(pb)}", ""]
    lines += ["📥 <b>Moliyadagi o'quvchilar puli</b>", breakdown_text(br) if br else "—", ""]
    if social:
        lines.append(f"Ijtimoiy to'lovlar (kassaga kirmaydi) shu oy: {S.fmt_som(int(social))}")
    lines += ["", "<b>Moliya kassani yopishlari</b>"]
    for cl in closes:
        mark = {"mos": "✅", "farq": "⚠️", "boshlang": "▫️"}[cl.status]
        lines.append(f"{mark} {cl.date:%d.%m}: sanadi {S.fmt_som(cl.counted)}" +
                     (f" (farq {'+' if cl.diff > 0 else ''}{S.fmt_som(cl.diff)})" if cl.diff else ""))
    if not closes:
        lines.append("—")
    await c.answer()
    await c.message.answer("\n".join(lines)[:3900], reply_markup=IM(inline_keyboard=[
        [IB(text="📋 Sizdagi pul harakatlari", callback_data="kv:list")],
        [IB(text="📋 O'quvchilar naqd to'lovlari", callback_data="cnl")],
        [IB(text="✏️ Raqam haqiqiy pulga mos emas — tuzatish", callback_data="kv:open")]]))


@router.callback_query(F.data == "cnl")
async def cash_list(c: CallbackQuery) -> None:
    """Kassa ochilgan kundan beri kiritilgan barcha naqd to'lovlar: vaqti, kim kiritgan, o'quvchi, summa, hisobga kirdimi."""
    from app.db.models import Student
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role in ("rahbar", "moliya")):
            await c.answer()
            return
        op = await _opening(s)
        if not op:
            await c.answer("Boshlang'ich qoldiq hali yo'q", show_alert=True)
            return
        since = datetime.fromisoformat(op["at"])
        open_day = date.fromisoformat(op["day"]) if op.get("day") else (since + timedelta(hours=5)).date()
        start = datetime.combine(open_day, datetime.min.time()) - timedelta(hours=5)
        rows = (await s.execute(
            select(Payment, Student.full_name, Staff.full_name)
            .join(Enrollment, Enrollment.id == Payment.enrollment_id).join(Student, Student.id == Enrollment.student_id)
            .outerjoin(Staff, Staff.id == Payment.entered_by)
            .where(Payment.method == "naqd", Payment.status == "tasdiq", Payment.created_at >= min(start, since),
                   func.coalesce(Payment.void_reason, "") != BOSS_REFUND)
            .order_by(Payment.created_at))).all()
    lines = [f"📋 <b>Naqd to'lovlar</b> ({open_day:%d.%m} dan beri)",
             f"Boshlang'ich qoldiq sanalgan vaqt: {since + timedelta(hours=5):%d.%m %H:%M}", ""]
    total = 0
    for p, st, by in rows:
        t = p.created_at + timedelta(hours=5)
        if p.created_at < since:
            mark = "⏪"   # qoldiq sanalishidan oldin — qoldiq ichida
        elif p.paid_date < open_day:
            mark = "📅"   # orqaga sana bilan — kassaga qo'shilmaydi
        else:
            mark = "✅"
            total += p.amount
        lines.append(f"{mark} {t:%d.%m %H:%M} · {st} · <b>{S.fmt_som(p.amount)}</b>"
                     + (f" · sana {p.paid_date:%d.%m}" if p.paid_date != t.date() else "") + (f" · {by}" if by else ""))
    if not rows:
        lines.append("—")
    lines += ["", f"✅ Kassaga qo'shilgan: <b>{S.fmt_som(total)}</b>",
              "⏪ — qoldiq sanalishidan oldin kiritilgan (qoldiq ichida)",
              "📅 — orqaga sana bilan kiritilgan (kassaga qo'shilmaydi)"]
    text = "\n".join(lines)
    for i in range(0, len(text), 3900):
        await c.message.answer(text[i:i + 3900])
    await c.answer()


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
DZ_SEL: dict[tuple[int, int], set[int]] = {}


async def _present(s, lid: int) -> list[tuple[Attendance, str]]:
    rows = (await s.execute(select(Attendance, Student.full_name).join(Enrollment, Enrollment.id == Attendance.enrollment_id)
                            .join(Student, Student.id == Enrollment.student_id)
                            .where(Attendance.lesson_id == lid, Attendance.mark != "y")
                            .order_by(Student.full_name))).all()
    return [(a, n) for a, n in rows]


@router.callback_query(F.data.startswith("dz:"))
async def lesson_end(c: CallbackQuery) -> None:
    lid = int(c.data.split(":")[1])
    async with Session() as s:
        les = await s.get(Lesson, lid)
        rows = await _present(s, lid)
    left = [(a, n) for a, n in rows if not a.left_at]
    if not rows:
        await c.answer("Avval davomatni oling.", show_alert=True)
        return
    if not left:
        await c.answer(f"Hamma o'quvchi uchun xabar yuborilgan ({les.ended_at:%H:%M})." if les.ended_at
                       else "Hamma uchun xabar yuborilgan.", show_alert=True)
        return
    done = len(rows) - len(left)
    await c.answer()
    await c.message.answer("🔔 Dars tugadi — kimlar chiqdi?" + (f"\n({done} ta o'quvchi uchun avval yuborilgan)" if done else ""),
                           reply_markup=IM(inline_keyboard=[
                               [IB(text=f"🔔 Hammasi chiqdi ({len(left)} ta)", callback_data=f"dza:{lid}")],
                               [IB(text="✅ Tanlab belgilash (bir qismi chiqdi)", callback_data=f"dzp:{lid}")]]))


def _dz_kb(lid: int, left, sel: set[int]) -> IM:
    kb = [[IB(text=f"{'✅' if a.id in sel else '▫️'} {n}", callback_data=f"dzt:{lid}:{a.id}")] for a, n in left]
    kb.append([IB(text=f"🔔 Tanlanganlarga xabar ({len(sel)} ta)", callback_data=f"dzk:{lid}")])
    return IM(inline_keyboard=kb)


@router.callback_query(F.data.startswith("dzp:"))
async def lesson_end_pick(c: CallbackQuery) -> None:
    lid = int(c.data.split(":")[1])
    async with Session() as s:
        left = [(a, n) for a, n in await _present(s, lid) if not a.left_at]
    DZ_SEL[(c.from_user.id, lid)] = set()
    await c.answer()
    await c.message.edit_text("Darsdan chiqqan o'quvchilarni belgilang:", reply_markup=_dz_kb(lid, left, set()))


@router.callback_query(F.data.startswith("dzt:"))
async def lesson_end_toggle(c: CallbackQuery) -> None:
    _, lid, aid = c.data.split(":")
    key = (c.from_user.id, int(lid))
    if key not in DZ_SEL:
        await c.answer("Qaytadan oching.", show_alert=True)
        return
    DZ_SEL[key] ^= {int(aid)}
    async with Session() as s:
        left = [(a, n) for a, n in await _present(s, int(lid)) if not a.left_at]
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=_dz_kb(int(lid), left, DZ_SEL[key]))


@router.callback_query(F.data.startswith(("dza:", "dzk:")))
async def lesson_end_send(c: CallbackQuery, bot: Bot) -> None:
    kind, lid = c.data.split(":")
    lid = int(lid)
    n = now()
    sel = DZ_SEL.pop((c.from_user.id, lid), set()) if kind == "dzk" else None
    if kind == "dzk" and not sel:
        await c.answer("Kamida bitta o'quvchini belgilang.", show_alert=True)
        return
    async with Session() as s:
        les = await s.get(Lesson, lid)
        g = await s.get(Group, les.group_id)
        rows = await _present(s, lid)
        msgs = []
        for a, _ in rows:
            if a.left_at or (sel is not None and a.id not in sel):
                continue
            a.left_at = n.replace(tzinfo=None)
            e = await s.get(Enrollment, a.enrollment_id, options=[selectinload(Enrollment.student)])
            for t in await S.parent_tg_ids(s, e.student_id):
                msgs.append((t, f"🔔 {e.student.full_name}: «{g.name}» darsi {n:%H:%M} da tugadi, uyga chiqdi."))
        rest = sum(1 for a, _ in rows if not a.left_at)
        if not rest and not les.ended_at:            # hamma chiqdi — dars yakunlandi
            les.ended_at = n.replace(tzinfo=None)
        await s.commit()
        early = (not rest) and g.end_time and (datetime.combine(n.date(), g.end_time, TZ) - n) > timedelta(minutes=EARLY_MIN)
    await c.answer("Ota-onalarga xabar yuborildi")
    await c.message.edit_text(f"🔔 {n:%H:%M}: {len(msgs)} ta ota-onaga «uyga chiqdi» xabari yuborildi."
                              + (f"\nHali darsda: {rest} ta o'quvchi — chiqqanda yana «🔔 Dars tugadi» ni bosing." if rest else
                                 "\nDars yakunlandi ✅")
                              + (f"\n⚠️ Jadvaldagidan {EARLY_MIN} daqiqadan ko'proq oldin tugadi — rahbar hisobotida "
                                 "ko'rinadi." if early else ""),
                              reply_markup=IM(inline_keyboard=[[IB(text="🔔 Qolganlar ham chiqdi", callback_data=f"dz:{lid}")]])
                              if rest else None)
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
