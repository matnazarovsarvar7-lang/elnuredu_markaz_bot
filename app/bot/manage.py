"""Rahbar: sozlamalar (oyliklar, coin miqdorlari, ruxsatlar) va xodimlarga vazifa berish."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import select

from app.bot import keyboards as K
from app.bot.notify import send
from app.config import settings
from app.db.models import AuditLog, RecurringTask, Setting, Staff, Task
from app.db.session import Session
from app.logic.coins import DEFAULT_RULES
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
PAY_TYPES = {"foiz": "Faqat foiz", "fiksal": "Faqat oklad", "ikkalasi": "Oklad + foiz"}
RULE_TITLES = {
    "attend_k": "Darsga keldi", "attend_l": "Kechikdi", "attend_y": "Kelmadi",
    "hw_done": "Vazifa bajardi", "hw_part": "Vazifa qisman", "hw_none": "Vazifa bajarmadi",
    "grade_alo": "Baho «a'lo»", "pay_unit": "To'lov: necha so'mga", "pay_coins": "To'lov: necha coin",
    "pay_bonus": "5-sanagacha to'lov bonusi", "achievement": "Yutuq (sertifikat, OTM)",
    "exam_pct": "Nazorat: necha foizdan yuqori", "exam_coin": "Nazorat: coin",
}


def today() -> date:
    return datetime.now(TZ).date()


class M(StatesGroup):
    tier_price = State()
    pct = State()
    oklad = State()
    rule = State()
    task_text = State()
    task_date = State()
    task_note = State()


async def _boss(tg_id: int) -> Staff | None:
    async with Session() as s:
        who = await S.who_by_tg(s, tg_id)
    return who.staff if who.staff and who.staff.current_role == "rahbar" else None


# =================== SOZLAMALAR ===================
@router.message(F.text == K.B_SETTINGS)
async def settings_menu(m: Message, state: FSMContext) -> None:
    if not await _boss(m.from_user.id):
        return
    await state.clear()
    async with Session() as s:
        row = await s.get(Setting, "admin_sees_debts")
    on = bool(row and row.value.get("on"))
    kb = IM(inline_keyboard=[
        [IB(text="💰 Xodimlar oyligi", callback_data="set:pay")],
        [IB(text="🪙 Coin miqdorlari", callback_data="set:coins")],
        [IB(text="🏷 Narx toifalari", callback_data="set:tiers")],
        [IB(text="🧾 O'zgarishlar tarixi", callback_data="set:audit:0")],
        [IB(text=f"👁 Admin qarzdorlarni ko'radi: {'HA' if on else 'YO`Q'}", callback_data="set:admdebt")],
        [IB(text="💳 Kichik kassa (shu oy)", callback_data="set:petty")],
        [IB(text="📐 Moliya foizi va doimiy xarajatlar", callback_data="set:nums")],
        [IB(text="🔐 PIN-kod (pul amallari)", callback_data="set:pin")],
    ])
    await m.answer("🛠 <b>Sozlamalar</b>\nHar bir o'zgarish kiritilgan paytdan kuchga kiradi va tarixga yoziladi.",
                   reply_markup=kb)


@router.callback_query(F.data == "set:admdebt")
async def toggle_admin_debts(c: CallbackQuery) -> None:
    boss = await _boss(c.from_user.id)
    if not boss:
        return
    async with Session() as s:
        row = await s.get(Setting, "admin_sees_debts") or Setting(key="admin_sees_debts", value={"on": False})
        row.value = {"on": not row.value.get("on")}
        s.add(row)
        await S.audit(s, boss, "sozlama", admin_sees_debts=row.value["on"])
        await s.commit()
    await c.answer(f"Admin qarzdorlarni ko'radi: {'HA' if row.value['on'] else 'YO`Q'}", show_alert=True)


# ---------- xodimlar oyligi ----------
@router.callback_query(F.data == "set:pay")
async def pay_list(c: CallbackQuery) -> None:
    async with Session() as s:
        rows = (await s.execute(select(Staff).where(Staff.active).order_by(Staff.full_name))).scalars().all()
    await c.message.edit_text("Kimning oylik shartini o'zgartirasiz?", reply_markup=IM(
        inline_keyboard=[[IB(text=x.full_name, callback_data=f"sp:{x.id}")] for x in rows]))


def _pay_text(x: Staff) -> str:
    return (f"<b>{x.full_name}</b>\nLavozimlar: {', '.join(S.ROLE_TITLES[r] for r in x.role_list())}\n"
            f"Oylik turi: {PAY_TYPES.get(x.pay_type or '', 'belgilanmagan')}\n"
            f"Foiz: {x.percent or 0}%\nOklad: {S.fmt_som(x.oklad or 0)}")


def _pay_kb(sid: int) -> IM:
    return IM(inline_keyboard=[[IB(text="📊 Foiz", callback_data=f"spp:{sid}"),
                                IB(text="💵 Oklad", callback_data=f"spo:{sid}")],
                               [IB(text="🔁 Oylik turi", callback_data=f"spt:{sid}")]])


@router.callback_query(F.data.startswith("sp:"))
async def pay_card(c: CallbackQuery) -> None:
    sid = int(c.data.split(":")[1])
    async with Session() as s:
        x = await s.get(Staff, sid)
    await c.message.edit_text(_pay_text(x), reply_markup=_pay_kb(sid))


@router.callback_query(F.data.startswith(("spp:", "spo:")))
async def pay_ask(c: CallbackQuery, state: FSMContext) -> None:
    kind, sid = c.data.split(":")
    await state.update_data(sid=int(sid))
    await state.set_state(M.pct if kind == "spp" else M.oklad)
    await c.message.answer("Yangi foizni yozing (0–100):" if kind == "spp" else
                           "Yangi oklad summasini yozing (masalan 700000, yo'q bo'lsa 0):")


async def _save_pay(m: Message, state: FSMContext, field: str, value: int) -> None:
    d = await state.get_data()
    async with Session() as s:
        x = await s.get(Staff, d["sid"])
        old = getattr(x, field)
        setattr(x, field, value)
        if x.percent and x.oklad:
            x.pay_type = "ikkalasi"
        elif x.oklad:
            x.pay_type = "fiksal"
        elif x.percent:
            x.pay_type = "foiz"
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "oylik_shart", staff=x.full_name, field=field, old=old, new=value)
        await s.commit()
    await state.clear()
    await m.answer("Saqlandi ✅ Keyingi hisob-kitoblarda yangi shart ishlatiladi.\n\n" + _pay_text(x),
                   reply_markup=_pay_kb(x.id))


@router.message(M.pct)
async def pay_pct(m: Message, state: FSMContext) -> None:
    digits = "".join(ch for ch in (m.text or "") if ch.isdigit())
    if not digits or int(digits) > 100:
        await m.answer("0 dan 100 gacha raqam yozing.")
        return
    await _save_pay(m, state, "percent", int(digits))


@router.message(M.oklad)
async def pay_oklad(m: Message, state: FSMContext) -> None:
    digits = "".join(ch for ch in (m.text or "") if ch.isdigit())
    if not digits:
        await m.answer("Faqat raqam yozing, masalan 700000.")
        return
    await _save_pay(m, state, "oklad", int(digits))


@router.callback_query(F.data.startswith("spt:"))
async def pay_type_pick(c: CallbackQuery) -> None:
    sid = int(c.data.split(":")[1])
    await c.message.edit_reply_markup(reply_markup=IM(
        inline_keyboard=[[IB(text=v, callback_data=f"spts:{sid}:{k}")] for k, v in PAY_TYPES.items()]))


@router.callback_query(F.data.startswith("spts:"))
async def pay_type_set(c: CallbackQuery) -> None:
    _, sid, t = c.data.split(":")
    async with Session() as s:
        x = await s.get(Staff, int(sid))
        old = x.pay_type
        x.pay_type = t
        if t == "foiz":
            x.oklad = 0
        if t == "fiksal":
            x.percent = 0
        who = await S.who_by_tg(s, c.from_user.id)
        await S.audit(s, who.staff, "oylik_shart", staff=x.full_name, field="pay_type", old=old, new=t)
        await s.commit()
    await c.message.edit_text("Saqlandi ✅\n\n" + _pay_text(x), reply_markup=_pay_kb(x.id))


# ---------- coin miqdorlari ----------
async def _coins_view() -> tuple[str, IM]:
    async with Session() as s:
        rules = await S.get_rules(s)
    lines = ["🪙 <b>Coin miqdorlari</b> (o'zgartirish uchun bosing)"]
    kb = []
    for k, title in RULE_TITLES.items():
        v = rules.get(k, DEFAULT_RULES.get(k, 0))
        shown = S.fmt_som(v) if k == "pay_unit" else f"{v:+d}" if k.startswith(("attend", "hw")) else str(v)
        kb.append([IB(text=f"{title}: {shown}", callback_data=f"cr:{k}")])
    return "\n".join(lines), IM(inline_keyboard=kb)


@router.callback_query(F.data == "set:coins")
async def coins_view(c: CallbackQuery) -> None:
    text, kb = await _coins_view()
    await c.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cr:"))
async def coin_ask(c: CallbackQuery, state: FSMContext) -> None:
    key = c.data.split(":")[1]
    await state.set_state(M.rule)
    await state.update_data(key=key)
    hint = "masalan 20000" if key == "pay_unit" else "manfiy bo'lsa minus bilan, masalan -1" \
        if key.startswith(("attend", "hw")) else "masalan 5; o'chirish uchun 0"
    await c.message.answer(f"«{RULE_TITLES[key]}» uchun yangi qiymat ({hint}):")


@router.message(M.rule)
async def coin_set(m: Message, state: FSMContext) -> None:
    mt = re.fullmatch(r"\s*(-?\d+)\s*", (m.text or "").replace(" ", ""))
    d = await state.get_data()
    if not mt or (d["key"] == "pay_unit" and int(mt.group(1)) <= 0):
        await m.answer("Faqat butun son yozing.")
        return
    async with Session() as s:
        row = await s.get(Setting, "coin_rules") or Setting(key="coin_rules", value={})
        old = {**DEFAULT_RULES, **row.value}.get(d["key"])
        row.value = {**row.value, d["key"]: int(mt.group(1))}
        s.add(row)
        who = await S.who_by_tg(s, m.from_user.id)
        await S.audit(s, who.staff, "coin_qoida", rule=d["key"], old=old, new=int(mt.group(1)))
        await s.commit()
    await state.clear()
    text, kb = await _coins_view()
    await m.answer("Saqlandi ✅ Oldin berilgan coinlar o'zgarmaydi.\n\n" + text, reply_markup=kb)


# ---------- narx toifalari (tarix bilan) ----------
def _next_month(t: date) -> str:
    return f"{t.year + (t.month == 12):04d}-{1 if t.month == 12 else t.month + 1:02d}"


@router.callback_query(F.data == "set:tiers")
async def tiers_view(c: CallbackQuery) -> None:
    async with Session() as s:
        hist = await S.tier_price_history(s)
        now_p = await S.get_tier_prices(s, today().year, today().month)
    lines = ["🏷 <b>Narx toifalari</b> (hozirgi oy)"] + [f"{k}-toifa: {S.fmt_som(v)}" for k, v in sorted(now_p.items())]
    future = [h for h in hist if h["from"] > f"{today():%Y-%m}"]
    for h in future:
        lines.append(f"\n{h['from']} dan: " + ", ".join(f"{k}-toifa {S.fmt_som(v)}" for k, v in sorted(h["prices"].items())))
    lines.append(f"\nO'zgartirilgan narx keyingi oydan ({_next_month(today())}) kuchga kiradi. "
                 "O'tgan oylar va istisno narxlar o'zgarmaydi.")
    kb = IM(inline_keyboard=[[IB(text=f"{k}-toifani o'zgartirish", callback_data=f"tp:{k}")] for k in sorted(now_p)])
    await c.message.edit_text("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("tp:"))
async def tier_ask(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(M.tier_price)
    await state.update_data(tier=int(c.data.split(":")[1]))
    await c.message.answer("Yangi oylik narxni yozing (masalan 320000):")


@router.message(M.tier_price)
async def tier_set(m: Message, state: FSMContext) -> None:
    digits = "".join(ch for ch in (m.text or "") if ch.isdigit())
    if not digits or int(digits) < 10_000:
        await m.answer("Summani to'liq yozing, masalan 320000.")
        return
    d = await state.get_data()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        await S.set_tier_price(s, d["tier"], int(digits), _next_month(today()), who.staff)
        await s.commit()
    await state.clear()
    await m.answer(f"Saqlandi ✅ {d['tier']}-toifa: {S.fmt_som(int(digits))}, {_next_month(today())} oydan.")


# ---------- o'zgarishlar tarixi ----------
ACTIONS = {
    "tolov": "to'lov kiritdi", "karta_tasdiq": "karta to'lovini tasdiqladi", "karta_rad": "karta to'lovini rad etdi",
    "chegirma": "chegirma berdi", "otaona_tel": "ota-ona raqamini qo'shdi", "oquvchi_tel": "o'quvchi raqamini o'zgartirdi",
    "guruh_vaqt": "dars vaqtini o'zgartirdi", "guruh_kunlar": "dars kunlarini o'zgartirdi",
    "xodim_qoshish": "xodim qo'shdi", "xodim_chiqarish": "xodimni chiqardi", "oylik_shart": "oylik shartini o'zgartirdi",
    "coin_qoida": "coin qoidasini o'zgartirdi", "narx_toifa": "narx toifasini o'zgartirdi", "sozlama": "sozlamani o'zgartirdi", "pin": "PIN-kodni o'rnatdi",
    "vazifa_berildi": "vazifa berdi", "vazifa_bajarildi": "vazifani bajardi", "davomat": "davomat oldi",
    "vazifa": "vazifa natijalarini kiritdi", "kelmadi_sabab": "kelmaganlik sababini yozdi", "import": "ma'lumot yukladi",
    "ariza": "ariza holatini belgiladi", "royxat": "yangi o'quvchini ro'yxatga oldi",
    "guruhga_qoshdi": "o'quvchini guruhga qo'shdi",
    "uy_vazifa": "uy vazifasini yubordi", "baho": "baho qo'ydi",
    "nazorat": "nazorat natijalarini kiritdi", "nazorat_kuni": "nazorat kunini o'zgartirdi",
    "apellyatsiya": "apellyatsiyani hal qildi", "test_sorov": "test so'radi", "test_yuklash": "test yukladi",
    "test_tuzatish": "testni tuzatdi", "test_qaytarish": "testni qaytardi", "test_tasdiq": "testni tasdiqladi", "ruxsat": "xodimga ruxsat berdi", "muzlatish": "o'quvchini muzlatdi",
    "muzlatish_tugadi": "o'quvchini muzlatishdan qaytardi", "qayta_hisob": "qayta hisobni hal qildi",
    "ketdi": "o'quvchini guruhdan chiqardi", "qaytarish": "pul qaytarishni belgiladi", "ketish_jarima": "ketish jarimasini yozdi",
    "guruh_toxtadi": "guruhni to'xtatdi", "kassa_yopish": "kassani yopdi", "kassa_qaror": "kassa farqi bo'yicha qaror qildi",
    "inkassatsiya": "kassani topshirdi", "market_buyurtma": "marketdan buyurtma berdi", "xarajat": "xarajat kiritdi", "xarajat_qaror": "xarajatni hal qildi",
    "kichik_kassa": "kichik kassani belgiladi", "xizmat_sotuv": "xizmat sotdi", "xizmat_narx": "xizmat qo'shdi", "printer_ichki": "ichki chop etishni qayd etdi", "qolda_coin": "qo'lda coin berdi",
    "dars_bekor": "darsni bekor qildi", "qoshimcha_dars": "qo'shimcha dars belgiladi", "ombor_sanoq": "omborni sanadi",
    "avans_sorov": "avans so'radi", "yutuq": "yutuq kiritdi", "yutuq_qaror": "yutuqni hal qildi", "sozlama": "sozlamani o'zgartirdi", "pin": "PIN-kodni o'rnatdi", "avans_qaror": "avansni hal qildi", "oylik_berildi": "oylik berdi",
    "market_tasdiq": "buyurtmani tasdiqladi", "market_rad": "buyurtmani rad etdi", "market_sovga": "sovg'ani o'zgartirdi", "inkassatsiya_qaror": "topshirishni tasdiqladi", "yuklash": "yordamchiga ish yukladi", "uy_vazifa_yoq": "«bugun vazifa yo'q» deb belgiladi",
}
HIDE = {"davomat", "vazifa"}  # kundalik ko'p yozuvlar — tarixda ko'rsatilmaydi
PAGE = 20


KEYS = {"staff": "xodim", "field": "maydon", "old": "eski", "new": "yangi", "amount": "summa", "method": "usul",
        "student": "o'quvchi", "group": "guruh", "kind": "tur", "pct": "foiz", "phone": "telefon", "time": "vaqt",
        "days": "kunlar", "name": "ism", "roles": "lavozimlar", "rule": "qoida", "tier": "toifa", "price": "narx",
        "from_month": "oydan", "due": "muddat", "reason": "sabab"}
VALS = {"percent": "foiz", "oklad": "oklad", "pay_type": "oylik turi", **RULE_TITLES}


def _fmt_details(d: dict) -> str:
    keep = {k: v for k, v in d.items() if k not in ("payment_id", "lesson_id", "attendance_id", "task_id")}
    parts = []
    for k, v in keep.items():
        if isinstance(v, int) and v >= 10_000 and k in ("amount", "price", "new", "old"):
            v = S.fmt_som(v)
        parts.append(f"{KEYS.get(k, k)}: {VALS.get(v, v) if isinstance(v, str) else v}")
    return "; ".join(parts)


@router.callback_query(F.data.startswith("set:audit:"))
async def audit_view(c: CallbackQuery) -> None:
    off = int(c.data.split(":")[2])
    async with Session() as s:
        rows = (await s.execute(select(AuditLog, Staff).outerjoin(Staff, Staff.id == AuditLog.staff_id)
                                .where(AuditLog.action.not_in(HIDE)).order_by(AuditLog.id.desc())
                                .offset(off).limit(PAGE + 1))).all()
    more = len(rows) > PAGE
    out = ["🧾 <b>O'zgarishlar tarixi</b>"]
    for a, x in rows[:PAGE]:
        who = x.full_name if x else "bot"
        out.append(f"{a.created_at:%d.%m %H:%M} · <b>{who}</b> {ACTIONS.get(a.action, a.action)}"
                   + (f"\n   {_fmt_details(a.details)}" if a.details else ""))
    if len(out) == 1:
        out.append("Hozircha yozuv yo'q.")
    kb = IM(inline_keyboard=[[IB(text="⬅️ Avvalgilari", callback_data=f"set:audit:{off + PAGE}")]]) if more else None
    await c.message.answer("\n".join(out)[:4000], reply_markup=kb)


# =================== VAZIFALAR ===================
@router.message(F.text == K.B_TASK_NEW)
async def task_new(m: Message, state: FSMContext) -> None:
    if not await _boss(m.from_user.id):
        return
    await state.clear()
    async with Session() as s:
        rows = (await s.execute(select(Staff).where(Staff.active).order_by(Staff.full_name))).scalars().all()
    await m.answer("Vazifa kimga?", reply_markup=IM(
        inline_keyboard=[[IB(text=x.full_name, callback_data=f"tn:{x.id}")] for x in rows]))


@router.callback_query(F.data.startswith("tn:"))
async def task_who(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(sid=int(c.data.split(":")[1]))
    await state.set_state(M.task_text)
    await c.message.edit_text("Vazifani yozing (aniq va qisqa):")


def _file_of(m: Message) -> tuple[str | None, str | None]:
    if m.photo:
        return m.photo[-1].file_id, "photo"
    if m.document:
        return m.document.file_id, "doc"
    return None, None


@router.message(M.task_text)
async def task_text(m: Message, state: FSMContext) -> None:
    text = (m.text or m.caption or "").strip()
    fid, kind = _file_of(m)
    if not text:
        await m.answer("Vazifa matnini yozing (rasm yoki fayl yuborsangiz, izoh qatoriga matn yozing).")
        return
    await state.update_data(text=text, file_id=fid, file_kind=kind)
    await m.answer("Vazifa turi?", reply_markup=IM(inline_keyboard=[
        [IB(text="1️⃣ Bir martalik", callback_data="tk:once")],
        [IB(text="🔁 Har hafta", callback_data="tk:weekly"), IB(text="🔁 Har oy", callback_data="tk:monthly")]]))


WEEKDAYS = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]


@router.callback_query(F.data.startswith("tk:"))
async def task_kind(c: CallbackQuery, state: FSMContext) -> None:
    kind = c.data.split(":")[1]
    if kind == "weekly":
        await c.message.edit_text("Har haftaning qaysi kuni?", reply_markup=IM(inline_keyboard=[
            [IB(text=w, callback_data=f"tr:weekly:{i}")] for i, w in enumerate(WEEKDAYS)]))
        return
    if kind == "monthly":
        await c.message.edit_text("Har oyning nechanchi kuni?", reply_markup=IM(inline_keyboard=[
            [IB(text=str(d), callback_data=f"tr:monthly:{d}") for d in range(r, min(r + 7, 29))] for r in (1, 8, 15, 22)]))
        return
    t = today()
    opts = [("Bugun", t), ("Ertaga", t + timedelta(days=1)), ("3 kunda", t + timedelta(days=3)),
            ("1 haftada", t + timedelta(days=7))]
    kb = IM(inline_keyboard=[[IB(text=f"{n} ({d:%d.%m})", callback_data=f"td:{d.isoformat()}")] for n, d in opts]
            + [[IB(text="📅 Boshqa sana", callback_data="td:other")]])
    await c.message.edit_text("📅 Vazifani qaysi kungacha bajarish kerak?\nKun tugaguncha bajarilmasa, xodimga va sizga eslatma boradi.", reply_markup=kb)


@router.callback_query(F.data.startswith("tr:"))
async def task_recurring(c: CallbackQuery, state: FSMContext) -> None:
    _, kind, day = c.data.split(":")
    d = await state.get_data()
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        x = await s.get(Staff, d["sid"])
        r = RecurringTask(staff_id=x.id, created_by=who.staff.id, text=d["text"], kind=kind, day=int(day), due_days=0)
        s.add(r)
        await S.audit(s, who.staff, "vazifa_berildi", staff=x.full_name, takror=kind, kun=int(day))
        await s.commit()
    await state.clear()
    when = f"har {WEEKDAYS[int(day)].lower()}" if kind == "weekly" else f"har oyning {day}-kuni"
    await c.message.edit_text(f"✅ Takrorlanadigan vazifa: {x.full_name}, {when} soat 8:30 da beriladi.")


@router.callback_query(F.data.startswith("td:"))
async def task_due(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    v = c.data.split(":", 1)[1]
    if v == "other":
        await state.set_state(M.task_date)
        await c.message.edit_text("Sanani kun va oy bilan yozing, masalan: 20 11")
        return
    await _create_task(c.message, state, bot, c.from_user.id, date.fromisoformat(v))


@router.message(M.task_date)
async def task_due_custom(m: Message, state: FSMContext, bot: Bot) -> None:
    nums = [int(x) for x in re.findall(r"\d+", m.text or "")]
    try:
        d = date(today().year, nums[1], nums[0])
        if d < today():
            d = date(today().year + 1, nums[1], nums[0])
    except (IndexError, ValueError):
        await m.answer("Tushunmadim. Masalan: 20 11")
        return
    await _create_task(m, state, bot, m.from_user.id, d)


async def _create_task(msg: Message, state: FSMContext, bot: Bot, tg_id: int, due: date) -> None:
    d = await state.get_data()
    async with Session() as s:
        who = await S.who_by_tg(s, tg_id)
        x = await s.get(Staff, d["sid"])
        t = Task(staff_id=x.id, created_by=who.staff.id, text=d["text"], due=due,
                 file_id=d.get("file_id"), file_kind=d.get("file_kind"))
        s.add(t)
        await S.audit(s, who.staff, "vazifa_berildi", staff=x.full_name, due=due.isoformat())
        await s.commit()
    await state.clear()
    await msg.answer(f"✅ Vazifa berildi: {x.full_name}, muddat {due:%d.%m}.")
    if x.tg_id:
        await deliver_task(bot, x.tg_id, t, f"📝 <b>Yangi vazifa</b> (rahbardan)\n{t.text}\nMuddat: {due:%d.%m.%Y}")
    else:
        await msg.answer("⚠️ Bu xodim hali botga kirmagan — vazifani u kirganda «Vazifalarim»da ko'radi.")


async def deliver_task(bot: Bot, tg_id: int, t: Task, text: str) -> None:
    from app.bot.notify import send_photo
    if t.file_kind == "photo":
        await send_photo(bot, tg_id, t.file_id, text, reply_markup=_done_kb(t.id))
    elif t.file_kind == "doc":
        try:
            await bot.send_document(tg_id, t.file_id, caption=text, reply_markup=_done_kb(t.id))
        except Exception:
            await send(bot, tg_id, text, reply_markup=_done_kb(t.id))
    else:
        await send(bot, tg_id, text, reply_markup=_done_kb(t.id))


def _done_kb(tid: int) -> IM:
    return IM(inline_keyboard=[[IB(text="✅ Bajarildi", callback_data=f"tdone:{tid}")]])


@router.message(F.text == K.B_MY_TASKS)
async def my_tasks(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not who.staff:
            return
        rows = (await s.execute(select(Task).where(Task.staff_id == who.staff.id, Task.status == "ochiq")
                                .order_by(Task.due))).scalars().all()
    if not rows:
        await m.answer("Ochiq vazifa yo'q ✅")
        return
    for t in rows:
        late = " ⚠️ muddati o'tgan" if t.due < today() else ""
        await deliver_task(m.bot, m.chat.id, t, f"📝 {t.text}\nMuddat: {t.due:%d.%m}{late}")


@router.callback_query(F.data.startswith("tdone:"))
async def task_done(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(M.task_note)
    await state.update_data(tid=int(c.data.split(":")[1]))
    await c.message.answer("Qisqa izoh yozing (nima qilindi). Rasm yoki fayl ham yuborishingiz mumkin "
                           "(izoh qatoriga matn yozing). Izohsiz bo'lsa «-» yozing:")


@router.message(M.task_note)
async def task_note(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    note = (m.text or m.caption or "").strip() or "-"
    fid, kind = _file_of(m)
    async with Session() as s:
        t = await s.get(Task, d["tid"])
        t.done_file_id, t.done_file_kind = fid, kind
        if t.status != "ochiq":
            await state.clear()
            await m.answer("Bu vazifa allaqachon yopilgan.")
            return
        t.status, t.done_at, t.done_note = "bajarildi", datetime.now(), None if note == "-" else note
        x = await s.get(Staff, t.staff_id)
        boss = await s.get(Staff, t.created_by) if t.created_by else None
        await S.audit(s, x, "vazifa_bajarildi", task_id=t.id)
        await s.commit()
    await state.clear()
    await m.answer("Rahmat! Vazifa bajarildi deb belgilandi ✅")
    if boss and boss.tg_id:
        late = " (muddatidan kech)" if t.done_at.date() > t.due else ""
        text = f"✅ {x.full_name} vazifani bajardi{late}:\n{t.text}" + (f"\nIzoh: {t.done_note}" if t.done_note else "")
        if fid and kind == "photo":
            from app.bot.notify import send_photo
            await send_photo(bot, boss.tg_id, fid, text)
        elif fid:
            try:
                await bot.send_document(boss.tg_id, fid, caption=text)
            except Exception:
                await send(bot, boss.tg_id, text)
        else:
            await send(bot, boss.tg_id, text)


@router.message(F.text == K.B_TASKS_ALL)
async def tasks_all(m: Message) -> None:
    if not await _boss(m.from_user.id):
        return
    since = today() - timedelta(days=7)
    async with Session() as s:
        rows = (await s.execute(select(Task, Staff).join(Staff, Staff.id == Task.staff_id)
                                .where((Task.status == "ochiq") | (Task.done_at >= since))
                                .order_by(Task.status, Task.due))).all()
    if not rows:
        await m.answer("Vazifalar yo'q.")
        return
    out = ["<b>Vazifalar</b>"]
    for t, x in rows:
        if t.status == "ochiq":
            icon = "🔴" if t.due < today() else "🟡"
            out.append(f"{icon} {x.full_name}: {t.text[:60]} — {t.due:%d.%m}")
        elif t.status == "bajarildi":
            out.append(f"🟢 {x.full_name}: {t.text[:60]} — bajarildi {t.done_at:%d.%m}")
    out.append("\n🔴 muddati o'tgan · 🟡 jarayonda · 🟢 oxirgi 7 kunda bajarilgan")
    await m.answer("\n".join(out))
    async with Session() as s:
        rec = (await s.execute(select(RecurringTask, Staff).join(Staff, Staff.id == RecurringTask.staff_id)
                               .where(RecurringTask.active))).all()
    if rec:
        lines = ["🔁 <b>Takrorlanadigan vazifalar</b>"]
        kb = []
        for r, x in rec:
            when = WEEKDAYS[r.day] if r.kind == "weekly" else f"har oy {r.day}-kuni"
            lines.append(f"• {x.full_name}: {r.text[:50]} — {when}")
            kb.append([IB(text=f"⏹ To'xtatish: {r.text[:30]}", callback_data=f"rstop:{r.id}")])
        await m.answer("\n".join(lines), reply_markup=IM(inline_keyboard=kb))


async def tasks_job(bot: Bot) -> None:
    """Har kuni 9:00: bugun muddati tugaydiganlar — xodimga eslatma; muddati o'tganlar — rahbarga bir marta."""
    t = today()
    async with Session() as s:
        rows = (await s.execute(select(Task).where(Task.status == "ochiq"))).scalars().all()
        msgs = []
        for task in rows:
            x = await s.get(Staff, task.staff_id)
            if task.due == t and x.tg_id:
                msgs.append((x.tg_id, f"⏰ Bugun muddati tugaydi:\n{task.text}", _done_kb(task.id)))
            if task.due < t and not task.overdue_notified:
                boss = await s.get(Staff, task.created_by) if task.created_by else None
                if boss and boss.tg_id:
                    msgs.append((boss.tg_id, f"🔴 Muddati o'tdi ({task.due:%d.%m}): {x.full_name}\n{task.text}", None))
                if x.tg_id:
                    msgs.append((x.tg_id, f"🔴 Vazifa muddati o'tdi:\n{task.text}", _done_kb(task.id)))
                task.overdue_notified = True
        await s.commit()
    for tg, text, kb in msgs:
        await send(bot, tg, text, reply_markup=kb) if kb else await send(bot, tg, text)



