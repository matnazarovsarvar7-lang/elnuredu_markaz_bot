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
    note = State()
    count = State()
    new = State()
    got = State()


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


# ---------------- sotuv va chop etishdan avtomatik sarf (finance.py chaqiradi) ----------------
async def on_sale(s, staff: Staff, service: Service, qty: int, sale_id: int) -> tuple[bool, str]:
    """Sotuvda: kitob — bog'langan tovar sotuvchining hisobidan; printer — qog'oz. (ruxsat, izoh)"""
    holder = None if staff.current_role == "rahbar" else staff.id
    if service.kind == "printer":
        item = (await s.execute(select(StockItem).where(StockItem.paper, StockItem.active).order_by(StockItem.id))).scalars().first()
        if not item:
            return True, ""
        left = await balance(s, item.id, holder) - qty
        s.add(StockMove(item_id=item.id, holder_id=holder, qty=-qty, kind="sotuv", sale_id=sale_id, by_id=staff.id,
                        note=service.name))
        return True, (f"\n📦 {item.name}: hisobingizda {_n(item, left)}" + (" ⚠️" if left < 0 else ""))
    item = (await s.execute(select(StockItem).where(StockItem.service_id == service.id, StockItem.active))).scalars().first()
    if not item:
        return True, ""
    have = await balance(s, item.id, holder)
    if have < qty:
        return False, (f"Hisobingizda «{item.name}» {_n(item, have)} bor — {qty} ta sotib bo'lmaydi. "
                       "Rahbardan oling (📦 Ombor).")
    s.add(StockMove(item_id=item.id, holder_id=holder, qty=-qty, kind="sotuv", sale_id=sale_id, by_id=staff.id,
                    note=service.name))
    return True, f"\n📦 Hisobingizda qoldi: {_n(item, have - qty)}"


async def sell_block(tg: int, service_id: int, qty: int) -> str | None:
    """Sotuvdan oldin: kitob sotuvchining hisobida yetarlimi? Yetmasa — sabab matni."""
    async with Session() as s:
        st = await _staff(s, tg)
        item = (await s.execute(select(StockItem).where(StockItem.service_id == service_id, StockItem.active))).scalars().first()
        if not item or not st:
            return None
        have = await balance(s, item.id, None if st.current_role == "rahbar" else st.id)
    if have < qty:
        return f"Hisobingizda «{item.name}» {_n(item, have)} bor — {qty} ta sotib bo'lmaydi. Rahbardan oling (📦 Ombor)."
    return None


async def on_sale_void(s, sale_id: int) -> None:
    """Sotuv bekor qilinsa — tovar sotuvchining hisobiga qaytadi."""
    for mv in (await s.execute(select(StockMove).where(StockMove.sale_id == sale_id, StockMove.kind == "sotuv"))).scalars():
        s.add(StockMove(item_id=mv.item_id, holder_id=mv.holder_id, qty=-mv.qty, kind="tuzatish",
                        note=f"sotuv #{sale_id} bekor", by_id=mv.by_id))


async def on_print(s, staff: Staff, pages: int, category: str) -> str:
    item = (await s.execute(select(StockItem).where(StockItem.paper, StockItem.active).order_by(StockItem.id))).scalars().first()
    if not item:
        return ""
    holder = None if staff.current_role == "rahbar" else staff.id
    left = await balance(s, item.id, holder) - pages
    s.add(StockMove(item_id=item.id, holder_id=holder, qty=-pages, kind="isrof" if category == "isrof" else "ichki",
                    by_id=staff.id, note=f"chop etish: {category}"))
    return f"\n📦 {item.name}: hisobingizda {_n(item, left)}" + (" ⚠️" if left < 0 else "")


# ---------------- menyu ----------------
@router.message(F.text == K.B_STOCK)
async def stock_menu(m: Message, state: FSMContext) -> None:
    await state.clear()
    async with Session() as s:
        st = await _staff(s, m.from_user.id)
        if not st or st.current_role not in ("rahbar",) + HOLDER_ROLES:
            return
        text, kb = await (_boss_view(s) if st.current_role == "rahbar" else _holder_view(s, st))
    await m.answer(text, reply_markup=kb)


