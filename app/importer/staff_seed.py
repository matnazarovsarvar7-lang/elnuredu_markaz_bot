"""Xodimlar ro'yxatini (data/staff.json) bazaga yozish va yordamchilarni guruhlarga biriktirish."""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, Staff


async def seed_staff(s: AsyncSession, path: Path) -> list[str]:
    out = []
    data = json.loads(path.read_text(encoding="utf-8"))
    by_name: dict[str, Staff] = {}
    for x in data:
        st = (await s.execute(select(Staff).where(Staff.full_name == x["full_name"]))).scalar_one_or_none()
        if not st:
            st = Staff(full_name=x["full_name"])
            s.add(st)
        st.phone, st.roles = x["phone"], x["roles"]
        st.pay_type, st.percent, st.oklad = x["pay_type"], x["percent"], x["oklad"]
        st.current_role = st.role_list()[0]
        by_name[x["full_name"]] = st
    await s.flush()
    for x in data:
        if x.get("assists"):
            teacher = by_name[x["assists"]]
            groups = (await s.execute(select(Group).where(Group.teacher_id == teacher.id))).scalars().all()
            for g in groups:
                g.assistant_id = by_name[x["full_name"]].id
            out.append(f"{x['full_name']} → {teacher.full_name}: {len(groups)} guruh")
    return out
