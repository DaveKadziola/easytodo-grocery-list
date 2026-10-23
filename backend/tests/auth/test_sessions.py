from datetime import UTC, datetime, timedelta

from factories import create_user
from helpers import count_queries
from sqlalchemy import select, update

from easytodo.auth.deps import active_session
from easytodo.auth.sessions import (
    create_session,
    load_session,
    load_session_and_user,
    revoke_all_sessions,
    revoke_session,
)
from easytodo.models import User, UserSession, UserStatus
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


async def test_load_session_and_user_reads_both_in_one_statement(db, settings):
    user = await create_user(db)
    raw_token, session = await new_session(db, user, settings)
    other_token, _ = await new_session(db, await create_user(db, "other@example.com"), settings)
    db.expunge_all()

    with count_queries(db) as statements:
        found = await load_session_and_user(db, raw_token)

    assert len(statements) == 1
    assert found is not None
    loaded_session, loaded_user = found
    assert (loaded_session.id, loaded_session.user_id) == (session.id, user.id)
    assert loaded_user.id == user.id  # not the other token's user
    assert (await load_session_and_user(db, raw_token + "x")) is None
    assert (await load_session_and_user(db, "")) is None
    assert (await load_session_and_user(db, other_token))[1].email == "other@example.com"


async def test_load_session_and_user_skips_expired_sessions(db, settings):
    user = await create_user(db)
    raw_token, session = await new_session(db, user, settings)
    session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    assert (await load_session_and_user(db, raw_token)) is None


async def test_load_session_and_user_updates_last_seen_at_like_load_session(db, settings):
    user = await create_user(db)
    raw_token, session = await new_session(db, user, settings)

    recent = datetime.now(UTC) - timedelta(minutes=4)
    session.last_seen_at = recent
    await db.commit()
    await load_session_and_user(db, raw_token)
    await db.refresh(session)
    assert session.last_seen_at == recent

    session.last_seen_at = datetime.now(UTC) - timedelta(minutes=6)
    await db.commit()
    found = await load_session_and_user(db, raw_token)
    assert found is not None
    assert datetime.now(UTC) - found[0].last_seen_at < timedelta(minutes=1)
    await db.refresh(session)  # re-read from the database: the update was committed
    assert datetime.now(UTC) - session.last_seen_at < timedelta(minutes=1)
    assert found[1].email == user.email  # the user is still usable after that commit


async def test_active_session_leaves_out_a_disabled_user_but_not_a_pending_session(db, settings):
    user = await create_user(db)
    raw_token, _ = await create_session(
        db, user, mfa_pending=True, user_agent=None, ip=None, settings=settings
    )
    signed_in = await active_session(db, raw_token)
    assert signed_in is not None  # what a pending session may do is for require_user to say
    assert (signed_in[0].id, signed_in[1].mfa_pending) == (user.id, True)
    assert await active_session(db, None) is None
    assert await active_session(db, raw_token + "x") is None

    await db.execute(update(User).where(User.id == user.id).values(status=UserStatus.DISABLED))
    await db.commit()
    db.expunge_all()
    assert await active_session(db, raw_token) is None


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
