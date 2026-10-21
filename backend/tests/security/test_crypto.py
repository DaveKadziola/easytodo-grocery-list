import base64

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

from easytodo.security.crypto import DecryptionError, decrypt, encrypt


def test_encrypt_round_trips_and_hides_plaintext(settings):
    ciphertext = encrypt("JBSWY3DPEHPK3PXP", settings)
    assert "JBSWY3DPEHPK3PXP" not in ciphertext
    assert encrypt("JBSWY3DPEHPK3PXP", settings) != ciphertext  # random IV
    assert decrypt(ciphertext, settings) == "JBSWY3DPEHPK3PXP"


def test_key_is_hkdf_sha256_of_secret_key(settings):
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"easytodo-fernet").derive(
        settings.secret_key.get_secret_value().encode()
    )
    expected = Fernet(base64.urlsafe_b64encode(raw))
    assert expected.decrypt(encrypt("secret", settings).encode()) == b"secret"


def test_decrypt_with_other_secret_fails(settings):
    other = settings.model_copy(update={"secret_key": SecretStr("b" * 40)})
    with pytest.raises(DecryptionError):
        decrypt(encrypt("secret", settings), other)


@pytest.mark.parametrize("garbage", ["", "not-a-fernet-token", "zażółć"])
def test_decrypt_garbage_fails(settings, garbage):
    with pytest.raises(DecryptionError):
        decrypt(garbage, settings)
