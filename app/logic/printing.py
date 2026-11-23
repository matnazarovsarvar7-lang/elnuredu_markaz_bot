"""Chop etish hisob-kitobi: bet → varaq → narx (yaxlitlash qoidasi bilan)."""
from __future__ import annotations

import io
import math

PER_SHEET = {"buklet": 4, "ikki": 2, "bir": 1}       # 1 varaqqa necha bet sig'adi
LAYOUTS = {"buklet": "📖 Kitobcha (buklet) — 1 varaqda 4 bet",
           "ikki": "📄 A4 ikki tomonlama — 1 varaqda 2 bet",
           "bir": "📃 A4 bir tomonlama — 1 varaqda 1 bet"}
LAYOUT_SHORT = {"buklet": "buklet", "ikki": "A4 ikki tomon", "bir": "A4 bir tomon"}
SHEET_PRICE = 500           # 1 varaq narxi (sozlamada o'zgartiriladi)
TEACHER_PCT, ADMIN_PCT = 7.0, 8.0
BIG = 15                    # shu varaqqacha yuqoriga, undan ko'pi pastga yaxlitlanadi


def sheets(pages: int, layout: str) -> int:
    return max(1, math.ceil(max(1, pages) / PER_SHEET.get(layout, 1)))


def price(n_sheets: int, unit: int = SHEET_PRICE) -> int:
    """Kitobcha narxi: varaq × narx, 1000 ga yaxlitlanadi —
    15 varaqgacha (15 ham) yuqoriga (3 500 → 4 000), 15 dan ko'p bo'lsa pastga (9 500 → 9 000)."""
    a = n_sheets * unit
    r = a % 1000
    if not r:
        return a
    return a - r + 1000 if n_sheets <= BIG else max(1000, a - r)


def pdf_pages(data: bytes) -> int | None:
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return None
