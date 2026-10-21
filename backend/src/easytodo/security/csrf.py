"""CSRF protection: same-origin ``Origin``/``Referer`` plus a double-submit token.

``CsrfCookieMiddleware`` issues the token cookie; pages send it back in the ``X-CSRF-Token``
header (HTMX) or the ``csrf_token`` form field.
"""

import hmac
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection, Request

from easytodo.errors import Forbidden

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


async def verify_csrf(request: HTTPConnection) -> None:
    """Global dependency (``FastAPI(dependencies=[Depends(verify_csrf)])``).

    Typed ``HTTPConnection`` because FastAPI also applies global dependencies to WebSocket
    routes; those check ``Origin`` themselves before accepting.
    """
    if not isinstance(request, Request) or request.method in SAFE_METHODS:
        return
    public_origin = request.app.state.settings.public_origin
    if _request_origin(request) != public_origin:
        raise Forbidden(
            f"This page was opened at a different address. Open the app at {public_origin}."
        )
    cookie = request.cookies.get(CSRF_COOKIE)
    submitted = request.headers.get(CSRF_HEADER)
    if submitted is None:
        submitted = (await request.form()).get(CSRF_FIELD)
    if (
        not cookie
        or not isinstance(submitted, str)
        or not hmac.compare_digest(submitted.encode(), cookie.encode())
    ):
        raise Forbidden("Invalid CSRF token.")
