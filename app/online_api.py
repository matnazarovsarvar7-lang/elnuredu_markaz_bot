"""Online MOC mini-ilovasi uchun HTTP API (agent API bilan bitta serverda, Caddy orqali HTTPS).

GET  /o/app                 — mini-ilova sahifasi
POST /o/api/start           — {init, m}: Telegram initData tekshiriladi → test ma'lumotlari, qolgan vaqt, token
GET  /o/api/page?m&p&t      — savollar PDF sahifasi (PNG) — faqat ochiq test davomida, token bilan
POST /o/api/save            — {m, t, a}: javoblar har belgilashda saqlanadi
POST /o/api/submit          — {m, t, a}: yakunlash (bir marta)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from app.config import settings
from app.db.models import Moc, MocSeat
from app.db.session import Session
from app.logic import moc as M

log = logging.getLogger("online")
PAGE = Path(__file__).parent / "web" / "online.html"
CACHE = Path("/tmp/elnur-online")
_LOCKS: dict[str, asyncio.Lock] = {}


def check_init(init: str, max_age: int = 24 * 3600) -> dict | None:
    """Telegram WebApp initData imzosini tekshiradi; to'g'ri bo'lsa — user ma'lumoti."""
    try:
        data = dict(parse_qsl(init, keep_blank_values=True))
        got = data.pop("hash")
    except (KeyError, ValueError):
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
        return None
    if int(data.get("auth_date", "0")) < time.time() - max_age:
        return None
    try:
        return json.loads(data.get("user", "{}"))
    except ValueError:
        return None


def _err(msg: str, code: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=code)


async def _body(req: web.Request) -> dict:
    try:
        d = await req.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _clean(sp: M.Spec, a: str) -> str:
    """Javoblar qatori: test — A..F, yozma — 0..mx, bo'sh — '-'."""
    a = (a or "")[:sp.n_questions].ljust(sp.n_questions, "-")
    out = []
    for i, (kind, k) in enumerate(sp.bubbles()):
        ch = a[i].upper()
        if kind == "t":
            out.append(ch if ch in "ABCDEF"[:k] else "-")
        else:
            out.append(ch if ch.isdigit() and int(ch) <= k else "-")
    return "".join(out)


async def _seat(mid: int, tg: int):
    from sqlalchemy import select
    async with Session() as s:
        moc = await s.get(Moc, mid)
        z = (await s.execute(select(MocSeat).where(MocSeat.moc_id == mid, MocSeat.tg_id == tg))).scalars().first()
    return moc, z


def _remain(moc, z) -> int:
    from app.bot import online as O
    dl = O.deadline(moc, z)
    return max(0, int((dl - O.now()).total_seconds())) if dl else 0


