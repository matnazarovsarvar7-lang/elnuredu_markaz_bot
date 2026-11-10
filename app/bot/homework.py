"""Uy vazifasi: o'qituvchi yuboradi → o'quvchi va ota-onaga boradi; eslatmalar."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import select

from app.bot import keyboards as K
from app.bot.notify import send, send_photo, mark_blocked
from app.config import settings
from app.db.models import Enrollment, Group, Lesson, Staff, Student
from app.db.session import Session
from app.logic.calendar import is_lesson_day, nth_lesson_on_or_after
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
DEADLINE_HOUR = 20
NO_HW = {"nazorat": "Nazorat ishi o'tkazildi", "takror": "Takrorlash darsi", "bayram": "Bayram yoki ta'til oldi",
         "boshqa": "Boshqa sabab"}


def now() -> datetime:
    return datetime.now(TZ)


class Hw(StatesGroup):
    text = State()


def files_of(fid: str | None, kind: str | None, files: list | None = None) -> list[list[str]]:
    """Vazifa fayllari ro'yxati [[turi, file_id], ...]. Eski yozuvlarda bitta fayl (fid, kind) bo'ladi."""
    if files:
        return [list(x) for x in files]
    return [[kind or "photo", fid]] if fid else []


def hw_for(les: Lesson, eid: int) -> tuple[str | None, list[list[str]], str | None] | None:
    """O'quvchining (enrollment) shu darsdagi vazifasi: (matn, fayllar, kichik guruh nomi).
    Kichik guruhga alohida berilgan bo'lsa — o'sha, aks holda umumiy vazifa."""
    for i, p in enumerate(les.homework_parts or []):
        if eid in p.get("students", []):
            return (p.get("text"), files_of(p.get("fid"), p.get("kind"), p.get("files")),
                    p.get("title") or f"{i + 1}-kichik guruh")
    if les.homework_text or les.homework_file_id or les.homework_files:
        return les.homework_text, files_of(les.homework_file_id, les.homework_file_kind, les.homework_files), None
    return None


async def send_hw(bot: Bot, chat: int, text: str, files: list[list[str]]) -> bool:
    """Vazifani yuboradi: matn + barcha rasmlar/fayllar. Bir nechta bo'lsa — albom (bitta to'plam) qilib."""
    from aiogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo
    try:
        if len(files) == 1:
            k, f = files[0]
            cap = text if len(text) <= 1024 else None
            if k == "photo":
                await bot.send_photo(chat, f, caption=cap)
            elif k == "video":
                await bot.send_video(chat, f, caption=cap)
            else:
                await bot.send_document(chat, f, caption=cap)
            if cap is None:
                await bot.send_message(chat, text)
            return True
        await bot.send_message(chat, text + (f"\n\n📎 {len(files)} ta rasm/fayl ⬇️" if files else ""))
        media = [x for x in files if x[0] in ("photo", "video")]
        docs = [x for x in files if x[0] not in ("photo", "video")]
        for group in (media, docs):
            for i in range(0, len(group), 10):   # Telegram albomida ko'pi bilan 10 ta
                chunk = group[i:i + 10]
                if len(chunk) == 1:
                    k, f = chunk[0]
                    await (bot.send_photo(chat, f) if k == "photo" else bot.send_video(chat, f) if k == "video"
                           else bot.send_document(chat, f))
                    continue
                await bot.send_media_group(chat, [
                    InputMediaPhoto(media=f) if k == "photo" else InputMediaVideo(media=f) if k == "video"
                    else InputMediaDocument(media=f) for k, f in chunk])
        return True
    except Exception as ex:
        if "forbidden" in str(ex).lower() or "blocked" in str(ex).lower():
            await mark_blocked(bot, chat)
        return False


def _part_ids(les: Lesson) -> set[int]:
    return {e for p in (les.homework_parts or []) for e in p.get("students", [])}


def prompt_kb(lid: int, given: bool = False) -> IM:
    first = "✏️ Vazifani o'zgartirish" if given else "✍️ Uy vazifasini yozish"
    return IM(inline_keyboard=[[IB(text=first, callback_data=f"hwg:{lid}")],
                               [IB(text="👥 Kichik guruhga alohida vazifa", callback_data=f"hwp:{lid}")],
                               [IB(text="💬 Berilgan vazifaga izoh qo'shish", callback_data=f"hwz:{lid}")],
                               [IB(text="🚫 Bugun vazifa yo'q", callback_data=f"hwn:{lid}")],
                               [IB(text="⭐ Baho qo'yish (ixtiyoriy)", callback_data=f"bh:{lid}")],
                               [IB(text="🔔 Dars tugadi — ota-onalarga xabar", callback_data=f"dz:{lid}")],
                               [IB(text="✏️ Davomat / vazifa tekshiruvini tuzatish", callback_data=f"lfx:{lid}")]])


def responsible_id(g: Group, what: str = "homework") -> int | None:
    if (g.delegated or {}).get(what) and g.assistant_id:
        return g.assistant_id
    return g.teacher_id


def next_lesson(g: Group, after: date) -> date:
    return nth_lesson_on_or_after(after + timedelta(days=1), g.days, 1)


async def lesson_status_text(lid: int) -> tuple[str, bool]:
    async with Session() as s:
        les = await s.get(Lesson, lid)
        g = await s.get(Group, les.group_id)
        roster = await S.group_roster(s, g.id, les.date)
    parts = les.homework_parts or []
    if parts:
        taken = _part_ids(les)
        rest = len([e for e in roster if e.id not in taken])
        lines = [f"<b>{g.name}</b> · {les.date:%d.%m}", "👥 Kichik guruhlarga alohida vazifa:"]
        lines += [f"• {p.get('title')}: {len(p['students'])} o'quvchi — {(p.get('text') or '(faylda)')[:60]}" for p in parts]
        if les.homework_text or les.homework_file_id:
            lines.append(f"• Qolganlar ({rest}): {(les.homework_text or '(faylda)')[:60]} ✅")
        elif rest:
            lines.append(f"⚠️ Qolgan {rest} o'quvchiga vazifa berilmagan — «✍️ Uy vazifasini yozish» (qolganlar uchun).")
        return "\n".join(lines), bool(les.homework_text or les.homework_file_id or not rest)
    if les.homework_text or les.homework_file_id:
        return (f"<b>{g.name}</b> · {les.date:%d.%m}\n📚 Uy vazifasi yuborilgan ✅\n\n{les.homework_text or '(faylda)'}",
                True)
    if les.no_homework_reason:
        return f"<b>{g.name}</b> · {les.date:%d.%m}\n🚫 Vazifa berilmadi: {les.no_homework_reason}", True
    return (f"<b>{g.name}</b> · {les.date:%d.%m}\n📚 Endi yangi uy vazifasini yuboring — u o'quvchilar va "
            f"ota-onalarga boradi. Muddat: bugun {DEADLINE_HOUR}:00 gacha.", False)


async def ask_homework(msg: Message, lid: int) -> None:
    text, given = await lesson_status_text(lid)
    await msg.answer(text, reply_markup=prompt_kb(lid, given))
    await exam_note(msg, lid)


async def exam_note(msg: Message, lid: int) -> None:
    from app.bot.exams import today_exam_button
    async with Session() as s:
        les = await s.get(Lesson, lid)
    kb = await today_exam_button(les.group_id, les.date)
    if kb:
        await msg.answer("📝 Bugun bu guruhda <b>nazorat kuni</b>. O'tkazgach, natijalarni kiriting:", reply_markup=kb)


# ---------- o'qituvchi: vazifa yozish ----------
@router.callback_query(F.data.startswith("hwg:"))
async def hw_write(c: CallbackQuery, state: FSMContext) -> None:
    lid = int(c.data.split(":")[1])
    async with Session() as s:
        les = await s.get(Lesson, lid)
    who_for = "<b>qolgan o'quvchilar</b> (alohida vazifa berilmaganlar) uchun " if les.homework_parts else ""
    await state.set_state(Hw.text)
    await state.update_data(lid=lid, part=None, text=None, files=[], fid=None, kind=None)
    BUF.pop(c.from_user.id, None)
    await c.answer()
    await c.message.answer(f"Uy vazifasini {who_for}yozing. Bir nechta rasm yoki fayl ham yuborish mumkin — "
                           "hammasi <b>bitta vazifa</b> bo'lib ketadi.\n"
                           "<i>Masalan: 45-bet, 3–7-misollar. Yangi so'zlarni yod olish.</i>\n\nBekor qilish: /start")


# bir nechta rasm (albom) alohida-alohida xabar bo'lib keladi — ularni bitta vazifaga yig'amiz
BUF: dict[int, dict] = {}
WAIT = 1.5   # oxirgi rasm kelgandan keyin shuncha soniya kutib, bitta "Tekshiring" chiqaramiz
MAX_FILES = 20


def _preview_kb() -> IM:
    return IM(inline_keyboard=[[IB(text="✅ Yuborish", callback_data="hwsend"),
                                IB(text="✏️ Qayta yozish", callback_data="hwagain")]])


async def _show_preview(uid: int, m: Message, state: FSMContext, token: int) -> None:
    import asyncio
    await asyncio.sleep(WAIT)
    b = BUF.get(uid)
    if not b or b["token"] != token:
        return   # yangi rasm keldi — keyingisi chiqaradi
    await state.update_data(text=b["text"], files=b["files"], fid=None, kind=None)
    if b.get("msg"):
        try:
            await m.bot.delete_message(m.chat.id, b["msg"])
        except Exception:
            pass
    n = len(b["files"])
    ph = sum(1 for k, _ in b["files"] if k == "photo")
    what = (f"\n📎 {n} ta fayl biriktirilgan" + (f" ({ph} ta rasm)" if ph and ph != n else "") if n else "")
    tip = ("\n\n<i>Yana rasm/fayl yoki matn yuborsangiz — shu vazifaga qo'shiladi.</i>" if n < MAX_FILES else "")
    sent = await m.answer(f"<b>Tekshiring:</b>\n\n{b['text'] or '(matnsiz, faqat fayl)'}{what}{tip}",
                          reply_markup=_preview_kb())
    b["msg"] = sent.message_id


@router.message(Hw.text)
async def hw_got(m: Message, state: FSMContext) -> None:
    import asyncio
    text = (m.text or m.caption or "").strip()
    fid = kind = None
    if m.photo:
        fid, kind = m.photo[-1].file_id, "photo"
    elif m.video:
        fid, kind = m.video.file_id, "video"
    elif m.document:
        fid, kind = m.document.file_id, "doc"
    if not text and not fid:
        await m.answer("Vazifa matnini yozing yoki rasm/fayl yuboring.")
        return
    uid = m.from_user.id
    b = BUF.setdefault(uid, {"text": None, "files": [], "raw": [], "token": 0, "msg": None})
    if fid:
        if len(b["files"]) >= MAX_FILES:
            await m.answer(f"Ko'pi bilan {MAX_FILES} ta fayl. «✅ Yuborish» ni bosing.")
            return
        b["raw"].append((m.message_id, kind, fid))
        b["files"] = [[k, f] for _, k, f in sorted(b["raw"])]   # albom tartibi saqlansin
    if text:
        new = f"{b['text']}\n{text}" if b["text"] and text not in b["text"] else (b["text"] or text)
        if len(new) > 900:
            await m.answer("Matn juda uzun (900 belgidan oshmasin). Qisqartirib yuboring.")
            return
        b["text"] = new
    b["token"] += 1
    asyncio.create_task(_show_preview(uid, m, state, b["token"]))


@router.callback_query(F.data == "hwagain")
async def hw_again(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    if not d.get("lid"):
        await c.answer("Ma'lumot eskirgan, qaytadan boshlang.", show_alert=True)
        return
    BUF.pop(c.from_user.id, None)
    await state.set_state(Hw.text)
    await state.update_data(text=None, files=[], fid=None, kind=None)
    await c.message.edit_text("Uy vazifasini qaytadan yozing (rasmlar ham qaytadan yuboriladi):")


@router.callback_query(F.data == "hwsend")
async def hw_send(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    b = BUF.get(c.from_user.id)
    if not d.get("lid"):
        await c.answer("Bu vazifa allaqachon yuborilgan yoki eskirgan.", show_alert=True)
        return
    if b and b.get("msg") and b["msg"] != c.message.message_id:
        await c.answer("Eng oxirgi «Tekshiring» xabaridagi tugmani bosing.", show_alert=True)
        return
    if b:   # kechikib kelgan rasmlar ham kirsin
        d.update(text=b["text"], files=b["files"])
    BUF.pop(c.from_user.id, None)
    await state.clear()
    if d.get("part"):
        await _send_part(c, bot, d)
        return
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        les = await s.get(Lesson, d["lid"])
        g = await s.get(Group, les.group_id)
        resend = bool(les.homework_text or les.homework_file_id or les.homework_files)
        files = files_of(d.get("fid"), d.get("kind"), d.get("files"))
        les.homework_text, les.homework_files = d.get("text"), files or None
        les.homework_file_id, les.homework_file_kind = (files[0][1], files[0][0]) if files else (None, None)
        les.no_homework_reason = None
        les.homework_at, les.homework_by = now().replace(tzinfo=None), who.staff.id if who.staff else None
        await S.audit(s, who.staff, "uy_vazifa", group=g.name, date=les.date.isoformat())
        await s.commit()
    await c.message.edit_text("⏳ Yuborilmoqda...")
    sent, missing = await deliver(bot, les.id, corrected=resend)
    await c.message.edit_text(f"✅ Uy vazifasi yuborildi: {sent} ta qabul qiluvchi."
                              + (f"\n⚠️ Botga ulanmagan o'quvchilar: {missing} ta (admin ota-onalarni ulashi kerak)."
                                 if missing else ""))


async def _targets(s, roster) -> tuple[dict[int, None], int]:
    targets: dict[int, None] = {}
    missing = 0
    for e in roster:
        ids = await S.parent_tg_ids(s, e.student_id)
        if not ids:
            missing += 1
        for t in ids:
            targets[t] = None
    return targets, missing


async def deliver(bot: Bot, lid: int, corrected: bool = False, part: int | None = None) -> tuple[int, int]:
    """Vazifani yuboradi: part=None — umumiy (kichik guruhga kirmagan barcha o'quvchilar, kelmaganlarga ham);
    part=i — faqat o'sha kichik guruh o'quvchilariga. O'quvchi va ota-onalarga."""
    async with Session() as s:
        les = await s.get(Lesson, lid)
        g = await s.get(Group, les.group_id)
        roster = await S.group_roster(s, g.id, les.date)
        if part is None:
            taken = _part_ids(les)
            roster = [e for e in roster if e.id not in taken]
            text_, title = les.homework_text, None
            files = files_of(les.homework_file_id, les.homework_file_kind, les.homework_files)
        else:
            p = les.homework_parts[part]
            only = set(p["students"])
            roster = [e for e in roster if e.id in only]
            text_, title = p.get("text"), p.get("title")
            files = files_of(p.get("fid"), p.get("kind"), p.get("files"))
        targets, missing = await _targets(s, roster)
        nxt = next_lesson(g, les.date)
    head = "✏️ <b>Uy vazifasi (tuzatildi)</b>" if corrected else "📚 <b>Uy vazifasi</b>"
    when = f" soat {g.start_time:%H:%M}" if g.start_time else ""
    text = (f"{head}\n{g.name} · {les.date:%d.%m}" + (f" · {title}" if title else "") + f"\n\n{text_ or ''}\n\n"
            f"Keyingi dars: {nxt:%d.%m}{when}")
    sent = 0
    for t in targets:
        sent += await send_hw(bot, t, text, files)
    return sent, missing


# ---------- 👥 kichik guruhga alohida vazifa ----------
def _pick_kb(lid: int, d: dict) -> IM:
    taken, sel = set(d["taken"]), set(d["sel"])
    rows = []
    for i, eid in enumerate(d["order"]):
        nm = d["names"].get(eid) or d["names"].get(str(eid), "?")
        if eid in taken:
            rows.append([IB(text=f"🔒 {nm} (boshqa vazifada)", callback_data="hwpt:x")])
        else:
            rows.append([IB(text=f"{'✅' if eid in sel else '▫️'} {nm}", callback_data=f"hwpt:{lid}:{i}")])
    rows.append([IB(text=f"➡️ Davom etish ({len(sel)} ta)", callback_data=f"hwpk:{lid}")])
    return IM(inline_keyboard=rows)


@router.callback_query(F.data.startswith("hwp:"))
async def part_pick(c: CallbackQuery, state: FSMContext) -> None:
    lid = int(c.data.split(":")[1])
    async with Session() as s:
        les = await s.get(Lesson, lid)
        roster = await S.group_roster(s, les.group_id, les.date)
        names = {e.id: e.student.full_name for e in roster}
    await state.clear()
    await state.update_data(hw_lid=lid, order=[e.id for e in roster], names=names, taken=sorted(_part_ids(les)), sel=[])
    d = await state.get_data()
    await c.answer()
    await c.message.answer("👥 Shu vazifa kimlar uchun? O'quvchilarni belgilang.\n"
                           "<i>Masalan: kuchli o'quvchilarga murakkabroq, qolganlarga oddiy vazifa. "
                           "Belgilanmaganlar umumiy vazifani oladi.</i>", reply_markup=_pick_kb(lid, d))


@router.callback_query(F.data.startswith("hwpt:"))
async def part_toggle(c: CallbackQuery, state: FSMContext) -> None:
    parts = c.data.split(":")
    if parts[1] == "x":
        await c.answer("Bu o'quvchiga shu darsda boshqa alohida vazifa berilgan.")
        return
    d = await state.get_data()
    if d.get("hw_lid") != int(parts[1]):
        await c.answer("Qaytadan oching.", show_alert=True)
        return
    eid = d["order"][int(parts[2])]
    sel = set(d["sel"]) ^ {eid}
    await state.update_data(sel=sorted(sel))
    d = await state.get_data()
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=_pick_kb(int(parts[1]), d))


