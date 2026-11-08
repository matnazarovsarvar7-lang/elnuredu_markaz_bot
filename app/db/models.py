"""Ma'lumotlar bazasi jadvallari (1-bosqich)."""
from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import (JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String,
                        Text, Time, UniqueConstraint, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


ROLES = ("rahbar", "moliya", "admin", "oqituvchi", "yordamchi")


class Staff(Base):
    __tablename__ = "staff"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120), unique=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True)  # noma'lum bo'lsa keyin kiritiladi
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    roles: Mapped[str] = mapped_column(String(120), default="")  # "rahbar,oqituvchi"
    current_role: Mapped[str | None] = mapped_column(String(20))
    pay_type: Mapped[str | None] = mapped_column(String(10))     # foiz | fiksal | ikkalasi
    percent: Mapped[int | None] = mapped_column(Integer)
    oklad: Mapped[int | None] = mapped_column(Integer)
    birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(6))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    def role_list(self) -> list[str]:
        return [r for r in self.roles.split(",") if r]


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    subject: Mapped[str | None] = mapped_column(String(60))
    teacher_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    assistant_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    delegated: Mapped[dict] = mapped_column(JSON, default=dict)  # {"attendance": true, ...}
    days: Mapped[str] = mapped_column(String(6), default="toq")  # toq | juft | har
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    room: Mapped[str | None] = mapped_column(String(30))
    capacity: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    teacher: Mapped[Staff | None] = relationship(foreign_keys=[teacher_id])


class Student(Base):
    __tablename__ = "students"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    tier: Mapped[int] = mapped_column(Integer, default=2)        # 1=200k, 2=300k, 3=350k
    grade: Mapped[int | None] = mapped_column(Integer)
    birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(6))
    school: Mapped[str | None] = mapped_column(String(60))
    phone: Mapped[str | None] = mapped_column(String(20))
    tg_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(10), default="faol")  # faol | muzlatilgan | ketgan
    coins: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str | None] = mapped_column(String(60))
    opening_balance: Mapped[int | None] = mapped_column(Integer)   # eski tizimdagi balans (ma'lumot uchun)
    note: Mapped[str | None] = mapped_column(Text)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime)
    bot_blocked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    parents: Mapped[list["Parent"]] = relationship(secondary="student_parents", back_populates="children")
    enrollments: Mapped[list["Enrollment"]] = relationship(back_populates="student")


class Parent(Base):
    __tablename__ = "parents"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20), unique=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger)
    lang: Mapped[str] = mapped_column(String(5), default="uz")
    bot_blocked_at: Mapped[datetime | None] = mapped_column(DateTime)
    children: Mapped[list[Student]] = relationship(secondary="student_parents", back_populates="parents")


class StudentParent(Base):
    __tablename__ = "student_parents"
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("parents.id"), primary_key=True)


class Enrollment(Base):
    """O'quvchining bitta guruhdagi o'rni — har biri alohida to'lanadi."""
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("student_id", "group_id", "join_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    join_date: Mapped[date] = mapped_column(Date)
    registered_at: Mapped[date] = mapped_column(Date)
    price_override: Mapped[int | None] = mapped_column(Integer)
    discount_kind: Mapped[str] = mapped_column(String(10), default="none")
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    left_date: Mapped[date | None] = mapped_column(Date)
    student: Mapped[Student] = relationship(back_populates="enrollments")
    group: Mapped[Group] = relationship()


class Lesson(Base):
    __tablename__ = "lessons"
    __table_args__ = (UniqueConstraint("group_id", "date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    date: Mapped[date] = mapped_column(Date)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    homework_text: Mapped[str | None] = mapped_column(Text)
    no_homework_reason: Mapped[str | None] = mapped_column(String(200))
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)


class Attendance(Base):
    __tablename__ = "attendance"
    __table_args__ = (UniqueConstraint("lesson_id", "enrollment_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id"))
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"))
    mark: Mapped[str | None] = mapped_column(String(1))       # k | l | y
    homework: Mapped[str | None] = mapped_column(String(4))   # done | part | none
    grade: Mapped[str | None] = mapped_column(String(20))
    absence_reason: Mapped[str | None] = mapped_column(String(120))


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"))
    month: Mapped[str] = mapped_column(String(7))            # "2026-12"
    amount: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(5))           # naqd | karta | ijtimoiy
    status: Mapped[str] = mapped_column(String(10), default="tasdiq")  # kutilmoqda | tasdiq | rad
    paid_date: Mapped[date] = mapped_column(Date)
    receipt_file_id: Mapped[str | None] = mapped_column(String(200))
    receipt_hash: Mapped[str | None] = mapped_column(String(64))
    entered_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    coins_given: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CoinTx(Base):
    __tablename__ = "coin_tx"
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    amount: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12))   # davomat | vazifa | tolov | bonus | qolda | market
    reason: Mapped[str] = mapped_column(String(200))
    by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    role: Mapped[str | None] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(60))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