async def _boss_view(s) -> tuple[str, IM]:
    items = list((await s.execute(select(StockItem).where(StockItem.active).order_by(StockItem.name))).scalars())
    bal = await balances(s)
    staff = {x.id: x for x in (await s.execute(select(Staff))).scalars()}
    lines = ["📦 <b>Ombor va hisobdorlik</b>", ""]
    if not items:
        lines.append("Hali tovar yo'q. «⚙️ Tovarlar» → «➕ Yangi tovar» dan boshlang (masalan: A4 qog'oz, kitoblar).")
    else:
        lines.append("<b>🏢 Markaz omborida:</b>")
        for it in items:
            q = bal.get((it.id, None), 0)
            lines.append(f"• {it.name} — {_n(it, q)}" + (" ⚠️ kam" if it.min_left and q <= it.min_left else ""))
        holders = sorted({h for (_, h) in bal if h is not None})
        pend = await pending(s)
        if holders or pend:
            lines += ["", "<b>👥 Xodimlar hisobida:</b>"]
        for h in holders:
            parts = [f"{it.name} {_n(it, bal[(it.id, h)])}" + (" ⚠️" if bal[(it.id, h)] < 0 else "")
                     for it in items if bal.get((it.id, h))]
            if parts:
                lines.append(f"• <b>{staff[h].full_name}</b>: " + " · ".join(parts))
        for mv in pend:
            it = next((i for i in items if i.id == mv.item_id), None)
            if it:
                lines.append(f"⏳ {staff[mv.holder_id].full_name}: {it.name} {_n(it, mv.qty)} — qabul qilishi kutilmoqda")
    kb = IM(inline_keyboard=[
        [IB(text="📤 Xodimga berish", callback_data="sk:give"), IB(text="📥 Omborga kirim", callback_data="sk:in")],
        [IB(text="↩️ Xodimdan qaytarib olish", callback_data="sk:back")],
        [IB(text="📊 Oylik hisobot", callback_data="sk:rep:0"), IB(text="🧾 Harakatlar", callback_data="sk:log")],
        [IB(text="⚙️ Tovarlar", callback_data="sk:items")]])
    return "\n".join(lines), kb


async def _holder_view(s, st: Staff) -> tuple[str, IM]:
    items = list((await s.execute(select(StockItem).where(StockItem.active).order_by(StockItem.name))).scalars())
    lines = ["📦 <b>Mening hisobimdagi tovarlar</b>", ""]
    have = []
    for it in items:
        q = await balance(s, it.id, st.id)
        if q:
            have.append(it)
            lines.append(f"• {it.name} — <b>{_n(it, q)}</b>" + (" ⚠️ minus — rahbarga ayting" if q < 0 else ""))
    pend = await pending(s, st.id)
    for mv in pend:
        it = next((i for i in items if i.id == mv.item_id), None)
        if it:
            lines.append(f"⏳ Sizga berilgan: {it.name} {_n(it, mv.qty)} — qabul qiling")
    if not have and not pend:
        lines.append("Hisobingizda tovar yo'q.")
    lines += ["", "Kitob va chop etish sotuvi «🛒 Xizmatlar» orqali kiritiladi — hisobingizdan o'zi ayiriladi. "
                  "Pulsiz ishlatilganini «➖ Sarf» bilan yozing."]
    kb = [[IB(text=f"✅ Qabul qilish: {next(i.name for i in items if i.id == mv.item_id)} {mv.qty}",
              callback_data=f"sk:acc:{mv.id}")] for mv in pend if any(i.id == mv.item_id for i in items)]
    if have:
        kb += [[IB(text="➖ Sarf (pulsiz ishlatildi)", callback_data="sk:use")],
               [IB(text="🧮 Sanoq (qo'limdagini sanadim)", callback_data="sk:cnt")],
               [IB(text="↩️ Omborga qaytarish", callback_data="sk:ret")]]
    return "\n".join(lines), IM(inline_keyboard=kb or [[IB(text="—", callback_data="sk:none")]])