@router.callback_query(F.data.startswith("hwpk:"))
async def part_go(c: CallbackQuery, state: FSMContext) -> None:
    lid = int(c.data.split(":")[1])
    d = await state.get_data()
    if d.get("hw_lid") != lid or not d.get("sel"):
        await c.answer("Kamida bitta o'quvchini belgilang.", show_alert=True)
        return
    sel = [e for e in d["order"] if e in set(d["sel"])]
    await state.set_state(Hw.text)
    await state.update_data(lid=lid, part=sel, text=None, files=[], fid=None, kind=None)
    BUF.pop(c.from_user.id, None)
    await c.answer()
    await c.message.edit_text(f"✅ {len(sel)} ta o'quvchi tanlandi.\nEndi ularga vazifani yozing "
                              "(rasm yoki fayl ham bo'ladi, izohiga matn yozing):")


async def _send_part(c: CallbackQuery, bot: Bot, d: dict) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        les = await s.get(Lesson, d["lid"])
        g = await s.get(Group, les.group_id)
        parts = list(les.homework_parts or [])
        taken = _part_ids(les)
        students = [e for e in d["part"] if e not in taken]
        if not students:
            await c.message.edit_text("Bu o'quvchilarga allaqachon alohida vazifa berilgan.")
            return
        files = files_of(d.get("fid"), d.get("kind"), d.get("files"))
        parts.append({"students": students, "text": d.get("text"), "files": files,
                      "fid": files[0][1] if files else None, "kind": files[0][0] if files else None,
                      "title": f"{len(parts) + 1}-kichik guruh", "at": now().strftime("%Y-%m-%d %H:%M"),
                      "by": who.staff.id if who.staff else None})
        les.homework_parts = parts
        les.no_homework_reason = None
        if not les.homework_at:
            les.homework_at, les.homework_by = now().replace(tzinfo=None), who.staff.id if who.staff else None
        await S.audit(s, who.staff, "uy_vazifa_kichik", group=g.name, date=les.date.isoformat(), students=len(students))
        await s.commit()
        idx = len(parts) - 1
        rest = len([e for e in await S.group_roster(s, g.id, les.date) if e.id not in _part_ids(les)])
        main_given = bool(les.homework_text or les.homework_file_id or les.homework_files)
    await c.message.edit_text("⏳ Yuborilmoqda...")
    sent, missing = await deliver(bot, d["lid"], part=idx)
    tail = ("" if main_given or not rest else
            f"\n\n⚠️ Qolgan {rest} o'quvchiga hali vazifa berilmagan: «✍️ Uy vazifasini yozish» — qolganlar uchun, "
            "yoki yana «👥 Kichik guruhga alohida vazifa».")
    await c.message.edit_text(f"✅ {idx + 1}-kichik guruh ({len(students)} o'quvchi) vazifasi yuborildi: {sent} ta qabul qiluvchi."
                              + (f"\n⚠️ Botga ulanmaganlar: {missing}" if missing else "") + tail,
                              reply_markup=prompt_kb(d["lid"], main_given))


