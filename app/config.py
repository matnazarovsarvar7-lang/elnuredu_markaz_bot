"""Sozlamalar .env faylidan o'qiladi. Token va parollar kodda saqlanmaydi."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    database_url: str = "sqlite+aiosqlite:///./dev.db"
    timezone: str = "Asia/Tashkent"
    center_name: str = "ELNUR_EDU"
    # Birinchi ishga tushirishda rahbar shu raqam bilan tanib olinadi
    owner_phone: str = ""


settings = Settings()
