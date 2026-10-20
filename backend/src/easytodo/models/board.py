from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, Enum, ForeignKey, Identity, String, func
from sqlalchemy.orm import Mapped, mapped_column

from easytodo.db import Base


class BoardRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


_board_role = Enum(BoardRole, name="board_role", values_callable=lambda e: [m.value for m in e])


class Board(Base):
    __tablename__ = "boards"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    owner_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class BoardMember(Base):
    __tablename__ = "board_members"

    board_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("boards.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[BoardRole] = mapped_column(_board_role)
    joined_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BoardInvite(Base):
    __tablename__ = "board_invites"
    __table_args__ = (CheckConstraint("role <> 'owner'", name="role_not_owner"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    board_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("boards.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str | None] = mapped_column(String(320))  # None: a link invite
    role: Mapped[BoardRole] = mapped_column(_board_role)
    created_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
