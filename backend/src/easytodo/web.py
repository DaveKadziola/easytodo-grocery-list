"""Template rendering shared by every HTML route."""

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"

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
