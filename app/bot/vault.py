"""Uchta kassa:
1) Qabul kassasi (moliyada) — o'quvchilar naqd to'lovi va naqd sotuvlar. Undan faqat rahbarga/bankka topshiriladi.
2) Kichik kassa (moliyada) — faqat rahbar bergan pul. Moliyaning barcha xarajatlari shundan.
3) Rahbar kassasi — qabul qilingan topshirishlar. Oylik, avans, rahbar xarajatlari, kichik kassani to'ldirish shundan.
Pul yetmasa — amal bajarilmaydi, "kassada pul yetarli emas" deb xabar beriladi."""
from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import func, select

from app.bot.notify import send
from app.db.models import CashHandover, Expense, KassaMove, Payout, Setting, Staff
from app.db.session import Session
from app import services as S

router = Router()
START_KEY = "kassa_v2"
SHIFT = timedelta(hours=5)
PENDING: dict[int, dict] = {}      # tasdiqlanishi kutilayotgan amal (PIN so'ralganda holat tozalanadi — shu yerda saqlanadi)


def _amount(text: str | None) -> int | None:
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    return int(digits) if digits else None


async def start_at(s) -> datetime | None:
    """Kassalar hisobi boshlangan payt (bazadagi UTC). Hech narsa so'ralmaydi: birinchi murojaatda
    hamma kassa 0 dan avtomatik boshlanadi — keyin faqat botga kiritilgan pul harakatlari hisoblanadi."""
    row = await s.get(Setting, START_KEY)
    if row:
        return datetime.fromisoformat(row.value["at"])
    return await ensure_started()


async def ensure_started() -> datetime:
    """Kassalarni 0 dan boshlaydi (alohida sessiyada — chaqirgan joy commit qilmasa ham saqlanadi)."""
    from app.bot.cash import _set_opening
    async with Session() as s2:
        row = await s2.get(Setting, START_KEY)
        if row:
            return datetime.fromisoformat(row.value["at"])
        db_now = (await s2.execute(select(func.now()))).scalar()
        if isinstance(db_now, str):
            db_now = datetime.fromisoformat(db_now)
        at = db_now.replace(tzinfo=None, microsecond=0)
        s2.add(Setting(key=START_KEY, value={"at": at.isoformat(), "auto": True}))
        if not await s2.get(Setting, "cash_opening"):
            await _set_opening(s2, 0)
        await s2.commit()
        return at


async def _sum(s, q) -> int:
    return int((await s.execute(q)).scalar() or 0)


async def boss_info(s, until: datetime | None = None, since: datetime | None = None) -> dict | None:
    """Rahbar kassasi. until (UTC) — shu paytgacha bo'lgan holat (oy yakuni taqsimoti uchun).
    since (UTC) — faqat shu paytdan keyingi harakatlar; "opening" — o'sha paytdagi qoldiq."""
    st0 = await start_at(s)
    if st0 is None:
        return None
    opening = 0
    if since:
        opening = (await boss_info(s, until=since))["balance"]
    st = max(st0, since) if since else st0

    def upto(col):
        lo = col >= since if since else True
        return (col < until) & lo if until and since else (col < until if until else lo)

    async def moves(*kinds):
        q = select(func.coalesce(func.sum(KassaMove.amount), 0)).where(KassaMove.kassa == "rahbar", upto(KassaMove.created_at))
        if kinds:
            q = q.where(KassaMove.kind.in_(kinds))
        return await _sum(s, q)

    total = await moves()
    to_petty = -await moves("kichikka")
    to_funds = -await moves("fond")
    taken = -await moves("egasi", "chiqim", "bankka")
    other = total + to_petty + to_funds + taken          # ochilish, tuzatish, kirim
    hand = await _sum(s, select(func.coalesce(func.sum(CashHandover.amount), 0)).where(
        CashHandover.to_kind == "rahbar", CashHandover.status == "tasdiq", CashHandover.created_at >= st,
        upto(CashHandover.created_at)))
    paid = await _sum(s, select(func.coalesce(func.sum(Payout.amount), 0)).where(
        Payout.method == "naqd", Payout.status.in_(("berildi", "olindi")), Payout.given_at >= st + SHIFT,
        (Payout.given_at < until + SHIFT) if until else True))
    spent = await _sum(s, select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.source == "asosiy", Expense.method == "naqd", Expense.status == "tasdiq", Expense.created_at >= st,
        upto(Expense.created_at)))
    return {"start": st + SHIFT, "since": since + SHIFT if since else None, "opening": opening, "base": other,
            "hand": hand, "paid": paid, "spent": spent, "to_petty": to_petty, "to_funds": to_funds, "taken": taken,
            "balance": opening + other + hand - paid - spent - to_petty - to_funds - taken}


