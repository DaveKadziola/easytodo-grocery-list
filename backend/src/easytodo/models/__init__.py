"""ORM models. Every model module is imported here so that ``Base.metadata`` knows all tables
(Alembic's env.py and the test fixtures import this package)."""

from easytodo.models.attachment import ATTACHMENT_KINDS, Attachment
from easytodo.models.auth import PasswordResetToken, RecoveryCode, UserSession
from easytodo.models.board import Board, BoardInvite, BoardMember, BoardRole
from easytodo.models.content import Category, CategorySortPref, ChecklistEntry, Item
from easytodo.models.notification import Notification, NotificationKind
from easytodo.models.user import RETENTION_CHOICES, SORT_FIELDS, User, UserStatus

__all__ = [
    "ATTACHMENT_KINDS",
    "RETENTION_CHOICES",
    "SORT_FIELDS",
    "Attachment",
    "Board",
    "BoardInvite",
    "BoardMember",
    "BoardRole",
    "Category",
    "CategorySortPref",
    "ChecklistEntry",
    "Item",
    "Notification",
    "NotificationKind",
    "PasswordResetToken",
    "RecoveryCode",
    "User",
    "UserSession",
    "UserStatus",
]
