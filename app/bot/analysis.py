"""📊 Savollar va mavzular tahlili: guruh nazorati (bot testlari) va MOC uchun.

• Yakunlashda ustoz va rahbarga — «Savollar tahlili» kartasi (savollar eng ko'p ishlanganidan eng kamigacha,
  mavzular, qiyinlik, e'tibor talab savollar, kalit xatosi shubhasi).
• Savollarda mavzu bo'lsa (oylik nazorat, MOC «mavzu:» qatori) — har bir o'quvchi/ota-onaga shaxsiy
  «Mavzular tahlili» kartasi: kuchli mavzular, takrorlash kerak bo'lganlar, guruh o'rtachasi, oldingi natijaga nisbatan.
• Natijalar oynasidan istalgan payt qayta ko'rish mumkin.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM
from sqlalchemy import select

from app import services as S
from app.db.models import (Enrollment, Exam, ExamResult, Group, Moc, MocSeat, Staff, Student, Test, TestPaper,
                           TestQuestion)
from app.db.session import Session
from app.logic import analysis_card as AC
from app.logic import itemstats as I

router = Router()
L4 = "ABCD"


# ======================= guruh nazorati (bot testlari) =======================
async def test_report(s, exam_id: int) -> tuple[I.Report | None, dict]:
    """Nazoratdagi barcha bot testlari varaqlaridan tahlil. Qaytaradi: (hisobot, {enrollment_id: paper})."""
    tests = list((await s.execute(select(Test).where(Test.exam_id == exam_id))).scalars())
    if not tests:
        return None, {}
    meta: dict[str, I.QMeta] = {}
    resps, papers = [], {}
    for t in tests:
        qs = {q.qid: q for q in (await s.execute(select(TestQuestion).where(TestQuestion.test_id == t.id))).scalars()}
        one = t.n_variants == 1
        for q in qs.values():
            meta[f"{t.id}:{q.qid}"] = I.QMeta(str(q.qid) if one and len(tests) == 1 else f"#{q.qid}",
                                              q.topic or "", "orta" if q.level in ("orta", "o'rta") else q.level,
                                              q.answer, q.text[:120])
        for p in (await s.execute(select(TestPaper).where(TestPaper.test_id == t.id))).scalars():
            if not p.answers or p.absent:
                continue
            items = []
            for i, qid in enumerate(p.qids):
                a = p.answers[i] if i < len(p.answers) else "-"
                ok = 1.0 if a == p.key[i] else 0.0
                orig = None
                if a in L4 and i < len(p.perms or []):
                    try:
                        orig = L4[p.perms[i][L4.index(a)]]
                    except (IndexError, ValueError):
                        orig = None
                items.append((f"{t.id}:{qid}", ok, orig))
            enr = await s.get(Enrollment, p.enrollment_id)
            stu = await s.get(Student, enr.student_id) if enr else None
            resps.append(I.Resp(p.enrollment_id, stu.full_name if stu else "?", items))
            papers[p.enrollment_id] = p
    if not resps:
        return None, {}
    return I.analyze(resps, meta), papers


async def _prev_topics(s, enrollment_id: int, before_exam: Exam) -> dict[str, float] | None:
    rows = (await s.execute(select(ExamResult, Exam).join(Exam, Exam.id == ExamResult.exam_id)
                            .where(ExamResult.enrollment_id == enrollment_id, ExamResult.topics.is_not(None),
                                   Exam.id != before_exam.id, Exam.date <= before_exam.date)
                            .order_by(Exam.date.desc()).limit(1))).first()
    if not rows:
        return None
    return {t: 100 * ok / n for t, (ok, n) in (rows[0].topics or {}).items() if n}


async def after_test_finish(bot: Bot, exam_id: int, to_staff: list[int]) -> None:
    """tests_flow.finish dan keyin: natijalarga mavzu statistikasini yozadi, kartalarni yuboradi."""
    async with Session() as s:
        ex = await s.get(Exam, exam_id)
        rep, _ = await test_report(s, exam_id)
        if not rep:
            return
        g = await s.get(Group, ex.group_id)
        personal = []
        gavg = {t.topic: t.pct for t in rep.topics}
        for eid, st in rep.students.items():
            r = (await s.execute(select(ExamResult).where(ExamResult.exam_id == exam_id,
                                                          ExamResult.enrollment_id == eid))).scalar_one_or_none()
            if r and rep.has_topics:
                prev = await _prev_topics(s, eid, ex)
                r.topics = st["topics"]
                enr = await s.get(Enrollment, eid)
                tgs = await S.parent_tg_ids(s, enr.student_id)
                if tgs:
                    personal.append((tgs, st, prev))
        await s.commit()
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    title = f"{g.name} · {ex.date:%d.%m.%Y}" + (" · oylik nazorat" if ex.monthly else "")
    png = AC.audience(rep, title, f"{g.name} · {rep.n} qatnashchi", f"{ex.date:%d.%m.%Y}")
    cap = _aud_caption(rep, title)
    for t in dict.fromkeys(to_staff + bosses):
        await _photo(bot, t, png, cap)
    for tgs, st, prev in personal:
        card = AC.personal(st["name"], f"{'Oylik nazorat' if ex.monthly else 'Nazorat'} · {g.name}",
                           f"{ex.date:%d.%m.%Y}", st["pct"], st["topics"], gavg, rep.mean, prev)
        cap2 = _personal_caption(st)
        for t in tgs:
            await _photo(bot, t, card, cap2)


def _aud_caption(rep: I.Report, title: str) -> str:
    lines = [f"📊 <b>Savollar tahlili</b> · {title}", f"O'rtacha: <b>{rep.mean:g}%</b> · qatnashchi: {rep.n}"]
    if rep.has_topics:
        rel = [t for t in rep.topics if t.reliable and t.topic != I.OTHER] or rep.topics
        lines.append(f"💪 Eng kuchli mavzu: {rel[0].topic} ({round(rel[0].pct)}%)")
        lines.append(f"📚 Eng zaif mavzu: {rel[-1].topic} ({round(rel[-1].pct)}%)")
    sus = [x.label for x in rep.items if x.suspicious]
    if sus:
        lines.append(f"⚠️ Kalitni tekshiring: {', '.join(sus[:8])} — kuchli o'quvchilar bu savollarda "
                     "kuchsizlardan ko'p xato qilgan.")
    return "\n".join(lines)


def _personal_caption(st: dict) -> str:
    strong, weak = I.strong_weak(st["topics"])
    out = [f"📊 <b>{st['name']}</b> — mavzular bo'yicha natija: <b>{st['pct']:g}%</b>"]
    if strong:
        out.append("💪 Kuchli: " + ", ".join(strong))
    if weak:
        out.append("📚 Takrorlash kerak: " + ", ".join(weak))
    elif st["pct"] >= 50:
        out.append("👏 Zaif mavzu yo'q — barakalla!")
    return "\n".join(out)


async def _photo(bot: Bot, tg: int, png: bytes, cap: str) -> None:
    try:
        await bot.send_photo(tg, BufferedInputFile(png, "tahlil.png"), caption=cap[:1024])
    except Exception:
        pass


# ======================= MOC =======================
def moc_report(moc: Moc, sp, seats: list[MocSeat]) -> I.Report | None:
    """MOC: varaqdagi har bir savol (test va yozma) bo'yicha. Variant ko'p bo'lsa — savol «A-12» kabi."""
    keys = moc.keys or {}
    multi = len([v for v in keys if keys[v]]) > 1
    kinds = sp.bubbles()
    meta, resps = {}, []
    for z in seats:
        r = z.result or {}
        pq = r.get("pq")
        if z.absent or not pq:
            continue
        v = z.variant or "A"
        key = keys.get(v) or ""
        items = []
        for i, ok in enumerate(pq):
            q = i + 1
            k = f"{v}:{q}" if multi else str(q)
            if k not in meta:
                kind = kinds[i][0] if i < len(kinds) else "t"
                meta[k] = I.QMeta(f"{v}-{q}" if multi else str(q), sp.topics.get(q, ""), "",
                                  key[i:i + 1] if kind == "t" else "")
            a = (z.answers or "")[i:i + 1] or "-"
            items.append((k, float(ok), a if a in "ABCDEF" else None))
        resps.append(I.Resp(z.id, z.name or "?", items))
    return I.analyze(resps, meta) if resps else None


