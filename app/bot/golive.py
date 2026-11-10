"""🧪 Sinov rejimi va 🚀 Haqiqiy ishni boshlash.

Sinov rejimida: oy avtomatik yopilmaydi (oylik hisoblanmaydi), 1-sanadagi fond taqsimoti bo'lmaydi,
ota-onalarga qarz eslatmasi ketmaydi. Rahbar «🚀 Haqiqiy ishni boshlash» ni bosganda barcha SINOV PUL
yozuvlari o'chiriladi (o'quvchilar, guruhlar, xodimlar, davomat, vazifa, testlar saqlanadi) va bot haqiqiy ishga o'tadi."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM
from sqlalchemy import delete, func, or_, select

from app.db.models import (BonusLedger, CashClose, CashHandover, CoinTx, Expense, KassaMove, Payment, Payout,
                           Payroll, ServiceSale, Setting, Student)
from app.db.session import Session
from app import services as S

router = Router()

WIPE = [(Payment, "to'lov"), (Expense, "xarajat"), (ServiceSale, "xizmat sotuvi"), (CashHandover, "kassa topshirish"),
        (CashClose, "kassa yopish"), (Payout, "oylik/avans"), (Payroll, "oylik hisobi"), (BonusLedger, "bonus yozuvi"),
        (KassaMove, "kassa/fond harakati")]
KEYS = ["cash_opening", "kassa_v2"]
LIKE = ["closed_%", "dist_%", "fund_%"]


async def is_test(s) -> bool:
    row = await s.get(Setting, "sinov")
    return bool(row and (row.value or {}).get("on"))


def coin_filter():
    return or_(CoinTx.kind == "tolov", (CoinTx.kind == "bonus") & CoinTx.reason.like("Bonus %"))


async def counts(s) -> list[tuple[str, int]]:
    out = []
    for model, name in WIPE:
        n = (await s.execute(select(func.count()).select_from(model))).scalar()
        if n:
            out.append((name, n))
    n = (await s.execute(select(func.count()).select_from(CoinTx).where(coin_filter()))).scalar()
    if n:
        out.append(("to'lov uchun berilgan coin", n))
    return out


async def _boss(tg: int):
    async with Session() as s:
        who = await S.who_by_tg(s, tg)
    return who.staff if who.staff and who.staff.current_role == "rahbar" else None


@router.callback_query(F.data == "gl")
async def golive_ask(c: CallbackQuery) -> None:
    if not await _boss(c.from_user.id):
        await c.answer("Faqat rahbar.", show_alert=True)
        return
    async with Session() as s:
        if not await is_test(s):
            await c.answer("Bot allaqachon haqiqiy ishda.", show_alert=True)
            return
        cnt = await counts(s)
    lines = ["🚀 <b>Haqiqiy ishni boshlash</b>", "",
             "Barcha <b>sinov pul yozuvlari o'chiriladi</b>:"] + [f"• {n}: {k} ta" for n, k in cnt] + [
             "", "Kassalar, fondlar, oy yopilishlari ham 0 dan boshlanadi.",
             "✅ Saqlanadi: o'quvchilar, ota-onalar, guruhlar, xodimlar, chegirmalar, davomat, vazifalar, testlar.", "",
             "Keyin: 💼 Kassa → 🔓 Kassalarni ochish va haqiqiy to'lovlarni kiritasiz.",
             "", "⚠️ <b>Bu amalni qaytarib bo'lmaydi.</b>"]
    await c.answer()
    await c.message.answer("\n".join(lines), reply_markup=IM(inline_keyboard=[
        [IB(text="🗑 Ha, sinov pullarini o'chir va boshlash", callback_data="gly")],
        [IB(text="❌ Hozircha emas", callback_data="gln")]]))


@router.callback_query(F.data == "gln")
async def golive_no(c: CallbackQuery) -> None:
    await c.answer()
    await c.message.edit_text("Sinov rejimi davom etmoqda. 🧪")


@router.callback_query(F.data == "gly")
async def golive_do(c: CallbackQuery, state: FSMContext) -> None:
    from app.bot.pin import need_pin
    st = await _boss(c.from_user.id)
    if not st:
        await c.answer("Faqat rahbar.", show_alert=True)
        return
    if await need_pin(c, state):
        return
    async with Session() as s:
        if not await is_test(s):
            await c.answer("Allaqachon bajarilgan.", show_alert=True)
            return
        cnt = await counts(s)
        # to'lov coinlarini o'quvchilardan qaytarib olish
        rows = (await s.execute(select(CoinTx.student_id, func.sum(CoinTx.amount)).where(coin_filter())
                                .group_by(CoinTx.student_id))).all()
        for sid, total in rows:
            stu = await s.get(Student, sid)
            if stu:
                stu.coins = max(0, (stu.coins or 0) - int(total or 0))
        await s.execute(delete(CoinTx).where(coin_filter()))
        for model, _ in WIPE:
            await s.execute(delete(model))
        await s.execute(delete(Setting).where(Setting.key.in_(KEYS)))
        for pat in LIKE:
            await s.execute(delete(Setting).where(Setting.key.like(pat)))
        row = await s.get(Setting, "sinov")
        row.value = {"on": False, "ended_by": st.id}
        await S.audit(s, st, "haqiqiy_ish", deleted=dict((n, k) for n, k in cnt))
        await s.commit()
    await c.answer()
    await c.message.edit_text("✅ <b>Bot haqiqiy ishga o'tdi.</b>\nSinov pul yozuvlari o'chirildi.\n\n"
                              "Endi:\n1. 💼 Kassa → 🔓 Kassalarni ochish (qo'lingizdagi va kichik kassadagi haqiqiy pul).\n"
                              "2. Moliya birinchi «🔐 Kassani yopish» da qabul kassasidagi haqiqiy pulni sanaydi.\n"
                              "3. Haqiqiy to'lovlar kiritiladi.")
