"""How a person is shown: the name (or the email's local part), the initials and colour of the
avatar, and the template globals every page can use without a running app."""

import re
import subprocess
import sys

import pytest
from factories import create_user

from easytodo import web
from easytodo.display import (
    AVATAR_COLOR_COUNT,
    avatar_color,
    display_name,
    initials,
    user_display_name,
)
from easytodo.models import User

NASTY = 'O\'Reilly <b>"x"</b> <script>alert(1)</script>'
NASTY_ESCAPED = "O&#39;Reilly &lt;b&gt;&#34;x&#34;&lt;/b&gt; &lt;script&gt;alert(1)&lt;/script&gt;"


def avatar(user_id: int, name: str, **kwargs: str) -> str:
    template = web.templates.env.get_template("partials/_avatar.html")
    return template.module.avatar(user_id, name, **kwargs).strip()  # type: ignore[attr-defined]


def test_display_name_falls_back_to_the_email_local_part():
    assert display_name("dave@example.com", "Dave K.") == "Dave K."
    assert display_name("dave@example.com", None) == "dave"
    assert display_name("dave@example.com", "") == "dave"
    assert display_name("a.b+c@example.com", None) == "a.b+c"
    assert user_display_name(User(email="dave@example.com", display_name=None)) == "dave"
    assert user_display_name(User(email="dave@example.com", display_name="")) == "dave"
    assert user_display_name(User(email="dave@example.com", display_name="Dave K.")) == "Dave K."


async def test_user_display_name_reads_a_stored_user(db):
    user = await create_user(db, "dave@example.com", display_name="Anna Kowalska")
    assert user_display_name(user) == "Anna Kowalska"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Anna Maria Kowalska", "AK"),  # the first and the last word, not the middle one
        ("dave", "D"),
        ("łukasz", "Ł"),
        ("żaneta ślusarczyk", "ŻŚ"),
        ("  anna \t kowalska ", "AK"),
        ("", "?"),
        ("   ", "?"),
        ("ßtefan", "S"),  # "ß".upper() is "SS": still one letter
    ],
)
def test_initials(name, expected):
    assert initials(name) == expected


def test_avatar_color_cycles_through_the_palette():
    assert AVATAR_COLOR_COUNT == 8
    assert [avatar_color(i) for i in range(8)] == list(range(8))
    assert avatar_color(13) == 5


def test_avatar_macro_renders_initials_and_a_palette_colour():
    html = avatar(3, "Anna Kowalska")

    assert re.fullmatch(
        r'<span class="avatar [^"]+" aria-hidden="true" title="Anna Kowalska">AK</span>', html
    )
    assert "size-8" not in html
    classes = {re.search(r'class="avatar ([^"]+)"', avatar(i, "Anna")).group(1) for i in range(8)}
    assert len(classes) == 8
    assert avatar(13, "Anna") == avatar(5, "Anna")
    assert "size-8 text-sm" in avatar(1, "Anna", size="md")


def test_avatar_macro_escapes_the_name():
    html = avatar(1, NASTY)

    assert f'title="{NASTY_ESCAPED}"' in html
    assert "<script>" not in html and "<b>" not in html


@pytest.mark.parametrize(
    "first", ["easytodo.web", "easytodo.errors", "easytodo.boards.permissions"]
)
def test_template_globals_need_no_app(first):
    """The helpers are globals of ``web.templates`` as soon as any of the modules that import it
    is: no ``create_app()`` first, and no import cycle whichever comes first."""
    code = f"""
import {first}
from easytodo import web
from easytodo.models import BoardRole

names = ["static_url", "can", "Action", "display_name", "user_display_name", "initials",
         "avatar_color"]
missing = [name for name in names if name not in web.templates.env.globals]
assert not missing, missing
template = web.templates.env.from_string(
    "{{{{ can(role, Action.EDIT_CONTENT) }}}} {{{{ initials(display_name('anna.k@x.pl', None)) }}}} "
    "{{{{ avatar_color(9) }}}}"
)
print(template.render(role=BoardRole.EDITOR))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True A 1"
