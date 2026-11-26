"""🏆 Olimpiada.

Tuzilishi: ota-yozuv (Moc, kind="olimp") — sozlamalar, sovrin pog'onalari, ustozlar; har bir sinf toifasi — alohida
bola-MOC (o'z PDF va kaliti, o'z reytingi). O'tkazish: online (mini-ilova) yoki markazda (qog'oz + skaner).

Qatnashchi: kanal tugmasi (yoki ustoz havolasi) → ism → telefon → maktab → sinf (toifa shundan) → ustozi →
[to'lov] → test. Ustoz: o'z havolasi orqali ro'yxatdan o'tadi; o'quvchilari shu havola bilan kirsa avtomatik bog'lanadi.
Yakunda: har toifada o'rinlar (teng ball — teng o'rin), sovrin pog'onasi, o'quvchiga g'olib sertifikati, ustozga
FAQAT o'quvchisining o'rni uchun mukofot va «G'olib tayyorlagan ustoz» sertifikati, rahbarga sovrinlar ro'yxati
(«🎁 Topshirildi» belgisi bilan) va har toifa uchun TOP-10.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, KeyboardButton,
                           Message, ReplyKeyboardMarkup, ReplyKeyboardRemove)
from sqlalchemy import select

from app import services as S
from app.bot import keyboards as K
from app.bot import online as O
from app.bot.notify import send
from app.db.models import Moc, MocSeat, Parent, Setting, Staff, Student
from app.db.session import Session
from app.logic import moc as M

router = Router()
WHO = {"hamma": "Hamma (markaz + tashqaridan)", "markaz": "Faqat markaz o'quvchilari", "tashqi": "Faqat tashqaridan"}


class Ol(StatesGroup):
    cats = State()
    when = State()
    place = State()
    prizes = State()
    name = State()
    phone = State()
    school = State()
    tname = State()
    t_name = State()
    t_phone = State()
    t_school = State()


def ocfg(p: Moc) -> dict:
    return dict((p.meta or {}).get("olimp") or {})


# ======================= matn tahlili =======================
def parse_cats(text: str) -> list[dict] | None:
    """«5-6; 7-8; 9-11» → [{"name": "5–6-sinflar", "grades": ["5","6"]}, …]."""
    out = []
    for part in re.split(r"[;,\n]+", text):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d{1,2})\s*(?:[-–]\s*(\d{1,2}))?", part)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if not 1 <= a <= b <= 11:
            return None
        out.append({"name": f"{a}–{b}-sinflar" if b > a else f"{a}-sinf", "grades": [str(x) for x in range(a, b + 1)]})
    seen = [g for c in out for g in c["grades"]]
    return out if out and len(seen) == len(set(seen)) else None


def parse_prizes(text: str) -> list[dict] | None:
    """«1-3: Planshet | 500 000 so'm» qatorlari → [{"a":1,"b":3,"s":"Planshet","t":"500 000 so'm"}]."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d{1,4})\s*(?:[-–]\s*(\d{1,4}))?\s*[:.)]\s*(.+)$", line)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        s_, _, t_ = m.group(3).partition("|")
        if a < 1 or b < a or not s_.strip():
            return None
        out.append({"a": a, "b": b, "s": s_.strip()[:80], "t": t_.strip()[:80]})
    out.sort(key=lambda x: x["a"])
    for x, y in zip(out, out[1:]):
        if y["a"] <= x["b"]:
            return None
    return out or None


def tier_of(prizes: list[dict], rank: int | None) -> dict | None:
    if not rank:
        return None
    return next((p for p in prizes if p["a"] <= rank <= p["b"]), None)


def tier_label(p: dict) -> str:
    return f"{p['a']}-o'rin" if p["a"] == p["b"] else f"{p['a']}–{p['b']}-o'rinlar"


# ======================= tashkilotchi: yaratish =======================
async def ask_cats(msg: Message, state: FSMContext) -> None:
    await msg.answer("🎓 Olimpiada <b>sinf toifalariga</b> bo'linadimi? Har toifaga alohida test va alohida reyting.",
                     reply_markup=IM(inline_keyboard=[
                         [IB(text="Yo'q — hamma bitta reytingda", callback_data="olc:0")],
                         [IB(text="Ha — toifalarni yozaman", callback_data="olc:1")]]))


