"""🌐 Online MOC — shaxsiy kanal orqali, Telegram mini-ilovasida.

Tashkilotchi (MOC → «🌐 Online MOC»): nom → format → savollar PDF + kalit → vaqt (hamma bir vaqtda yoki
oraliq + shaxsiy taymer) → maktab/sinf kerakmi → narx (faqat moliya/rahbar) → tayyor havola va kanalga e'lon.

Qatnashchi (kanaldagi tugma → bot): ism → telefon (tugma bilan) → [maktab, sinf] → [to'lov cheki] →
«▶️ Testni boshlash» (mini-ilova: savollar sahifalari + javob varag'i + taymer) → [yozma ish rasmlari].
Tashqi odam botda faqat o'z imtihonlarini ko'radi.

Muddat tugagach hammasi avtomatik: topshirmaganlar saqlangan javoblari bilan yakunlanadi, yozma qism bo'lsa
ustozga baholash navbati, so'ng natija + kalit + tahlil + sertifikat hammaga birdan, rahbarga kanal uchun TOP-10.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM,
                           KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)
from sqlalchemy import select

from app import services as S
from app.bot import keyboards as K
from app.bot.notify import send
from app.config import settings
from app.db.models import Moc, MocSeat, ServiceSale, Setting, Staff
from app.db.session import Session
from app.logic import moc as M

router = Router()
TZ = ZoneInfo(settings.timezone)
PRICERS = ("moliya", "rahbar")
GRADES = ["5", "6", "7", "8", "9", "10", "11", "Bitirgan", "Boshqa"]


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def cfg(moc: Moc) -> dict:
    return ((moc.meta or {}).get("online") or {}) if moc else {}


def is_online(moc: Moc) -> bool:
    return bool(cfg(moc))


def times(moc: Moc) -> tuple[datetime, datetime, int, str]:
    c = cfg(moc)
    return datetime.fromisoformat(c["start"]), datetime.fromisoformat(c["end"]), int(c["minutes"]), c["mode"]


def zinfo(z: MocSeat) -> dict:
    return dict(z.online or {})


def deadline(moc: Moc, z: MocSeat) -> datetime | None:
    st, end, minutes, mode = times(moc)
    o = zinfo(z)
    if mode == "fix":
        return end
    if not o.get("started"):
        return None
    return min(datetime.fromisoformat(o["started"]) + timedelta(minutes=minutes), end)


def token(z: MocSeat) -> str:
    return hmac.new(settings.bot_token.encode(), f"onl:{z.id}:{z.token}".encode(), hashlib.sha256).hexdigest()[:32]


def app_url(mid: int) -> str:
    return f"https://{settings.print_host}/o/app?m={mid}" if settings.print_host else ""


def parse_dt(text: str, base: date | None = None) -> datetime | None:
    """«30.11 10:00», «30.11.2026 10:00», «10:00» (bugun)."""
    t = text.strip().replace("/", ".").replace("-", ".")
    base = base or now().date()
    m = re.match(r"^(?:(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+)?(\d{1,2})[:.](\d{2})$", t)
    if not m:
        return None
    d, mo, y, hh, mm = m.groups()
    try:
        yy = int(y) + (2000 if y and len(y) == 2 else 0) if y else base.year
        day = date(yy, int(mo), int(d)) if d else base
        return datetime(day.year, day.month, day.day, int(hh), int(mm))
    except ValueError:
        return None


def when_text(moc: Moc) -> str:
    st, end, minutes, mode = times(moc)
    if mode == "fix":
        return f"📅 {st:%d.%m.%Y} · ⏰ {st:%H:%M}–{end:%H:%M} ({minutes} daqiqa, hamma bir vaqtda)"
    return (f"📅 {st:%d.%m %H:%M} dan {end:%d.%m %H:%M} gacha — istalgan paytda boshlaysiz\n"
            f"⏱ Boshlaganingizdan so'ng {minutes} daqiqa")


async def _staff(s, tg: int) -> Staff | None:
    return (await S.who_by_tg(s, tg)).staff


def _role(st: Staff | None) -> str:
    return st.current_role if st else ""


class On(StatesGroup):
    start = State()
    end = State()
    minutes = State()
    price = State()
    card = State()
    channel = State()
    score = State()


class Reg(StatesGroup):
    name = State()
    phone = State()
    school = State()


# ======================= tashkilotchi: sozlash =======================
async def ask_mode(msg: Message, state: FSMContext) -> None:
    if not settings.print_host:
        await msg.answer("⚠️ Online MOC uchun serverda HTTPS manzil (PRINT_HOST) sozlangan bo'lishi kerak — "
                         "printer dasturi uchun qilingan sozlamaning o'zi. Sozlangach qayta urinib ko'ring.")
        await state.clear()
        return
    await msg.answer("⏰ <b>Vaqt qanday bo'ladi?</b>", reply_markup=IM(inline_keyboard=[
        [IB(text="👥 Hamma bir vaqtda (masalan 10:00–13:00)", callback_data="onm:fix")],
        [IB(text="🕒 Oraliq + shaxsiy taymer", callback_data="onm:win")]]))


@router.callback_query(F.data.startswith("onm:"))
async def got_mode(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    if not w.get("online"):
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    w["omode"] = c.data.split(":")[1]
    await state.update_data(w=w)
    await state.set_state(On.start)
    await c.answer()
    await c.message.answer(("Boshlanish vaqti?" if w["omode"] == "fix" else "Test qachondan ochiladi?")
                           + "\nMasalan: <code>30.11 10:00</code>")


@router.message(On.start, F.text, ~F.text.in_(K.menu_texts()))
async def got_start(m: Message, state: FSMContext) -> None:
    t = parse_dt(m.text)
    if not t or t < now() - timedelta(minutes=5):
        return await m.answer("Vaqtni shunday yozing: <code>30.11 10:00</code> (o'tib ketgan vaqt bo'lmasin)")
    w = (await state.get_data())["w"]
    w["ostart"] = t.isoformat()
    await state.update_data(w=w)
    if w["omode"] == "fix":
        await state.set_state(On.minutes)
        return await m.answer("Davomiyligi necha daqiqa? (masalan <code>180</code>)")
    await state.set_state(On.end)
    await m.answer("Test qachon yopiladi? (shu vaqtdan keyin boshlab bo'lmaydi)\nMasalan: <code>2.12 22:00</code>")


@router.message(On.end, F.text, ~F.text.in_(K.menu_texts()))
async def got_end(m: Message, state: FSMContext) -> None:
    w = (await state.get_data())["w"]
    t = parse_dt(m.text, datetime.fromisoformat(w["ostart"]).date())
    if not t or t <= datetime.fromisoformat(w["ostart"]):
        return await m.answer("Yopilish vaqti ochilishdan keyin bo'lsin: <code>2.12 22:00</code>")
    w["oend"] = t.isoformat()
    await state.update_data(w=w)
    await state.set_state(On.minutes)
    await m.answer("Har bir qatnashchiga necha daqiqa? (masalan <code>180</code>)")


@router.message(On.minutes, F.text, ~F.text.in_(K.menu_texts()))
async def got_minutes(m: Message, state: FSMContext) -> None:
    try:
        v = int(m.text.strip())
        assert 5 <= v <= 600
    except (ValueError, AssertionError):
        return await m.answer("Daqiqani raqamda yozing (5–600): 180")
    w = (await state.get_data())["w"]
    w["omin"] = v
    if w["omode"] == "fix":
        w["oend"] = (datetime.fromisoformat(w["ostart"]) + timedelta(minutes=v)).isoformat()
    elif (datetime.fromisoformat(w["oend"]) - datetime.fromisoformat(w["ostart"])).total_seconds() < v * 60:
        return await m.answer("Oraliq bu vaqtdan qisqa — kamroq daqiqa yozing yoki oraliqni uzaytiring.")
    await state.update_data(w=w)
    await state.set_state(None)
    await m.answer("🏫 Qatnashchidan <b>maktab va sinf</b> so'ralsinmi?", reply_markup=IM(inline_keyboard=[
        [IB(text="✅ Ha", callback_data="ons:1"), IB(text="Yo'q — faqat ism va telefon", callback_data="ons:0")]]))


@router.callback_query(F.data.startswith("ons:"))
async def got_school(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    if not w.get("online"):
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    w["oschool"] = c.data.endswith("1")
    await state.update_data(w=w)
    await c.answer()
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
    if _role(st) not in PRICERS:
        w["price"] = 0
        await state.update_data(w=w)
        return await _confirm(c.message, state)
    await state.set_state(On.price)
    await c.message.answer("💵 Qatnashish narxi (so'm)? Bepul bo'lsa — 0.",
                           reply_markup=IM(inline_keyboard=[[IB(text="Bepul", callback_data="onp:0")]]))


def _money(text: str) -> int | None:
    t = text.lower().replace(" ", "").replace("so'm", "").replace("som", "")
    mul = 1000 if t.endswith(("k", "ming")) else 1
    try:
        v = int(round(float(t.rstrip("kming").replace(",", ".")) * mul))
        return v if 0 <= v <= 10_000_000 else None
    except ValueError:
        return None


@router.callback_query(F.data == "onp:0")
async def price_free(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    w["price"] = 0
    await state.update_data(w=w)
    await state.set_state(None)
    await c.answer()
    await _confirm(c.message, state)


@router.message(On.price, F.text, ~F.text.in_(K.menu_texts()))
async def price_typed(m: Message, state: FSMContext) -> None:
    v = _money(m.text)
    if v is None:
        return await m.answer("Summani raqamda yozing: 25000")
    w = (await state.get_data())["w"]
    w["price"] = v
    await state.update_data(w=w)
    await state.set_state(None)
    async with Session() as s:
        card = await s.get(Setting, "online_card")
    if v and not (card and card.value):
        await state.set_state(On.card)
        return await m.answer("💳 Qatnashchilar to'lovni qaysi kartaga o'tkazadi? Karta raqami va egasini yozing "
                              "(bir marta so'raladi, keyin sozlamada qoladi):\n<code>8600 1234 5678 9012 — Sarvar M.</code>")
    await _confirm(m, state)


@router.message(On.card, F.text, ~F.text.in_(K.menu_texts()))
async def card_typed(m: Message, state: FSMContext) -> None:
    txt = m.text.strip()[:120]
    if len(re.sub(r"\D", "", txt)) < 12:
        return await m.answer("Karta raqamini to'liq yozing (16 ta raqam) va egasini.")
    async with Session() as s:
        row = await s.get(Setting, "online_card")
        if row:
            row.value = {"text": txt}
        else:
            s.add(Setting(key="online_card", value={"text": txt}))
        await s.commit()
    await state.set_state(None)
    if (await state.get_data()).get("w"):
        return await _confirm(m, state)
    await state.clear()
    await m.answer("✅ Karta saqlandi — qatnashchilar to'lovni shu kartaga o'tkazadi.")


async def _confirm(msg: Message, state: FSMContext) -> None:
    w = (await state.get_data())["w"]
    sp = M.parse_spec(w["spec"])
    fake = Moc(meta={"online": {"start": w["ostart"], "end": w["oend"], "minutes": w["omin"], "mode": w["omode"]}})
    await msg.answer(
        f"🌐 <b>{w['title']}</b> — online MOC\n\n{sp.summary()}\n\n{when_text(fake)}\n"
        f"🏫 Maktab/sinf: {'so`raladi' if w.get('oschool') else 'so`ralmaydi'}\n"
        f"💵 Narx: {S.fmt_som(w['price']) if w.get('price') else 'bepul (yoki moliya/rahbar belgilaydi)'}\n"
        "🔒 Har bir akkaunt va telefon — bitta urinish\n📊 Natija, kalit va tahlil — muddat tugagach hammaga birdan",
        reply_markup=IM(inline_keyboard=[[IB(text="✅ Yaratish", callback_data="onok"),
                                          IB(text="❌ Bekor", callback_data="mqno")]]))


@router.callback_query(F.data == "onok")
async def create(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    from app.bot import moc as MB
    w = (await state.get_data()).get("w") or {}
    if not w.get("online") or not w.get("ostart"):
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    await state.clear()
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        moc = Moc(kind="moc", title=w["title"], date=datetime.fromisoformat(w["ostart"]).date(), spec=w["spec"],
                  n_variants=1, booklets=w["booklets"], keys=w["keys"], group_ids=[], price=int(w.get("price") or 0),
                  created_by=st.id if st else None,
                  meta={"pay_all": True, "online": {"start": w["ostart"], "end": w["oend"], "minutes": w["omin"],
                                                    "mode": w["omode"], "school": bool(w.get("oschool"))}})
        s.add(moc)
        await S.audit(s, st, "online_moc", title=moc.title)
        await s.commit()
        mid = moc.id
    if _role(st) not in PRICERS:
        from app.bot.moc_ext import ask_price
        await ask_price(bot, mid)
    await MB.card(c.message, mid, c.from_user.id)


# ======================= tashkilotchi: karta =======================
async def online_card(msg: Message, moc: Moc, sp: M.Spec, seats: list[MocSeat], st, edit: bool) -> None:
    from app.bot import moc as MB
    from app.bot.moc_ext import money_line, money_rows
    uname = await MB._bot_username(msg.bot)
    c = cfg(moc)
    reg = [z for z in seats if z.tg_id]
    paid = [z for z in reg if not moc.price or z.paid >= moc.price]
    started = [z for z in reg if zinfo(z).get("started")]
    done = [z for z in reg if zinfo(z).get("submitted")]
    stage = c.get("stage") or ("yakun" if moc.status == "yakun" else "")
    lines = [f"🌐 <b>{moc.title}</b> — online MOC" + (" · ✅ yakunlangan" if moc.status == "yakun" else ""),
             when_text(moc),
             f"💵 {S.fmt_som(moc.price) if moc.price else 'Bepul'} · 🏫 maktab/sinf: {'ha' if c.get('school') else 'yo`q'}",
             f"\n👥 Ro'yxatdan o'tgan: <b>{len(reg)}</b>" + (f" · to'lagan: {len(paid)}" if moc.price else ""),
             f"▶️ Boshlagan: {len(started)} · ✅ Topshirgan: <b>{len(done)}</b>"]
    if stage == "baholash":
        left = sum(1 for z in done if any(n not in (z.baho or {}) for n in _baho_names(sp)))
        lines.append(f"✍️ Yozma ishlar baholanmoqda: qoldi {left} ta")
    if uname:
        lines.append(f"\n🔗 Havola: https://t.me/{uname}?start=o{moc.id}")
    r = MB._role(st)
    if r in PRICERS:
        lines.append(await money_line(moc, MB.payers(moc, seats)))
    rows = []
    if moc.status == "tayyor":
        rows.append([IB(text="📢 Kanalga e'lon", callback_data=f"onch:{moc.id}")])
        if stage == "baholash":
            rows.append([IB(text="✍️ Yozma ishlarni baholash", callback_data=f"onh:{moc.id}")])
        elif r in MB.MAKERS:
            rows.append([IB(text="⏹ Muddatidan oldin yakunlash", callback_data=f"onx:{moc.id}")])
    rows.append([IB(text="👥 Qatnashchilar", callback_data=f"mql:{moc.id}")])
    if moc.status == "yakun":
        rows.append([IB(text="📊 Natijalar (Excel)", callback_data=f"mqxl:{moc.id}"),
                     IB(text="📈 Savollar tahlili", callback_data=f"anm:{moc.id}")])
        rows.append([IB(text="🏆 TOP-10 ni kanalga", callback_data=f"ontop:{moc.id}")])
    if r in PRICERS:
        rows += money_rows(moc)
    if moc.status == "tayyor" and r in MB.MAKERS and not done:
        rows.append([IB(text="🗑 Bekor qilish", callback_data=f"mqdel:{moc.id}")])
    rows.append([IB(text="⬅️ Ro'yxat", callback_data="mq:list")])
    text = "\n".join(x for x in lines if x)
    if edit:
        try:
            return await msg.edit_text(text, reply_markup=IM(inline_keyboard=rows), disable_web_page_preview=True)
        except Exception:
            pass
    await msg.answer(text, reply_markup=IM(inline_keyboard=rows), disable_web_page_preview=True)


def _baho_names(sp: M.Spec) -> list[str]:
    return [x.name for x in sp.sections if x.kind == "baho"]


# ======================= kanal =======================
def post_text(moc: Moc, sp: M.Spec) -> str:
    secs = [f"• {x.name}: {x.n} ta" if x.kind != "baho" else f"• {x.name} (yozma ish)" for x in sp.sections]
    return (f"🎓 <b>{moc.title}</b>\n<b>ONLINE MOC</b> — Telegram'ning o'zida\n\n{when_text(moc)}\n\n"
            + "\n".join(secs)
            + f"\n\n💵 Qatnashish: <b>{S.fmt_som(moc.price) if moc.price else 'BEPUL'}</b>"
            + ("\n🏅 Daraja sertifikati va 1–3-o'rinlarga g'olib sertifikati" if sp.levels else
               "\n🏅 1–3-o'rinlarga g'olib sertifikati")
            + "\n📊 Natija, xatolar va mavzular tahlili — test yakunlangach shaxsan sizga"
            + "\n\n👇 Qatnashish uchun tugmani bosing")


async def _channel(s) -> str | None:
    row = await s.get(Setting, "online_channel")
    return (row.value or {}).get("chat") if row else None


@router.callback_query(F.data.startswith("onch:"))
async def channel_preview(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    from app.bot import moc as MB
    mid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        if _role(st) != "rahbar":
            return await c.answer("Kanalga faqat rahbar joylaydi.", show_alert=True)
        ch = await _channel(s)
        moc = await s.get(Moc, mid)
    await c.answer()
    if not ch:
        await state.set_state(On.channel)
        await state.update_data(onmid=mid)
        return await c.message.answer("📢 Kanalingiz manzilini yozing: <code>@kanal_nomi</code>\n"
                                      "Avval botni kanalga admin qilib qo'shing («Xabar joylash» huquqi yetarli).")
    uname = await MB._bot_username(bot)
    await c.message.answer(f"👀 <b>Kanalga shunday chiqadi</b> ({ch}):")
    await c.message.answer(post_text(moc, MB._spec(moc)), reply_markup=IM(inline_keyboard=[
        [IB(text="📝 Qatnashish", url=f"https://t.me/{uname}?start=o{mid}")]]))
    await c.message.answer("Joylaymizmi?", reply_markup=IM(inline_keyboard=[
        [IB(text="📢 Kanalga joylash", callback_data=f"onpub:{mid}")],
        [IB(text="🔁 Boshqa kanal", callback_data=f"onchx:{mid}")]]))


@router.callback_query(F.data.startswith("onchx:"))
async def channel_reset(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.set_state(On.channel)
    await state.update_data(onmid=int(c.data.split(":")[1]))
    await c.message.answer("📢 Kanal manzilini yozing: <code>@kanal_nomi</code>")


@router.message(On.channel, F.text, ~F.text.in_(K.menu_texts()))
async def channel_typed(m: Message, state: FSMContext, bot: Bot) -> None:
    raw = m.text.strip()
    ch = "@" + raw.rsplit("/", 1)[-1].lstrip("@") if not raw.lstrip("-").isdigit() else raw
    try:
        me = await bot.get_me()
        mem = await bot.get_chat_member(ch, me.id)
        ok = mem.status in ("administrator", "creator")
    except Exception:
        ok = False
    if not ok:
        return await m.answer(f"❌ Bot {ch} kanalida admin emas yoki kanal topilmadi.\n"
                              "Kanal → Adminlar → botni qo'shing («Xabar joylash» huquqi), so'ng qayta yozing.")
    mid = (await state.get_data()).get("onmid")
    await state.clear()
    async with Session() as s:
        row = await s.get(Setting, "online_channel")
        if row:
            row.value = {"chat": ch}
        else:
            s.add(Setting(key="online_channel", value={"chat": ch}))
        await s.commit()
    await m.answer(f"✅ Kanal saqlandi: {ch}", reply_markup=IM(inline_keyboard=[
        [IB(text="👀 E'lonni ko'rish", callback_data=f"onch:{mid}")]]) if mid else None)


@router.callback_query(F.data.startswith("onpub:"))
async def channel_publish(c: CallbackQuery, bot: Bot) -> None:
    from app.bot import moc as MB
    mid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        if _role(st) != "rahbar":
            return await c.answer("Faqat rahbar.", show_alert=True)
        ch = await _channel(s)
        moc = await s.get(Moc, mid)
    uname = await MB._bot_username(bot)
    try:
        await bot.send_message(ch, post_text(moc, MB._spec(moc)), reply_markup=IM(inline_keyboard=[
            [IB(text="📝 Qatnashish", url=f"https://t.me/{uname}?start=o{mid}")]]))
    except Exception as e:
        return await c.answer(f"Joylab bo'lmadi: {str(e)[:150]}", show_alert=True)
    await c.answer("📢 Joylandi")
    await c.message.edit_text(f"✅ E'lon {ch} kanaliga joylandi.")


# ======================= qatnashchi: ro'yxatdan o'tish =======================
async def register_start(m: Message, state: FSMContext, mid: int) -> bool:
    async with Session() as s:
        moc = await s.get(Moc, mid)
        if not moc or not is_online(moc):
            return False
        z = (await s.execute(select(MocSeat).where(MocSeat.moc_id == mid, MocSeat.tg_id == m.from_user.id))).scalars().first()
    if z:
        await status_view(m, moc, z)
        return True
    st, end, _, _ = times(moc)
    if moc.status != "tayyor" or now() >= end:
        await m.answer(f"🎓 <b>{moc.title}</b>\nRo'yxatdan o'tish yopilgan.", reply_markup=ReplyKeyboardRemove())
        return True
    await state.clear()
    await state.update_data(onreg={"mid": mid})
    await state.set_state(Reg.name)
    sp = M.parse_spec(moc.spec)
    await m.answer(f"🎓 <b>{moc.title}</b> — online MOC\n\n{when_text(moc)}\n"
                   f"💵 {S.fmt_som(moc.price) if moc.price else 'Bepul'} · {sp.n_questions} ta savol\n\n"
                   "📝 <b>Ro'yxatdan o'tish</b> (1 daqiqa)\nIsm va familiyangizni yozing — sertifikatga shunday yoziladi:",
                   reply_markup=ReplyKeyboardRemove())
    return True


@router.message(Reg.name, F.text, ~F.text.in_(K.menu_texts()))
async def reg_name(m: Message, state: FSMContext) -> None:
    name = " ".join(m.text.split())[:80]
    if len(name.split()) < 2 or any(ch.isdigit() for ch in name):
        return await m.answer("Ism va familiyani to'liq yozing, masalan: <i>Aliyev Vali</i>")
    d = (await state.get_data())["onreg"]
    d["name"] = name
    await state.update_data(onreg=d)
    await state.set_state(Reg.phone)
    await m.answer("📱 Telefon raqamingizni pastdagi tugma orqali yuboring:", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Raqamni yuborish", request_contact=True)]], resize_keyboard=True,
        one_time_keyboard=True))


@router.message(Reg.phone, F.contact)
async def reg_phone(m: Message, state: FSMContext, bot: Bot) -> None:
    if m.contact.user_id != m.from_user.id:
        return await m.answer("O'zingizning raqamingizni tugma orqali yuboring.")
    phone = S.norm_phone(m.contact.phone_number) or m.contact.phone_number
    d = (await state.get_data())["onreg"]
    async with Session() as s:
        moc = await s.get(Moc, d["mid"])
        dup = (await s.execute(select(MocSeat).where(MocSeat.moc_id == moc.id, MocSeat.phone == phone))).scalars().first()
    if dup:
        await state.clear()
        return await m.answer("❗ Bu raqam bilan allaqachon ro'yxatdan o'tilgan — har bir qatnashchi bir marta "
                              "ishtirok etadi.", reply_markup=ReplyKeyboardRemove())
    d["phone"] = phone
    await state.update_data(onreg=d)
    if cfg(moc).get("school"):
        await state.set_state(Reg.school)
        return await m.answer("🏫 Maktabingiz (masalan: <i>12-maktab, Urganch</i>):", reply_markup=ReplyKeyboardRemove())
    await _make_seat(m, state, bot)


@router.message(Reg.phone)
async def reg_phone_text(m: Message) -> None:
    await m.answer("Iltimos, «📱 Raqamni yuborish» tugmasini bosing.")


@router.message(Reg.school, F.text, ~F.text.in_(K.menu_texts()))
async def reg_school(m: Message, state: FSMContext) -> None:
    d = (await state.get_data())["onreg"]
    d["school"] = m.text.strip()[:80]
    await state.update_data(onreg=d)
    await m.answer("Nechanchi sinfdasiz?", reply_markup=IM(inline_keyboard=[
        [IB(text=g, callback_data=f"ong:{g}") for g in GRADES[:5]],
        [IB(text=g, callback_data=f"ong:{g}") for g in GRADES[5:]]]))


@router.callback_query(F.data.startswith("ong:"))
async def reg_grade(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    d = (await state.get_data()).get("onreg")
    if not d or "phone" not in d:
        return await c.answer("Jarayon eskirgan — kanaldagi tugmani qayta bosing.", show_alert=True)
    d["grade"] = c.data.split(":", 1)[1]
    await state.update_data(onreg=d)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    await _make_seat(c.message, state, bot, c.from_user.id)


async def _make_seat(msg: Message, state: FSMContext, bot: Bot, tg: int | None = None) -> None:
    from app.bot import moc as MB
    tg = tg or msg.from_user.id
    d = (await state.get_data())["onreg"]
    await state.clear()
    async with Session() as s:
        moc = await s.get(Moc, d["mid"])
        if (await s.execute(select(MocSeat).where(MocSeat.moc_id == moc.id, MocSeat.tg_id == tg))).scalars().first():
            return
        ids = await MB._make_seats(s, moc, [], [(d["name"], d["phone"])], 0)
        z = await s.get(MocSeat, ids[0])
        z.tg_id = tg
        z.online = {"reg": now().isoformat(), **{k: d[k] for k in ("school", "grade") if d.get(k)}}
        await s.commit()
    await msg.answer(f"✅ <b>{d['name']}</b>, ro'yxatdan o'tdingiz!", reply_markup=ReplyKeyboardRemove())
    await status_view(msg, moc, z)


async def status_view(msg: Message, moc: Moc, z: MocSeat) -> None:
    o = zinfo(z)
    st, end, minutes, mode = times(moc)
    head = f"🎓 <b>{moc.title}</b>\n{when_text(moc)}\n"
    if moc.status == "yakun":
        return await msg.answer(head + "\n🏁 Imtihon yakunlangan — natijangiz sizga yuborilgan.")
    if o.get("submitted"):
        return await msg.answer(head + f"\n✅ Javoblaringiz qabul qilingan. Natija, kalit va tahlil "
                                       f"<b>{end:%d.%m %H:%M}</b> dan so'ng shu yerga keladi.")
    if moc.price and z.paid < moc.price:
        async with Session() as s:
            card = await s.get(Setting, "online_card")
        if o.get("receipt"):
            return await msg.answer(head + "\n⏳ Chekingiz tekshirilmoqda — tasdiqlangach xabar beramiz.")
        z_o = {**o, "await_receipt": True}
        async with Session() as s:
            zz = await s.get(MocSeat, z.id)
            zz.online = z_o
            await s.commit()
        return await msg.answer(head + f"\n💳 <b>To'lov: {S.fmt_som(moc.price)}</b>\nKarta: <code>"
                                       f"{(card.value or {}).get('text', '—') if card else '—'}</code>\n\n"
                                       "To'lovdan so'ng <b>chek rasmini (skrinshot) shu yerga yuboring</b>.")
    t = now()
    if t < st:
        return await msg.answer(head + f"\n⏳ Test <b>{st:%d.%m %H:%M}</b> da ochiladi — o'sha paytda bot o'zi "
                                       "eslatadi va «▶️ Boshlash» tugmasini yuboradi.")
    if t >= end or (o.get("started") and deadline(moc, z) and t >= deadline(moc, z)):
        return await msg.answer(head + "\n⌛ Test vaqti tugagan.")
    await msg.answer(head + "\n" + rules_text(moc), reply_markup=start_kb(moc))


def rules_text(moc: Moc) -> str:
    _, end, minutes, mode = times(moc)
    return ("📌 <b>Qoidalar</b>\n• Bitta urinish — yakunlaganingizdan so'ng javob o'zgarmaydi\n"
            + (f"• Test {end:%H:%M} da hammaga birdan yopiladi\n" if mode == "fix" else
               f"• Boshlaganingizdan so'ng {minutes} daqiqa vaqtingiz bor, taymer to'xtamaydi\n")
            + "• Javoblar har belgilashda saqlanadi — internet uzilsa, qayta kirib davom etasiz\n"
            + "• Natija va kalit muddat tugagach hammaga birdan")


def start_kb(moc: Moc) -> IM:
    return IM(inline_keyboard=[[IB(text="▶️ Testni boshlash", web_app=WebAppInfo(url=app_url(moc.id)))]])


# ======================= chek va yozma ish rasmlari =======================
async def _my_open_seat(s, tg: int, flag: str) -> tuple[Moc, MocSeat] | None:
    rows = (await s.execute(select(MocSeat, Moc).join(Moc, Moc.id == MocSeat.moc_id)
                            .where(MocSeat.tg_id == tg, Moc.status == "tayyor"))).all()
    for z, moc in rows:
        if is_online(moc) and zinfo(z).get(flag):
            return moc, z
    return None


@router.message(F.photo | F.document)
async def media(m: Message, bot: Bot, state: FSMContext) -> None:
    if await state.get_state():                          # boshqa jarayon ichida — tegmaymiz
        raise SkipHandler()
    async with Session() as s:
        hit = await _my_open_seat(s, m.from_user.id, "await_receipt")
        kind = "receipt" if hit else None
        if not hit:
            hit = await _my_open_seat(s, m.from_user.id, "await_photos")
            kind = "photos" if hit else None
        if not hit:
            raise SkipHandler()
        moc, z = hit
        fid = m.photo[-1].file_id if m.photo else m.document.file_id
        o = zinfo(z)
        if kind == "receipt":
            o.update(receipt=("p:" if m.photo else "d:") + fid, await_receipt=False)
            z.online = o
            await s.commit()
            to = []
            for r in PRICERS:
                to += [x.tg_id for x in await S.staff_with_role(s, r) if x.tg_id and x.tg_id not in to]
        else:
            dl = deadline(moc, z)
            if dl and now() > dl + timedelta(minutes=20):
                o["await_photos"] = False
                z.online = o
                await s.commit()
                return await m.answer("⌛ Yozma ish yuborish vaqti tugagan.")
            o["photos"] = list(o.get("photos") or []) + [fid]
            z.online = o
            await s.commit()
    if kind == "receipt":
        await m.answer("✅ Chek qabul qilindi — tekshirilgach testni boshlash tugmasi keladi.")
        cap = (f"💳 <b>Online MOC to'lovi</b>\n{moc.title}\n{z.name} · {z.phone}\nSumma: {S.fmt_som(moc.price)}")
        kb = IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=f"onpay:{z.id}:1"),
                                  IB(text="❌ Rad etish", callback_data=f"onpay:{z.id}:0")]])
        for t in to:
            try:
                if m.photo:
                    await bot.send_photo(t, fid, caption=cap, reply_markup=kb)
                else:
                    await bot.send_document(t, fid, caption=cap, reply_markup=kb)
            except Exception:
                pass
        return
    n = len(zinfo(z).get("photos") or [])
    await m.answer(f"📷 Qabul qilindi ({n} ta). Hammasini yuborgach «✅ Tayyor» ni bosing.",
                   reply_markup=IM(inline_keyboard=[[IB(text="✅ Tayyor", callback_data=f"onwd:{z.id}")]]))


@router.callback_query(F.data.startswith("onwd:"))
async def photos_done(c: CallbackQuery) -> None:
    async with Session() as s:
        z = await s.get(MocSeat, int(c.data.split(":")[1]))
        if not z or z.tg_id != c.from_user.id:
            return await c.answer()
        o = zinfo(z)
        o["await_photos"] = False
        z.online = o
        await s.commit()
    await c.answer("Saqlandi")
    await c.message.edit_text(f"✅ Yozma ishingiz qabul qilindi ({len(o.get('photos') or [])} ta rasm). "
                              "Natija muddat tugagach keladi.")


@router.callback_query(F.data.startswith("onpay:"))
async def pay_decide(c: CallbackQuery, bot: Bot) -> None:
    from app.bot.moc import moc_service
    _, zid, ok = c.data.split(":")
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        if _role(st) not in PRICERS:
            return await c.answer("Faqat moliya yoki rahbar.", show_alert=True)
        z = await s.get(MocSeat, int(zid))
        moc = await s.get(Moc, z.moc_id)
        o = zinfo(z)
        if z.paid >= moc.price:
            return await c.answer("Allaqachon tasdiqlangan.", show_alert=True)
        if ok == "1":
            sv = await moc_service(s)
            s.add(ServiceSale(service_id=sv.id, qty=1, amount=moc.price, profit=moc.price, method="karta", by_id=st.id))
            z.paid, z.pay_method = moc.price, "karta"
            await S.audit(s, st, "online_tolov", seat=z.id, amount=moc.price)
        else:
            o.pop("receipt", None)
            o["await_receipt"] = True
            z.online = o
        await s.commit()
    await c.answer("Tasdiqlandi" if ok == "1" else "Rad etildi")
    try:
        await c.message.edit_caption(caption=(c.message.caption or "") + ("\n\n✅ Tasdiqlandi" if ok == "1"
                                                                          else "\n\n❌ Rad etildi"), reply_markup=None)
    except Exception:
        pass
    if ok == "1":
        await send(bot, z.tg_id, "✅ To'lovingiz tasdiqlandi!")
        st_, _, _, _ = times(moc)
        if now() >= st_:
            await send(bot, z.tg_id, f"🎓 <b>{moc.title}</b>\n" + rules_text(moc), reply_markup=start_kb(moc))
        else:
            await send(bot, z.tg_id, f"⏳ Test <b>{st_:%d.%m %H:%M}</b> da ochiladi — bot o'zi eslatadi.")
    else:
        await send(bot, z.tg_id, "❌ Chek tasdiqlanmadi (summa yoki rasm noto'g'ri). To'g'ri chekni qayta yuboring.")


# ======================= mini-ilova bilan bog'lanish =======================
def sections_json(sp: M.Spec) -> list[dict]:
    return [{"name": x.name, "kind": x.kind, "start": x.start, "n": x.n, "opts": x.opts, "mx": int(x.mx or 0)}
            for x in sp.sections]


async def on_submitted(bot: Bot, moc: Moc, z: MocSeat, auto: bool = False) -> None:
    sp = M.parse_spec(moc.spec)
    _, end, _, _ = times(moc)
    txt = ("⌛ Vaqt tugadi — belgilagan javoblaringiz avtomatik topshirildi." if auto else
           "✅ <b>Javoblaringiz qabul qilindi!</b>")
    txt += f"\nNatija, kalit va tahlil <b>{end:%d.%m %H:%M}</b> dan so'ng shu yerga keladi."
    names = _baho_names(sp)
    if names and not auto:
        txt += (f"\n\n✍️ <b>Yozma qism</b> ({', '.join(names)}): ishingizni rasmga olib <b>shu chatga yuboring</b> "
                "(bir nechta rasm bo'lishi mumkin), so'ng «✅ Tayyor» ni bosing. 20 daqiqa vaqt bor.")
    await send(bot, z.tg_id, txt)


# ======================= vaqt bo'yicha avtomatika =======================
async def tick(bot: Bot) -> None:
    """Har 2 daqiqada: boshlanish eslatmasi, muddati o'tganlarni yakunlash, yozma qism bo'lsa — baholash navbati."""
    t = now()
    async with Session() as s:
        mocs = [x for x in (await s.execute(select(Moc).where(Moc.status == "tayyor"))).scalars() if is_online(x)]
    for moc in mocs:
        st, end, minutes, mode = times(moc)
        c = cfg(moc)
        if t >= st and not c.get("opened"):
            await _set_cfg(moc.id, opened=True)
            async with Session() as s:
                seats = [z for z in (await s.execute(select(MocSeat).where(MocSeat.moc_id == moc.id))).scalars()
                         if z.tg_id and (not moc.price or z.paid >= moc.price) and not zinfo(z).get("started")]
            for z in seats:
                await send(bot, z.tg_id, f"🔔 <b>{moc.title}</b> boshlandi!\n\n" + rules_text(moc),
                           reply_markup=start_kb(moc))
        elif st - timedelta(hours=1) <= t < st and not c.get("warned"):
            await _set_cfg(moc.id, warned=True)
            async with Session() as s:
                seats = [z for z in (await s.execute(select(MocSeat).where(MocSeat.moc_id == moc.id))).scalars()
                         if z.tg_id]
            for z in seats:
                await send(bot, z.tg_id, f"⏰ <b>{moc.title}</b> {st:%H:%M} da boshlanadi. Internet va telefon "
                                         "quvvatini tekshirib qo'ying!")
        # shaxsiy taymeri tugaganlar — avtomatik topshirish
        async with Session() as s:
            seats = list((await s.execute(select(MocSeat).where(MocSeat.moc_id == moc.id))).scalars())
            late = []
            for z in seats:
                o = zinfo(z)
                dl = deadline(moc, z)
                if o.get("started") and not o.get("submitted") and dl and t >= dl + timedelta(seconds=30):
                    o["submitted"], o["auto"] = t.isoformat(), True
                    z.online = o
                    late.append(z)
            await s.commit()
        for z in late:
            await on_submitted(bot, moc, z, auto=True)
        if t >= end + timedelta(minutes=1) and c.get("stage") not in ("baholash",):
            await close(bot, moc.id)


async def _set_cfg(mid: int, **kw) -> None:
    async with Session() as s:
        moc = await s.get(Moc, mid)
        meta = dict(moc.meta or {})
        meta["online"] = {**(meta.get("online") or {}), **kw}
        moc.meta = meta
        await s.commit()


async def close(bot: Bot, mid: int, force: bool = False) -> None:
    """Muddat tugadi: topshirmaganlar → yakun yoki «kelmadi»; yozma qism bo'lsa → baholash; aks holda natija."""
    async with Session() as s:
        moc = await s.get(Moc, mid)
        sp = M.parse_spec(moc.spec)
        names = _baho_names(sp)
        need_grade = []
        for z in await _seats(s, mid):
            if not z.tg_id:
                continue
            o = zinfo(z)
            if o.get("started") and not o.get("submitted"):
                o["submitted"], o["auto"] = now().isoformat(), True
            o["await_photos"] = o["await_receipt"] = False
            z.online = o
            if not o.get("submitted"):
                z.absent = True
            elif not z.answers:
                z.answers = "-" * sp.n_questions
            if o.get("submitted") and names:
                if o.get("photos"):
                    if any(n not in (z.baho or {}) for n in names):
                        need_grade.append(z.id)
                else:
                    z.baho = {**(z.baho or {}), **{n: 0.0 for n in names if n not in (z.baho or {})}}
        await s.commit()
        creator = await s.get(Staff, moc.created_by) if moc.created_by else None
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    if need_grade and not force:
        await _set_cfg(mid, stage="baholash")
        for t in dict.fromkeys(([creator.tg_id] if creator and creator.tg_id else []) + bosses):
            await send(bot, t, f"✍️ <b>{moc.title}</b> — test yakunlandi. {len(need_grade)} ta yozma ishni "
                               "baholang; tugashi bilan natijalar hammaga avtomatik yuboriladi.",
                       reply_markup=IM(inline_keyboard=[[IB(text="✍️ Baholashni boshlash", callback_data=f"onh:{mid}")]]))
        return
    await finalize(bot, mid)


