"""✉️ Murojaatlar: ota-ona (yoki o'quvchi) savol, taklif, shikoyat yuboradi — rahbar javob beradi.

• «👨‍🏫 Ustozga yozish» — savol to'g'ridan-to'g'ri farzandning o'qituvchisiga boradi, u javob beradi
  (rahbar ro'yxatda ko'rib turadi; 24 soat javobsiz qolsa rahbarga eslatiladi).

• Har murojaatga raqam beriladi, holati kuzatiladi: 🆕 yangi → 💬 javob berildi → ✅ yopildi.
• Shikoyat va taklif — faqat rahbarga; savol — rahbar va adminga; to'lov — rahbar va moliyaga.
• Javob berilmagan murojaat har kuni 10:00 da rahbarga eslatiladi; javobdan keyin ota-ona baho beradi.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import select

from app import services as S
from app.bot import keyboards as K
from app.bot.notify import send
from app.config import settings
from app.db.models import Murojaat, Staff
from app.db.session import Session

router = Router()
CATS = {"ustoz": "👨‍🏫 Ustozga savol", "savol": "❓ Savol", "taklif": "💡 Taklif", "shikoyat": "⚠️ Shikoyat", "tolov": "💳 To'lov masalasi",
        "boshqa": "📝 Boshqa"}
TO = {"ustoz": ("rahbar",), "savol": ("rahbar", "admin"), "taklif": ("rahbar",), "shikoyat": ("rahbar",), "tolov": ("rahbar", "moliya"),
      "boshqa": ("rahbar",)}
ST = {"yangi": "🆕 ko'rib chiqilmoqda", "javob": "💬 javob berildi", "yopildi": "✅ yopildi"}


class Mj(StatesGroup):
    text = State()
    answer = State()


async def _sender(s, tg: int):
    who = await S.who_by_tg(s, tg)
    if who.parent:
        return who.parent, list(who.parent.children)
    if who.student:
        return None, [who.student]
    return None, []


# ---------------- ota-ona / o'quvchi ----------------
@router.message(F.text == K.B_APPEAL)
async def start(m: Message, state: FSMContext) -> None:
    await state.clear()
    async with Session() as s:
        par, kids = await _sender(s, m.from_user.id)
        if not kids:
            return
        mine = list((await s.execute(select(Murojaat).where(Murojaat.from_tg == m.from_user.id)
                                     .order_by(Murojaat.id.desc()).limit(5))).scalars())
    boss = [k for k in CATS if k != "ustoz"]
    rows = [[IB(text="👨‍🏫 Ustozga yozish", callback_data="mjc:ustoz")]]
    rows += [[IB(text=CATS[k], callback_data=f"mjc:{k}") for k in boss[i:i + 2]] for i in range(0, len(boss), 2)]
    if mine:
        rows.append([IB(text="📂 Mening murojaatlarim", callback_data="mjl")])
    await m.answer("✉️ <b>Murojaat</b>\n\n👨‍🏫 <b>Ustozga yozish</b> — dars, vazifa, baho haqidagi savol "
                   "to'g'ridan-to'g'ri o'qituvchiga boradi.\n\nPastdagi turlar — <b>markaz rahbariyatiga</b> "
                   "(taklif, shikoyat, to'lov va h.k.).\n\nKimga yozasiz?", reply_markup=IM(inline_keyboard=rows))


@router.callback_query(F.data.startswith("mjc:"))
async def pick_cat(c: CallbackQuery, state: FSMContext) -> None:
    cat = c.data.split(":")[1]
    async with Session() as s:
        _, kids = await _sender(s, c.from_user.id)
    await state.update_data(cat=cat, kid=kids[0].id if len(kids) == 1 else None, to=None)
    await c.answer()
    if len(kids) > 1:
        extra = [] if cat == "ustoz" else [[IB(text="Umumiy", callback_data="mjk:0")]]
        await c.message.edit_text(f"{CATS[cat]} — qaysi farzandingiz haqida?", reply_markup=IM(inline_keyboard=[
            [IB(text=k.full_name, callback_data=f"mjk:{k.id}")] for k in kids] + extra))
        return
    await _next(c.message, state, cat, kids[0].id if kids else None)


async def _teachers(s, kid: int | None) -> list[tuple]:
    """Farzand o'qiyotgan guruhlar o'qituvchilari: [(Staff, "Guruh nomi"), ...]."""
    from app.db.models import Enrollment, Group
    if not kid:
        return []
    rows = (await s.execute(select(Staff, Group.name).join(Group, Group.teacher_id == Staff.id)
                            .join(Enrollment, Enrollment.group_id == Group.id)
                            .where(Enrollment.student_id == kid, Enrollment.active, Staff.active))).all()
    out: dict[int, tuple] = {}
    for st, gname in rows:
        out.setdefault(st.id, (st, []))[1].append(gname)
    return [(st, ", ".join(g)) for st, g in out.values()]


async def _next(msg: Message, state: FSMContext, cat: str, kid: int | None) -> None:
    if cat != "ustoz":
        return await _ask_text(msg, state, cat)
    async with Session() as s:
        ts = await _teachers(s, kid)
    if not ts:
        await state.clear()
        return await msg.answer("Farzandingiz hozir hech bir guruhda o'qituvchiga biriktirilmagan. "
                                "Savolingizni «❓ Savol» orqali rahbariyatga yozing.")
    if len(ts) == 1:
        await state.update_data(to=ts[0][0].id, to_name=ts[0][0].full_name)
        return await _ask_text(msg, state, cat)
    await msg.answer("👨‍🏫 Qaysi ustozga yozasiz?", reply_markup=IM(inline_keyboard=[
        [IB(text=f"{st.full_name} — {g}"[:60], callback_data=f"mjt:{st.id}")] for st, g in ts]))


@router.callback_query(F.data.startswith("mjt:"))
async def pick_teacher(c: CallbackQuery, state: FSMContext) -> None:
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await s.get(Staff, sid)
    await state.update_data(to=sid, to_name=st.full_name if st else "")
    await c.answer()
    await _ask_text(c.message, state, "ustoz")


@router.callback_query(F.data.startswith("mjk:"))
async def pick_kid(c: CallbackQuery, state: FSMContext) -> None:
    kid = int(c.data.split(":")[1]) or None
    await state.update_data(kid=kid)
    d = await state.get_data()
    await c.answer()
    await _next(c.message, state, d["cat"], kid)


async def _ask_text(msg: Message, state: FSMContext, cat: str) -> None:
    await state.set_state(Mj.text)
    d = await state.get_data()
    hint = {"ustoz": f"Ustoz <b>{d.get('to_name') or ''}</b> ga savolingizni yozing — javob shu yerga keladi.","shikoyat": "Nima bo'ldi, qachon, kim bilan — batafsil yozing. Murojaatingizni faqat rahbar ko'radi.",
            "taklif": "Taklifingizni yozing — markazni yaxshilashga yordam beradi.",
            "tolov": "To'lov bo'yicha savolingizni yozing (chek rasmini ham yuborishingiz mumkin).",
            }.get(cat, "Murojaatingizni yozing.")
    await msg.answer(f"{CATS[cat]}\n{hint}\n\n<i>Matn, rasm yoki ovozli xabar yuborishingiz mumkin.</i>")


@router.message(Mj.text, (F.text & ~F.text.in_(K.menu_texts())) | F.photo | F.voice | F.document)
async def save(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    await state.clear()
    fid = kind = None
    if m.photo:
        fid, kind = m.photo[-1].file_id, "photo"
    elif m.voice:
        fid, kind = m.voice.file_id, "voice"
    elif m.document:
        fid, kind = m.document.file_id, "document"
    text = (m.text or m.caption or "").strip()[:3000]
    async with Session() as s:
        par, kids = await _sender(s, m.from_user.id)
        mj = Murojaat(parent_id=par.id if par else None, student_id=d.get("kid"), from_tg=m.from_user.id,
                      category=d["cat"], text=text or None, file_id=fid, file_kind=kind, to_staff_id=d.get("to"))
        s.add(mj)
        await s.commit()
        to = await _recipients(s, mj)
        head = await _head(s, mj)
    await m.answer(f"✅ Murojaatingiz qabul qilindi — <b>№{mj.id}</b>.\nJavob shu yerga keladi. Holatini "
                   "«✉️ Murojaat» → «📂 Mening murojaatlarim» da ko'rasiz.")
    for tg in dict.fromkeys(to):
        await _deliver(bot, tg, mj, head)


async def _recipients(s, mj: Murojaat) -> list[int]:
    """Kimga boradi: «ustoz» — faqat o'sha o'qituvchiga (Telegrami yo'q bo'lsa rahbarga); qolganlari — TO bo'yicha."""
    if mj.category == "ustoz" and mj.to_staff_id:
        t = await s.get(Staff, mj.to_staff_id)
        if t and t.tg_id and t.active:
            return [t.tg_id]
    to = []
    for role in TO[mj.category]:
        to += [x.tg_id for x in await S.staff_with_role(s, role) if x.tg_id]
    return to


