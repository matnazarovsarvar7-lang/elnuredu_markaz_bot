"""🏅 Sertifikatlar.

Kategoriyalar (har biri va har o'rin — alohida rang, muhr va maqtov so'zlari):
• group_place — guruh nazorati: 1–3-o'rin (apellyatsiya muddati — 48 soat — tugagach beriladi)
• perfect     — nazoratda 100% natija («Mukammal natija»)
• month_place — oylik nazoratlar: butun markaz bo'yicha 1–3-o'rin (rahbar tasdiqlagach)
• moc_level   — MOC daraja (A+, A … / C1, B2, B1 / IELTS band)
• moc_place   — MOC: 1–3-o'rin
• star/growth — reyting: oy yulduzi va eng ko'p o'sgan o'quvchi
Teng natija — teng o'rin («zich» tartib: 1, 1, 2, 3).
Har sertifikatda raqam va QR bor: skanerlansa bot haqiqiyligini tasdiqlaydi.
"""
from __future__ import annotations

import secrets
import zlib
from datetime import date, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import select

from app import services as S
from app.bot import keyboards as K
from app.bot.notify import send
from app.db.models import Certificate, Enrollment, Exam, ExamResult, Group, Setting, Staff, Student
from app.db.session import Session
from app.logic import certificate as C

router = Router()
CFG_KEY = "cert_cfg"
DEFAULT = {"group": True, "perfect": True, "monthly": True, "moc_level": True, "moc_place": True, "star": True,
           "min_pct": 60}
CFG_NAMES = {"group": "Guruh nazorati: 1–3-o'rin", "perfect": "100% natija", "monthly": "Oylik: markaz bo'yicha 1–3-o'rin",
             "moc_level": "MOC: daraja (A+, B2 …)", "moc_place": "MOC: 1–3-o'rin", "star": "Reyting: oy yulduzi"}
MONTHS = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"]
TONE = {1: "gold", 2: "silver", 3: "bronze"}

PRAISE = {
    ("group_place", 1): ["Bilimingiz va mehnatingiz eng yuksak cho'qqida! Shu shijoat bilan yangi marralar sari!",
                         "Guruhingizning eng kuchlisi — siz! Bu g'alaba sabr va mehnatning mevasidir.",
                         "Birinchilik — tasodif emas, har kungi mehnat natijasi. Tabriklaymiz!"],
    ("group_place", 2): ["Ajoyib natija! G'alaba sari atigi bir qadam qoldi — siz bunga albatta erishasiz.",
                         "Yuqori natija va katta salohiyat! Keyingi nazoratda cho'qqi sizniki bo'lsin.",
                         "Mehnatingiz ko'rinib turibdi — shu yo'lda davom eting, oldinda katta marralar!"],
    ("group_place", 3): ["Munosib natija! Har bir nazorat sizni yangi marralarga yaqinlashtirmoqda.",
                         "Sovrinli o'rin — mehnatingizning yorqin isboti. Oldinga, yanada yuqoriga!",
                         "Siz to'g'ri yo'ldasiz! Izchil mehnat albatta yuqori cho'qqilarga olib chiqadi."],
    ("month_place", 1): ["Siz — markazimizning faxri va barchaga ibratsiz! Bu g'alaba katta orzularingiz sari dadil qadamdir.",
                         "Butun markaz bo'yicha eng yuqori natija — bu chinakam iste'dod va mehnat timsoli. Tabriklaymiz!"],
    ("month_place", 2): ["Markaz bo'yicha sovrinli o'rin — ulkan yutuq! Bilimingiz va qat'iyatingiz bilan faxrlanamiz.",
                         "Yuzlab o'quvchilar orasida eng yaxshilardansiz! Navbatdagi cho'qqi ham sizni kutmoqda."],
    ("month_place", 3): ["Markazning eng kuchli o'quvchilari qatoridasiz! Bu natija — katta kelajakning boshlanishi.",
                         "Butun markaz bo'yicha sovrinli o'rin — mehnatingizga munosib baho. Oldinga!"],
    ("moc_place", 1): ["Mock imtihonning eng yuqori natijasi! Haqiqiy imtihonda ham shunday g'alaba tilaymiz!",
                       "Raqobat kuchli edi, lekin siz eng yaxshisi bo'ldingiz. Orzuingizdagi oliygoh sizni kutmoqda!"],
    ("moc_place", 2): ["Ajoyib natija! Har bir imtihon sizni orzuingizdagi oliygohga yanada yaqinlashtirmoqda.",
                       "Sovrinli o'rin — tayyorgarligingiz kuchli ekanining isboti. Shu sur'atda davom eting!"],
    ("moc_place", 3): ["Munosib natija! Har bir imtihon sizni orzuingizdagi oliygohga yanada yaqinlashtirmoqda.",
                       "Sovrinli uchlikdasiz! Bir oz harakat — va cho'qqi sizniki bo'ladi."],
    ("perfect", 0): ["Bitta ham xatosiz! Mukammallik — bu odat, va siz uni isbotladingiz.",
                     "100% — bu nafaqat bilim, balki diqqat va mas'uliyat namunasidir. Barakalla!"],
    ("star", 0): ["Har kungi mehnat — eng ishonchli g'alaba. Siz bunga yorqin misolsiz!",
                  "Nazoratlarda ham, uy vazifalarida ham barqaror — haqiqiy yulduzlar shunday bo'ladi!"],
    ("growth", 0): ["Eng katta o'sish — eng katta g'alaba! Mehnatingiz o'z samarasini bermoqda.",
                    "Bir oyda shunday o'sish — kuchli iroda va qat'iyat natijasi. Oldinga!"],
    ("moc_level", "top"): ["Eng yuqori daraja — bu tinimsiz mehnat va yuksak bilim mahsuli. Rasmiy imtihonda ham shunday g'alaba tilaymiz!",
                           "Yuqori daraja — orzularingiz sari ishonchli qadam. Siz bilan faxrlanamiz!"],
    ("moc_level", "mid"): ["Mustahkam natija! Yana bir oz harakat — va eng yuqori daraja sizniki.",
                           "Bilimingiz yaxshi poydevorga ega. Rasmiy imtihonda yanada yuqori natija tilaymiz!"],
    ("moc_level", "base"): ["Sertifikat darajasiga erishdingiz — bu birinchi va eng muhim marra. Oldinga!",
                            "Yaxshi boshlanish! Har bir mock sizni yuqori darajaga yaqinlashtiradi."],
}


