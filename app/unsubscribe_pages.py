"""Public unsubscribe pages under ``/u/<token>`` (no Cloudflare Access, no admin CSRF).

Routes:

- ``GET`` / ``HEAD /u/<token>``: the confirmation page (the link in the email's footer). It asks the
  visitor to type their email address and press **Unsubscribe**. It never records anything, so
  link scanners and previews that fetch the link don't unsubscribe anyone, and it never shows the
  address.
- ``POST /u/<token>``: that page's form. Records the opt-out only if the typed ``email`` matches the
  token's address (``unsubscribes.address_keys``: case, surrounding spaces and Unicode/punycode
  domain forms don't matter); otherwise the form is shown again (HTTP 400) saying the address
  doesn't match and to look at the original email for the address it was sent to. The typed value
  is never echoed back and the real address is never shown or hinted at. A body over
  ``MAX_FORM_BYTES`` (from ``Content-Length``, or as soon as the streamed body passes it; never
  buffered further) and a typed value over ``MAX_TYPED_BYTES`` of UTF-8 are mismatches like any
  other, so the typed value is checked before it is normalised or validated. The comparison
  (email-validator) and the recording run in a worker thread, never on the event loop. A client
  that disconnects before the whole body arrives gets an empty 400 (``DISCONNECTED_STATUS``):
  nothing is recorded, no rate limit is touched and nothing is logged above debug.
- ``POST /u/<token>/one-click``: the RFC 8058 one-click target, the only URL in the
  ``List-Unsubscribe`` header (``unsubscribe_tokens.one_click_url``). Mail providers can't type
  anything, so it records with no body requirements (``List-Unsubscribe=One-Click`` form-encoded,
  multipart, or anything else; the body is never read, so a large one can't slow it down).
- ``GET`` / ``HEAD /u/<token>/one-click`` (a mail app that opens the header URL in a browser): the
  same confirmation page as ``GET /u/<token>``; records nothing.

Repeating a recording POST is harmless (same page, no new line).

The typed address is a guard against accidental unsubscribes (someone a newsletter was forwarded
to pressing the button), not a security boundary: the token is the capability, and it encodes the
address anyway. It is an HMAC of the address (``unsubscribe_tokens``), so only someone holding the
link from the email can make a valid request, and there is no cookie or session for a cross-site
request to borrow, so classic CSRF has nothing to abuse: a third-party site that knows a token
could just as well send the POST itself (or the one-click POST, which needs no typing). An Origin
check is impossible (mail providers send none, and these pages' ``no-referrer`` makes browsers
send ``Origin: null``). The worst a request can do is unsubscribe the address the token was made
for. Framing is forbidden (``frame-ancestors 'none'``), so the button can't be clickjacked.

Rate limits (``app.ratelimit``), checked only after the token has been verified:

- Per client IP (``ip_limiter``, ``RATE_*``): every ``GET``/``HEAD``, every request with an invalid
  token, and every form POST whose typed address doesn't match.
- Per address (``address_limiter``, ``ADDRESS_*``), two buckets: ``mismatch:<email>`` takes one per
  non-matching form POST (with the IP bucket: guessing protection); ``repeat:<email>`` takes one
  only when a recording POST finds the address already recorded, and a recording POST is refused
  only when that bucket is empty. So the first genuine opt-out for an address is never refused:
  its ``repeat`` bucket is full until the address is already in the file.
- A valid recording POST (one-click, or the form with a matching address) never touches the IP
  bucket: Google and Microsoft send one-click POSTs from their own servers, many people's from one
  address, and a 429 there would silently lose an opt-out.

The IP comes from ``CF-Connecting-IP`` (``ratelimit.client_key``). A ``/u/`` request without it
is logged as a warning, at most once per ``MISSING_IP_WARN_SECONDS``: behind cloudflared it means
every visitor shares one bucket.

The pages reveal nothing: no address, no business name, nothing from campaigns or drafts (which
are deleted over time; the link must work 30+ days later and depends only on
``UNSUBSCRIBE_SECRET``). An invalid token gets the same generic 404 page whatever is wrong with it.
A write failure (or ``unsubscribed.txt`` locked for too long) is a 503 "try again", never a success.

Styling: ``app.public_style`` (inline CSS allowed by hash in a page-specific CSP).
"""

import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.campaigns import local_zone
from app.public_style import PAGE_CSS
from app.ratelimit import CF_CONNECTING_IP, RateLimiter, client_key
from app.security import DISCONNECTED_STATUS
from app.unsubscribe_tokens import (
    MAX_EMAIL_BYTES,
    ONE_CLICK_SUFFIX,
    UNSUBSCRIBE_PATH,
    verify_token,
)
from app.unsubscribes import address_keys, record_unsubscribe

