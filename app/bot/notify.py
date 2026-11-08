"""Xabar yuborish. Bot bloklangan bo'lsa — belgilab, adminga xabar beradi."""
from __future__ import annotations

from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select

from app.db.models import Parent, Student
from app.db.session import Session
from app import services as S


async def send(bot: Bot, tg_id: int, text: str, **kw) -> bool:
    try:
        await bot.send_message(tg_id, text, **kw)
        return True
    except TelegramForbiddenError:
        await mark_blocked(bot, tg_id)
        return False
    except Exception:
        return False


async def mark_blocked(bot: Bot, tg_id: int) -> None:
    async with Session() as s:
        who = []
        for model in (Parent, Student):
            for x in (await s.execute(select(model).where(model.tg_id == tg_id))).scalars():
                if not x.bot_blocked_at:
                    x.bot_blocked_at = datetime.now()
                    who.append(getattr(x, "full_name", None) or getattr(x, "phone", ""))
        admins = await S.staff_with_role(s, "admin")
        await s.commit()
    for name in who:
        for a in admins:
            if a.tg_id:
                try:
                    await bot.send_message(a.tg_id, f"⚠️ {name} botni o'chirdi yoki blokladi. Qo'ng'iroq qilib, "
                                                    "qayta ulanishini so'rang — eslatma va cheklar unga bormayapti.")
                except Exception:
                    pass


async def send_photo(bot: Bot, tg_id: int, file_id: str, caption: str, **kw) -> bool:
    try:
        await bot.send_photo(tg_id, file_id, caption=caption, **kw)
        return True
    except TelegramForbiddenError:
        await mark_blocked(bot, tg_id)
        return False
    except Exception:
        return False
