"""Tuzatishlar (3 darajali qoida):
🟡 davomat, vazifa tekshiruvi, nazorat natijasi — shu kuni muallif, keyin rahbar; yopilgan oy — yopiq;
🔴 to'lov, xarajat, xizmat sotuvi — o'chirilmaydi: teskari yozuv (storno), sabab + rahbar + PIN;
🟢 nazorat o'tkazuvchisi, vazifa va ariza matni — vakolatli xodim, tarixga yoziladi."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.bot.notify import send
from app.config import settings
from app.db.models import (Attendance, Enrollment, Exam, ExamResult, Expense, Group, Lead, Lesson, Payment,
                           RecurringTask, Service, ServiceSale, Setting, Staff, Student, Task)
from app.db.session import Session
from app.logic import coins as C
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
MARK_ICON = {"k": "✅", "l": "🕒", "y": "❌"}
MARK_NAME = {"k": "keldi", "l": "kechikdi", "y": "kelmadi"}
NEXT_MARK = {"k": "y", "y": "l", "l": "k"}
HW_ICON = {"done": "✅", "part": "➗", "none": "❌"}
NEXT_HW = {"done": "part", "part": "none", "none": "done"}
FIX_ATT: dict[int, dict[int, str]] = {}
FIX_HW: dict[int, dict[int, str]] = {}


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _who(s, tg: int) -> Staff | None:
    return (await S.who_by_tg(s, tg)).staff


async def month_closed(s, d: date) -> bool:
    return await s.get(Setting, f"closed_{d:%Y-%m}") is not None


async def _bosses(s) -> list[int]:
    return [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]


# =====================================================================
# 🟡 1, 6. Davomat va vazifa tekshiruvini tuzatish
# =====================================================================
async def lesson_fix_error(s, st: Staff | None, les: Lesson) -> str | None:
    if not st:
        return "Ruxsat yo'q."
    if st.current_role == "rahbar":
        if les.date != today() and await month_closed(s, les.date):
            return "Bu oy yopilgan (oylik hisoblangan) — tuzatib bo'lmaydi."
        return None
    if les.date != today():
        return "O'tgan kun darsini faqat rahbar tuzatadi. Rahbarga yozing."
    g = await s.get(Group, les.group_id)
    if st.id in (g.teacher_id, g.assistant_id):
        return None
    return "Bu guruh sizniki emas."


@router.callback_query(F.data.startswith("lfx:"))
async def lesson_fix_menu(c: CallbackQuery) -> None:
    lid = int(c.data.split(":")[1])
    async with Session() as s:
        les = await s.get(Lesson, lid)
        err = await lesson_fix_error(s, await _who(s, c.from_user.id), les)
        g = await s.get(Group, les.group_id)
        has_hw = (await s.execute(select(Attendance.id).where(Attendance.lesson_id == lid,
                                                              Attendance.homework.is_not(None)))).first()
    if err:
        await c.answer(err, show_alert=True)
        return
    rows = [[IB(text="✏️ Davomatni tuzatish", callback_data=f"atf:{lid}")]]
    if has_hw:
        rows.append([IB(text="✏️ Vazifa tekshiruvini tuzatish", callback_data=f"hcf:{lid}")])
    await c.answer()
    await c.message.answer(f"✏️ <b>{g.name}</b> · {les.date:%d.%m.%Y} — nimani tuzatamiz?\n"
                           "Coinlar avtomatik qayta hisoblanadi, ota-onaga «tuzatildi» xabari boradi.",
                           reply_markup=IM(inline_keyboard=rows))


async def _att_rows(s, lid: int) -> list[tuple[int, str, str | None, str | None]]:
    q = (select(Attendance, Student.full_name).join(Enrollment, Enrollment.id == Attendance.enrollment_id)
         .join(Student, Student.id == Enrollment.student_id).where(Attendance.lesson_id == lid)
         .order_by(Student.full_name))
    return [(a.enrollment_id, n, a.mark, a.homework) for a, n in (await s.execute(q)).all()]


def _att_kb(lid: int, rows, draft: dict[int, str]) -> IM:
    kb = [[IB(text=f"{MARK_ICON[draft[e]]} {n}", callback_data=f"atft:{lid}:{e}")] for e, n, _, _ in rows]
    kb.append([IB(text="💾 Tuzatishni saqlash", callback_data=f"atfy:{lid}")])
    return IM(inline_keyboard=kb)


@router.callback_query(F.data.startswith("atf:"))
async def att_fix(c: CallbackQuery) -> None:
    lid = int(c.data.split(":")[1])
    async with Session() as s:
        les = await s.get(Lesson, lid)
        err = await lesson_fix_error(s, await _who(s, c.from_user.id), les)
        rows = await _att_rows(s, lid) if not err else []
    if err or not rows:
        await c.answer(err or "Bu darsda davomat yo'q.", show_alert=True)
        return
    FIX_ATT[lid] = {e: m or "k" for e, _, m, _ in rows}
    await c.answer()
    await c.message.edit_text("✏️ Davomatni tuzating (bosilganda: keldi → kelmadi → kechikdi), keyin «Saqlash»:",
                              reply_markup=_att_kb(lid, rows, FIX_ATT[lid]))


@router.callback_query(F.data.startswith("atft:"))
async def att_fix_toggle(c: CallbackQuery) -> None:
    _, lid, eid = c.data.split(":")
    lid, eid = int(lid), int(eid)
    if lid not in FIX_ATT:
        await c.answer("Qaytadan oching.", show_alert=True)
        return
    FIX_ATT[lid][eid] = NEXT_MARK[FIX_ATT[lid].get(eid, "k")]
    async with Session() as s:
        rows = await _att_rows(s, lid)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=_att_kb(lid, rows, FIX_ATT[lid]))


@router.callback_query(F.data.startswith("atfy:"))
async def att_fix_save(c: CallbackQuery, bot: Bot) -> None:
    lid = int(c.data.split(":")[1])
    draft = FIX_ATT.pop(lid, None)
    if draft is None:
        await c.answer("Qaytadan oching.", show_alert=True)
        return
    from app.bot.handlers import REASONS
    async with Session() as s:
        st = await _who(s, c.from_user.id)
        les = await s.get(Lesson, lid)
        err = await lesson_fix_error(s, st, les)
        if err:
            await c.answer(err, show_alert=True)
            return
        g = await s.get(Group, les.group_id)
        rows = await _att_rows(s, lid)
        names = {e: n for e, n, _, _ in rows}
        changed = {e: m for e, _, old, _ in rows if (m := draft.get(e, old)) != old}
        if not changed:
            await c.answer("O'zgarish yo'q.")
            await c.message.edit_text("O'zgarish yo'q — davomat avvalgidek qoldi.")
            return
        olds = {e: old for e, _, old, _ in rows}
        await S.save_attendance(s, les, changed, st)
        rules = await S.get_rules(s)
        alo = int(rules.get("grade_alo", 1))
        absent_ids, family = [], []
        for e, m in changed.items():
            att = (await s.execute(select(Attendance).where(Attendance.lesson_id == lid,
                                                            Attendance.enrollment_id == e))).scalar_one()
            enr = await s.get(Enrollment, e, options=[selectinload(Enrollment.student)])
            if m == "y":
                # kelmagan o'quvchiga vazifa va baho coinlari qaytariladi
                back = -C.homework_coin(att.homework, rules) - (alo if att.grade == "alo" else 0)
                if back and await S.coins_allowed(s, enr.student, les.date):
                    await S.add_coins(s, enr.student, back, "tuzatish", f"Davomat tuzatildi {les.date:%d.%m}", st)
                att.homework, att.grade = None, None
                absent_ids.append(att.id)
            for t in await S.parent_tg_ids(s, enr.student_id):
                family.append((t, f"✏️ <b>Davomat tuzatildi</b>\n{g.name} · {les.date:%d.%m}\n"
                                  f"{enr.student.full_name}: <b>{MARK_NAME[m]}</b>"))
        await S.audit(s, st, "davomat_tuzatish", lesson_id=lid, group=g.name, date=f"{les.date}",
                      changes={names[e]: f"{olds[e]}->{m}" for e, m in changed.items()})
        await s.commit()
        admins = [x.tg_id for x in await S.staff_with_role(s, "admin") if x.tg_id]
        calls = []
        for aid in absent_ids:
            a = await s.get(Attendance, aid)
            e = await s.get(Enrollment, a.enrollment_id, populate_existing=True,
                            options=[selectinload(Enrollment.student).selectinload(Student.parents)])
            phones = ", ".join(p.phone for p in e.student.parents) or (e.student.phone or "raqam yo'q")
            calls.append((aid, e.student.full_name, phones))
    await c.answer("Saqlandi")
    await c.message.edit_text("✅ Davomat tuzatildi:\n" + "\n".join(f"• {names[e]}: {MARK_NAME[m]}" for e, m in changed.items()))
    for t, text in family:
        await send(bot, t, text)
    for aid, name, phones in calls:
        kb = IM(inline_keyboard=[[IB(text=v, callback_data=f"ab:{aid}:{k}")] for k, v in REASONS.items()])
        for adm in admins:
            await send(bot, adm, f"📞 Darsga kelmadi (davomat tuzatildi): <b>{name}</b>\n{g.name} · {les.date:%d.%m}\n"
                                 f"Tel: {phones}\nQo'ng'iroq qilib, sababini tanlang:", reply_markup=kb)


def _hw_kb(lid: int, rows, draft: dict[int, str]) -> IM:
    kb = [[IB(text=f"{HW_ICON[draft[e]]} {n}", callback_data=f"hcft:{lid}:{e}")] for e, n, m, _ in rows if e in draft]
    kb.append([IB(text="💾 Tuzatishni saqlash", callback_data=f"hcfy:{lid}")])
    return IM(inline_keyboard=kb)


@router.callback_query(F.data.startswith("hcf:"))
async def hw_fix(c: CallbackQuery) -> None:
    lid = int(c.data.split(":")[1])
    async with Session() as s:
        les = await s.get(Lesson, lid)
        err = await lesson_fix_error(s, await _who(s, c.from_user.id), les)
        rows = await _att_rows(s, lid) if not err else []
    rows = [r for r in rows if r[2] != "y" and r[3]]      # faqat tekshirilganlar
    if err or not rows:
        await c.answer(err or "Bu darsda vazifa tekshiruvi kiritilmagan.", show_alert=True)
        return
    FIX_HW[lid] = {e: hw for e, _, _, hw in rows}
    await c.answer()
    await c.message.edit_text("✏️ Vazifa tekshiruvini tuzating (bajardi → qisman → bajarmadi), keyin «Saqlash»:",
                              reply_markup=_hw_kb(lid, rows, FIX_HW[lid]))


@router.callback_query(F.data.startswith("hcft:"))
async def hw_fix_toggle(c: CallbackQuery) -> None:
    _, lid, eid = c.data.split(":")
    lid, eid = int(lid), int(eid)
    if lid not in FIX_HW:
        await c.answer("Qaytadan oching.", show_alert=True)
        return
    FIX_HW[lid][eid] = NEXT_HW[FIX_HW[lid].get(eid, "done")]
    async with Session() as s:
        rows = await _att_rows(s, lid)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=_hw_kb(lid, rows, FIX_HW[lid]))


@router.callback_query(F.data.startswith("hcfy:"))
async def hw_fix_save(c: CallbackQuery) -> None:
    lid = int(c.data.split(":")[1])
    draft = FIX_HW.pop(lid, None)
    if draft is None:
        await c.answer("Qaytadan oching.", show_alert=True)
        return
    async with Session() as s:
        st = await _who(s, c.from_user.id)
        les = await s.get(Lesson, lid)
        err = await lesson_fix_error(s, st, les)
        if err:
            await c.answer(err, show_alert=True)
            return
        rows = await _att_rows(s, lid)
        changed = {e: draft[e] for e, _, m, hw in rows if m != "y" and hw and e in draft and draft[e] != hw}
        if changed:
            await S.save_homework(s, les, changed, st)
            await S.audit(s, st, "vazifa_tuzatish", lesson_id=lid, changes=len(changed))
            await s.commit()
    await c.answer("Saqlandi")
    await c.message.edit_text(f"✅ Vazifa tekshiruvi tuzatildi ({len(changed)} ta o'zgarish). Coinlar qayta hisoblandi."
                              if changed else "O'zgarish yo'q.")


@router.callback_query(F.data.startswith("lsn:"))
async def past_lessons(c: CallbackQuery) -> None:
    """Rahbar: guruhning o'tgan darslari — davomat/vazifa tekshiruvini tuzatish uchun."""
    gid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _who(s, c.from_user.id)
        if not st or st.current_role != "rahbar":
            await c.answer("Faqat rahbar", show_alert=True)
            return
        ls = (await s.execute(select(Lesson).where(Lesson.group_id == gid, Lesson.confirmed_at.is_not(None))
                              .order_by(Lesson.date.desc()).limit(12))).scalars().all()
    if not ls:
        await c.answer("Davomat olingan dars yo'q.", show_alert=True)
        return
    await c.answer()
    await c.message.answer("🗂 Qaysi dars? (oxirgi 12 ta)", reply_markup=IM(inline_keyboard=[
        [IB(text=f"{x.date:%d.%m.%Y}", callback_data=f"lfx:{x.id}")] for x in ls]))


