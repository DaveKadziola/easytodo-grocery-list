import pytest
from pydantic import ValidationError

from easytodo.config import Settings


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
