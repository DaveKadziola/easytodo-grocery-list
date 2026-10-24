"""Notifications: who is told about membership, role, archive and moved-category changes, what the
bell's badge and panel show (the last 30 days, escaped), and that reading clears only the reader's
own badge and tells their other devices."""

import re
from datetime import timedelta
from types import SimpleNamespace

from factories import (
    create_board,
    create_category,
    create_notification,
    create_user,
)
from helpers import NASTY, NASTY_ESCAPED, count_queries, signed_in
from sqlalchemy import func, select, update

from easytodo.boards.invites import create_link_invite
from easytodo.models import BoardRole, Notification, NotificationKind
from easytodo.notifications.service import (
    MESSAGE_MAX_LENGTH,
    MESSAGES,
    RETENTION,
    notify,
    recent,
    unread_count,
)
from easytodo.realtime.events import BoardEvent, EventType
from easytodo.security.csrf import CSRF_COOKIE, CSRF_HEADER
from easytodo.web import static_url

HX = {"HX-Request": "true"}
BADGE_OF_3 = '<span id="notifications-badge" class="notifications-badge">3<span class="sr-only"> unread</span></span>'
NO_BADGE = '<span id="notifications-badge" hidden></span>'


async def notifications_of(db, user) -> list[tuple[str, int | None]]:
    """What ``user`` was told, oldest first: ``(message, board id)``."""
    rows = await db.execute(
        select(Notification.message, Notification.board_id)
        .where(Notification.user_id == user.id)
        .order_by(Notification.id)
    )
    return [(message, board_id) for message, board_id in rows]


def test_every_kind_has_its_message_and_the_retention_is_30_days():
    assert set(MESSAGES) == set(NotificationKind)
    assert MESSAGES == {
        NotificationKind.MEMBER_JOINED: "{actor} joined “{board}”.",
        NotificationKind.MEMBER_LEFT: "{actor} left “{board}”.",
        NotificationKind.MEMBER_REMOVED: "{actor} removed you from “{board}”.",
        NotificationKind.ROLE_CHANGED: "{actor} changed your role on “{board}” to {role}.",
        NotificationKind.BOARD_ARCHIVED: "{actor} archived “{board}”.",
        NotificationKind.BOARD_RESTORED: "{actor} restored “{board}”.",
        NotificationKind.CATEGORY_MOVED: "{actor} moved “{category}” from “{source}” to “{target}”.",
    }
    assert RETENTION == timedelta(days=30)
    assert MESSAGE_MAX_LENGTH == 600


async def test_membership_and_role_changes_notify_the_right_person(client_factory, db, team):
    board, owner, eve, frank = team
    vic = await create_user(db, "vic@example.com")
    token, _ = await create_link_invite(db, board.id, owner, BoardRole.EDITOR)

    r = await (await signed_in(client_factory, "vic@example.com")).post(f"/invite/{token}/accept")

    assert r.status_code == 303
    joined = ("vic joined “Groceries”.", board.id)
    assert await notifications_of(db, owner) == [joined]
    assert [await notifications_of(db, user) for user in (eve, frank, vic)] == [[], [], []]

    owner_client = await signed_in(client_factory, "owner@example.com")
    for _ in range(2):  # the second time changes nothing, and tells nobody
        r = await owner_client.patch(
            f"/boards/{board.id}/members/{eve.id}", data={"role": "viewer"}, headers=HX
        )
        assert r.status_code == 200
    changed = ("owner changed your role on “Groceries” to viewer.", board.id)
    assert await notifications_of(db, eve) == [changed]

    r = await (await signed_in(client_factory, "eve@example.com")).post(
        f"/boards/{board.id}/leave", headers=HX
    )
    assert r.status_code == 200
    assert await notifications_of(db, owner) == [joined, ("Eve Adams left “Groceries”.", board.id)]
    assert await notifications_of(db, eve) == [changed]  # leaving tells the owner, not oneself

    r = await owner_client.delete(f"/boards/{board.id}/members/{frank.id}", headers=HX)
    assert r.status_code == 200
    # No board to open any more.
    assert await notifications_of(db, frank) == [("owner removed you from “Groceries”.", None)]
    assert len(await notifications_of(db, owner)) == 2  # the owner's own act is no news to them
    assert await notifications_of(db, vic) == []


async def test_archive_and_restore_notify_members_but_not_the_owner(client_factory, db, team):
    board, owner, eve, frank = team
    owner_client = await signed_in(client_factory, "owner@example.com")

    r = await owner_client.post(f"/boards/{board.id}/archive", headers=HX)

    assert r.status_code == 200
    archived = ("owner archived “Groceries”.", None)  # an archived board cannot be opened
    assert [await notifications_of(db, user) for user in (eve, frank)] == [[archived]] * 2
    assert await notifications_of(db, owner) == []

    r = await owner_client.post(f"/boards/{board.id}/restore", headers=HX)

    assert r.status_code == 200
    restored = ("owner restored “Groceries”.", board.id)
    assert [await notifications_of(db, user) for user in (eve, frank)] == [[archived, restored]] * 2
    assert await notifications_of(db, owner) == []


