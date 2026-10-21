from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.responses import Response
from starlette.types import Scope

from easytodo.admin.routes import router as admin_router
from easytodo.auth.account_routes import router as account_router
from easytodo.auth.routes import router as auth_router
from easytodo.boards.invite_routes import router as invite_router
from easytodo.boards.permissions import Action, can
from easytodo.boards.routes import router as boards_router
from easytodo.config import Settings, get_settings
from easytodo.content.routes import router as content_router
from easytodo.db import create_engine
from easytodo.errors import install_error_handlers
from easytodo.mail.mailer import get_mailer
from easytodo.middleware import (
    ClientIdMiddleware,
    CsrfCookieMiddleware,
    SecurityHeadersMiddleware,
)
from easytodo.realtime.hub import Hub
from easytodo.realtime.listener import EventListener, listener_dsn
from easytodo.realtime.routes import router as realtime_router
from easytodo.security.csrf import verify_csrf
from easytodo.security.ratelimit import RateLimiter
from easytodo.web import STATIC_DIR, templates

IMMUTABLE = "public, max-age=31536000, immutable"


class CachedStaticFiles(StaticFiles):
    """``/static``: a URL with a version (``?v=``, from ``static_url``) may be cached for good;
    any other is revalidated (ETag) on every use, so a phone never keeps an old file after an
    update."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        versioned = "v" in parse_qs(scope.get("query_string", b"").decode("latin-1"))
        response.headers["Cache-Control"] = IMMUTABLE if versioned else "no-cache"
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory: ``uvicorn --factory easytodo.main:create_app``."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        app.state.mailer = get_mailer(settings)
        # This worker's board sockets, and the LISTEN connection that feeds them every event.
        app.state.hub = hub = Hub()
        listener = EventListener(
            listener_dsn(settings.database_url),
            on_event=hub.dispatch,
            on_reconnect=hub.reconnect_all,
        )
        await listener.start()
        try:
            yield
        finally:
            await listener.stop()
            await engine.dispose()

    # No /docs, /redoc or /openapi.json: no public API, and Swagger UI's CDN assets break the CSP.
    app = FastAPI(
        title="EasyToDo",
        lifespan=lifespan,
        dependencies=[Depends(verify_csrf)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.rate_limiter = RateLimiter()
    install_error_handlers(app)
    # Templates show controls by the member's role: {% if can(role, Action.EDIT_CONTENT) %}.
    templates.env.globals.update(can=can, Action=Action)
    app.add_middleware(ClientIdMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(CsrfCookieMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)  # outermost

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        try:
            async with request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError):
            return JSONResponse({"status": "degraded", "db": "error"}, status_code=503)
        return JSONResponse({"status": "ok", "db": "ok"})

    app.include_router(auth_router)
    app.include_router(account_router)
    app.include_router(boards_router)
    app.include_router(invite_router)
    app.include_router(content_router)
    app.include_router(realtime_router)
    app.include_router(admin_router)
    app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")
    return app
