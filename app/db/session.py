from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import Base

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


# Mavjud bazaga yangi ustunlar (create_all ularni qo'shmaydi)
MIGRATIONS = [
    "ALTER TABLE payments ADD COLUMN batch VARCHAR(36)",
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
