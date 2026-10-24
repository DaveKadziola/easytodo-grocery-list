import os

import pytest
from pydantic import ValidationError

from easytodo.config import Settings
from easytodo.web import templates


def test_secret_key_must_have_32_chars():
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+asyncpg://u:p@h/db", secret_key="short")


def test_cookie_secure_follows_public_url():
    s = Settings(
        database_url="postgresql+asyncpg://u:p@h/db",
        secret_key="x" * 40,
        public_url="https://todo.example.com",
    )
    assert s.cookie_secure is True
    assert s.public_origin == "https://todo.example.com"


def test_public_origin_keeps_non_default_port():
    s = Settings(database_url="postgresql+asyncpg://u:p@h/db", secret_key="x" * 40)
    assert s.cookie_secure is False
    assert s.public_origin == "http://localhost:8000"


DATABASE_URL = "postgresql+asyncpg://u:p@h/db"


def settings_with_extra_origins(
    monkeypatch: pytest.MonkeyPatch, value: str, public_url: str = "http://localhost:8000"
) -> Settings:
    """Settings as the app reads them, with ``EASYTODO_EXTRA_ORIGINS=value`` in the environment."""
    monkeypatch.setenv("EASYTODO_EXTRA_ORIGINS", value)
    return Settings(
        _env_file=None, database_url=DATABASE_URL, secret_key="x" * 40, public_url=public_url
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        (
            "http://192.168.122.50:8000, http://192.168.1.100:8000",
            ["http://192.168.122.50:8000", "http://192.168.1.100:8000"],
        ),
        ("http://192.168.122.50:8000/", ["http://192.168.122.50:8000"]),
        ("http://LAN.Example:8000", ["http://lan.example:8000"]),
        ("HTTP://lan.example:80", ["http://lan.example"]),
        ("https://todo.example.com:443/", ["https://todo.example.com"]),
        ("", []),
        ("  ", []),
    ],
)
def test_extra_origins_are_read_as_browsers_send_origin(monkeypatch, value, expected):
    assert settings_with_extra_origins(monkeypatch, value).extra_origins == expected


def test_no_extra_origins_by_default(monkeypatch):
    monkeypatch.delenv("EASYTODO_EXTRA_ORIGINS", raising=False)
    s = Settings(_env_file=None, database_url=DATABASE_URL, secret_key="x" * 40)
    assert s.extra_origins == []
    assert s.allowed_origins == frozenset({"http://localhost:8000"})


@pytest.mark.parametrize(
    "entry",
    [
        "ftp://x",
        "http://",
        "http://x/path",
        "http://u@x",
        "http://x?q=1",
        "http://x#f",
        "not a url",
    ],
)
def test_malformed_extra_origin_is_refused_by_name(monkeypatch, entry):
    with pytest.raises(ValidationError) as caught:
        settings_with_extra_origins(monkeypatch, f"http://192.168.122.50:8000, {entry}")
    [error] = caught.value.errors()
    assert error["loc"] == ("extra_origins",)  # the entrypoint names EASYTODO_EXTRA_ORIGINS
    assert repr(entry) in error["msg"]


def test_http_extra_origin_is_refused_under_an_https_public_url(monkeypatch):
    """The session cookie is Secure then: the browser would never send it to the http address,
    so signing in there would loop."""
    with pytest.raises(ValidationError) as caught:
        settings_with_extra_origins(
            monkeypatch, "http://192.168.122.50:8000", public_url="https://todo.example.com"
        )
    [error] = caught.value.errors()
    assert error["loc"] == ("extra_origins",)
    assert "'http://192.168.122.50:8000' is http, but EASYTODO_PUBLIC_URL is https" in error["msg"]
    assert "Secure" in error["msg"]


def test_https_extra_origin_under_an_http_public_url_is_fine(monkeypatch):
    s = settings_with_extra_origins(monkeypatch, "https://todo.example.com")
    assert s.extra_origins == ["https://todo.example.com"]


def test_allowed_origins_are_the_public_and_the_extra_origins(monkeypatch):
    s = settings_with_extra_origins(
        monkeypatch,
        "http://192.168.122.50:8000, http://192.168.1.100:8000",
        public_url="http://localhost:8000/",
    )
    assert s.allowed_origins == frozenset(
        {"http://localhost:8000", "http://192.168.122.50:8000", "http://192.168.1.100:8000"}
    )
    assert s.public_origin == "http://localhost:8000"  # links and the error messages use it


def test_purge_runs_daily_unless_turned_off(monkeypatch):
    def interval() -> int:
        return Settings(
            _env_file=None, database_url=DATABASE_URL, secret_key="x" * 40
        ).purge_interval_hours

    assert interval() == 24
    monkeypatch.setenv("EASYTODO_PURGE_INTERVAL_HOURS", "0")
    assert interval() == 0
    monkeypatch.setenv("EASYTODO_PURGE_INTERVAL_HOURS", "-1")
    with pytest.raises(ValidationError):
        interval()


def test_database_url_is_kept_secret():
    """The URL holds the database password: it stays out of reprs, logs and templates."""
    s = Settings(_env_file=None, database_url=DATABASE_URL, secret_key="x" * 40)

    assert s.database_url.get_secret_value() == DATABASE_URL
    assert "u:p@h" not in repr(s) and "u:p@h" not in str(s)
    assert (
        templates.env.from_string("{{ settings.database_url }}").render(settings=s) == "**********"
    )


def test_tests_ignore_the_developers_environment_and_env_file(tmp_path, monkeypatch):
    """``tests/conftest.py`` drops every EASYTODO_* variable and the ``.env`` file for the whole
    session, so a developer's (or CI's) settings cannot change what a test sees."""
    assert [name for name in os.environ if name.upper().startswith("EASYTODO_")] == []
    (tmp_path / ".env").write_text("EASYTODO_PUBLIC_URL=https://leak.example\n")
    monkeypatch.chdir(tmp_path)
    s = Settings(database_url=DATABASE_URL, secret_key="x" * 40)  # on purpose without _env_file
    assert s.public_origin == "http://localhost:8000"
