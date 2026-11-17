"""Savol matnida kasrlarni haqiqiy ko'rinishda (surat / chiziq / maxraj) chizish.

Kasr yozilishi:
  • aniq:      [[x² + x − 5 | x]]
  • avtomatik: (x² + x − 5)/x ,  3x/(x² + x − 5) ,  (a + b)/(a − b)
    — kamida bir tomoni qavsli ifoda bo'lsa; «m/s», «1/2» kabi oddiy yozuvlar o'zgarmaydi.
"""
from __future__ import annotations

import re

from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Flowable

_PAREN = r"\((?:[^()]|\([^()]*\))*\)"
_ATOM = r"[0-9A-Za-z²³⁴⁵ⁿ√π·.,]+"
EXPL = re.compile(r"\[\[(.+?)\|(.+?)\]\]")
AUTO = re.compile(rf"(?<![\w)²³])((?:{_PAREN})+|{_ATOM})\s*/\s*({_PAREN}|{_ATOM})")


def _strip(p: str) -> str:
    p = p.strip()
    if p.startswith("(") and p.endswith(")"):
        depth = 0
        for i, ch in enumerate(p):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(p) - 1:
                return p                      # (a)(b) — tashqi qavs butun ifodani o'ramaydi
        return p[1:-1].strip()
    return p


def tokens(text: str) -> list[tuple]:
    """[("t", matn) | ("f", surat, maxraj)]"""
    out: list[tuple] = []
    pos = 0
    marks = []
    for m in EXPL.finditer(text):
        marks.append((m.start(), m.end(), m.group(1).strip(), m.group(2).strip()))
    for m in AUTO.finditer(text):
        a, b = m.group(1), m.group(2)
        if not (a.startswith("(") or b.startswith("(")):
            continue
        if any(s <= m.start() < e for s, e, *_ in marks):
            continue
        marks.append((m.start(), m.end(), _strip(a), _strip(b)))
    for s, e, num, den in sorted(marks):
        if s < pos:
            continue
        if s > pos:
            out.append(("t", text[pos:s]))
        out.append(("f", num, den))
        pos = e
    if pos < len(text):
        out.append(("t", text[pos:]))
    return out


def has_fraction(text: str) -> bool:
    return any(t[0] == "f" for t in tokens(text or ""))


def plain(text: str) -> str:
    """Telegram matni uchun: [[a|b]] → (a)/(b)."""
    return EXPL.sub(lambda m: f"({m.group(1).strip()})/({m.group(2).strip()})", text or "")


class MathPara(Flowable):
    """Paragrafdek qatorlarga bo'linadigan matn; kasrlar ikki qavatli chiziladi."""

    def __init__(self, text: str, font: str, size: float, leading: float, indent: float = 0,
                 space_before: float = 0, color: str = "#000000"):
        super().__init__()
        self.font, self.size, self.leading, self.indent = font, size, leading, indent
        self.spaceBefore, self.color = space_before, color
        self.fs = size * 0.95                       # kasr ichidagi shrift
        self.items = []                             # ("w", so'z) | ("f", surat, maxraj)
        for t in tokens(text):
            if t[0] == "f":
                self.items.append(t)
            else:
                for part in re.split(r"(\s+)", t[1]):
                    if part:
                        self.items.append(("w", part))
        self.lines: list[list] = []

    def _w(self, it) -> float:
        if it[0] == "w":
            return stringWidth(it[1], self.font, self.size)
        return max(stringWidth(it[1], self.font, self.fs), stringWidth(it[2], self.font, self.fs)) + 8

    def wrap(self, aw, ah):
        width = aw - self.indent
        self.lines, cur, x = [], [], 0.0
        for it in self.items:
            w = self._w(it)
            if it[0] == "w" and not it[1].strip():
                if cur:
                    cur.append((it, w))
                    x += w
                continue
            if cur and x + w > width:
                while cur and cur[-1][0][0] == "w" and not cur[-1][0][1].strip():
                    cur.pop()
                self.lines.append(cur)
                cur, x = [], 0.0
            cur.append((it, w))
            x += w
        if cur:
            self.lines.append(cur)
        self.heights = [self._lh(ln) for ln in self.lines]
        self.width, self.height = aw, sum(self.heights)
        return aw, self.height

    def _lh(self, ln) -> float:
        return self.fs * 2 + 11 if any(it[0] == "f" for it, _ in ln) else self.leading

    def draw(self):
        c = self.canv
        c.setFillColor(self.color)
        c.setStrokeColor(self.color)
        y = self.height
        for ln, lh in zip(self.lines, self.heights):
            frac = lh != self.leading
            base = y - lh / 2 - self.size * 0.33 if frac else y - self.size
            x = self.indent
            for it, w in ln:
                if it[0] == "w":
                    c.setFont(self.font, self.size)
                    c.drawString(x, base, it[1])
                else:
                    mid = base + self.size * 0.33          # kasr chizig'i matn o'rtasida
                    c.setFont(self.font, self.fs)
                    nw, dw = stringWidth(it[1], self.font, self.fs), stringWidth(it[2], self.font, self.fs)
                    c.drawString(x + (w - nw) / 2, mid + 2.2, it[1])
                    c.drawString(x + (w - dw) / 2, mid - self.fs * 0.78 - 2.2, it[2])
                    c.setLineWidth(0.8)
                    c.line(x + 1.5, mid, x + w - 1.5, mid)
                x += w
            y -= lh
