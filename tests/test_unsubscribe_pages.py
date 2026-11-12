"""Public unsubscribe pages (``/u/<token>``, ``/u/<token>/one-click``) and the admin page."""

import asyncio
import base64
import fcntl
import hashlib
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import campaigns, unsubscribe_pages, unsubscribes
from app.config import Settings, load_settings
from app.main import create_app
from app.public_style import PAGE_CSS, UNSUBSCRIBE_CSP
from app.ratelimit import RateLimiter
from app.unsubscribe_tokens import make_token, one_click_url, unsubscribe_url
from tests.test_gate import outbox
from tests.test_ratelimit import FakeMonotonic, request
from tests.test_security import AUD, TEAM
from tests.test_sender import FakeClock
from tests.test_ui_campaigns import ready_draft, start, step_all
from tests.test_ui_compose import BASE

SECRET = "x" * 32  # conftest's UNSUBSCRIBE_SECRET
ADA = "ada@example.com"
ONE_CLICK = {"content": b"List-Unsubscribe=One-Click"}
FORM_TYPE = {"Content-Type": "application/x-www-form-urlencoded"}
STYLE_RE = re.compile(r"<style>(.*?)</style>", re.S)
MISMATCH = "That email address doesn't match."
REFER_BACK = "Look at the original email to find the\naddress it was sent to"


@pytest.fixture
def app_settings(valid_env: dict[str, str], tmp_path: Path) -> Settings:
    return load_settings({**valid_env, "DATA_DIR": str(tmp_path), "SEND_RATE_PER_MINUTE": "60"})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(app_settings: Settings, clock: FakeClock) -> Iterator[TestClient]:
    with TestClient(create_app(app_settings, clock=clock, run_worker=False), base_url=BASE) as c:
        yield c


def path_for(email: str, secret: str = SECRET) -> str:
    """The confirmation page (the email's footer link)."""
    return "/u/" + make_token(secret, email)


def one_click_for(email: str, secret: str = SECRET) -> str:
    """The RFC 8058 one-click route (the List-Unsubscribe header)."""
    return path_for(email, secret) + "/one-click"


def typed(email: str) -> dict[str, object]:
    """The confirmation form, submitted with ``email`` typed in."""
    return {"data": {"email": email}}


def file_text(tmp_path: Path) -> str | None:
    path = tmp_path / "unsubscribed.txt"
    return path.read_text(encoding="utf-8") if path.exists() else None


def expected_line(clock: FakeClock, email: str = ADA) -> str:
    at = clock().astimezone(campaigns.local_zone("America/New_York"))
    return f"{email}\t{at.isoformat(timespec='seconds')}\n"