async def app_page(req: web.Request) -> web.Response:
    return web.Response(text=PAGE.read_text(encoding="utf-8"), content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def start(req: web.Request) -> web.Response:
    from app.bot import online as O
    d = await _body(req)
    user = check_init(str(d.get("init", "")))
    if not user:
        return _err("Telegram orqali oching", 403)
    try:
        mid = int(d.get("m"))
    except (TypeError, ValueError):
        return _err("Imtihon topilmadi", 404)
    moc, z = await _seat(mid, int(user.get("id", 0)))
    if not moc or not O.is_online(moc) or not z:
        return _err("Siz bu imtihonga ro'yxatdan o'tmagansiz", 404)
    sp = M.parse_spec(moc.spec)
    st, end, minutes, mode = O.times(moc)
    t = O.now()
    o = O.zinfo(z)
    if moc.status != "tayyor":
        return _err("Imtihon yakunlangan", 410)
    if moc.price and z.paid < moc.price:
        return _err("To'lov tasdiqlanmagan", 402)
    if t < st:
        return _err(f"Test {st:%d.%m %H:%M} da ochiladi", 425)
    if o.get("submitted"):
        return web.json_response({"ok": True, "submitted": True, "title": moc.title})
    if not o.get("started"):
        if t >= end:
            return _err("Test vaqti tugagan", 410)
        async with Session() as s:
            zz = await s.get(MocSeat, z.id)
            zz.online = {**(zz.online or {}), "started": t.isoformat(), "ua": req.headers.get("User-Agent", "")[:120]}
            await s.commit()
            z = zz
    rem = _remain(moc, z)
    if rem <= 0:
        return _err("Vaqtingiz tugagan", 410)
    b = (moc.booklets or {}).get("A") or {}
    return web.json_response({
        "ok": True, "title": moc.title, "name": z.name, "remain": rem, "token": O.token(z), "seat": z.id,
        "sections": O.sections_json(sp), "n": sp.n_questions, "answers": z.answers or "",
        "pages": int(b.get("pages") or 0)})


async def _auth(d: dict):
    from app.bot import online as O
    try:
        mid = int(d.get("m"))
        zid = int(d.get("z"))
    except (TypeError, ValueError):
        return None, None
    async with Session() as s:
        z = await s.get(MocSeat, zid)
        moc = await s.get(Moc, mid) if z and z.moc_id == mid else None
    if not moc or not hmac.compare_digest(O.token(z), str(d.get("t", ""))):
        return None, None
    return moc, z


async def save(req: web.Request) -> web.Response:
    from app.bot import online as O
    d = await _body(req)
    moc, z = await _auth(d)
    if not z:
        return _err("Ruxsat yo'q", 403)
    o = O.zinfo(z)
    if o.get("submitted") or moc.status != "tayyor":
        return web.json_response({"ok": True, "submitted": True})
    sp = M.parse_spec(moc.spec)
    rem = _remain(moc, z)
    async with Session() as s:
        zz = await s.get(MocSeat, z.id)
        if rem > -30:                                   # tarmoq kechikishi uchun 30 soniya
            zz.answers = _clean(sp, str(d.get("a", "")))
        await s.commit()
    return web.json_response({"ok": True, "remain": rem})


async def submit(req: web.Request) -> web.Response:
    from app.bot import online as O
    d = await _body(req)
    moc, z = await _auth(d)
    if not z:
        return _err("Ruxsat yo'q", 403)
    if O.zinfo(z).get("submitted"):
        return web.json_response({"ok": True, "submitted": True})
    sp = M.parse_spec(moc.spec)
    rem = _remain(moc, z)
    async with Session() as s:
        zz = await s.get(MocSeat, z.id)
        if rem > -30:
            zz.answers = _clean(sp, str(d.get("a", "")))
        names = [x.name for x in sp.sections if x.kind == "baho"]
        zz.online = {**(zz.online or {}), "submitted": O.now().isoformat(), "await_photos": bool(names) and rem > 0}
        await s.commit()
        z = zz
    await O.on_submitted(req.app["bot"], moc, z, auto=rem <= 0)
    return web.json_response({"ok": True, "submitted": True})


async def _render(mid: int, fid: str, p: int, bot) -> Path | None:
    d = CACHE / str(mid)
    out = d / f"p{p}.png"
    if out.exists():
        return out
    lock = _LOCKS.setdefault(f"{mid}", asyncio.Lock())
    async with lock:
        if out.exists():
            return out
        d.mkdir(parents=True, exist_ok=True)
        pdf = d / "q.pdf"
        if not pdf.exists():
            buf = await bot.download(fid)
            pdf.write_bytes(buf.read())
        proc = await asyncio.create_subprocess_exec("pdftoppm", "-r", "120", "-png", "-f", str(p), "-l", str(p),
                                                    "-singlefile", str(pdf), str(d / f"p{p}"))
        await proc.wait()
    return out if out.exists() else None


async def page(req: web.Request) -> web.Response:
    from app.bot import online as O
    q = req.query
    moc, z = await _auth({"m": q.get("m"), "z": q.get("z"), "t": q.get("t")})
    if not z:
        return _err("Ruxsat yo'q", 403)
    o = O.zinfo(z)
    if not o.get("started") or o.get("submitted") or _remain(moc, z) <= 0:
        return _err("Sahifa faqat test davomida ochiladi", 403)
    try:
        p = int(q.get("p", "1"))
    except ValueError:
        return _err("Sahifa raqami", 400)
    b = (moc.booklets or {}).get("A") or {}
    if not b.get("file_id") or not 1 <= p <= int(b.get("pages") or 0):
        return _err("Sahifa topilmadi", 404)
    path = await _render(moc.id, b["file_id"], p, req.app["bot"])
    if not path:
        return _err("Sahifani tayyorlab bo'lmadi", 500)
    return web.FileResponse(path, headers={"Cache-Control": "private, no-store"})


def add_routes(app: web.Application) -> None:
    app.router.add_get("/o/app", app_page)
    app.router.add_post("/o/api/start", start)
    app.router.add_post("/o/api/save", save)
    app.router.add_post("/o/api/submit", submit)
    app.router.add_get("/o/api/page", page)
