import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_DEFAULT_PORTS = {"http": 80, "https": 443}

# scheme://host[:port], with at most a trailing "/": no user, path, query or fragment. The URL
# parser then checks the host and the port.
_ORIGIN_SHAPE = re.compile(r"https?://[^/\\?#@\s]+/?", re.IGNORECASE)


def _origin(url: AnyHttpUrl) -> str:
    """``scheme://host[:port]`` as browsers send it in the ``Origin`` header."""
    origin = f"{url.scheme}://{url.host}"
    if url.port != _DEFAULT_PORTS.get(url.scheme):
        origin += f":{url.port}"
    return origin


def _origin_url(entry: str) -> AnyHttpUrl:
    """``entry`` (of EASYTODO_EXTRA_ORIGINS) as a URL; ``ValueError`` naming it unless it is an
    origin."""
    problem = ValueError(
        f"{entry!r} is not an origin such as http://192.168.1.20:8000 (http or https, a host "
        "and an optional port, with no path, user, query or fragment)"
    )
    if not _ORIGIN_SHAPE.fullmatch(entry):
        raise problem
    try:
        return AnyHttpUrl(entry)
    except ValidationError:
        raise problem from None


class Settings(BaseSettings):
    """Application configuration, read from ``EASYTODO_*`` environment variables (or ``.env``)."""

    # extra="ignore": the shared .env also holds compose-only keys (POSTGRES_PASSWORD, ...).
    model_config = SettingsConfigDict(env_prefix="EASYTODO_", env_file=".env", extra="ignore")

    database_url: str  # postgresql+asyncpg://user:password@host:port/dbname
    secret_key: SecretStr = Field(min_length=32)
    public_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    # More addresses the app is opened at (comma-separated in EASYTODO_EXTRA_ORIGINS), stored
    # as origins; links in mails and invites still use public_url.
    extra_origins: Annotated[list[str], NoDecode] = []
    env: Literal["dev", "test", "prod"] = "dev"
    registration_open: bool = True
    session_ttl_days: int = 30
    # How often, in hours, the app deletes what is past its retention (see easytodo.maintenance);
    # 0 turns it off (the CLI's ``purge`` still works).
    purge_interval_hours: int = Field(default=24, ge=0)
    mail_backend: Literal["console", "smtp"] = "console"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str = "EasyToDo <noreply@localhost>"
    smtp_starttls: bool = True
    forwarded_allow_ips: str = "127.0.0.1"
    # Where attached files are kept (relative to the working directory, unless absolute), and the
    # limits on them: bytes per file, files per task and bytes in all.
    attachments_dir: Path = Path("data/attachments")
    attachment_max_bytes: int = 10 * 1024 * 1024
    attachments_per_item: int = 20
    attachments_total_bytes: int = 2 * 1024**3

    @field_validator("extra_origins", mode="before")
    @classmethod
    def _split_extra_origins(cls, value: object) -> object:
        """EASYTODO_EXTRA_ORIGINS is a comma-separated list (not JSON); empty means none."""
        return value.split(",") if isinstance(value, str) else value

    @field_validator("extra_origins")
    @classmethod
    def _normalize_extra_origins(cls, entries: list[str], info: ValidationInfo) -> list[str]:
        """Each entry as browsers send it in ``Origin`` (empty entries are skipped). Under an
        https public_url an http entry is refused: the session cookie is ``Secure`` then, so
        browsers never send it there."""
        public_url = info.data.get("public_url")  # absent when it is not valid itself
        origins = []
        for entry in filter(None, (entry.strip() for entry in entries)):
            url = _origin_url(entry)
            if url.scheme == "http" and public_url is not None and public_url.scheme == "https":
                raise ValueError(
                    f"{entry!r} is http, but EASYTODO_PUBLIC_URL is https: the session cookie "
                    "is then Secure, so browsers never send it to an http address and signing "
                    "in there would loop; use https for it too"
                )
            origins.append(_origin(url))
        return origins

    @property
    def cookie_secure(self) -> bool:
        return self.public_url.scheme == "https"

    @property
    def public_origin(self) -> str:
        """``scheme://host[:port]`` as browsers send it in the ``Origin`` header."""
        return _origin(self.public_url)

    @property
    def allowed_origins(self) -> frozenset[str]:
        """The origins requests and WebSockets may come from: public_origin and extra_origins."""
        return frozenset((self.public_origin, *self.extra_origins))


@lru_cache
def get_settings() -> Settings:
    return Settings()
