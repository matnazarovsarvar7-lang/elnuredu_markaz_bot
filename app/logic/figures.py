"""Test savollaridagi chizmalar (SVG): xavfsizlik tekshiruvi va PDF uchun chizmaga aylantirish."""
from __future__ import annotations

import io
import logging
import re

log = logging.getLogger(__name__)
MAX_BYTES = 30_000
_BAD = re.compile(r"<\s*(script|image|foreignObject|iframe|use\b)|\son\w+\s*=|(?:xlink:)?href\s*=\s*[\"'](?!#)|"
                  r"url\(\s*[\"']?(?!#)|<!ENTITY|<!DOCTYPE", re.I)


def check(svg: str) -> str | None:
    """None — yaroqli. Aks holda xato matni."""
    s = (svg or "").strip()
    if not s.lower().startswith("<svg") or not s.lower().endswith("</svg>"):
        return "chizma <svg ...> bilan boshlanib </svg> bilan tugashi kerak"
    if len(s.encode()) > MAX_BYTES:
        return "chizma juda katta (30 KB gacha)"
    if _BAD.search(s):
        return "chizmada ruxsat etilmagan element bor (rasm, skript, tashqi havola)"
    if to_drawing(s, 80, 60) is None:
        return "chizma o'qilmadi (SVG xato)"
    return None


def to_drawing(svg: str, max_w_mm: float, max_h_mm: float):
    """SVG → reportlab Drawing, o'lchami chegaraga moslab kichraytiriladi (kattalashtirilmaydi)."""
    try:
        from reportlab.lib.units import mm
        from svglib.svglib import svg2rlg
        if "xmlns" not in svg[:200]:
            svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
        d = svg2rlg(io.BytesIO(svg.encode()))
        if d is None or not d.width or not d.height:
            return None
        k = min(1.0, max_w_mm * mm / d.width, max_h_mm * mm / d.height)
        if k < 1.0:
            d.scale(k, k)
            d.width, d.height = d.width * k, d.height * k
        return d
    except Exception:
        log.exception("svg")
        return None
