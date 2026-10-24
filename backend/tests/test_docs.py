"""The documentation keeps up with the code: a setting nobody can read about is a setting nobody
sets."""

import re
from pathlib import Path

from easytodo.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def test_every_setting_is_in_the_readme_and_env_example():
    readme = (ROOT / "README.md").read_text()
    env_example = (ROOT / ".env.example").read_text()
    for name in Settings.model_fields:
        variable = f"EASYTODO_{name.upper()}"
        # A row of the Configuration table: the name in backticks in its first cell. Prose, which
        # has no leading "|", does not count.
        row = rf"^\|[^|\n]*`{re.escape(variable)}`[^|\n]*\|"
        assert re.search(row, readme, re.MULTILINE), f"README.md has no row for `{variable}`"
        assert f"{variable}=" in env_example, f".env.example has no line for {variable}="