# =====================================================================
# 🔴 2, 5, 9. Bekor qilish (storno): to'lov, xarajat, xizmat sotuvi
# =====================================================================
KIND = {"p": (Payment, "To'lov"), "x": (Expense, "Xarajat"), "s": (ServiceSale, "Xizmat sotuvi")}
VOID_ROLES = {"p": ("moliya", "rahbar"), "x": ("moliya", "rahbar"), "s": ("moliya", "rahbar", "admin")}


class Vd(StatesGroup):
    reason = State()


async def _describe(s, k: str, obj) -> str:
    if k == "p":
        rows = await _batch(s, obj)
        e = await s.get(Enrollment, obj.enrollment_id, options=[selectinload(Enrollment.student), selectinload(Enrollment.group)])
        groups = []
        for r in rows:
            en = await s.get(Enrollment, r.enrollment_id, options=[selectinload(Enrollment.group)])
            groups.append(en.group.name)
        meth = {"naqd": "naqd", "karta": "karta"}.get(obj.method, obj.method)
        return (f"{e.student.full_name} · {', '.join(groups)}\n{S.fmt_som(sum(r.amount for r in rows))} ({meth}) · "
                f"{obj.paid_date:%d.%m.%Y} · oy {obj.month}")
    if k == "x":
        return f"{obj.category} · {S.fmt_som(obj.amount)} ({obj.method}) · {obj.date:%d.%m.%Y}\n{obj.note or ''}"
    sv = await s.get(Service, obj.service_id)
    return f"{sv.name if sv else '?'} × {obj.qty} · {S.fmt_som(obj.amount)} ({obj.method}) · {obj.created_at:%d.%m.%Y}"