# ---------- vazifa yo'q ----------
@router.callback_query(F.data.startswith("hwn:"))
async def hw_none(c: CallbackQuery) -> None:
    lid = int(c.data.split(":")[1])
    kb = IM(inline_keyboard=[[IB(text=v, callback_data=f"hwr:{lid}:{k}")] for k, v in NO_HW.items()])
    await c.message.edit_text("Nega bugun uy vazifasi berilmaydi?", reply_markup=kb)


@router.callback_query(F.data.startswith("hwr:"))
async def hw_none_reason(c: CallbackQuery) -> None:
    _, lid, code = c.data.split(":")
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        les = await s.get(Lesson, int(lid))
        g = await s.get(Group, les.group_id)
        les.no_homework_reason = NO_HW[code]
        les.homework_at, les.homework_by = now().replace(tzinfo=None), who.staff.id if who.staff else None
        await S.audit(s, who.staff, "uy_vazifa_yoq", group=g.name, reason=NO_HW[code])
        await s.commit()
    await c.message.edit_text(f"Saqlandi: bugun vazifa yo'q — {NO_HW[code]}.")


# ---------- ota-ona / o'quvchi: so'nggi vazifa ----------
async def latest_homework(s, group_id: int) -> Lesson | None:
    return (await s.execute(select(Lesson).where(Lesson.group_id == group_id, Lesson.homework_at.is_not(None))
                            .order_by(Lesson.date.desc()).limit(1))).scalar_one_or_none()


