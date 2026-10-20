from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from easytodo.config import Settings, get_settings
from easytodo.db import create_engine


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

    app = FastAPI(title="EasyToDo", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        try:
            async with request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError):
            return JSONResponse({"status": "degraded", "db": "error"}, status_code=503)
        return JSONResponse({"status": "ok", "db": "ok"})

    return app
