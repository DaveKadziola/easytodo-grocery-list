"""The page shell: base layout, static files, the vendored scripts, and what a phone needs to
install the app on its home screen and keep clear of the notch and the home indicator."""

import re
import struct

import pytest

from easytodo.security.csrf import CSRF_COOKIE
from easytodo.web import FLASH_COOKIE, FRONTEND_DIR

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
    + ["/static/js/app.js", "/static/js/board.js", "/static/js/realtime.js"]
    + ["/static/images/favicon.webp", "/static/manifest.webmanifest"],
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


def test_app_js_reads_the_cookie_the_server_sets_for_flash_toasts():
    source = (FRONTEND_DIR / "static" / "js" / "app.js").read_text()
    assert f'"{FLASH_COOKIE}"' in source


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


async def test_base_layout_is_ready_for_phones_and_home_screens(client):
    html = (await client.get("/login")).text
    assert (
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
    ) in html
    assert '<meta name="color-scheme" content="light dark">' in html
    assert '<link rel="manifest" href="/static/manifest.webmanifest">' in html
    assert '<link rel="apple-touch-icon" href="/static/images/apple-touch-icon.png">' in html


async def test_manifest_makes_the_app_installable(client):
    r = await client.get("/static/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/manifest+json"
    manifest = r.json()
    assert manifest["name"] == manifest["short_name"] == "EasyToDo"
    assert manifest["start_url"] == manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["background_color"] and manifest["theme_color"]

    for icon in manifest["icons"]:
        r = await client.get(icon["src"])
        assert r.status_code == 200 and r.headers["content-type"] == "image/png", icon
        width, height = map(int, icon["sizes"].split("x"))
        assert png_size(r.content) == (width, height), icon
    plain = {icon["sizes"] for icon in manifest["icons"] if icon.get("purpose", "any") == "any"}
    assert {"192x192", "512x512"} <= plain
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])


async def test_apple_touch_icon_is_a_180px_png(client):
    r = await client.get("/static/images/apple-touch-icon.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert png_size(r.content) == (180, 180)


def test_styles_keep_the_bottom_bar_clear_of_the_home_indicator():
    source = (FRONTEND_DIR / "styles" / "input.css").read_text()
    assert "env(safe-area-inset-bottom)" in source