@router.message(F.text == K.B_HOMEWORK)
async def hw_view(m: Message, bot: Bot) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        kids = list(who.parent.children) if who.parent else ([who.student] if who.student else [])
        items = []
        for k in kids:
            for e in await S.student_enrollments(s, k.id):
                les = await latest_homework(s, e.group_id)
                items.append((k.full_name, e.group, les, e.id))
    if not items:
        await m.answer("Hozircha ma'lumot yo'q.")
        return
    for name, g, les, eid in items:
        title = f"📚 <b>{g.name}</b>" + (f" · {name}" if len(kids) > 1 else "")
        hw = hw_for(les, eid) if les else None
        if not les:
            await m.answer(f"{title}\nHali uy vazifasi kiritilmagan.")
        elif les.no_homework_reason:
            await m.answer(f"{title} · {les.date:%d.%m}\nVazifa berilmagan: {les.no_homework_reason}")
        elif not hw:
            await m.answer(f"{title} · {les.date:%d.%m}\nVazifa hali kiritilmagan.")
        else:
            text_, files, part = hw
            cap = f"{title} · {les.date:%d.%m}" + (f" · {part}" if part else "") + f"\n\n{text_ or ''}"
            if files:
                await send_hw(bot, m.chat.id, cap, files)
            else:
                await m.answer(cap)