async def two_boards(db):
    """Home (owner; eve an editor, frank a viewer) and Shop (gina; eve an editor, owner a viewer):
    eve may move a category from one to the other."""
    owner = await create_user(db, "owner@example.com")
    eve = await create_user(db, "eve@example.com")
    frank = await create_user(db, "frank@example.com")
    gina = await create_user(db, "gina@example.com")
    home = await create_board(db, owner, "Home", {eve: BoardRole.EDITOR, frank: BoardRole.VIEWER})
    shop = await create_board(db, gina, "Shop", {eve: BoardRole.EDITOR, owner: BoardRole.VIEWER})
    return SimpleNamespace(owner=owner, eve=eve, frank=frank, gina=gina, home=home, shop=shop)


async def test_moving_a_category_notifies_each_member_of_both_boards_once(client_factory, db):
    t = await two_boards(db)
    dairy = await create_category(db, t.home, "Dairy")
    eve_client = await signed_in(client_factory, "eve@example.com")

    r = await eve_client.post(
        f"/categories/{dairy.id}/move", data={"board_id": t.shop.id, "name": "Dairy"}, headers=HX
    )

    assert r.status_code == 200
    moved = "eve moved “Dairy” from “Home” to “Shop”."
    # Whoever can see it on the target board is sent there (the owner is on both boards: once);
    # whoever only lost it is sent to the board it left. Not the one who moved it.
    assert await notifications_of(db, t.owner) == [(moved, t.shop.id)]
    assert await notifications_of(db, t.gina) == [(moved, t.shop.id)]
    assert await notifications_of(db, t.frank) == [(moved, t.home.id)]
    assert await notifications_of(db, t.eve) == []


async def test_a_moved_category_is_named_as_it_is_after_the_move(client_factory, db):
    t = await two_boards(db)
    dairy = await create_category(db, t.home, "Dairy")
    eve_client = await signed_in(client_factory, "eve@example.com")

    r = await eve_client.post(
        f"/categories/{dairy.id}/move",
        data={"board_id": t.shop.id, "name": " Fresh   dairy "},
        headers=HX,
    )

    assert r.status_code == 200
    assert await notifications_of(db, t.frank) == [
        ("eve moved “Fresh dairy” from “Home” to “Shop”.", t.home.id)
    ]


async def test_nothing_is_told_when_a_change_does_not_happen(client_factory, db):
    t = await two_boards(db)
    dairy = await create_category(db, t.home, "Dairy")
    await create_category(db, t.shop, "Dairy")
    eve_client = await signed_in(client_factory, "eve@example.com")
    frank_client = await signed_in(client_factory, "frank@example.com")

    # The name is taken on the target board; a viewer may not move; nor change roles or archive.
    r = await eve_client.post(
        f"/categories/{dairy.id}/move", data={"board_id": t.shop.id, "name": "Dairy"}, headers=HX
    )
    assert r.status_code == 409
    r = await frank_client.post(
        f"/categories/{dairy.id}/move",
        data={"board_id": t.shop.id, "name": "Dairy 2"},
        headers=HX,
    )
    assert r.status_code == 403
    r = await eve_client.patch(
        f"/boards/{t.home.id}/members/{t.frank.id}", data={"role": "editor"}, headers=HX
    )
    assert r.status_code == 403
    r = await eve_client.post(f"/boards/{t.home.id}/archive", headers=HX)
    assert r.status_code == 403

    assert (await db.scalar(select(func.count()).select_from(Notification))) == 0


async def test_notify_skips_duplicates_and_truncates(db, team):
    _, _, eve, frank = team

    events = notify(
        db,
        [eve.id, eve.id, frank.id],
        NotificationKind.MEMBER_LEFT,
        board_id=None,
        actor="x" * 700,
        board="G",
    )

    assert events == [
        BoardEvent(EventType.NOTIFICATIONS_CHANGED, user_id=u) for u in (eve.id, frank.id)
    ]
    # Added, not flushed (the test database counts bytes where a UTF-8 one counts characters, and
    # "…" is three of them): one row each, 600 characters, the last the ellipsis.
    rows = sorted(db.new, key=lambda n: n.user_id)
    assert [(n.user_id, n.kind, n.board_id, n.read_at) for n in rows] == [
        (eve.id, "member_left", None, None),
        (frank.id, "member_left", None, None),
    ]
    assert [n.message for n in rows] == ["x" * 599 + "…"] * 2
    assert len(rows[0].message) == MESSAGE_MAX_LENGTH == 600
    await db.rollback()


