"""Eski CRM fayllarini bot bazasiga yuklash."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Enrollment, Group, Parent, Staff, Student
from app.importer.crm_export import consolidate, read_file

TIER_BY_PRICE = {200_000: 1, 300_000: 2, 350_000: 3}


async def load_folder(s: AsyncSession, folder: Path, join: date, registered: date,
                      archived_active_groups: set[str] = frozenset(),
                      days_override: dict[str, str] | None = None,
                      merge_into: dict[str, str] | None = None) -> dict:
    """merge_into: {asl nom: yakuniy nom} — narx bo'yicha bo'lingan fayllar bitta guruhga birlashadi."""
    merge_into = merge_into or {}
    files = [read_file(p) for p in sorted(folder.glob("*.xlsx"))]
    days_override = days_override or {}
    teachers: dict[str, Staff] = {}
    by_final: dict[str, Group] = {}
    groups: dict[str, Group] = {}
    for f in files:
        tname = " ".join(f.teacher.split())
        if tname not in teachers:
            t = (await s.execute(select(Staff).where(Staff.full_name == tname))).scalar_one_or_none()
            teachers[tname] = t or Staff(full_name=tname, roles="oqituvchi")
            s.add(teachers[tname])
        orig = " ".join(f.group.split())
        final = merge_into.get(orig, orig)
        if final not in by_final:
            by_final[final] = Group(name=final, subject=f.course.strip(), teacher=teachers[tname],
                                    days=days_override.get(final, days_override.get(orig, f.days or f.days_from_name or "toq")))
            s.add(by_final[final])
        groups[f.file] = by_final[final]
    await s.flush()
    people = consolidate(files, archived_active_groups)
    parents: dict[str, Parent] = {}
    n_enr = 0
    for p in people:
        prices = [g.price for g, _ in p.groups if g.price]
        stu = Student(full_name=p.name, grade=p.grade,
                      tier=TIER_BY_PRICE.get(max(prices, key=prices.count) if prices else 300_000, 2),
                      opening_balance=next((r.balance for _, r in p.groups if r.balance is not None), None))
        if p.phone:
            par = parents.get(p.phone)
            if not par:
                par = parents[p.phone] = Parent(phone=p.phone)
                s.add(par)
            stu.parents.append(par)
        s.add(stu)
        await s.flush()
        seen = set()
        for gf, _ in p.groups:
            if groups[gf.file].id in seen:
                continue
            seen.add(groups[gf.file].id)
            s.add(Enrollment(student_id=stu.id, group_id=groups[gf.file].id, join_date=join,
                             registered_at=registered, price_override=gf.price))
            n_enr += 1
    await s.flush()
    return {"groups": len({g.id for g in groups.values()}), "teachers": len(teachers), "students": len(people),
            "enrollments": n_enr, "parents": len(parents)}
