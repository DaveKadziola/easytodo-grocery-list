"""The files of attachments on disk, outside the database.

A file lives at ``<root>/<key[:2]>/<key>`` under a random key the service makes (32 hex digits):
never the user's file name or a board's id, so nothing in a path can be chosen from outside. An
image has two more files beside it, made when it is attached: ``<key>.thumb.webp`` (the tile in
the item's details) and, for a HEIC photo, ``<key>.preview.jpg`` (which browsers can show).
Everything here is plain blocking file work: ``save`` runs it in a thread, the rest is meant for a
thread or for a few unlinks.
"""

import asyncio
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

THUMB_SUFFIX = ".thumb.webp"
PREVIEW_SUFFIX = ".preview.jpg"
_KEY = re.compile(r"[0-9a-f]{32}")
_DERIVED = re.compile(
    rf"(?P<key>[0-9a-f]{{32}})(?:{re.escape(THUMB_SUFFIX)}|{re.escape(PREVIEW_SUFFIX)})"
)
_CHUNK = 1024 * 1024


class AttachmentStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def writable(self) -> bool:
        """Whether this process can write in the directory: the files are written below it, and
        the directory is made on the first upload if it is not there, so then it is the nearest
        directory that is there that has to be writable."""
        nearest = self.root.absolute()
        while not nearest.exists() and nearest != nearest.parent:
            nearest = nearest.parent
        return os.access(nearest, os.W_OK)

    def path(self, key: str) -> Path:
        return self.root / key[:2] / key

    def thumb_path(self, key: str) -> Path:
        return self.root / key[:2] / (key + THUMB_SUFFIX)

    def preview_path(self, key: str) -> Path:
        return self.root / key[:2] / (key + PREVIEW_SUFFIX)

    def _save(self, key: str, source: BinaryIO) -> None:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as target:  # "x": a key is never used twice
            shutil.copyfileobj(source, target, _CHUNK)

    async def save(self, key: str, source: BinaryIO) -> None:
        """Write ``source``, from its current position to its end, as the file ``key``."""
        await asyncio.to_thread(self._save, key, source)

    def delete(self, key: str) -> bool:
        """Remove the file ``key`` and what was made from it; a file that is not there is no
        error. True if the file itself was there and this call removed it (a second call, or
        another process that was quicker, gets False)."""
        removed = True
        try:
            self.path(key).unlink()
        except FileNotFoundError:
            removed = False
        self.thumb_path(key).unlink(missing_ok=True)
        self.preview_path(key).unlink(missing_ok=True)
        return removed

    def _listing(self) -> Iterator[tuple[Path, list[str]]]:
        """Each directory of the store with the names of the files in it; none while the
        directory does not exist yet."""
        try:
            directories = [entry.path for entry in os.scandir(self.root) if entry.is_dir()]
        except FileNotFoundError:
            return
        for directory in directories:
            try:
                names = [entry.name for entry in os.scandir(directory) if entry.is_file()]
            except FileNotFoundError:  # taken away since the listing above
                continue
            yield Path(directory), names

    def keys(self) -> Iterator[str]:
        """The key of every file (not of the thumbnails and previews beside them), in no
        particular order; none while the directory does not exist yet."""
        for _, names in self._listing():
            yield from (name for name in names if _KEY.fullmatch(name))

    def derived_orphans(self) -> Iterator[tuple[str, Path]]:
        """The thumbnails and previews whose file is not there, as ``(key, path)`` (an upload that
        was cancelled after they were written leaves them; ``keys`` never names them), in no
        particular order."""
        for directory, names in self._listing():
            present = set(names)
            for name in names:
                derived = _DERIVED.fullmatch(name)
                if derived is not None and derived["key"] not in present:
                    yield derived["key"], directory / name

    def delete_derived(self, path: Path) -> bool:
        """Remove a thumbnail or preview that ``derived_orphans`` named. True if it was there and
        this call removed it."""
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True
