import pytest
from argon2 import PasswordHasher

from easytodo.errors import ValidationFailed
from easytodo.security.passwords import (
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)


def test_hash_is_argon2id_and_salted():
    h1, h2 = hash_password("long-enough-pw"), hash_password("long-enough-pw")
    assert h1.startswith("$argon2id$") and h1 != h2
    assert verify_password(h1, "long-enough-pw") and not verify_password(h1, "wrong-password")


def test_verify_password_returns_false_for_a_malformed_hash():
    assert verify_password("not-an-argon2-hash", "long-enough-pw") is False


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