@router.callback_query(F.data.startswith("rstop:"))
async def recurring_stop(c: CallbackQuery) -> None:
    if not await _boss(c.from_user.id):
        return
    async with Session() as s:
        r = await s.get(RecurringTask, int(c.data.split(":")[1]))
        r.active = False
        who = await S.who_by_tg(s, c.from_user.id)
        await S.audit(s, who.staff, "sozlama", takroriy_vazifa_toxtatildi=r.text[:50])
        await s.commit()
    await c.answer("To'xtatildi", show_alert=True)


async def recurring_job(bot: Bot) -> None:
    """Har kuni 8:30: bugungi takrorlanadigan vazifalarni yaratadi va yuboradi."""
    t = today()
    async with Session() as s:
        rows = (await s.execute(select(RecurringTask).where(RecurringTask.active))).scalars().all()
        made = []
        for r in rows:
            hit = (r.kind == "weekly" and t.weekday() == r.day) or (r.kind == "monthly" and t.day == r.day)
            if not hit or r.last_created == t:
                continue
            task = Task(staff_id=r.staff_id, created_by=r.created_by, text=r.text,
                        due=t + timedelta(days=r.due_days), recurring_id=r.id)
            s.add(task)
            r.last_created = t
            await s.flush()
            x = await s.get(Staff, r.staff_id)
            if x.tg_id:
                made.append((x.tg_id, task))
        await s.commit()
    for tg, task in made:
        await deliver_task(bot, tg, task, f"🔁 <b>Muntazam vazifa</b>\n{task.text}\nMuddat: {task.due:%d.%m}")


