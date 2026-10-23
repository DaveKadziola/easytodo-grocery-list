import gc

from asgi_lifespan import LifespanManager

from easytodo.main import create_app


async def test_startup_freezes_the_collector_and_shutdown_unfreezes_it(settings):
    """Frozen objects are never collected, so the app lets go of them when it stops."""
    async with LifespanManager(create_app(settings)):
        assert gc.get_freeze_count() > 0
    assert gc.get_freeze_count() == 0
