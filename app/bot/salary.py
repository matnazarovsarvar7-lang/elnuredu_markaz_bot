"""💰 Oyligim, 💸 avans, oylik yopilishi (so'nggi kun 14:00), berildi/oldim, bonus, foyda taqsimoti hisoboti."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, Message
from sqlalchemy import func, select
import logging

from app.bot import keyboards as K
from app.bot.notify import send
from app.config import settings
from app.db.models import BonusLedger, Expense, Payout, Payroll, Setting, Staff
from app.db.session import Session
from app import payroll as PR
from app import services as S

log = logging.getLogger(__name__)
router = Router()
TZ = ZoneInfo(settings.timezone)
SPLIT = {"ehson": 0.05, "yostiq": 0.20, "rivoj": 0.25, "egasi": 0.50}
FUND_NAMES = {"ehson": "Ehson", "yostiq": "Moliyaviy yostiq", "rivoj": "Rivojlanish va filial", "egasi": "Egasining daromadi",
              "yillik": "Yillik bonus fondi"}
PROFIT_MIN = 0.30


def now() -> datetime:
    return datetime.now(TZ)


def _amount(text: str | None) -> int | None:
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    return int(digits) if digits else None


async def _fund(s, key: str) -> int:
    row = await s.get(Setting, f"fund_{key}")
    return int((row.value or {}).get("v", 0)) if row else 0


async def _fund_add(s, key: str, delta: int) -> None:
    row = await s.get(Setting, f"fund_{key}")
    if row:
        row.value = {"v": int((row.value or {}).get("v", 0)) + delta}
    else:
        s.add(Setting(key=f"fund_{key}", value={"v": delta}))


# ================= 💰 Oyligim =================
@router.message(F.text == K.B_MY_SALARY)
async def my_salary(m: Message) -> None:
    n = now()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        st = who.staff
        if not st:
            return
        month = PR.current_month(n)
        start, _ = PR.period(month)
        e, lines = await PR.earned(s, st, start, n.replace(tzinfo=None) - PR.UTC_SHIFT,
                                   min(1.0, n.day / 30))
        _, taken, can = await PR.avans_info(s, st, n)
        acc = int((await s.execute(select(func.coalesce(func.sum(BonusLedger.amount), 0)).where(
            BonusLedger.staff_id == st.id, BonusLedger.status == "yigilmoqda"))).scalar())
        prev = (await s.execute(select(Payroll).where(Payroll.staff_id == st.id).order_by(Payroll.month.desc())
                                .limit(12))).scalars().all()
        pts_lines = []
        for r in (st.roles or "").split(","):
            if r and r != "rahbar":
                p, why = await PR.kpi_points(s, st, r, f"{n:%Y-%m}")
                pts_lines.append(f"{S.ROLE_TITLES.get(r, r)}: <b>{p}</b> ball" + (f" ({'; '.join(why)})" if why else ""))
        monthly = await PR._pref_monthly(s, st.id)
    out = [f"💰 <b>Oyligim · {month}</b> (hozirgacha)"]
    out += [f"• {name}: {S.fmt_som(v)}" for name, v in lines] or ["• hozircha hisob yo'q"]
    out += [f"Jami ishlab topilgan: <b>{S.fmt_som(e)}</b>", f"Olingan avans: {S.fmt_som(taken)}",
            f"Avans olish mumkin: {S.fmt_som(can)}" if can else "Avans: hozir mumkin emas (25-sanadan keyin yoki limit)",
            "", "<b>Ballar (shu oy, taxminiy)</b>"] + (pts_lines or ["—"])
    out.append(f"Yig'ilgan bonus/jarima: <b>{S.fmt_som(acc)}</b> — " + ("har oy beriladi" if monthly else "dekabr va iyunda beriladi"))
    if prev:
        out += ["", "<b>Oxirgi oylar</b>"] + [f"{p.month}: {S.fmt_som(p.net)} (hisob {S.fmt_som(p.gross)}"
                                              + (f", bonus {S.fmt_som(p.bonus)}" if p.bonus else "")
                                              + (f", jarima −{S.fmt_som(p.penalty)}" if p.penalty else "") + ")"
                                              for p in prev]
        out.append(f"Yil bo'yicha jami: {S.fmt_som(sum(p.net for p in prev))}")
    kb = [[IB(text="💸 Avans so'rash", callback_data="av:new")]] if can else []
    kb.append([IB(text=("🔁 Bonusni 6 oyda olish" if monthly else "🔁 Bonusni har oy olish"), callback_data="bp:toggle")])
    await m.answer("\n".join(out), reply_markup=IM(inline_keyboard=kb))


@router.callback_query(F.data == "bp:toggle")
async def bonus_pref(c: CallbackQuery) -> None:
    """Bonusni har oy yoki 6 oyda olish — yarim yilda bir marta o'zgartiriladi."""
    t = now().replace(tzinfo=None)
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        row = await s.get(Setting, f"bonus_pref_{who.staff.id}")
        cur = bool(row and (row.value or {}).get("monthly"))
        changed = (row.value or {}).get("changed") if row else None
        if changed and t - datetime.fromisoformat(changed) < timedelta(days=180):
            await c.answer("Tanlovni yarim yilda bir marta o'zgartirish mumkin.", show_alert=True)
            return
        val = {"monthly": not cur, "changed": t.isoformat()}
        if row:
            row.value = val
        else:
            s.add(Setting(key=f"bonus_pref_{who.staff.id}", value=val))
        await s.commit()
    await c.answer("Saqlandi: " + ("bonus har oy beriladi" if not cur else "bonus 6 oyda (dekabr, iyun) beriladi"),
                   show_alert=True)


