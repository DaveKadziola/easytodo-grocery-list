"""What each board role may do. Apart from ``permissions`` so that ``web`` (the template
globals) can use it: ``permissions`` imports ``errors``, which imports ``web``."""

from enum import StrEnum, auto

from easytodo.models import BoardRole


class Action(StrEnum):
    VIEW = auto()
    EDIT_CONTENT = auto()
    MANAGE_MEMBERS = auto()
    MANAGE_BOARD = auto()


_ALLOWED: dict[BoardRole, frozenset[Action]] = {
    BoardRole.VIEWER: frozenset({Action.VIEW}),
    BoardRole.EDITOR: frozenset({Action.VIEW, Action.EDIT_CONTENT}),
    BoardRole.OWNER: frozenset(Action),
}


def can(role: BoardRole, action: Action) -> bool:
    return action in _ALLOWED[role]


def roles_for(action: Action) -> tuple[BoardRole, ...]:
    """The roles that may do ``action`` (for a query that should find only their members)."""
    return tuple(role for role, actions in _ALLOWED.items() if action in actions)
