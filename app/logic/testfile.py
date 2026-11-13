"""Test faylini o'qish va tekshirish (prompt beradigan format).

#TEST
fan: ...
savollar_soni: 30
variantlar: 4
umumiy: 5
#SAVOL
id: 1
variant: umumiy | A | B | C | D
qiyinlik: oson | orta | qiyin
manba: ...
savol: ...
chizma:            (ixtiyoriy — SVG, keyingi qatordan </svg> gacha)
<svg ...> ... </svg>
A) ...
B) ...
C) ...
D) ...
javob: C
#OXIRI
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

LETTERS = "ABCD"
VARIANTS = "ABCD"
LEVELS = {"oson", "orta", "o'rta", "qiyin"}


@dataclass
class Q:
    qid: int
    variant: str            # "umumiy" yoki "A".."D"
    level: str
    source: str
    text: str
    options: list[str]
    answer: str             # "A".."D"
    figure: str = ""        # SVG chizma (bo'lmasa — bo'sh)


@dataclass
class TestFile:
    meta: dict
    questions: list[Q] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return int(self.meta.get("savollar_soni", 0) or 0)

    @property
    def is_bank(self) -> bool:
        """Bank rejimi: har o'quvchiga shaxsiy savollar to'plami (variantsiz)."""
        return str(self.meta.get("rejim", "")).strip().lower() == "bank"

    @property
    def variants(self) -> int:
        return 0 if self.is_bank else int(self.meta.get("variantlar", 1) or 1)

    @property
    def overlap(self) -> int:
        v = str(self.meta.get("takror", "")).strip()
        return int(v) if v.isdigit() else max(1, round(self.n / 6))

    @property
    def common(self) -> int:
        return int(self.meta.get("umumiy", 0) or 0)

    def variant_questions(self, v: str) -> list[Q]:
        """Variant savollari: umumiylar + shu variantniki (variant 1 ta bo'lsa yoki bank — hammasi)."""
        if self.variants <= 1:
            return list(self.questions)
        return [q for q in self.questions if q.variant in ("umumiy", v)]


def _norm(s: str) -> str:
    return s.replace("‘", "'").replace("’", "'").replace("ʼ", "'").replace("ʻ", "'")


def parse(raw: str) -> TestFile:
    text = _norm(raw.replace("\r\n", "\n").replace("\r", "\n")).strip("﻿ \n")
    tf = TestFile(meta={})
    if "#TEST" not in text:
        tf.errors.append("Faylda «#TEST» sarlavhasi yo'q — format noto'g'ri.")
        return tf
    body = text.split("#OXIRI")[0]
    parts = re.split(r"^\s*#SAVOL\s*$", body, flags=re.M)
    head = parts[0].split("#TEST", 1)[1]
    for line in head.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            tf.meta[k.strip().lower()] = v.strip()
    for key in ("savollar_soni", "variantlar", "umumiy", "takror"):
        if key in tf.meta and not str(tf.meta[key]).isdigit():
            tf.errors.append(f"Sarlavhada «{key}» raqam bo'lishi kerak.")
    if tf.errors:
        return tf
    tf.meta.setdefault("variantlar", "1")
    tf.meta.setdefault("umumiy", "0")
    for i, block in enumerate(parts[1:], 1):
        q = _parse_q(block, i, tf.errors)
        if q:
            tf.questions.append(q)
    _validate(tf)
    return tf


def _parse_q(block: str, i: int, errors: list[str]) -> Q | None:
    fields: dict[str, str] = {}
    opts: dict[str, str] = {}
    cur = None
    svg: list[str] | None = None
    for line in block.strip().splitlines():
        if svg is not None:                      # chizma ichida: </svg> gacha hammasi SVG
            svg.append(line)
            if "</svg>" in line.lower():
                fields["chizma"] = "\n".join(svg).strip()
                svg, cur = None, None
            continue
        m_fig = re.match(r"^\s*chizma\s*:\s*(.*)$", line, flags=re.I)
        if m_fig:
            rest = m_fig.group(1).strip()
            svg = [rest] if rest else []
            if rest and "</svg>" in rest.lower():
                fields["chizma"], svg = rest, None
            continue
        m_opt = re.match(r"^\s*([A-Da-d])\s*[\)\.]\s*(.+)$", line)
        m_key = re.match(r"^\s*(id|variant|qiyinlik|manba|savol|javob)\s*:\s*(.*)$", line, flags=re.I)
        if m_key:
            cur = m_key.group(1).lower()
            fields[cur] = m_key.group(2).strip()
        elif m_opt and "savol" in fields:
            cur = "opt:" + m_opt.group(1).upper()
            opts[m_opt.group(1).upper()] = m_opt.group(2).strip()
        elif line.strip() and cur:
            if cur.startswith("opt:"):
                opts[cur[4:]] += " " + line.strip()
            else:
                fields[cur] += "\n" + line.strip()
    label = f"{fields.get('id', i)}-savol"
    bad = []
    if svg is not None:
        bad.append("chizma </svg> bilan yopilmagan")
    if fields.get("chizma"):
        from app.logic.figures import check
        err = check(fields["chizma"])
        if err:
            bad.append(err)
    if not fields.get("savol"):
        bad.append("savol matni yo'q")
    missing = [x for x in LETTERS if not opts.get(x)]
    if missing:
        bad.append("javob varianti yo'q: " + ", ".join(missing))
    ans = (fields.get("javob") or "").strip().upper()[:1]
    if ans not in LETTERS:
        bad.append("«javob:» A, B, C yoki D bo'lishi kerak")
    variant = (fields.get("variant") or "umumiy").strip()
    variant = variant.lower() if variant.lower() in ("umumiy", "bank") else variant.upper()
    if variant not in ("umumiy", "bank") and variant not in VARIANTS:
        bad.append(f"variant «{variant}» noto'g'ri")
    level = (fields.get("qiyinlik") or "oson").strip().lower().replace("o'rta", "orta")
    if level not in LEVELS:
        bad.append(f"qiyinlik «{level}» noto'g'ri")
    if bad:
        errors.append(f"{label}: " + "; ".join(bad))
        return None
    try:
        qid = int(fields.get("id", i))
    except ValueError:
        qid = i
    return Q(qid, variant, level, fields.get("manba", ""), fields["savol"], [opts[x] for x in LETTERS], ans,
             fields.get("chizma", ""))


def _validate(tf: TestFile) -> None:
    if not tf.questions:
        tf.errors.append("Birorta ham to'g'ri savol topilmadi.")
        return
    ids = [q.qid for q in tf.questions]
    dup = sorted({x for x in ids if ids.count(x) > 1})
    if dup:
        tf.errors.append("Takroriy id: " + ", ".join(map(str, dup)))
    v, n, c = tf.variants, tf.n, tf.common
    if tf.is_bank:
        if not n:
            tf.errors.append("Bank rejimida «savollar_soni» (bir o'quvchiga) yozilishi kerak.")
        elif len(tf.questions) < n:
            tf.errors.append(f"Bankda {len(tf.questions)} ta savol — bitta o'quvchiga {n} ta kerak.")
        lv = level_counts(tf.questions)
        for L, share in (("oson", 0.5), ("orta", 0.3), ("qiyin", 0.2)):
            if n and lv[L] < round(n * share):
                tf.errors.append(f"Bankda «{L}» savollar {lv[L]} ta — bir o'quvchiga {round(n * share)} ta kerak.")
        tf.meta["variantlar"], tf.meta["umumiy"] = "0", "0"
    elif not 1 <= v <= 4:
        tf.errors.append("Variantlar soni 1 dan 4 gacha bo'lishi kerak.")
        return
    elif v == 1:
        if n and len(tf.questions) != n:
            tf.errors.append(f"savollar_soni: {n}, lekin faylda {len(tf.questions)} ta savol.")
        tf.meta["savollar_soni"] = str(len(tf.questions))
    else:
        common = [q for q in tf.questions if q.variant == "umumiy"]
        if len(common) != c:
            tf.errors.append(f"Umumiy savollar {c} ta bo'lishi kerak, faylda {len(common)} ta.")
        for x in VARIANTS[:v]:
            own = [q for q in tf.questions if q.variant == x]
            if len(own) != n - c:
                tf.errors.append(f"{x}-variantda {n - c} ta savol bo'lishi kerak, faylda {len(own)} ta.")
        extra = [q for q in tf.questions if q.variant not in ("umumiy", *VARIANTS[:v])]
        if extra:
            tf.errors.append(f"{len(extra)} ta savol mavjud bo'lmagan variantga yozilgan.")
    # ogohlantirishlar
    texts = {}
    for q in tf.questions:
        key = re.sub(r"\W+", "", q.text.lower())
        if key in texts:
            tf.warnings.append(f"{q.qid}-savol {texts[key]}-savolga juda o'xshash.")
        texts[key] = q.qid
        if len(set(o.strip().lower() for o in q.options)) < 4:
            tf.warnings.append(f"{q.qid}-savolda bir xil javob variantlari bor.")


def level_counts(qs: list[Q]) -> dict[str, int]:
    out = {"oson": 0, "orta": 0, "qiyin": 0}
    for q in qs:
        out["orta" if q.level in ("orta", "o'rta") else q.level] += 1
    return out


def render(meta: dict, questions: list[Q]) -> str:
    """Saqlangan testni qayta fayl ko'rinishiga keltiradi (tuzatishdan keyin yuborish uchun)."""
    lines = ["#TEST"] + [f"{k}: {v}" for k, v in meta.items()]
    for q in questions:
        lines += ["#SAVOL", f"id: {q.qid}", f"variant: {q.variant}", f"qiyinlik: {q.level}",
                  f"manba: {q.source}", f"savol: {q.text}"]
        if q.figure:
            lines += ["chizma:", q.figure]
        lines += [f"{x}) {o}" for x, o in zip(LETTERS, q.options)]
        lines.append(f"javob: {q.answer}")
    lines.append("#OXIRI")
    return "\n".join(lines) + "\n"
