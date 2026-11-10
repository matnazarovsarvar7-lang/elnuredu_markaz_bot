"""Ma'lumotlar bazasi jadvallari (1-bosqich)."""
from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import (JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, String,
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
    pay_type: Mapped[str | None] = mapped_column(String(20))     # foiz | fiksal | ikkalasi
    percent: Mapped[int | None] = mapped_column(Integer)
    oklad: Mapped[int | None] = mapped_column(Integer)
    birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(20))
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
    days: Mapped[str] = mapped_column(String(20), default="toq")  # toq | juft | har
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
    gender: Mapped[str | None] = mapped_column(String(20))
    school: Mapped[str | None] = mapped_column(String(60))
    region: Mapped[str | None] = mapped_column(String(80))             # viloyat, tuman
    phone: Mapped[str | None] = mapped_column(String(20))
    tg_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="faol")  # faol | muzlatilgan | ketgan
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
    lang: Mapped[str] = mapped_column(String(20), default="uz")
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
    price_override: Mapped[int | None] = mapped_column(Integer)   # faqat istisno narx
    tier: Mapped[int | None] = mapped_column(Integer)             # 1/2/3 — narx toifadan (tarix bilan)
    discount_kind: Mapped[str] = mapped_column(String(20), default="none")
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
    homework_text: Mapped[str | None] = mapped_column(Text)
    no_homework_reason: Mapped[str | None] = mapped_column(String(200))
    homework_file_id: Mapped[str | None] = mapped_column(String(200))
    homework_file_kind: Mapped[str | None] = mapped_column(String(20))   # photo | doc
    homework_at: Mapped[datetime | None] = mapped_column(DateTime)      # vazifa (yoki "yo'q") kiritilgan vaqt
    homework_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)         # "Dars tugadi" bosilgan vaqt
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    cancel_status: Mapped[str | None] = mapped_column(String(20))      # sorov | tasdiq | rad
    makeup_date: Mapped[date | None] = mapped_column(Date)             # bekor darsning o'rniga
    extra: Mapped[bool] = mapped_column(Boolean, default=False)        # qo'shimcha (o'rniga) dars
    makeup_penalized: Mapped[bool] = mapped_column(Boolean, default=False)


class Attendance(Base):
    __tablename__ = "attendance"
    __table_args__ = (UniqueConstraint("lesson_id", "enrollment_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id"))
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"))
    mark: Mapped[str | None] = mapped_column(String(20))       # k | l | y
    homework: Mapped[str | None] = mapped_column(String(20))   # done | part | none
    grade: Mapped[str | None] = mapped_column(String(20))
    absence_reason: Mapped[str | None] = mapped_column(String(120))


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"))
    month: Mapped[str] = mapped_column(String(20))            # "2026-12"
    amount: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(20))           # naqd | karta | ijtimoiy
    status: Mapped[str] = mapped_column(String(20), default="tasdiq")  # kutilmoqda | tasdiq | rad
    paid_date: Mapped[date] = mapped_column(Date)
    receipt_file_id: Mapped[str | None] = mapped_column(String(200))
    receipt_hash: Mapped[str | None] = mapped_column(String(64))
    batch: Mapped[str | None] = mapped_column(String(36), index=True)  # bitta to'lov bir necha guruhga bo'linganda
    entered_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    coins_given: Mapped[int] = mapped_column(Integer, default=0)
    void_of: Mapped[int | None] = mapped_column(Integer)              # bekor qilish yozuvi: asl yozuv id
    void_reason: Mapped[str | None] = mapped_column(String(300))       # bekor qilish sababi (so'rov / qaror)
    void_req_by: Mapped[int | None] = mapped_column(Integer)           # so'ragan xodim
    voided_at: Mapped[datetime | None] = mapped_column(DateTime)       # asl yozuv bekor qilingan vaqt
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CoinTx(Base):
    __tablename__ = "coin_tx"
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    amount: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))   # davomat | vazifa | tolov | bonus | qolda | market
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


