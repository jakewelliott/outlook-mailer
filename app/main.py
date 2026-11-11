"""FastAPI application factory.

Run with ``uvicorn app.main:app``. The module-level ``app`` is built on first access, so
importing this module (e.g. in tests) does not read the environment. If the environment is
invalid, accessing ``app`` prints each problem and exits, so the server refuses to start.
"""

import logging
import sys
from typing import Any

from fastapi import FastAPI

from app.config import ConfigError, Settings, load_settings

log = logging.getLogger("outlook_mailer")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Settings are loaded from the environment when not given.

    Raises ``ConfigError`` if the environment is invalid.
    """
    if settings is None:
        settings = load_settings()

    logging.basicConfig(level=settings.log_level)
    if settings.auth_disabled:
        log.warning(
            "AUTH_DISABLED=1: admin pages are NOT protected by Cloudflare Access. "
            "Use this for local testing only."
        )

    app = FastAPI(title="outlook-mailer", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _app
    if _app is None:
        try:
            _app = create_app()
        except ConfigError as exc:
            print(f"outlook-mailer refused to start. {exc}", file=sys.stderr)
            raise SystemExit(1) from None
    return _app
