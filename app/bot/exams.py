"""Haftalik nazoratlar: rejalashtirish, natija kiritish, coin, apellyatsiya, ruxsat, majburiyat nazorati."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import select

from app.bot import keyboards as K
from app.bot.notify import send
from app.config import settings
from app.db.models import Appeal, Enrollment, Exam, ExamResult, Group, Staff, StaffLeave, StaffPoint, Student
from app.db.session import Session
from app.logic.calendar import is_lesson_day
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
KINDS = {"internet": "🌐 Internet test (ball)", "yozma": "✍️ Yozma ish", "test": "📝 Test (to'g'ri javoblar soni)"}
KIND_SHORT = {"internet": "internet test", "yozma": "yozma ish", "test": "test"}
STATUS = {"reja": "⏳ rejada", "otdi": "✅ o'tkazildi", "otkazilmadi": "❌ o'tkazilmadi", "bekor": "➖ bekor (ruxsat)"}
WD = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
MISSED_POINTS = -2
APPEAL_HOURS, APPEAL_MAX = 48, 3


def now() -> datetime:
    return datetime.now(TZ)


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_lessons(g: Group, mon: date) -> list[date]:
    return [mon + timedelta(days=i) for i in range(6) if is_lesson_day(mon + timedelta(days=i), g.days)]


def last_lesson_of_month(g: Group, lessons: list[date]) -> date | None:
    """Shu haftadagi dars kunlaridan qaysi biri o'z oyining oxirgi darsi bo'lsa — o'sha (oylik nazorat)."""
    for d in reversed(lessons):
        nxt = d + timedelta(days=1)
        while nxt.month == d.month and not is_lesson_day(nxt, g.days):
            nxt += timedelta(days=1)
        if nxt.month != d.month:
            return d
    return None


def conductor_of(g: Group) -> int | None:
    return g.assistant_id or g.teacher_id


def slots_for(g: Group, lessons: list[date]) -> list[date]:
    """Yordamchi bo'lsa — haftaning birinchi va oxirgi darsi; aks holda — birinchi darsi."""
    if not lessons:
        return []
    if g.assistant_id and len(lessons) > 1:
        return [lessons[0], lessons[-1]]
    return [lessons[0]]


async def on_leave(s, staff_id: int | None, d: date) -> bool:
    if not staff_id:
        return False
    return (await s.execute(select(StaffLeave.id).where(StaffLeave.staff_id == staff_id,
                                                        StaffLeave.date == d))).first() is not None


def fmt_day(d: date) -> str:
    return f"{WD[d.weekday()]}, {d:%d.%m}"


async def _family_targets(s, g: Group, on: date, students_first: bool = False) -> list[int]:
    """Guruhdagi o'quvchi va ota-onalar (students_first: o'quvchi botda bo'lsa faqat unga)."""
    out: dict[int, None] = {}
    for e in await S.group_roster(s, g.id, on):
        stu = await s.get(Student, e.student_id)
        if students_first and stu.tg_id:
            out[stu.tg_id] = None
            continue
        for t in await S.parent_tg_ids(s, stu.id):
            out[t] = None
    return list(out)