log = logging.getLogger("outlook_mailer.unsubscribe")

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Per client IP: 20 requests at once, then one every 3 s. Only page views, invalid tokens and
# mistyped addresses count; valid opt-outs don't (see the module docstring).
RATE_CAPACITY = 20
RATE_REFILL_PER_SECOND = 1 / 3
RATE_MAX_CLIENTS = 10_000
# Per address: 5 at once, then one a minute, for mistyped addresses and for repeat opt-outs.
ADDRESS_CAPACITY = 5
ADDRESS_REFILL_PER_SECOND = 1 / 60
ADDRESS_MAX_KEYS = 10_000
MISSING_IP_WARN_SECONDS = 3600.0
WRITE_RETRY_AFTER = "60"
# A typed value over MAX_EMAIL_BYTES of UTF-8 (surrounding spaces included, before any
# normalising) can't be a token's address, so it is a mismatch without being compared: the
# comparison (email-validator) costs far more on long input, e.g. combining marks.
MAX_TYPED_BYTES = MAX_EMAIL_BYTES
# The form's body (one short field, url-encoded or multipart): anything bigger is a mismatch,
# refused from its Content-Length or as soon as the streamed body passes the limit.
MAX_FORM_BYTES = 4096
_DIGITS_RE = re.compile(r"\d+", re.ASCII)

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.globals["page_css"] = Markup(PAGE_CSS)

router = APIRouter(default_response_class=HTMLResponse)


def make_limiter() -> RateLimiter:
    """The per-IP limiter (``app.state.unsubscribe_limiter``)."""
    return RateLimiter(
        capacity=RATE_CAPACITY,
        refill_per_second=RATE_REFILL_PER_SECOND,
        max_clients=RATE_MAX_CLIENTS,
    )


def make_address_limiter() -> RateLimiter:
    """The per-address limiter (``app.state.unsubscribe_address_limiter``)."""
    return RateLimiter(
        capacity=ADDRESS_CAPACITY,
        refill_per_second=ADDRESS_REFILL_PER_SECOND,
        max_clients=ADDRESS_MAX_KEYS,
    )


