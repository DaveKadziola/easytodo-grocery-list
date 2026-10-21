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
from factories import DEFAULT_PASSWORD, create_board, create_category, create_item, create_user
from live_server import LiveServer, in_worker_thread, live_server  # noqa: F401 (fixture)
from playwright.sync_api import Browser, BrowserContext, Page, expect
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from easytodo.models import User

PHONE = {"viewport": {"width": 375, "height": 812}, "is_mobile": True, "has_touch": True}

# Counts the board content reloads that are in place (swapped in, htmx set up on them) in
# <html data-board-reloads>: an attribute, which expect() can wait for under the page's CSP
# (page.wait_for_function cannot: it polls with eval).
COUNT_BOARD_RELOADS = r"""
document.addEventListener("htmx:afterSettle", (event) => {
  const { xhr } = event.detail;
  if (xhr && /^\/boards\/\d+\/content$/.test(new URL(xhr.responseURL).pathname)) {
    const root = document.documentElement;
    root.dataset.boardReloads = Number(root.dataset.boardReloads || 0) + 1;
  }
});
"""


def watch_console(page: Page) -> list[str]:
    """Every console error or warning and every uncaught exception on ``page`` from now on."""
    problems: list[str] = []
    page.on(
        "console",
        lambda msg: (
            problems.append(f"{msg.type}: {msg.text}") if msg.type in ("error", "warning") else None
        ),
    )
    page.on("pageerror", lambda exc: problems.append(f"pageerror: {exc}"))
    return problems


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
    # The board list, or straight to the board when the user has only one ("/" opens there).
    expect(page).to_have_url(re.compile(r"/boards(/\d+)?$"))
    return page


@pytest.fixture
def signed_in_as(
    browser: Browser,
    live_server: LiveServer,  # noqa: F811
) -> Iterator[Callable[..., Page]]:
    """``signed_in_as(email, **context_options)`` -> a page signed in as that (existing) user, in
    a new browser context (own cookies, so another device) with these options (viewport,
    has_touch, ...)."""
    contexts: list[BrowserContext] = []

    def open_page(email: str, **context_options: Any) -> Page:
        context = browser.new_context(base_url=live_server.url, **context_options)
        context.add_init_script(COUNT_BOARD_RELOADS)
        contexts.append(context)
        return sign_in(context, email)

    yield open_page
    for context in contexts:
        context.close()


@pytest.fixture
def signed_in_page(signed_in_as: Callable[..., Page], user: User) -> Callable[..., Page]:
    """``signed_in_page(**context_options)`` -> a page signed in as ``user`` (as
    ``signed_in_as``)."""
    return lambda **context_options: signed_in_as(user.email, **context_options)


@pytest.fixture
def logged_in_page(signed_in_page: Callable[..., Page]) -> Page:
    return signed_in_page()


@pytest.fixture
def logged_in_page_mobile(signed_in_page: Callable[..., Page]) -> Page:
    """A phone: 375×812 viewport with touch."""
    return signed_in_page(**PHONE)


def _open_board(page: Page, board_id: int) -> None:
    page.goto(f"/boards/{board_id}")
    expect(page.locator("#conn-status")).to_have_text("Live")
    expect(page.locator("html")).to_have_attribute("data-board-reloads", re.compile(r"^[1-9]"))


@pytest.fixture
def open_board() -> Callable[[Page, int], None]:
    """``open_board(page, board_id)``: go to the board's page (on a page from these fixtures) and
    wait until it is live: connected, and its content reloaded once more and in place, as on
    every (re)connect. Changes made from then on reach the page as events, and nothing the test
    does next is swapped away by that reload."""
    return _open_board


@pytest.fixture
def seeded_board(seed: Seed, user: User) -> int:
    """A board "Groceries" of ``user`` (its id) that fills a screen: categories with ticked and open
    items, an empty one, and long names that do not break by themselves."""

    async def make(db: AsyncSession) -> int:
        board = await create_board(db, user, "Groceries")
        categories = {
            "Dairy": ["Milk", "Yogurt", "Butter"],
            "Bakery": ["Bread", "Croissants"],
            "Fruit and vegetables": ["Apples", "Supercalifragilisticexpialidocious-bananas"],
            "Household": ["Washing-up liquid", "Bin bags"],
            "Pharmacyandpersonalcarewithaveryveryverylongname": [],
        }
        for position, (name, items) in enumerate(categories.items()):
            category = await create_category(db, board, name, position)
            for item_name in items:
                await create_item(db, category, item_name, is_done=item_name == "Milk")
        return board.id

    return seed(make)


@pytest.fixture
def logged_in_page_with_seed(logged_in_page: Page, seeded_board: int) -> Page:
    """``logged_in_page`` on ``seeded_board``, live."""
    _open_board(logged_in_page, seeded_board)
    return logged_in_page


@pytest.fixture
def two_logged_in_pages(
    signed_in_page: Callable[..., Page], seed: Seed, user: User
) -> tuple[Page, Page]:
    """Two devices of ``user`` (own browser contexts, so own sessions), both live on the same
    board: "Groceries", with the category "Dairy" holding "Milk"."""

    async def make(db: AsyncSession) -> int:
        board = await create_board(db, user, "Groceries")
        await create_item(db, await create_category(db, board, "Dairy"), "Milk")
        return board.id

    board_id = seed(make)
    a, b = signed_in_page(), signed_in_page()
    for page in (a, b):
        _open_board(page, board_id)
    return a, b
