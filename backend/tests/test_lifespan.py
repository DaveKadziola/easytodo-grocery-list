import gc
import logging
import os
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager

from easytodo import main
from easytodo.main import _freeze_startup_heap, create_app

REAL_FREEZE = _freeze_startup_heap  # before the autouse fixture of tests/conftest.py replaces it


async def test_startup_freezes_the_collector_and_shutdown_unfreezes_it(settings, monkeypatch):
    """Frozen objects are never collected, so the app lets go of them when it stops."""
    monkeypatch.setattr(main, "_freeze_startup_heap", REAL_FREEZE)

    async with LifespanManager(create_app(settings)):
        assert gc.get_freeze_count() > 0
    assert gc.get_freeze_count() == 0


async def test_the_tests_do_not_freeze_the_collector(settings):
    """What ``tests/conftest.py`` turns off, as every test starts an app."""
    async with LifespanManager(create_app(settings)):
        assert gc.get_freeze_count() == 0


def is_the_attachments_error(record: logging.LogRecord) -> bool:
    return record.levelno == logging.ERROR and "attachments directory" in record.getMessage()


def deny_writing_in(monkeypatch, directory: Path) -> None:
    """``os.access(directory, W_OK)`` says no (the tests run as root, who may write anywhere);
    any other path is asked of the system."""
    real = os.access

    def access(path, mode, **kwargs):
        if Path(path) == directory and mode == os.W_OK:
            return False
        return real(path, mode, **kwargs)

    monkeypatch.setattr(os, "access", access)


async def test_startup_logs_an_error_for_an_attachments_directory_it_cannot_write(
    settings, tmp_path, monkeypatch, caplog
):
    directory = tmp_path / "attachments"
    directory.mkdir()
    deny_writing_in(monkeypatch, directory)

    with caplog.at_level(logging.ERROR, logger="easytodo.main"):
        async with LifespanManager(
            create_app(settings.model_copy(update={"attachments_dir": directory}))
        ):
            pass

    errors = [r for r in caplog.records if is_the_attachments_error(r)]
    assert len(errors) == 1
    assert str(directory) in errors[0].getMessage()
    assert "chown -R 10001:10001" in errors[0].getMessage()


async def test_startup_checks_the_nearest_existing_directory_of_one_that_is_not_there_yet(
    settings, tmp_path, monkeypatch, caplog
):
    """The first upload makes the directory (and its parents), so what counts is the nearest
    directory that exists."""
    deny_writing_in(monkeypatch, tmp_path)
    directory = tmp_path / "data" / "attachments"

    with caplog.at_level(logging.ERROR, logger="easytodo.main"):
        async with LifespanManager(
            create_app(settings.model_copy(update={"attachments_dir": directory}))
        ):
            pass

    assert [r for r in caplog.records if is_the_attachments_error(r)]


@pytest.mark.parametrize("exists", [True, False])
async def test_startup_logs_nothing_for_a_directory_it_can_write(
    settings, tmp_path, caplog, exists
):
    directory = tmp_path / "attachments"
    if exists:
        directory.mkdir()

    with caplog.at_level(logging.ERROR, logger="easytodo.main"):
        async with LifespanManager(
            create_app(settings.model_copy(update={"attachments_dir": directory}))
        ):
            pass

    assert not [r for r in caplog.records if is_the_attachments_error(r)]
