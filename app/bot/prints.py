"""🖨 Chop etish: xodim faylni adminga yuboradi, admin chiqarib beradi.

• 📄 Ichki hujjat — bepul (o'qituvchi/xodim hujjatlari). «Chiqarildi» bosilganda qog'oz ichki hisobga yoziladi.
• 📚 Kitobcha — pullik. Kitoblar fan bo'yicha kutubxonada saqlanadi (yoki bir martalik).
  Narx: varaq × 500 so'm, 1000 ga yaxlitlanadi (15 varaqgacha yuqoriga, undan ko'pi pastga).
  Admin chiqargach guruh ro'yxatidan kim to'laganini belgilaydi → kassaga tushadi.
  Har to'langan kitobchadan: yuklagan o'qituvchiga 7%, pulni olgan adminga 8% (maoshga qo'shiladi).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import func, select

from app import services as S
from app.bot import keyboards as K
from app.bot.notify import send
from app.db.models import (Enrollment, Group, PrintBook, PrinterUse, PrintJob, Service, ServiceSale, Setting, Staff,
                           Student)
from app.db.session import Session
from app.logic import printing as P

router = Router()
CASHIERS = ("admin", "rahbar", "moliya")
SET_KEYS = {"sheet_price": ("1 varaq narxi (so'm)", P.SHEET_PRICE),
            "book_teacher_pct": ("O'qituvchi (muallif) ulushi, %", P.TEACHER_PCT),
            "book_admin_pct": ("Admin ulushi, %", P.ADMIN_PCT)}
ST = {"yangi": "⏳ navbatda", "chiqarildi": "✅ chiqarildi", "bekor": "❌ bekor qilindi"}


class Pr(StatesGroup):
    file = State()
    pages = State()
    copies = State()
    note = State()
    title = State()
    subject = State()
    reason = State()
    setv = State()


# ---------------- yordamchilar ----------------
async def num(s, key: str) -> float:
    row = await s.get(Setting, key)
    d = SET_KEYS[key][1]
    try:
        return float((row.value or {}).get("v", d)) if row else d
    except (TypeError, ValueError):
        return d


async def book_service(s) -> Service:
    sv = (await s.execute(select(Service).where(Service.kind == "kitobcha"))).scalars().first()
    if not sv:
        sv = Service(name="📚 Kitobcha", kind="kitobcha", price=0, cost=0, active=False)
        s.add(sv)
        await s.flush()
    return sv


def _role(st: Staff) -> str:
    return st.current_role or ""


async def _me(s, tg: int) -> Staff | None:
    who = await S.who_by_tg(s, tg)
    return who.staff


def _calc_line(pages: int, layout: str, unit: int) -> tuple[int, int, str]:
    sh = P.sheets(pages, layout)
    pr = P.price(sh, unit)
    raw = sh * unit
    txt = (f"{pages} bet · {P.LAYOUT_SHORT[layout]} → <b>{sh} varaq</b> × {S.fmt_som(unit)} = "
           + (f"{S.fmt_som(raw)} → <b>{S.fmt_som(pr)}</b> (yaxlitlandi)" if pr != raw else f"<b>{S.fmt_som(pr)}</b>"))
    return sh, pr, txt


def _rows(btns: list[IB], n: int = 2) -> list[list[IB]]:
    return [btns[i:i + n] for i in range(0, len(btns), n)]


# ---------------- bosh menyu ----------------
@router.message(F.text == K.B_PRINT)
async def menu(m: Message, state: FSMContext) -> None:
    await state.clear()
    async with Session() as s:
        st = await _me(s, m.from_user.id)
        if not st:
            return
        q = (await s.execute(select(func.count(PrintJob.id)).where(PrintJob.status == "yangi"))).scalar()
    r = _role(st)
    rows = [[IB(text="📄 Ichki hujjat (bepul)", callback_data="pr:in")],
            [IB(text="📚 Kitobcha chiqarish (sotuv)", callback_data="pr:bk")],
            [IB(text="📂 Mening so'rovlarim", callback_data="pr:my")]]
    if r in ("admin", "rahbar"):
        rows.append([IB(text=f"🗂 Chop navbati · {q}", callback_data="pr:q")])
    if r in CASHIERS:
        rows.append([IB(text="💵 Kitobcha to'lovlari", callback_data="pr:pay")])
    if r == "rahbar":
        rows.append([IB(text="📊 Hisobot", callback_data="pr:rep"), IB(text="⚙️ Narx va ulush", callback_data="pr:set")])
    await m.answer("🖨 <b>Chop etish</b>\n\n📄 <b>Ichki hujjat</b> — o'zingizga yoki o'quvchilarga kerak hujjat, bepul.\n"
                   "📚 <b>Kitobcha</b> — o'quvchilarga sotiladigan qo'llanma: narxni bot hisoblaydi, "
                   "pulni admin yig'adi.", reply_markup=IM(inline_keyboard=rows))


# ---------------- fayl qabul qilish (ichki va yangi kitob) ----------------
@router.callback_query(F.data == "pr:in")
async def in_start(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(kind="ichki")
    await state.set_state(Pr.file)
    await c.answer()
    await c.message.answer("📄 Chiqariladigan faylni yuboring — <b>PDF</b>, Word yoki rasm.\n"
                           "<i>PDF bo'lsa sahifalarni bot o'zi sanaydi.</i>")


@router.callback_query(F.data == "pr:bk")
async def bk_start(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await c.answer()
    await c.message.answer("📚 <b>Kitobcha</b>", reply_markup=IM(inline_keyboard=[
        [IB(text="📚 Kutubxonadan tanlash", callback_data="pr:lib")],
        [IB(text="➕ Yangi kitob yuborish", callback_data="pr:new")]]))


@router.callback_query(F.data == "pr:new")
async def bk_new(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(kind="kitob")
    await state.set_state(Pr.file)
    await c.answer()
    await c.message.answer("📚 Kitob faylini yuboring (<b>PDF</b> tavsiya etiladi — sahifalarni bot o'zi sanaydi).")


@router.message(Pr.file, F.document | F.photo)
async def got_file(m: Message, state: FSMContext, bot: Bot) -> None:
    pages = None
    if m.photo:
        fid, kind, pages = m.photo[-1].file_id, "photo", 1
    else:
        fid, kind = m.document.file_id, "document"
        name = (m.document.file_name or "").lower()
        if name.endswith(".pdf") or m.document.mime_type == "application/pdf":
            if (m.document.file_size or 0) <= 19 * 1024 * 1024:
                try:
                    buf = await bot.download(m.document.file_id)
                    pages = P.pdf_pages(buf.read())
                except Exception:
                    pages = None
        await state.update_data(fname=m.document.file_name)
    await state.update_data(file_id=fid, file_kind=kind)
    if pages:
        await state.update_data(pages=pages)
        await m.answer(f"✅ Fayl qabul qilindi — <b>{pages} bet</b>.")
        return await _after_pages(m, state)
    await state.set_state(Pr.pages)
    await m.answer("Faylda necha bet (sahifa) bor? Raqam yozing: 16")


@router.message(Pr.file, ~F.text.in_(K.menu_texts()))
async def need_file(m: Message, state: FSMContext) -> None:
    await m.answer("Faylni yuboring (PDF, Word yoki rasm).")


@router.message(Pr.pages, F.text, ~F.text.in_(K.menu_texts()))
async def got_pages(m: Message, state: FSMContext) -> None:
    t = m.text.strip()
    if not t.isdigit() or not 0 < int(t) <= 2000:
        return await m.answer("Bet sonini raqamda yozing: 16")
    await state.update_data(pages=int(t))
    await _after_pages(m, state)


async def _after_pages(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    await state.set_state(None)
    if d["kind"] == "ichki":
        if d["pages"] == 1:
            await state.update_data(layout="bir")
            return await _ask_copies(m, state)
        return await m.answer("Qanday chiqarilsin?", reply_markup=IM(inline_keyboard=[
            [IB(text="📃 Bir tomonlama", callback_data="prl:bir"), IB(text="📄 Ikki tomonlama", callback_data="prl:ikki")]]))
    async with Session() as s:
        unit = int(await num(s, "sheet_price"))
    lines = "\n".join(f"• {v}: {S.fmt_som(P.price(P.sheets(d['pages'], k), unit))}" for k, v in P.LAYOUTS.items())
    await m.answer(f"Kitob qanday chiqariladi?\n\n{lines}", reply_markup=IM(inline_keyboard=[
        [IB(text=v.split(' — ')[0], callback_data=f"prl:{k}")] for k, v in P.LAYOUTS.items()]))


@router.callback_query(F.data.startswith("prl:"))
async def got_layout(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    if "pages" not in d:
        return await c.answer("Jarayon eskirgan — qaytadan boshlang.", show_alert=True)
    await state.update_data(layout=c.data.split(":")[1])
    await c.answer()
    if d["kind"] == "ichki":
        return await _ask_copies(c.message, state)
    # kitob: fan tanlash
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        subs = await _subjects(s, st)
    await state.update_data(subs=subs)
    await c.message.edit_text("Qaysi fan kitobi? (kutubxonada shu fan bo'limiga qo'yiladi)", reply_markup=IM(
        inline_keyboard=_rows([IB(text=x, callback_data=f"prf:{i}") for i, x in enumerate(subs)])
        + [[IB(text="✏️ Boshqa fan", callback_data="prf:-1")]]))


STD_SUBJECTS = ["Matematika", "Fizika", "Kimyo", "Biologiya", "Ingliz tili", "Ona tili", "Tarix", "Informatika"]


def _detect(name: str) -> str | None:
    n = (name or "").lower()
    for x in STD_SUBJECTS:
        if x.lower()[:5] in n:
            return x
    return None


async def _subjects(s, st: Staff | None) -> list[str]:
    """Fanlar: avval o'qituvchining o'z guruhlari fani, keyin kutubxonadagi va standart fanlar."""
    mine = []
    if st:
        for g in (await s.execute(select(Group).where(
                Group.active, (Group.teacher_id == st.id) | (Group.assistant_id == st.id)))).scalars():
            x = _detect(g.name) or _detect(g.subject or "")
            if x:
                mine.append(x)
    have = [x for x in (await s.execute(select(PrintBook.subject).where(PrintBook.saved, PrintBook.active))).scalars()]
    out = []
    for x in mine + sorted(set(have)) + STD_SUBJECTS:
        if x and x not in out:
            out.append(x)
    return out[:20]


@router.callback_query(F.data.startswith("prf:"))
async def got_subject(c: CallbackQuery, state: FSMContext) -> None:
    i = int(c.data.split(":")[1])
    d = await state.get_data()
    await c.answer()
    if i < 0 or i >= len(d.get("subs", [])):
        await state.set_state(Pr.subject)
        return await c.message.edit_text("Fan nomini yozing: Matematika")
    await state.update_data(subject=d["subs"][i])
    await _ask_title(c.message, state)


@router.message(Pr.subject, F.text, ~F.text.in_(K.menu_texts()))
async def typed_subject(m: Message, state: FSMContext) -> None:
    await state.update_data(subject=m.text.strip()[:60].capitalize())
    await _ask_title(m, state)


async def _ask_title(msg: Message, state: FSMContext) -> None:
    d = await state.get_data()
    await state.set_state(Pr.title)
    hint = ""
    if d.get("fname"):
        base = d["fname"].rsplit(".", 1)[0][:60]
        hint = "\n\nYoki fayl nomini qoldirish uchun bosing:"
        await state.update_data(fname_title=base)
    await msg.answer(f"📗 Kitob nomini yozing (masalan: <i>Algebra 7-sinf, 1-qism</i>).{hint}",
                     reply_markup=IM(inline_keyboard=[[IB(text=d['fname'].rsplit('.', 1)[0][:40], callback_data="prtf")]])
                     if hint else None)


@router.callback_query(F.data == "prtf")
async def title_from_file(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    await c.answer()
    await _set_title(c.message, state, d.get("fname_title") or "Kitob")


@router.message(Pr.title, F.text, ~F.text.in_(K.menu_texts()))
async def got_title(m: Message, state: FSMContext) -> None:
    await _set_title(m, state, m.text.strip()[:150])


async def _set_title(msg: Message, state: FSMContext, title: str) -> None:
    await state.update_data(title=title)
    await state.set_state(None)
    await msg.answer(f"📗 <b>{title}</b>\n\n❓ Bu kitob <b>keyin ham kerak bo'ladimi?</b>\n"
                     "Ha — kutubxonaga saqlanadi, keyingi safar faylni qayta yubormay, ro'yxatdan tanlaysiz.",
                     reply_markup=IM(inline_keyboard=[[IB(text="✅ Ha, saqlansin", callback_data="prsv:1"),
                                                       IB(text="1️⃣ Bir martalik", callback_data="prsv:0")]]))


@router.callback_query(F.data.startswith("prsv:"))
async def got_save(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    if "title" not in d:
        return await c.answer("Jarayon eskirgan — qaytadan boshlang.", show_alert=True)
    await state.update_data(saved=c.data.endswith("1"))
    await c.answer()
    await _ask_group(c.message, state)


# ---------------- kutubxona ----------------
@router.callback_query(F.data == "pr:lib")
async def lib(c: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        rows = (await s.execute(select(PrintBook.subject, func.count(PrintBook.id)).where(
            PrintBook.saved, PrintBook.active).group_by(PrintBook.subject).order_by(PrintBook.subject))).all()
    await c.answer()
    if not rows:
        return await c.message.answer("📚 Kutubxona hozircha bo'sh. «➕ Yangi kitob yuborish» orqali qo'shing — "
                                      "«keyin ham kerak» desangiz shu yerga tushadi.",
                                      reply_markup=IM(inline_keyboard=[[IB(text="➕ Yangi kitob", callback_data="pr:new")]]))
    await state.update_data(libsubs=[r[0] for r in rows])
    await c.message.answer("📚 <b>Kutubxona</b> — fanni tanlang:", reply_markup=IM(inline_keyboard=_rows(
        [IB(text=f"{sub} · {n}", callback_data=f"prls:{i}") for i, (sub, n) in enumerate(rows)])))


@router.callback_query(F.data.startswith("prls:"))
async def lib_subject(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    subs = d.get("libsubs") or []
    i = int(c.data.split(":")[1])
    if i >= len(subs):
        return await c.answer("Qaytadan oching.", show_alert=True)
    async with Session() as s:
        unit = int(await num(s, "sheet_price"))
        books = (await s.execute(select(PrintBook).where(PrintBook.saved, PrintBook.active,
                                                         PrintBook.subject == subs[i]).order_by(PrintBook.title))).scalars().all()
    await c.answer()
    await c.message.edit_text(f"📚 <b>{subs[i]}</b> — kitobni tanlang:", reply_markup=IM(inline_keyboard=[
        [IB(text=f"{b.title[:38]} · {b.pages} bet · {S.fmt_som(P.price(P.sheets(b.pages, b.layout), unit))}",
            callback_data=f"prb:{b.id}")] for b in books] + [[IB(text="⬅️ Fanlar", callback_data="pr:lib")]]))


@router.callback_query(F.data.startswith("prb:"))
async def book_card(c: CallbackQuery) -> None:
    bid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        b = await s.get(PrintBook, bid)
        if not b or not b.active:
            return await c.answer("Kitob topilmadi.", show_alert=True)
        unit = int(await num(s, "sheet_price"))
        owner = await s.get(Staff, b.owner_id) if b.owner_id else None
        used = (await s.execute(select(func.coalesce(func.sum(PrintJob.copies), 0)).where(
            PrintJob.book_id == b.id, PrintJob.status == "chiqarildi"))).scalar()
    _, _, line = _calc_line(b.pages, b.layout, unit)
    rows = [[IB(text="🖨 Buyurtma berish", callback_data=f"prbo:{b.id}")],
            [IB(text="📎 Faylni ko'rish", callback_data=f"prbf:{b.id}")]]
    if st and (st.id == b.owner_id or _role(st) == "rahbar"):
        rows.append([IB(text="🗑 Kutubxonadan olib tashlash", callback_data=f"prbd:{b.id}")])
    await c.answer()
    await c.message.answer(f"📗 <b>{b.title}</b>\nFan: {b.subject}\n{line}\n"
                           f"Muallif: {owner.full_name if owner else '—'} · shu paytgacha {used} nusxa chiqarilgan",
                           reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data.startswith("prbf:"))
async def book_file(c: CallbackQuery) -> None:
    async with Session() as s:
        b = await s.get(PrintBook, int(c.data.split(":")[1]))
    await c.answer()
    if b:
        await _send_file(c.bot, c.message.chat.id, b.file_id, b.file_kind, b.title)


@router.callback_query(F.data.startswith("prbd:"))
async def book_del(c: CallbackQuery) -> None:
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        b = await s.get(PrintBook, int(c.data.split(":")[1]))
        if not b or not st or (st.id != b.owner_id and _role(st) != "rahbar"):
            return await c.answer("Faqat muallif yoki rahbar.", show_alert=True)
        b.active = False
        await S.audit(s, st, "kitob_ochirildi", id=b.id, title=b.title)
        await s.commit()
    await c.answer("Olib tashlandi")
    await c.message.edit_text(c.message.html_text + "\n\n🗑 Kutubxonadan olib tashlandi.")


@router.callback_query(F.data.startswith("prbo:"))
async def book_order(c: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        b = await s.get(PrintBook, int(c.data.split(":")[1]))
    if not b or not b.active:
        return await c.answer("Kitob topilmadi.", show_alert=True)
    await state.clear()
    await state.update_data(kind="kitob", book_id=b.id, pages=b.pages, layout=b.layout, title=b.title)
    await c.answer()
    await _ask_group(c.message, state)


# ---------------- guruh, nusxa, tasdiq ----------------
async def _ask_group(msg: Message, state: FSMContext) -> None:
    async with Session() as s:
        st = await _me(s, msg.chat.id)
        q = select(Group).where(Group.active)
        if st and _role(st) not in ("rahbar", "admin"):
            q = q.where((Group.teacher_id == st.id) | (Group.assistant_id == st.id))
        gs = (await s.execute(q.order_by(Group.name))).scalars().all()
    await msg.answer("Qaysi guruh uchun?", reply_markup=IM(inline_keyboard=_rows(
        [IB(text=g.name, callback_data=f"prg:{g.id}") for g in gs]) + [[IB(text="Guruhsiz", callback_data="prg:0")]]))


@router.callback_query(F.data.startswith("prg:"))
async def got_group(c: CallbackQuery, state: FSMContext) -> None:
    gid = int(c.data.split(":")[1]) or None
    await state.update_data(group_id=gid)
    await c.answer()
    await _ask_copies(c.message, state, gid)


async def _ask_copies(msg: Message, state: FSMContext, gid: int | None = None) -> None:
    await state.set_state(Pr.copies)
    btns = [IB(text="1", callback_data="prc:1")]
    if gid:
        async with Session() as s:
            n = (await s.execute(select(func.count(Enrollment.id)).where(Enrollment.group_id == gid,
                                                                         Enrollment.active))).scalar()
        if n:
            btns = [IB(text=f"{n} ta (guruhdagilar soni)", callback_data=f"prc:{n}")]
    await msg.answer("Necha nusxa? Tugmani bosing yoki sonini yozing:", reply_markup=IM(inline_keyboard=[btns]))


@router.callback_query(F.data.startswith("prc:"))
async def copies_btn(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await _set_copies(c.message, state, int(c.data.split(":")[1]))


@router.message(Pr.copies, F.text, ~F.text.in_(K.menu_texts()))
async def copies_typed(m: Message, state: FSMContext) -> None:
    t = m.text.strip()
    if not t.isdigit() or not 0 < int(t) <= 500:
        return await m.answer("Nusxa sonini yozing: 15")
    await _set_copies(m, state, int(t))


async def _set_copies(msg: Message, state: FSMContext, n: int) -> None:
    await state.update_data(copies=n)
    d = await state.get_data()
    if "pages" not in d:
        await state.clear()
        return await msg.answer("Jarayon eskirgan — «🖨 Chop etish» dan qaytadan boshlang.")
    if d["kind"] == "ichki":
        await state.set_state(Pr.note)
        return await msg.answer("Qisqa izoh (nima uchun, qachongacha kerak):",
                                reply_markup=IM(inline_keyboard=[[IB(text="⏭ Izohsiz yuborish", callback_data="prnn")]]))
    await state.set_state(None)
    async with Session() as s:
        unit = int(await num(s, "sheet_price"))
        g = await s.get(Group, d["group_id"]) if d.get("group_id") else None
    sh, pr, line = _calc_line(d["pages"], d["layout"], unit)
    await msg.answer(f"📚 <b>{d['title']}</b>" + (f" · {g.name}" if g else "") + f"\n{line}\n"
                     f"{n} nusxa → {sh * n} varaq qog'oz · jami <b>{S.fmt_som(pr * n)}</b> tushum\n\n"
                     "Adminga yuboraymi?", reply_markup=IM(inline_keyboard=[
                         [IB(text="📤 Adminga yuborish", callback_data="prok"), IB(text="❌ Bekor", callback_data="prx")]]))


@router.callback_query(F.data == "prnn")
async def note_skip(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await _create(c.message, state, c.from_user.id, None, c.bot)


@router.message(Pr.note, F.text, ~F.text.in_(K.menu_texts()))
async def note_typed(m: Message, state: FSMContext, bot: Bot) -> None:
    await _create(m, state, m.from_user.id, m.text.strip()[:300], bot)


@router.callback_query(F.data == "prok")
async def confirm(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    await _create(c.message, state, c.from_user.id, None, c.bot)


@router.callback_query(F.data == "prx")
async def cancel(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await c.answer("Bekor qilindi")
    await c.message.edit_text(c.message.html_text + "\n\n❌ Bekor qilindi.")


async def _create(msg: Message, state: FSMContext, tg: int, note: str | None, bot: Bot) -> None:
    d = await state.get_data()
    await state.clear()
    if "pages" not in d or "copies" not in d:
        return await msg.answer("Jarayon eskirgan — «🖨 Chop etish» dan qaytadan boshlang.")
    async with Session() as s:
        st = await _me(s, tg)
        if not st:
            return
        unit = int(await num(s, "sheet_price"))
        sh = P.sheets(d["pages"], d["layout"])
        job = PrintJob(kind=d["kind"], staff_id=st.id, group_id=d.get("group_id"), copies=d["copies"], pages=d["pages"],
                       layout=d["layout"], sheets=sh, note=note)
        if d["kind"] == "kitob":
            if d.get("book_id"):
                b = await s.get(PrintBook, d["book_id"])
            else:
                b = PrintBook(subject=d["subject"], title=d["title"], pages=d["pages"], layout=d["layout"],
                              file_id=d["file_id"], file_kind=d["file_kind"], owner_id=st.id, saved=d.get("saved", False))
                s.add(b)
                await s.flush()
            job.book_id, job.file_id, job.file_kind, job.price = b.id, b.file_id, b.file_kind, P.price(sh, unit)
        else:
            job.file_id, job.file_kind = d["file_id"], d["file_kind"]
        s.add(job)
        await S.audit(s, st, "chop_sorov", kind=job.kind, copies=job.copies, sheets=sh)
        await s.commit()
        cap, kb = await _job_card(s, job)
        to = await _printers(s)
    await msg.answer(f"✅ So'rov №{job.id} adminga yuborildi. Chiqarilgach xabar keladi.")
    for t in to:
        await _send_file(bot, t, job.file_id, job.file_kind, cap, kb)


async def _printers(s) -> list[int]:
    to = [x.tg_id for x in await S.staff_with_role(s, "admin") if x.tg_id]
    if not to:
        to = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    return list(dict.fromkeys(to))


async def _send_file(bot: Bot, chat: int, fid: str | None, kind: str | None, caption: str, kb=None) -> None:
    try:
        if fid and kind == "photo":
            await bot.send_photo(chat, fid, caption=caption[:1024], reply_markup=kb)
        elif fid:
            await bot.send_document(chat, fid, caption=caption[:1024], reply_markup=kb)
        else:
            raise ValueError
    except Exception:
        await send(bot, chat, caption, reply_markup=kb)


async def _job_card(s, job: PrintJob) -> tuple[str, IM | None]:
    st = await s.get(Staff, job.staff_id) if job.staff_id else None
    g = await s.get(Group, job.group_id) if job.group_id else None
    total = job.sheets * job.copies
    if job.kind == "kitob":
        b = await s.get(PrintBook, job.book_id)
        cap = (f"📚 <b>Kitobcha №{job.id}</b> — SOTUV\n{b.title} ({b.subject})\n"
               f"{job.pages} bet · {P.LAYOUT_SHORT[job.layout]} → {job.sheets} varaq\n"
               f"💰 Narx: <b>{S.fmt_som(job.price)}</b> (dona)\n"
               f"🖨 {job.copies} nusxa = {total} varaq" + (f" · {g.name}" if g else "")
               + f"\n👤 {st.full_name if st else ''}")
    else:
        cap = (f"📄 <b>Ichki hujjat №{job.id}</b> — bepul\n"
               f"{job.pages} bet · {P.LAYOUT_SHORT[job.layout]} × {job.copies} nusxa = {total} varaq\n"
               f"👤 {st.full_name if st else ''}" + (f"\n📝 {job.note}" if job.note else ""))
    kb = None
    if job.status == "yangi":
        kb = IM(inline_keyboard=[[IB(text="✅ Chiqarildi", callback_data=f"prd:{job.id}"),
                                  IB(text="❌ Bekor", callback_data=f"prn:{job.id}")]])
    else:
        cap += f"\n\n{ST[job.status]}"
    return cap, kb


# ---------------- admin: chiqarildi / bekor ----------------
@router.callback_query(F.data.startswith("prd:"))
async def done(c: CallbackQuery, bot: Bot) -> None:
    jid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        job = await s.get(PrintJob, jid)
        if not st or _role(st) not in ("admin", "rahbar"):
            return await c.answer("Faqat admin.", show_alert=True)
        if not job or job.status != "yangi":
            return await c.answer("Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
        job.status, job.done_by, job.done_at = "chiqarildi", st.id, datetime.utcnow()
        req = await s.get(Staff, job.staff_id) if job.staff_id else None
        if job.kind == "ichki":
            teach = req and any(r in (req.roles or "") for r in ("oqituvchi", "yordamchi"))
            s.add(PrinterUse(category="oqituvchi" if teach else "markaz", pages=job.sheets * job.copies,
                             note=f"So'rov №{job.id}" + (f": {job.note}" if job.note else "")[:180],
                             staff_id=job.staff_id, by_id=st.id))
        await S.audit(s, st, "chop_chiqarildi", id=job.id, kind=job.kind, sheets=job.sheets * job.copies)
        await s.commit()
        b = await s.get(PrintBook, job.book_id) if job.book_id else None
        pay = await pay_card(s, job) if job.kind == "kitob" else None
    await c.answer("Qayd etildi ✅")
    try:
        await c.message.edit_caption(caption=(c.message.html_text or "") + f"\n\n✅ Chiqarildi — {st.full_name}")
    except Exception:
        try:
            await c.message.edit_text((c.message.html_text or "") + f"\n\n✅ Chiqarildi — {st.full_name}")
        except Exception:
            pass
    if req and req.tg_id:
        if job.kind == "kitob":
            await send(bot, req.tg_id, f"✅ <b>{b.title}</b> — {job.copies} nusxa tayyor (so'rov №{job.id}).\n"
                                       f"O'quvchilar adminga {S.fmt_som(job.price)} dan to'laydi. Kim to'lagani — "
                                       "«🖨 Chop etish» → «📂 Mening so'rovlarim».")
        else:
            await send(bot, req.tg_id, f"✅ Hujjatingiz tayyor (so'rov №{job.id}, {job.copies} nusxa). Admindan oling.")
    if pay:
        await c.message.answer(pay[0], reply_markup=pay[1])


@router.callback_query(F.data.startswith("prn:"))
async def reject(c: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        job = await s.get(PrintJob, int(c.data.split(":")[1]))
    if not st or _role(st) not in ("admin", "rahbar"):
        return await c.answer("Faqat admin.", show_alert=True)
    if not job or job.status != "yangi":
        return await c.answer("Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
    await state.set_state(Pr.reason)
    await state.update_data(jid=job.id)
    await c.answer()
    await c.message.answer(f"So'rov №{job.id} nima sababdan bekor qilinmoqda? (so'ragan xodimga boradi)")


@router.message(Pr.reason, F.text, ~F.text.in_(K.menu_texts()))
async def reject_save(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        st = await _me(s, m.from_user.id)
        job = await s.get(PrintJob, d["jid"])
        if not job or job.status != "yangi":
            return await m.answer("So'rov allaqachon ko'rib chiqilgan.")
        job.status, job.done_by, job.done_at, job.note = "bekor", st.id, datetime.utcnow(), \
            ((job.note or "") + f" | Bekor: {m.text.strip()}")[:300]
        await S.audit(s, st, "chop_bekor", id=job.id)
        await s.commit()
        req = await s.get(Staff, job.staff_id) if job.staff_id else None
    await m.answer(f"❌ So'rov №{job.id} bekor qilindi.")
    if req and req.tg_id:
        await send(bot, req.tg_id, f"❌ Chop etish so'rovingiz №{job.id} bekor qilindi.\nSabab: {m.text.strip()}")


@router.callback_query(F.data == "pr:q")
async def queue(c: CallbackQuery) -> None:
    async with Session() as s:
        jobs = (await s.execute(select(PrintJob).where(PrintJob.status == "yangi").order_by(PrintJob.id))).scalars().all()
        cards = [(j, *(await _job_card(s, j))) for j in jobs]
    await c.answer()
    if not cards:
        return await c.message.answer("🗂 Navbat bo'sh ✅")
    for j, cap, kb in cards:
        await _send_file(c.bot, c.message.chat.id, j.file_id, j.file_kind, cap, kb)


# ---------------- kitobcha to'lovlari ----------------
async def _paid(s, job_id: int) -> list[ServiceSale]:
    return list((await s.execute(select(ServiceSale).where(
        ServiceSale.job_id == job_id, ServiceSale.amount > 0, ServiceSale.status == "tasdiq",
        ServiceSale.voided_at.is_(None)))).scalars())


async def pay_card(s, job: PrintJob, readonly: bool = False) -> tuple[str, IM | None]:
    b = await s.get(PrintBook, job.book_id)
    g = await s.get(Group, job.group_id) if job.group_id else None
    paid = await _paid(s, job.id)
    by_sid = {p.student_id for p in paid if p.student_id}
    extra = sum(1 for p in paid if not p.student_id)
    studs = []
    if g:
        studs = list((await s.execute(select(Student).join(Enrollment, Enrollment.student_id == Student.id).where(
            Enrollment.group_id == g.id, Enrollment.active).order_by(Student.full_name))).scalars())
        known = {x.id for x in studs}
        for sid in by_sid - known:          # guruhdan chiqqan, lekin to'lagan
            x = await s.get(Student, sid)
            if x:
                studs.append(x)
    left = job.copies - len(paid)
    yes = [x for x in studs if x.id in by_sid]
    no = [x for x in studs if x.id not in by_sid]
    txt = (f"💵 <b>Kitobcha to'lovi</b> · №{job.id}\n📗 {b.title}" + (f" · {g.name}" if g else "")
           + f"\nNarx: <b>{S.fmt_som(job.price)}</b> · chiqarildi: {job.copies} nusxa\n"
           f"To'ladi: <b>{len(paid)}/{job.copies}</b> · {S.fmt_som(sum(p.amount for p in paid))}"
           + (f" · qolgan nusxa: {left}" if left > 0 else " · hammasi sotildi ✅"))
    if yes:
        txt += "\n\n✅ " + ", ".join(x.full_name for x in yes)
    if extra:
        txt += f"\n✅ Boshqa xaridorlar: {extra}"
    if no:
        txt += "\n\n⏳ To'lamagan: " + ", ".join(x.full_name for x in no)
    if readonly:
        return txt, None
    btns = [IB(text=x.full_name[:28], callback_data=f"kp:{job.id}:{x.id}") for x in no] if left > 0 else []
    rows = _rows(btns)
    if left > 0:
        rows.append([IB(text="➕ Boshqa xaridor", callback_data=f"kp:{job.id}:0")])
    rows.append([IB(text="🔄 Yangilash", callback_data=f"kpr:{job.id}")])
    return txt, IM(inline_keyboard=rows)


@router.callback_query(F.data == "pr:pay")
async def pay_list(c: CallbackQuery) -> None:
    edge = datetime.utcnow() - timedelta(days=120)
    async with Session() as s:
        jobs = (await s.execute(select(PrintJob).where(PrintJob.kind == "kitob", PrintJob.status == "chiqarildi",
                                                       PrintJob.done_at >= edge).order_by(PrintJob.id.desc()))).scalars().all()
        rows = []
        for j in jobs:
            n = len(await _paid(s, j.id))
            if n < j.copies:
                b = await s.get(PrintBook, j.book_id)
                g = await s.get(Group, j.group_id) if j.group_id else None
                rows.append([IB(text=f"{b.title[:26]} · {g.name if g else 'guruhsiz'} · {n}/{j.copies}",
                                callback_data=f"kpr:{j.id}")])
    await c.answer()
    if not rows:
        return await c.message.answer("💵 To'lanmagan kitobcha yo'q ✅")
    await c.message.answer("💵 <b>Kitobcha to'lovlari</b> — to'lov yig'ilayotganlar:", reply_markup=IM(inline_keyboard=rows[:30]))


@router.callback_query(F.data.startswith("kpr:"))
async def pay_refresh(c: CallbackQuery) -> None:
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        job = await s.get(PrintJob, int(c.data.split(":")[1]))
        if not job:
            return await c.answer()
        txt, kb = await pay_card(s, job, readonly=not st or _role(st) not in CASHIERS)
    await c.answer()
    try:
        await c.message.edit_text(txt, reply_markup=kb)
    except Exception:
        await c.message.answer(txt, reply_markup=kb)


@router.callback_query(F.data.startswith("kp:"))
async def pay_pick(c: CallbackQuery) -> None:
    _, jid, sid = c.data.split(":")
    async with Session() as s:
        job = await s.get(PrintJob, int(jid))
        x = await s.get(Student, int(sid)) if int(sid) else None
    who = x.full_name if x else "Boshqa xaridor"
    await c.answer()
    await c.message.edit_text(f"💵 {who} — <b>{S.fmt_som(job.price)}</b>. To'lov usuli?", reply_markup=IM(inline_keyboard=[
        [IB(text="💵 Naqd", callback_data=f"kpm:{jid}:{sid}:naqd"), IB(text="💳 Karta", callback_data=f"kpm:{jid}:{sid}:karta")],
        [IB(text="⬅️ Orqaga", callback_data=f"kpr:{jid}")]]))


@router.callback_query(F.data.startswith("kpm:"))
async def pay_save(c: CallbackQuery, bot: Bot) -> None:
    _, jid, sid, method = c.data.split(":")
    sid = int(sid) or None
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        if not st or _role(st) not in CASHIERS:
            return await c.answer("Faqat admin / moliya.", show_alert=True)
        job = await s.get(PrintJob, int(jid))
        paid = await _paid(s, job.id)
        if len(paid) >= job.copies:
            return await c.answer("Chiqarilgan nusxalar tugagan. Yana kerak bo'lsa — yangi buyurtma.", show_alert=True)
        if sid and any(p.student_id == sid for p in paid):
            return await c.answer("Bu o'quvchi allaqachon to'lagan.", show_alert=True)
        b = await s.get(PrintBook, job.book_id)
        sv = await book_service(s)
        s.add(ServiceSale(service_id=sv.id, qty=1, amount=job.price, profit=job.price, method=method, by_id=st.id,
                          job_id=job.id, student_id=sid, owner_id=b.owner_id))
        await S.audit(s, st, "kitobcha_tolov", job=job.id, student=sid, amount=job.price, method=method)
        await s.commit()
        x = await s.get(Student, sid) if sid else None
        tgs = await S.parent_tg_ids(s, sid) if sid else []
        txt, kb = await pay_card(s, job)
    await c.answer(f"✅ {S.fmt_som(job.price)} qabul qilindi")
    await c.message.edit_text(txt, reply_markup=kb)
    for t in tgs:
        await send(bot, t, f"📚 <b>{x.full_name}</b> uchun «{b.title}» kitobchasi to'lovi qabul qilindi: "
                           f"{S.fmt_som(job.price)} ({'naqd' if method == 'naqd' else 'karta'}). Rahmat!")


# ---------------- mening so'rovlarim ----------------
@router.callback_query(F.data == "pr:my")
async def my_jobs(c: CallbackQuery) -> None:
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        jobs = (await s.execute(select(PrintJob).where(PrintJob.staff_id == st.id).order_by(PrintJob.id.desc())
                                .limit(10))).scalars().all()
        lines, rows = ["📂 <b>Mening so'rovlarim</b>"], []
        for j in jobs:
            when = f"{(j.created_at + timedelta(hours=5)):%d.%m}"
            if j.kind == "kitob":
                b = await s.get(PrintBook, j.book_id)
                n = len(await _paid(s, j.id)) if j.status == "chiqarildi" else 0
                lines.append(f"\n№{j.id} · {when} · 📚 {b.title} × {j.copies} — {ST[j.status]}"
                             + (f" · to'ladi {n}/{j.copies}" if j.status == "chiqarildi" else ""))
                if j.status == "chiqarildi":
                    rows.append([IB(text=f"👁 №{j.id}: kim to'ladi", callback_data=f"kpr:{j.id}")])
            else:
                lines.append(f"\n№{j.id} · {when} · 📄 {j.pages} bet × {j.copies} — {ST[j.status]}")
        books = (await s.execute(select(PrintBook).where(PrintBook.owner_id == st.id, PrintBook.saved,
                                                         PrintBook.active))).scalars().all()
    if len(lines) == 1:
        lines.append("\nHali so'rov yo'q.")
    if books:
        lines.append(f"\n📚 Kutubxonadagi kitoblaringiz: {len(books)} ta — ular har sotilganda sizga ulush tushadi.")
    await c.answer()
    await c.message.answer("\n".join(lines), reply_markup=IM(inline_keyboard=rows[:8]) if rows else None)


# ---------------- ulushlar (maosh uchun) ----------------
async def book_shares(s, st: Staff, start: datetime, end: datetime) -> list[tuple[str, int]]:
    """Kitobchalardan: muallif o'qituvchiga 7%, to'lovni olgan xodimga 8% (sof: bekor qilinganlar ayiriladi)."""
    tp, ap = await num(s, "book_teacher_pct"), await num(s, "book_admin_pct")
    base = [ServiceSale.job_id.isnot(None), ServiceSale.status == "tasdiq", ServiceSale.created_at > start,
            ServiceSale.created_at <= end]
    own = int((await s.execute(select(func.coalesce(func.sum(ServiceSale.amount), 0)).where(
        ServiceSale.owner_id == st.id, *base))).scalar())
    sold = int((await s.execute(select(func.coalesce(func.sum(ServiceSale.amount), 0)).where(
        ServiceSale.by_id == st.id, *base))).scalar())
    out = []
    if own:
        out.append((f"Kitobchalar (muallif): {S.fmt_som(own)} × {tp:g}%", round(own * tp / 100)))
    if sold:
        out.append((f"Kitobchalar sotuvi: {S.fmt_som(sold)} × {ap:g}%", round(sold * ap / 100)))
    return out


async def paper_sheets(s, holder_id: int, since: datetime | None) -> int:
    """Ombor tekshiruvi uchun: shu xodim chiqargan kitobchalar varag'i."""
    q = select(func.coalesce(func.sum(PrintJob.sheets * PrintJob.copies), 0)).where(
        PrintJob.kind == "kitob", PrintJob.status == "chiqarildi", PrintJob.done_by == holder_id)
    if since:
        q = q.where(PrintJob.done_at >= since)
    return int((await s.execute(q)).scalar())


