"""Mini apps wired like ``create_app`` (global ``verify_csrf``, the same middleware and error
handlers), with test-only routes, so the production app carries no test routes."""

from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import Depends, FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse
from pydantic import AnyHttpUrl

from easytodo.config import Settings
from easytodo.errors import (
    AppError,
    Conflict,
    Forbidden,
    NotAuthenticated,
    NotFound,
    ValidationFailed,
    install_error_handlers,
)
from easytodo.middleware import CsrfCookieMiddleware, SecurityHeadersMiddleware
from easytodo.security.csrf import verify_csrf
from easytodo.security.ratelimit import RateLimiter, rate_limit

# GET /raise-<status> raises these. The 409 message carries HTML to check escaping, the 422
# one a non-latin-1 character (en dash) to check header encoding.
ERRORS: dict[int, Callable[[], AppError]] = {
    401: NotAuthenticated,
    403: lambda: Forbidden("You don't have permission to do that."),
    404: NotFound,
    409: lambda: Conflict('A category named <b>O\'Reilly "x"</b> already exists.'),
    422: lambda: ValidationFailed("Board name must be 1–80 characters."),
}


def build_mini_app(settings: Settings) -> FastAPI:
    app = FastAPI(dependencies=[Depends(verify_csrf)])
    app.state.settings = settings
    app.state.rate_limiter = RateLimiter()
    install_error_handlers(app)
    app.add_middleware(CsrfCookieMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)

    @app.get("/token")
    async def token(request: Request) -> PlainTextResponse:
        return PlainTextResponse(request.state.csrf_token)

    @app.post("/submit")
    async def submit() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/limited", dependencies=[Depends(rate_limit("test", 2, 60))])
    async def limited() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/validate")
    async def validate(n: int) -> dict[str, int]:
        return {"n": n}

    @app.get("/raise-{status}")
    async def raise_error(status: int) -> None:
        raise ERRORS[status]()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    return app


def mini_client(app: FastAPI, origin: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=origin, headers={"Origin": origin}
    )


@pytest.fixture
def mini_app(settings: Settings) -> FastAPI:
    return build_mini_app(settings)


@pytest.fixture
async def csrf_app_client(
    mini_app: FastAPI, settings: Settings
) -> AsyncIterator[httpx.AsyncClient]:
    async with mini_client(mini_app, settings.public_origin) as c:
        yield c


@pytest.fixture
async def err_app_client(mini_app: FastAPI, settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async with mini_client(mini_app, settings.public_origin) as c:
        yield c


@pytest.fixture
def extra_origin() -> str:
    """The origin of a second address the app is opened at (``EASYTODO_EXTRA_ORIGINS``)."""
    return "http://192.168.122.50:8000"


@pytest.fixture
async def extra_origin_app_client(
    settings: Settings, extra_origin: str
) -> AsyncIterator[httpx.AsyncClient]:
    """A mini app whose ``extra_origins`` is ``[extra_origin]``, and a client of it opened at
    that address (base URL and ``Origin``)."""
    extra = Settings(_env_file=None, **(settings.model_dump() | {"extra_origins": [extra_origin]}))
    async with mini_client(build_mini_app(extra), extra_origin) as c:
        yield c


@pytest.fixture
async def https_csrf_app_client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """A mini app whose public URL is https://testserver (secure cookies, HSTS)."""
    https = settings.model_copy(update={"public_url": AnyHttpUrl("https://testserver")})
    async with mini_client(build_mini_app(https), https.public_origin) as c:
        yield c
