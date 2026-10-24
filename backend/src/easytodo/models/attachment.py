from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Identity, String, func
from sqlalchemy.orm import Mapped, mapped_column

from easytodo.db import Base

# What the CHECK constraint below allows (the literal there must list the same values).
ATTACHMENT_KINDS = ("image", "pdf", "text", "markdown")


class Attachment(Base):
    """A file attached to a task. The bytes live on disk under ``storage_key``."""

    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint("kind IN ('image', 'pdf', 'text', 'markdown')", name="kind_allowed"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(10))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(32), unique=True)
    # NULL once the uploader's account is deleted (the file stays with the task).
    uploaded_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
