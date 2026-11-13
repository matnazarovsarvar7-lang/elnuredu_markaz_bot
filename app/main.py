import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import router
from app.bot.payments import router as pay_router
from app.bot.edit import router as edit_router
from app.bot.manage import router as manage_router
from app.bot.leads import router as leads_router
from app.bot.register import router as register_router
from app.bot.homework import router as homework_router
from app.bot.exams import router as exams_router
from app.bot.delegate import router as delegate_router
from app.bot.tests_flow import router as tests_router, scan_router
from app.bot.status import router as status_router
from app.bot.cash import router as cash_router
from app.bot.market import router as market_router
from app.bot.finance import router as finance_router
from app.bot.salary import router as salary_router
from app.bot.achieve import router as achieve_router
from app.bot.pin import router as pin_router
from app.bot.extra import router as extra_router
from app.bot.fixes import router as fixes_router
from app.bot.reports import router as reports_router
from app.bot.vault import router as vault_router
from app.bot.funds import router as funds_router
from app.bot.golive import router as golive_router
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


async def announce_scanner(bot: Bot) -> None:
    """Bir marta: o'qituvchilarga yangi menyu (📷 Skaner tugmasi bilan) yuboriladi."""
    from app.db.session import Session
    from sqlalchemy import select
    from app.db.models import Setting, Staff
    from app.bot import keyboards as K
    async with Session() as s:
        if await s.get(Setting, "menu_scan_v1"):
            return
        s.add(Setting(key="menu_scan_v1", value={"on": True}))
        staff = list((await s.execute(select(Staff).where(Staff.tg_id.is_not(None)))).scalars())
        await s.commit()
    for st in staff:
        if st.current_role not in ("oqituvchi", "yordamchi", "rahbar"):
            continue
        try:
            await bot.send_message(st.tg_id, "📷 <b>Yangi: jonli skaner</b>\nPastki menyudagi «📷 Skaner» tugmasini "
                                   "bosing — kamera ochiladi. Javob varaqlarini birin-ketin kameraga tuting: bot har "
                                   "bir varaqning o'quvchisi va guruhini QR koddan o'zi aniqlaydi. Tugatgach "
                                   "«✅ Tugatdim» — natijalar shu yerga keladi.",
                                   reply_markup=K.staff_menu(st.current_role, len(st.role_list()) > 1))
        except Exception:
            pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await ensure_owner()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    from app.bot.notify import menu_reset_middleware
    dp.message.outer_middleware(menu_reset_middleware)
    # /start har doim birinchi: boshlangan amal (to'lov, tahrir) o'rtasida ham ishlaydi
    from app.bot.handlers import start as start_handler
    from aiogram import Router
    from aiogram.filters import CommandStart
    top = Router()
    top.message.register(start_handler, CommandStart())
    dp.include_router(top)
    dp.include_router(scan_router)
    dp.include_router(pin_router)
    dp.include_router(pay_router)
    dp.include_router(manage_router)
    dp.include_router(leads_router)
    dp.include_router(register_router)
    dp.include_router(homework_router)
    dp.include_router(status_router)
    dp.include_router(cash_router)
    dp.include_router(market_router)
    dp.include_router(finance_router)
    dp.include_router(salary_router)
    dp.include_router(achieve_router)
    dp.include_router(extra_router)
    dp.include_router(fixes_router)
    dp.include_router(reports_router)
    dp.include_router(vault_router)
    dp.include_router(funds_router)
    dp.include_router(golive_router)
    dp.include_router(tests_router)
    dp.include_router(exams_router)
    dp.include_router(delegate_router)
    dp.include_router(edit_router)
    dp.include_router(router)
    setup_jobs(bot)
    await announce_scanner(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
