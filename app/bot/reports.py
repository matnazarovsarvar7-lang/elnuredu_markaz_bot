"""📈 Hisobotlar (rahbar): kunlik to'lovlar ro'yxati, daromad/xarajat va Excel."""
from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton as IB,
                           InlineKeyboardMarkup as IM, Message)
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models import (Enrollment, Expense, Group, Payment, Payout, Service, ServiceSale, Staff, Student)
from app.db.session import Session
from app import services as S

router = Router()
TZ = ZoneInfo(settings.timezone)
SHIFT = timedelta(hours=5)          # baza UTC, hisobot mahalliy vaqtda
METHOD = {"naqd": "naqd", "karta": "karta", "bank": "bank", "ijtimoiy": "ijtimoiy", "kochirish": "ko'chirish",
          "qarz": "qarz"}
REAL = ("naqd", "karta")            # haqiqiy pul (ijtimoiy shartli to'lov va guruhdan ko'chirish emas)


def today() -> date:
    return datetime.now(TZ).date()


def report_kb() -> IM:
    return IM(inline_keyboard=[
        [IB(text="📋 Bugungi to'lovlar", callback_data="rpd:0"), IB(text="📋 Kecha", callback_data="rpd:1")],
        [IB(text="📥 Excel: bugun", callback_data="rpx:d"), IB(text="📥 Excel: shu oy", callback_data="rpx:m")],
        [IB(text="📥 Excel: o'tgan oy", callback_data="rpx:p"), IB(text="📥 Excel: boshqa davr", callback_data="rpx:x")]])


async def _boss(tg: int) -> bool:
    async with Session() as s:
        who = await S.who_by_tg(s, tg)
    return bool(who.staff and who.staff.current_role == "rahbar")


# ---------------- ma'lumot yig'ish ----------------
async def collect(s, d1: date, d2: date) -> dict:
    """d1..d2 (ikkalasi ham kiradi) oralig'idagi pul harakatlari."""
    staff = {x.id: x.full_name for x in (await s.execute(select(Staff))).scalars()}
    pays = (await s.execute(select(Payment).where(Payment.paid_date >= d1, Payment.paid_date <= d2,
                                                  Payment.status.in_(("tasdiq", "kutilmoqda")))
                            .order_by(Payment.paid_date, Payment.created_at))).scalars().all()
    eids = {p.enrollment_id for p in pays} or {0}
    enrs = {e.id: e for e in (await s.execute(select(Enrollment).where(Enrollment.id.in_(eids)).options(
        selectinload(Enrollment.student), selectinload(Enrollment.group)))).scalars()}
    p_rows = []
    for p in pays:
        e = enrs.get(p.enrollment_id)
        g = e.group if e else None
        note = ("bekor qilish yozuvi" if p.void_of else "❌ bekor qilingan" if p.voided_at else
                "rahbar tasdig'ini kutmoqda" if p.status == "kutilmoqda" else "")
        p_rows.append({"date": p.paid_date, "student": e.student.full_name if e else "?", "group": g.name if g else "?",
                       "teacher": staff.get(g.teacher_id, "") if g else "", "month": p.month, "amount": p.amount,
                       "method": p.method, "status": p.status, "by": staff.get(p.entered_by, "ota-ona" if not p.entered_by else ""),
                       "note": note, "real": p.method in REAL and p.status == "tasdiq"})
    exps = (await s.execute(select(Expense).where(Expense.date >= d1, Expense.date <= d2)
                            .order_by(Expense.date, Expense.created_at))).scalars().all()
    x_rows = [{"date": x.date, "category": x.category, "amount": x.amount, "method": x.method, "source": x.source,
               "status": x.status, "note": (x.note or "") + (" · ❌ bekor qilingan" if x.voided_at else ""),
               "by": staff.get(x.by_id, ""), "real": x.status == "tasdiq"} for x in exps]
    a, b = datetime.combine(d1, datetime.min.time()) - SHIFT, datetime.combine(d2 + timedelta(days=1), datetime.min.time()) - SHIFT
    svc = {v.id: v.name for v in (await s.execute(select(Service))).scalars()}
    sales = (await s.execute(select(ServiceSale).where(ServiceSale.created_at >= a, ServiceSale.created_at < b,
                                                       ServiceSale.status == "tasdiq")
                             .order_by(ServiceSale.created_at))).scalars().all()
    s_rows = [{"date": (x.created_at + SHIFT).date(), "service": svc.get(x.service_id, "?"), "qty": x.qty,
               "amount": x.amount, "profit": x.profit, "method": x.method, "by": staff.get(x.by_id, "")} for x in sales]
    outs = (await s.execute(select(Payout).where(Payout.status.in_(("berildi", "olindi", "tasdiq")),
                                                 Payout.method != "qarz"))).scalars().all()
    o_rows = []
    for x in outs:
        when = x.given_at or x.created_at
        dd = (when + SHIFT).date() if x.given_at is None else when.date()
        if d1 <= dd <= d2:
            o_rows.append({"date": dd, "staff": staff.get(x.staff_id, "?"), "kind": x.kind, "month": x.month,
                           "amount": x.amount, "method": x.method, "status": x.status})
    return {"pays": p_rows, "exps": x_rows, "sales": s_rows, "outs": o_rows}