class LockHeld:
    """Hold an exclusive flock on ``unsubscribed.txt`` and make lock waits instant: the module's
    clock jumps by each poll's sleep, so a waiter gives up after LOCK_WAIT_SECONDS of fake time."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.path = tmp_path / "unsubscribed.txt"
        self.path.touch()
        self.now = 0.0
        self.sleeps = 0
        monkeypatch.setattr(unsubscribes, "_monotonic", lambda: self.now)
        monkeypatch.setattr(unsubscribes, "_sleep", self._sleep)

    def _sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds

    def __enter__(self) -> "LockHeld":
        self.f = self.path.open("rb")
        fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        self.f.close()


# --- GET: confirmation only --------------------------------------------------------------------


def test_get_shows_the_form_and_records_nothing(client: TestClient, tmp_path: Path) -> None:
    path = path_for(ADA)
    for url in (path, path + "/one-click"):  # a browser opening the header URL gets the same page
        r = client.get(url)
        assert r.status_code == 200
        assert "Unsubscribe?" in r.text
        assert f'<form method="post" action="{path}">' in r.text  # never the one-click route
        assert '<input id="email" name="email" type="text"' in r.text
        assert '<label for="email">' in r.text
        assert r.text.count("<button") == 1 and ">Unsubscribe</button>" in r.text
        assert MISMATCH not in r.text
        assert ADA not in r.text and "ada" not in r.text.lower()
        assert client.head(url).status_code == 200
        for _ in range(3):
            client.get(url)
    assert file_text(tmp_path) is None


# --- POST /u/<token>: the page's form, needs the typed address ----------------------------------


@pytest.mark.parametrize("entered", [ADA, "  ADA@Example.COM  ", "Ada@EXAMPLE.com"])
def test_form_with_matching_email_records_once(
    client: TestClient, tmp_path: Path, clock: FakeClock, entered: str
) -> None:
    path = path_for(ADA)
    r = client.post(path, **typed(entered))  # no CSRF token, no cookie
    assert r.status_code == 200
    assert "You're unsubscribed" in r.text and ADA not in r.text
    first = expected_line(clock)
    assert file_text(tmp_path) == first
    clock.advance(3600)
    r = client.post(path, **typed(entered))
    assert r.status_code == 200 and "You're unsubscribed" in r.text
    assert file_text(tmp_path) == first


@pytest.mark.parametrize(
    ("token_for", "entered"),
    [
        ("jose@bücher.example", "jose@xn--bcher-kva.example"),
        ("jose@xn--bcher-kva.example", "Jose@BÜCHER.example"),
        ("jose@bücher.example", " jose@bücher.example\t"),
    ],
)
def test_form_matches_idn_forms(
    client: TestClient, tmp_path: Path, token_for: str, entered: str
) -> None:
    r = client.post(path_for(token_for), **typed(entered))
    assert r.status_code == 200 and "You're unsubscribed" in r.text
    assert (file_text(tmp_path) or "").startswith(token_for.lower() + "\t")


@pytest.mark.parametrize(
    "kw",
    [
        typed(""),
        typed("   "),
        typed("eve@example.com"),
        typed("ada@example.org"),
        typed("ada@example.com.evil.example"),
        typed("ada"),
        typed("not an address"),
        typed("ada@example.com\nada@example.com"),
        typed("a" * 5000),
        {},  # no field at all (the old one-button form)
        {"data": {"mail": ADA}},  # wrong field name
        {**ONE_CLICK, "headers": FORM_TYPE},  # a one-click body sent to the page by mistake
        {"files": {"email": ("ada.txt", ADA.encode())}},  # a file, not a typed value
        {"content": b"\xff\xfe--garbage", "headers": {"Content-Type": "multipart/form-data"}},
    ],
)
def test_form_without_a_matching_email_records_nothing(
    client: TestClient, tmp_path: Path, kw: dict[str, object]
) -> None:
    path = path_for(ADA)
    r = client.post(path, **kw)  # type: ignore[arg-type]
    assert r.status_code == 400
    assert MISMATCH in r.text and REFER_BACK in r.text
    assert f'<form method="post" action="{path}">' in r.text  # they can try again
    assert "You're unsubscribed" not in r.text
    assert "ada" not in r.text.lower() and "example.com" not in r.text  # no address, no hint
    assert "eve@" not in r.text and 'value="' not in r.text  # the typed value isn't echoed
    assert r.headers["content-security-policy"] == UNSUBSCRIBE_CSP
    assert file_text(tmp_path) is None


def test_typed_value_is_never_reflected(client: TestClient) -> None:
    r = client.post(path_for(ADA), **typed('"><script>alert(1)</script>'))
    assert r.status_code == 400 and "<script" not in r.text and "alert(1)" not in r.text


# --- typed value and body limits ----------------------------------------------------------------


class Spy:
    """Wraps ``address_keys`` and email-validator: counts calls, notes if one ran on the loop."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        self.on_loop = False
        keys, validate = unsubscribe_pages.address_keys, unsubscribes.validate_email

        def address_keys(addr: str) -> frozenset[str]:
            self._note()
            return keys(addr)

        def validate_email(*args: object, **kw: object) -> object:
            self._note()
            return validate(*args, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(unsubscribe_pages, "address_keys", address_keys)
        monkeypatch.setattr(unsubscribes, "validate_email", validate_email)

    def _note(self) -> None:
        self.calls += 1
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.on_loop = True


@pytest.mark.parametrize(
    "entered",
    [
        "ada" + "́" * 600 + "@example.com",  # 1,215 bytes, but well under the body limit
        "́" * 170,  # 170 characters (under maxlength=320) but 340 bytes
        "a" * 321,
    ],
)
def test_over_long_typed_value_is_refused_without_validating(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entered: str
) -> None:
    spy = Spy(monkeypatch)
    r = client.post(path_for(ADA), **typed(entered))
    assert r.status_code == 400 and MISMATCH in r.text
    assert spy.calls == 0  # refused on its length, before normalising or email-validator
    assert file_text(tmp_path) is None
    assert client.post(path_for(ADA), **typed(ADA)).status_code == 200  # the spy does see calls
    assert spy.calls > 0 and not spy.on_loop  # ...and they ran in a worker thread
    assert file_text(tmp_path) is not None


def test_typed_limit_is_320_bytes_spaces_included(client: TestClient, tmp_path: Path) -> None:
    longest = "a" * (unsubscribe_pages.MAX_TYPED_BYTES - len("@example.com")) + "@example.com"
    assert len(longest.encode()) == 320 == unsubscribe_pages.MAX_TYPED_BYTES
    path = path_for(longest)
    assert client.post(path, **typed(" " + longest)).status_code == 400  # 321 bytes
    assert file_text(tmp_path) is None
    assert client.post(path, **typed(longest)).status_code == 200
    assert (file_text(tmp_path) or "").startswith(longest + "\t")


def _form_body(size: int) -> bytes:
    """A url-encoded form typing ADA, padded to exactly ``size`` bytes."""
    head = b"email=ada%40example.com&pad="
    return head + b"x" * (size - len(head))


def _chunks(body: bytes) -> Iterator[bytes]:  # an iterator: sent chunked, no Content-Length
    for i in range(0, len(body), 1000):
        yield body[i : i + 1000]


@pytest.mark.parametrize("chunked", [False, True])
def test_form_body_limit(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chunked: bool
) -> None:
    limit = unsubscribe_pages.MAX_FORM_BYTES
    assert limit == 4096
    spy = Spy(monkeypatch)
    for body in (_form_body(limit + 1), _form_body(100_000)):
        content = _chunks(body) if chunked else body
        r = client.post(path_for(ADA), content=content, headers=FORM_TYPE)
        assert r.status_code == 400 and MISMATCH in r.text  # the right address, but too big
        assert r.headers["content-security-policy"] == UNSUBSCRIBE_CSP
    assert spy.calls == 0 and file_text(tmp_path) is None
    body = _form_body(limit)
    r = client.post(path_for(ADA), content=_chunks(body) if chunked else body, headers=FORM_TYPE)
    assert r.status_code == 200 and file_text(tmp_path) is not None


def _limited_body(headers: list[tuple[bytes, bytes]]) -> tuple[bytes | None, int]:
    """Run ``_limited_body`` on an endless body of 1,000-byte chunks; (result, chunks read)."""
    reads = 0

    async def receive() -> dict[str, object]:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x" * 1000, "more_body": True}

    scope = {"type": "http", "method": "POST", "path": "/u/x", "headers": headers}
    result = asyncio.run(unsubscribe_pages._limited_body(Request(scope, receive)))
    return result, reads


def test_form_body_is_not_buffered_past_the_limit() -> None:
    assert _limited_body([]) == (None, 5)  # stops at the chunk that passes 4,096 bytes
    for length in (b"4097", b"1" * 30):  # refused from Content-Length, nothing read
        assert _limited_body([(b"content-length", length)]) == (None, 0)


def test_one_click_never_reads_the_body(client: TestClient, tmp_path: Path) -> None:
    async def receive() -> dict[str, object]:
        raise AssertionError("the one-click route read the body")

    token = make_token(SECRET, ADA)
    path = f"/u/{token}/one-click"
    scope = {"type": "http", "method": "POST", "path": path, "headers": [], "app": client.app}
    r = unsubscribe_pages.one_click(Request(scope, receive), token)
    assert r.status_code == 200 and file_text(tmp_path) is not None
    big = client.post(one_click_for("bo@example.com"), content=b"x" * 1_000_000)
    assert big.status_code == 200 and "bo@example.com\t" in (file_text(tmp_path) or "")


@pytest.mark.parametrize("suffix", ["?utm=1", "#top", "?"])
def test_one_click_url_refuses_a_query_or_fragment(suffix: str) -> None:
    page = unsubscribe_url(BASE, SECRET, ADA)
    assert one_click_url(page) == page + "/one-click"
    with pytest.raises(ValueError):
        one_click_url(page + suffix)


# --- POST /u/<token>/one-click: RFC 8058, no body needed ------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {**ONE_CLICK, "headers": FORM_TYPE},
        {"files": {"List-Unsubscribe": (None, "One-Click")}},  # multipart/form-data
        {"content": b"", "headers": {"Content-Type": "text/plain"}},
        {},  # no body at all
        typed("someone-else@example.com"),  # a typed address is ignored here
    ],
)
def test_one_click_post(
    client: TestClient, tmp_path: Path, clock: FakeClock, kw: dict[str, object]
) -> None:
    client.cookies.clear()
    r = client.post(one_click_for(ADA), **kw)  # type: ignore[arg-type]
    assert r.status_code == 200 and "You're unsubscribed" in r.text
    assert file_text(tmp_path) == expected_line(clock)
    assert client.post(one_click_for(ADA), **kw).status_code == 200  # type: ignore[arg-type]
    assert file_text(tmp_path) == expected_line(clock)