# ---------- avtomatik eslatmalar ----------
async def homework_watch(bot: Bot) -> None:
    """Dars tugaganidan 1 soat o'tib vazifa yuborilmagan bo'lsa — har soatda eslatma (20:00 gacha)."""
    n = now()
    if n.hour >= DEADLINE_HOUR:
        return
    t = n.date()
    todo = []
    async with Session() as s:
        for g in (await s.execute(select(Group).where(Group.active, Group.end_time.is_not(None)))).scalars():
            if not is_lesson_day(t, g.days) or await S.group_paused(s, g.id, t):
                continue
            passed = n - datetime.combine(t, g.end_time, TZ)
            if passed < timedelta(hours=1) or passed % timedelta(hours=1) >= timedelta(minutes=10):
                continue
            les = (await s.execute(select(Lesson).where(Lesson.group_id == g.id, Lesson.date == t))).scalar_one_or_none()
            if les and (les.cancelled or les.homework_at):
                continue
            rid = responsible_id(g)
            st = await s.get(Staff, rid) if rid else None
            from app.bot.exams import on_leave
            if not (st and st.tg_id) or await on_leave(s, st.id, t):
                continue
            if not les:
                les = await S.get_or_create_lesson(s, g.id, t)
                await s.commit()
            todo.append((st.tg_id, g.name, les.id))
    for tg, name, lid in todo:
        await send(bot, tg, f"⏰ «{name}» guruhiga bugungi uy vazifasi hali yuborilmagan. Muddat — "
                            f"{DEADLINE_HOUR}:00.", reply_markup=prompt_kb(lid))


