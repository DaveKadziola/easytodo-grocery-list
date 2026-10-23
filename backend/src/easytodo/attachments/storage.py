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
_CHUNK = 1024 * 1024


class AttachmentStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

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

    def delete(self, key: str) -> None:
        """Remove the file ``key`` and what was made from it; a file that is not there is no
        error."""
        for path in (self.path(key), self.thumb_path(key), self.preview_path(key)):
            path.unlink(missing_ok=True)

    def keys(self) -> Iterator[str]:
        """The key of every file (not of the thumbnails and previews beside them), in no
        particular order; none while the directory does not exist yet."""
        try:
            directories = [entry.path for entry in os.scandir(self.root) if entry.is_dir()]
        except FileNotFoundError:
            return
        for directory in directories:
            try:
                names = [entry.name for entry in os.scandir(directory) if entry.is_file()]
            except FileNotFoundError:  # taken away since the listing above
                continue
            yield from (name for name in names if _KEY.fullmatch(name))
