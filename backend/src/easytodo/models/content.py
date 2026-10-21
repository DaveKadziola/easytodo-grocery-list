from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from easytodo.db import Base


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        Index("ix_categories_board_id_position", "board_id", "position"),
        # The target of the composite foreign key of Item: a task cannot leave its category's board.
        UniqueConstraint("id", "board_id", name="uq_categories_id_board_id"),
    )
    # Fetch the new updated_at with the UPDATE (RETURNING): an expired attribute cannot be loaded
    # lazily in async code.
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    board_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("boards.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    position: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    archived_at: Mapped[datetime | None]


# Category names are unique per board, ignoring case; an archived category frees its name.
Index(
    "uq_categories_board_id_lower_name",
    Category.board_id,
    func.lower(Category.name),
    unique=True,
    postgresql_where=text("archived_at IS NULL"),
)


class Item(Base):
    """A to-do or a shopping item (one model for both, with the same fields as in v1)."""

    __tablename__ = "items"
    __table_args__ = (
        # (category_id, board_id) must be a category of that board: moving a category to another
        # board (ON UPDATE CASCADE) takes its items along, and no statement can split them.
        ForeignKeyConstraint(
            ["category_id", "board_id"],
            ["categories.id", "categories.board_id"],
            name="fk_items_category_id_board_id_categories",
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
    )
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Denormalised from the category: authorisation and the whole-board query need no join.
    board_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("boards.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(2000))
    is_done: Mapped[bool] = mapped_column(server_default=false())
    done_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    # The author; NULL once their account is deleted (the task stays on the shared board).
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    deleted_at: Mapped[datetime | None]


class CategorySortPref(Base):
    """How one user sorts one category (the user's own choice, not shared with the board)."""

    __tablename__ = "category_sort_prefs"
    __table_args__ = (
        CheckConstraint("field IN ('name', 'created', 'updated')", name="field_allowed"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    open_first: Mapped[bool]
    field: Mapped[str] = mapped_column(String(10))
    descending: Mapped[bool]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ChecklistEntry(Base):
    __tablename__ = "checklist_entries"
    __table_args__ = (Index("ix_checklist_entries_item_id_position", "item_id", "position"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(String(200))
    is_done: Mapped[bool] = mapped_column(server_default=false())
    position: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
