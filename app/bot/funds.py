"""🏦 Fondlar va oy yakuni taqsimoti.

Har oyning 1-sanasi 09:10 da rahbar kassasidagi pul (oy oxiri — 00:00 holati) to'liq taqsimlanadi va kassa 0 dan
boshlanadi. Navbat:
  1) 💼 Oylik zaxirasi — hali berilmagan oyliklar (oylik shu fonddan beriladi);
  2) 🏆 Yillik bonus fondi — tushumning 1% i (berilgan bonuslardan qolgani) + ushlangan jarimalar;
  3) qolgan sof pul: 🤲 Ehson 5% · 🛡 Moliyaviy yostiq 20% · 🚀 Rivojlanish 25% · 👤 Egasining ulushi 50%.
Yostiq 3 oylik zarur xarajatga yetgach, uning ulushi rivojlanishga o'tadi.
Kassada oyliklarga yetmasa — yostiqdan olinadi va rahbar ogohlantiriladi.
Fonddan pul faqat sabab bilan, rahbar tomonidan olinadi (PIN)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import func, select

from app.bot.notify import send
from app.config import settings
from app.db.models import BonusLedger, KassaMove, Payout, Payroll, Setting, Staff
from app.db.session import Session
from app import payroll as PR
from app import services as S

log = logging.getLogger(__name__)
router = Router()
TZ = ZoneInfo(settings.timezone)
SHIFT = timedelta(hours=5)

FUNDS = {"oylik": "💼 Oylik zaxirasi", "yillik": "🏆 Yillik bonus fondi", "ehson": "🤲 Ehson",
         "yostiq": "🛡 Moliyaviy yostiq", "rivoj": "🚀 Rivojlanish va filial", "egasi": "👤 Egasining ulushi"}
SPLIT = {"ehson": 0.05, "yostiq": 0.20, "rivoj": 0.25, "egasi": 0.50}
CUSHION_MONTHS = 3
MONTHS_UZ = ["", "Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun", "Iyul", "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr"]


def now() -> datetime:
    return datetime.now(TZ)


def month_name(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{MONTHS_UZ[m]} {y}"


def _amount(text: str | None) -> int | None:
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    return int(digits) if digits else None


async def balance(s, key: str) -> int:
    return int((await s.execute(select(func.coalesce(func.sum(KassaMove.amount), 0)).where(
        KassaMove.kassa == f"fond_{key}"))).scalar() or 0)


async def balances(s) -> dict[str, int]:
    return {k: await balance(s, k) for k in FUNDS}


async def fund_move(s, src: str | None, dst: str | None, amount: int, note: str, by_id: int | None = None) -> None:
    """Fondlar orasida (yoki fonddan tashqariga: dst=None) pul o'tkazish."""
    if src:
        s.add(KassaMove(kassa=f"fond_{src}", amount=-amount, kind="fond", note=note, by_id=by_id))
    if dst:
        s.add(KassaMove(kassa=f"fond_{dst}", amount=amount, kind="fond", note=note, by_id=by_id))


async def yillik_add(s, month: str) -> int:
    """Oy uchun yillik bonus fondiga: tushumning 1% i − shu oy yozilgan bonuslar (manfiy emas) + ushlangan jarimalar."""
    start, end = PR.period(month)
    pool = round(await PR.income(s, start, end) * PR.BONUS_SHARE)
    bonus = int((await s.execute(select(func.coalesce(func.sum(BonusLedger.amount), 0)).where(
        BonusLedger.month == month, BonusLedger.amount > 0, BonusLedger.role != "yutuq"))).scalar())
    fines = int((await s.execute(select(func.coalesce(func.sum(Payroll.penalty), 0)).where(
        Payroll.month == month))).scalar() or 0)
    return max(0, pool - bonus) + fines


