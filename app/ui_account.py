"""The Account page: connect the Outlook account (device code), cancel, sign out.

- ``GET /account``: the state (mock, not configured, signed out, signing in with the code and
  link, signed in as ...), messages and the buttons.
- ``GET /account/status``: small JSON for ``app.js``, which polls it while a sign-in is in
  progress and reloads the page when the state changes. Without JavaScript: a Refresh link.
- ``POST /account/connect``, ``/account/cancel``, ``/account/sign-out``: CSRF-checked, then
  redirect back to ``/account`` (results show on the page).
"""

import contextlib
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.msauth import TROUBLESHOOTING_URL, MicrosoftAuth, SignInError
from app.security import ADMIN_DEPENDENCIES
from app.ui import templates

ACTIVE = "/account"
POLL_SECONDS = 3

router = APIRouter(dependencies=ADMIN_DEPENDENCIES, default_response_class=HTMLResponse)


def _auth(request: Request) -> MicrosoftAuth:
    return request.app.state.msauth


def _back() -> RedirectResponse:
    return RedirectResponse("/account", status_code=303)


def _minutes(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


@router.get("/account")
def account(request: Request) -> HTMLResponse:
    status = _auth(request).status()
    return templates.TemplateResponse(
        request,
        "ui/account.html.j2",
        {
            "active": ACTIVE,
            "s": status,
            "left": _minutes(status.seconds_left) if status.seconds_left is not None else None,
            "poll_seconds": POLL_SECONDS,
            "troubleshooting_url": TROUBLESHOOTING_URL,
        },
    )


@router.get("/account/status")
def account_status(request: Request) -> JSONResponse:
    status = _auth(request).status()
    data: dict[str, Any] = {"state": status.state, "seconds_left": status.seconds_left}
    return JSONResponse(data)


# Plain ``def``: starting a sign-in waits for Microsoft, so FastAPI runs these in a thread.
@router.post("/account/connect")
def connect(request: Request) -> RedirectResponse:
    # A failure's message is kept in the status and shown on the page.
    with contextlib.suppress(SignInError):
        _auth(request).start_sign_in()
    return _back()


@router.post("/account/cancel")
def cancel(request: Request) -> RedirectResponse:
    _auth(request).cancel_sign_in()
    return _back()


@router.post("/account/sign-out")
def sign_out(request: Request) -> RedirectResponse:
    _auth(request).sign_out()
    return _back()
