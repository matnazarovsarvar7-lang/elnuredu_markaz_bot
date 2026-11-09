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
from app.db.models import Expense, PrinterCount, Service, ServiceSale, Setting, Staff, StaffPoint
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
    exp = await expected_cash(s)
    if exp is None:
        return None
    return exp - await obligations(s)


# ================= 🧾 xarajat kiritish =================
class Ex(StatesGroup):
    cat = State()
    amount = State()
    note = State()
    receipt = State()


@router.message(F.text == K.B_EXPENSE)
async def exp_start(m: Message, state: FSMContext) -> None:
    month = f"{now():%Y-%m}"
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not (who.staff and who.staff.current_role in ("moliya", "rahbar")):
            return
        cats = await categories(s)
        lim = await petty_limit(s, month)
        spent = await petty_spent(s, month)
    await state.clear()
    await state.set_state(Ex.cat)
    head = "🧾 <b>Xarajat</b>\n"
    if who.staff.current_role == "moliya":
        head += (f"Kichik kassa qoldig'i: <b>{S.fmt_som(lim - spent)}</b>\n" if lim is not None else
                 "Kichik kassa summasi bu oy hali belgilanmagan — xarajat rahbar tasdig'iga boradi.\n")
    rows = [[IB(text=c, callback_data=f"xc:{i}")] for i, c in enumerate(cats)]
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
    await state.update_data(amount=a)
    await state.set_state(Ex.note)
    await m.answer("Izoh (nima olindi, kimdan):")


@router.message(Ex.note)
async def exp_note(m: Message, state: FSMContext) -> None:
    await state.update_data(note=(m.text or "").strip()[:300])
    await state.set_state(Ex.receipt)
    await m.answer("📷 Chek rasmini yuboring (chek bo'lmasa — «chek yo'q» deb yozing):")


@router.message(Ex.receipt)
async def exp_save(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    fid = m.photo[-1].file_id if m.photo else None
    if not fid and "chek" not in (m.text or "").lower():
        await m.answer("Chek rasmini yuboring yoki «chek yo'q» deb yozing.")
        return
    await state.clear()
    t = now().date()
    month = f"{t:%Y-%m}"
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        boss = who.staff.current_role == "rahbar"
        lim = await petty_limit(s, month)
        left = (lim - await petty_spent(s, month)) if lim is not None else 0
        free = await free_cash(s)
        e = Expense(date=t, category=d["cat"], amount=d["amount"], note=d["note"], receipt_file_id=fid, by_id=who.staff.id)
        if boss:
            e.source, e.status, e.approved_by = "asosiy", "tasdiq", who.staff.id
        elif d["amount"] <= left:
            e.source, e.status = "kichik", "tasdiq"
        else:
            e.source, e.status = "asosiy", "kutilmoqda"
        s.add(e)
        await s.flush()
        await S.audit(s, who.staff, "xarajat", category=d["cat"], amount=d["amount"], status=e.status)
        await s.commit()
        eid, status = e.id, e.status
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        name = who.staff.full_name
    if boss:
        warn = ""
        if free is not None and d["amount"] > free * SPEND_SHARE:
            warn = f"\n⚠️ Kassa qulfi: erkin pulning 80% i ({S.fmt_som(int(max(0, free) * SPEND_SHARE))}) dan oshdi."
        await m.answer(f"✅ Xarajat saqlandi: {d['cat']} · {S.fmt_som(d['amount'])}." + warn)
        return
    if status == "tasdiq":
        await m.answer(f"✅ Kichik kassadan: {d['cat']} · {S.fmt_som(d['amount'])}. Qoldiq: {S.fmt_som(left - d['amount'])}.")
        return
    await m.answer("Kichik kassa mablag'i yetarli emas — xarajat rahbar tasdig'iga yuborildi.")
    lock = ""
    if free is not None:
        allowed = int(max(0, free) * SPEND_SHARE)
        lock = (f"\nKassa qulfi: ishlatish mumkin {S.fmt_som(allowed)}" +
                (" — ✅ yetadi" if d["amount"] <= allowed else " — ⚠️ OSHADI (faqat o'ta zarur bo'lsa tasdiqlang)"))
    text = (f"🧾 <b>Xarajat so'rovi</b> ({name})\n{d['cat']}: <b>{S.fmt_som(d['amount'])}</b>\n{d['note']}{lock}")
    kb = IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=f"xy:{eid}"), IB(text="❌ Rad", callback_data=f"xn:{eid}")]])
    for b in bosses:
        if fid:
            await send_photo(bot, b, fid, text, reply_markup=kb)
        else:
            await send(bot, b, text + "\n(chek yo'q)", reply_markup=kb)


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
class Pt(StatesGroup):
    amount = State()