@router.callback_query(F.data.startswith("olc:"))
async def got_cats_mode(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    if not w.get("olimp"):
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    if c.data.endswith("0"):
        return await _cats_ready(c.message, state, [{"name": "Umumiy", "grades": [str(x) for x in range(1, 12)]}])
    await state.set_state(Ol.cats)
    await c.message.answer("Toifalarni yozing: <code>5-6; 7-8; 9-11</code>")


@router.message(Ol.cats, F.text, ~F.text.in_(K.menu_texts()))
async def got_cats(m: Message, state: FSMContext) -> None:
    cats = parse_cats(m.text)
    if not cats:
        return await m.answer("Shunday yozing: <code>5-6; 7-8; 9-11</code> (sinflar takrorlanmasin)")
    await state.set_state(None)
    await _cats_ready(m, state, cats)


async def _cats_ready(msg: Message, state: FSMContext, cats: list[dict]) -> None:
    from app.bot import moc as MB
    w = (await state.get_data())["w"]
    w.update(cats=cats, nv=len(cats), booklets={}, keys={}, cur=0)
    await state.update_data(w=w)
    await MB._ask_pdf(msg, state)


async def ask_mode(msg: Message, state: FSMContext) -> None:
    await msg.answer("🏟 Olimpiada qanday o'tkaziladi?", reply_markup=IM(inline_keyboard=[
        [IB(text="📲 Online (Telegram mini-ilovasi)", callback_data="olm:onl")],
        [IB(text="🏫 Markazda (qog'oz, javob varag'i)", callback_data="olm:mrk")]]))


@router.callback_query(F.data.startswith("olm:"))
async def got_mode(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    if not w.get("olimp"):
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    if c.data.endswith("onl"):
        w["online"] = True
        await state.update_data(w=w)
        return await O.ask_mode(c.message, state)
    w["online"] = False
    await state.update_data(w=w)
    await state.set_state(Ol.when)
    await c.message.answer("📅 Olimpiada qachon? Masalan: <code>30.11 10:00</code>")


@router.message(Ol.when, F.text, ~F.text.in_(K.menu_texts()))
async def got_when(m: Message, state: FSMContext) -> None:
    t = O.parse_dt(m.text)
    if not t or t < O.now() - timedelta(minutes=5):
        return await m.answer("Shunday yozing: <code>30.11 10:00</code>")
    w = (await state.get_data())["w"]
    w["ostart"] = t.isoformat()
    await state.update_data(w=w)
    await state.set_state(Ol.place)
    await m.answer("📍 Manzil (qatnashchilarga shunday yoziladi):\n<i>ELNUR EDU, Urganch sh., … ko'chasi</i>")


@router.message(Ol.place, F.text, ~F.text.in_(K.menu_texts()))
async def got_place(m: Message, state: FSMContext) -> None:
    w = (await state.get_data())["w"]
    w["oplace"] = m.text.strip()[:150]
    w["oschool"] = True
    await state.update_data(w=w)
    await state.set_state(None)
    async with Session() as s:
        st = await O._staff(s, m.from_user.id)
    if O._role(st) in O.PRICERS:
        await state.set_state(O.On.price)
        return await m.answer("💵 Qatnashish narxi (so'm)? Bepul bo'lsa — 0.",
                              reply_markup=IM(inline_keyboard=[[IB(text="Bepul", callback_data="onp:0")]]))
    w["price"] = 0
    await state.update_data(w=w)
    await ask_who(m, state)


async def ask_who(msg: Message, state: FSMContext) -> None:
    await msg.answer("👥 Kimlar qatnashadi?", reply_markup=IM(inline_keyboard=[
        [IB(text=v, callback_data=f"olw:{k}")] for k, v in WHO.items()]))


@router.callback_query(F.data.startswith("olw:"))
async def got_who(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    if not w.get("olimp"):
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    w["owho"] = c.data.split(":")[1]
    await state.update_data(w=w)
    await state.set_state(Ol.prizes)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("🎁 <b>Sovrin pog'onalari</b> — har qatorda: o'rinlar, o'quvchi sovrini | ustoz mukofoti "
                           "(ustozniki ixtiyoriy):\n\n<code>1-3: Planshet | 500 000 so'm\n4-10: Kitob to'plami | "
                           "200 000 so'm\n11-50: Kursga 50% chegirma</code>\n\n"
                           "<i>Ustoz mukofoti faqat o'quvchisi olgan o'rin uchun beriladi. Toifalar bo'lsa — "
                           "pog'onalar har bir toifaga alohida qo'llanadi.</i>")


@router.message(Ol.prizes, F.text, ~F.text.in_(K.menu_texts()))
async def got_prizes(m: Message, state: FSMContext) -> None:
    pr = parse_prizes(m.text)
    if not pr:
        return await m.answer("Shunday yozing (oraliqlar kesishmasin):\n<code>1-3: Planshet | 500 000 so'm\n"
                              "4-10: Kitob to'plami</code>")
    w = (await state.get_data())["w"]
    w["oprizes"] = pr
    await state.update_data(w=w)
    await state.set_state(None)
    if any(p["t"] for p in pr):
        return await m.answer("👩‍🏫 Bir ustozning bir nechta o'quvchisi o'rin olsa, ustoz mukofoti qanday beriladi?",
                              reply_markup=IM(inline_keyboard=[
                                  [IB(text="Har bir g'olib uchun alohida", callback_data="olt:each")],
                                  [IB(text="Eng yuqori o'rin uchun bir marta", callback_data="olt:best")]]))
    w["otmode"] = "each"
    await state.update_data(w=w)
    await _confirm(m, state)


@router.callback_query(F.data.startswith("olt:"))
async def got_tmode(c: CallbackQuery, state: FSMContext) -> None:
    w = (await state.get_data()).get("w") or {}
    if not w.get("oprizes"):
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    w["otmode"] = c.data.split(":")[1]
    await state.update_data(w=w)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    await _confirm(c.message, state)


def _when(w: dict) -> str:
    if w.get("online"):
        fake = Moc(meta={"online": {"start": w["ostart"], "end": w["oend"], "minutes": w["omin"], "mode": w["omode"]}})
        return O.when_text(fake)
    return f"📅 {datetime.fromisoformat(w['ostart']):%d.%m.%Y %H:%M} · 📍 {w.get('oplace', '')}"


async def _confirm(msg: Message, state: FSMContext) -> None:
    w = (await state.get_data())["w"]
    sp = M.parse_spec(w["spec"])
    pr = "\n".join(f"• {tier_label(p)}: {p['s']}" + (f" · ustozga {p['t']}" if p["t"] else "") for p in w["oprizes"])
    await msg.answer(
        f"🏆 <b>{w['title']}</b> — olimpiada\n\n{sp.summary()}\n\n"
        f"Toifalar: {', '.join(c['name'] for c in w['cats'])}\n{'📲 Online' if w.get('online') else '🏫 Markazda'}\n"
        f"{_when(w)}\n👥 {WHO[w['owho']]}\n💵 {S.fmt_som(w['price']) if w.get('price') else 'Bepul'}\n\n"
        f"🎁 <b>Sovrinlar</b>\n{pr}"
        + ("\n👩‍🏫 Ustoz mukofoti: " + ("har g'olib uchun" if w.get("otmode") == "each" else "eng yuqori o'rin uchun bir marta")
           if any(p["t"] for p in w["oprizes"]) else ""),
        reply_markup=IM(inline_keyboard=[[IB(text="✅ Yaratish", callback_data="olok"),
                                          IB(text="❌ Bekor", callback_data="mqno")]]))


@router.callback_query(F.data == "olok")
async def create(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    from app.bot import moc as MB
    w = (await state.get_data()).get("w") or {}
    if not w.get("olimp") or not w.get("oprizes"):
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    await state.clear()
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    start = datetime.fromisoformat(w["ostart"])
    online = {"start": w["ostart"], "end": w["oend"], "minutes": w["omin"], "mode": w["omode"], "school": True} \
        if w.get("online") else None
    async with Session() as s:
        st = await O._staff(s, c.from_user.id)
        p = Moc(kind="olimp", title=w["title"], date=start.date(), spec=w["spec"], n_variants=1, booklets={}, keys={},
                group_ids=[], price=int(w.get("price") or 0), created_by=st.id if st else None,
                meta={"olimp": {"cats": [], "prizes": w["oprizes"], "who": w["owho"], "tmode": w.get("otmode", "each"),
                                "online": bool(online), "start": w["ostart"], "place": w.get("oplace", ""),
                                "teachers": [], "given": []}})
        s.add(p)
        await s.flush()
        cats = []
        for i, cat in enumerate(w["cats"]):
            v = MB.VARS[i]
            ch = Moc(kind="moc", title=f"{w['title']} · {cat['name']}" if len(w["cats"]) > 1 else w["title"],
                     date=start.date(), spec=w["spec"], n_variants=1, booklets={"A": w["booklets"][v]},
                     keys={"A": w["keys"].get(v, "")}, group_ids=[], price=p.price, created_by=p.created_by,
                     meta={"pay_all": True, "olimp_id": p.id, "cat": cat["name"],
                           **({"online": dict(online)} if online else {})})
            s.add(ch)
            await s.flush()
            cats.append({**cat, "mid": ch.id})
        meta = dict(p.meta)
        meta["olimp"] = {**meta["olimp"], "cats": cats}
        p.meta = meta
        await S.audit(s, st, "olimpiada", title=p.title)
        await s.commit()
        pid = p.id
    if O._role(st) not in O.PRICERS:
        from app.bot.moc_ext import ask_price
        await ask_price(bot, pid)
    await card(c.message, pid, c.from_user.id)


# ======================= karta =======================
async def card(msg: Message, pid: int, tg: int, edit: bool = False) -> None:
    from app.bot import moc as MB
    uname = await MB._bot_username(msg.bot)
    async with Session() as s:
        st = await O._staff(s, tg)
        p = await s.get(Moc, pid)
        c = ocfg(p)
        stats = []
        kids = []
        for cat in c["cats"]:
            ch = await s.get(Moc, cat["mid"])
            seats = [z for z in await O._seats(s, ch.id) if z.name]
            kids.append(ch)
            done = sum(1 for z in seats if O.zinfo(z).get("submitted") or z.answers)
            stats.append(f"• {cat['name']}: ro'yxat {len(seats)} · topshirgan {done}"
                         + (" · ✅" if ch.status == "yakun" else ""))
    r = MB._role(st)
    when = (O.when_text(kids[0]) if c.get("online") and kids else
            f"📅 {datetime.fromisoformat(c['start']):%d.%m.%Y %H:%M} · 📍 {c.get('place', '')}")
    pr = "\n".join(f"• {tier_label(x)}: {x['s']}" + (f" · ustozga {x['t']}" if x["t"] else "") for x in c["prizes"])
    lines = [f"🏆 <b>{p.title}</b> — olimpiada" + (" · ✅ yakunlangan" if p.status == "yakun" else ""),
             f"{'📲 Online' if c.get('online') else '🏫 Markazda'} · 👥 {WHO.get(c.get('who'), '')}", when,
             f"💵 {S.fmt_som(p.price) if p.price else 'Bepul'}", "", *stats,
             f"👩‍🏫 Ro'yxatdan o'tgan ustozlar: {len(c.get('teachers') or [])}", "", f"🎁 <b>Sovrinlar</b>\n{pr}"]
    if uname:
        lines += ["", f"🔗 Qatnashchi: https://t.me/{uname}?start=l{pid}",
                  f"👩‍🏫 Ustozlar uchun: https://t.me/{uname}?start=u{pid}"]
    rows = []
    if p.status == "tayyor":
        rows.append([IB(text="📢 Kanalga e'lon", callback_data=f"olch:{pid}")])
    rows += [[IB(text=f"📂 {cat['name']}", callback_data=f"mqc:{cat['mid']}")] for cat in c["cats"]]
    rows.append([IB(text=f"👩‍🏫 Ustozlar ({len(c.get('teachers') or [])})", callback_data=f"olu:{pid}")])
    if p.status == "tayyor" and not c.get("online") and r in MB.MAKERS:
        rows.append([IB(text="🖨 Javob varaqlarini chiqarish", callback_data=f"olpr:{pid}")])
    if p.status == "yakun":
        rows.append([IB(text="🎁 Sovrinlar ro'yxati", callback_data=f"olg:{pid}")])
    if r in O.PRICERS:
        from app.bot.moc_ext import money_rows
        rows += money_rows(p)
    rows.append([IB(text="⬅️ Ro'yxat", callback_data="mq:list")])
    text = "\n".join(lines)
    if edit:
        try:
            return await msg.edit_text(text, reply_markup=IM(inline_keyboard=rows), disable_web_page_preview=True)
        except Exception:
            pass
    await msg.answer(text, reply_markup=IM(inline_keyboard=rows), disable_web_page_preview=True)


async def sync_price(pid: int) -> None:
    """Narx ota-yozuvda belgilansa — barcha toifalarga."""
    async with Session() as s:
        p = await s.get(Moc, pid)
        for cat in ocfg(p).get("cats") or []:
            ch = await s.get(Moc, cat["mid"])
            ch.price = p.price
        await s.commit()


@router.callback_query(F.data.startswith("olch:"))
async def channel(c: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    from app.bot import moc as MB
    pid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await O._staff(s, c.from_user.id)
        if O._role(st) != "rahbar":
            return await c.answer("Kanalga faqat rahbar joylaydi.", show_alert=True)
        ch = await O._channel(s)
        p = await s.get(Moc, pid)
        kid = await s.get(Moc, ocfg(p)["cats"][0]["mid"])
    await c.answer()
    if not ch:
        await state.set_state(O.On.channel)
        return await c.message.answer("📢 Avval kanal manzilini yozing: <code>@kanal_nomi</code> (bot kanalda admin bo'lsin)")
    uname = await MB._bot_username(bot)
    await c.message.answer(f"👀 <b>Kanalga shunday chiqadi</b> ({ch}):")
    await c.message.answer(post_text(p, kid), reply_markup=_post_kb(uname, pid))
    await c.message.answer("Joylaymizmi?", reply_markup=IM(inline_keyboard=[
        [IB(text="📢 Kanalga joylash", callback_data=f"olpb:{pid}")]]))


def _post_kb(uname: str, pid: int) -> IM:
    return IM(inline_keyboard=[[IB(text="📝 Qatnashish", url=f"https://t.me/{uname}?start=l{pid}")],
                               [IB(text="👩‍🏫 Ustozlar uchun ro'yxat", url=f"https://t.me/{uname}?start=u{pid}")]])


def post_text(p: Moc, kid: Moc) -> str:
    c = ocfg(p)
    when = O.when_text(kid) if c.get("online") else \
        f"📅 {datetime.fromisoformat(c['start']):%d.%m.%Y} · ⏰ {datetime.fromisoformat(c['start']):%H:%M}\n📍 {c.get('place', '')}"
    pr = "\n".join(f"{'🥇' if x['a'] == 1 else '🏅'} {tier_label(x)}: <b>{x['s']}</b>" for x in c["prizes"])
    tch = any(x["t"] for x in c["prizes"])
    return (f"🏆 <b>{p.title}</b>\n{'ONLINE ' if c.get('online') else ''}OLIMPIADA\n\n{when}\n"
            + (f"🎓 Toifalar: {', '.join(x['name'] for x in c['cats'])}\n" if len(c["cats"]) > 1 else "")
            + f"💵 Qatnashish: <b>{S.fmt_som(p.price) if p.price else 'BEPUL'}</b>\n\n🎁 <b>SOVRINLAR</b>\n{pr}"
            + ("\n\n👩‍🏫 G'oliblarni tayyorlagan ustozlar ham taqdirlanadi — «Ustozlar uchun» tugmasi orqali "
               "ro'yxatdan o'ting va havolangizni o'quvchilaringizga yuboring." if tch else "")
            + "\n\n👇 Qatnashish uchun tugmani bosing")


@router.callback_query(F.data.startswith("olpb:"))
async def publish(c: CallbackQuery, bot: Bot) -> None:
    from app.bot import moc as MB
    pid = int(c.data.split(":")[1])
    async with Session() as s:
        st = await O._staff(s, c.from_user.id)
        if O._role(st) != "rahbar":
            return await c.answer("Faqat rahbar.", show_alert=True)
        ch = await O._channel(s)
        p = await s.get(Moc, pid)
        kid = await s.get(Moc, ocfg(p)["cats"][0]["mid"])
    try:
        await bot.send_message(ch, post_text(p, kid), reply_markup=_post_kb(await MB._bot_username(bot), pid))
    except Exception as e:
        return await c.answer(f"Joylab bo'lmadi: {str(e)[:150]}", show_alert=True)
    await c.answer("📢 Joylandi")
    await c.message.edit_text(f"✅ E'lon {ch} kanaliga joylandi.")


@router.callback_query(F.data.startswith("olpr:"))
async def print_sheets(c: CallbackQuery, bot: Bot) -> None:
    from app.bot import moc as MB
    pid = int(c.data.split(":")[1])
    async with Session() as s:
        p = await s.get(Moc, pid)
        todo = []
        for cat in ocfg(p)["cats"]:
            ids = [z.id for z in await O._seats(s, cat["mid"]) if z.name and not O.zinfo(z).get("printed")
                   and (not p.price or z.paid >= p.price)]
            for zid in ids:
                z = await s.get(MocSeat, zid)
                z.online = {**O.zinfo(z), "printed": True}
            todo.append((cat, ids))
        await s.commit()
    await c.answer()
    total = sum(len(i) for _, i in todo)
    if not total:
        return await c.message.answer("Yangi (chop etilmagan, to'lovi tasdiqlangan) qatnashchi yo'q.")
    for cat, ids in todo:
        if ids:
            await c.message.answer(f"⏳ {cat['name']}: {len(ids)} ta javob varag'i tayyorlanmoqda…")
            await MB.issue(bot, cat["mid"], ids, c.from_user.id, booklets=True)


# ======================= ustozlar =======================
def _teachers(p: Moc) -> list[dict]:
    return list(ocfg(p).get("teachers") or [])


async def teacher_start(m: Message, state: FSMContext, pid: int) -> bool:
    async with Session() as s:
        p = await s.get(Moc, pid)
    if not p or p.kind != "olimp":
        return False
    me = next((t for t in _teachers(p) if t.get("tg") == m.from_user.id), None)
    if me:
        await _teacher_home(m, p, me)
        return True
    if p.status != "tayyor":
        await m.answer("Olimpiada yakunlangan.")
        return True
    await state.clear()
    await state.update_data(olt={"pid": pid})
    await state.set_state(Ol.t_name)
    await m.answer(f"👩‍🏫 <b>{p.title}</b> — ustozlar ro'yxati\n\nRo'yxatdan o'tsangiz, shaxsiy havola olasiz: "
                   "o'quvchilaringiz shu havola orqali kirsa, avtomatik sizga bog'lanadi va ular o'rin olsa — "
                   "siz ham taqdirlanasiz.\n\nIsm-familiyangizni yozing:", reply_markup=ReplyKeyboardRemove())
    return True


@router.message(Ol.t_name, F.text, ~F.text.in_(K.menu_texts()))
async def t_name(m: Message, state: FSMContext) -> None:
    name = " ".join(m.text.split())[:80]
    if len(name.split()) < 2:
        return await m.answer("Ism va familiyani to'liq yozing.")
    d = (await state.get_data())["olt"]
    d["name"] = name
    await state.update_data(olt=d)
    await state.set_state(Ol.t_phone)
    await m.answer("📱 Raqamingizni tugma orqali yuboring:", reply_markup=_contact_kb())


def _contact_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Raqamni yuborish", request_contact=True)]],
                               resize_keyboard=True, one_time_keyboard=True)


@router.message(Ol.t_phone, F.contact)
async def t_phone(m: Message, state: FSMContext) -> None:
    if m.contact.user_id != m.from_user.id:
        return await m.answer("O'zingizning raqamingizni yuboring.")
    d = (await state.get_data())["olt"]
    d["phone"] = S.norm_phone(m.contact.phone_number) or m.contact.phone_number
    await state.update_data(olt=d)
    await state.set_state(Ol.t_school)
    await m.answer("🏫 Qayerda ishlaysiz? (masalan: <i>12-maktab, Urganch</i>)", reply_markup=ReplyKeyboardRemove())


@router.message(Ol.t_phone)
async def t_phone_text(m: Message) -> None:
    await m.answer("«📱 Raqamni yuborish» tugmasini bosing.")


@router.message(Ol.t_school, F.text, ~F.text.in_(K.menu_texts()))
async def t_school(m: Message, state: FSMContext) -> None:
    d = (await state.get_data())["olt"]
    await state.clear()
    async with Session() as s:
        p = await s.get(Moc, d["pid"])
        ts = _teachers(p)
        if any(t.get("tg") == m.from_user.id or t.get("phone") == d["phone"] for t in ts):
            me = next(t for t in ts if t.get("tg") == m.from_user.id or t.get("phone") == d["phone"])
        else:
            me = {"id": (max([t["id"] for t in ts] or [0]) + 1), "code": secrets.token_hex(3), "name": d["name"],
                  "phone": d["phone"], "tg": m.from_user.id, "school": m.text.strip()[:80]}
            meta = dict(p.meta)
            meta["olimp"] = {**meta["olimp"], "teachers": ts + [me]}
            p.meta = meta
            await s.commit()
    await m.answer(f"✅ {me['name']}, ro'yxatdan o'tdingiz!")
    await _teacher_home(m, p, me)


async def _teacher_home(m: Message, p: Moc, me: dict) -> None:
    from app.bot import moc as MB
    uname = await MB._bot_username(m.bot)
    link = f"https://t.me/{uname}?start=l{p.id}_t{me['code']}"
    await m.answer(f"🏆 <b>{p.title}</b>\n\n🔗 <b>Sizning havolangiz</b> (o'quvchilaringizga yuboring):\n{link}\n\n"
                   "Shu havola orqali kirgan o'quvchilar avtomatik sizga bog'lanadi. Havolasiz kirganlar ham "
                   "ro'yxatdan o'tishda sizni tanlashi mumkin.",
                   reply_markup=IM(inline_keyboard=[[IB(text="👥 O'quvchilarim", callback_data=f"olmy:{p.id}")]]),
                   disable_web_page_preview=True)


@router.callback_query(F.data.startswith("olmy:"))
async def my_students(c: CallbackQuery) -> None:
    pid = int(c.data.split(":")[1])
    async with Session() as s:
        p = await s.get(Moc, pid)
        me = next((t for t in _teachers(p) if t.get("tg") == c.from_user.id), None)
        if not me:
            return await c.answer()
        rows = []
        for cat in ocfg(p)["cats"]:
            for z in await O._seats(s, cat["mid"]):
                if O.zinfo(z).get("teacher_id") == me["id"]:
                    r = z.result or {}
                    rows.append(f"• {z.name} ({cat['name']})" + (f" — {r['rank']}-o'rin" if r.get("rank") else
                                                                  " — ✅ topshirdi" if O.zinfo(z).get("submitted") else ""))
    await c.answer()
    await c.message.answer("👥 <b>O'quvchilaringiz</b>\n" + ("\n".join(rows) if rows else "Hali hech kim yo'q."))


@router.callback_query(F.data.startswith("olu:"))
async def teachers_list(c: CallbackQuery) -> None:
    pid = int(c.data.split(":")[1])
    async with Session() as s:
        p = await s.get(Moc, pid)
        cnt: dict[int, int] = {}
        for cat in ocfg(p)["cats"]:
            for z in await O._seats(s, cat["mid"]):
                tid = O.zinfo(z).get("teacher_id")
                if tid:
                    cnt[tid] = cnt.get(tid, 0) + 1
    ts = _teachers(p)
    await c.answer()
    await c.message.answer("👩‍🏫 <b>Ustozlar</b>\n" + ("\n".join(
        f"• {t['name']} · {t.get('school', '')} · {t.get('phone', '')} — {cnt.get(t['id'], 0)} o'quvchi" for t in ts)
        if ts else "Hali hech kim ro'yxatdan o'tmagan."))


# ======================= qatnashchi: ro'yxatdan o'tish =======================
async def register_start(m: Message, state: FSMContext, arg: str) -> bool:
    mm = re.match(r"^l(\d+)(?:_t([0-9a-f]+))?$", arg)
    if not mm:
        return False
    pid, code = int(mm.group(1)), mm.group(2)
    async with Session() as s:
        p = await s.get(Moc, pid)
        if not p or p.kind != "olimp":
            return False
        mine = None
        for cat in ocfg(p)["cats"]:
            z = (await s.execute(select(MocSeat).where(MocSeat.moc_id == cat["mid"],
                                                       MocSeat.tg_id == m.from_user.id))).scalars().first()
            if z:
                mine = (await s.get(Moc, cat["mid"]), z)
    if mine:
        await seat_status(m, p, *mine)
        return True
    c = ocfg(p)
    closed = p.status != "tayyor"
    if c.get("online"):
        async with Session() as s:
            kid = await s.get(Moc, c["cats"][0]["mid"])
        closed = closed or O.now() >= O.times(kid)[1]
    else:
        closed = closed or O.now() >= datetime.fromisoformat(c["start"])
    if closed:
        await m.answer(f"🏆 <b>{p.title}</b>\nRo'yxatdan o'tish yopilgan.", reply_markup=ReplyKeyboardRemove())
        return True
    teacher = next((t for t in _teachers(p) if t["code"] == code), None) if code else None
    await state.clear()
    await state.update_data(olr={"pid": pid, "teacher_id": teacher["id"] if teacher else None})
    await state.set_state(Ol.name)
    await m.answer(f"🏆 <b>{p.title}</b> — olimpiada\n"
                   + (f"👩‍🏫 Ustozingiz: {teacher['name']}\n" if teacher else "")
                   + "\n📝 <b>Ro'yxatdan o'tish</b>\nIsm va familiyangizni yozing — sertifikatga shunday yoziladi:",
                   reply_markup=ReplyKeyboardRemove())
    return True


@router.message(Ol.name, F.text, ~F.text.in_(K.menu_texts()))
async def r_name(m: Message, state: FSMContext) -> None:
    name = " ".join(m.text.split())[:80]
    if len(name.split()) < 2 or any(ch.isdigit() for ch in name):
        return await m.answer("Ism va familiyani to'liq yozing, masalan: <i>Aliyev Vali</i>")
    d = (await state.get_data())["olr"]
    d["name"] = name
    await state.update_data(olr=d)
    await state.set_state(Ol.phone)
    await m.answer("📱 Telefon raqamingizni tugma orqali yuboring:", reply_markup=_contact_kb())


async def _is_center(s, phone: str, tg: int) -> bool:
    who = await S.who_by_tg(s, tg)
    if who.parent or who.student:
        return True
    if (await s.execute(select(Parent.id).where(Parent.phone == phone))).first():
        return True
    return bool((await s.execute(select(Student.id).where(Student.phone == phone))).first())


@router.message(Ol.phone, F.contact)
async def r_phone(m: Message, state: FSMContext) -> None:
    if m.contact.user_id != m.from_user.id:
        return await m.answer("O'zingizning raqamingizni yuboring.")
    phone = S.norm_phone(m.contact.phone_number) or m.contact.phone_number
    d = (await state.get_data())["olr"]
    async with Session() as s:
        p = await s.get(Moc, d["pid"])
        c = ocfg(p)
        for cat in c["cats"]:
            if (await s.execute(select(MocSeat.id).where(MocSeat.moc_id == cat["mid"], MocSeat.phone == phone))).first():
                await state.clear()
                return await m.answer("❗ Bu raqam bilan allaqachon ro'yxatdan o'tilgan.", reply_markup=ReplyKeyboardRemove())
        center = await _is_center(s, phone, m.from_user.id)
    if c.get("who") == "markaz" and not center:
        await state.clear()
        return await m.answer("Bu olimpiada faqat ELNUR EDU o'quvchilari uchun.", reply_markup=ReplyKeyboardRemove())
    if c.get("who") == "tashqi" and center:
        await state.clear()
        return await m.answer("Bu olimpiada markazdan tashqaridagi o'quvchilar uchun — siz uchun alohida "
                              "nazoratlar o'tkaziladi.", reply_markup=ReplyKeyboardRemove())
    d["phone"] = phone
    await state.update_data(olr=d)
    await state.set_state(Ol.school)
    await m.answer("🏫 Maktabingiz (masalan: <i>12-maktab, Urganch</i>):", reply_markup=ReplyKeyboardRemove())


@router.message(Ol.phone)
async def r_phone_text(m: Message) -> None:
    await m.answer("«📱 Raqamni yuborish» tugmasini bosing.")


@router.message(Ol.school, F.text, ~F.text.in_(K.menu_texts()))
async def r_school(m: Message, state: FSMContext) -> None:
    d = (await state.get_data())["olr"]
    d["school"] = m.text.strip()[:80]
    await state.update_data(olr=d)
    async with Session() as s:
        p = await s.get(Moc, d["pid"])
    grades = [g for cat in ocfg(p)["cats"] for g in cat["grades"]]
    if len(ocfg(p)["cats"]) == 1:
        grades = [str(x) for x in range(5, 12)] + ["Bitirgan"]
    btn = [IB(text=g, callback_data=f"olgr:{g}") for g in grades]
    await m.answer("Nechanchi sinfdasiz?", reply_markup=IM(inline_keyboard=[btn[i:i + 6] for i in range(0, len(btn), 6)]))


@router.callback_query(F.data.startswith("olgr:"))
async def r_grade(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    d = (await state.get_data()).get("olr")
    if not d or "school" not in d:
        return await c.answer("Jarayon eskirgan — havolani qayta bosing.", show_alert=True)
    d["grade"] = c.data.split(":", 1)[1]
    await state.update_data(olr=d)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    if d.get("teacher_id"):
        return await _make(c.message, state, bot, c.from_user.id)
    async with Session() as s:
        p = await s.get(Moc, d["pid"])
    ts = _teachers(p)
    sch = d["school"].lower()
    ts.sort(key=lambda t: (sch.split(",")[0].strip() not in (t.get("school") or "").lower(), t["name"]))
    kb = [[IB(text=f"👩‍🏫 {t['name']}", callback_data=f"olti:{t['id']}")] for t in ts[:25]]
    kb += [[IB(text="✏️ Ustozim ismini yozaman", callback_data="olti:w")], [IB(text="Ustozim yo'q", callback_data="olti:0")]]
    await c.message.answer("👩‍🏫 Sizni kim tayyorlagan? (g'olib bo'lsangiz, ustozingiz ham taqdirlanadi)",
                           reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data.startswith("olti:"))
async def r_teacher(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    d = (await state.get_data()).get("olr")
    if not d or "grade" not in d:
        return await c.answer("Jarayon eskirgan.", show_alert=True)
    v = c.data.split(":")[1]
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    if v == "w":
        await state.set_state(Ol.tname)
        return await c.message.answer("Ustozingizning ism-familiyasini yozing:")
    d["teacher_id"] = int(v) or None
    await state.update_data(olr=d)
    await _make(c.message, state, bot, c.from_user.id)


@router.message(Ol.tname, F.text, ~F.text.in_(K.menu_texts()))
async def r_tname(m: Message, state: FSMContext, bot: Bot) -> None:
    d = (await state.get_data())["olr"]
    d["teacher_name"] = " ".join(m.text.split())[:80]
    await state.update_data(olr=d)
    await _make(m, state, bot, m.from_user.id)


async def _make(msg: Message, state: FSMContext, bot: Bot, tg: int) -> None:
    from app.bot import moc as MB
    d = (await state.get_data())["olr"]
    await state.clear()
    async with Session() as s:
        p = await s.get(Moc, d["pid"])
        cats = ocfg(p)["cats"]
        cat = next((x for x in cats if d["grade"] in x["grades"]), cats[-1] if len(cats) == 1 else None)
        if not cat:
            return await msg.answer("Sizning sinfingiz uchun toifa yo'q.")
        kid = await s.get(Moc, cat["mid"])
        ids = await MB._make_seats(s, kid, [], [(d["name"], d["phone"])], 0)
        z = await s.get(MocSeat, ids[0])
        z.tg_id = tg
        z.online = {"reg": O.now().isoformat(), "school": d["school"], "grade": d["grade"],
                    **({"teacher_id": d["teacher_id"]} if d.get("teacher_id") else {}),
                    **({"teacher_name": d["teacher_name"]} if d.get("teacher_name") else {})}
        await s.commit()
    await msg.answer(f"✅ <b>{d['name']}</b>, olimpiadaga ro'yxatdan o'tdingiz! Toifangiz: {cat['name']}",
                     reply_markup=ReplyKeyboardRemove())
    await seat_status(msg, p, kid, z)


async def seat_status(msg: Message, p: Moc, kid: Moc, z: MocSeat) -> None:
    if O.is_online(kid):
        return await O.status_view(msg, kid, z)
    c = ocfg(p)
    head = f"🏆 <b>{p.title}</b> · {kid.meta.get('cat', '')}\n"
    if kid.status == "yakun":
        return await msg.answer(head + "🏁 Olimpiada yakunlangan — natijangiz yuborilgan.")
    if p.price and z.paid < p.price:
        async with Session() as s:
            card_ = await s.get(Setting, "online_card")
            zz = await s.get(MocSeat, z.id)
            o = O.zinfo(zz)
            if not o.get("receipt"):
                zz.online = {**o, "await_receipt": True}
            await s.commit()
        if o.get("receipt"):
            return await msg.answer(head + "⏳ Chekingiz tekshirilmoqda.")
        return await msg.answer(head + f"\n💳 <b>To'lov: {S.fmt_som(p.price)}</b>\nKarta: <code>"
                                       f"{(card_.value or {}).get('text', '—') if card_ else '—'}</code>\n\n"
                                       "To'lovdan so'ng <b>chek rasmini shu yerga yuboring</b>.")
    st = datetime.fromisoformat(c["start"])
    await msg.answer(head + f"\n✅ Ro'yxatdasiz. Olimpiada <b>{st:%d.%m.%Y} soat {st:%H:%M}</b> da.\n📍 {c.get('place', '')}\n"
                            "Pasport yoki guvohnoma va qora ruchka olib keling. Natija va sertifikat shu yerga keladi.")


# ======================= yakun: sovrinlar va ustoz mukofotlari =======================
async def child_done(bot: Bot, kid_id: int) -> None:
    """Toifa yakunlandi. Hamma toifa tugagach — sovrinlar, sertifikatlar, ustoz mukofotlari, rahbarga ro'yxat."""
    async with Session() as s:
        kid = await s.get(Moc, kid_id)
        p = await s.get(Moc, (kid.meta or {}).get("olimp_id"))
        if not p or p.status == "yakun":
            return
        kids = [await s.get(Moc, c["mid"]) for c in ocfg(p)["cats"]]
        if any(k.status != "yakun" for k in kids):
            return
        p.status = "yakun"
        await s.commit()
    await awards(bot, p.id)


def winners(p: Moc, kids_seats: list[tuple[dict, Moc, list[MocSeat]]]) -> tuple[list, dict]:
    """→ (o'quvchi sovrinlari [(cat, seat, rank, tier)], ustoz mukofotlari {teacher_id: [(seat, rank, tier, cat)]})."""
    c = ocfg(p)
    studs, teach = [], {}
    for cat, kid, seats in kids_seats:
        for z in seats:
            r = (z.result or {}).get("rank")
            t = tier_of(c["prizes"], r)
            if not t or z.absent:
                continue
            studs.append((cat, z, r, t))
            tid = O.zinfo(z).get("teacher_id")
            if tid and t.get("t"):
                teach.setdefault(tid, []).append((z, r, t, cat))
    if c.get("tmode") == "best":
        teach = {k: [min(v, key=lambda x: x[1])] for k, v in teach.items()}
    return studs, teach


async def awards(bot: Bot, pid: int) -> None:
    from app.bot.certs import TONE, _boss_name, deliver, issue, uz_date
    async with Session() as s:
        p = await s.get(Moc, pid)
        c = ocfg(p)
        data = []
        for cat in c["cats"]:
            kid = await s.get(Moc, cat["mid"])
            data.append((cat, kid, [z for z in await O._seats(s, kid.id) if z.result and not z.absent]))
        studs, teach = winners(p, data)
        signers = [(await _boss_name(s), "Rahbar")]
        ids, msgs = [], []
        for cat, z, r, t in studs:
            n = next(len(x[2]) for x in data if x[0]["mid"] == cat["mid"])
            sp = M.parse_spec(p.spec)
            tot = M.fmt_total(sp, z.result)
            cname = f" ({cat['name']})" if len(c["cats"]) > 1 else ""
            cert = await issue(s, "olimp", f"olimp:{pid}", f"z{z.id}", z.name, {
                "date": uz_date(p.date), "signers": signers, "score_big": tot.split(" / ")[0], "score_small": "ball",
                "category": f"Olimpiada · {r}-o'rin" + (f" · {cat['name']}" if len(c["cats"]) > 1 else ""),
                "tone": TONE.get(r, "navy"), "emblem_big": str(r), "emblem_small": "o'rin",
                "body": f"«{p.title}» olimpiadasida{cname} {tot} natija bilan {n} nafar ishtirokchi orasida "
                        f"{r}-o'rinni egalladi.",
                "praise": "G'alabangiz bilan tabriklaymiz! Bilim va mehnat — eng katta sovrin." if r <= 3 else
                          "Kuchli raqobatda munosib natija! Keyingi olimpiadada cho'qqi sizniki bo'lsin."},
                seat_id=z.id, place=r)
            if cert:
                ids.append(cert.id)
            msgs.append((z.tg_id, f"🎁 <b>Tabriklaymiz!</b>\n«{p.title}»{cname}: <b>{r}-o'rin</b>\n"
                                  f"Sovriningiz: <b>{t['s']}</b>\nSovrinni olish vaqti va joyi haqida markaz siz bilan "
                                  "bog'lanadi."))
        t_ids, t_msgs = [], []
        teachers = {t["id"]: t for t in _teachers(p)}
        for tid, wins in teach.items():
            t = teachers.get(tid)
            if not t:
                continue
            lst = ", ".join(f"{_short(z.name)} — {r}-o'rin" for z, r, _, _ in wins)
            cert = await issue(s, "olimp_ustoz", f"olimp:{pid}", f"t{tid}", t["name"], {
                "date": uz_date(p.date), "signers": signers, "score_big": str(len(wins)), "score_small": "g'olib",
                "category": "G'olib tayyorlagan ustoz", "tone": "gold", "emblem_big": "★", "emblem_small": "ustoz",
                "body": f"«{p.title}» olimpiadasida o'quvchilari ({lst}) sovrinli o'rinlarni egallagani — "
                        "ustozning mahorati va fidokorona mehnati uchun.",
                "praise": "Shogirdlar g'alabasi — ustozning eng katta mukofoti. Rahmat sizga!", "tg": t.get("tg")})
            if cert:
                t_ids.append(cert.id)
            rew = "\n".join(f"• {_short(z.name)} — {r}-o'rin → <b>{tt['t']}</b>" for z, r, tt, _ in wins)
            t_msgs.append((t.get("tg"), f"🏆 <b>Tabriklaymiz, {t['name']}!</b>\n«{p.title}» olimpiadasida "
                                        f"o'quvchilaringiz sovrinli o'rinlarni egalladi:\n{rew}\n\nMukofotni olish "
                                        "haqida markaz siz bilan bog'lanadi."))
        await s.commit()
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    for tg, txt in msgs + t_msgs:
        if tg:
            await send(bot, tg, txt)
    await deliver(bot, ids + t_ids, f"{p.title} — olimpiada", bosses)
    for b in bosses:
        await prize_list(bot, b, pid)
    for cat, kid, _ in data:
        for b in bosses:
            await O.top_preview(bot, kid.id, b)


def _short(name: str | None) -> str:
    q = (name or "").split()
    return f"{q[0]} {q[1][0]}." if len(q) >= 2 else (name or "")


async def prize_list(bot: Bot, to: int, pid: int, msg: Message | None = None) -> None:
    async with Session() as s:
        p = await s.get(Moc, pid)
        c = ocfg(p)
        data = []
        for cat in c["cats"]:
            kid = await s.get(Moc, cat["mid"])
            data.append((cat, kid, [z for z in await O._seats(s, kid.id) if z.result and not z.absent]))
    studs, teach = winners(p, data)
    given = set(c.get("given") or [])
    teachers = {t["id"]: t for t in _teachers(p)}
    lines, kb = [f"🎁 <b>{p.title}</b> — sovrinlar ro'yxati", ""], []
    for cat, z, r, t in sorted(studs, key=lambda x: (x[0]["name"], x[2])):
        key = f"s{z.id}"
        mark = "✅" if key in given else "⬜"
        lines.append(f"{mark} {r}-o'rin · {z.name} ({cat['name']}) · {z.phone or ''} → {t['s']}")
        if key not in given:
            kb.append(IB(text=f"🎁 {_short(z.name)}", callback_data=f"olgv:{pid}:{key}"))
    if teach:
        lines += ["", "👩‍🏫 <b>Ustozlar</b>"]
        for tid, wins in teach.items():
            t = teachers.get(tid, {})
            for z, r, tt, _ in wins:
                key = f"t{tid}:{z.id}"
                mark = "✅" if key in given else "⬜"
                lines.append(f"{mark} {t.get('name', '?')} · {t.get('phone', '')} → {tt['t']} ({_short(z.name)}, {r}-o'rin)")
                if key not in given:
                    kb.append(IB(text=f"👩‍🏫 {t.get('name', '?').split()[0]} · {_short(z.name)}",
                                 callback_data=f"olgv:{pid}:{key}"))
    typed = [(z, O.zinfo(z).get("teacher_name")) for _, z, _, _ in studs if O.zinfo(z).get("teacher_name")]
    if typed:
        lines += ["", "✏️ <i>O'quvchi o'zi yozgan ustozlar (ro'yxatdan o'tmagan — tekshiring):</i>"]
        lines += [f"• {_short(z.name)} → {n}" for z, n in typed]
    lines += ["", "Topshirilganda tugmani bosing — ✅ bo'ladi."]
    rows = [kb[i:i + 2] for i in range(0, min(len(kb), 40), 2)]
    text = "\n".join(lines)[:4000]
    if msg:
        try:
            return await msg.edit_text(text, reply_markup=IM(inline_keyboard=rows) if rows else None)
        except Exception:
            pass
    await send(bot, to, text, reply_markup=IM(inline_keyboard=rows) if rows else None)


@router.callback_query(F.data.startswith("olg:"))
async def prizes_cb(c: CallbackQuery, bot: Bot) -> None:
    await c.answer()
    await prize_list(bot, c.from_user.id, int(c.data.split(":")[1]))


@router.callback_query(F.data.startswith("olgv:"))
async def prize_given(c: CallbackQuery, bot: Bot) -> None:
    _, pid, key = c.data.split(":", 2)
    async with Session() as s:
        st = await O._staff(s, c.from_user.id)
        if O._role(st) not in ("rahbar", "moliya", "admin"):
            return await c.answer("Ruxsat yo'q.", show_alert=True)
        p = await s.get(Moc, int(pid))
        meta = dict(p.meta)
        meta["olimp"] = {**meta["olimp"], "given": list(set(meta["olimp"].get("given") or []) | {key})}
        p.meta = meta
        await S.audit(s, st, "olimp_sovrin", id=p.id, key=key)
        await s.commit()
    await c.answer("🎁 Topshirildi")
    await prize_list(bot, c.from_user.id, int(pid), c.message)
