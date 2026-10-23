"""Cleaning the category and item names and the item descriptions that users type in (and the
text imported from v1, which follows the same rules)."""

from easytodo.errors import ValidationFailed

CATEGORY_NAME_MAX_LENGTH = 120  # categories.name is String(120)
ITEM_NAME_MAX_LENGTH = 200  # items.name is String(200)
DESCRIPTION_MAX_LENGTH = 2000  # items.description is String(2000)


def _without_nul(value: str) -> str:
    return value.replace("\x00", "")  # PostgreSQL text cannot hold NUL


def normalize_name(value: str) -> str:
    """``value`` without NUL, stripped, with every run of whitespace inside turned into one space.
    No limit."""
    return " ".join(_without_nul(value).split())


def normalize_description(value: str | None) -> str:
    """``value`` without NUL, stripped, keeping its line breaks (as ``\\n``); "" when nothing is
    left. No limit."""
    return _without_nul(value or "").replace("\r\n", "\n").strip()


def clean_name(value: str, *, max_len: int, label: str) -> str:
    """``value`` as ``normalize_name`` has it, which must be 1 to ``max_len`` characters."""
    name = normalize_name(value)
    if not 1 <= len(name) <= max_len:
        raise ValidationFailed(f"{label} must be 1–{max_len} characters.")
    return name


def clean_description(value: str | None) -> str | None:
    """``value`` as ``normalize_description`` has it, at most 2000 characters; ``None`` when
    nothing is left."""
    description = normalize_description(value)
    if len(description) > DESCRIPTION_MAX_LENGTH:
        raise ValidationFailed("Description must be at most 2000 characters.")
    return description or None