# ---------- rejalashtirish ----------
async def plan_week(mon: date, notify: Bot | None = None, from_day: date | None = None) -> int:
    created = []
    async with Session() as s:
        for g in (await s.execute(select(Group).where(Group.active))).scalars():
            exists = (await s.execute(select(Exam.id).where(Exam.group_id == g.id, Exam.week == mon,
                                                            Exam.slot >= 1))).first()
            if exists:
                continue
            lessons = week_lessons(g, mon)
            days = [d for d in slots_for(g, lessons) if not from_day or d >= from_day]
            month_end = last_lesson_of_month(g, lessons)
            for i, d in enumerate(days, 1):
                ex = Exam(group_id=g.id, date=d, week=mon, slot=i, conductor_id=conductor_of(g),
                          monthly=(d == month_end))
                s.add(ex)
                created.append((g, ex))
            if month_end and month_end not in days and (not from_day or month_end >= from_day):
                ex = Exam(group_id=g.id, date=month_end, week=mon, slot=len(days) + 1,
                          conductor_id=conductor_of(g), monthly=True)
                s.add(ex)
                created.append((g, ex))
        await s.commit()
        if notify:
            by_group: dict[int, list[str]] = {}
            for g, ex in created:
                by_group.setdefault(g.id, []).append(fmt_day(ex.date) + (" (oylik nazorat)" if ex.monthly else ""))
            msgs = []
            for g, ex in created:
                if ex.slot != 1:
                    continue
                days_txt = " va ".join(by_group[g.id])
                text = (f"📝 <b>Bu hafta nazorat</b> · {g.name}\n{days_txt}\n"
                        "O'tgan hafta o'tilgan mavzular bo'yicha. Tayyorlanib keling!")
                for t in await _family_targets(s, g, ex.date):
                    msgs.append((t, text))
                st = await s.get(Staff, ex.conductor_id) if ex.conductor_id else None
                if st and st.tg_id:
                    msgs.append((st.tg_id, f"📝 Siz o'tkazadigan nazorat · {g.name}: {days_txt}.\n"
                                           "Kunni o'zgartirish: «📝 Nazoratlar» (shanbagacha)."))
            for t, text in msgs:
                await send(notify, t, text)
    return len(created)


async def weekly_plan_job(bot: Bot) -> None:
    """Yakshanba 19:00 — kelasi hafta nazoratlari rejalashtiriladi va e'lon qilinadi."""
    await plan_week(monday(now().date()) + timedelta(days=7), notify=bot)


async def ensure_current_week() -> None:
    """Ishga tushganda: shu hafta rejasi bo'lmasa — bugundan keyingi kunlarga tuziladi (xabarsiz)."""
    t = now().date()
    await plan_week(monday(t), from_day=t)


async def exam_reminder_job(bot: Bot) -> None:
    """18:00 — ertangi nazorat haqida o'quvchiga (botda bo'lmasa ota-onaga) va o'tkazuvchiga."""
    tm = now().date() + timedelta(days=1)
    msgs = []
    async with Session() as s:
        for ex in (await s.execute(select(Exam).where(Exam.date == tm, Exam.status == "reja"))).scalars():
            g = await s.get(Group, ex.group_id)
            for t in await _family_targets(s, g, tm, students_first=True):
                msgs.append((t, f"⏰ Ertaga «{g.name}» darsida nazorat. O'tgan hafta mavzularini takrorlab keling."))
            st = await s.get(Staff, ex.conductor_id) if ex.conductor_id else None
            if st and st.tg_id:
                msgs.append((st.tg_id, f"⏰ Ertaga «{g.name}» guruhida nazorat o'tkazasiz."))
    for t, text in msgs:
        await send(bot, t, text)


async def missed_job(bot: Bot) -> None:
    """Shanba 20:30 — shu hafta o'tkazilmagan nazoratlar: jarima (ruxsat bo'lmasa) va rahbarga hisobot."""
    t = now().date()
    lines, staff_msgs = [], []
    async with Session() as s:
        rows = (await s.execute(select(Exam).where(Exam.week == monday(t), Exam.status == "reja",
                                                   Exam.date <= t))).scalars().all()
        for ex in rows:
            g = await s.get(Group, ex.group_id)
            st = await s.get(Staff, ex.conductor_id) if ex.conductor_id else None
            ex.status = "otkazilmadi"
            if st and not await on_leave(s, st.id, ex.date):
                s.add(StaffPoint(staff_id=st.id, points=MISSED_POINTS, role="yordamchi" if g.assistant_id else "oqituvchi",
                                 reason=f"Nazorat o'tkazilmadi: {g.name} {ex.date:%d.%m}", date=t))
                if st.tg_id:
                    staff_msgs.append((st.tg_id, f"❌ «{g.name}» guruhida {ex.date:%d.%m} dagi nazorat o'tkazilmadi: "
                                                 f"{MISSED_POINTS} ball."))
                lines.append(f"• {g.name} · {ex.date:%d.%m} · {st.full_name} ({MISSED_POINTS})")
            else:
                lines.append(f"• {g.name} · {ex.date:%d.%m} · ruxsatda (jarimasiz)")
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        await s.commit()
    for tg, text in staff_msgs:
        await send(bot, tg, text)
    if lines:
        for b in bosses:
            await send(bot, b, "📝 <b>Shu hafta o'tkazilmagan nazoratlar</b>\n" + "\n".join(lines))


