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


DOC = "doc:"   # fayl ko'rinishidagi chek (PDF va h.k.) — file_id oldiga qo'yiladi


def receipt_of(m) -> tuple[str, str] | None:
    """Xabardan chek: rasm yoki fayl (PDF, rasm-fayl). (saqlanadigan file_id, takrorni aniqlash uchun unique_id)."""
    if m.photo:
        p = m.photo[-1]
        return p.file_id, p.file_unique_id
    d = m.document
    if d and ((d.mime_type or "").startswith("image/") or (d.mime_type or "") == "application/pdf"
              or (d.file_name or "").lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp"))):
        return DOC + d.file_id, d.file_unique_id
    return None


async def send_photo(bot: Bot, tg_id: int, file_id: str, caption: str, **kw) -> bool:
    """Rasm yoki (doc: belgili) faylni izoh bilan yuboradi."""
    try:
        if file_id.startswith(DOC):
            await bot.send_document(tg_id, file_id[len(DOC):], caption=caption, **kw)
        else:
            await bot.send_photo(tg_id, file_id, caption=caption, **kw)
        return True
    except TelegramForbiddenError:
        await mark_blocked(bot, tg_id)
        return False
    except Exception:
        return False
