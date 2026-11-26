"""🎓 MOC imtihonlar va 📄 tayyor PDF testlar.

O'qituvchi o'z PDF savollarini (1 yoki bir nechta variant) va javob kalitini yuboradi. Bot har qatnashchiga
QR kodli javob varag'i tayyorlaydi (guruh o'quvchilari, tashqi qatnashchilar, bo'sh varaqlar), hammasini adminga
chop etishga yuboradi. Varaqlar skaner (jonli yoki rasm) bilan o'qiladi, yozma savollarni o'qituvchi varaqdagi
ball doirachalari bilan baholaydi, Writing/Speaking kabi bo'limlar botga yoziladi. Yakunda ball, daraja, o'rin
hisoblanadi; ota-onaga / qatnashchiga chiroyli natija kartasi, rahbarga Excel hisobot boradi.
Formatlar: app/logic/moc.py (Milliy sertifikat, DTM, CEFR, IELTS, oddiy test, o'z formati).
"""
from __future__ import annotations

import io
import secrets
from datetime import date, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton as IB,
                           InlineKeyboardMarkup as IM, Message)
from sqlalchemy import select

from app import services as S
from app.bot import keyboards as K
from app.bot.notify import send
from app.config import settings
from app.db.models import (Enrollment, Exam, ExamResult, Group, Moc, MocSeat, PrintJob, Service, ServiceSale, Setting,
                           Staff, Student)
from app.db.session import Session
from app.logic import moc as M
from app.logic import printing as P
from app.logic import sheet2 as S2

router = Router()
VARS = "ABCDEF"
MAKERS = ("rahbar", "oqituvchi", "yordamchi")
VIEWERS = MAKERS + ("admin", "moliya")
KIND_ICON = {"moc": "🎓", "nazorat": "📄", "tashqi": "🌐"}
PRICERS = ("moliya", "rahbar")              # qatnashish narxi va xarid xarajatini faqat shular kiritadi


def payers(moc: Moc, seats: list[MocSeat]) -> list[MocSeat]:
    """To'lashi kerak bo'lganlar: tashqi qatnashchilar (yoki «hamma to'laydi» bo'lsa — barcha ismlilar)."""
    if not moc.price:
        return []
    every = (moc.meta or {}).get("pay_all")
    return [z for z in seats if z.name and not z.absent and (every or not z.enrollment_id)]


class Mc(StatesGroup):
    cal = State()
    title = State()
    spec = State()
    pdf = State()
    key = State()
    date = State()
    ext = State()
    blanks = State()
    price = State()
    scan = State()
    manual = State()
    name = State()
    baho = State()
    add = State()
    extmax = State()


def now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5)


async def _staff(s, tg: int) -> Staff | None:
    return (await S.who_by_tg(s, tg)).staff


def _role(st: Staff | None) -> str:
    return (st.current_role or "") if st else ""


def _rows(btns: list[IB], n: int = 2) -> list[list[IB]]:
    return [btns[i:i + n] for i in range(0, len(btns), n)]


def _spec(moc: Moc) -> M.Spec:
    return M.parse_spec(moc.spec)


# ======================= ro'yxat va kirish =======================
async def list_view(msg: Message, tg: int, edit: bool = False) -> None:
    async with Session() as s:
        st = await _staff(s, tg)
        if not st or _role(st) not in VIEWERS:
            return
        q = select(Moc).where(Moc.status != "bekor").order_by(Moc.id.desc()).limit(12)
        mocs = (await s.execute(q)).scalars().all()
        if _role(st) in ("oqituvchi", "yordamchi"):
            mine = {g.id for g in await S.teacher_groups(s, st, _role(st))}
            mocs = [x for x in mocs if x.created_by == st.id or set(x.group_ids or []) & mine]
        rows = []
        for x in mocs:
            seats = (await s.execute(select(MocSeat).where(MocSeat.moc_id == x.id))).scalars().all()
            done = sum(1 for z in seats if z.answers or z.absent)
            done = sum(1 for z in seats if z.answers or z.absent or z.baho)
            icon = "✅" if x.status == "yakun" else {"nazorat": "📄", "tashqi": "🌐"}.get(x.kind, "🎓")
            rows.append([IB(text=f"{icon} {x.date:%d.%m} · {x.title[:28]} · {done}/{len(seats)}",
                            callback_data=f"mqc:{x.id}")])
    if _role(st) == "rahbar":
        rows.append([IB(text="📐 Mock ballini rasmiy natijaga moslash", callback_data="mq:cal")])
    if _role(st) in MAKERS:
        rows.append([IB(text="➕ Yangi MOC imtihon", callback_data="mq:new")])
        rows.append([IB(text="📄 Tayyor PDF test (nazorat)", callback_data="mq:pdf")])
        rows.append([IB(text="🌐 Tashqi tekshiruvli test (kanal tekshiradi)", callback_data="mq:ext")])
    text = ("🎓 <b>MOC imtihonlar va PDF testlar</b>\n\nFormatlar: Milliy sertifikat, DTM, CEFR, IELTS, oddiy test "
            "yoki o'z formatingiz. Savollar — sizning PDF faylingiz, javob varaqlari va tekshirish — bot.\n"
            "🌐 <b>Tashqi tekshiruv</b> — test sotib olingan kanalda tekshiriladi, siz faqat natijani yuklaysiz.")
    kb = IM(inline_keyboard=rows)
    if edit:
        try:
            return await msg.edit_text(text, reply_markup=kb)
        except Exception:
            pass
    await msg.answer(text, reply_markup=kb)


@router.message(F.text == K.B_MOC)
async def moc_menu(m: Message, state: FSMContext) -> None:
    await state.clear()
    await list_view(m, m.from_user.id)