async def after_moc_finish(bot: Bot, moc: Moc, sp, rep: I.Report, targets: list, staff_tg: int | None) -> None:
    async with Session() as s:
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    title = f"{moc.title} · {moc.date:%d.%m.%Y}"
    png = AC.audience(rep, moc.title, f"{rep.n} qatnashchi", f"{moc.date:%d.%m.%Y}")
    cap = _aud_caption(rep, title)
    for t in dict.fromkeys(([staff_tg] if staff_tg else []) + bosses):
        await _photo(bot, t, png, cap)
    if not sp.topics:
        return
    gavg = {t.topic: t.pct for t in rep.topics}
    for z, tgs in targets:
        st = rep.students.get(z.id)
        if not st or not tgs:
            continue
        card = AC.personal(st["name"], moc.title, f"{moc.date:%d.%m.%Y}", st["pct"], st["topics"], gavg, rep.mean)
        for t in tgs:
            await _photo(bot, t, card, _personal_caption(st))


# ======================= qayta ko'rish tugmalari =======================
async def _staff(s, tg: int) -> Staff | None:
    who = await S.who_by_tg(s, tg)
    return who.staff


@router.callback_query(F.data.startswith("anx:"))
async def show_exam(c: CallbackQuery) -> None:
    eid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        ex = await s.get(Exam, eid)
        g = await s.get(Group, ex.group_id) if ex else None
        if not st or not g:
            return await c.answer()
        role = st.current_role
        if role not in ("rahbar", "admin") and st.id not in (g.teacher_id, g.assistant_id):
            return await c.answer("Faqat shu guruh ustozi va rahbar.", show_alert=True)
        rep, _ = await test_report(s, eid)
    await c.answer()
    if not rep:
        return await c.message.answer("Bu nazorat bot testi bilan o'tkazilmagan — savollar bo'yicha ma'lumot yo'q.\n"
                                      "Savollar tahlili javob varag'i (skaner) bilan o'tgan nazoratlarda chiqadi.")
    title = f"{g.name} · {ex.date:%d.%m.%Y}" + (" · oylik nazorat" if ex.monthly else "")
    await c.message.answer_photo(BufferedInputFile(AC.audience(rep, title, f"{g.name} · {rep.n} qatnashchi",
                                                               f"{ex.date:%d.%m.%Y}"), "tahlil.png"),
                                 caption=_aud_caption(rep, title)[:1024])


@router.callback_query(F.data.startswith("anm:"))
async def show_moc(c: CallbackQuery) -> None:
    from app.bot import moc as MB
    mid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await MB._staff(s, c.from_user.id)
        if not st or MB._role(st) not in MB.VIEWERS:
            return await c.answer()
        moc = await s.get(Moc, mid)
        seats = await MB._seats(s, mid)
    await c.answer()
    rep = moc_report(moc, MB._spec(moc), seats) if moc else None
    if not rep:
        return await c.message.answer("Tahlil uchun javoblar yo'q (imtihon hali yakunlanmagan yoki tashqi tekshiruv).")
    await c.message.answer_photo(BufferedInputFile(AC.audience(rep, moc.title, f"{rep.n} qatnashchi",
                                                               f"{moc.date:%d.%m.%Y}"), "tahlil.png"),
                                 caption=_aud_caption(rep, moc.title)[:1024])


def exam_button(eid: int) -> list[IB]:
    return [IB(text="📊 Savollar tahlili", callback_data=f"anx:{eid}")]