async def boss_month_info(s) -> dict | None:
    """Kassa: oxirgi oy yakuni taqsimotidan keyingi harakatlar (oy boshidan)."""
    last = (await s.execute(select(func.max(KassaMove.created_at)).where(
        KassaMove.kassa == "rahbar", KassaMove.kind == "fond"))).scalar()
    return await boss_info(s, since=last + timedelta(microseconds=1) if last else None)


async def card_info(s, until: datetime | None = None) -> dict | None:
    """🏦 Markaz kartasi (bank): karta/o'tkazma to'lovlar + bankka topshirilgan naqd − karta orqali oylik/avans
    − bank orqali xarajatlar − fondlarga taqsimlangan."""
    st = await start_at(s)
    if st is None:
        return None

    def upto(col):
        return col < until if until else True

    from app.db.models import Payment, ServiceSale
    base = await _sum(s, select(func.coalesce(func.sum(KassaMove.amount), 0)).where(
        KassaMove.kassa == "karta", KassaMove.kind != "fond", upto(KassaMove.created_at)))
    to_funds = -await _sum(s, select(func.coalesce(func.sum(KassaMove.amount), 0)).where(
        KassaMove.kassa == "karta", KassaMove.kind == "fond", upto(KassaMove.created_at)))
    inc = await _sum(s, select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.method.in_(("karta", "kochirish")), Payment.status == "tasdiq", Payment.created_at >= st,
        upto(Payment.created_at)))
    inc += await _sum(s, select(func.coalesce(func.sum(ServiceSale.amount), 0)).where(
        ServiceSale.method == "karta", ServiceSale.status == "tasdiq", ServiceSale.created_at >= st,
        upto(ServiceSale.created_at)))
    bank = await _sum(s, select(func.coalesce(func.sum(CashHandover.amount), 0)).where(
        CashHandover.to_kind == "bank", CashHandover.status == "tasdiq", CashHandover.created_at >= st,
        upto(CashHandover.created_at)))
    paid = await _sum(s, select(func.coalesce(func.sum(Payout.amount), 0)).where(
        Payout.method == "karta", Payout.status.in_(("berildi", "olindi")), Payout.given_at >= st + SHIFT,
        (Payout.given_at < until + SHIFT) if until else True))
    spent = await _sum(s, select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.method == "bank", Expense.status == "tasdiq", Expense.created_at >= st, upto(Expense.created_at)))
    return {"base": base, "inc": inc, "bank": bank, "paid": paid, "spent": spent, "to_funds": to_funds,
            "balance": base + inc + bank - paid - spent - to_funds}


async def card_balance(s) -> int | None:
    c = await card_info(s)
    return c["balance"] if c else None


async def boss_balance(s) -> int | None:
    b = await boss_info(s)
    return b["balance"] if b else None


async def petty_balance(s) -> int | None:
    st = await start_at(s)
    if st is None:
        return None
    given = await _sum(s, select(func.coalesce(func.sum(KassaMove.amount), 0)).where(KassaMove.kassa == "kichik"))
    spent = await _sum(s, select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.source == "kichik", Expense.method == "naqd", Expense.status == "tasdiq", Expense.created_at >= st))
    return given - spent