async def unpaid_salaries(s, upto: str) -> tuple[int, list[tuple[str, str, int]]]:
    """Hisoblangan, lekin hali berilmagan oyliklar (rahbardan tashqari)."""
    rows = (await s.execute(select(Payroll, Staff).join(Staff, Staff.id == Payroll.staff_id).where(
        Payroll.month <= upto, Payroll.net > 0))).all()
    out = []
    for pr, st in rows:
        if PR.has_role(st, "rahbar"):
            continue
        paid = (await s.execute(select(Payout.id).where(Payout.staff_id == st.id, Payout.month == pr.month,
                                                        Payout.kind == "oylik"))).first()
        if not paid:
            out.append((st.full_name, pr.month, pr.net))
    return sum(x[2] for x in out), out


async def monthly_need(s, month: str) -> int:
    nets = int((await s.execute(select(func.coalesce(func.sum(Payroll.net), 0)).where(
        Payroll.month == month, Payroll.net > 0))).scalar() or 0)
    row = await s.get(Setting, "fixed_costs")
    fixed = int((row.value or {}).get("v", 2_000_000)) if row else 2_000_000
    return nets + fixed


async def plan(s, month: str, until_utc: datetime | None = None) -> dict | None:
    """Taqsimot rejasi (bazaga yozmaydi). until_utc — kassa qaysi paytdagi holati bo'yicha (None — hozir)."""
    from app.bot.vault import boss_info
    bi = await boss_info(s, until_utc)
    if bi is None:
        return None
    kassa = bi["balance"]
    bal = await balances(s)
    unpaid, who = await unpaid_salaries(s, month)
    need_oylik = max(0, unpaid - bal["oylik"])
    need_yil = await yillik_add(s, month)
    avail, from_cushion = kassa, 0
    if avail < need_oylik:                                 # oyliklarga yetmasa — yostiqdan
        from_cushion = max(0, min(bal["yostiq"], need_oylik - avail))
        avail += from_cushion
    parts = {k: 0 for k in FUNDS}
    parts["oylik"] = max(0, min(avail, need_oylik))
    avail -= parts["oylik"]
    parts["yillik"] = max(0, min(avail, need_yil))
    avail -= parts["yillik"]
    rest = max(0, avail)
    cap = CUSHION_MONTHS * await monthly_need(s, month)
    if rest:
        parts["ehson"] = round(rest * SPLIT["ehson"])
        want = round(rest * SPLIT["yostiq"])
        room = max(0, cap - (bal["yostiq"] - from_cushion))
        parts["yostiq"] = min(want, room)
        parts["rivoj"] = round(rest * SPLIT["rivoj"]) + (want - parts["yostiq"])
        parts["egasi"] = rest - parts["ehson"] - parts["yostiq"] - parts["rivoj"]
    short = max(0, need_oylik - parts["oylik"])
    return {"month": month, "kassa": kassa, "parts": parts, "from_cushion": from_cushion, "unpaid": unpaid,
            "unpaid_list": who, "need_yil": need_yil, "rest": rest, "cap": cap, "short": short,
            "left": kassa + from_cushion - sum(parts.values()), "before": bal}


def plan_text(p: dict, done: bool) -> str:
    pt = p["parts"]
    head = (f"🏦 <b>{month_name(p['month'])} yakuni — taqsimot</b>" if done else
            f"🏦 <b>{month_name(p['month'])} — taqsimot (taxminiy, hozir taqsimlansa)</b>")
    lines = [head, f"Sizda edi: <b>{S.fmt_som(p['kassa'])}</b>", ""]
    if p["from_cushion"]:
        lines.append(f"⚠️ Oyliklarga yetmadi — yostiqdan olindi: {S.fmt_som(p['from_cushion'])}")
    lines.append(f"1️⃣ {FUNDS['oylik']}: <b>{S.fmt_som(pt['oylik'])}</b>"
                 + (f" (berilmagan oyliklar {S.fmt_som(p['unpaid'])})" if p["unpaid"] else ""))
    lines.append(f"2️⃣ {FUNDS['yillik']}: <b>{S.fmt_som(pt['yillik'])}</b>")
    lines.append(f"3️⃣ Sof qoldiq: <b>{S.fmt_som(p['rest'])}</b>")
    for k in ("ehson", "yostiq", "rivoj", "egasi"):
        extra = ""
        if k == "yostiq" and pt["yostiq"] < round(p["rest"] * SPLIT["yostiq"]):
            extra = " (yostiq to'lgan — ortig'i rivojga)"
        lines.append(f"   {FUNDS[k]} ({SPLIT[k] * 100:.0f}%): <b>{S.fmt_som(pt[k])}</b>{extra}")
    if p["short"]:
        lines.append(f"\n🔴 Oyliklar uchun yana <b>{S.fmt_som(p['short'])}</b> yetmaydi!")
    lines.append(f"\n👤 Sizda: <b>{S.fmt_som(p['left'] if done else p['kassa'])}</b>"
                 + (" — yangi oy 0 dan boshlandi." if done and p["left"] == 0 else ""))
    return "\n".join(lines)


