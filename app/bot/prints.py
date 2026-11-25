"""🖨 Chop etish: xodim faylni adminga yuboradi, admin chiqarib beradi.

• 📄 Ichki hujjat — bepul (o'qituvchi/xodim hujjatlari). «Chiqarildi» bosilganda qog'oz ichki hisobga yoziladi.
• 📚 Kitobcha — pullik. Kitoblar fan bo'yicha kutubxonada saqlanadi (yoki bir martalik).
  Narx: varaq × 500 so'm, 1000 ga yaxlitlanadi (15 varaqgacha yuqoriga, undan ko'pi pastga).
  Admin chiqargach guruh ro'yxatidan kim to'laganini belgilaydi → kassaga tushadi.
  Har to'langan kitobchadan: yuklagan o'qituvchiga 7%, pulni olgan adminga 8% (maoshga qo'shiladi).
  Narxni qo'lda ham belgilash mumkin (sotib olingan test, maxsus material): bir martalik yoki kitobga doimiy.
  «🛒 Sotib olingan material» — markaz pul to'lab olgan: muallif ulushi (7%) berilmaydi, admin ulushi qoladi.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
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
PRICERS = ("moliya", "rahbar")            # narx va «sotib olingan» belgisini faqat shular o'zgartiradi
SET_KEYS = {"sheet_price": ("1 varaq narxi (so'm)", P.SHEET_PRICE),
            "book_teacher_pct": ("O'qituvchi (muallif) ulushi, %", P.TEACHER_PCT),
            "book_admin_pct": ("Admin ulushi, %", P.ADMIN_PCT)}
ST = {"yangi": "⏳ navbatda", "chiqarildi": "✅ chiqarildi", "bekor": "❌ bekor qilindi"}


class Pr(StatesGroup):
    search = State()
    rename = State()
    file = State()
    pages = State()
    copies = State()
    note = State()
    title = State()
    subject = State()
    reason = State()
    setv = State()
    price = State()


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
    rows = [[IB(text="📄 Ichki hujjat (bepul)", callback_data="chp:in")],
            [IB(text="📚 Kitobcha chiqarish (sotuv)", callback_data="chp:bk")],
            [IB(text="📂 Mening so'rovlarim", callback_data="chp:my")]]
    if r in ("admin", "rahbar"):
        rows.append([IB(text=f"🗂 Chop navbati · {q}", callback_data="chp:q"),
                     IB(text="🖥 Printer dasturi", callback_data="chp:ag")])
    if r in CASHIERS:
        rows.append([IB(text="💵 Kitobcha to'lovlari", callback_data="chp:pay")])
    if r == "moliya":
        rows.append([IB(text="📚 Kutubxona — kitob narxlari", callback_data="chp:lib")])
    if r == "rahbar":
        rows.append([IB(text="📊 Hisobot", callback_data="chp:rep"), IB(text="⚙️ Narx va ulush", callback_data="chp:set")])
    await m.answer("🖨 <b>Chop etish</b>\n\n📄 <b>Ichki hujjat</b> — o'zingizga yoki o'quvchilarga kerak hujjat, bepul.\n"
                   "📚 <b>Kitobcha</b> — o'quvchilarga sotiladigan qo'llanma: narxni bot hisoblaydi, "
                   "pulni admin yig'adi.", reply_markup=IM(inline_keyboard=rows))


# ---------------- fayl qabul qilish (ichki va yangi kitob) ----------------
@router.callback_query(F.data == "chp:in")
async def in_start(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(kind="ichki")
    await state.set_state(Pr.file)
    await c.answer()
    await c.message.answer("📄 Chiqariladigan faylni yuboring — <b>PDF</b>, Word yoki rasm.\n"
                           "<i>PDF bo'lsa sahifalarni bot o'zi sanaydi.</i>")


@router.callback_query(F.data == "chp:bk")
async def bk_start(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await c.answer()
    await c.message.answer("📚 <b>Kitobcha</b>", reply_markup=IM(inline_keyboard=[
        [IB(text="📚 Kutubxonadan tanlash", callback_data="chp:lib")],
        [IB(text="➕ Yangi kitob yuborish", callback_data="chp:new")]]))


@router.callback_query(F.data == "chp:new")
async def bk_new(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(kind="kitob")
    await state.set_state(Pr.file)
    await c.answer()
    await c.message.answer("📚 Kitob faylini yuboring (<b>PDF</b> tavsiya etiladi — sahifalarni bot o'zi sanaydi).")


@router.message(Pr.file, F.document | F.photo)
async def got_file(m: Message, state: FSMContext, bot: Bot) -> None:
    if (await state.get_data()).get("mode") == "lib":
        return await _lib_collect(m, state, bot)
    pages = None
    if m.photo:
        fid, kind, pages = m.photo[-1].file_id, "photo", 1
    else:
        fid, kind = m.document.file_id, "document"
        name = (m.document.file_name or "").lower()
        if name.endswith(".pdf") or m.document.mime_type == "application/pdf":
            kind = "pdf"
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
    if (await state.get_data()).get("mode") == "lib":
        return await m.answer("📚 Kitob PDF fayllarini yuboring (bir nechtasini birdaniga ham bo'ladi) yoki tugating:",
                              reply_markup=IM(inline_keyboard=[[IB(text="✅ Tugatdim", callback_data="chp:lib")]]))
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
    if d.get("book_id"):                                   # kutubxonadan — fan va nom bor
        return await _ask_group(c.message, state)
    # kitob: fan tanlash
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        subs = await _subjects(s, st)
    if d.get("last_subj"):
        subs = [d["last_subj"]] + [x for x in subs if x != d["last_subj"]]
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
    if d.get("mode") == "lib":
        return await _lib_save_batch(c.message, state, d["subs"][i])
    await state.update_data(subject=d["subs"][i])
    await _ask_title(c.message, state)


@router.message(Pr.subject, F.text, ~F.text.in_(K.menu_texts()))
async def typed_subject(m: Message, state: FSMContext) -> None:
    if (await state.get_data()).get("mode") == "lib":
        return await _lib_save_batch(m, state, m.text.strip()[:60].capitalize())
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
async def _lib_view(msg: Message, state: FSMContext) -> None:
    async with Session() as s:
        rows = (await s.execute(select(PrintBook.subject, func.count(PrintBook.id)).where(
            PrintBook.saved, PrintBook.active).group_by(PrintBook.subject).order_by(PrintBook.subject))).all()
    add = [IB(text="➕ Kitob qo'shish", callback_data="chp:libadd"), IB(text="🔎 Qidirish", callback_data="chp:libq")]
    if not rows:
        return await msg.answer("📚 <b>Kutubxona</b> hozircha bo'sh.\n\nDoimiy ishlatadigan kitoblaringizni "
                                "«➕ Kitob qo'shish» orqali yuklang — ular fanlar bo'yicha joylashadi va kerak bo'lganda "
                                "faylni qayta yubormay chop ettirasiz.", reply_markup=IM(inline_keyboard=[add[:1]]))
    await state.update_data(libsubs=[r[0] for r in rows])
    total = sum(n for _, n in rows)
    await msg.answer(f"📚 <b>Kutubxona</b> — {total} ta kitob, {len(rows)} ta fan.\nFanni tanlang:",
                     reply_markup=IM(inline_keyboard=_rows(
                         [IB(text=f"{sub} · {n}", callback_data=f"prls:{i}") for i, (sub, n) in enumerate(rows)])
                         + [add]))


@router.message(F.text == K.B_LIBRARY)
async def lib_menu(m: Message, state: FSMContext) -> None:
    await state.clear()
    async with Session() as s:
        if not await _me(s, m.from_user.id):
            return
    await _lib_view(m, state)


@router.callback_query(F.data == "chp:lib")
async def lib(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    await state.clear()
    await state.update_data(last_subj=d.get("last_subj"))
    await c.answer()
    await _lib_view(c.message, state)


@router.callback_query(F.data == "chp:libadd")
async def lib_add(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    await state.clear()
    await state.update_data(mode="lib", kind="kitob", last_subj=d.get("last_subj"), queue=[])
    await state.set_state(Pr.file)
    await c.answer()
    await c.message.answer("➕ <b>Kutubxonaga kitob qo'shish</b>\n\nKitoblarning <b>PDF</b> fayllarini yuboring — "
                           "bittadan yoki bir nechtasini birdaniga. Nomi fayl nomidan olinadi, faqat fanini so'rayman.\n"
                           "<i>Keyin ham to'xtovsiz yuboraverishingiz mumkin.</i>")


_LOCK: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_GEN: dict[int, int] = {}


def _strip_name(title: str, mine: set[str]) -> str:
    """Yuklovchining «Familiya Ism» juftligi nom ichida qayerda bo'lsa ham olib tashlanadi."""
    w = title.split()
    out, k = [], 0
    while k < len(w):
        if k + 1 < len(w) and w[k].lower() in mine and w[k + 1].lower() in mine:
            k += 2
            continue
        out.append(w[k])
        k += 1
    while len(out) > 1 and out[-1].lower() in mine:
        out.pop()
    return " ".join(out) or title