def _pick(key, name: str) -> str:
    opts = PRAISE.get(key) or PRAISE.get((key[0], 0)) or [""]
    return opts[zlib.crc32(name.encode()) % len(opts)]


def uz_date(d: date) -> str:
    return f"{d.day}-{MONTHS[d.month - 1]}, {d.year}-yil"


def dense(scores: dict) -> dict:
    """Teng natija — teng o'rin: {kalit: ball} → {kalit: o'rin} (1, 1, 2, 3 …)."""
    vals = sorted({round(v, 1) for v in scores.values()}, reverse=True)
    pos = {v: i + 1 for i, v in enumerate(vals)}
    return {k: pos[round(v, 1)] for k, v in scores.items()}


def level_tone(level: str) -> tuple[str, str]:
    lv = (level or "").upper().replace("BAND ", "")
    if lv in ("A+", "A", "C1", "C2"):
        return "gold", "top"
    if lv in ("B+", "B", "B2"):
        return "silver", "mid"
    try:
        b = float(lv)
        return ("gold", "top") if b >= 7 else ("silver", "mid") if b >= 6 else ("bronze", "base")
    except ValueError:
        return "bronze", "base"


async def cfg(s) -> dict:
    row = await s.get(Setting, CFG_KEY)
    return {**DEFAULT, **((row.value or {}) if row else {})}


async def since(s) -> datetime:
    """Sertifikatlar ishga tushgan payt — undan oldingi nazoratlarga ommaviy sertifikat chiqmaydi."""
    row = await s.get(Setting, "cert_since")
    if not row:
        row = Setting(key="cert_since", value={"at": datetime.utcnow().isoformat()})
        s.add(row)
        await s.flush()
    return datetime.fromisoformat(row.value["at"])


async def _boss_name(s) -> str:
    st = await S.staff_with_role(s, "rahbar")
    return st[0].full_name if st else ""


async def issue(s, kind: str, ref: str, who: str, name: str, data: dict, student_id: int | None = None,
                seat_id: int | None = None, place: int | None = None, level: str | None = None) -> Certificate | None:
    if (await s.execute(select(Certificate.id).where(Certificate.kind == kind, Certificate.ref == ref,
                                                     Certificate.who == who))).first():
        return None
    c = Certificate(kind=kind, ref=ref, who=who, name=name, data=data, student_id=student_id, seat_id=seat_id,
                    place=place, level=level, token=secrets.token_hex(4))
    s.add(c)
    await s.flush()
    return c


