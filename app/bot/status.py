"""O'quvchini muzlatish, qaytarish, guruhdan chiqarish; guruhni to'xtatish (yoz); ketish xavfi."""
from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.bot.notify import send
from app.config import settings
from app.db.models import Attendance, Enrollment, Freeze, Group, Lesson, Payment, Staff, StaffPoint, Student
from app.db.session import Session
from app.logic.calendar import is_lesson_day, lesson_dates
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
FREEZE_ROLES = ("rahbar", "admin", "moliya", "oqituvchi")
LEAVE_ROLES = ("rahbar", "admin", "moliya")
LEAVE_REASONS = {"kochdi": "Ko'chib ketdi", "narx": "Narx qimmat", "vaqt": "Vaqt to'g'ri kelmadi",
                 "markaz": "Boshqa markazga o'tdi", "maqsad": "Maqsadiga erishdi", "natija": "Natijadan norozi",
                 "ustoz": "O'qituvchidan norozi", "boshqa": "Boshqa sabab"}
UNHAPPY = {"natija", "ustoz"}


def today() -> date:
    return datetime.now(TZ).date()


def month_end(d: date) -> date:
    return d.replace(day=monthrange(d.year, d.month)[1])


def parse_day(text: str, base: date) -> date | None:
    x = re.findall(r"\d+", text or "")
    try:
        d = date(base.year, int(x[1]), int(x[0]))
    except (IndexError, ValueError):
        return None
    if d < base - timedelta(days=60):
        d = d.replace(year=base.year + 1)
    return d


async def _role(tg: int) -> tuple[Staff | None, str]:
    async with Session() as s:
        who = await S.who_by_tg(s, tg)
    return who.staff, (who.staff.current_role if who.staff else "")


async def _family(s, student_id: int) -> list[int]:
    return await S.parent_tg_ids(s, student_id)


async def _sync_student_status(s, stu: Student) -> None:
    enrs = (await s.execute(select(Enrollment).where(Enrollment.student_id == stu.id, Enrollment.active))).scalars().all()
    if not enrs:
        stu.status = "ketgan"
        return
    t = today()
    frozen = [e for e in enrs if await S.is_frozen(s, await s.get(Enrollment, e.id, options=[selectinload(Enrollment.group)]), t)]
    stu.status = "muzlatilgan" if len(frozen) == len(enrs) else "faol"


