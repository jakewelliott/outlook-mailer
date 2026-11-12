"""Unsubscribe tokens: ``base64url(email) "." base64url(HMAC-SHA256(key, email)[:16])``.

The address is lower-cased and trimmed first (``unsubscribed.txt`` is compared in lower case). The
HMAC key is derived from ``UNSUBSCRIBE_SECRET`` with a purpose string, so these tokens and the
CSRF tokens (``app/security.py``) can never stand in for each other. 16 bytes (128 bits) of MAC
keep the link short and are far beyond guessing. Base64url without padding, so a token only holds
``A-Z a-z 0-9 - _`` and one ``.``.

Changing ``UNSUBSCRIBE_SECRET`` breaks the links in emails already sent; CAN-SPAM needs them to keep
working for at least 30 days.
"""

import base64
import binascii
import hashlib
import hmac
import re

from app.emails import normalise_key

UNSUBSCRIBE_PATH = "/u/"
ONE_CLICK_SUFFIX = "/one-click"  # /u/<token>/one-click: the List-Unsubscribe header's URL
MAC_BYTES = 16
MAX_EMAIL_BYTES = 320
_PURPOSE = b"outlook-mailer unsubscribe v1"
_TOKEN_RE = re.compile(r"([A-Za-z0-9_-]{4,440})\.([A-Za-z0-9_-]{22})")


def _key(secret: str) -> bytes:
    return hmac.new(secret.encode(), _PURPOSE, hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: str, email: str) -> bytes:
    return hmac.new(_key(secret), email.encode(), hashlib.sha256).digest()[:MAC_BYTES]


def make_token(secret: str, email: str) -> str:
    email = normalise_key(email)
    if not email or len(email.encode()) > MAX_EMAIL_BYTES:
        raise ValueError("email must be a non-empty address of at most 320 bytes")
    return f"{_b64(email.encode())}.{_b64(_mac(secret, email))}"


def verify_token(secret: str, token: str) -> str | None:
    """The (lower-case) address a valid token was made for, else None. Constant-time MAC check."""
    m = _TOKEN_RE.fullmatch(token)
    if m is None:
        return None
    try:
        email = _unb64(m.group(1)).decode("utf-8")
        sent = _unb64(m.group(2))
    except (binascii.Error, UnicodeDecodeError):
        return None
    if email != normalise_key(email) or not email or len(email.encode()) > MAX_EMAIL_BYTES:
        return None
    if not hmac.compare_digest(_mac(secret, email), sent):
        return None
    return email


def unsubscribe_url(public_base_url: str, secret: str, email: str) -> str:
    """``PUBLIC_BASE_URL/u/<token>`` for ``email``."""
    return f"{public_base_url.rstrip('/')}{UNSUBSCRIBE_PATH}{make_token(secret, email)}"


def one_click_url(page_url: str) -> str:
    """The RFC 8058 one-click URL for the page at ``page_url`` (``.../u/<token>/one-click``).

    Only the ``List-Unsubscribe`` header carries it; the email body links to the page itself.
    A URL with a query or fragment is refused (``ValueError``): the suffix would land inside it.
    """
    if "?" in page_url or "#" in page_url:
        raise ValueError("the unsubscribe page URL must not contain '?' or '#'")
    return page_url + ONE_CLICK_SUFFIX