# ---------- 📝 Nazoratlar (xodim) ----------
async def _my_exams(s, staff: Staff, role: str) -> list[tuple[Exam, Group]]:
    t = now().date()
    q = select(Exam, Group).join(Group, Group.id == Exam.group_id).where(Exam.week >= monday(t) - timedelta(days=7))
    if role == "yordamchi":
        q = q.where(Exam.conductor_id == staff.id)
    elif role != "rahbar":
        q = q.where((Group.teacher_id == staff.id) | (Exam.conductor_id == staff.id))
    return list((await s.execute(q.order_by(Exam.date))).all())


@router.message(F.text == K.B_EXAMS)
async def exams_list(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not who.staff:
            return
        rows = await _my_exams(s, who.staff, who.staff.current_role)
    if not rows:
        await m.answer("Hozircha nazorat rejasi yo'q. Reja har yakshanba kechqurun tuziladi.")
        return
    kb = [[IB(text=f"{STATUS[e.status][:1]} {fmt_day(e.date)} · {g.name}" + (" · oylik" if e.monthly else ""),
              callback_data=f"ex:{e.id}")] for e, g in rows]
    await m.answer("📝 <b>Nazoratlar</b> (o'tgan, shu va kelasi hafta). Tanlang:", reply_markup=IM(inline_keyboard=kb))


async def exam_card(eid: int) -> tuple[str, IM]:
    async with Session() as s:
        ex = await s.get(Exam, eid)
        g = await s.get(Group, ex.group_id)
        st = await s.get(Staff, ex.conductor_id) if ex.conductor_id else None
        res = (await s.execute(select(ExamResult).where(ExamResult.exam_id == eid))).scalars().all()
    lines = [f"📝 <b>{g.name}</b> · {fmt_day(ex.date)}" + (" · <b>OYLIK</b>" if ex.monthly else ""),
             f"Holat: {STATUS[ex.status]}",
             f"O'tkazuvchi: {st.full_name if st else '—'}"]
    if ex.moved_note:
        lines.append(f"Izoh: {ex.moved_note}")
    if ex.test_id:
        from app.db.models import Test
        async with Session() as s:
            t = await s.get(Test, ex.test_id)
        if t:
            from app.bot.tests_flow import ST as TST
            lines.append(f"Test #{t.id}: {TST.get(t.status, t.status)}" + (" · 🖨 chop etildi" if t.printed_at else ""))
    rows = []
    if ex.status == "otdi":
        took = [r for r in res if r.percent is not None]
        avg = sum(r.percent for r in took) / len(took) if took else 0
        lines += [f"Turi: {KIND_SHORT.get(ex.kind, '—')} · eng ko'p ball {ex.max_score:g}",
                  f"Qatnashdi: {len(took)} · o'rtacha {avg:.0f}%"]
        rows.append([IB(text="📊 Natijalar", callback_data=f"exv:{eid}")])
    elif ex.status != "bekor":
        rows.append([IB(text="✅ Natijalarni kiritish", callback_data=f"exr:{eid}")])
        if ex.status == "reja":
            rows.append([IB(text="📅 Kunni o'zgartirish", callback_data=f"exd:{eid}")])
    return "\n".join(lines), IM(inline_keyboard=rows)


@router.callback_query(F.data.startswith("ex:"))
async def exam_open(c: CallbackQuery) -> None:
    text, kb = await exam_card(int(c.data.split(":")[1]))
    await c.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("exv:"))
async def exam_view(c: CallbackQuery) -> None:
    eid = int(c.data.split(":")[1])
    async with Session() as s:
        ex = await s.get(Exam, eid)
        rows = (await s.execute(select(ExamResult, Student.full_name).join(Enrollment, Enrollment.id == ExamResult.enrollment_id)
                                .join(Student, Student.id == Enrollment.student_id).where(ExamResult.exam_id == eid)
                                .order_by(ExamResult.percent.desc().nullslast()))).all()
    lines = [f"{n}: " + (f"{r.score:g}/{ex.max_score:g} · {r.percent:.0f}%" if r.percent is not None else "qatnashmadi")
             for r, n in rows]
    await c.message.answer("📊 <b>Natijalar</b>\n" + "\n".join(lines))


