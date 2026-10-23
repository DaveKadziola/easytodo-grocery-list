"""The hub: which board sockets get which event, and which sockets are closed with which code."""

import json

from easytodo.realtime.events import BoardEvent, EventType
from easytodo.realtime.hub import QUEUE_SIZE, Hub, Subscriber


def sub(
    board_id: int = 1, user_id: int = 1, session_id: int = 1, client_id: str | None = None
) -> Subscriber:
    return Subscriber(
        board_id=board_id, user_id=user_id, session_id=session_id, client_id=client_id
    )


def messages(subscriber: Subscriber) -> list[dict]:
    """Everything queued for ``subscriber`` (taken off its queue)."""
    taken = []
    while not subscriber.queue.empty():
        taken.append(json.loads(subscriber.queue.get_nowait()))
    return taken


def hub_with(*subscribers: Subscriber) -> Hub:
    hub = Hub()
    for subscriber in subscribers:
        hub.subscribe(subscriber)
    return hub


async def test_dispatch_skips_origin_client_and_other_boards():
    a, b, c = sub(board_id=1, client_id="A"), sub(board_id=1, client_id="B"), sub(board_id=2)
    hub = hub_with(a, b, c)

    await hub.dispatch(
        BoardEvent(EventType.CATEGORY_CHANGED, board_id=1, category_ids=(7,), origin="A")
    )

    assert a.queue.empty() and c.queue.empty()
    assert json.loads(b.queue.get_nowait()) == {
        "type": "category.changed",
        "board_id": 1,
        "category_ids": [7],
    }
    assert (a.close_code, b.close_code, c.close_code) == (None, None, None)


async def test_item_ids_reach_the_page_only_when_there_are_some():
    page = sub()
    hub = hub_with(page)

    await hub.dispatch(
        BoardEvent(EventType.CATEGORY_CHANGED, board_id=1, category_ids=(7,), item_ids=(3,))
    )
    await hub.dispatch(BoardEvent(EventType.CATEGORY_CHANGED, board_id=1, category_ids=(7,)))

    assert messages(page) == [
        {"type": "category.changed", "board_id": 1, "category_ids": [7], "item_ids": [3]},
        {"type": "category.changed", "board_id": 1, "category_ids": [7]},  # no key at all
    ]


async def test_event_without_origin_reaches_sockets_without_client_id():
    # No origin (a plain form post) must not match a socket that gave no client id.
    anonymous_tab = sub(client_id=None)
    hub = hub_with(anonymous_tab)

    await hub.dispatch(BoardEvent(EventType.CATEGORIES_REORDERED, board_id=1))

    assert messages(anonymous_tab) == [
        {"type": "categories.reordered", "board_id": 1, "category_ids": []}
    ]


async def test_full_queue_disconnects_subscriber_with_1013():
    slow, fast = sub(client_id="slow"), sub(client_id="fast")
    hub = hub_with(slow, fast)
    for _ in range(QUEUE_SIZE):
        slow.queue.put_nowait("{}")

    await hub.dispatch(BoardEvent(EventType.CATEGORY_CHANGED, board_id=1, category_ids=(7,)))

    assert slow.close_code == 1013
    assert slow.queue.empty()  # nothing more is sent: the page reloads the board on reconnect
    assert slow not in hub.subscribers()
    assert fast.close_code is None and len(messages(fast)) == 1
    assert QUEUE_SIZE == 100


async def test_member_removed_closes_only_that_user_with_4403():
    removed_tab, removed_phone = sub(user_id=5, session_id=1), sub(user_id=5, session_id=2)
    owner = sub(user_id=1, session_id=3, client_id="owner-tab")
    owner_other_tab = sub(user_id=1, session_id=3, client_id="other-tab")
    other_board = sub(board_id=2, user_id=5, session_id=1)
    hub = hub_with(removed_tab, removed_phone, owner, owner_other_tab, other_board)

    await hub.dispatch(
        BoardEvent(EventType.MEMBER_REMOVED, board_id=1, user_id=5, origin="owner-tab")
    )

    for removed in (removed_tab, removed_phone):
        assert messages(removed) == [{"type": "member.removed", "board_id": 1, "category_ids": []}]
        assert removed.close_code == 4403
        assert removed not in hub.subscribers()
    assert messages(owner) == []  # its own change
    assert messages(owner_other_tab) == [
        {"type": "board.updated", "board_id": 1, "category_ids": []}
    ]
    assert owner_other_tab.close_code is None
    assert messages(other_board) == [] and other_board.close_code is None


