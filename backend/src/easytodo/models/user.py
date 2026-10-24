from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, Enum, Identity, String, false, func, true
from sqlalchemy.orm import Mapped, mapped_column

from easytodo.db import Base

# What the CHECK constraints below allow (the literals there must list the same values).
RETENTION_CHOICES = (7, 30, 90, 365)  # days an archived board is kept; None: never deleted
SORT_FIELDS = ("name", "created", "updated")


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint(
            "archive_retention_days IN (7, 30, 90, 365)", name="archive_retention_days_allowed"
        ),
        CheckConstraint("sort_field IN ('name', 'created', 'updated')", name="sort_field_allowed"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str]
    is_admin: Mapped[bool] = mapped_column(server_default=false())
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status", values_callable=lambda e: [m.value for m in e]),
        server_default=UserStatus.ACTIVE.value,
    )
    must_change_password: Mapped[bool] = mapped_column(server_default=false())
    totp_secret_enc: Mapped[str | None]
    totp_enabled: Mapped[bool] = mapped_column(server_default=false())
    totp_last_step: Mapped[int | None]
    failed_login_count: Mapped[int] = mapped_column(server_default="0")
    locked_until: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_login_at: Mapped[datetime | None]
    display_name: Mapped[str | None] = mapped_column(String(60))
    # NULL means "never delete". The column default is 30, so a new User(...=None) is stored as
    # 30: set NULL with an UPDATE.
    archive_retention_days: Mapped[int | None] = mapped_column(server_default="30")
    sort_open_first: Mapped[bool] = mapped_column(server_default=true())
    sort_field: Mapped[str] = mapped_column(String(10), server_default="name")
    sort_descending: Mapped[bool] = mapped_column(server_default=false())
