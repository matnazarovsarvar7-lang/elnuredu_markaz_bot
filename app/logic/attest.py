"""Pedagog kadrlar attestatsiyasi — tayyorlov testi natijasini rasmiy chegaralar bilan izohlash.

Manba: Vazirlar Mahkamasining 17.09.2021 yildagi 572-son qarori bilan tasdiqlangan nizom (amaldagi tartib).
Ikki bosqich jami 100 ball: 1-bosqich — test (80 ball), 2-bosqich — pedagogik mahorat (20 ball).
Toifa bir attestatsiyada ko'pi bilan bir pog'ona oshadi. Chegaralar joriy toifaga bog'liq.
"""
from __future__ import annotations

STAGE1, STAGE2 = 80.0, 20.0

CATS = {
    "oliy": "Oliy toifa (bosh o'qituvchi)",
    "birinchi": "Birinchi toifa (yetakchi o'qituvchi)",
    "ikkinchi": "Ikkinchi toifa (katta o'qituvchi)",
    "mutaxassis": "Mutaxassis (oliy ma'lumotli o'qituvchi)",
}

# joriy toifa → [(kerakli jami ball, natija)] — yuqoridan pastga
RULES = {
    "oliy": [(80, "Oliy toifa saqlanadi")],
    "birinchi": [(80, "Oliy toifa beriladi"), (70, "Birinchi toifa saqlanadi")],
    "ikkinchi": [(70, "Birinchi toifa beriladi"), (60, "Ikkinchi toifa saqlanadi")],
    "mutaxassis": [(60, "Ikkinchi toifa beriladi"), (55, "Lavozim saqlanadi")],
}
FALL = {"oliy": "Birinchi toifaga tushiriladi", "birinchi": "Ikkinchi toifaga tushiriladi",
        "ikkinchi": "Mutaxassis lavozimiga tushiriladi", "mutaxassis": "Lavozimiga muvofiq emas deb topiladi"}

SPEC = ("nom: {title}\n1-35: Mutaxassislik fani | test 4 | 2\n36-40: Kasb standarti | test 4 | 2\nnatija: ball")


def to_stage1(total: float, total_max: float) -> float:
    """Testimiz bali → rasmiy 1-bosqich shkalasi (80 ball)."""
    return round(total / total_max * STAGE1, 1) if total_max else 0.0


def outlook(t80: float, cat: str) -> list[tuple[str, str]]:
    """[(natija, 2-bosqichda kerakli ball izohi)] — joriy toifa uchun."""
    out = []
    for need, label in RULES.get(cat, []):
        k = need - t80
        if k <= 0:
            out.append((label, "test bali yetarli ✅"))
        elif k <= STAGE2:
            out.append((label, f"2-bosqichda kamida {k:g} ball"))
        else:
            out.append((label, f"erishib bo'lmaydi (2-bosqich ko'pi bilan {STAGE2:g})"))
    return out


def text(total: float, total_max: float, cat: str | None) -> str:
    t80 = to_stage1(total, total_max)
    lines = [f"📋 <b>Attestatsiya bo'yicha</b>", f"1-bosqich (test) hisobida: <b>{t80:g} / {STAGE1:g}</b>"]
    if cat in RULES:
        lines.append(f"Joriy toifangiz: {CATS[cat]}")
        for label, need in outlook(t80, cat):
            lines.append(f"• {label} — {need}")
        lowest = RULES[cat][-1][0]
        if t80 + STAGE2 < lowest:
            lines.append(f"⚠️ 2-bosqichda maksimal ball bilan ham {lowest} ga yetmaydi: {FALL[cat].lower()}. "
                         "Tayyorgarlikni kuchaytirish tavsiya etiladi.")
    lines.append("<i>2-bosqich (pedagogik mahorat) — 20 ball. Chegaralar: VM 572-son qarori.</i>")
    return "\n".join(lines)