async def test_board_deleted_closes_board_sockets_with_4404():
    owner_tab = sub(user_id=1, client_id="owner-tab")
    member = sub(user_id=2, session_id=2)
    other_board = sub(board_id=2, user_id=2, session_id=2)
    hub = hub_with(owner_tab, member, other_board)

    await hub.dispatch(BoardEvent(EventType.BOARD_DELETED, board_id=1, origin="owner-tab"))

    for subscriber in (owner_tab, member):  # the origin too: its board is gone
        assert messages(subscriber) == [
            {"type": "board.deleted", "board_id": 1, "category_ids": []}
        ]
        assert subscriber.close_code == 4404
    assert hub.subscribers() == [other_board]
    assert other_board.close_code is None and messages(other_board) == []


async def test_sessions_revoked_closes_matching_sockets_with_4401():
    revoked_a, revoked_b = sub(board_id=1, session_id=10), sub(board_id=2, session_id=11)
    kept = sub(board_id=1, session_id=12)
    revoked_a.queue.put_nowait('{"type": "board.updated"}')  # not sent any more
    hub = hub_with(revoked_a, revoked_b, kept)

    await hub.dispatch(BoardEvent(EventType.SESSIONS_REVOKED, user_id=1, session_ids=(10, 11)))

    assert (revoked_a.close_code, revoked_b.close_code, kept.close_code) == (4401, 4401, None)
    assert revoked_a.queue.empty() and revoked_b.queue.empty()
    assert hub.subscribers() == [kept]
    assert messages(kept) == []


async def test_role_change_tells_only_that_member_to_reload():
    member_tab, member_phone = sub(user_id=5, session_id=1), sub(user_id=5, session_id=2)
    other = sub(user_id=6, session_id=3)
    owner = sub(user_id=1, session_id=4, client_id="owner-tab")
    hub = hub_with(member_tab, member_phone, other, owner)

    await hub.dispatch(
        BoardEvent(EventType.BOARD_UPDATED, board_id=1, user_id=5, origin="owner-tab")
    )

    for member in (member_tab, member_phone):
        assert messages(member) == [
            {"type": "board.updated", "board_id": 1, "category_ids": [], "reload": True}
        ]
    assert messages(other) == [{"type": "board.updated", "board_id": 1, "category_ids": []}]
    assert messages(owner) == []
    assert all(s.close_code is None for s in (member_tab, member_phone, other, owner))


async def test_reconnect_all_closes_every_socket_with_1013():
    """After the listener lost its connection, events may have been missed, revocations and
    removals too: every socket is closed with 1013, so its page reconnects (and reloads its
    board), and the reconnect checks the session and the membership again."""
    subscribers = [sub(board_id=1, client_id="A"), sub(board_id=2), sub(board_id=3)]
    hub = hub_with(*subscribers)
    await hub.dispatch(BoardEvent(EventType.BOARD_UPDATED, board_id=1))

    await hub.reconnect_all()

    assert [s.close_code for s in subscribers] == [1013] * 3
    assert [messages(s) for s in subscribers] == [[]] * 3  # all is reloaded on reconnect anyway
    assert hub.subscribers() == []


async def test_unsubscribed_socket_gets_nothing():
    gone, staying = sub(session_id=1), sub(session_id=2)
    hub = hub_with(gone, staying)
    hub.unsubscribe(gone)
    hub.unsubscribe(gone)  # twice is fine (the route always unsubscribes on the way out)

    await hub.dispatch(BoardEvent(EventType.BOARD_UPDATED, board_id=1))

    assert gone.queue.empty()
    assert len(messages(staying)) == 1
    assert hub.subscribers() == [staying]
    await hub.reconnect_all()
    assert gone.close_code is None and staying.close_code == 1013