async def boss_can_pay(s, amount: int, method: str = "naqd") -> str | None:
    """None — pul yetadi. Aks holda rahbarga ko'rsatiladigan sabab. method: naqd | karta."""
    if method in ("karta", "bank"):
        cb = await card_balance(s)
        if cb is None:
            return "Avval kassani oching: 💼 Kassa → 🧮 Pulni sanab kiritish."
        if amount > cb:
            return f"Kartada pul yetarli emas. Bor: {S.fmt_som(cb)}, kerak: {S.fmt_som(amount)}."
        return None
    bal = await boss_balance(s)
    if bal is None:
        return "Avval kassani oching: 💼 Kassa → 🧮 Pulni sanab kiritish."
    if amount > bal:
        return f"Sizda pul yetarli emas. Bor: {S.fmt_som(bal)}, kerak: {S.fmt_som(amount)}."
    return None


def boss_text(b: dict) -> str:
    lines = []
    if b.get("since"):
        lines.append(f"Oy boshida (taqsimotdan keyin, {b['since']:%d.%m}): {S.fmt_som(b['opening'])}")
        if b["base"]:
            lines.append(f"+ Kirim/tuzatish: {S.fmt_som(b['base'])}")
    else:
        lines.append(f"Boshida/tuzatish/kirim: {S.fmt_som(b['base'])}")
    lines.append(f"+ Moliyadan olgan pulingiz: {S.fmt_som(b['hand'])}")
    if b["paid"]:
        lines.append(f"− Oylik va avanslar: {S.fmt_som(b['paid'])}")
    if b["spent"]:
        lines.append(f"− Xarajatlar (sizning): {S.fmt_som(b['spent'])}")
    if b["to_petty"]:
        lines.append(f"− Moliyaga xarajat puli berilgan: {S.fmt_som(b['to_petty'])}")
    if b["to_funds"]:
        lines.append(f"− Oy yakunida fondlarga taqsimlangan: {S.fmt_som(b['to_funds'])}")
    if b["taken"]:
        lines.append(f"− O'zingiz olgan / kartaga qo'yilgan / boshqa chiqim: {S.fmt_som(b['taken'])}")
    lines.append(f"= <b>Qoldiq: {S.fmt_som(b['balance'])}</b>")
    return "\n".join(lines)


def kassa_kb(opened: bool = True) -> IM:
    return IM(inline_keyboard=[[IB(text="💳 Moliyaga xarajat puli berish", callback_data="kv:petty")],
                               [IB(text="➕ Kirim", callback_data="kv:in"), IB(text="➖ Chiqim", callback_data="kv:out")],
                               [IB(text="🏦 Fondlar", callback_data="fd"), IB(text="📋 Tafsilot", callback_data="kv:det")]])


async def _boss(tg: int):
    async with Session() as s:
        who = await S.who_by_tg(s, tg)
    return who.staff if who.staff and who.staff.current_role == "rahbar" else None


# ---------- ochish / kirim / chiqim / tuzatish / kichik kassaga berish ----------
class Kv(StatesGroup):
    boss = State()
    card = State()
    recv = State()
    petty = State()
    amount = State()
    note = State()


ASK = {"open": "✏️ <b>Tuzatish</b> — faqat bot ko'rsatgan raqam haqiqiy puldan farq qilsa (4 ta savol)\n\n"
               "1/4. 👤 <b>Qo'lingizda</b> hozir qancha naqd pul bor?\n<i>Sanab yozing. Yo'q bo'lsa — 0.</i>",
       "petty": "💳 Moliyaga xarajat uchun qancha pul berasiz? Summani yozing:",
       "in": "➕ Sizga qancha pul kirdi? Summani yozing (masalan, o'zingiz qo'shgan pul):",
       "out": "➖ Sizdan qancha pul chiqdi? Summani yozing:",
       "pfix": "✏️ Moliyadagi <b>xarajat pulida</b> (faqat xarajat puli, o'quvchilar to'lovi emas) hozir aniq qancha pul bor? "
               "Summani yozing — bot shunga tenglaydi (yo'q bo'lsa 0):",
       "fix": "✏️ Qo'lingizdagi pulni sanadingizmi? Hozir qo'lingizdagi <b>aniq</b> summani yozing — bot qoldiqni shunga tenglaydi:"}