async def missing_today(s, t: date) -> list[str]:
    """Bugun dars bo'lgan, lekin vazifa (yoki 'yo'q' sababi) kiritilmagan guruhlar."""
    out = []
    for g in (await s.execute(select(Group).where(Group.active))).scalars():
        if not is_lesson_day(t, g.days) or await S.group_paused(s, g.id, t):
            continue
        les = (await s.execute(select(Lesson).where(Lesson.group_id == g.id, Lesson.date == t))).scalar_one_or_none()
        if les and (les.cancelled or les.homework_at):
            continue
        out.append(g.name)
    return out


async def tomorrow_reminder(bot: Bot) -> None:
    """Soat 18:00 da: ertaga darsi bor o'quvchiga vazifani eslatadi (o'quvchi botda bo'lmasa — ota-onaga)."""
    t = now().date()
    tm = t + timedelta(days=1)
    jobs = []
    async with Session() as s:
        for g in (await s.execute(select(Group).where(Group.active))).scalars():
            if not is_lesson_day(tm, g.days):
                continue
            les = await latest_homework(s, g.id)
            if not les or les.no_homework_reason or les.date < t - timedelta(days=7):
                continue
            when = f" soat {g.start_time:%H:%M}" if g.start_time else ""
            for e in await S.group_roster(s, g.id, tm):
                hw = hw_for(les, e.id)
                if not hw:
                    continue
                text = (f"⏰ Ertaga{when} — «{g.name}» darsi.\nUy vazifasi ({les.date:%d.%m}):\n\n"
                        f"{hw[0] or '(faylda — «📚 Uy vazifasi» tugmasini bosing)'}")
                stu = await s.get(Student, e.student_id)
                if stu.tg_id:
                    jobs.append((stu.tg_id, text))
                else:
                    for p in await S.parent_tg_ids(s, stu.id):
                        jobs.append((p, text))
    for tg, text in jobs:
        await send(bot, tg, text)