async def test_notify_leaves_the_commit_to_the_caller(db, team):
    _, owner, *_ = team

    notify(db, [owner.id], NotificationKind.BOARD_RESTORED, board_id=None, actor="a", board="G")
    await db.rollback()

    assert await db.scalar(select(func.count()).select_from(Notification)) == 0


async def test_badge_and_panel_show_the_last_30_days(client_factory, db, team):
    board, owner, eve, _ = team
    for n in range(3):
        await create_notification(db, owner, message=f"new {n}", board=board)
    await create_notification(db, owner, message="seen", read=True)
    stale = await create_notification(db, owner, message="31 days ago")
    fresh = await create_notification(db, owner, message="29 days ago")
    await db.execute(
        update(Notification)
        .where(Notification.id == stale.id)
        .values(created_at=func.now() - timedelta(days=31))
    )
    await db.execute(
        update(Notification)
        .where(Notification.id == fresh.id)
        .values(created_at=func.now() - timedelta(days=29), read_at=func.now())
    )
    for n in range(25):
        await create_notification(db, eve, message=f"n{n}", read=n < 10)
    await db.commit()
    owner_client = await signed_in(client_factory, "owner@example.com")

    r = await owner_client.get("/notifications/badge")

    assert r.status_code == 200
    assert r.text == BADGE_OF_3  # the one from 31 days ago is not counted, the seen ones neither
    with count_queries(db) as statements:
        assert await unread_count(db, owner) == 3
    assert len(statements) == 1
    with count_queries(db) as statements:
        listed = await recent(db, eve)
    assert len(statements) == 1
    assert [n.message for n in listed] == [
        f"n{n}" for n in range(24, 4, -1)
    ]  # 20 of 25, newest first
    assert [n.message for n in await recent(db, owner)] == [
        "seen",
        "new 2",
        "new 1",
        "new 0",
        "29 days ago",  # the oldest it shows
    ]

    html = (await owner_client.get("/notifications/panel")).text
    assert html.startswith('<div id="notifications-panel" class="menu ')
    assert re.search(r"<h2[^>]*>Notifications</h2>", html)
    assert "31 days ago" not in html
    assert "Notifications are kept for 30 days." in html
    assert "No notifications." not in html
    entries = re.findall(r'<li id="notification-(\d+)"([^>]*)>(.*?)</li>', html, re.DOTALL)
    # Newest first; the unread ones are marked, the read ones (seen, 29 days ago) are not.
    assert [(" data-unread" in attrs) for _, attrs, _ in entries] == [
        False,
        True,
        True,
        True,
        False,
    ]
    seen, newest = entries[0][2], entries[1][2]
    assert "new 2" in newest
    assert re.search(r'<time datetime="[^"]+"[^>]*>\d{4}-\d\d-\d\d \d\d:\d\d UTC</time>', newest)
    assert f'<a href="/boards/{board.id}"' in newest and "Open board" in newest
    assert "seen" in seen and "Open board" not in seen  # it names no board

    eve_html = (
        await (await signed_in(client_factory, "eve@example.com")).get("/notifications/panel")
    ).text
    assert len(re.findall(r'<li id="notification-', eve_html)) == 20

    frank_html = (
        await (await signed_in(client_factory, "frank@example.com")).get("/notifications/panel")
    ).text
    assert "No notifications." in frank_html
    assert "Notifications are kept for 30 days." in frank_html
    assert "<li" not in frank_html
    frank_client = await signed_in(client_factory, "frank@example.com")
    assert (await frank_client.get("/notifications/badge")).text == NO_BADGE


async def test_reading_clears_only_own_badge_and_tells_other_devices(
    client_factory, db, team, published
):
    _, owner, eve, _ = team
    await create_notification(db, owner)
    await create_notification(db, owner)
    await create_notification(db, eve)
    owner_client = await signed_in(client_factory, "owner@example.com")
    eve_client = await signed_in(client_factory, "eve@example.com")

    r = await owner_client.post("/notifications/read", headers=HX | {"X-Client-Id": "tab-1"})

    assert r.status_code == 200
    assert r.text == NO_BADGE
    # Their other devices are told (the tab that read is skipped: the origin), nobody else.
    assert published == [
        ((BoardEvent(EventType.NOTIFICATIONS_CHANGED, user_id=owner.id),), "tab-1")
    ]
    assert await unread_count(db, owner) == 0
    assert (await eve_client.get("/notifications/badge")).text.count(">1<span") == 1
    assert await unread_count(db, eve) == 1

    r = await owner_client.post("/notifications/read", headers=HX | {"X-Client-Id": "tab-1"})

    assert (r.status_code, r.text) == (200, NO_BADGE)
    assert len(published) == 1  # nothing was unread: nothing changed, nobody is told
    # Read, not gone: the panel still lists them.
    assert (
        len(
            re.findall(
                r'<li id="notification-', (await owner_client.get("/notifications/panel")).text
            )
        )
        == 2
    )


