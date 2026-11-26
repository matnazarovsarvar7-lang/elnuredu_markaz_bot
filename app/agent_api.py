"""Printer dasturi (admin kompyuteridagi PowerShell agent) uchun HTTP API.

Agent faqat o'zi serverga murojaat qiladi (kompyuterga tashqaridan ulanilmaydi):
  POST /agent/poll           — "men tirikman" + printerlar ro'yxati → navbatdagi topshiriqlar
  GET  /agent/file/{id}      — chop etiladigan PDF
  POST /agent/done           — topshiriq natijasi
  POST /agent/log            — Windows chop etish jurnali (bot orqali va botsiz chiqqan hamma narsa)
  GET  /agent/script         — agent.ps1 (yangilash uchun)
  GET  /agent/install/{t}    — bir martalik o'rnatish skripti (15 daqiqa amal qiladi)
Hamma so'rov «Authorization: Bearer <kalit>» bilan; kalitning faqat xeshi saqlanadi.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiohttp import web
from sqlalchemy import select

from app.config import settings
from app.db.models import PrintJob, PrintLog, PrintTask, Setting
from app.db.session import Session
from app.logic import impose

log = logging.getLogger("agent")
AGENT_PS1 = Path(__file__).parent / "agent" / "agent.ps1"
INSTALL_PS1 = Path(__file__).parent / "agent" / "install.ps1"
CACHE = Path("/tmp/elnur-print")


async def _body(req: web.Request) -> dict:
    try:
        d = json.loads((await req.read()).decode("utf-8-sig") or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _h(x: str) -> str:
    return hashlib.sha256(x.encode()).hexdigest()


async def agent_state(s) -> dict:
    row = await s.get(Setting, "print_agent")
    return dict(row.value or {}) if row else {}


async def save_state(s, st: dict) -> None:
    row = await s.get(Setting, "print_agent")
    if row:
        row.value = dict(st)
    else:
        s.add(Setting(key="print_agent", value=dict(st)))


def online(st: dict, sec: int = 90) -> bool:
    try:
        return datetime.utcnow() - datetime.fromisoformat(st.get("seen", "")) < timedelta(seconds=sec)
    except ValueError:
        return False


async def new_install(s) -> tuple[str, str]:
    """Yangi kalit + 15 daqiqalik o'rnatish havolasi. Eski kalit bekor bo'ladi."""
    key, tok = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
    st = await agent_state(s)
    st["key"] = _h(key)
    st["install"] = {"t": _h(tok), "exp": (datetime.utcnow() + timedelta(minutes=15)).isoformat(), "k": key}
    await save_state(s, st)
    return key, f"https://{settings.print_host}/agent/install/{tok}"


async def _auth(req: web.Request) -> bool:
    got = req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not got:
        return False
    async with Session() as s:
        st = await agent_state(s)
    return bool(st.get("key")) and hmac.compare_digest(_h(got), st["key"])


def _guard(fn):
    async def w(req: web.Request):
        if not await _auth(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await fn(req)
    return w


async def poll(req: web.Request) -> web.Response:
    body = await _body(req)
    printers = [str(x)[:120] for x in (body.get("printers") or [])][:20]
    async with Session() as s:
        st = await agent_state(s)
        st["seen"] = datetime.utcnow().isoformat()
        st["printers"] = printers
        st["ver"] = body.get("ver")
        await save_state(s, st)
        tasks = (await s.execute(select(PrintTask).where(PrintTask.status == "navbat").order_by(PrintTask.id)
                                 .limit(5))).scalars().all()
        out = []
        for t in tasks:
            t.status, t.sent_at = "yuborildi", datetime.utcnow()
            out.append({"id": t.id, "printer": t.printer, "name": f"elnur-{t.id}-{t.side}.pdf"})
        await s.commit()
    return web.json_response({"tasks": out})


async def _pdf_for(bot: Bot, t: PrintTask, s) -> bytes:
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{t.id}.pdf"
    if f.exists():
        return f.read_bytes()
    st = await agent_state(s)
    cal = (st.get("cal") or {}).get(t.printer, {})
    if t.job_id is None:                                   # sinov varag'i
        pdf = impose.test_sheets(t.side)
    else:
        job = await s.get(PrintJob, t.job_id)
        raw = (await bot.download(job.file_id)).read()
        if job.file_kind == "photo" or raw[:4] != b"%PDF":
            raw = impose.image_to_pdf(raw)
        pdf, n = impose.build(raw, job.layout, job.copies, t.side, cal.get("rev", False), cal.get("rot", False))
        t.pages = n
        await s.commit()
    f.write_bytes(pdf)
    return pdf


async def file(req: web.Request) -> web.Response:
    tid = int(req.match_info["id"])
    async with Session() as s:
        t = await s.get(PrintTask, tid)
        if not t:
            return web.Response(status=404)
        try:
            pdf = await _pdf_for(req.app["bot"], t, s)
        except Exception as e:
            log.exception("pdf")
            t.status, t.error = "xato", f"Faylni tayyorlab bo'lmadi: {e}"[:300]
            await s.commit()
            from app.bot.prints import on_task_result
            await on_task_result(req.app["bot"], t.id)
            return web.Response(status=500)
    return web.Response(body=pdf, content_type="application/pdf")


async def done(req: web.Request) -> web.Response:
    body = await _body(req)
    async with Session() as s:
        t = await s.get(PrintTask, int(body.get("id", 0)))
        if not t or t.status not in ("yuborildi", "navbat"):
            return web.json_response({"ok": True})
        ok = bool(body.get("ok"))
        t.status, t.done_at = ("tayyor" if ok else "xato"), datetime.utcnow()
        t.error = None if ok else str(body.get("err") or "Printer xatosi")[:300]
        await s.commit()
    (CACHE / f"{t.id}.pdf").unlink(missing_ok=True)
    from app.bot.prints import on_task_result
    await on_task_result(req.app["bot"], t.id)
    return web.json_response({"ok": True})


async def logs(req: web.Request) -> web.Response:
    body = await _body(req)
    fresh = []
    async with Session() as s:
        for it in (body.get("items") or [])[:500]:
            try:
                rid = int(it["rid"])
                at = datetime.fromisoformat(str(it["time"]).replace("Z", "+00:00")).replace(tzinfo=None)
            except (KeyError, ValueError, TypeError):
                continue
            if (await s.execute(select(PrintLog.id).where(PrintLog.rid == rid))).first():
                continue
            doc = str(it.get("doc") or "")[:250]
            task_id = None
            if doc.startswith("elnur-"):
                try:
                    task_id = int(doc.split("-")[1])
                except (IndexError, ValueError):
                    pass
            row = PrintLog(rid=rid, at=at, printer=str(it.get("printer") or "")[:120], doc=doc,
                           user=str(it.get("user") or "")[:80], pages=max(0, int(it.get("pages") or 0)),
                           task_id=task_id, status="bot" if task_id else "yangi")
            s.add(row)
            await s.flush()
            if not task_id and row.pages:
                fresh.append(row.id)
        await s.commit()
    if fresh:
        from app.bot.prints import notify_unaccounted
        await notify_unaccounted(req.app["bot"], fresh)
    return web.json_response({"ok": True, "n": len(fresh)})


async def script(req: web.Request) -> web.Response:
    return web.Response(text=AGENT_PS1.read_text(encoding="utf-8"), content_type="text/plain")


async def install(req: web.Request) -> web.Response:
    tok = req.match_info["t"]
    async with Session() as s:
        st = await agent_state(s)
        ins = st.get("install") or {}
        try:
            fresh = datetime.utcnow() < datetime.fromisoformat(ins.get("exp", ""))
        except ValueError:
            fresh = False
        if not ins.get("k") or not fresh or not hmac.compare_digest(_h(tok), ins.get("t", "")):
            return web.Response(text="Write-Host 'Havola eskirgan. Botdan yangisini oling.' -ForegroundColor Red",
                                content_type="text/plain")
        key = ins["k"]
        st["install"] = {}                                   # bir martalik
        await save_state(s, st)
        await s.commit()
    txt = (INSTALL_PS1.read_text(encoding="utf-8").replace("__URL__", f"https://{settings.print_host}")
           .replace("__KEY__", key))
    return web.Response(text=txt, content_type="text/plain")


def make_app(bot: Bot) -> web.Application:
    app = web.Application(client_max_size=4 * 1024 * 1024)
    app["bot"] = bot
    app.router.add_post("/agent/poll", _guard(poll))
    app.router.add_get("/agent/file/{id:\\d+}", _guard(file))
    app.router.add_post("/agent/done", _guard(done))
    app.router.add_post("/agent/log", _guard(logs))
    app.router.add_get("/agent/script", _guard(script))
    app.router.add_get("/agent/install/{t}", install)
    app.router.add_get("/agent/ping", lambda r: web.Response(text="ok"))
    from app.online_api import add_routes
    add_routes(app)                                       # online MOC mini-ilovasi
    return app


async def start(bot: Bot) -> web.AppRunner | None:
    runner = web.AppRunner(make_app(bot), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", settings.print_api_port).start()
    except OSError:
        log.exception("agent API port band")
        return None
    return runner