async def _batch(s, p: Payment) -> list[Payment]:
    if p.batch:
        return list((await s.execute(select(Payment).where(Payment.batch == p.batch, Payment.void_of.is_(None),
                                                           Payment.amount > 0))).scalars())
    return [p]


def _voidable(k: str, obj) -> str | None:
    if obj is None:
        return "Topilmadi."
    if obj.void_of is not None or obj.amount <= 0:
        return "Bu yozuvni bekor qilib bo'lmaydi."
    if obj.voided_at:
        return "Allaqachon bekor qilingan."
    if obj.status != "tasdiq":
        return "Faqat tasdiqlangan yozuv bekor qilinadi (kutilayotganini rad eting)."
    if k == "p" and obj.method not in ("naqd", "karta"):
        return "Bu to'lov turi bekor qilinmaydi (ko'chirish yoki ijtimoiy)."
    return None


@router.callback_query(F.data.startswith("vd:"))
async def void_start(c: CallbackQuery, state: FSMContext) -> None:
    _, k, oid = c.data.split(":")
    async with Session() as s:
        st = await _who(s, c.from_user.id)
        obj = await s.get(KIND[k][0], int(oid))
        err = None if st and st.current_role in VOID_ROLES[k] else "Ruxsat yo'q."
        err = err or _voidable(k, obj)
        if not err and obj.void_reason and obj.void_req_by:
            err = "Bu yozuv bo'yicha so'rov allaqachon rahbarda."
        desc = await _describe(s, k, obj) if not err else ""
    if err:
        await c.answer(err, show_alert=True)
        return
    await state.set_state(Vd.reason)
    await state.update_data(k=k, oid=int(oid))
    await c.answer()
    await c.message.answer(f"↩️ <b>{KIND[k][1]}ni bekor qilish</b>\n{desc}\n\n"
                           "Yozuv o'chirilmaydi: yonida teskari yozuv paydo bo'ladi, kassa va hisob avtomatik "
                           "to'g'rilanadi.\nSababini yozing (masalan: «summa xato kiritilgan, to'g'risi 300 000»):")


