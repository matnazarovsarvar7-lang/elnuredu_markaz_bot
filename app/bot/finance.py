"""🧾 Xarajatlar (kichik kassa / rahbar tasdig'i, kassa qulfi), 🛒 pullik xizmatlar (kitob, printer), printer hisoblagichi."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import func, select

from app.bot import keyboards as K
from app.bot.notify import send, send_photo
from app.config import settings
from app.db.models import Expense, PrinterCount, PrinterUse, Service, ServiceSale, Setting, Staff, StaffPoint
from app.db.session import Session
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
DEFAULT_CATS = ["Ijara", "Kommunal", "Internet", "Kanstovar", "Reklama", "O'quvchilar sovg'alari", "Kitob xaridi",
                "Printer sarf materiallari", "Ta'mirlash", "Xo'jalik mollari", "Bank xizmatlari", "Boshqa"]
SPEND_SHARE = 0.80          # kassa qulfi: erkin pulning ko'pi bilan 80% i
REWARD_SHARE, REWARD_CAP = 0.15, 150_000


def now() -> datetime:
    return datetime.now(TZ)


def _amount(text: str | None) -> int | None:
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    return int(digits) if digits else None


async def categories(s) -> list[str]:
    row = await s.get(Setting, "expense_categories")
    return (row.value or {}).get("list", DEFAULT_CATS) if row else DEFAULT_CATS


# ================= kichik kassa =================
async def petty_limit(s, month: str) -> int | None:
    row = await s.get(Setting, f"petty_{month}")
    return int(row.value["limit"]) if row else None


async def petty_spent(s, month: str) -> int:
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return int((await s.execute(select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.source == "kichik", Expense.status == "tasdiq", Expense.date >= first, Expense.date < nxt))).scalar())


async def free_cash(s) -> int | None:
    """Kassa qulfi: kassadagi pul − barcha majburiyatlar. None — boshlang'ich qoldiq kiritilmagan."""
    from app.bot.cash import expected_cash
    from app.payroll import obligations
    from app.bot.vault import boss_balance
    exp = await expected_cash(s)
    bb = await boss_balance(s)
    if exp is None and bb is None:
        return None
    return (exp or 0) + (bb or 0) - await obligations(s)


# ================= 🧾 xarajat kiritish =================
class Ex(StatesGroup):
    cat = State()
    amount = State()
    note = State()
    receipt = State()


AGAIN = IM(inline_keyboard=[[IB(text="➕ Yana xarajat", callback_data="xnew")]])


@router.callback_query(F.data == "xnew")
async def exp_again(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await exp_start(c.message, state, tg_id=c.from_user.id)


@router.message(F.text == K.B_EXPENSE)
async def exp_start(m: Message, state: FSMContext, tg_id: int | None = None) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, tg_id or m.from_user.id)
        if not (who.staff and who.staff.current_role in ("moliya", "rahbar")):
            return
        cats = await categories(s)
        from app.bot.vault import boss_balance, petty_balance
        pb, bb = await petty_balance(s), await boss_balance(s)
    await state.clear()
    if who.staff.current_role == "moliya" and pb is None:
        await m.answer("💳 Xarajat puli hali kiritilmagan. Rahbar «💼 Kassa → 🧮 Pulni sanab kiritish» ni bosgach, "
                       "xarajat kiritiladi.")
        return
    await state.set_state(Ex.cat)
    head = "🧾 <b>Xarajat</b>\n"
    if who.staff.current_role == "moliya":
        head += f"💳 Xarajat pulingiz: <b>{S.fmt_som(pb)}</b>\n"
    elif bb is not None:
        head += f"👤 Sizda: <b>{S.fmt_som(bb)}</b>\n"
    rows = [[IB(text=c, callback_data=f"xc:{i}")] for i, c in enumerate(cats)]
    rows.append([IB(text="📋 Oxirgi xarajatlar / bekor qilish", callback_data="xl")])
    await m.answer(head + "Toifani tanlang:", reply_markup=IM(inline_keyboard=rows))