def _mine(staff: Staff):
    """Shu xodim ko'radigan murojaatlar sharti."""
    from sqlalchemy import and_, or_
    role = staff.current_role
    cats = [k for k, v in TO.items() if role in v and k != "ustoz"]
    conds = [Murojaat.category.in_(cats)] if cats else []
    if role == "rahbar":
        conds.append(Murojaat.category == "ustoz")
    elif role in ("oqituvchi", "yordamchi"):
        conds.append(and_(Murojaat.category == "ustoz", Murojaat.to_staff_id == staff.id))
    return or_(*conds) if conds else Murojaat.id < 0


async def _head(s, mj: Murojaat) -> str:
    from app.db.models import Parent, Student
    p = await s.get(Parent, mj.parent_id) if mj.parent_id else None
    st = await s.get(Student, mj.student_id) if mj.student_id else None
    who = (p.full_name or p.phone) if p else "O'quvchi"
    t = await s.get(Staff, mj.to_staff_id) if mj.to_staff_id else None
    return (f"✉️ <b>Murojaat №{mj.id}</b> · {CATS[mj.category]}" + (f" → {t.full_name}" if t else "")
            + f"\nKimdan: {who}" + (f" ({p.phone})" if p and p.full_name else "")
            + (f"\nO'quvchi: {st.full_name}" if st else "") + (f"\n\n{mj.text}" if mj.text else ""))


