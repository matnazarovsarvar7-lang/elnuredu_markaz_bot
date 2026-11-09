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

router = Router()
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
    rows.append([IB(text="📎 Tayyor testni yuklash (.txt)", callback_data="tsu:new")])
    rows.append([IB(text="📋 Testlar holati", callback_data="tsl:list")])
    await m.answer("📚 <b>Testlar</b>\n\n"
                   "• So'rov — mavzu va kitob rasmlarini rahbarga yuborasiz, u test tuzib beradi.\n"
                   "• Tayyor test — andoza bo'yicha yozilgan .txt faylni yuklaysiz.\n"
                   "Bot har bir o'quvchi uchun alohida varaq tayyorlab, adminga chop etishga yuboradi.",
                   reply_markup=IM(inline_keyboard=rows))


async def _groups_for(s, st: Staff) -> list[Group]:
    return await S.teacher_groups(s, st, st.current_role)


async def _open_exams(s, gid: int) -> list[Exam]:
    t = now().date()
    rows = (await s.execute(select(Exam).where(Exam.group_id == gid, Exam.status == "reja", Exam.date >= t)
                            .order_by(Exam.date))).scalars().all()
    return [e for e in rows if not e.test_id]


# ================= 1. So'rov (o'qituvchi) =================
class Rq(StatesGroup):
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
    rows = [[IB(text=f"📝 {_exam_label(e)}", callback_data=f"tse:{e.id}")] for e in exams]
    rows.append([IB(text="Nazorat sanasi hali belgilanmagan", callback_data="tse:0")])
    await c.message.edit_text("Qaysi nazorat uchun?", reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data.startswith("tse:"))
async def got_exam(c: CallbackQuery, state: FSMContext) -> None:
    eid = int(c.data.split(":")[1])
    await state.update_data(eid=eid or None)
    d = await state.get_data()
    if d.get("mode") == "tsu":
        await state.set_state(Up.file)
        await c.message.edit_text("📎 Test faylini (.txt) yuboring. Format — «Test tuzish prompti» dagi andoza.\n"
                                  "Bekor qilish: /start")
        return
    await state.set_state(Rq.topic)
    await c.message.edit_text("Mavzu(lar)ni yozing.\n<i>Masalan: Aniq va noaniq artikllar (a, an, the)</i>")


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
    await c.message.edit_text("📷 O'tilgan mavzuning kitob sahifalarini rasmga olib yuboring (bir nechta bo'lishi "
                              "mumkin). Tugatgach «✅ So'rovni yuborish» ni bosing.",
                              reply_markup=IM(inline_keyboard=[[IB(text="✅ So'rovni yuborish", callback_data="tsq:send")]]))


