from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Identity, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from easytodo.db import Base


class NotificationKind(StrEnum):
    MEMBER_JOINED = "member_joined"
    MEMBER_LEFT = "member_left"
    MEMBER_REMOVED = "member_removed"
    ROLE_CHANGED = "role_changed"
    BOARD_ARCHIVED = "board_archived"
    BOARD_RESTORED = "board_restored"
    CATEGORY_MOVED = "category_moved"


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('member_joined', 'member_left', 'member_removed', 'role_changed', "
            "'board_archived', 'board_restored', 'category_moved')",
            name="kind_allowed",
        ),
        Index("ix_notifications_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(String(600))
    # NULL once the board is deleted (the notification stays).
    board_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("boards.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    read_at: Mapped[datetime | None]
