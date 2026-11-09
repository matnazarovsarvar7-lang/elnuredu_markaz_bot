from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import Base

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
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
    "ALTER TABLE lessons ADD COLUMN homework_at TIMESTAMP",
    "ALTER TABLE lessons ADD COLUMN homework_by INTEGER",
    "ALTER TABLE exams ADD COLUMN monthly BOOLEAN DEFAULT FALSE",
    "ALTER TABLE exams ADD COLUMN test_id INTEGER",
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
