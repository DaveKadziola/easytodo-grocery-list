"""How a person is shown to the others: a name (their own, or the email's local part), the
initials in their avatar, and the colour of that avatar. Also template globals (see web.py)."""

from easytodo.models import User

AVATAR_COLOR_COUNT = 8  # the colours of the avatar macro's palette (partials/_avatar.html)


def display_name(email: str, name: str | None) -> str:
    """``name`` as the person set it, or the part of ``email`` before the @ when there is none."""
    return name or email.partition("@")[0]


def user_display_name(user: User) -> str:
    return display_name(user.email, user.display_name)


def initials(name: str) -> str:
    """The first letter of the first and of the last word of ``name``, upper case ("AK" for
    "Anna Maria Kowalska"); one letter for one word; "?" when there is no word."""
    words = name.split()
    if not words:
        return "?"
    ends = words[:1] if len(words) == 1 else [words[0], words[-1]]
    # A letter's upper case can be longer than the letter ("ß" -> "SS"): keep its first character.
    return "".join(word[0].upper()[0] for word in ends)


def avatar_color(user_id: int) -> int:
    """The palette index of ``user_id``: always the same one for a person."""
    return user_id % AVATAR_COLOR_COUNT
