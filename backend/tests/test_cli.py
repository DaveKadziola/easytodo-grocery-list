"""The command line: ``python -m easytodo.cli create-admin`` makes an admin account, or makes an
existing account an admin."""

import asyncio
import io
import subprocess
import sys

import pytest
from factories import DEFAULT_PASSWORD, create_user
from sqlalchemy import func, select

from easytodo.cli import main
from easytodo.config import get_settings
from easytodo.models import User
from easytodo.security.passwords import verify_password

PASSWORD = "secret-password-1"


@pytest.fixture
def cli_env(monkeypatch, settings):
    """The environment the CLI reads its settings from, pointing at the test database."""
    monkeypatch.setenv("EASYTODO_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("EASYTODO_SECRET_KEY", settings.secret_key.get_secret_value())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def stdin(monkeypatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


async def create_admin(*args: str) -> int:
    # main runs its own event loop (asyncio.run), so it cannot run in the test's.
    return await asyncio.to_thread(main, ["create-admin", *args])


async def user_count(db) -> int:
    return await db.scalar(select(func.count()).select_from(User))


async def test_cli_create_admin_promotes_existing_user(db, monkeypatch, cli_env, capsys):
    user = await create_user(db, "dave@example.com")
    stdin(monkeypatch, PASSWORD + "\n")

    assert await create_admin("--email", " Dave@Example.com ", "--password-stdin") == 0

    await db.refresh(user)
    assert user.is_admin is True
    assert verify_password(user.password_hash, DEFAULT_PASSWORD)  # the password stays as it was
    assert user.must_change_password is False
    assert "dave@example.com is now an admin." in capsys.readouterr().out
    assert await user_count(db) == 1


async def test_cli_create_admin_creates_an_account(db, monkeypatch, cli_env, capsys):
    stdin(monkeypatch, PASSWORD + "\n")

    assert await create_admin("--email", "Boss@Example.com", "--password-stdin") == 0

    user = await db.scalar(select(User).where(User.email == "boss@example.com"))
    assert user.is_admin is True
    assert user.must_change_password is False
    assert verify_password(user.password_hash, PASSWORD)  # without the line break
    assert "Created the admin account boss@example.com." in capsys.readouterr().out


async def test_cli_create_admin_asks_for_the_password(db, monkeypatch, cli_env):
    answers = iter([PASSWORD, PASSWORD])
    prompts: list[str] = []

    def getpass(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("getpass.getpass", getpass)

    assert await create_admin("--email", "boss@example.com") == 0

    assert prompts == ["Password: ", "Repeat the password: "]
    user = await db.scalar(select(User).where(User.email == "boss@example.com"))
    assert user.is_admin is True
    assert verify_password(user.password_hash, PASSWORD)


async def test_cli_create_admin_refuses_passwords_that_differ(db, monkeypatch, cli_env, capsys):
    answers = iter([PASSWORD, "something-else-1"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))

    assert await create_admin("--email", "boss@example.com") == 1

    assert "Passwords do not match." in capsys.readouterr().err
    assert await user_count(db) == 0


@pytest.mark.parametrize(
    ("email", "password", "message"),
    [
        ("boss@example.com", "short", "Password must be at least 10 characters."),
        ("not-an-email", PASSWORD, "Enter a valid email address."),
    ],
)
async def test_cli_create_admin_refuses_bad_input(
    db, monkeypatch, cli_env, capsys, email, password, message
):
    stdin(monkeypatch, password + "\n")

    assert await create_admin("--email", email, "--password-stdin") == 1

    assert f"error: {message}" in capsys.readouterr().err
    assert await user_count(db) == 0


async def test_cli_create_admin_leaves_an_admin_as_is(db, monkeypatch, cli_env, capsys):
    await create_user(db, "boss@example.com", is_admin=True)
    monkeypatch.setattr("getpass.getpass", lambda prompt: pytest.fail("asked for a password"))

    assert await create_admin("--email", "boss@example.com") == 0

    assert "boss@example.com is already an admin." in capsys.readouterr().out


def test_cli_runs_as_a_module():
    result = subprocess.run(
        [sys.executable, "-m", "easytodo.cli", "create-admin", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--password-stdin" in result.stdout
