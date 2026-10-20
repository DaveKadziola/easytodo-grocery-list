"""Template rendering shared by every HTML route."""

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from easytodo.config import Settings

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"
FLASH_COOKIE = "easytodo_flash"  # read and cleared by static/js/app.js
FLASH_MAX_AGE_S = 60

# autoescape=True for every template, whatever its extension.
templates = Jinja2Templates(
    env=Environment(loader=FileSystemLoader(FRONTEND_DIR / "templates"), autoescape=True)
)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def render(
    request: Request, name: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    """Render ``name`` with ``csrf_token``, ``current_user`` and ``settings`` added to
    ``context`` (keys given in ``context`` win)."""
    defaults = {
        "csrf_token": getattr(request.state, "csrf_token", None),
        "current_user": getattr(request.state, "user", None),
        "settings": request.app.state.settings,
    }
    return templates.TemplateResponse(request, name, defaults | context, status_code=status_code)


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
