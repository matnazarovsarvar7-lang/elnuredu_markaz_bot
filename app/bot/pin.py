"""🔐 Pul amallari uchun 4 xonali PIN-kod (rahbar). Bir marta kiritilsa — 10 daqiqa amal qiladi."""
from __future__ import annotations

import hashlib
import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.db.models import Setting
from app.db.session import Session
from app import services as S

router = Router()
TTL = 600
_ok: dict[int, float] = {}


def _h(pin: str, tg: int) -> str:
    return hashlib.sha256(f"elnur:{tg}:{pin}".encode()).hexdigest()


class Pn(StatesGroup):
    enter = State()
    new = State()


async def need_pin(c: CallbackQuery, state: FSMContext) -> bool:
    """True — PIN so'raldi (amal bajarilmasin). PIN o'rnatilmagan bo'lsa — to'xtatmaydi."""
    async with Session() as s:
        row = await s.get(Setting, f"pin_{c.from_user.id}")
    if not row:
        return False
    if time.time() - _ok.get(c.from_user.id, 0) < TTL:
        return False
    await state.set_state(Pn.enter)
    await c.answer("🔐 PIN-kod kerak", show_alert=False)
    await c.message.answer("🔐 Pul amali: 4 xonali PIN-kodingizni yozing. Keyin tugmani qayta bosing.")
    return True


@router.message(Pn.enter)
async def pin_enter(m: Message, state: FSMContext) -> None:
    async with Session() as s:
        row = await s.get(Setting, f"pin_{m.from_user.id}")
    await state.clear()
    try:
        await m.delete()
    except Exception:
        pass
    if row and row.value.get("h") == _h((m.text or "").strip(), m.from_user.id):
        _ok[m.from_user.id] = time.time()
        await m.answer("✅ PIN to'g'ri. Endi tugmani qayta bosing (10 daqiqa amal qiladi).")
    else:
        await m.answer("❌ PIN noto'g'ri.")


@router.callback_query(F.data == "set:pin")
async def pin_set_ask(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Pn.new)
    await c.answer()
    await c.message.answer("🔐 Yangi 4 xonali PIN-kodni yozing (oylik berish, pul qaytarish, katta xarajat va kassa "
                           "qarorlarida so'raladi). Xabaringiz darhol o'chiriladi.")


@router.message(Pn.new)
async def pin_set(m: Message, state: FSMContext) -> None:
    pin = (m.text or "").strip()
    try:
        await m.delete()
    except Exception:
        pass
    if not (pin.isdigit() and len(pin) == 4):
        await m.answer("PIN 4 ta raqamdan iborat bo'lsin.")
        return
    await state.clear()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        row = await s.get(Setting, f"pin_{m.from_user.id}")
        if row:
            row.value = {"h": _h(pin, m.from_user.id)}
        else:
            s.add(Setting(key=f"pin_{m.from_user.id}", value={"h": _h(pin, m.from_user.id)}))
        await S.audit(s, who.staff, "pin")
        await s.commit()
    await m.answer("✅ PIN-kod o'rnatildi.")