def _cert_obj(c: Certificate, uname: str) -> C.Cert:
    d = c.data or {}
    return C.Cert(kind=c.kind, name=c.name, category=d.get("category", ""), body=d.get("body", ""),
                  praise=d.get("praise", ""), tone=d.get("tone", "gold"), emblem_big=d.get("emblem_big", ""),
                  emblem_small=d.get("emblem_small", ""), score_big=d.get("score_big", ""),
                  score_small=d.get("score_small", "natija"), date_txt=d.get("date", ""),
                  number=f"ELN-{c.created_at.year if c.created_at else date.today().year}-{c.id:05d}",
                  signers=[tuple(x) for x in d.get("signers", [])],
                  verify_url=f"https://t.me/{uname}?start=c{c.id}_{c.token}" if uname else "")


async def deliver(bot: Bot, ids: list[int], title: str, notify_staff: list[int]) -> int:
    """Sertifikatlarni chizadi, egasiga (ota-ona / o'quvchi / qatnashchi) PNG + PDF yuboradi; rahbarga umumiy PDF."""
    if not ids:
        return 0
    uname = (await bot.get_me()).username or ""
    images, sent = [], 0
    async with Session() as s:
        certs = sorted([await s.get(Certificate, i) for i in ids], key=lambda c: (c.place or 9, c.kind, c.name))
        targets = []
        for c in certs:
            tgs = []
            if c.student_id:
                tgs = await S.parent_tg_ids(s, c.student_id)
            elif c.seat_id:
                from app.db.models import MocSeat
                z = await s.get(MocSeat, c.seat_id)
                tgs = [z.tg_id] if z and z.tg_id else []
            elif (c.data or {}).get("tg"):                   # olimpiada ustozi
                tgs = [c.data["tg"]]
            targets.append((c, tgs))
    for c, tgs in targets:
        im = C.render(_cert_obj(c, uname))
        images.append(im)
        png, pdf = C.to_png(im), C.to_pdf([im])
        fname = f"Sertifikat_{c.name.replace(' ', '_')}.pdf"
        cap = (f"🏅 <b>Tabriklaymiz!</b>\n{c.name} — {C.uz((c.data or {}).get('category', ''))}.\n"
               "Chop etish uchun sertifikatning PDF nusxasi ham yuborildi.")
        fid = None
        for t in tgs:
            try:
                await bot.send_photo(t, BufferedInputFile(png, "sertifikat.png"), caption=cap)
                msg = await bot.send_document(t, BufferedInputFile(pdf, fname))
                fid = fid or msg.document.file_id
                sent += 1
            except Exception:
                pass
        async with Session() as s:
            cc = await s.get(Certificate, c.id)
            cc.sent, cc.file_id = bool(fid) or cc.sent, fid or cc.file_id
            await s.commit()
    staff = list(dict.fromkeys(notify_staff))
    if staff and images:
        allpdf = C.to_pdf(images)
        lines = "\n".join(f"• {c.name} — {C.uz((c.data or {}).get('category', ''))}" for c, _ in targets)
        for i, t in enumerate(staff):
            try:
                msg = await bot.send_document(
                    t, BufferedInputFile(allpdf, f"Sertifikatlar_{date.today():%d.%m}.pdf"),
                    caption=f"🏅 <b>{title}</b> — {len(images)} ta sertifikat\n{lines}"[:1024])
                if i == 0:
                    async with Session() as s:
                        key = f"certb_{secrets.token_hex(4)}"
                        s.add(Setting(key=key, value={"file_id": msg.document.file_id, "n": len(images)}))
                        await s.commit()
                    await bot.send_message(t, "Rangli printerda chop etish uchun adminga yuboraymi?",
                                           reply_markup=IM(inline_keyboard=[[IB(text="🖨 Chop etishga yuborish",
                                                                                callback_data=f"ctp:{key[6:]}")]]))
            except Exception:
                pass
    return sent


