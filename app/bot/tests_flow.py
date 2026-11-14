"""📚 Testlar: so'rov -> yuklash -> ko'rib chiqish/tuzatish -> tasdiq -> har o'quvchiga varaq -> skaner/kiritish -> natija."""
from __future__ import annotations

import io
import zlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton as IB,
                           InlineKeyboardMarkup as IM, InputMediaPhoto, Message)
from sqlalchemy import delete, select

from app.bot import keyboards as K
from app.bot.notify import send
from app.config import settings
from app.db.models import (Attendance, Enrollment, Exam, ExamResult, Group, Lesson, Staff, Student, Test,
                           TestPaper, TestQuestion)
from app.db.session import Session
from app.logic import papers as P
from app.logic import sheets as SH
from app.logic import testfile as TF
from app import services as S

import logging
log = logging.getLogger(__name__)
router = Router()
scan_router = Router()          # jonli skaner natijasi — boshqa holatlardan oldin ushlanadi
TZ = ZoneInfo(settings.timezone)
ST = {"sorov": "📨 so'rov yuborilgan", "korib": "👀 o'qituvchi ko'rib chiqmoqda", "qaytarildi": "↩️ tuzatishga qaytarilgan",
      "tasdiq": "✅ tasdiqlangan"}
WD = ["Du", "Se", "Chor", "Pay", "Ju", "Sha", "Yak"]


def now() -> datetime:
    return datetime.now(TZ)


def _exam_label(ex: Exam) -> str:
    return f"{WD[ex.date.weekday()]} {ex.date:%d.%m}" + (" · oylik" if ex.monthly else "")


async def _staff(s, tg: int) -> Staff | None:
    who = await S.who_by_tg(s, tg)
    return who.staff


def _is_boss(st: Staff | None) -> bool:
    return bool(st and st.current_role == "rahbar")


# ================= 📚 Testlar menyusi =================
@router.message(F.text == K.B_TESTS)
async def tests_menu(m: Message, state: FSMContext) -> None:
    await state.clear()
    async with Session() as s:
        st = await _staff(s, m.from_user.id)
    if not st or st.current_role not in ("rahbar", "oqituvchi", "yordamchi"):
        return
    rows = []
    if st.current_role != "rahbar":
        rows.append([IB(text="📨 Rahbardan test so'rash", callback_data="tsq:new")])
    else:
        async with Session() as s:
            n_req = len(await pending_requests(s))
        rows.append([IB(text=f"📥 Kelgan so'rovlar ({n_req})", callback_data="tsr:list")])
    rows.append([IB(text="📎 Tayyor testni yuklash (.txt)", callback_data="tsu:new")])
    rows.append([IB(text="📋 Testlar holati", callback_data="tsl:list")])
    await m.answer("📚 <b>Testlar</b>\n\n"
                   "• So'rov — mavzu va kitob rasmlarini rahbarga yuborasiz, u test tuzib beradi.\n"
                   "• Tayyor test — andoza bo'yicha yozilgan .txt faylni yuklaysiz.\n"
                   "Bot har bir o'quvchi uchun alohida varaq tayyorlab, adminga chop etishga yuboradi.\n"
                   "• Test o'tgach — «📋 Testlar holati» → «📷 Tekshirish»: javob varaqlarini rasmga olasiz, "
                   "bot o'zi tekshiradi.",
                   reply_markup=IM(inline_keyboard=rows))


async def _groups_for(s, st: Staff) -> list[Group]:
    return await S.teacher_groups(s, st, st.current_role)


async def exam_tests(s, exam_id: int) -> list[Test]:
    return list((await s.execute(select(Test).where(Test.exam_id == exam_id).order_by(Test.id))).scalars())


async def _taken(s, exam_id: int) -> tuple[bool, set[int]]:
    """(butun guruhga test bormi, tanlangan testlarga tushgan enrollment id lar)."""
    whole, ids = False, set()
    for t in await exam_tests(s, exam_id):
        if t.students is None:
            whole = True
        else:
            ids |= set(t.students)
    return whole, ids


async def _exam_roster(s, ex: Exam | None, gid: int) -> list[Enrollment]:
    return await S.group_roster(s, gid, ex.date if ex else now().date())


async def _open_exams(s, gid: int) -> list[tuple[Exam, int, int]]:
    """Test berilishi mumkin bo'lgan nazoratlar: (nazorat, testga tushganlar, guruhdagilar)."""
    t = now().date()
    rows = (await s.execute(select(Exam).where(Exam.group_id == gid, Exam.status == "reja", Exam.date >= t)
                            .order_by(Exam.date))).scalars().all()
    out = []
    for e in rows:
        whole, ids = await _taken(s, e.id)
        if whole:
            continue
        roster = {x.id for x in await _exam_roster(s, e, gid)}
        if roster and roster <= ids:
            continue
        out.append((e, len(ids & roster), len(roster)))
    return out


# ================= 1. So'rov (o'qituvchi) =================
class Rq(StatesGroup):
    when = State()
    topic = State()
    count = State()
    photos = State()


@router.callback_query(F.data.in_({"tsq:new", "tsu:new"}))
async def pick_group(c: CallbackQuery, state: FSMContext) -> None:
    mode = c.data.split(":")[0]
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        gs = await _groups_for(s, st) if st else []
    if not gs:
        await c.answer("Sizda guruh yo'q.", show_alert=True)
        return
    await state.clear()
    await state.update_data(mode=mode)
    await c.message.edit_text("Qaysi guruh uchun?",
                              reply_markup=IM(inline_keyboard=[[IB(text=g.name, callback_data=f"tsg:{g.id}")] for g in gs]))


@router.callback_query(F.data.startswith("tsg:"))
async def pick_exam(c: CallbackQuery, state: FSMContext) -> None:
    gid = int(c.data.split(":")[1])
    async with Session() as s:
        exams = await _open_exams(s, gid)
    await state.update_data(gid=gid)
    await state.set_state(Rq.when)              # sanani yozib ham yuborish mumkin
    rows = [[IB(text=f"📝 {_exam_label(e)}" + (f" · testi bor: {k}/{n}" if k else ""), callback_data=f"tse:{e.id}")]
            for e, k, n in exams]
    rows.append([IB(text="Sana hali aniq emas", callback_data="tse:0")])
    await c.message.edit_text("📅 <b>Test qachonga mo'ljallangan?</b>\n"
                              + ("Rejadagi nazoratni tanlang yoki " if exams else "")
                              + "sanani yozing: <i>14.11</i> yoki <i>14.11.2026</i>",
                              reply_markup=IM(inline_keyboard=rows))


def _parse_day(text: str) -> date | None:
    import re
    m_ = re.fullmatch(r"\s*(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\s*", text or "")
    if not m_:
        return None
    d_, mo, y = int(m_.group(1)), int(m_.group(2)), m_.group(3)
    t = now().date()
    given = bool(y)
    yr = (int(y) + 2000 if len(y) == 2 else int(y)) if y else t.year
    try:
        day = date(yr, mo, d_)
        if not given and day < t:          # yil yozilmagan va sana o'tib ketgan — keyingi yil
            day = date(yr + 1, mo, d_)
    except ValueError:
        return None
    return day


@router.message(Rq.when)
async def when_typed(m: Message, state: FSMContext) -> None:
    day = _parse_day(m.text or "")
    t = now().date()
    if not day:
        await m.answer("Sanani shunday yozing: 14.11 yoki 14.11.2026")
        return
    if day < t or day > t + timedelta(days=90):
        await m.answer(f"Sana bugundan ({t:%d.%m}) keyin va 3 oy ichida bo'lsin.")
        return
    d = await state.get_data()
    async with Session() as s:
        ex = (await s.execute(select(Exam).where(Exam.group_id == d["gid"], Exam.date == day,
                                                 Exam.status == "reja"))).scalars().first()
    await state.update_data(planned=day.isoformat())
    text, kb = await _scope_prompt(state, ex.id if ex else 0)
    note = (f"📅 {day:%d.%m.%Y} — rejadagi nazoratga biriktirildi.\n\n" if ex else
            f"📅 Test sanasi: <b>{day:%d.%m.%Y}</b>\n\n")
    await m.answer(note + text, reply_markup=kb)


@router.callback_query(F.data.startswith("tse:"))
async def got_exam(c: CallbackQuery, state: FSMContext) -> None:
    eid = int(c.data.split(":")[1])
    if not (await state.get_data()).get("gid"):
        await c.answer("Sessiya eskirgan — 📚 Testlar dan qaytadan boshlang.", show_alert=True)
        return
    text, kb = await _scope_prompt(state, eid)
    await c.answer()
    await c.message.edit_text(text, reply_markup=kb)


async def _scope_prompt(state: FSMContext, eid: int) -> tuple[str, IM]:
    await state.set_state(None)
    await state.update_data(eid=eid or None)
    d = await state.get_data()
    async with Session() as s:
        ex = await s.get(Exam, eid) if eid else None
        roster = await _exam_roster(s, ex, d["gid"])
        whole, taken = await _taken(s, eid) if eid else (False, set())
    order = [e.id for e in roster]
    names = {e.id: e.student.full_name for e in roster}
    free = [i for i in order if i not in taken]
    await state.update_data(order=order, names=names, taken=sorted(taken), sel=[])
    rows = []
    if not taken:
        rows.append([IB(text=f"👥 Butun guruh ({len(order)} ta)", callback_data="tws:all")])
    else:
        rows.append([IB(text=f"👥 Qolgan {len(free)} ta o'quvchi", callback_data="tws:rest")])
    rows.append([IB(text="✅ O'quvchilarni tanlash", callback_data="tws:pick")])
    head = "👥 <b>Test kimlar uchun?</b>"
    if taken:
        head = (f"Bu nazoratda {len(taken & set(order))} ta o'quvchiga boshqa mavzudan test bor.\n"
                f"Qolgan {len(free)} ta o'quvchi uchun test kimlarga?")
    return (head + "\n\n<i>Bitta guruhni bir necha kichik guruhga bo'lib, har biriga alohida "
            "mavzudan test so'rash mumkin.</i>", IM(inline_keyboard=rows))


