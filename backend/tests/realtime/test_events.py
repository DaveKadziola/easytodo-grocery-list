import json

import httpx
import pytest
from sqlalchemy import func, select

from easytodo.middleware import ClientIdMiddleware
from easytodo.models import User
from easytodo.realtime.events import BoardEvent, EventType, commit_and_publish, current_origin


def test_event_types_use_the_wire_names():
    assert {t.name: t.value for t in EventType} == {
        "BOARD_UPDATED": "board.updated",
        "BOARD_DELETED": "board.deleted",
        "CATEGORY_CHANGED": "category.changed",
        "CATEGORY_DELETED": "category.deleted",
        "CATEGORIES_REORDERED": "categories.reordered",
        "MEMBER_REMOVED": "member.removed",
        "SESSIONS_REVOKED": "sessions.revoked",
        "RESYNC": "resync",
    }


def test_event_round_trips_through_json():
    event = BoardEvent(
        EventType.CATEGORY_CHANGED,
        board_id=3,
        category_ids=(7, 8),
        item_ids=(9,),
        user_id=5,
        session_ids=(11,),
        origin="tab-1",
    )

    payload = event.to_json()

    assert json.loads(payload) == {
        "type": "category.changed",
        "board_id": 3,
        "category_ids": [7, 8],
        "item_ids": [9],
        "user_id": 5,
        "session_ids": [11],
        "origin": "tab-1",
    }
    assert BoardEvent.from_json(payload) == event


def test_from_json_fills_in_the_defaults():
    assert BoardEvent.from_json('{"type": "resync"}') == BoardEvent(EventType.RESYNC)


async def test_commit_and_publish_commits(db):
    db.add(User(email="dave@example.com", password_hash="x"))

    await commit_and_publish(db, BoardEvent(EventType.BOARD_UPDATED, board_id=1))
    await db.rollback()  # nothing left to roll back once committed

    assert await db.scalar(select(func.count()).select_from(User)) == 1


async def _origin_seen_by_app(headers: dict[str, str]) -> str | None:
    seen: list[str | None] = []

    async def app(scope, receive, send):
        seen.append(current_origin.get())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    transport = httpx.ASGITransport(app=ClientIdMiddleware(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.get("/", headers=headers)
    return seen[0]


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"X-Client-Id": "3f0c9a52-tab"}, "3f0c9a52-tab"),
        ({"X-Client-Id": "a" * 64}, "a" * 64),
        ({"X-Client-Id": "a" * 65}, None),  # longer than 64 characters: ignored
        ({"X-Client-Id": ""}, None),
        ({}, None),
    ],
)
async def test_client_id_middleware_sets_current_origin_for_the_request(headers, expected):
    assert await _origin_seen_by_app(headers) == expected
    assert current_origin.get() is None  # reset once the request is done