@router.callback_query(F.data.startswith("ctp:"))
async def to_print(c: CallbackQuery, bot: Bot) -> None:
    from app.bot import prints as PR
    from app.db.models import PrintJob
    key = "certb_" + c.data.split(":")[1]
    async with Session() as s:
        st = (await S.who_by_tg(s, c.from_user.id)).staff
        row = await s.get(Setting, key)
        if not st or not row:
            return await c.answer("Topilmadi.", show_alert=True)
        n = int(row.value.get("n") or 1)
        job = PrintJob(kind="ichki", staff_id=st.id, copies=1, pages=n, layout="bir", sheets=n,
                       file_id=row.value["file_id"], file_kind="pdf",
                       note=f"🏅 Sertifikatlar ({n} ta) — RANGLI printerda, qalin qog'ozga")
        s.add(job)
        await s.delete(row)
        await s.commit()
        cap, kb = await PR._job_card(s, job)
        to = await PR._printers(s)
    for t in to:
        await PR._send_file(bot, t, job.file_id, job.file_kind, cap, kb)
    await c.answer("Yuborildi")
    await c.message.edit_text("🖨 Sertifikatlar adminga chop etishga yuborildi.")


# ======================= guruh nazorati =======================
async def exam_job(bot: Bot) -> None:
    """Har soat: apellyatsiya muddati (48 soat) tugagan nazoratlar bo'yicha sertifikatlar."""
    from app.bot.exams import APPEAL_HOURS
    edge = datetime.utcnow() + timedelta(hours=5) - timedelta(hours=APPEAL_HOURS)
    async with Session() as s:
        start = await since(s)
        await s.commit()
        exs = (await s.execute(select(Exam).where(Exam.status == "otdi", Exam.done_at.is_not(None),
                                                  Exam.done_at <= edge, Exam.cert_done.is_not(True)))).scalars().all()
        todo = []
        for ex in exs:
            ex.cert_done = True
            if ex.done_at - timedelta(hours=5) >= start:
                todo.append(ex.id)
        await s.commit()
    for eid in todo:
        await exam_certs(bot, eid)


async def exam_certs(bot: Bot, eid: int) -> list[int]:
    async with Session() as s:
        conf = await cfg(s)
        ex = await s.get(Exam, eid)
        g = await s.get(Group, ex.group_id)
        rows = (await s.execute(select(ExamResult).where(ExamResult.exam_id == eid,
                                                         ExamResult.percent.is_not(None)))).scalars().all()
        if not rows:
            return []
        pct = {r.enrollment_id: r.percent for r in rows}
        place = dense(pct)
        teacher = await s.get(Staff, g.teacher_id) if g.teacher_id else None
        signers = [(await _boss_name(s), "Rahbar")] + ([(teacher.full_name, "Ustoz")] if teacher else [])
        made = []
        what = "oylik nazoratda" if ex.monthly else "nazoratda"
        for r in rows:
            enr = await s.get(Enrollment, r.enrollment_id)
            stu = await s.get(Student, enr.student_id)
            p = place[r.enrollment_id]
            common = {"date": uz_date(ex.date), "signers": signers, "score_big": f"{r.percent:g}%", "score_small": "natija"}
            if conf["group"] and p <= 3 and r.percent >= conf["min_pct"]:
                c = await issue(s, "group_place", f"exam:{eid}", f"s{stu.id}", stu.full_name, {
                    **common, "category": f"Guruh nazorati · {p}-o'rin", "tone": TONE[p], "emblem_big": str(p),
                    "emblem_small": "o'rin",
                    "body": f"«{g.name}» guruhida {uz_date(ex.date).split(',')[0]} kuni o'tkazilgan {what} "
                            f"{r.percent:g}% natija bilan {p}-o'rinni egalladi.",
                    "praise": _pick(("group_place", p), stu.full_name)}, student_id=stu.id, place=p)
                if c:
                    made.append(c.id)
            if conf["perfect"] and r.percent >= 100:
                c = await issue(s, "perfect", f"exam:{eid}", f"s{stu.id}", stu.full_name, {
                    **common, "category": "Mukammal natija · 100%", "tone": "gold", "emblem_big": "100%",
                    "emblem_small": "mukammal",
                    "body": f"«{g.name}» guruhida {uz_date(ex.date).split(',')[0]} kuni o'tkazilgan {what} barcha "
                            "savollarga to'g'ri javob berib, mukammal natija ko'rsatdi.",
                    "praise": _pick(("perfect", 0), stu.full_name)}, student_id=stu.id)
                if c:
                    made.append(c.id)
        await s.commit()
        staff = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        if teacher and teacher.tg_id:
            staff.append(teacher.tg_id)
    await deliver(bot, made, f"{g.name} · nazorat {ex.date:%d.%m}", staff)
    return made