def test_public_posts_ignore_origin(client: TestClient, tmp_path: Path) -> None:
    """Public POSTs aren't Origin-checked: mail providers send none, browsers send null here."""
    for n, origin in enumerate(("null", "https://evil.example")):
        r = client.post(one_click_for(f"p{n}@example.com"), headers={"Origin": origin})
        assert r.status_code == 200
        r = client.post(
            path_for(f"q{n}@example.com"), headers={"Origin": origin}, **typed(f"q{n}@example.com")
        )
        assert r.status_code == 200
    text = file_text(tmp_path) or ""
    for addr in ("p0", "p1", "q0", "q1"):
        assert f"{addr}@example.com\t" in text


def test_variant_address_already_present(client: TestClient, tmp_path: Path) -> None:
    existing = "ADA@Example.COM\t2026-11-01T09:00:00-05:00\njose@xn--bcher-kva.example\n"
    (tmp_path / "unsubscribed.txt").write_text(existing, encoding="utf-8")
    assert client.post(one_click_for(ADA)).status_code == 200
    assert (
        client.post(path_for("jose@bücher.example"), **typed("jose@bücher.example")).status_code
        == 200
    )
    assert file_text(tmp_path) == existing


def test_bom_file_first_address_is_already_present(client: TestClient, tmp_path: Path) -> None:
    existing = "﻿ada@example.com\t2026-11-01T09:00:00-05:00\n"
    (tmp_path / "unsubscribed.txt").write_text(existing, encoding="utf-8")
    assert client.post(one_click_for(ADA)).status_code == 200
    assert file_text(tmp_path) == existing