async def _deliver(bot: Bot, tg: int, mj: Murojaat, head: str) -> None:
    kb = IM(inline_keyboard=[[IB(text="✍️ Javob berish", callback_data=f"mja:{mj.id}")]])
    if mj.file_id:
        try:
            m = {"photo": bot.send_photo, "voice": bot.send_voice, "document": bot.send_document}[mj.file_kind]
            await m(tg, mj.file_id, caption=head[:1000], reply_markup=kb)
            return
        except Exception:
            pass
    await send(bot, tg, head, reply_markup=kb)


@router.callback_query(F.data == "mjl")
async def my_list(c: CallbackQuery) -> None:
    async with Session() as s:
        rows = list((await s.execute(select(Murojaat).where(Murojaat.from_tg == c.from_user.id)
                                     .order_by(Murojaat.id.desc()).limit(10))).scalars())
    await c.answer()
    lines = ["📂 <b>Mening murojaatlarim</b>"]
    for r in rows:
        lines.append(f"\n№{r.id} · {CATS[r.category]} · {(r.created_at + timedelta(hours=5)):%d.%m} — {ST[r.status]}"
                     + (f"\n   {(r.text or '')[:80]}" if r.text else "")
                     + (f"\n   💬 Javob: {r.answer[:200]}" if r.answer else ""))
    await c.message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("mjr:"))
async def rate(c: CallbackQuery, bot: Bot) -> None:
    _, mid, ok = c.data.split(":")
    async with Session() as s:
        mj = await s.get(Murojaat, int(mid))
        if not mj or mj.from_tg != c.from_user.id:
            return await c.answer()
        mj.rating = int(ok)
        mj.status = "yopildi" if ok == "1" else "yangi"
        await s.commit()
        bosses = await _recipients(s, mj)
        head = await _head(s, mj)
    await c.answer("Rahmat!" if ok == "1" else "Qayta ko'rib chiqiladi")
    if ok == "1":
        await c.message.edit_text(c.message.html_text + "\n\n✅ Rahmat! Murojaat yopildi.")
    else:
        await c.message.edit_text(c.message.html_text + "\n\n↩️ Murojaat qayta ochildi — yana ko'rib chiqiladi. "
                                  "Qo'shimcha izoh bo'lsa, «✉️ Murojaat» orqali yozing.")
        for b in bosses:
            await _deliver(bot, b, mj, "↩️ <b>Javobdan qoniqmadi</b>\n" + head + f"\n\nOldingi javob: {mj.answer}")


# ---------------- rahbar / xodim javobi ----------------
@router.callback_query(F.data.startswith("mja:"))
async def answer_start(c: CallbackQuery, state: FSMContext) -> None:
    mid = int(c.data.split(":")[1])
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        mj = await s.get(Murojaat, mid)
        if not who.staff or not mj:
            return await c.answer("Ruxsat yo'q.", show_alert=True)
        own = mj.category == "ustoz" and mj.to_staff_id == who.staff.id
        if not own and who.staff.current_role not in TO[mj.category]:
            return await c.answer("Bu murojaatga " + " / ".join(TO[mj.category]) + " javob beradi.", show_alert=True)
        if mj.status == "yopildi":
            return await c.answer("Murojaat yopilgan.", show_alert=True)
    await state.set_state(Mj.answer)
    await state.update_data(mid=mid)
    await c.answer()
    await c.message.answer(f"✍️ Murojaat №{mid} ga javobingizni yozing (ota-onaga shu matn boradi):")