@router.callback_query(F.data == "mq:list")
async def moc_list_cb(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await c.answer()
    await list_view(c.message, c.from_user.id)


# ======================= yaratish (sehrgar) =======================
@router.callback_query(F.data == "mq:new")
async def new_moc(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(w={"kind": "moc"})
    await state.set_state(Mc.title)
    await c.answer()
    await c.message.answer("🎓 <b>Yangi MOC imtihon</b>\n\nImtihon nomini yozing (masalan: <i>Noyabr DTM mock</i>):")


@router.callback_query(F.data == "mq:ext")
async def new_ext(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(w={"kind": "tashqi"})
    await state.set_state(Mc.title)
    await c.answer()
    await c.message.answer("🌐 <b>Tashqi tekshiruvli test</b>\n\nTest sotib olingan kanal (bot) javoblarni o'zi "
                           "tekshiradi — siz faqat tayyor natijani botga yuklaysiz. Bot uni o'quvchilarga moslab, "
                           "ota-onalarga natija kartasi, reyting va sertifikat sifatida tarqatadi.\n\n"
                           "Test nomini yozing (masalan: <i>Noyabr biologiya mock</i>):")


EXT_FORMATS = {     # kalit: (tugma, maks., natija turi, darajalar)
    "ms": ("🏅 Milliy sertifikat bali (0–75) + daraja", 75, "ball", M.MS_LEVELS + "; 0 Sertifikatsiz"),
    "cefr": ("🇬🇧 CEFR multilevel (0–75) + daraja", 75, "ball", "65 C1; 51 B2; 38 B1; 0 B1 dan past"),
    "dtm": ("🎯 DTM bali (0–189)", 189, "ball", None),
    "foiz": ("💯 Foiz (0–100)", 100, "foiz", None),
    "ball": ("🔢 Boshqa ball (maksimumini yozasiz)", None, "ball", None),
}


def ext_spec(title: str, mx: float, mode: str, levels: str | None) -> str:
    return f"nom: {title}\nBall | baho {mx:g}\nnatija: {mode}" + (f"\ndaraja: {levels}" if levels else "")


@router.callback_query(F.data.startswith("mqk:"))
async def ext_format(c: CallbackQuery, state: FSMContext) -> None:
    key = c.data.split(":")[1]
    w = (await state.get_data()).get("w") or {}
    if key not in EXT_FORMATS or not w.get("title"):
        return await c.answer("Jarayon eskirgan — qaytadan boshlang.", show_alert=True)
    await c.answer()
    _, mx, mode, lv = EXT_FORMATS[key]
    if mx is None:
        await state.set_state(Mc.extmax)
        return await c.message.answer("Maksimal ball nechchi? (masalan: <code>30</code> yoki <code>100</code>)")
    await _ext_after_format(c.message, state, ext_spec(w["title"], mx, mode, lv))


@router.message(Mc.extmax, F.text, ~F.text.in_(K.menu_texts()))
async def ext_max(m: Message, state: FSMContext) -> None:
    try:
        mx = float(m.text.strip().replace(",", "."))
        assert 0 < mx <= 10000
    except (ValueError, AssertionError):
        return await m.answer("Raqam yozing: 30")
    w = (await state.get_data()).get("w") or {}
    await _ext_after_format(m, state, ext_spec(w.get("title", "Test"), mx, "ball", None))


async def _ext_after_format(msg: Message, state: FSMContext, spec: str) -> None:
    w = (await state.get_data()).get("w") or {}
    w.update(spec=spec, nv=1, booklets={}, keys={}, cur=0)
    await state.update_data(w=w)
    await state.set_state(Mc.pdf)
    await msg.answer("📎 Sotib olingan test <b>PDF faylini</b> yuboring — admin qatnashchilar soniga qarab chop etadi.\n"
                     "Testni o'zingiz chiqargan bo'lsangiz — «PDFsiz davom etish».",
                     reply_markup=IM(inline_keyboard=[[IB(text="⏭ PDFsiz davom etish", callback_data="mqtp")]]))


@router.callback_query(F.data == "mqtp")
async def ext_no_pdf(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    if w.get("kind") != "tashqi":
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    w["cur"] = 0
    await state.update_data(w=w)
    await _next_variant(c.message, state)


@router.callback_query(F.data == "mq:pdf")
async def new_pdf(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        gs = await S.teacher_groups(s, st, _role(st)) if st else []
    await state.update_data(w={"kind": "nazorat"})
    await c.answer()
    if not gs:
        return await c.message.answer("Sizga biriktirilgan guruh yo'q.")
    await c.message.answer("📄 <b>Tayyor PDF test</b> — qaysi guruh uchun?", reply_markup=IM(inline_keyboard=_rows(
        [IB(text=g.name, callback_data=f"mqpg:{g.id}") for g in gs])))


@router.callback_query(F.data.startswith("mqpg:"))
async def pdf_group(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    w["gids"] = [int(c.data.split(":")[1])]
    await state.update_data(w=w)
    await state.set_state(Mc.title)
    await c.answer()
    await c.message.edit_text("Test mavzusi (masalan: <i>Logarifmlar</i>):")


@router.message(Mc.title, F.text, ~F.text.in_(K.menu_texts()))
async def got_title(m: Message, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    w["title"] = m.text.strip()[:140]
    await state.update_data(w=w)
    await state.set_state(None)
    if w.get("kind") == "tashqi":
        return await m.answer("Kanal natijani qaysi ko'rinishda beradi?", reply_markup=IM(inline_keyboard=[
            [IB(text=v[0], callback_data=f"mqk:{k}")] for k, v in EXT_FORMATS.items()]))
    if w.get("kind") == "nazorat":
        btns = [IB(text=f"{n} ta savol", callback_data=f"mqf:n{n}") for n in (10, 15, 20, 25, 30, 35, 40, 50)]
        return await m.answer("Testda nechta savol bor? (har biri A–D, 1 ball)", reply_markup=IM(
            inline_keyboard=_rows(btns, 4) + [[IB(text="✏️ Boshqa format (yozma, A–E, ball…)", callback_data="mqf:own")]]))
    btns = [IB(text=v[0], callback_data=f"mqf:{k}") for k, v in M.PRESETS.items() if k != "oddiy"]
    btns.append(IB(text="✏️ O'z formatim", callback_data="mqf:own"))
    await m.answer("Imtihon formati:", reply_markup=IM(inline_keyboard=_rows(btns)))


OWN_HELP = ("✏️ Formatni shu namunadek yozing (har qator — bitta bo'lim):\n\n<code>nom: Matematika MOC\n"
            "1-30: Algebra | test 4 | 1\n31-35: Geometriya | test 5 | 1.5\n36-40: Ochiq savollar | yozma 2 | 1\n"
            "Writing | baho 75\nnatija: ball\ndaraja: 85 A'lo; 70 Yaxshi; 55 Qoniqarli</code>\n\n"
            "• <b>test K</b> — K ta variant (A..F), oxirgi raqam — 1 to'g'ri javob bali\n"
            "• <b>yozma M</b> — ochiq savol: o'qituvchi varaqda 0..M ball belgilaydi\n"
            "• <b>baho M</b> — varaqda yo'q, ballni botga yozasiz (Writing/Speaking)\n"
            "• <b>natija</b>: ball, foiz, shkala 75, cefr yoki ielts\n• <b>daraja</b> — ixtiyoriy")


@router.callback_query(F.data.startswith("mqf:"))
async def got_format(c: CallbackQuery, state: FSMContext) -> None:
    key = c.data.split(":")[1]
    w = (await state.get_data()).get("w") or {}
    await c.answer()
    if key == "own":
        await state.set_state(Mc.spec)
        txt = OWN_HELP
        if w.get("spec"):
            txt += f"\n\nHozirgi format (nusxalab o'zgartiring):\n<code>{w['spec']}</code>"
        return await c.message.answer(txt)
    if key.startswith("n"):
        sp = M.simple_spec(int(key[1:]))
        sp.name = w.get("title") or sp.name
    else:
        sp = M.preset(key)
    w["spec"] = sp.to_text()
    await state.update_data(w=w)
    await c.message.answer(sp.summary() + ("\n\n<i>Milliy sertifikatda rasmiy ball Rasch modeli bilan hisoblanadi — "
                                           "bu yerdagi ball taxminiy.</i>" if key == "ms" else ""),
                           reply_markup=IM(inline_keyboard=[[IB(text="✅ Shu format", callback_data="mqfok")],
                                                            [IB(text="✏️ O'zgartirish", callback_data="mqf:own")]]))


@router.message(Mc.spec, F.text, ~F.text.in_(K.menu_texts()))
async def got_spec(m: Message, state: FSMContext) -> None:
    sp = M.parse_spec(m.text)
    if sp.errors:
        return await m.answer("❌ Formatda xato:\n• " + "\n• ".join(sp.errors[:8]) + "\n\nTuzatib qayta yuboring.")
    w = (await state.get_data()).get("w") or {}
    w["spec"] = sp.to_text()
    await state.update_data(w=w)
    await state.set_state(None)
    await m.answer(sp.summary(), reply_markup=IM(inline_keyboard=[[IB(text="✅ Shu format", callback_data="mqfok")],
                                                                  [IB(text="✏️ O'zgartirish", callback_data="mqf:own")]]))


@router.callback_query(F.data == "mqfok")
async def format_ok(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("Nechta variant bor? (har variantning o'z PDF fayli va kaliti bo'ladi)",
                           reply_markup=IM(inline_keyboard=[[IB(text=str(i), callback_data=f"mqv:{i}") for i in range(1, 7)]]))


@router.callback_query(F.data.startswith("mqv:"))
async def got_variants(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    if not w.get("spec"):
        return await c.answer("Jarayon eskirgan — qaytadan boshlang.", show_alert=True)
    w["nv"], w["booklets"], w["keys"], w["cur"] = int(c.data.split(":")[1]), {}, {}, 0
    await state.update_data(w=w)
    await c.answer()
    await _ask_pdf(c.message, state)


async def _ask_pdf(msg: Message, state: FSMContext) -> None:
    w = (await state.get_data())["w"]
    v = VARS[w["cur"]]
    await state.set_state(Mc.pdf)
    name = f"<b>{v}-variant</b> " if w["nv"] > 1 else ""
    await msg.answer(f"📎 {name}savollar PDF faylini yuboring.")


@router.message(Mc.pdf, F.document)
async def got_pdf(m: Message, state: FSMContext, bot: Bot) -> None:
    doc = m.document
    if not ((doc.file_name or "").lower().endswith(".pdf") or doc.mime_type == "application/pdf"):
        return await m.answer("PDF fayl yuboring (Word bo'lsa — PDF qilib saqlang).")
    pages = None
    if (doc.file_size or 0) <= 19 * 1024 * 1024:
        try:
            pages = P.pdf_pages((await bot.download(doc.file_id)).read())
        except Exception:
            pages = None
    w = (await state.get_data())["w"]
    v = VARS[w["cur"]]
    w["booklets"][v] = {"file_id": doc.file_id, "pages": pages or 1, "name": doc.file_name, "uid": doc.file_unique_id}
    await state.update_data(w=w)
    sp = M.parse_spec(w["spec"])
    if not sp.n_test:
        return await _next_variant(m, state)
    await state.set_state(Mc.key)
    await m.answer(f"✅ PDF qabul qilindi ({pages or '?'} bet).\n\n🔑 {v + '-variant ' if w['nv'] > 1 else ''}javob "
                   f"kalitini yuboring — {sp.n_test} ta test savoli uchun.\nIstalgan ko'rinishda: <code>ABCDA…</code> "
                   "yoki <code>1A 2B 3C …</code> (matn yoki .txt fayl)."
                   + (f"\n<i>Yozma savollar ({sp.n_questions - sp.n_test} ta) uchun harf kerak emas.</i>"
                      if sp.n_questions > sp.n_test else ""))


@router.message(Mc.pdf, ~F.text.in_(K.menu_texts()))
async def need_pdf(m: Message) -> None:
    await m.answer("Savollar PDF faylini yuboring.")


@router.message(Mc.key, F.text | F.document, ~F.text.in_(K.menu_texts()))
async def got_key(m: Message, state: FSMContext, bot: Bot) -> None:
    text = m.text or ""
    if m.document:
        try:
            raw = (await bot.download(m.document.file_id)).read()
            text = raw.decode("utf-8-sig", errors="replace")
        except Exception:
            return await m.answer("Faylni o'qib bo'lmadi — kalitni matn qilib yuboring.")
    w = (await state.get_data())["w"]
    sp = M.parse_spec(w["spec"])
    key, err = M.parse_key(text, sp)
    if err:
        return await m.answer(f"❌ {err}\n\nTuzatib qayta yuboring.")
    w["keys"][VARS[w["cur"]]] = key
    await state.update_data(w=w)
    await _next_variant(m, state)


async def _next_variant(msg: Message, state: FSMContext) -> None:
    w = (await state.get_data())["w"]
    w["cur"] += 1
    await state.update_data(w=w)
    if w["cur"] < w["nv"]:
        return await _ask_pdf(msg, state)
    await state.set_state(None)
    t = now().date()
    await msg.answer("📅 Imtihon sanasi?", reply_markup=IM(inline_keyboard=[[
        IB(text=f"Bugun {t:%d.%m}", callback_data=f"mqd:{t.isoformat()}"),
        IB(text=f"Ertaga {t + timedelta(days=1):%d.%m}", callback_data=f"mqd:{(t + timedelta(days=1)).isoformat()}")],
        [IB(text="✏️ Boshqa sana", callback_data="mqd:x")]]))


@router.callback_query(F.data.startswith("mqd:"))
async def got_date_btn(c: CallbackQuery, state: FSMContext) -> None:
    v = c.data.split(":", 1)[1]
    await c.answer()
    if v == "x":
        await state.set_state(Mc.date)
        return await c.message.answer("Sanani yozing: 28.11 yoki 28.11.2026")
    await _set_date(c.message, state, date.fromisoformat(v))


@router.message(Mc.date, F.text, ~F.text.in_(K.menu_texts()))
async def got_date_text(m: Message, state: FSMContext) -> None:
    t = m.text.strip().replace("/", ".").replace("-", ".")
    try:
        bits = [int(x) for x in t.split(".") if x]
        d = date(bits[2] if len(bits) > 2 and bits[2] > 2000 else now().year, bits[1], bits[0])
    except (ValueError, IndexError):
        return await m.answer("Sanani shu ko'rinishda yozing: 28.11")
    await _set_date(m, state, d)


async def _set_date(msg: Message, state: FSMContext, d: date) -> None:
    w = (await state.get_data())["w"]
    w["date"] = d.isoformat()
    w.setdefault("gids", [])
    w.setdefault("ext", [])
    w.setdefault("blanks", 0)
    await state.update_data(w=w)
    await state.set_state(None)
    if w["kind"] == "nazorat":
        return await _confirm(msg, state)
    await _participants(msg, state, msg.chat.id)


async def _participants(msg: Message, state: FSMContext, tg: int, edit: bool = False) -> None:
    w = (await state.get_data())["w"]
    async with Session() as s:
        st = await _staff(s, tg)
        gs = await S.teacher_groups(s, st, "rahbar" if _role(st) in ("rahbar", "admin") else _role(st))
    sel = set(w["gids"])
    btns = [IB(text=("✅ " if g.id in sel else "▫️ ") + g.name, callback_data=f"mqg:{g.id}") for g in gs]
    rows = _rows(btns)
    rows.append([IB(text=f"➕ Tashqi qatnashchilar ({len(w['ext'])})", callback_data="mqx")])
    if w.get("kind") != "tashqi":
        rows.append([IB(text=f"📄 Bo'sh varaqlar ({w['blanks']})", callback_data="mqb")])
    rows.append([IB(text="➡️ Davom etish", callback_data="mqgo")])
    text = ("👥 <b>Qatnashchilar</b>\n• Guruhlarni belgilang — o'quvchilar ismi yozilgan varaq oladi.\n"
            "• Tashqi qatnashchilar — ism va telefon bilan (chiptada natijani olish QR kodi bo'ladi).\n"
            "• Bo'sh varaqlar — oldindan yozilmaganlar uchun (ismni keyin kiritasiz).")
    if edit:
        try:
            return await msg.edit_text(text, reply_markup=IM(inline_keyboard=rows))
        except Exception:
            pass
    await msg.answer(text, reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data.startswith("mqg:"))
async def toggle_group(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    if "gids" not in w:
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    gid = int(c.data.split(":")[1])
    w["gids"] = [x for x in w["gids"] if x != gid] if gid in w["gids"] else w["gids"] + [gid]
    await state.update_data(w=w)
    await c.answer()
    await _participants(c.message, state, c.from_user.id, edit=True)


@router.callback_query(F.data == "mqx")
async def ext_ask(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Mc.ext)
    await c.answer()
    await c.message.answer("Tashqi qatnashchilarni yozing — har qatorda bitta:\n<code>Aliyev Vali, 901234567\n"
                           "Karimova Laylo +998 93 111 22 33</code>")


def _parse_people(text: str) -> list[tuple[str, str | None]]:
    out = []
    for line in text.splitlines():
        line = line.strip(" ,;-")
        if not line:
            continue
        digits = "".join(ch for ch in line if ch.isdigit() or ch == "+")
        name = "".join(ch for ch in line if not (ch.isdigit() or ch == "+")).strip(" ,;-") or line
        phone = S.norm_phone(digits) if len(digits.strip("+")) >= 9 else None
        out.append((" ".join(name.split())[:120], phone))
    return out


@router.message(Mc.ext, F.text, ~F.text.in_(K.menu_texts()))
async def ext_got(m: Message, state: FSMContext) -> None:
    w = (await state.get_data())["w"]
    ppl = _parse_people(m.text)
    w["ext"] = w.get("ext", []) + [list(p) for p in ppl]
    await state.update_data(w=w)
    await state.set_state(None)
    await m.answer(f"✅ {len(ppl)} ta qatnashchi qo'shildi.")
    await _participants(m, state, m.from_user.id)


@router.callback_query(F.data == "mqb")
async def blanks_ask(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Mc.blanks)
    await c.answer()
    await c.message.answer("Nechta bo'sh varaq kerak? (0 — kerak emas)")


@router.message(Mc.blanks, F.text, ~F.text.in_(K.menu_texts()))
async def blanks_got(m: Message, state: FSMContext) -> None:
    t = m.text.strip()
    if not t.isdigit() or int(t) > 300:
        return await m.answer("Son yozing: 10")
    w = (await state.get_data())["w"]
    w["blanks"] = int(t)
    await state.update_data(w=w)
    await state.set_state(None)
    await _participants(m, state, m.from_user.id)


@router.callback_query(F.data == "mqgo")
async def participants_done(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    if not (w.get("gids") or w.get("ext") or w.get("blanks")):
        return await c.answer("Kamida bitta guruh, tashqi qatnashchi yoki bo'sh varaq tanlang.", show_alert=True)
    await c.answer()
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
    if _role(st) not in PRICERS:                # narx — markaz moliyasi ishi: moliya/rahbar kartada belgilaydi
        w["price"] = 0
        await state.update_data(w=w)
        return await _confirm(c.message, state)
    if w.get("ext") or w.get("blanks") or w.get("kind") == "tashqi":
        await state.set_state(Mc.price)
        who = "Qatnashish" if w.get("kind") == "tashqi" else "Tashqi qatnashchilar uchun imtihon"
        return await c.message.answer(f"💵 {who} narxi (so'm)? Bepul bo'lsa — 0.\n"
                                      "<i>Narxni o'zingiz belgilaysiz — varaq hisobiga bog'liq emas.</i>",
                                      reply_markup=IM(inline_keyboard=[[IB(text="Bepul", callback_data="mqpr:0")]]))
    await _confirm(c.message, state)


async def _after_price(msg: Message, state: FSMContext) -> None:
    w = (await state.get_data())["w"]
    if w.get("price") and w.get("gids"):
        return await msg.answer(f"💵 {S.fmt_som(w['price'])} — kimlar to'laydi?", reply_markup=IM(inline_keyboard=[
            [IB(text="👥 Hamma (guruh o'quvchilari ham)", callback_data="mqwp:all")],
            [IB(text="🙋 Faqat tashqi qatnashchilar", callback_data="mqwp:ext")]]))
    await _confirm(msg, state)


@router.callback_query(F.data.startswith("mqwp:"))
async def who_pays(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    if "price" not in w:
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    w["pay_all"] = c.data.endswith("all")
    await state.update_data(w=w)
    await c.answer()
    await _confirm(c.message, state)


@router.callback_query(F.data.startswith("mqpr:"))
async def price_btn(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data())["w"]
    w["price"] = int(c.data.split(":")[1])
    await state.update_data(w=w)
    await state.set_state(None)
    await c.answer()
    await _after_price(c.message, state)


@router.message(Mc.price, F.text, ~F.text.in_(K.menu_texts()))
async def price_got(m: Message, state: FSMContext) -> None:
    v = S.parse_amount(m.text) if hasattr(S, "parse_amount") else None
    if v is None:
        t = "".join(ch for ch in m.text if ch.isdigit())
        v = int(t) if t else None
    if v is None:
        return await m.answer("Summani yozing: 50000")
    w = (await state.get_data())["w"]
    w["price"] = int(v)
    await state.update_data(w=w)
    await state.set_state(None)
    await _after_price(m, state)


async def _confirm(msg: Message, state: FSMContext) -> None:
    w = (await state.get_data())["w"]
    sp = M.parse_spec(w["spec"])
    async with Session() as s:
        names, n_st = [], 0
        for gid in w.get("gids", []):
            g = await s.get(Group, gid)
            r = await S.group_roster(s, gid, date.fromisoformat(w["date"]))
            names.append(f"{g.name} ({len(r)})")
            n_st += len(r)
    total = n_st + len(w.get("ext", [])) + w.get("blanks", 0)
    ext = w["kind"] == "tashqi"
    text = (f"{KIND_ICON.get(w['kind'], '🎓')} <b>{w['title']}</b> · {date.fromisoformat(w['date']):%d.%m.%Y}\n\n"
            + (f"🌐 Tashqi tekshiruv (kanal tekshiradi)\n{sp.summary().split(chr(10), 2)[-1]}\n"
               f"📎 Test PDF: {'bor — chop etishga ketadi' if w.get('booklets') else 'yo`q'}\n"
               if ext else f"{sp.summary()}\n\nVariantlar: {w['nv']} ({', '.join(VARS[:w['nv']])})\n")
            + f"Qatnashchilar: {total} ta" + (f"\n• Guruhlar: {', '.join(names)}" if names else "")
            + (f"\n• Tashqi: {len(w['ext'])}" if w.get("ext") else "")
            + (f"\n• Bo'sh varaq: {w['blanks']}" if w.get("blanks") else "")
            + (f"\n💵 Narx: {S.fmt_som(w.get('price', 0))} · "
               + ("hamma to'laydi" if w.get("pay_all") else "faqat tashqi qatnashchilar") if w.get("price") else "")
            + ("\n\nTasdiqlasangiz — test chop etishga ketadi; natija kelgach «📥 Natijani yuklash» ni bosasiz."
               if ext else "\n\nTasdiqlasangiz — javob varaqlari tayyorlanadi va savollar bilan birga adminga "
                           "chop etishga ketadi."))
    await msg.answer(text, reply_markup=IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data="mqok"),
                                                             IB(text="❌ Bekor", callback_data="mqno")]]))


@router.callback_query(F.data == "mqno")
async def wizard_cancel(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await c.answer("Bekor qilindi")
    await c.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "mqok")
async def wizard_ok(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    w = (await state.get_data()).get("w") or {}
    if not w.get("date"):
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    await state.clear()
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        moc = Moc(kind=w["kind"], title=w["title"], spec=w["spec"], date=date.fromisoformat(w["date"]),
                  group_ids=w.get("gids", []), n_variants=w["nv"], booklets=w["booklets"], keys=w["keys"],
                  price=int(w.get("price") or 0), created_by=st.id if st else None,
                  meta={"pay_all": bool(w.get("pay_all"))})
        s.add(moc)
        await s.flush()
        ids = await _make_seats(s, moc, w.get("gids", []), [tuple(x) for x in w.get("ext", [])], w.get("blanks", 0))
        await S.audit(s, st, "moc_yaratildi", id=moc.id, title=moc.title, seats=len(ids))
        await s.commit()
        mid = moc.id
    if _role(st) not in PRICERS and (w["kind"] == "tashqi" or w.get("ext")):
        from app.bot.moc_ext import ask_price
        await ask_price(bot, mid)
    if w["kind"] == "tashqi":
        from app.bot.moc_ext import issue_ext
        await issue_ext(bot, mid, ids, c.from_user.id)
    else:
        await c.message.answer(f"⏳ {len(ids)} ta javob varag'i tayyorlanmoqda…")
        await issue(bot, mid, ids, c.from_user.id, booklets=True)
    await card(c.message, mid, c.from_user.id)


async def _make_seats(s, moc: Moc, gids: list[int], ext: list[tuple], blanks: int) -> list[int]:
    have = {x for x in (await s.execute(select(MocSeat.student_id).where(MocSeat.moc_id == moc.id))).scalars() if x}
    n0 = len((await s.execute(select(MocSeat.id).where(MocSeat.moc_id == moc.id))).all())
    seats, k = [], n0
    for gid in gids:
        for e in await S.group_roster(s, gid, moc.date):
            if e.student_id in have:
                continue
            have.add(e.student_id)
            stu = await s.get(Student, e.student_id)
            seats.append(MocSeat(moc_id=moc.id, enrollment_id=e.id, student_id=stu.id, name=stu.full_name,
                                 variant=VARS[k % moc.n_variants], code="", token=secrets.token_hex(4)))
            k += 1
    for name, phone in ext:
        seats.append(MocSeat(moc_id=moc.id, name=name, phone=phone, variant=VARS[k % moc.n_variants], code="",
                             token=secrets.token_hex(4)))
        k += 1
    for _ in range(blanks):
        seats.append(MocSeat(moc_id=moc.id, blank=True, variant="A" if moc.n_variants == 1 else None, code="",
                             token=secrets.token_hex(4)))
    for i, z in enumerate(seats, n0 + 1):
        z.code = f"M{moc.id}-{i:03d}"
        s.add(z)
    await s.flush()
    return [z.id for z in seats]


# ======================= varaqlar va chop etish =======================
async def _bot_username(bot: Bot) -> str:
    me = await bot.get_me()
    return me.username or ""


def _legend(sp: M.Spec) -> str:
    on = [x for x in sp.sections if x.on_sheet]
    if len(on) < 2:
        return ""
    return " · ".join(f"{x.start}–{x.start + x.n - 1} {x.name}" for x in on)


async def issue(bot: Bot, moc_id: int, seat_ids: list[int], tg: int, booklets: bool) -> None:
    """Javob varaqlari (+ chiptalar) PDF ini yaratib, so'raganga yuboradi va adminga chop etish so'rovlarini beradi."""
    from app.bot import prints as PR
    uname = await _bot_username(bot)
    async with Session() as s:
        moc = await s.get(Moc, moc_id)
        st = await _staff(s, tg)
        sp = _spec(moc)
        base = sp.layout()
        seats = [await s.get(MocSeat, i) for i in seat_ids]
        gnames = {g.id: g.name for g in (await s.execute(select(Group))).scalars()}
        enr_g = {e.id: e.group_id for e in (await s.execute(select(Enrollment).where(
            Enrollment.id.in_([z.enrollment_id for z in seats if z.enrollment_id] or [0])))).scalars()}
        items, tickets = [], []
        for z in seats:
            lay = base + (f".V{moc.n_variants}" if z.blank and moc.n_variants > 1 else "")
            grp = gnames.get(enr_g.get(z.enrollment_id), "") if z.enrollment_id else ("Tashqi qatnashchi" if z.name else "")
            items.append(S2.Sheet2(z.id, lay, moc.title, z.name, grp, f"{moc.date:%d.%m.%Y}", z.code, z.variant,
                                   _legend(sp)))
            if not z.enrollment_id and uname:
                tickets.append(S2.Ticket(moc.title, z.name, z.code, z.variant, f"{moc.date:%d.%m.%Y}",
                                         f"https://t.me/{uname}?start=r{z.id}_{z.token}"))
        counts: dict[str, int] = {}
        for z in seats:
            counts[z.variant or "?"] = counts.get(z.variant or "?", 0) + 1
        if "?" in counts:                     # bo'sh varaqlar: variantlar teng bo'linadi
            q = counts.pop("?")
            for i in range(q):
                v = VARS[i % moc.n_variants]
                counts[v] = counts.get(v, 0) + 1
    pdf = S2.sheets_pdf(items, settings.center_name)
    fn = f"MOC{moc_id}_javob_varaqlari.pdf"
    sent = await bot.send_document(tg, BufferedInputFile(pdf, fn), caption=f"📄 Javob varaqlari: {len(items)} ta "
                                   "(admin bir tomonlama, 100% masshtabda chop etadi)")
    jobs = [("Javob varaqlari — bir tomonlama, 100% masshtab", sent.document.file_id, len(items), 1, "bir")]
    if tickets:
        tp = S2.tickets_pdf(tickets, settings.center_name)
        t_msg = await bot.send_document(tg, BufferedInputFile(tp, f"MOC{moc_id}_chiptalar.pdf"),
                                        caption=f"🎫 Chiptalar: {len(tickets)} ta — qatnashchiga beriladi, QR orqali "
                                                "natijani Telegramda oladi. Qirqib tarqating.")
        jobs.append(("Qatnashchi chiptalari (qirqiladi)", t_msg.document.file_id, -(-len(tickets) // 8), 1, "bir"))
    if booklets:
        for v, n in sorted(counts.items()):
            b = (moc.booklets or {}).get(v)
            if b and n:
                pages = int(b.get("pages") or 1)
                jobs.append((f"{v}-variant savollar" if moc.n_variants > 1 else "Savollar", b["file_id"], pages, n,
                             "ikki" if pages > 1 else "bir"))
    async with Session() as s:
        st = await _staff(s, tg)
        made = []
        for note, fid, pages, copies, layout in jobs:
            job = PrintJob(kind="ichki", staff_id=st.id if st else None, copies=copies, pages=pages, layout=layout,
                           sheets=P.sheets(pages, layout), file_id=fid, file_kind="pdf",
                           note=f"🎓 {moc.title[:60]}: {note}"[:300])
            s.add(job)
            made.append(job)
        await s.commit()
        cards = [(j, *(await PR._job_card(s, j))) for j in made]
        to = await PR._printers(s)
    for j, cap, kb in cards:
        for t in to:
            await PR._send_file(bot, t, j.file_id, j.file_kind, cap, kb)
    await send(bot, tg, f"🖨 Adminga {len(made)} ta chop etish so'rovi yuborildi (savollar, javob varaqlari"
                        + (", chiptalar" if tickets else "") + ").")


# ======================= imtihon kartasi =======================
async def _seats(s, mid: int) -> list[MocSeat]:
    return list((await s.execute(select(MocSeat).where(MocSeat.moc_id == mid).order_by(MocSeat.id))).scalars())


async def _money_line(moc: Moc, payers_: list[MocSeat]) -> str:
    from app.bot.moc_ext import money_line
    return await money_line(moc, payers_)


async def card(msg: Message, mid: int, tg: int, edit: bool = False) -> None:
    async with Session() as s:
        moc = await s.get(Moc, mid)
        st = await _staff(s, tg)
        seats = await _seats(s, mid)
    if not moc:
        return
    sp = _spec(moc)
    named = [z for z in seats if z.name or z.answers]
    done = [z for z in seats if z.answers]
    absent = [z for z in seats if z.absent]
    uns = [z for z in seats if z.unsure]
    no_var = [z for z in done if not z.variant]
    no_name = [z for z in done if not z.name]
    baho = [x.name for x in sp.sections if x.kind == "baho"]
    need_baho = [z for z in seats if baho and not z.absent and (z.answers or not z.blank)
                 and any(n not in (z.baho or {}) for n in baho)]
    ext = payers(moc, seats)
    unpaid = [z for z in ext if moc.price and z.paid < moc.price]
    if moc.kind == "tashqi":
        from app.bot.moc_ext import ext_card
        return await ext_card(msg, moc, sp, seats, st, unpaid, ext, edit)
    lines = [f"{KIND_ICON.get(moc.kind, '🎓')} <b>{moc.title}</b> · {moc.date:%d.%m.%Y}"
             + (" · ✅ yakunlangan" if moc.status == "yakun" else ""),
             f"{sp.name} · {sp.n_questions} savol · {moc.n_variants} variant",
             f"\n📷 Tekshirildi: <b>{len(done)}</b> / {len(named)}" + (f" · qatnashmadi: {len(absent)}" if absent else ""),
             f"📄 Bo'sh varaqlar: {sum(1 for z in seats if z.blank)}" if any(z.blank for z in seats) else ""]
    if uns:
        lines.append(f"⚠️ Noaniq javobli varaqlar: {len(uns)}")
    if no_var or no_name:
        lines.append(f"⚠️ Bo'sh varaqlar: variant/ism kiritilmagan — {len(set(no_var) | set(no_name))}")
    if baho:
        lines.append(f"📝 {', '.join(baho)} kiritilmagan: {len(need_baho)}")
    if moc.price:
        lines.append(f"💵 To'lov: {len(ext) - len(unpaid)}/{len(ext)} · {S.fmt_som(sum(z.paid for z in ext))}")
    lines.append("\nTekshirish: menyudagi <b>📷 Skaner</b> (jonli) yoki «🖼 Rasm yuborish».")
    rows = []
    r = _role(st)
    if moc.status != "yakun":
        rows.append([IB(text="🖼 Rasm yuborib tekshirish", callback_data=f"mqs:{mid}"),
                     IB(text="✏️ Qo'lda", callback_data=f"mqm:{mid}")])
        if uns or no_var or no_name:
            first = (uns or no_var or no_name)[0]
            rows.append([IB(text="⚠️ Noaniqlarni hal qilish", callback_data=f"mqu:{first.id}")])
        if baho:
            rows.append([IB(text=f"📝 {', '.join(baho)} ballari", callback_data=f"mqh:{mid}")])
        if len(baho) == 1:
            rows.append([IB(text=f"📥 {baho[0]} ballarini fayldan yuklash", callback_data=f"xri:{mid}")])
    rows.append([IB(text="👥 Qatnashchilar", callback_data=f"mql:{mid}"),
                 IB(text="➕ Qo'shish", callback_data=f"mqa:{mid}")] if moc.status != "yakun" else
                [IB(text="👥 Qatnashchilar", callback_data=f"mql:{mid}")])
    if moc.price and ext:
        rows.append([IB(text="💵 To'lovlar", callback_data=f"mqp:{mid}")])
    if r in PRICERS:
        from app.bot.moc_ext import money_rows
        rows += money_rows(moc)
        lines.append(await _money_line(moc, ext))
    if moc.status != "yakun" and r in MAKERS:
        rows.append([IB(text="🏁 Yakunlash va natijalarni yuborish", callback_data=f"mqz:{mid}")])
    if moc.status == "yakun":
        rows.append([IB(text="📊 Natijalar (Excel)", callback_data=f"mqxl:{mid}")])
        if moc.kind != "tashqi":
            rows.append([IB(text="📈 Savollar tahlili", callback_data=f"anm:{mid}")])
    if r in MAKERS and moc.status != "yakun":
        rows.append([IB(text="🖨 Varaqlarni qayta olish", callback_data=f"mqr:{mid}"),
                     IB(text="🗑 Bekor qilish", callback_data=f"mqdel:{mid}")])
    rows.append([IB(text="⬅️ Ro'yxat", callback_data="mq:list")])
    text = "\n".join(x for x in lines if x)
    if edit:
        try:
            return await msg.edit_text(text, reply_markup=IM(inline_keyboard=rows))
        except Exception:
            pass
    await msg.answer(text, reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data.startswith("mqc:"))
async def card_cb(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await c.answer()
    await card(c.message, int(c.data.split(":")[1]), c.from_user.id)


# ======================= javoblarni saqlash =======================
def _decode_live(raw: str) -> tuple[str, list[int], str | None]:
    """Jonli skaner: 'a'..'f' → taxmin harf, 'p'..'t' → taxmin 1..5, '@V' — bo'sh varaq varianti."""
    raw, _, var = raw.partition("@")
    out, uns = [], []
    for i, ch in enumerate(raw, 1):
        if ch in "abcdef":
            out.append(ch.upper())
            uns.append(i)
        elif ch in "pqrst":
            out.append(str("pqrst".index(ch) + 1))
            uns.append(i)
        elif ch == "?":
            out.append("?")
            uns.append(i)
        else:
            out.append(ch)
    v = var if var in VARS else None
    return "".join(out), uns, v


def _valid(sp: M.Spec, answers: str) -> bool:
    if len(answers) != sp.n_questions:
        return False
    for ch, (k, nb) in zip(answers, sp.bubbles()):
        ok = ("-*?" + (VARS[:nb] if k == "t" else "123456789"[:nb]))
        if ch not in ok:
            return False
    return True


async def store(s, seat: MocSeat, answers: str, unsure: list[int], variant: str | None, scanned: bool,
                st: Staff | None) -> tuple[str | None, str]:
    """Qaytaradi: (xato, qisqa natija qatori)."""
    moc = await s.get(Moc, seat.moc_id)
    if not moc or moc.status == "bekor":
        return "bu imtihon bekor qilingan", ""
    if moc.status == "yakun":
        return "imtihon yakunlangan — natijalar yuborilgan", ""
    sp = _spec(moc)
    if not _valid(sp, answers):
        return f"varaqdagi savollar ({len(answers)}) imtihonga ({sp.n_questions}) mos emas", ""
    seat.answers, seat.absent, seat.scanned, seat.entered_by = answers, False, scanned, st.id if st else None
    seat.unsure = [q for q in unsure if 0 < q <= len(answers) and answers[q - 1] != "*"] or None
    if seat.blank and variant:
        seat.variant = variant
    return None, _line(moc, sp, seat)


def _line(moc: Moc, sp: M.Spec, z: MocSeat) -> str:
    who = z.name or f"Bo'sh varaq {z.code}"
    if not z.variant:
        return f"• {who} — ⚠️ variant aniqlanmadi"
    key = (moc.keys or {}).get(z.variant) or "." * sp.n_questions
    r = M.score(sp, key, z.answers or "", z.baho or {})
    line = f"• {who} ({z.variant}) — <b>{'≈' if sp.mode == 'rasch' else ''}{M.fmt_total(sp, r)}</b>" \
        + (f" · {r['level']}" if r.get("level") else "")
    if r.get("missing"):
        line += f" · {', '.join(r['missing'])} kutilmoqda"
    dbl = (z.answers or "").count("*")
    if dbl:
        line += f" · ikkita belgilangan: {dbl}"
    if z.unsure:
        line += f" ⚠️ noaniq: {', '.join(map(str, z.unsure[:10]))}"
    return line


async def live_scan(m: Message, items: list[tuple[int, str]]) -> None:
    """Jonli skanerdan kelgan v2 varaqlar (E1|m<id>:<javoblar>)."""
    lines, errs, fix, mids = [], [], [], set()
    async with Session() as s:
        st = await _staff(s, m.from_user.id)
        for sid, raw in items:
            z = await s.get(MocSeat, sid)
            if not z:
                errs.append(f"varaq №{sid}: bazada topilmadi")
                continue
            ans, uns, var = _decode_live(raw)
            err, line = await store(s, z, ans, uns, var, True, st)
            if err:
                errs.append(f"{z.name or z.code}: {err}")
                continue
            lines.append(line)
            mids.add(z.moc_id)
            if z.unsure or not z.variant or not z.name:
                fix.append(z)
        await s.commit()
        titles = {i: (await s.get(Moc, i)).title for i in mids}
    text = f"📷 <b>MOC skaner</b>: {len(lines)} ta varaq\n" + "\n".join(lines)
    if errs:
        text += "\n\n❗ " + "\n❗ ".join(errs)
    kb = [[IB(text=f"⚠️ {z.name or z.code} — hal qilish", callback_data=f"mqu:{z.id}")] for z in fix[:15]]
    kb += [[IB(text=f"🎓 {titles[i][:40]} — kartasi", callback_data=f"mqc:{i}")] for i in mids]
    await _long(m, text, IM(inline_keyboard=kb) if kb else None)


async def _long(m: Message, text: str, kb) -> None:
    while len(text) > 3900:
        cut = text.rfind("\n", 0, 3900)
        await m.answer(text[:cut])
        text = text[cut:].lstrip("\n")
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("mqs:"))
async def photo_start(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Mc.scan)
    await state.update_data(mid=int(c.data.split(":")[1]))
    await c.answer()
    await c.message.answer("🖼 Javob varaqlarini rasmga olib yuboring (bir nechtasini birdaniga ham bo'ladi). "
                           "Varaq to'liq, 4 ta qora burchagi bilan ko'rinsin.",
                           reply_markup=IM(inline_keyboard=[[IB(text="✅ Tugatdim", callback_data=f"mqc:{c.data.split(':')[1]}")]]))


@router.message(Mc.scan, F.photo | F.document)
async def photo_got(m: Message, state: FSMContext, bot: Bot) -> None:
    from app.logic import omr
    fid = m.photo[-1].file_id if m.photo else m.document.file_id
    data = (await bot.download(fid)).read()
    r = omr.read_sheet2(data)
    if r.error:
        return await m.reply(f"❌ {r.error}")
    async with Session() as s:
        st = await _staff(s, m.from_user.id)
        z = await s.get(MocSeat, r.seat_id)
        if not z:
            return await m.reply("Bu varaq bazada topilmadi.")
        err, line = await store(s, z, r.answers, r.unsure, r.variant, True, st)
        await s.commit()
    if err:
        return await m.reply(f"❌ {err}")
    kb = None
    if z.unsure or not z.variant or not z.name:
        kb = IM(inline_keyboard=[[IB(text="⚠️ Hal qilish", callback_data=f"mqu:{z.id}")]])
    await m.reply("✅ " + line.lstrip("• "), reply_markup=kb)


# ---------------- noaniqlar, variant, ism ----------------
def _labels(sp: M.Spec, q: int) -> tuple[str, str]:
    k, nb = sp.bubbles()[q - 1]
    return k, (VARS[:nb] if k == "t" else "123456789"[:nb])


@router.callback_query(F.data.startswith("mqu:"))
async def resolve(c: CallbackQuery, state: FSMContext) -> None:
    sid = int(c.data.split(":")[1])
    await c.answer()
    await _resolve_next(c.message, state, sid, edit=False)


async def _resolve_next(msg: Message, state: FSMContext, sid: int, edit: bool) -> None:
    async with Session() as s:
        z = await s.get(MocSeat, sid)
        moc = await s.get(Moc, z.moc_id) if z else None
    if not z or not moc:
        return
    sp = _spec(moc)
    who = z.name or f"Bo'sh varaq {z.code}"
    if z.answers and not z.variant:
        return await msg.answer(f"📄 {who}: varaqda <b>variant</b> belgisi aniqlanmadi. Varaqqa qarab tanlang:",
                                reply_markup=IM(inline_keyboard=[[IB(text=v, callback_data=f"mqvr:{z.id}:{v}")
                                                                  for v in VARS[:moc.n_variants]]]))
    if z.unsure:
        q = z.unsure[0]
        k, labels = _labels(sp, q)
        sec = sp.section_of(q)
        btns = [IB(text=x, callback_data=f"mqua:{z.id}:{q}:{x}") for x in labels]
        extra = [IB(text="Bo'sh" if k == "t" else "0 ball", callback_data=f"mqua:{z.id}:{q}:-")]
        if k == "t":
            extra.append(IB(text="Ikkita (xato)", callback_data=f"mqua:{z.id}:{q}:*"))
        text = (f"⚠️ {who} · <b>{q}-savol</b>" + (f" ({sec.name})" if sec else "")
                + (" — qaysi doiracha bo'yalgan?" if k == "t" else " — o'qituvchi qaysi ballni belgilagan?")
                + f"\nQolgan noaniq: {len(z.unsure)}")
        kb = IM(inline_keyboard=[btns, extra])
        if edit:
            try:
                return await msg.edit_text(text, reply_markup=kb)
            except Exception:
                pass
        return await msg.answer(text, reply_markup=kb)
    if z.answers and not z.name:
        await state.set_state(Mc.name)
        await state.update_data(sid=z.id)
        return await msg.answer(f"📄 Bo'sh varaq {z.code}: qatnashchining ism-familiyasi va telefonini yozing "
                                "(<i>Aliyev Vali 901234567</i>):")
    # hammasi hal bo'ldi — keyingi muammoli varaq
    async with Session() as s:
        nxt = next((x for x in await _seats(s, z.moc_id)
                    if x.answers and (x.unsure or not x.variant or not x.name)), None)
        line = _line(moc, sp, await s.get(MocSeat, sid))
    text = f"✅ {line.lstrip('• ')}"
    kb = IM(inline_keyboard=[[IB(text=f"⚠️ Keyingisi: {nxt.name or nxt.code}", callback_data=f"mqu:{nxt.id}")]]
            if nxt else [[IB(text="🎓 Imtihon kartasi", callback_data=f"mqc:{z.moc_id}")]])
    if edit:
        try:
            return await msg.edit_text(text, reply_markup=kb)
        except Exception:
            pass
    await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("mqua:"))
async def resolve_pick(c: CallbackQuery, state: FSMContext) -> None:
    _, sid, q, ch = c.data.split(":")
    sid, q = int(sid), int(q)
    async with Session() as s:
        z = await s.get(MocSeat, sid)
        moc = await s.get(Moc, z.moc_id) if z else None
        if not z or moc.status == "yakun" or q not in (z.unsure or []):
            return await c.answer("Allaqachon hal qilingan.", show_alert=True)
        a = list(z.answers)
        a[q - 1] = ch
        z.answers = "".join(a)
        z.unsure = [x for x in z.unsure if x != q] or None
        await s.commit()
    await c.answer("Saqlandi")
    await _resolve_next(c.message, state, sid, edit=True)


@router.callback_query(F.data.startswith("mqvr:"))
async def set_variant(c: CallbackQuery, state: FSMContext) -> None:
    _, sid, v = c.data.split(":")
    async with Session() as s:
        z = await s.get(MocSeat, int(sid))
        if not z:
            return await c.answer()
        z.variant = v
        await s.commit()
    await c.answer(f"{v}-variant")
    await _resolve_next(c.message, state, int(sid), edit=True)


@router.message(Mc.name, F.text, ~F.text.in_(K.menu_texts()))
async def set_name(m: Message, state: FSMContext) -> None:
    sid = (await state.get_data()).get("sid")
    ppl = _parse_people(m.text)
    if not ppl:
        return await m.answer("Ism-familiyani yozing.")
    await state.set_state(None)
    async with Session() as s:
        z = await s.get(MocSeat, sid)
        z.name, z.phone = ppl[0][0], ppl[0][1] or z.phone
        await s.commit()
    await _resolve_next(m, state, sid, edit=False)


# ---------------- qo'lda kiritish va qatnashmadi ----------------
@router.callback_query(F.data.startswith("mqm:"))
async def manual_list(c: CallbackQuery) -> None:
    mid = int(c.data.split(":")[1])
    async with Session() as s:
        seats = [z for z in await _seats(s, mid) if not z.answers and not z.absent and z.name]
    await c.answer()
    if not seats:
        return await c.message.answer("Hammasi kiritilgan ✅ (bo'sh varaqlar skaner orqali kiritiladi).")
    await c.message.answer("✏️ Kimning javobini kiritasiz?", reply_markup=IM(inline_keyboard=[
        [IB(text=f"{z.name[:30]} ({z.variant or '?'})", callback_data=f"mqme:{z.id}"),
         IB(text="🚫 Kelmadi", callback_data=f"mqab:{z.id}")] for z in seats[:40]]))


@router.callback_query(F.data.startswith("mqme:"))
async def manual_one(c: CallbackQuery, state: FSMContext) -> None:
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        z = await s.get(MocSeat, sid)
        sp = _spec(await s.get(Moc, z.moc_id))
    await state.set_state(Mc.manual)
    await state.update_data(sid=sid)
    await c.answer()
    ex = "".join("A" if k == "t" else "1" for k, _ in sp.bubbles())
    await c.message.answer(f"✏️ <b>{z.name}</b> ({z.variant}) — {sp.n_questions} ta javobni ketma-ket yozing.\n"
                           "Test: A–F, yozma: ball raqami (0 — 0 ball), javobsiz: - \n"
                           f"Namuna: <code>{ex[:60]}</code>")


@router.message(Mc.manual, F.text, ~F.text.in_(K.menu_texts()))
async def manual_save(m: Message, state: FSMContext) -> None:
    sid = (await state.get_data()).get("sid")
    raw = "".join(m.text.split()).upper().replace("0", "-")
    async with Session() as s:
        st = await _staff(s, m.from_user.id)
        z = await s.get(MocSeat, sid)
        err, line = await store(s, z, raw, [], None, False, st)
        if err:
            return await m.answer(f"❌ {err}. Qayta yozing.")
        await s.commit()
    await state.set_state(None)
    await m.answer("✅ " + line.lstrip("• "), reply_markup=IM(inline_keyboard=[
        [IB(text="✏️ Keyingisi", callback_data=f"mqm:{z.moc_id}"), IB(text="🎓 Karta", callback_data=f"mqc:{z.moc_id}")]]))


@router.callback_query(F.data.startswith("mqab:"))
async def mark_absent(c: CallbackQuery) -> None:
    async with Session() as s:
        z = await s.get(MocSeat, int(c.data.split(":")[1]))
        if not z or z.answers:
            return await c.answer()
        z.absent = True
        await s.commit()
    await c.answer(f"{z.name}: qatnashmadi")
    await c.message.edit_reply_markup(reply_markup=None)


# ---------------- Writing/Speaking kabi bo'limlar ----------------
@router.callback_query(F.data.startswith("mqh:"))
async def baho_start(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(skip=[])
    await c.answer()
    await _baho_next(c.message, state, int(c.data.split(":")[1]))


async def _baho_next(msg: Message, state: FSMContext, mid: int) -> None:
    skip = set((await state.get_data()).get("skip") or [])
    async with Session() as s:
        moc = await s.get(Moc, mid)
        sp = _spec(moc)
        names = [x for x in sp.sections if x.kind == "baho"]
        todo = [z for z in await _seats(s, mid) if not z.absent and z.name and z.id not in skip
                and any(x.name not in (z.baho or {}) for x in names)]
    if not todo:
        await state.set_state(None)
        return await msg.answer("📝 Hamma ballar kiritildi ✅", reply_markup=IM(inline_keyboard=[
            [IB(text="🎓 Imtihon kartasi", callback_data=f"mqc:{mid}")]]))
    z = todo[0]
    await state.set_state(Mc.baho)
    await state.update_data(sid=z.id, mid=mid)
    rng = ", ".join(f"{x.name} (0–{x.mx:g})" for x in names)
    ex = " ".join(f"{round(x.mx * 0.7):g}" if x.mx > 9 else "6.5" for x in names)
    await msg.answer(f"📝 <b>{z.name}</b>\n{rng} — ketma-ket yozing: <code>{ex}</code>\nQolgan: {len(todo)}",
                     reply_markup=IM(inline_keyboard=[[IB(text="⏭ Keyinroq", callback_data=f"mqhs:{z.id}"),
                                                       IB(text="🚫 Qatnashmadi", callback_data=f"mqhn:{z.id}")]]))


@router.message(Mc.baho, F.text, ~F.text.in_(K.menu_texts()))
async def baho_save(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    async with Session() as s:
        z = await s.get(MocSeat, d["sid"])
        moc = await s.get(Moc, z.moc_id)
        names = [x for x in _spec(moc).sections if x.kind == "baho"]
        try:
            vals = [float(x.replace(",", ".")) for x in m.text.split()]
        except ValueError:
            return await m.answer("Faqat raqamlar: " + " ".join("55" for _ in names))
        if len(vals) != len(names) or any(not 0 <= v <= x.mx for v, x in zip(vals, names)):
            return await m.answer(f"{len(names)} ta raqam kerak: " + ", ".join(f"{x.name} 0–{x.mx:g}" for x in names))
        z.baho = {**(z.baho or {}), **{x.name: v for x, v in zip(names, vals)}}
        await s.commit()
    await _baho_next(m, state, d["mid"])


@router.callback_query(F.data.startswith("mqhs:"))
async def baho_skip(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    await state.update_data(skip=(d.get("skip") or []) + [int(c.data.split(":")[1])])
    await c.answer()
    if d.get("mid"):
        await _baho_next(c.message, state, d["mid"])


@router.callback_query(F.data.startswith("mqhn:"))
async def baho_absent(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    async with Session() as s:
        z = await s.get(MocSeat, int(c.data.split(":")[1]))
        if z and not z.answers:
            z.absent = True
            await s.commit()
    await c.answer("Qatnashmadi")
    if d.get("mid"):
        await _baho_next(c.message, state, d["mid"])


# ---------------- qatnashchilar, qo'shish, to'lov ----------------
@router.callback_query(F.data.startswith("mql:"))
async def people(c: CallbackQuery) -> None:
    mid = int(c.data.split(":")[1])
    async with Session() as s:
        moc = await s.get(Moc, mid)
        seats = await _seats(s, mid)
    sp = _spec(moc)
    lines = [f"👥 <b>{moc.title}</b> — {len(seats)} o'rin"]
    for z in seats:
        st = ("🚫" if z.absent else "✅" if (z.answers or z.baho) else "⏳")
        extra = ""
        if z.result:
            extra = f" — {M.fmt_total(sp, z.result)}" + (f" {z.result.get('level')}" if z.result.get("level") else "")
        lines.append(f"{st} {z.code} {z.name or '(bo`sh varaq)'} · {z.variant or '?'}{extra}"
                     + (" 📱" if z.tg_id else "") + (" 💵" if moc.price and z.paid >= moc.price else ""))
    await c.answer()
    await _long(c.message, "\n".join(lines), IM(inline_keyboard=[[IB(text="⬅️ Karta", callback_data=f"mqc:{mid}")]]))


@router.callback_query(F.data.startswith("mqa:"))
async def add_ask(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Mc.add)
    await state.update_data(mid=int(c.data.split(":")[1]))
    await c.answer()
    await c.message.answer("➕ Qo'shiladigan qatnashchilar — har qatorda bitta (<i>Aliyev Vali 901234567</i>).\n"
                           "Yoki bo'sh varaqlar soni: <code>+5</code>")


@router.message(Mc.add, F.text, ~F.text.in_(K.menu_texts()))
async def add_got(m: Message, state: FSMContext, bot: Bot) -> None:
    mid = (await state.get_data())["mid"]
    t = m.text.strip()
    blanks = int(t[1:]) if t.startswith("+") and t[1:].isdigit() else 0
    ppl = [] if blanks else _parse_people(t)
    await state.set_state(None)
    async with Session() as s:
        moc = await s.get(Moc, mid)
        ids = await _make_seats(s, moc, [], ppl, blanks)
        await s.commit()
    if moc.kind == "tashqi":
        from app.bot.moc_ext import issue_ext
        await m.answer(f"✅ {len(ids)} ta qo'shildi.")
        return await issue_ext(bot, mid, ids, m.from_user.id, extra=True)
    await m.answer(f"✅ {len(ids)} ta qo'shildi — varaqlar tayyorlanmoqda…")
    await issue(bot, mid, ids, m.from_user.id, booklets=True)


async def moc_service(s) -> Service:
    sv = (await s.execute(select(Service).where(Service.kind == "moc"))).scalars().first()
    if not sv:
        sv = Service(name="🎓 MOC imtihon", kind="moc", price=0, cost=0, active=False)
        s.add(sv)
        await s.flush()
    return sv


@router.callback_query(F.data.startswith("mqp:"))
async def payments(c: CallbackQuery) -> None:
    await c.answer()
    await _pay_view(c.message, int(c.data.split(":")[1]))


async def _pay_view(msg: Message, mid: int) -> None:
    async with Session() as s:
        moc = await s.get(Moc, mid)
        ext = payers(moc, await _seats(s, mid))
    unpaid = [z for z in ext if z.paid < moc.price]
    text = (f"💵 <b>{moc.title}</b> — narx {S.fmt_som(moc.price)}\nTo'ladi: {len(ext) - len(unpaid)}/{len(ext)} · "
            f"{S.fmt_som(sum(z.paid for z in ext))}" + ("\n\nTo'lamaganlar:" if unpaid else "\n\nHammasi to'ladi ✅"))
    await msg.answer(text, reply_markup=IM(inline_keyboard=[
        [IB(text=f"{z.name[:24]} · 💵 naqd", callback_data=f"mqpm:{z.id}:naqd"),
         IB(text="💳 karta", callback_data=f"mqpm:{z.id}:karta")] for z in unpaid[:30]]
        + [[IB(text="⬅️ Karta", callback_data=f"mqc:{mid}")]]))


@router.callback_query(F.data.startswith("mqpm:"))
async def pay(c: CallbackQuery) -> None:
    _, sid, method = c.data.split(":")
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        if not st or _role(st) not in ("admin", "rahbar", "moliya"):
            return await c.answer("Faqat admin / moliya.", show_alert=True)
        z = await s.get(MocSeat, int(sid))
        moc = await s.get(Moc, z.moc_id)
        if z.paid >= moc.price:
            return await c.answer("Allaqachon to'lagan.", show_alert=True)
        sv = await moc_service(s)
        s.add(ServiceSale(service_id=sv.id, qty=1, amount=moc.price, profit=moc.price, method=method, by_id=st.id))
        z.paid, z.pay_method = moc.price, method
        await S.audit(s, st, "moc_tolov", seat=z.id, amount=moc.price, method=method)
        await s.commit()
    await c.answer(f"✅ {S.fmt_som(moc.price)} qabul qilindi")
    await _pay_view(c.message, z.moc_id)


# ---------------- qayta chop etish va bekor qilish ----------------
@router.callback_query(F.data.startswith("mqr:"))
async def reprint(c: CallbackQuery, bot: Bot) -> None:
    mid = int(c.data.split(":")[1])
    async with Session() as s:
        ids = [z.id for z in await _seats(s, mid) if not z.answers]
    await c.answer()
    if not ids:
        return await c.message.answer("Hamma varaqlar allaqachon tekshirilgan.")
    await c.message.answer(f"⏳ {len(ids)} ta tekshirilmagan varaq qayta tayyorlanmoqda…")
    await issue(bot, mid, ids, c.from_user.id, booklets=False)


@router.callback_query(F.data.startswith("mqdel:"))
async def cancel_ask(c: CallbackQuery) -> None:
    mid = int(c.data.split(":")[1])
    await c.answer()
    await c.message.answer("🗑 Imtihon bekor qilinsinmi? Kiritilgan javoblar ham o'chadi.", reply_markup=IM(inline_keyboard=[
        [IB(text="✅ Ha, bekor qilish", callback_data=f"mqdy:{mid}"), IB(text="↩️ Yo'q", callback_data=f"mqc:{mid}")]]))


@router.callback_query(F.data.startswith("mqdy:"))
async def cancel_yes(c: CallbackQuery) -> None:
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        moc = await s.get(Moc, int(c.data.split(":")[1]))
        if not moc or moc.status == "yakun" or not st or _role(st) not in MAKERS:
            return await c.answer("Bekor qilib bo'lmaydi.", show_alert=True)
        moc.status = "bekor"
        await S.audit(s, st, "moc_bekor", id=moc.id)
        await s.commit()
    await c.answer("Bekor qilindi")
    await c.message.edit_text(f"🗑 «{moc.title}» bekor qilindi.")


# ======================= yakunlash =======================
@router.callback_query(F.data.startswith("mqz:"))
async def finish(c: CallbackQuery, bot: Bot) -> None:
    parts = c.data.split(":")
    mid, force = int(parts[1]), len(parts) > 2
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        moc = await s.get(Moc, mid)
        if not st or _role(st) not in MAKERS or not moc or moc.status != "tayyor":
            return await c.answer("Yakunlab bo'lmaydi.", show_alert=True)
        sp = _spec(moc)
        seats = await _seats(s, mid)
        baho = [x.name for x in sp.sections if x.kind == "baho"]
        bad = [z for z in seats if z.answers and (z.unsure or not z.variant)]
        if bad:
            await c.answer()
            return await c.message.answer(f"⚠️ {len(bad)} ta varaqda noaniq javob yoki variant bor — avval hal qiling.",
                                          reply_markup=IM(inline_keyboard=[[IB(text="⚠️ Hal qilish",
                                                                               callback_data=f"mqu:{bad[0].id}")]]))
        left = [z for z in seats if z.name and not z.answers and not z.absent and not (baho and z.baho)]
        miss = [z for z in seats if baho and z.name and not z.absent and (z.answers or z.baho)
                and any(n not in (z.baho or {}) for n in baho)]
        if (left or miss) and not force:
            await c.answer()
            txt = []
            if left:
                txt.append(f"{len(left)} ta qatnashchining javobi kiritilmagan — «qatnashmadi» hisoblanadi.")
            if miss:
                txt.append(f"{len(miss)} ta qatnashchida {', '.join(baho)} bali yo'q — 0 hisoblanadi.")
            return await c.message.answer("⚠️ " + "\n".join(txt) + "\nBaribir yakunlaymizmi?", reply_markup=IM(
                inline_keyboard=[[IB(text="✅ Ha, yakunlash", callback_data=f"mqz:{mid}:ok"),
                                  IB(text="⬅️ Orqaga", callback_data=f"mqc:{mid}")]]))
    await c.answer("Hisoblanmoqda…")
    await c.message.edit_reply_markup(reply_markup=None)
    n_sent, summary, xls = await compute_and_send(bot, mid, st.id)
    await c.message.answer(summary)
    await bot.send_document(c.message.chat.id, BufferedInputFile(xls, f"MOC{mid}_natijalar.xlsx"),
                            caption=f"📊 Natijalar va savollar tahlili · {n_sent} ta manzilga yuborildi")
    async with Session() as s:
        kind = (await s.get(Moc, mid)).kind
    if kind in ("moc", "tashqi"):                        # nazorat (PDF test) — guruh sertifikatlari 48 soatdan so'ng
        from app.bot.certs import moc_certs
        await moc_certs(bot, mid)


def _res_text(moc: Moc, sp: M.Spec, z: MocSeat, n: int) -> str:
    r = z.result
    lines = [f"{KIND_ICON.get(moc.kind, '🎓')} <b>{moc.title}</b> · {moc.date:%d.%m.%Y}", f"{z.name}",
             f"Natija: <b>{M.fmt_total(sp, r)}</b>" + (f" · <b>{r['level']}</b>" if r.get("level") else "")
             + (f"\n{'Markazda' if r.get('ext_rank') else 'O`rin'}: {r['rank']}"
                + (f"-o'rin ({n} kishidan)" if r.get("ext_rank") else f" / {n}") if r.get("rank") else "")
             + (f"\n🌍 Umumiy reytingda: {r['ext_rank']}-o'rin" + (f" ({r['ext_of']} ta qatnashchidan)" if r.get("ext_of") else "")
                if r.get("ext_rank") else "")]
    if sp.mode == "rasch" and r.get("raw_total") is not None:
        lines.append(f"Xom ball: {r['primary']:g} / {r['primary_max']:g} (oddiy hisobda {r['raw_total']:g})")
    for x in r["sections"]:
        if x["kind"] == "baho":
            lines.append(f"• {x['name']}: {x['raw'] if x['raw'] is not None else '—'}")
        elif sp.mode in ("cefr", "ielts"):
            lines.append(f"• {x['name']}: {x['raw']}/{x['raw_max']} → {x.get('scaled')}")
        else:
            lines.append(f"• {x['name']}: {x['raw']:g}/{x['raw_max']:g}" + (f" · {x['score']:g} ball" if sp.mode == "ball" else ""))
    wrong = r.get("wrong") or []
    if wrong:
        lines.append("\nXato javoblar: " + ", ".join(wrong[:40]) + (" …" if len(wrong) > 40 else ""))
    dbl = (z.answers or "").count("*")
    if dbl:
        lines.append(f"⚠️ {dbl} ta savolda ikkita javob belgilangan — xato hisoblandi.")
    if sp.mode == "shkala" and sp.levels:
        lines.append("\n<i>Ball taxminiy: rasmiy imtihonda Rasch modeli qo'llanadi.</i>")
    if sp.mode == "rasch":
        lines.append("\n<i>Rasch modeli bo'yicha hisoblangan mock bali — rasmiy imtihon natijasining bashorati.</i>")
    return "\n".join(lines)


def _card_png(moc: Moc, sp: M.Spec, z: MocSeat, n: int) -> bytes:
    from app.logic import moc_card
    r = z.result
    secs = []
    for x in r["sections"]:
        if x["kind"] == "baho":
            v = x["raw"] or 0
            secs.append((x["name"], f"{v:g} / {x['raw_max']:g}", v / x["raw_max"] * 100 if x["raw_max"] else 0))
        elif sp.mode == "ball":
            secs.append((x["name"], f"{x['score']:g} / {x['max']:g}", x["score"] / x["max"] * 100 if x["max"] else 0))
        else:
            secs.append((x["name"], f"{x['raw']:g} / {x['raw_max']:g}", x["raw"] / x["raw_max"] * 100 if x["raw_max"] else 0))
    note = "Taxminiy ball: rasmiy imtihonda Rasch modeli qo'llanadi." if sp.mode == "shkala" and sp.levels else ""
    if sp.mode == "rasch":
        note = f"Rasch modeli · xom ball {r.get('primary', 0):g}/{r.get('primary_max', 0):g}"
    return moc_card.render(moc.title, f"{moc.date:%d.%m.%Y}", z.name or "", M.fmt_total(sp, r), r.get("level"),
                           r["percent"], secs, r.get("rank"), n, note)


async def _deliver(bot: Bot, moc: Moc, sp: M.Spec, z: MocSeat, n: int, tgs: list[int]) -> int:
    if not tgs:
        return 0
    png = _card_png(moc, sp, z, n)
    text = _res_text(moc, sp, z, n)
    k = 0
    for t in tgs:
        try:
            await bot.send_photo(t, BufferedInputFile(png, "natija.png"), caption=text[:1024])
            if len(text) > 1024:
                await bot.send_message(t, text[1024:])
            k += 1
        except Exception:
            pass
    return k


async def compute_and_send(bot: Bot, mid: int, staff_id: int) -> tuple[int, str, bytes]:
    async with Session() as s:
        moc = await s.get(Moc, mid)
        sp = _spec(moc)
        seats = await _seats(s, mid)
        present = [z for z in seats if not z.absent and z.name and (z.answers or z.baho)]
        for z in present:
            key = (moc.keys or {}).get(z.variant or "A") or "." * sp.n_questions
            r = M.score(sp, key, z.answers or "-" * sp.n_questions, z.baho or {})
            wrong = []
            for q, (k, _) in enumerate(sp.bubbles(), 1):
                a = (z.answers or "")[q - 1:q] or "-"
                if k == "t" and a != key[q - 1]:
                    wrong.append(f"{q} ({'—' if a == '-' else 'ikkita' if a == '*' else a}→{key[q - 1]})")
            r["wrong"] = wrong
            r["pq"] = [round(x, 3) for x in r.pop("per_q", [])]        # savollar tahlili uchun
            ext = (z.baho or {}).get("_ext") or {}
            if ext.get("lvl") and sp.levels:              # tashqi kanal bergan daraja — o'shani qo'yamiz
                r["level"] = ext["lvl"]
            if ext.get("rank"):
                r["ext_rank"], r["ext_of"] = ext["rank"], ext.get("of")
            z.result = r
        rinfo = await _rasch(s, moc, sp, present) if sp.mode == "rasch" and present else None
        order = sorted(present, key=lambda z: -z.result["total"])
        from app.bot.certs import dense
        ranks = dense({z.id: z.result["total"] for z in present})      # teng natija — teng o'rin
        for z in order:
            z.result = {**z.result, "rank": ranks[z.id]}
        from app.bot.analysis import moc_report
        rep = moc_report(moc, sp, present)
        if rep and sp.topics:
            for z in present:
                if z.id in rep.students:
                    z.result = {**z.result, "topics": rep.students[z.id]["topics"]}
        moc.status = "yakun"
        st = await s.get(Staff, staff_id)
        if moc.kind in ("nazorat", "tashqi"):
            await _to_exam(s, moc, present, st)
        await S.audit(s, st, "moc_yakun", id=moc.id, n=len(present))
        await s.commit()
        targets = []
        for z in present:
            tgs = await S.parent_tg_ids(s, z.student_id) if z.student_id else []
            if z.tg_id and z.tg_id not in tgs:
                tgs.append(z.tg_id)
            targets.append((z, tgs))
    n = len(present)
    sent = 0
    for z, tgs in targets:
        k = await _deliver(bot, moc, sp, z, n, tgs)
        if k:
            sent += k
            async with Session() as s:
                zz = await s.get(MocSeat, z.id)
                zz.sent_at = datetime.utcnow()
                await s.commit()
    if rep and moc.kind != "tashqi":
        from app.bot.analysis import after_moc_finish
        try:
            await after_moc_finish(bot, moc, sp, rep, targets, st.tg_id if st else None)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("MOC tahlili")
    totals = [z.result["total"] for z in present]
    avg = sum(totals) / len(totals) if totals else 0
    levels: dict[str, int] = {}
    for z in present:
        if z.result.get("level"):
            levels[z.result["level"]] = levels.get(z.result["level"], 0) + 1
    top = "\n".join(f"{z.result['rank']}. {z.name} — {M.fmt_total(sp, z.result)}" for z in order[:5])
    wait_ext = sum(1 for z, t in targets if not t and not z.student_id)
    wait_grp = sum(1 for z, t in targets if not t and z.student_id)
    summary = (f"🏁 <b>{moc.title}</b> yakunlandi\nQatnashdi: {n} · o'rtacha: {avg:.1f}"
               + (f" / {present[0].result['total_max']:g}" if present else "")
               + ("\nDarajalar: " + ", ".join(f"{k} — {v}" for k, v in levels.items()) if levels else "")
               + (f"\n\n🏆 Eng yaxshilar:\n{top}" if top else "")
               + (_rasch_note(rinfo) if rinfo else "")
               + f"\n\n📨 Natija yuborildi: {sent} ta manzilga"
               + (f"\n⏳ {wait_ext} ta tashqi qatnashchi hali Telegramga ulanmagan — chiptadagi QR ni skanerlasa, "
                  "natija o'sha zahoti boradi." if wait_ext else "")
               + (f"\n⏳ {wait_grp} ta o'quvchining ota-onasi botga ulanmagan — natijani «👥 Qatnashchilar» yoki "
                  "Excel'dan ko'rasiz." if wait_grp else ""))
    return sent, summary, excel(moc, sp, seats)


async def _to_exam(s, moc: Moc, present: list[MocSeat], st: Staff | None) -> None:
    """Nazorat / tashqi test: har bir guruhning haftalik nazorati (Exam) va reyting/coin tizimiga yoziladi."""
    gids = list(moc.group_ids or [])
    if not gids:
        return
    eg = {e.id: e.group_id for e in (await s.execute(select(Enrollment).where(
        Enrollment.id.in_([z.enrollment_id for z in present if z.enrollment_id] or [0])))).scalars()}
    done = dict((moc.meta or {}).get("exams") or {})
    for gid in gids:
        mine = [z for z in present if z.enrollment_id and eg.get(z.enrollment_id) == gid]
        if not mine:
            continue
        eid = await _to_exam_one(s, moc, gid, mine, st,
                                 done.get(str(gid)) or (moc.exam_id if len(gids) == 1 else None))
        done[str(gid)] = eid
    moc.meta = {**(moc.meta or {}), "exams": done}
    if len(gids) == 1 and done:
        moc.exam_id = next(iter(done.values()))


async def _to_exam_one(s, moc: Moc, gid: int, present: list[MocSeat], st: Staff | None, eid: int | None) -> int:
    from app.bot.exams import _apply_coins
    ex = await s.get(Exam, eid) if eid else None
    if not ex:
        ex = (await s.execute(select(Exam).where(Exam.group_id == gid, Exam.status == "reja",
                                                 Exam.date >= moc.date - timedelta(days=7),
                                                 Exam.date <= moc.date + timedelta(days=1))
                              .order_by(Exam.date.desc()))).scalars().first()
    if not ex:
        week = moc.date - timedelta(days=moc.date.weekday())
        n = len((await s.execute(select(Exam.id).where(Exam.group_id == gid, Exam.week == week))).all())
        ex = Exam(group_id=gid, date=moc.date, week=week, slot=n + 1, conductor_id=st.id if st else None, kind="test")
        s.add(ex)
        await s.flush()
    ex.kind, ex.status, ex.done_at, ex.max_score, ex.topic = "test", "otdi", now().replace(tzinfo=None), 100.0, moc.title[:200]
    rules = await S.get_rules(s)
    for z in present:
        r = (await s.execute(select(ExamResult).where(ExamResult.exam_id == ex.id,
                                                      ExamResult.enrollment_id == z.enrollment_id))).scalar_one_or_none()
        if not r:
            r = ExamResult(exam_id=ex.id, enrollment_id=z.enrollment_id)
            s.add(r)
        r.percent = z.result["percent"]
        r.score = z.result["percent"]
        r.answers = (z.answers or "")[:400]
        r.topics = z.result.get("topics")
        await s.flush()
        stu = await s.get(Student, z.student_id)
        await _apply_coins(s, r, stu, rules, st, ex)
    return ex.id


def excel(moc: Moc, sp: M.Spec, seats: list[MocSeat]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Natijalar"
    secs = [x.name for x in sp.sections]
    rasch = sp.mode == "rasch"
    ws.append(["O'rin", "F.I.Sh", "Kod", "Variant", "Telefon"] + secs + ["Jami", "Foiz", "Daraja", "Holat"]
              + (["Xom ball", "θ (logit)", "Xato (SE)"] if rasch else []))
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0E2A47")
    rows = sorted(seats, key=lambda z: (z.result is None, -(z.result or {}).get("total", 0)))
    for z in rows:
        r = z.result or {}
        vals = []
        for x in r.get("sections", []):
            if x["kind"] == "baho":
                vals.append(x["raw"])
            elif sp.mode in ("cefr", "ielts"):
                vals.append(x.get("scaled"))
            else:
                vals.append(round(x["score"], 2) if sp.mode == "ball" else x["raw"])
        if not r:
            vals = [None] * len(secs)
        ws.append([r.get("rank"), z.name or "(bo'sh varaq)", z.code, z.variant, z.phone] + vals
                  + [r.get("total"), r.get("percent"), r.get("level"),
                     "qatnashmadi" if z.absent else ("" if r else "kiritilmagan")]
                  + ([r.get("raw_total"), r.get("theta"), r.get("se")] if rasch else []))
    for col, wd in zip("ABCDE", (6, 30, 11, 8, 14)):
        ws.column_dimensions[col].width = wd
    # savollar tahlili
    wa = wb.create_sheet("Savollar tahlili")
    wa.append(["Savol", "Bo'lim", "Variant", "To'g'ri, %", "Javob berganlar", "Eng ko'p tanlangan xato"]
              + (["Qiyinlik (logit)"] if rasch else []))
    diff = ((moc.meta or {}).get("rasch") or {}).get("items") or {}
    for cell in wa[1]:
        cell.font = Font(bold=True)
    kinds = sp.bubbles()
    for v in VARS[:moc.n_variants]:
        group = [z for z in seats if z.variant == v and z.answers and not z.absent]
        if not group:
            continue
        key = (moc.keys or {}).get(v) or "." * sp.n_questions
        for q, (k, nb) in enumerate(kinds, 1):
            sec = sp.section_of(q)
            ans = [z.answers[q - 1] for z in group]
            if k == "t":
                ok = sum(1 for a in ans if a == key[q - 1])
                wrong: dict[str, int] = {}
                for a in ans:
                    if a != key[q - 1] and a in VARS:
                        wrong[a] = wrong.get(a, 0) + 1
                common = max(wrong, key=wrong.get) if wrong else ""
                wa.append([q, sec.name if sec else "", v, round(ok / len(ans) * 100), len(ans),
                           f"{common} ({wrong[common]})" if common else ""] + ([diff.get(f"{v}{q}")] if rasch else []))
            else:
                got = sum(int(a) for a in ans if a.isdigit())
                wa.append([q, sec.name if sec else "", v, round(got / (len(ans) * nb) * 100), len(ans), "yozma"]
                          + ([diff.get(f"{v}{q}")] if rasch else []))
    wa.column_dimensions["B"].width = 24
    wa.column_dimensions["F"].width = 22
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@router.callback_query(F.data.startswith("mqxl:"))
async def excel_cb(c: CallbackQuery, bot: Bot) -> None:
    mid = int(c.data.split(":")[1])
    async with Session() as s:
        moc = await s.get(Moc, mid)
        seats = await _seats(s, mid)
    await c.answer()
    await bot.send_document(c.message.chat.id, BufferedInputFile(excel(moc, _spec(moc), seats), f"MOC{mid}_natijalar.xlsx"))


# ======================= chipta: natijani Telegramda olish =======================
async def claim(m: Message, arg: str, bot: Bot) -> bool:
    """/start r<o'rin>_<token> — tashqi qatnashchi chiptadagi QR ni skanerladi."""
    try:
        sid_s, tok = arg[1:].split("_", 1)
        sid = int(sid_s)
    except ValueError:
        return False
    async with Session() as s:
        z = await s.get(MocSeat, sid)
        if not z or z.token != tok:
            await m.answer("Chipta topilmadi yoki eskirgan.")
            return True
        z.tg_id = m.from_user.id
        await s.commit()
        moc = await s.get(Moc, z.moc_id)
        n = len([x for x in await _seats(s, moc.id) if x.result])
    sp = _spec(moc)
    if z.result:
        await _deliver(bot, moc, sp, z, n, [m.from_user.id])
        return True
    await m.answer(f"✅ Siz «{moc.title}» ({moc.date:%d.%m.%Y}) qatnashchisisiz — kod {z.code}.\n"
                   "Natija tayyor bo'lishi bilan shu yerga keladi.")
    return True


# ======================= Rasch modeli =======================
CAL_KEY = "rasch_cal"


def _item_prefix(moc: Moc, v: str) -> str:
    """Savollar banki: bir xil PDF (Telegram fayl uid) qaysi imtihonda bo'lmasin — bitta savol to'plami."""
    b = (moc.booklets or {}).get(v) or {}
    return f"u{b['uid']}" if b.get("uid") else f"m{moc.id}{v}"


def _response(sp: M.Spec, key: str, answers: str, prefix: str, baho: dict | None = None) -> tuple[dict, dict]:
    """Rasch uchun javoblar. Varaqdagi savollar — o'z bali; yozma ish / esse (botga yozilgan ball) —
    0..5 toifaga keltirilib qo'shiladi (aks holda katta yozma ish natijaga ta'sir qilmay qolardi)."""
    resp, mc = {}, {}
    for j, s in enumerate(sp.sections):
        if s.kind == "baho" and s.mx and (baho or {}).get(s.name) is not None:
            iid = f"{prefix}:b{j}"
            resp[iid], mc[iid] = int(round(max(0.0, min(float(baho[s.name]), s.mx)) / s.mx * 5)), 5
    for q, (k, nb) in enumerate(sp.bubbles(), 1):
        a = answers[q - 1] if len(answers) >= q else "-"
        iid = f"{prefix}:{q}"
        if k == "t":
            resp[iid], mc[iid] = int(a == key[q - 1]), 1
        else:
            resp[iid], mc[iid] = (int(a) if a.isdigit() else 0), nb
    return resp, mc


async def _cal_points(s, family: str) -> list:
    row = await s.get(Setting, CAL_KEY)
    return list(((row.value or {}) if row else {}).get(family) or [])


async def _rasch(s, moc: Moc, sp: M.Spec, present: list[MocSeat]) -> dict:
    """Hamma qatnashchilar (+ shu PDF ishlatilgan oldingi imtihonlar) javoblaridan Rasch hisobi."""
    from app.logic import rasch as RS
    responses, maxcat = [], {}
    for z in present:
        key = (moc.keys or {}).get(z.variant or "A") or "." * sp.n_questions
        r, mc = _response(sp, key, z.answers or "-" * sp.n_questions, _item_prefix(moc, z.variant or "A"), z.baho)
        responses.append(r)
        maxcat.update(mc)
    uids = {b.get("uid") for b in (moc.booklets or {}).values() if b.get("uid")}
    pooled = 0
    if uids:
        others = (await s.execute(select(Moc).where(Moc.status == "yakun", Moc.id != moc.id))).scalars().all()
        for o in others:
            if M.parse_spec(o.spec).layout() != sp.layout():
                continue
            vs = [v for v, b in (o.booklets or {}).items() if b.get("uid") in uids]
            if not vs:
                continue
            for z in await _seats(s, o.id):
                if z.answers and not z.absent and z.variant in vs:
                    r, mc = _response(sp, (o.keys or {}).get(z.variant) or "." * sp.n_questions, z.answers,
                                      _item_prefix(o, z.variant), z.baho)
                    responses.append(r)
                    maxcat.update(mc)
                    pooled += 1
    fit = RS.fit(responses, maxcat)
    n = len(present)
    th = fit.theta[:n]
    pts = await _cal_points(s, sp.name)
    if len(pts) >= 5:
        a, b = RS.linear_scale([p[0] for p in pts], [p[1] for p in pts])
        how = f"rasmiy natijalar bo'yicha sozlangan ({len(pts)} ta)"
    else:
        raw = [z.result["total"] for z in present]
        a, b = RS.linear_scale(th, raw)
        how = "xom ball darajasiga moslangan (rasmiy natijalar hali kiritilmagan)"
    for z, t_, se in zip(present, th, fit.se[:n]):
        ball = round(float(min(sp.scale, max(0.0, a + b * t_))), 1)
        r = dict(z.result)
        r.update(raw_total=r["total"], total=ball, theta=round(float(t_), 3), se=round(float(se), 2),
                 percent=round(ball / sp.scale * 100, 1), level=M.level_of(sp, ball))
        z.result = r
    items = {}
    for v in VARS[:moc.n_variants]:
        pre = _item_prefix(moc, v)
        for q in range(1, sp.n_questions + 1):
            d = fit.difficulty.get(f"{pre}:{q}")
            if d is not None:
                items[f"{v}{q}"] = round(d, 2)
    info = {"n": n, "pooled": pooled, "a": round(a, 3), "b": round(b, 3), "cal": len(pts), "how": how,
            "converged": bool(fit.converged), "items": items}
    moc.meta = {**(moc.meta or {}), "rasch": info}
    return info


def _rasch_note(info: dict) -> str:
    out = (f"\n\n📐 <b>Rasch modeli</b>: {info['n']} qatnashchi"
           + (f" + oldingi imtihonlardan {info['pooled']} ta javob varag'i (shu PDF savollari)" if info["pooled"] else "")
           + f"\nShkala: {info['how']}.")
    if info["n"] + info["pooled"] < 30:
        out += "\n⚠️ Qatnashchilar 30 tadan kam — savollar qiyinligi aniq emas, Rasch bali taxminiyroq."
    if info["cal"] < 5:
        out += ("\n💡 O'quvchilaringiz rasmiy imtihonni topshirgach, ularning ballini «📐 Rasch sozlash» ga "
                "kiriting — keyingi mock'lar rasmiy natijaga yaqinlashadi.")
    return out


@router.callback_query(F.data == "mq:cal")
async def cal_menu(c: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        if _role(st) != "rahbar":
            return await c.answer("Faqat rahbar.", show_alert=True)
        fams = []
        for m in (await s.execute(select(Moc).where(Moc.status == "yakun"))).scalars():
            sp = _spec(m)
            if sp.mode == "rasch" and sp.name not in fams:
                fams.append(sp.name)
        row = await s.get(Setting, CAL_KEY)
        cal = (row.value or {}) if row else {}
    await state.update_data(fams=fams)
    await c.answer()
    if not fams:
        return await c.message.answer(
            "📐 <b>Bu bo'lim nima uchun?</b>\n"
            "Bot mock'da qo'ygan ballni rasmiy Milliy sertifikat baliga yaqinlashtirish uchun.\n\n"
            "<b>Qanday ishlaydi:</b>\n"
            "1️⃣ Milliy sertifikat formatida mock o'tkazasiz — bot har o'quvchiga ball qo'yadi.\n"
            "2️⃣ O'quvchilar rasmiy imtihonni topshiradi.\n"
            "3️⃣ Ularning rasmiy ballini shu yerga yozasiz: <code>Aliyev Vali 66</code>\n"
            "4️⃣ 5 ta natija yig'ilgach, bot keyingi mock'larni rasmiy natijaga moslab hisoblaydi.\n\n"
            "⏳ Hozircha Milliy sertifikat formatida yakunlangan mock yo'q — avval mock o'tkazing. "
            "Hozir hech narsa qilish shart emas.")
    lines = ["📐 <b>Mock ballini rasmiy natijaga moslash</b>\n",
             "O'quvchilaringiz rasmiy imtihonni topshirgach, ularning <b>rasmiy ballini</b> shu yerga yozing. "
             "Bot uni o'quvchining oxirgi mock bali bilan solishtiradi. 5 ta natija yig'ilgach, keyingi mock'lar "
             "rasmiy natijaga yaqinroq hisoblanadi.\n"]
    for f in fams:
        pts = cal.get(f) or []
        lines.append(f"• {f}: {len(pts)} ta rasmiy natija" + (" ✅ sozlangan" if len(pts) >= 5 else ""))
    await c.message.answer("\n".join(lines), reply_markup=IM(inline_keyboard=[
        [IB(text=f"➕ {f[:40]}", callback_data=f"mqcf:{i}")] for i, f in enumerate(fams)]
        + [[IB(text=f"🗑 {f[:36]} — tozalash", callback_data=f"mqcx:{i}")] for i, f in enumerate(fams) if cal.get(f)]))


@router.callback_query(F.data.startswith("mqcf:"))
async def cal_ask(c: CallbackQuery, state: FSMContext) -> None:
    fams = (await state.get_data()).get("fams") or []
    i = int(c.data.split(":")[1])
    if i >= len(fams):
        return await c.answer("Qaytadan oching.", show_alert=True)
    await state.update_data(fam=fams[i])
    await state.set_state(Mc.cal)
    await c.answer()
    await c.message.answer(f"📐 <b>{fams[i]}</b> — rasmiy natijalarni yozing, har qatorda bitta:\n"
                           "<code>Qodirova Charos 66.4\nAliyev Vali 58</code>\n"
                           "Bot har birini shu formatdagi oxirgi mock natijasi bilan bog'laydi.")


def _norm(x: str) -> str:
    return " ".join(sorted(w for w in x.lower().replace("ʻ", "'").replace("`", "'").split() if w))


@router.message(Mc.cal, F.text, ~F.text.in_(K.menu_texts()))
async def cal_save(m: Message, state: FSMContext) -> None:
    from app.logic import rasch as RS
    fam = (await state.get_data()).get("fam")
    rows = []
    for line in m.text.splitlines():
        bits = line.replace(",", ".").split()
        if len(bits) < 2:
            continue
        try:
            ball = float(bits[-1])
        except ValueError:
            continue
        if 0 <= ball <= 100:
            rows.append((" ".join(bits[:-1]), ball))
    if not rows:
        return await m.answer("Namuna: <code>Qodirova Charos 66.4</code>")
    await state.set_state(None)
    async with Session() as s:
        st = await _staff(s, m.from_user.id)
        mocs = [x for x in (await s.execute(select(Moc).where(Moc.status == "yakun").order_by(Moc.date.desc(), Moc.id.desc())))
                .scalars() if _spec(x).name == fam and _spec(x).mode == "rasch"]
        latest: dict[str, tuple] = {}
        for x in mocs:
            for z in await _seats(s, x.id):
                if z.name and z.result and z.result.get("theta") is not None and _norm(z.name) not in latest:
                    latest[_norm(z.name)] = (z.result["theta"], x.title, x.date)
        row = await s.get(Setting, CAL_KEY)
        cal = dict((row.value or {}) if row else {})
        pts = list(cal.get(fam) or [])
        ok, miss = [], []
        for name, ball in rows:
            hit = latest.get(_norm(name))
            if not hit:
                miss.append(name)
                continue
            pts = [p for p in pts if p[2] != _norm(name)] + [[hit[0], ball, _norm(name)]]
            ok.append(f"{name}: mock θ={hit[0]:+.2f} ({hit[1][:24]}, {hit[2]:%d.%m}) → rasmiy {ball:g}")
        cal[fam] = pts
        if row:
            row.value = cal
        else:
            s.add(Setting(key=CAL_KEY, value=cal))
        await S.audit(s, st, "rasch_sozlash", family=fam, n=len(ok))
        await s.commit()
    text = f"📐 <b>{fam}</b>: {len(ok)} ta natija qo'shildi, jami {len(pts)} ta."
    if ok:
        text += "\n" + "\n".join("• " + x for x in ok[:15])
    if miss:
        text += "\n\n❓ Mock natijasi topilmadi: " + ", ".join(miss[:15])
    if len(pts) >= 5:
        a, b = RS.linear_scale([p[0] for p in pts], [p[1] for p in pts])
        text += (f"\n\n✅ Shkala sozlandi: ball = {a:.1f} {'+' if b >= 0 else '−'} {abs(b):.1f}·θ\n"
                 f"Masalan: θ=0 → {a:.1f}, θ=+1 → {a + b:.1f}. Keyingi mock'lar shu bilan hisoblanadi.")
    else:
        text += f"\n\nShkala sozlanishi uchun yana {5 - len(pts)} ta natija kerak."
    await m.answer(text)


@router.callback_query(F.data.startswith("mqcx:"))
async def cal_clear(c: CallbackQuery, state: FSMContext) -> None:
    fams = (await state.get_data()).get("fams") or []
    i = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        if _role(st) != "rahbar" or i >= len(fams):
            return await c.answer("Ruxsat yo'q.", show_alert=True)
        row = await s.get(Setting, CAL_KEY)
        if row:
            cal = dict(row.value or {})
            cal.pop(fams[i], None)
            row.value = cal
            await s.commit()
    await c.answer("Tozalandi")
    await c.message.edit_text(f"🗑 «{fams[i]}» sozlamasi tozalandi.")