@pytest.mark.parametrize("form", [False, True])
def test_write_failure_is_a_503_not_a_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, form: bool
) -> None:
    def broken(*args: object) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(unsubscribe_pages, "record_unsubscribe", broken)
    r = client.post(path_for(ADA), **typed(ADA)) if form else client.post(one_click_for(ADA))
    assert r.status_code == 503
    assert r.headers["retry-after"] == unsubscribe_pages.WRITE_RETRY_AFTER
    assert "unsubscribed" not in r.text.lower() and "try again" in r.text
    assert r.headers["content-security-policy"] == UNSUBSCRIBE_CSP


@pytest.mark.parametrize("form", [False, True])
def test_file_locked_too_long_is_a_503(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: bool
) -> None:
    with LockHeld(tmp_path, monkeypatch) as held:
        r = client.post(path_for(ADA), **typed(ADA)) if form else client.post(one_click_for(ADA))
    assert r.status_code == 503 and "try again" in r.text
    assert r.headers["retry-after"] == unsubscribe_pages.WRITE_RETRY_AFTER
    assert r.headers["content-security-policy"] == UNSUBSCRIBE_CSP
    assert held.now >= unsubscribes.LOCK_WAIT_SECONDS and held.sleeps > 1
    assert file_text(tmp_path) == ""  # nothing recorded
    # Once the lock is free, the same request records.
    r = client.post(path_for(ADA), **typed(ADA)) if form else client.post(one_click_for(ADA))
    assert r.status_code == 200 and ADA in (file_text(tmp_path) or "")


