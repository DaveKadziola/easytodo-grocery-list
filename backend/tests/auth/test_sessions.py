from datetime import UTC, datetime, timedelta

from factories import create_user
from sqlalchemy import select

from easytodo.auth.sessions import (
    create_session,
    load_session,
    revoke_all_sessions,
    revoke_session,
)
from easytodo.models import UserSession
from easytodo.security.tokens import hash_token


async def new_session(db, user, settings) -> tuple[str, UserSession]:
    return await create_session(
        db, user, mfa_pending=False, user_agent=None, ip=None, settings=settings
    )


async def test_create_and_load_session(db, settings):
    user = await create_user(db)
    raw_token, session = await create_session(
        db, user, mfa_pending=True, user_agent="UA", ip="10.0.0.1", settings=settings
    )
    assert session.token_hash == hash_token(raw_token) != raw_token
    assert (session.user_id, session.mfa_pending) == (user.id, True)
    assert (session.user_agent, session.ip) == ("UA", "10.0.0.1")
    assert (await load_session(db, raw_token)) is session
    assert (await load_session(db, raw_token + "x")) is None


async def test_load_session_skips_expired_sessions(db, settings):
    user = await create_user(db)
    raw_token, session = await new_session(db, user, settings)
    session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    assert (await load_session(db, raw_token)) is None


async def test_last_seen_at_is_updated_at_most_every_5_minutes(db, settings):
    user = await create_user(db)
    raw_token, session = await new_session(db, user, settings)

    recent = datetime.now(UTC) - timedelta(minutes=4)
    session.last_seen_at = recent
    await db.commit()
    await load_session(db, raw_token)
    await db.refresh(session)
    assert session.last_seen_at == recent

    stale = datetime.now(UTC) - timedelta(minutes=6)
    session.last_seen_at = stale
    await db.commit()
    await load_session(db, raw_token)
    await db.refresh(session)  # re-read from the database: the update was committed
    assert datetime.now(UTC) - session.last_seen_at < timedelta(minutes=1)


async def test_revoke_all_sessions_can_keep_the_current_one(db, settings):
    dave = await create_user(db, "dave@example.com")
    eve = await create_user(db, "eve@example.com")
    current = (await new_session(db, dave, settings))[1].id
    others = {(await new_session(db, dave, settings))[1].id for _ in range(2)}
    eves = (await new_session(db, eve, settings))[1].id

    revoked = await revoke_all_sessions(db, dave.id, except_session_id=current)
    assert sorted(revoked) == sorted(others)
    assert set(await db.scalars(select(UserSession.id))) == {current, eves}

    assert await revoke_all_sessions(db, dave.id) == [current]
    assert set(await db.scalars(select(UserSession.id))) == {eves}


async def test_revoke_session_deletes_only_that_session(db, settings):
    user = await create_user(db)
    first = (await new_session(db, user, settings))[1].id
    second = (await new_session(db, user, settings))[1].id
    await revoke_session(db, first)
    assert set(await db.scalars(select(UserSession.id))) == {second}
