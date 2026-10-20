from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from easytodo.auth.account_routes import router as account_router
from easytodo.auth.routes import router as auth_router
from easytodo.boards.routes import router as boards_router
from easytodo.config import Settings, get_settings
from easytodo.content.routes import router as content_router
from easytodo.db import create_engine
from easytodo.errors import install_error_handlers
from easytodo.middleware import (
    ClientIdMiddleware,
    CsrfCookieMiddleware,
    SecurityHeadersMiddleware,
)
from easytodo.security.csrf import verify_csrf
from easytodo.security.ratelimit import RateLimiter
from easytodo.web import FRONTEND_DIR


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory: ``uvicorn --factory easytodo.main:create_app``."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            yield
        finally:
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
    app.include_router(content_router)
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")
    return app
