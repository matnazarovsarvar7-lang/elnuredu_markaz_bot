from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import Base

# Bazadagi vaqtlar (created_at = now()) har doim UTC da yoziladi — hisob-kitob shunga asoslangan.
_connect = {"server_settings": {"timezone": "UTC"}} if settings.database_url.startswith("postgresql+asyncpg") else {}
engine = create_async_engine(settings.database_url, pool_pre_ping=True, connect_args=_connect)
Session = async_sessionmaker(engine, expire_on_commit=False)


# Mavjud bazaga yangi ustunlar (create_all ularni qo'shmaydi)
MIGRATIONS = [
    "ALTER TABLE payments ADD COLUMN batch VARCHAR(36)",
    "ALTER TABLE enrollments ADD COLUMN tier INTEGER",
    # import paytida guruh narxi price_override ga yozilgan edi -> toifaga o'tkazamiz
    "UPDATE enrollments SET tier = CASE price_override WHEN 200000 THEN 1 WHEN 300000 THEN 2 WHEN 350000 THEN 3 END, "
    "price_override = NULL WHERE tier IS NULL AND price_override IN (200000, 300000, 350000)",
    "ALTER TABLE tasks ADD COLUMN file_id VARCHAR(200)",
    "ALTER TABLE tasks ADD COLUMN file_kind VARCHAR(10)",
    "ALTER TABLE tasks ADD COLUMN done_file_id VARCHAR(200)",
    "ALTER TABLE tasks ADD COLUMN done_file_kind VARCHAR(10)",
    "ALTER TABLE tasks ADD COLUMN recurring_id INTEGER",
    "ALTER TABLE lessons ADD COLUMN homework_file_id VARCHAR(200)",
    "ALTER TABLE lessons ADD COLUMN homework_file_kind VARCHAR(10)",
    "ALTER TABLE lessons ADD COLUMN homework_files JSON",
    "ALTER TABLE lessons ADD COLUMN homework_at TIMESTAMP",
    "ALTER TABLE lessons ADD COLUMN homework_by INTEGER",
    "ALTER TABLE exams ADD COLUMN monthly BOOLEAN DEFAULT FALSE",
    "ALTER TABLE exams ADD COLUMN test_id INTEGER",
    "ALTER TABLE tests ADD COLUMN students JSON",
    "ALTER TABLE enrollments ADD COLUMN discount_amount INTEGER",
    "ALTER TABLE lessons ADD COLUMN homework_parts JSON",
    "ALTER TABLE attendance ADD COLUMN left_at TIMESTAMP",
    "ALTER TABLE payments ADD COLUMN void_of INTEGER",
    "ALTER TABLE payments ADD COLUMN void_reason VARCHAR(300)",
    "ALTER TABLE payments ADD COLUMN void_req_by INTEGER",
    "ALTER TABLE payments ADD COLUMN voided_at TIMESTAMP",
    "ALTER TABLE expenses ADD COLUMN void_of INTEGER",
    "ALTER TABLE expenses ADD COLUMN void_reason VARCHAR(300)",
    "ALTER TABLE expenses ADD COLUMN void_req_by INTEGER",
    "ALTER TABLE expenses ADD COLUMN voided_at TIMESTAMP",
    "ALTER TABLE service_sales ADD COLUMN void_of INTEGER",
    "ALTER TABLE service_sales ADD COLUMN void_reason VARCHAR(300)",
    "ALTER TABLE service_sales ADD COLUMN void_req_by INTEGER",
    "ALTER TABLE service_sales ADD COLUMN voided_at TIMESTAMP",
    "ALTER TABLE lessons ADD COLUMN ended_at TIMESTAMP",
    "ALTER TABLE students ADD COLUMN region VARCHAR(80)",
    "ALTER TABLE lessons ADD COLUMN cancel_reason VARCHAR(200)",
    "ALTER TABLE lessons ADD COLUMN cancel_status VARCHAR(10)",
    "ALTER TABLE lessons ADD COLUMN makeup_date DATE",
    "ALTER TABLE lessons ADD COLUMN extra BOOLEAN DEFAULT FALSE",
    "ALTER TABLE lessons ADD COLUMN makeup_penalized BOOLEAN DEFAULT FALSE",
    "ALTER TABLE lessons ADD COLUMN cancelled BOOLEAN DEFAULT FALSE",
]


async def init_db() -> None:
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    for sql in MIGRATIONS:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        except Exception:
            pass  # ustun allaqachon bor
    # Qisqa matn ustunlarini kengaytirish (Postgres uzunlikni qat'iy tekshiradi)
    if engine.dialect.name == "postgresql":
        async with engine.begin() as conn:
            rows = (await conn.execute(text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND data_type = 'character varying' "
                "AND character_maximum_length < 20"))).all()
        for tname, cname in rows:
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(
                        f'ALTER TABLE "{tname}" ALTER COLUMN "{cname}" TYPE VARCHAR(20)'))
            except Exception:
                pass
    await _one_time_fixes()


async def _one_time_fixes() -> None:
    """Rahbar qarorlarini bir marta qo'llash (keyin rahbar sozlamadan o'zgartirsa — qayta tegilmaydi)."""
    from sqlalchemy import select
    from app.db.models import Setting, Staff
    async with Session() as s:
        if await s.get(Setting, "fix_2026_11_pay"):
            return
        for st in (await s.execute(select(Staff))).scalars():
            roles = (st.roles or "").split(",")
            if "moliya" in roles and st.oklad:
                st.oklad = 0            # moliya bo'limi okladi o'rniga barcha tushumning 2%
            if "rahbar" in roles and st.percent == 70:
                st.percent = 50         # rahbarning o'qituvchilik ulushi 50%
        s.add(Setting(key="fix_2026_11_pay", value={"done": True}))
        await s.commit()
