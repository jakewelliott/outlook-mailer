import base64
import re

import pytest

from app.security import CsrfSigner
from app.unsubscribe_tokens import make_token, unsubscribe_url, verify_token

SECRET = "s" * 32


def test_round_trip_and_normalised() -> None:
    token = make_token(SECRET, "  Ada@Example.COM ")
    assert token == make_token(SECRET, "ada@example.com")
    assert verify_token(SECRET, token) == "ada@example.com"
    assert re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{22}", token)
    encoded = token.split(".")[0]
    assert base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)) == b"ada@example.com"


def test_unicode_address() -> None:
    token = make_token(SECRET, "zoë@exämple.com")
    assert verify_token(SECRET, token) == "zoë@exämple.com"


def test_other_secret_rejected() -> None:
    assert verify_token("t" * 32, make_token(SECRET, "ada@example.com")) is None


def test_swapped_email_rejected() -> None:
    mac = make_token(SECRET, "ada@example.com").split(".")[1]
    other = base64.urlsafe_b64encode(b"bob@example.com").rstrip(b"=").decode()
    assert verify_token(SECRET, f"{other}.{mac}") is None


@pytest.mark.parametrize(
    "token",
    [
        "",
        ".",
        "abc",
        "YWRh.",
        "YWRhQGV4YW1wbGUuY29t.short",
        "YWRhQGV4YW1wbGUuY29t." + "A" * 22 + "x",
        "!!!!." + "A" * 22,
        "YWRhQGV4YW1wbGUuY29t.." + "A" * 22,
        # an upper-case address can't come from make_token
        base64.urlsafe_b64encode(b"Ada@example.com").rstrip(b"=").decode() + "." + "A" * 22,
        # invalid UTF-8
        base64.urlsafe_b64encode(b"\xff\xfe@x").rstrip(b"=").decode() + "." + "A" * 22,
    ],
)
def test_malformed_rejected(token: str) -> None:
    assert verify_token(SECRET, token) is None


def test_csrf_token_is_not_an_unsubscribe_mac() -> None:
    email = "ada@example.com"
    csrf = CsrfSigner(SECRET).token("n" * 43, email)
    encoded = make_token(SECRET, email).split(".")[0]
    assert verify_token(SECRET, f"{encoded}.{csrf[:22]}") is None


def test_url_shape() -> None:
    url = unsubscribe_url("https://mail.example.com/", SECRET, "ada@example.com")
    assert url == "https://mail.example.com/u/" + make_token(SECRET, "ada@example.com")


@pytest.mark.parametrize("email", ["", "   ", "a" * 310 + "@example.com"])
def test_bad_address_refused(email: str) -> None:
    with pytest.raises(ValueError):
        make_token(SECRET, email)