def test_send_worker_sends_nothing_while_the_file_is_locked(
    client: TestClient, tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The send-time check fails safe: a file it can't read stops the step before any send."""
    draft = ready_draft(client)
    start(client, draft)
    worker = client.app.state.worker  # type: ignore[attr-defined]
    with LockHeld(tmp_path, monkeypatch):
        for _ in range(3):
            with pytest.raises(unsubscribes.LockTimeout):
                asyncio.run(worker.step())
            assert outbox(tmp_path) == []
    step_all(client, clock)  # readable again: the send goes on
    assert outbox(tmp_path)


# --- bad tokens --------------------------------------------------------------------------------


def _tampered() -> list[str]:
    good = make_token(SECRET, ADA)
    email_part, mac = good.split(".")
    other = base64.urlsafe_b64encode(b"eve@example.com").rstrip(b"=").decode()
    flipped = mac[:-2] + ("A" if mac[-2] != "A" else "B") + mac[-1]
    return [
        f"{other}.{mac}",  # someone else's address with Ada's MAC
        f"{email_part}.{flipped}",
        make_token("y" * 32, ADA),  # another secret
        good + "x",
        email_part,
        "garbage",
        "a" * 600,
    ]


@pytest.mark.parametrize("token", _tampered())
def test_bad_token_reveals_nothing(client: TestClient, tmp_path: Path, token: str) -> None:
    generic = client.get("/u/nothing-like-a-token").text
    for method, suffix, kw in [
        ("GET", "", {}),
        ("POST", "", typed(ADA)),
        ("POST", "", typed("eve@example.com")),
        ("GET", "/one-click", {}),
        ("POST", "/one-click", ONE_CLICK),
    ]:
        r = client.request(method, "/u/" + token + suffix, **kw)  # type: ignore[arg-type]
        assert r.status_code == 404
        assert r.text == generic  # the same page whatever is wrong
        assert "ada" not in r.text.lower() and "eve" not in r.text.lower()
        assert "<form" not in r.text and "isn't valid" in r.text
    assert file_text(tmp_path) is None


# --- headers -----------------------------------------------------------------------------------


def _pages(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list:
    good = path_for(ADA)
    pages = [
        client.get(good),
        client.get(good + "/one-click"),
        client.post(good, **typed("eve@example.com")),
        client.post(good, **typed(ADA)),
        client.post(one_click_for("bob@example.com")),
        client.get("/u/bad.token"),
        client.post("/u/bad.token/one-click"),
    ]
    with LockHeld(tmp_path, monkeypatch):
        pages.append(client.post(one_click_for("cy@example.com")))
    client.app.state.unsubscribe_limiter = RateLimiter(  # type: ignore[attr-defined]
        capacity=1, refill_per_second=1, clock=FakeMonotonic()
    )
    client.get(good)
    pages.append(client.get(good))
    return pages


def test_public_pages_headers_and_markup(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected_hash = base64.b64encode(hashlib.sha256(PAGE_CSS.encode()).digest()).decode()
    statuses = []
    for r in _pages(client, tmp_path, monkeypatch):
        statuses.append(r.status_code)
        h = r.headers
        assert h["content-security-policy"] == UNSUBSCRIBE_CSP
        assert f"style-src 'sha256-{expected_hash}'" in UNSUBSCRIBE_CSP
        for part in (
            "default-src 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
        ):
            assert part in UNSUBSCRIBE_CSP
        assert h["cache-control"] == "no-store"
        assert h["referrer-policy"] == "no-referrer"
        assert h["x-frame-options"] == "DENY"
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-robots-tag"] == "noindex, nofollow"
        assert "set-cookie" not in h  # no admin CSRF cookie on public pages
        styles = STYLE_RE.findall(r.text)
        assert styles == [PAGE_CSS]  # exactly the hashed block
        assert "style=" not in r.text and "<script" not in r.text
        assert "/static/" not in r.text and "<img" not in r.text
        assert "<nav" not in r.text and "outlook-mailer" not in r.text
    assert statuses == [200, 200, 400, 200, 200, 404, 404, 503, 429]


# --- rate limit --------------------------------------------------------------------------------


@pytest.fixture
def fake_mono(client: TestClient) -> FakeMonotonic:
    mono = FakeMonotonic()
    client.app.state.unsubscribe_limiter = RateLimiter(  # type: ignore[attr-defined]
        capacity=3, refill_per_second=0.5, clock=mono
    )
    client.app.state.unsubscribe_address_limiter = RateLimiter(  # type: ignore[attr-defined]
        capacity=2, refill_per_second=1 / 60, clock=mono
    )
    return mono


def test_rate_limit_per_cf_connecting_ip(
    client: TestClient, tmp_path: Path, fake_mono: FakeMonotonic
) -> None:
    ip = {"CF-Connecting-IP": "203.0.113.7"}
    path = path_for(ADA)
    assert [client.get(path, headers=ip).status_code for _ in range(3)] == [200] * 3
    r = client.get(path, headers=ip)
    assert r.status_code == 429 and r.headers["retry-after"] == "2"
    assert "Too many requests" in r.text
    # Bad tokens count too (guessing costs requests), and so do mistyped addresses.
    assert client.get("/u/bad.token", headers=ip).status_code == 429
    assert client.post("/u/bad.token/one-click", headers=ip).status_code == 429
    assert client.post(path, headers=ip, **typed("eve@example.com")).status_code == 429
    assert file_text(tmp_path) is None
    # Another visitor isn't affected.
    assert client.get(path, headers={"CF-Connecting-IP": "203.0.113.8"}).status_code == 200
    # A valid opt-out from the exhausted IP is never refused: one-click and the typed form.
    assert client.post(one_click_for(ADA), headers=ip).status_code == 200
    assert (
        client.post(path_for("bob@example.com"), headers=ip, **typed("BOB@example.com")).status_code
        == 200
    )
    text = file_text(tmp_path) or ""
    assert f"{ADA}\t" in text and "bob@example.com\t" in text
    fake_mono.now += 2
    assert client.get(path, headers=ip).status_code == 200


def test_many_one_click_posts_from_one_provider_all_record(
    client: TestClient, tmp_path: Path
) -> None:
    """Gmail/Outlook.com send one-click POSTs from their own servers: dozens of people's opt-outs
    from one IP (or one IPv6 /64) must all be recorded, with the default limits."""
    v4 = {"CF-Connecting-IP": "209.85.220.41"}
    for i in range(30):
        r = client.post(one_click_for(f"v4-{i}@example.com"), headers=v4, **ONE_CLICK)
        assert r.status_code == 200, i
    for i in range(30):
        v6 = {"CF-Connecting-IP": f"2001:db8:4860:4864::{i + 1:x}"}  # all in one /64
        r = client.post(one_click_for(f"v6-{i}@example.com"), headers=v6, **ONE_CLICK)
        assert r.status_code == 200, i
    text = file_text(tmp_path) or ""
    assert len(text.splitlines()) == 60
    for i in range(30):
        assert f"v4-{i}@example.com\t" in text and f"v6-{i}@example.com\t" in text


def test_get_exhausted_ip_still_records_a_valid_post(client: TestClient, tmp_path: Path) -> None:
    ip = {"CF-Connecting-IP": "203.0.113.9"}
    path = path_for(ADA)
    codes = [
        client.get(path, headers=ip).status_code for _ in range(unsubscribe_pages.RATE_CAPACITY + 1)
    ]
    assert codes[-1] == 429
    assert client.post(path, headers=ip, **typed(ADA)).status_code == 200
    assert client.post(one_click_for("bob@example.com"), headers=ip).status_code == 200
    text = file_text(tmp_path) or ""
    assert f"{ADA}\t" in text and "bob@example.com\t" in text


def test_mismatches_are_limited_per_address(
    client: TestClient, tmp_path: Path, fake_mono: FakeMonotonic
) -> None:
    path = path_for(ADA)
    codes = [
        client.post(
            path, headers={"CF-Connecting-IP": f"198.51.100.{i}"}, **typed(f"guess{i}@example.com")
        ).status_code
        for i in range(4)
    ]
    assert codes == [400, 400, 429, 429]  # from different IPs: the address's bucket ran out
    r = client.post(path, headers={"CF-Connecting-IP": "198.51.100.99"}, **typed("x@example.com"))
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    # Another address isn't affected, and the right address still records.
    assert client.post(path_for("bob@example.com"), **typed("eve@example.com")).status_code == 400
    assert client.post(path, **typed(ADA)).status_code == 200
    assert (file_text(tmp_path) or "").startswith(f"{ADA}\t")


def test_repeats_are_limited_per_address_but_never_the_first(
    client: TestClient, tmp_path: Path, fake_mono: FakeMonotonic
) -> None:
    ip = {"CF-Connecting-IP": "203.0.113.7"}
    # Mismatches don't use up the address's opt-out allowance.
    for _ in range(3):
        client.post(path_for(ADA), headers=ip, **typed("eve@example.com"))
    codes = [client.post(one_click_for(ADA)).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]  # recorded, then 2 repeats allowed
    assert len((file_text(tmp_path) or "").splitlines()) == 1
    assert client.post(one_click_for("bob@example.com")).status_code == 200  # others unaffected
    fake_mono.now += 60
    assert client.post(one_click_for(ADA)).status_code == 200


def test_spoofed_x_forwarded_for_does_not_reset_the_limit(
    client: TestClient, fake_mono: FakeMonotonic
) -> None:
    path = path_for(ADA)
    ip = "203.0.113.7"
    codes = [
        client.get(
            path, headers={"CF-Connecting-IP": ip, "X-Forwarded-For": f"10.0.0.{i}"}
        ).status_code
        for i in range(5)
    ]
    assert codes == [200, 200, 200, 429, 429]
    # Without CF-Connecting-IP, requests carrying X-Forwarded-For share one bucket.
    codes = [
        client.get(path, headers={"X-Forwarded-For": f"10.0.1.{i}"}).status_code for i in range(5)
    ]
    assert codes == [200, 200, 200, 429, 429]


def test_default_limiters_are_bounded(client: TestClient) -> None:
    rl = client.app.state.unsubscribe_limiter  # type: ignore[attr-defined]
    assert rl.max_clients == unsubscribe_pages.RATE_MAX_CLIENTS
    assert rl.capacity >= 4  # room for a few page views and mistypes
    al = client.app.state.unsubscribe_address_limiter  # type: ignore[attr-defined]
    assert al.max_clients == unsubscribe_pages.ADDRESS_MAX_KEYS and al.capacity >= 1


def test_missing_cf_connecting_ip_is_logged_once(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="outlook_mailer.unsubscribe")
    ip = {"CF-Connecting-IP": "203.0.113.7"}
    client.get(path_for(ADA), headers=ip)
    client.post(one_click_for(ADA), headers=ip)
    assert "CF-Connecting-IP" not in caplog.text
    client.get(path_for(ADA))
    client.post(one_click_for(ADA))
    client.get("/u/bad.token")
    warnings = [r for r in caplog.records if "CF-Connecting-IP" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert "Remove visitor IP headers" in warnings[0].getMessage()


def test_missing_ip_warning_repeats_after_an_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    mono = FakeMonotonic()
    warning = unsubscribe_pages.MissingIpWarning(clock=mono)
    logged: list[float] = []
    monkeypatch.setattr(unsubscribe_pages.log, "warning", lambda *a, **k: logged.append(mono.now))
    for step in (0, 10, unsubscribe_pages.MISSING_IP_WARN_SECONDS, 1):
        mono.now += step
        warning.check(request({}))
    warning.check(request({"CF-Connecting-IP": "203.0.113.7"}))
    assert logged == [1000.0, 1000.0 + 10 + unsubscribe_pages.MISSING_IP_WARN_SECONDS]


# --- access ------------------------------------------------------------------------------------


@pytest.fixture
def auth_client(valid_env: dict[str, str], tmp_path: Path) -> Iterator[TestClient]:
    settings = load_settings(
        {
            **valid_env,
            "DATA_DIR": str(tmp_path),
            "AUTH_DISABLED": "0",
            "CF_ACCESS_TEAM_DOMAIN": TEAM,
            "CF_ACCESS_AUD": AUD,
        }
    )
    with TestClient(create_app(settings, run_worker=False), base_url=BASE) as c:
        yield c


def test_public_routes_need_no_access_jwt(auth_client: TestClient, tmp_path: Path) -> None:
    path = path_for(ADA)
    assert auth_client.get(path).status_code == 200
    assert auth_client.get(path + "/one-click").status_code == 200
    assert auth_client.post(path, **typed("eve@example.com")).status_code == 400
    assert auth_client.post(path, **typed(ADA)).status_code == 200
    bob = one_click_for("bob@example.com")
    assert auth_client.post(bob, **ONE_CLICK, headers=FORM_TYPE).status_code == 200
    text = file_text(tmp_path) or ""
    assert f"{ADA}\t" in text and "bob@example.com\t" in text


@pytest.mark.parametrize("path", ["/unsubscribes", "/unsubscribes/unsubscribed.txt"])
def test_admin_unsubscribes_needs_access(
    auth_client: TestClient, tmp_path: Path, path: str
) -> None:
    (tmp_path / "unsubscribed.txt").write_text(
        f"{ADA}\t2026-12-01T10:00:00-05:00\n", encoding="utf-8"
    )
    r = auth_client.get(path)
    assert r.status_code == 403
    assert ADA not in r.text


# --- 30+ days later, and the links in a real email ---------------------------------------------


def test_link_from_a_sent_email_works_after_the_send_is_gone(
    client: TestClient, tmp_path: Path, clock: FakeClock, app_settings: Settings
) -> None:
    draft = ready_draft(client)
    cid = start(client, draft)
    step_all(client, clock)
    assert campaigns.get_campaign(client.app.state.db, cid).status == campaigns.FINISHED  # type: ignore[attr-defined,union-attr]
    msg = next(m for m in outbox(tmp_path) if ADA in str(m["To"]))
    header = str(msg["List-Unsubscribe"])
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    one_click = header.strip("<>")
    assert one_click.startswith(BASE + "/u/") and one_click.endswith("/one-click")
    page = one_click.removesuffix("/one-click")
    plain = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    assert page in plain and f'href="{page}"' in html  # the body links to the page...
    assert one_click not in plain and one_click not in html  # ...never to one-click

    # 45 days later, in a fresh app with an empty database: tokens don't expire and don't depend
    # on campaign or draft data.
    clock.advance(45 * 86400)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    settings = load_settings(
        {
            "PUBLIC_BASE_URL": BASE,
            "UNSUBSCRIBE_SECRET": SECRET,
            "MAIL_TRANSPORT": "mock",
            "AUTH_DISABLED": "1",
            "DATA_DIR": str(fresh),
        }
    )
    with TestClient(create_app(settings, clock=clock, run_worker=False), base_url=BASE) as c:
        assert c.get(urlsplit(page).path).status_code == 200
        assert c.get(urlsplit(one_click).path).status_code == 200
        assert file_text(fresh) is None
        r = c.post(urlsplit(one_click).path, **ONE_CLICK, headers=FORM_TYPE)  # what Gmail sends
        assert r.status_code == 200
    assert file_text(fresh) == expected_line(clock)


def test_unsubscribe_during_a_send_is_skipped(
    client: TestClient, tmp_path: Path, clock: FakeClock
) -> None:
    draft = ready_draft(client)
    cid = start(client, draft)
    worker = client.app.state.worker  # type: ignore[attr-defined]
    assert asyncio.run(worker.step()) is not None  # starts; Ada is sent first
    while not any(ADA in str(m["To"]) for m in outbox(tmp_path)):
        clock.advance(asyncio.run(worker.step()) or 0)
    assert client.post(one_click_for("bob@example.com")).status_code == 200
    step_all(client, clock)
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.connect() as conn:
        rows = {
            r["email"]: r["status"]
            for r in conn.execute(
                "SELECT email, status FROM campaign_recipients WHERE campaign_id = ?", (cid,)
            )
        }
    assert rows["bob@example.com"] == campaigns.R_SKIPPED
    assert rows[ADA] == campaigns.R_SENT
    assert not any("bob@example.com" in str(m["To"]) for m in outbox(tmp_path))


def test_unsubscribe_urls_match_the_routes(client: TestClient, tmp_path: Path) -> None:
    url = unsubscribe_url(BASE + "/", SECRET, "Cy@Example.com")
    assert client.post(urlsplit(url).path, **typed("cy@example.com")).status_code == 200
    assert (file_text(tmp_path) or "").startswith("cy@example.com\t")
    url = one_click_url(unsubscribe_url(BASE, SECRET, "Dee@Example.com"))
    assert client.post(urlsplit(url).path, **ONE_CLICK, headers=FORM_TYPE).status_code == 200
    assert "dee@example.com\t" in (file_text(tmp_path) or "")


# --- admin page ---------------------------------------------------------------------------------


def test_admin_page_empty(client: TestClient) -> None:
    r = client.get("/unsubscribes")
    assert r.status_code == 200
    assert "Nobody has unsubscribed yet." in r.text
    assert 'href="/unsubscribes/unsubscribed.txt"' in r.text
    d = client.get("/unsubscribes/unsubscribed.txt")
    assert d.status_code == 200 and d.content == b""
    assert d.headers["content-type"] == "text/plain; charset=utf-8"
    assert d.headers["content-disposition"] == 'attachment; filename="unsubscribed.txt"'


def test_admin_page_lists_entries(client: TestClient, tmp_path: Path) -> None:
    raw = (
        "﻿ann@example.com\t2026-12-01T10:00:00-05:00\n"
        "<b>x</b>@example.com\tby hand\n"
        "junk line\n"
        "bo@example.com\t2026-12-02T15:30:00+00:00"
    )
    (tmp_path / "unsubscribed.txt").write_text(raw, encoding="utf-8")
    page = client.get("/unsubscribes").text
    assert '<p id="unsub-count"><strong>3</strong> addresses, newest first.</p>' in page
    assert page.index("bo@example.com") < page.index("ann@example.com")  # newest first
    assert "﻿" not in page  # the BOM isn't part of the first address
    assert "2026-12-02 10:30:00 EST" in page  # shown in TZ
    assert "2026-12-01 10:00:00 EST" in page
    assert "&lt;b&gt;x&lt;/b&gt;@example.com" in page and "<b>x</b>" not in page
    assert "by hand" in page and "junk line" not in page
    assert 'aria-current="page">Unsubscribes' in page
    d = client.get("/unsubscribes/unsubscribed.txt")
    assert d.content == raw.encode()  # the download is the file as it is


def test_admin_page_counts_addresses_not_lines(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "unsubscribed.txt").write_text(
        "ann@example.com\t2026-12-01T10:00:00-05:00\n"
        "ANN@Example.com\tby hand\n"
        "jose@bücher.example\n"
        "jose@xn--bcher-kva.example\n"
        "bo@example.com\n",
        encoding="utf-8",
    )
    page = client.get("/unsubscribes").text
    assert "<strong>3</strong> addresses, newest first. The file has 5 entries:" in page
    assert page.count("<tr><td>") == 5  # every entry is still listed


def test_admin_page_after_a_real_unsubscribe(client: TestClient, clock: FakeClock) -> None:
    client.post(one_click_for(ADA))
    page = client.get("/unsubscribes").text
    assert "<strong>1</strong> address, newest first.</p>" in page and ADA in page
    assert campaigns.local_time(clock(), campaigns.local_zone("America/New_York")) in page
