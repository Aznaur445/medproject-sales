"""Application settings loaded from environment variables (.env)."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    app_name: str = "МедПроект Продажи"
    public_url: str = "http://localhost:8000"
    timezone: str = "Europe/Moscow"
    log_level: str = "INFO"

    # Security
    secret_key: SecretStr = Field(..., description="Session signing key, 64+ random chars")
    fernet_key: SecretStr = Field(..., description="Key for encrypting secrets stored in DB")
    session_max_age_hours: int = 12
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15
    # One-time link /setup?token=... to create the first owner when no users exist (servers set up without SSH).
    setup_token: SecretStr | None = None

    # Infrastructure
    database_url: str = "postgresql+psycopg://medproject:medproject@localhost:5432/medproject"
    # Own schema instead of "public": on PostgreSQL 15+ a non-owner user may not create tables in "public"
    # (managed databases). Empty = default search_path.
    db_schema: str = ""
    redis_url: str = "redis://localhost:6379/0"
    files_dir: str = "./data/files"

    # Telegram
    telegram_bot_token: SecretStr | None = None
    telegram_owner_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # Mail (VK WorkSpace / Mail.ru)
    smtp_host: str = "smtp.mail.ru"
    smtp_port: int = 465
    smtp_ssl: bool = True
    smtp_starttls: bool = True  # used only when smtp_ssl is false
    imap_host: str = "imap.mail.ru"
    imap_port: int = 993
    mail_user: str | None = None
    mail_app_password: SecretStr | None = None
    mail_from_name: str = "МедПроект"

    # LLM
    llm_provider: Literal["deepseek", "yandexgpt", "anthropic", "none"] = "none"
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    yandex_api_key: SecretStr | None = None
    yandex_folder_id: str | None = None

    # S3-compatible storage (Timeweb Cloud S3) for backups and files
    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_region: str = "ru-1"

    backup_dir: str = "./data/backups"
    backup_retention_days: int = 14

    # Owner-approved updates: the panel writes a request here, the host timer (deploy/updater.sh) applies it.
    update_dir: str = "./data/update"
    update_repo: str = "Aznaur445/medproject-sales"
    update_branch: str = "claude/new-session-11cxoi"

    @field_validator(
        "telegram_bot_token",
        "setup_token",
        "mail_user",
        "mail_app_password",
        "deepseek_api_key",
        "yandex_api_key",
        "yandex_folder_id",
        "s3_endpoint_url",
        "s3_bucket",
        "s3_access_key",
        "s3_secret_key",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("telegram_owner_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.replace(" ", "").split(",") if part]
        if isinstance(value, int):
            return [value]
        return value

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
