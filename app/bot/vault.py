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
    """Kassalar ochilgan payt (bazadagi UTC). None — rahbar hali kassalarni ochmagan."""
    row = await s.get(Setting, START_KEY)
    return datetime.fromisoformat(row.value["at"]) if row else None


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
    taken = -await moves("egasi", "chiqim")
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


async def boss_can_pay(s, amount: int) -> str | None:
    """None — pul yetadi. Aks holda rahbarga ko'rsatiladigan sabab."""
    bal = await boss_balance(s)
    if bal is None:
        return "Avval kassangizni oching: 💼 Kassa → 🔓 Kassalarni ochish."
    if amount > bal:
        return f"Kassangizda pul yetarli emas. Qoldiq: {S.fmt_som(bal)}, kerak: {S.fmt_som(amount)}."
    return None


def boss_text(b: dict) -> str:
    lines = []
    if b.get("since"):
        lines.append(f"Oy boshida (taqsimotdan keyin, {b['since']:%d.%m}): {S.fmt_som(b['opening'])}")
        if b["base"]:
            lines.append(f"+ Kirim/tuzatish: {S.fmt_som(b['base'])}")
    else:
        lines.append(f"Ochilish/tuzatish/kirim: {S.fmt_som(b['base'])}")
    lines.append(f"+ Moliyadan qabul qilingan: {S.fmt_som(b['hand'])}")
    if b["paid"]:
        lines.append(f"− Oylik va avanslar: {S.fmt_som(b['paid'])}")
    if b["spent"]:
        lines.append(f"− Xarajatlar (sizning): {S.fmt_som(b['spent'])}")
    if b["to_petty"]:
        lines.append(f"− Kichik kassaga berilgan: {S.fmt_som(b['to_petty'])}")
    if b["to_funds"]:
        lines.append(f"− Oy yakunida fondlarga taqsimlangan: {S.fmt_som(b['to_funds'])}")
    if b["taken"]:
        lines.append(f"− Egasi olgan / boshqa chiqim: {S.fmt_som(b['taken'])}")
    lines.append(f"= <b>Qoldiq: {S.fmt_som(b['balance'])}</b>")
    return "\n".join(lines)


def kassa_kb(opened: bool) -> IM:
    if not opened:
        return IM(inline_keyboard=[[IB(text="🔓 Kassalarni ochish", callback_data="kv:open")],
                                   [IB(text="🏦 Fondlar", callback_data="fd"),
                                    IB(text="📋 Naqd to'lovlar ro'yxati", callback_data="cnl")]])
    return IM(inline_keyboard=[[IB(text="💳 Kichik kassaga pul berish", callback_data="kv:petty")],
                               [IB(text="➕ Kassamga kirim", callback_data="kv:in"),
                                IB(text="➖ Kassamdan chiqim", callback_data="kv:out")],
                               [IB(text="✏️ Qoldiqni tuzatish (sanab ko'rdim)", callback_data="kv:fix")],
                               [IB(text="✏️ Kichik kassani tuzatish", callback_data="kv:pfix")],
                               [IB(text="🏦 Fondlar", callback_data="fd")],
                               [IB(text="📋 Kassam harakatlari", callback_data="kv:list"),
                                IB(text="📋 Naqd to'lovlar", callback_data="cnl")]])


async def _boss(tg: int):
    async with Session() as s:
        who = await S.who_by_tg(s, tg)
    return who.staff if who.staff and who.staff.current_role == "rahbar" else None


# ---------- ochish / kirim / chiqim / tuzatish / kichik kassaga berish ----------
class Kv(StatesGroup):
    boss = State()
    petty = State()
    amount = State()
    note = State()


ASK = {"open": "🔓 <b>Kassalarni ochish</b>\n1/2. Hozir <b>sizning qo'lingizda</b> (rahbar kassasida) qancha naqd pul bor?\n"
               "<i>Sanab, summani yozing. Yo'q bo'lsa — 0.</i>",
       "petty": "💳 Kichik kassaga qancha pul berasiz? Summani yozing:",
       "in": "➕ Kassangizga qancha pul kirdi? Summani yozing (masalan, o'zingiz qo'shgan pul):",
       "out": "➖ Kassangizdan qancha pul chiqdi? Summani yozing:",
       "pfix": "✏️ Moliyadagi <b>kichik kassada</b> (faqat xarajat puli, o'quvchilar to'lovi emas) hozir aniq qancha pul bor? "
               "Summani yozing — bot shunga tenglaydi (yo'q bo'lsa 0):",
       "fix": "✏️ Kassangizni sanadingizmi? Hozir qo'lingizdagi <b>aniq</b> summani yozing — bot qoldiqni shunga tenglaydi:"}


