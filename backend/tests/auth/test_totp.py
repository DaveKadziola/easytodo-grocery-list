from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest

from easytodo.security import totp
from easytodo.security.totp import (
    ISSUER,
    new_totp_secret,
    provisioning_uri,
    qr_svg,
    verify_totp,
)

NOW = 1_800_000_015.0  # 15 s into a 30 s step


@pytest.fixture
def frozen_clock(monkeypatch) -> float:
    """verify_totp's clock stopped at ``NOW``."""
    monkeypatch.setattr(totp, "time", SimpleNamespace(time=lambda: NOW))
    return NOW


def test_verify_totp_rejects_replay():
    s = new_totp_secret()
    code = pyotp.TOTP(s).now()
    step = verify_totp(s, code, None)
    assert step is not None
    assert verify_totp(s, code, step) is None


def test_verify_totp_accepts_previous_step(frozen_clock):
    s = new_totp_secret()
    current_step = int(NOW) // 30
    assert verify_totp(s, pyotp.TOTP(s).at(NOW - 30), None) == current_step - 1
    assert verify_totp(s, pyotp.TOTP(s).at(NOW - 60), None) is None
    assert verify_totp(s, pyotp.TOTP(s).at(NOW - 90), None) is None


def test_verify_totp_accepts_next_step_only(frozen_clock):
    s = new_totp_secret()
    current_step = int(NOW) // 30
    assert verify_totp(s, pyotp.TOTP(s).at(NOW), None) == current_step
    assert verify_totp(s, pyotp.TOTP(s).at(NOW + 30), None) == current_step + 1
    assert verify_totp(s, pyotp.TOTP(s).at(NOW + 60), None) is None


def test_verify_totp_rejects_a_step_before_the_last_used_one(frozen_clock):
    s = new_totp_secret()
    current_step = int(NOW) // 30
    previous = pyotp.TOTP(s).at(NOW - 30)
    assert verify_totp(s, previous, current_step) is None
    assert verify_totp(s, previous, current_step - 2) == current_step - 1
    assert verify_totp(s, pyotp.TOTP(s).at(NOW), current_step - 1) == current_step


def test_verify_totp_ignores_spaces_in_the_code(frozen_clock):
    s = new_totp_secret()
    code = pyotp.TOTP(s).at(NOW)
    assert verify_totp(s, f" {code[:3]} {code[3:]} ", None) == int(NOW) // 30


@pytest.mark.parametrize(
    "code",
    ["", "12345", "1234567", "abcdef", "12345a", "١٢٣٤٥٦", "１２３４５６"],
    ids=["empty", "short", "long", "letters", "mixed", "arabic-indic-digits", "fullwidth-digits"],
)
def test_verify_totp_rejects_malformed_codes(frozen_clock, code):
    assert verify_totp(new_totp_secret(), code, None) is None


def test_verify_totp_rejects_another_secrets_code(frozen_clock):
    assert verify_totp(new_totp_secret(), pyotp.TOTP(new_totp_secret()).at(NOW), None) is None


def test_new_totp_secret_is_random_base32():
    first, second = new_totp_secret(), new_totp_secret()
    assert first != second
    assert len(first) == 32 and set(first) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def test_provisioning_uri_names_the_issuer_and_the_account():
    secret = new_totp_secret()
    uri = urlsplit(provisioning_uri(secret, "dave@example.com"))
    assert (uri.scheme, uri.netloc, uri.path) == ("otpauth", "totp", "/EasyToDo:dave%40example.com")
    assert parse_qs(uri.query) == {"secret": [secret], "issuer": [ISSUER]}
    assert ISSUER == "EasyToDo"


def test_qr_svg_is_an_inline_svg_without_inline_styles():
    svg = qr_svg(provisioning_uri(new_totp_secret(), "dave@example.com"))
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "<?xml" not in svg  # embeddable in the page as is
    assert "style" not in svg  # the CSP (style-src 'self') would drop it