@router.callback_query(Ex.cat, F.data.startswith("xc:"))
async def exp_cat(c: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        cats = await categories(s)
    await state.update_data(cat=cats[int(c.data.split(":")[1])])
    await state.set_state(Ex.amount)
    await c.message.edit_text("Summa (so'm):")


@router.message(Ex.amount)
async def exp_amount(m: Message, state: FSMContext) -> None:
    a = _amount(m.text)
    if not a:
        await m.answer("Summani raqam bilan yozing: 150000")
        return
    await state.update_data(amount=a, method="naqd")
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
    if who.staff and who.staff.current_role == "rahbar":
        await m.answer("Qayerdan to'landi?", reply_markup=IM(inline_keyboard=[
            [IB(text="💵 Naqd — qo'limdagi puldan", callback_data="xm:naqd")],
            [IB(text="🏦 Bank / karta orqali", callback_data="xm:bank")]]))
        return
    await state.set_state(Ex.note)
    await m.answer("Izoh (nima olindi, kimdan):")


@router.callback_query(Ex.amount, F.data.startswith("xm:"))
async def exp_method(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(method=c.data.split(":")[1])
    await state.set_state(Ex.note)
    await c.answer()
    await c.message.edit_text("Izoh (nima olindi, kimdan):")


@router.message(Ex.note)
async def exp_note(m: Message, state: FSMContext) -> None:
    await state.update_data(note=(m.text or "").strip()[:300])
    await state.set_state(Ex.receipt)
    await m.answer("📷 Chekni yuboring — rasm yoki PDF fayl (chek bo'lmasa — «chek yo'q» deb yozing):")


@router.message(Ex.receipt)
async def exp_save(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    from app.bot.notify import receipt_of
    rc = receipt_of(m)
    fid = rc[0] if rc else None
    if not fid and "chek" not in (m.text or "").lower():
        await m.answer("Chekni rasm yoki PDF fayl qilib yuboring yoki «chek yo'q» deb yozing.")
        return
    await state.clear()
    t = now().date()
    from app.bot.vault import boss_balance, petty_balance
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        boss = who.staff.current_role == "rahbar"
        method = d.get("method", "naqd")
        e = Expense(date=t, category=d["cat"], amount=d["amount"], note=d["note"], receipt_file_id=fid,
                    by_id=who.staff.id, method=method)
        if boss:
            bal = await boss_balance(s)
            if method == "naqd" and bal is not None and d["amount"] > bal:
                await m.answer(f"❌ Sizda pul yetarli emas. Bor: {S.fmt_som(bal)}, xarajat: "
                               f"{S.fmt_som(d['amount'])}.\nXarajat saqlanmadi.", reply_markup=AGAIN)
                return
            e.source, e.status, e.approved_by = "asosiy", "tasdiq", who.staff.id
            left = None
        else:
            left = await petty_balance(s) or 0
            e.source = "kichik"
            e.status = "tasdiq" if d["amount"] <= left else "kutilmoqda"
        s.add(e)
        await s.flush()
        await S.audit(s, who.staff, "xarajat", category=d["cat"], amount=d["amount"], status=e.status)
        await s.commit()
        eid, status = e.id, e.status
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        name = who.staff.full_name
        bb = await boss_balance(s)
    if boss:
        tail = f"\n👤 Sizda qoldi: {S.fmt_som(bb)}" if method == "naqd" and bb is not None else ""
        await m.answer(f"✅ Xarajat saqlandi: {d['cat']} · {S.fmt_som(d['amount'])}" +
                       (" (bank/karta)" if method != "naqd" else "") + "." + tail, reply_markup=AGAIN)
        return
    if status == "tasdiq":
        from app.bot.vault import low_petty_alert
        await low_petty_alert(bot)
        await m.answer(f"✅ Xarajat: {d['cat']} · {S.fmt_som(d['amount'])}. "
                       f"Xarajat pulingizda qoldi: {S.fmt_som(left - d['amount'])}.", reply_markup=AGAIN)
        return
    need = d["amount"] - left
    await m.answer(f"⚠️ Xarajat pulingiz yetmaydi: bor {S.fmt_som(left)}, kerak {S.fmt_som(d['amount'])}.\n"
                   f"Rahbardan {S.fmt_som(need)} so'raldi. Rahbar pul bergach xarajat o'tadi.",
                   reply_markup=AGAIN)
    text = (f"🧾 <b>Moliyaning xarajat puli yetmayapti</b> ({name})\n{d['cat']}: <b>{S.fmt_som(d['amount'])}</b>\n{d['note']}\n"
            f"Xarajat pulida: {S.fmt_som(left)} · yetmayapti: <b>{S.fmt_som(need)}</b>\n"
            f"Sizda: {S.fmt_som(bb)}")
    kb = IM(inline_keyboard=[[IB(text=f"💳 {S.fmt_som(need)} berib tasdiqlash", callback_data=f"xk:{eid}")],
                             [IB(text="❌ Rad", callback_data=f"xn:{eid}")]])
    for b in bosses:
        if fid:
            await send_photo(bot, b, fid, text, reply_markup=kb)
        else:
            await send(bot, b, text + "\n(chek yo'q)", reply_markup=kb)


@router.callback_query(F.data.startswith("xk:"))
async def exp_topup(c: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    """Rahbar: kichik kassaga yetmagan pulni berib, moliyaning xarajatini tasdiqlaydi (pul rahbar kassasidan)."""
    from app.bot.pin import need_pin
    from app.bot.vault import boss_balance, petty_balance
    from app.db.models import KassaMove
    eid = int(c.data.split(":")[1])
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            await c.answer("Faqat rahbar.", show_alert=True)
            return
        e = await s.get(Expense, eid)
        if e.status != "kutilmoqda":
            await c.answer("Allaqachon hal qilingan.", show_alert=True)
            return
        need = max(0, e.amount - (await petty_balance(s) or 0))
        bal = await boss_balance(s)
        if bal is None or need > bal:
            await c.answer(f"Sizda pul yetarli emas. Bor: {S.fmt_som(bal or 0)}, kerak: {S.fmt_som(need)}.",
                           show_alert=True)
            return
    if await need_pin(c, state, need):
        return
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        e = await s.get(Expense, eid)
        if e.status != "kutilmoqda":
            await c.answer("Allaqachon hal qilingan.", show_alert=True)
            return
        need = max(0, e.amount - (await petty_balance(s) or 0))
        if need:
            s.add(KassaMove(kassa="rahbar", amount=-need, kind="kichikka", note=f"Xarajat #{e.id} uchun", by_id=who.staff.id))
            s.add(KassaMove(kassa="kichik", amount=need, kind="kirim", note="Rahbardan", by_id=who.staff.id))
        e.status, e.approved_by = "tasdiq", who.staff.id
        await S.audit(s, who.staff, "xarajat_qaror", expense=e.id, status="tasdiq", topup=need)
        await s.commit()
        by = await s.get(Staff, e.by_id) if e.by_id else None
        info = f"{e.category} · {S.fmt_som(e.amount)}"
        pb, bal = await petty_balance(s), await boss_balance(s)
    note = f"✅ Moliyaga {S.fmt_som(need)} berildi, xarajat tasdiqlandi. Sizda: {S.fmt_som(bal)}"
    try:
        await c.message.edit_caption(caption=(c.message.caption or "") + f"\n\n{note}")
    except Exception:
        await c.message.edit_text(c.message.html_text + f"\n\n{note}")
    if by and by.tg_id:
        await send(bot, by.tg_id, f"🧾 Xarajat ({info}) tasdiqlandi. Rahbar {S.fmt_som(need)} berdi — "
                                  f"pulni rahbardan oling. Xarajat pulingiz: {S.fmt_som(pb)}.")


@router.callback_query(F.data.startswith(("xy:", "xn:")))
async def exp_decide(c: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    from app.bot.pin import need_pin
    if c.data.startswith("xy:") and await need_pin(c, state):
        return
    kind, eid = c.data.split(":")
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            await c.answer("Faqat rahbar.", show_alert=True)
            return
        e = await s.get(Expense, int(eid))
        if e.status != "kutilmoqda":
            await c.answer("Allaqachon hal qilingan.", show_alert=True)
            return
        if kind == "xy" and e.source == "asosiy" and e.method == "naqd":
            from app.bot.vault import boss_can_pay
            why = await boss_can_pay(s, e.amount)
            if why:
                await c.answer(why, show_alert=True)
                return
        e.status, e.approved_by = ("tasdiq" if kind == "xy" else "rad"), who.staff.id
        await S.audit(s, who.staff, "xarajat_qaror", expense=e.id, status=e.status)
        await s.commit()
        by = await s.get(Staff, e.by_id) if e.by_id else None
        info = f"{e.category} · {S.fmt_som(e.amount)}"
    note = "✅ Tasdiqlandi" if kind == "xy" else "❌ Rad etildi"
    try:
        await c.message.edit_caption(caption=(c.message.caption or "") + f"\n\n{note}")
    except Exception:
        await c.message.edit_text(c.message.html_text + f"\n\n{note}")
    if by and by.tg_id:
        await send(bot, by.tg_id, f"🧾 Xarajat ({info}): {note.lower()}.")


# ================= kichik kassa summasi (rahbar, 1–5-sana) =================
@router.callback_query(F.data == "set:petty")
async def petty_ask(c: CallbackQuery, state: FSMContext) -> None:
    """Endi kichik kassa — haqiqiy pul: rahbar kassasidan beriladi (💼 Kassa → 💳 Kichik kassaga pul berish)."""
    from app.bot.vault import kassa_kb, petty_balance, boss_balance
    async with Session() as s:
        pb, bb = await petty_balance(s), await boss_balance(s)
    await c.answer()
    if pb is None:
        await c.message.answer("💼 Kassa hali ochilmagan.", reply_markup=kassa_kb(False))
        return
    await c.message.answer(f"💳 Moliyadagi xarajat puli: <b>{S.fmt_som(pb)}</b>\n👤 Sizda: <b>{S.fmt_som(bb)}</b>",
                           reply_markup=kassa_kb(True))


async def petty_reminder(bot: Bot) -> None:
    """Oyning 1-sanasi 09:00 — rahbarga kichik kassa qoldig'i (to'ldirish kerakmi)."""
    from app.bot.vault import petty_balance
    async with Session() as s:
        pb = await petty_balance(s)
        if pb is None:
            return
        to = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        from app.bot.vault import suggest_topup, topup_kb
        amt = await suggest_topup(s)
    for t in to:
        await send(bot, t, f"💳 Yangi oy: moliyadagi xarajat puli {S.fmt_som(pb)}. Oxirgi oyda xarajat: ~{S.fmt_som(amt)}.",
                   reply_markup=topup_kb(amt))


# ================= 🛒 pullik xizmatlar =================
class Sv(StatesGroup):
    qty = State()
    new = State()


@router.message(F.text == K.B_SERVICES)
async def svc_menu(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not (who.staff and who.staff.current_role in ("admin", "moliya", "rahbar")):
            return
        svcs = (await s.execute(select(Service).where(Service.active).order_by(Service.kind, Service.name))).scalars().all()
    rows = [[IB(text=f"{'📕' if v.kind == 'kitob' else '🖨' if v.kind == 'printer' else '🧩'} {v.name} — "
                     f"{S.fmt_som(v.price)}" + (f" · {v.stock} ta" if v.kind == "kitob" else ""),
                callback_data=f"sv:{v.id}")] for v in svcs]
    rows.append([IB(text="🖨 Ichki chop etish (bepul)", callback_data="pu")])
    rows.append([IB(text="🖨 Printer hisoblagichi", callback_data="svp")])
    rows.append([IB(text="📋 Oxirgi sotuvlar / bekor qilish", callback_data="sll")])
    if who.staff.current_role == "rahbar":
        rows.append([IB(text="➕ Xizmat/kitob qo'shish", callback_data="svn")])
    await m.answer("🛒 <b>Xizmatlar</b>\n• Pullik sotuv — xizmatni tanlang (o'quvchi hisobiga yozilmaydi, qarzga "
                   "sotilmaydi).\n• Pul olinmaydigan chop etish (test, markaz va o'qituvchi hujjatlari, texnik isrof) — "
                   "«🖨 Ichki chop etish».", reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data.startswith("sv:"))
async def svc_pick(c: CallbackQuery, state: FSMContext) -> None:
    vid = int(c.data.split(":")[1])
    async with Session() as s:
        v = await s.get(Service, vid)
    await state.set_state(Sv.qty)
    await state.update_data(vid=vid)
    unit = "sahifa" if v.kind == "printer" else "dona"
    await c.answer()
    await c.message.answer(f"{v.name} — {S.fmt_som(v.price)}. Tugmani bosing yoki sonini yozing ({unit}):",
                           reply_markup=IM(inline_keyboard=[
                               [IB(text=f"1 {unit} · 💵 naqd", callback_data="svq:1:naqd"),
                                IB(text=f"1 {unit} · 💳 karta", callback_data="svq:1:karta")]]))


@router.callback_query(Sv.qty, F.data.startswith("svq:"))
async def svc_quick(c: CallbackQuery, state: FSMContext) -> None:
    _, q, method = c.data.split(":")
    await state.update_data(qty=int(q))
    d = await state.get_data()
    async with Session() as s:
        v = await s.get(Service, d["vid"])
    if v.kind == "kitob" and (v.stock or 0) < int(q):
        await c.answer(f"Omborda faqat {v.stock or 0} ta bor.", show_alert=True)
        return
    await _svc_do(c, state, method)


@router.message(Sv.qty)
async def svc_qty(m: Message, state: FSMContext) -> None:
    if not (m.text or "").strip().isdigit() or int(m.text) <= 0:
        await m.answer("Sonini yozing: 1")
        return
    d = await state.get_data()
    async with Session() as s:
        v = await s.get(Service, d["vid"])
    q = int(m.text)
    if v.kind == "kitob" and (v.stock or 0) < q:
        await m.answer(f"Omborda faqat {v.stock or 0} ta bor.")
        return
    await state.update_data(qty=q)
    await m.answer(f"{v.name} × {q} = <b>{S.fmt_som(v.price * q)}</b>. To'lov usuli?",
                   reply_markup=IM(inline_keyboard=[[IB(text="💵 Naqd", callback_data="svm:naqd"),
                                                     IB(text="💳 Karta", callback_data="svm:karta")]]))


@router.callback_query(Sv.qty, F.data.startswith("svm:"))
async def svc_save(c: CallbackQuery, state: FSMContext) -> None:
    await _svc_do(c, state, c.data.split(":")[1])


async def _svc_do(c: CallbackQuery, state: FSMContext, method: str) -> None:
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        v = await s.get(Service, d["vid"])
        q = d["qty"]
        if v.kind == "kitob":
            v.stock = (v.stock or 0) - q
        sale = ServiceSale(service_id=v.id, qty=q, amount=v.price * q, profit=(v.price - (v.cost or 0)) * q,
                           method=method, by_id=who.staff.id)
        s.add(sale)
        await S.audit(s, who.staff, "xizmat_sotuv", service=v.name, qty=q, amount=v.price * q, method=method)
        await s.commit()
        left = v.stock
        name = v.name
    await c.message.edit_text(f"✅ Sotuv: {name} × {q} = {S.fmt_som(d['qty'] * v.price)} ({method})."
                              + (f"\nOmborda qoldi: {left}" if v.kind == "kitob" else ""),
                              reply_markup=IM(inline_keyboard=[[IB(text=f"➕ Yana: {name[:30]}", callback_data=f"sv:{v.id}")]]))


@router.callback_query(F.data == "svn")
async def svc_new(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Sv.new)
    await c.answer()
    await c.message.answer("Yangi xizmat — bir qatorda yozing:\n<code>tur; nomi; narx; tannarx; ombor</code>\n"
                           "Misollar:\n<code>kitob; English File A1; 85000; 60000; 20</code>\n"
                           "<code>printer; Chop etish (A4); 500; 150; -</code>")


@router.message(Sv.new)
async def svc_new_save(m: Message, state: FSMContext) -> None:
    parts = [x.strip() for x in (m.text or "").split(";")]
    try:
        kind, name, price, cost = parts[0].lower(), parts[1], int(parts[2]), int(parts[3])
        stock = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
        assert kind in ("kitob", "printer", "boshqa") and name
    except (IndexError, ValueError, AssertionError):
        await m.answer("Format: tur; nomi; narx; tannarx; ombor")
        return
    await state.clear()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        s.add(Service(kind=kind, name=name, price=price, cost=cost, stock=stock if kind == "kitob" else None))
        await S.audit(s, who.staff, "xizmat_narx", service=name, price=price)
        await s.commit()
    await m.answer(f"✅ Qo'shildi: {name} — {S.fmt_som(price)}")


# ---------- printer hisoblagichi (haftada 1 marta) ----------
class Pc(StatesGroup):
    counter = State()


@router.callback_query(F.data == "svp")
async def printer_ask(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Pc.counter)
    await c.answer()
    await c.message.answer("🖨 Printer hisoblagichidagi umumiy sahifa sonini yozing:")


@router.message(Pc.counter)
async def printer_save(m: Message, state: FSMContext, bot: Bot) -> None:
    if not (m.text or "").strip().isdigit():
        await m.answer("Raqam yozing: 15230")
        return
    await state.clear()
    cnt = int(m.text.strip())
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        prev = (await s.execute(select(PrinterCount).order_by(PrinterCount.id.desc()))).scalars().first()
        if prev and cnt < prev.counter:
            await m.answer(f"Oldingi ko'rsatkich {prev.counter} edi — kichik raqam bo'lishi mumkin emas. Qayta yozing.")
            return
        s.add(PrinterCount(counter=cnt, by_id=who.staff.id))
        rep = await printer_summary(s, prev.created_at if prev else None) if prev else None
        await s.commit()
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    if not prev:
        await m.answer("✅ Boshlang'ich ko'rsatkich qayd etildi. Keyingi haftadan bot solishtiradi.")
        return
    printed = cnt - prev.counter
    paid, internal, by_cat = rep
    diff = printed - paid - internal
    text = (f"🖨 <b>Printer · haftalik solishtirish</b>\nHisoblagich bo'yicha: {printed} sahifa\n"
            f"• Pullik (sotuvda): {paid}\n• Ichki (bepul): {internal}\n"
            + "".join(f"   – {PU_CATS[k]}: {v}\n" for k, v in by_cat.items() if v)
            + f"Hisobsiz farq: <b>{diff}</b>" + (f" ({abs(diff) / printed * 100:.0f}%)" if printed else ""))
    waste = by_cat.get("isrof", 0)
    alert = printed > 0 and (abs(diff) > printed * 0.10 or waste > printed * 0.05)
    await m.answer("✅ Qayd etildi." + (" ⚠️ Farq bor — rahbarga yuborildi." if alert else ""))
    for b in bosses:
        await send(bot, b, text + ("\n⚠️ Farq 10% dan oshdi!" if printed and abs(diff) > printed * 0.10 else "")
                   + ("\n⚠️ Isrof 5% dan oshdi!" if printed and waste > printed * 0.05 else ""))


PU_CATS = {"test": "Test va nazorat varaqlari", "markaz": "Markaz hujjatlari", "oqituvchi": "O'qituvchilar uchun hujjatlar",
           "isrof": "Texnik xato / isrof"}


async def printer_summary(s, since: datetime) -> tuple[int, int, dict[str, int]]:
    paid = int((await s.execute(select(func.coalesce(func.sum(ServiceSale.qty), 0)).join(
        Service, Service.id == ServiceSale.service_id).where(Service.kind == "printer", ServiceSale.status == "tasdiq",
                                                             ServiceSale.created_at >= since))).scalar())
    rows = (await s.execute(select(PrinterUse.category, func.sum(PrinterUse.pages)).where(
        PrinterUse.created_at >= since).group_by(PrinterUse.category))).all()
    by_cat = {k: int(v) for k, v in rows}
    return paid, sum(by_cat.values()), by_cat


class Pu(StatesGroup):
    pages = State()
    note = State()


@router.callback_query(F.data == "pu")
async def pu_start(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await c.answer()
    await c.message.answer("🖨 Ichki (bepul) chop etish — turini tanlang:", reply_markup=IM(inline_keyboard=[
        [IB(text=v, callback_data=f"puc:{k}")] for k, v in PU_CATS.items()]))


@router.callback_query(F.data.startswith("puc:"))
async def pu_cat(c: CallbackQuery, state: FSMContext) -> None:
    cat = c.data.split(":")[1]
    await state.update_data(cat=cat)
    if cat == "oqituvchi":
        async with Session() as s:
            ts = [x for x in (await s.execute(select(Staff).where(Staff.active).order_by(Staff.full_name))).scalars()
                  if any(r in (x.roles or "") for r in ("oqituvchi", "yordamchi"))]
        await c.message.edit_text("Kim uchun?", reply_markup=IM(inline_keyboard=[
            [IB(text=x.full_name, callback_data=f"put:{x.id}")] for x in ts]))
        return
    await state.set_state(Pu.pages)
    await c.message.edit_text(f"{PU_CATS[cat]}: necha sahifa?")


@router.callback_query(F.data.startswith("put:"))
async def pu_teacher(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(staff=int(c.data.split(":")[1]))
    await state.set_state(Pu.pages)
    await c.message.edit_text("Necha sahifa?")


@router.message(Pu.pages)
async def pu_pages(m: Message, state: FSMContext) -> None:
    if not (m.text or "").strip().isdigit() or int(m.text) <= 0:
        await m.answer("Sahifalar sonini yozing: 12")
        return
    await state.update_data(pages=int(m.text))
    await state.set_state(Pu.note)
    d = await state.get_data()
    await m.answer("Qisqa izoh (nima chop etildi" + (", nima sababdan isrof bo'ldi" if d["cat"] == "isrof" else "") + "):")


@router.message(Pu.note)
async def pu_save(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        s.add(PrinterUse(category=d["cat"], pages=d["pages"], note=(m.text or "").strip()[:200], staff_id=d.get("staff"),
                         by_id=who.staff.id))
        await S.audit(s, who.staff, "printer_ichki", category=d["cat"], pages=d["pages"])
        await s.commit()
    await m.answer(f"✅ Qayd etildi: {PU_CATS[d['cat']]} — {d['pages']} sahifa (pul olinmaydi).")


async def printer_reminder(bot: Bot) -> None:
    """Dushanba 10:00 — printer hisoblagichini kiritish eslatmasi."""
    async with Session() as s:
        has = (await s.execute(select(Service.id).where(Service.kind == "printer", Service.active))).first()
        to = [x.tg_id for x in await S.staff_with_role(s, "moliya") if x.tg_id]
    if has:
        for t in to:
            await send(bot, t, "🖨 Haftalik tekshiruv: printer hisoblagichini kiriting — 🛒 Xizmatlar → 🖨 Printer hisoblagichi.")


async def service_rewards(s, staff_id: int, start: datetime, end: datetime) -> int:
    """Sotuvchiga xizmat foydasining 15% i, oyiga ko'pi bilan 150 000."""
    profit = int((await s.execute(select(func.coalesce(func.sum(ServiceSale.profit), 0)).where(
        ServiceSale.by_id == staff_id, ServiceSale.status == "tasdiq", ServiceSale.created_at > start,
        ServiceSale.created_at <= end))).scalar())
    return min(REWARD_CAP, int(profit * REWARD_SHARE))
