from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, String, false, func
from sqlalchemy.orm import Mapped, mapped_column

from easytodo.db import Base


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (Index("ix_categories_board_id_position", "board_id", "position"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    board_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("boards.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    position: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


# Category names are unique per board, ignoring case.
Index(
    "uq_categories_board_id_lower_name",
    Category.board_id,
    func.lower(Category.name),
    unique=True,
)


class Item(Base):
    """A to-do or a shopping item (one model for both, with the same fields as in v1)."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Denormalised from the category: authorisation and the whole-board query need no join.
    board_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("boards.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("categories.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(2000))
    is_done: Mapped[bool] = mapped_column(server_default=false())
    done_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