# ---------------- rahbar: hisobot va sozlamalar ----------------
@router.callback_query(F.data == "pr:rep")
async def report(c: CallbackQuery) -> None:
    now = datetime.utcnow() + timedelta(hours=5)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=5)
    async with Session() as s:
        tp, ap = await num(s, "book_teacher_pct"), await num(s, "book_admin_pct")
        jobs = (await s.execute(select(PrintJob).where(PrintJob.status == "chiqarildi",
                                                       PrintJob.done_at >= start))).scalars().all()
        sales = (await s.execute(select(ServiceSale).where(ServiceSale.job_id.isnot(None), ServiceSale.status == "tasdiq",
                                                           ServiceSale.created_at >= start))).scalars().all()
        by_owner, by_seller = {}, {}
        for x in sales:
            by_owner[x.owner_id] = by_owner.get(x.owner_id, 0) + x.amount
            by_seller[x.by_id] = by_seller.get(x.by_id, 0) + x.amount
        names = {st.id: st.full_name for st in (await s.execute(select(Staff))).scalars()}
        unpaid = 0
        for j in [j for j in jobs if j.kind == "kitob"]:
            unpaid += j.copies - len(await _paid(s, j.id))
    kit = [j for j in jobs if j.kind == "kitob"]
    ich = [j for j in jobs if j.kind == "ichki"]
    total = sum(x.amount for x in sales)
    lines = [f"📊 <b>Chop etish — {now:%m.%Y}</b>",
             f"\n📚 Kitobcha: {sum(j.copies for j in kit)} nusxa · {sum(j.sheets * j.copies for j in kit)} varaq",
             f"💰 Tushum: <b>{S.fmt_som(total)}</b> · to'lanmagan nusxa: {unpaid}",
             f"📄 Ichki hujjat: {len(ich)} ta so'rov · {sum(j.sheets * j.copies for j in ich)} varaq"]
    if by_owner:
        lines.append(f"\n👨‍🏫 Muallif ulushi ({tp:g}%):")
        lines += [f"• {names.get(k, '—')}: {S.fmt_som(round(v * tp / 100))}" for k, v in by_owner.items() if v]
    if by_seller:
        lines.append(f"\n🧾 Admin ulushi ({ap:g}%):")
        lines += [f"• {names.get(k, '—')}: {S.fmt_som(round(v * ap / 100))}" for k, v in by_seller.items() if v]
    await c.answer()
    await c.message.answer("\n".join(lines))


