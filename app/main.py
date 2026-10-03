from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import get_settings
from app.core.db import get_async_engine
from app.core.logging import configure_logging, get_logger
from app.web import routes_auth, routes_main, routes_sales, routes_settings
from app.web.auth import LoginRequired

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("api_started")
    yield
    await get_async_engine().dispose()  # graceful shutdown: close DB pool
    log.info("api_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.env != "dev")
    app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key.get_secret_value(),
        session_cookie="mp_session",
        max_age=settings.session_max_age_hours * 3600,
        same_site="lax",
        https_only=settings.is_prod,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
        )
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired):
        if request.headers.get("hx-request"):
            return RedirectResponse("/login", status_code=200, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
    app.include_router(routes_main.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_sales.router)
    app.include_router(routes_settings.router)
    return app


app = create_app()