async def distribute(month: str, by_id: int | None = None) -> dict | None:
    """Oy yakuni taqsimotini yozadi (bir marta). Kassa oy oxiri (keyingi oy 1-sanasi 00:00) holati bo'yicha."""
    y, m = map(int, PR.next_month(month).split("-"))
    until = datetime(y, m, 1) - SHIFT
    async with Session() as s:
        if await s.get(Setting, f"dist_{month}"):
            return None
        p = await plan(s, month, until)
        if p is None:
            return None
        note = f"{month_name(month)} taqsimoti"
        if p["from_cushion"]:
            await fund_move(s, "yostiq", None, p["from_cushion"], note + ": oyliklarga", by_id)
        for k, v in p["parts"].items():
            if v:
                s.add(KassaMove(kassa=f"fond_{k}", amount=v, kind="taqsimot", note=note, by_id=by_id))
        moved = sum(p["parts"].values()) - p["from_cushion"]
        if moved:
            s.add(KassaMove(kassa="rahbar", amount=-moved, kind="fond", note=note, by_id=by_id))
        s.add(Setting(key=f"dist_{month}", value={"at": f"{now():%Y-%m-%d %H:%M}", "parts": p["parts"],
                                                   "kassa": p["kassa"], "from_cushion": p["from_cushion"]}))
        await s.commit()
    return p


async def distribution_job(bot: Bot) -> None:
    """1-sana 09:10: o'tgan oy yopilgan bo'lsa — taqsimlaydi va rahbarga hisobot yuboradi."""
    month = PR.prev_month(f"{now():%Y-%m}")
    from app.bot.golive import is_test
    async with Session() as s:
        if await is_test(s) or not await s.get(Setting, f"closed_{month}") or await s.get(Setting, f"dist_{month}"):
            return
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    p = await distribute(month)
    if not p:
        return
    async with Session() as s:
        bal = await balances(s)
    text = plan_text(p, True) + "\n\n<b>Fondlar qoldig'i</b>\n" + "\n".join(f"{FUNDS[k]}: {S.fmt_som(v)}" for k, v in bal.items())
    for b in bosses:
        await send(bot, b, text, reply_markup=IM(inline_keyboard=[[IB(text="🏦 Fondlar", callback_data="fd")]]))


async def month_end_reminders(bot: Bot, who: str) -> None:
    """Oyning oxirgi kuni: moliyaga (18:00) — hamma naqdni topshirish; rahbarga (21:30) — topshirishlarni tasdiqlash."""
    from app.db.models import CashHandover
    async with Session() as s:
        if who == "moliya":
            to = [x.tg_id for x in await S.staff_with_role(s, "moliya") if x.tg_id]
            text = ("📅 Bugun oyning oxirgi kuni. Kassani yopgach, <b>qabul kassasidagi barcha naqdni</b> rahbarga "
                    "topshiring («💼 Kassani topshirish») — ertaga ertalab oy yakuni taqsimlanadi.")
        else:
            pend = (await s.execute(select(func.count(CashHandover.id)).where(CashHandover.status == "kutilmoqda"))).scalar()
            to = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
            text = ("📅 Bugun oyning oxirgi kuni. Ertaga 09:10 da sizdagi pul fondlarga taqsimlanadi va kassa 0 dan "
                    "boshlanadi." + (f"\n⚠️ Tasdiqlanmagan topshirishlar: {pend} ta — bugun tasdiqlang." if pend else ""))
    for t in to:
        await send(bot, t, text)


