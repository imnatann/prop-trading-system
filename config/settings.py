"""
Application Environment Settings.
Memuat konfigurasi dari file .env menggunakan Pydantic.
"""

from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from .prop_rules import PropFirmRules


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Runtime Mode
    app_mode: str = "DEV"  # "DEV" (Mock Broker simulation) atau "LIVE" (MT5 Terminal)
    log_level: str = "INFO"

    # MT5 Broker Credentials (Diperlukan saat mode LIVE di Windows VPS)
    mt5_login: Optional[int] = None
    mt5_password: Optional[str] = None
    mt5_server: Optional[str] = None
    mt5_path: Optional[str] = None

    # Alerting (Telegram)
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # Prop Firm Rules
    rules: PropFirmRules = PropFirmRules()


settings = Settings()
