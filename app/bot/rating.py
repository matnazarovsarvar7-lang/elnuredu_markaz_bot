"""🏆 O'quvchilar reytingi: nazorat natijalari (60%) + uy vazifasi (40%).

• Vazifa: oxirgi 30 kun, ✅ = 1, ➗ = 0.5, ❌ = 0 (faqat tekshirilgan darslar).
• Nazorat: guruhning oxirgi 4 ta o'tkazilgan nazorati (60 kun ichida); keyingisi og'irroq (1, 1, 1.5, 2).
  Qatnashmagan nazorat — 0%.
• Ball faqat 3+ vazifa va 1+ nazorat bo'lsa chiqadi, aks holda «ma'lumot yig'ilmoqda».
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import select

from app import services as S
from app.bot import keyboards as K
from app.bot.notify import send
from app.config import settings
from app.db.models import Attendance, Enrollment, Exam, ExamResult, Group, Lesson, Setting, Staff
from app.db.session import Session

router = Router()
TZ = ZoneInfo(settings.timezone)
HW_W = {"done": 1.0, "part": 0.5, "none": 0.0}
LEVELS = [(85, "🏆 A'lo"), (70, "🥈 Yaxshi"), (55, "🥉 O'rta"), (0, "⚠️ E'tibor kerak")]
MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}


def today() -> date:
    return datetime.now(TZ).date()


def level(score: float | None) -> str:
    if score is None:
        return "ma'lumot yig'ilmoqda"
    return next(name for lim, name in LEVELS if score >= lim)


@dataclass
class Row:
    eid: int
    sid: int
    name: str
    hw: float | None = None          # foiz
    n_hw: int = 0
    ex: float | None = None          # foiz (og'irlikli)
    n_ex: int = 0
    last_ex: tuple | None = None     # (mavzu, foiz | None)
    score: float | None = None
    rank: int | None = None
    partial: bool = False            # faqat bitta ko'rsatkich bor (dastlabki baho)


async def group_ratings(s, gid: int, until: date | None = None) -> list[Row]:
    until = until or today()
    from sqlalchemy.orm import selectinload
    roster = await S.group_roster(s, gid, until)
    if not roster:                               # guruh to'xtatilgan kun — faol o'quvchilar
        roster = list((await s.execute(select(Enrollment).where(Enrollment.group_id == gid, Enrollment.active)
                                       .options(selectinload(Enrollment.student)))).scalars())
    rows = {e.id: Row(e.id, e.student_id, e.student.full_name if e.student else "?") for e in roster}
    if not rows:
        return []
    # uy vazifasi
    hw = (await s.execute(select(Attendance.enrollment_id, Attendance.homework).join(Lesson, Lesson.id == Attendance.lesson_id)
                          .where(Lesson.group_id == gid, Lesson.date > until - timedelta(days=30), Lesson.date <= until,
                                 Attendance.homework.is_not(None), Attendance.enrollment_id.in_(rows)))).all()
    acc: dict[int, list[float]] = {}
    for eid, h in hw:
        if h in HW_W:
            acc.setdefault(eid, []).append(HW_W[h])
    for eid, xs in acc.items():
        rows[eid].hw, rows[eid].n_hw = sum(xs) / len(xs) * 100, len(xs)
    # nazoratlar
    exams = list((await s.execute(select(Exam).where(Exam.group_id == gid, Exam.status == "otdi", Exam.date <= until,
                                                     Exam.date > until - timedelta(days=60))
                                  .order_by(Exam.date.desc()).limit(4))).scalars())[::-1]
    weights = [1, 1, 1.5, 2][-len(exams):] if exams else []
    res = {(r.exam_id, r.enrollment_id): r for r in (await s.execute(select(ExamResult).where(
        ExamResult.exam_id.in_([e.id for e in exams] or [0])))).scalars()}
    for r in rows.values():
        tot = wsum = 0.0
        for ex, w in zip(exams, weights):
            rr = res.get((ex.id, r.eid))
            if rr is None:
                continue                          # o'sha paytda guruhda bo'lmagan
            pct = rr.percent if rr.score is not None and rr.percent is not None else 0.0
            tot += pct * w
            wsum += w
            r.n_ex += 1
            r.last_ex = (ex.topic or f"{ex.date:%d.%m}", rr.percent if rr.score is not None else None)
        if wsum:
            r.ex = tot / wsum
    for r in rows.values():
        if r.n_hw and r.n_ex:
            r.score = round(0.6 * r.ex + 0.4 * r.hw, 1)
            r.partial = r.n_hw < 3
        elif r.n_ex:                              # vazifa hali belgilanmagan — dastlabki baho nazoratdan
            r.score, r.partial = round(r.ex, 1), True
        elif r.n_hw:                              # nazorat hali bo'lmagan — dastlabki baho vazifadan
            r.score, r.partial = round(r.hw, 1), True
    ranked = sorted(rows.values(), key=lambda r: (r.score is None, -(r.score or 0), r.name))
    k = 0
    prev = None
    for i, r in enumerate(ranked, 1):
        if r.score is None:
            break
        if r.score != prev:
            k, prev = i, r.score
        r.rank = k
    return ranked


def _arrow(now_: float | None, before: float | None) -> str:
    if now_ is None or before is None:
        return ""
    d = round(now_ - before)
    return f" ↑+{d}" if d > 0 else f" ↓{d}" if d < 0 else " ="


async def group_text(s, gid: int, mark_sid: int | None = None, staff_view: bool = False) -> str:
    g = await s.get(Group, gid)
    rows = await group_ratings(s, gid)
    before = {r.eid: r.score for r in await group_ratings(s, gid, today() - timedelta(days=30))}
    scored = [r for r in rows if r.score is not None]
    avg = round(sum(r.score for r in scored) / len(scored)) if scored else None
    out = [f"🏆 <b>{g.name}</b> — reyting (oxirgi 30 kun)"]
    me = next((r for r in rows if r.sid == mark_sid), None) if mark_sid else None
    if me:
        if me.score is None:
            out.append(f"\n👉 <b>{me.name}</b>: ma'lumot yig'ilmoqda (vazifa {me.n_hw}/3, nazorat {me.n_ex}/1)")
        else:
            out.append(f"\n👉 <b>{me.name}</b>: <b>{me.rank}-o'rin / {len(scored)}</b> · {me.score:.0f} ball · "
                       f"{level(me.score)}{_arrow(me.score, before.get(me.eid))}")
            out.append(f"📝 Nazoratlar: {me.ex:.0f}%" + (f" (oxirgi: {me.last_ex[0]} — "
                       + (f"{me.last_ex[1]:.0f}%" if me.last_ex[1] is not None else "qatnashmadi") + ")" if me.last_ex else "")
                       + f"\n📚 Uy vazifasi: {me.hw:.0f}% ({me.n_hw} ta darsdan)")
            if avg is not None:
                mine = round(me.score)
            out.append(f"👥 Guruh o'rtachasi: {avg} — " + ("o'rtachadan yuqori ✅" if mine > avg else
                                                               "o'rtacha darajada" if mine == avg else "o'rtachadan past"))
    out.append("")
    for r in scored:
        tag = "👉 " if me and r.eid == me.eid else ""
        extra = f" (nazorat {r.ex:.0f}%, vazifa {r.hw:.0f}%)" if staff_view else ""
        out.append(f"{tag}{MEDAL.get(r.rank, f'{r.rank}.')} {r.name} — <b>{r.score:.0f}</b>"
                   f"{_arrow(r.score, before.get(r.eid))}{extra}")
    rest = [r for r in rows if r.score is None]
    if rest:
        out.append("\n⏳ Ma'lumot yig'ilmoqda: " + ", ".join(r.name for r in rest))
    if not scored:
        out.append("Hali reyting chiqarish uchun yetarli ma'lumot yo'q (3+ vazifa va 1+ nazorat kerak).")
    out.append("\n<i>Ball = nazoratlar 60% + uy vazifasi 40%.</i>")
    return "\n".join(out)


async def child_line(s, sid: int) -> str:
    """«Farzandim» uchun qisqa qator."""
    lines = []
    for e in await S.student_enrollments(s, sid):
        rows = await group_ratings(s, e.group_id)
        me = next((r for r in rows if r.eid == e.id), None)
        n = sum(1 for r in rows if r.score is not None)
        if me and me.score is not None:
            lines.append(f"🏆 {e.group.name}: {me.rank}-o'rin / {n} · {me.score:.0f} ball · {level(me.score)}")
    return "\n".join(lines)


async def card(s, gid: int, mark_sid: int | None) -> tuple[bytes, str]:
    """Chiroyli reyting rasmi va qisqa izoh."""
    from app.logic.rating_card import CardRow, render
    g = await s.get(Group, gid)
    rows = await group_ratings(s, gid)
    before = {r.eid: r.score for r in await group_ratings(s, gid, today() - timedelta(days=30))}
    scored = [r for r in rows if r.score is not None]
    avg = round(sum(r.score for r in scored) / len(scored)) if scored else None
    me = next((r for r in rows if r.sid == mark_sid), None) if mark_sid else None

    def tr(r: Row) -> int | None:
        b = before.get(r.eid)
        return round(r.score - b) if r.score is not None and b is not None and round(r.score - b) else None

    crow = [CardRow(r.rank, r.name, r.score, tr(r), bool(me and r.eid == me.eid), r.partial) for r in scored]
    medata = None
    if me:
        medata = {"name": me.name, "rank": me.rank, "total": len(scored), "score": me.score, "ex": me.ex, "hw": me.hw,
                  "trend": tr(me), "partial": me.partial}
    png = render(g.name, f"{today():%d.%m.%Y}", crow, medata, avg, [r.name for r in rows if r.score is None])
    if me and me.score is not None:
        cap = (f"🏆 <b>{me.name}</b> — «{g.name}» guruhida <b>{me.rank}-o'rin</b> ({len(scored)} o'quvchidan), "
               f"{me.score:.0f} ball · {level(me.score)}")
        if me.partial:
            cap += "\n<i>Dastlabki baho: nazorat va vazifalar ko'paygan sari aniqlashadi.</i>"
    elif me:
        cap = (f"🏆 «{g.name}»: {me.name} reytingi birinchi nazorat yoki tekshirilgan uy vazifasidan keyin chiqadi.")
    else:
        cap = f"🏆 «{g.name}» reytingi"
    return png, cap


# ---------------- ota-ona / o'quvchi ----------------
@router.message(F.text == K.B_RATING)
async def parent_rating(m: Message) -> None:
    from aiogram.types import BufferedInputFile
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if who.staff and who.staff.current_role in ("rahbar", "oqituvchi", "yordamchi"):
            await _staff_pick(m, s, who.staff)
            return
        kids = list(who.parent.children) if who.parent else ([who.student] if who.student else [])
        cards = []
        for k in kids:
            for e in await S.student_enrollments(s, k.id):
                cards.append(await card(s, e.group_id, k.id))
    if not cards:
        await m.answer("Hozircha reyting yo'q.")
    for png, cap in cards:
        await m.answer_photo(BufferedInputFile(png, "reyting.png"), caption=cap)


# ---------------- o'qituvchi / rahbar ----------------
async def _staff_pick(m: Message, s, st: Staff) -> None:
    q = select(Group).where(Group.active).order_by(Group.name)
    if st.current_role != "rahbar":
        q = q.where((Group.teacher_id == st.id) | (Group.assistant_id == st.id))
    gs = list((await s.execute(q)).scalars())
    await m.answer("🏆 Qaysi guruh reytingi?", reply_markup=IM(inline_keyboard=[
        [IB(text=g.name, callback_data=f"rt:{g.id}")] for g in gs] or [[IB(text="—", callback_data="rt:0")]]))


@router.callback_query(F.data.startswith("rt:"))
async def staff_group(c: CallbackQuery) -> None:
    gid = int(c.data.split(":")[1])
    await c.answer()
    if not gid:
        return
    from aiogram.types import BufferedInputFile
    async with Session() as s:
        png, _ = await card(s, gid, None)
        text = await group_text(s, gid, staff_view=True)
    await c.message.answer_photo(BufferedInputFile(png, "reyting.png"))
    await c.message.answer(text)


# ---------------- haftalik xabar va ogohlantirish (yakshanba) ----------------
async def weekly(bot: Bot) -> None:
    async with Session() as s:
        snap_row = await s.get(Setting, "rating_snap")
        snap = dict(snap_row.value) if snap_row else {}
        new_snap = {}
        msgs: list[tuple[int, str]] = []
        for g in (await s.execute(select(Group).where(Group.active))).scalars():
            rows = await group_ratings(s, g.id)
            n = sum(1 for r in rows if r.score is not None)
            teacher = await s.get(Staff, g.teacher_id) if g.teacher_id else None
            drops = []
            for r in rows:
                if r.score is None:
                    continue
                new_snap[str(r.eid)] = r.score
                old = snap.get(str(r.eid))
                fall = old is not None and old - r.score >= 10
                txt = (f"🏆 <b>Haftalik reyting</b> · {r.name}\n{g.name}: <b>{r.rank}-o'rin / {n}</b> · {r.score:.0f} ball · "
                       f"{level(r.score)}{_arrow(r.score, old)}\n📝 Nazorat {r.ex:.0f}% · 📚 Vazifa {r.hw:.0f}%"
                       + ("\n\n⚠️ Reyting sezilarli tushdi. O'qituvchi bilan gaplashib olishni tavsiya qilamiz." if fall else "")
                       + "\nTo'liq ro'yxat: «🏆 Reyting»")
                for tg in await S.parent_tg_ids(s, r.sid):
                    msgs.append((tg, txt))
                if fall or r.score < 55:
                    drops.append(f"• {r.name}: {r.score:.0f}" + (f" (oldin {old:.0f})" if fall else ""))
            if drops and teacher and teacher.tg_id:
                msgs.append((teacher.tg_id, f"⚠️ <b>{g.name}</b>: e'tibor kerak bo'lgan o'quvchilar\n" + "\n".join(drops)))
        if snap_row:
            snap_row.value = new_snap
        else:
            s.add(Setting(key="rating_snap", value=new_snap))
        await s.commit()
    for tg, txt in msgs:
        await send(bot, tg, txt)


# ---------------- oy yulduzi (har oy 1-sana) ----------------
STAR_COINS = 10


async def month_stars(bot: Bot) -> None:
    end = today() - timedelta(days=1)
    msgs: list[tuple[int, str]] = []
    async with Session() as s:
        if await s.get(Setting, f"stars_{end:%Y-%m}"):
            return
        s.add(Setting(key=f"stars_{end:%Y-%m}", value={"at": f"{today()}"}))
        from app.db.models import Student
        for g in (await s.execute(select(Group).where(Group.active))).scalars():
            rows = [r for r in await group_ratings(s, g.id, end) if r.score is not None]
            if len(rows) < 3:
                continue
            before = {r.eid: r.score for r in await group_ratings(s, g.id, end - timedelta(days=30))}
            best = rows[0]
            grow = max((r for r in rows if before.get(r.eid) is not None), key=lambda r: r.score - before[r.eid], default=None)
            winners = [(best, f"guruhda 1-o'rin ({best.score:.0f} ball)")]
            if grow and grow.eid != best.eid and grow.score - before[grow.eid] >= 5:
                winners.append((grow, f"eng ko'p o'sgan o'quvchi (+{grow.score - before[grow.eid]:.0f} ball)"))
            for r, why in winners:
                stu = await s.get(Student, r.sid)
                await S.add_coins(s, stu, STAR_COINS, "reyting", f"Oy yulduzi {end:%m.%Y}: {g.name}")
                for tg in await S.parent_tg_ids(s, r.sid):
                    msgs.append((tg, f"🌟 <b>Oy yulduzi!</b> {r.name} — «{g.name}» guruhida {end:%m.%Y} oyining {why}. "
                                     f"Tabriklaymiz! 🎉"))
        await s.commit()
    for tg, txt in msgs:
        await send(bot, tg, txt)
