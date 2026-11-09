"""Uy vazifasi: o'qituvchi yuboradi → o'quvchi va ota-onaga boradi; eslatmalar."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import select

from app.bot import keyboards as K
from app.bot.notify import send, send_photo, mark_blocked
from app.config import settings
from app.db.models import Enrollment, Group, Lesson, Staff, Student
from app.db.session import Session
from app.logic.calendar import is_lesson_day, nth_lesson_on_or_after
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
DEADLINE_HOUR = 20
NO_HW = {"nazorat": "Nazorat ishi o'tkazildi", "takror": "Takrorlash darsi", "bayram": "Bayram yoki ta'til oldi",
         "boshqa": "Boshqa sabab"}


def now() -> datetime:
    return datetime.now(TZ)


class Hw(StatesGroup):
    text = State()


def prompt_kb(lid: int, given: bool = False) -> IM:
    first = "✏️ Vazifani o'zgartirish" if given else "✍️ Uy vazifasini yozish"
    return IM(inline_keyboard=[[IB(text=first, callback_data=f"hwg:{lid}")],
                               [IB(text="🚫 Bugun vazifa yo'q", callback_data=f"hwn:{lid}")]])


def responsible_id(g: Group, what: str = "homework") -> int | None:
    if (g.delegated or {}).get(what) and g.assistant_id:
        return g.assistant_id
    return g.teacher_id


def next_lesson(g: Group, after: date) -> date:
    return nth_lesson_on_or_after(after + timedelta(days=1), g.days, 1)


async def lesson_status_text(lid: int) -> tuple[str, bool]:
    async with Session() as s:
        les = await s.get(Lesson, lid)
        g = await s.get(Group, les.group_id)
    if les.homework_text or les.homework_file_id:
        return (f"<b>{g.name}</b> · {les.date:%d.%m}\n📚 Uy vazifasi yuborilgan ✅\n\n{les.homework_text or '(faylda)'}",
                True)
    if les.no_homework_reason:
        return f"<b>{g.name}</b> · {les.date:%d.%m}\n🚫 Vazifa berilmadi: {les.no_homework_reason}", True
    return (f"<b>{g.name}</b> · {les.date:%d.%m}\n📚 Endi yangi uy vazifasini yuboring — u o'quvchilar va "
            f"ota-onalarga boradi. Muddat: bugun {DEADLINE_HOUR}:00 gacha.", False)


async def ask_homework(msg: Message, lid: int) -> None:
    text, given = await lesson_status_text(lid)
    await msg.answer(text, reply_markup=prompt_kb(lid, given))


# ---------- o'qituvchi: vazifa yozish ----------
@router.callback_query(F.data.startswith("hwg:"))
async def hw_write(c: CallbackQuery, state: FSMContext) -> None:
    lid = int(c.data.split(":")[1])
    await state.set_state(Hw.text)
    await state.update_data(lid=lid)
    await c.answer()
    await c.message.answer("Uy vazifasini yozing. Rasm yoki fayl ham yuborish mumkin (izohiga matn yozing).\n"
                           "<i>Masalan: 45-bet, 3–7-misollar. Yangi so'zlarni yod olish.</i>\n\nBekor qilish: /start")


@router.message(Hw.text)
async def hw_got(m: Message, state: FSMContext) -> None:
    text = (m.text or m.caption or "").strip()
    fid = kind = None
    if m.photo:
        fid, kind = m.photo[-1].file_id, "photo"
    elif m.document:
        fid, kind = m.document.file_id, "doc"
    if not text and not fid:
        await m.answer("Vazifa matnini yozing yoki rasm/fayl yuboring.")
        return
    if len(text) > 900:
        await m.answer("Matn juda uzun (900 belgidan oshmasin). Qisqartirib yuboring.")
        return
    await state.update_data(text=text or None, fid=fid, kind=kind)
    preview = text or "(matnsiz, faqat fayl)"
    await m.answer(f"<b>Tekshiring:</b>\n\n{preview}" + ("\n📎 fayl biriktirilgan" if fid else ""),
                   reply_markup=IM(inline_keyboard=[[IB(text="✅ Yuborish", callback_data="hwsend"),
                                                     IB(text="✏️ Qayta yozish", callback_data="hwagain")]]))


@router.callback_query(F.data == "hwagain")
async def hw_again(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Hw.text)
    await c.message.edit_text("Uy vazifasini qaytadan yozing:")


@router.callback_query(F.data == "hwsend")
async def hw_send(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    if not d.get("lid"):
        await c.answer("Ma'lumot eskirgan, qaytadan boshlang.", show_alert=True)
        return
    await state.clear()
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        les = await s.get(Lesson, d["lid"])
        g = await s.get(Group, les.group_id)
        resend = bool(les.homework_text or les.homework_file_id)
        les.homework_text, les.homework_file_id, les.homework_file_kind = d.get("text"), d.get("fid"), d.get("kind")
        les.no_homework_reason = None
        les.homework_at, les.homework_by = now().replace(tzinfo=None), who.staff.id if who.staff else None
        await S.audit(s, who.staff, "uy_vazifa", group=g.name, date=les.date.isoformat())
        await s.commit()
    await c.message.edit_text("⏳ Yuborilmoqda...")
    sent, missing = await deliver(bot, les.id, corrected=resend)
    await c.message.edit_text(f"✅ Uy vazifasi yuborildi: {sent} ta qabul qiluvchi."
                              + (f"\n⚠️ Botga ulanmagan o'quvchilar: {missing} ta (admin ota-onalarni ulashi kerak)."
                                 if missing else ""))


async def deliver(bot: Bot, lid: int, corrected: bool = False) -> tuple[int, int]:
    """Guruhdagi barcha faol o'quvchilarga (kelmaganlarga ham) va ota-onalarga yuboradi."""
    async with Session() as s:
        les = await s.get(Lesson, lid)
        g = await s.get(Group, les.group_id)
        roster = await S.group_roster(s, g.id, les.date)
        targets: dict[int, None] = {}
        missing = 0
        for e in roster:
            ids = await S.parent_tg_ids(s, e.student_id)
            if not ids:
                missing += 1
            for t in ids:
                targets[t] = None
        nxt = next_lesson(g, les.date)
    head = "✏️ <b>Uy vazifasi (tuzatildi)</b>" if corrected else "📚 <b>Uy vazifasi</b>"
    when = f" soat {g.start_time:%H:%M}" if g.start_time else ""
    text = (f"{head}\n{g.name} · {les.date:%d.%m}\n\n{les.homework_text or ''}\n\n"
            f"Keyingi dars: {nxt:%d.%m}{when}")
    sent = 0
    for t in targets:
        if les.homework_file_kind == "photo":
            ok = await send_photo(bot, t, les.homework_file_id, text)
        elif les.homework_file_kind == "doc":
            try:
                await bot.send_document(t, les.homework_file_id, caption=text)
                ok = True
            except Exception as ex:  # bloklagan bo'lsa belgilaymiz
                ok = False
                if "forbidden" in str(ex).lower():
                    await mark_blocked(bot, t)
        else:
            ok = await send(bot, t, text)
        sent += ok
    return sent, missing