# ================= o'quvchi-guruh menyusi (o'quvchi kartasidan) =================
@router.callback_query(F.data.startswith("en:"))
async def enr_menu(c: CallbackQuery) -> None:
    eid = int(c.data.split(":")[1])
    st, role = await _role(c.from_user.id)
    async with Session() as s:
        e = await s.get(Enrollment, eid, options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
        fr = [f for f in await S.freezes_for(s, e) if f.enrollment_id == eid and f.end >= today()]
    lines = [f"<b>{e.student.full_name}</b> · {e.group.name}", f"Qo'shilgan: {e.join_date:%d.%m.%Y}"]
    rows = []
    if fr:
        f = fr[-1]
        lines.append(f"❄️ Muzlatilgan: {f.start:%d.%m} – {f.end:%d.%m} ({f.reason or ''})")
        if role in FREEZE_ROLES:
            rows.append([IB(text="▶️ Muzlatishdan qaytdi", callback_data=f"fzr:{f.id}")])
    elif role in FREEZE_ROLES:
        rows.append([IB(text="❄️ Muzlatish", callback_data=f"fz:{eid}")])
    if role in LEAVE_ROLES:
        rows.append([IB(text="🔀 Boshqa guruhga ko'chirish", callback_data=f"mv:{eid}")])
        rows.append([IB(text="✏️ Xato guruhga yozilgan — tuzatish", callback_data=f"rgfx:{eid}")])
        rows.append([IB(text="🚪 Guruhdan chiqarish", callback_data=f"lvq:{eid}")])
    rows.append([IB(text="⬅️ Kartaga qaytish", callback_data=f"st:{e.student_id}")])
    await c.message.edit_text("\n".join(lines), reply_markup=IM(inline_keyboard=rows))


# ================= ❄️ muzlatish =================
class Fz(StatesGroup):
    start = State()
    end = State()
    reason = State()


@router.callback_query(F.data.startswith("fz:"))
async def fz_start(c: CallbackQuery, state: FSMContext) -> None:
    t = today()
    await state.clear()
    await state.set_state(Fz.start)
    await state.update_data(eid=int(c.data.split(":")[1]))
    kb = [[IB(text=f"Bugundan ({t:%d.%m})", callback_data=f"fzs:{t.isoformat()}"),
           IB(text=f"Ertadan ({t + timedelta(1):%d.%m})", callback_data=f"fzs:{(t + timedelta(1)).isoformat()}")]]
    await c.message.edit_text("❄️ Qaysi kundan muzlatiladi? Boshqa sana: <i>20.11</i>", reply_markup=IM(inline_keyboard=kb))


async def _ask_end(msg: Message, state: FSMContext, start: date) -> None:
    await state.update_data(start=start.isoformat())
    await state.set_state(Fz.end)
    me = month_end(start)
    opts = [("Oy oxirigacha", me), ("2 hafta", start + timedelta(days=13)), ("1 oy", start + timedelta(days=30))]
    kb = [[IB(text=f"{lbl} ({d:%d.%m})", callback_data=f"fze:{d.isoformat()}")] for lbl, d in opts]
    await msg.answer("Qaysi kungacha (shu kun ham kiradi)? Boshqa sana: <i>10.12</i>\n"
                     "Muddat: kamida 4 dars, ko'pi bilan 1 oy.", reply_markup=IM(inline_keyboard=kb))


@router.callback_query(Fz.start, F.data.startswith("fzs:"))
async def fz_start_btn(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await _ask_end(c.message, state, date.fromisoformat(c.data.split(":")[1]))


@router.message(Fz.start)
async def fz_start_text(m: Message, state: FSMContext) -> None:
    d = parse_day(m.text, today())
    if not d or d < today() - timedelta(days=7):
        await m.answer("Sanani kun.oy ko'rinishida yozing (ko'pi bilan 7 kun oldingi): 20.11")
        return
    await _ask_end(m, state, d)


async def _set_end(msg: Message, state: FSMContext, end: date) -> None:
    d = await state.get_data()
    start = date.fromisoformat(d["start"])
    async with Session() as s:
        e = await s.get(Enrollment, d["eid"], options=[selectinload(Enrollment.group)])
        days = [x for x in (start + timedelta(i) for i in range((end - start).days + 1)) if is_lesson_day(x, e.group.days)]
    if end < start or (end - start).days > 31:
        await msg.answer("Muddat 1 oydan oshmasligi kerak. Boshqa sana yozing:")
        return
    if len(days) < 4:
        await msg.answer(f"Bu oraliqda {len(days)} ta dars bor — kamida 4 ta dars bo'lishi kerak. Boshqa sana yozing:")
        return
    await state.update_data(end=end.isoformat(), n=len(days))
    await state.set_state(Fz.reason)
    await msg.answer(f"{start:%d.%m} – {end:%d.%m}: {len(days)} ta dars.\nSababini yozing (masalan: kasal, safarda):")


@router.callback_query(Fz.end, F.data.startswith("fze:"))
async def fz_end_btn(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await _set_end(c.message, state, date.fromisoformat(c.data.split(":")[1]))


@router.message(Fz.end)
async def fz_end_text(m: Message, state: FSMContext) -> None:
    d = parse_day(m.text, today())
    if not d:
        await m.answer("Sanani kun.oy ko'rinishida yozing: 10.12")
        return
    await _set_end(m, state, d)


@router.message(Fz.reason)
async def fz_save(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    reason = (m.text or "").strip()[:200] or "—"
    start, end = date.fromisoformat(d["start"]), date.fromisoformat(d["end"])
    await state.clear()
    # oy oxirigacha bo'lsa — qayta hisob avtomatik; oy ichida qaytsa — moliya tasdiqlaydi
    recalc = "auto" if end == month_end(end) else "kutilmoqda"
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        e = await s.get(Enrollment, d["eid"], options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
        f = Freeze(enrollment_id=e.id, start=start, end=end, reason=reason, recalc=recalc, by_id=who.staff.id)
        s.add(f)
        await s.flush()
        await _sync_student_status(s, e.student)
        await S.audit(s, who.staff, "muzlatish", student=e.student.full_name, group=e.group.name,
                      date=f"{start:%d.%m}-{end:%d.%m}")
        await s.commit()
        fam = await _family(s, e.student_id)
        teacher = await s.get(Staff, e.group.teacher_id) if e.group.teacher_id else None
        name, gname, fid = e.student.full_name, e.group.name, f.id
    await m.answer(f"❄️ {name} «{gname}» guruhida {start:%d.%m} – {end:%d.%m} muzlatildi ({d['n']} dars). "
                   "Bu davrda eslatma va davomat bo'lmaydi." +
                   ("\nTo'lov avtomatik qayta hisoblandi." if recalc == "auto" else
                    "\nOy ichida qaytgani uchun qayta hisob moliya bo'limiga tasdiqqa yuborildi."))
    for t in fam:
        await send(bot, t, f"❄️ {name} «{gname}» guruhida {start:%d.%m.%Y} – {end:%d.%m.%Y} muzlatildi.")
    if teacher and teacher.tg_id:
        await send(bot, teacher.tg_id, f"❄️ {name} «{gname}» guruhida {start:%d.%m} – {end:%d.%m} muzlatildi.")
    if recalc == "kutilmoqda":
        await _ask_finance(bot, fid)


async def _ask_finance(bot: Bot, fid: int) -> None:
    async with Session() as s:
        f = await s.get(Freeze, fid)
        e = await s.get(Enrollment, f.enrollment_id, options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
        y, mth = f.start.year, f.start.month
        full = await S.month_status(s, e, y, mth)            # hozircha (tasdiqsiz) — to'liq
        info = (e.student.full_name, e.group.name, f.start, f.end, f.reason)
        f.recalc = "tasdiq"
        reduced = await S.month_status(s, e, y, mth)
        await s.rollback()
        fin = [x.tg_id for x in await S.staff_with_role(s, "moliya") if x.tg_id] or \
              [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    name, gname, fs, fe, why = info
    text = (f"🧾 <b>Qayta hisob</b>\n{name} · {gname}\nMuzlatish: {fs:%d.%m} – {fe:%d.%m} "
            f"({why})\n{mth:02d}-oy to'lovi: {S.fmt_som(full.payable)} → <b>{S.fmt_som(reduced.payable)}</b>\n"
            "Rad etilsa — to'liq summa qoladi.")
    kb = IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=f"fzok:{fid}"),
                              IB(text="❌ Rad etish", callback_data=f"fzno:{fid}")]])
    for t in fin:
        await send(bot, t, text, reply_markup=kb)


@router.callback_query(F.data.startswith(("fzok:", "fzno:")))
async def fz_decide(c: CallbackQuery) -> None:
    kind, fid = c.data.split(":")
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role in ("moliya", "rahbar")):
            await c.answer("Ruxsat yo'q.", show_alert=True)
            return
        f = await s.get(Freeze, int(fid))
        if f.recalc != "kutilmoqda":
            await c.answer("Allaqachon hal qilingan.", show_alert=True)
            return
        f.recalc = "tasdiq" if kind == "fzok" else "rad"
        await S.audit(s, who.staff, "qayta_hisob", freeze=f.id, result=f.recalc)
        await s.commit()
    await c.message.edit_text(c.message.html_text + ("\n\n✅ Tasdiqlandi" if kind == "fzok" else "\n\n❌ Rad etildi"))


@router.callback_query(F.data.startswith("fzr:"))
async def fz_return(c: CallbackQuery, bot: Bot) -> None:
    t = today()
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        f = await s.get(Freeze, int(c.data.split(":")[1]))
        if not who.staff or not f:
            await c.answer("Ruxsat yo'q yoki allaqachon hal qilingan.", show_alert=True)
            return
        e = await s.get(Enrollment, f.enrollment_id, options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
        new_end = t - timedelta(days=1)
        if new_end < f.start:
            await s.delete(f)                               # hali boshlanmagan — bekor
            changed = False
        else:
            f.end = new_end
            changed = new_end != month_end(new_end)
            if changed:
                f.recalc = "kutilmoqda"
        await _sync_student_status(s, e.student)
        await S.audit(s, who.staff, "muzlatish_tugadi", student=e.student.full_name, group=e.group.name)
        await s.commit()
        fam = await _family(s, e.student_id)
        name, gname, fid = e.student.full_name, e.group.name, f.id
    await c.message.edit_text(f"▶️ {name} «{gname}» guruhiga bugundan qaytdi." +
                              ("\nOy ichida qaytgani uchun qayta hisob moliyaga tasdiqqa yuborildi." if changed else ""))
    for x in fam:
        await send(bot, x, f"▶️ {name} «{gname}» guruhida darslar davom etadi ({t:%d.%m} dan).")
    if changed:
        await _ask_finance(bot, fid)


# ================= 🚪 guruhdan chiqarish =================
# ================= 🔀 boshqa guruhga ko'chirish =================
def _d2s(d: date) -> str:
    return f"{d:%Y%m%d}"


def _s2d(x: str) -> date:
    return date(int(x[:4]), int(x[4:6]), int(x[6:]))


def _next_lessons(g: Group, start: date, n: int = 3) -> list[date]:
    out, y, mth = [], start.year, start.month
    for _ in range(3):
        out += [d for d in lesson_dates(y, mth, g.days) if d >= start]
        if len(out) >= n:
            break
        y, mth = (y + 1, 1) if mth == 12 else (y, mth + 1)
    return out[:n]


@router.callback_query(F.data.startswith("mv:"))
async def mv_groups(c: CallbackQuery) -> None:
    eid = int(c.data.split(":")[1])
    async with Session() as s:
        e = await s.get(Enrollment, eid, options=[selectinload(Enrollment.student), selectinload(Enrollment.group)])
        busy = set((await s.execute(select(Enrollment.group_id).where(
            Enrollment.student_id == e.student_id, Enrollment.active))).scalars())
        gs = (await s.execute(select(Group).where(Group.active).order_by(Group.name))).scalars().all()
    gs = [g for g in gs if g.id not in busy]
    if not gs:
        await c.answer("Ko'chirish mumkin bo'lgan boshqa guruh yo'q.", show_alert=True)
        return
    await c.answer()
    kb = [[IB(text=g.name, callback_data=f"mvg:{eid}:{g.id}")] for g in gs]
    kb.append([IB(text="⬅️ Orqaga", callback_data=f"en:{eid}")])
    await c.message.edit_text(f"🔀 <b>{e.student.full_name}</b> «{e.group.name}» guruhidan qaysi guruhga o'tadi?",
                              reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("mvg:"))
async def mv_date(c: CallbackQuery) -> None:
    _, eid, gid = c.data.split(":")
    async with Session() as s:
        g = await s.get(Group, int(gid))
    t = today()
    opts = _next_lessons(g, t)
    kb = [[IB(text=f"{d:%d.%m} ({'bugun' if d == t else 'dars'})", callback_data=f"mvd:{eid}:{gid}:{_d2s(d)}")] for d in opts]
    kb.append([IB(text="⬅️ Orqaga", callback_data=f"mv:{eid}")])
    await c.answer()
    await c.message.edit_text(f"📅 «{g.name}» guruhida birinchi darsi qaysi kun?\n"
                              "Eski guruhda shu kundan oldingi darslar hisoblanadi.",
                              reply_markup=IM(inline_keyboard=kb))


def _mv_last_day(d: date) -> date:
    """Eski guruhdagi oxirgi hisob kuni: yangi guruh bugun boshlansa — kecha, keyinroq boshlansa — bugun."""
    t = today()
    return d - timedelta(days=1) if d <= t else t


async def _mv_calc(s, e: Enrollment, last: date) -> tuple[int, int, int, list[tuple[str, int]]]:
    """(eski guruh hisobi oxirgi oyda, to'langan, qarz, ko'chiriladigan [(oy, summa)])."""
    mkey = f"{last:%Y-%m}"
    paid = await S.paid_for_month(s, e.id, mkey)
    if last < e.join_date:
        payable = 0
    else:
        e.left_date = last
        payable = (await S.month_status(s, e, last.year, last.month)).payable
        e.left_date = None
    moves = []
    if paid > payable:
        moves.append((mkey, paid - payable))
    later = (await s.execute(select(Payment.month, Payment.amount).where(
        Payment.enrollment_id == e.id, Payment.status == "tasdiq", Payment.month > mkey))).all()
    agg: dict[str, int] = {}
    for mo, a in later:
        agg[mo] = agg.get(mo, 0) + a
    moves += [(mo, a) for mo, a in sorted(agg.items()) if a > 0]
    return payable, paid, max(0, payable - paid), moves


@router.callback_query(F.data.startswith("mvd:"))
async def mv_confirm(c: CallbackQuery) -> None:
    _, eid, gid, ds = c.data.split(":")
    d = _s2d(ds)
    async with Session() as s:
        e = await s.get(Enrollment, int(eid), options=[selectinload(Enrollment.student), selectinload(Enrollment.group)])
        g = await s.get(Group, int(gid))
        last = _mv_last_day(d)
        payable, paid, debt, moves = await _mv_calc(s, e, last)
        name, old_name, new_name = e.student.full_name, e.group.name, g.name
        disc = e.discount_kind != "none"
        override = e.price_override
        await s.rollback()
    lines = [f"🔀 <b>{name}</b>", f"«{old_name}» → «{new_name}», {d:%d.%m.%Y} dan",
             f"\nEski guruh: oxirgi hisob kuni {last:%d.%m}; {last:%m}-oy hisobi {S.fmt_som(payable)}, to'langan {S.fmt_som(paid)}."]
    if moves:
        lines.append("Yangi guruhga o'tkaziladigan to'lov: " + ", ".join(f"{mo} — {S.fmt_som(a)}" for mo, a in moves))
    if debt:
        lines.append(f"Eski guruh bo'yicha qarz qoladi: <b>{S.fmt_som(debt)}</b>")
    lines.append("Yangi guruhda shu oy to'lovi birinchi darsidan hisoblanadi.")
    if disc:
        lines.append("Chegirma yangi guruhga ham o'tadi.")
    if override:
        lines.append("⚠️ Eski guruhdagi istisno narx yangi guruhga o'tmaydi — oddiy narx bo'ladi.")
    await c.answer()
    await c.message.edit_text("\n".join(lines), reply_markup=IM(inline_keyboard=[
        [IB(text="✅ Ko'chirish", callback_data=f"mvy:{eid}:{gid}:{ds}"), IB(text="❌ Bekor", callback_data=f"en:{eid}")]]))


@router.callback_query(F.data.startswith("mvy:"))
async def mv_save(c: CallbackQuery, bot: Bot) -> None:
    import uuid
    _, eid, gid, ds = c.data.split(":")
    d, t = _s2d(ds), today()
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role in LEAVE_ROLES):
            await c.answer("Ruxsat yo'q.", show_alert=True)
            return
        e = await s.get(Enrollment, int(eid), options=[selectinload(Enrollment.student), selectinload(Enrollment.group)])
        g = await s.get(Group, int(gid))
        dup = (await s.execute(select(Enrollment.id).where(Enrollment.student_id == e.student_id,
                                                           Enrollment.group_id == g.id, Enrollment.active))).first()
        if not e.active or dup:
            await c.answer("Allaqachon ko'chirilgan.", show_alert=True)
            return
        last = _mv_last_day(d)
        _, _, _, moves = await _mv_calc(s, e, last)
        new = (await s.execute(select(Enrollment).where(Enrollment.student_id == e.student_id, Enrollment.group_id == g.id,
                                                        Enrollment.join_date == d))).scalar_one_or_none()
        if new:                        # shu kuni shu guruhdan chiqqan bo'lsa — o'sha yozuv qayta ochiladi
            new.active, new.left_date = True, None
            new.tier, new.discount_kind, new.discount_percent = e.tier, e.discount_kind, e.discount_percent
            new.discount_amount = e.discount_amount
        else:
            new = Enrollment(student_id=e.student_id, group_id=g.id, join_date=d, registered_at=t, tier=e.tier,
                             discount_kind=e.discount_kind, discount_percent=e.discount_percent,
                             discount_amount=e.discount_amount)
            s.add(new)
        e.left_date = last
        e.active = False
        await s.flush()
        batch = str(uuid.uuid4())
        for mo, a in moves:
            for enr_id, amt in ((e.id, -a), (new.id, a)):
                s.add(Payment(enrollment_id=enr_id, month=mo, amount=amt, method="kochirish", status="tasdiq",
                              paid_date=t, batch=batch, entered_by=who.staff.id, confirmed_by=who.staff.id))
        await _sync_student_status(s, e.student)
        await S.audit(s, who.staff, "kochirildi", student=e.student.full_name, old=e.group.name, new=g.name,
                      date=f"{d:%Y-%m-%d}", moved=sum(a for _, a in moves))
        await s.commit()
        fam = await _family(s, e.student_id)
        teachers = {x for x in (e.group.teacher_id, g.teacher_id) if x}
        t_ids = [st.tg_id for st in [await s.get(Staff, x) for x in teachers] if st and st.tg_id]
        name, old_name, new_name, sid = e.student.full_name, e.group.name, g.name, e.student_id
        when = f", soat {g.start_time:%H:%M}" if g.start_time else ""
    await c.answer("Ko'chirildi")
    await c.message.edit_text(f"✅ {name} «{old_name}» → «{new_name}» guruhiga ko'chirildi ({d:%d.%m.%Y} dan).",
                              reply_markup=IM(inline_keyboard=[[IB(text="👤 Kartasi", callback_data=f"st:{sid}")]]))
    for x in fam:
        await send(bot, x, f"🔀 {name} {d:%d.%m} dan «{new_name}» guruhida o'qiydi{when}. "
                           "To'langan pul yangi guruh hisobiga o'tkazildi.")
    for x in t_ids:
        await send(bot, x, f"🔀 {name}: «{old_name}» → «{new_name}» ({d:%d.%m} dan).")


@router.callback_query(F.data.startswith("lvq:"))
async def leave_reason(c: CallbackQuery) -> None:
    eid = c.data.split(":")[1]
    kb = [[IB(text=v, callback_data=f"lvr:{eid}:{k}")] for k, v in LEAVE_REASONS.items()]
    await c.message.edit_text("🚪 Nima sababdan ketmoqda?", reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("lvr:"))
async def leave_confirm(c: CallbackQuery) -> None:
    _, eid, code = c.data.split(":")
    t = today()
    async with Session() as s:
        e = await s.get(Enrollment, int(eid), options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
        name, gname = e.student.full_name, e.group.name
        e.left_date = t
        st = await S.month_status(s, e, t.year, t.month)
        await s.rollback()
    diff = st.paid - st.payable
    money = (f"Bu oy: hisob {S.fmt_som(st.payable)} (oxirgi dars {t:%d.%m} gacha), to'langan {S.fmt_som(st.paid)}.\n"
             + (f"↩️ Qaytariladigan pul: <b>{S.fmt_som(diff)}</b> — rahbar tasdig'iga boradi." if diff > 0 else
                f"Qarz: <b>{S.fmt_som(-diff)}</b>." if diff < 0 else "Hisob teng."))
    await c.message.edit_text(f"🚪 <b>{name}</b> «{gname}» guruhidan chiqariladi.\n"
                              f"Sabab: {LEAVE_REASONS[code]}\n{money}",
                              reply_markup=IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=f"lvy:{eid}:{code}"),
                                                                IB(text="❌ Bekor", callback_data=f"en:{eid}")]]))


