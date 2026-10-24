"""Guards on helpers that the browser tests share (no browser here, so this runs in a plain
``pytest``)."""

import re
from pathlib import Path


def test_watch_console_is_defined_only_in_conftest():
    defined = [
        path.name
        for path in sorted(Path(__file__).parent.glob("*.py"))
        if re.search(r"^def watch_console\(", path.read_text(), re.MULTILINE)
    ]
    assert defined == ["conftest.py"]  # not copied into the test modules