# ---------- vazifa yo'q ----------
@router.callback_query(F.data.startswith("hwn:"))
async def hw_none(c: CallbackQuery) -> None:
    lid = int(c.data.split(":")[1])
    kb = IM(inline_keyboard=[[IB(text=v, callback_data=f"hwr:{lid}:{k}")] for k, v in NO_HW.items()])
    await c.message.edit_text("Nega bugun uy vazifasi berilmaydi?", reply_markup=kb)


@router.callback_query(F.data.startswith("hwr:"))
async def hw_none_reason(c: CallbackQuery) -> None:
    _, lid, code = c.data.split(":")
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        les = await s.get(Lesson, int(lid))
        g = await s.get(Group, les.group_id)
        les.no_homework_reason = NO_HW[code]
        les.homework_at, les.homework_by = now().replace(tzinfo=None), who.staff.id if who.staff else None
        await S.audit(s, who.staff, "uy_vazifa_yoq", group=g.name, reason=NO_HW[code])
        await s.commit()
    await c.message.edit_text(f"Saqlandi: bugun vazifa yo'q — {NO_HW[code]}.")


# ---------- ota-ona / o'quvchi: so'nggi vazifa ----------
async def latest_homework(s, group_id: int) -> Lesson | None:
    return (await s.execute(select(Lesson).where(Lesson.group_id == group_id, Lesson.homework_at.is_not(None))
                            .order_by(Lesson.date.desc()).limit(1))).scalar_one_or_none()


