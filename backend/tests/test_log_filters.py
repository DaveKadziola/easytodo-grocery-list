"""The access log must not hold a link that works: an invite or password-reset token."""

import logging

import pytest

from easytodo.log_filters import REDACTED, RedactTokens
from easytodo.main import create_app

T = "AbC_-12"  # a token (the alphabet of new_token)
ACCESS_LOG_FORMAT = '%s - "%s %s HTTP/%s" %d'  # uvicorn's h11 and httptools access lines


@pytest.fixture
def access():
    """uvicorn's access logger as a fresh process has it: without our filter, so that the test
    shows ``create_app`` putting it there."""
    logger = logging.getLogger("uvicorn.access")
    kept = list(logger.filters)
    logger.filters[:] = [f for f in kept if not isinstance(f, RedactTokens)]
    yield logger
    logger.filters[:] = kept


@pytest.mark.parametrize(
    "path,shown",
    [
        (f"/password/reset/{T}", f"/password/reset/{REDACTED}"),
        (f"/invite/{T}/accept", f"/invite/{REDACTED}/accept"),
        (f"/invite/{T}", f"/invite/{REDACTED}"),
        (f"/login?next=%2Finvite%2F{T}", f"/login?next=%2Finvite%2F{REDACTED}"),
        # Sign-up from a link: the page's own address keeps the token in its query.
        (f"/register?next=/invite/{T}", f"/register?next=/invite/{REDACTED}"),
        (f"/login/2fa?next=%2finvite%2f{T}", f"/login/2fa?next=%2finvite%2f{REDACTED}"),
        (f"/login?next=%252Finvite%252F{T}", f"/login?next=%252Finvite%252F{REDACTED}"),
        (
            f"/login?next=%2Fpassword%2Freset%2F{T}&x=1",
            f"/login?next=%2Fpassword%2Freset%2F{REDACTED}&x=1",
        ),
        ("/boards/7/invites/3", "/boards/7/invites/3"),  # an id, not a token
        ("/boards/7/members", "/boards/7/members"),
    ],
)
def test_access_log_hides_reset_and_invite_tokens(settings, access, path, shown):
    create_app(settings)
    create_app(settings)  # every app (and test) starts the same way: one filter, not one each
    record = access.makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        ACCESS_LOG_FORMAT,
        ("127.0.0.1:5", "GET", path, "1.1", 200),
        None,
    )

    assert access.filter(record)
    assert record.getMessage() == f'127.0.0.1:5 - "GET {shown} HTTP/1.1" 200'
    assert sum(isinstance(f, RedactTokens) for f in access.filters) == 1


def test_the_filter_keeps_the_five_fields_uvicorn_colours():
    """uvicorn's AccessFormatter unpacks exactly (client, method, path, version, status)."""
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        ACCESS_LOG_FORMAT,
        ("127.0.0.1:5", "GET", f"/invite/{T}", "1.1", 200),
        None,
    )

    assert RedactTokens().filter(record) is True
    assert record.args == ("127.0.0.1:5", "GET", f"/invite/{REDACTED}", "1.1", 200)