class Task(Base):
    """Rahbar xodimga beradigan qo'shimcha vazifa."""
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    text: Mapped[str] = mapped_column(Text)
    due: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="ochiq")   # ochiq | bajarildi | bekor
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    done_note: Mapped[str | None] = mapped_column(Text)
    overdue_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    file_id: Mapped[str | None] = mapped_column(String(200))
    file_kind: Mapped[str | None] = mapped_column(String(20))      # photo | doc
    done_file_id: Mapped[str | None] = mapped_column(String(200))
    done_file_kind: Mapped[str | None] = mapped_column(String(20))
    recurring_id: Mapped[int | None] = mapped_column(ForeignKey("recurring_tasks.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RecurringTask(Base):
    """Takrorlanadigan vazifa: har hafta (kun) yoki har oy (sana)."""
    __tablename__ = "recurring_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))        # weekly | monthly
    day: Mapped[int] = mapped_column(Integer)           # haftada: 0=Du..6=Ya; oyda: 1..28
    due_days: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_created: Mapped[date | None] = mapped_column(Date)


class Lead(Base):
    """Botga yozgan yangi qiziquvchi (kursga yozilish arizasi)."""
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger)
    phone: Mapped[str | None] = mapped_column(String(20))
    child_name: Mapped[str] = mapped_column(String(120))
    subject: Mapped[str | None] = mapped_column(String(60))
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="yangi")  # yangi | boglanildi | oquvchi | rad
    handled_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Exam(Base):
    """Haftalik nazorat. Bot oldindan rejalashtiradi; o'tkazilgach natijalar kiritiladi."""
    __tablename__ = "exams"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    date: Mapped[date] = mapped_column(Date)
    week: Mapped[date] = mapped_column(Date)                      # haftaning dushanbasi
    slot: Mapped[int] = mapped_column(Integer, default=1)         # 1 yoki 2
    conductor_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    kind: Mapped[str | None] = mapped_column(String(20))          # internet | yozma | test
    max_score: Mapped[float | None] = mapped_column(Float)
    topic: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="reja")  # reja | otdi | otkazilmadi
    moved_note: Mapped[str | None] = mapped_column(String(200))
    monthly: Mapped[bool] = mapped_column(Boolean, default=False)
    test_id: Mapped[int | None] = mapped_column(Integer)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ExamResult(Base):
    __tablename__ = "exam_results"
    __table_args__ = (UniqueConstraint("exam_id", "enrollment_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"))
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"))
    score: Mapped[float | None] = mapped_column(Float)            # None = qatnashmadi
    percent: Mapped[float | None] = mapped_column(Float)
    answers: Mapped[str | None] = mapped_column(String(400))      # bot testi uchun
    coins_given: Mapped[int] = mapped_column(Integer, default=0)
    appeals: Mapped[int] = mapped_column(Integer, default=0)


class Appeal(Base):
    __tablename__ = "appeals"
    id: Mapped[int] = mapped_column(primary_key=True)
    result_id: Mapped[int] = mapped_column(ForeignKey("exam_results.id"))
    from_tg: Mapped[int] = mapped_column(BigInteger)
    question_no: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="yangi")  # yangi | qabul | rad
    reply: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StaffLeave(Base):
    """Rahbar bergan ruxsat (shu kuni jarima yo'q, nazorat ko'chadi)."""
    __tablename__ = "staff_leaves"
    __table_args__ = (UniqueConstraint("staff_id", "date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(String(200))
    by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))


class StaffPoint(Base):
    """Xodim bonus/jarima ballari (oylik hisob 2-bosqichda shundan foydalanadi)."""
    __tablename__ = "staff_points"
    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    points: Mapped[int] = mapped_column(Integer)
    role: Mapped[str | None] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(200))
    date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Test(Base):
    """Nazorat testi: o'qituvchi so'rovi -> rahbar yuklaydi -> o'qituvchi tasdiqlaydi -> varaqlar."""
    __tablename__ = "tests"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    exam_id: Mapped[int | None] = mapped_column(ForeignKey("exams.id"))
    topic: Mapped[str] = mapped_column(String(200))
    n_questions: Mapped[int] = mapped_column(Integer, default=20)
    n_variants: Mapped[int] = mapped_column(Integer, default=4)
    common: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    photos: Mapped[list] = mapped_column(JSON, default=list)          # so'rov rasmlari (file_id)
    students: Mapped[list | None] = mapped_column(JSON)               # None = butun guruh; aks holda enrollment id lar
    status: Mapped[str] = mapped_column(String(20), default="sorov")  # sorov | korib | qaytarildi | tasdiq
    source: Mapped[str] = mapped_column(String(20), default="rahbar") # rahbar | oqituvchi
    version: Mapped[int] = mapped_column(Integer, default=0)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    review_note: Mapped[str | None] = mapped_column(Text)
    printed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TestQuestion(Base):
    __tablename__ = "test_questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id"))
    qid: Mapped[int] = mapped_column(Integer)
    variant: Mapped[str] = mapped_column(String(20))
    level: Mapped[str] = mapped_column(String(20))
    source: Mapped[str | None] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON)
    answer: Mapped[str] = mapped_column(String(20))


