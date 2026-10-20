"""Symmetric encryption of secrets at rest (Fernet, key derived from ``EASYTODO_SECRET_KEY``)."""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from easytodo.config import Settings


class DecryptionError(Exception):
    """The ciphertext is malformed or was encrypted with another secret key."""


def _fernet(settings: Settings) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"easytodo-fernet").derive(
        settings.secret_key.get_secret_value().encode()
    )
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(plaintext: str, settings: Settings) -> str:
    return _fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str, settings: Settings) -> str:
    try:
        return _fernet(settings).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError("Could not decrypt the value.") from exc