@router.callback_query(F.data == "pr:set")
async def settings_view(c: CallbackQuery) -> None:
    async with Session() as s:
        vals = {k: await num(s, k) for k in SET_KEYS}
    unit = int(vals["sheet_price"])
    ex = ", ".join(f"{n} varaq → {S.fmt_som(P.price(n, unit))}" for n in (4, 7, 13, 15, 19))
    await c.answer()
    await c.message.answer(
        "⚙️ <b>Kitobcha narxi va ulushlar</b>\n" + "\n".join(f"• {SET_KEYS[k][0]}: <b>{v:g}</b>" for k, v in vals.items())
        + f"\n\nMisol: {ex}\n<i>15 varaqgacha yuqoriga, undan ko'pi pastga 1000 ga yaxlitlanadi.</i>",
        reply_markup=IM(inline_keyboard=[[IB(text=f"✏️ {SET_KEYS[k][0]}", callback_data=f"prset:{k}")] for k in SET_KEYS]))


@router.callback_query(F.data.startswith("prset:"))
async def settings_ask(c: CallbackQuery, state: FSMContext) -> None:
    k = c.data.split(":")[1]
    async with Session() as s:
        st = await _me(s, c.from_user.id)
    if not st or _role(st) != "rahbar" or k not in SET_KEYS:
        return await c.answer("Faqat rahbar.", show_alert=True)
    await state.set_state(Pr.setv)
    await state.update_data(key=k)
    await c.answer()
    await c.message.answer(f"{SET_KEYS[k][0]} — yangi qiymatni yozing:")


@router.message(Pr.setv, F.text, ~F.text.in_(K.menu_texts()))
async def settings_save(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    t = m.text.strip().replace(",", ".").replace(" ", "")
    try:
        v = float(t)
    except ValueError:
        return await m.answer("Raqam yozing: 500")
    if v < 0 or (d["key"] != "sheet_price" and v > 50):
        return await m.answer("Qiymat to'g'ri emas.")
    await state.clear()
    async with Session() as s:
        st = await _me(s, m.from_user.id)
        row = await s.get(Setting, d["key"])
        if row:
            row.value = {"v": v}
        else:
            s.add(Setting(key=d["key"], value={"v": v}))
        await S.audit(s, st, "chop_sozlama", key=d["key"], value=v)
        await s.commit()
    await m.answer(f"✅ {SET_KEYS[d['key']][0]}: <b>{v:g}</b>. Yangi buyurtmalarga qo'llanadi "
                   "(chiqarilganlarning narxi o'zgarmaydi).")
