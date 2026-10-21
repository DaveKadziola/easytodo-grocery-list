"""Admin fixtures: a signed-in admin, the events the admin code publishes, and the live server
(for the board WebSocket)."""

import httpx
import pytest
from factories import create_user
from helpers import signed_in
from live_server import live_server  # noqa: F401 (fixture)

from easytodo.admin import service
from easytodo.models import User
from easytodo.realtime.events import BoardEvent

ADMIN_EMAIL = "admin@example.com"


@pytest.fixture
async def admin(db) -> User:
    """admin@example.com (password ``DEFAULT_PASSWORD``), the only admin."""
    return await create_user(db, ADMIN_EMAIL, is_admin=True)


@pytest.fixture
async def admin_client(client_factory, admin) -> httpx.AsyncClient:
    """A client signed in as ``admin``."""
    return await signed_in(client_factory, admin.email)


@pytest.fixture
def admin_events(monkeypatch) -> list[BoardEvent]:
    """Every event the admin service commits, in order."""
    events: list[BoardEvent] = []
    commit_and_publish = service.commit_and_publish

    async def spy(db, *published):
        events.extend(published)
        await commit_and_publish(db, *published)

    monkeypatch.setattr(service, "commit_and_publish", spy)
    return events