class TestPaper(Base):
    """Bitta o'quvchining varag'i: variant, tartib, kalit, javoblar."""
    __tablename__ = "test_papers"
    id: Mapped[int] = mapped_column(primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id"))
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"))
    code: Mapped[str] = mapped_column(String(20))
    variant: Mapped[str] = mapped_column(String(20))
    qids: Mapped[list] = mapped_column(JSON)
    perms: Mapped[list] = mapped_column(JSON)
    key: Mapped[str] = mapped_column(String(100))
    answers: Mapped[str | None] = mapped_column(String(100))
    correct: Mapped[int | None] = mapped_column(Integer)
    absent: Mapped[bool] = mapped_column(Boolean, default=False)
    entered_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    scanned: Mapped[bool] = mapped_column(Boolean, default=False)


class Freeze(Base):
    """Muzlatish: bitta o'quvchi-guruh (enrollment_id) yoki butun guruh (group_id, masalan yozgi to'xtash)."""
    __tablename__ = "freezes"
    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int | None] = mapped_column(ForeignKey("enrollments.id"))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"))
    start: Mapped[date] = mapped_column(Date)
    end: Mapped[date] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(String(200))
    recalc: Mapped[str] = mapped_column(String(20), default="auto")   # auto | kutilmoqda | tasdiq | rad
    by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CashClose(Base):
    """Kunlik kassa yopish ("ko'r" usul): moliya sanagan naqdni kiritadi, bot faqat mos/farq deydi."""
    __tablename__ = "cash_closes"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date)
    expected: Mapped[int] = mapped_column(Integer)
    counted: Mapped[int] = mapped_column(Integer)
    diff: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))        # boshlang | mos | farq
    decision: Mapped[str | None] = mapped_column(String(20))
    by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CashHandover(Base):
    """Inkassatsiya: naqdni rahbarga yoki bankka topshirish."""
    __tablename__ = "cash_handovers"
    id: Mapped[int] = mapped_column(primary_key=True)
    amount: Mapped[int] = mapped_column(Integer)
    to_kind: Mapped[str] = mapped_column(String(20))         # rahbar | bank
    status: Mapped[str] = mapped_column(String(20), default="kutilmoqda")  # kutilmoqda | tasdiq | rad
    receipt_file_id: Mapped[str | None] = mapped_column(String(200))
    by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Product(Base):
    """Market sovg'asi."""
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    coins: Mapped[int] = mapped_column(Integer)
    cost: Mapped[int] = mapped_column(Integer, default=0)          # xarid narxi, so'm
    stock: Mapped[int] = mapped_column(Integer, default=0)
    photo_file_id: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    coins: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="band")   # band | tasdiq | rad | topshirildi
    from_tg: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(String(200))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)


class StockCount(Base):
    """Oylik ombor sanog'i."""
    __tablename__ = "stock_counts"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    expected: Mapped[int] = mapped_column(Integer)
    counted: Mapped[int] = mapped_column(Integer)
    by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())



class Expense(Base):
    """Xarajat: kichik kassadan (moliya, limit ichida) yoki asosiy kassadan (rahbar tasdig'i bilan)."""
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date)
    category: Mapped[str] = mapped_column(String(40))
    amount: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(300))
    method: Mapped[str] = mapped_column(String(20), default="naqd")    # naqd | karta | bank
    source: Mapped[str] = mapped_column(String(20), default="kichik")  # kichik | asosiy
    status: Mapped[str] = mapped_column(String(20), default="tasdiq") # kutilmoqda | tasdiq | rad
    receipt_file_id: Mapped[str | None] = mapped_column(String(200))
    by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    void_of: Mapped[int | None] = mapped_column(Integer)              # bekor qilish yozuvi: asl yozuv id
    void_reason: Mapped[str | None] = mapped_column(String(300))       # bekor qilish sababi (so'rov / qaror)
    void_req_by: Mapped[int | None] = mapped_column(Integer)           # so'ragan xodim
    voided_at: Mapped[datetime | None] = mapped_column(DateTime)       # asl yozuv bekor qilingan vaqt
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Service(Base):
    """Pullik xizmat: kitob (omborli), printer (sahifa), boshqa."""
    __tablename__ = "services"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))                 # kitob | printer | boshqa
    price: Mapped[int] = mapped_column(Integer)                  # sotuv narxi (so'm)
    cost: Mapped[int] = mapped_column(Integer, default=0)        # tannarx (so'm)
    stock: Mapped[int | None] = mapped_column(Integer)           # kitob uchun
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ServiceSale(Base):
    __tablename__ = "service_sales"
    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    qty: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[int] = mapped_column(Integer)
    profit: Mapped[int] = mapped_column(Integer, default=0)
    method: Mapped[str] = mapped_column(String(20), default="naqd")   # naqd | karta
    status: Mapped[str] = mapped_column(String(20), default="tasdiq") # tasdiq | bekor
    by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    void_of: Mapped[int | None] = mapped_column(Integer)              # bekor qilish yozuvi: asl yozuv id
    void_reason: Mapped[str | None] = mapped_column(String(300))       # bekor qilish sababi (so'rov / qaror)
    void_req_by: Mapped[int | None] = mapped_column(Integer)           # so'ragan xodim
    voided_at: Mapped[datetime | None] = mapped_column(DateTime)       # asl yozuv bekor qilingan vaqt
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PrinterCount(Base):
    __tablename__ = "printer_counts"
    id: Mapped[int] = mapped_column(primary_key=True)
    counter: Mapped[int] = mapped_column(Integer)
    by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Payout(Base):
    """Xodimga berilgan pul: avans yoki oylik (bonus ichida)."""
    __tablename__ = "payouts"
    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    month: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(20))                     # avans | oylik
    amount: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(20), default="naqd")
    status: Mapped[str] = mapped_column(String(20), default="kutilmoqda")  # kutilmoqda | tasdiq | berildi | olindi | rad
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    given_at: Mapped[datetime | None] = mapped_column(DateTime)
    received_at: Mapped[datetime | None] = mapped_column(DateTime)