@router.callback_query(F.data.startswith("exd:"))
async def exam_move_ask(c: CallbackQuery) -> None:
    eid = int(c.data.split(":")[1])
    t = now().date()
    async with Session() as s:
        ex = await s.get(Exam, eid)
        g = await s.get(Group, ex.group_id)
    if t > ex.week + timedelta(days=5):
        await c.answer("Bu hafta tugagan.", show_alert=True)
        return
    days = [d for d in week_lessons(g, ex.week) if d >= t and d != ex.date]
    if not days:
        await c.answer("Bu haftada boshqa dars kuni qolmagan.", show_alert=True)
        return
    kb = [[IB(text=fmt_day(d), callback_data=f"exm:{eid}:{d.isoformat()}")] for d in days]
    await c.message.edit_text("Yangi kunni tanlang (shu hafta dars kunlari):", reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("exm:"))
async def exam_move(c: CallbackQuery, bot: Bot) -> None:
    _, eid, iso = c.data.split(":")
    d = date.fromisoformat(iso)
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        ex = await s.get(Exam, int(eid))
        g = await s.get(Group, ex.group_id)
        old = ex.date
        ex.date = d
        await S.audit(s, who.staff, "nazorat_kuni", group=g.name, old=old.isoformat(), new=iso)
        await s.commit()
        targets = await _family_targets(s, g, d)
    for t in targets:
        await send(bot, t, f"📝 «{g.name}» nazorati ko'chirildi: {fmt_day(old)} → <b>{fmt_day(d)}</b>.")
    text, kb = await exam_card(int(eid))
    await c.message.edit_text("Kun o'zgartirildi ✅\n\n" + text, reply_markup=kb)


# ---------- natija kiritish ----------
class Ex(StatesGroup):
    max = State()
    score = State()


@router.callback_query(F.data.startswith("exr:"))
async def res_start(c: CallbackQuery) -> None:
    eid = int(c.data.split(":")[1])
    from app.db.models import TestPaper
    async with Session() as s:
        ex = await s.get(Exam, eid)
        has_papers = ex.test_id and (await s.execute(select(TestPaper.id).where(TestPaper.test_id == ex.test_id))).first()
    if has_papers:
        from app.bot.tests_flow import entry_menu
        text, kb = await entry_menu(ex.test_id)
        await c.message.answer(text, reply_markup=kb)
        await c.answer()
        return
    kb = [[IB(text=v, callback_data=f"exk:{eid}:{k}")] for k, v in KINDS.items()]
    await c.message.edit_text("Nazorat qanday o'tkazildi?", reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("exk:"))
async def res_kind(c: CallbackQuery, state: FSMContext) -> None:
    _, eid, kind = c.data.split(":")
    await state.clear()
    await state.set_state(Ex.max)
    await state.update_data(eid=int(eid), kind=kind)
    hint = {"internet": "Masalan: 100 (platforma bergan eng ko'p ball)", "yozma": "Masalan: 10",
            "test": "Masalan: 30 (savollar soni)"}[kind]
    await c.message.edit_text(f"{KINDS[kind]}\n\nEng ko'p ball (yoki savollar soni) nechta?\n<i>{hint}</i>")


