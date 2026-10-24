"""Attachment fixtures: the content fixtures (a board, an item, an editor and a viewer signed in
to it), an app whose files go to a directory of the test's own, and its store."""

import pytest
from conftest import _test_settings
from content.conftest import (  # noqa: F401 (fixtures)
    board,
    category,
    content_snapshot,
    editor_client,
    item,
    published,
    signed_in_client,
    viewer_client,
)

from easytodo.attachments.storage import AttachmentStore
from easytodo.config import Settings


@pytest.fixture
def settings(tmp_path, request) -> Settings:
    """The settings, with the files in the test's own directory; a test changes some with
    ``@pytest.mark.parametrize("settings", [{"attachments_per_item": 2}], indirect=True)``."""
    overrides = getattr(request, "param", {})
    return _test_settings(attachments_dir=tmp_path / "attachments", **overrides)


@pytest.fixture
def store(app, _running_app) -> AttachmentStore:
    """The running app's store of files."""
    return app.state.attachment_store