@router.message(Rq.photos, F.photo)
async def rq_photo(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    photos = d.get("photos", []) + [m.photo[-1].file_id]
    await state.update_data(photos=photos)
    if len(photos) in (1, 5, 10):
        await m.answer(f"Qabul qilindi: {len(photos)} ta rasm. Yana yuboring yoki «✅ So'rovni yuborish».",
                       reply_markup=IM(inline_keyboard=[[IB(text="✅ So'rovni yuborish", callback_data="tsq:send")]]))


@router.callback_query(Rq.photos, F.data == "tsq:send")
async def rq_send(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        g = await s.get(Group, d["gid"])
        ex = await s.get(Exam, d["eid"]) if d.get("eid") else None
        t = Test(group_id=g.id, exam_id=ex.id if ex else None, topic=d["topic"], n_questions=d["n"],
                 photos=d.get("photos", []), status="sorov", source="rahbar", requested_by=st.id)
        s.add(t)
        await s.flush()
        if ex:
            ex.test_id = t.id
        await S.audit(s, st, "test_sorov", group=g.name, topic=d["topic"])
        await s.commit()
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        tid, gname, who = t.id, g.name, st.full_name
    when = _exam_label(ex) if ex else "sana belgilanmagan"
    text = (f"📨 <b>Test so'rovi #{tid}</b>\n{who} · {gname}\nNazorat: {when}\nMavzu: {d['topic']}\n"
            f"Savollar: {d['n']} ta · rasmlar: {len(d.get('photos', []))} ta\n\n"
            "Rasmlarni «Test tuzish prompti» bilan Claude'ga yuboring, tayyor faylni shu yerga yuklang.")
    for b in bosses:
        photos = d.get("photos", [])
        for i in range(0, len(photos), 10):
            try:
                await bot.send_media_group(b, [InputMediaPhoto(media=f) for f in photos[i:i + 10]])
            except Exception:
                pass
        await send(bot, b, text, reply_markup=IM(inline_keyboard=[[IB(text="📎 Test faylini yuklash",
                                                                       callback_data=f"tup:{tid}")]]))
    await c.message.edit_text(f"✅ So'rov #{tid} rahbarga yuborildi. Test tayyor bo'lgach sizga ko'rib chiqishga keladi.")


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
                     source="rahbar" if _is_boss(st) else "oqituvchi", requested_by=st.id)
            s.add(t)
            await s.flush()
            if ex:
                ex.test_id = t.id
        await s.execute(delete(TestQuestion).where(TestQuestion.test_id == t.id))
        for q in tf.questions:
            s.add(TestQuestion(test_id=t.id, qid=q.qid, variant=q.variant, level=q.level, source=q.source[:120],
                               text=q.text, options=q.options, answer=q.answer))
        t.n_questions, t.n_variants, t.common = tf.n, tf.variants, tf.common
        t.meta, t.version, t.uploaded_by = tf.meta, (t.version or 0) + 1, st.id
        own = not _is_boss(st)
        t.status = "tasdiq" if own else "korib"
        if own:
            t.approved_by = st.id
        await S.audit(s, st, "test_yuklash", test=t.id, version=t.version)
        await s.commit()
        tid = t.id
    lv = TF.level_counts(tf.variant_questions("A"))
    summary = (f"✅ Fayl qabul qilindi (#{tid}, {tf.n} savol, {tf.variants} variant).\n"
               f"A-variant qiyinligi: oson {lv['oson']}, o'rta {lv['orta']}, qiyin {lv['qiyin']}.")
    if tf.warnings:
        summary += "\n⚠️ " + "\n⚠️ ".join(tf.warnings[:5])
    if own:
        await m.answer(summary + "\n\nO'zingiz yuklaganingiz uchun tasdiqlandi. Varaqlar tayyorlanmoqda...")
        await make_papers(bot, tid, m.chat.id)
    else:
        await m.answer(summary + "\n\nO'qituvchiga ko'rib chiqishga yuborildi.")
        await send_review(bot, tid)


# ================= 3. Ko'rib chiqish =================
async def _reviewers(s, t: Test) -> list[int]:
    g = await s.get(Group, t.group_id)
    ids = {x for x in (g.teacher_id, g.assistant_id, t.requested_by) if x}
    out = []
    for i in ids:
        st = await s.get(Staff, i)
        if st and st.tg_id and not st.roles.startswith("rahbar"):
            out.append(st.tg_id)
    if not out:                                   # rahbarning o'z guruhi
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
    rows = [(q.qid, q.variant, q.level, q.source or "", q.text, q.options, q.answer) for q in qs]
    return SH.review_pdf(f"#{t.id} · {g.name} · {t.topic} · {t.version}-nusxa", rows)


async def send_review(bot: Bot, tid: int) -> None:
    async with Session() as s:
        t = await s.get(Test, tid)
        g = await s.get(Group, t.group_id)
        pdf = await _review_file(s, t)
        to = await _reviewers(s, t)
    cap = (f"👀 <b>Test #{tid} ko'rib chiqish uchun</b> ({t.version}-nusxa)\n{g.name} · {t.topic}\n"
           f"{t.n_questions} savol · {t.n_variants} variant\n\n"
           "Faylda har savolning to'g'ri javobi belgilangan. Tekshiring:\n"
           "• hammasi to'g'ri — «✅ Tasdiqlash»;\n• kichik xato — «✏️ Savolni tuzatish» (raqami bilan);\n"
           "• ko'p xato — «↩️ Izoh bilan qaytarish».")
    for tg in to:
        try:
            await bot.send_document(tg, BufferedInputFile(pdf, f"test_{tid}_v{t.version}.pdf"), caption=cap,
                                    reply_markup=_review_kb(tid))
        except Exception:
            await send(bot, tg, cap, reply_markup=_review_kb(tid))


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
        txt = TF.render(t.meta or {}, [TF.Q(q.qid, q.variant, q.level, q.source or "", q.text, q.options, q.answer)
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
        roster = await S.group_roster(s, g.id, day)
        qs = (await s.execute(select(TestQuestion).where(TestQuestion.test_id == tid))).scalars().all()
        by_id = {q.qid: q for q in qs}
        variants = P.assign_variants(len(roster), max(1, t.n_variants))
        booklets, sheets_ = [], []
        for i, (e, v) in enumerate(zip(roster, variants), 1):
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
                qtexts.append((q.text, [q.options[j] for j in perm]))
            booklets.append(SH.BookletItem(e.student.full_name, g.name, f"{day:%d.%m.%Y}", code, v, title, qtexts))
            sheets_.append(SH.SheetInfo(paper.id, e.student.full_name, g.name, f"{day:%d.%m.%Y}", code, v,
                                        len(pp.qids), title))
        if ex:
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
    cap = (f"🖨 <b>Chop etish: {g.name}</b> · nazorat {day:%d.%m}\n{len(booklets)} ta o'quvchi · "
           f"{t.n_questions} savol\n1) Savollar varaqlari  2) Javob varaqlari (skaner uchun — 100% masshtabda, "
           "bir tomonlama chop eting). Chop etib, o'qituvchiga bering.")
    for a in admins:
        try:
            await bot.send_document(a, BufferedInputFile(b_pdf, f"{g.name}_{day:%d.%m}_savollar.pdf"))
            await bot.send_document(a, BufferedInputFile(a_pdf, f"{g.name}_{day:%d.%m}_javob_varaqlari.pdf"),
                                    caption=cap, reply_markup=IM(inline_keyboard=[[IB(
                                        text="✅ Chop etildi", callback_data=f"tpr:{tid}")]]))
        except Exception:
            pass
    if report_to:
        await send(bot, report_to, f"🖨 {len(booklets)} ta o'quvchi uchun varaqlar adminga chop etishga yuborildi.")


@router.callback_query(F.data.startswith("tpr:"))
async def printed(c: CallbackQuery, bot: Bot) -> None:
    tid = int(c.data.split(":")[1])
    async with Session() as s:
        t = await s.get(Test, tid)
        t.printed_at = now().replace(tzinfo=None)
        g = await s.get(Group, t.group_id)
        ex = await s.get(Exam, t.exam_id) if t.exam_id else None
        cond = await s.get(Staff, ex.conductor_id) if ex and ex.conductor_id else None
        await s.commit()
    await c.answer("Belgilandi")
    await c.message.edit_reply_markup(reply_markup=None)
    if cond and cond.tg_id:
        await send(bot, cond.tg_id, f"🖨 «{g.name}» nazorati uchun varaqlar chop etildi — admindan olib keting.")


# ================= 5. Natijalar: skaner va qo'lda =================
async def entry_menu(tid: int) -> tuple[str, IM]:
    async with Session() as s:
        ps = (await s.execute(select(TestPaper).where(TestPaper.test_id == tid))).scalars().all()
    done = sum(1 for p in ps if p.answers or p.absent)
    text = (f"📝 Test #{tid} natijalari: kiritildi {done} / {len(ps)}\n\n"
            "📷 <b>Skaner</b>: javob varaqlarini rasmga olib ketma-ket yuboring — bot QR koddan o'quvchini "
            "taniydi va o'zi tekshiradi.\n⌨️ <b>Qo'lda</b>: o'quvchini tanlab, javoblarini harflar bilan yozasiz.")
    kb = [[IB(text="📷 Varaqlarni skaner qilish", callback_data=f"tsc:{tid}")],
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
    d = await state.get_data()
    fid = m.photo[-1].file_id if m.photo else m.document.file_id
    data = (await bot.download(fid, destination=io.BytesIO())).getvalue()
    async with Session() as s:
        ps = {p.id: p for p in (await s.execute(select(TestPaper).where(TestPaper.test_id == d["tid"]))).scalars()}
    res = read_sheet(data, lambda pid: len(ps[pid].qids) if pid in ps else None)
    if res.error:
        await m.reply("⚠️ " + res.error)
        return
    await _save_answers(m, res.paper_id, res.answers, scanned=True, unsure=res.unsure)


async def _save_answers(m: Message, paper_id: int, answers: str, scanned: bool, unsure: list[int] | None = None) -> None:
    async with Session() as s:
        p = await s.get(TestPaper, paper_id)
        st = await _staff(s, m.from_user.id)
        p.answers, p.absent, p.scanned, p.entered_by = answers, False, scanned, st.id if st else None
        p.correct = P.score(answers, p.key)
        enr = await s.get(Enrollment, p.enrollment_id)
        stu = await s.get(Student, enr.student_id)
        await s.commit()
        name, n, correct = stu.full_name, len(p.key), p.correct
    text = f"✅ {name}: <b>{correct} / {n}</b> ({correct / n * 100:.0f}%)"
    kb = None
    if unsure:
        text += (f"\n⚠️ Aniqlanmagan savollar: {', '.join(map(str, unsure))} (bo'sh, ikki marta yoki yarim belgilangan) "
                 "— varaqqa qarab tekshiring.")
        kb = IM(inline_keyboard=[[IB(text="✏️ Javoblarni tuzatish", callback_data=f"tpe:{paper_id}")]])
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
        rows = (await s.execute(select(TestPaper, Student.full_name).join(Enrollment, Enrollment.id == TestPaper.enrollment_id)
                                .join(Student, Student.id == Enrollment.student_id).where(TestPaper.test_id == tid)
                                .order_by(TestPaper.code))).all()
    kb = [[IB(text=("✅ " if p.answers else "➖ " if p.absent else "▫️ ") + f"{n} ({p.code})" +
                  (f" — {p.correct}" if p.answers else ""), callback_data=f"tpe:{p.id}")] for p, n in rows]
    kb.append([IB(text="⬅️ Orqaga", callback_data=f"tme:{tid}")])
    await c.message.answer("O'quvchini tanlang:", reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("tpe:"))
async def manual_one(c: CallbackQuery, state: FSMContext) -> None:
    pid = int(c.data.split(":")[1])
    async with Session() as s:
        p = await s.get(TestPaper, pid)
        enr = await s.get(Enrollment, p.enrollment_id)
        stu = await s.get(Student, enr.student_id)
    await state.set_state(Sc.manual)
    await state.update_data(pid=pid, tid=p.test_id)
    cur = f"\nHozirgi: <code>{p.answers}</code>" if p.answers else ""
    await c.answer()
    await c.message.answer(f"<b>{stu.full_name}</b> ({p.code}, {p.variant}-variant){cur}\n\n"
                           f"Javob varag'idagi {len(p.key)} ta javobni tartib bilan yozing, masalan:\n"
                           f"<code>{('ABCD' * 20)[:len(p.key)]}</code>\nBo'sh qoldirilgan savol: <b>-</b>",
                           reply_markup=IM(inline_keyboard=[[IB(text="➖ Qatnashmadi", callback_data=f"tab:{pid}")]]))


@router.message(Sc.manual)
async def manual_save(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    async with Session() as s:
        p = await s.get(TestPaper, d["pid"])
        n = len(p.key)
    ans = P.clean_answers(m.text or "", n)
    if not ans:
        got = len("".join(ch for ch in (m.text or "").upper() if ch in "ABCD-0"))
        await m.answer(f"{n} ta javob kerak, siz {got} ta yozdingiz. Qaytadan yozing:")
        return
    await state.clear()
    await _save_answers(m, d["pid"], ans, scanned=False)
    text, kb = await entry_menu(d["tid"])
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("tab:"))
async def manual_absent(c: CallbackQuery, state: FSMContext) -> None:
    pid = int(c.data.split(":")[1])
    async with Session() as s:
        p = await s.get(TestPaper, pid)
        p.absent, p.answers, p.correct = True, None, None
        await s.commit()
        tid = p.test_id
    await state.clear()
    await c.answer("Qatnashmadi deb belgilandi")
    text, kb = await entry_menu(tid)
    await c.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("tfin:"))
async def finish(c: CallbackQuery, bot: Bot) -> None:
    tid = int(c.data.split(":")[1])
    force = c.data.endswith(":ok")
    async with Session() as s:
        t = await s.get(Test, tid)
        ps = (await s.execute(select(TestPaper).where(TestPaper.test_id == tid))).scalars().all()
        left = [p for p in ps if not p.answers and not p.absent]
        if left and not force:
            await c.message.answer(f"⚠️ {len(left)} ta o'quvchining javobi kiritilmagan. Ular «qatnashmadi» deb "
                                   "hisoblanadi. Davom etamizmi?",
                                   reply_markup=IM(inline_keyboard=[[IB(text="✅ Ha, yakunlash", callback_data=f"tfin:{tid}:ok"),
                                                                     IB(text="⬅️ Orqaga", callback_data=f"tme:{tid}")]]))
            await c.answer()
            return
        if not t.exam_id:
            await c.answer("Bu test nazoratga bog'lanmagan.", show_alert=True)
            return
        st = await _staff(s, c.from_user.id)
        ex = await s.get(Exam, t.exam_id)
        g = await s.get(Group, ex.group_id)
        rules = await S.get_rules(s)
        from app.bot.exams import _apply_coins, APPEAL_HOURS
        ex.kind, ex.max_score, ex.status, ex.done_at = "test", float(t.n_questions), "otdi", now().replace(tzinfo=None)
        msgs = []
        by_qid = {q.qid: q for q in (await s.execute(select(TestQuestion).where(TestQuestion.test_id == tid))).scalars()}
        for p in ps:
            r = (await s.execute(select(ExamResult).where(ExamResult.exam_id == ex.id,
                                                          ExamResult.enrollment_id == p.enrollment_id))).scalar_one_or_none()
            if not r:
                r = ExamResult(exam_id=ex.id, enrollment_id=p.enrollment_id)
                s.add(r)
            n = len(p.key)
            r.score = float(p.correct) if p.answers else None
            r.percent = round(p.correct / n * 100, 1) if p.answers else None
            r.answers = p.answers
            await s.flush()
            enr = await s.get(Enrollment, p.enrollment_id)
            stu = await s.get(Student, enr.student_id)
            await _apply_coins(s, r, stu, rules, st, ex)
            if not p.answers:
                continue
            wrong = [f"{i}: siz {a if a in 'ABCD' else '—'}, to'g'ri {k}" for i, (a, k) in enumerate(zip(p.answers, p.key), 1)
                     if a != k]
            text = (f"📝 <b>Nazorat natijasi</b> · {g.name} · {ex.date:%d.%m}" + (" · oylik" if ex.monthly else "") +
                    f"\n{stu.full_name}: <b>{p.correct} / {n}</b> ({r.percent:.0f}%)"
                    + (f"\n🎉 +{r.coins_given} coin" if r.coins_given else "")
                    + ("\n\nXato javoblar:\n" + "\n".join(wrong[:40]) if wrong else "\n\nHammasi to'g'ri! 👏"))
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
        if not ex.test_id:
            return None
        p = (await s.execute(select(TestPaper).where(TestPaper.test_id == ex.test_id,
                                                     TestPaper.enrollment_id == r.enrollment_id))).scalar_one_or_none()
        if not p or not p.answers or not 1 <= qno <= len(p.qids):
            return None
        q = (await s.execute(select(TestQuestion).where(TestQuestion.test_id == ex.test_id,
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
    if not rows:
        await c.message.answer("Hozircha test yo'q.")
        return
    lines = [f"#{t.id} · {gname} · {t.topic[:40]} — {ST.get(t.status, t.status)}"
             + (" · 🖨 chop etildi" if t.printed_at else "") for t, gname in rows]
    kb = [[IB(text=f"📎 #{t.id} faylini yuklash", callback_data=f"tup:{t.id}")]
          for t, _ in rows if _is_boss(st) and t.status in ("sorov", "qaytarildi")]
    await c.message.answer("📋 <b>Testlar</b>\n" + "\n".join(lines), reply_markup=IM(inline_keyboard=kb) if kb else None)
