"""Boshlang'ich ma'lumotlar paketi (JSON): kodda saqlanmaydi, rahbar botga fayl qilib yuboradi."""
from __future__ import annotations

import json
from datetime import date, time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Enrollment, Group, Parent, Staff, Student


async def export_bundle(s: AsyncSession) -> dict:
    staff = (await s.execute(select(Staff))).scalars().all()
    groups = (await s.execute(select(Group))).scalars().all()
    students = (await s.execute(select(Student).options(selectinload(Student.parents),
                                                        selectinload(Student.enrollments)))).scalars().all()
    sid = {x.id: x.full_name for x in staff}
    gid = {g.id: g.name for g in groups}
    return {
        "version": 1,
        "staff": [{"full_name": x.full_name, "phone": x.phone, "roles": x.roles, "pay_type": x.pay_type,
                   "percent": x.percent, "oklad": x.oklad} for x in staff],
        "groups": [{"name": g.name, "subject": g.subject, "teacher": sid.get(g.teacher_id),
                    "assistant": sid.get(g.assistant_id), "days": g.days,
                    "start": g.start_time.strftime("%H:%M") if g.start_time else None,
                    "end": g.end_time.strftime("%H:%M") if g.end_time else None, "room": g.room} for g in groups],
        "students": [{"full_name": st.full_name, "tier": st.tier, "grade": st.grade, "phone": st.phone,
                      "parents": [p.phone for p in st.parents],
                      "enrollments": [{"group": gid[e.group_id], "join": e.join_date.isoformat(),
                                       "registered": e.registered_at.isoformat(), "price": e.price_override,
                                       "discount": e.discount_kind, "percent": e.discount_percent}
                                      for e in st.enrollments if e.active]} for st in students],
    }


def _t(v: str | None) -> time | None:
    return time(*map(int, v.split(":"))) if v else None


async def import_bundle(s: AsyncSession, data: dict) -> dict:
    if (await s.execute(select(func.count(Student.id)))).scalar():
        raise ValueError("Bazada allaqachon o'quvchilar bor — paket faqat bo'sh bazaga yuklanadi.")
    staff = {}
    for x in data["staff"]:
        st = None
        if x["phone"]:
            st = (await s.execute(select(Staff).where(Staff.phone == x["phone"]))).scalar_one_or_none()
        st = st or (await s.execute(select(Staff).where(Staff.full_name == x["full_name"]))).scalar_one_or_none() or Staff()
        for k in ("full_name", "phone", "roles", "pay_type", "percent", "oklad"):
            setattr(st, k, x[k])
        st.current_role = st.role_list()[0] if st.role_list() else None
        s.add(st)
        staff[x["full_name"]] = st
    await s.flush()
    groups = {}
    for g in data["groups"]:
        grp = Group(name=g["name"], subject=g["subject"], days=g["days"], room=g["room"],
                    start_time=_t(g["start"]), end_time=_t(g["end"]),
                    teacher_id=staff[g["teacher"]].id if g["teacher"] else None,
                    assistant_id=staff[g["assistant"]].id if g["assistant"] else None)
        s.add(grp)
        groups[g["name"]] = grp
    await s.flush()
    parents: dict[str, Parent] = {}
    n_enr = 0
    for x in data["students"]:
        stu = Student(full_name=x["full_name"], tier=x["tier"], grade=x["grade"], phone=x["phone"])
        for ph in x["parents"]:
            parents.setdefault(ph, Parent(phone=ph))
            stu.parents.append(parents[ph])
        s.add(stu)
        await s.flush()
        for e in x["enrollments"]:
            s.add(Enrollment(student_id=stu.id, group_id=groups[e["group"]].id, join_date=date.fromisoformat(e["join"]),
                             registered_at=date.fromisoformat(e["registered"]), price_override=e["price"],
                             discount_kind=e["discount"], discount_percent=e["percent"]))
            n_enr += 1
    await s.flush()
    return {"xodimlar": len(staff), "guruhlar": len(groups), "o'quvchilar": len(data["students"]),
            "o'rinlar": n_enr, "ota-onalar": len(parents)}