@router.callback_query(F.data == "sk:none")
async def _none(c: CallbackQuery) -> None:
    await c.answer()


# ---------------- tovarlar ----------------
@router.callback_query(F.data == "sk:items")
async def items_view(c: CallbackQuery) -> None:
    async with Session() as s:
        if (await _staff(s, c.from_user.id)).current_role != "rahbar":
            return await c.answer("Faqat rahbar.", show_alert=True)
        items = list((await s.execute(select(StockItem).order_by(StockItem.name))).scalars())
        svc = {v.id: v.name for v in (await s.execute(select(Service))).scalars()}
    lines = ["⚙️ <b>Tovarlar</b>"]
    for it in items:
        tag = ("🖨 chop etishda sarflanadi" if it.paper else f"🛒 sotuv: {svc.get(it.service_id)}" if it.service_id else
               "faqat ichki")
        lines.append(f"• {it.name} ({it.unit}) — {tag}" + (f" · ogohlantirish ≤ {it.min_left}" if it.min_left else "")
                     + ("" if it.active else " · yashirilgan"))
    await c.answer()
    await c.message.answer("\n".join(lines), reply_markup=IM(inline_keyboard=[
        [IB(text="➕ Yangi tovar", callback_data="sk:new")]]))


@router.callback_query(F.data == "sk:new")
async def item_new(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Sk.new)
    await state.update_data(step="name")
    await c.answer()
    await c.message.answer("➕ <b>Yangi tovar</b> (1/3)\nTovar nomini yozing, masalan: <code>A4 qog'oz</code> yoki "
                           "<code>Biologiya kitobi</code>")


@router.message(Sk.new)
async def item_new_name(m: Message, state: FSMContext) -> None:
    name = (m.text or "").strip()[:80]
    if not name:
        await m.answer("Nomini yozing.")
        return
    await state.update_data(name=name)
    await m.answer(f"«{name}» (2/3) — qanday o'lchanadi?", reply_markup=IM(inline_keyboard=[
        [IB(text=u, callback_data=f"sknu:{u}") for u in ("varaq", "dona", "pachka")],
        [IB(text=u, callback_data=f"sknu:{u}") for u in ("quti", "litr", "kg")]]))