@router.message(Mj.answer, F.text, ~F.text.in_(K.menu_texts()))
async def answer_save(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        mj = await s.get(Murojaat, d["mid"])
        mj.answer, mj.answered_by, mj.answered_at, mj.status = m.text.strip()[:3000], who.staff.id, datetime.utcnow(), "javob"
        await S.audit(s, who.staff, "murojaat_javob", id=mj.id)
        await s.commit()
        others = [] if mj.category == "ustoz" else [x for x in await _recipients(s, mj) if x != m.from_user.id]
        name = who.staff.full_name
    await m.answer(f"✅ Javob yuborildi (murojaat №{mj.id}).")
    if mj.from_tg:
        await send(bot, mj.from_tg, f"💬 <b>Murojaatingiz №{mj.id} ga javob</b> ({CATS[mj.category]})\n\n{mj.answer}\n\n"
                                    f"— {name}, {settings.center_name}\n\n"
                                    "Javob sizni qoniqtirdimi?",
                   reply_markup=IM(inline_keyboard=[[IB(text="✅ Ha, rahmat", callback_data=f"mjr:{mj.id}:1"),
                                                     IB(text="↩️ Yo'q, hal bo'lmadi", callback_data=f"mjr:{mj.id}:0")]]))
    for tg in dict.fromkeys(others):
        await send(bot, tg, f"ℹ️ Murojaat №{mj.id} ga {name} javob berdi.")


@router.message(F.text == K.B_APPEALS)
async def staff_list(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not who.staff:
            return
        cond = _mine(who.staff)
        rows = list((await s.execute(select(Murojaat).where(cond)
                                     .order_by(Murojaat.status != "yangi", Murojaat.id.desc()).limit(15))).scalars())
        heads = {r.id: await _head(s, r) for r in rows if r.status == "yangi"}
        stats = (await s.execute(select(Murojaat.status, Murojaat.rating).where(cond))).all()
    new = [r for r in rows if r.status == "yangi"]
    good = sum(1 for _, r in stats if r == 1)
    rated = sum(1 for _, r in stats if r is not None)
    await m.answer(f"📨 <b>Murojaatlar</b>\nJavob kutayotgan: <b>{len(new)}</b> · jami: {len(stats)}"
                   + (f" · qoniqish: {good}/{rated}" if rated else "")
                   + ("" if new else "\n\nHammasiga javob berilgan ✅"))
    for r in new:
        await _deliver(m.bot, m.chat.id, r, heads[r.id])
    done = [r for r in rows if r.status != "yangi"][:5]
    if done:
        await m.answer("Oxirgi javob berilganlar:\n" + "\n".join(
            f"№{r.id} · {CATS[r.category]} — {ST[r.status]}" + (" 👍" if r.rating == 1 else " 👎" if r.rating == 0 else "")
            for r in done))


async def pending_cards(bot: Bot, chat: int, role: str) -> int:
    """«⏳ Kutilayotganlar» uchun: javob berilmagan murojaatlar."""
    from sqlalchemy import or_
    cats = [k for k, v in TO.items() if role in v and k != "ustoz"]
    edge = datetime.utcnow() - timedelta(hours=24)
    async with Session() as s:
        rows = list((await s.execute(select(Murojaat).where(Murojaat.status == "yangi", or_(
            Murojaat.category.in_(cats), (Murojaat.category == "ustoz") & (Murojaat.created_at < edge)))
                                     .order_by(Murojaat.id))).scalars())
        heads = {r.id: await _head(s, r) for r in rows}
    for r in rows:
        await _deliver(bot, chat, r, heads[r.id])
    return len(rows)


async def reminder(bot: Bot) -> None:
    """Har kuni 10:00 — 24 soatdan ortiq javobsiz murojaatlar rahbarga; ustozlarga — o'zlariga kelgan javobsizlar."""
    edge = datetime.utcnow() - timedelta(hours=24)
    async with Session() as s:
        mine = (await s.execute(select(Staff.tg_id, Murojaat.id).join(Staff, Staff.id == Murojaat.to_staff_id)
                                .where(Murojaat.status == "yangi", Murojaat.category == "ustoz"))).all()
        per: dict[int, int] = {}
        for tg, _ in mine:
            if tg:
                per[tg] = per.get(tg, 0) + 1
        for tg, k in per.items():
            await send(bot, tg, f"📨 Sizga {k} ta ota-ona savoli javob kutmoqda — «📨 Murojaatlar».")
        n = len((await s.execute(select(Murojaat.id).where(Murojaat.status == "yangi", Murojaat.created_at < edge))).all())
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    if n:
        for b in bosses:
            await send(bot, b, f"📨 {n} ta murojaat 24 soatdan beri javob kutmoqda — «📨 Murojaatlar».")
