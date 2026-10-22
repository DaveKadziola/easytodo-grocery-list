"""Test data factories. Each one commits, so the app under test (own connection) sees the rows."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.models import (
    Board,
    BoardMember,
    BoardRole,
    Category,
    ChecklistEntry,
    Item,
    Notification,
    NotificationKind,
    User,
    UserStatus,
)
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
    display_name: str | None = None,
) -> User:
    user = User(
        email=email,
        password_hash=hash_password(password),
        is_admin=is_admin,
        must_change_password=must_change_password,
        status=status,
        display_name=display_name,
    )
    db.add(user)
    await db.commit()
    return user


async def create_board(
    db: AsyncSession,
    owner: User,
    name: str = "Groceries",
    members: dict[User, BoardRole] = {},  # noqa: B006 (read only)
    *,
    archived_at: datetime | None = None,
) -> Board:
    """A board with ``owner`` as its owner member, plus ``members`` with the given roles;
    archived at ``archived_at`` when that is given."""
    board = Board(name=name, owner_id=owner.id, archived_at=archived_at)
    db.add(board)
    await db.flush()
    db.add(BoardMember(board_id=board.id, user_id=owner.id, role=BoardRole.OWNER))
    db.add_all(
        BoardMember(board_id=board.id, user_id=user.id, role=role) for user, role in members.items()
    )
    await db.commit()
    return board


async def create_category(
    db: AsyncSession,
    board: Board,
    name: str = "Dairy",
    position: int = 0,
    *,
    archived_at: datetime | None = None,
) -> Category:
    """A category of ``board``; archived at ``archived_at`` when that is given."""
    category = Category(board_id=board.id, name=name, position=position, archived_at=archived_at)
    db.add(category)
    await db.commit()
    return category


async def create_item(
    db: AsyncSession,
    category: Category,
    name: str = "Milk",
    is_done: bool = False,
    *,
    created_by: User | None = None,
    description: str | None = None,
) -> Item:
    item = Item(
        board_id=category.board_id,
        category_id=category.id,
        name=name,
        is_done=is_done,
        done_at=datetime.now(UTC) if is_done else None,
        created_by=created_by.id if created_by else None,
        description=description,
    )
    db.add(item)
    await db.commit()
    return item


async def create_checklist_entry(
    db: AsyncSession, item: Item, text: str = "Eggs", is_done: bool = False, position: int = 0
) -> ChecklistEntry:
    entry = ChecklistEntry(item_id=item.id, text=text, is_done=is_done, position=position)
    db.add(entry)
    await db.commit()
    return entry


async def create_notification(
    db: AsyncSession,
    user: User,
    kind: NotificationKind = NotificationKind.MEMBER_JOINED,
    message: str = "x joined “Groceries”.",
    board: Board | None = None,
    read: bool = False,
) -> Notification:
    notification = Notification(
        user_id=user.id,
        kind=kind,
        message=message,
        board_id=board.id if board else None,
        read_at=datetime.now(UTC) if read else None,
    )
    db.add(notification)
    await db.commit()
    return notification