@router.callback_query(F.data.startswith("lvy:"))
async def leave_save(c: CallbackQuery, bot: Bot) -> None:
    _, eid, code = c.data.split(":")
    t = today()
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role in LEAVE_ROLES):
            await c.answer("Ruxsat yo'q.", show_alert=True)
            return
        e = await s.get(Enrollment, int(eid), options=[selectinload(Enrollment.group), selectinload(Enrollment.student)])
        if not e.active:
            await c.answer("Allaqachon chiqarilgan.", show_alert=True)
            return
        e.left_date = t
        st = await S.month_status(s, e, t.year, t.month)
        e.active = False
        await _sync_student_status(s, e.student)
        await S.audit(s, who.staff, "ketdi", student=e.student.full_name, group=e.group.name,
                      reason=LEAVE_REASONS[code])
        await s.commit()
        fam = await _family(s, e.student_id)
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        teacher = await s.get(Staff, e.group.teacher_id) if e.group.teacher_id else None
        name, gname, refund = e.student.full_name, e.group.name, st.paid - st.payable
    await c.message.edit_text(f"✅ {name} «{gname}» guruhidan chiqarildi ({t:%d.%m.%Y}). Coinlari va tarixi saqlanadi.")
    for x in fam:
        await send(bot, x, f"{name} «{gname}» guruhidagi o'qishini yakunladi. Biz bilan o'qiganingiz uchun rahmat! "
                           "Har doim qaytishingizni kutamiz.")
    if teacher and teacher.tg_id:
        await send(bot, teacher.tg_id, f"🚪 {name} «{gname}» guruhidan ketdi. Sabab: {LEAVE_REASONS[code]}.")
    for b in bosses:
        if refund > 0:
            await send(bot, b, f"↩️ <b>Pul qaytarish</b>\n{name} · {gname}\nO'tilmagan darslar uchun: "
                               f"<b>{S.fmt_som(refund)}</b>",
                       reply_markup=IM(inline_keyboard=[[IB(text="✅ Qaytarildi deb belgilash", callback_data=f"rf:{eid}:{refund}"),
                                                         IB(text="❌ Qaytarilmaydi", callback_data="rf:0:0")]]))
        if code in UNHAPPY and teacher:
            await send(bot, b, f"⚠️ {name} «{gname}» guruhidan <b>{LEAVE_REASONS[code].lower()}</b> sababli ketdi.\n"
                               f"O'qituvchi ({teacher.full_name}) ga −5 jarima bali yozilsinmi?",
                       reply_markup=IM(inline_keyboard=[[IB(text="Ha, −5", callback_data=f"lvp:{teacher.id}:{eid}"),
                                                         IB(text="Yo'q", callback_data="lvp:0:0")]]))


