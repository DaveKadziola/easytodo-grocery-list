"""The page shell: base layout, static files and the vendored scripts."""

import re

import pytest

from easytodo.security.csrf import CSRF_COOKIE
from easytodo.web import FRONTEND_DIR

VENDOR_SCRIPTS = {
    "htmx-2.0.4.min.js": "2.0.4",
    "alpine-csp-3.14.8.min.js": "3.14.8",
    "sortable-1.15.6.min.js": "1.15.6",
}


async def test_base_layout(client):
    r = await client.get("/login")
    token = client.cookies[CSRF_COOKIE]
    assert f'<meta name="csrf-token" content="{token}">' in r.text
    assert (
        '<meta name="htmx-config" '
        """content='{"includeIndicatorStyles": false, "defaultSwapStyle": "outerHTML"}'>"""
    ) in r.text
    scripts = re.findall(r"<script([^>]*)>(.*?)</script>", r.text, re.DOTALL)
    assert [attrs for attrs, _ in scripts] == [
        ' src="/static/vendor/htmx-2.0.4.min.js" defer',
        ' src="/static/vendor/alpine-csp-3.14.8.min.js" defer',
        ' src="/static/vendor/sortable-1.15.6.min.js" defer',
        ' src="/static/js/app.js" defer',
    ]
    assert all(body == "" for _, body in scripts)  # no inline scripts (CSP script-src 'self')
    assert re.search(r'<div id="toasts" aria-live="polite"[^>]*></div>', r.text)


@pytest.mark.parametrize(
    "path",
    [f"/static/vendor/{name}" for name in VENDOR_SCRIPTS]
    + ["/static/js/app.js", "/static/images/favicon.webp"],
)
async def test_static_files_are_served(client, path):
    r = await client.get(path)
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("name,version", VENDOR_SCRIPTS.items())
def test_vendored_scripts_match_their_file_names(name, version):
    assert version in (FRONTEND_DIR / "static" / "vendor" / name).read_text()


def test_js_never_parses_strings_as_html():
    """Toasts (and anything else) go in via textContent: messages can quote names that other
    board members chose."""
    scripts = sorted((FRONTEND_DIR / "static" / "js").glob("*.js"))
    assert scripts
    for path in scripts:
        source = path.read_text()
        for sink in ("innerHTML", "insertAdjacentHTML"):
            assert sink not in source, f"{path.name} uses {sink}"