@router.callback_query(F.data.startswith("kv:") & F.data.in_({"kv:open", "kv:petty", "kv:in", "kv:out", "kv:fix", "kv:pfix"}))
async def kv_ask(c: CallbackQuery, state: FSMContext) -> None:
    if not await _boss(c.from_user.id):
        await c.answer("Faqat rahbar.", show_alert=True)
        return
    act = c.data.split(":")[1]
    async with Session() as s:
        opened = await start_at(s) is not None
        bal, pb = await boss_balance(s), await petty_balance(s)
    if act == "open" and opened:
        await c.answer("Kassalar allaqachon ochilgan.", show_alert=True)
        return
    if act != "open" and not opened:
        await c.answer("Avval «🔓 Kassalarni ochish».", show_alert=True)
        return
    await state.clear()
    await state.set_state(Kv.boss if act == "open" else Kv.amount)
    await state.update_data(act=act)
    head = "" if act == "open" else f"Kassangizda: {S.fmt_som(bal)} · Kichik kassa: {S.fmt_som(pb)}\n\n"
    await c.answer()
    await c.message.answer(head + ASK[act])


@router.message(Kv.boss)
async def kv_open_boss(m: Message, state: FSMContext) -> None:
    a = _amount(m.text)
    if a is None:
        await m.answer("Summani raqam bilan yozing (yo'q bo'lsa 0).")
        return
    await state.update_data(boss=a)
    await state.set_state(Kv.petty)
    await m.answer("2/2. <b>Moliyadagi kichik kassada</b> (xarajatlar uchun) hozir qancha pul bor?\n"
                   "<i>Moliyadan so'rang. Yo'q bo'lsa — 0.</i>")


@router.message(Kv.petty)
async def kv_open_petty(m: Message, state: FSMContext) -> None:
    a = _amount(m.text)
    if a is None:
        await m.answer("Summani raqam bilan yozing (yo'q bo'lsa 0).")
        return
    d = await state.get_data()
    PENDING[m.from_user.id] = {**d, "petty": a}
    await m.answer(f"Tekshiring:\nRahbar kassasi: <b>{S.fmt_som(d['boss'])}</b>\nKichik kassa: <b>{S.fmt_som(a)}</b>\n\n"
                   "Shu paytdan boshlab: oylik, avans va xarajatlaringiz — faqat kassangizdan; moliya xarajatlari — "
                   "faqat kichik kassadan.", reply_markup=IM(inline_keyboard=[[
                       IB(text="✅ Ochish", callback_data="kvok"), IB(text="✏️ Qayta", callback_data="kv:open2")]]))


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
        kb = IM(inline_keyboard=[[IB(text="👤 Egasi oldi (foydadan)", callback_data="kvn:egasi")],
                                 [IB(text="🏦 Bankka/kartaga qo'ydim", callback_data="kvn:bank")]]) if d["act"] == "out" else None
        await m.answer(hint, reply_markup=kb)
        return
    await _confirm(m, state, m.from_user.id)


@router.callback_query(Kv.note, F.data.startswith("kvn:"))
async def kv_note_pick(c: CallbackQuery, state: FSMContext) -> None:
    k = c.data.split(":")[1]
    await state.update_data(note={"egasi": "Egasi oldi (foydadan)", "bank": "Bankka/kartaga qo'yildi"}[k],
                            kind="egasi" if k == "egasi" else "chiqim")
    await c.answer()
    await _confirm(c.message, state, c.from_user.id)


@router.message(Kv.note)
async def kv_note(m: Message, state: FSMContext) -> None:
    await state.update_data(note=(m.text or "").strip()[:300] or None)
    await _confirm(m, state, m.from_user.id)


