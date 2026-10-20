from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_PORTS = {"http": 80, "https": 443}


class Settings(BaseSettings):
    """Application configuration, read from ``EASYTODO_*`` environment variables (or ``.env``)."""

    # extra="ignore": the shared .env also holds compose-only keys (POSTGRES_PASSWORD, ...).
    model_config = SettingsConfigDict(env_prefix="EASYTODO_", env_file=".env", extra="ignore")

    database_url: str  # postgresql+asyncpg://user:password@host:port/dbname
    secret_key: SecretStr = Field(min_length=32)
    public_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    env: Literal["dev", "test", "prod"] = "dev"
    registration_open: bool = True
    session_ttl_days: int = 30
    mail_backend: Literal["console", "smtp"] = "console"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str = "EasyToDo <noreply@localhost>"
    smtp_starttls: bool = True
    forwarded_allow_ips: str = "127.0.0.1"

    @property
    def cookie_secure(self) -> bool:
        return self.public_url.scheme == "https"

    @property
    def public_origin(self) -> str:
        """``scheme://host[:port]`` as browsers send it in the ``Origin`` header."""
        url = self.public_url
        origin = f"{url.scheme}://{url.host}"
        if url.port != _DEFAULT_PORTS.get(url.scheme):
            origin += f":{url.port}"
        return origin


@lru_cache
def get_settings() -> Settings:
    return Settings()