async def _seats(s, mid: int) -> list[MocSeat]:
    return list((await s.execute(select(MocSeat).where(MocSeat.moc_id == mid).order_by(MocSeat.id))).scalars())


async def finalize(bot: Bot, mid: int) -> None:
    from app.bot.certs import moc_certs
    from app.bot.moc import compute_and_send
    async with Session() as s:
        moc = await s.get(Moc, mid)
        if moc.status != "tayyor":
            return
        creator = await s.get(Staff, moc.created_by) if moc.created_by else None
        if not creator:
            creator = next(iter(await S.staff_with_role(s, "rahbar")), None)
        present = [z for z in await _seats(s, mid) if z.tg_id and not z.absent]
    await _set_cfg(mid, stage="yakun")
    if not present:
        async with Session() as s:
            m2 = await s.get(Moc, mid)
            m2.status = "yakun"
            await s.commit()
        if creator and creator.tg_id:
            await send(bot, creator.tg_id, f"🏁 <b>{moc.title}</b> yakunlandi — hech kim topshirmadi.")
        return
    n_sent, summary, xls = await compute_and_send(bot, mid, creator.id)
    await moc_certs(bot, mid)
    async with Session() as s:
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    for t in dict.fromkeys(([creator.tg_id] if creator.tg_id else []) + bosses):
        await send(bot, t, summary)
        try:
            await bot.send_document(t, BufferedInputFile(xls, f"MOC{mid}_natijalar.xlsx"),
                                    caption=f"📊 Natijalar · {n_sent} ta manzilga yuborildi")
        except Exception:
            pass
    for t in bosses:
        await top_preview(bot, mid, t)