class Nums(StatesGroup):
    val = State()


NUMS = {"moliya_pct": ("Moliya bo'limi foizi (barcha tushumdan)", 2), "fixed_costs": ("Oylik doimiy xarajatlar (ijara, kommunal, internet)", 2_000_000)}


@router.callback_query(F.data == "set:nums")
async def nums_menu(c: CallbackQuery, state: FSMContext) -> None:
    if not await _boss(c.from_user.id):
        return
    async with Session() as s:
        cur = {k: ((await s.get(Setting, k)).value or {}).get("v", d) if await s.get(Setting, k) else d
               for k, (_, d) in NUMS.items()}
    await state.set_state(Nums.val)
    await c.answer()
    await c.message.answer("📐 Hozirgi qiymatlar:\n" + "\n".join(f"• <code>{k}</code> — {t}: <b>{cur[k]}</b>"
                                                                for k, (t, _) in NUMS.items())
                           + "\n\nO'zgartirish uchun yozing: <code>moliya_pct 2</code> yoki <code>fixed_costs 2000000</code>")


@router.message(Nums.val)
async def nums_set(m: Message, state: FSMContext) -> None:
    parts = (m.text or "").split()
    if len(parts) != 2 or parts[0] not in NUMS:
        await m.answer("Masalan: moliya_pct 2")
        return
    try:
        v = float(parts[1].replace(",", "."))
    except ValueError:
        await m.answer("Qiymat raqam bo'lsin.")
        return
    await state.clear()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        row = await s.get(Setting, parts[0])
        if row:
            row.value = {"v": v}
        else:
            s.add(Setting(key=parts[0], value={"v": v}))
        await S.audit(s, who.staff, "sozlama", key=parts[0], value=v)
        await s.commit()
    await m.answer(f"✅ {NUMS[parts[0]][0]}: {v:g}")