@router.message(Ex.max)
async def res_max(m: Message, state: FSMContext) -> None:
    try:
        mx = float((m.text or "").replace(",", "."))
        assert 0 < mx <= 1000
    except (ValueError, AssertionError):
        await m.answer("Raqam yozing, masalan: 30")
        return
    d = await state.get_data()
    from app.db.models import Attendance, Lesson
    async with Session() as s:
        ex = await s.get(Exam, d["eid"])
        roster = await S.group_roster(s, ex.group_id, ex.date)
        les = (await s.execute(select(Lesson).where(Lesson.group_id == ex.group_id, Lesson.date == ex.date))).scalar_one_or_none()
        absent = set()
        if les:
            absent = set((await s.execute(select(Attendance.enrollment_id).where(
                Attendance.lesson_id == les.id, Attendance.mark == "y"))).scalars())
        people = [(e.id, e.student.full_name) for e in roster if e.id not in absent]
        pre = {str(e): None for e in absent}
    if not people:
        await state.clear()
        await m.answer("Natija kiritadigan o'quvchi yo'q.")
        return
    await state.update_data(max=mx, people=people, i=0, scores=pre, absent=len(absent))
    await state.set_state(Ex.score)
    note = f"Darsga kelmagan {len(absent)} ta o'quvchi o'tkazib yuborildi.\n" if absent else ""
    await m.answer(f"Endi har bir o'quvchining natijasini yozing (0 dan {mx:g} gacha).\n{note}"
                   "Qatnashmagan bo'lsa: <b>-</b>\n\n"
                   f"1/{len(people)}. <b>{people[0][1]}</b>:")


