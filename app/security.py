"""Request guard: Access check on admin paths, CSRF, Origin check and security headers.

Everything is admin except ``PUBLIC_PATHS`` / ``PUBLIC_PREFIXES`` (default deny). UI static files
under ``/static/`` are admin-only too; public pages must not depend on them.

CSRF uses signed double-submit: an HttpOnly cookie holds a random nonce, and forms carry
``HMAC(key, nonce + email)``. The key is derived from ``UNSUBSCRIBE_SECRET`` with a purpose
string, so tokens survive restarts and can't be reused as unsubscribe tokens (or vice versa). A
cookie planted from another subdomain is useless without the key. The token check runs as a
router dependency (``csrf_protect``) because it may need the parsed form body; the Origin check
runs in the middleware for every unsafe admin request.
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets
from urllib.parse import urlsplit

from fastapi import Depends, Request
from markupsafe import Markup
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth import JWT_HEADER, AccessDenied, AccessVerifier, KeysUnavailable
from app.config import Settings

log = logging.getLogger("outlook_mailer.security")

PUBLIC_PATHS = frozenset({"/healthz"})
PUBLIC_PREFIXES = ("/u/", "/img/")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

CSRF_COOKIE = "om_csrf"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32)

# Forward notes for later stages:
# - Stage 8: an ``<iframe sandbox srcdoc>`` preview inherits this CSP from the parent page, so the
#   email HTML's inline styles would be blocked. That frame needs a per-route CSP allowing inline
#   styles, or the preview must be served from its own route with its own CSP.
# - Stage 13: public pages (``/u/*``) can't use ``/static/`` (admin-only), so they need their own
#   minimal styling and CSP.
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
# Admin pages must use ``same-origin``, not ``no-referrer``: under ``no-referrer`` Chromium and
# Firefox send ``Origin: null`` and no Referer on same-origin form POSTs, so the Origin check
# would reject every admin form. ``same-origin`` still sends nothing to other sites. Public paths
# (unsubscribe links, email images) keep ``no-referrer``. ``Origin: null`` is never accepted.
ADMIN_REFERRER_POLICY = "same-origin"
PUBLIC_REFERRER_POLICY = "no-referrer"
# Close code for a websocket refused by the guard (policy violation).
WS_DENIED_CODE = 1008
WS_UNAVAILABLE_CODE = 1011

ACCESS_DENIED_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Access denied</title></head>
<body><h1>Access denied</h1>
<p>This page must be opened through Cloudflare Access. Sign in at the app's normal address.</p>
</body></html>
"""


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _origin(url: str) -> str | None:
    """``scheme://host[:port]`` in lower case, without default ports; None if not absolute."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


class CsrfSigner:
    def __init__(self, secret: str) -> None:
        self._key = hmac.new(secret.encode(), b"outlook-mailer csrf v1", hashlib.sha256).digest()

    def token(self, nonce: str, email: str) -> str:
        mac = hmac.new(self._key, f"{nonce}\n{email}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()

    def check(self, nonce: str, email: str, sent: str) -> bool:
        return hmac.compare_digest(self.token(nonce, email).encode(), sent.encode())


class RequestGuard:
    """Pure ASGI middleware (not BaseHTTPMiddleware), so request bodies pass through untouched."""

    def __init__(
        self, app: ASGIApp, settings: Settings, verifier: AccessVerifier | None = None
    ) -> None:
        self.app = app
        self.settings = settings
        if settings.auth_disabled:
            self.verifier = None
        else:
            assert settings.cf_access_team_domain and settings.cf_access_aud
            self.verifier = verifier or AccessVerifier(
                settings.cf_access_team_domain, settings.cf_access_aud
            )
        self.allowed_origin = _origin(settings.public_base_url)
        self.secure_cookie = settings.public_base_url.startswith("https://") and not (
            settings.auth_disabled
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
            return
        if scope["type"] != "http":  # lifespan
            await self.app(scope, receive, send)
            return

        admin = not is_public_path(scope["path"])
        new_nonce: str | None = None

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                headers["Referrer-Policy"] = (
                    ADMIN_REFERRER_POLICY if admin else PUBLIC_REFERRER_POLICY
                )
                if admin:
                    headers["Cache-Control"] = "no-store"
                if new_nonce is not None:
                    headers.append("Set-Cookie", self._cookie(new_nonce))
            await send(message)

        if not admin:
            await self.app(scope, receive, send_with_headers)
            return

        request = Request(scope)
        error = await self._check(request)
        if error is not None:
            await error(scope, receive, send_with_headers)
            return

        nonce = request.cookies.get(CSRF_COOKIE, "")
        if not _NONCE_RE.fullmatch(nonce):
            nonce = new_nonce = secrets.token_urlsafe(32)
        request.state.csrf_nonce = nonce
        await self.app(scope, receive, send_with_headers)

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Same public-path / Access decision as HTTP; a refused socket is closed before accept."""
        if not is_public_path(scope["path"]):
            conn = HTTPConnection(scope)
            try:
                await self._authenticate(conn, "WEBSOCKET")
            except AccessDenied:
                await send({"type": "websocket.close", "code": WS_DENIED_CODE})
                return
            except KeysUnavailable:
                await send({"type": "websocket.close", "code": WS_UNAVAILABLE_CODE})
                return
        await self.app(scope, receive, send)

    async def _authenticate(self, conn: HTTPConnection, method: str) -> None:
        """Verify the Access JWT and mark the connection as admin.

        Raises ``AccessDenied`` or ``KeysUnavailable``.
        """
        if self.verifier is None:
            conn.state.user_email = None
        else:
            try:
                identity = await self.verifier.verify(conn.headers.get(JWT_HEADER))
            except AccessDenied as exc:
                log.info("Denied %s %s: %s", method, conn.url.path, exc)
                raise
            conn.state.user_email = identity.email
        conn.state.admin = True

    async def _check(self, request: Request) -> Response | None:
        """Authenticate and Origin-check an admin request. Returns an error response or None."""
        try:
            await self._authenticate(request, request.method)
        except AccessDenied:
            return HTMLResponse(ACCESS_DENIED_HTML, status_code=403)
        except KeysUnavailable:
            return PlainTextResponse(
                "Cannot reach Cloudflare Access to check your sign-in. Try again shortly.",
                status_code=503,
                headers={"Retry-After": "30"},
            )

        if request.method not in SAFE_METHODS and not self._origin_ok(request):
            log.info("Rejected %s %s: bad Origin/Referer", request.method, request.url.path)
            return PlainTextResponse("Request origin not allowed.", status_code=403)
        return None

    def _origin_ok(self, request: Request) -> bool:
        # "Origin: null" (sandboxed frames, no-referrer pages, privacy redirects) is not a URL, so
        # _origin() returns None and it is rejected; the Referer is not consulted in its place.
        raw = request.headers.get("origin") or request.headers.get("referer")
        if not raw:
            return False
        origin = _origin(raw)
        if origin is None:
            return False
        allowed = {self.allowed_origin}
        if self.settings.auth_disabled:
            host = request.headers.get("host")
            if host:
                allowed.add(_origin(f"{request.url.scheme}://{host}"))
        return origin in allowed

    def _cookie(self, nonce: str) -> str:
        # Lax, not Strict: the HMAC token is the real protection, and Strict drops the cookie on
        # the first request after a cross-site navigation (e.g. the Access login redirect), which
        # reissues the nonce and invalidates tokens in already-open forms.
        cookie = f"{CSRF_COOKIE}={nonce}; Path=/; HttpOnly; SameSite=Lax"
        return cookie + "; Secure" if self.secure_cookie else cookie


