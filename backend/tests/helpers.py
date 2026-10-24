"""Shared test steps that drive the app over HTTP, the names that test escaping and typed
confirmations, reading pages and toasts, counting the SQL a block runs, and waiting for another
connection's row lock."""

import asyncio
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import quote, unquote

import httpx
from factories import DEFAULT_PASSWORD
from sqlalchemy import event, text

from easytodo.security.csrf import CSRF_COOKIE, CSRF_HEADER
from easytodo.web import FLASH_COOKIE

# A name with quotes, HTML and a script, and how the templates must show it (autoescaped).
NASTY = 'O\'Reilly <b>"x"</b> <script>alert(1)</script>'
NASTY_ESCAPED = "O&#39;Reilly &lt;b&gt;&#34;x&#34;&lt;/b&gt; &lt;script&gt;alert(1)&lt;/script&gt;"

# The sentence the "Remove" confirmation of a member gets while the board has an invite link (a
# link works for anyone who has it, so the person removed could come back with one).
LINKS_STILL_WORK = (
    "The invite links below still work, so they could rejoin with one. Revoke a link to stop that."
)

# The CSRF token as a page carries it: masked afresh on every render, so no two pages (or renders
# of one page) have the same text. In the meta tag, and as the hidden field of every form.
META_CSRF = re.compile(r'<meta name="csrf-token" content="([^"]*)">')
FIELD_CSRF = re.compile(r'<input type="hidden" name="csrf_token" value="([^"]*)">')

# Names typed into an hx-prompt to confirm a permanent delete, each with the headers htmx sends
# it in (Review Focus 3): ``(name, headers)``, for ``pytest.mark.parametrize``.
TYPED_NAMES = [
    ("Łosoś", {"HX-Prompt": quote("Łosoś", safe=""), "HX-Prompt-URI-AutoEncoded": "true"}),
    ("Café", {"HX-Prompt": "Café".encode("latin-1")}),  # Latin-1: htmx sends it as it is
    ('O\'Reilly <b>"x"</b>', {"HX-Prompt": ' O\'Reilly <b>"x"</b> '}),
    ("Deals %41", {"HX-Prompt": "Deals %41"}),  # not marked as encoded: taken literally
]


async def login(client: httpx.AsyncClient, email: str, password: str = DEFAULT_PASSWORD) -> None:
    """Sign ``client`` in through the login form; later requests carry the CSRF header."""
    await client.get("/login")  # issues the CSRF cookie
    token = client.cookies[CSRF_COOKIE]
    r = await client.post(
        "/login", data={"email": email, "password": password, "csrf_token": token}
    )
    assert r.status_code == 303, r.text
    client.headers[CSRF_HEADER] = token


async def signed_in(client_factory, email: str) -> httpx.AsyncClient:
    """A new client (own cookies) from ``client_factory``, signed in as ``email``."""
    client = await client_factory()
    await login(client, email)
    return client


def toast_message(response: httpx.Response) -> str:
    """The toast of an htmx request's response (its ``HX-Trigger`` header)."""
    return json.loads(response.headers["hx-trigger"])["toast"]["message"]


def without_csrf_mask(html: str) -> str:
    """``html`` with each CSRF mask replaced by a placeholder, to compare whole pages that differ
    in nothing else (every render has a mask of its own)."""
    html = META_CSRF.sub('<meta name="csrf-token" content="[masked]">', html)
    return FIELD_CSRF.sub('<input type="hidden" name="csrf_token" value="[masked]">', html)


def text_of(html: str) -> str:
    """``html`` without its tags, each run of whitespace as one space."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def flash_of(response: httpx.Response) -> dict:
    """The toast ``response`` leaves for the next page (its flash cookie), as
    ``{"level": ..., "message": ...}``."""
    return json.loads(unquote(response.cookies[FLASH_COOKIE]))


@contextmanager
def count_queries(db) -> Iterator[list[str]]:
    """The SQL statements ``db``'s engine runs inside the block."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def wait_until_a_query_waits_for_a_lock(db) -> None:
    """Return once some query on the test database waits for a lock (a request blocked by the row
    lock another connection of the test holds); fail after 5 s."""
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        async with db.bind.connect() as conn:  # a fresh snapshot of pg_stat_activity each time
            waiting = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no query started waiting for the row lock")
