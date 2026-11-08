"""Eski CRM'dan chiqarilgan davomat fayllarini o'qish (har fayl = bitta guruh)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import openpyxl

WEEKDAY_PATTERN = {frozenset({0, 2, 4}): "toq", frozenset({1, 3, 5}): "juft"}


@dataclass
class Row:
    raw_name: str
    name: str
    phone: str | None
    balance: int | None
    marks: list[str]
    section: str            # active | archived
    grade_hint: int | None = None
    phone_from_name: bool = False


@dataclass
class GroupFile:
    file: str
    group: str
    course: str
    teacher: str
    price: int | None
    days: str | None
    days_from_name: str | None
    dates: list[str]
    rows: list[Row] = field(default_factory=list)


def norm_phone(v) -> str | None:
    if v is None:
        return None
    d = re.sub(r"\D", "", str(v))
    if len(d) == 9:
        d = "998" + d
    return "+" + d if len(d) == 12 and d.startswith("998") else None


def clean_name(raw: str) -> tuple[str, int | None, str | None]:
    """'Aslbek4-sinf 2026 Sultonov' -> ('Aslbek Sultonov', 4, None); telefon nom ichida bo'lsa ajratadi."""
    s = str(raw)
    phone = None
    m = re.search(r"\(?(\d{9})\)?", s)
    if m:
        phone = "+998" + m.group(1)
    grade = None
    g = re.search(r"(\d{1,2})\s*-?\s*sinf", s, re.I)
    if g:
        grade = int(g.group(1))
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\d{1,2}\s*-?\s*sinf", " ", s, flags=re.I)
    s = re.sub(r"\b20\d\dy?\b", " ", s)
    s = re.sub(r"\d+", " ", s)
    s = s.replace("`", "'").replace("‘", "'").replace("’", "'")
    s = re.sub(r"\s+", " ", s).strip(" -")
    return s, grade, phone


def price_from_filename(name: str) -> int | None:
    m = re.search(r"(200|300|350)", name)
    return int(m.group(1)) * 1000 if m else None


def days_from_filename(name: str) -> str | None:
    n = name.lower()
    return "toq" if "toq" in n else "juft" if "juft" in n else None


def days_from_dates(dates: list[str], year: int = 2026) -> str | None:
    wd = set()
    for d in dates:
        try:
            day, mon = map(int, d.split("."))
            wd.add(date(year, mon, day).weekday())
        except Exception:
            continue
    return WEEKDAY_PATTERN.get(frozenset(wd))


def read_file(path: Path) -> GroupFile:
    ws = openpyxl.load_workbook(path, data_only=True).active
    meta, rows, section, dates = {}, [], None, []
    for vals in ws.iter_rows(values_only=True):
        vals = list(vals)
        first = vals[0]
        if isinstance(first, str) and first.strip().endswith(":"):
            val = next((v for v in vals[1:] if v not in (None, " ", "")), None)
            if val is not None:
                meta[first.strip().rstrip(":")] = str(val).strip()
            continue
        if isinstance(first, str) and first.strip() in ("Active students", "Archived students"):
            section = "active" if first.startswith("Active") else "archived"
            continue
        if isinstance(first, str) and first.strip() == "Name":
            dates = [str(v).strip() for v in vals[3:] if v not in (None, " ")]
            continue
        if section is None or not first or str(first).startswith("Arxivlangan"):
            continue
        phone = norm_phone(vals[1])
        if phone is None and isinstance(vals[1], (int, float)) and not isinstance(vals[1], bool):
            # telefon ustuni bo'sh — qolgan ustunlar bitta chapga surilgan
            balance, marks = int(vals[1]), [str(v).strip() for v in vals[2:]]
        else:
            balance = int(vals[2]) if isinstance(vals[2], (int, float)) else None
            marks = [str(v).strip() if v is not None else "" for v in vals[3:]]
        name, grade, phone_in_name = clean_name(first)
        rows.append(Row(str(first), name, phone or phone_in_name, balance, marks, section, grade,
                        phone is None and phone_in_name is not None))
    fname = path.name.split("-", 1)[1] if re.match(r"^[0-9a-f]{8}-", path.name) else path.name
    return GroupFile(fname, meta.get("Guruh nomi", ""), meta.get("Kurs nomi", ""), meta.get("O'qituvchi", ""),
                     price_from_filename(fname), days_from_dates(dates), days_from_filename(fname), dates, rows)


def name_key(name: str) -> str:
    s = name.lower().replace("'", "").replace("x", "h").replace("o'", "o").replace("g'", "g")
    return " ".join(sorted(s.split()))


@dataclass
class Person:
    name: str
    phone: str | None
    grade: int | None
    groups: list[tuple[GroupFile, Row]] = field(default_factory=list)


def consolidate(files: list[GroupFile], archived_active_groups: set[str] = frozenset()) -> list[Person]:
    """Bir xil telefon + bir xil ism = bitta o'quvchi. Bir telefon, turli ism = aka-uka (bitta ota-ona)."""
    people: dict[tuple, Person] = {}
    for g in files:
        for r in g.rows:
            if r.section == "archived" and g.group not in archived_active_groups:
                continue
            key = (r.phone, name_key(r.name))
            p = people.get(key)
            if not p and r.phone is None:  # telefonsiz: faqat ism bo'yicha
                p = next((x for k, x in people.items() if k[1] == key[1]), None)
            if not p:
                p = people[key] = Person(r.name, r.phone, r.grade_hint)
            p.grade = p.grade or r.grade_hint
            p.groups.append((g, r))
    return list(people.values())