class Forbidden(Exception):
    pass


def _signer(request: Request) -> CsrfSigner:
    return request.app.state.csrf_signer


def csrf_token(request: Request) -> str:
    return _signer(request).token(request.state.csrf_nonce, request.state.user_email or "")


def csrf_field(request: Request) -> Markup:
    """Hidden form input with the CSRF token (Jinja global)."""
    return Markup('<input type="hidden" name="{}" value="{}">').format(
        CSRF_FIELD, csrf_token(request)
    )


async def require_admin(request: Request) -> None:
    """Belt and braces: fail closed if a route is somehow reached without the guard."""
    if not getattr(request.state, "admin", False):
        raise Forbidden


async def csrf_protect(request: Request) -> None:
    """Router dependency: unsafe methods need a valid token (form field or header)."""
    if request.method in SAFE_METHODS:
        return
    sent = request.headers.get(CSRF_HEADER)
    if sent is None:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            value = (await request.form()).get(CSRF_FIELD)
            sent = value if isinstance(value, str) else None
    nonce = getattr(request.state, "csrf_nonce", None)
    if (
        sent is None
        or nonce is None
        or not _signer(request).check(nonce, request.state.user_email or "", sent)
    ):
        raise Forbidden


def forbidden_response(request: Request, exc: Exception) -> Response:
    return PlainTextResponse(
        "Security check failed. Reload the page and try again.", status_code=403
    )


# Every admin router is built with these, so new routes get the checks by default.
ADMIN_DEPENDENCIES = [Depends(require_admin), Depends(csrf_protect)]
