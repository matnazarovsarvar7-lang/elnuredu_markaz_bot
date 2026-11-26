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
from app.bot.stock import router as stock_router
from app.bot.rating import router as rating_router
from app.bot.murojaat import router as murojaat_router
from app.bot.prints import router as prints_router
from app.bot.moc import router as moc_router
from app.bot.certs import router as certs_router
from app.bot.coach import router as coach_router
from app.bot.moc_ext import router as moc_ext_router
from app.bot.analysis import router as analysis_router
from app.bot.online import router as online_router
from app.bot.olymp import router as olymp_router
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


async def announce_menu_v2(bot: Bot) -> None:
    """Bir marta: hamma xodimga yangi (ixcham) menyu; rahbarga — bo'limlar haqida qisqa izoh."""
    from app.db.session import Session
    from sqlalchemy import select
    from app.db.models import Setting, Staff
    from app.bot import keyboards as K
    async with Session() as s:
        if await s.get(Setting, "menu_v2"):
            return
        s.add(Setting(key="menu_v2", value={"on": True}))
        staff = list((await s.execute(select(Staff).where(Staff.tg_id.is_not(None), Staff.active))).scalars())
        await s.commit()
    for st in staff:
        role = st.current_role or (st.role_list() or ["oqituvchi"])[0]
        text = ("✨ <b>Menyu yangilandi</b> — ixchamroq bo'ldi.\n" + (
            "Eng ko'p ishlatiladiganlar asosiy menyuda, qolganlari uchta bo'limda:\n"
            "🎓 Ta'lim · 💰 Moliya · 🏢 Boshqaruv. Qaytish — «⬅️ Asosiy menyu».\n"
            "Har tugma nima qilishi — «ℹ️ Yordam»." if role == "rahbar" else
            "Har tugma nima qilishi — «ℹ️ Yordam»."))
        try:
            await bot.send_message(st.tg_id, text, reply_markup=K.staff_menu(role, len(st.role_list()) > 1))
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
    dp.include_router(online_router)
    dp.include_router(olymp_router)
    dp.include_router(scan_router)
    dp.include_router(pin_router)
    dp.include_router(pay_router)
    dp.include_router(manage_router)
    dp.include_router(coach_router)
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
    dp.include_router(stock_router)
    dp.include_router(rating_router)
    dp.include_router(murojaat_router)
    dp.include_router(prints_router)
    dp.include_router(moc_router)
    dp.include_router(moc_ext_router)
    dp.include_router(analysis_router)
    dp.include_router(certs_router)
    dp.include_router(tests_router)
    dp.include_router(exams_router)
    dp.include_router(delegate_router)
    dp.include_router(edit_router)
    dp.include_router(router)
    setup_jobs(bot)
    await announce_scanner(bot)
    await announce_menu_v2(bot)
    from app import agent_api
    await agent_api.start(bot)          # printer dasturi uchun API (faqat ichki port, tashqariga Caddy orqali)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
