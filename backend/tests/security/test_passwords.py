import asyncio
import threading

import pytest
from argon2 import PasswordHasher

from easytodo.errors import ValidationFailed
from easytodo.security.passwords import (
    PASSWORD_HASH_THREADS,
    hash_password,
    hash_password_async,
    needs_rehash,
    validate_password,
    verify_password,
    verify_password_async,
)


def test_hash_is_argon2id_and_salted():
    h1, h2 = hash_password("long-enough-pw"), hash_password("long-enough-pw")
    assert h1.startswith("$argon2id$") and h1 != h2
    assert verify_password(h1, "long-enough-pw") and not verify_password(h1, "wrong-password")


def test_verify_password_returns_false_for_a_malformed_hash():
    assert verify_password("not-an-argon2-hash", "long-enough-pw") is False


async def test_async_wrappers_agree_with_the_plain_functions():
    hashed = await hash_password_async("long-enough-pw")
    assert hashed.startswith("$argon2id$") and verify_password(hashed, "long-enough-pw")
    assert await verify_password_async(hashed, "long-enough-pw") is True
    assert await verify_password_async(hashed, "wrong-password") is False
    assert await verify_password_async("not-an-argon2-hash", "long-enough-pw") is False


async def test_async_hashing_uses_at_most_the_hashing_threads(monkeypatch):
    names: set[str] = set()
    real_hash = PasswordHasher.hash

    def recording_hash(self, *args, **kwargs):
        names.add(threading.current_thread().name)
        return real_hash(self, *args, **kwargs)

    monkeypatch.setattr(PasswordHasher, "hash", recording_hash)

    await asyncio.gather(
        *(hash_password_async("long-enough-pw") for _ in range(2 * PASSWORD_HASH_THREADS + 2))
    )

    assert 1 <= len(names) <= PASSWORD_HASH_THREADS
    assert all(name.startswith("argon2") for name in names)


def test_needs_rehash_only_for_weaker_parameters():
    assert needs_rehash(hash_password("long-enough-pw")) is False
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("long-enough-pw")
    assert needs_rehash(weak) is True


@pytest.mark.parametrize(
    "pw,ok", [("a" * 9, False), ("a" * 10, True), ("a" * 256, True), ("a" * 257, False)]
)
def test_validate_password_length(pw, ok):
    if ok:
        assert validate_password(pw) is None
    else:
        with pytest.raises(ValidationFailed):
            validate_password(pw)


@pytest.mark.parametrize(
    "pw,message",
    [
        ("a" * 9, "Password must be at least 10 characters."),
        ("a" * 257, "Password must be at most 256 characters."),
    ],
)
def test_validate_password_messages(pw, message):
    with pytest.raises(ValidationFailed) as exc_info:
        validate_password(pw)
    assert exc_info.value.message == message
    assert exc_info.value.status_code == 422