@router.callback_query(F.data.startswith("rf:"))
async def refund(c: CallbackQuery, state: FSMContext) -> None:
    from app.bot.pin import need_pin
    if not c.data.startswith("rf:0") and await need_pin(c, state):
        return
    _, eid, amount = c.data.split(":")
    if eid == "0":
        await c.message.edit_text(c.message.html_text + "\n\n❌ Qaytarilmaydi.")
        return
    t = today()
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        s.add(Payment(enrollment_id=int(eid), month=f"{t:%Y-%m}", amount=-int(amount), method="naqd", status="tasdiq",
                      paid_date=t, entered_by=who.staff.id, confirmed_by=who.staff.id))
        await S.audit(s, who.staff, "qaytarish", enrollment=int(eid), amount=int(amount))
        await s.commit()
    await c.message.edit_text(c.message.html_text + "\n\n✅ Qaytarildi deb belgilandi.")


@router.callback_query(F.data.startswith("lvp:"))
async def leave_penalty(c: CallbackQuery) -> None:
    _, sid, eid = c.data.split(":")
    if sid == "0":
        await c.message.edit_text(c.message.html_text + "\n\nJarimasiz.")
        return
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        e = await s.get(Enrollment, int(eid), options=[selectinload(Enrollment.student), selectinload(Enrollment.group)])
        s.add(StaffPoint(staff_id=int(sid), points=-5, role="oqituvchi", date=today(),
                         reason=f"O'quvchi norozilik bilan ketdi: {e.student.full_name} ({e.group.name})"))
        await S.audit(s, who.staff, "ketish_jarima", student=e.student.full_name)
        await s.commit()
    await c.message.edit_text(c.message.html_text + "\n\n−5 ball yozildi.")