@router.message(F.text == K.B_HOMEWORK)
async def hw_view(m: Message, bot: Bot) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        kids = list(who.parent.children) if who.parent else ([who.student] if who.student else [])
        items = []
        for k in kids:
            for e in await S.student_enrollments(s, k.id):
                les = await latest_homework(s, e.group_id)
                items.append((k.full_name, e.group, les))
    if not items:
        await m.answer("Hozircha ma'lumot yo'q.")
        return
    for name, g, les in items:
        title = f"📚 <b>{g.name}</b>" + (f" · {name}" if len(kids) > 1 else "")
        if not les:
            await m.answer(f"{title}\nHali uy vazifasi kiritilmagan.")
        elif les.no_homework_reason:
            await m.answer(f"{title} · {les.date:%d.%m}\nVazifa berilmagan: {les.no_homework_reason}")
        elif les.homework_file_kind == "photo":
            await send_photo(bot, m.chat.id, les.homework_file_id, f"{title} · {les.date:%d.%m}\n\n{les.homework_text or ''}")
        elif les.homework_file_kind == "doc":
            await bot.send_document(m.chat.id, les.homework_file_id,
                                    caption=f"{title} · {les.date:%d.%m}\n\n{les.homework_text or ''}")
        else:
            await m.answer(f"{title} · {les.date:%d.%m}\n\n{les.homework_text}")


# ---------- avtomatik eslatmalar ----------
async def homework_watch(bot: Bot) -> None:
    """Dars tugaganidan 1 soat o'tib vazifa yuborilmagan bo'lsa — har soatda eslatma (20:00 gacha)."""
    n = now()
    if n.hour >= DEADLINE_HOUR:
        return
    t = n.date()
    todo = []
    async with Session() as s:
        for g in (await s.execute(select(Group).where(Group.active, Group.end_time.is_not(None)))).scalars():
            if not is_lesson_day(t, g.days):
                continue
            passed = n - datetime.combine(t, g.end_time, TZ)
            if passed < timedelta(hours=1) or passed % timedelta(hours=1) >= timedelta(minutes=10):
                continue
            les = (await s.execute(select(Lesson).where(Lesson.group_id == g.id, Lesson.date == t))).scalar_one_or_none()
            if les and (les.cancelled or les.homework_at):
                continue
            rid = responsible_id(g)
            st = await s.get(Staff, rid) if rid else None
            if not (st and st.tg_id):
                continue
            if not les:
                les = await S.get_or_create_lesson(s, g.id, t)
                await s.commit()
            todo.append((st.tg_id, g.name, les.id))
    for tg, name, lid in todo:
        await send(bot, tg, f"⏰ «{name}» guruhiga bugungi uy vazifasi hali yuborilmagan. Muddat — "
                            f"{DEADLINE_HOUR}:00.", reply_markup=prompt_kb(lid))


async def missing_today(s, t: date) -> list[str]:
    """Bugun dars bo'lgan, lekin vazifa (yoki 'yo'q' sababi) kiritilmagan guruhlar."""
    out = []
    for g in (await s.execute(select(Group).where(Group.active))).scalars():
        if not is_lesson_day(t, g.days):
            continue
        les = (await s.execute(select(Lesson).where(Lesson.group_id == g.id, Lesson.date == t))).scalar_one_or_none()
        if les and (les.cancelled or les.homework_at):
            continue
        out.append(g.name)
    return out


async def tomorrow_reminder(bot: Bot) -> None:
    """Soat 18:00 da: ertaga darsi bor o'quvchiga vazifani eslatadi (o'quvchi botda bo'lmasa — ota-onaga)."""
    t = now().date()
    tm = t + timedelta(days=1)
    jobs = []
    async with Session() as s:
        for g in (await s.execute(select(Group).where(Group.active))).scalars():
            if not is_lesson_day(tm, g.days):
                continue
            les = await latest_homework(s, g.id)
            if not les or les.no_homework_reason or les.date < t - timedelta(days=7):
                continue
            when = f" soat {g.start_time:%H:%M}" if g.start_time else ""
            text = (f"⏰ Ertaga{when} — «{g.name}» darsi.\nUy vazifasi ({les.date:%d.%m}):\n\n"
                    f"{les.homework_text or '(faylda — «📚 Uy vazifasi» tugmasini bosing)'}")
            for e in await S.group_roster(s, g.id, tm):
                stu = await s.get(Student, e.student_id)
                if stu.tg_id:
                    jobs.append((stu.tg_id, text))
                else:
                    for p in await S.parent_tg_ids(s, stu.id):
                        jobs.append((p, text))
    for tg, text in jobs:
        await send(bot, tg, text)