def totals(data: dict) -> dict:
    t = {"naqd": 0, "karta": 0, "kutilmoqda": 0, "xizmat": 0, "xizmat_naqd": 0, "xarajat": 0, "xarajat_naqd": 0,
         "oylik": 0, "oylik_naqd": 0}
    for p in data["pays"]:
        if p["real"]:
            t[p["method"]] += p["amount"]
        elif p["status"] == "kutilmoqda":
            t["kutilmoqda"] += p["amount"]
    for x in data["sales"]:
        t["xizmat"] += x["amount"]
        t["xizmat_naqd"] += x["amount"] if x["method"] == "naqd" else 0
    for x in data["exps"]:
        if x["real"]:
            t["xarajat"] += x["amount"]
            t["xarajat_naqd"] += x["amount"] if x["method"] == "naqd" else 0
    for x in data["outs"]:
        t["oylik"] += x["amount"]
        t["oylik_naqd"] += x["amount"] if x["method"] == "naqd" else 0
    t["tushum"] = t["naqd"] + t["karta"] + t["xizmat"]
    t["chiqim"] = t["xarajat"] + t["oylik"]
    t["sof"] = t["tushum"] - t["chiqim"]
    return t


# ---------------- 📋 kunlik ro'yxat ----------------
@router.callback_query(F.data.startswith("rpd:"))
async def day_list(c: CallbackQuery) -> None:
    if not await _boss(c.from_user.id):
        await c.answer("Faqat rahbar", show_alert=True)
        return
    d = today() - timedelta(days=int(c.data.split(":")[1]))
    async with Session() as s:
        data = await collect(s, d, d)
    t = totals(data)
    pays = [p for p in data["pays"] if p["method"] in REAL]
    lines = [f"📋 <b>{d:%d.%m.%Y} — to'lovlar ({len([p for p in pays if p['amount'] > 0])} ta)</b>",
             f"💵 Naqd: <b>{S.fmt_som(t['naqd'])}</b> · 💳 Karta: <b>{S.fmt_som(t['karta'])}</b>"
             + (f"\n⏳ Tasdiq kutmoqda: {S.fmt_som(t['kutilmoqda'])}" if t["kutilmoqda"] else ""), ""]
    # o'quvchi bo'yicha jamlash (bir to'lov bir necha guruhga bo'lingan bo'lsa ham bitta qator)
    agg: dict[tuple, dict] = {}
    for p in pays:
        k = (p["student"], p["method"], p["status"], p["note"])
        r = agg.setdefault(k, {**p, "amount": 0, "groups": []})
        r["amount"] += p["amount"]
        if p["group"] not in r["groups"]:
            r["groups"].append(p["group"])
    for i, r in enumerate(agg.values(), 1):
        icon = "💵" if r["method"] == "naqd" else "💳"
        tail = f" · {r['note']}" if r["note"] else ""
        lines.append(f"{i}. {r['student']} — <b>{S.fmt_som(r['amount'])}</b> {icon} · {', '.join(r['groups'])}"
                     f" · {r['by']}{tail}")
    if not agg:
        lines.append("To'lov yo'q.")
    if data["sales"]:
        lines += ["", f"🛒 Xizmatlar: {S.fmt_som(t['xizmat'])} ({len(data['sales'])} ta sotuv)"]
    if data["exps"]:
        lines += ["", f"🧾 <b>Xarajatlar: {S.fmt_som(t['xarajat'])}</b>"]
        lines += [f"• {x['category']} — {S.fmt_som(x['amount'])} ({METHOD.get(x['method'], x['method'])})"
                  + (f" · {x['note'][:40]}" if x["note"] else "") + ("" if x["real"] else f" · {x['status']}")
                  for x in data["exps"]]
    if data["outs"]:
        lines += ["", f"💸 Oylik/avans berildi: {S.fmt_som(t['oylik'])}"]
        lines += [f"• {x['staff']} — {S.fmt_som(x['amount'])} ({x['kind']})" for x in data["outs"]]
    lines += ["", f"<b>Kun yakuni:</b> tushum {S.fmt_som(t['tushum'])} − chiqim {S.fmt_som(t['chiqim'])} = "
                  f"<b>{S.fmt_som(t['sof'])}</b>",
              f"Naqd bo'yicha: +{S.fmt_som(t['naqd'] + t['xizmat_naqd'])} / −{S.fmt_som(t['xarajat_naqd'] + t['oylik_naqd'])}"]
    await c.answer()
    text = "\n".join(lines)
    while text:                                      # Telegram xabari 4096 belgidan oshmasin
        cut = text if len(text) <= 3900 else text[:text.rfind("\n", 0, 3900)]
        await c.message.answer(cut, reply_markup=IM(inline_keyboard=[[IB(text="📥 Shu kun — Excel",
                                                                         callback_data=f"rpe:{d:%Y%m%d}:{d:%Y%m%d}")]])
                               if len(text) == len(cut) else None)
        text = text[len(cut):].lstrip("\n")