# ================= ⏸ guruhni to'xtatish (yoz, uzrli sabab) — faqat rahbar =================
class Gp(StatesGroup):
    start = State()
    end = State()
    reason = State()


@router.callback_query(F.data.startswith("gpz:"))
async def gp_start(c: CallbackQuery, state: FSMContext) -> None:
    st, role = await _role(c.from_user.id)
    if role != "rahbar":
        await c.answer("Guruhni faqat rahbar to'xtatadi.", show_alert=True)
        return
    await state.clear()
    await state.set_state(Gp.start)
    await state.update_data(gid=int(c.data.split(":")[1]))
    await c.message.answer("⏸ Guruh qaysi kundan to'xtatiladi? (masalan yozgi ta'til: <i>1.07</i>)")


@router.message(Gp.start)
async def gp_s(m: Message, state: FSMContext) -> None:
    d = parse_day(m.text, today())
    if not d:
        await m.answer("Sanani kun.oy ko'rinishida yozing: 1.07")
        return
    await state.update_data(start=d.isoformat())
    await state.set_state(Gp.end)
    await m.answer("Qaysi kungacha (shu kun ham kiradi)? Masalan: <i>31.08</i>")


@router.message(Gp.end)
async def gp_e(m: Message, state: FSMContext) -> None:
    d = parse_day(m.text, today())
    data = await state.get_data()
    if not d or d < date.fromisoformat(data["start"]):
        await m.answer("Tugash sanasi boshlanishidan keyin bo'lsin: 31.08")
        return
    await state.update_data(end=d.isoformat())
    await state.set_state(Gp.reason)
    await m.answer("Sababi (masalan: yozgi ta'til, o'qituvchi kasal):")


