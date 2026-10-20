"""Pure ASGI middleware (no BaseHTTPMiddleware: no extra task per request, context vars intact)."""

from http.cookies import SimpleCookie

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from easytodo.config import Settings
from easytodo.realtime.events import current_origin
from easytodo.security.csrf import CSRF_COOKIE
from easytodo.security.tokens import new_token

CLIENT_ID_HEADER = "X-Client-Id"  # sent by static/js/app.js on every htmx request
CLIENT_ID_MAX_LENGTH = 64

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)


class CsrfCookieMiddleware:
    """Puts the CSRF token in ``request.state.csrf_token``, issuing the cookie when it is missing."""

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
    """Sets the security headers (HSTS only when the public URL is https) on every response."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.headers = {
            "Content-Security-Policy": CONTENT_SECURITY_POLICY,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
            "X-Frame-Options": "DENY",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }
        if settings.cookie_secure:
            self.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