async def test_notification_text_is_escaped(client_factory, db):  # Review Focus 3
    owner = await create_user(db, "owner@example.com")
    eve = await create_user(db, "eve@example.com", display_name=NASTY)
    board = await create_board(db, owner, NASTY, {eve: BoardRole.EDITOR})
    await (await signed_in(client_factory, "eve@example.com")).post(f"/boards/{board.id}/leave")
    owner_client = await signed_in(client_factory, "owner@example.com")

    html = (await owner_client.get("/notifications/panel")).text

    assert html.count(NASTY_ESCAPED) == 2  # the actor and the board
    assert NASTY not in html and "<script>" not in html


async def test_routes_need_sign_in_csrf_and_a_new_password(client, client_factory, db, team):
    # Signed out: the login page (a page), 401 with a redirect for htmx.
    for path in ("/notifications/badge", "/notifications/panel"):
        r = await client.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
        assert (await client.get(path, headers=HX)).status_code == 401
    await client.get("/login")  # issues the CSRF cookie
    token = client.cookies[CSRF_COOKIE]
    r = await client.post("/notifications/read", headers={CSRF_HEADER: token})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = await client.post("/notifications/read", headers={CSRF_HEADER: token} | HX)
    assert r.status_code == 401

    # Signed in: a POST needs the token and the app's own Origin.
    owner_client = await signed_in(client_factory, "owner@example.com")
    csrf = owner_client.headers.pop(CSRF_HEADER)
    r = await owner_client.post("/notifications/read")
    assert r.status_code == 403
    r = await owner_client.post(
        "/notifications/read", headers={CSRF_HEADER: csrf, "Origin": "http://evil.example"}
    )
    assert r.status_code == 403
    r = await owner_client.post("/notifications/read", headers={CSRF_HEADER: csrf})
    assert r.status_code == 200

    # Someone who must pick a new password has no bell, and its routes are closed to them.
    await create_user(db, "newbie@example.com", must_change_password=True)
    newbie = await signed_in(client_factory, "newbie@example.com")
    for path in ("/notifications/badge", "/notifications/panel"):
        r = await newbie.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/account/password", path
    assert (await newbie.post("/notifications/read")).status_code == 403
    page = (await newbie.get("/account/password")).text
    assert 'id="notifications"' not in page and "notifications.js" not in page


async def test_signed_in_pages_load_the_bell_after_htmx(client_factory, db, team):
    board, *_ = team
    await create_user(db, "admin@example.com", is_admin=True)
    for email in ("owner@example.com", "admin@example.com"):
        client = await signed_in(client_factory, email)
        for path in ("/boards", "/account"):
            html = (await client.get(path)).text
            scripts = re.findall(r'<script src="([^"]+)"', html)
            assert scripts[:2] == [
                "/static/vendor/htmx-2.0.4.min.js",
                static_url("js/notifications.js"),
            ]
            assert (
                '<summary class="btn-icon relative cursor-pointer list-none text-slate-600 dark:text-slate-400 '
                '[&::-webkit-details-marker]:hidden" aria-label="Notifications" '
                'aria-describedby="notifications-badge">'
            ) in html
            assert re.search(r'<details id="notifications" class="md:relative">\s*<summary', html)
            # The badge loads itself: rendering the page asks the database nothing for the bell.
            assert (
                '<span id="notifications-badge" hx-get="/notifications/badge" hx-trigger="load" '
                'hx-swap="outerHTML"></span>'
            ) in html
            panel = re.search(
                r'<div id="notifications-panel" class="menu[^"]*">(.*?)</div>', html, re.DOTALL
            )
            assert panel and re.fullmatch(r"\s*<h2[^>]*>Notifications</h2>\s*", panel[1])
            assert html.index('id="notifications"') < html.index("Account</a>")
            if email.startswith("admin"):
                assert html.index('id="notifications"') < html.index('href="/admin"')
    owner_client = await signed_in(client_factory, "owner@example.com")
    board_page = (await owner_client.get(f"/boards/{board.id}")).text
    assert re.findall(r'<script src="([^"]+)"', board_page)[:3] == [
        "/static/vendor/htmx-2.0.4.min.js",
        static_url("js/notifications.js"),
        static_url("js/board.js"),
    ]


async def test_signed_out_pages_have_no_bell(client):
    for path in ("/login", "/register"):
        html = (await client.get(path)).text
        assert 'id="notifications' not in html and "notifications.js" not in html, path
