"""Browser tests (``pytest -m e2e``): Chromium pages signed in to a live server, and test data
written straight into its database.

Everything here is synchronous (Playwright's sync API); see live_server.py for why the database
work runs in a worker thread.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from factories import DEFAULT_PASSWORD, create_user
from live_server import LiveServer, in_worker_thread, live_server  # noqa: F401 (fixture)
from playwright.sync_api import Browser, BrowserContext, Page, expect
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from easytodo.models import User

PHONE = {"viewport": {"width": 375, "height": 812}, "is_mobile": True, "has_touch": True}

type Seed = Callable[[Callable[[AsyncSession], Awaitable[Any]]], Any]


@pytest.fixture
def seed(live_server: LiveServer) -> Seed:  # noqa: F811
    """``seed(lambda db: create_board(db, ...))`` -> what the coroutine returns: runs it with a
    session on the live server's database (objects come back detached, attributes loaded)."""

    def run(make: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
        async def main() -> Any:
            engine = create_async_engine(live_server.settings.database_url, poolclass=NullPool)
            try:
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    return await make(db)
            finally:
                await engine.dispose()

        return in_worker_thread(asyncio.run, main())

    return run


@pytest.fixture
def email() -> str:
    return "dave@example.com"


@pytest.fixture
def user(seed: Seed, email: str) -> User:
    """The account the signed-in pages use (password ``DEFAULT_PASSWORD``, no boards)."""
    return seed(lambda db: create_user(db, email))


def sign_in(context: BrowserContext, email: str) -> Page:
    """A new page in ``context``, signed in as ``email`` through the login form."""
    page = context.new_page()
    page.goto("/login")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(DEFAULT_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(re.compile(r"/boards$"))
    return page


@pytest.fixture
def signed_in_page(
    browser: Browser,
    live_server: LiveServer,  # noqa: F811
    user: User,
) -> Iterator[Callable[..., Page]]:
    """``signed_in_page(**context_options)`` -> a page signed in as ``user``, in a new browser
    context (own cookies) with these options (viewport, has_touch, ...)."""
    contexts: list[BrowserContext] = []

    def open_page(**context_options: Any) -> Page:
        context = browser.new_context(base_url=live_server.url, **context_options)
        contexts.append(context)
        return sign_in(context, user.email)

    yield open_page
    for context in contexts:
        context.close()


@pytest.fixture
def logged_in_page(signed_in_page: Callable[..., Page]) -> Page:
    return signed_in_page()


@pytest.fixture
def logged_in_page_mobile(signed_in_page: Callable[..., Page]) -> Page:
    """A phone: 375×812 viewport with touch."""
    return signed_in_page(**PHONE)
