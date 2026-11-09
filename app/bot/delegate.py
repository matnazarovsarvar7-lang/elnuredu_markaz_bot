"""O'qituvchi o'z guruhidagi ishlarni yordamchiga yuklaydi (nazorat yordamchida avtomatik)."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.bot import keyboards as K
from app.bot.notify import send
from app.db.models import Group, Staff
from app.db.session import Session
from app import services as S

router = Router()
JOBS = {"attendance": "Davomat olish", "hw_check": "Vazifa bajarilishini belgilash",
        "grades": "Baho qo'yish", "homework": "Uy vazifasini yuborish"}


async def _groups(s, staff: Staff) -> list[Group]:
    q = select(Group).where(Group.active, Group.assistant_id.is_not(None))
    if staff.current_role != "rahbar":
        q = q.where(Group.teacher_id == staff.id)
    return list((await s.execute(q.order_by(Group.name))).scalars())


@router.message(F.text == K.B_DELEGATE)
async def deleg_list(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not (who.staff and who.staff.current_role in ("oqituvchi", "rahbar")):
            return
        gs = await _groups(s, who.staff)
    if not gs:
        await m.answer("Sizning guruhlaringizda yordamchi o'qituvchi biriktirilmagan.")
        return
    await m.answer("🤝 Qaysi guruh ishlarini yordamchiga yuklaysiz?",
                   reply_markup=IM(inline_keyboard=[[IB(text=g.name, callback_data=f"dg:{g.id}")] for g in gs]))


async def _card(gid: int) -> tuple[str, IM]:
    async with Session() as s:
        g = await s.get(Group, gid)
        a = await s.get(Staff, g.assistant_id)
    d = g.delegated or {}
    rows = [[IB(text=f"{'✅' if d.get(k) else '▫️'} {v}", callback_data=f"dgt:{gid}:{k}")] for k, v in JOBS.items()]
    text = (f"🤝 <b>{g.name}</b> · yordamchi: {a.full_name if a else '—'}\n\n"
            "Belgilangan ishlar yordamchiga yuklanadi: eslatmalar unga boradi, o'z vaqtida bajarilmasa jarima ham "
            "unga yoziladi. Siz istalgan vaqtda qaytarib olasiz.\n\n"
            "📝 Haftalik nazorat (haftasiga 2 marta) yordamchiga avtomatik yuklangan, siz natijalarini nazorat qilasiz.")
    return text, IM(inline_keyboard=rows)


@router.callback_query(F.data.startswith("dg:"))
async def deleg_card(c: CallbackQuery) -> None:
    text, kb = await _card(int(c.data.split(":")[1]))
    await c.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("dgt:"))
async def deleg_toggle(c: CallbackQuery, bot: Bot) -> None:
    _, gid, key = c.data.split(":")
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        g = await s.get(Group, int(gid))
        if not who.staff or (who.staff.current_role != "rahbar" and g.teacher_id != who.staff.id):
            await c.answer("Ruxsat yo'q.", show_alert=True)
            return
        d = dict(g.delegated or {})
        d[key] = not d.get(key)
        g.delegated = d
        flag_modified(g, "delegated")
        await S.audit(s, who.staff, "yuklash", group=g.name, job=JOBS[key], on=d[key])
        a = await s.get(Staff, g.assistant_id)
        await s.commit()
        on, gname, atg = d[key], g.name, a.tg_id if a else None
    if atg:
        await send(bot, atg, f"🤝 «{gname}»: «{JOBS[key]}» sizga " + ("yuklandi." if on else "endi yuklanmagan."))
    text, kb = await _card(int(gid))
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer("Yuklandi" if on else "Qaytarib olindi")