@router.callback_query(F.data.startswith("kv:") & F.data.in_({"kv:open", "kv:petty", "kv:in", "kv:out", "kv:fix", "kv:pfix"}))
async def kv_ask(c: CallbackQuery, state: FSMContext) -> None:
    if not await _boss(c.from_user.id):
        await c.answer("Faqat rahbar.", show_alert=True)
        return
    act = c.data.split(":")[1]
    async with Session() as s:
        opened = await start_at(s) is not None
        bal, pb = await boss_balance(s), await petty_balance(s)
    if act != "open" and not opened:
        await c.answer("Avval «🧮 Pulni sanab kiritish».", show_alert=True)
        return
    await state.clear()
    await state.set_state(Kv.boss if act == "open" else Kv.amount)
    await state.update_data(act=act)
    head = "" if act == "open" else f"Sizda: {S.fmt_som(bal)} · Xarajat puli: {S.fmt_som(pb)}\n\n"
    await c.answer()
    await c.message.answer(head + ASK[act])


@router.message(Kv.boss)
async def kv_open_boss(m: Message, state: FSMContext) -> None:
    a = _amount(m.text)
    if a is None:
        await m.answer("Summani raqam bilan yozing (yo'q bo'lsa 0).")
        return
    await state.update_data(boss=a)
    await state.set_state(Kv.card)
    await m.answer("2/4. 🏦 <b>Markaz kartasida</b> (bank hisobida) hozir qancha pul bor?\n"
                   "<i>Bank ilovasidan qarab yozing. Yo'q bo'lsa — 0.</i>")


@router.message(Kv.card)
async def kv_open_card(m: Message, state: FSMContext) -> None:
    a = _amount(m.text)
    if a is None:
        await m.answer("Summani raqam bilan yozing (yo'q bo'lsa 0).")
        return
    await state.update_data(card=a)
    await state.set_state(Kv.recv)
    await m.answer("3/4. 📥 <b>Moliyada o'quvchilar to'lagan pul</b> (sizga hali topshirilmagan) qancha?\n"
                   "<i>Moliya sanab aytsin. Yo'q bo'lsa — 0.</i>")


@router.message(Kv.recv)
async def kv_open_recv(m: Message, state: FSMContext) -> None:
    a = _amount(m.text)
    if a is None:
        await m.answer("Summani raqam bilan yozing (yo'q bo'lsa 0).")
        return
    await state.update_data(recv=a)
    await state.set_state(Kv.petty)
    await m.answer("4/4. 💳 <b>Moliyada xarajat uchun alohida pul</b> qancha? (o'quvchilar pulidan tashqari)\n"
                   "<i>Alohida pul yo'q bo'lsa — 0.</i>")


@router.message(Kv.petty)
async def kv_open_petty(m: Message, state: FSMContext) -> None:
    a = _amount(m.text)
    if a is None:
        await m.answer("Summani raqam bilan yozing (yo'q bo'lsa 0).")
        return
    d = await state.get_data()
    PENDING[m.from_user.id] = {**d, "petty": a, "act": "open"}
    await m.answer(f"<b>Tekshiring:</b>\n👤 Qo'lingizda (naqd): <b>{S.fmt_som(d['boss'])}</b>\n"
                   f"🏦 Kartada: <b>{S.fmt_som(d.get('card', 0))}</b>\n"
                   f"📥 Moliyada — o'quvchilar puli: <b>{S.fmt_som(d['recv'])}</b>\n"
                   f"💳 Moliyada — xarajat puli: <b>{S.fmt_som(a)}</b>\n\nBot shu paytdan boshlab hisoblaydi.",
                   reply_markup=IM(inline_keyboard=[[IB(text="✅ To'g'ri", callback_data="kvok"),
                                                     IB(text="✏️ Qaytadan", callback_data="kv:open2")]]))


