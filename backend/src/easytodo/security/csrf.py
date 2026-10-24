"""CSRF protection: an ``Origin``/``Referer`` of the app's own addresses
(``Settings.allowed_origins``) plus a double-submit token.

``CsrfCookieMiddleware`` issues the token cookie; pages send it back in the ``X-CSRF-Token``
header (HTMX) or the ``csrf_token`` form field. A page holds a mask of the token, new on every
render (``security.tokens.mask_token``), and sends that; the token itself, as the cookie holds it,
is accepted too, for tests, ``scripts/bench.py`` and curl.
"""

import hmac
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection, Request

from easytodo.errors import Forbidden
from easytodo.security.tokens import unmask_token

CSRF_COOKIE = "easytodo_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin is not None:
        return origin
    referer = request.headers.get("referer")
    if referer is None:
        return None
    try:
        parts = urlsplit(referer)
    except ValueError:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _token_matches(cookie: str | None, submitted: object) -> bool:
    """Whether ``submitted`` is the cookie's token: as it is when it has the cookie's length,
    otherwise as a mask of it (one that is no mask is refused)."""
    if not cookie or not isinstance(submitted, str):
        return False
    if len(submitted) != len(cookie):
        submitted = unmask_token(submitted)
        if submitted is None:
            return False
    return hmac.compare_digest(submitted.encode(), cookie.encode())


async def verify_csrf(request: HTTPConnection) -> AsyncIterator[None]:
    """Global dependency (``FastAPI(dependencies=[Depends(verify_csrf)])``).

    Typed ``HTTPConnection`` because FastAPI also applies global dependencies to WebSocket
    routes; those check ``Origin`` themselves before accepting.

    A request without the header is a plain form post: the token is read from the form, which
    this parses. The files of that form (an upload spooled to a temporary file) are closed when
    the request is over, whether the route read the form too or not.
    """
    form = None
    try:
        if isinstance(request, Request) and request.method not in SAFE_METHODS:
            settings = request.app.state.settings
            if _request_origin(request) not in settings.allowed_origins:
                raise Forbidden(
                    "This page was opened at a different address. "
                    f"Open the app at {settings.public_origin}."
                )
            cookie = request.cookies.get(CSRF_COOKIE)
            submitted = request.headers.get(CSRF_HEADER)
            if submitted is None:
                form = await request.form()
                submitted = form.get(CSRF_FIELD)
            if not _token_matches(cookie, submitted):
                raise Forbidden("Invalid CSRF token.")
        yield
    finally:
        if form is not None:
            await form.close()