async def _confirm(m: Message, state: FSMContext, uid: int) -> None:
    d = await state.get_data()
    PENDING[uid] = d
    a = d["amount"]
    text = {"petty": f"💳 Kichik kassaga <b>{S.fmt_som(a)}</b> berasizmi? (kassangizdan chiqadi)",
            "in": f"➕ Kassangizga <b>{S.fmt_som(a)}</b> kirim",
            "out": f"➖ Kassangizdan <b>{S.fmt_som(a)}</b> chiqim",
            "fix": f"✏️ Kassangiz qoldig'i <b>{S.fmt_som(a)}</b> deb tenglashtirilsinmi?",
            "pfix": f"✏️ Kichik kassa qoldig'i <b>{S.fmt_som(a)}</b> deb tenglashtirilsinmi?"}[d["act"]]
    if d.get("note"):
        text += f"\nIzoh: {d['note']}"
    await m.answer(text + "\n(PIN so'raladi)", reply_markup=IM(inline_keyboard=[[
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
    if await need_pin(c, state):
        return
    act = d["act"]
    notify = None
    async with Session() as s:
        if act == "open":
            if await start_at(s) is not None:
                await c.answer("Allaqachon ochilgan.", show_alert=True)
                return
            s.add(Setting(key=START_KEY, value={"at": datetime.utcnow().isoformat()}))
            s.add(KassaMove(kassa="rahbar", amount=d["boss"], kind="boshlang", note="Ochilish", by_id=st.id))
            s.add(KassaMove(kassa="kichik", amount=d["petty"], kind="boshlang", note="Ochilish", by_id=st.id))
            msg = (f"✅ Kassalar ochildi.\nRahbar kassasi: {S.fmt_som(d['boss'])}\nKichik kassa: {S.fmt_som(d['petty'])}")
            notify = (f"💳 Kichik kassa ochildi: <b>{S.fmt_som(d['petty'])}</b>.\nEndi barcha xarajatlar faqat shu kassadan. "
                      "O'quvchilar to'lagan pul (qabul kassasi) bilan aralashtirmang — alohida saqlang.")
        else:
            a = d["amount"]
            bal = await boss_balance(s)
            if act in ("petty", "out") and a > bal:
                await state.clear()
                PENDING.pop(c.from_user.id, None)
                await c.message.edit_text(f"❌ Kassangizda pul yetarli emas. Qoldiq: {S.fmt_som(bal)}.")
                return
            if act == "petty":
                s.add(KassaMove(kassa="rahbar", amount=-a, kind="kichikka", note="Kichik kassaga", by_id=st.id))
                s.add(KassaMove(kassa="kichik", amount=a, kind="kirim", note="Rahbardan", by_id=st.id))
                await s.flush()
                pb = await petty_balance(s)
                msg = f"✅ Kichik kassaga {S.fmt_som(a)} berildi. Kassangizda: {S.fmt_som(bal - a)}"
                notify = f"💳 Rahbar kichik kassaga <b>{S.fmt_som(a)}</b> berdi. Kichik kassa: {S.fmt_som(pb)}"
            elif act == "in":
                s.add(KassaMove(kassa="rahbar", amount=a, kind="kirim", note=d.get("note"), by_id=st.id))
                msg = f"✅ Kirim: {S.fmt_som(a)}. Kassangizda: {S.fmt_som(bal + a)}"
            elif act == "out":
                s.add(KassaMove(kassa="rahbar", amount=-a, kind=d.get("kind", "chiqim"), note=d.get("note"), by_id=st.id))
                msg = f"✅ Chiqim: {S.fmt_som(a)}. Kassangizda: {S.fmt_som(bal - a)}"
            elif act == "pfix":
                pb = await petty_balance(s)
                s.add(KassaMove(kassa="kichik", amount=a - pb, kind="tuzatish", note=d.get("note"), by_id=st.id))
                msg = f"✅ Kichik kassa {S.fmt_som(a)} ga tenglashtirildi (farq {'+' if a >= pb else ''}{S.fmt_som(a - pb)})."
                notify = f"✏️ Rahbar kichik kassani tuzatdi: endi <b>{S.fmt_som(a)}</b>."
            else:
                s.add(KassaMove(kassa="rahbar", amount=a - bal, kind="tuzatish", note=d.get("note"), by_id=st.id))
                msg = f"✅ Qoldiq {S.fmt_som(a)} ga tenglashtirildi (farq {'+' if a >= bal else ''}{S.fmt_som(a - bal)})."
        await S.audit(s, st, "kassa_" + act, **{k: v for k, v in d.items() if k in ("amount", "boss", "petty", "note")})
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
                    "egasi": "Egasi oldi", "kichikka": "Kichik kassaga", "fond": "Fondlarga taqsimot"}.get(x.kind, x.kind)
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
    lines = ["📋 <b>Kassam harakatlari</b>", ""] + [
        f"{t + SHIFT:%d.%m %H:%M} · {n} · <b>{'+' if a > 0 else ''}{S.fmt_som(a)}</b>" for t, n, a in rows[-60:]]
    lines += ["", f"Qoldiq: <b>{S.fmt_som(bal)}</b>"]
    await c.answer()
    text = "\n".join(lines)
    for i in range(0, len(text), 3900):
        await c.message.answer(text[i:i + 3900])