@router.callback_query(F.data == "kv:open2")
async def kv_open_again(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Kv.boss)
    await state.update_data(act="open")
    await c.message.edit_text(ASK["open"])


@router.message(Kv.amount)
async def kv_amount(m: Message, state: FSMContext) -> None:
    a = _amount(m.text)
    d = await state.get_data()
    if a is None or (a == 0 and d["act"] not in ("fix", "pfix")):
        await m.answer("Summani raqam bilan yozing: 500000")
        return
    await state.update_data(amount=a)
    if d["act"] in ("in", "out", "fix", "pfix"):
        await state.set_state(Kv.note)
        hint = {"in": "Qayerdan? (masalan: o'zim qo'shdim)",
                "out": "Nima uchun? Tanlang yoki yozing:",
                "fix": "Farq sababi? (masalan: avvalgi xarajat kiritilmagan edi)",
                "pfix": "Sababi? (masalan: ochishda noto'g'ri kiritilgan)"}[d["act"]]
        kb = IM(inline_keyboard=[[IB(text="👤 O'zim oldim (ulushimdan oldindan)", callback_data="kvn:egasi")],
                                 [IB(text="🏦 Naqdni kartaga qo'ydim", callback_data="kvn:bank")]]) if d["act"] == "out" else None
        await m.answer(hint, reply_markup=kb)
        return
    await _confirm(m, state, m.from_user.id)


@router.callback_query(Kv.note, F.data.startswith("kvn:"))
async def kv_note_pick(c: CallbackQuery, state: FSMContext) -> None:
    k = c.data.split(":")[1]
    await state.update_data(note={"egasi": "O'zim oldim (ulushimdan oldindan)", "bank": "Naqd kartaga qo'yildi"}[k],
                            kind="egasi" if k == "egasi" else "bankka")
    await c.answer()
    if k == "egasi":
        from app.bot import funds as FD
        d = await state.get_data()
        async with Session() as s:
            p = await FD.plan(s, FD.PR.current_month(FD.now()))
        if p:
            share = max(0, p["owner_total"] - p["draws"])
            await c.message.answer(
                "ℹ️ <b>Bu foyda emas, ulushingizdan oldindan olish (avans).</b>\n"
                "Foyda oy oxirida (1-sana) hisoblanadi. Shunda ulushingizdan shu summa ayiriladi.\n\n"
                f"Hozirgi holat bo'yicha bu oy taxminiy ulushingiz: <b>{S.fmt_som(share)}</b>"
                + (f" (allaqachon olgansiz: {S.fmt_som(p['draws'])})" if p["draws"] else "")
                + (f"\n⚠️ Olmoqchi bo'lgan {S.fmt_som(d['amount'])} taxminiy ulushdan ko'p — oy oxirida fondlar "
                   "kamroq to'ladi yoki oyliklarga yetmay qolishi mumkin." if d["amount"] > share else ""))
    await _confirm(c.message, state, c.from_user.id)


@router.message(Kv.note)
async def kv_note(m: Message, state: FSMContext) -> None:
    await state.update_data(note=(m.text or "").strip()[:300] or None)
    await _confirm(m, state, m.from_user.id)


async def _confirm(m: Message, state: FSMContext, uid: int) -> None:
    d = await state.get_data()
    PENDING[uid] = d
    a = d["amount"]
    text = {"petty": f"💳 Moliyaga xarajat puli <b>{S.fmt_som(a)}</b> berasizmi? (sizdagi puldan chiqadi)",
            "in": f"➕ Sizga <b>{S.fmt_som(a)}</b> kirim",
            "out": f"➖ Sizdan <b>{S.fmt_som(a)}</b> chiqim",
            "fix": f"✏️ Sizdagi pul <b>{S.fmt_som(a)}</b> deb tenglashtirilsinmi?",
            "pfix": f"✏️ Xarajat puli <b>{S.fmt_som(a)}</b> deb tenglashtirilsinmi?"}[d["act"]]
    if d.get("note"):
        text += f"\nIzoh: {d['note']}"
    from app.bot.pin import PIN_FROM
    await m.answer(text + ("\n(PIN so'raladi)" if a >= PIN_FROM else ""), reply_markup=IM(inline_keyboard=[[
        IB(text="✅ Tasdiqlash", callback_data="kvok"), IB(text="❌ Bekor", callback_data="kvno")]]))