def _clean_title(fname: str | None) -> str:
    base = (fname or "Kitob").rsplit(".", 1)[0].replace("_", " ").replace("-", " ")
    return " ".join(base.split())[:150] or "Kitob"


async def _lib_collect(m: Message, state: FSMContext, bot: Bot) -> None:
    """Kutubxona: PDF lar navbatga yig'iladi; oxirgi fayldan 2 soniya o'tib bitta umumiy savol (fan) beriladi."""
    doc = m.document
    if not doc or not ((doc.file_name or "").lower().endswith(".pdf") or doc.mime_type == "application/pdf"):
        return await m.answer("Kutubxonaga faqat <b>PDF</b> kitoblar qo'shiladi (Word faylni PDF qilib saqlab yuboring) — "
                              "printer ularni o'zi chiqara oladi.")
    pages = None
    if (doc.file_size or 0) <= 19 * 1024 * 1024:
        try:
            pages = P.pdf_pages((await bot.download(doc.file_id)).read())
        except Exception:
            pages = None
    uid = m.from_user.id
    async with _LOCK[uid]:
        d = await state.get_data()
        q = list(d.get("queue") or [])
        q.append({"file_id": doc.file_id, "fname": doc.file_name, "pages": pages or 0})
        await state.update_data(queue=q)
        _GEN[uid] = gen = _GEN.get(uid, 0) + 1
    await asyncio.sleep(2)
    if _GEN.get(uid) != gen:
        return
    d = await state.get_data()
    q = d.get("queue") or []
    if not q:
        return
    async with Session() as s:
        st = await _me(s, uid)
        subs = await _subjects(s, st)
    if d.get("last_subj"):
        subs = [d["last_subj"]] + [x for x in subs if x != d["last_subj"]]
    await state.update_data(subs=subs)
    lines = "\n".join(f"• {_clean_title(x['fname'])} — {x['pages'] or '?'} bet" for x in q)
    await m.answer(f"📥 <b>{len(q)} ta kitob</b> qabul qilindi:\n{lines}\n\nQaysi fan bo'limiga qo'yilsin?",
                   reply_markup=IM(inline_keyboard=_rows([IB(text=x, callback_data=f"prf:{i}") for i, x in enumerate(subs)])
                                   + [[IB(text="✏️ Boshqa fan", callback_data="prf:-1"),
                                       IB(text="❌ Bekor", callback_data="chp:libno")]]))


async def _lib_save_batch(msg: Message, state: FSMContext, subject: str) -> None:
    d = await state.get_data()
    q = d.get("queue") or []
    if not q:
        return await msg.answer("Bu kitoblar allaqachon saqlangan.")
    saved, dups = [], 0
    async with Session() as s:
        st = await _me(s, msg.chat.id)
        mine = {w.lower() for w in (st.full_name.split() if st else [])}
        for x in q:
            title = _clean_title(x["fname"])
            title = _strip_name(title, mine)
            if (await s.execute(select(PrintBook.id).where(
                    PrintBook.saved, PrintBook.active, PrintBook.subject == subject,
                    func.lower(PrintBook.title) == title.lower()))).first():
                dups += 1
            b = PrintBook(subject=subject, title=title, pages=x["pages"] or 1, layout="buklet", file_id=x["file_id"],
                          file_kind="pdf", owner_id=st.id if st else None, saved=True)
            s.add(b)
            saved.append(b)
        await S.audit(s, st, "kitob_qoshildi", n=len(saved), subject=subject)
        await s.commit()
    await state.update_data(queue=[], last_subj=subject, mode="lib", kind="kitob")
    await state.set_state(Pr.file)
    lines = "\n".join(f"• {b.title} — {b.pages} bet" for b in saved)
    await msg.answer(f"✅ <b>{len(saved)} ta kitob</b> 📚 <b>{subject}</b> bo'limiga qo'shildi:\n{lines}"
                     + (f"\n⚠️ {dups} tasi shu nom bilan avval ham bor edi — keraksizini o'chirib qo'ying." if dups else "")
                     + "\n\nYana PDF yuborsangiz, ular ham qo'shiladi. Nomini o'zgartirish — kitob kartasidan.",
                     reply_markup=IM(inline_keyboard=[[IB(text="✅ Tugatdim — kutubxonani ochish", callback_data="chp:lib")]]))