# ---------------- 📥 Excel ----------------
class Rp(StatesGroup):
    period = State()


def _month_bounds(d: date) -> tuple[date, date]:
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt - timedelta(days=1)


@router.callback_query(F.data.startswith("rpx:"))
async def excel_pick(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not await _boss(c.from_user.id):
        await c.answer("Faqat rahbar", show_alert=True)
        return
    k = c.data.split(":")[1]
    t = today()
    if k == "x":
        await state.set_state(Rp.period)
        await c.answer()
        await c.message.answer("Davrni yozing: <code>1.11 10.11</code> (boshi va oxiri) yoki bitta kun: <code>5.11</code>")
        return
    d1, d2 = (t, t) if k == "d" else _month_bounds(t) if k == "m" else _month_bounds(t.replace(day=1) - timedelta(days=1))
    if k == "m":
        d2 = t
    await c.answer("Tayyorlanmoqda…")
    await send_excel(bot, c.message.chat.id, d1, d2)


@router.callback_query(F.data.startswith("rpe:"))
async def excel_exact(c: CallbackQuery, bot: Bot) -> None:
    if not await _boss(c.from_user.id):
        await c.answer("Faqat rahbar", show_alert=True)
        return
    _, a, b = c.data.split(":")
    await c.answer("Tayyorlanmoqda…")
    await send_excel(bot, c.message.chat.id, datetime.strptime(a, "%Y%m%d").date(), datetime.strptime(b, "%Y%m%d").date())


def parse_period(text: str, t: date) -> tuple[date, date] | None:
    nums = [int(x) for x in re.findall(r"\d+", text or "")]
    try:
        if len(nums) in (2, 3):
            d1 = date(nums[2] if len(nums) == 3 and nums[2] > 2000 else t.year, nums[1], nums[0])
            d2 = d1
        elif len(nums) >= 4:
            d1 = date(t.year, nums[1], nums[0])
            d2 = date(t.year, nums[3], nums[2])
        else:
            return None
    except ValueError:
        return None
    if d1 > t:
        d1 = d1.replace(year=d1.year - 1)
    if d2 < d1:
        d2 = d2.replace(year=d2.year + 1)
    return (d1, min(d2, t)) if d1 <= t else None


@router.message(Rp.period)
async def excel_period(m: Message, state: FSMContext, bot: Bot) -> None:
    p = parse_period(m.text or "", today())
    if not p or (p[1] - p[0]).days > 400:
        await m.answer("Tushunmadim. Masalan: <code>1.11 10.11</code> yoki <code>5.11</code>")
        return
    await state.clear()
    await m.answer("⏳ Tayyorlanmoqda…")
    await send_excel(bot, m.chat.id, *p)


async def send_excel(bot: Bot, chat_id: int, d1: date, d2: date) -> None:
    async with Session() as s:
        data = await collect(s, d1, d2)
        debts = await _debts(s, d2)
    raw = build_xlsx(data, d1, d2, debts)
    name = f"ELNUR_hisobot_{d1:%d.%m.%Y}" + (f"-{d2:%d.%m.%Y}" if d2 != d1 else "") + ".xlsx"
    t = totals(data)
    await bot.send_document(chat_id, BufferedInputFile(raw, name), caption=(
        f"📥 Hisobot {d1:%d.%m}" + (f"–{d2:%d.%m.%Y}" if d2 != d1 else f".{d1:%Y}") +
        f"\nTushum {S.fmt_som(t['tushum'])} · chiqim {S.fmt_som(t['chiqim'])} · sof {S.fmt_som(t['sof'])}\n"
        "Varaqlar: Xulosa, Kunlar, To'lovlar, Xarajatlar, Xizmatlar, Oylik-avans, Qarzdorlar"))


async def _debts(s, d: date) -> list[dict]:
    """Davr oxiridagi oy bo'yicha qarzdorlar (o'quvchi bo'yicha jamlangan)."""
    agg: dict[int, dict] = {}
    for st in await S.debtors(s, d.year, d.month):
        e = st.enrollment
        r = agg.setdefault(e.student_id, {"student": e.student.full_name, "groups": [], "payable": 0, "paid": 0, "debt": 0})
        r["groups"].append(e.group.name)
        r["payable"] += st.payable
        r["paid"] += st.paid
        r["debt"] += st.debt
    rows = sorted(agg.values(), key=lambda r: -r["debt"])
    for r in rows:
        r["groups"] = ", ".join(r["groups"])
    return rows


def build_xlsx(data: dict, d1: date, d2: date, debts: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="0D2440")
    head_font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="D9DEE5")
    money = '#,##0'

    def sheet(ws, title, headers, rows, money_cols=(), widths=None, total_cols=()):
        ws.title = title
        ws.append(headers)
        for c in ws[1]:
            c.fill, c.font = head_fill, head_font
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for r in rows:
            ws.append(r)
        n = len(rows)
        if total_cols and n:
            ws.append(["JAMI"] + [None] * (len(headers) - 1))
            for col in total_cols:
                L = get_column_letter(col)
                ws.cell(row=n + 2, column=col, value=f"=SUM({L}2:{L}{n + 1})")
            for c in ws[n + 2]:
                c.font = Font(bold=True)
                c.fill = PatternFill("solid", fgColor="EEF2F7")
        for col in money_cols:
            for row in ws.iter_rows(min_row=2, min_col=col, max_col=col):
                for c in row:
                    c.number_format = money
        for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
            for c in row:
                c.border = Border(bottom=thin)
        for i, w in enumerate(widths or [], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        if n:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{n + 1}"

    t = totals(data)
    ws = wb.active
    ws.title = "Xulosa"
    ws.append([f"{settings.center_name} — moliyaviy hisobot"])
    ws["A1"].font = Font(bold=True, size=14, color="0D2440")
    ws.append([f"Davr: {d1:%d.%m.%Y} — {d2:%d.%m.%Y}"])
    ws.append([f"Tayyorlandi: {datetime.now(TZ):%d.%m.%Y %H:%M}"])
    ws.append([])
    items = [("TUSHUM", None), ("O'quvchi to'lovlari — naqd", t["naqd"]), ("O'quvchi to'lovlari — karta", t["karta"]),
             ("Xizmatlar sotuvi", t["xizmat"]), ("Jami tushum", t["tushum"]), ("", None), ("CHIQIM", None),
             ("Xarajatlar", t["xarajat"]), ("Oylik va avans (berilgan)", t["oylik"]), ("Jami chiqim", t["chiqim"]),
             ("", None), ("SOF PUL OQIMI (tushum − chiqim)", t["sof"]), ("", None),
             ("Tasdiq kutayotgan karta to'lovlari (hisobga kirmagan)", t["kutilmoqda"])]
    for k, v in items:
        ws.append([k, v])
        r = ws.max_row
        if v is None and k:
            ws.cell(row=r, column=1).font = Font(bold=True, color="B9770E")
        elif k.startswith(("Jami", "SOF")):
            ws.cell(row=r, column=1).font = Font(bold=True)
            ws.cell(row=r, column=2).font = Font(bold=True)
        ws.cell(row=r, column=2).number_format = money
    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 18
    ws.append([])
    ws.append(["Izoh: to'lovlar to'langan sanasi bo'yicha; ijtimoiy shartli to'lovlar va guruhdan ko'chirishlar tushumga "
               "kirmaydi; bekor qilingan to'lovlar teskari yozuv bilan ayiriladi."])

    days: dict[date, dict] = {}
    cur = d1
    while cur <= d2:
        days[cur] = {"naqd": 0, "karta": 0, "xizmat": 0, "xarajat": 0, "oylik": 0, "soni": 0}
        cur += timedelta(days=1)
    for p in data["pays"]:
        if p["real"] and p["date"] in days:
            days[p["date"]][p["method"]] += p["amount"]
            days[p["date"]]["soni"] += 1 if p["amount"] > 0 else 0
    for x in data["sales"]:
        if x["date"] in days:
            days[x["date"]]["xizmat"] += x["amount"]
    for x in data["exps"]:
        if x["real"] and x["date"] in days:
            days[x["date"]]["xarajat"] += x["amount"]
    for x in data["outs"]:
        if x["date"] in days:
            days[x["date"]]["oylik"] += x["amount"]
    sheet(wb.create_sheet(), "Kunlar", ["Sana", "To'lovlar soni", "Naqd", "Karta", "Xizmatlar", "Xarajatlar",
                                        "Oylik/avans", "Sof"],
          [[dd, v["soni"], v["naqd"], v["karta"], v["xizmat"], v["xarajat"], v["oylik"],
            v["naqd"] + v["karta"] + v["xizmat"] - v["xarajat"] - v["oylik"]] for dd, v in days.items()],
          money_cols=range(3, 9), widths=[12, 10, 14, 14, 14, 14, 14, 14], total_cols=range(2, 9))
    sheet(wb.create_sheet(), "To'lovlar", ["Sana", "O'quvchi", "Guruh", "O'qituvchi", "Qaysi oy uchun", "Summa",
                                           "Usul", "Holat", "Kiritgan", "Izoh"],
          [[p["date"], p["student"], p["group"], p["teacher"], p["month"], p["amount"], METHOD.get(p["method"], p["method"]),
            p["status"], p["by"], p["note"]] for p in data["pays"]],
          money_cols=(6,), widths=[12, 28, 20, 22, 12, 14, 10, 12, 20, 26], total_cols=(6,))
    sheet(wb.create_sheet(), "Xarajatlar", ["Sana", "Toifa", "Summa", "Usul", "Manba", "Holat", "Izoh", "Kiritgan"],
          [[x["date"], x["category"], x["amount"], x["method"], x["source"], x["status"], x["note"], x["by"]]
           for x in data["exps"]],
          money_cols=(3,), widths=[12, 18, 14, 10, 10, 12, 40, 20], total_cols=(3,))
    sheet(wb.create_sheet(), "Xizmatlar", ["Sana", "Xizmat", "Soni", "Summa", "Foyda", "Usul", "Sotuvchi"],
          [[x["date"], x["service"], x["qty"], x["amount"], x["profit"], x["method"], x["by"]] for x in data["sales"]],
          money_cols=(4, 5), widths=[12, 30, 8, 14, 14, 10, 20], total_cols=(3, 4, 5))
    sheet(wb.create_sheet(), "Oylik-avans", ["Sana", "Xodim", "Turi", "Qaysi oy", "Summa", "Usul", "Holat"],
          [[x["date"], x["staff"], x["kind"], x["month"], x["amount"], x["method"], x["status"]] for x in data["outs"]],
          money_cols=(5,), widths=[12, 26, 10, 10, 14, 10, 10], total_cols=(5,))
    sheet(wb.create_sheet(), "Qarzdorlar", ["O'quvchi", "Guruh(lar)", "Hisob", "To'langan", "Qarz"],
          [[x["student"], x["groups"], x["payable"], x["paid"], x["debt"]] for x in debts],
          money_cols=(3, 4, 5), widths=[28, 30, 14, 14, 14], total_cols=(3, 4, 5))
    for w in wb.worksheets[1:]:
        for row in w.iter_rows(min_row=2, max_col=1):
            for c in row:
                if isinstance(c.value, date):
                    c.number_format = "DD.MM.YYYY"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