@router.message(Ex.score)
async def res_score(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    raw = (m.text or "").strip().replace(",", ".")
    if raw in ("-", "—"):
        val = None
    else:
        try:
            val = float(raw)
            assert 0 <= val <= d["max"]
        except (ValueError, AssertionError):
            await m.answer(f"0 dan {d['max']:g} gacha raqam yoki «-» yozing.")
            return
    people, i = d["people"], d["i"]
    scores = d["scores"]
    scores[str(people[i][0])] = val
    i += 1
    await state.update_data(scores=scores, i=i)
    if i < len(people):
        await m.answer(f"{i + 1}/{len(people)}. <b>{people[i][1]}</b>:")
        return
    await state.update_data(done=True)
    lines = [f"{n}: " + (f"{scores[str(e)]:g}" if scores[str(e)] is not None else "qatnashmadi") for e, n in people]
    if d.get("absent"):
        lines.append(f"(+ darsga kelmagan {d['absent']} ta — qatnashmadi)")
    await m.answer("<b>Tekshiring:</b>\n" + "\n".join(lines),
                   reply_markup=IM(inline_keyboard=[[IB(text="✅ Saqlash va yuborish", callback_data="exsave"),
                                                     IB(text="✏️ Qaytadan", callback_data="exagain")]]))


@router.callback_query(F.data == "exagain")
async def res_again(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    if not d.get("people"):
        await c.answer("Qaytadan boshlang.", show_alert=True)
        return
    await state.update_data(i=0, done=False, scores={k: v for k, v in d["scores"].items()
                                                     if k not in {str(e) for e, _ in d["people"]}})
    await state.set_state(Ex.score)
    await c.message.edit_text(f"1/{len(d['people'])}. <b>{d['people'][0][1]}</b>:")


@router.callback_query(F.data == "exsave")
async def res_save(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    if not d.get("done"):
        await c.answer("Avval barcha o'quvchilar natijasini kiriting.", show_alert=True)
        return
    await state.clear()
    msgs = []
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        rules = await S.get_rules(s)
        ex = await s.get(Exam, d["eid"])
        g = await s.get(Group, ex.group_id)
        ex.kind, ex.max_score, ex.status, ex.done_at = d["kind"], d["max"], "otdi", now().replace(tzinfo=None)
        took = 0
        for eid_s, val in d["scores"].items():
            eid = int(eid_s)
            r = (await s.execute(select(ExamResult).where(ExamResult.exam_id == ex.id,
                                                          ExamResult.enrollment_id == eid))).scalar_one_or_none()
            if not r:
                r = ExamResult(exam_id=ex.id, enrollment_id=eid)
                s.add(r)
            r.score = val
            r.percent = round(val / d["max"] * 100, 1) if val is not None else None
            await s.flush()
            enr = await s.get(Enrollment, eid)
            stu = await s.get(Student, enr.student_id)
            await _apply_coins(s, r, stu, rules, who.staff, ex)
            if val is None:
                continue
            took += 1
            text = (f"📝 <b>Nazorat natijasi</b> · {g.name} · {ex.date:%d.%m}\n{stu.full_name}: "
                    f"<b>{val:g} / {d['max']:g}</b> ({r.percent:.0f}%)"
                    + (f"\n🎉 +{r.coins_given} coin" if r.coins_given else ""))
            for t in await S.parent_tg_ids(s, stu.id):
                msgs.append((t, text, r.id))
        await S.audit(s, who.staff, "nazorat", group=g.name, date=ex.date.isoformat(), kind=d["kind"])
        await s.commit()
    kb = lambda rid: IM(inline_keyboard=[[IB(text="📝 Apellyatsiya", callback_data=f"apl:{rid}")]])
    for t, text, rid in msgs:
        await send(bot, t, text + f"\n\nNatijaga e'tirozingiz bo'lsa — {APPEAL_HOURS} soat ichida:", reply_markup=kb(rid))
    await c.message.edit_text(f"✅ Saqlandi va yuborildi. Qatnashdi: {took} ta o'quvchi.")


async def _apply_coins(s, r: ExamResult, stu: Student, rules: dict, by: Staff | None, ex: Exam) -> None:
    """Foiz chegaradan yuqori bo'lsa coin; natija o'zgarsa farqi to'g'rilanadi."""
    want = int(rules.get("exam_coin", 2)) if (r.percent or 0) > float(rules.get("exam_pct", 75)) else 0
    delta = want - (r.coins_given or 0)
    if not delta:
        return
    if delta > 0 and not await S.coins_allowed(s, stu, ex.date):
        return
    await S.add_coins(s, stu, delta, "nazorat", f"Nazorat {ex.date:%d.%m}", by)
    r.coins_given = want


# ---------- apellyatsiya ----------
class Ap(StatesGroup):
    text = State()
    new_score = State()
    reject = State()


@router.callback_query(F.data.startswith("apl:"))
async def appeal_start(c: CallbackQuery, state: FSMContext) -> None:
    rid = int(c.data.split(":")[1])
    async with Session() as s:
        r = await s.get(ExamResult, rid)
        ex = await s.get(Exam, r.exam_id)
    if ex.done_at and now().replace(tzinfo=None) - ex.done_at > timedelta(hours=APPEAL_HOURS):
        await c.answer(f"Apellyatsiya muddati ({APPEAL_HOURS} soat) o'tgan.", show_alert=True)
        return
    if r.appeals >= APPEAL_MAX:
        await c.answer(f"Bu nazorat bo'yicha ko'pi bilan {APPEAL_MAX} ta apellyatsiya berish mumkin.", show_alert=True)
        return
    await state.set_state(Ap.text)
    await state.update_data(rid=rid)
    await c.answer()
    await c.message.answer("E'tirozingizni yozing: qaysi savol (raqami) va nega javobingiz to'g'ri deb "
                           "hisoblaysiz. Ustoz ko'rib chiqadi.\n\nBekor qilish: /start")


@router.message(Ap.text)
async def appeal_text(m: Message, state: FSMContext, bot: Bot) -> None:
    text = (m.text or "").strip()
    if len(text) < 5:
        await m.answer("Batafsilroq yozing.")
        return
    d = await state.get_data()
    await state.clear()
    qn = re.search(r"\d+", text)
    async with Session() as s:
        r = await s.get(ExamResult, d["rid"])
        ex = await s.get(Exam, r.exam_id)
        g = await s.get(Group, ex.group_id)
        enr = await s.get(Enrollment, r.enrollment_id)
        stu = await s.get(Student, enr.student_id)
        ap = Appeal(result_id=r.id, from_tg=m.from_user.id, text=text[:1500], question_no=int(qn.group()) if qn else None)
        s.add(ap)
        r.appeals += 1
        await s.flush()
        to = {x for x in (ex.conductor_id, g.teacher_id) if x}
        tgs = [st.tg_id for st in [await s.get(Staff, i) for i in to] if st and st.tg_id]
        await s.commit()
        apid = ap.id
    await m.answer("✅ Apellyatsiya ustozga yuborildi. Javob shu yerga keladi.")
    kb = IM(inline_keyboard=[[IB(text="✅ To'g'ri — natijani o'zgartirish", callback_data=f"apa:{apid}")],
                             [IB(text="❌ Rad etish", callback_data=f"apr:{apid}")]])
    detail = ""
    if qn:
        from app.bot.tests_flow import paper_question_info
        info = await paper_question_info(d["rid"], int(qn.group()))
        detail = f"\n\n{info}" if info else ""
    for t in tgs:
        await send(bot, t, f"📝 <b>Apellyatsiya</b> · {g.name} · {ex.date:%d.%m}\n{stu.full_name}: "
                           f"{r.score:g}/{ex.max_score:g}\n\n«{text[:900]}»{detail}", reply_markup=kb)


@router.callback_query(F.data.startswith(("apa:", "apr:")))
async def appeal_decide(c: CallbackQuery, state: FSMContext) -> None:
    kind, apid = c.data.split(":")
    async with Session() as s:
        ap = await s.get(Appeal, int(apid))
    if ap.status != "yangi":
        await c.answer("Bu apellyatsiya allaqachon ko'rib chiqilgan.", show_alert=True)
        return
    await state.update_data(apid=int(apid))
    if kind == "apa":
        await state.set_state(Ap.new_score)
        await c.message.answer("Yangi natijani yozing (ball):")
    else:
        await state.set_state(Ap.reject)
        await c.message.answer("Rad etish sababini qisqa yozing:")
    await c.answer()


@router.message(Ap.new_score)
async def appeal_accept(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    async with Session() as s:
        ap = await s.get(Appeal, d["apid"])
        r = await s.get(ExamResult, ap.result_id)
        ex = await s.get(Exam, r.exam_id)
        try:
            val = float((m.text or "").replace(",", "."))
            assert 0 <= val <= ex.max_score
        except (ValueError, AssertionError):
            await m.answer(f"0 dan {ex.max_score:g} gacha raqam yozing.")
            return
        who = await S.who_by_tg(s, m.from_user.id)
        old = r.score
        r.score, r.percent = val, round(val / ex.max_score * 100, 1)
        enr = await s.get(Enrollment, r.enrollment_id)
        stu = await s.get(Student, enr.student_id)
        await _apply_coins(s, r, stu, await S.get_rules(s), who.staff, ex)
        ap.status, ap.decided_by, ap.reply = "qabul", who.staff.id if who.staff else None, f"{old:g} → {val:g}"
        await S.audit(s, who.staff, "apellyatsiya", student=stu.full_name, result=f"{old:g}->{val:g}")
        await s.commit()
        to, pct, mx = ap.from_tg, r.percent, ex.max_score
    await state.clear()
    await m.answer("✅ Natija o'zgartirildi, o'quvchiga xabar yuborildi.")
    await send(bot, to, f"✅ Apellyatsiyangiz qabul qilindi. Yangi natija: <b>{val:g} / {mx:g}</b> ({pct:.0f}%).")


@router.message(Ap.reject)
async def appeal_reject(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    reason = (m.text or "").strip()[:500] or "—"
    async with Session() as s:
        ap = await s.get(Appeal, d["apid"])
        who = await S.who_by_tg(s, m.from_user.id)
        ap.status, ap.decided_by, ap.reply = "rad", who.staff.id if who.staff else None, reason
        await S.audit(s, who.staff, "apellyatsiya", result="rad", reason=reason)
        await s.commit()
        to = ap.from_tg
    await state.clear()
    await m.answer("Rad etildi, o'quvchiga javob yuborildi.")
    await send(bot, to, f"❌ Apellyatsiyangiz rad etildi.\nUstoz izohi: {reason}")


# ---------- 🛂 Ruxsat (rahbar) ----------
class Lv(StatesGroup):
    day = State()
    reason = State()


@router.message(F.text == K.B_LEAVE)
async def leave_start(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            return
        staff = (await s.execute(select(Staff).where(Staff.active, Staff.id != who.staff.id)
                                 .order_by(Staff.full_name))).scalars().all()
    kb = [[IB(text=x.full_name, callback_data=f"lv:{x.id}")] for x in staff]
    await m.answer("🛂 Kimga ruxsat berasiz? (shu kuni jarima yozilmaydi, nazorat keyingi darsga ko'chadi)",
                   reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("lv:"))
async def leave_who(c: CallbackQuery, state: FSMContext) -> None:
    sid = int(c.data.split(":")[1])
    t = now().date()
    await state.set_state(Lv.day)
    await state.update_data(sid=sid)
    kb = [[IB(text=f"{lbl} ({d:%d.%m})", callback_data=f"lvd:{d.isoformat()}")]
          for lbl, d in (("Bugun", t), ("Ertaga", t + timedelta(1)), ("Indinga", t + timedelta(2)))]
    await c.message.edit_text("Qaysi kunga? Boshqa sana bo'lsa yozing: <i>20.11</i>", reply_markup=IM(inline_keyboard=kb))


@router.callback_query(Lv.day, F.data.startswith("lvd:"))
async def leave_day_btn(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(day=c.data.split(":")[1])
    await state.set_state(Lv.reason)
    await c.message.edit_text("Sababini qisqa yozing (masalan: kasal, oilaviy):")


@router.message(Lv.day)
async def leave_day_text(m: Message, state: FSMContext) -> None:
    x = re.findall(r"\d+", m.text or "")
    t = now().date()
    try:
        d = date(t.year, int(x[1]), int(x[0]))
        if d < t - timedelta(days=7):
            d = d.replace(year=t.year + 1)
    except (IndexError, ValueError):
        await m.answer("Sanani kun.oy ko'rinishida yozing: 20.11")
        return
    await state.update_data(day=d.isoformat())
    await state.set_state(Lv.reason)
    await m.answer("Sababini qisqa yozing (masalan: kasal, oilaviy):")


@router.message(Lv.reason)
async def leave_save(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    day = date.fromisoformat(d["day"])
    reason = (m.text or "").strip()[:200]
    moved = []
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        st = await s.get(Staff, d["sid"])
        if not await on_leave(s, st.id, day):
            s.add(StaffLeave(staff_id=st.id, date=day, reason=reason, by_id=who.staff.id))
        for ex in (await s.execute(select(Exam).where(Exam.conductor_id == st.id, Exam.date == day,
                                                      Exam.status == "reja"))).scalars():
            g = await s.get(Group, ex.group_id)
            nxt = day + timedelta(days=1)
            while not is_lesson_day(nxt, g.days):
                nxt += timedelta(days=1)
            if monday(nxt) != ex.week:
                # shu haftada dars qolmadi — bu nazorat bekor, keyingi haftaning nazorati odatdagidek
                ex.status, ex.moved_note = "bekor", f"Ruxsat ({day:%d.%m}) sababli bekor qilindi"
                moved.append((g, None, await _family_targets(s, g, day)))
            else:
                ex.date, ex.moved_note = nxt, f"Ruxsat ({day:%d.%m}) sababli ko'chirildi"
                moved.append((g, nxt, await _family_targets(s, g, nxt)))
        await S.audit(s, who.staff, "ruxsat", staff=st.full_name, date=day.isoformat(), reason=reason)
        await s.commit()
        name, tg = st.full_name, st.tg_id
    await state.clear()
    info = "".join(f"\n• {g.name}: nazorat → {fmt_day(n)}" if n else f"\n• {g.name}: shu haftadagi nazorat bekor"
                   for g, n, _ in moved)
    await m.answer(f"✅ {name}ga {day:%d.%m} uchun ruxsat berildi." + (info or "\nShu kuni nazorati yo'q."))
    if tg:
        await send(bot, tg, f"🛂 Sizga {day:%d.%m} uchun ruxsat berildi." + info)
    for g, n, targets in moved:
        for t in targets:
            await send(bot, t, f"📝 «{g.name}» nazorati {fmt_day(n)} kuniga ko'chirildi." if n
                       else f"📝 «{g.name}» guruhida {day:%d.%m} dagi nazorat bekor qilindi.")


# ---------- bugungi dars kartasida ----------
async def today_exam_button(group_id: int, d: date) -> IM | None:
    async with Session() as s:
        ex = (await s.execute(select(Exam).where(Exam.group_id == group_id, Exam.date == d,
                                                 Exam.status == "reja"))).scalars().first()
    if not ex:
        return None
    return IM(inline_keyboard=[[IB(text="📝 Bugungi nazorat natijalarini kiritish", callback_data=f"exr:{ex.id}")]])
