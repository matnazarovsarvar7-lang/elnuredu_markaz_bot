"""🏆 Yutuqlar: kiritish (hujjat bilan) -> rahbar tasdig'i -> o'quvchiga coin, o'qituvchiga yutuq bali; 📜 markaz tarixi."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.bot import keyboards as K
from app.bot.notify import send, send_photo
from app.config import settings
from app.db.models import Achievement, BonusLedger, Enrollment, Group, Staff, Student
from app.db.session import Session
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
BALL_SOM = 10_000
KINDS = {
    "sertifikat": ("Milliy sertifikat", {"A+": 5, "A": 5, "B+": 3, "B": 3, "C+": 2, "C": 2}),
    "otm": ("OTMga kirdi", {"grant": 5, "kontrakt": 3}),
    "maktab": ("Prezident / Al-Xorazmiy / ixtisoslashtirilgan maktab", {"-": 5}),
    "litsey": ("Harbiy / akademik litsey, bilim yurti", {"-": 3}),
    "olimpiada": ("Olimpiada", {"viloyat": 5, "respublika": 10}),
}
FINISH = {"sertifikat"}          # shu yutuq bilan o'qishni tugatsa — keyingi yutuqlarga xodim bali yo'q


def today() -> date:
    return datetime.now(TZ).date()


def academic_year(d: date) -> str:
    y = d.year if d.month >= 9 else d.year - 1
    return f"{y}-{y + 1}"


class Ac(StatesGroup):
    detail = State()
    region = State()
    doc = State()


@router.message(F.text == K.B_ACHIEVE)
async def ac_start(m: Message, state: FSMContext) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        st = who.staff
        if not (st and st.current_role in ("oqituvchi", "admin", "moliya", "rahbar")):
            return
        if st.current_role == "oqituvchi":
            gs = await S.teacher_groups(s, st, "oqituvchi")
        else:
            gs = (await s.execute(select(Group).where(Group.active).order_by(Group.name))).scalars().all()
    await state.clear()
    await m.answer("🏆 <b>Yutuq kiritish</b>\nO'quvchi qaysi guruhda o'qigan (o'qiydi)?",
                   reply_markup=IM(inline_keyboard=[[IB(text=g.name, callback_data=f"acg:{g.id}")] for g in gs]))


@router.callback_query(F.data.startswith("acg:"))
async def ac_group(c: CallbackQuery, state: FSMContext) -> None:
    gid = int(c.data.split(":")[1])
    since = today() - timedelta(days=400)
    async with Session() as s:
        rows = (await s.execute(select(Enrollment).where(Enrollment.group_id == gid,
                                                         Enrollment.active | (Enrollment.left_date >= since))
                                .options(selectinload(Enrollment.student)))).scalars().all()
    rows = sorted(rows, key=lambda e: e.student.full_name)
    await state.update_data(gid=gid)
    await c.message.edit_text("O'quvchini tanlang:", reply_markup=IM(inline_keyboard=[
        [IB(text=e.student.full_name + ("" if e.active else " (bitirgan)"), callback_data=f"acs:{e.student_id}")]
        for e in rows]))


@router.callback_query(F.data.startswith("acs:"))
async def ac_student(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(sid=int(c.data.split(":")[1]))
    await c.message.edit_text("Yutuq turi:", reply_markup=IM(inline_keyboard=[
        [IB(text=v[0], callback_data=f"ack:{k}")] for k, v in KINDS.items()]))


@router.callback_query(F.data.startswith("ack:"))
async def ac_kind(c: CallbackQuery, state: FSMContext) -> None:
    kind = c.data.split(":")[1]
    await state.update_data(kind=kind)
    levels = KINDS[kind][1]
    if list(levels) == ["-"]:
        await state.update_data(level=None)
        await state.set_state(Ac.detail)
        await c.message.edit_text("Muassasa nomi (masalan: Xiva Prezident maktabi):")
        return
    await c.message.edit_text("Darajasi:", reply_markup=IM(inline_keyboard=[
        [IB(text=f"{lv} (+{p} ball)", callback_data=f"acl:{lv}") for lv, p in list(levels.items())[i:i + 3]]
        for i in range(0, len(levels), 3)]))


@router.callback_query(F.data.startswith("acl:"))
async def ac_level(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(level=c.data.split(":", 1)[1])
    await state.set_state(Ac.detail)
    d = await state.get_data()
    hint = {"sertifikat": "Fan va ball (masalan: Matematika, 63 ball)", "otm": "OTM va yo'nalish (masalan: TATU, Dasturiy injiniring)",
            "olimpiada": "Fan va o'rin (masalan: Fizika, 2-o'rin)"}.get(d["kind"], "Izoh")
    await c.message.edit_text(f"{hint}:")


@router.message(Ac.detail)
async def ac_detail(m: Message, state: FSMContext) -> None:
    await state.update_data(detail=(m.text or "").strip()[:200])
    await state.set_state(Ac.region)
    await m.answer("O'quvchi qayerdan (viloyat, tuman)? Masalan: Xorazm, Urganch tumani")


@router.message(Ac.region)
async def ac_region(m: Message, state: FSMContext) -> None:
    await state.update_data(region=(m.text or "").strip()[:80])
    await state.set_state(Ac.doc)
    await m.answer("📎 Hujjat rasmini yuboring (sertifikat, buyruq, diplom):")


@router.message(Ac.doc, F.photo | F.document)
async def ac_save(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    await state.clear()
    fid = m.photo[-1].file_id if m.photo else m.document.file_id
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        g = await s.get(Group, d["gid"])
        stu = await s.get(Student, d["sid"])
        pts = KINDS[d["kind"]][1].get(d.get("level") or "-", 0)
        a = Achievement(student_id=stu.id, group_id=g.id, teacher_id=g.teacher_id, kind=d["kind"], level=d.get("level"),
                        detail=d["detail"], region=d["region"], file_id=fid, points=pts, year=academic_year(today()),
                        by_id=who.staff.id)
        s.add(a)
        if not stu.region:
            stu.region = d["region"]
        await s.flush()
        await S.audit(s, who.staff, "yutuq", student=stu.full_name, kind=d["kind"])
        await s.commit()
        aid = a.id
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        label = f"{KINDS[d['kind']][0]}" + (f" {d['level']}" if d.get("level") else "")
        text = (f"🏆 <b>Yutuq — tasdiqlang</b>\n{stu.full_name} · {g.name}\n{label}: {d['detail']}\n"
                f"Hudud: {d['region']}\nKiritdi: {who.staff.full_name}")
    await m.answer("✅ Yutuq rahbar tasdig'iga yuborildi.")
    kb = IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=f"acy:{aid}"), IB(text="❌ Rad", callback_data=f"acn:{aid}")]])
    for b in bosses:
        if m.photo:
            await send_photo(bot, b, fid, text, reply_markup=kb)
        else:
            await bot.send_document(b, fid, caption=text, reply_markup=kb)


@router.callback_query(F.data.startswith(("acy:", "acn:")))
async def ac_decide(c: CallbackQuery, bot: Bot) -> None:
    kind, aid = c.data.split(":")
    msgs = []
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            await c.answer("Faqat rahbar.", show_alert=True)
            return
        a = await s.get(Achievement, int(aid))
        if a.status != "kutilmoqda":
            await c.answer("Allaqachon hal qilingan.", show_alert=True)
            return
        stu = await s.get(Student, a.student_id)
        if kind == "acn":
            a.status, a.approved_by = "rad", who.staff.id
            note = "❌ Rad etildi"
        else:
            a.status, a.approved_by = "tasdiq", who.staff.id
            rules = await S.get_rules(s)
            await S.add_coins(s, stu, int(rules.get("achievement", 10)), "yutuq", f"Yutuq: {KINDS[a.kind][0]}", who.staff)
            finished = (await s.execute(select(Achievement.id).where(
                Achievement.student_id == stu.id, Achievement.status == "tasdiq", Achievement.id != a.id,
                Achievement.kind.in_(FINISH)))).first()
            parts = []
            if a.teacher_id and a.points and not finished:
                s.add(BonusLedger(staff_id=a.teacher_id, month=f"{today():%Y-%m}", role="yutuq", points=a.points,
                                  value=BALL_SOM, amount=a.points * BALL_SOM))
                from app.bot.salary import _fund_add
                await _fund_add(s, "rivoj", -a.points * BALL_SOM)
                t = await s.get(Staff, a.teacher_id)
                parts.append(f"{t.full_name}: +{a.points} ball ({S.fmt_som(a.points * BALL_SOM)})")
                if t.tg_id:
                    msgs.append((t.tg_id, f"🏆 {stu.full_name} yutug'i tasdiqlandi: +{a.points} yutuq bali "
                                          f"({S.fmt_som(a.points * BALL_SOM)}, dekabr/iyunda beriladi)."))
                # moliya: o'quvchida qarz yo'q bo'lsa +2
                y, mth = today().year, today().month
                debt = (await S.student_debt(s, stu.id, y, mth)).debt
                if debt == 0:
                    for f in await S.staff_with_role(s, "moliya"):
                        s.add(BonusLedger(staff_id=f.id, month=f"{y}-{mth:02d}", role="yutuq", points=2, value=BALL_SOM,
                                          amount=2 * BALL_SOM))
                        await _fund_add(s, "rivoj", -2 * BALL_SOM)
                        parts.append(f"{f.full_name}: +2 ball")
            elif finished:
                parts.append("Xodimlarga ball yo'q (o'quvchi avval sertifikat bilan tugatgan)")
            note = "✅ Tasdiqlandi. O'quvchiga +10 coin" + (". " + "; ".join(parts) if parts else "")
            for t in await S.parent_tg_ids(s, stu.id):
                msgs.append((t, f"🏆 Tabriklaymiz! {stu.full_name}: {KINDS[a.kind][0]}"
                                + (f" ({a.level})" if a.level else "") + " — markaz yutuqlar ro'yxatiga kiritildi. +10 coin!"))
        await S.audit(s, who.staff, "yutuq_qaror", achievement=a.id, status=a.status)
        await s.commit()
    try:
        await c.message.edit_caption(caption=(c.message.caption or "") + f"\n\n{note}")
    except Exception:
        await c.message.edit_text(c.message.html_text + f"\n\n{note}")
    for t, text in msgs:
        await send(bot, t, text)


# ================= 📜 markaz tarixi (rahbar) =================
@router.message(F.text == K.B_HISTORY)
async def history(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            return
        rows = (await s.execute(select(Achievement).where(Achievement.status == "tasdiq"))).scalars().all()
        students = (await s.execute(select(Student.status, func.count(Student.id)).group_by(Student.status))).all()
    years: dict[str, list[Achievement]] = {}
    for a in rows:
        years.setdefault(a.year, []).append(a)
    lines = ["📜 <b>Markaz tarixi</b>", "O'quvchilar: " + ", ".join(f"{k} — {v}" for k, v in students), ""]
    if not years:
        lines.append("Hali tasdiqlangan yutuq yo'q.")
    for y in sorted(years, reverse=True):
        lst = years[y]
        lines.append(f"<b>{y} o'quv yili</b> — {len(lst)} ta yutuq")
        for k, (name, _) in KINDS.items():
            ks = [a for a in lst if a.kind == k]
            if ks:
                lv = {}
                for a in ks:
                    lv[a.level or ""] = lv.get(a.level or "", 0) + 1
                det = ", ".join(f"{l}: {n}" for l, n in sorted(lv.items()) if l)
                lines.append(f"• {name}: {len(ks)}" + (f" ({det})" if det else ""))
        reg: dict[str, int] = {}
        for a in lst:
            key = (a.region or "—").split(",")[0].strip()
            reg[key] = reg.get(key, 0) + 1
        top = sorted(reg.items(), key=lambda x: -x[1])[:5]
        lines.append("Hududlar: " + ", ".join(f"{r} — {n}" for r, n in top))
        lines.append("")
    await m.answer("\n".join(lines))


async def alumni_job(bot: Bot) -> None:
    """5-avgust: o'quv yilida bitirgan/ketgan o'quvchilar natijasini so'rash uchun adminga ro'yxat."""
    since = today() - timedelta(days=330)
    async with Session() as s:
        rows = (await s.execute(select(Student).join(Enrollment, Enrollment.student_id == Student.id).where(
            Enrollment.left_date >= since).distinct())).scalars().all()
        grads = [st for st in rows if (st.grade or 0) >= 11 or st.status == "ketgan"]
        to = [x.tg_id for x in await S.staff_with_role(s, "admin") if x.tg_id]
    if not grads:
        return
    text = ("🎓 <b>Bitiruvchilar bilan bog'laning</b>\nQuyidagi o'quvchilardan OTM, sertifikat va boshqa natijalarini "
            "so'rang va «🏆 Yutuq kiritish» orqali kiriting:\n" + "\n".join(f"• {g.full_name}" for g in grads[:60]))
    for t in to:
        await send(bot, t, text)