@router.message(Vd.reason)
async def void_reason(m: Message, state: FSMContext, bot: Bot) -> None:
    reason = " ".join((m.text or "").split())
    if len(reason) < 5:
        await m.answer("Sababni aniqroq yozing (kamida 5 harf).")
        return
    d = await state.get_data()
    await state.clear()
    k, oid = d["k"], d["oid"]
    async with Session() as s:
        st = await _who(s, m.from_user.id)
        obj = await s.get(KIND[k][0], oid)
        err = _voidable(k, obj)
        if err:
            await m.answer(err)
            return
        obj.void_reason, obj.void_req_by = reason[:300], st.id
        desc = await _describe(s, k, obj)
        await S.audit(s, st, "bekor_sorov", kind=KIND[k][1], id=oid, reason=reason)
        await s.commit()
        bosses = await _bosses(s)
    kb = IM(inline_keyboard=[[IB(text="✅ Bekor qilish", callback_data=f"vdy:{k}:{oid}"),
                              IB(text="❌ Rad etish", callback_data=f"vdn:{k}:{oid}")]])
    text = f"↩️ <b>{KIND[k][1]}ni bekor qilish</b>\n{desc}\nSabab: {reason}\nSo'radi: {st.full_name}"
    if st.current_role == "rahbar":
        await m.answer(text + "\n\nTasdiqlang (PIN so'raladi):", reply_markup=kb)
        return
    for b in bosses:
        await send(bot, b, text, reply_markup=kb)
    await m.answer("✅ So'rov rahbarga yuborildi. Qaror sizga xabar qilinadi.")


@router.callback_query(F.data.startswith("vdn:"))
async def void_reject(c: CallbackQuery, bot: Bot) -> None:
    _, k, oid = c.data.split(":")
    async with Session() as s:
        st = await _who(s, c.from_user.id)
        if not st or st.current_role != "rahbar":
            await c.answer("Faqat rahbar", show_alert=True)
            return
        obj = await s.get(KIND[k][0], int(oid))
        req = await s.get(Staff, obj.void_req_by) if obj.void_req_by else None
        obj.void_reason, obj.void_req_by = None, None
        await S.audit(s, st, "bekor_rad", kind=KIND[k][1], id=int(oid))
        await s.commit()
    await c.answer("Rad etildi")
    await c.message.edit_text(c.message.html_text + "\n\n❌ Rad etildi — yozuv o'z holicha qoldi.")
    if req and req.tg_id and req.tg_id != c.from_user.id:
        await send(bot, req.tg_id, f"❌ {KIND[k][1]} #{oid} ni bekor qilish so'rovi rad etildi.")