def _pick_kb(d: dict) -> IM:
    taken, sel = set(d["taken"]), set(d["sel"])
    rows = []
    for i, eid in enumerate(d["order"]):
        nm = d["names"][str(eid)] if str(eid) in d["names"] else d["names"].get(eid, "?")
        if eid in taken:
            rows.append([IB(text=f"🔒 {nm} (boshqa testda)", callback_data="twt:x")])
        else:
            rows.append([IB(text=f"{'✅' if eid in sel else '▫️'} {nm}", callback_data=f"twt:{i}")])
    rows.append([IB(text="☑️ Hammasini belgilash", callback_data="tws:allpick"),
                 IB(text="⬜ Tozalash", callback_data="tws:none")])
    rows.append([IB(text=f"➡️ Davom etish ({len(sel)} ta)", callback_data="tws:ok")])
    return IM(inline_keyboard=rows)


@router.callback_query(F.data.startswith("tws:"))
async def scope(c: CallbackQuery, state: FSMContext) -> None:
    act = c.data.split(":")[1]
    d = await state.get_data()
    if "order" not in d:
        await c.answer("Sessiya eskirgan — 📚 Testlar dan qaytadan boshlang.", show_alert=True)
        return
    taken = set(d["taken"])
    free = [i for i in d["order"] if i not in taken]
    if act == "all":
        await state.update_data(students=None)
        return await _after_scope(c, state)
    if act == "rest":
        await state.update_data(students=free)
        return await _after_scope(c, state)
    if act in ("pick", "allpick", "none"):
        await state.update_data(sel=free if act == "allpick" else [])
        d = await state.get_data()
        await c.answer()
        await c.message.edit_text("O'quvchilarni belgilang (✅ — shu testga kiradi):", reply_markup=_pick_kb(d))
        return
    if act == "ok":
        if not d["sel"]:
            await c.answer("Kamida bitta o'quvchini belgilang.", show_alert=True)
            return
        sel = [i for i in d["order"] if i in set(d["sel"])]
        await state.update_data(students=None if (not taken and len(sel) == len(d["order"])) else sel)
        return await _after_scope(c, state)


@router.callback_query(F.data.startswith("twt:"))
async def scope_toggle(c: CallbackQuery, state: FSMContext) -> None:
    x = c.data.split(":")[1]
    if x == "x":
        await c.answer("Bu o'quvchi shu nazoratda boshqa testga kiritilgan.")
        return
    d = await state.get_data()
    if "order" not in d:
        await c.answer("Sessiya eskirgan.", show_alert=True)
        return
    eid = d["order"][int(x)]
    sel = set(d["sel"])
    sel ^= {eid}
    await state.update_data(sel=sorted(sel))
    d = await state.get_data()
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=_pick_kb(d))


def _scope_text(d: dict) -> str:
    st = d.get("students")
    if st is None:
        return "butun guruh"
    names = [d["names"].get(str(i)) or d["names"].get(i, "?") for i in st]
    return f"{len(st)} ta o'quvchi: " + ", ".join(n.split()[0] + (" " + n.split()[1][0] + "." if len(n.split()) > 1 else "")
                                                for n in names[:30])