@router.message(Gp.reason)
async def gp_save(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    await state.clear()
    start, end = date.fromisoformat(d["start"]), date.fromisoformat(d["end"])
    reason = (m.text or "").strip()[:200]
    targets: dict[int, None] = {}
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        g = await s.get(Group, d["gid"])
        s.add(Freeze(group_id=g.id, start=start, end=end, reason=reason, recalc="auto", by_id=who.staff.id))
        await S.audit(s, who.staff, "guruh_toxtadi", group=g.name, date=f"{start:%d.%m}-{end:%d.%m}", reason=reason)
        await s.commit()
        for e in (await s.execute(select(Enrollment).where(Enrollment.group_id == g.id, Enrollment.active))).scalars():
            for t in await _family(s, e.student_id):
                targets[t] = None
        gname = g.name
    await m.answer(f"⏸ «{gname}» {start:%d.%m.%Y} – {end:%d.%m.%Y} to'xtatildi. Bu davr uchun to'lov olinmaydi, "
                   "eslatmalar, davomat va nazoratlar bo'lmaydi.")
    for t in targets:
        await send(bot, t, f"⏸ «{gname}» guruhida darslar {start:%d.%m.%Y} – {end:%d.%m.%Y} bo'lmaydi ({reason}). "
                           "Bu davr uchun to'lov olinmaydi.")


# ================= ketish xavfi (har kuni 20:40) =================
async def risk_job(bot: Bot) -> None:
    t = today()
    alerts = []
    async with Session() as s:
        les = (await s.execute(select(Lesson).where(Lesson.date == t, Lesson.confirmed_at.is_not(None)))).scalars().all()
        for l in les:
            g = await s.get(Group, l.group_id)
            absent = (await s.execute(select(Attendance).where(Attendance.lesson_id == l.id,
                                                               Attendance.mark == "y"))).scalars().all()
            for a in absent:
                marks = (await s.execute(select(Attendance.mark).join(Lesson, Lesson.id == Attendance.lesson_id)
                                         .where(Attendance.enrollment_id == a.enrollment_id, Lesson.date <= t)
                                         .order_by(Lesson.date.desc()).limit(4))).scalars().all()
                streak = 0
                for mk in marks:
                    if mk != "y":
                        break
                    streak += 1
                if streak in (2, 3):
                    e = await s.get(Enrollment, a.enrollment_id, options=[selectinload(Enrollment.student)])
                    alerts.append((e.student.full_name, g.name, streak))
        to = {x.tg_id for r in ("rahbar", "admin") for x in await S.staff_with_role(s, r) if x.tg_id}
    if not alerts:
        return
    lines = [f"• {n} · {g} — {k} dars ketma-ket" + (" (muzlatish masalasini ko'ring)" if k == 3 else "")
             for n, g, k in alerts]
    for x in to:
        await send(bot, x, "⚠️ <b>Ketish xavfi</b>\n" + "\n".join(lines))
