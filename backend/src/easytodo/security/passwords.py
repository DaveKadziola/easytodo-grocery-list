"""Password hashing with argon2id (``PasswordHasher()`` default parameters).

A hash takes about 60 ms and 64 MiB. A route awaits ``hash_password_async`` and
``verify_password_async``, which run it on threads of their own, so that the event loop goes on
serving everyone else meanwhile. The plain functions are for code that has no event loop to keep
free (the CLI, the start-up).
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from easytodo.errors import ValidationFailed

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256
# How many hashes run at once: at most 2 x 64 MiB. A pool of our own, not ``asyncio.to_thread``:
# its executor has up to 32 threads (anyio's limiter 40), which a burst of sign-ins would fill with
# hashes of 64 MiB each, and they are the threads other code waits for.
PASSWORD_HASH_THREADS = 2

_hasher = PasswordHasher()
_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


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


def _hashing_pool() -> ThreadPoolExecutor:
    """The threads argon2 runs on, started on first use, not at import (a process that forks
    after it starts its own). One pool for the whole process, however many apps it runs (the tests
    start one per test), so it is never shut down: its idle threads end with the interpreter, which
    waits only for a hash that is running at that moment."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=PASSWORD_HASH_THREADS, thread_name_prefix="argon2"
            )
        return _pool


async def hash_password_async(pw: str) -> str:
    """``hash_password`` on the hashing threads."""
    return await asyncio.get_running_loop().run_in_executor(_hashing_pool(), hash_password, pw)


async def verify_password_async(hash: str, pw: str) -> bool:
    """``verify_password`` on the hashing threads."""
    return await asyncio.get_running_loop().run_in_executor(
        _hashing_pool(), verify_password, hash, pw
    )
