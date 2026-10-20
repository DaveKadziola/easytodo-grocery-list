"""Test data factories. Each one commits, so the app under test (own connection) sees the rows."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.models import Board, BoardMember, BoardRole, Category, Item, User, UserStatus
from easytodo.security.passwords import hash_password

DEFAULT_PASSWORD = "correct-horse-battery-staple"


async def create_user(
    db: AsyncSession,
    email: str = "user@example.com",
    password: str = DEFAULT_PASSWORD,
    *,
    is_admin: bool = False,
    must_change_password: bool = False,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    user = User(
        email=email,
        password_hash=hash_password(password),
        is_admin=is_admin,
        must_change_password=must_change_password,
        status=status,
    )
    db.add(user)
    await db.commit()
    return user


async def create_board(
    db: AsyncSession,
    owner: User,
    name: str = "Groceries",
    members: dict[User, BoardRole] = {},  # noqa: B006 (read only)
) -> Board:
    """A board with ``owner`` as its owner member, plus ``members`` with the given roles."""
    board = Board(name=name, owner_id=owner.id)
    db.add(board)
    await db.flush()
    db.add(BoardMember(board_id=board.id, user_id=owner.id, role=BoardRole.OWNER))
    db.add_all(
        BoardMember(board_id=board.id, user_id=user.id, role=role) for user, role in members.items()
    )
    await db.commit()
    return board


async def create_category(
    db: AsyncSession, board: Board, name: str = "Dairy", position: int = 0
) -> Category:
    category = Category(board_id=board.id, name=name, position=position)
    db.add(category)
    await db.commit()
    return category


async def create_item(
    db: AsyncSession, category: Category, name: str = "Milk", is_done: bool = False
) -> Item:
    item = Item(
        board_id=category.board_id,
        category_id=category.id,
        name=name,
        is_done=is_done,
        done_at=datetime.now(UTC) if is_done else None,
    )
    db.add(item)
    await db.commit()
    return item