# ---------- baholar (ixtiyoriy) ----------
GR_ICON = {None: "▫️", "alo": "🌟", "yaxshi": "👍", "qoniqarli": "👌", "yomon": "⚠️"}
GR_NEXT = {None: "alo", "alo": "yaxshi", "yaxshi": "qoniqarli", "qoniqarli": "yomon", "yomon": None}
DRAFT_GR: dict[int, dict[int, str | None]] = {}


async def _grade_rows(lid: int) -> list[tuple[int, str, str | None]]:
    from app.db.models import Attendance
    async with Session() as s:
        les = await s.get(Lesson, lid)
        roster = await S.group_roster(s, les.group_id, les.date)
        atts = {a.enrollment_id: a for a in (await s.execute(select(Attendance).where(Attendance.lesson_id == lid))).scalars()}
    draft = DRAFT_GR.setdefault(lid, {})
    rows = []
    for e in roster:
        a = atts.get(e.id)
        if not a or a.mark == "y":
            continue
        draft.setdefault(e.id, a.grade)
        rows.append((e.id, e.student.full_name, draft[e.id]))
    return rows


def _grade_kb(lid: int, rows) -> IM:
    kb = [[IB(text=f"{GR_ICON[g]} {name}" + (f" — {S.GRADE_NAMES[g]}" if g else ""), callback_data=f"grd:{lid}:{eid}")]
          for eid, name, g in rows]
    kb.append([IB(text="💾 Saqlash", callback_data=f"grok:{lid}")])
    return IM(inline_keyboard=kb)


@router.callback_query(F.data.startswith("bh:"))
async def grade_open(c: CallbackQuery) -> None:
    lid = int(c.data.split(":")[1])
    rows = await _grade_rows(lid)
    if not rows:
        await c.answer("Davomat olinmagan yoki darsga hech kim kelmagan.", show_alert=True)
        return
    await c.answer()
    await c.message.answer("⭐ <b>Baho qo'yish</b> (ixtiyoriy). Bosgan sari o'zgaradi:\n▫️ bahosiz → 🌟 A'lo → "
                           "👍 Yaxshi → 👌 Qoniqarli → ⚠️ Yomon. Faqat kerakli o'quvchilarga qo'ying.",
                           reply_markup=_grade_kb(lid, rows))


@router.callback_query(F.data.startswith("grd:"))
async def grade_toggle(c: CallbackQuery) -> None:
    _, lid, eid = c.data.split(":")
    lid, eid = int(lid), int(eid)
    draft = DRAFT_GR.setdefault(lid, {})
    draft[eid] = GR_NEXT[draft.get(eid)]
    await c.message.edit_reply_markup(reply_markup=_grade_kb(lid, await _grade_rows(lid)))
    await c.answer()