# ---------- 🏦 Fondlar ko'rinishi ----------
async def _is_boss(tg: int) -> Staff | None:
    async with Session() as s:
        who = await S.who_by_tg(s, tg)
    return who.staff if who.staff and who.staff.current_role == "rahbar" else None


async def funds_text(s) -> str:
    bal = await balances(s)
    lines = ["🏦 <b>Fondlar</b>", ""] + [f"{FUNDS[k]}: <b>{S.fmt_som(v)}</b>" for k, v in bal.items()]
    lines.append(f"Jami fondlarda: <b>{S.fmt_som(sum(bal.values()))}</b>")
    rows = (await s.execute(select(Setting).where(Setting.key.like("dist_%")).order_by(Setting.key.desc()).limit(3))).scalars().all()
    if rows:
        lines += ["", "<b>Oxirgi taqsimotlar</b>"]
        for r in rows:
            pt = r.value.get("parts", {})
            lines.append(f"• {month_name(r.key[5:])}: kassada {S.fmt_som(r.value.get('kassa', 0))} → "
                         + ", ".join(f"{FUNDS[k].split(' ', 1)[1]} {S.fmt_som(v)}" for k, v in pt.items() if v))
    cap = CUSHION_MONTHS * await monthly_need(s, PR.prev_month(f"{now():%Y-%m}"))
    lines += ["", f"🛡 Yostiq maqsadi (3 oylik zarur xarajat): {S.fmt_som(cap)}"
              + (" ✅ to'lgan" if bal["yostiq"] >= cap else f" — {bal['yostiq'] * 100 // max(1, cap)}%")]
    return "\n".join(lines)


def funds_kb() -> IM:
    return IM(inline_keyboard=[[IB(text="📊 Shu oy taqsimoti (taxminiy)", callback_data="fdp")],
                               [IB(text="➖ Fonddan pul olish / sarflash", callback_data="fdw")],
                               [IB(text="📋 Fond harakatlari", callback_data="fdl")]])


@router.callback_query(F.data == "fd")
async def funds_view(c: CallbackQuery) -> None:
    if not await _is_boss(c.from_user.id):
        await c.answer("Faqat rahbar.", show_alert=True)
        return
    async with Session() as s:
        text = await funds_text(s)
    await c.answer()
    await c.message.answer(text, reply_markup=funds_kb())


@router.callback_query(F.data == "fdp")
async def funds_preview(c: CallbackQuery) -> None:
    if not await _is_boss(c.from_user.id):
        await c.answer("Faqat rahbar.", show_alert=True)
        return
    async with Session() as s:
        p = await plan(s, PR.current_month(now()))
    await c.answer()
    if not p:
        await c.message.answer("Avval kassani oching: 💼 Kassa → 🧮 Pulni sanab kiritish.")
        return
    await c.message.answer(plan_text(p, False) + "\n\n<i>Oy yopilgach (1-sana 09:10) bot o'zi taqsimlaydi.</i>")


@router.callback_query(F.data == "fdl")
async def funds_log(c: CallbackQuery) -> None:
    if not await _is_boss(c.from_user.id):
        await c.answer("Faqat rahbar.", show_alert=True)
        return
    async with Session() as s:
        rows = (await s.execute(select(KassaMove).where(KassaMove.kassa.like("fond_%"))
                                .order_by(KassaMove.id.desc()).limit(50))).scalars().all()
    lines = ["📋 <b>Fond harakatlari</b> (oxirgi 50)", ""] + [
        f"{x.created_at + SHIFT:%d.%m} · {FUNDS.get(x.kassa[5:], x.kassa)} · <b>{'+' if x.amount > 0 else ''}"
        f"{S.fmt_som(x.amount)}</b>" + (f" · {x.note}" if x.note else "") for x in reversed(rows)]
    if not rows:
        lines.append("—")
    await c.answer()
    await c.message.answer("\n".join(lines)[:3900])


