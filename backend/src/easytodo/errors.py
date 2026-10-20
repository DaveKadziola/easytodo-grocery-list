"""Application errors and their HTTP handlers.

HTMX requests get an empty body with ``HX-Reswap: none`` and an ``HX-Trigger`` toast (plus
``resync`` for ``NotFound``, so the client reloads a board that changed elsewhere); other
requests get the error page, or a redirect to the login page for ``NotAuthenticated``.
"""

import json
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response

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


class ValidationFailed(AppError):
    status_code = 422


class NotAuthenticated(AppError):
    status_code = 401

    def __init__(self, message: str = "Not authenticated.") -> None:
        super().__init__(message)


async def app_error_handler(request: Request, exc: AppError) -> Response:
    if isinstance(exc, NotAuthenticated):
        if is_htmx(request):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
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
    return await app_error_handler(request, ValidationFailed(exc.errors()[0]["msg"]))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
