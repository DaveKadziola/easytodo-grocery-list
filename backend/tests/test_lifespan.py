import gc

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