@router.callback_query(F.data.startswith("vdy:"))
async def void_apply(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    from app.bot.pin import need_pin
    _, k, oid = c.data.split(":")
    oid = int(oid)
    async with Session() as s:
        st = await _who(s, c.from_user.id)
    if not st or st.current_role != "rahbar":
        await c.answer("Faqat rahbar", show_alert=True)
        return
    if await need_pin(c, state):
        return
    family: list[tuple[int, str]] = []
    async with Session() as s:
        st = await _who(s, c.from_user.id)
        obj = await s.get(KIND[k][0], oid)
        err = _voidable(k, obj)
        if err:
            await c.answer(err, show_alert=True)
            return
        reason = obj.void_reason or "rahbar qarori"
        req = await s.get(Staff, obj.void_req_by) if obj.void_req_by else None
        desc = await _describe(s, k, obj)
        t, at = today(), utcnow()
        if k == "p":
            rows = await _batch(s, obj)
            coins = 0
            for r in rows:
                s.add(Payment(enrollment_id=r.enrollment_id, month=r.month, amount=-r.amount, method=r.method,
                              status="tasdiq", paid_date=r.paid_date, entered_by=st.id, confirmed_by=st.id, void_of=r.id,
                              void_reason=reason))
                r.voided_at, r.void_reason = at, reason
                coins += r.coins_given or 0
            e = await s.get(Enrollment, obj.enrollment_id, options=[selectinload(Enrollment.student)])
            if coins:
                await S.add_coins(s, e.student, -coins, "tolov", "To'lov bekor qilindi", st)
            total = sum(r.amount for r in rows)
            for tg in await S.parent_tg_ids(s, e.student_id):
                family.append((tg, f"↩️ <b>To'lov bekor qilindi</b>\n{e.student.full_name}: {S.fmt_som(total)} "
                                   f"({obj.paid_date:%d.%m})\nSabab: {reason}\nHisobingiz yangilandi."
                                   + (f" Coin: −{coins}." if coins else "")))
        elif k == "x":
            s.add(Expense(date=obj.date, category=obj.category, amount=-obj.amount, note=f"Bekor qilindi: #{obj.id}",
                          method=obj.method, source=obj.source, status="tasdiq", by_id=st.id, approved_by=st.id,
                          void_of=obj.id, void_reason=reason))
            obj.voided_at, obj.void_reason = at, reason
        else:
            s.add(ServiceSale(service_id=obj.service_id, qty=-obj.qty, amount=-obj.amount, profit=-(obj.profit or 0),
                              method=obj.method, status="tasdiq", by_id=obj.by_id, void_of=obj.id, void_reason=reason))
            obj.voided_at, obj.void_reason = at, reason
            sv = await s.get(Service, obj.service_id)
            if sv and sv.stock is not None:
                sv.stock += obj.qty
        await S.audit(s, st, "bekor_qilindi", kind=KIND[k][1], id=oid, reason=reason)
        await s.commit()
    await c.answer("Bekor qilindi")
    await c.message.edit_text(f"✅ <b>{KIND[k][1]} bekor qilindi</b>\n{desc}\nSabab: {reason}\n"
                              "Teskari yozuv qo'shildi, kassa va hisob to'g'rilandi.")
    if req and req.tg_id and req.tg_id != c.from_user.id:
        await send(bot, req.tg_id, f"✅ {KIND[k][1]} bekor qilindi:\n{desc}")
    for tg, text in family:
        await send(bot, tg, text)


# --- ro'yxatlar ---
@router.callback_query(F.data.startswith("pl:"))
async def pay_list(c: CallbackQuery) -> None:
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _who(s, c.from_user.id)
        if not st or st.current_role not in ("moliya", "rahbar"):
            await c.answer("Ruxsat yo'q", show_alert=True)
            return
        eids = list((await s.execute(select(Enrollment.id).where(Enrollment.student_id == sid))).scalars())
        pays = (await s.execute(select(Payment).where(Payment.enrollment_id.in_(eids or [0]), Payment.amount > 0,
                                                      Payment.void_of.is_(None))
                                .order_by(Payment.created_at.desc()).limit(40))).scalars().all()
        stu = await s.get(Student, sid)
        gname = {e.id: e.group.name for e in (await s.execute(select(Enrollment).where(Enrollment.id.in_(eids or [0]))
                                                              .options(selectinload(Enrollment.group)))).scalars()}
    seen, items = set(), []
    for p in pays:
        key = p.batch or f"#{p.id}"
        if key in seen:
            continue
        seen.add(key)
        part = [x for x in pays if (x.batch or f"#{x.id}") == key]
        items.append((p, sum(x.amount for x in part), ", ".join(gname.get(x.enrollment_id, "?") for x in part)))
        if len(items) >= 12:
            break
    if not items:
        await c.answer("To'lov yo'q.", show_alert=True)
        return
    meth = {"naqd": "naqd", "karta": "karta", "ijtimoiy": "ijtimoiy", "kochirish": "ko'chirilgan"}
    lines, kb = [f"💵 <b>{stu.full_name}</b> — oxirgi to'lovlar"], []
    for p, total, groups in items:
        mark = " ❌ bekor qilingan" if p.voided_at else (" ⏳ kutilmoqda" if p.status == "kutilmoqda" else
                                                        " ✖️ rad" if p.status == "rad" else "")
        lines.append(f"• {p.paid_date:%d.%m} · {S.fmt_som(total)} · {meth.get(p.method, p.method)} · {groups} · oy {p.month}{mark}")
        if not _voidable("p", p):
            kb.append([IB(text=f"↩️ Bekor qilish: {p.paid_date:%d.%m} · {S.fmt_som(total)}", callback_data=f"vd:p:{p.id}")])
    kb.append([IB(text="⬅️ Kartaga qaytish", callback_data=f"st:{sid}")])
    await c.answer()
    await c.message.answer("\n".join(lines), reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data == "xl")
async def exp_list(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with Session() as s:
        st = await _who(s, c.from_user.id)
        if not st or st.current_role not in ("moliya", "rahbar"):
            await c.answer("Ruxsat yo'q", show_alert=True)
            return
        rows = (await s.execute(select(Expense).where(Expense.amount > 0, Expense.void_of.is_(None))
                                .order_by(Expense.created_at.desc()).limit(15))).scalars().all()
    if not rows:
        await c.answer("Xarajat yo'q.", show_alert=True)
        return
    lines = ["🧾 <b>Oxirgi xarajatlar</b>"]
    kb = []
    for x in rows:
        mark = " ❌ bekor" if x.voided_at else (" ⏳" if x.status == "kutilmoqda" else " ✖️ rad" if x.status == "rad" else "")
        lines.append(f"• {x.date:%d.%m} · {x.category} · {S.fmt_som(x.amount)} · {(x.note or '')[:30]}{mark}")
        if not _voidable("x", x):
            kb.append([IB(text=f"↩️ {x.date:%d.%m} · {x.category} · {S.fmt_som(x.amount)}", callback_data=f"vd:x:{x.id}")])
    await c.answer()
    await c.message.answer("\n".join(lines), reply_markup=IM(inline_keyboard=kb) if kb else None)


@router.callback_query(F.data == "sll")
async def sale_list(c: CallbackQuery) -> None:
    async with Session() as s:
        st = await _who(s, c.from_user.id)
        if not st or st.current_role not in VOID_ROLES["s"]:
            await c.answer("Ruxsat yo'q", show_alert=True)
            return
        q = select(ServiceSale, Service.name).join(Service, Service.id == ServiceSale.service_id).where(
            ServiceSale.amount > 0, ServiceSale.void_of.is_(None))
        if st.current_role == "admin":
            q = q.where(ServiceSale.by_id == st.id)
        rows = (await s.execute(q.order_by(ServiceSale.created_at.desc()).limit(15))).all()
    if not rows:
        await c.answer("Sotuv yo'q.", show_alert=True)
        return
    lines, kb = ["🛒 <b>Oxirgi sotuvlar</b>"], []
    for x, name in rows:
        lines.append(f"• {x.created_at:%d.%m} · {name} × {x.qty} · {S.fmt_som(x.amount)}" + (" ❌ bekor" if x.voided_at else ""))
        if not _voidable("s", x):
            kb.append([IB(text=f"↩️ {x.created_at:%d.%m} · {name[:25]} · {S.fmt_som(x.amount)}", callback_data=f"vd:s:{x.id}")])
    await c.answer()
    await c.message.answer("\n".join(lines), reply_markup=IM(inline_keyboard=kb) if kb else None)


# =====================================================================
# 🟡 7. Nazorat natijasini tuzatish · 🟢 8. O'tkazuvchini almashtirish
# =====================================================================
class Ef(StatesGroup):
    score = State()


async def _exam_fix_error(s, st: Staff | None, ex: Exam) -> str | None:
    if not st:
        return "Ruxsat yo'q."
    if st.current_role == "rahbar":
        return "Bu oy yopilgan — tuzatib bo'lmaydi." if await month_closed(s, ex.date) else None
    g = await s.get(Group, ex.group_id)
    if st.id not in (ex.conductor_id, g.teacher_id):
        return "Bu nazorat sizniki emas."
    done = ex.done_at.date() if ex.done_at else None
    if done != today():
        return "Natijani faqat kiritilgan kuni tuzatasiz. Keyin — rahbar orqali yoki o'quvchi apellyatsiyasi bilan."
    return None


@router.callback_query(F.data.startswith("exf:"))
async def exam_fix_list(c: CallbackQuery) -> None:
    eid = int(c.data.split(":")[1])
    async with Session() as s:
        ex = await s.get(Exam, eid)
        err = await _exam_fix_error(s, await _who(s, c.from_user.id), ex)
        rows = (await s.execute(select(ExamResult, Student.full_name).join(Enrollment, Enrollment.id == ExamResult.enrollment_id)
                                .join(Student, Student.id == Enrollment.student_id).where(ExamResult.exam_id == eid)
                                .order_by(Student.full_name))).all() if not err else []
    if err or not rows:
        await c.answer(err or "Natija yo'q.", show_alert=True)
        return
    await c.answer()
    await c.message.answer("✏️ Kimning natijasini tuzatamiz?", reply_markup=IM(inline_keyboard=[
        [IB(text=f"{n}: " + (f"{r.score:g}/{ex.max_score:g}" if r.score is not None else "qatnashmadi"),
            callback_data=f"exfs:{r.id}")] for r, n in rows]))


@router.callback_query(F.data.startswith("exfs:"))
async def exam_fix_ask(c: CallbackQuery, state: FSMContext) -> None:
    rid = int(c.data.split(":")[1])
    async with Session() as s:
        r = await s.get(ExamResult, rid)
        ex = await s.get(Exam, r.exam_id)
        err = await _exam_fix_error(s, await _who(s, c.from_user.id), ex)
    if err:
        await c.answer(err, show_alert=True)
        return
    await state.set_state(Ef.score)
    await state.update_data(rid=rid)
    await c.answer()
    await c.message.answer(f"Yangi natijani yozing: 0 dan {ex.max_score:g} gacha raqam yoki «-» (qatnashmadi):")


@router.message(Ef.score)
async def exam_fix_save(m: Message, state: FSMContext, bot: Bot) -> None:
    from app.bot.exams import _apply_coins
    d = await state.get_data()
    raw = (m.text or "").strip().replace(",", ".")
    async with Session() as s:
        st = await _who(s, m.from_user.id)
        r = await s.get(ExamResult, d["rid"])
        ex = await s.get(Exam, r.exam_id)
        err = await _exam_fix_error(s, st, ex)
        if err:
            await state.clear()
            await m.answer(err)
            return
        if raw == "-":
            val = None
        else:
            try:
                val = float(raw)
                assert 0 <= val <= (ex.max_score or 0)
            except (ValueError, AssertionError):
                await m.answer(f"0 dan {ex.max_score:g} gacha raqam yoki «-» yozing.")
                return
        await state.clear()
        old = r.score
        r.score = val
        r.percent = round(val / ex.max_score * 100, 1) if val is not None and ex.max_score else None
        enr = await s.get(Enrollment, r.enrollment_id, options=[selectinload(Enrollment.student)])
        g = await s.get(Group, ex.group_id)
        await _apply_coins(s, r, enr.student, await S.get_rules(s), st, ex)
        await S.audit(s, st, "natija_tuzatish", exam=ex.id, student=enr.student.full_name, old=old, new=val)
        await s.commit()
        fam = await S.parent_tg_ids(s, enr.student_id)
        name, pct = enr.student.full_name, r.percent
    res = f"{val:g}/{ex.max_score:g} ({pct:.0f}%)" if val is not None else "qatnashmadi"
    await m.answer(f"✅ {name}: {res}. Coin qayta hisoblandi.")
    for t in fam:
        await send(bot, t, f"✏️ <b>Nazorat natijasi tuzatildi</b>\n{g.name} · {ex.date:%d.%m}\n{name}: <b>{res}</b>")


@router.callback_query(F.data.startswith("exc:"))
async def exam_conductor_list(c: CallbackQuery) -> None:
    eid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _who(s, c.from_user.id)
        ex = await s.get(Exam, eid)
        g = await s.get(Group, ex.group_id)
        if not st or not (st.current_role == "rahbar" or st.id == g.teacher_id):
            await c.answer("Faqat rahbar yoki guruh o'qituvchisi", show_alert=True)
            return
        if ex.status != "reja":
            await c.answer("Nazorat allaqachon o'tkazilgan yoki bekor qilingan.", show_alert=True)
            return
        cands = [x for x in (await s.execute(select(Staff).where(Staff.active).order_by(Staff.full_name))).scalars()
                 if {"oqituvchi", "yordamchi"} & set(x.role_list())]
    kb = [[IB(text=("✅ " if x.id == ex.conductor_id else "") + x.full_name, callback_data=f"excs:{eid}:{x.id}")]
          for x in cands]
    await c.answer()
    await c.message.answer(f"👤 {g.name} · {ex.date:%d.%m} nazoratini kim o'tkazadi?", reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("excs:"))
async def exam_conductor_set(c: CallbackQuery, bot: Bot) -> None:
    _, eid, sid = c.data.split(":")
    async with Session() as s:
        st = await _who(s, c.from_user.id)
        ex = await s.get(Exam, int(eid))
        g = await s.get(Group, ex.group_id)
        if not st or not (st.current_role == "rahbar" or st.id == g.teacher_id) or ex.status != "reja":
            await c.answer("Ruxsat yo'q", show_alert=True)
            return
        old = await s.get(Staff, ex.conductor_id) if ex.conductor_id else None
        new = await s.get(Staff, int(sid))
        ex.conductor_id = new.id
        await S.audit(s, st, "nazorat_otkazuvchi", exam=ex.id, old=old.full_name if old else None, new=new.full_name)
        await s.commit()
    await c.answer("Saqlandi")
    await c.message.edit_text(f"✅ {g.name} · {ex.date:%d.%m} nazoratini endi <b>{new.full_name}</b> o'tkazadi.")
    if new.tg_id:
        await send(bot, new.tg_id, f"📝 Sizga nazorat biriktirildi: {g.name} · {ex.date:%d.%m}. Natijalarni o'sha kuni kiriting.")
    if old and old.tg_id and old.id != new.id:
        await send(bot, old.tg_id, f"ℹ️ {g.name} · {ex.date:%d.%m} nazorati endi {new.full_name} ga o'tkazildi.")


# =====================================================================
# 🟢 10. Vazifalarni tahrirlash (rahbar)
# =====================================================================
class Tk(StatesGroup):
    text = State()
    due = State()
    rtext = State()


async def _boss(s, tg: int) -> Staff | None:
    st = await _who(s, tg)
    return st if st and st.current_role == "rahbar" else None


@router.callback_query(F.data.startswith("tke:"))
async def task_edit_menu(c: CallbackQuery) -> None:
    tid = int(c.data.split(":")[1])
    async with Session() as s:
        if not await _boss(s, c.from_user.id):
            await c.answer("Faqat rahbar", show_alert=True)
            return
        t = await s.get(Task, tid)
        x = await s.get(Staff, t.staff_id)
    if t.status != "ochiq":
        await c.answer("Vazifa yopilgan.", show_alert=True)
        return
    await c.answer()
    await c.message.answer(f"✏️ <b>{x.full_name}</b>: {t.text[:300]}\nMuddat: {t.due:%d.%m.%Y}",
                           reply_markup=IM(inline_keyboard=[
                               [IB(text="📝 Matnini o'zgartirish", callback_data=f"tket:{tid}"),
                                IB(text="📅 Muddat", callback_data=f"tked:{tid}")],
                               [IB(text="❌ Vazifani bekor qilish", callback_data=f"tkx:{tid}")]]))


@router.callback_query(F.data.startswith(("tket:", "tked:")))
async def task_edit_ask(c: CallbackQuery, state: FSMContext) -> None:
    kind, tid = c.data.split(":")
    await state.set_state(Tk.text if kind == "tket" else Tk.due)
    await state.update_data(tid=int(tid))
    await c.answer()
    await c.message.answer("Yangi matnni yozing:" if kind == "tket" else "Yangi muddat: kun.oy (masalan 25.11)")


async def _task_notify(bot: Bot, tid: int, what: str) -> None:
    async with Session() as s:
        t = await s.get(Task, tid)
        x = await s.get(Staff, t.staff_id)
    if x.tg_id:
        await send(bot, x.tg_id, f"✏️ <b>Vazifa {what}</b>\n{t.text[:500]}\nMuddat: {t.due:%d.%m.%Y}"
                   if what != "bekor qilindi" else f"❌ Vazifa bekor qilindi: {t.text[:300]}")


@router.message(Tk.text)
async def task_text_set(m: Message, state: FSMContext, bot: Bot) -> None:
    text = (m.text or "").strip()
    if len(text) < 3:
        await m.answer("Matnni yozing.")
        return
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        st = await _boss(s, m.from_user.id)
        t = await s.get(Task, d["tid"])
        old, t.text = t.text, text
        await S.audit(s, st, "vazifa_tahrir", task=t.id, old=old[:100], new=text[:100])
        await s.commit()
    await m.answer("✅ Vazifa matni o'zgartirildi, xodimga xabar yuborildi.")
    await _task_notify(bot, d["tid"], "o'zgartirildi")


@router.message(Tk.due)
async def task_due_set(m: Message, state: FSMContext, bot: Bot) -> None:
    from app.bot.status import parse_day
    d0 = parse_day(m.text or "", today())
    if not d0 or d0 < today():
        await m.answer("Sanani kun.oy ko'rinishida yozing (bugundan oldin bo'lmasin), masalan 25.11")
        return
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        st = await _boss(s, m.from_user.id)
        t = await s.get(Task, d["tid"])
        old, t.due, t.overdue_notified = t.due, d0, False
        await S.audit(s, st, "vazifa_muddat", task=t.id, old=f"{old}", new=f"{d0}")
        await s.commit()
    await m.answer(f"✅ Yangi muddat: {d0:%d.%m.%Y}. Xodimga xabar yuborildi.")
    await _task_notify(bot, d["tid"], "muddati o'zgardi")


@router.callback_query(F.data.startswith("tkx:"))
async def task_cancel(c: CallbackQuery, bot: Bot) -> None:
    tid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _boss(s, c.from_user.id)
        if not st:
            await c.answer("Faqat rahbar", show_alert=True)
            return
        t = await s.get(Task, tid)
        if t.status != "ochiq":
            await c.answer("Vazifa yopilgan.", show_alert=True)
            return
        t.status = "bekor"
        await S.audit(s, st, "vazifa_bekor", task=tid)
        await s.commit()
    await c.answer("Bekor qilindi")
    await c.message.edit_text(c.message.html_text + "\n\n❌ Bekor qilindi (ball yozilmaydi).")
    await _task_notify(bot, tid, "bekor qilindi")


@router.callback_query(F.data.startswith("rte:"))
async def rec_edit_ask(c: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        if not await _boss(s, c.from_user.id):
            await c.answer("Faqat rahbar", show_alert=True)
            return
    await state.set_state(Tk.rtext)
    await state.update_data(rid=int(c.data.split(":")[1]))
    await c.answer()
    await c.message.answer("Takrorlanadigan vazifaning yangi matnini yozing (keyingi safardan amal qiladi):")


@router.message(Tk.rtext)
async def rec_text_set(m: Message, state: FSMContext) -> None:
    text = (m.text or "").strip()
    if len(text) < 3:
        await m.answer("Matnni yozing.")
        return
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        st = await _boss(s, m.from_user.id)
        r = await s.get(RecurringTask, d["rid"])
        r.text = text
        await S.audit(s, st, "takroriy_vazifa_tahrir", id=r.id, new=text[:100])
        await s.commit()
    await m.answer("✅ Saqlandi — keyingi vazifalar yangi matn bilan beriladi.")


# =====================================================================
# 🟢 11. Arizalarni tahrirlash (admin, rahbar)
# =====================================================================
LEAD_STATUS = {"yangi": "🆕 Yangi", "boglanildi": "📞 Bog'lanildi", "oquvchi": "✅ O'quvchi bo'ldi", "rad": "❌ Rad etildi"}


class Ld(StatesGroup):
    name = State()
    phone = State()
    subject = State()


async def _lead_editor(s, tg: int) -> Staff | None:
    st = await _who(s, tg)
    return st if st and st.current_role in ("admin", "rahbar") else None


@router.callback_query(F.data.startswith("lde:"))
async def lead_edit_menu(c: CallbackQuery) -> None:
    lid = int(c.data.split(":")[1])
    async with Session() as s:
        if not await _lead_editor(s, c.from_user.id):
            await c.answer("Ruxsat yo'q", show_alert=True)
            return
        x = await s.get(Lead, lid)
    await c.answer()
    await c.message.answer(f"✏️ Ariza: <b>{x.child_name}</b>\nTel: {x.phone or '—'} · Fan: {x.subject or '—'}\n"
                           f"Holat: {LEAD_STATUS.get(x.status, x.status)}", reply_markup=IM(inline_keyboard=[
                               [IB(text="👶 Ism", callback_data=f"ldn:{lid}"), IB(text="📞 Telefon", callback_data=f"ldp:{lid}"),
                                IB(text="📚 Fan", callback_data=f"lds:{lid}")],
                               [IB(text=v, callback_data=f"ldh:{lid}:{k}") for k, v in list(LEAD_STATUS.items())[:2]],
                               [IB(text=v, callback_data=f"ldh:{lid}:{k}") for k, v in list(LEAD_STATUS.items())[2:]]]))


@router.callback_query(F.data.startswith(("ldn:", "ldp:", "lds:")))
async def lead_field_ask(c: CallbackQuery, state: FSMContext) -> None:
    kind, lid = c.data.split(":")
    await state.set_state({"ldn": Ld.name, "ldp": Ld.phone, "lds": Ld.subject}[kind])
    await state.update_data(lid=int(lid))
    await c.answer()
    await c.message.answer({"ldn": "Bola ismini yozing:", "ldp": "Telefon: +998901234567", "lds": "Fan nomini yozing:"}[kind])


async def _lead_set(m: Message, state: FSMContext, field: str, value: str) -> None:
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        st = await _lead_editor(s, m.from_user.id)
        x = await s.get(Lead, d["lid"])
        old = getattr(x, field)
        setattr(x, field, value)
        await S.audit(s, st, "ariza_tahrir", lead=x.id, field=field, old=old, new=value)
        await s.commit()
    await m.answer("✅ Saqlandi.")


@router.message(Ld.name)
async def lead_name_set(m: Message, state: FSMContext) -> None:
    v = " ".join((m.text or "").split())
    if len(v) < 2 or len(v) > 120:
        await m.answer("Ismni yozing.")
        return
    await _lead_set(m, state, "child_name", v)


@router.message(Ld.phone)
async def lead_phone_set(m: Message, state: FSMContext) -> None:
    v = S.norm_phone(m.text or "")
    if not v:
        await m.answer("Raqam noto'g'ri. Masalan: +998901234567")
        return
    await _lead_set(m, state, "phone", v)


@router.message(Ld.subject)
async def lead_subject_set(m: Message, state: FSMContext) -> None:
    v = " ".join((m.text or "").split())[:60]
    if len(v) < 2:
        await m.answer("Fan nomini yozing.")
        return
    await _lead_set(m, state, "subject", v)


@router.callback_query(F.data.startswith("ldh:"))
async def lead_status_set(c: CallbackQuery) -> None:
    _, lid, stt = c.data.split(":")
    async with Session() as s:
        st = await _lead_editor(s, c.from_user.id)
        if not st:
            await c.answer("Ruxsat yo'q", show_alert=True)
            return
        x = await s.get(Lead, int(lid))
        old, x.status, x.handled_by = x.status, stt, st.id
        await S.audit(s, st, "ariza_holat", lead=x.id, old=old, new=stt)
        await s.commit()
    await c.answer("Saqlandi")
    await c.message.edit_text(c.message.html_text.split("\nHolat:")[0] + f"\nHolat: {LEAD_STATUS[stt]} ✅")


@router.callback_query(F.data == "ldc")
async def leads_closed(c: CallbackQuery) -> None:
    async with Session() as s:
        if not await _lead_editor(s, c.from_user.id):
            await c.answer("Ruxsat yo'q", show_alert=True)
            return
        rows = (await s.execute(select(Lead).where(Lead.status.in_(("oquvchi", "rad")))
                                .order_by(Lead.created_at.desc()).limit(15))).scalars().all()
    if not rows:
        await c.answer("Yopilgan ariza yo'q.", show_alert=True)
        return
    await c.answer()
    await c.message.answer("📁 Yopilgan arizalar (oxirgi 15). Tahrirlash uchun tanlang:", reply_markup=IM(inline_keyboard=[
        [IB(text=f"{LEAD_STATUS[x.status][:2]} {x.child_name} · {x.created_at:%d.%m}", callback_data=f"lde:{x.id}")]
        for x in rows]))
