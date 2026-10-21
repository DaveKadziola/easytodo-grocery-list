"""Password hashing with argon2id (``PasswordHasher()`` default parameters)."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from easytodo.errors import ValidationFailed

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256

_hasher = PasswordHasher()


def hash_password(pw: str) -> str:
    return _hasher.hash(pw)


def verify_password(hash: str, pw: str) -> bool:
    try:
        return _hasher.verify(hash, pw)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(hash: str) -> bool:
    return _hasher.check_needs_rehash(hash)


def validate_password(pw: str) -> None:
    if len(pw) < MIN_PASSWORD_LENGTH:
        raise ValidationFailed(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(pw) > MAX_PASSWORD_LENGTH:
        raise ValidationFailed(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