@router.callback_query(Sk.new, F.data.startswith("sknu:"))
async def item_new_unit(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(unit=c.data.split(":", 1)[1])
    async with Session() as s:
        books = list((await s.execute(select(Service).where(Service.active, Service.kind == "kitob")
                                      .order_by(Service.name))).scalars())
    rows = [[IB(text="🖨 Qog'oz — chop etishda sarflanadi", callback_data="sknk:qogoz")],
            [IB(text="🏢 Faqat ichki ish uchun (marker, kartrij...)", callback_data="sknk:ichki")]]
    rows += [[IB(text=f"🛒 Sotiladi: {v.name}", callback_data=f"sknk:{v.id}")] for v in books]
    await c.answer()
    await c.message.edit_text("(3/3) Bu tovar nima uchun?"
                              + ("" if books else "\n\n<i>Kitob sotish uchun avval 🛒 Xizmatlar → ➕ da kitobni narxi "
                                                   "bilan qo'shing, keyin bu yerda tanlaysiz.</i>"),
                              reply_markup=IM(inline_keyboard=rows))


@router.callback_query(Sk.new, F.data.startswith("sknk:"))
async def item_new_kind(c: CallbackQuery, state: FSMContext) -> None:
    kind = c.data.split(":")[1]
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        svc = await s.get(Service, int(kind)) if kind.isdigit() else None
        it = StockItem(name=d["name"], unit=d["unit"], paper=kind == "qogoz", service_id=svc.id if svc else None,
                       min_left=200 if kind == "qogoz" else 5)
        s.add(it)
        moved = 0
        if svc and svc.stock:                     # eski «omborda N ta» — markaz omboriga o'tkaziladi
            await s.flush()
            moved = svc.stock
            s.add(StockMove(item_id=it.id, holder_id=None, qty=svc.stock, kind="kirim", by_id=st.id,
                            note="Xizmatlardagi eski qoldiq"))
            svc.stock = None
        await S.audit(s, st, "ombor_tovar", name=d["name"])
        await s.commit()
        iid = it.id
    await c.answer("Qo'shildi")
    await c.message.edit_text(f"✅ Tovar qo'shildi: <b>{d['name']}</b> ({d['unit']})"
                              + (f"\nXizmatlardagi {moved} ta qoldiq markaz omboriga o'tkazildi." if moved else "")
                              + f"\nOmborda {it.min_left} tadan kam qolsa — ogohlantiraman.\n\nKeyingi qadam: sonini kiriting.",
                              reply_markup=IM(inline_keyboard=[[IB(text="📥 Omborga kirim", callback_data=f"skin:{iid}")]]))


# ---------------- tanlash yordamchilari ----------------
async def _pick_item(c: CallbackQuery, nxt: str, holder: int | None = None, only_have: bool = False) -> None:
    async with Session() as s:
        items = list((await s.execute(select(StockItem).where(StockItem.active).order_by(StockItem.name))).scalars())
        rows = []
        for it in items:
            q = await balance(s, it.id, holder)
            if only_have and q <= 0:
                continue
            rows.append([IB(text=f"{it.name} — {_n(it, q)}", callback_data=f"{nxt}:{it.id}")])
    await c.answer()
    if not rows:
        if only_have and holder is not None:
            await c.message.answer("Hisobingizda tovar yo'q.")
        elif only_have and items:
            await c.message.answer("Markaz omborida tovar qolmagan. Avval «📥 Omborga kirim» qiling.",
                                   reply_markup=IM(inline_keyboard=[[IB(text="📥 Omborga kirim", callback_data="sk:in")]]))
        else:
            await c.message.answer("Hali tovar qo'shilmagan. Avval tovarni ro'yxatga qo'shing (masalan: A4 qog'oz, "
                                   "kitob) — keyin kirim va berish ishlaydi.",
                                   reply_markup=IM(inline_keyboard=[[IB(text="➕ Yangi tovar qo'shish", callback_data="sk:new")]]))
        return
    await c.message.answer("Qaysi tovar?", reply_markup=IM(inline_keyboard=rows))


async def _ask_qty(c: CallbackQuery, state: FSMContext, op: str, **data) -> None:
    await state.set_state(Sk.qty)
    await state.update_data(op=op, **data)
    async with Session() as s:
        it = await s.get(StockItem, data["item"])
    await c.message.edit_text(f"{it.name}: nechta ({it.unit})? Raqam yozing:")


# ---------------- rahbar: kirim / berish / qaytarib olish ----------------
@router.callback_query(F.data == "sk:in")
async def give_in(c: CallbackQuery) -> None:
    await _pick_item(c, "skin")


@router.callback_query(F.data.startswith("skin:"))
async def give_in_item(c: CallbackQuery, state: FSMContext) -> None:
    await _ask_qty(c, state, "kirim", item=int(c.data.split(":")[1]))


@router.callback_query(F.data == "sk:give")
async def give(c: CallbackQuery) -> None:
    await _pick_item(c, "skg", None, only_have=True)


@router.callback_query(F.data.startswith("skg:"))
async def give_item(c: CallbackQuery) -> None:
    iid = int(c.data.split(":")[1])
    async with Session() as s:
        hs = await _holders(s)
    await c.answer()
    await c.message.edit_text("Kimga?", reply_markup=IM(inline_keyboard=[
        [IB(text=f"{h.full_name} ({', '.join(r for r in HOLDER_ROLES if r in h.roles)})", callback_data=f"skgh:{iid}:{h.id}")]
        for h in hs]))


@router.callback_query(F.data.startswith("skgh:"))
async def give_holder(c: CallbackQuery, state: FSMContext) -> None:
    _, iid, hid = c.data.split(":")
    await _ask_qty(c, state, "berish", item=int(iid), holder=int(hid))


@router.callback_query(F.data == "sk:back")
async def take_back(c: CallbackQuery) -> None:
    async with Session() as s:
        hs = await _holders(s)
    await c.answer()
    await c.message.answer("Kimdan?", reply_markup=IM(inline_keyboard=[
        [IB(text=h.full_name, callback_data=f"skb:{h.id}")] for h in hs]))


@router.callback_query(F.data.startswith("skb:"))
async def take_back_holder(c: CallbackQuery) -> None:
    await _pick_item(c, f"skbi:{c.data.split(':')[1]}", int(c.data.split(":")[1]), only_have=True)


@router.callback_query(F.data.startswith("skbi:"))
async def take_back_item(c: CallbackQuery, state: FSMContext) -> None:
    _, hid, iid = c.data.split(":")
    await _ask_qty(c, state, "olish", item=int(iid), holder=int(hid))


# ---------------- xodim: sarf / sanoq / qaytarish / qabul ----------------
@router.callback_query(F.data == "sk:use")
async def use(c: CallbackQuery) -> None:
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
    await _pick_item(c, "sku", st.id, only_have=True)


@router.callback_query(F.data.startswith("sku:"))
async def use_item(c: CallbackQuery) -> None:
    iid = c.data.split(":")[1]
    await c.answer()
    await c.message.edit_text("Nimaga ishlatildi?", reply_markup=IM(inline_keyboard=[
        [IB(text=v, callback_data=f"skuc:{iid}:{k}")] for k, v in USE_CATS.items()]))


@router.callback_query(F.data.startswith("skuc:"))
async def use_cat(c: CallbackQuery, state: FSMContext) -> None:
    _, iid, cat = c.data.split(":")
    await _ask_qty(c, state, cat, item=int(iid))


@router.callback_query(F.data == "sk:ret")
async def ret(c: CallbackQuery) -> None:
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
    await _pick_item(c, "skr", st.id, only_have=True)


@router.callback_query(F.data.startswith("skr:"))
async def ret_item(c: CallbackQuery, state: FSMContext) -> None:
    await _ask_qty(c, state, "qaytarish", item=int(c.data.split(":")[1]))


@router.callback_query(F.data == "sk:cnt")
async def cnt(c: CallbackQuery) -> None:
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
    await _pick_item(c, "skc", st.id)


@router.callback_query(F.data.startswith("skc:"))
async def cnt_item(c: CallbackQuery, state: FSMContext) -> None:
    await _ask_qty(c, state, "sanoq", item=int(c.data.split(":")[1]))


@router.message(Sk.qty)
async def qty_entered(m: Message, state: FSMContext, bot: Bot) -> None:
    t = (m.text or "").replace(" ", "")
    d = await state.get_data()
    if not t.isdigit() or (int(t) <= 0 and d["op"] != "sanoq"):
        await m.answer("Raqam yozing: 500")
        return
    q = int(t)
    if d["op"] in ("ichki", "isrof"):
        await state.update_data(q=q)
        await state.set_state(Sk.note)
        await m.answer("Qisqa izoh: nimaga ishlatildi?" if d["op"] == "ichki" else "Nima bo'ldi (sababi)?")
        return
    await state.clear()
    await _apply(m, bot, d, q, "")


@router.message(Sk.note)
async def note_entered(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    await state.clear()
    await _apply(m, bot, d, d["q"], (m.text or "").strip()[:150])


async def _apply(m: Message, bot: Bot, d: dict, q: int, note: str) -> None:
    op, iid = d["op"], d["item"]
    notify: list[tuple[int, str, IM | None]] = []
    async with Session() as s:
        st = await _staff(s, m.from_user.id)
        it = await s.get(StockItem, iid)
        if op == "kirim":
            have = await balance(s, iid, None)
            s.add(StockMove(item_id=iid, holder_id=None, qty=q, kind="kirim", by_id=st.id, note=note or None))
            msg = f"✅ Omborga kirim: {it.name} +{_n(it, q)}. Omborda: {_n(it, have + q)}"
        elif op == "berish":
            have = await balance(s, iid, None)
            if q > have:
                await m.answer(f"Omborda faqat {_n(it, have)} bor. Avval «📥 Omborga kirim» qiling.")
                return
            out = StockMove(item_id=iid, holder_id=None, qty=-q, kind="berildi", by_id=st.id)
            s.add(out)
            await s.flush()
            inc = StockMove(item_id=iid, holder_id=d["holder"], qty=q, kind="berildi", by_id=st.id, pair_id=out.id,
                            confirmed=False)
            s.add(inc)
            await s.flush()
            out.pair_id = inc.id
            h = await s.get(Staff, d["holder"])
            msg = f"📤 {h.full_name}ga {it.name} {_n(it, q)} berildi. U qabul qilganini tasdiqlaydi."
            if h.tg_id:
                notify.append((h.tg_id, f"📦 Rahbar sizga <b>{it.name} {_n(it, q)}</b> berdi. Sanab, qabul qiling:",
                               IM(inline_keyboard=[[IB(text="✅ To'liq qabul qildim", callback_data=f"sk:acc:{inc.id}")],
                                                   [IB(text="✏️ Boshqa son keldi", callback_data=f"sk:got:{inc.id}")]])))
        elif op == "olish":
            have = await balance(s, iid, d["holder"])
            q = min(q, have)
            out = StockMove(item_id=iid, holder_id=d["holder"], qty=-q, kind="qaytdi", by_id=st.id)
            s.add(out)
            await s.flush()
            s.add(StockMove(item_id=iid, holder_id=None, qty=q, kind="qaytdi", by_id=st.id, pair_id=out.id))
            h = await s.get(Staff, d["holder"])
            msg = f"↩️ {h.full_name}dan {it.name} {_n(it, q)} qaytarib olindi."
            if h.tg_id:
                notify.append((h.tg_id, f"↩️ Rahbar sizdan {it.name} {_n(it, q)} qaytarib oldi.", None))
        elif op == "qaytarish":
            have = await balance(s, iid, st.id)
            if q > have:
                await m.answer(f"Hisobingizda {_n(it, have)} bor.")
                return
            out = StockMove(item_id=iid, holder_id=st.id, qty=-q, kind="qaytdi", by_id=st.id)
            s.add(out)
            await s.flush()
            s.add(StockMove(item_id=iid, holder_id=None, qty=q, kind="qaytdi", by_id=st.id, pair_id=out.id))
            msg = f"↩️ Omborga qaytarildi: {it.name} {_n(it, q)}. Hisobingizda: {_n(it, have - q)}"
            for b in await _bosses(s):
                notify.append((b, f"↩️ {st.full_name} omborga {it.name} {_n(it, q)} qaytardi.", None))
        elif op in ("ichki", "isrof"):
            have = await balance(s, iid, st.id)
            if q > have:
                await m.answer(f"Hisobingizda {_n(it, have)} bor — {q} ta yozib bo'lmaydi.")
                return
            s.add(StockMove(item_id=iid, holder_id=st.id, qty=-q, kind=op, by_id=st.id, note=note or None))
            msg = f"✅ Qayd etildi: {it.name} −{_n(it, q)} ({KIND[op]}). Hisobingizda: {_n(it, have - q)}"
            if op == "isrof":
                for b in await _bosses(s):
                    notify.append((b, f"🗑 {st.full_name}: {it.name} {_n(it, q)} isrof — {note}", None))
        else:                                       # sanoq
            have = await balance(s, iid, st.id)
            diff = q - have
            if diff:
                s.add(StockMove(item_id=iid, holder_id=st.id, qty=diff, kind="sanoq", by_id=st.id,
                                note=f"hisobda {have}, sanaldi {q}"))
                for b in await _bosses(s):
                    notify.append((b, f"🧮 <b>Sanoqda farq</b> · {st.full_name}\n{it.name}: hisobda {_n(it, have)}, "
                                      f"qo'lda {_n(it, q)} → <b>{'+' if diff > 0 else ''}{diff}</b>", None))
                msg = f"🧮 Farq qayd etildi: {'+' if diff > 0 else ''}{diff} {it.unit}. Rahbarga xabar berildi."
            else:
                msg = f"✅ Sanoq to'g'ri: {it.name} {_n(it, q)}."
        await S.audit(s, st, "ombor_" + op, item=it.name, qty=q)
        await s.commit()
        low = it.min_left and op in ("berish",) and await balance(s, iid, None) <= it.min_left
        if low:
            for b in await _bosses(s):
                notify.append((b, f"⚠️ Markaz omborida {it.name} kam qoldi: {_n(it, await balance(s, iid, None))}", None))
    await m.answer(msg)
    for tg, txt, kb in notify:
        await send(bot, tg, txt, reply_markup=kb)


@router.callback_query(F.data.startswith("sk:acc:"))
async def accept(c: CallbackQuery, bot: Bot) -> None:
    mid = int(c.data.split(":")[2])
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        mv = await s.get(StockMove, mid)
        if not mv or mv.holder_id != (st.id if st else None):
            return await c.answer("Bu sizga berilmagan.", show_alert=True)
        if mv.confirmed:
            return await c.answer("Allaqachon qabul qilingan.")
        mv.confirmed = True
        it = await s.get(StockItem, mv.item_id)
        await s.commit()
        bosses = await _bosses(s)
        left = await balance(s, it.id, st.id)
    await c.answer("Qabul qilindi")
    await c.message.edit_text(f"✅ Qabul qildingiz: {it.name} {_n(it, mv.qty)}. Hisobingizda: {_n(it, left)}")
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
        out = await s.get(StockMove, mv.pair_id) if mv.pair_id else None
        if q > mv.qty:
            await m.answer(f"Berilgandan ({mv.qty}) ko'p bo'lishi mumkin emas. Ortiqchasini rahbarga ayting.")
            return
        sent = mv.qty
        mv.qty, mv.confirmed = q, True
        if out:
            out.qty = -q                              # kelmagan qismi ombordan chiqmagan hisoblanadi
        await S.audit(s, st, "ombor_kam_keldi", item=it.name, sent=sent, got=q)
        await s.commit()
        bosses = await _bosses(s)
    await m.answer(f"✅ Qabul qildingiz: {it.name} {_n(it, q)} (berilgan edi {sent}).")
    for b in bosses:
        await send(bot, b, f"⚠️ {st.full_name}: {it.name} {sent} ta berilgan, <b>{q}</b> ta qabul qildi. "
                           f"Farq {sent - q} ta markaz omborida qoldi deb hisoblandi — tekshiring.")


# ---------------- hisobot va tarix ----------------
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
                            .where(StockMove.confirmed, StockMove.created_at >= a, StockMove.created_at < b)
                            .group_by(StockMove.holder_id, StockMove.item_id, StockMove.kind))).all()
    end = {(i, h): int(q) for i, h, q in (await s.execute(
        select(StockMove.item_id, StockMove.holder_id, func.sum(StockMove.qty)).where(StockMove.confirmed, StockMove.created_at < b)
        .group_by(StockMove.item_id, StockMove.holder_id))).all()}
    from app.db.models import ServiceSale
    money = {}
    for iid, it in items.items():
        if it.service_id:
            money[iid] = int((await s.execute(select(func.coalesce(func.sum(ServiceSale.amount), 0)).where(
                ServiceSale.service_id == it.service_id, ServiceSale.created_at >= a, ServiceSale.created_at < b))).scalar())
    data: dict = {}
    for h, i, k, q in rows:
        data.setdefault(h, {}).setdefault(i, {})[k] = int(q)
    out = [f"📊 <b>Ombor hisoboti · {label}</b>"]
    if not data:
        out.append("Bu oyda harakat yo'q.")
    for h in sorted(data, key=lambda x: (x is not None, staff.get(x, ""))):
        if h is None:
            continue
        out.append(f"\n👤 <b>{staff.get(h, '?')}</b>")
        for i, ks in data[h].items():
            it = items[i]
            got = ks.get("berildi", 0)
            parts = [f"oldi {got}" if got else None,
                     f"sotdi {-ks['sotuv']}" if ks.get("sotuv") else None,
                     f"ichki {-ks['ichki']}" if ks.get("ichki") else None,
                     f"isrof {-ks['isrof']}" if ks.get("isrof") else None,
                     f"qaytardi {-ks['qaytdi']}" if ks.get("qaytdi") else None,
                     f"sanoq farqi {ks['sanoq']:+d}" if ks.get("sanoq") else None]
            out.append(f"• {it.name}: " + ", ".join(p for p in parts if p) + f" → qoldiq <b>{_n(it, end.get((i, h), 0))}</b>")
    main = data.get(None, {})
    out.append("\n🏢 <b>Markaz ombori</b>")
    for i, it in items.items():
        ks = main.get(i, {})
        if not ks and not end.get((i, None)):
            continue
        parts = [f"kirim {ks['kirim']}" if ks.get("kirim") else None,
                 f"berildi {-ks['berildi']}" if ks.get("berildi") else None,
                 f"qaytdi {ks['qaytdi']}" if ks.get("qaytdi") else None,
                 f"sotuv/ishlatish {-(ks.get('sotuv', 0) + ks.get('ichki', 0) + ks.get('isrof', 0))}"
                 if ks.get("sotuv") or ks.get("ichki") or ks.get("isrof") else None]
        out.append(f"• {it.name}: " + (", ".join(p for p in parts if p) or "harakat yo'q")
                   + f" → qoldiq <b>{_n(it, end.get((i, None), 0))}</b>")
    if any(money.values()):
        out.append("\n💰 <b>Sotuvdan tushum</b>")
        for i, v in money.items():
            if v:
                out.append(f"• {items[i].name}: {S.fmt_som(v)}")
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
        mvs = list((await s.execute(select(StockMove).order_by(StockMove.id.desc()).limit(30))).scalars())
        items = {i.id: i for i in (await s.execute(select(StockItem))).scalars()}
        staff = {x.id: x.full_name for x in (await s.execute(select(Staff))).scalars()}
    lines = ["🧾 <b>Oxirgi harakatlar</b>"]
    for mv in mvs:
        if mv.holder_id is None and mv.kind in ("berildi", "qaytdi"):
            continue                                 # juftning ombor tomoni — takrorlamaymiz
        it = items.get(mv.item_id)
        who = "Ombor" if mv.holder_id is None else staff.get(mv.holder_id, "?")
        lines.append(f"{mv.created_at + timedelta(hours=5):%d.%m %H:%M} · {who} · {it.name if it else '?'} "
                     f"{mv.qty:+d} · {KIND.get(mv.kind, mv.kind)}" + ("" if mv.confirmed else " ⏳")
                     + (f" — {mv.note}" if mv.note else ""))
    await c.answer()
    await c.message.answer("\n".join(lines[:31]))


# ---------------- oy oxiri: sanoq eslatmasi, 1-sana: hisobot ----------------
async def month_end_count(bot: Bot) -> None:
    async with Session() as s:
        bal = await balances(s)
        holders = {h for (_, h), q in bal.items() if h is not None and q}
        tgs = [(await s.get(Staff, h)).tg_id for h in holders]
    for tg in tgs:
        if tg:
            await send(bot, tg, "🧮 Oy oxiri: hisobingizdagi tovarlarni sanang — 📦 Ombor → «🧮 Sanoq».")


async def month_report(bot: Bot) -> None:
    async with Session() as s:
        if not (await s.execute(select(StockMove.id).limit(1))).first():
            return
        text = await report_text(s, 1)
        bosses = await _bosses(s)
    for b in bosses:
        await send(bot, b, text)
