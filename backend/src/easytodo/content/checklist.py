"""The checklist inside an item: entries a member adds, renames, ticks, deletes and puts in order.

Like the other content services, every function finds what it changes through the user's own board
memberships (a board they are not on, an item that is gone, deleted or in an archived category or
board answer ``NotFound``; a role that may not edit gets ``Forbidden``) and commits the change
together with its board event. A checklist is the item's details, not the item: no write here
touches the item's row, so a tick does not tick the item and does not set its "Modified". Each
write names the item in its event (``item_ids``), so a page with it open fetches its details again,
and the category (its tile shows how many entries are done).
"""

from functools import cache

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from easytodo.boards.permissions import (
    Action,
    MemberLookup,
    RowLock,
    is_possible_id,
    load_for_member,
    member_lookup,
)
from easytodo.content.service import _category_changed, item_child_query, load_item
from easytodo.content.validation import clean_name
from easytodo.errors import Conflict, NotFound, ValidationFailed
from easytodo.models import BoardRole, ChecklistEntry, Item, User
from easytodo.realtime.events import BoardEvent, commit_and_publish

ENTRY_MAX_LENGTH = 200  # checklist_entries.text is String(200)
MAX_ENTRIES = 100
ENTRY_GONE = "This checklist entry no longer exists."
TOO_MANY_ENTRIES = "A checklist can have at most 100 entries."
CHECKLIST_OUT_OF_DATE = "Checklist order is out of date. Refresh the task."


@cache
def _entry_lookup(action: Action, lock: RowLock | None) -> MemberLookup:
    """The statements of ``load_entry`` for one action and lock, built once."""
    query = item_child_query(ChecklistEntry, "entry_id")
    return member_lookup(query, action, lock=lock, of=ChecklistEntry)


async def load_entry(
    db: AsyncSession,
    entry_id: int,
    user: User,
    action: Action,
    *,
    lock: RowLock = RowLock.NO_KEY_UPDATE,
) -> tuple[ChecklistEntry, Item, BoardRole]:
    """The entry, its item and ``user``'s role, in one query joined with the item, its active
    category, the active board and ``board_members``. An entry of a deleted item or one in an
    archived category or board is not found.

    For a change (any action but VIEW) the entry's row stays locked in mode ``lock`` until the
    commit, so a delete from another device that got there first makes this ``NotFound``, and a
    later one waits for it. A member whose role does not allow the change takes no lock (see
    ``load_for_member``). The item is read, not locked: a change to it (a delete) does not wait
    for a tick, and a tick that loses that race ends on an entry that is gone with it.
    """
    if not is_possible_id(entry_id):
        raise NotFound(ENTRY_GONE)
    row_lock = None if action is Action.VIEW else lock
    entry, item, role = await load_for_member(
        db,
        _entry_lookup(action, row_lock),
        ENTRY_GONE,
        {"entry_id": entry_id, "user_id": user.id},
    )
    return entry, item, role


def _checklist_changed(item: Item) -> BoardEvent:
    return _category_changed(item.board_id, item.category_id, item_ids=(item.id,))


def _clean_entry_text(text: str) -> str:
    return clean_name(text, max_len=ENTRY_MAX_LENGTH, label="Checklist entry")


async def add_entry(db: AsyncSession, item_id: int, user: User, text: str) -> ChecklistEntry:
    """A new entry at the end of the item's checklist; a ``Conflict`` once it has ``MAX_ENTRIES``."""
    # The default lock on the item serializes the entries added at the same time, so that each
    # counts the others and takes the next position; it does not hold up a tick or a rename.
    item, _ = await load_item(db, item_id, user, Action.EDIT_CONTENT)
    text = _clean_entry_text(text)
    count, last = (
        await db.execute(
            select(func.count(), func.max(ChecklistEntry.position)).where(
                ChecklistEntry.item_id == item.id
            )
        )
    ).one()
    if count >= MAX_ENTRIES:
        raise Conflict(TOO_MANY_ENTRIES)
    entry = ChecklistEntry(
        item_id=item.id, text=text, is_done=False, position=0 if last is None else last + 1
    )
    db.add(entry)
    await commit_and_publish(db, _checklist_changed(item))
    return entry


async def rename_entry(db: AsyncSession, entry_id: int, user: User, text: str) -> ChecklistEntry:
    entry, item, _ = await load_entry(db, entry_id, user, Action.EDIT_CONTENT)
    entry.text = _clean_entry_text(text)
    await commit_and_publish(db, _checklist_changed(item))
    return entry


async def check_entry(
    db: AsyncSession, entry_id: int, user: User, done: bool | None = None
) -> ChecklistEntry:
    """Tick the entry or untick it: as ``done`` says, or the other way round from what it is when
    ``done`` is None. Clients send the state their user wants (see ``content.service.toggle_item``);
    an entry that is in that state already stays as it is, with no event."""
    entry, item, _ = await load_entry(db, entry_id, user, Action.EDIT_CONTENT)
    wanted = not entry.is_done if done is None else done
    if wanted == entry.is_done:
        await commit_and_publish(db)  # nothing to change: just ends the transaction (row lock)
        return entry
    entry.is_done = wanted
    await commit_and_publish(db, _checklist_changed(item))
    return entry


async def delete_entry(db: AsyncSession, entry_id: int, user: User) -> int:
    """Delete the entry; returns the id of its item. The other entries keep their positions."""
    entry, item, _ = await load_entry(db, entry_id, user, Action.EDIT_CONTENT, lock=RowLock.UPDATE)
    await db.delete(entry)
    await commit_and_publish(db, _checklist_changed(item))
    return item.id


async def reorder_entries(
    db: AsyncSession, item_id: int, user: User, ordered_ids: list[int]
) -> None:
    """Put the item's entries in the order of ``ordered_ids``, which must name each of them
    exactly once (a client that missed a change gets ``ValidationFailed``)."""
    item, _ = await load_item(db, item_id, user, Action.EDIT_CONTENT)
    rows = await db.scalars(  # locked in id order: two reorders at once cannot deadlock
        select(ChecklistEntry)
        .where(ChecklistEntry.item_id == item.id)
        .order_by(ChecklistEntry.id)
        .with_for_update(key_share=True)
    )
    by_id = {entry.id: entry for entry in rows}
    if sorted(ordered_ids) != sorted(by_id):
        raise ValidationFailed(CHECKLIST_OUT_OF_DATE)
    for position, entry_id in enumerate(ordered_ids):
        by_id[entry_id].position = position
    await commit_and_publish(db, _checklist_changed(item))