@router.callback_query(F.data.startswith("grok:"))
async def grade_save(c: CallbackQuery, bot: Bot) -> None:
    lid = int(c.data.split(":")[1])
    draft = DRAFT_GR.pop(lid, {})
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        les = await s.get(Lesson, lid)
        g = await s.get(Group, les.group_id)
        changed = await S.save_grades(s, les, draft, who.staff)
        await s.commit()
        notes = []
        for sid, gr in changed:
            stu = await s.get(Student, sid)
            for t in await S.parent_tg_ids(s, sid):
                notes.append((t, f"⭐ {stu.full_name} · {g.name} · {les.date:%d.%m}\nBaho: <b>{S.GRADE_NAMES[gr]}</b>"))
    n = sum(1 for v in draft.values() if v)
    await c.message.edit_text(f"Baholar saqlandi: {n} ta o'quvchi." + (" «A'lo» uchun coin yozildi." if
                              any(v == "alo" for v in draft.values()) else ""))
    for t, text in notes:
        await send(bot, t, text)


# ---------- 💬 berilgan vazifaga izoh qo'shish ----------
class HwZ(StatesGroup):
    text = State()


@router.callback_query(F.data.startswith("hwz:"))
async def hwz_start(c: CallbackQuery, state: FSMContext) -> None:
    lid = int(c.data.split(":")[1])
    async with Session() as s:
        les = await s.get(Lesson, lid)
    main = bool(les and (les.homework_text or les.homework_file_id or les.homework_files))
    parts = (les.homework_parts or []) if les else []
    if not main and not parts:
        await c.answer("Avval vazifa bering — keyin izoh qo'shiladi.", show_alert=True)
        return
    await c.answer()
    if parts:
        rows = ([[IB(text="📚 Umumiy vazifaga", callback_data=f"hwzp:{lid}:m")]] if main else []) + [
            [IB(text=f"👥 {p.get('title') or f'{i + 1}-kichik guruh'} ({len(p['students'])} o'quvchi)",
                callback_data=f"hwzp:{lid}:{i}")] for i, p in enumerate(parts)]
        await c.message.answer("Qaysi vazifaga izoh?", reply_markup=IM(inline_keyboard=rows))
        return
    await _hwz_ask(c.message, state, lid, None)


@router.callback_query(F.data.startswith("hwzp:"))
async def hwz_part(c: CallbackQuery, state: FSMContext) -> None:
    _, lid, p = c.data.split(":")
    await c.answer()
    await _hwz_ask(c.message, state, int(lid), None if p == "m" else int(p))


async def _hwz_ask(msg: Message, state: FSMContext, lid: int, part: int | None) -> None:
    await state.clear()
    await state.set_state(HwZ.text)
    await state.update_data(lid=lid, part=part)
    await msg.answer("💬 Izohni yozing (masalan: «3-misolni ham bajaring» yoki «daftarni olib keling»).\n"
                     "U vazifa olgan ota-ona va o'quvchilarga yuboriladi va vazifaga qo'shiladi.")


@router.message(HwZ.text)
async def hwz_send(m: Message, state: FSMContext, bot: Bot) -> None:
    text = (m.text or m.caption or "").strip()
    if not text:
        await m.answer("Izohni matn bilan yozing.")
        return
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        les = await s.get(Lesson, d["lid"])
        g = await s.get(Group, les.group_id)
        roster = await S.group_roster(s, g.id, les.date)
        if d["part"] is None:
            taken = _part_ids(les)
            roster = [e for e in roster if e.id not in taken]
            les.homework_text = ((les.homework_text or "") + f"\n💬 {text}").strip()[:1500]
            title = None
        else:
            parts = [dict(p) for p in les.homework_parts]
            p = parts[d["part"]]
            only = set(p["students"])
            roster = [e for e in roster if e.id in only]
            p["text"] = ((p.get("text") or "") + f"\n💬 {text}").strip()[:1500]
            les.homework_parts = parts
            title = p.get("title")
        targets, missing = await _targets(s, roster)
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "vazifa_izoh", lesson=les.id, text=text[:200])
        await s.commit()
        lid = les.id
    msg = (f"💬 <b>Uy vazifasiga izoh</b>\n{g.name} · {les.date:%d.%m}" + (f" · {title}" if title else "")
           + f"\n\n{text}")
    sent = 0
    for t in targets:
        sent += bool(await send(bot, t, msg))
    await m.answer(f"✅ Izoh yuborildi: {sent} ta qabul qiluvchi." + (f"\n⚠️ Botga ulanmaganlar: {missing}" if missing else ""),
                   reply_markup=prompt_kb(lid, True))
