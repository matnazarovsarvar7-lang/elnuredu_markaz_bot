"""👥 Xodimlar murabbiyi: ertalabki reja, kechki natija, haftalik yakun va rag'bat.

• 08:45 (Du–Sha) — har bir xodimga bugungi reja; dushanba kuni hafta rejasi ham qo'shiladi.
• 20:40 — kunlik natija kartasi (foiz + ko'rsatkichlar) va darajaga mos xabar:
  a'lo → maqtov, yaxshi → rag'bat, o'rta/past → «biz sizga ishonamiz» + bitta aniq maslahat.
• Haftaning oxirgi ish kuni (shanba) — kunlik o'rniga haftalik natija kartasi; eng yaxshi xodim belgilanadi.
• Rahbarga: har kuni qisqa jamoa xulosasi, hafta oxirida jamoa kartasi.

Hech qanday ball/jarima yozmaydi — faqat ko'rsatadi va ruhlantiradi.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.types import BufferedInputFile, CallbackQuery
from aiogram.types import InlineKeyboardButton as IB, InlineKeyboardMarkup as IM
from sqlalchemy import func, select

from app import services as S
from app.bot.notify import send
from app.db.models import (Attendance, CashClose, Exam, Group, Lead, Lesson, Murojaat, Payment, PrintJob,
                           Setting, Staff, Task)
from app.db.session import Session
from app.logic import coach as C
from app.logic.calendar import is_lesson_day

router = Router()
TZ = ZoneInfo("Asia/Tashkent")
UTC5 = timedelta(hours=5)                    # created_at / answered_at bazada UTC
CUT = time(20, 30)                           # 24 soatlik muddatlar oynasi shu vaqtdan hisoblanadi
DEFAULT = {"on": True, "boss_daily": True}


def today() -> date:
    return datetime.now(TZ).date()


async def cfg(s) -> dict:
    row = await s.get(Setting, "coach")
    return {**DEFAULT, **((row.value or {}) if row else {})}


def _loc(x: datetime | None) -> datetime | None:
    return x + UTC5 if x else None


def _win(d: date) -> tuple[datetime, datetime]:
    """«Kecha» oynasi (UTC): [d-2 20:30, d-1 20:30) — muddati bugun tugaydigan murojaat/arizalar."""
    a = datetime.combine(d - timedelta(days=2), CUT) - UTC5
    return a, a + timedelta(days=1)


# ===================================== kun konteksti =====================================
@dataclass
class Ctx:
    d: date
    teach: list = field(default_factory=list)          # [(Group, Lesson|None)]
    hw_prev: set = field(default_factory=set)          # oldingi darsda vazifa berilgan guruhlar
    hw_checked: set = field(default_factory=set)       # bugun uy vazifasi belgilangan dars id
    exams: list = field(default_factory=list)
    mj: list = field(default_factory=list)
    leads: list = field(default_factory=list)
    prints: list = field(default_factory=list)
    absents: list = field(default_factory=list)
    pays: list = field(default_factory=list)
    cash_need: bool = False
    cash_done: bool = False
    tasks: list = field(default_factory=list)
    leave: set = field(default_factory=set)
    gall: dict = field(default_factory=dict)          # barcha faol guruhlar


async def load(s, d: date, groups: list[Group]) -> Ctx:
    from app.db.models import StaffLeave
    c = Ctx(d, gall={g.id: g for g in groups})
    les = {x.group_id: x for x in (await s.execute(select(Lesson).where(Lesson.date == d))).scalars()}
    for g in groups:
        x = les.get(g.id)
        regular = is_lesson_day(d, g.days) and not await S.group_paused(s, g.id, d)
        if (regular or (x and x.extra)) and not (x and x.cancelled):
            c.teach.append((g, x))
            prev = (await s.execute(select(Lesson).where(Lesson.group_id == g.id, Lesson.date < d,
                                                         Lesson.cancelled.is_(False))
                                    .order_by(Lesson.date.desc()).limit(1))).scalar_one_or_none()
            if prev and prev.homework_text:
                c.hw_prev.add(g.id)
    ids = [x.id for _, x in c.teach if x]
    if ids:
        c.hw_checked = {r[0] for r in await s.execute(select(Attendance.lesson_id).where(
            Attendance.lesson_id.in_(ids), Attendance.homework.is_not(None)).distinct())}
        c.absents = list((await s.execute(select(Attendance).where(Attendance.lesson_id.in_(ids),
                                                                   Attendance.mark == "y"))).scalars())
    c.exams = list((await s.execute(select(Exam).where(Exam.date == d))).scalars())
    a, b = _win(d)
    c.mj = list((await s.execute(select(Murojaat).where(Murojaat.created_at >= a, Murojaat.created_at < b))).scalars())
    c.leads = list((await s.execute(select(Lead).where(Lead.created_at >= a, Lead.created_at < b))).scalars())
    d0 = datetime.combine(d, time(0)) - UTC5
    c.prints = list((await s.execute(select(PrintJob).where(PrintJob.created_at >= d0,
                                                            PrintJob.created_at < d0 + timedelta(hours=17)))).scalars())
    c.pays = list((await s.execute(select(Payment).where(Payment.created_at >= d0,
                                                         Payment.created_at < d0 + timedelta(days=1),
                                                         Payment.receipt_file_id.is_not(None),
                                                         Payment.void_of.is_(None)))).scalars())
    cash = (await s.execute(select(func.count(Payment.id)).where(
        Payment.paid_date == d, Payment.method == "naqd", Payment.status == "tasdiq"))).scalar()
    c.cash_need = bool(cash)
    c.cash_done = (await s.execute(select(CashClose.id).where(
        CashClose.date == d, CashClose.status != "boshlang",
        (CashClose.decision.is_(None)) | (CashClose.decision != "qayta")))).first() is not None
    c.tasks = list((await s.execute(select(Task).where(Task.due == d, Task.status != "bekor"))).scalars())
    c.leave = {r[0] for r in await s.execute(select(StaffLeave.staff_id).where(StaffLeave.date == d))}
    return c


def _resp(g: Group, what: str) -> int | None:
    if (g.delegated or {}).get(what) and g.assistant_id:
        return g.assistant_id
    return g.teacher_id


def items_for(st: Staff, c: Ctx) -> list[C.Item]:
    if st.id in c.leave:
        return []
    roles = st.role_list()
    out: list[C.Item] = []
    # --- o'qituvchi / yordamchi
    att = [(g, x) for g, x in c.teach if _resp(g, "attendance") == st.id]
    hw = [(g, x) for g, x in c.teach if _resp(g, "homework") == st.id]
    if att:
        ok = 0
        for g, x in att:
            if x and x.confirmed_at:
                lim = datetime.combine(c.d, g.end_time) + timedelta(hours=1) if g.end_time else \
                    datetime.combine(c.d, time(23, 59))
                ok += x.confirmed_at <= lim
        out.append(C.Item("att", ok, len(att)))
    if hw:
        out.append(C.Item("hw", sum(1 for _, x in hw if x and x.homework_at and x.homework_at.date() == c.d), len(hw)))
        chk = [(g, x) for g, x in hw if g.id in c.hw_prev]
        if chk:
            out.append(C.Item("hwc", sum(1 for _, x in chk if x and x.id in c.hw_checked), len(chk)))
    ex = [e for e in c.exams if _cond(e, c) == st.id and not (e.status == "otkazilmadi" and e.moved_note)]
    if ex:
        out.append(C.Item("exam", sum(1 for e in ex if e.status == "otdi"), len(ex)))
    mt = [m for m in c.mj if m.category == "ustoz" and m.to_staff_id == st.id]
    if mt:
        out.append(C.Item("mjt", sum(1 for m in mt if _ans24(m)), len(mt)))
    # --- admin
    if "admin" in roles:
        if c.absents:
            out.append(C.Item("absent", sum(1 for a in c.absents if a.absence_reason), len(c.absents)))
        if c.leads:
            out.append(C.Item("lead", sum(1 for x in c.leads if x.status != "yangi"), len(c.leads)))
        ma = [m for m in c.mj if m.category != "ustoz"]
        if ma:
            out.append(C.Item("mja", sum(1 for m in ma if _ans24(m)), len(ma)))
        pr = c.prints
        if pr:
            out.append(C.Item("print", sum(1 for p in pr if p.status != "yangi" and _loc(p.done_at)
                                           and _loc(p.done_at).date() == c.d), len(pr)))
    # --- moliya
    if "moliya" in roles:
        if c.pays:
            out.append(C.Item("pay", sum(1 for p in c.pays if p.status != "kutilmoqda"), len(c.pays)))
        if c.cash_need:
            out.append(C.Item("cash", int(c.cash_done), 1))
    # --- hamma uchun: vazifalar
    tk = [t for t in c.tasks if t.staff_id == st.id]
    if tk:
        out.append(C.Item("task", sum(1 for t in tk if t.done_at and t.done_at.date() <= t.due), len(tk)))
    return out


def _cond(e: Exam, c: Ctx) -> int | None:
    g = c.gall.get(e.group_id)
    return e.conductor_id or (g.teacher_id if g else None)


def _ans24(m: Murojaat) -> bool:
    return bool(m.answered_at and m.answered_at - m.created_at <= timedelta(hours=24))


def role_title(st: Staff) -> str:
    rs = [r for r in st.role_list() if r not in ("otaona",)]
    return " · ".join(S.ROLE_TITLES.get(r, r) for r in rs) or "Xodim"


async def _staff(s) -> list[Staff]:
    rows = (await s.execute(select(Staff).where(Staff.active, Staff.tg_id.is_not(None)))).scalars().all()
    return [x for x in rows if set(x.role_list()) - {"otaona"}]


async def _groups(s) -> list[Group]:
    return list((await s.execute(select(Group).where(Group.active))).scalars())


class Cache:
    """Bir ishga tushirishda har bir sana konteksti bir marta yuklanadi."""

    def __init__(self, s, groups):
        self.s, self.groups, self.m = s, groups, {}

    async def get(self, d: date) -> Ctx:
        if d not in self.m:
            self.m[d] = await load(self.s, d, self.groups)
        return self.m[d]


async def week(cache: Cache, st: Staff, mon: date, upto: date) -> tuple[list[C.Item], list[tuple[str, float | None]]]:
    per, days = [], []
    for i in range(6):
        d = mon + timedelta(days=i)
        if d > upto:
            break
        its = items_for(st, await cache.get(d))
        per.append(its)
        days.append((C.WD2[i], C.score(its)))
    return C.merge(per), days


def _seed(st: Staff, d: date) -> int:
    return st.id * 7 + d.toordinal()


# ===================================== ertalabki reja =====================================
async def morning_text(s, cache: Cache, st: Staff, d: date) -> tuple[str, IM | None] | None:
    c = await cache.get(d)
    if st.id in c.leave:
        return None
    roles = st.role_list()
    name = C.short_name(st.full_name)
    lines = [f"☀️ <b>Xayrli tong, {name}!</b>", f"🗓 <b>Bugungi reja · {C.WD[d.weekday()]}, {d:%d.%m}</b>"]
    body = 0
    mine = [(g, x) for g, x in c.teach if st.id in (g.teacher_id, g.assistant_id)]
    mine.sort(key=lambda t: t[0].start_time or time(23))
    if mine:
        lines.append("\n📚 <b>Darslar</b>")
        for g, _ in mine:
            tm = (f"🕐 {g.start_time:%H:%M}–{g.end_time:%H:%M} · " if g.start_time and g.end_time
                  else "🕐 <i>vaqti kiritilmagan</i> · ")
            duty = [w for w, k in (("davomat", "attendance"), ("uy vazifasi", "homework")) if _resp(g, k) == st.id]
            if g.id in c.hw_prev and _resp(g, "homework") == st.id:
                duty.insert(1, "vazifani tekshirish")
            if not duty:
                duty = ["dars o'tish (davomat va uy vazifasi — yordamchida)"]
            room = (f"{g.room}-xona" if g.room.strip().isdigit() else g.room.strip()) if g.room else ""
            lines.append(f"  {tm}<b>{g.name}</b>" + (f" · 🚪 {room}" if room else "")
                         + f"\n      ↳ {', '.join(duty)}")
        body += 1
    ex = [e for e in c.exams if e.status == "reja" and _cond(e, c) == st.id]
    if ex:
        names = {g.id: g.name for g in cache.groups}
        lines.append("\n🧪 <b>Nazorat</b>: " + ", ".join(names.get(e.group_id, "?") + (f" ({e.topic})" if e.topic else "")
                                                       for e in ex))
        body += 1
    tasks = list((await s.execute(select(Task).where(Task.staff_id == st.id, Task.status == "ochiq",
                                                     Task.due <= d + timedelta(days=1)).order_by(Task.due))).scalars())
    kb = []
    if tasks:
        lines.append(f"\n📝 <b>Vazifalar ({len(tasks)})</b>")
        for t in tasks[:8]:
            late = (d - t.due).days
            when = ("muddati bugun" if t.due == d else "muddati ertaga" if t.due > d
                    else f"⚠️ muddati {t.due:%d.%m} da o'tgan ({late} kun)")
            txt = t.text if len(t.text) <= 140 else t.text[:137].rstrip() + "…"
            lines.append(f"  • {txt}\n     <i>{when}</i>")
        if any(t.due < d for t in tasks):
            lines.append("  <i>Bajarilgan bo'lsa — pastdagi «✅» tugmasini bosing, ro'yxatdan chiqadi.</i>")
        kb += [[IB(text=f"✅ {t.text[:28]}", callback_data=f"tdone:{t.id}")] for t in tasks[:5]]
        body += 1
    wait = (await s.execute(select(func.count(Murojaat.id)).where(Murojaat.status == "yangi",
                                                                   Murojaat.category == "ustoz",
                                                                   Murojaat.to_staff_id == st.id))).scalar()
    q = []
    if wait:
        q.append(f"💬 Javob kutayotgan murojaatlar: <b>{wait}</b>")
    if "admin" in roles:
        n_lead = (await s.execute(select(func.count(Lead.id)).where(Lead.status == "yangi"))).scalar()
        n_pr = (await s.execute(select(func.count(PrintJob.id)).where(PrintJob.status == "yangi"))).scalar()
        n_mj = (await s.execute(select(func.count(Murojaat.id)).where(Murojaat.status == "yangi",
                                                                       Murojaat.category != "ustoz"))).scalar()
        q += [x for x in (f"📞 Bog'lanilmagan arizalar: <b>{n_lead}</b>" if n_lead else "",
                          f"🖨 Chop etish navbatida: <b>{n_pr}</b>" if n_pr else "",
                          f"📨 Javobsiz murojaatlar: <b>{n_mj}</b>" if n_mj else "") if x]
        q.append("📞 Darsga kelmaganlar ota-onasiga qo'ng'iroq — kun davomida")
    if "moliya" in roles:
        n_pay = len(await S.pending_payments(s))
        if n_pay:
            q.append(f"💳 Tasdiq kutayotgan to'lovlar: <b>{n_pay}</b>")
        q.append("🔐 Kun oxirida kassani yopish")
    if q:
        lines.append("\n📌 <b>Navbatda</b>\n" + "\n".join("  " + x for x in q))
        body += 1
    # dushanba — hafta rejasi
    if d.weekday() == 0:
        wk = await week_plan(s, cache, st, d)
        if wk:
            lines.insert(2, wk)
            body += 1
    if not body:
        return None
    # kechagi / hafta natijasi
    y = d - timedelta(days=1)
    if y.weekday() == 6:
        y -= timedelta(days=1)
    ys = C.score(items_for(st, await cache.get(y)))
    if ys is not None and d.weekday() != 0:
        lines.append(f"\n🎯 Kechagi natija: <b>{ys:g}%</b>" + (" 🌟" if ys >= 90 else ""))
    lines.append(f"\n💡 <i>{C.pick(C.QUOTES, d.toordinal())}</i>")
    kb.append([IB(text="📈 Shu haftadagi natijam", callback_data="cch:me")])
    return "\n".join(lines), IM(inline_keyboard=kb)


async def week_plan(s, cache: Cache, st: Staff, mon: date) -> str:
    sat = mon + timedelta(days=5)
    groups = [g for g in cache.groups if st.id in (_resp(g, "attendance"), _resp(g, "homework"))]
    n_les = sum(1 for g in groups for i in range(6) if is_lesson_day(mon + timedelta(days=i), g.days))
    gids = {g.id for g in cache.groups if g.teacher_id == st.id}
    exams = list((await s.execute(select(Exam).where(Exam.date >= mon, Exam.date <= sat, Exam.status == "reja")
                                  .order_by(Exam.date))).scalars())
    exams = [e for e in exams if e.conductor_id == st.id or (not e.conductor_id and e.group_id in gids)]
    n_task = (await s.execute(select(func.count(Task.id)).where(Task.staff_id == st.id, Task.status == "ochiq",
                                                                 Task.due <= sat))).scalar()
    last, _ = await week(cache, st, mon - timedelta(days=7), mon - timedelta(days=2))
    lp = C.score(last)
    if not (n_les or exams or n_task or lp is not None):
        return ""
    names = {g.id: g.name for g in cache.groups}
    out = [f"\n🗓 <b>Yangi hafta · {mon:%d.%m}–{sat:%d.%m}</b>"]
    if n_les:
        out.append(f"  📚 Darslar: <b>{n_les}</b> ta")
    if exams:
        out.append("  🧪 Nazoratlar: " + ", ".join(f"{C.WD2[e.date.weekday()]} — {names.get(e.group_id, '?')}"
                                                 for e in exams))
    if n_task:
        out.append(f"  📝 Hafta ichida yopiladigan vazifalar: <b>{n_task}</b>")
    if lp is not None:
        goal = "a'loni saqlab qolish" if lp >= 90 else f"{min(100, int(lp // 5 * 5 + 10))}% va undan yuqori"
        out.append(f"  🎯 O'tgan hafta: <b>{lp:g}%</b> → maqsad: <b>{goal}</b>")
    out.append("  💪 Yangi haftangiz barakali o'tsin!")
    return "\n".join(out)


async def morning_job(bot: Bot) -> None:
    d = today()
    if d.weekday() == 6:
        return
    out = []
    async with Session() as s:
        if not (await cfg(s))["on"]:
            return
        cache = Cache(s, await _groups(s))
        for st in await _staff(s):
            try:
                r = await morning_text(s, cache, st, d)
            except Exception:
                r = None
            if r:
                out.append((st.tg_id, *r))
    for tg, text, kb in out:
        await send(bot, tg, text, reply_markup=kb)


# ===================================== kechki natija =====================================
async def _photo(bot: Bot, tg: int, png: bytes, caption: str, kb: IM | None = None) -> None:
    try:
        await bot.send_photo(tg, BufferedInputFile(png, "natija.png"), caption=caption[:1024], reply_markup=kb)
    except Exception:
        await send(bot, tg, caption, reply_markup=kb)


def _is_week_end(d: date) -> bool:
    return d.weekday() == 5


async def day_result(cache: Cache, st: Staff, d: date) -> tuple[bytes, str, float] | None:
    its = items_for(st, await cache.get(d))
    p = C.score(its)
    if p is None:
        return None
    y = d - timedelta(days=1)
    if y.weekday() == 6:
        y -= timedelta(days=1)
    prev = C.score(items_for(st, await cache.get(y)))
    png = C.render("day", st.full_name, role_title(st), f"{C.WD[d.weekday()]}, {d:%d.%m.%Y}", p, its, prev=prev)
    return png, C.day_text(st.full_name, p, its, _seed(st, d)), p


async def week_result(cache: Cache, st: Staff, d: date, best_id: int | None
                      ) -> tuple[bytes, str, float, list[C.Item]] | None:
    mon = d - timedelta(days=d.weekday())
    its, days = await week(cache, st, mon, d)
    p = C.score(its)
    if p is None:
        return None
    last, _ = await week(cache, st, mon - timedelta(days=7), mon - timedelta(days=2))
    prev = C.score(last)
    best = st.id == best_id
    png = C.render("week", st.full_name, role_title(st), f"{mon:%d.%m}–{d:%d.%m.%Y}", p, its, days=days,
                   prev=prev, best=best)
    return png, C.week_text(st.full_name, p, its, _seed(st, d), prev, best), p, its


async def evening_job(bot: Bot) -> None:
    d = today()
    weekly = _is_week_end(d)
    sends, team = [], []
    async with Session() as s:
        conf = await cfg(s)
        if not conf["on"]:
            return
        cache = Cache(s, await _groups(s))
        staff = await _staff(s)
        bosses = [x.tg_id for x in await S.staff_with_role(s, "rahbar") if x.tg_id]
        best_id = None
        if weekly:
            mon = d - timedelta(days=d.weekday())
            sc = []
            for st in staff:
                its, _ = await week(cache, st, mon, d)
                p = C.score(its)
                if p is not None and sum(i.total for i in its) >= 5:
                    sc.append((p, sum(i.total for i in its), st.id))
            if sc:
                top = max(sc)
                best_id = top[2] if top[0] >= 85 else None
        for st in staff:
            if weekly:
                r = await week_result(cache, st, d, best_id)
                if r:
                    png, txt, p, its = r
                    sends.append((st.tg_id, png, txt))
                    last, _ = await week(cache, st, mon - timedelta(days=7), mon - timedelta(days=2))
                    team.append(C.TeamRow(st.full_name, role_title(st), p, C.score(last), _weak(its)))
            else:
                r = await day_result(cache, st, d)
                if r:
                    png, txt, p = r
                    sends.append((st.tg_id, png, txt))
                    team.append(C.TeamRow(st.full_name, role_title(st), p, None,
                                          _weak(items_for(st, await cache.get(d)))))
    for tg, png, txt in sends:
        await _photo(bot, tg, png, txt)
    if not team:
        return
    if weekly:
        mon = d - timedelta(days=d.weekday())
        png = C.render_team(f"Hafta · {mon:%d.%m}–{d:%d.%m}", f"{d:%d.%m.%Y}", team)
        cap = team_caption(team, weekly=True)
        for b in bosses:
            await _photo(bot, b, png, cap)
    elif conf["boss_daily"]:
        for b in bosses:
            await send(bot, b, team_caption(team, weekly=False, d=d))


def _weak(items: list[C.Item]) -> str:
    miss = [i for i in items if i.done < i.total]
    if not miss:
        return ""
    w = max(miss, key=lambda i: i.weight * (1 - i.ratio))
    return f"{w.label.lower()} {w.done}/{w.total}"


def team_caption(team: list[C.TeamRow], weekly: bool, d: date | None = None) -> str:
    rows = sorted(team, key=lambda r: -(r.pct or 0))
    avg = round(sum(r.pct for r in rows) / len(rows))
    head = "👥 <b>Jamoa — haftalik yakun</b>" if weekly else f"👥 <b>Jamoa bugun · {d:%d.%m}</b>"
    out = [head, f"O'rtacha: <b>{avg}%</b>"]
    if not weekly:
        for r in rows:
            mark = {"zor": "🟢", "yaxshi": "🔵", "orta": "🟡", "past": "🟠"}[C.tier(r.pct)]
            out.append(f"{mark} {C.short_name(r.name)} — <b>{r.pct:g}%</b>" + (f"  <i>({r.weak})</i>" if r.weak else ""))
    good = [C.short_name(r.name) for r in rows if r.pct >= 90]
    help_ = [C.short_name(r.name) for r in rows if r.pct < 75]
    if good:
        out.append(f"\n🌟 Maqtovga loyiq: {', '.join(good)}")
    if help_:
        out.append(f"🤝 Qo'llab-quvvatlash kerak: {', '.join(help_)}\n"
                   "<i>Ularga «biz sizga ishonamiz» xabari yuborildi; shaxsan bir og'iz iliq so'z ham katta kuch beradi.</i>")
    return "\n".join(out)[:1024 if weekly else 4000]


# ===================================== tugmalar va sozlamalar =====================================
@router.callback_query(F.data == "cch:me")
async def my_week(c: CallbackQuery) -> None:
    d = today()
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not who.staff:
            await c.answer()
            return
        cache = Cache(s, await _groups(s))
        r = await week_result(cache, who.staff, d, None)
    await c.answer()
    if not r:
        await c.message.answer("Bu hafta hali o'lchanadigan ish bo'lmadi. Dars, vazifa yoki navbatdagi ishlar "
                               "bajarilishi bilan natijangiz shu yerda ko'rinadi.")
        return
    png, txt, _, _ = r
    await c.message.answer_photo(BufferedInputFile(png, "natija.png"), caption=txt[:1024])


async def _is_boss(s, tg: int) -> bool:
    who = await S.who_by_tg(s, tg)
    return bool(who.staff and "rahbar" in who.staff.role_list())


def _settings_kb(conf: dict) -> IM:
    on = lambda v: "✅" if v else "▫️"   # noqa: E731
    return IM(inline_keyboard=[
        [IB(text=f"{on(conf['on'])} Xodimlarga reja va natija yuborish", callback_data="cch:t:on")],
        [IB(text=f"{on(conf['boss_daily'])} Menga har kuni jamoa xulosasi", callback_data="cch:t:boss_daily")],
        [IB(text="👥 Jamoa natijasi (shu hafta)", callback_data="cch:team")],
        [IB(text="☀️ Ertalabki xabar namunasi (o'zimga)", callback_data="cch:demo")],
    ])


SETTINGS_TEXT = ("👥 <b>Xodimlar murabbiyi</b>\n"
                 "☀️ 08:45 — har bir xodimga bugungi reja (dushanba — hafta rejasi ham)\n"
                 "🌙 20:40 — kunlik natija kartasi va xabar\n"
                 "🏁 Shanba 20:40 — haftalik natija, «Hafta xodimi»\n\n"
                 "Xabar ohangi natijaga qarab: <b>90%+</b> — maqtov, <b>75%+</b> — rag'bat, "
                 "undan past — «<b>biz sizga ishonamiz</b>» va bitta aniq maslahat. Ball yoki jarima yozilmaydi.")


@router.callback_query(F.data == "set:coach")
async def settings(c: CallbackQuery) -> None:
    async with Session() as s:
        if not await _is_boss(s, c.from_user.id):
            await c.answer()
            return
        conf = await cfg(s)
    await c.answer()
    await c.message.answer(SETTINGS_TEXT, reply_markup=_settings_kb(conf))


@router.callback_query(F.data.startswith("cch:t:"))
async def toggle(c: CallbackQuery) -> None:
    key = c.data.split(":")[2]
    async with Session() as s:
        if not await _is_boss(s, c.from_user.id) or key not in DEFAULT:
            await c.answer()
            return
        conf = await cfg(s)
        conf[key] = not conf[key]
        row = await s.get(Setting, "coach")
        if row:
            row.value = conf
        else:
            s.add(Setting(key="coach", value=conf))
        who = await S.who_by_tg(s, c.from_user.id)
        await S.audit(s, who.staff, "sozlama", murabbiy=f"{key}={conf[key]}")
        await s.commit()
    await c.answer("Saqlandi")
    try:
        await c.message.edit_reply_markup(reply_markup=_settings_kb(conf))
    except Exception:
        pass


@router.callback_query(F.data == "cch:team")
async def team_now(c: CallbackQuery) -> None:
    d = today()
    mon = d - timedelta(days=d.weekday())
    team = []
    async with Session() as s:
        if not await _is_boss(s, c.from_user.id):
            await c.answer()
            return
        cache = Cache(s, await _groups(s))
        for st in await _staff(s):
            its, _ = await week(cache, st, mon, d)
            p = C.score(its)
            if p is not None:
                last, _ = await week(cache, st, mon - timedelta(days=7), mon - timedelta(days=2))
                team.append(C.TeamRow(st.full_name, role_title(st), p, C.score(last), _weak(its)))
    await c.answer()
    if not team:
        await c.message.answer("Bu hafta hali o'lchanadigan ish yo'q.")
        return
    png = C.render_team(f"Hafta · {mon:%d.%m}–{d:%d.%m}", f"{d:%d.%m.%Y}", team)
    await c.message.answer_photo(BufferedInputFile(png, "jamoa.png"), caption=team_caption(team, weekly=True))


@router.callback_query(F.data == "cch:demo")
async def demo(c: CallbackQuery) -> None:
    d = today()
    async with Session() as s:
        who = await S.who_by_tg(s, c.from_user.id)
        if not (who.staff and "rahbar" in who.staff.role_list()):
            await c.answer()
            return
        cache = Cache(s, await _groups(s))
        r = await morning_text(s, cache, who.staff, d if d.weekday() != 6 else d + timedelta(days=1))
    await c.answer()
    if not r:
        await c.message.answer("Bugun sizda reja yo'q — xodimlarga ham faqat ishi bor kunlari yuboriladi.")
        return
    await c.message.answer(r[0], reply_markup=r[1])