@router.callback_query(F.data == "chp:libno")
async def lib_cancel(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(queue=[])
    await c.answer("Bekor qilindi")
    await c.message.edit_text("❌ Bu fayllar qo'shilmadi. Boshqa PDF yuborishingiz yoki «📚 Kutubxona» ga qaytishingiz mumkin.")


@router.message(StateFilter(None), F.document.file_name.lower().endswith(".pdf"))
async def stray_pdf(m: Message, state: FSMContext, bot: Bot) -> None:
    """Xodim hech qanday jarayonsiz PDF yuborsa — kutubxonaga qo'shishni taklif qiladi."""
    async with Session() as s:
        st = await _me(s, m.from_user.id)
    if not st or _role(st) not in ("oqituvchi", "yordamchi", "rahbar", "admin"):
        return
    d = await state.get_data()
    await state.update_data(mode="lib", kind="kitob", last_subj=d.get("last_subj"), queue=[])
    await state.set_state(Pr.file)
    await _lib_collect(m, state, bot)


@router.callback_query(F.data == "chp:libq")
async def lib_search(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Pr.search)
    await c.answer()
    await c.message.answer("🔎 Kitob nomidan bir so'z yozing (masalan: algebra):")


@router.message(Pr.search, F.text, ~F.text.in_(K.menu_texts()))
async def lib_search_do(m: Message, state: FSMContext) -> None:
    await state.set_state(None)
    q = m.text.strip()[:50]
    async with Session() as s:
        books = (await s.execute(select(PrintBook).where(
            PrintBook.saved, PrintBook.active, func.lower(PrintBook.title).contains(q.lower()))
            .order_by(PrintBook.subject, PrintBook.title).limit(20))).scalars().all()
    if not books:
        return await m.answer(f"«{q}» bo'yicha kitob topilmadi.", reply_markup=IM(inline_keyboard=[
            [IB(text="🔎 Qayta qidirish", callback_data="chp:libq"), IB(text="📚 Kutubxona", callback_data="chp:lib")]]))
    await m.answer(f"🔎 «{q}» — {len(books)} ta:", reply_markup=IM(inline_keyboard=[
        [IB(text=f"{b.subject}: {b.title[:36]} · {b.pages} bet", callback_data=f"prb:{b.id}")] for b in books]))


@router.callback_query(F.data.startswith("prls:"))
async def lib_subject(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    subs = d.get("libsubs") or []
    i = int(c.data.split(":")[1])
    if i >= len(subs):
        return await c.answer("Qaytadan oching.", show_alert=True)
    await c.answer()
    await _subject_view(c, subs[i], i, delete=False)


async def _subject_view(c: CallbackQuery, subject: str, i: int, delete: bool) -> None:
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        books = (await s.execute(select(PrintBook).where(PrintBook.saved, PrintBook.active,
                                                         PrintBook.subject == subject).order_by(PrintBook.title))).scalars().all()
    boss = bool(st and _role(st) == "rahbar")
    if delete and boss:
        head = f"🗑 <b>{subject}</b> — o'chiriladigan kitobni bosing:"
        rows = [[IB(text=f"🗑 {b.title[:42]} · {b.pages} bet", callback_data=f"prbd:{b.id}:{i}")] for b in books]
        rows.append([IB(text="✅ Tayyor", callback_data=f"prls:{i}")])
    else:
        head = f"📚 <b>{subject}</b> — kitobni tanlang:" if books else f"📚 <b>{subject}</b> — bu fanda kitob qolmadi."
        rows = [[IB(text=f"{b.title[:45]} · {b.pages} bet", callback_data=f"prb:{b.id}")] for b in books]
        if boss and books:
            rows.append([IB(text="🗑 Kitob o'chirish", callback_data=f"prdel:{i}")])
        rows.append([IB(text="⬅️ Fanlar", callback_data="chp:lib")])
    try:
        await c.message.edit_text(head, reply_markup=IM(inline_keyboard=rows))
    except Exception:
        await c.message.answer(head, reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data.startswith("prdel:"))
async def lib_delete_mode(c: CallbackQuery, state: FSMContext) -> None:
    subs = (await state.get_data()).get("libsubs") or []
    i = int(c.data.split(":")[1])
    if i >= len(subs):
        return await c.answer("Qaytadan oching.", show_alert=True)
    await c.answer()
    await _subject_view(c, subs[i], i, delete=True)


@router.callback_query(F.data.startswith("prb:"))
async def book_card(c: CallbackQuery) -> None:
    bid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        b = await s.get(PrintBook, bid)
        if not b or not b.active:
            return await c.answer("Kitob topilmadi.", show_alert=True)
        owner = await s.get(Staff, b.owner_id) if b.owner_id else None
        used = (await s.execute(select(func.coalesce(func.sum(PrintJob.copies), 0)).where(
            PrintJob.book_id == b.id, PrintJob.status == "chiqarildi"))).scalar()
    rows = [[IB(text="🖨 Chop ettirish", callback_data=f"prbo:{b.id}")],
            [IB(text="📎 Faylni ko'rish", callback_data=f"prbf:{b.id}")]]
    if st and (st.id == b.owner_id or _role(st) == "rahbar"):
        rows.append([IB(text="✏️ Nomini o'zgartirish", callback_data=f"prbr:{b.id}"),
                     IB(text="📂 Fanini o'zgartirish", callback_data=f"prbs:{b.id}")])
        if _role(st) == "rahbar":
            rows.append([IB(text="🗑 Kutubxonadan olib tashlash", callback_data=f"prbd:{b.id}:-1")])
    await c.answer()
    if st and _role(st) in PRICERS:
        rows.append([IB(text="💰 Narx", callback_data=f"prbp:{b.id}"),
                     IB(text=f"{'✅' if b.bought else '▫️'} 🛒 Sotib olingan", callback_data=f"prbb:{b.id}")])
    async with Session() as s:
        unit = int(await num(s, "sheet_price"))
    auto = P.price(P.sheets(b.pages, b.layout), unit)
    await c.message.answer(f"📗 <b>{b.title}</b>\n📚 {b.subject} · {b.pages} bet\n"
                           + (f"💰 Narx: <b>{S.fmt_som(b.price)}</b> (belgilangan; varaq hisobida {S.fmt_som(auto)})\n"
                              if b.price is not None else f"💰 Narx: {S.fmt_som(auto)} (varaq hisobida)\n")
                           + ("🛒 Sotib olingan material — muallif ulushi yo'q\n" if b.bought else "")
                           + f"Yuklagan: {owner.full_name if owner else '—'} · {used} nusxa chiqarilgan",
                           reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data.startswith("prbp:"))
async def book_price(c: CallbackQuery, state: FSMContext) -> None:
    bid = int(c.data.split(":")[1])
    async with Session() as s:
        st, b = await _pricer(s, c.from_user.id, bid)
        if not b:
            return await c.answer("Narxni faqat moliya yoki rahbar belgilaydi.", show_alert=True)
        unit = int(await num(s, "sheet_price"))
    await state.clear()
    await state.set_state(Pr.price)
    await state.update_data(book_price=bid)
    await c.answer()
    await c.message.answer(f"💰 <b>{b.title}</b> — 1 dona narxini yozing (so'm).\n"
                           f"Varaq hisobida: {S.fmt_som(P.price(P.sheets(b.pages, b.layout), unit))}. "
                           "Avtomatik hisobga qaytarish uchun — <code>avto</code>")


async def _book_price_set(m: Message, state: FSMContext, v: int | None) -> None:
    bid = (await state.get_data()).get("book_price")
    await state.clear()
    async with Session() as s:
        st, b = await _pricer(s, m.from_user.id, bid)
        if not b:
            return
        b.price = v
        await S.audit(s, st, "kitob_narx", book=b.id, price=v)
        await s.commit()
    await m.answer(f"✅ <b>{b.title}</b>: narx " + (S.fmt_som(v) if v is not None else "avtomatik (varaq hisobida)"),
                   reply_markup=IM(inline_keyboard=[[IB(text="📗 Kitob", callback_data=f"prb:{bid}")]]))


@router.callback_query(F.data.startswith("prbb:"))
async def book_bought(c: CallbackQuery) -> None:
    bid = int(c.data.split(":")[1])
    async with Session() as s:
        st, b = await _pricer(s, c.from_user.id, bid)
        if not b:
            return await c.answer("Faqat moliya yoki rahbar.", show_alert=True)
        b.bought = not b.bought
        await S.audit(s, st, "kitob_sotib_olingan", book=b.id, v=b.bought)
        await s.commit()
    await c.answer("🛒 Sotib olingan — muallif ulushi berilmaydi" if b.bought else "Muallif ulushi qaytarildi")


async def _pricer(s, tg: int, bid: int) -> tuple[Staff | None, PrintBook | None]:
    st = await _me(s, tg)
    b = await s.get(PrintBook, bid)
    if not b or not st or _role(st) not in PRICERS:
        return None, None
    return st, b


async def _can_edit(s, tg: int, bid: int) -> tuple[Staff | None, PrintBook | None]:
    st = await _me(s, tg)
    b = await s.get(PrintBook, bid)
    if not b or not st or (st.id != b.owner_id and _role(st) != "rahbar"):
        return None, None
    return st, b


@router.callback_query(F.data.startswith("prbr:"))
async def book_rename(c: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        st, b = await _can_edit(s, c.from_user.id, int(c.data.split(":")[1]))
    if not b:
        return await c.answer("Faqat kitob muallifi yoki rahbar.", show_alert=True)
    await state.set_state(Pr.rename)
    await state.update_data(bid=b.id)
    await c.answer()
    await c.message.answer(f"«{b.title}» — yangi nomini yozing:")


@router.message(Pr.rename, F.text, ~F.text.in_(K.menu_texts()))
async def book_rename_save(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        st, b = await _can_edit(s, m.from_user.id, d["bid"])
        if not b:
            return
        b.title = m.text.strip()[:150]
        await S.audit(s, st, "kitob_nomi", id=b.id, title=b.title)
        await s.commit()
    await m.answer(f"✅ Nomi o'zgardi: <b>{b.title}</b>",
                   reply_markup=IM(inline_keyboard=[[IB(text="📗 Kitobni ochish", callback_data=f"prb:{b.id}")]]))


@router.callback_query(F.data.startswith("prbs:"))
async def book_subject(c: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        st, b = await _can_edit(s, c.from_user.id, int(c.data.split(":")[1]))
        if not b:
            return await c.answer("Faqat kitob muallifi yoki rahbar.", show_alert=True)
        subs = [x for x in await _subjects(s, st) if x != b.subject]
    await state.update_data(mvsubs=subs)
    await c.answer()
    await c.message.answer(f"«{b.title}» qaysi fanga o'tkazilsin?", reply_markup=IM(inline_keyboard=_rows(
        [IB(text=x, callback_data=f"prbm:{b.id}:{i}") for i, x in enumerate(subs)])))


@router.callback_query(F.data.startswith("prbm:"))
async def book_move(c: CallbackQuery, state: FSMContext) -> None:
    _, bid, i = c.data.split(":")
    subs = (await state.get_data()).get("mvsubs") or []
    if int(i) >= len(subs):
        return await c.answer("Qaytadan oching.", show_alert=True)
    async with Session() as s:
        st, b = await _can_edit(s, c.from_user.id, int(bid))
        if not b:
            return await c.answer("Faqat kitob muallifi yoki rahbar.", show_alert=True)
        b.subject = subs[int(i)]
        await S.audit(s, st, "kitob_fani", id=b.id, subject=b.subject)
        await s.commit()
    await c.answer("O'tkazildi ✅")
    await c.message.edit_text(f"✅ «{b.title}» → 📚 <b>{b.subject}</b>")


@router.callback_query(F.data.startswith("prbf:"))
async def book_file(c: CallbackQuery) -> None:
    async with Session() as s:
        b = await s.get(PrintBook, int(c.data.split(":")[1]))
    await c.answer()
    if b:
        await _send_file(c.bot, c.message.chat.id, b.file_id, b.file_kind, b.title)


@router.callback_query(F.data.startswith("prbd:"))
async def book_del(c: CallbackQuery) -> None:
    _, bid, i = c.data.split(":")
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        b = await s.get(PrintBook, int(bid))
    if not st or _role(st) != "rahbar":
        return await c.answer("Kitobni faqat rahbar o'chiradi.", show_alert=True)
    if not b or not b.active:
        return await c.answer("Kitob allaqachon o'chirilgan.", show_alert=True)
    await c.answer()
    back = f"prls:{i}" if i != "-1" else f"prb:{b.id}"
    await c.message.edit_text(f"🗑 <b>{b.title}</b> ({b.subject}, {b.pages} bet) kutubxonadan o'chirilsinmi?\n"
                              "<i>Avval chiqarilgan buyurtmalar va to'lovlar hisobi saqlanib qoladi.</i>",
                              reply_markup=IM(inline_keyboard=[[IB(text="✅ Ha, o'chirish", callback_data=f"prbdy:{b.id}:{i}"),
                                                                IB(text="↩️ Yo'q", callback_data=back)]]))


@router.callback_query(F.data.startswith("prbdy:"))
async def book_del_yes(c: CallbackQuery, state: FSMContext) -> None:
    _, bid, i = c.data.split(":")
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        if not st or _role(st) != "rahbar":
            return await c.answer("Kitobni faqat rahbar o'chiradi.", show_alert=True)
        b = await s.get(PrintBook, int(bid))
        if not b or not b.active:
            return await c.answer("Kitob allaqachon o'chirilgan.", show_alert=True)
        b.active = False
        await S.audit(s, st, "kitob_ochirildi", id=b.id, title=b.title)
        await s.commit()
    await c.answer("O'chirildi ✅")
    subs = (await state.get_data()).get("libsubs") or []
    if i != "-1" and int(i) < len(subs):
        return await _subject_view(c, subs[int(i)], int(i), delete=True)
    await c.message.edit_text(f"🗑 <b>{b.title}</b> kutubxonadan o'chirildi.")


@router.callback_query(F.data.startswith("prbo:"))
async def book_order(c: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        b = await s.get(PrintBook, int(c.data.split(":")[1]))
    if not b or not b.active:
        return await c.answer("Kitob topilmadi.", show_alert=True)
    await state.clear()
    await state.update_data(kind="kitob", book_id=b.id, pages=b.pages, title=b.title)
    await c.answer()
    await c.message.answer(f"🖨 <b>{b.title}</b> — {b.pages} bet")
    await _after_pages(c.message, state)


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
    if d.get("book_id") and "bought" not in d:          # kutubxona kitobi: saqlangan narx va belgi
        async with Session() as s:
            b = await s.get(PrintBook, d["book_id"])
        await state.update_data(bought=bool(b and b.bought), custom=b.price if b and b.price is not None else None,
                                keep=False)
    await _kit_confirm(msg, state)


async def _kit_confirm(msg: Message, state: FSMContext) -> None:
    d = await state.get_data()
    n = d["copies"]
    async with Session() as s:
        unit = int(await num(s, "sheet_price"))
        g = await s.get(Group, d["group_id"]) if d.get("group_id") else None
        st = await _me(s, msg.chat.id)
    can = bool(st and _role(st) in PRICERS)
    sh, auto, line = _calc_line(d["pages"], d["layout"], unit)
    pr = d["custom"] if d.get("custom") is not None else auto
    if d.get("custom") is not None:
        line = (f"{d['pages']} bet · {P.LAYOUT_SHORT[d['layout']]} → {sh} varaq\n"
                f"💰 Narx: <b>{S.fmt_som(pr)}</b> — belgilangan narx (varaq hisobida {S.fmt_som(auto)})"
                + (" · 📌 doimiy" if d.get("keep") else ""))
    kb = [[IB(text="📤 Adminga yuborish", callback_data="prok")]]
    if can:
        kb += [[IB(text="✏️ Narxni o'zgartirish", callback_data="prv:e")]
               + ([IB(text="↩️ Avto narx", callback_data="prv:a")] if d.get("custom") is not None else []),
               [IB(text=f"{'✅' if d.get('bought') else '▫️'} 🛒 Sotib olingan material (ulushsiz)",
                   callback_data="prv:b")]]
    kb.append([IB(text="❌ Bekor", callback_data="prx")])
    hint = "" if can else "\n<i>Maxsus narx kerak bo'lsa — moliya yoki rahbar kutubxonadagi kitob kartasida belgilaydi.</i>"
    warn = ("\n⚠️ <i>Narx qog'oz hisobidan arzon — markaz zarar ko'rishi mumkin.</i>"
            if 0 < pr < sh * unit * 0.6 else "")
    bought = d.get("bought")
    await msg.answer(f"📚 <b>{d['title']}</b>" + (f" · {g.name}" if g else "") + f"\n{line}\n"
                     f"{n} nusxa → {sh * n} varaq qog'oz · jami <b>{S.fmt_som(pr * n)}</b> tushum"
                     + ("\n🛒 Sotib olingan material — muallif ulushi berilmaydi" if bought else "") + warn
                     + hint + "\n\nAdminga yuboraymi?", reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("prv:"))
async def price_opts(c: CallbackQuery, state: FSMContext) -> None:
    v = c.data.split(":")[1]
    d = await state.get_data()
    if "copies" not in d or d.get("kind") != "kitob":
        return await c.answer("Jarayon eskirgan — qaytadan boshlang.", show_alert=True)
    async with Session() as s:
        st = await _me(s, c.from_user.id)
    if not st or _role(st) not in PRICERS:
        return await c.answer("Narxni faqat moliya yoki rahbar o'zgartiradi.", show_alert=True)
    await c.answer()
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if v == "e":
        async with Session() as s:
            unit = int(await num(s, "sheet_price"))
        auto = P.price(P.sheets(d["pages"], d["layout"]), unit)
        await state.set_state(Pr.price)
        return await c.message.answer(f"💰 1 dona narxini yozing (so'm). Varaq hisobida: {S.fmt_som(auto)}\n"
                                      "Masalan: <code>15000</code> · bepul tarqatish uchun — <code>0</code>")
    if v == "a":
        await state.update_data(custom=None, keep=False)
    elif v == "b":
        await state.update_data(bought=not d.get("bought"))
    elif v in ("k1", "k0"):
        await state.update_data(keep=v == "k1")
    await _kit_confirm(c.message, state)


@router.message(Pr.price, F.text, ~F.text.in_(K.menu_texts()))
async def price_typed(m: Message, state: FSMContext) -> None:
    if m.text.strip().lower() in ("avto", "auto") and (await state.get_data()).get("book_price"):
        return await _book_price_set(m, state, None)
    t = m.text.lower().replace(" ", "").replace("so'm", "").replace("som", "")
    mul = 1000 if t.endswith(("k", "ming")) else 1
    t = t.rstrip("kming").replace(",", ".")
    try:
        v = int(round(float(t) * mul))
        assert 0 <= v <= 10_000_000
    except (ValueError, AssertionError):
        return await m.answer("Summani raqamda yozing: 15000")
    if (await state.get_data()).get("book_price"):
        return await _book_price_set(m, state, v)
    await state.set_state(None)
    await state.update_data(custom=v)
    d = await state.get_data()
    if d.get("book_id") or d.get("saved"):
        return await m.answer(f"💰 {S.fmt_som(v)} — bu narx qachon amal qilsin?", reply_markup=IM(inline_keyboard=[
            [IB(text="📌 Bu kitob uchun doim shu narx", callback_data="prv:k1")],
            [IB(text="1️⃣ Faqat shu buyurtma", callback_data="prv:k0")]]))
    await _kit_confirm(m, state)


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
            auto = P.price(sh, unit)
            custom = d.get("custom")
            job.book_id, job.file_id, job.file_kind = b.id, b.file_id, b.file_kind
            job.price = custom if custom is not None else auto
            job.auto_price = auto if custom is not None else None
            job.bought = bool(d.get("bought"))
            if b.saved:
                b.bought = job.bought                    # material xususiyati — kitobda saqlanadi
                if d.get("keep") and custom is not None:
                    b.price = custom
                elif custom is None and b.price is not None and not d.get("book_id"):
                    b.price = None
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
               f"💰 Narx: <b>{S.fmt_som(job.price)}</b> (dona)"
               + (f" — qo'lda belgilangan (varaq hisobida {S.fmt_som(job.auto_price)})" if job.auto_price is not None else "")
               + ("\n🛒 Sotib olingan material — muallif ulushi yo'q" if job.bought else "") + "\n"
               f"🖨 {job.copies} nusxa = {total} varaq" + (f" · {g.name}" if g else "")
               + f"\n👤 {st.full_name if st else ''}")
    else:
        cap = (f"📄 <b>Ichki hujjat №{job.id}</b> — bepul\n"
               f"{job.pages} bet · {P.LAYOUT_SHORT[job.layout]} × {job.copies} nusxa = {total} varaq\n"
               f"👤 {st.full_name if st else ''}" + (f"\n📝 {job.note}" if job.note else ""))
    kb = None
    if job.status == "yangi":
        rows = []
        if job.file_kind in ("pdf", "photo"):
            rows.append([IB(text="🖨 Printerga yuborish", callback_data=f"prp:{job.id}")])
        else:
            cap += "\n<i>Word fayl — printerga avtomatik yuborilmaydi, qo'lda chiqaring.</i>"
        rows.append([IB(text="✅ Qo'lda chiqarildi", callback_data=f"prd:{job.id}"),
                     IB(text="❌ Bekor", callback_data=f"prn:{job.id}")])
        kb = IM(inline_keyboard=rows)
    else:
        cap += f"\n\n{ST[job.status]}"
    return cap, kb


# ---------------- admin: chiqarildi / bekor ----------------
@router.callback_query(F.data.startswith("prd:"))
async def done(c: CallbackQuery, bot: Bot) -> None:
    jid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _me(s, c.from_user.id)
        if not st or _role(st) not in ("admin", "rahbar"):
            return await c.answer("Faqat admin.", show_alert=True)
    ok = await finalize(bot, jid, st.id, c.message.chat.id)
    if not ok:
        return await c.answer("Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
    await c.answer("Qayd etildi ✅")
    await _stamp(c.message, f"✅ Chiqarildi — {st.full_name}")


async def _stamp(msg: Message, line: str) -> None:
    try:
        await msg.edit_caption(caption=(msg.html_text or "") + f"\n\n{line}")
    except Exception:
        try:
            await msg.edit_text((msg.html_text or "") + f"\n\n{line}")
        except Exception:
            pass


async def finalize(bot: Bot, jid: int, staff_id: int, chat: int | None) -> bool:
    """So'rov chiqarildi: ichki — qog'oz hisobga; kitob — to'lov ro'yxati ochiladi; so'raganga xabar."""
    async with Session() as s:
        job = await s.get(PrintJob, jid)
        st = await s.get(Staff, staff_id)
        if not job or job.status != "yangi":
            return False
        job.status, job.done_by, job.done_at = "chiqarildi", staff_id, datetime.utcnow()
        req = await s.get(Staff, job.staff_id) if job.staff_id else None
        if job.kind == "ichki":
            teach = req and any(r in (req.roles or "") for r in ("oqituvchi", "yordamchi"))
            s.add(PrinterUse(category="oqituvchi" if teach else "markaz", pages=job.sheets * job.copies,
                             note=(f"So'rov №{job.id}" + (f": {job.note}" if job.note else ""))[:200],
                             staff_id=job.staff_id, by_id=staff_id))
        await S.audit(s, st, "chop_chiqarildi", id=job.id, kind=job.kind, sheets=job.sheets * job.copies)
        await s.commit()
        b = await s.get(PrintBook, job.book_id) if job.book_id else None
        pay = await pay_card(s, job) if job.kind == "kitob" else None
    if req and req.tg_id:
        if job.kind == "kitob":
            await send(bot, req.tg_id, f"✅ <b>{b.title}</b> — {job.copies} nusxa tayyor (so'rov №{job.id}).\n"
                                       f"O'quvchilar adminga {S.fmt_som(job.price)} dan to'laydi. Kim to'lagani — "
                                       "«🖨 Chop etish» → «📂 Mening so'rovlarim».")
        else:
            await send(bot, req.tg_id, f"✅ Hujjatingiz tayyor (so'rov №{job.id}, {job.copies} nusxa). Admindan oling.")
    if pay and chat:
        await send(bot, chat, pay[0], reply_markup=pay[1])
    return True


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


@router.callback_query(F.data == "chp:q")
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


@router.callback_query(F.data == "chp:pay")
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
                          job_id=job.id, student_id=sid, owner_id=None if job.bought else b.owner_id))
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
@router.callback_query(F.data == "chp:my")
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
@router.callback_query(F.data == "chp:rep")
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


@router.callback_query(F.data == "chp:set")
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


# ======================= PRINTER DASTURI (admin kompyuteri) =======================
from app.agent_api import agent_state, new_install, online, save_state  # noqa: E402
from app.config import settings  # noqa: E402
from app.db.models import PrintLog, PrintTask  # noqa: E402

LOG_ST = {"ichki": "📄 ichki hujjat", "sotuv": "💰 sotuv", "isrof": "🗑 isrof / xato", "kiritilgan": "✔️ avval kiritilgan"}


def _two_sided(job: PrintJob) -> bool:
    return (job.layout == "buklet" and job.pages > 2) or (job.layout == "ikki" and job.pages > 1)


async def _printer_pick(s) -> tuple[dict, list[str]]:
    st = await agent_state(s)
    names = list(st.get("printers") or [])
    names.sort(key=lambda n: 0 if any(k in n.lower() for k in ("2900", "lbp", "canon")) else 1)
    return st, names


@router.callback_query(F.data.startswith("prp:"))
async def to_printer(c: CallbackQuery) -> None:
    jid = int(c.data.split(":")[1])
    async with Session() as s:
        me = await _me(s, c.from_user.id)
        if not me or _role(me) not in ("admin", "rahbar"):
            return await c.answer("Faqat admin.", show_alert=True)
        st, names = await _printer_pick(s)
    if not names:
        return await c.answer("Printer dasturi hali o'rnatilmagan yoki printerlar topilmadi. «🖨 Chop etish» → "
                              "«🖥 Printer dasturi».", show_alert=True)
    await c.answer()
    if len(names) == 1:
        return await _queue(c, jid, names[0])
    await c.message.answer("Qaysi printerdan chiqarilsin?", reply_markup=IM(inline_keyboard=[
        [IB(text=("⚫ " if i == 0 else "🌈 ") + n[:40], callback_data=f"prpp:{jid}:{i}")] for i, n in enumerate(names)]))


@router.callback_query(F.data.startswith("prpp:"))
async def to_printer_pick(c: CallbackQuery) -> None:
    _, jid, i = c.data.split(":")
    async with Session() as s:
        _, names = await _printer_pick(s)
    if int(i) >= len(names):
        return await c.answer("Ro'yxat o'zgardi — qaytadan bosing.", show_alert=True)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    await _queue(c, int(jid), names[int(i)])


async def _queue(c: CallbackQuery, jid: int, printer: str) -> None:
    async with Session() as s:
        me = await _me(s, c.from_user.id)
        job = await s.get(PrintJob, jid)
        if not job or job.status != "yangi":
            return await c.message.answer("Bu so'rov allaqachon ko'rib chiqilgan.")
        busy = (await s.execute(select(PrintTask.id).where(PrintTask.job_id == jid,
                                                           PrintTask.status.in_(("navbat", "yuborildi"))))).first()
        if busy:
            return await c.message.answer("Bu so'rov allaqachon printer navbatida.")
        st = await agent_state(s)
        two = _two_sided(job)
        if two and printer not in (st.get("cal") or {}):
            return await c.message.answer(
                f"«{printer}» uchun hali <b>ikki tomon sinovi</b> o'tkazilmagan. Bu bir martalik: bot 2 varaq "
                "chiqaradi va varaqlarni qanday qayta solish kerakligini aniqlaydi.",
                reply_markup=IM(inline_keyboard=[[IB(text="🧪 Sinovni boshlash", callback_data="prt:" + _pidx(st, printer))]]))
        s.add(PrintTask(job_id=jid, side="front" if two else "all", printer=printer, by_id=me.id))
        await s.commit()
        on = online(st)
    await c.message.answer(
        f"🖨 So'rov №{jid} printerga yuborildi ({printer})."
        + (" Avval 1-tomon chiqadi, keyin bot varaqlarni qayta solishni aytadi." if two else "")
        + ("" if on else "\n⚠️ Kompyuter yoki printer dasturi hozir o'chiq — yoqilishi bilan o'zi chiqadi."))


def _pidx(st: dict, printer: str) -> str:
    names = list(st.get("printers") or [])
    return str(names.index(printer)) if printer in names else "0"


async def on_task_result(bot: Bot, tid: int) -> None:
    """Agent topshiriqni bajardi (yoki xato) — adminga keyingi qadam."""
    async with Session() as s:
        t = await s.get(PrintTask, tid)
        if not t:
            return
        me = await s.get(Staff, t.by_id) if t.by_id else None
        chat = me.tg_id if me else None
        job = await s.get(PrintJob, t.job_id) if t.job_id else None
        st = await agent_state(s)
        pi = _pidx(st, t.printer)
    if not chat:
        return
    if t.status == "xato":
        return await send(bot, chat, f"⚠️ <b>Printer xatosi</b> ({t.printer})\n{t.error}\n\nPrinter yoqilganini, qog'oz "
                                     "borligini tekshiring.", reply_markup=IM(inline_keyboard=[
                                         [IB(text="🔁 Qayta yuborish", callback_data=f"prre:{t.id}")]]
                                         + ([[IB(text="✅ Qo'lda chiqarildi", callback_data=f"prd:{job.id}")]] if job else [])))
    if t.status != "tayyor":
        return
    if job is None:                                          # sinov varag'i
        if t.side == "front":
            return await send(bot, chat, "🧪 <b>Sinov: 1-qadam tayyor</b> — 2 varaq chiqdi.\n\n"
                                         "Endi chiqqan 2 varaqni <b>tartibini o'zgartirmay</b>, toza tomoni chop "
                                         "etiladigan qilib lotokka qayta soling. Qanday solganingizni eslab qoling — "
                                         "keyin har doim shunday qilasiz.",
                              reply_markup=IM(inline_keyboard=[[IB(text="▶️ Orqa tomonni chiqarish",
                                                                   callback_data=f"prt2:{pi}")]]))
        return await send(bot, chat, "🧪 <b>Sinov: tekshiramiz</b>\n<b>1-VARAQ OLD</b> yozilgan varaqni oling. "
                                     "Uning orqasida nima yozilgan?", reply_markup=IM(inline_keyboard=[
                                         [IB(text="«1-VARAQ ORQA»", callback_data=f"prtq:{pi}:0")],
                                         [IB(text="«2-VARAQ ORQA»", callback_data=f"prtq:{pi}:1")],
                                         [IB(text="Hech narsa / boshqa", callback_data=f"prtq:{pi}:x")]]))
    if t.side == "front":
        return await send(bot, chat, f"🖨 So'rov №{job.id}: <b>1-tomon chiqdi</b> ({t.pages or ''} varaq).\n\n"
                                     "Varaqlarni <b>sinovdagidek</b> qayta soling (tartibini o'zgartirmay), so'ng "
                                     "bosing:", reply_markup=IM(inline_keyboard=[
                                         [IB(text="▶️ 2-tomonni chiqarish", callback_data=f"prb2:{t.id}")],
                                         [IB(text="⛔ To'xtatish", callback_data=f"prbx:{t.id}")]]))
    ok = await finalize(bot, job.id, t.by_id, chat)
    if ok:
        await send(bot, chat, f"✅ So'rov №{job.id} chop etildi va hisobga yozildi.")


@router.callback_query(F.data.startswith("prb2:"))
async def back_side(c: CallbackQuery) -> None:
    async with Session() as s:
        t = await s.get(PrintTask, int(c.data.split(":")[1]))
        if not t or t.side != "front":
            return await c.answer()
        if (await s.execute(select(PrintTask.id).where(PrintTask.job_id == t.job_id, PrintTask.side == "back",
                                                       PrintTask.status != "xato"))).first():
            return await c.answer("2-tomon allaqachon yuborilgan.", show_alert=True)
        s.add(PrintTask(job_id=t.job_id, side="back", printer=t.printer, by_id=t.by_id))
        await s.commit()
    await c.answer("Yuborildi")
    await c.message.edit_text(c.message.html_text + "\n\n▶️ 2-tomon printerga yuborildi.")


@router.callback_query(F.data.startswith("prbx:"))
async def back_stop(c: CallbackQuery) -> None:
    await c.answer()
    await c.message.edit_text(c.message.html_text + "\n\n⛔ To'xtatildi. So'rov navbatda qoldi — qayta "
                              "«🖨 Printerga yuborish» yoki «✅ Qo'lda chiqarildi».")


@router.callback_query(F.data.startswith("prre:"))
async def retry(c: CallbackQuery) -> None:
    async with Session() as s:
        t = await s.get(PrintTask, int(c.data.split(":")[1]))
        if not t or t.status != "xato":
            return await c.answer()
        s.add(PrintTask(job_id=t.job_id, side=t.side, printer=t.printer, by_id=t.by_id))
        t.status = "bekor"
        await s.commit()
    await c.answer("Qayta yuborildi")
    await c.message.edit_text(c.message.html_text + "\n\n🔁 Qayta yuborildi.")


# ---------------- ikki tomon sinovi (kalibrovka) ----------------
async def _pname(s, i: str) -> str | None:
    st = await agent_state(s)
    names = list(st.get("printers") or [])
    return names[int(i)] if i.isdigit() and int(i) < len(names) else None


@router.callback_query(F.data.startswith("prt:"))
async def cal_start(c: CallbackQuery) -> None:
    async with Session() as s:
        me = await _me(s, c.from_user.id)
        name = await _pname(s, c.data.split(":")[1])
        if not me or not name:
            return await c.answer("Printer topilmadi.", show_alert=True)
        s.add(PrintTask(job_id=None, side="front", printer=name, by_id=me.id))
        await s.commit()
    await c.answer()
    await c.message.answer(f"🧪 <b>Ikki tomon sinovi</b> — {name}\nPrinterga 2 ta toza varaq qo'ying. "
                           "1-qadam yuborildi: «OLD TOMON» yozilgan 2 varaq chiqadi.")


@router.callback_query(F.data.startswith("prt2:"))
async def cal_back(c: CallbackQuery) -> None:
    async with Session() as s:
        me = await _me(s, c.from_user.id)
        name = await _pname(s, c.data.split(":")[1])
        if not me or not name:
            return await c.answer("Printer topilmadi.", show_alert=True)
        s.add(PrintTask(job_id=None, side="back", printer=name, by_id=me.id))
        await s.commit()
    await c.answer("Yuborildi")
    await c.message.edit_text(c.message.html_text + "\n\n▶️ Orqa tomon yuborildi.")


@router.callback_query(F.data.startswith("prtq:"))
async def cal_q1(c: CallbackQuery) -> None:
    _, i, a = c.data.split(":")
    await c.answer()
    if a == "x":
        return await c.message.edit_text("🧪 Varaqlar noto'g'ri solingan ko'rinadi. Sinovni qaytadan o'tkazing: "
                                         "varaqlarni <b>toza tomoni chop etiladigan</b> qilib soling.",
                                         reply_markup=IM(inline_keyboard=[[IB(text="🧪 Qaytadan", callback_data=f"prt:{i}")]]))
    await c.message.edit_text("🧪 Endi shu varaqning ikkala tomonidagi <b>strelkalarni</b> solishtiring "
                              "(varaqni chap-o'ngga ag'darib qarang). Ikkalasi ham yuqoriga qaraydimi?",
                              reply_markup=IM(inline_keyboard=[
                                  [IB(text="✅ Ha, bir xil", callback_data=f"prtr:{i}:{a}:0")],
                                  [IB(text="🔄 Yo'q, orqadagisi teskari", callback_data=f"prtr:{i}:{a}:1")]]))


@router.callback_query(F.data.startswith("prtr:"))
async def cal_save(c: CallbackQuery) -> None:
    _, i, rev, rot = c.data.split(":")
    async with Session() as s:
        me = await _me(s, c.from_user.id)
        name = await _pname(s, i)
        if not name:
            return await c.answer("Printer topilmadi.", show_alert=True)
        st = await agent_state(s)
        cal = dict(st.get("cal") or {})
        cal[name] = {"rev": rev == "1", "rot": rot == "1"}
        st["cal"] = cal
        await save_state(s, st)
        await S.audit(s, me, "printer_sinov", printer=name, rev=rev, rot=rot)
        await s.commit()
    await c.answer("Saqlandi ✅")
    await c.message.edit_text(f"✅ <b>{name}</b> sozlandi. Endi kitobcha va ikki tomonlama hujjatlar shu printerdan "
                              "to'g'ri chiqadi.\n\nEslatma: varaqlarni har doim sinovdagidek qayta soling.")


# ---------------- botsiz chop etilganlar ----------------
def _log_line(r: PrintLog) -> str:
    return (f"«{r.doc[:60]}» — <b>{r.pages} sahifa</b>\n{r.printer} · {(r.at + timedelta(hours=5)):%d.%m %H:%M}"
            + (f" · {r.user}" if r.user else ""))


def _log_kb(r: PrintLog) -> IM:
    return IM(inline_keyboard=[[IB(text="📄 Ichki hujjat", callback_data=f"plc:{r.id}:ichki"),
                                IB(text="💰 Sotuv (pul olindi)", callback_data=f"plc:{r.id}:sotuv")],
                               [IB(text="🗑 Isrof / xato", callback_data=f"plc:{r.id}:isrof"),
                                IB(text="✔️ Avval kiritilgan", callback_data=f"plc:{r.id}:kiritilgan")]])


async def notify_unaccounted(bot: Bot, ids: list[int]) -> None:
    async with Session() as s:
        rows = (await s.execute(select(PrintLog).where(PrintLog.id.in_(ids)).order_by(PrintLog.id))).scalars().all()
        to = [x.tg_id for x in await S.staff_with_role(s, "admin") if x.tg_id] or \
             [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    if not rows:
        return
    for tg in to:
        if len(rows) > 5:
            await send(bot, tg, f"🖨 Bot orqali o'tmagan <b>{len(rows)} ta</b> hujjat chop etildi "
                                f"({sum(r.pages for r in rows)} sahifa). Har birini belgilang:",
                       reply_markup=IM(inline_keyboard=[[IB(text="🔎 Ko'rish", callback_data="chp:un")]]))
            continue
        for r in rows:
            await send(bot, tg, "🖨 <b>Bot orqali o'tmagan chop etish</b>\n" + _log_line(r) + "\n\nBu nima edi?",
                       reply_markup=_log_kb(r))


@router.callback_query(F.data == "chp:un")
async def unaccounted(c: CallbackQuery) -> None:
    async with Session() as s:
        rows = (await s.execute(select(PrintLog).where(PrintLog.status == "yangi").order_by(PrintLog.id)
                                .limit(15))).scalars().all()
    await c.answer()
    if not rows:
        return await c.message.answer("🔎 Belgilanmagan chop etish yo'q ✅")
    for r in rows:
        await c.message.answer("🖨 " + _log_line(r) + "\nBu nima edi?", reply_markup=_log_kb(r))


@router.callback_query(F.data.startswith("plc:"))
async def classify(c: CallbackQuery) -> None:
    _, lid, kind = c.data.split(":")
    async with Session() as s:
        me = await _me(s, c.from_user.id)
        if not me or _role(me) not in ("admin", "rahbar", "moliya"):
            return await c.answer("Ruxsat yo'q.", show_alert=True)
        r = await s.get(PrintLog, int(lid))
        if not r or r.status != "yangi":
            return await c.answer("Allaqachon belgilangan.", show_alert=True)
        extra = ""
        if kind in ("ichki", "isrof"):
            s.add(PrinterUse(category="markaz" if kind == "ichki" else "isrof", pages=r.pages,
                             note=f"Jurnal: {r.doc}"[:200], by_id=me.id))
        elif kind == "sotuv":
            sv = (await s.execute(select(Service).where(Service.kind == "printer", Service.active))).scalars().first()
            if not sv:
                return await c.answer("«Printer» xizmati narxi kiritilmagan (🛒 Xizmatlar). Avval narxni kiriting.",
                                      show_alert=True)
            s.add(ServiceSale(service_id=sv.id, qty=r.pages, amount=sv.price * r.pages,
                              profit=(sv.price - (sv.cost or 0)) * r.pages, method="naqd", by_id=me.id))
            extra = f"\n💰 Kassaga: {S.fmt_som(sv.price * r.pages)} (naqd)"
        r.status, r.by_id = kind, me.id
        await S.audit(s, me, "printer_jurnal", id=r.id, kind=kind, pages=r.pages)
        await s.commit()
    await c.answer("Belgilandi ✅")
    await c.message.edit_text(c.message.html_text.replace("\nBu nima edi?", "").replace("\n\nBu nima edi?", "")
                              + f"\n\n{LOG_ST[kind]} — {me.full_name}{extra}")


# ---------------- holat va o'rnatish ----------------
@router.callback_query(F.data == "chp:ag")
async def agent_menu(c: CallbackQuery) -> None:
    async with Session() as s:
        me = await _me(s, c.from_user.id)
        st, names = await _printer_pick(s)
        un = (await s.execute(select(func.count(PrintLog.id)).where(PrintLog.status == "yangi"))).scalar()
    cal = st.get("cal") or {}
    if st.get("seen"):
        seen = datetime.fromisoformat(st["seen"]) + timedelta(hours=5)
        state = "🟢 ishlayapti" if online(st) else f"🔴 o'chiq (oxirgi marta: {seen:%d.%m %H:%M})"
    else:
        state = "⚪ hali o'rnatilmagan"
    lines = [f"🖥 <b>Printer dasturi</b> — {state}"]
    for n in names:
        lines.append(f"• {n}" + (" — ikki tomon sozlangan ✅" if n in cal else " — ikki tomon sinovi o'tkazilmagan"))
    rows = [[IB(text=f"🧪 Ikki tomon sinovi: {n[:28]}", callback_data=f"prt:{_pidx(st, n)}")] for n in names]
    rows.append([IB(text=f"🔎 Botsiz chop etilganlar · {un}", callback_data="chp:un")])
    if me and _role(me) == "rahbar":
        rows.append([IB(text="🔑 O'rnatish / qayta o'rnatish", callback_data="chp:inst")])
    await c.answer()
    await c.message.answer("\n".join(lines), reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data == "chp:inst")
async def agent_install(c: CallbackQuery, bot: Bot) -> None:
    async with Session() as s:
        me = await _me(s, c.from_user.id)
        if not me or _role(me) != "rahbar":
            return await c.answer("Faqat rahbar.", show_alert=True)
        if not settings.print_host:
            return await c.answer("Server hali sozlanmagan (PRINT_HOST). Yo'riqnomadagi server qadamini bajaring.",
                                  show_alert=True)
        _, link = await new_install(s)
        await S.audit(s, me, "printer_ornatish")
        await s.commit()
    await c.answer()
    m = await c.message.answer(
        "🔑 <b>Printer dasturini o'rnatish</b> (havola 15 daqiqa amal qiladi, bir marta ishlaydi)\n\n"
        "1. Printerlar ulangan kompyuterda <b>Start</b> → «PowerShell» deb yozing → oching.\n"
        "2. Quyidagi qatorni nusxalab, PowerShell oynasiga qo'ying va <b>Enter</b> bosing:\n\n"
        f"<code>irm {link} | iex</code>\n\n"
        "3. «Ruxsat berasizmi?» oynasi chiqsa — <b>Ha</b>.\n"
        "4. «TAYYOR» yozuvi chiqqach, oynani yoping va «🖥 Printer dasturi» ni qayta oching.\n\n"
        "⚠️ Bu xabarni hech kimga yubormang — 15 daqiqadan keyin o'zi o'chadi. Eski o'rnatish bekor bo'ldi.")

    async def _wipe():
        import asyncio
        await asyncio.sleep(15 * 60)
        try:
            await bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            pass
    import asyncio
    asyncio.create_task(_wipe())


async def daily_summary(bot: Bot) -> None:
    """Har kuni kechqurun rahbarga: printer jurnali bo'yicha bugungi chop etish."""
    now = datetime.utcnow() + timedelta(hours=5)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=5)
    async with Session() as s:
        st = await agent_state(s)
        if not st.get("seen"):
            return
        rows = (await s.execute(select(PrintLog).where(PrintLog.at >= start))).scalars().all()
        un_all = (await s.execute(select(func.count(PrintLog.id)).where(PrintLog.status == "yangi"))).scalar()
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    if not rows and not un_all:
        return
    by_p: dict[str, dict[str, int]] = {}
    for r in rows:
        d = by_p.setdefault(r.printer, {})
        d[r.status] = d.get(r.status, 0) + r.pages
    lines = [f"🖨 <b>Printer — {now:%d.%m}</b>"]
    for p, d in by_p.items():
        lines.append(f"\n<b>{p}</b>: jami {sum(d.values())} sahifa")
        lines.append(f"• bot orqali: {d.get('bot', 0)}")
        for k, v in LOG_ST.items():
            if d.get(k):
                lines.append(f"• botsiz, {v}: {d[k]}")
        if d.get("yangi"):
            lines.append(f"• ⚠️ botsiz, belgilanmagan: <b>{d['yangi']}</b>")
    if un_all:
        lines.append(f"\n⚠️ Jami belgilanmagan yozuvlar: {un_all} — admin belgilashi kerak.")
    if not online(st, 6 * 3600) and now.weekday() < 6:
        lines.append("\n🔴 Printer dasturi bugun ishlamagan (kompyuter o'chiq bo'lgan bo'lishi mumkin).")
    for b in bosses:
        await send(bot, b, "\n".join(lines))