# ================= 💸 avans =================
class Av(StatesGroup):
    amount = State()


@router.callback_query(F.data == "av:new")
async def av_start(c: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        _, _, can = await PR.avans_info(s, who.staff, now())
    if not can:
        await c.answer("Hozir avans olish mumkin emas.", show_alert=True)
        return
    await state.set_state(Av.amount)
    await state.update_data(can=can)
    await c.answer()
    await c.message.answer(f"💸 Avans: ko'pi bilan {S.fmt_som(can)}. Qancha kerak?")


@router.message(Av.amount)
async def av_amount(m: Message, state: FSMContext, bot: Bot) -> None:
    d = await state.get_data()
    a = _amount(m.text)
    if not a or a > d["can"]:
        await m.answer(f"1 dan {S.fmt_som(d['can'])} gacha summa yozing.")
        return
    await state.clear()
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        p = Payout(staff_id=who.staff.id, month=PR.current_month(now()), kind="avans", amount=a)
        s.add(p)
        await s.flush()
        await S.audit(s, who.staff, "avans_sorov", amount=a)
        await s.commit()
        from app.bot.finance import free_cash, SPEND_SHARE
        free = await free_cash(s)
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        pid, name = p.id, who.staff.full_name
    await m.answer("✅ Avans so'rovi rahbarga yuborildi.")
    lock = ""
    if free is not None:
        allowed = int(max(0, free) * SPEND_SHARE)
        lock = f"\nKassa qulfi: {S.fmt_som(allowed)} gacha" + (" ✅" if a <= allowed else " — ⚠️ oshadi")
    kb = IM(inline_keyboard=[[IB(text="✅ Tasdiqlash", callback_data=f"avy:{pid}"), IB(text="❌ Rad", callback_data=f"avn:{pid}")]])
    for b in bosses:
        await send(bot, b, f"💸 <b>Avans so'rovi</b>\n{name}: {S.fmt_som(a)}{lock}", reply_markup=kb)


@router.callback_query(F.data.startswith(("avy:", "avn:")))
async def av_decide(c: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    from app.bot.pin import need_pin
    if c.data.startswith("avy:") and await need_pin(c, state):
        return
    kind, pid = c.data.split(":")
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            await c.answer("Faqat rahbar.", show_alert=True)
            return
        p = await s.get(Payout, int(pid))
        if p.status != "kutilmoqda":
            await c.answer("Allaqachon hal qilingan.", show_alert=True)
            return
        if kind == "avy":
            p.status, p.approved_by, p.given_at = "berildi", who.staff.id, now().replace(tzinfo=None)
        else:
            p.status, p.approved_by = "rad", who.staff.id
        await S.audit(s, who.staff, "avans_qaror", payout=p.id, status=p.status)
        await s.commit()
        st = await s.get(Staff, p.staff_id)
        amt = p.amount
    await c.message.edit_text(c.message.html_text + ("\n\n✅ Tasdiqlandi va berildi" if kind == "avy" else "\n\n❌ Rad etildi"))
    if st.tg_id:
        if kind == "avy":
            await send(bot, st.tg_id, f"💸 Avans {S.fmt_som(amt)} berildi. Qo'lingizga olgach tasdiqlang:",
                       reply_markup=IM(inline_keyboard=[[IB(text="✅ Oldim", callback_data=f"got:{pid}")]]))
        else:
            await send(bot, st.tg_id, f"💸 Avans so'rovi ({S.fmt_som(amt)}) rad etildi.")


@router.callback_query(F.data.startswith("got:"))
async def got_money(c: CallbackQuery) -> None:
    async with Session() as s:
        p = await s.get(Payout, int(c.data.split(":")[1]))
        p.status, p.received_at = "olindi", now().replace(tzinfo=None)
        await s.commit()
    await c.message.edit_text(c.message.html_text + "\n\n✅ Oldim")


# ================= oyni yopish (so'nggi kun 14:00) =================
async def payroll_job(bot: Bot) -> None:
    """Har soatda tekshiradi: yopilish vaqti o'tgan, lekin hali yopilmagan oy bo'lsa — yopadi
    (bot 14:00 da ishlamay qolgan bo'lsa ham keyinroq yopiladi)."""
    n = now()
    cm = PR.close_moment(n.year, n.month)
    if n.replace(tzinfo=None) < cm:
        return
    await close_and_report(bot, f"{n:%Y-%m}")


async def year_prizes(s, month: str, prs: list[Payroll]) -> list[str]:
    """Iyun yopilganda: yillik fonddan yil g'oliblari (15% / 10%; 8 dan ko'p xodimda 12/8/5%)."""
    y = int(month[:4])
    months = [f"{y - 1}-{m:02d}" for m in range(9, 13)] + [f"{y}-{m:02d}" for m in range(1, 7)]
    rows = (await s.execute(select(BonusLedger).where(BonusLedger.month.in_(months),
                                                      BonusLedger.role != "qoldiq"))).scalars().all()
    score: dict[tuple[int, str], list[int]] = {}
    extra: dict[int, int] = {}
    for r in rows:
        if r.role == "yutuq":
            extra[r.staff_id] = extra.get(r.staff_id, 0) + r.points
            continue
        score.setdefault((r.staff_id, r.role), []).append(r.points)
    for (sid, role), v in score.items():          # yutuq ballari o'qituvchilik natijasiga qo'shiladi
        if role == "oqituvchi" and sid in extra:
            v.append(0)
            v[-1] = extra.pop(sid)
    ranked = sorted(((sum(v) / (PR.MX * len(v)), -sum(1 for x in v if x < 0), k) for k, v in score.items()
                     if len(v) >= 8), reverse=True)
    people = len({k[0] for k in score})
    shares = [0.12, 0.08, 0.05] if people > 8 else [0.15, 0.10]
    fund = await _fund(s, "yillik")
    out, used = [], set()
    for res, _, (sid, role) in ranked:
        if len(out) >= len(shares) or sid in used:
            continue
        amt = int(fund * shares[len(out)])
        used.add(sid)
        st = await s.get(Staff, sid)
        pr = next((p for p in prs if p.staff_id == sid), None)
        if pr:
            pr.lines = pr.lines + [[f"🏆 Yil g'olibi ({len(out) + 1}-o'rin)", amt]]
            pr.net += amt
        out.append(f"{len(out) + 1}-o'rin: {st.full_name} ({S.ROLE_TITLES.get(role, role)}, {res * 100:.0f}%) — {S.fmt_som(amt)}")
        await _fund_add(s, "yillik", -amt)
    return out


async def close_and_report(bot: Bot, month: str) -> None:
    async with Session() as s:
        done = await s.get(Setting, f"closed_{month}")
        if done:                       # oy bir marta yopiladi (fondlar ikki marta yozilmasin)
            log.warning("Oy %s allaqachon yopilgan", month)
            return
        s.add(Setting(key=f"closed_{month}", value={"at": f"{now():%Y-%m-%d %H:%M}"}))
        prs = await PR.close_month(s, month)
        rep = await profit_report(s, month, final=True)
        if month.endswith("-06"):
            winners = await year_prizes(s, month, prs)
            if winners:
                rep += "\n\n🏆 <b>Yil g'oliblari</b>\n" + "\n".join(winners)
        fines = sum(pr.penalty or 0 for pr in prs)
        if fines:                      # oylikdan ushlangan jarimalar yillik fondga
            await _fund_add(s, "yillik", fines)
            rep += f"\n\nUshlangan jarimalar yillik fondga: {S.fmt_som(fines)}"
        await s.commit()
        slips = []
        for pr in prs:
            st = await s.get(Staff, pr.staff_id)
            slips.append((st, pr))
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
    for st, pr in slips:
        if st.tg_id and not PR.has_role(st, "rahbar"):
            await send(bot, st.tg_id, _slip(pr))
    summary = [f"💰 <b>Oyliklar · {month}</b>"]
    kb = []
    for st, pr in slips:
        summary.append(f"• {st.full_name}: <b>{S.fmt_som(pr.net)}</b>")
        if pr.net > 0 and not PR.has_role(st, "rahbar"):
            kb.append([IB(text=f"✅ {st.full_name} — berildi", callback_data=f"pay:{pr.id}")])
    summary.append(f"Jami: {S.fmt_som(sum(pr.net for _, pr in slips))}")
    for b in bosses:
        await send(bot, b, "\n".join(summary), reply_markup=IM(inline_keyboard=kb) if kb else None)
        await send(bot, b, rep)


def _slip(pr: Payroll) -> str:
    lines = [f"💰 <b>Oylik hisob · {pr.month}</b>"] + [f"• {n}: {S.fmt_som(v)}" for n, v in pr.lines]
    lines.append(f"Hisoblandi: {S.fmt_som(pr.gross)}")
    if pr.avans:
        lines.append(f"Avans: −{S.fmt_som(pr.avans)}")
    if pr.bonus:
        lines.append(f"Bonus: +{S.fmt_som(pr.bonus)}")
    if pr.penalty:
        lines.append(f"Jarima: −{S.fmt_som(pr.penalty)}")
    lines.append(f"<b>Qo'lga: {S.fmt_som(pr.net)}</b>")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("pay:"))
async def salary_given(c: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    from app.bot.pin import need_pin
    if await need_pin(c, state):
        return
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            await c.answer("Faqat rahbar.", show_alert=True)
            return
        pr = await s.get(Payroll, int(c.data.split(":")[1]))
        exists = (await s.execute(select(Payout.id).where(Payout.staff_id == pr.staff_id, Payout.month == pr.month,
                                                          Payout.kind == "oylik"))).first()
        if exists:
            await c.answer("Allaqachon berilgan.", show_alert=True)
            return
        p = Payout(staff_id=pr.staff_id, month=pr.month, kind="oylik", amount=pr.net, status="berildi",
                   approved_by=who.staff.id, given_at=now().replace(tzinfo=None))
        s.add(p)
        await s.flush()
        await S.audit(s, who.staff, "oylik_berildi", staff=pr.staff_id, amount=pr.net)
        await s.commit()
        st = await s.get(Staff, pr.staff_id)
        pid, amt = p.id, pr.net
    await c.answer(f"{st.full_name}: berildi")
    if st.tg_id:
        await send(bot, st.tg_id, f"💰 {pr.month} oyligi {S.fmt_som(amt)} berildi. Olgach tasdiqlang:",
                   reply_markup=IM(inline_keyboard=[[IB(text="✅ Oldim", callback_data=f"got:{pid}")]]))


async def negative_notice(bot: Bot) -> None:
    """25-sana — yig'ilgan natija manfiy bo'lsa xodimga eslatma."""
    async with Session() as s:
        rows = (await s.execute(select(BonusLedger.staff_id, func.sum(BonusLedger.amount)).where(
            BonusLedger.status == "yigilmoqda").group_by(BonusLedger.staff_id))).all()
        msgs = []
        for sid, total in rows:
            if total and total < 0:
                st = await s.get(Staff, sid)
                if st and st.tg_id:
                    msgs.append((st.tg_id, f"⚠️ Yig'ilgan ballaringiz manfiy: {S.fmt_som(int(total))}. Dekabr/iyun "
                                           "hisobida (oylikning ko'pi bilan 10% i) ushlanadi. Keyingi oylarda bonus ball "
                                           "yig'ib, uni yopishingiz mumkin."))
    for t, text in msgs:
        await send(bot, t, text)


# ================= 📈 foyda taqsimoti hisoboti =================
async def profit_report(s, month: str, final: bool = False) -> str:
    start, end = PR.period(month)
    y, m = map(int, month.split("-"))
    tushum = await PR.income(s, start, end)
    teachers = owners = okl = mol = rew = 0
    staff = (await s.execute(select(Staff).where(Staff.active))).scalars().all()
    for st in staff:
        sh, _ = await PR.teacher_share(s, st, start, end)
        if PR.has_role(st, "rahbar"):
            # rahbarning o'qituvchilik ulushi xarajat; egasining daromadi — foyda taqsimotidan
            teachers += sh
            continue
        e, lines = await PR.earned(s, st, start, end)
        teachers += sh
        okl += st.oklad or 0
        mol += sum(v for n, v in lines if n.startswith("Moliya"))
        rew += sum(v for n, v in lines if n.startswith("Xizmat"))
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    exp_rows = (await s.execute(select(Expense.category, func.sum(Expense.amount)).where(
        Expense.status == "tasdiq", Expense.date >= first, Expense.date < nxt).group_by(Expense.category))).all()
    expenses = sum(int(v) for _, v in exp_rows)
    bonus = int((await s.execute(select(func.coalesce(func.sum(BonusLedger.amount), 0)).where(
        BonusLedger.month == month, BonusLedger.amount > 0, BonusLedger.role != "yutuq"))).scalar())
    pool = round(tushum * PR.BONUS_SHARE)
    before = tushum - teachers - okl - mol - rew - expenses
    after = before - bonus
    pct = after / tushum * 100 if tushum else 0
    lines = [f"📈 <b>Foyda taqsimoti · {month}</b>" + ("" if final else " (taxminiy, oy davom etmoqda)"),
             f"Tushum: {S.fmt_som(tushum)}",
             f"− O'qituvchilar ulushi: {S.fmt_som(teachers)}", f"− Okladlar: {S.fmt_som(okl)}",
             f"− Moliya bo'limi: {S.fmt_som(mol)}"] + ([f"− Xizmat rag'bati: {S.fmt_som(rew)}"] if rew else []) + \
            [f"− Xarajatlar: {S.fmt_som(expenses)}"] + [f"   · {c}: {S.fmt_som(int(v))}" for c, v in exp_rows] + \
            [f"Bonusdan oldingi foyda: {S.fmt_som(before)}", f"− Xodimlar bonusi (1%): {S.fmt_som(bonus)}",
             f"<b>Sof foyda: {S.fmt_som(after)} ({pct:.1f}%)</b>"]
    if tushum and pct < PROFIT_MIN * 100:
        lines.append(f"⚠️ Foyda {PROFIT_MIN * 100:.0f}% dan past!")
    if after > 0:
        lines.append("")
        for k, share in SPLIT.items():
            v = round(after * share)
            lines.append(f"{FUND_NAMES[k]} ({share * 100:.0f}%): {S.fmt_som(v)}")
            if final:
                await _fund_add(s, k, v)
    elif final:
        lines.append("Zarar oyi: sof foyda taqsimlanmaydi, kamomad moliyaviy yostiqdan.")
        await _fund_add(s, "yostiq", after)
    if final:
        await _fund_add(s, "yillik", max(0, pool - bonus))
    # zararsizlik: 1 o'quvchi o'rtacha qancha qoldiradi
    per = (tushum - teachers) / max(1, (await _active_seats(s)))
    fixed = okl + expenses
    if per > 0:
        lines.append(f"\nZararsizlik nuqtasi: ~{int(fixed / per) + 1} to'lovchi o'rin (hozir {await _active_seats(s)})")
    if final:
        lines += ["", "<b>Fondlar qoldig'i</b>"] + [f"{FUND_NAMES[k]}: {S.fmt_som(await _fund(s, k))}"
                                                     for k in ("ehson", "yostiq", "rivoj", "yillik")]
    return "\n".join(lines)


async def _active_seats(s) -> int:
    from app.db.models import Enrollment
    return int((await s.execute(select(func.count(Enrollment.id)).where(Enrollment.active,
                                                                        Enrollment.discount_kind != "ijtimoiy"))).scalar())


@router.message(F.text == K.B_REPORT)
async def report_view(m: Message) -> None:
    async with Session() as s:
        who = await S.who_by_tg(s, m.from_user.id)
        if not (who.staff and who.staff.current_role == "rahbar"):
            return
        text = await profit_report(s, PR.current_month(now()))
        funds = [f"{FUND_NAMES[k]}: {S.fmt_som(await _fund(s, k))}" for k in ("ehson", "yostiq", "rivoj", "yillik")]
        await s.rollback()
    await m.answer(text + "\n\n<b>Fondlar qoldig'i</b>\n" + "\n".join(funds))