@router.callback_query(F.data == "set:petty")
async def petty_ask(c: CallbackQuery, state: FSMContext) -> None:
    month = f"{now():%Y-%m}"
    async with Session() as s:
        lim = await petty_limit(s, month)
        free = await free_cash(s)
    await state.set_state(Pt.amount)
    cap = f"\nTavsiya chegarasi (erkin pulning 20%): {S.fmt_som(int(max(0, free) * 0.2))}" if free is not None else ""
    await c.answer()
    await c.message.answer(f"💳 Kichik kassa ({month}): hozir {S.fmt_som(lim) if lim is not None else 'belgilanmagan'}.{cap}\n"
                           "Yangi summani yozing:")


@router.message(Pt.amount)
async def petty_set(m: Message, state: FSMContext, bot: Bot) -> None:
    a = _amount(m.text)
    if a is None:
        await m.answer("Summani yozing: 1000000")
        return
    await state.clear()
    month = f"{now():%Y-%m}"
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        row = await s.get(Setting, f"petty_{month}")
        if row:
            row.value = {"limit": a}
        else:
            s.add(Setting(key=f"petty_{month}", value={"limit": a}))
        await S.audit(s, who.staff, "kichik_kassa", month=month, limit=a)
        await s.commit()
        fin = [x.tg_id for x in await S.staff_with_role(s, "moliya") if x.tg_id]
    await m.answer(f"✅ {month} uchun kichik kassa: {S.fmt_som(a)}.")
    for t in fin:
        await send(bot, t, f"💳 Bu oy kichik kassa: {S.fmt_som(a)}. Xarajatlarni «🧾 Xarajat» orqali chek bilan kiriting.")


async def petty_reminder(bot: Bot) -> None:
    """Oyning 1-sanasi 09:00 — rahbarga kichik kassani belgilash eslatmasi (5-sanagacha)."""
    async with Session() as s:
        if await petty_limit(s, f"{now():%Y-%m}") is not None:
            return
        to = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    for t in to:
        await send(bot, t, "💳 Bu oy uchun kichik kassa summasini belgilang (5-sanagacha): 🛠 Sozlamalar → 💳 Kichik kassa.")


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
    rows.append([IB(text="🖨 Printer hisoblagichi", callback_data="svp")])
    if who.staff.current_role == "rahbar":
        rows.append([IB(text="➕ Xizmat/kitob qo'shish", callback_data="svn")])
    await m.answer("🛒 <b>Pullik xizmatlar</b>\nSotuvni kiritish uchun xizmatni tanlang. (Xizmatlar o'quvchi hisobiga "
                   "yozilmaydi va qarzga sotilmaydi.)", reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data.startswith("sv:"))
async def svc_pick(c: CallbackQuery, state: FSMContext) -> None:
    vid = int(c.data.split(":")[1])
    async with Session() as s:
        v = await s.get(Service, vid)
    await state.set_state(Sv.qty)
    await state.update_data(vid=vid)
    unit = "sahifa" if v.kind == "printer" else "dona"
    await c.answer()
    await c.message.answer(f"{v.name}: necha {unit}? (masalan: 1)")


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
    d = await state.get_data()
    await state.clear()
    method = c.data.split(":")[1]
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
                              + (f"\nOmborda qoldi: {left}" if v.kind == "kitob" else ""))


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
        s.add(PrinterCount(counter=cnt, by_id=who.staff.id))
        sold = 0
        if prev:
            sold = int((await s.execute(select(func.coalesce(func.sum(ServiceSale.qty), 0)).join(
                Service, Service.id == ServiceSale.service_id).where(Service.kind == "printer",
                ServiceSale.status == "tasdiq", ServiceSale.created_at >= prev.created_at))).scalar())
        await s.commit()
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    if not prev:
        await m.answer("✅ Boshlang'ich ko'rsatkich qayd etildi.")
        return
    printed = cnt - prev.counter
    diff = printed - sold
    await m.answer(f"✅ Qayd etildi. Shu davrda chop etilgan: {printed}, sotuvda kiritilgan: {sold}.")
    if printed > 0 and abs(diff) > printed * 0.10:
        for b in bosses:
            await send(bot, b, f"🖨 <b>Printer farqi</b>: hisoblagich bo'yicha {printed} sahifa, sotuvda {sold} "
                               f"(farq {diff}, {abs(diff) / printed * 100:.0f}%).")


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
