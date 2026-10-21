import hashlib
import re

from easytodo.security.tokens import hash_token, new_token


def test_new_token_is_urlsafe_32_bytes_and_unique():
    tokens = {new_token() for _ in range(100)}
    assert len(tokens) == 100
    # token_urlsafe(32): 32 random bytes -> 43 base64url characters, no padding
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", t) for t in tokens)


def test_hash_token_is_sha256_hex():
    assert hash_token("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    token = new_token()
    assert hash_token(token) == hashlib.sha256(token.encode()).hexdigest()
