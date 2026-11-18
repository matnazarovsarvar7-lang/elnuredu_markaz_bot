"""📦 Ombor va hisobdorlik: rahbar tovarni (qog'oz, kitob, ...) xodimlarga beradi, xodim sarf/sotuvni qayd etadi,
bot har bir xodim hisobidagi qoldiqni yuritadi, sanoqni solishtiradi va hisobot beradi.

Qoldiq = shu xodimga tegishli StockMove.qty yig'indisi (faqat qabul qilinganlari).
Markaz ombori — holder_id = None.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import func, select

from app import services as S
from app.bot import keyboards as K
from app.bot.notify import send
from app.config import settings
from app.db.models import Service, Staff, StockItem, StockMove
from app.db.session import Session

router = Router()
TZ = ZoneInfo(settings.timezone)
HOLDER_ROLES = ("admin", "moliya")
KIND = {"kirim": "📥 kirim", "berildi": "📤 berildi", "qaytdi": "↩️ qaytarildi", "sotuv": "💰 sotildi",
        "ichki": "🏢 ichki ish", "isrof": "🗑 isrof", "sanoq": "🧮 sanoq farqi", "tuzatish": "✏️ tuzatish"}
USE_CATS = {"ichki": "🏢 Markazning ichki ishi (test, hujjat, o'qituvchi)", "isrof": "🗑 Isrof / buzildi / yo'qoldi"}


class Sk(StatesGroup):
    qty = State()
    new = State()
    got = State()
    rep = State()


# ---------------- qoldiqlar ----------------
async def balance(s, item_id: int, holder_id: int | None) -> int:
    q = select(func.coalesce(func.sum(StockMove.qty), 0)).where(StockMove.item_id == item_id, StockMove.confirmed)
    q = q.where(StockMove.holder_id.is_(None)) if holder_id is None else q.where(StockMove.holder_id == holder_id)
    return int((await s.execute(q)).scalar())


async def balances(s) -> dict[tuple[int, int | None], int]:
    rows = (await s.execute(select(StockMove.item_id, StockMove.holder_id, func.sum(StockMove.qty))
                            .where(StockMove.confirmed).group_by(StockMove.item_id, StockMove.holder_id))).all()
    return {(i, h): int(q) for i, h, q in rows}


async def pending(s, holder_id: int | None = None) -> list[StockMove]:
    q = select(StockMove).where(StockMove.confirmed.is_(False), StockMove.kind == "berildi")
    if holder_id is not None:
        q = q.where(StockMove.holder_id == holder_id)
    return list((await s.execute(q.order_by(StockMove.id))).scalars())


def _who_label(st: Staff | None) -> str:
    return "Markaz ombori" if st is None else st.full_name


def _n(item: StockItem, q: int) -> str:
    return f"{q:,}".replace(",", " ") + f" {item.unit}"


async def _staff(s, tg: int) -> Staff | None:
    return (await S.who_by_tg(s, tg)).staff


async def _holders(s) -> list[Staff]:
    out = []
    for st in (await s.execute(select(Staff).where(Staff.active).order_by(Staff.full_name))).scalars():
        if any(r in (st.roles or "") for r in HOLDER_ROLES):
            out.append(st)
    return out


async def _bosses(s) -> list[int]:
    return [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]


# ---------------- sotuv / chop etish (finance.py chaqiradi) ----------------
# Hisob xodimning o'z hisobotiga asoslanadi; Xizmatlardagi sotuv va chop etish yozuvlari faqat SOLISHTIRISH uchun.
async def on_sale(s, staff: Staff, service: Service, qty: int, sale_id: int) -> tuple[bool, str]:
    return True, ""


async def sell_block(tg: int, service_id: int, qty: int) -> str | None:
    return None


async def on_sale_void(s, sale_id: int) -> None:
    return None


async def on_print(s, staff: Staff, pages: int, category: str) -> str:
    return ""


async def recorded(s, item: StockItem, holder_id: int, since: datetime | None) -> tuple[int | None, int | None]:
    """Bot yozuvlari bo'yicha (sotuv, ichki) — xodim hisobotini tekshirish uchun. None — solishtirib bo'lmaydi."""
    from app.db.models import PrinterUse, ServiceSale
    cond = [ServiceSale.by_id == holder_id, ServiceSale.status == "tasdiq"]
    if since:
        cond.append(ServiceSale.created_at >= since)
    if item.paper:
        sold = (await s.execute(select(func.coalesce(func.sum(ServiceSale.qty), 0)).join(
            Service, Service.id == ServiceSale.service_id).where(Service.kind == "printer", *cond))).scalar()
        pc = [PrinterUse.by_id == holder_id] + ([PrinterUse.created_at >= since] if since else [])
        inner = (await s.execute(select(func.coalesce(func.sum(PrinterUse.pages), 0)).where(*pc))).scalar()
        return int(sold), int(inner)
    if item.service_id:
        sold = (await s.execute(select(func.coalesce(func.sum(ServiceSale.qty), 0)).where(
            ServiceSale.service_id == item.service_id, *cond))).scalar()
        return int(sold), None
    return None, None


async def last_report(s, item_id: int, holder_id: int) -> StockMove | None:
    return (await s.execute(select(StockMove).where(StockMove.item_id == item_id, StockMove.holder_id == holder_id,
                                                    StockMove.kind == "hisobot").order_by(StockMove.id.desc()))).scalars().first()


# ---------------- menyu ----------------
def _d(dt: datetime | None) -> str:
    return (dt + timedelta(hours=5)).strftime("%d.%m") if dt else "—"


@router.message(F.text == K.B_STOCK)
async def stock_menu(m: Message, state: FSMContext) -> None:
    await state.clear()
    async with Session() as s:
        st = await _staff(s, m.from_user.id)
        if not st or st.current_role not in ("rahbar",) + HOLDER_ROLES:
            return
        text, kb = await (_boss_view(s) if st.current_role == "rahbar" else _holder_view(s, st))
    await m.answer(text, reply_markup=kb)


async def _report_tag(s, it: StockItem, h: int) -> str:
    r = await last_report(s, it.id, h)
    if not r:
        return "hisobot hali berilmagan"
    return f"hisobot {_d(r.created_at)} " + ("✅" if "farq 0" in (r.note or "") else "⚠️")


async def _boss_view(s) -> tuple[str, IM]:
    items = {i.id: i for i in (await s.execute(select(StockItem).where(StockItem.active))).scalars()}
    bal = await balances(s)
    staff = {x.id: x for x in (await s.execute(select(Staff))).scalars()}
    lines = ["📦 <b>Ombor — kimda nima bor</b>"]
    holders = sorted({h for (i, h), q in bal.items() if h is not None and i in items and q}, key=lambda h: staff[h].full_name)
    for h in holders:
        lines.append(f"\n👤 <b>{staff[h].full_name}</b>")
        for i, it in items.items():
            q = bal.get((i, h), 0)
            if q:
                lines.append(f"• {it.name} — <b>{_n(it, q)}</b> · {await _report_tag(s, it, h)}")
    for mv in await pending(s):
        it = items.get(mv.item_id)
        if it:
            lines.append(f"⏳ {staff[mv.holder_id].full_name}: {it.name} {_n(it, mv.qty)} — qabul qilishi kutilmoqda")
    if len(lines) == 1:
        lines.append("\nHali hech kimga tovar berilmagan. «📤 Tovar berish» ni bosing — A4 qog'oz, kitob va h.k.")
    kb = IM(inline_keyboard=[
        [IB(text="📤 Tovar berish", callback_data="sk:give")],
        [IB(text="↩️ Qaytarib olish", callback_data="sk:back"), IB(text="📊 Oylik hisobot", callback_data="sk:rep:0")],
        [IB(text="🧾 Harakatlar", callback_data="sk:log")]])
    return "\n".join(lines), kb


async def _holder_view(s, st: Staff) -> tuple[str, IM]:
    items = list((await s.execute(select(StockItem).where(StockItem.active).order_by(StockItem.name))).scalars())
    lines = ["📦 <b>Mening hisobimdagi tovarlar</b>", ""]
    have = []
    for it in items:
        q = await balance(s, it.id, st.id)
        if q:
            have.append(it)
            lines.append(f"• {it.name} — <b>{_n(it, q)}</b> · {await _report_tag(s, it, st.id)}")
    pend = await pending(s, st.id)
    names = {i.id: i.name for i in items}
    for mv in pend:
        lines.append(f"⏳ Sizga berilgan: {names.get(mv.item_id)} {mv.qty} — sanab, qabul qiling")
    if not have and not pend:
        lines.append("Hisobingizda tovar yo'q.")
    if have:
        lines += ["", "Hafta oxirida «📝 Hisobot berish»: nechtasi ichki ishlarga, nechtasi sotuvga ketdi va "
                      "qo'lingizda nechta qoldi — bot o'zi tekshiradi."]
    kb = [[IB(text=f"✅ Qabul qilish: {names.get(mv.item_id)} {mv.qty}", callback_data=f"sk:acc:{mv.id}")] for mv in pend]
    if have:
        kb += [[IB(text="📝 Hisobot berish", callback_data="sk:report")],
               [IB(text="↩️ Rahbarga qaytarish", callback_data="sk:ret")]]
    return "\n".join(lines), IM(inline_keyboard=kb or [[IB(text="—", callback_data="sk:none")]])


@router.callback_query(F.data == "sk:none")
async def _none(c: CallbackQuery) -> None:
    await c.answer()


# ---------------- rahbar: tovar berish (yangi tovar shu yerda qo'shiladi) ----------------
@router.callback_query(F.data == "sk:give")
async def give(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with Session() as s:
        items = list((await s.execute(select(StockItem).where(StockItem.active).order_by(StockItem.name))).scalars())
    rows = [[IB(text=f"{'🖨' if it.paper else '📕' if it.service_id else '📦'} {it.name}", callback_data=f"skg:{it.id}")]
            for it in items]
    rows.append([IB(text="➕ Yangi tovar", callback_data="sk:new")])
    await c.answer()
    await c.message.answer("📤 Qaysi tovarni berasiz?", reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data == "sk:new")
async def item_new(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Sk.new)
    await c.answer()
    await c.message.answer("➕ Yangi tovar nomini yozing, masalan: <code>A4 qog'oz</code> yoki "
                           "<code>Biologiya kitobi</code>")


@router.message(Sk.new)
async def item_new_name(m: Message, state: FSMContext) -> None:
    name = (m.text or "").strip()[:80]
    if not name:
        await m.answer("Nomini yozing.")
        return
    await state.update_data(name=name)
    async with Session() as s:
        books = list((await s.execute(select(Service).where(Service.active, Service.kind == "kitob")
                                      .order_by(Service.name))).scalars())
    rows = [[IB(text="🖨 Qog'oz (varaq) — ichki ish va chop etish sotuvi", callback_data="sknk:qogoz")]]
    rows += [[IB(text=f"📕 Kitob: {v.name} ({S.fmt_som(v.price)})", callback_data=f"sknk:{v.id}")] for v in books]
    rows.append([IB(text="📦 Boshqa (dona) — faqat ichki ish", callback_data="sknk:ichki")])
    await m.answer(f"«{name}» — bu nima?" + ("" if books else "\n<i>Kitob sotiladigan bo'lsa, avval 🛒 Xizmatlar → ➕ da "
                                                              "kitobni narxi bilan qo'shing.</i>"),
                   reply_markup=IM(inline_keyboard=rows))


@router.callback_query(Sk.new, F.data.startswith("sknk:"))
async def item_new_kind(c: CallbackQuery, state: FSMContext) -> None:
    kind = c.data.split(":")[1]
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        svc = await s.get(Service, int(kind)) if kind.isdigit() else None
        it = StockItem(name=d["name"], unit="varaq" if kind == "qogoz" else "dona", paper=kind == "qogoz",
                       service_id=svc.id if svc else None)
        s.add(it)
        await S.audit(s, st, "ombor_tovar", name=d["name"])
        await s.commit()
        iid = it.id
    await c.answer("Qo'shildi")
    await _give_item(c.message, iid)


@router.callback_query(F.data.startswith("skg:"))
async def give_item(c: CallbackQuery) -> None:
    await c.answer()
    await _give_item(c.message, int(c.data.split(":")[1]))


async def _give_item(msg: Message, iid: int) -> None:
    async with Session() as s:
        hs = await _holders(s)
        it = await s.get(StockItem, iid)
    await msg.answer(f"📤 {it.name} — kimga berasiz?", reply_markup=IM(inline_keyboard=[
        [IB(text=f"{h.full_name} ({', '.join(r for r in HOLDER_ROLES if r in h.roles)})", callback_data=f"skgh:{iid}:{h.id}")]
        for h in hs]))


@router.callback_query(F.data.startswith("skgh:"))
async def give_holder(c: CallbackQuery, state: FSMContext) -> None:
    _, iid, hid = c.data.split(":")
    await _ask_qty(c, state, "berish", item=int(iid), holder=int(hid))


async def _ask_qty(c: CallbackQuery, state: FSMContext, op: str, **data) -> None:
    await state.set_state(Sk.qty)
    await state.update_data(op=op, **data)
    async with Session() as s:
        it = await s.get(StockItem, data["item"])
    await c.message.edit_text(f"{it.name}: nechta ({it.unit})? Raqam yozing:")


@router.callback_query(F.data == "sk:back")
async def take_back(c: CallbackQuery) -> None:
    async with Session() as s:
        bal = await balances(s)
        staff = {x.id: x for x in (await s.execute(select(Staff))).scalars()}
        items = {i.id: i for i in (await s.execute(select(StockItem))).scalars()}
    rows = [[IB(text=f"{staff[h].full_name}: {items[i].name} {_n(items[i], q)}", callback_data=f"skbi:{h}:{i}")]
            for (i, h), q in bal.items() if h is not None and q > 0 and i in items]
    await c.answer()
    await c.message.answer("↩️ Nimani qaytarib olasiz?" if rows else "Hech kimda tovar yo'q.",
                           reply_markup=IM(inline_keyboard=rows) if rows else None)


@router.callback_query(F.data.startswith("skbi:"))
async def take_back_item(c: CallbackQuery, state: FSMContext) -> None:
    _, hid, iid = c.data.split(":")
    await _ask_qty(c, state, "olish", item=int(iid), holder=int(hid))


@router.callback_query(F.data == "sk:ret")
async def ret(c: CallbackQuery) -> None:
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        items = list((await s.execute(select(StockItem).where(StockItem.active))).scalars())
        rows = [[IB(text=f"{it.name} — {_n(it, q)}", callback_data=f"skr:{it.id}")]
                for it in items if (q := await balance(s, it.id, st.id)) > 0]
    await c.answer()
    await c.message.answer("Nimani qaytarasiz?", reply_markup=IM(inline_keyboard=rows) if rows else None)


@router.callback_query(F.data.startswith("skr:"))
async def ret_item(c: CallbackQuery, state: FSMContext) -> None:
    await _ask_qty(c, state, "qaytarish", item=int(c.data.split(":")[1]))


@router.message(Sk.qty)
async def qty_entered(m: Message, state: FSMContext, bot: Bot) -> None:
    t = (m.text or "").replace(" ", "")
    if not t.isdigit() or int(t) <= 0:
        await m.answer("Raqam yozing: 500")
        return
    d = await state.get_data()
    await state.clear()
    q = int(t)
    op, iid = d["op"], d["item"]
    notify: list[tuple[int, str, IM | None]] = []
    async with Session() as s:
        st = await _staff(s, m.from_user.id)
        it = await s.get(StockItem, iid)
        if op == "berish":
            out = StockMove(item_id=iid, holder_id=None, qty=-q, kind="berildi", by_id=st.id)
            s.add(out)
            await s.flush()
            inc = StockMove(item_id=iid, holder_id=d["holder"], qty=q, kind="berildi", by_id=st.id, pair_id=out.id,
                            confirmed=False)
            s.add(inc)
            await s.flush()
            out.pair_id = inc.id
            h = await s.get(Staff, d["holder"])
            msg = f"📤 {h.full_name}ga {it.name} {_n(it, q)} berildi. U sanab, qabul qilganini tasdiqlaydi."
            if h.tg_id:
                notify.append((h.tg_id, f"📦 Rahbar sizga <b>{it.name} {_n(it, q)}</b> berdi. Sanab, qabul qiling:",
                               IM(inline_keyboard=[[IB(text="✅ To'liq qabul qildim", callback_data=f"sk:acc:{inc.id}")],
                                                   [IB(text="✏️ Boshqa son keldi", callback_data=f"sk:got:{inc.id}")]])))
        else:
            holder = d.get("holder") if op == "olish" else st.id
            have = await balance(s, iid, holder)
            if q > have:
                await m.answer(f"Hisobda {_n(it, have)} bor.")
                return
            out = StockMove(item_id=iid, holder_id=holder, qty=-q, kind="qaytdi", by_id=st.id)
            s.add(out)
            await s.flush()
            s.add(StockMove(item_id=iid, holder_id=None, qty=q, kind="qaytdi", by_id=st.id, pair_id=out.id))
            h = await s.get(Staff, holder)
            if op == "olish":
                msg = f"↩️ {h.full_name}dan {it.name} {_n(it, q)} qaytarib olindi. Unda qoldi: {_n(it, have - q)}"
                if h.tg_id:
                    notify.append((h.tg_id, f"↩️ Rahbar sizdan {it.name} {_n(it, q)} qaytarib oldi.", None))
            else:
                msg = f"↩️ Rahbarga qaytarildi: {it.name} {_n(it, q)}. Hisobingizda: {_n(it, have - q)}"
                for b in await _bosses(s):
                    notify.append((b, f"↩️ {st.full_name} {it.name} {_n(it, q)} qaytardi — qabul qilib oling.", None))
        await S.audit(s, st, "ombor_" + op, item=it.name, qty=q)
        await s.commit()
    await m.answer(msg)
    for tg, txt, kb in notify:
        await send(bot, tg, txt, reply_markup=kb)


@router.callback_query(F.data.startswith("sk:acc:"))
async def accept(c: CallbackQuery, bot: Bot) -> None:
    mid = int(c.data.split(":")[2])
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        mv = await s.get(StockMove, mid)
        if not mv or not st or mv.holder_id != st.id:
            return await c.answer("Bu sizga berilmagan.", show_alert=True)
        if mv.confirmed:
            return await c.answer("Allaqachon qabul qilingan.")
        mv.confirmed = True
        it = await s.get(StockItem, mv.item_id)
        await s.commit()
        bosses = await _bosses(s)
        left = await balance(s, it.id, st.id)
    await c.answer("Qabul qilindi")
    await c.message.edit_text(f"✅ Qabul qildingiz: {it.name} {_n(it, mv.qty)}. Hisobingizda: {_n(it, left)}\n"
                              "Ishlatganingizni hafta oxirida 📦 Ombor → «📝 Hisobot berish» orqali yozasiz.")
    for b in bosses:
        await send(bot, b, f"✅ {st.full_name} {it.name} {_n(it, mv.qty)} ni qabul qildi.")


@router.callback_query(F.data.startswith("sk:got:"))
async def got_other(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Sk.got)
    await state.update_data(mid=int(c.data.split(":")[2]))
    await c.answer()
    await c.message.answer("Aslida nechta keldi? Raqam yozing:")


@router.message(Sk.got)
async def got_save(m: Message, state: FSMContext, bot: Bot) -> None:
    t = (m.text or "").replace(" ", "")
    if not t.isdigit():
        await m.answer("Raqam yozing: 480")
        return
    d = await state.get_data()
    await state.clear()
    q = int(t)
    async with Session() as s:
        st = await _staff(s, m.from_user.id)
        mv = await s.get(StockMove, d["mid"])
        if not mv or mv.holder_id != st.id or mv.confirmed:
            await m.answer("Bu berish allaqachon hal qilingan.")
            return
        it = await s.get(StockItem, mv.item_id)
        if q > mv.qty:
            await m.answer(f"Berilgandan ({mv.qty}) ko'p bo'lishi mumkin emas.")
            return
        out = await s.get(StockMove, mv.pair_id) if mv.pair_id else None
        sent = mv.qty
        mv.qty, mv.confirmed = q, True
        if out:
            out.qty = -q
        await S.audit(s, st, "ombor_kam_keldi", item=it.name, sent=sent, got=q)
        await s.commit()
        bosses = await _bosses(s)
    await m.answer(f"✅ Qabul qildingiz: {it.name} {_n(it, q)} (berilgan edi {sent}).")
    for b in bosses:
        await send(bot, b, f"⚠️ {st.full_name}: {it.name} {sent} ta berildi, <b>{q}</b> ta qabul qildi "
                           f"(farq {sent - q}). Tekshiring.")


# ---------------- xodim hisoboti: ichki / sotuv / qoldiq → bot tekshiradi ----------------
@router.callback_query(F.data == "sk:report")
async def report_start(c: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        items = [it.id for it in (await s.execute(select(StockItem).where(StockItem.active).order_by(StockItem.name))).scalars()
                 if await balance(s, it.id, st.id)]
    if not items:
        return await c.answer("Hisobingizda tovar yo'q.", show_alert=True)
    await state.set_state(Sk.rep)
    await state.update_data(queue=items, step=0, ans={})
    await c.answer()
    await _report_ask(c.message, state)


async def _report_ask(msg: Message, state: FSMContext) -> None:
    d = await state.get_data()
    iid = d["queue"][0]
    async with Session() as s:
        it = await s.get(StockItem, iid)
    q = {0: f"📝 <b>{it.name}</b> (1/3)\nOxirgi hisobotdan beri <b>ichki ishlarga</b> nechta ishlatildi "
            f"(test, hujjat, o'qituvchilar, buzilgani ham)? Yo'q bo'lsa 0 yozing:",
         1: f"📝 <b>{it.name}</b> (2/3)\n<b>Sotuvga</b> nechta ketdi"
            + (" (pullik chop etish sahifalari)" if it.paper else "") + "? Yo'q bo'lsa 0:",
         2: f"📝 <b>{it.name}</b> (3/3)\nHozir <b>qo'lingizda nechta bor</b>? Sanab yozing:"}[d["step"]]
    await msg.answer(q)


@router.message(Sk.rep)
async def report_answer(m: Message, state: FSMContext, bot: Bot) -> None:
    t = (m.text or "").replace(" ", "")
    if not t.isdigit():
        await m.answer("Raqam yozing (yo'q bo'lsa 0).")
        return
    d = await state.get_data()
    ans = d["ans"]
    ans[str(d["step"])] = int(t)
    if d["step"] < 2:
        await state.update_data(ans=ans, step=d["step"] + 1)
        await _report_ask(m, state)
        return
    iid = d["queue"][0]
    inner, sold, left = ans["0"], ans["1"], ans["2"]
    async with Session() as s:
        st = await _staff(s, m.from_user.id)
        it = await s.get(StockItem, iid)
        start = await balance(s, iid, st.id)
        prev = await last_report(s, iid, st.id)
        since = prev.created_at if prev else None
        rec_sold, rec_inner = await recorded(s, it, st.id, since)
        expect = start - inner - sold
        diff = left - expect
        for kind, q in (("ichki", -inner), ("sotuv", -sold), ("sanoq", diff)):
            if q:
                s.add(StockMove(item_id=iid, holder_id=st.id, qty=q, kind=kind, by_id=st.id))
        warn = []
        if diff:
            warn.append(f"Qoldiqda farq: kutilgan {expect}, sanaldi {left} → <b>{diff:+d}</b> {it.unit}")
        if rec_sold is not None and rec_sold != sold:
            warn.append(f"Sotuv: siz {sold} dedingiz, 🛒 Xizmatlarda {rec_sold} ta sotuv yozilgan"
                        + (f" — {sold - rec_sold} tasining puli kiritilmagan bo'lishi mumkin" if sold > rec_sold else ""))
        if it.paper and rec_inner and rec_inner != inner:
            warn.append(f"Ichki: siz {inner} dedingiz, «Ichki chop etish»da {rec_inner} sahifa yozilgan")
        note = f"ichki {inner}, sotuv {sold}, qoldiq {left}, farq {diff}"
        s.add(StockMove(item_id=iid, holder_id=st.id, qty=0, kind="hisobot", by_id=st.id, note=note))
        await S.audit(s, st, "ombor_hisobot", item=it.name, inner=inner, sold=sold, left=left, diff=diff)
        await s.commit()
        bosses = await _bosses(s)
    period = f"{_d(since)} dan beri" if since else "boshidan"
    body = (f"{it.name} ({period}):\n• Hisobda edi: {_n(it, start)}\n• Ichki ishlarga: {inner}\n• Sotuvga: {sold}"
            + (f" (🛒 Xizmatlarda: {rec_sold})" if rec_sold is not None else "")
            + f"\n• Qo'lda qoldi: {_n(it, left)}")
    await m.answer(("⚠️ " if warn else "✅ ") + "Hisobot qabul qilindi\n" + body
                   + ("\n\n" + "\n".join("⚠️ " + w for w in warn) if warn else "\nHammasi to'g'ri."))
    for b in bosses:
        await send(bot, b, ("⚠️ " if warn else "✅ ") + f"<b>Ombor hisoboti</b> · {st.full_name}\n" + body
                   + ("\n\n" + "\n".join("⚠️ " + w for w in warn) if warn else "\nFarq yo'q."))
    rest = d["queue"][1:]
    if rest:
        await state.update_data(queue=rest, step=0, ans={})
        await _report_ask(m, state)
    else:
        await state.clear()


# ---------------- oylik hisobot va tarix (rahbar) ----------------
def _month_bounds(back: int) -> tuple[datetime, datetime, str]:
    t = datetime.now(TZ).date().replace(day=1)
    for _ in range(back):
        t = (t - timedelta(days=1)).replace(day=1)
    nxt = (t + timedelta(days=32)).replace(day=1)
    to_utc = lambda d: datetime(d.year, d.month, d.day) - timedelta(hours=5)
    return to_utc(t), to_utc(nxt), f"{t:%m.%Y}"


async def report_text(s, back: int = 0) -> str:
    a, b, label = _month_bounds(back)
    items = {i.id: i for i in (await s.execute(select(StockItem))).scalars()}
    staff = {x.id: x.full_name for x in (await s.execute(select(Staff))).scalars()}
    rows = (await s.execute(select(StockMove.holder_id, StockMove.item_id, StockMove.kind, func.sum(StockMove.qty))
                            .where(StockMove.confirmed, StockMove.holder_id.is_not(None), StockMove.created_at >= a,
                                   StockMove.created_at < b)
                            .group_by(StockMove.holder_id, StockMove.item_id, StockMove.kind))).all()
    end = {(i, h): int(q) for i, h, q in (await s.execute(
        select(StockMove.item_id, StockMove.holder_id, func.sum(StockMove.qty)).where(
            StockMove.confirmed, StockMove.created_at < b).group_by(StockMove.item_id, StockMove.holder_id))).all()}
    data: dict = {}
    for h, i, k, q in rows:
        data.setdefault(h, {}).setdefault(i, {})[k] = int(q)
    out = [f"📊 <b>Ombor hisoboti · {label}</b>"]
    if not data:
        out.append("Bu oyda harakat yo'q.")
    tot_loss = 0
    for h in sorted(data, key=lambda x: staff.get(x, "")):
        out.append(f"\n👤 <b>{staff.get(h, '?')}</b>")
        for i, ks in data[h].items():
            it = items[i]
            parts = [f"oldi {ks['berildi']}" if ks.get("berildi") else None,
                     f"ichki {-ks['ichki']}" if ks.get("ichki") else None,
                     f"sotuv {-ks['sotuv']}" if ks.get("sotuv") else None,
                     f"qaytardi {-ks['qaytdi']}" if ks.get("qaytdi") else None,
                     f"⚠️ farq {ks['sanoq']:+d}" if ks.get("sanoq") else None]
            if ks.get("sanoq", 0) < 0:
                tot_loss += -ks["sanoq"]
            out.append(f"• {it.name}: " + (", ".join(p for p in parts if p) or "—")
                       + f" → qoldiq <b>{_n(it, end.get((i, h), 0))}</b>")
    if tot_loss:
        out.append(f"\n⚠️ Oy davomida hisobsiz kamomad jami: {tot_loss}")
    return "\n".join(out)


@router.callback_query(F.data.startswith("sk:rep:"))
async def report(c: CallbackQuery) -> None:
    back = int(c.data.split(":")[2])
    async with Session() as s:
        if (await _staff(s, c.from_user.id)).current_role != "rahbar":
            return await c.answer("Faqat rahbar.", show_alert=True)
        text = await report_text(s, back)
    await c.answer()
    await c.message.answer(text, reply_markup=IM(inline_keyboard=[[
        IB(text="⬅️ Oldingi oy", callback_data=f"sk:rep:{back + 1}")] + (
        [IB(text="Keyingi oy ➡️", callback_data=f"sk:rep:{back - 1}")] if back else [])]))


@router.callback_query(F.data == "sk:log")
async def log(c: CallbackQuery) -> None:
    async with Session() as s:
        if (await _staff(s, c.from_user.id)).current_role != "rahbar":
            return await c.answer("Faqat rahbar.", show_alert=True)
        mvs = list((await s.execute(select(StockMove).where(StockMove.holder_id.is_not(None))
                                    .order_by(StockMove.id.desc()).limit(30))).scalars())
        items = {i.id: i for i in (await s.execute(select(StockItem))).scalars()}
        staff = {x.id: x.full_name for x in (await s.execute(select(Staff))).scalars()}
    lines = ["🧾 <b>Oxirgi harakatlar</b>"]
    for mv in mvs:
        it = items.get(mv.item_id)
        lab = "📝 hisobot" if mv.kind == "hisobot" else KIND.get(mv.kind, mv.kind)
        lines.append(f"{_d(mv.created_at)} · {staff.get(mv.holder_id, '?')} · {it.name if it else '?'} "
                     + ("" if mv.kind == "hisobot" else f"{mv.qty:+d} ") + lab + ("" if mv.confirmed else " ⏳")
                     + (f" — {mv.note}" if mv.note else ""))
    await c.answer()
    await c.message.answer("\n".join(lines))


# ---------------- eslatmalar ----------------
async def month_end_count(bot: Bot) -> None:
    """Shanba 16:00 va oy oxiri: hisobida tovar bor xodimlarga — hisobot bering."""
    async with Session() as s:
        bal = await balances(s)
        holders = {h for (_, h), q in bal.items() if h is not None and q}
        tgs = [(await s.get(Staff, h)).tg_id for h in holders]
    for tg in tgs:
        if tg:
            await send(bot, tg, "📝 Ombor hisobotini bering: 📦 Ombor → «📝 Hisobot berish» "
                                "(ichki ishlarga nechta, sotuvga nechta, qo'lda nechta qoldi).")


async def month_report(bot: Bot) -> None:
    async with Session() as s:
        if not (await s.execute(select(StockMove.id).limit(1))).first():
            return
        text = await report_text(s, 1)
        bosses = await _bosses(s)
    for b in bosses:
        await send(bot, b, text)