# ---------- ➖ fonddan olish ----------
class Fw(StatesGroup):
    amount = State()
    note = State()


PENDING: dict[int, dict] = {}


@router.callback_query(F.data == "fdw")
async def fw_pick(c: CallbackQuery) -> None:
    if not await _is_boss(c.from_user.id):
        await c.answer("Faqat rahbar.", show_alert=True)
        return
    async with Session() as s:
        bal = await balances(s)
    await c.answer()
    await c.message.answer("Qaysi fonddan?", reply_markup=IM(inline_keyboard=[
        [IB(text=f"{FUNDS[k]} · {S.fmt_som(bal[k])}", callback_data=f"fwf:{k}")]
        for k in ("egasi", "ehson", "rivoj", "yostiq", "yillik")]))


@router.callback_query(F.data.startswith("fwf:"))
async def fw_fund(c: CallbackQuery, state: FSMContext) -> None:
    k = c.data.split(":")[1]
    await state.clear()
    await state.set_state(Fw.amount)
    await state.update_data(fund=k)
    await c.answer()
    await c.message.edit_text(f"{FUNDS[k]} — qancha olasiz? Summani yozing:")


@router.message(Fw.amount)
async def fw_amount(m: Message, state: FSMContext) -> None:
    a = _amount(m.text)
    if not a:
        await m.answer("Summani raqam bilan yozing: 1000000")
        return
    await state.update_data(amount=a)
    await state.set_state(Fw.note)
    d = await state.get_data()
    hint = {"egasi": "Izoh (masalan: o'z ulushimni oldim)", "ehson": "Kimga / nimaga ehson?",
            "rivoj": "Nimaga sarflandi? (masalan: proyektor, ta'mir)", "yostiq": "Nima uchun yostiqdan olindi?",
            "yillik": "Nima uchun? (yillik bonuslar oy yopilganda o'zi beriladi)"}[d["fund"]]
    await m.answer(hint)


@router.message(Fw.note)
async def fw_note(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    d["note"] = (m.text or "").strip()[:250] or "—"
    PENDING[m.from_user.id] = d
    await state.clear()
    await m.answer(f"{FUNDS[d['fund']]}: <b>−{S.fmt_som(d['amount'])}</b>\nIzoh: {d['note']}\n(PIN so'raladi)",
                   reply_markup=IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data="fwok"),
                                                     IB(text="❌ Bekor", callback_data="fwno")]]))


@router.callback_query(F.data == "fwno")
async def fw_cancel(c: CallbackQuery) -> None:
    PENDING.pop(c.from_user.id, None)
    await c.answer("Bekor qilindi")
    await c.message.edit_text("Bekor qilindi.")


@router.callback_query(F.data == "fwok")
async def fw_save(c: CallbackQuery, state: FSMContext) -> None:
    from app.bot.pin import need_pin
    st = await _is_boss(c.from_user.id)
    d = PENDING.get(c.from_user.id)
    if not st or not d:
        await c.answer("Ma'lumot eskirgan, qaytadan boshlang.", show_alert=True)
        return
    if await need_pin(c, state):
        return
    async with Session() as s:
        bal = await balance(s, d["fund"])
        if d["amount"] > bal:
            PENDING.pop(c.from_user.id, None)
            await c.message.edit_text(f"❌ Fondda pul yetarli emas. Qoldiq: {S.fmt_som(bal)}.")
            return
        await fund_move(s, d["fund"], None, d["amount"], d["note"], st.id)
        await S.audit(s, st, "fond_olish", fund=d["fund"], amount=d["amount"], note=d["note"])
        await s.commit()
        left = await balance(s, d["fund"])
    PENDING.pop(c.from_user.id, None)
    await c.answer()
    await c.message.edit_text(f"✅ {FUNDS[d['fund']]}: −{S.fmt_som(d['amount'])} ({d['note']}). Qoldiq: {S.fmt_som(left)}")
