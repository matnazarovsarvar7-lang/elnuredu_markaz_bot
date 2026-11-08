import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import router
from app.bot.payments import router as pay_router
from app.bot.edit import router as edit_router
from app.bot.manage import router as manage_router
from app.config import settings
from app.db.session import init_db
from app.bot.jobs import setup as setup_jobs


async def ensure_owner() -> None:
    """Birinchi ishga tushirishda rahbar .env dagi OWNER_PHONE bilan kira olsin."""
    from sqlalchemy import select
    from app.db.models import Staff
    from app.db.session import Session
    from app.services import norm_phone
    phone = norm_phone(settings.owner_phone)
    if not phone:
        return
    async with Session() as s:
        st = (await s.execute(select(Staff).where(Staff.phone == phone))).scalar_one_or_none()
        if not st:
            s.add(Staff(full_name="Rahbar", phone=phone, roles="rahbar", current_role="rahbar"))
            await s.commit()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await ensure_owner()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    # /start har doim birinchi: boshlangan amal (to'lov, tahrir) o'rtasida ham ishlaydi
    from app.bot.handlers import start as start_handler
    from aiogram import Router
    from aiogram.filters import CommandStart
    top = Router()
    top.message.register(start_handler, CommandStart())
    dp.include_router(top)
    dp.include_router(pay_router)
    dp.include_router(manage_router)
    dp.include_router(edit_router)
    dp.include_router(router)
    setup_jobs(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
