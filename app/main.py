"""FastAPI application factory.

Run with ``uvicorn app.main:app``. The module-level ``app`` is built on first access, so
importing this module (e.g. in tests) does not read the environment. If the environment is
invalid, accessing ``app`` prints each problem and exits, so the server refuses to start.
"""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from app import campaigns, render, ui, ui_account, ui_campaigns, ui_compose, ui_send
from app.auth import AccessVerifier
from app.config import ConfigError, Settings, load_settings
from app.db import Database
from app.msauth import MicrosoftAuth
from app.security import CsrfSigner, Forbidden, RequestGuard, forbidden_response
from app.sender import Clock, SendWorker, utcnow
from app.transport import Transport, make_transport

log = logging.getLogger("outlook_mailer")


class EmailImages(StaticFiles):
    """`/img/<file>`: public email images. Image files only (so CREDITS.md is not served), no
    directory listing (StaticFiles without `html=True` answers 404 for directories), long cache.

    Cached as immutable: image file names carry a version, and a changed image gets a new name
    (see `app/render.py`).
    """

    SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif"})
    CACHE_CONTROL = "public, max-age=31536000, immutable"

    async def get_response(self, path: str, scope: Scope) -> Response:
        if Path(path).suffix.lower() not in self.SUFFIXES:
            raise HTTPException(status_code=404)
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = self.CACHE_CONTROL
        return response


def create_app(
    settings: Settings | None = None,
    verifier: AccessVerifier | None = None,
    *,
    transport: Transport | None = None,
    clock: Clock | None = None,
    run_worker: bool = True,
) -> FastAPI:
    """Build the app. Settings are loaded from the environment when not given.

    ``verifier`` overrides the Cloudflare Access verifier, ``transport`` the one named by
    ``MAIL_TRANSPORT``, and ``clock`` the time source of the send worker and campaign pages.
    With ``run_worker=False`` the send worker is built (``app.state.worker``) but not started,
    so tests can drive it with ``step``. All for tests.

    Raises ``ConfigError`` if the environment is invalid.
    """
    if settings is None:
        settings = load_settings()

    logging.basicConfig(level=settings.log_level)
    # MSAL's debug output includes request details; keep it quiet even with LOG_LEVEL=DEBUG.
    logging.getLogger("msal").setLevel(
        max(logging.WARNING, logging.getLevelName(settings.log_level))
    )
    if settings.auth_disabled:
        log.warning(
            "AUTH_DISABLED=1: admin pages are NOT protected by Cloudflare Access. "
            "Use this for local testing only."
        )

    db = Database(Path(settings.data_dir) / "app.db")
    clock = clock or utcnow
    if transport is None:
        try:
            transport = make_transport(settings)
        except NotImplementedError as exc:
            log.warning("No send worker: %s", exc)
    worker = SendWorker(db, settings, transport, clock=clock) if transport is not None else None
    msauth = MicrosoftAuth(settings, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db.init()  # create the schema at startup so a bad DATA_DIR fails early
        msauth.load()  # local files only; nothing is sent to Microsoft at startup
        now = clock()
        interrupted = campaigns.recover_interrupted(db, now)
        if interrupted:
            log.warning(
                "%d recipient(s) were mid-send when the app stopped; marked failed, not resent.",
                interrupted,
            )
        cleaned = campaigns.cleanup_csvs(db, settings.data_dir, now)
        if cleaned:
            log.info("Deleted %d uploaded CSV file(s) no longer needed.", cleaned)
        if worker is not None and run_worker:
            worker.start()
        try:
            yield
        finally:
            # Joins the sign-in thread (up to 3 s): keep it off the event loop.
            await asyncio.to_thread(msauth.shutdown)
            if worker is not None:
                await worker.stop()
            if transport is not None:
                await transport.aclose()
            db.close()

    app = FastAPI(
        title="outlook-mailer", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.db = db
    app.state.clock = clock
    app.state.worker = worker
    app.state.msauth = msauth
    app.state.csrf_signer = CsrfSigner(settings.unsubscribe_secret)
    app.add_middleware(RequestGuard, settings=settings, verifier=verifier)
    app.add_exception_handler(Forbidden, forbidden_response)
    app.add_exception_handler(ui_send.UploadTooLarge, ui_send.upload_too_large)

    # HEAD too, for uptime monitors that probe with it.
    @app.api_route("/healthz", methods=["GET", "HEAD"])
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(ui.router)
    app.include_router(ui_send.upload_router)
    app.include_router(ui_send.router)
    app.include_router(ui_compose.router)
    app.include_router(ui_campaigns.router)
    app.include_router(ui_account.router)
    # Admin-only: the guard protects everything outside the public paths.
    app.mount("/static", StaticFiles(directory=ui.STATIC_DIR), name="static")
    # Public: images linked from sent emails (the guard treats /img/* as public).
    app.mount("/img", EmailImages(directory=render.EMAIL_IMG_DIR), name="img")

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
