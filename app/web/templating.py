from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core.config import get_settings
from app.web.auth import csrf_token

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def render(request: Request, name: str, status_code: int = 200, **context: object):
    context.setdefault("app_name", get_settings().app_name)
    context["csrf_token"] = csrf_token(request)
    return templates.TemplateResponse(request, name, context, status_code=status_code)
