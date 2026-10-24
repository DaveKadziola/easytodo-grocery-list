"""Pure ASGI middleware (no BaseHTTPMiddleware: no extra task per request, context vars intact)."""

import re
from http.cookies import SimpleCookie

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import ClientDisconnect, HTTPConnection, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from easytodo.attachments.service import too_large
from easytodo.config import Settings
from easytodo.errors import PayloadTooLarge, app_error_handler
from easytodo.realtime.events import current_origin
from easytodo.security.csrf import CSRF_COOKIE
from easytodo.security.headers import security_headers
from easytodo.security.tokens import new_token

CLIENT_ID_HEADER = "X-Client-Id"  # sent by static/js/app.js on every htmx request
CLIENT_ID_MAX_LENGTH = 64


class CsrfCookieMiddleware:
    """Puts the CSRF token in ``request.state.csrf_token``, issuing the cookie when it is missing.

    The token goes into a page only through ``web.render``, which masks it: never read
    ``request.state.csrf_token`` into a response (or a template) any other way."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.secure = settings.cookie_secure

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        existing = HTTPConnection(scope).cookies.get(CSRF_COOKIE)
        token = existing or new_token()
        scope.setdefault("state", {})["csrf_token"] = token  # read as request.state.csrf_token
        if existing:
            await self.app(scope, receive, send)
            return
        cookie = self._cookie(token)

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("set-cookie", cookie)
            await send(message)

        await self.app(scope, receive, send_with_cookie)

    def _cookie(self, token: str) -> str:
        cookie: SimpleCookie = SimpleCookie()
        cookie[CSRF_COOKIE] = token
        cookie[CSRF_COOKIE]["path"] = "/"
        cookie[CSRF_COOKIE]["httponly"] = True
        cookie[CSRF_COOKIE]["samesite"] = "lax"
        if self.secure:
            cookie[CSRF_COOKIE]["secure"] = True
        return cookie.output(header="").strip()


class ClientIdMiddleware:
    """Sets ``current_origin`` to the request's ``X-Client-Id`` (the browser tab's id) while the
    request is handled, so the events it causes can skip that tab. A value longer than 64
    characters is ignored."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client_id = Headers(scope=scope).get(CLIENT_ID_HEADER)
        if client_id is not None and not 0 < len(client_id) <= CLIENT_ID_MAX_LENGTH:
            client_id = None
        token = current_origin.set(client_id)
        try:
            await self.app(scope, receive, send)
        finally:
            current_origin.reset(token)


class SecurityHeadersMiddleware:
    """Sets the security headers (``security_headers``: HSTS only when the public URL is https) on
    every response. The Content-Security-Policy is a default: a response that has one of its own
    (a file shown on its own, under the stricter policy of ``attachments.routes``) keeps it.

    An unhandled error is answered outside this middleware (see ``errors.server_error_handler``),
    which sets the same headers itself."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.headers = security_headers(settings)
        self.default_policy = self.headers.pop("Content-Security-Policy")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", self.default_policy)
                for name, value in self.headers.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


UPLOAD_PATH = re.compile(r"/items/\d+/attachments")
FORM_OVERHEAD = 64 * 1024  # what the rest of the upload form (field names, boundaries) may add
# What any other request that has a body may carry. The largest form is a few KB (a description is
# at most 2000 characters; a new order lists the ids of one board's categories or one checklist),
# so 1 MiB is far more than a person can send and far less than a body of any size.
BODY_LIMIT = 1024 * 1024
REQUEST_TOO_LARGE = "The request is too large."
_WITH_BODY = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class UploadSizeLimitMiddleware:
    """Stops a request body that is far too large before the app has read it all.

    The form is parsed (and, for a multipart one, spooled to a temporary file) before the route
    runs, and the CSRF check reads it too, so without this anyone, signed in or not, could make
    the server take in a body of any size. An attachment upload (``UPLOAD_PATH``) may carry the
    largest file plus ``FORM_OVERHEAD``; the service then checks the file itself against its
    limit. Every other POST, PUT, PATCH or DELETE may carry ``BODY_LIMIT``.

    A request that says it is larger (``Content-Length``) is answered 413 at once, unread. One
    that does not say (chunked) is read up to the limit; then the app is told the client is gone,
    which fails its parsing, and the answer it sends for that (a 400) is swapped for the 413.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.upload_limit = settings.attachment_max_bytes + FORM_OVERHEAD

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _WITH_BODY:
            await self.app(scope, receive, send)
            return
        if scope["method"] == "POST" and UPLOAD_PATH.fullmatch(scope["path"]):
            limit, error = self.upload_limit, too_large(self.settings)
        else:
            limit, error = BODY_LIMIT, PayloadTooLarge(REQUEST_TOO_LARGE)
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdecimal() and int(declared) > limit:
            await self._refuse(scope, receive, send, error)
            return

        received = 0
        exceeded = False
        refused = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def limited_send(message: Message) -> None:
            nonlocal refused
            if refused:  # the rest of the answer being replaced
                return
            if exceeded and message["type"] == "http.response.start" and message["status"] >= 400:
                refused = True
                await self._refuse(scope, receive, send, error)
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except ClientDisconnect:
            # Raised by a dependency that read the form (the CSRF check, with no header): no
            # answer was started.
            if not exceeded or refused:
                raise
            await self._refuse(scope, receive, send, error)

    async def _refuse(
        self, scope: Scope, receive: Receive, send: Send, error: PayloadTooLarge
    ) -> None:
        request = Request(scope, receive)
        response = await app_error_handler(request, error)
        await response(scope, receive, send)