# ======================= yozma ishlarni baholash =======================
@router.callback_query(F.data.startswith("onh:"))
async def grade_start(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    from app.bot import moc as MB
    mid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        if MB._role(st) not in MB.MAKERS:
            return await c.answer("Faqat ustoz yoki rahbar.", show_alert=True)
    await c.answer()
    await _grade_next(c.message, state, bot, mid)


async def _grade_next(msg: Message, state: FSMContext, bot: Bot, mid: int) -> None:
    async with Session() as s:
        moc = await s.get(Moc, mid)
        sp = M.parse_spec(moc.spec)
        names = _baho_names(sp)
        todo = [z for z in await _seats(s, mid) if zinfo(z).get("submitted") and zinfo(z).get("photos")
                and any(n not in (z.baho or {}) for n in names)]
    if not todo:
        await state.clear()
        await msg.answer("✅ Hamma yozma ishlar baholandi — natijalar yuborilmoqda…")
        return await finalize(bot, mid)
    z = todo[0]
    sec = next(x for x in sp.sections if x.kind == "baho" and x.name not in (z.baho or {}))
    photos = zinfo(z)["photos"]
    for fid in photos[:10]:
        try:
            await bot.send_photo(msg.chat.id, fid)
        except Exception:
            try:
                await bot.send_document(msg.chat.id, fid)
            except Exception:
                pass
    await state.set_state(On.score)
    await state.update_data(ong={"mid": mid, "zid": z.id, "sec": sec.name, "mx": sec.mx})
    await msg.answer(f"✍️ <b>{sec.name}</b> · qatnashchi kodi {z.code} (qoldi {len(todo)} ta)\n"
                     f"Ball 0–{sec.mx:g} — raqam yozing:")


@router.message(On.score, F.text, ~F.text.in_(K.menu_texts()))
async def grade_score(m: Message, state: FSMContext, bot: Bot) -> None:
    d = (await state.get_data()).get("ong") or {}
    try:
        v = float(m.text.strip().replace(",", "."))
        assert 0 <= v <= d["mx"]
    except (ValueError, AssertionError, KeyError):
        return await m.answer(f"0 dan {d.get('mx', 0):g} gacha raqam yozing.")
    async with Session() as s:
        z = await s.get(MocSeat, d["zid"])
        z.baho = {**(z.baho or {}), d["sec"]: v}
        await s.commit()
    await _grade_next(m, state, bot, d["mid"])


@router.callback_query(F.data.startswith("onx:"))
async def close_now_ask(c: CallbackQuery) -> None:
    mid = int(c.data.split(":")[1])
    await c.answer()
    await c.message.answer("⏹ Online MOC hozir yakunlansinmi? Topshirmaganlar saqlangan javoblari bilan yakunlanadi, "
                           "natijalar hammaga yuboriladi.", reply_markup=IM(inline_keyboard=[
                               [IB(text="✅ Ha, yakunlash", callback_data=f"onxy:{mid}"),
                                IB(text="↩️ Yo'q", callback_data=f"mqc:{mid}")]]))


@router.callback_query(F.data.startswith("onxy:"))
async def close_now(c: CallbackQuery, bot: Bot) -> None:
    from app.bot import moc as MB
    mid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        if MB._role(st) not in MB.MAKERS:
            return await c.answer("Ruxsat yo'q.", show_alert=True)
        moc = await s.get(Moc, mid)
        meta = dict(moc.meta or {})
        meta["online"] = {**meta["online"], "end": min(now(), times(moc)[1]).isoformat()}
        moc.meta = meta
        await s.commit()
    await c.answer("Yakunlanmoqda…")
    await close(bot, mid)


# ======================= TOP-10 kanal uchun =======================
async def top_preview(bot: Bot, mid: int, to: int) -> None:
    from app.bot import moc as MB
    from app.logic.analysis_card import top_list
    async with Session() as s:
        moc = await s.get(Moc, mid)
        sp = MB._spec(moc)
        seats = [z for z in await _seats(s, mid) if z.result and not z.absent]
    if not seats:
        return
    seats.sort(key=lambda z: (z.result.get("rank") or 999, z.name or ""))
    top = [z for z in seats if (z.result.get("rank") or 999) <= 10][:15]
    rows = [(z.result.get("rank"), _short(z.name), M.fmt_total(sp, z.result), z.result.get("level") or "") for z in top]
    png = top_list(moc.title, f"{moc.date:%d.%m.%Y}", rows, len(seats))
    cap = (f"🏆 <b>{moc.title}</b> — TOP-10\nQatnashdi: {len(seats)} kishi\n\n"
           + "\n".join(f"{'🥇🥈🥉'[r - 1] if r and r <= 3 else f'{r}.'} {n} — {t}" for r, n, t, _ in rows))
    try:
        await bot.send_photo(to, BufferedInputFile(png, "top10.png"), caption=cap[:1024],
                             reply_markup=IM(inline_keyboard=[[IB(text="📢 Kanalga joylash", callback_data=f"ontp:{mid}")]]))
    except Exception:
        pass


def _short(name: str | None) -> str:
    p = (name or "").split()
    return f"{p[0]} {p[1][0]}." if len(p) >= 2 else (name or "")


@router.callback_query(F.data.startswith("ontop:"))
async def top_again(c: CallbackQuery, bot: Bot) -> None:
    await c.answer()
    await top_preview(bot, int(c.data.split(":")[1]), c.from_user.id)


@router.callback_query(F.data.startswith("ontp:"))
async def top_publish(c: CallbackQuery, bot: Bot) -> None:
    async with Session() as s:
        st = await _staff(s, c.from_user.id)
        if _role(st) != "rahbar":
            return await c.answer("Faqat rahbar.", show_alert=True)
        ch = await _channel(s)
    if not ch:
        return await c.answer("Avval kanalni sozlang: online MOC kartasi → «📢 Kanalga e'lon».", show_alert=True)
    try:
        await bot.send_photo(ch, c.message.photo[-1].file_id, caption=c.message.caption, parse_mode=None,
                             caption_entities=c.message.caption_entities)
    except Exception as e:
        return await c.answer(f"Joylab bo'lmadi: {str(e)[:150]}", show_alert=True)
    await c.answer("📢 Kanalga joylandi")
    await c.message.edit_reply_markup(reply_markup=None)


# ======================= tashqi foydalanuvchi uchun «Mening imtihonlarim» =======================
async def guest_home(m: Message) -> bool:
    """Botda boshqa roli yo'q, lekin online MOC qatnashchisi bo'lsa — faqat o'z imtihonlari."""
    async with Session() as s:
        rows = (await s.execute(select(MocSeat, Moc).join(Moc, Moc.id == MocSeat.moc_id)
                                .where(MocSeat.tg_id == m.from_user.id).order_by(Moc.date.desc()).limit(10))).all()
    rows = [(z, moc) for z, moc in rows if is_online(moc)]
    if not rows:
        return False
    kb = [[IB(text=f"{'✅' if moc.status == 'yakun' else '🎓'} {moc.title[:40]}", callback_data=f"onme:{z.id}")]
          for z, moc in rows]
    await m.answer("🎓 <b>Mening imtihonlarim</b>", reply_markup=IM(inline_keyboard=kb))
    return True


@router.callback_query(F.data.startswith("onme:"))
async def guest_one(c: CallbackQuery) -> None:
    async with Session() as s:
        z = await s.get(MocSeat, int(c.data.split(":")[1]))
        if not z or z.tg_id != c.from_user.id:
            return await c.answer()
        moc = await s.get(Moc, z.moc_id)
    await c.answer()
    if moc.status == "yakun" and z.result:
        from app.bot.moc import _deliver
        sp = M.parse_spec(moc.spec)
        async with Session() as s:
            n = sum(1 for x in await _seats(s, moc.id) if x.result and not x.absent)
        await _deliver(c.bot, moc, sp, z, n, [c.from_user.id])
        return
    await status_view(c.message, moc, z)
