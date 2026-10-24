"""Template rendering shared by every HTML route."""

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from fastapi import Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from easytodo.boards.roles import Action, can
from easytodo.config import Settings
from easytodo.display import avatar_color, display_name, initials, user_display_name
from easytodo.security.tokens import mask_token

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"
STATIC_DIR = FRONTEND_DIR / "static"
FLASH_COOKIE = "easytodo_flash"  # read and cleared by static/js/app.js
FLASH_MAX_AGE_S = 60

# autoescape=True for every template, whatever its extension.
templates = Jinja2Templates(
    env=Environment(loader=FileSystemLoader(FRONTEND_DIR / "templates"), autoescape=True)
)


_asset_versions: dict[str, tuple[int, int, str]] = {}  # path -> (mtime_ns, size, version)


def static_url(path: str) -> str:
    """``/static/<path>?v=<version>``, the version taken from the file's content.

    For the app's own scripts and stylesheet, whose names stay the same across releases: a new
    version is a new URL, so browsers can keep each one for good (see ``CachedStaticFiles``) and
    still never run an old realtime.js against new pages. Hashed again only when the file changes.
    """
    file = STATIC_DIR / path
    stat = file.stat()
    known = _asset_versions.get(path)
    if known is None or known[:2] != (stat.st_mtime_ns, stat.st_size):
        known = (stat.st_mtime_ns, stat.st_size, hashlib.sha256(file.read_bytes()).hexdigest()[:12])
        _asset_versions[path] = known
    return f"/static/{path}?v={known[2]}"


# What templates call without being passed it: {% if can(role, Action.EDIT_CONTENT) %} shows
# controls by the member's role, the display helpers show a person (see display.py). Set here, not
# when the app is created, so that a template renders with them whichever module is imported first.
templates.env.globals.update(
    static_url=static_url,
    can=can,
    Action=Action,
    display_name=display_name,
    user_display_name=user_display_name,
    initials=initials,
    avatar_color=avatar_color,
)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def prompt_answer(request: Request) -> str:
    """What the user typed into an ``hx-prompt``, which htmx sends as ``HX-Prompt``; "" when the
    request has none.

    Text a header cannot carry (beyond Latin-1, such as "Łosoś") htmx sends percent-encoded, and
    says so in ``HX-Prompt-URI-AutoEncoded``: only then is it decoded.
    """
    answer = request.headers.get("HX-Prompt", "")
    if request.headers.get("HX-Prompt-URI-AutoEncoded") == "true":
        answer = unquote(answer)
    return answer


def render(
    request: Request, name: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    """Render ``name`` with ``csrf_token``, ``current_user`` and ``settings`` added to
    ``context`` (keys given in ``context`` win).

    ``csrf_token`` is a new mask of the token on every call (see ``mask_token``), shared by the
    page's meta tag and its forms; the token itself is never in a page."""
    csrf_token = getattr(request.state, "csrf_token", None)
    defaults = {
        "csrf_token": None if csrf_token is None else mask_token(csrf_token),
        "current_user": getattr(request.state, "user", None),
        "settings": request.app.state.settings,
    }
    return templates.TemplateResponse(request, name, defaults | context, status_code=status_code)


def no_store(response: HTMLResponse) -> HTMLResponse:
    """For a page that holds a secret (a TOTP key, recovery codes, a temporary password) or the
    form of a link that works once: no cache, the browser's included, keeps a copy."""
    response.headers["Cache-Control"] = "no-store"
    return response


def flash(response: Response, message: str, settings: Settings, level: str = "info") -> None:
    """Show ``message`` as a toast on the next page the browser loads.

    For a redirect after a plain form post, which cannot carry ``HX-Trigger``. The cookie is
    readable by script on purpose: app.js turns it into a toast and deletes it.
    """
    payload = quote(json.dumps({"level": level, "message": message}), safe="")
    response.set_cookie(
        FLASH_COOKIE,
        payload,
        max_age=FLASH_MAX_AGE_S,
        path="/",
        secure=settings.cookie_secure,
        httponly=False,
        samesite="lax",
    )
