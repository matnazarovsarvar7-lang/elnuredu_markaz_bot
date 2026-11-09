"""Qo'lda coin; dars bekor qilish va qo'shimcha dars; ota-onalar so'rovnomasi; haftalik hisobot; market ombor sanog'i."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.bot.notify import send
from app.config import settings
from app.db.models import (Attendance, CoinTx, Enrollment, ExamResult, Exam, Group, Lesson, Product, Setting, Staff,
                           StaffPoint, StockCount, Student, SurveyAnswer)
from app.db.session import Session
from app.logic.calendar import is_lesson_day
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
MAKEUP_DAYS = 14
MANUAL_LIMIT = 300            # moliya oyiga shu coingacha o'zi beradi, ortig'i — rahbar tasdig'i


def today() -> date:
    return datetime.now(TZ).date()


def parse_day(text: str, base: date) -> date | None:
    x = re.findall(r"\d+", text or "")
    try:
        d = date(base.year, int(x[1]), int(x[0]))
    except (IndexError, ValueError):
        return None
    return d.replace(year=base.year + 1) if d < base - timedelta(days=60) else d


# ================= 🪙 qo'lda coin =================
class Mc(StatesGroup):
    amount = State()
    reason = State()


@router.callback_query(F.data.startswith("mcn:"))
async def mc_start(c: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
    if not (who.staff and who.staff.current_role in ("moliya", "rahbar")):
        await c.answer("Ruxsat yo'q.", show_alert=True)
        return
    await state.set_state(Mc.amount)
    await state.update_data(sid=int(c.data.split(":")[1]))
    await c.answer()
    await c.message.answer("🪙 Necha coin? Berish: <code>20</code>, ayirish: <code>-20</code>")


@router.message(Mc.amount)
async def mc_amount(m: Message, state: FSMContext) -> None:
    t = (m.text or "").strip().replace(" ", "")
    if not re.fullmatch(r"[-+]?\d+", t) or int(t) == 0:
        await m.answer("Raqam yozing: 20 yoki -20")
        return
    await state.update_data(amount=int(t))
    await state.set_state(Mc.reason)
    await m.answer("Sababi (majburiy): masalan, tug'ilgan kun, tadbirda faol qatnashdi")


@router.message(Mc.reason)
async def mc_save(m: Message, state: FSMContext, bot: Bot) -> None:
    reason = (m.text or "").strip()[:150]
    if len(reason) < 3:
        await m.answer("Sababni yozing.")
        return
    d = await state.get_data()
    await state.clear()
    month_start = today().replace(day=1)
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        stu = await s.get(Student, d["sid"])
        given = int((await s.execute(select(func.coalesce(func.sum(func.abs(CoinTx.amount)), 0)).where(
            CoinTx.kind == "qolda", CoinTx.by_staff_id == who.staff.id,
            CoinTx.created_at >= datetime.combine(month_start, datetime.min.time())))).scalar())
        boss = who.staff.current_role == "rahbar"
        if boss or given + abs(d["amount"]) <= MANUAL_LIMIT:
            await S.add_coins(s, stu, d["amount"], "qolda", reason, who.staff)
            await S.audit(s, who.staff, "qolda_coin", student=stu.full_name, amount=d["amount"], reason=reason)
            await s.commit()
            fam = await S.parent_tg_ids(s, stu.id)
            await m.answer(f"✅ {stu.full_name}: {d['amount']:+d} coin ({reason}).")
            for t in fam:
                await send(bot, t, f"🪙 {stu.full_name}: {d['amount']:+d} coin — {reason}")
            return
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        name = who.staff.full_name
        key = f"mc_{int(datetime.now().timestamp())}_{stu.id}"
        s.add(Setting(key=key, value={"sid": stu.id, "amount": d["amount"], "reason": reason, "by": who.staff.id}))
        await s.commit()
    await m.answer(f"Oylik chegara ({MANUAL_LIMIT} coin) dan oshdi — rahbar tasdig'iga yuborildi.")
    for b in bosses:
        await send(bot, b, f"🪙 <b>Qo'lda coin (chegaradan ortiq)</b>\n{name} → {stu.full_name}: {d['amount']:+d}\nSabab: {reason}",
                   reply_markup=IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=f"mcy:{key}"),
                                                     IB(text="❌ Rad", callback_data=f"mcx:{key}")]]))


@router.callback_query(F.data.startswith(("mcy:", "mcx:")))
async def mc_decide(c: CallbackQuery, bot: Bot) -> None:
    kind, key = c.data.split(":", 1)
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        row = await s.get(Setting, key)
        if not (who.staff and who.staff.current_role == "rahbar") or not row:
            await c.answer("Hal qilingan yoki ruxsat yo'q.", show_alert=True)
            return
        v = row.value
        await s.delete(row)
        fam = []
        if kind == "mcy":
            stu = await s.get(Student, v["sid"])
            giver = await s.get(Staff, v["by"])
            await S.add_coins(s, stu, v["amount"], "qolda", v["reason"], giver)
            await S.audit(s, who.staff, "qolda_coin", student=stu.full_name, amount=v["amount"], approved=True)
            fam = await S.parent_tg_ids(s, stu.id)
            txt = f"🪙 {stu.full_name}: {v['amount']:+d} coin — {v['reason']}"
        await s.commit()
    await c.message.edit_text(c.message.html_text + ("\n\n✅ Tasdiqlandi" if kind == "mcy" else "\n\n❌ Rad etildi"))
    for t in fam:
        await send(bot, t, txt)


# ================= ❌ dars bekor qilish va 📅 qo'shimcha dars =================
class Lc(StatesGroup):
    day = State()
    reason = State()
    mk_day = State()


async def _my_groups(s, st: Staff) -> list[Group]:
    if st.current_role == "rahbar":
        return list((await s.execute(select(Group).where(Group.active).order_by(Group.name))).scalars())
    return list((await s.execute(select(Group).where(Group.active, Group.teacher_id == st.id))).scalars())


@router.callback_query(F.data == "lc:menu")
async def lc_menu(c: CallbackQuery) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        gs = await _my_groups(s, who.staff)
        need = (await s.execute(select(Lesson, Group.name).join(Group, Group.id == Lesson.group_id).where(
            Lesson.group_id.in_([g.id for g in gs] or [0]), Lesson.cancelled, Lesson.cancel_status == "tasdiq",
            Lesson.makeup_date.is_(None)))).all()
    rows = [[IB(text=f"❌ Bekor qilish: {g.name}", callback_data=f"lcg:{g.id}")] for g in gs]
    rows += [[IB(text=f"📅 O'rniga dars: {n} ({l.date:%d.%m})", callback_data=f"lmk:{l.id}")] for l, n in need]
    await c.answer()
    await c.message.answer("Tanlang. Bekor qilingan dars o'rniga 14 kun ichida qo'shimcha dars o'tilishi shart.",
                           reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data.startswith("lcg:"))
async def lc_group(c: CallbackQuery, state: FSMContext) -> None:
    gid = int(c.data.split(":")[1])
    t = today()
    async with Session() as s:
        g = await s.get(Group, gid)
    days = [t + timedelta(i) for i in range(0, 15) if is_lesson_day(t + timedelta(i), g.days)][:5]
    await state.set_state(Lc.day)
    await state.update_data(gid=gid)
    await c.message.edit_text(f"«{g.name}»: qaysi dars bekor qilinadi?", reply_markup=IM(inline_keyboard=[
        [IB(text=f"{d:%d.%m}", callback_data=f"lcd:{d.isoformat()}")] for d in days]))


@router.callback_query(Lc.day, F.data.startswith("lcd:"))
async def lc_day(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(day=c.data.split(":")[1])
    await state.set_state(Lc.reason)
    await c.message.edit_text("Sababi (kasallik, oilaviy holat va h.k.):")


@router.message(Lc.reason)
async def lc_reason(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    await state.clear()
    reason = (m.text or "").strip()[:200]
    day = date.fromisoformat(d["day"])
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        les = await S.get_or_create_lesson(s, d["gid"], day)
        g = await s.get(Group, d["gid"])
        les.cancel_reason, les.cancel_status = reason, "sorov"
        boss = who.staff.current_role == "rahbar"
        await s.commit()
        lid = les.id
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    if boss:
        await _approve(bot, lid, who.staff.id)
        await m.answer(f"✅ «{g.name}» {day:%d.%m} darsi bekor qilindi. O'rniga 14 kun ichida qo'shimcha dars belgilang.")
        return
    await m.answer("✅ So'rov rahbarga yuborildi.")
    for b in bosses:
        await send(bot, b, f"❌ <b>Dars bekor qilish so'rovi</b>\n{who.staff.full_name} · {g.name} · {day:%d.%m}\nSabab: {reason}",
                   reply_markup=IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=f"lcy:{lid}"),
                                                     IB(text="❌ Rad", callback_data=f"lcn:{lid}")]]))


async def _approve(bot: Bot, lid: int, by: int) -> None:
    async with Session() as s:
        les = await s.get(Lesson, lid)
        g = await s.get(Group, les.group_id)
        les.cancelled, les.cancel_status = True, "tasdiq"
        # oyiga 1 tadan ortiq bekor — har biri uchun −2
        first = les.date.replace(day=1)
        n = (await s.execute(select(func.count(Lesson.id)).join(Group, Group.id == Lesson.group_id).where(
            Group.teacher_id == g.teacher_id, Lesson.cancel_status == "tasdiq", Lesson.date >= first))).scalar()
        if n > 1 and g.teacher_id:
            s.add(StaffPoint(staff_id=g.teacher_id, points=-2, role="oqituvchi", date=today(),
                             reason=f"Oyiga 1 dan ortiq dars bekor qilindi: {g.name} {les.date:%d.%m}"))
        await S.audit(s, await s.get(Staff, by), "dars_bekor", group=g.name, date=les.date.isoformat())
        await s.commit()
        fam = set()
        for e in await S.group_roster(s, g.id, les.date):
            fam.update(await S.parent_tg_ids(s, e.student_id))
        t = await s.get(Staff, g.teacher_id) if g.teacher_id else None
        info = (g.name, les.date, les.cancel_reason, t.tg_id if t else None, n)
    gname, d, why, ttg, n = info
    for x in fam:
        await send(bot, x, f"❌ «{gname}» guruhida {d:%d.%m} kungi dars bo'lmaydi ({why}). O'rniga qo'shimcha dars "
                           "kuni alohida xabar qilinadi.")
    if ttg:
        await send(bot, ttg, f"✅ «{gname}» {d:%d.%m} darsi bekor qilindi. 14 kun ichida o'rniga dars belgilang: "
                             "📋 Bugungi darslar → «📅 qo'shimcha dars»." + ("\n⚠️ Bu oy 1 tadan ortiq bekor — −2 ball." if n > 1 else ""))


@router.callback_query(F.data.startswith(("lcy:", "lcn:")))
async def lc_decide(c: CallbackQuery, bot: Bot) -> None:
    kind, lid = c.data.split(":")
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            await c.answer("Faqat rahbar.", show_alert=True)
            return
        les = await s.get(Lesson, int(lid))
        if les.cancel_status != "sorov":
            await c.answer("Hal qilingan.", show_alert=True)
            return
        if kind == "lcn":
            les.cancel_status = "rad"
            g = await s.get(Group, les.group_id)
            t = await s.get(Staff, g.teacher_id) if g.teacher_id else None
            await s.commit()
    if kind == "lcy":
        await _approve(bot, int(lid), who.staff.id)
        await c.message.edit_text(c.message.html_text + "\n\n✅ Tasdiqlandi")
    else:
        await c.message.edit_text(c.message.html_text + "\n\n❌ Rad etildi")
        if t and t.tg_id:
            await send(bot, t.tg_id, f"❌ «{g.name}» {les.date:%d.%m} darsini bekor qilish rad etildi — dars o'tiladi.")


@router.callback_query(F.data.startswith("lmk:"))
async def mk_start(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Lc.mk_day)
    await state.update_data(lid=int(c.data.split(":")[1]))
    await c.answer()
    await c.message.answer("📅 Qo'shimcha dars sanasi va vaqti: <i>14.11 15:00</i>")


@router.message(Lc.mk_day)
async def mk_save(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    day = parse_day(m.text, today())
    tm = re.search(r"(\d{1,2})[:.](\d{2})\s*$", m.text or "")
    async with Session() as s:
        les = await s.get(Lesson, d["lid"])
        if not day or day < today() or day > les.date + timedelta(days=MAKEUP_DAYS):
            await m.answer(f"Sana bugundan {les.date + timedelta(days=MAKEUP_DAYS):%d.%m} gacha bo'lsin. Masalan: 14.11 15:00")
            return
        g = await s.get(Group, les.group_id)
        ex = await S.get_or_create_lesson(s, g.id, day)
        ex.extra, les.makeup_date = True, day
        await S.audit(s, (await S.who_by_tg(s, m.from_user.id)).staff, "qoshimcha_dars", group=g.name, date=day.isoformat())
        await s.commit()
        fam = set()
        for e in await S.group_roster(s, g.id, day):
            fam.update(await S.parent_tg_ids(s, e.student_id))
    await state.clear()
    when = f" soat {tm.group(1)}:{tm.group(2)}" if tm else ""
    await m.answer(f"✅ «{g.name}»: {les.date:%d.%m} darsi o'rniga {day:%d.%m}{when} qo'shimcha dars.")
    for x in fam:
        await send(bot, x, f"📅 «{g.name}»: {les.date:%d.%m} kungi dars o'rniga qo'shimcha dars — <b>{day:%d.%m}{when}</b>.")


async def makeup_job(bot: Bot) -> None:
    """Har kuni 21:10 — 14 kunda o'rniga dars o'tilmagan bo'lsa o'qituvchiga −3."""
    t = today()
    msgs = []
    async with Session() as s:
        rows = (await s.execute(select(Lesson).where(Lesson.cancel_status == "tasdiq", Lesson.makeup_penalized.is_(False),
                                                     Lesson.date <= t - timedelta(days=MAKEUP_DAYS)))).scalars().all()
        for l in rows:
            done = False
            if l.makeup_date:
                ex = (await s.execute(select(Lesson).where(Lesson.group_id == l.group_id, Lesson.date == l.makeup_date))).scalar_one_or_none()
                done = bool(ex and ex.confirmed_at)
            if done:
                l.makeup_penalized = True
                continue
            g = await s.get(Group, l.group_id)
            l.makeup_penalized = True
            if g.teacher_id:
                s.add(StaffPoint(staff_id=g.teacher_id, points=-3, role="oqituvchi", date=t,
                                 reason=f"Bekor dars o'rniga qo'shimcha dars o'tilmadi: {g.name} {l.date:%d.%m}"))
                tch = await s.get(Staff, g.teacher_id)
                if tch.tg_id:
                    msgs.append((tch.tg_id, f"⚠️ «{g.name}» {l.date:%d.%m} darsi o'rniga 14 kunda dars o'tilmadi: −3 ball."))
        await s.commit()
    for x, text in msgs:
        await send(bot, x, text)


# ================= ⭐ ota-onalar so'rovnomasi (har oy 20-sana) =================
async def survey_job(bot: Bot) -> None:
    month = f"{today():%Y-%m}"
    jobs = []
    async with Session() as s:
        enrs = (await s.execute(select(Enrollment).where(Enrollment.active).options(
            selectinload(Enrollment.group), selectinload(Enrollment.student)))).scalars().all()
        for e in enrs:
            if e.student.status != "faol" or (today() - e.join_date).days < 14:
                continue
            done = (await s.execute(select(SurveyAnswer.id).where(SurveyAnswer.month == month,
                                                                  SurveyAnswer.student_id == e.student_id,
                                                                  SurveyAnswer.group_id == e.group_id))).first()
            if done:
                continue
            from app.db.models import Parent, StudentParent
            pids = (await s.execute(select(Parent.tg_id).join(StudentParent, StudentParent.parent_id == Parent.id).where(
                StudentParent.student_id == e.student_id, Parent.tg_id.is_not(None)))).scalars().all()
            for p in pids[:1]:
                jobs.append((p, e.student.full_name, e.group.name, e.student_id, e.group_id))
    for tg, name, gname, sid, gid in jobs:
        kb = IM(inline_keyboard=[[IB(text=str(n), callback_data=f"sv1:{sid}:{gid}:{n}") for n in range(1, 6)],
                                 [IB(text=str(n), callback_data=f"sv1:{sid}:{gid}:{n}") for n in range(6, 11)]])
        await send(bot, tg, f"⭐ <b>Qisqa so'rovnoma</b>\n{name} · «{gname}»\nUstozning ishidan qanchalik mamnunsiz? "
                            "(1 — umuman emas, 10 — juda mamnun). Javobingiz ustozga ism-familiyasiz ko'rsatiladi.",
                   reply_markup=kb)


class Sv(StatesGroup):
    comment = State()


@router.callback_query(F.data.startswith("sv1:"))
async def survey_answer(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    _, sid, gid, n = c.data.split(":")
    month = f"{today():%Y-%m}"
    async with Session() as s:
        g = await s.get(Group, int(gid))
        row = (await s.execute(select(SurveyAnswer).where(SurveyAnswer.month == month, SurveyAnswer.student_id == int(sid),
                                                          SurveyAnswer.group_id == int(gid)))).scalar_one_or_none()
        if not row:
            row = SurveyAnswer(month=month, student_id=int(sid), group_id=int(gid), teacher_id=g.teacher_id, score=int(n))
            s.add(row)
        row.score = int(n)
        await s.commit()
        rid = row.id
    await c.message.edit_text(f"Rahmat! Bahoyingiz: {n}/10.")
    if int(n) <= 6:
        await state.set_state(Sv.comment)
        await state.update_data(rid=rid)
        await c.message.answer("Nima yaxshilanishi kerak? Qisqa yozing (rahbarga boradi):")


@router.message(Sv.comment)
async def survey_comment(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        row = await s.get(SurveyAnswer, d["rid"])
        row.comment = (m.text or "").strip()[:1000]
        g = await s.get(Group, row.group_id)
        stu = await s.get(Student, row.student_id)
        await s.commit()
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    await m.answer("Rahmat, fikringiz rahbarga yetkazildi.")
    for b in bosses:
        await send(bot, b, f"⭐ <b>Past baho ({row.score}/10)</b> · {g.name}\n{stu.full_name} ota-onasi: «{row.comment}»")


async def survey_avg(s, teacher_id: int, month: str) -> tuple[float | None, int]:
    r = (await s.execute(select(func.avg(SurveyAnswer.score), func.count(SurveyAnswer.id)).where(
        SurveyAnswer.teacher_id == teacher_id, SurveyAnswer.month == month))).one()
    return (float(r[0]) if r[0] is not None else None), int(r[1])


# ================= 📬 haftalik hisobot (shanba 18:00) =================
async def weekly_report(bot: Bot) -> None:
    t = today()
    mon = t - timedelta(days=t.weekday())
    jobs = []
    async with Session() as s:
        studs = (await s.execute(select(Student).where(Student.status == "faol"))).scalars().all()
        for stu in studs:
            fam = await S.parent_tg_ids(s, stu.id)
            if not fam:
                continue
            rows = (await s.execute(select(Attendance, Lesson, Group.name).join(Lesson, Lesson.id == Attendance.lesson_id)
                                    .join(Enrollment, Enrollment.id == Attendance.enrollment_id)
                                    .join(Group, Group.id == Lesson.group_id)
                                    .where(Enrollment.student_id == stu.id, Lesson.date >= mon, Lesson.date <= t))).all()
            if not rows:
                continue
            came = sum(1 for a, _, _ in rows if a.mark in ("k", "l"))
            late = sum(1 for a, _, _ in rows if a.mark == "l")
            hw = [a.homework for a, _, _ in rows if a.homework]
            grades = [S.GRADE_NAMES.get(a.grade, a.grade) for a, _, _ in rows if a.grade]
            coins = int((await s.execute(select(func.coalesce(func.sum(CoinTx.amount), 0)).where(
                CoinTx.student_id == stu.id, CoinTx.created_at >= datetime.combine(mon, datetime.min.time())))).scalar())
            exams = (await s.execute(select(ExamResult.percent, Exam.date).join(Exam, Exam.id == ExamResult.exam_id)
                                     .join(Enrollment, Enrollment.id == ExamResult.enrollment_id)
                                     .where(Enrollment.student_id == stu.id, Exam.date >= mon,
                                            ExamResult.percent.is_not(None)))).all()
            debt = (await S.student_debt(s, stu.id, t.year, t.month)).debt
            lines = [f"📬 <b>Haftalik hisobot · {stu.full_name}</b> ({mon:%d.%m}–{t:%d.%m})",
                     f"Darslar: {came}/{len(rows)} keldi" + (f", {late} marta kechikdi" if late else ""),
                     f"Uy vazifasi: {sum(1 for x in hw if x == 'done')}/{len(hw)} bajarildi" if hw else "Uy vazifasi: tekshirilmagan"]
            if grades:
                lines.append("Baholar: " + ", ".join(grades))
            if exams:
                lines.append("Nazorat: " + ", ".join(f"{p:.0f}% ({d:%d.%m})" for p, d in exams))
            lines.append(f"Coin: {coins:+d} (jami {stu.coins})")
            if debt and t.day > 5:
                lines.append(f"To'lov: {S.fmt_som(debt)} qarz")
            for x in fam:
                jobs.append((x, "\n".join(lines)))
    for x, text in jobs:
        await send(bot, x, text)


# ================= 📦 market ombor sanog'i (oyning oxirgi kuni) =================
class Sc(StatesGroup):
    count = State()


async def stock_reminder(bot: Bot) -> None:
    t = today()
    if (t + timedelta(days=1)).month == t.month:
        return
    async with Session() as s:
        has = (await s.execute(select(Product.id).where(Product.active))).first()
        to = [x.tg_id for x in await S.staff_with_role(s, "moliya") if x.tg_id]
    if has:
        for x in to:
            await send(bot, x, "📦 Oy oxiri: market omborini sanang.",
                       reply_markup=IM(inline_keyboard=[[IB(text="📦 Sanoqni boshlash", callback_data="stk")]]))


@router.callback_query(F.data == "stk")
async def stock_start(c: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        prods = (await s.execute(select(Product).where(Product.active).order_by(Product.name))).scalars().all()
    if not prods:
        await c.answer("Sovg'alar yo'q.", show_alert=True)
        return
    await state.set_state(Sc.count)
    await state.update_data(ids=[p.id for p in prods], i=0, res={})
    await c.answer()
    await c.message.answer(f"📦 Har bir sovg'adan nechta borligini yozing.\n1/{len(prods)}. <b>{prods[0].name}</b>:")


@router.message(Sc.count)
async def stock_count(m: Message, state: FSMContext, bot: Bot) -> None:
    if not (m.text or "").strip().isdigit():
        await m.answer("Sonini yozing: 12")
        return
    d = await state.get_data()
    res = d["res"]
    res[str(d["ids"][d["i"]])] = int(m.text)
    i = d["i"] + 1
    async with Session() as s:
        if i < len(d["ids"]):
            p = await s.get(Product, d["ids"][i])
            await state.update_data(res=res, i=i)
            await m.answer(f"{i + 1}/{len(d['ids'])}. <b>{p.name}</b>:")
            return
        who = await S.who_by_tg(s, m.from_user.id)
        diffs = []
        for pid, cnt in res.items():
            p = await s.get(Product, int(pid))
            s.add(StockCount(product_id=p.id, expected=p.stock, counted=cnt, by_id=who.staff.id))
            if cnt != p.stock:
                diffs.append(f"• {p.name}: bazada {p.stock}, sanaldi {cnt} ({cnt - p.stock:+d})")
            p.stock = cnt
        await S.audit(s, who.staff, "ombor_sanoq", diffs=len(diffs))
        await s.commit()
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    await state.clear()
    await m.answer("✅ Sanoq saqlandi." + (" Farqlar rahbarga yuborildi." if diffs else " Hammasi mos."))
    if diffs:
        for b in bosses:
            await send(bot, b, "📦 <b>Ombor sanog'ida farq</b>\n" + "\n".join(diffs))