# ======================= oylik: markaz bo'yicha =======================
async def month_winners(s, ym: str) -> list[tuple[int, int, float, list[str]]]:
    """[(o'rin, o'quvchi id, eng yaxshi foiz, guruhlar)] — oylik nazoratlar, butun markaz bo'yicha."""
    y, m = map(int, ym.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    exs = (await s.execute(select(Exam).where(Exam.monthly, Exam.status == "otdi", Exam.date >= start,
                                              Exam.date < end))).scalars().all()
    best: dict[int, float] = {}
    groups: dict[int, list[str]] = {}
    for ex in exs:
        g = await s.get(Group, ex.group_id)
        for r in (await s.execute(select(ExamResult).where(ExamResult.exam_id == ex.id,
                                                           ExamResult.percent.is_not(None)))).scalars():
            enr = await s.get(Enrollment, r.enrollment_id)
            if r.percent > best.get(enr.student_id, -1):
                best[enr.student_id] = r.percent
            groups.setdefault(enr.student_id, []).append(g.name)
    if not best:
        return []
    place = dense(best)
    return sorted([(place[sid], sid, best[sid], groups[sid]) for sid in best if place[sid] <= 3],
                  key=lambda x: (x[0], -x[2]))


def _ym_txt(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{MONTHS[m - 1]} {y}"


async def month_preview(bot: Bot, ym: str, chat: int | None = None) -> None:
    async with Session() as s:
        conf = await cfg(s)
        win = [w for w in await month_winners(s, ym) if w[2] >= conf["min_pct"]]
        names = {sid: (await s.get(Student, sid)).full_name for _, sid, _, _ in win}
        bosses = [chat] if chat else [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        done = (await s.execute(select(Certificate.id).where(Certificate.kind == "month_place",
                                                             Certificate.ref == f"month:{ym}"))).first()
    if not win:
        if chat:
            await send(bot, chat, f"🏆 {_ym_txt(ym)}: o'tkazilgan oylik nazoratlar yoki {conf['min_pct']}% dan yuqori "
                                  "natija topilmadi.")
        return
    lines = [f"🏆 <b>Oylik nazoratlar — {_ym_txt(ym)}</b>\nMarkaz bo'yicha g'oliblar (teng natija — teng o'rin):\n"]
    for p, sid, pc, gs in win:
        lines.append(f"{'🥇🥈🥉'[p - 1]} {p}-o'rin — {names[sid]} · {pc:g}% ({', '.join(dict.fromkeys(gs))})")
    if done:
        lines.append("\n✅ Bu oy uchun sertifikatlar allaqachon berilgan.")
    kb = None if done else IM(inline_keyboard=[[IB(text="✅ Tasdiqlash va sertifikat berish", callback_data=f"ctm:{ym}")]])
    for b in bosses:
        await send(bot, b, "\n".join(lines), reply_markup=kb)


async def month_job(bot: Bot) -> None:
    """Har oyning 3-kuni: o'tgan oy g'oliblari rahbarga tasdiqlash uchun (apellyatsiyalar tugagach)."""
    async with Session() as s:
        if not (await cfg(s))["monthly"]:
            return
    t = date.today().replace(day=1) - timedelta(days=1)
    await month_preview(bot, f"{t:%Y-%m}")


@router.callback_query(F.data.startswith("ctm:"))
async def month_issue(c: CallbackQuery, bot: Bot) -> None:
    ym = c.data.split(":", 1)[1]
    async with Session() as s:
        st = (await S.who_by_tg(s, c.from_user.id)).staff
        if not st or st.current_role != "rahbar":
            return await c.answer("Faqat rahbar.", show_alert=True)
        conf = await cfg(s)
        win = [w for w in await month_winners(s, ym) if w[2] >= conf["min_pct"]]
        signers = [(st.full_name, "Rahbar")]
        y, m = map(int, ym.split("-"))
        made = []
        for p, sid, pc, gs in win:
            stu = await s.get(Student, sid)
            x = await issue(s, "month_place", f"month:{ym}", f"s{sid}", stu.full_name, {
                "category": f"Oylik nazorat · markaz bo'yicha {p}-o'rin", "tone": TONE[p], "emblem_big": str(p),
                "emblem_small": "o'rin", "score_big": f"{pc:g}%", "score_small": "natija",
                "date": uz_date(date.today()), "signers": signers,
                "body": f"{MONTHS[m - 1].capitalize()} oyida o'tkazilgan oylik nazoratlarda {pc:g}% natija bilan "
                        f"butun ELNUR EDU o'quv markazi bo'yicha {p}-o'rinni egalladi.",
                "praise": _pick(("month_place", p), stu.full_name)}, student_id=sid, place=p)
            if x:
                made.append(x.id)
        await S.audit(s, st, "sertifikat_oylik", month=ym, n=len(made))
        await s.commit()
    await c.message.edit_reply_markup(reply_markup=None)
    if not made:
        return await c.answer("Bu oy uchun sertifikatlar allaqachon berilgan.", show_alert=True)
    await c.answer("Tayyorlanmoqda…")
    n = await deliver(bot, made, f"Oylik g'oliblar — {_ym_txt(ym)}", [c.from_user.id])
    await c.message.answer(f"🏅 {len(made)} ta sertifikat tayyorlandi, {n} ta manzilga yuborildi.")


# ======================= MOC =======================
async def moc_certs(bot: Bot, mid: int) -> list[int]:
    from app.db.models import Moc, MocSeat
    from app.logic import moc as M
    async with Session() as s:
        conf = await cfg(s)
        moc = await s.get(Moc, mid)
        sp = M.parse_spec(moc.spec)
        seats = [z for z in (await s.execute(select(MocSeat).where(MocSeat.moc_id == mid))).scalars() if z.result]
        if not seats:
            return []
        maker = await s.get(Staff, moc.created_by) if moc.created_by else None
        signers = [(await _boss_name(s), "Rahbar")]
        if maker and maker.current_role != "rahbar" and "rahbar" not in (maker.roles or ""):
            signers.append((maker.full_name, "Ustoz"))
        place = dense({z.id: z.result["total"] for z in seats})
        made = []
        for z in seats:
            r = z.result
            tot = M.fmt_total(sp, r).split(" / ")[0].replace("Band ", "")
            common = {"date": uz_date(moc.date), "signers": signers, "score_big": tot,
                      "score_small": "band" if sp.mode == "ielts" else "natija" if sp.mode == "foiz" else "ball"}
            who = f"s{z.student_id}" if z.student_id else f"z{z.id}"
            lvl = r.get("level")
            if sp.mode == "ielts" and r["total"] >= 5:
                lvl = f"Band {r['total']:g}"
            real = lvl and lvl not in ("Sertifikatsiz", "B1 dan past", "—")
            if conf["moc_level"] and real:
                tone, tier = level_tone(lvl)
                c = await issue(s, "moc_level", f"moc:{mid}", who, z.name, {
                    **common, "category": f"{sp.name} · {lvl}" + ("" if lvl.startswith("Band") else " daraja"),
                    "tone": tone, "emblem_big": lvl.replace("Band ", ""), "emblem_small": "band" if lvl.startswith("Band") else "daraja",
                    "body": f"{uz_date(moc.date).split(',')[0]} kuni o'tkazilgan «{moc.title}» imtihonida "
                            f"{M.fmt_total(sp, r)} natija bilan {lvl} " + ("natijasini" if lvl.startswith("Band") else "darajasini")
                            + " qo'lga kiritdi.",
                    "praise": _pick(("moc_level", tier), z.name)}, student_id=z.student_id, seat_id=z.id, level=lvl)
                if c:
                    made.append(c.id)
            p = place[z.id]
            if conf["moc_place"] and p <= 3:
                c = await issue(s, "moc_place", f"moc:{mid}", who, z.name, {
                    **common, "category": f"{'Mock imtihon' if moc.kind == 'moc' else 'Nazorat'} · {p}-o'rin",
                    "tone": TONE[p], "emblem_big": str(p), "emblem_small": "o'rin",
                    "body": f"«{moc.title}» imtihonida {M.fmt_total(sp, r)} natija bilan {len(seats)} nafar "
                            f"qatnashchi orasida {p}-o'rinni egalladi.",
                    "praise": _pick(("moc_place", p), z.name)}, student_id=z.student_id, seat_id=z.id, place=p)
                if c:
                    made.append(c.id)
        await s.commit()
        staff = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        if maker and maker.tg_id:
            staff.append(maker.tg_id)
    await deliver(bot, made, moc.title, staff)
    return made


# ======================= reyting: oy yulduzi =======================
async def star_cert(s, kind: str, ym: str, gid: int, sid: int, name: str, gname: str, score: float,
                    grow: float | None) -> int | None:
    conf = await cfg(s)
    if not conf["star"]:
        return None
    g = await s.get(Group, gid)
    teacher = await s.get(Staff, g.teacher_id) if g and g.teacher_id else None
    signers = [(await _boss_name(s), "Rahbar")] + ([(teacher.full_name, "Ustoz")] if teacher else [])
    y, m = map(int, ym.split("-"))
    if kind == "star":
        data = {"category": "Oy yulduzi", "tone": "navy", "emblem_big": "★", "emblem_small": "oy yulduzi",
                "score_big": f"{score:.0f}", "score_small": "reyting",
                "body": f"«{gname}» guruhida {MONTHS[m - 1]} oyining eng yuqori reytingi — {score:.0f} ball: "
                        "nazoratlar va uy vazifalarida eng barqaror natija.",
                "praise": _pick(("star", 0), name)}
    else:
        data = {"category": "O'sish yulduzi", "tone": "navy", "emblem_big": "★", "emblem_small": "o'sish",
                "score_big": f"+{grow:.0f}", "score_small": "ball o'sdi",
                "body": f"«{gname}» guruhida {MONTHS[m - 1]} oyida reytingini {grow:.0f} ballga oshirib, "
                        "eng katta o'sishni ko'rsatdi.",
                "praise": _pick(("growth", 0), name)}
    data.update(date=uz_date(date.today()), signers=signers)
    c = await issue(s, kind, f"star:{ym}:g{gid}", f"s{sid}", name, data, student_id=sid)
    return c.id if c else None


# ======================= tekshirish (QR) =======================
KIND_NAMES = {"group_place": "Guruh nazorati", "perfect": "Mukammal natija", "month_place": "Oylik nazorat (markaz)",
              "moc_level": "MOC daraja", "moc_place": "MOC o'rin", "star": "Oy yulduzi", "growth": "O'sish yulduzi"}


async def verify(m: Message, arg: str) -> bool:
    try:
        cid_s, tok = arg[1:].split("_", 1)
        cid = int(cid_s)
    except ValueError:
        return False
    async with Session() as s:
        c = await s.get(Certificate, cid)
    if not c or c.token != tok:
        await m.answer("❌ Bunday sertifikat ELNUR EDU bazasida topilmadi.")
        return True
    d = c.data or {}
    await m.answer(f"✅ <b>Haqiqiy sertifikat</b>\n№ ELN-{c.created_at.year}-{c.id:05d}\n{c.name}\n"
                   f"{C.uz(d.get('category', ''))}\n{d.get('date', '')}\n\nELNUR EDU o'quv markazi tomonidan berilgan.")
    return True


# ======================= sozlamalar =======================
class Ct(StatesGroup):
    pct = State()


async def settings_view(msg: Message, edit: bool = False) -> None:
    async with Session() as s:
        conf = await cfg(s)
        n = len((await s.execute(select(Certificate.id))).all())
    rows = [[IB(text=("✅ " if conf[k] else "▫️ ") + v, callback_data=f"ctt:{k}")] for k, v in CFG_NAMES.items()]
    rows.append([IB(text=f"📉 Eng kam natija (o'rinlar uchun): {conf['min_pct']}%", callback_data="ctpc")])
    t = date.today()
    prev = t.replace(day=1) - timedelta(days=1)
    rows.append([IB(text=f"🏆 Oylik g'oliblar: {MONTHS[prev.month - 1]}", callback_data=f"ctw:{prev:%Y-%m}"),
                 IB(text=f"{MONTHS[t.month - 1]}", callback_data=f"ctw:{t:%Y-%m}")])
    rows.append([IB(text="📜 Oxirgi sertifikatlar", callback_data="ctl")])
    text = ("🏅 <b>Sertifikatlar</b>\nHar kategoriya va har o'rin — alohida dizayn va maqtov so'zlari. "
            "Teng natija — teng o'rin.\n• Guruh nazorati — apellyatsiya muddati (48 soat) tugagach.\n"
            "• Oylik markaz g'oliblari — har oyning 3-kuni sizga tasdiqlash uchun keladi.\n"
            f"• MOC — natijalar bilan birga.\n\nJami berilgan: {n} ta")
    if edit:
        try:
            return await msg.edit_text(text, reply_markup=IM(inline_keyboard=rows))
        except Exception:
            pass
    await msg.answer(text, reply_markup=IM(inline_keyboard=rows))


async def _is_boss(tg: int) -> bool:
    async with Session() as s:
        st = (await S.who_by_tg(s, tg)).staff
    return bool(st and st.current_role == "rahbar")


@router.callback_query(F.data == "set:cert")
async def settings_cb(c: CallbackQuery, state: FSMContext) -> None:
    if not await _is_boss(c.from_user.id):
        return await c.answer("Faqat rahbar.", show_alert=True)
    await state.clear()
    await c.answer()
    await settings_view(c.message)


@router.callback_query(F.data.startswith("ctt:"))
async def toggle(c: CallbackQuery) -> None:
    if not await _is_boss(c.from_user.id):
        return await c.answer("Faqat rahbar.", show_alert=True)
    k = c.data.split(":")[1]
    async with Session() as s:
        conf = await cfg(s)
        conf[k] = not conf.get(k)
        row = await s.get(Setting, CFG_KEY)
        if row:
            row.value = conf
        else:
            s.add(Setting(key=CFG_KEY, value=conf))
        await s.commit()
    await c.answer("Saqlandi")
    await settings_view(c.message, edit=True)


@router.callback_query(F.data == "ctpc")
async def pct_ask(c: CallbackQuery, state: FSMContext) -> None:
    if not await _is_boss(c.from_user.id):
        return await c.answer("Faqat rahbar.", show_alert=True)
    await state.set_state(Ct.pct)
    await c.answer()
    await c.message.answer("1–3-o'rin sertifikati uchun eng kam natija necha foiz bo'lsin? (masalan 60)")


@router.message(Ct.pct, F.text, ~F.text.in_(K.menu_texts()))
async def pct_save(m: Message, state: FSMContext) -> None:
    t = m.text.strip().rstrip("%")
    if not t.isdigit() or not 0 <= int(t) <= 100:
        return await m.answer("0 dan 100 gacha son yozing.")
    await state.clear()
    async with Session() as s:
        conf = await cfg(s)
        conf["min_pct"] = int(t)
        row = await s.get(Setting, CFG_KEY)
        if row:
            row.value = conf
        else:
            s.add(Setting(key=CFG_KEY, value=conf))
        await s.commit()
    await m.answer(f"✅ Eng kam natija: {t}%")
    await settings_view(m)


@router.callback_query(F.data.startswith("ctw:"))
async def winners_now(c: CallbackQuery, bot: Bot) -> None:
    if not await _is_boss(c.from_user.id):
        return await c.answer("Faqat rahbar.", show_alert=True)
    await c.answer()
    await month_preview(bot, c.data.split(":", 1)[1], chat=c.message.chat.id)


@router.callback_query(F.data == "ctl")
async def last_list(c: CallbackQuery) -> None:
    async with Session() as s:
        rows = (await s.execute(select(Certificate).order_by(Certificate.id.desc()).limit(25))).scalars().all()
    await c.answer()
    if not rows:
        return await c.message.answer("Hali sertifikat berilmagan.")
    lines = ["📜 <b>Oxirgi sertifikatlar</b>"]
    for x in rows:
        lines.append(f"№{x.id:05d} · {x.created_at:%d.%m} · {x.name} — {C.uz((x.data or {}).get('category', ''))}"
                     + ("" if x.sent else " (ota-onasi botga ulanmagan)"))
    await c.message.answer("\n".join(lines), reply_markup=IM(inline_keyboard=[
        [IB(text=f"📄 №{x.id:05d} {x.name[:22]}", callback_data=f"ctr:{x.id}")] for x in rows[:10]]))


@router.callback_query(F.data.startswith("ctr:"))
async def resend(c: CallbackQuery, bot: Bot) -> None:
    async with Session() as s:
        x = await s.get(Certificate, int(c.data.split(":")[1]))
    if not x:
        return await c.answer()
    await c.answer("Tayyorlanmoqda…")
    uname = (await bot.get_me()).username or ""
    im = C.render(_cert_obj(x, uname))
    await bot.send_document(c.message.chat.id, BufferedInputFile(C.to_pdf([im]), f"Sertifikat_{x.name.replace(' ', '_')}.pdf"))
