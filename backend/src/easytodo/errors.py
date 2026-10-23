"""Application errors and their HTTP handlers.

HTMX requests get an empty body with ``HX-Reswap: none`` and an ``HX-Trigger`` toast (plus
``resync`` for ``NotFound``, so the client reloads a board that changed elsewhere); other
requests get the error page, or a redirect to the login page for ``NotAuthenticated`` (and to
the location of a ``RedirectRequired``: the password form, or the second sign-in step).
Starlette's errors (unknown page, wrong method) and HTTPExceptions get the same treatment.
"""

import json
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from easytodo.web import is_htmx, render


class AppError(Exception):
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(AppError):
    status_code = 404

    def __init__(self, message: str = "Not found.") -> None:
        super().__init__(message)


class Forbidden(AppError):
    status_code = 403


class Conflict(AppError):
    status_code = 409


class PayloadTooLarge(AppError):
    status_code = 413


class ValidationFailed(AppError):
    status_code = 422


class TooManyRequests(AppError):
    status_code = 429


class NotAuthenticated(AppError):
    status_code = 401

    def __init__(self, message: str = "Not authenticated.") -> None:
        super().__init__(message)


class RedirectRequired(AppError):
    """A page the user cannot have yet: they go to ``location`` first."""

    status_code = 303

    def __init__(self, message: str, location: str) -> None:
        super().__init__(message)
        self.location = location


class PasswordChangeRequired(RedirectRequired):
    """A signed-in user who must pick a new password asked for a page; they go to ``location``."""

    def __init__(self, location: str) -> None:
        super().__init__("You must change your password first.", location)


class SecondFactorRequired(RedirectRequired):
    """A sign-in still waiting for its second factor asked for a page; it goes to ``location``."""

    def __init__(self, location: str) -> None:
        super().__init__("Enter your two-factor code first.", location)


def _login_after(request: Request) -> str:
    """For an htmx request: the login page, then back to the page it was sent from (its
    ``HX-Current-URL``, when that is a page of this app, at any of its addresses)."""
    current = request.headers.get("HX-Current-URL", "")
    try:
        parts = urlsplit(current)
    except ValueError:
        return "/login"
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path
    if origin not in request.app.state.settings.allowed_origins or not path.startswith("/"):
        return "/login"
    if path.startswith("//"):  # would read as another host once it is a URL again
        return "/login"
    target = path + (f"?{parts.query}" if parts.query else "")
    return f"/login?next={quote(target, safe='')}"


async def app_error_handler(request: Request, exc: AppError) -> Response:
    if isinstance(exc, NotAuthenticated):
        if is_htmx(request):
            return Response(status_code=401, headers={"HX-Redirect": _login_after(request)})
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
    if isinstance(exc, RedirectRequired):
        if is_htmx(request):
            return Response(headers={"HX-Redirect": exc.location})
        return RedirectResponse(exc.location, status_code=303)
    if is_htmx(request):
        trigger: dict[str, object] = {"toast": {"level": "error", "message": exc.message}}
        if isinstance(exc, NotFound):
            trigger["resync"] = True
        # json.dumps escapes non-ASCII, so the header stays latin-1 encodable.
        headers = {"HX-Reswap": "none", "HX-Trigger": json.dumps(trigger)}
        return Response(status_code=exc.status_code, headers=headers)
    return render(
        request,
        "errors/error.html",
        {"status_code": exc.status_code, "message": exc.message},
        status_code=exc.status_code,
    )


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> Response:
    error = exc.errors()[0]
    if error["loc"] and error["loc"][0] == "path":
        # /boards/abc: there is no such page, rather than a form the user got wrong.
        return await app_error_handler(request, NotFound())
    return await app_error_handler(request, ValidationFailed(error["msg"]))


# Starlette's own errors (no route, a method the route does not take, a static file that is not
# there) and HTTPExceptions, in the app's words rather than as {"detail": ...} JSON.
_HTTP_MESSAGES = {
    404: "Page not found.",
    405: "This page does not accept that request.",
}


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    error = AppError(_HTTP_MESSAGES.get(exc.status_code, str(exc.detail)))
    error.status_code = exc.status_code  # this instance only; a plain 404 is no board resync
    response = await app_error_handler(request, error)
    for name, value in (exc.headers or {}).items():  # Allow on a 405
        response.headers.setdefault(name, value)
    return response


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