class Payroll(Base):
    """Oy yopilgandagi oylik hisob (har xodimga bitta)."""
    __tablename__ = "payrolls"
    __table_args__ = (UniqueConstraint("staff_id", "month"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    month: Mapped[str] = mapped_column(String(20))
    lines: Mapped[list] = mapped_column(JSON, default=list)          # [[nomi, summa], ...]
    gross: Mapped[int] = mapped_column(Integer, default=0)
    avans: Mapped[int] = mapped_column(Integer, default=0)
    bonus: Mapped[int] = mapped_column(Integer, default=0)
    penalty: Mapped[int] = mapped_column(Integer, default=0)
    net: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BonusLedger(Base):
    """Har oy har rol bo'yicha ball natijasi so'mda — 6 oy yig'iladi (dekabr/iyun) yoki har oy beriladi."""
    __tablename__ = "bonus_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    month: Mapped[str] = mapped_column(String(20))
    role: Mapped[str] = mapped_column(String(20))
    points: Mapped[int] = mapped_column(Integer)
    value: Mapped[int] = mapped_column(Integer)                      # 1 ball narxi
    amount: Mapped[int] = mapped_column(Integer)                     # + bonus / − jarima
    status: Mapped[str] = mapped_column(String(20), default="yigilmoqda")  # yigilmoqda | tolandi | ushlandi
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Achievement(Base):
    """O'quvchi yutug'i (sertifikat, OTM, maktab, olimpiada) — hujjat bilan, rahbar tasdig'idan keyin."""
    __tablename__ = "achievements"
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"))
    teacher_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    kind: Mapped[str] = mapped_column(String(20))
    level: Mapped[str | None] = mapped_column(String(20))
    detail: Mapped[str | None] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(80))
    file_id: Mapped[str | None] = mapped_column(String(200))
    points: Mapped[int] = mapped_column(Integer, default=0)
    year: Mapped[str] = mapped_column(String(20))                 # o'quv yili: "2026-2027"
    status: Mapped[str] = mapped_column(String(20), default="kutilmoqda")   # kutilmoqda | tasdiq | rad
    by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PrinterUse(Base):
    """Bepul (ichki) chop etish: test varaqlari, markaz hujjatlari, o'qituvchi hujjatlari, texnik xato/isrof."""
    __tablename__ = "printer_uses"
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(20))            # test | markaz | oqituvchi | isrof
    pages: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(200))
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))   # kimning hujjati (o'qituvchi)
    test_id: Mapped[int | None] = mapped_column(Integer)
    by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SurveyAnswer(Base):
    """Ota-onalar so'rovnomasi: o'qituvchi ishiga 1–10 baho (o'qituvchiga anonim)."""
    __tablename__ = "survey_answers"
    __table_args__ = (UniqueConstraint("month", "student_id", "group_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(20))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    teacher_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    score: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GroupTeacher(Base):
    """Guruh o'qituvchisi tarixi: o'qituvchi almashsa, to'lov ulushi shu sanadan yangi o'qituvchiga."""
    __tablename__ = "group_teachers"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    teacher_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    since: Mapped[datetime] = mapped_column(DateTime)