@router.callback_query(F.data == "kvno")
async def kv_cancel(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    PENDING.pop(c.from_user.id, None)
    await c.answer("Bekor qilindi")
    await c.message.edit_text("Bekor qilindi.")


@router.callback_query(F.data == "kvok")
async def kv_save(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    from app.bot.pin import need_pin
    st = await _boss(c.from_user.id)
    if not st:
        await c.answer("Faqat rahbar.", show_alert=True)
        return
    d = PENDING.get(c.from_user.id) or {}
    if not d.get("act"):
        await c.answer("Ma'lumot eskirgan, qaytadan boshlang.", show_alert=True)
        return
    if await need_pin(c, state, None if d["act"] == "open" else d.get("amount")):
        return
    act = d["act"]
    notify = None
    async with Session() as s:
        if act == "open":
            from app.bot.cash import _set_opening
            if await start_at(s) is None:
                s.add(Setting(key=START_KEY, value={"at": datetime.utcnow().isoformat()}))
                s.add(KassaMove(kassa="rahbar", amount=d["boss"], kind="boshlang", note="Sanab kiritildi", by_id=st.id))
                s.add(KassaMove(kassa="kichik", amount=d["petty"], kind="boshlang", note="Sanab kiritildi", by_id=st.id))
                s.add(KassaMove(kassa="karta", amount=d.get("card", 0), kind="boshlang", note="Sanab kiritildi", by_id=st.id))
            else:                                  # qayta sanash — farqni tuzatish sifatida
                bal, pb = await boss_balance(s), await petty_balance(s)
                if d["boss"] != bal:
                    s.add(KassaMove(kassa="rahbar", amount=d["boss"] - bal, kind="tuzatish", note="Qayta sanaldi", by_id=st.id))
                cb = await card_balance(s)
                if d.get("card", 0) != cb:
                    s.add(KassaMove(kassa="karta", amount=d.get("card", 0) - cb, kind="tuzatish", note="Qayta sanaldi", by_id=st.id))
                if d["petty"] != pb:
                    s.add(KassaMove(kassa="kichik", amount=d["petty"] - pb, kind="tuzatish", note="Qayta sanaldi", by_id=st.id))
            await _set_opening(s, d["recv"])
            msg = (f"✅ Saqlandi.\n👤 Qo'lingizda: {S.fmt_som(d['boss'])}\n🏦 Kartada: {S.fmt_som(d.get('card', 0))}\n📥 Moliyada — o'quvchilar puli: {S.fmt_som(d['recv'])}\n"
                   f"💳 Moliyada — xarajat puli: {S.fmt_som(d['petty'])}")
            notify = (f"🧮 Rahbar kassani sanab kiritdi:\n📥 O'quvchilar puli: <b>{S.fmt_som(d['recv'])}</b>\n"
                      f"💳 Xarajat puli: <b>{S.fmt_som(d['petty'])}</b>\n"
                      "Ikkalasini alohida saqlang: xarajat faqat xarajat pulidan.")
        else:
            a = d["amount"]
            bal = await boss_balance(s)
            if act in ("petty", "out") and a > bal:
                await state.clear()
                PENDING.pop(c.from_user.id, None)
                await c.message.edit_text(f"❌ Sizda pul yetarli emas. Bor: {S.fmt_som(bal)}.")
                return
            if act == "petty":
                s.add(KassaMove(kassa="rahbar", amount=-a, kind="kichikka", note="Moliyaga xarajat puli", by_id=st.id))
                s.add(KassaMove(kassa="kichik", amount=a, kind="kirim", note="Rahbardan", by_id=st.id))
                await s.flush()
                pb = await petty_balance(s)
                msg = f"✅ Moliyaga xarajat puli {S.fmt_som(a)} berildi. Sizda qoldi: {S.fmt_som(bal - a)}"
                notify = f"💳 Rahbar xarajat uchun <b>{S.fmt_som(a)}</b> berdi. Xarajat pulingiz: {S.fmt_som(pb)}"
            elif act == "in":
                s.add(KassaMove(kassa="rahbar", amount=a, kind="kirim", note=d.get("note"), by_id=st.id))
                msg = f"✅ Kirim: {S.fmt_som(a)}. Sizda: {S.fmt_som(bal + a)}"
            elif act == "out":
                s.add(KassaMove(kassa="rahbar", amount=-a, kind=d.get("kind", "chiqim"), note=d.get("note"), by_id=st.id))
                if d.get("kind") == "bankka":          # naqd kartaga o'tdi — pul yo'qolmaydi, kartada ko'rinadi
                    s.add(KassaMove(kassa="karta", amount=a, kind="kirim", note="Naqddan", by_id=st.id))
                msg = f"✅ Chiqim: {S.fmt_som(a)}. Sizda: {S.fmt_som(bal - a)}"
            elif act == "pfix":
                pb = await petty_balance(s)
                s.add(KassaMove(kassa="kichik", amount=a - pb, kind="tuzatish", note=d.get("note"), by_id=st.id))
                msg = f"✅ Xarajat puli {S.fmt_som(a)} ga tenglashtirildi (farq {'+' if a >= pb else ''}{S.fmt_som(a - pb)})."
                notify = f"✏️ Rahbar xarajat pulini tuzatdi: endi <b>{S.fmt_som(a)}</b>."
            else:
                s.add(KassaMove(kassa="rahbar", amount=a - bal, kind="tuzatish", note=d.get("note"), by_id=st.id))
                msg = f"✅ Qoldiq {S.fmt_som(a)} ga tenglashtirildi (farq {'+' if a >= bal else ''}{S.fmt_som(a - bal)})."
        await S.audit(s, st, "kassa_" + act, **{k: v for k, v in d.items() if k in ("amount", "boss", "card", "recv", "petty", "note")})
        await s.commit()
        fin = [x.tg_id for x in await S.staff_with_role(s, "moliya") if x.tg_id] if notify else []
    await state.clear()
    PENDING.pop(c.from_user.id, None)
    await c.answer()
    await c.message.edit_text(msg)
    for t in fin:
        await send(bot, t, notify)


@router.callback_query(F.data == "kv:list")
async def kv_list(c: CallbackQuery) -> None:
    if not await _boss(c.from_user.id):
        await c.answer("Faqat rahbar.", show_alert=True)
        return
    async with Session() as s:
        st = await start_at(s)
        rows: list[tuple[datetime, str, int]] = []
        for x in (await s.execute(select(KassaMove).where(KassaMove.kassa == "rahbar"))).scalars():
            name = {"boshlang": "Ochilish", "tuzatish": "Tuzatish", "kirim": "Kirim", "chiqim": "Chiqim",
                    "egasi": "O'zim oldim (ulushdan oldindan)", "bankka": "Naqd kartaga", "kichikka": "Moliyaga xarajat puli", "fond": "Fondlarga taqsimot"}.get(x.kind, x.kind)
            rows.append((x.created_at, name + (f" · {x.note}" if x.note and x.kind not in ("kichikka", "boshlang") else ""), x.amount))
        for h in (await s.execute(select(CashHandover).where(
                CashHandover.to_kind == "rahbar", CashHandover.status == "tasdiq", CashHandover.created_at >= st))).scalars():
            rows.append((h.created_at, "Moliyadan qabul", h.amount))
        for p, nm in (await s.execute(select(Payout, Staff.full_name).join(Staff, Staff.id == Payout.staff_id).where(
                Payout.method == "naqd", Payout.status.in_(("berildi", "olindi")), Payout.given_at >= st + SHIFT))).all():
            rows.append((p.given_at - SHIFT, f"{'Oylik' if p.kind == 'oylik' else 'Avans'} · {nm}", -p.amount))
        for e in (await s.execute(select(Expense).where(Expense.source == "asosiy", Expense.method == "naqd",
                                                        Expense.status == "tasdiq", Expense.created_at >= st))).scalars():
            rows.append((e.created_at, f"Xarajat · {e.category}", -e.amount))
        bal = await boss_balance(s)
    rows.sort(key=lambda r: r[0])
    lines = ["📋 <b>Sizdagi pul harakatlari</b>", ""] + [
        f"{t + SHIFT:%d.%m %H:%M} · {n} · <b>{'+' if a > 0 else ''}{S.fmt_som(a)}</b>" for t, n, a in rows[-60:]]
    lines += ["", f"Qoldiq: <b>{S.fmt_som(bal)}</b>"]
    await c.answer()
    text = "\n".join(lines)
    for i in range(0, len(text), 3900):
        await c.message.answer(text[i:i + 3900])


# ---------- 💳 xarajat puli kamaysa — bitta tugma bilan to'ldirish ----------
LOW = 100_000
_low_sent: dict[str, bool] = {}


async def suggest_topup(s) -> int:
    """Oxirgi 30 kundagi moliya xarajatlari (yuz minggacha yaxlitlab), kamida 300 000."""
    since = datetime.utcnow() - timedelta(days=30)
    spent = await _sum(s, select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.source == "kichik", Expense.status == "tasdiq", Expense.created_at >= since))
    return max(300_000, -(-spent // 100_000) * 100_000)


def topup_kb(amount: int) -> IM:
    return IM(inline_keyboard=[[IB(text=f"💳 {S.fmt_som(amount)} berish", callback_data=f"kvq:{amount}")],
                               [IB(text="Boshqa summa", callback_data="kv:petty")]])


async def low_petty_alert(bot: Bot) -> None:
    """Moliyaning xarajat puli LOW dan kamaysa — rahbarga kuniga bir marta taklif."""
    async with Session() as s:
        pb = await petty_balance(s)
        if pb is None or pb >= LOW:
            return
        key = f"{datetime.utcnow() + SHIFT:%Y-%m-%d}"
        if _low_sent.get(key):
            return
        _low_sent[key] = True
        amt = await suggest_topup(s)
        bal = await boss_balance(s)
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    for b in bosses:
        await send(bot, b, f"💳 Moliyadagi xarajat puli kamaydi: <b>{S.fmt_som(pb)}</b>.\n"
                           f"Oxirgi oyda xarajat: ~{S.fmt_som(amt)}. Sizda: {S.fmt_som(bal)}.", reply_markup=topup_kb(amt))


@router.callback_query(F.data.startswith("kvq:"))
async def kv_quick_topup(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    from app.bot.pin import need_pin
    st = await _boss(c.from_user.id)
    if not st:
        await c.answer("Faqat rahbar.", show_alert=True)
        return
    a = int(c.data.split(":")[1])
    async with Session() as s:
        why = await boss_can_pay(s, a)
    if why:
        await c.answer(why, show_alert=True)
        return
    if await need_pin(c, state, a):
        return
    async with Session() as s:
        s.add(KassaMove(kassa="rahbar", amount=-a, kind="kichikka", note="Moliyaga xarajat puli", by_id=st.id))
        s.add(KassaMove(kassa="kichik", amount=a, kind="kirim", note="Rahbardan", by_id=st.id))
        await S.audit(s, st, "kassa_petty", amount=a)
        await s.commit()
        pb, bal = await petty_balance(s), await boss_balance(s)
        fin = [x.tg_id for x in await S.staff_with_role(s, "moliya") if x.tg_id]
    await c.answer()
    await c.message.edit_text(f"✅ Moliyaga xarajat puli {S.fmt_som(a)} berildi. Sizda qoldi: {S.fmt_som(bal)}")
    for t in fin:
        await send(bot, t, f"💳 Rahbar xarajat uchun <b>{S.fmt_som(a)}</b> berdi — pulni oling. Xarajat pulingiz: {S.fmt_som(pb)}")
