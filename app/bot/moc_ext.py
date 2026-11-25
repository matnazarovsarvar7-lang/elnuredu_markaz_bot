"""🌐 Tashqi tekshiruvli test: test sotib olingan kanal javoblarni o'zi tekshiradi, markaz faqat natijani yuklaydi.

Natija qanday kelsa ham qabul qilinadi: Excel / CSV / PDF fayl, oddiy matn yoki kanal xabarlarini forward qilish
(bir nechtasini ketma-ket ham). Bot ismlarni (lotin/kirill, tartibi har xil, otasining ismi bilan) qatnashchilarga
moslaydi, shubhalilarini so'raydi, kelmaganlarni belgilaydi. Keyin odatdagi «🏁 Yakunlash» — ota-onaga natija
kartasi, guruh reytingi (haftalik nazorat), darajali sertifikatlar.

Xuddi shu yuklash istalgan MOC dagi yagona «baho» bo'limi uchun ham ishlaydi (masalan Writing ballari).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM,
                           Message)
from sqlalchemy import select

from app import services as S
from app.bot import keyboards as K
from app.bot.notify import send
from app.config import settings
from app.db.models import Moc, MocSeat, PrintJob
from app.db.session import Session
from app.logic import extres as E
from app.logic import moc as M
from app.logic import printing as P
from app.logic import sheet2 as S2

router = Router()
_LOCK: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_GEN: dict[int, int] = {}


class Xr(StatesGroup):
    imp = State()


def _baho(moc: Moc) -> M.Section | None:
    xs = [x for x in M.parse_spec(moc.spec).sections if x.kind == "baho"]
    return xs[0] if len(xs) == 1 else None


# ======================= chop etish (savollar + chiptalar) =======================
async def issue_ext(bot: Bot, mid: int, seat_ids: list[int], tg: int, extra: bool = False) -> None:
    """Javob varag'i yo'q: faqat sotib olingan test PDF (qatnashchi soniga qarab) va tashqi qatnashchilarga chipta."""
    from app.bot import moc as MB
    from app.bot import prints as PR
    uname = await MB._bot_username(bot)
    async with Session() as s:
        moc = await s.get(Moc, mid)
        st = await MB._staff(s, tg)
        seats = [await s.get(MocSeat, i) for i in seat_ids]
        tickets = [S2.Ticket(moc.title, z.name, z.code, "", f"{moc.date:%d.%m.%Y}",
                             f"https://t.me/{uname}?start=r{z.id}_{z.token}")
                   for z in seats if not z.enrollment_id and z.name and uname]
    jobs = []
    b = (moc.booklets or {}).get("A")
    if b and seats:
        pages = int(b.get("pages") or 1)
        jobs.append(("Test savollari" + (" (qo'shimcha)" if extra else ""), b["file_id"], pages, len(seats),
                     "ikki" if pages > 1 else "bir"))
    if tickets:
        tp = S2.tickets_pdf(tickets, settings.center_name)
        t_msg = await bot.send_document(tg, BufferedInputFile(tp, f"Test{mid}_chiptalar.pdf"),
                                        caption=f"🎫 Chiptalar: {len(tickets)} ta — tashqi qatnashchiga beriladi, QR orqali "
                                                "natijani Telegramda oladi.")
        jobs.append(("Qatnashchi chiptalari (qirqiladi)", t_msg.document.file_id, -(-len(tickets) // 8), 1, "bir"))
    if not jobs:
        return
    async with Session() as s:
        made = []
        for note, fid, pages, copies, layout in jobs:
            j = PrintJob(kind="ichki", staff_id=st.id if st else None, copies=copies, pages=pages, layout=layout,
                         sheets=P.sheets(pages, layout), file_id=fid, file_kind="pdf",
                         note=f"🌐 {moc.title[:60]}: {note}"[:300])
            s.add(j)
            made.append(j)
        await s.commit()
        cards = [(j, *(await PR._job_card(s, j))) for j in made]
        to = await PR._printers(s)
    for j, cap, kb in cards:
        for t in to:
            await PR._send_file(bot, t, j.file_id, j.file_kind, cap, kb)
    await send(bot, tg, f"🖨 Adminga {len(made)} ta chop etish so'rovi yuborildi.")


# ======================= karta =======================
async def ext_card(msg: Message, moc: Moc, sp: M.Spec, seats: list[MocSeat], st, unpaid, payers, edit: bool) -> None:
    from app.bot import moc as MB
    named = [z for z in seats if z.name]
    got = [z for z in named if z.baho and not z.absent]
    absent = [z for z in named if z.absent]
    wait = [z for z in named if not z.baho and not z.absent]
    sec = _baho(moc)
    lines = [f"🌐 <b>{moc.title}</b> · {moc.date:%d.%m.%Y}" + (" · ✅ yakunlangan" if moc.status == "yakun" else ""),
             f"Tashqi tekshiruv · natija 0–{sec.mx:g}" + (f" · darajali" if sp.levels else ""),
             f"\n📥 Natija kiritildi: <b>{len(got)}</b> / {len(named)}"
             + (f" · kelmadi: {len(absent)}" if absent else "")]
    if wait and moc.status != "yakun":
        lines.append(f"⏳ Natijasi yo'q: {len(wait)}")
    if moc.price:
        lines.append(f"💵 To'lov: {len(payers) - len(unpaid)}/{len(payers)} · {S.fmt_som(sum(z.paid for z in payers))}")
    rows = []
    r = MB._role(st)
    mid = moc.id
    if moc.status != "yakun":
        rows.append([IB(text="📥 Natijani yuklash (fayl / matn / forward)", callback_data=f"xri:{mid}")])
        rows.append([IB(text="✍️ Qo'lda kiritish", callback_data=f"mqh:{mid}"),
                     IB(text="➕ Qatnashchi", callback_data=f"mqa:{mid}")])
        if wait and got:
            rows.append([IB(text=f"🚫 Natijasizlar ({len(wait)}) — «kelmadi»", callback_data=f"xrn:{mid}")])
    rows.append([IB(text="👥 Qatnashchilar", callback_data=f"mql:{mid}")]
                + ([IB(text="💵 To'lovlar", callback_data=f"mqp:{mid}")] if moc.price and payers else []))
    if moc.status != "yakun" and r in MB.MAKERS:
        rows.append([IB(text="🏁 Yakunlash va natijalarni yuborish", callback_data=f"mqz:{mid}")])
        rows.append([IB(text="🗑 Bekor qilish", callback_data=f"mqdel:{mid}")])
    if moc.status == "yakun":
        rows.append([IB(text="📊 Natijalar (Excel)", callback_data=f"mqxl:{mid}")])
    rows.append([IB(text="⬅️ Ro'yxat", callback_data="mq:list")])
    text = "\n".join(lines)
    if edit:
        try:
            return await msg.edit_text(text, reply_markup=IM(inline_keyboard=rows))
        except Exception:
            pass
    await msg.answer(text, reply_markup=IM(inline_keyboard=rows))


# ======================= natijani yuklash =======================
HELP = ("📥 <b>Natijani yuboring</b> — kanal qanday bergan bo'lsa, shundayligicha:\n"
        "• 📊 Excel / CSV fayl\n• 📄 PDF (matnli)\n• 💬 Matn: har qatorda <i>ism va ball</i>\n"
        "• ↪️ Kanal xabarlarini <b>forward</b> qiling — bir nechtasini ketma-ket ham bo'ladi\n\n"
        "Lotin yoki kirill, «Familiya Ism» yoki «Ism Familiya» — farqi yo'q, bot o'zi moslaydi.\n"
        "<i>Rasm (skrinshot) o'qilmaydi — fayl yoki matn kerak.</i>")


@router.callback_query(F.data.startswith("xri:"))
async def imp_start(c: CallbackQuery, state: FSMContext) -> None:
    mid = int(c.data.split(":")[1])
    async with Session() as s:
        moc = await s.get(Moc, mid)
    if not moc or moc.status != "tayyor" or not _baho(moc):
        return await c.answer("Bu imtihonga natija yuklab bo'lmaydi.", show_alert=True)
    await state.clear()
    await state.set_state(Xr.imp)
    await state.update_data(xmid=mid, xtexts=[], xrows=None)
    await c.answer()
    await c.message.answer(HELP + f"\n\nBall oralig'i: 0–{_baho(moc).mx:g}",
                           reply_markup=IM(inline_keyboard=[[IB(text="❌ Bekor", callback_data=f"xrx:{mid}")]]))


@router.message(Xr.imp, F.photo)
async def imp_photo(m: Message) -> None:
    await m.answer("🖼 Rasmdagi jadvalni o'qiy olmayman. Kanaldan natijani <b>Excel/PDF fayl</b> yoki <b>matn</b> "
                   "ko'rinishida oling (ko'p kanallar «faylda yuborish» imkonini beradi) yoki ✍️ qo'lda kiriting.")


@router.message(Xr.imp, F.text | F.document | F.caption, ~F.text.in_(K.menu_texts()))
async def imp_got(m: Message, state: FSMContext, bot: Bot) -> None:
    uid = m.from_user.id
    text = m.text or m.caption or ""
    if m.document:
        if (m.document.file_size or 0) > 19 * 1024 * 1024:
            return await m.answer("Fayl juda katta (20 MB dan oshmasin).")
        try:
            data = (await bot.download(m.document.file_id)).read()
            text = E.text_from_file(data, m.document.file_name or "") + "\n" + (m.caption or "")
        except Exception:
            return await m.answer("Faylni o'qib bo'lmadi. Excel (.xlsx), CSV, PDF yoki matn yuboring.")
    async with _LOCK[uid]:
        d = await state.get_data()
        await state.update_data(xtexts=list(d.get("xtexts") or []) + [text])
        _GEN[uid] = gen = _GEN.get(uid, 0) + 1
    await asyncio.sleep(1.5)
    if _GEN.get(uid) != gen:
        return
    await _process(m, state)


async def _process(msg: Message, state: FSMContext) -> None:
    d = await state.get_data()
    async with Session() as s:
        moc = await s.get(Moc, d["xmid"])
        sec = _baho(moc)
        seats = [z for z in (await s.execute(select(MocSeat).where(MocSeat.moc_id == moc.id))).scalars().all()]
    rows, bad = E.parse_text(list(d.get("xtexts") or []), sec.mx)
    # bir odam ikki marta kelsa — oxirgisi
    seen, uniq = {}, []
    for r in rows:
        k = " ".join(sorted(E.tokens(r.name)))
        if k in seen:
            uniq[seen[k]] = r
        else:
            seen[k] = len(uniq)
            uniq.append(r)
    E.match(uniq, [(z.id, z.name) for z in seats if z.name])
    await state.update_data(xrows=[[r.name, r.value, r.seat, r.sim, r.cands, r.raw] for r in uniq], xbad=bad[:30])
    await _preview(msg, state)


def _rows(d: dict) -> list[list]:
    return d.get("xrows") or []


async def _preview(msg: Message, state: FSMContext) -> None:
    d = await state.get_data()
    rows = _rows(d)
    async with Session() as s:
        moc = await s.get(Moc, d["xmid"])
        seats = {z.id: z for z in (await s.execute(select(MocSeat).where(MocSeat.moc_id == moc.id))).scalars().all()}
    if not rows:
        return await msg.answer("❌ Natija topilmadi. Har qatorda ism va ball bo'lishi kerak, masalan:\n"
                                "<code>Aliyev Vali 62.5\nKarimova Laylo — 55 ball</code>")
    ok = [r for r in rows if r[2]]
    lost = [r for r in rows if not r[2]]
    taken = {r[2] for r in ok}
    wait = [z for z in seats.values() if z.name and z.id not in taken and not z.absent]
    upd = sum(1 for r in ok if r[2] in seats and seats[r[2]].baho)
    new = sum(1 for r in ok if r[2] == -1)
    fuzzy = [r for r in ok if r[3] < 0.97 and r[2] in seats]
    lines = [f"📥 <b>{moc.title}</b> — {len(rows)} ta natija o'qildi",
             f"✅ Mos keldi: <b>{len(ok)}</b>" + (f" (shundan {upd} tasi yangilanadi)" if upd else ""),
             f"🚫 «Kelmadi» deb yozilgan: {sum(1 for r in ok if r[1] is None)}" if any(r[1] is None for r in ok) else "",
             f"➕ Yangi tashqi qatnashchi sifatida: {new}" if new else "",
             f"⚠️ Ismi aniqlanmadi: <b>{len(lost)}</b>" if lost else "",
             f"👤 Natijasi kelmagan qatnashchilar: {len(wait)}" if wait else ""]
    if fuzzy:
        lines.append("\n🔎 <b>Taxminiy mosliklar — tekshirib qo'ying:</b>")
        lines += [f"• {r[0]} → <b>{seats[r[2]].name}</b>" for r in fuzzy[:15]]
    if lost:
        lines.append("\n⚠️ <b>Aniqlanmadi:</b>")
        lines += [f"• {r[0]} — {'kelmadi' if r[1] is None else f'{r[1]:g}'}" for r in lost[:15]]
    if d.get("xbad"):
        lines.append("\n❔ <b>O'qilmagan qatorlar</b> (ball yo'q yoki oraliqdan tashqarida):")
        lines += [f"• <code>{x[:60]}</code>" for x in d["xbad"][:8]]
    kb = [[IB(text=f"💾 Saqlash ({len(ok)})", callback_data=f"xrs:{moc.id}")]]
    if lost:
        kb.append([IB(text=f"🔍 Aniqlanmaganlarni biriktirish ({len(lost)})", callback_data="xrr")])
    kb.append([IB(text="➕ Yana yuborish", callback_data="xra"), IB(text="❌ Bekor", callback_data=f"xrx:{moc.id}")])
    text = "\n".join(x for x in lines if x)
    if len(text) > 4000:
        text = text[:3990] + "…"
    await msg.answer(text, reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data == "xra")
async def imp_more(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Xr.imp)
    await c.answer()
    await c.message.answer("Yana natija yuboring (oldingilari saqlanib qoladi).")


@router.callback_query(F.data == "xrr")
async def resolve(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await _resolve_next(c.message, state)


async def _resolve_next(msg: Message, state: FSMContext) -> None:
    d = await state.get_data()
    rows = _rows(d)
    skip = set(d.get("xskip") or [])
    todo = [i for i, r in enumerate(rows) if not r[2] and i not in skip]
    if not todo:
        return await _preview(msg, state)
    i = todo[0]
    r = rows[i]
    taken = {x[2] for x in rows if x[2]}
    async with Session() as s:
        seats = [z for z in (await s.execute(select(MocSeat).where(MocSeat.moc_id == d["xmid"]))).scalars().all()
                 if z.name and z.id not in taken]
    cand = [z for z in sorted(seats, key=lambda z: -E.name_sim(r[0], z.name))][:6]
    kb = [[IB(text=f"👤 {z.name[:30]}", callback_data=f"xrp:{i}:{z.id}")] for z in cand]
    kb.append([IB(text="➕ Yangi tashqi qatnashchi", callback_data=f"xrp:{i}:new"),
               IB(text="⏭ Tashlab ketish", callback_data=f"xrp:{i}:skip")])
    await msg.answer(f"⚠️ Faylda: <b>{r[0]}</b> — {'kelmadi' if r[1] is None else f'{r[1]:g}'}\n"
                     f"<i>{r[5][:80]}</i>\n\nBu kim? (qolgan: {len(todo)})", reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("xrp:"))
async def resolve_pick(c: CallbackQuery, state: FSMContext) -> None:
    _, i, v = c.data.split(":")
    i = int(i)
    d = await state.get_data()
    rows = _rows(d)
    if i >= len(rows):
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    if v == "skip":
        await state.update_data(xskip=list(d.get("xskip") or []) + [i])
    elif v == "new":
        rows[i][2] = -1                              # saqlashda yangi tashqi qatnashchi yaratiladi
        rows[i][3] = 1.0
    else:
        if any(r[2] == int(v) for r in rows):
            return await c.answer("Bu qatnashchiga allaqachon natija biriktirilgan.", show_alert=True)
        rows[i][2], rows[i][3] = int(v), 1.0
    await state.update_data(xrows=rows)
    await c.answer()
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _resolve_next(c.message, state)


@router.callback_query(F.data.startswith("xrs:"))
async def save(c: CallbackQuery, state: FSMContext) -> None:
    from app.bot import moc as MB
    mid = int(c.data.split(":")[1])
    d = await state.get_data()
    rows = _rows(d)
    if d.get("xmid") != mid or not rows:
        return await c.answer("Jarayon eskirgan — qaytadan yuklang.", show_alert=True)
    n_val = n_abs = n_new = 0
    async with Session() as s:
        moc = await s.get(Moc, mid)
        if moc.status != "tayyor":
            return await c.answer("Imtihon yakunlangan.", show_alert=True)
        st = await MB._staff(s, c.from_user.id)
        key = _baho(moc).name
        new_ids = []
        for name, val, sid, *_ in rows:
            if not sid:
                continue
            if sid == -1:
                ids = await MB._make_seats(s, moc, [], [(name[:120], None)], 0)
                sid = ids[0]
                new_ids.append(sid)
                n_new += 1
            z = await s.get(MocSeat, sid)
            if not z or z.moc_id != mid:
                continue
            if val is None:
                z.absent, z.baho = True, {}
                n_abs += 1
            else:
                z.absent = False
                z.baho = {**(z.baho or {}), key: float(val)}
                n_val += 1
        await S.audit(s, st, "tashqi_natija", id=mid, n=n_val, absent=n_abs, new=n_new)
        await s.commit()
        wait = [z for z in await MB._seats(s, mid) if z.name and not z.absent and key not in (z.baho or {})]
    await state.clear()
    await c.answer("Saqlandi ✅")
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    txt = (f"💾 Saqlandi: <b>{n_val}</b> ta natija" + (f", {n_abs} ta kelmagan" if n_abs else "")
           + (f", {n_new} ta yangi qatnashchi" if n_new else "") + ".")
    kb = []
    if wait:
        txt += f"\n\n⏳ Natijasi yo'q ({len(wait)}): " + ", ".join(z.name for z in wait[:20]) + ("…" if len(wait) > 20 else "")
        kb.append([IB(text="🚫 Ularni «kelmadi» deb belgilash", callback_data=f"xrn:{mid}")])
    kb.append([IB(text="🏁 Yakunlash va natijalarni yuborish", callback_data=f"mqz:{mid}")])
    kb.append([IB(text="🎓 Imtihon kartasi", callback_data=f"mqc:{mid}")])
    await c.message.answer(txt, reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("xrn:"))
async def mark_rest_absent(c: CallbackQuery) -> None:
    from app.bot import moc as MB
    mid = int(c.data.split(":")[1])
    async with Session() as s:
        moc = await s.get(Moc, mid)
        st = await MB._staff(s, c.from_user.id)
        if not st or MB._role(st) not in MB.MAKERS or moc.status != "tayyor":
            return await c.answer("Ruxsat yo'q.", show_alert=True)
        n = 0
        for z in await MB._seats(s, mid):
            if z.name and not z.absent and not z.baho and not z.answers:
                z.absent = True
                n += 1
        await s.commit()
    await c.answer(f"{n} ta «kelmadi» deb belgilandi")
    await MB.card(c.message, mid, c.from_user.id)


@router.callback_query(F.data.startswith("xrx:"))
async def cancel(c: CallbackQuery, state: FSMContext) -> None:
    from app.bot import moc as MB
    await state.clear()
    await c.answer("Bekor qilindi")
    await MB.card(c.message, int(c.data.split(":")[1]), c.from_user.id)
