"""🛍 Market: o'quvchi coinga sovg'a tanlaydi (coin band), moliya tasdiqlaydi/rad etadi, topshiradi."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.bot import keyboards as K
from app.bot.notify import send, send_photo
from app.config import settings
from app.db.models import Order, Product, Setting, Staff, Student
from app.db.session import Session
from app.logic.money import is_market_debtor, new_student_deadline
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
STAFF = ("moliya", "rahbar")
ST = {"band": "⏳ tasdiq kutmoqda", "tasdiq": "✅ tasdiqlandi — olib ketishingiz mumkin", "rad": "❌ rad etildi",
      "topshirildi": "🎁 topshirildi", "bekor": "➖ bekor qilindi"}


def today() -> date:
    return datetime.now(TZ).date()


async def reserved(s, sid: int) -> int:
    return int((await s.execute(select(func.coalesce(func.sum(Order.coins), 0)).where(
        Order.student_id == sid, Order.status == "band"))).scalar())


async def market_block(s, stu: Student) -> str | None:
    """Xarid qila olmasa — sababi; aks holda None."""
    t = today()
    if stu.status == "ketgan":
        return "O'quvchi markazda o'qimayapti — xarid qilib bo'lmaydi."
    enrs = await S.debt_enrollments(s, stu.id, t.year, t.month)
    for e in enrs:
        st = await S.month_status(s, e, t.year, t.month)
        dl = new_student_deadline(e.registered_at or e.join_date, e.group.days) \
            if (e.join_date.year, e.join_date.month) == (t.year, t.month) else None
        if is_market_debtor(t, st.payable, st.paid, dl):
            return "Shu oy to'lovi to'liq qilinmagan — to'lovdan keyin xarid qilish mumkin."
    for e in enrs:
        y, m = e.join_date.year, e.join_date.month
        while (y, m) < (t.year, t.month):
            if (await S.month_status(s, e, y, m)).debt > 0:
                return "Oldingi oydan qarz bor — qarz yopilgach xarid qilish mumkin."
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return None


# ================= o'quvchi / ota-ona =================
async def _kids(s, tg: int) -> list[Student]:
    who = await S.who_by_tg(s, tg)
    if who.parent:
        return [k for k in who.parent.children if k.status != "ketgan"] or list(who.parent.children)
    return [who.student] if who.student else []


@router.message(F.text == K.B_MARKET)
async def market_open(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if who.staff and who.staff.current_role in STAFF:
            await _staff_market(m)
            return
        kids = await _kids(s, m.from_user.id)
    if not kids:
        return
    if len(kids) > 1:
        await m.answer("Kim uchun?", reply_markup=IM(inline_keyboard=[[IB(text=k.full_name, callback_data=f"mk:{k.id}")]
                                                                       for k in kids]))
        return
    text, kb = await _shop(kids[0].id)
    await m.answer(text, reply_markup=kb)


async def _shop(sid: int) -> tuple[str, IM]:
    async with Session() as s:
        stu = await s.get(Student, sid)
        free = stu.coins - await reserved(s, sid)
        prods = (await s.execute(select(Product).where(Product.active, Product.stock > 0)
                                 .order_by(Product.coins))).scalars().all()
        mine = (await s.execute(select(Order, Product.name).join(Product, Product.id == Order.product_id)
                                .where(Order.student_id == sid, Order.status.in_(("band", "tasdiq")))
                                .order_by(Order.id.desc()))).all()
        block = await market_block(s, stu)
    lines = [f"🛍 <b>Market</b> · {stu.full_name}", f"Coin: <b>{free}</b>" + (f" (yana {stu.coins - free} tasi band)" if free != stu.coins else "")]
    if block:
        lines.append(f"\n⚠️ {block}")
    for o, name in mine:
        lines.append(f"• {name} — {ST[o.status]}")
    rows = []
    if not prods:
        lines.append("\nHozircha sovg'alar yo'q.")
    for p in prods:
        ok = p.coins <= free and not block
        rows.append([IB(text=f"{'🎁' if ok else '🔒'} {p.name} — {p.coins} coin", callback_data=f"mp:{sid}:{p.id}")])
    return "\n".join(lines), IM(inline_keyboard=rows)


@router.callback_query(F.data.startswith("mk:"))
async def shop_for(c: CallbackQuery) -> None:
    text, kb = await _shop(int(c.data.split(":")[1]))
    await c.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("mp:"))
async def product_view(c: CallbackQuery, bot: Bot) -> None:
    _, sid, pid = c.data.split(":")
    async with Session() as s:
        p = await s.get(Product, int(pid))
        stu = await s.get(Student, int(sid))
        free = stu.coins - await reserved(s, stu.id)
        block = await market_block(s, stu)
    if block:
        await c.answer(block, show_alert=True)
        return
    if p.coins > free:
        await c.answer(f"Coin yetmaydi: kerak {p.coins}, sizda {free}. Yana {p.coins - free} coin yig'ing!", show_alert=True)
        return
    kb = IM(inline_keyboard=[[IB(text=f"✅ {p.coins} coin evaziga buyurtma berish", callback_data=f"mo:{sid}:{pid}")]])
    text = f"🎁 <b>{p.name}</b>\nNarxi: {p.coins} coin\nBuyurtmadan keyin {free - p.coins} coin qoladi."
    await c.answer()
    if p.photo_file_id:
        await send_photo(bot, c.message.chat.id, p.photo_file_id, text, reply_markup=kb)
    else:
        await c.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("mo:"))
async def order_make(c: CallbackQuery, bot: Bot) -> None:
    _, sid, pid = c.data.split(":")
    async with Session() as s:
        p = await s.get(Product, int(pid))
        stu = await s.get(Student, int(sid))
        free = stu.coins - await reserved(s, stu.id)
        block = await market_block(s, stu)
        if block or p.coins > free or p.stock <= 0 or not p.active:
            await c.answer(block or "Buyurtma berib bo'lmaydi (coin yoki sovg'a yetarli emas).", show_alert=True)
            return
        o = Order(student_id=stu.id, product_id=p.id, coins=p.coins, from_tg=c.from_user.id)
        s.add(o)
        await s.flush()
        await S.audit(s, None, "market_buyurtma", student=stu.full_name, product=p.name)
        await s.commit()
        oid, name, pname, price = o.id, stu.full_name, p.name, p.coins
        to = [x.tg_id for x in await S.staff_with_role(s, "moliya") if x.tg_id] or \
             [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(f"✅ Buyurtma qabul qilindi: {pname}. {price} coin band qilindi — moliya bo'limi tasdiqlaydi.")
    for t in to:
        await send(bot, t, f"🛍 <b>Yangi buyurtma</b>\n{name}: {pname} ({price} coin)", reply_markup=_order_kb(oid))


def _order_kb(oid: int) -> IM:
    return IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=f"moy:{oid}"),
                                IB(text="❌ Rad etish", callback_data=f"mon:{oid}")]])


# ================= moliya: tasdiq, rad, topshirish =================
class Rj(StatesGroup):
    reason = State()


@router.callback_query(F.data.startswith("moy:"))
async def order_ok(c: CallbackQuery, bot: Bot) -> None:
    oid = int(c.data.split(":")[1])
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role in STAFF):
            await c.answer("Ruxsat yo'q.", show_alert=True)
            return
        o = await s.get(Order, oid)
        if o.status != "band":
            await c.answer("Bu buyurtma allaqachon ko'rib chiqilgan.", show_alert=True)
            return
        p = await s.get(Product, o.product_id)
        stu = await s.get(Student, o.student_id)
        if p.stock <= 0:
            await c.answer("Omborda qolmagan — rad eting yoki omborni to'ldiring.", show_alert=True)
            return
        o.status, o.decided_by = "tasdiq", who.staff.id
        p.stock -= 1
        await S.add_coins(s, stu, -o.coins, "market", f"Market: {p.name}", who.staff)
        await S.audit(s, who.staff, "market_tasdiq", student=stu.full_name, product=p.name)
        await s.commit()
        fam = await S.parent_tg_ids(s, stu.id)
        low = p.stock <= 2
        name, pname, left = stu.full_name, p.name, p.stock
    await c.message.edit_text(c.message.html_text + "\n\n✅ Tasdiqlandi", reply_markup=IM(inline_keyboard=[[
        IB(text="🎁 Topshirildi", callback_data=f"mod:{oid}")]]))
    for t in fam:
        await send(bot, t, f"🎉 {name}: «{pname}» buyurtmasi tasdiqlandi! Sovg'ani moliya bo'limidan olib keting.")
    if low:
        await c.message.answer(f"⚠️ «{pname}» omborda {left} ta qoldi.")


@router.callback_query(F.data.startswith("mod:"))
async def order_delivered(c: CallbackQuery) -> None:
    async with Session() as s:
        o = await s.get(Order, int(c.data.split(":")[1]))
        o.status, o.delivered_at = "topshirildi", datetime.now(TZ).replace(tzinfo=None)
        await s.commit()
    await c.message.edit_text(c.message.html_text + "\n🎁 Topshirildi")


@router.callback_query(F.data.startswith("mon:"))
async def order_no(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Rj.reason)
    await state.update_data(oid=int(c.data.split(":")[1]))
    await c.answer()
    await c.message.answer("Rad etish sababini yozing:")


@router.message(Rj.reason)
async def order_no_save(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    await state.clear()
    reason = (m.text or "").strip()[:200] or "—"
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        o = await s.get(Order, d["oid"])
        if o.status != "band":
            await m.answer("Bu buyurtma allaqachon ko'rib chiqilgan.")
            return
        o.status, o.reason, o.decided_by = "rad", reason, who.staff.id
        p = await s.get(Product, o.product_id)
        stu = await s.get(Student, o.student_id)
        await S.audit(s, who.staff, "market_rad", student=stu.full_name, product=p.name, reason=reason)
        await s.commit()
        fam = await S.parent_tg_ids(s, stu.id)
        name, pname, coins = stu.full_name, p.name, o.coins
    await m.answer("Rad etildi, coin qaytarildi.")
    for t in fam:
        await send(bot, t, f"❌ {name}: «{pname}» buyurtmasi rad etildi ({reason}). {coins} coin qaytarildi.")


# ================= moliya/rahbar: sovg'alar va buyurtmalar =================
async def _staff_market(m: Message) -> None:
    async with Session() as s:
        prods = (await s.execute(select(Product).where(Product.active).order_by(Product.coins))).scalars().all()
        band = (await s.execute(select(func.count(Order.id)).where(Order.status == "band"))).scalar()
        give = (await s.execute(select(func.count(Order.id)).where(Order.status == "tasdiq"))).scalar()
        coins = (await s.execute(select(func.coalesce(func.sum(Student.coins), 0)).where(Student.status != "ketgan"))).scalar()
        rate = await s.get(Setting, "coin_som")
        who = await S.who_by_tg(s, m.from_user.id)
    r = int((rate.value or {}).get("som", 45)) if rate else 45
    lines = ["🛍 <b>Market boshqaruvi</b>", f"Tasdiq kutayotgan: {band} · topshirilishi kerak: {give}"]
    if who.staff.current_role == "rahbar":
        lines.append(f"Muomaladagi coinlar: {coins} (~{S.fmt_som(int(coins) * r)}, 1 coin ≈ {r} so'm)")
    lines.append("\n<b>Sovg'alar</b>")
    lines += [f"• {p.name} — {p.coins} coin · omborda {p.stock}" + (" ⚠️" if p.stock <= 2 else "") for p in prods] or ["—"]
    rows = [[IB(text="📋 Buyurtmalar", callback_data="mso")], [IB(text="➕ Sovg'a qo'shish", callback_data="msa")]]
    rows += [[IB(text=f"✏️ {p.name}", callback_data=f"mse:{p.id}")] for p in prods]
    await m.answer("\n".join(lines), reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data == "mso")
async def orders_list(c: CallbackQuery) -> None:
    async with Session() as s:
        rows = (await s.execute(select(Order, Product.name, Student.full_name).join(Product, Product.id == Order.product_id)
                                .join(Student, Student.id == Order.student_id)
                                .where(Order.status.in_(("band", "tasdiq"))).order_by(Order.id))).all()
    if not rows:
        await c.answer("Ochiq buyurtma yo'q.", show_alert=True)
        return
    await c.answer()
    for o, pname, sname in rows[:30]:
        kb = _order_kb(o.id) if o.status == "band" else IM(inline_keyboard=[[IB(text="🎁 Topshirildi", callback_data=f"mod:{o.id}")]])
        await c.message.answer(f"🛍 {sname}: {pname} ({o.coins} coin) — {ST[o.status]}", reply_markup=kb)


class Pr(StatesGroup):
    name = State()
    price = State()
    stock = State()
    photo = State()
    edit = State()


@router.callback_query(F.data == "msa")
async def prod_add(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Pr.name)
    await c.answer()
    await c.message.answer("Sovg'a nomi (masalan: Daftar 48 varaq):")


@router.message(Pr.name)
async def prod_name(m: Message, state: FSMContext) -> None:
    await state.update_data(name=(m.text or "").strip()[:120])
    await state.set_state(Pr.price)
    await m.answer("Narxi necha coin? (1 coin ≈ 45 so'm: 8 000 so'mlik daftar ≈ 180 coin)")


@router.message(Pr.price)
async def prod_price(m: Message, state: FSMContext) -> None:
    if not (m.text or "").strip().isdigit():
        await m.answer("Raqam yozing: 180")
        return
    await state.update_data(price=int(m.text.strip()))
    await state.set_state(Pr.stock)
    await m.answer("Omborda nechta bor?")


@router.message(Pr.stock)
async def prod_stock(m: Message, state: FSMContext) -> None:
    if not (m.text or "").strip().isdigit():
        await m.answer("Raqam yozing: 20")
        return
    await state.update_data(stock=int(m.text.strip()))
    await state.set_state(Pr.photo)
    await m.answer("Sovg'a rasmini yuboring (ixtiyoriy) yoki «-» yozing.")


@router.message(Pr.photo)
async def prod_photo(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        s.add(Product(name=d["name"], coins=d["price"], stock=d["stock"],
                      photo_file_id=m.photo[-1].file_id if m.photo else None))
        await S.audit(s, who.staff, "market_sovga", name=d["name"], coins=d["price"], stock=d["stock"])
        await s.commit()
    await m.answer(f"✅ «{d['name']}» qo'shildi: {d['price']} coin, omborda {d['stock']} ta.")


@router.callback_query(F.data.startswith("mse:"))
async def prod_edit(c: CallbackQuery, state: FSMContext) -> None:
    pid = int(c.data.split(":")[1])
    await state.set_state(Pr.edit)
    await state.update_data(pid=pid)
    await c.answer()
    await c.message.answer("Nimani o'zgartiramiz? Yozing:\n• <code>narx 200</code>\n• <code>ombor +10</code> (qo'shish) "
                           "yoki <code>ombor 15</code> (sanoq natijasi)\n• <code>yashirish</code>")


@router.message(Pr.edit)
async def prod_edit_save(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    parts = (m.text or "").strip().lower().split()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        p = await s.get(Product, d["pid"])
        if parts[:1] == ["narx"] and len(parts) == 2 and parts[1].isdigit():
            p.coins = int(parts[1])
        elif parts[:1] == ["ombor"] and len(parts) == 2 and parts[1].lstrip("+").isdigit():
            p.stock = p.stock + int(parts[1][1:]) if parts[1].startswith("+") else int(parts[1])
        elif parts[:1] == ["yashirish"]:
            p.active = False
        else:
            await m.answer("Tushunmadim. Masalan: narx 200 · ombor +10 · yashirish")
            return
        await S.audit(s, who.staff, "market_sovga", name=p.name, change=m.text)
        await s.commit()
        txt = f"✅ {p.name}: {p.coins} coin, omborda {p.stock}" + ("" if p.active else " (yashirildi)")
    await state.clear()
    await m.answer(txt)