async def _after_scope(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    await c.answer()
    if d.get("mode") == "tsu":
        await state.set_state(Up.file)
        await c.message.edit_text(f"Test: {_scope_text(d)}.\n\n📎 Test faylini (.txt) yuboring. "
                                  "Format — «Test tuzish prompti» dagi andoza.\nBekor qilish: /start")
        return
    await state.set_state(Rq.topic)
    await c.message.edit_text(f"Test: {_scope_text(d)}.\n\nMavzu(lar)ni yozing.\n"
                              "<i>Masalan: Aniq va noaniq artikllar (a, an, the)</i>")


@router.message(Rq.topic)
async def rq_topic(m: Message, state: FSMContext) -> None:
    if len((m.text or "").strip()) < 3:
        await m.answer("Mavzuni yozing.")
        return
    await state.update_data(topic=m.text.strip()[:200])
    await state.set_state(Rq.count)
    await m.answer("Har bir o'quvchiga nechta savol?", reply_markup=IM(inline_keyboard=[[
        IB(text=str(n), callback_data=f"tsn:{n}") for n in (10, 15, 20, 25, 30)]]))


@router.callback_query(Rq.count, F.data.startswith("tsn:"))
async def rq_count(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(n=int(c.data.split(":")[1]), photos=[])
    await state.set_state(Rq.photos)
    await c.message.edit_text("📷 O'tilgan mavzuning kitob sahifalarini yuboring:\n"
                              f"• <b>{MAX_PHOTOS} varaqgacha</b> — rasm yoki PDF\n"
                              f"• <b>{MAX_PHOTOS} varaqdan ko'p</b> — faqat PDF (bitta fayl qilib)\n"
                              "Tugatgach «✅ So'rovni yuborish» ni bosing.",
                              reply_markup=IM(inline_keyboard=[[IB(text="✅ So'rovni yuborish", callback_data="tsq:send")]]))


MAX_PHOTOS = 3
SEND_KB = IM(inline_keyboard=[[IB(text="✅ So'rovni yuborish", callback_data="tsq:send")]])


@router.message(Rq.photos, F.photo)
async def rq_photo(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    files = d.get("photos", [])
    n_img = sum(1 for f in files if not f.startswith("doc:"))
    if n_img >= MAX_PHOTOS:
        if not d.get("warned"):
            await state.update_data(warned=True)
            await m.answer(f"⚠️ Rasm {MAX_PHOTOS} tadan oshdi. {MAX_PHOTOS} varaqdan ko'p bo'lsa — barcha sahifalarni "
                           "<b>bitta PDF</b> qilib yuboring (ortiqcha rasmlar qabul qilinmadi).", reply_markup=SEND_KB)
        return
    files = files + [m.photo[-1].file_id]
    await state.update_data(photos=files)
    if n_img + 1 in (1, MAX_PHOTOS):
        await m.answer(f"Qabul qilindi: {n_img + 1} ta rasm" + (" (eng ko'pi)." if n_img + 1 == MAX_PHOTOS else ".")
                       + " Tugatgach «✅ So'rovni yuborish».", reply_markup=SEND_KB)


@router.message(Rq.photos, F.document)
async def rq_pdf(m: Message, state: FSMContext) -> None:
    doc = m.document
    if not ((doc.mime_type or "") == "application/pdf" or (doc.file_name or "").lower().endswith(".pdf")):
        await m.answer("Faqat rasm yoki PDF fayl qabul qilinadi.")
        return
    d = await state.get_data()
    files = d.get("photos", []) + ["doc:" + doc.file_id]
    await state.update_data(photos=files)
    n_pdf = sum(1 for f in files if f.startswith("doc:"))
    await m.answer(f"📄 PDF qabul qilindi ({n_pdf} ta). Tugatgach «✅ So'rovni yuborish».", reply_markup=SEND_KB)


@router.callback_query(Rq.photos, F.data == "tsq:send")
async def rq_send(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        g = await s.get(Group, d["gid"])
        ex = await s.get(Exam, d["eid"]) if d.get("eid") else None
        t = Test(group_id=g.id, exam_id=ex.id if ex else None, topic=d["topic"], n_questions=d["n"],
                 photos=d.get("photos", []), status="sorov", source="rahbar", requested_by=st.id,
                 students=d.get("students"), meta={"sana": d["planned"]} if d.get("planned") else {})
        s.add(t)
        await s.flush()
        if ex and not ex.test_id:
            ex.test_id = t.id
        await S.audit(s, st, "test_sorov", group=g.name, topic=d["topic"], students=len(d.get("students") or []))
        await s.commit()
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        tid, gname, who = t.id, g.name, st.full_name
    when = (_exam_label(ex) if ex else
            f"{date.fromisoformat(d['planned']):%d.%m.%Y}" if d.get("planned") else "sana hali aniq emas")
    text = (f"📨 <b>Test so'rovi #{tid}</b>\n{who} · {gname}\nNazorat: {when}\nKimlar uchun: {_scope_text(d)}\n"
            f"Mavzu: {d['topic']}\n"
            + _bank_line(len(d["students"]) if d.get("students") is not None else len(d.get("order", [])), d["n"]) +
            f"Savollar: {d['n']} ta · rasm: {sum(1 for f in d.get('photos', []) if not f.startswith('doc:'))} ta"
            f" · PDF: {sum(1 for f in d.get('photos', []) if f.startswith('doc:'))} ta\n\n"
            "Materiallarni «Test tuzuvchi» loyihasiga yuboring (yuqoridagi bank qatori bilan), tayyor .txt faylni shu yerga yuklang.")
    for b in bosses:
        photos = [f for f in d.get("photos", []) if not f.startswith("doc:")]
        for i in range(0, len(photos), 10):
            try:
                await bot.send_media_group(b, [InputMediaPhoto(media=f) for f in photos[i:i + 10]])
            except Exception:
                pass
        for f in d.get("photos", []):
            if f.startswith("doc:"):
                try:
                    await bot.send_document(b, f[4:])
                except Exception:
                    pass
        await send(bot, b, text, reply_markup=IM(inline_keyboard=[[IB(text="📎 Test faylini yuklash",
                                                                       callback_data=f"tup:{tid}")]]))
    rest = ""
    if d.get("students") is not None and d.get("eid"):
        left = len(d["order"]) - len(set(d["taken"]) | set(d["students"]))
        if left > 0:
            rest = (f"\n\nℹ️ Bu nazoratda yana {left} ta o'quvchiga test berilmagan. Ular uchun ham "
                    "📚 Testlar → 📨 Rahbardan test so'rash orqali boshqa mavzudan so'rang.")
    await c.message.edit_text(f"✅ So'rov #{tid} rahbarga yuborildi. Test tayyor bo'lgach sizga ko'rib chiqishga keladi."
                              + rest)


# ================= 2. Yuklash =================
class Up(StatesGroup):
    file = State()


@router.callback_query(F.data.startswith("tup:"))
async def up_for_request(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(tid=int(c.data.split(":")[1]))
    await state.set_state(Up.file)
    await c.answer()
    await c.message.answer("📎 Test faylini (.txt) yuboring.\nBekor qilish: /start")


@router.message(Up.file, F.document)
async def up_file(m: Message, state: FSMContext, bot: Bot) -> None:
    doc = m.document
    if not (doc.file_name or "").lower().endswith(".txt"):
        await m.answer("Faqat .txt fayl qabul qilinadi.")
        return
    raw = (await bot.download(doc.file_id, destination=io.BytesIO())).getvalue()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    tf = TF.parse(text)
    if tf.errors:
        await m.answer("❌ Faylda xatolar bor, tuzatib qayta yuboring:\n\n• " + "\n• ".join(tf.errors[:15])
                       + (f"\n… yana {len(tf.errors) - 15} ta" if len(tf.errors) > 15 else ""))
        return
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        st = await _staff(s, m.from_user.id)
        if d.get("tid"):
            t = await s.get(Test, d["tid"])
        else:
            ex = await s.get(Exam, d["eid"]) if d.get("eid") else None
            t = Test(group_id=d["gid"], exam_id=ex.id if ex else None, topic=tf.meta.get("mavzu", "")[:200] or "Test",
                     source="rahbar" if _is_boss(st) else "oqituvchi", requested_by=st.id, students=d.get("students"))
            s.add(t)
            await s.flush()
            if ex and not ex.test_id:
                ex.test_id = t.id
        await s.execute(delete(TestQuestion).where(TestQuestion.test_id == t.id))
        for q in tf.questions:
            s.add(TestQuestion(test_id=t.id, qid=q.qid, variant=q.variant, level=q.level, source=q.source[:120],
                               figure=q.figure or None,
                               text=q.text, options=q.options, answer=q.answer))
        t.n_questions, t.n_variants, t.common = tf.n, tf.variants, tf.common
        planned = d.get("planned") or (t.meta or {}).get("sana")      # so'rovdagi sana yo'qolmasin
        t.meta, t.version, t.uploaded_by = tf.meta, (t.version or 0) + 1, st.id
        if planned:
            t.meta = {**(tf.meta or {}), "sana": planned}
        own = not _is_boss(st)
        t.status = "tasdiq" if own else "korib"
        if own:
            t.approved_by = st.id
        await S.audit(s, st, "test_yuklash", test=t.id, version=t.version)
        await s.commit()
        tid = t.id
    n_fig = sum(1 for q in tf.questions if q.figure)
    fig_note = f"\n🖼 Chizmali savollar: {n_fig} ta (varaqlarda rasm bo'lib chiqadi)." if n_fig else ""
    if tf.is_bank:
        lv = TF.level_counts(tf.questions)
        async with Session() as s:
            n_st = len(await _paper_roster(s, tid))
        need = P.bank_need(max(1, n_st), tf.n, tf.overlap)
        summary = (f"✅ Fayl qabul qilindi (#{tid}) — <b>bank rejimi</b>: {len(tf.questions)} ta savol "
                   f"(oson {lv['oson']}, o'rta {lv['orta']}, qiyin {lv['qiyin']}).\n"
                   f"Har o'quvchiga {tf.n} ta shaxsiy savol; istalgan ikki o'quvchida ko'pi bilan {tf.overlap} ta bir xil.\n"
                   f"{n_st} ta o'quvchi uchun tavsiya: {need['total']} ta"
                   + (" ✅" if len(tf.questions) >= need["total"] else
                      f" — ⚠️ bank kichikroq (matematik chegara {need['min']}); bot baribir urinib ko'radi, "
                      "yetmasa takror chegarasini biroz oshiradi va xabar beradi."))
    else:
        lv = TF.level_counts(tf.variant_questions("A"))
        summary = (f"✅ Fayl qabul qilindi (#{tid}, {tf.n} savol, {tf.variants} variant).\n"
                   f"A-variant qiyinligi: oson {lv['oson']}, o'rta {lv['orta']}, qiyin {lv['qiyin']}.")
    summary += fig_note
    if tf.warnings:
        summary += "\n⚠️ " + "\n⚠️ ".join(tf.warnings[:5])
    if own:
        await m.answer(summary + "\n\nO'zingiz yuklaganingiz uchun tasdiqlandi. Varaqlar tayyorlanmoqda...")
        await make_papers(bot, tid, m.chat.id)
    else:
        async with Session() as s:
            t = await s.get(Test, tid)
            names = []
            g = await s.get(Group, t.group_id)
            for i in dict.fromkeys(x for x in (t.requested_by, g.teacher_id, g.assistant_id) if x):
                st2 = await s.get(Staff, i)
                if st2 and st2.tg_id:
                    names.append(st2.full_name)
        n = await send_review(bot, tid)
        await m.answer(summary + f"\n\n👀 Ko'rib chiqishga yuborildi ({n} ta): " + (", ".join(names) or "rahbar")
                       + ".\nO'qituvchi tasdiqlagach varaqlar adminga chop etishga ketadi.")


# ================= 3. Ko'rib chiqish =================
async def _reviewers(s, t: Test) -> list[int]:
    """Ko'rib chiquvchilar: so'ragan xodim (har doim), guruh o'qituvchisi va yordamchisi.
    Faylni yuklagan rahbar o'zi so'ramagan va guruh o'qituvchisi bo'lmasa — unga yuborilmaydi."""
    g = await s.get(Group, t.group_id)
    ids = [x for x in (t.requested_by, g.teacher_id, g.assistant_id) if x]
    out: list[int] = []
    for i in dict.fromkeys(ids):
        st = await s.get(Staff, i)
        if not (st and st.tg_id):
            continue
        if i == t.uploaded_by and i not in (t.requested_by, g.teacher_id):
            continue
        if st.tg_id not in out:
            out.append(st.tg_id)
    if not out:                                   # hech kim topilmasa — rahbar
        out = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    return out


def _review_kb(tid: int) -> IM:
    return IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=f"tok:{tid}")],
                               [IB(text="✏️ Savolni tuzatish", callback_data=f"tfx:{tid}")],
                               [IB(text="↩️ Izoh bilan qaytarish", callback_data=f"trt:{tid}")]])


async def _review_file(s, t: Test) -> bytes:
    g = await s.get(Group, t.group_id)
    qs = (await s.execute(select(TestQuestion).where(TestQuestion.test_id == t.id)
                          .order_by(TestQuestion.variant, TestQuestion.qid))).scalars().all()
    rows = [(q.qid, q.variant, q.level, q.source or "", q.text, q.options, q.answer, q.figure) for q in qs]
    return SH.review_pdf(f"#{t.id} · {g.name} · {t.topic} · {t.version}-nusxa", rows)


async def send_review(bot: Bot, tid: int, only: int | None = None) -> int:
    """Ko'rib chiqish uchun PDF + tugmalar. only — faqat shu chatga (qayta so'ralganda). Qaytaradi: nechta yuborildi."""
    async with Session() as s:
        t = await s.get(Test, tid)
        g = await s.get(Group, t.group_id)
        pdf = await _review_file(s, t)
        to = [only] if only else await _reviewers(s, t)
    cap = (f"👀 <b>Test #{tid} ko'rib chiqish uchun</b> ({t.version}-nusxa)\n{g.name} · {t.topic}\n"
           f"{t.n_questions} savol · " + ("bank rejimi (har o'quvchiga shaxsiy)" if t.n_variants == 0
                                             else f"{t.n_variants} variant") + "\n\n"
           "Faylda har savolning to'g'ri javobi belgilangan. Tekshiring:\n"
           "• hammasi to'g'ri — «✅ Tasdiqlash»;\n• kichik xato — «✏️ Savolni tuzatish» (raqami bilan);\n"
           "• ko'p xato — «↩️ Izoh bilan qaytarish».")
    sent = 0
    for tg in to:
        try:
            await bot.send_document(tg, BufferedInputFile(pdf, f"test_{tid}_v{t.version}.pdf"), caption=cap,
                                    reply_markup=_review_kb(tid))
            sent += 1
        except Exception:
            log.exception("review pdf %s", tid)
            sent += bool(await send(bot, tg, cap, reply_markup=_review_kb(tid)))
    return sent


@router.callback_query(F.data.startswith("trv:"))
async def review_again(c: CallbackQuery, bot: Bot) -> None:
    """Testlar holatidan: ko'rib chiqish faylini qayta olish."""
    await c.answer()
    await send_review(bot, int(c.data.split(":")[1]), only=c.message.chat.id)


class Fx(StatesGroup):
    qno = State()
    rewrite = State()
    note = State()


@router.callback_query(F.data.startswith("tfx:"))
async def fix_ask(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Fx.qno)
    await state.update_data(tid=int(c.data.split(":")[1]))
    await c.answer()
    await c.message.answer("Tuzatiladigan savolning raqamini yozing (fayldagi # raqam):")


@router.message(Fx.qno)
async def fix_show(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    if not (m.text or "").strip().lstrip("#").isdigit():
        await m.answer("Raqam yozing, masalan: 12")
        return
    qid = int(m.text.strip().lstrip("#"))
    async with Session() as s:
        q = (await s.execute(select(TestQuestion).where(TestQuestion.test_id == d["tid"],
                                                        TestQuestion.qid == qid))).scalar_one_or_none()
    if not q:
        await m.answer("Bunday raqamli savol yo'q. Qaytadan yozing:")
        return
    await state.update_data(tq=q.id)
    opts = "\n".join(f"{x}) {o}" for x, o in zip("ABCD", q.options))
    kb = [[IB(text=f"To'g'ri javob → {x}", callback_data=f"tfa:{q.id}:{x}") for x in "ABCD" if x != q.answer],
          [IB(text="✍️ Savolni qayta yozish", callback_data=f"tfw:{q.id}")]]
    await m.answer(f"#{q.qid} ({q.variant}, {q.level})\n{q.text}\n{opts}\nHozirgi javob: <b>{q.answer}</b>",
                   reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("tfa:"))
async def fix_answer(c: CallbackQuery, state: FSMContext) -> None:
    _, qid, letter = c.data.split(":")
    async with Session() as s:
        q = await s.get(TestQuestion, int(qid))
        old, q.answer = q.answer, letter
        st = await _staff(s, c.from_user.id)
        await S.audit(s, st, "test_tuzatish", test=q.test_id, q=q.qid, answer=f"{old}->{letter}")
        await s.commit()
        tid = q.test_id
    await state.clear()
    await c.message.edit_text(f"✅ #{q.qid}: to'g'ri javob {old} → {letter}.", reply_markup=_review_kb(tid))


@router.callback_query(F.data.startswith("tfw:"))
async def fix_rewrite_ask(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Fx.rewrite)
    await state.update_data(tq=int(c.data.split(":")[1]))
    await c.answer()
    await c.message.answer("Savolni shu ko'rinishda yuboring:\n\n<code>savol: ...\nA) ...\nB) ...\nC) ...\nD) ...\n"
                           "javob: B</code>")


@router.message(Fx.rewrite)
async def fix_rewrite(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    errs: list[str] = []
    q = TF._parse_q(m.text or "", 1, errs)
    if not q:
        await m.answer("Format noto'g'ri: " + "; ".join(errs) + "\nQaytadan yuboring.")
        return
    async with Session() as s:
        tq = await s.get(TestQuestion, d["tq"])
        tq.text, tq.options, tq.answer = q.text, q.options, q.answer
        if q.figure:                         # yangi chizma yuborilsa — almashtiriladi, aks holda eskisi qoladi
            tq.figure = q.figure
        st = await _staff(s, m.from_user.id)
        await S.audit(s, st, "test_tuzatish", test=tq.test_id, q=tq.qid, rewrite=True)
        await s.commit()
        tid, no = tq.test_id, tq.qid
    await state.clear()
    await m.answer(f"✅ #{no} qayta yozildi.", reply_markup=_review_kb(tid))


@router.callback_query(F.data.startswith("trt:"))
async def return_ask(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Fx.note)
    await state.update_data(tid=int(c.data.split(":")[1]))
    await c.answer()
    await c.message.answer("Qanday xato va kamchiliklar bor? Batafsil yozing (savol raqamlari bilan):")


@router.message(Fx.note)
async def return_send(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    note = (m.text or "").strip()
    if len(note) < 5:
        await m.answer("Batafsilroq yozing.")
        return
    await state.clear()
    async with Session() as s:
        t = await s.get(Test, d["tid"])
        st = await _staff(s, m.from_user.id)
        t.status, t.review_note = "qaytarildi", note[:3000]
        await S.audit(s, st, "test_qaytarish", test=t.id)
        await s.commit()
        qs = (await s.execute(select(TestQuestion).where(TestQuestion.test_id == t.id)
                              .order_by(TestQuestion.qid))).scalars().all()
        txt = TF.render(t.meta or {}, [TF.Q(q.qid, q.variant, q.level, q.source or "", q.text, q.options, q.answer, q.figure or "")
                                       for q in qs])
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        g = await s.get(Group, t.group_id)
        tid, who = t.id, st.full_name
    await m.answer("↩️ Izohingiz rahbarga yuborildi. Tuzatilgan nusxa yana sizga keladi.")
    for b in bosses:
        try:
            await bot.send_document(b, BufferedInputFile(txt.encode(), f"test_{tid}_hozirgi.txt"),
                                    caption=f"↩️ <b>Test #{tid} qaytarildi</b> · {g.name}\n{who}:\n«{note[:800]}»\n\n"
                                            "Joriy faylni izoh bilan Claude'ga yuboring va tuzatilganini yuklang.",
                                    reply_markup=IM(inline_keyboard=[[IB(text="📎 Tuzatilgan faylni yuklash",
                                                                         callback_data=f"tup:{tid}")]]))
        except Exception:
            pass


@router.callback_query(F.data.startswith("tok:"))
async def approve(c: CallbackQuery, bot: Bot) -> None:
    tid = int(c.data.split(":")[1])
    async with Session() as s:
        t = await s.get(Test, tid)
        if t.status == "tasdiq":
            await c.answer("Allaqachon tasdiqlangan.", show_alert=True)
            return
        st = await _staff(s, c.from_user.id)
        t.status, t.approved_by = "tasdiq", st.id
        await S.audit(s, st, "test_tasdiq", test=tid)
        await s.commit()
    await c.answer("Tasdiqlandi")
    await c.message.answer(f"✅ Test #{tid} tasdiqlandi. Har bir o'quvchi uchun varaqlar tayyorlanmoqda...")
    await make_papers(bot, tid, c.message.chat.id)


# ================= 4. Varaqlar =================
def _bank_line(n_students: int, n: int) -> str:
    """So'rovga: Claude'ga yoziladigan bank hajmi (har o'quvchiga shaxsiy test uchun)."""
    nd = P.bank_need(max(1, n_students), n)
    return (f"O'quvchilar: {n_students} ta · har biriga {n} savol · takror ≤ {nd['k']}\n"
            f"🧮 <b>Bank: {nd['total']} ta savol</b> (oson {nd['oson']}, o'rta {nd['orta']}, qiyin {nd['qiyin']})\n"
            f"Claude'ga yozing: <code>bank {nd['total']}, har biriga {n}</code>\n")


async def _paper_roster(s, tid: int) -> list:
    """Varaq oladigan o'quvchilar: nazorat kunidagi guruh ro'yxati (test kimlar uchun bo'lsa — faqat ular)."""
    t = await s.get(Test, tid)
    ex = await s.get(Exam, t.exam_id) if t.exam_id else None
    day = ex.date if ex else now().date()
    roster = await S.group_roster(s, t.group_id, day)
    if t.students is not None:
        only = set(t.students)
        roster = [e for e in roster if e.id in only]
    return roster


async def make_papers(bot: Bot, tid: int, report_to: int | None = None) -> None:
    async with Session() as s:
        t = await s.get(Test, tid)
        g = await s.get(Group, t.group_id)
        ex = await s.get(Exam, t.exam_id) if t.exam_id else None
        day = ex.date if ex else now().date()
        if (await s.execute(select(TestPaper.id).where(TestPaper.test_id == tid,
                                                       TestPaper.answers.is_not(None)))).first():
            if report_to:
                await send(bot, report_to, "Bu test bo'yicha javoblar kiritilgan — varaqlar qayta tuzilmaydi.")
            return
        await s.execute(delete(TestPaper).where(TestPaper.test_id == tid))
        roster = await _paper_roster(s, tid)
        qs = (await s.execute(select(TestQuestion).where(TestQuestion.test_id == tid))).scalars().all()
        by_id = {q.qid: q for q in qs}
        bank_note = ""
        if t.n_variants == 0:               # bank rejimi: har o'quvchiga shaxsiy to'plam
            k0 = int((t.meta or {}).get("takror") or P.overlap_limit(t.n_questions))
            levels = {q.qid: ("orta" if q.level in ("orta", "o'rta") else q.level) for q in qs}
            sets, k = None, k0
            while sets is None and k <= t.n_questions:
                for sd in range(3):
                    sets = P.assign_bank(levels, len(roster), t.n_questions, k, seed=tid * 7 + sd)
                    if sets:
                        break
                if sets is None:
                    k += 1
            if sets is None:
                if report_to:
                    await send(bot, report_to, "❌ Bank juda kichik — varaqlar tuzilmadi. Ko'proq savol yuklang.")
                return
            bank_note = (f"\nHar o'quvchiga shaxsiy test: istalgan ikki o'quvchida ko'pi bilan {P.max_overlap(sets)} ta "
                         "bir xil savol." + (f" ⚠️ Bank yetmagani uchun chegara {k0} → {k} ga oshirildi."
                                             if k > k0 else ""))
            variants = [f"S{i:02d}" for i in range(1, len(roster) + 1)]
        else:
            variants = P.assign_variants(len(roster), max(1, t.n_variants))
        booklets, sheets_ = [], []
        for i, (e, v) in enumerate(zip(roster, variants), 1):
            if t.n_variants == 0:
                vq = [by_id[q] for q in sets[i - 1]]
            else:
                vq = [q for q in qs if t.n_variants == 1 or q.variant in ("umumiy", v)]
            pp = P.make_paper(v, vq, seed=zlib.crc32(f"{tid}:{e.id}:{t.version}".encode()))
            code = f"T{tid}-{i:02d}"
            paper = TestPaper(test_id=tid, enrollment_id=e.id, code=code, variant=v, qids=pp.qids, perms=pp.perms,
                              key=pp.key)
            s.add(paper)
            await s.flush()
            title = f"{g.subject or g.name} · {t.topic}" + (" · OYLIK NAZORAT" if ex and ex.monthly else "")
            qtexts = []
            for qid, perm in zip(pp.qids, pp.perms):
                q = by_id[qid]
                qtexts.append((q.text, [q.options[j] for j in perm], q.figure))
            booklets.append(SH.BookletItem(e.student.full_name, g.name, f"{day:%d.%m.%Y}", code, v, title, qtexts))
            sheets_.append(SH.SheetInfo(paper.id, e.student.full_name, g.name, f"{day:%d.%m.%Y}", code, v,
                                        len(pp.qids), title))
        if ex and not ex.test_id:
            ex.test_id = tid
        await s.commit()
        admins = [x.tg_id for x in await S.staff_with_role(s, "admin") if x.tg_id] or \
                 [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    if not booklets:
        if report_to:
            await send(bot, report_to, "Guruhda faol o'quvchi yo'q — varaq tayyorlanmadi.")
        return
    b_pdf = SH.booklets_pdf(booklets, settings.center_name)
    a_pdf = SH.answer_sheets_pdf(sheets_, settings.center_name)
    pages = sum(x.count(b"/Type /Page") - x.count(b"/Type /Pages") for x in (b_pdf, a_pdf))
    async with Session() as s:
        t = await s.get(Test, tid)
        t.meta = {**(t.meta or {}), "pages": pages}
        await s.commit()
    cap = (f"🖨 <b>Chop etish: {g.name}</b> · nazorat {day:%d.%m} · test #{tid} ({t.topic[:40]})\n{len(booklets)} ta o'quvchi · "
           f"{t.n_questions} savol\n1) Savollar varaqlari  2) Javob varaqlari (skaner uchun — 100% masshtabda, "
           "bir tomonlama chop eting). Chop etib, o'qituvchiga bering.")
    for a in admins:
        try:
            await bot.send_document(a, BufferedInputFile(b_pdf, f"{g.name}_{day:%d.%m}_T{tid}_savollar.pdf"))
            await bot.send_document(a, BufferedInputFile(a_pdf, f"{g.name}_{day:%d.%m}_T{tid}_javob_varaqlari.pdf"),
                                    caption=cap, reply_markup=IM(inline_keyboard=[[IB(
                                        text="✅ Chop etildi", callback_data=f"tpr:{tid}")]]))
        except Exception:
            pass
    if report_to:
        await send(bot, report_to, f"🖨 {len(booklets)} ta o'quvchi uchun varaqlar adminga chop etishga yuborildi."
                   + bank_note)


@router.callback_query(F.data.startswith("tpr:"))
async def printed(c: CallbackQuery, bot: Bot) -> None:
    tid = int(c.data.split(":")[1])
    async with Session() as s:
        t = await s.get(Test, tid)
        if t.printed_at:
            await c.answer("Allaqachon belgilangan.")
            return
        t.printed_at = now().replace(tzinfo=None)
        g = await s.get(Group, t.group_id)
        from app.db.models import PrinterUse
        who = await S.who_by_tg(s, c.from_user.id)
        s.add(PrinterUse(category="test", pages=int((t.meta or {}).get("pages", 0)), note=f"Test #{tid} · {g.name}",
                         test_id=tid, by_id=who.staff.id if who.staff else None))
        ex = await s.get(Exam, t.exam_id) if t.exam_id else None
        cond = await s.get(Staff, ex.conductor_id) if ex and ex.conductor_id else None
        teacher = await s.get(Staff, g.teacher_id) if g.teacher_id else None
        await s.commit()
    await c.answer("Belgilandi (test varaqlari printerda bepul chop etish sifatida qayd etildi)")
    await c.message.edit_reply_markup(reply_markup=None)
    scan_kb = IM(inline_keyboard=[[IB(text="📷 Test o'tgach: tekshirish (skaner)", callback_data=f"tme:{tid}")]])
    told = set()
    for x, txt in ((cond, "admindan olib keting."), (teacher, "")):
        if x and x.tg_id and x.tg_id not in told:
            told.add(x.tg_id)
            await send(bot, x.tg_id, f"🖨 «{g.name}» nazorati uchun varaqlar chop etildi{' — ' + txt if txt else '.'}\n"
                                     "Test o'tgach, javob varaqlarini shu tugma orqali telefon kamerasi bilan tekshirasiz "
                                     "(yoki: 📚 Testlar → 📋 Testlar holati).", reply_markup=scan_kb)


# ================= 5. Natijalar: skaner va qo'lda =================
async def scope_tests(s, tid: int) -> list[Test]:
    """Bitta nazoratdagi barcha testlar (kichik guruhlar) — natijalar birga kiritiladi va yakunlanadi."""
    t = await s.get(Test, tid)
    return await exam_tests(s, t.exam_id) if t and t.exam_id else ([t] if t else [])


async def scope_papers(s, tid: int) -> list[TestPaper]:
    ids = [t.id for t in await scope_tests(s, tid)]
    return list((await s.execute(select(TestPaper).where(TestPaper.test_id.in_(ids or [0]))
                                 .order_by(TestPaper.test_id, TestPaper.code))).scalars())


async def entry_menu(tid: int) -> tuple[str, IM]:
    async with Session() as s:
        ts = await scope_tests(s, tid)
        ps = await scope_papers(s, tid)
    done = sum(1 for p in ps if p.answers or p.absent)
    parts = ""
    if len(ts) > 1:
        parts = "\n" + "\n".join(f"• Test #{t.id}: {t.topic[:40]} — "
                                  + (f"{sum(1 for p in ps if p.test_id == t.id)} ta varaq" if any(p.test_id == t.id for p in ps)
                                     else ST.get(t.status, t.status)) for t in ts)
    text = (f"📝 Nazorat natijalari: kiritildi {done} / {len(ps)}{parts}\n\n"
            "📷 <b>Skaner</b>: pastki menyudagi «📷 Skaner» tugmasini bosing — kamera ochiladi. Varaqlarni "
            "birin-ketin kameraga tuting: bot QR koddan o'quvchi va guruhni o'zi aniqlaydi va tekshiradi.\n"
            "🖼 <b>Rasm bilan</b>: varaq rasmlarini botga yuborasiz.\n"
            "⌨️ <b>Qo'lda</b>: o'quvchini tanlab, javoblarini harflar bilan yozasiz.")
    kb = []
    uns = [p for p in ps if _uns(p)]
    if uns:
        text += (f"\n\n⚠️ <b>{len(uns)} ta o'quvchida noaniq savol bor</b> ("
                 f"{sum(len(_uns(p)) for p in uns)} ta). Yakunlashdan oldin varaqqa qarab tanlang.")
        kb.append([IB(text=f"⚠️ Noaniqlarni tanlash ({len(uns)} ta o'quvchi)", callback_data=f"tpe:{uns[0].id}")])
    kb += [[IB(text="🖼 Rasm yuborib tekshirish", callback_data=f"tsc:{tid}")],
          [IB(text="⌨️ Qo'lda kiritish", callback_data=f"tmn:{tid}")],
          [IB(text="✅ Yakunlash va natijalarni yuborish", callback_data=f"tfin:{tid}")]]
    return text, IM(inline_keyboard=kb)


class Sc(StatesGroup):
    scan = State()
    manual = State()


@router.callback_query(F.data.startswith("tsc:"))
async def scan_start(c: CallbackQuery, state: FSMContext) -> None:
    tid = int(c.data.split(":")[1])
    await state.set_state(Sc.scan)
    await state.update_data(tid=tid)
    await c.answer()
    await c.message.answer("📷 Javob varaqlarini yuboring (har biri alohida rasm). Maslahat: varaq tekis yuzada, "
                           "4 ta qora burchak ko'rinib tursin, soya tushmasin.\nTugatgach: «✅ Tugatdim».",
                           reply_markup=IM(inline_keyboard=[[IB(text="✅ Tugatdim", callback_data=f"tme:{tid}")]]))


@router.message(Sc.scan, F.photo | F.document)
async def scan_photo(m: Message, state: FSMContext, bot: Bot) -> None:
    from app.logic.omr import read_sheet
    fid = m.photo[-1].file_id if m.photo else m.document.file_id
    data = (await bot.download(fid, destination=io.BytesIO())).getvalue()
    res = read_sheet(data)                         # avval QR: qaysi varaq ekanini bilamiz
    if res.paper_id:
        async with Session() as s:
            st = await _staff(s, m.from_user.id)
            p = await s.get(TestPaper, res.paper_id)
            why = await _paper_block(s, st, p)
        if why:
            await m.reply("⚠️ " + why)
            return
        res = read_sheet(data, lambda pid: len(p.key) if pid == p.id else None)
    if res.error:
        await m.reply("⚠️ " + res.error)
        return
    await _save_answers(m, res.paper_id, res.answers, scanned=True, unsure=res.unsure)


async def _paper_block(s, st: Staff | None, p: TestPaper | None) -> str | None:
    """Varaq natijasini shu xodim kirita oladimi? Mumkin bo'lsa None, aks holda sabab."""
    if not p:
        return "Bu varaq bazada topilmadi."
    if not st or st.current_role not in ("rahbar", "oqituvchi", "yordamchi", "admin"):
        return "Ruxsat yo'q."
    t = await s.get(Test, p.test_id)
    ex = await s.get(Exam, t.exam_id) if t.exam_id else None
    if ex and ex.status == "otdi":
        return f"«{(await s.get(Group, t.group_id)).name}» nazorati natijalari allaqachon e'lon qilingan."
    if _is_boss(st) or (ex and ex.conductor_id == st.id):
        return None
    if t.group_id in {g.id for g in await _groups_for(s, st)}:
        return None
    return "Bu varaq sizning guruhingizga tegishli emas."


# ---------- jonli skaner (Telegram Mini App, docs/scan.html) ----------
@scan_router.message(F.web_app_data)
async def live_scan_result(m: Message, bot: Bot) -> None:
    data = m.web_app_data.data or ""
    if not data.startswith("E1|"):
        return
    items = []
    for part in data[3:].split(";"):
        pid, _, ans = part.partition(":")
        if pid.isdigit() and ans and all(ch in "ABCDabcd-*?" for ch in ans):
            items.append((int(pid), ans))
    if not items:
        await m.answer("Skanerdan ma'lumot kelmadi.")
        return
    by_test: dict[int, list[str]] = {}
    fixes, errors, tids = [], [], []
    async with Session() as s:
        st = await _staff(s, m.from_user.id)
        for pid, raw in items:
            p = await s.get(TestPaper, pid)
            why = await _paper_block(s, st, p)
            if why:
                errors.append(f"№{pid}: {why}")
                continue
            enr = await s.get(Enrollment, p.enrollment_id)
            name = (await s.get(Student, enr.student_id)).full_name
            n = len(p.key)
            if len(raw) > n and len(raw) == 20 * -(-n // 20):     # eski varaq: skaner butun ustunni o'qigan
                raw = raw[:n]
            if len(raw) != n:
                errors.append(f"{name}: varaqdagi savollar soni ({len(raw)}) testga ({n}) mos emas — qayta skaner qiling "
                              "yoki qo'lda kiriting.")
                continue
            unsure = [i + 1 for i, ch in enumerate(raw) if ch in "abcd?"]
            double = [i + 1 for i, ch in enumerate(raw) if ch == "*"]
            ans = raw.upper()
            p.answers, p.absent, p.scanned, p.entered_by = ans, False, True, st.id
            p.unsure = unsure or None
            p.correct = P.score(ans, p.key)
            line = f"• {name} — <b>{p.correct}/{n}</b> ({p.correct / n * 100:.0f}%)"
            if double:
                line += f" · ikkita belgilangan (xato): {', '.join(map(str, double))}"
            if unsure:
                line += f" ⚠️ noaniq: {', '.join(map(str, unsure))}"
                fixes.append((p.id, name))
            by_test.setdefault(p.test_id, []).append(line)
        await s.commit()
        blocks = []
        for tid, lines in by_test.items():
            t = await s.get(Test, tid)
            g = await s.get(Group, t.group_id)
            ps = await scope_papers(s, tid)
            done = sum(1 for p in ps if p.answers or p.absent)
            blocks.append(f"<b>{g.name}</b> · test #{tid} — tekshirildi {done}/{len(ps)}\n" + "\n".join(lines))
            tids.append((tid, g.name, done, len(ps)))
    saved = sum(len(v) for v in by_test.values())
    text = (f"📷 <b>Skaner natijasi</b>: {saved} ta varaq\n\n" + "\n\n".join(blocks)) if saved else \
        "📷 <b>Skaner natijasi</b>: hech bir varaq saqlanmadi."
    if fixes:
        text += ("\n\n⚠️ Noaniq savollar: o'quvchining varag'iga qarang va qaysi doiracha bo'yalganini tugma bilan "
                 "tanlang. To'g'ri javobni bilish shart emas — bot o'zi tekshiradi.")
    if errors:
        text += "\n\n❗ " + "\n❗ ".join(errors)
    kb = [[IB(text=f"✏️ {nm} — noaniqlarni tanlash", callback_data=f"tpe:{pid}")] for pid, nm in fixes[:20]]
    for tid, gname, done, total in tids:
        kb.append([IB(text=f"📝 {gname}: {done}/{total} — davom etish / yakunlash", callback_data=f"tme:{tid}")])
    while len(text) > 3900:                                  # Telegram chegarasi
        cut = text.rfind("\n", 0, 3900)
        await m.answer(text[:cut])
        text = text[cut:].lstrip("\n")
    await m.answer(text, reply_markup=IM(inline_keyboard=kb) if kb else None)


async def _save_answers(m: Message, paper_id: int, answers: str, scanned: bool, unsure: list[int] | None = None) -> None:
    async with Session() as s:
        p = await s.get(TestPaper, paper_id)
        st = await _staff(s, m.from_user.id)
        p.answers, p.absent, p.scanned, p.entered_by = answers, False, scanned, st.id if st else None
        p.unsure = [q for q in (unsure or []) if 0 < q <= len(answers) and answers[q - 1] != "*"] or None
        p.correct = P.score(answers, p.key)
        enr = await s.get(Enrollment, p.enrollment_id)
        stu = await s.get(Student, enr.student_id)
        await s.commit()
        name, n, correct = stu.full_name, len(p.key), p.correct
    text = f"✅ {name}: <b>{correct} / {n}</b> ({correct / n * 100:.0f}%)"
    dbl = [i + 1 for i, ch in enumerate(answers) if ch == "*"]
    if dbl:
        text += f"\n❌ Ikkita belgilangan (xato hisoblandi): {', '.join(map(str, dbl))}"
    unsure = [q for q in (unsure or []) if q not in dbl]
    kb = None
    if unsure:
        text += (f"\n⚠️ Aniqlanmagan savollar: {', '.join(map(str, unsure))} — o'quvchining varag'iga qarab "
                 "qaysi doiracha bo'yalganini tanlaysiz.")
        kb = IM(inline_keyboard=[[IB(text="✏️ Noaniqlarni tanlash", callback_data=f"tpe:{paper_id}")]])
    await m.reply(text, reply_markup=kb)


@router.callback_query(F.data.startswith("tme:"))
async def scan_done(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await entry_menu(int(c.data.split(":")[1]))
    await c.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("tmn:"))
async def manual_list(c: CallbackQuery) -> None:
    tid = int(c.data.split(":")[1])
    async with Session() as s:
        ids = [t.id for t in await scope_tests(s, tid)]
        rows = (await s.execute(select(TestPaper, Student.full_name).join(Enrollment, Enrollment.id == TestPaper.enrollment_id)
                                .join(Student, Student.id == Enrollment.student_id).where(TestPaper.test_id.in_(ids))
                                .order_by(Student.full_name))).all()
    kb = [[IB(text=("✅ " if p.answers else "➖ " if p.absent else "▫️ ") + f"{n} ({p.code})" +
                  (f" — {p.correct}" if p.answers else ""), callback_data=f"tpe:{p.id}")] for p, n in rows]
    kb.append([IB(text="⬅️ Orqaga", callback_data=f"tme:{tid}")])
    await c.message.answer("O'quvchini tanlang:", reply_markup=IM(inline_keyboard=kb))


def _ask_kb(pid: int, q: int) -> IM:
    return IM(inline_keyboard=[
        [IB(text=x, callback_data=f"tpq:{pid}:{q}:{x}") for x in "ABCD"],
        [IB(text="⚪ Bo'sh qoldirilgan", callback_data=f"tpq:{pid}:{q}:-"),
         IB(text="❌ Ikkitasi bo'yalgan", callback_data=f"tpq:{pid}:{q}:*")],
        [IB(text="⌨️ Hamma javobni qayta yozish", callback_data=f"tpe:{pid}:all")]])


def _uns(p) -> list[int]:
    """Haqiqiy noaniq savollar: ikkita belgilangan («*») savol noaniq emas — avtomatik xato."""
    if not p or not p.unsure or not p.answers:
        return []
    return [q for q in p.unsure if 0 < q <= len(p.answers) and p.answers[q - 1] != "*"]


async def _ask_unsure(msg: Message, pid: int, edit: bool = False) -> bool:
    """Varaqdagi navbatdagi noaniq savolni so'raydi. So'raydigan savol qolmasa — False."""
    async with Session() as s:
        p = await s.get(TestPaper, pid)
        if not p or not _uns(p):
            return False
        enr = await s.get(Enrollment, p.enrollment_id)
        name = (await s.get(Student, enr.student_id)).full_name
    pu = _uns(p)
    q = pu[0]
    g = p.answers[q - 1]
    text = (f"✏️ <b>{name}</b> · <b>{q}-savol</b>" + (f"  (yana {len(pu) - 1} ta)" if len(pu) > 1 else "")
            + "\n\nO'quvchining javob varag'iga qarang: shu savolda qaysi doiracha bo'yalgan?"
            + (f"\nSkaner taxmini: <b>{g}</b>" if g in "ABCD" else "")
            + "\n\n<i>To'g'ri javobni bilish shart emas — o'quvchi nimani belgilaganini tanlang, bot o'zi tekshiradi.</i>")
    if edit:
        await msg.edit_text(text, reply_markup=_ask_kb(pid, q))
    else:
        await msg.answer(text, reply_markup=_ask_kb(pid, q))
    return True


@router.callback_query(F.data.startswith("tpq:"))
async def unsure_pick(c: CallbackQuery) -> None:
    _, pid, q, ch = c.data.split(":")
    pid, q = int(pid), int(q)
    async with Session() as s:
        p = await s.get(TestPaper, pid)
        if not p or q not in _uns(p):
            await c.answer("Bu savol allaqachon tanlangan.")
            return
        a = list(p.answers)
        a[q - 1] = ch
        p.answers = "".join(a)
        p.unsure = [x for x in _uns(p) if x != q] or None
        p.correct = P.score(p.answers, p.key)
        await s.commit()
        tid, n, correct, left = p.test_id, len(p.key), p.correct, p.unsure
        nxt = None
        if not left:
            nxt = next((x for x in await scope_papers(s, tid) if _uns(x) and x.id != pid), None)
            enr = await s.get(Enrollment, p.enrollment_id)
            name = (await s.get(Student, enr.student_id)).full_name
    await c.answer(f"{q}-savol: {ch if ch in 'ABCD' else ('bo‘sh' if ch == '-' else 'ikkita')}")
    if left:
        await _ask_unsure(c.message, pid, edit=True)
        return
    await c.message.edit_text(f"✅ {name}: <b>{correct} / {n}</b> ({correct / n * 100:.0f}%) — noaniq savol qolmadi.")
    if nxt:
        await _ask_unsure(c.message, nxt.id)
    else:
        text, kb = await entry_menu(tid)
        await c.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("tpe:"))
async def manual_one(c: CallbackQuery, state: FSMContext) -> None:
    parts = c.data.split(":")
    pid = int(parts[1])
    if len(parts) == 2 and await _ask_unsure(c.message, pid):
        await c.answer()
        return
    async with Session() as s:
        p = await s.get(TestPaper, pid)
        enr = await s.get(Enrollment, p.enrollment_id)
        stu = await s.get(Student, enr.student_id)
    await state.set_state(Sc.manual)
    await state.update_data(pid=pid, tid=p.test_id)
    cur = f"\nHozirgi: <code>{p.answers}</code>" if p.answers else ""
    await c.answer()
    await c.message.answer(f"<b>{stu.full_name}</b> ({p.code}, " + ("shaxsiy test" if p.variant.startswith("S") else f"{p.variant}-variant") + f"){cur}\n\n"
                           f"Javob varag'idagi {len(p.key)} ta javobni tartib bilan yozing, masalan:\n"
                           f"<code>{('ABCD' * 20)[:len(p.key)]}</code>\nBo'sh qoldirilgan savol: <b>-</b>"
                           + ("\n\nFaqat bitta-ikkita savolni o'zgartirish uchun savol raqami va o'quvchi bo'yagan "
                              "harfni yozing, masalan: <code>25 B</code> yoki <code>25B 12C</code>" if p.answers else ""),
                           reply_markup=IM(inline_keyboard=[[IB(text="➖ Qatnashmadi", callback_data=f"tab:{pid}")]]))


@router.message(Sc.manual)
async def manual_save(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    async with Session() as s:
        p = await s.get(TestPaper, d["pid"])
        n = len(p.key)
    import re
    pairs = re.findall(r"(\d{1,3})\s*[-=:.)]?\s*([A-Da-d])(?![A-Da-d])", m.text or "")
    rest = re.sub(r"(\d{1,3})\s*[-=:.)]?\s*([A-Da-d])(?![A-Da-d])", "", m.text or "").strip(" ,;\n")
    if pairs and not rest and p.answers:                 # «25 B» — faqat shu savollar o'zgaradi
        bad = [q for q, _ in pairs if not 1 <= int(q) <= n]
        if bad:
            await m.answer(f"Savol raqami 1–{n} oralig'ida bo'lishi kerak.")
            return
        a = list(p.answers)
        for q, ch in pairs:
            a[int(q) - 1] = ch.upper()
        fixed = {int(q) for q, _ in pairs}
        unsure = [q for q in (p.unsure or []) if q not in fixed]
        await state.clear()
        await _save_answers(m, d["pid"], "".join(a), scanned=p.scanned, unsure=unsure)
        text, kb = await entry_menu(d["tid"])
        await m.answer(text, reply_markup=kb)
        return
    ans = P.clean_answers(m.text or "", n)
    if not ans:
        got = len("".join(ch for ch in (m.text or "").upper() if ch in "ABCD-0"))
        await m.answer(f"{n} ta javob kerak, siz {got} ta yozdingiz. Qaytadan yozing:")
        return
    await state.clear()
    await _save_answers(m, d["pid"], ans, scanned=False)          # to'liq qayta yozildi — noaniqlar tozalanadi
    text, kb = await entry_menu(d["tid"])
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("tab:"))
async def manual_absent(c: CallbackQuery, state: FSMContext) -> None:
    pid = int(c.data.split(":")[1])
    async with Session() as s:
        p = await s.get(TestPaper, pid)
        p.absent, p.answers, p.correct, p.unsure = True, None, None, None
        await s.commit()
        tid = p.test_id
    await state.clear()
    await c.answer("Qatnashmadi deb belgilandi")
    text, kb = await entry_menu(tid)
    await c.message.answer(text, reply_markup=kb)


async def _ensure_exam(s, t: Test, st: Staff | None) -> Exam:
    """Test nazoratga bog'lanmagan bo'lsa: shu guruhning yaqin rejadagi nazoratiga bog'laydi, bo'lmasa yangisini ochadi."""
    today = now().date()
    try:
        day = date.fromisoformat((t.meta or {}).get("sana") or "")
    except ValueError:
        day = today
    day = min(day, today)
    ex = (await s.execute(select(Exam).where(Exam.group_id == t.group_id, Exam.status == "reja",
                                             Exam.date >= day - timedelta(days=7), Exam.date <= today + timedelta(days=1))
                          .order_by(Exam.date.desc()))).scalars().first()
    if ex and (await exam_tests(s, ex.id)):
        ex = None                              # bu nazoratning o'z testi bor — aralashtirmaymiz
    if not ex:
        week = day - timedelta(days=day.weekday())
        n = len((await s.execute(select(Exam.id).where(Exam.group_id == t.group_id, Exam.week == week))).all())
        ex = Exam(group_id=t.group_id, date=day, week=week, slot=n + 1, conductor_id=st.id if st else None,
                  kind="test", topic=(t.topic or "")[:200], test_id=t.id)
        s.add(ex)
        await s.flush()
    t.exam_id = ex.id
    await s.flush()
    return ex


@router.callback_query(F.data.startswith("tfin:"))
async def finish(c: CallbackQuery, bot: Bot) -> None:
    tid = int(c.data.split(":")[1])
    force = c.data.endswith(":ok")
    skip_unsure = force or c.data.endswith(":u")
    async with Session() as s:
        t = await s.get(Test, tid)
        ts = await scope_tests(s, tid)
        ps = await scope_papers(s, tid)
        with_papers = {p.test_id for p in ps}
        not_ready = [x for x in ts if x.id not in with_papers]
        if not_ready:
            await c.answer()
            await c.message.answer("⚠️ Bu nazoratdagi boshqa test(lar) hali tayyor emas: "
                                   + ", ".join(f"#{x.id} {x.topic[:30]} ({ST.get(x.status, x.status)})" for x in not_ready)
                                   + ".\nNatijalar barcha testlar bilan birga yakunlanadi — avval ularni tayyorlang.")
            return
        uns = [p for p in ps if _uns(p)]
        if uns and not skip_unsure:
            await c.answer()
            await c.message.answer(f"⚠️ {len(uns)} ta o'quvchida noaniq savol bor. Avval varaqqa qarab tanlang — "
                                   "aks holda ular xato deb hisoblanishi mumkin.",
                                   reply_markup=IM(inline_keyboard=[
                                       [IB(text="✏️ Noaniqlarni tanlash", callback_data=f"tpe:{uns[0].id}")],
                                       [IB(text="Baribir yakunlash", callback_data=f"tfin:{tid}:u")]]))
            return
        left = [p for p in ps if not p.answers and not p.absent]
        if left and not force:
            await c.message.answer(f"⚠️ {len(left)} ta o'quvchining javobi kiritilmagan. Ular «qatnashmadi» deb "
                                   "hisoblanadi. Davom etamizmi?",
                                   reply_markup=IM(inline_keyboard=[[IB(text="✅ Ha, yakunlash", callback_data=f"tfin:{tid}:ok"),
                                                                     IB(text="⬅️ Orqaga", callback_data=f"tme:{tid}")]]))
            await c.answer()
            return
        st = await _staff(s, c.from_user.id)
        if not t.exam_id:                      # rejadan tashqari test — nazoratga o'zi bog'lanadi
            await _ensure_exam(s, t, st)
        ex = await s.get(Exam, t.exam_id)
        g = await s.get(Group, ex.group_id)
        rules = await S.get_rules(s)
        from app.bot.exams import _apply_coins, APPEAL_HOURS
        sizes = {len(p.key) for p in ps}
        mixed = len(sizes) > 1                 # kichik guruhlarda savollar soni har xil — 100 ballik
        ex.kind, ex.status, ex.done_at = "test", "otdi", now().replace(tzinfo=None)
        ex.max_score = 100.0 if mixed else float(sizes.pop() if sizes else t.n_questions)
        msgs = []
        for p in ps:
            r = (await s.execute(select(ExamResult).where(ExamResult.exam_id == ex.id,
                                                          ExamResult.enrollment_id == p.enrollment_id))).scalar_one_or_none()
            if not r:
                r = ExamResult(exam_id=ex.id, enrollment_id=p.enrollment_id)
                s.add(r)
            n = len(p.key)
            r.percent = round(p.correct / n * 100, 1) if p.answers else None
            r.score = (r.percent if mixed else float(p.correct)) if p.answers else None
            r.answers = p.answers
            await s.flush()
            enr = await s.get(Enrollment, p.enrollment_id)
            stu = await s.get(Student, enr.student_id)
            await _apply_coins(s, r, stu, rules, st, ex)
            if not p.answers:
                continue
            wrong = [f"{i}: " + (f"siz {a}" if a in "ABCD" else "ikkita javob belgilangan" if a == "*"
                                  else "javob belgilanmagan") + f", to'g'ri {k}"
                     for i, (a, k) in enumerate(zip(p.answers, p.key), 1) if a != k]
            n_double = p.answers.count("*")
            topic = next((x.topic for x in ts if x.id == p.test_id), "")
            text = (f"📝 <b>Nazorat natijasi</b> · {g.name} · {ex.date:%d.%m}" + (" · oylik" if ex.monthly else "") +
                    (f"\nMavzu: {topic}" if len(ts) > 1 and topic else "") +
                    f"\n{stu.full_name}: <b>{p.correct} / {n}</b> ({r.percent:.0f}%)"
                    + (f"\n🎉 +{r.coins_given} coin" if r.coins_given else "")
                    + ("\n\nXato javoblar:\n" + "\n".join(wrong[:40]) if wrong else "\n\nHammasi to'g'ri! 👏")
                    + (f"\n\n⚠️ {n_double} ta savolda ikkita javob belgilangan — qoidaga ko'ra bunday savol xato "
                       "hisoblanadi. Har savolda faqat BITTA doirachani bo'yash kerak." if n_double else ""))
            for tgt in await S.parent_tg_ids(s, stu.id):
                msgs.append((tgt, text, r.id))
        await S.audit(s, st, "nazorat", group=g.name, date=ex.date.isoformat(), kind="test")
        await s.commit()
    kb = lambda rid: IM(inline_keyboard=[[IB(text="📝 Apellyatsiya", callback_data=f"apl:{rid}")]])
    for tgt, text, rid in msgs:
        await send(bot, tgt, text + f"\n\nE'tirozingiz bo'lsa — {APPEAL_HOURS} soat ichida:", reply_markup=kb(rid))
    await c.message.answer(f"✅ Nazorat yakunlandi. Natijalar {len(msgs)} ta manzilga yuborildi.")
    await c.answer()


async def paper_question_info(result_id: int, qno: int) -> str | None:
    """Apellyatsiya uchun: o'quvchining varag'idagi qno-savol, uning javobi va to'g'ri javob."""
    async with Session() as s:
        r = await s.get(ExamResult, result_id)
        ex = await s.get(Exam, r.exam_id)
        ids = [t.id for t in await exam_tests(s, ex.id)]
        if not ids:
            return None
        p = (await s.execute(select(TestPaper).where(TestPaper.test_id.in_(ids),
                                                     TestPaper.enrollment_id == r.enrollment_id))).scalars().first()
        if not p or not p.answers or not 1 <= qno <= len(p.qids):
            return None
        q = (await s.execute(select(TestQuestion).where(TestQuestion.test_id == p.test_id,
                                                        TestQuestion.qid == p.qids[qno - 1]))).scalar_one()
    perm = p.perms[qno - 1]
    opts = "\n".join(f"{x}) {q.options[j]}" for x, j in zip("ABCD", perm))
    return (f"Varaqdagi {qno}-savol (bazada #{q.qid}):\n{q.text}\n{opts}\n"
            f"O'quvchi javobi: <b>{p.answers[qno - 1]}</b> · to'g'ri: <b>{p.key[qno - 1]}</b>")


# ================= 6. Holat =================
@router.callback_query(F.data == "tsl:list")
async def tests_list(c: CallbackQuery) -> None:
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        q = select(Test, Group.name).join(Group, Group.id == Test.group_id).order_by(Test.id.desc()).limit(15)
        if not _is_boss(st):
            gids = [g.id for g in await _groups_for(s, st)]
            q = q.where(Test.group_id.in_(gids or [0]))
        rows = (await s.execute(q)).all()
        prog = {}                                   # test -> (tekshirilgan, jami, yakunlanganmi, exam_id)
        for t, _ in rows:
            ps = list((await s.execute(select(TestPaper).where(TestPaper.test_id == t.id))).scalars())
            if ps:
                ex = await s.get(Exam, t.exam_id) if t.exam_id else None
                prog[t.id] = (sum(1 for p in ps if p.answers or p.absent), len(ps),
                              bool(ex and ex.status == "otdi"), t.exam_id)
    if not rows:
        await c.message.answer("Hozircha test yo'q.")
        return
    lines = []
    for t, gname in rows:
        line = (f"#{t.id} · {gname} · {t.topic[:40]}" + (f" · {len(t.students)} o'quvchi" if t.students is not None else "")
                + f" — {ST.get(t.status, t.status)}" + (" · 🖨 chop etildi" if t.printed_at else ""))
        if t.id in prog:
            done, total, fin, _ = prog[t.id]
            line += " · 📊 natija e'lon qilindi" if fin else f" · 📝 tekshirildi {done}/{total}"
        lines.append(line)
    kb = [[IB(text=f"📎 #{t.id} faylini yuklash", callback_data=f"tup:{t.id}")]
          for t, _ in rows if _is_boss(st) and t.status in ("sorov", "qaytarildi")]
    kb += [[IB(text=f"👀 #{t.id} ni ko'rib chiqish", callback_data=f"trv:{t.id}")]
           for t, _ in rows if t.status == "korib"]
    for t, _ in rows:
        if t.id not in prog:
            continue
        done, total, fin, eid = prog[t.id]
        if fin:
            kb.append([IB(text=f"📊 #{t.id} natijalari", callback_data=f"exv:{eid}")])
        else:
            kb.append([IB(text=f"📷 #{t.id} ni tekshirish (skaner)", callback_data=f"tme:{t.id}")])
    await c.message.answer("📋 <b>Testlar</b>\n" + "\n".join(lines), reply_markup=IM(inline_keyboard=kb) if kb else None)



# ================= rahbar: kelgan test so'rovlari =================
async def pending_requests(s) -> list[tuple[Test, str, str]]:
    rows = (await s.execute(select(Test, Group.name, Staff.full_name).join(Group, Group.id == Test.group_id)
                            .outerjoin(Staff, Staff.id == Test.requested_by)
                            .where(Test.status.in_(("sorov", "qaytarildi"))).order_by(Test.id))).all()
    return [(t, g, n or "") for t, g, n in rows]


async def send_request_cards(bot: Bot, chat: int) -> int:
    """Har bir kutilayotgan so'rov: materiallar (rasm/PDF) + «📎 Test faylini yuklash»."""
    async with Session() as s:
        reqs = await pending_requests(s)
        exams = {t.exam_id: await s.get(Exam, t.exam_id) for t, *_ in reqs if t.exam_id}
    for t, gname, who in reqs:
        ex = exams.get(t.exam_id)
        when = (_exam_label(ex) if ex else
                f"{date.fromisoformat(t.meta['sana']):%d.%m.%Y}" if (t.meta or {}).get("sana") else "sana aniq emas")
        files = t.photos or []
        imgs = [f for f in files if not f.startswith("doc:")]
        for i in range(0, len(imgs), 10):
            try:
                await bot.send_media_group(chat, [InputMediaPhoto(media=f) for f in imgs[i:i + 10]])
            except Exception:
                pass
        for f in files:
            if f.startswith("doc:"):
                try:
                    await bot.send_document(chat, f[4:])
                except Exception:
                    pass
        status = "🔁 qaytarilgan, qayta yuklash kerak" if t.status == "qaytarildi" else "kutilmoqda"
        async with Session() as s2:
            n_st = len(await _paper_roster(s2, t.id))
        await send(bot, chat, f"📨 <b>Test so'rovi #{t.id}</b> ({status})\n{who} · {gname}\nNazorat: {when}\n"
                              f"Mavzu: {t.topic}\n" + _bank_line(n_st, t.n_questions) + f"Savollar: {t.n_questions} ta"
                              + (f" · {len(t.students)} o'quvchi uchun" if t.students is not None else " · butun guruh")
                              + (f"\nIzoh: {t.review_note}" if t.status == "qaytarildi" and t.review_note else ""),
                   reply_markup=IM(inline_keyboard=[[IB(text="📎 Test faylini yuklash", callback_data=f"tup:{t.id}")]]))
    return len(reqs)


@router.callback_query(F.data == "tsr:list")
async def requests_list(c: CallbackQuery, bot: Bot) -> None:
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
    if not _is_boss(st):
        await c.answer("Faqat rahbar.", show_alert=True)
        return
    await c.answer()
    if not await send_request_cards(bot, c.message.chat.id):
        await c.message.answer("📥 Kutilayotgan test so'rovi yo'q ✅")