class MissingIpWarning:
    """Logs "no CF-Connecting-IP" at most once per ``every`` seconds."""

    def __init__(
        self, every: float = MISSING_IP_WARN_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.every = every
        self.clock = clock
        self._last: float | None = None
        self._lock = threading.Lock()

    def check(self, request: Request) -> None:
        if CF_CONNECTING_IP in request.headers:
            return
        with self._lock:
            now = self.clock()
            if self._last is not None and now - self._last < self.every:
                return
            self._last = now
        log.warning(
            "An unsubscribe request arrived without the CF-Connecting-IP header, so visitors "
            "can't be told apart for rate limiting (behind cloudflared they all share one limit). "
            "Make sure Cloudflare passes it: don't turn on 'Remove visitor IP headers'. "
            "Requests straight to the LAN port never have it. (Logged at most once an hour.)"
        )


def _page(name: str, status: int = 200, **context: object) -> HTMLResponse:
    html = _env.get_template(f"public/{name}.html.j2").render(**context)
    return HTMLResponse(html, status_code=status)


def _too_many(limiter: RateLimiter, key: str) -> HTMLResponse:
    response = _page("rate_limited", 429)
    response.headers["Retry-After"] = str(limiter.retry_after(key))
    return response


def _ip_limited(request: Request) -> HTMLResponse | None:
    """Take one from the client IP's bucket; the 429 page if it is empty."""
    limiter: RateLimiter = request.app.state.unsubscribe_limiter
    key = client_key(request)
    return None if limiter.allow(key) else _too_many(limiter, key)


def _start(request: Request, token: str) -> str | None:
    """Warn about a missing ``CF-Connecting-IP``; the token's address, or None if invalid."""
    request.app.state.unsubscribe_ip_warning.check(request)
    return verify_token(request.app.state.settings.unsubscribe_secret, token)


def _invalid(request: Request) -> HTMLResponse:
    """An invalid token: counts against the client IP, then the generic 404."""
    limited = _ip_limited(request)
    return limited if limited is not None else _page("invalid", 404)


def _confirm(request: Request, token: str) -> HTMLResponse:
    if _start(request, token) is None:
        return _invalid(request)
    limited = _ip_limited(request)
    if limited is not None:
        return limited
    return _page("confirm", action=UNSUBSCRIBE_PATH + token, mismatch=False)


def _record(request: Request, email: str) -> HTMLResponse:
    """Record ``email``'s opt-out (a valid token's, so never IP-limited). In a worker thread."""
    limiter: RateLimiter = request.app.state.unsubscribe_address_limiter
    repeat = "repeat:" + email
    if not limiter.has_room(repeat):  # only empty after several POSTs found it already recorded
        return _too_many(limiter, repeat)
    settings = request.app.state.settings
    at = request.app.state.clock().astimezone(local_zone(settings.tz))
    try:
        added = record_unsubscribe(settings.data_dir, email, at)
    except ValueError:  # an address no real token holds (tab or line break)
        return _page("invalid", 404)
    except OSError:  # includes LockTimeout: the file stayed locked
        log.exception("Could not record an unsubscribe in unsubscribed.txt")
        response = _page("error", 503)
        response.headers["Retry-After"] = WRITE_RETRY_AFTER
        return response
    if added:
        log.info("Unsubscribe recorded.")
    else:
        limiter.allow(repeat)
    return _page("done")


def _typed_matches(typed: object, email: str) -> bool:
    """Whether ``typed`` is ``email`` in one of its forms. Calls email-validator, so it runs in a
    worker thread, and only on values of at most ``MAX_TYPED_BYTES`` (checked first)."""
    if not isinstance(typed, str) or len(typed.encode("utf-8", "surrogatepass")) > MAX_TYPED_BYTES:
        return False
    if not typed.strip():
        return False
    return not address_keys(typed).isdisjoint(address_keys(email))


async def _limited_body(request: Request) -> bytes | None:
    """The request body, or None if it is over ``MAX_FORM_BYTES`` (never buffering more)."""
    length = request.headers.get("content-length", "")
    if _DIGITS_RE.fullmatch(length) and (len(length) > 18 or int(length) > MAX_FORM_BYTES):
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return None
    return bytes(body)


async def _typed_email(request: Request) -> object:
    """The form's ``email`` value; None for an oversized or malformed body. Raises
    ``ClientDisconnect`` if the client disconnects before the body has arrived."""
    body = await _limited_body(request)
    if body is None:
        return None

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    try:
        form = await Request(request.scope, receive).form()
    except Exception:  # a malformed body is the same as no address typed
        return None
    try:
        return form.get("email")
    finally:
        await form.close()


@router.api_route(UNSUBSCRIBE_PATH + "{token}", methods=["GET", "HEAD"])
def confirm(request: Request, token: str) -> HTMLResponse:
    return _confirm(request, token)


@router.api_route(UNSUBSCRIBE_PATH + "{token}" + ONE_CLICK_SUFFIX, methods=["GET", "HEAD"])
def one_click_page(request: Request, token: str) -> HTMLResponse:
    """A browser opening the header URL: the normal confirmation page, nothing recorded."""
    return _confirm(request, token)


@router.post(UNSUBSCRIBE_PATH + "{token}")
async def unsubscribe(request: Request, token: str) -> Response:
    """The confirmation page's form: records only when the typed address matches."""
    email = _start(request, token)
    if email is None:
        return _invalid(request)
    try:
        typed = await _typed_email(request)
    except ClientDisconnect:  # the client left mid-body: nobody to answer, nothing to record
        log.debug("An unsubscribe form POST was dropped before its body arrived.")
        return Response(status_code=DISCONNECTED_STATUS)
    return await run_in_threadpool(_submit, request, token, email, typed)


def _submit(request: Request, token: str, email: str, typed: object) -> HTMLResponse:
    """Compare the typed value and record or refuse. In a worker thread (off the event loop)."""
    if _typed_matches(typed, email):
        return _record(request, email)
    limited = _ip_limited(request)
    if limited is not None:
        return limited
    limiter: RateLimiter = request.app.state.unsubscribe_address_limiter
    if not limiter.allow("mismatch:" + email):
        return _too_many(limiter, "mismatch:" + email)
    return _page("confirm", 400, action=UNSUBSCRIBE_PATH + token, mismatch=True)


@router.post(UNSUBSCRIBE_PATH + "{token}" + ONE_CLICK_SUFFIX)
def one_click(request: Request, token: str) -> HTMLResponse:
    """RFC 8058 one-click POST from a mail provider: no body needed. Runs in a worker thread."""
    email = _start(request, token)
    if email is None:
        return _invalid(request)
    return _record(request, email)
