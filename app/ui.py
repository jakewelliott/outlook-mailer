"""Admin UI pages (server-rendered Jinja2)."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.config import Settings
from app.security import ADMIN_DEPENDENCIES, csrf_field

APP_DIR = Path(__file__).parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

# Nav entries: (path, label). Pages not built yet render a placeholder.
NAV = [
    ("/", "Home"),
    ("/send/new", "New send"),
    ("/unsubscribes", "Unsubscribes"),
    ("/account", "Account"),
]
# Pages with real routes elsewhere: "/" here, "/send/new" in ui_send.
PLACEHOLDERS = {path: label for path, label in NAV if path not in ("/", "/send/new")}


def _templates() -> Jinja2Templates:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(csrf_field=csrf_field, nav=NAV)
    return Jinja2Templates(env=env)


templates = _templates()
router = APIRouter(dependencies=ADMIN_DEPENDENCIES, default_response_class=HTMLResponse)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/")
def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "ui/home.html.j2", {"settings": _settings(request), "active": "/"}
    )


def _placeholder(path: str, label: str) -> None:
    def page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "ui/placeholder.html.j2", {"title": label, "active": path}
        )

    router.add_api_route(path, page, methods=["GET"], name=f"placeholder:{path}")


for _path, _label in PLACEHOLDERS.items():
    _placeholder(_path, _label)
