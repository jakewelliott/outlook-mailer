"""Microsoft Graph transport: request shape, every response/exception mapping, time budget, and
the worker in graph mode. Graph is faked with respx and the sign-in with ``FakeAuth``: no network.
"""

import asyncio
import base64
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app import campaigns, drafts, gate, msauth, sender
from app.compose import RenderedEmail
from app.config import Settings, load_settings
from app.db import Database
from app.emails import normalise_key
from app.message import MessageError, build_mime
from app.msauth import (
    STATE_SIGNED_IN,
    STATE_SIGNED_OUT,
    AccountChanged,
    MicrosoftAuth,
    NotSignedIn,
    ReauthRequired,
    Sender,
    Status,
    TokenUnavailable,
)
from app.recipients import Recipient
from app.sender import SendWorker
from app.transport import GraphTransport, SendResult, make_transport
from app.transport import graph as graph_mod
from app.transport.graph import SEND_MAIL_URL, parse_retry_after
from tests.conftest import VALID_ENV
from tests.test_sender import CONTENT, FakeClock

BASE = "https://mail.example.com"
TOKEN = "eyJ0eXAi.GRAPH-ACCESS-TOKEN-secret.sig"
ME = Sender("Jake@outlook.com", "Jake Elliott")
GRAPH_ENV = {
    **VALID_ENV,
    "MAIL_TRANSPORT": "graph",
    "MS_CLIENT_ID": "00001111-aaaa-2222-bbbb-3333cccc4444",
}


class FakeAuth(MicrosoftAuth):
    """The parts of ``MicrosoftAuth`` the transport, worker and pages use, without MSAL.

    ``token``: a string to return, or an exception to raise from ``get_access_token``.
    ``on_token``: called (in the token thread) at the start of each ``get_access_token``."""

    def __init__(self, settings: Settings, db: Database, *, account: Sender | None = ME) -> None:
        super().__init__(settings, db)
        self.account = account
        self.token: str | BaseException = TOKEN
        self.calls = 0
        self.rejected = 0
        self.accepted = 0
        self.on_token: Callable[[], object] | None = None
        self.expected: list[str | None] = []

    def load(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def sender(self) -> Sender | None:
        return self.account

    def status(self) -> Status:
        if self.account is None:
            return Status(STATE_SIGNED_OUT)
        return Status(STATE_SIGNED_IN, sender=self.account, reauth=self.rejected > 0)

    def get_access_token(self, expected_address: str | None = None) -> str:
        self.calls += 1
        self.expected.append(expected_address)
        if self.on_token is not None:
            self.on_token()
        if self.account is None:
            raise NotSignedIn(msauth.NO_ACCOUNT)
        if expected_address is not None and normalise_key(self.account.address) != normalise_key(
            expected_address
        ):
            raise AccountChanged(expected_address, self.account.address)
        if isinstance(self.token, BaseException):
            raise self.token
        return self.token

    def note_token_rejected(self) -> None:
        self.rejected += 1

    def note_token_accepted(self) -> None:
        self.accepted += 1
        self.rejected = 0


def graph_settings(tmp_path: Path, **env: str) -> Settings:
    return load_settings({**GRAPH_ENV, "DATA_DIR": str(tmp_path), **env})


def fake_auth(tmp_path: Path, account: Sender | None = ME) -> FakeAuth:
    db = Database(tmp_path / "app.db")
    db.init()
    return FakeAuth(graph_settings(tmp_path), db, account=account)


def mime(to: str = "ada@example.com") -> bytes:
    r = RenderedEmail(
        subject="Merry Christmas", html="<p>Hi</p>", text="Hi", unsubscribe_url=f"{BASE}/u/t"
    )
    return build_mime(r, to_addr=to, to_name="Ada", from_addr=ME.address, from_name="Barn")


def send(t: GraphTransport, raw: bytes | None = None, to: str = "ada@example.com") -> SendResult:
    async def go() -> SendResult:
        try:
            return await t.send(raw if raw is not None else mime(to), recipient=to)
        finally:
            await t.aclose()

    return asyncio.run(go())


@pytest.fixture
def graph() -> Any:
    with respx.mock(assert_all_called=False) as router:
        yield router.post(SEND_MAIL_URL)


@pytest.fixture
def auth(tmp_path: Path) -> FakeAuth:
    return fake_auth(tmp_path)


def transport(auth: FakeAuth, **kw: Any) -> GraphTransport:
    return GraphTransport(auth, **kw)


def error(status: int, code: str | None, **headers: str) -> httpx.Response:
    body = {"error": {"code": code, "message": f"secret detail {TOKEN}"}} if code else None
    return httpx.Response(status, json=body, headers=headers)


# --- the request -------------------------------------------------------------------------------


def test_request_shape(auth: FakeAuth, graph: respx.Route) -> None:
    graph.mock(return_value=httpx.Response(202, headers={"request-id": "req-123"}))
    raw = mime()
    result = send(transport(auth), raw)
    assert result == SendResult.success("req-123")
    request = graph.calls.last.request
    assert request.method == "POST" and str(request.url) == SEND_MAIL_URL
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["content-type"] == "text/plain"
    assert base64.b64decode(request.content) == raw
    assert auth.calls == 1


def test_client_request_id_used_when_no_request_id(auth: FakeAuth, graph: respx.Route) -> None:
    graph.mock(return_value=httpx.Response(202, headers={"client-request-id": "c-1"}))
    assert send(transport(auth)).provider_id == "c-1"
    graph.mock(return_value=httpx.Response(202))
    assert send(transport(auth)) == SendResult.success(None)


def test_wrong_recipient_refused_before_anything(auth: FakeAuth, graph: respx.Route) -> None:
    with pytest.raises(MessageError):
        send(transport(auth), mime("ada@example.com"), to="bob@example.com")
    assert not graph.called and auth.calls == 0


def test_too_large_refused(
    auth: FakeAuth, graph: respx.Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.message.GRAPH_MAX_REQUEST_BYTES", 10)
    with pytest.raises(MessageError, match="too large"):
        send(transport(auth))
    assert not graph.called


class Ticks:
    """A fake monotonic clock for ``GraphTransport(monotonic=...)``: time passes only when told."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_token_fetched_off_the_event_loop(auth: FakeAuth, graph: respx.Route) -> None:
    """The token call blocks until another task on the loop releases it: that can only happen
    if the loop keeps running while the token is fetched (no timing involved)."""
    graph.mock(return_value=httpx.Response(202))
    entered, release = threading.Event(), threading.Event()
    released: list[bool] = []

    def blocking() -> None:
        entered.set()
        released.append(release.wait(30))  # on the loop this would wait the full 30 s

    auth.on_token = blocking
    t = transport(auth)

    async def go() -> tuple[SendResult, bool]:
        await t.open()
        ran = asyncio.Event()

        async def other_task() -> None:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            ran.set()  # runs while the token call is blocked
            release.set()

        try:
            result, _ = await asyncio.gather(
                t.send(mime(), recipient="ada@example.com"), other_task()
            )
        finally:
            release.set()
            await t.aclose()
        return result, ran.is_set()

    result, ran = asyncio.run(go())
    assert ran and released == [True] and result.ok


def test_shared_client_opened_and_closed(auth: FakeAuth, graph: respx.Route) -> None:
    graph.mock(return_value=httpx.Response(202))
    t = transport(auth)

    async def go() -> None:
        await t.open()
        client = t._client
        assert client is not None
        await t.send(mime(), recipient="ada@example.com")
        await t.send(mime(), recipient="ada@example.com")
        assert t._client is client  # one client for every send
        await t.aclose()
        assert client.is_closed and t._client is None
        await t.aclose()  # twice is fine

    asyncio.run(go())


def test_given_client_is_not_closed(auth: FakeAuth, graph: respx.Route) -> None:
    graph.mock(return_value=httpx.Response(202))

    async def go() -> None:
        client = httpx.AsyncClient()
        t = transport(auth, client=client)
        await t.send(mime(), recipient="ada@example.com")
        await t.aclose()
        assert not client.is_closed
        await client.aclose()

    asyncio.run(go())


# --- token problems ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (NotSignedIn(msauth.NO_ACCOUNT), "auth"),
        (ReauthRequired("The Microsoft sign-in has expired."), "auth"),
        (TokenUnavailable("Couldn't reach Microsoft to renew the sign-in."), "retry"),
        (RuntimeError("msal bug"), "retry"),
    ],
)
def test_token_errors(auth: FakeAuth, graph: respx.Route, exc: Exception, kind: str) -> None:
    auth.token = exc
    result = send(transport(auth))
    assert not graph.called  # no request without a token
    if kind == "auth":
        assert result.auth_error and str(exc) in (result.error or "")
        assert result.signed_out == isinstance(exc, NotSignedIn)  # fix C3
    else:
        assert result.retryable and not result.throttled


def test_budget_running_out_during_the_token_step(auth: FakeAuth, graph: respx.Route) -> None:
    """The token call never returns by itself: the send gives up on its own (retry, nothing
    sent) while the token thread is still blocked, which is then released."""
    release = threading.Event()
    auth.on_token = lambda: release.wait(30)
    t = transport(auth, budget=0.01)

    async def go() -> tuple[SendResult, bool]:
        await t.open()
        try:
            result = await t.send(mime(), recipient="ada@example.com")
            still_blocked = not release.is_set()
        finally:
            release.set()  # let the abandoned token thread finish
            await t.aclose()
        return result, still_blocked

    result, still_blocked = asyncio.run(go())
    assert still_blocked  # the send didn't wait for the token
    assert result.retryable and not result.throttled and not graph.called
    assert "took too long" in (result.error or "")


@pytest.mark.parametrize(("spent", "sends"), [(9.5, False), (9.0, True)])
def test_too_little_budget_left_after_token(
    auth: FakeAuth, graph: respx.Route, spent: float, sends: bool
) -> None:
    """With a 12 s budget and ``MIN_REQUEST_SECONDS`` = 3, a token step that took 9.5 s (on the
    fake clock) leaves too little for a request; 9 s leaves exactly enough."""
    graph.mock(return_value=httpx.Response(202))
    ticks = Ticks()
    auth.on_token = lambda: ticks.advance(spent)
    assert graph_mod.SEND_BUDGET_SECONDS - graph_mod.MIN_REQUEST_SECONDS == 9.0
    result = send(transport(auth, monotonic=ticks))
    if sends:
        assert result.ok and graph.call_count == 1
    else:
        assert result.retryable and not graph.called


def test_request_timeouts_shortened_to_the_budget_left(auth: FakeAuth, graph: respx.Route) -> None:
    graph.mock(return_value=httpx.Response(202))
    ticks = Ticks()
    auth.on_token = lambda: ticks.advance(4.0)  # 8 s of the 12 s budget left
    assert send(transport(auth, monotonic=ticks)).ok
    timeout = graph.calls.last.request.extensions["timeout"]
    assert timeout == {"connect": 5.0, "read": 8.0, "write": 8.0, "pool": 2.0}


OTHER = Sender("other@outlook.com", "Other")


def send_as(t: GraphTransport, expected: str | None) -> SendResult:
    async def go() -> SendResult:
        try:
            return await t.send(mime(), recipient="ada@example.com", expected_sender=expected)
        finally:
            await t.aclose()

    return asyncio.run(go())


def test_expected_sender_checked_with_the_token(auth: FakeAuth, graph: respx.Route) -> None:
    graph.mock(return_value=httpx.Response(202))
    assert send_as(transport(auth), "jake@OUTLOOK.com").ok  # same mailbox, any case
    assert auth.expected == ["jake@OUTLOOK.com"]
    assert send_as(transport(auth), None).ok  # no expectation: whoever is connected
    assert graph.call_count == 2

    auth.account = OTHER
    result = send_as(transport(auth), ME.address)
    assert result.auth_error and "account changed" in (result.error or "")
    assert result.sender_changed == (ME.address, OTHER.address)
    assert graph.call_count == 2  # nothing sent


# --- HTTP answers ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "kind", "fragment"),
    [
        (httpx.Response(202), "ok", None),
        (httpx.Response(200), "ok", None),
        (error(401, "InvalidAuthenticationToken"), "auth", "InvalidAuthenticationToken"),
        (httpx.Response(401), "auth", "HTTP 401"),
        (error(403, "ErrorAccessDenied"), "auth", "Mail.Send"),
        (error(403, "ErrorMessageSubmissionBlocked"), "auth", "blocking this Outlook account"),
        (error(403, "ErrorSendAsDenied"), "auth", "sender address"),
        (error(403, "SendAsDenied"), "auth", "sender address"),
        (error(403, "ErrorQuotaExceeded"), "auth", "limit"),
        (error(403, "SomethingNew"), "auth", "SomethingNew"),
        (httpx.Response(403), "auth", "HTTP 403"),
        (error(404, "MailboxNotEnabledForRESTAPI"), "auth", "no mailbox"),
        (error(400, "ErrorMessageSubmissionBlocked"), "auth", "blocking"),
        (error(429, "ErrorExceededMessageLimit"), "auth", "limit"),
        (error(503, "ErrorExceededMessageLimit"), "auth", "limit"),
        (error(509, "ErrorQuotaExceeded"), "auth", "limit"),
        (error(500, "ErrorMessageSubmissionBlocked"), "unknown", "HTTP 500"),
        (error(502, "ErrorAccessDenied"), "unknown", "HTTP 502"),
        (error(504, "ErrorQuotaExceeded"), "unknown", "HTTP 504"),
        (error(400, "ErrorMimeContentInvalidBase64String"), "permanent", "InvalidBase64"),
        (error(400, "ErrorInvalidRecipients"), "permanent", "ErrorInvalidRecipients"),
        (httpx.Response(413), "permanent", "HTTP 413"),
        (error(404, "ResourceNotFound"), "permanent", "ResourceNotFound"),
        (error(500, "InternalServerError"), "unknown", "HTTP 500"),
        (httpx.Response(502), "unknown", "HTTP 502"),
        (error(504, "GatewayTimeout"), "unknown", "HTTP 504"),
        (httpx.Response(507), "unknown", "HTTP 507"),
        (httpx.Response(302, headers={"location": "https://example.com/"}), "unknown", "302"),
    ],
)
def test_status_mapping(
    auth: FakeAuth,
    graph: respx.Route,
    response: httpx.Response,
    kind: str,
    fragment: str | None,
) -> None:
    graph.mock(return_value=response)
    result = send(transport(auth))
    flags = {
        "ok": result.ok,
        "auth": result.auth_error,
        "permanent": not result.ok
        and not (result.retryable or result.auth_error or result.outcome_unknown),
        "unknown": result.outcome_unknown,
    }
    assert flags[kind], result
    if fragment:
        assert fragment in (result.error or "")
    assert "secret detail" not in (result.error or "")  # Graph's message is never kept
    assert TOKEN not in (result.error or "")


def test_401_marks_reconnect_needed(auth: FakeAuth, graph: respx.Route) -> None:
    graph.mock(return_value=error(401, "InvalidAuthenticationToken"))
    send(transport(auth))
    assert auth.rejected == 1
    graph.mock(return_value=error(403, "ErrorAccessDenied"))
    send(transport(auth))
    assert auth.rejected == 1  # only a rejected token, not a refusal
    assert auth.accepted == 0
    graph.mock(return_value=httpx.Response(202))
    assert send(transport(auth)).ok
    assert auth.accepted == 1 and auth.rejected == 0  # an accepted send clears it


@pytest.mark.parametrize("status", [500, 502, 504, 507, 302])
@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        ("ErrorMessageSubmissionBlocked", "blocking this Outlook account"),
        ("ErrorAccessDenied", "Mail.Send"),
        ("ErrorQuotaExceeded", "limit"),
        ("MailboxNotEnabledForRESTAPI", "no mailbox"),
    ],
)
def test_server_error_with_account_code_is_unknown_and_stops(
    auth: FakeAuth, graph: respx.Route, status: int, code: str, fragment: str
) -> None:
    """A 5xx (or 3xx) may come after Microsoft took the message: never auth (which resends on
    resume), always unknown, with the account problem as the reason to stop."""
    graph.mock(return_value=error(status, code, **{"Retry-After": "30"}))
    result = send(transport(auth))
    assert result.outcome_unknown and not result.auth_error and not result.retryable
    assert result.retry_after is None
    assert result.error == f"Microsoft answered HTTP {status} {code}"
    assert result.stop_reason and fragment in result.stop_reason and code in result.stop_reason
    assert "secret detail" not in result.stop_reason and TOKEN not in result.stop_reason


@pytest.mark.parametrize("status", [500, 502, 504])
def test_server_error_without_account_code_doesnt_stop(
    auth: FakeAuth, graph: respx.Route, status: int
) -> None:
    for response in (httpx.Response(status), error(status, "InternalServerError")):
        graph.mock(return_value=response)
        result = send(transport(auth))
        assert result.outcome_unknown and result.stop_reason is None


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("code", ["ErrorMessageSubmissionBlocked", "ErrorAccessDenied"])
def test_refusal_with_account_code_stays_auth(
    auth: FakeAuth, graph: respx.Route, status: int, code: str
) -> None:
    graph.mock(return_value=error(status, code))
    result = send(transport(auth))
    assert result.auth_error and not result.outcome_unknown and result.stop_reason is None


@pytest.mark.parametrize("status", [429, 503])
def test_throttled_with_seconds(auth: FakeAuth, graph: respx.Route, status: int) -> None:
    graph.mock(return_value=error(status, "ApplicationThrottled", **{"Retry-After": "42"}))
    result = send(transport(auth))
    assert result.retryable and result.retry_after == 42 and result.throttled


@pytest.mark.parametrize("status", [429, 503])
def test_throttled_without_header_uses_default(
    auth: FakeAuth, graph: respx.Route, status: int
) -> None:
    graph.mock(return_value=httpx.Response(status))
    result = send(transport(auth))
    assert result.retryable and result.retry_after == graph_mod.DEFAULT_RETRY_AFTER


def test_throttled_with_http_date(auth: FakeAuth, graph: respx.Route) -> None:
    now = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)
    when = format_datetime(now + timedelta(seconds=90), usegmt=True)
    graph.mock(return_value=httpx.Response(429, headers={"Retry-After": when}))
    result = send(transport(auth, now=lambda: now))
    assert result.retryable and result.retry_after == 90


def test_parse_retry_after() -> None:
    now = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)
    assert parse_retry_after(None, now) is None
    assert parse_retry_after("", now) is None
    assert parse_retry_after("soon", now) is None
    assert parse_retry_after("0", now) == 0
    assert parse_retry_after(" 7 ", now) == 7
    assert parse_retry_after("1.5", now) == 1.5
    assert parse_retry_after("9999999999", now) == graph_mod.MAX_RETRY_AFTER
    past = format_datetime(now - timedelta(minutes=5), usegmt=True)
    assert parse_retry_after(past, now) == 0
    assert parse_retry_after("Tue, 01 Dec 2026 15:02:00 GMT", now) == 120
    assert parse_retry_after("Tue, 01 Dec 2026 15:02:00", now) == 120  # no zone: GMT


# --- connection problems -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (httpx.ConnectError("dns"), "retry"),
        (httpx.ConnectTimeout("connect"), "retry"),
        (httpx.PoolTimeout("pool"), "retry"),
        (httpx.ReadTimeout("read"), "unknown"),
        (httpx.WriteTimeout("write"), "unknown"),
        (httpx.RemoteProtocolError("dropped"), "unknown"),
        (httpx.ReadError("reset"), "unknown"),
        (httpx.WriteError("broken pipe"), "unknown"),
    ],
)
def test_connection_mapping(auth: FakeAuth, graph: respx.Route, exc: Exception, kind: str) -> None:
    graph.mock(side_effect=exc)
    result = send(transport(auth))
    if kind == "retry":
        assert result.retryable and not result.throttled
    else:
        assert result.outcome_unknown


def test_budget_running_out_mid_request_is_unknown(
    auth: FakeAuth, graph: respx.Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token step uses up all but 10 ms of the budget (on the fake clock, so a slow machine
    can't turn this into "the token step ran out"); the request, which Graph never answers,
    is then cut off: it may have been accepted, so the outcome is unknown."""
    monkeypatch.setattr(graph_mod, "MIN_REQUEST_SECONDS", 0.0)
    ticks = Ticks()
    auth.on_token = lambda: ticks.advance(graph_mod.SEND_BUDGET_SECONDS - 0.01)

    started: list[httpx.Request] = []

    async def never_answers(request: httpx.Request) -> httpx.Response:
        started.append(request)  # respx doesn't record a call that was cut off
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    graph.mock(side_effect=never_answers)
    result = send(transport(auth, monotonic=ticks))
    assert len(started) == 1 and result.outcome_unknown


def test_timeouts_below_shutdown_grace() -> None:
    t = graph_mod.REQUEST_TIMEOUT
    budget = graph_mod.SEND_BUDGET_SECONDS
    assert budget < sender.SHUTDOWN_GRACE_SECONDS
    for part in (t.connect, t.read, t.write, t.pool):
        assert part is not None and 0 < part < budget
    assert (t.connect or 0) <= 5 and (t.read or 0) <= 10
    assert budget > graph_mod.MIN_REQUEST_SECONDS


def test_token_and_payload_never_logged(
    auth: FakeAuth, graph: respx.Route, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    raw = mime()
    payload = base64.b64encode(raw).decode()
    for response in (
        error(400, "ErrorInvalidRecipients"),
        error(429, "Throttled"),
        error(500, "Boom"),
        httpx.Response(202, headers={"request-id": "r"}),
    ):
        graph.mock(return_value=response)
        send(transport(auth), raw)
    graph.mock(side_effect=httpx.ReadTimeout("read"))
    send(transport(auth), raw)
    text = caplog.text
    assert TOKEN not in text and payload[:40] not in text and "secret detail" not in text
    assert "ErrorInvalidRecipients" in text  # the code is logged


# --- factory and app ---------------------------------------------------------------------------


def test_factory_graph(tmp_path: Path, auth: FakeAuth) -> None:
    t = make_transport(graph_settings(tmp_path), auth)
    assert isinstance(t, GraphTransport) and t.name == "graph" and t.msauth is auth
    with pytest.raises(ValueError, match="msauth"):
        make_transport(graph_settings(tmp_path))


# --- the worker in graph mode ------------------------------------------------------------------


class GraphEnv:
    def __init__(self, tmp_path: Path, account: Sender | None = ME) -> None:
        self.tmp_path = tmp_path
        self.clock = FakeClock()
        self.auth = fake_auth(tmp_path, account)
        self.db = self.auth.db
        self.settings = self.auth.settings
        self.transport = GraphTransport(self.auth)
        self.worker = SendWorker(
            self.db, self.settings, self.transport, clock=self.clock, sender=self.auth.sender
        )

    def campaign(self, emails: list[str], sender_address: str | None = None) -> int:
        draft = drafts.create_draft(self.db, self.tmp_path, "list.csv", b"Email\nx@example.com\n")
        recipients = [Recipient(i + 2, e, f"N{i}") for i, e in enumerate(emails)]
        return campaigns.create_campaign(
            self.db,
            draft.id,
            CONTENT,
            recipients,
            self.clock(),
            transport="graph",
            sender_address=sender_address,
        )

    def drive(self) -> None:
        async def go() -> None:
            for _ in range(100):
                delay = await self.worker.step()
                if delay is None:
                    break
                self.clock.advance(delay)
            await self.transport.aclose()

        asyncio.run(go())

    def campaign_row(self, cid: int) -> campaigns.Campaign:
        c = campaigns.get_campaign(self.db, cid)
        assert c is not None
        return c

    def recipient(self, cid: int, email: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            return dict(
                conn.execute(
                    "SELECT * FROM campaign_recipients WHERE campaign_id = ? AND email = ?",
                    (cid, email),
                ).fetchone()
            )


EMAILS = ["ada@example.com", "bob@example.com"]


def test_worker_sends_from_the_signed_in_address(tmp_path: Path, graph: respx.Route) -> None:
    graph.mock(return_value=httpx.Response(202, headers={"request-id": "rid"}))
    env = GraphEnv(tmp_path)
    cid = env.campaign(EMAILS)
    env.drive()
    assert env.campaign_row(cid).status == campaigns.FINISHED
    assert graph.call_count == 2
    sent = [
        message_from_bytes(base64.b64decode(c.request.content), policy=policy.default)
        for c in graph.calls
    ]
    assert [str(m["To"]) for m in sent] == ["N0 <ada@example.com>", "N1 <bob@example.com>"]
    for m in sent:
        assert m["From"].addresses[0].addr_spec == "Jake@outlook.com"
        assert m["From"].addresses[0].display_name == "The Calico Barn"
    assert env.recipient(cid, "ada@example.com")["provider_id"] == "rid"


def test_worker_signed_out_stops_without_sending(tmp_path: Path, graph: respx.Route) -> None:
    """Nothing connected: the stop says so, without blaming Microsoft (fix C3), and the
    recipient is sent once after connecting and resuming."""
    graph.mock(return_value=httpx.Response(202))
    env = GraphEnv(tmp_path, account=None)
    cid = env.campaign(EMAILS)
    env.drive()
    c = env.campaign_row(cid)
    assert c.status == campaigns.STOPPED_ERROR
    assert c.last_error == (
        "No Outlook account is connected. Connect it on the Account page. Then resume this send."
    )
    assert "Microsoft won't take mail" not in c.last_error
    assert not graph.called and env.auth.calls == 0
    row = env.recipient(cid, "ada@example.com")
    assert row["status"] == campaigns.R_PENDING and row["attempts"] == 0

    env.auth.account = ME
    campaigns.resume(env.db, cid, env.clock(), transport="graph")
    env.drive()
    assert env.campaign_row(cid).status == campaigns.FINISHED
    assert env.recipient(cid, "ada@example.com")["status"] == campaigns.R_SENT
    assert graph.call_count == len(EMAILS)  # each sent once


@pytest.mark.parametrize(
    "problem",
    [
        msauth.NO_ACCOUNT,
        "The saved Microsoft sign-in isn't for the sending address the app has stored "
        "(jake@outlook.com); the app probably stopped while a new sign-in was being saved. "
        "To be safe it isn't used. Connect the account again.",
    ],
)
def test_worker_signed_out_at_the_token(tmp_path: Path, graph: respx.Route, problem: str) -> None:
    """``NotSignedIn`` from the token step (signed out meanwhile, or a saved sign-in that doesn't
    match: fix B4): nothing sent, the stop gives that reason without the Microsoft prefix."""
    graph.mock(return_value=httpx.Response(202))
    env = GraphEnv(tmp_path)
    env.auth.token = NotSignedIn(problem)
    cid = env.campaign(EMAILS, sender_address=ME.address)
    env.drive()
    c = env.campaign_row(cid)
    assert c.status == campaigns.STOPPED_ERROR
    assert c.last_error == f"{problem} {sender.SIGNED_OUT_AFTER}"
    assert not c.last_error.startswith(sender.AUTH_STOP_MESSAGE)
    assert not graph.called
    row = env.recipient(cid, "ada@example.com")
    assert row["status"] == campaigns.R_PENDING and row["attempts"] == 0
    assert row["last_error"] == problem

    env.auth.token = TOKEN  # connected again
    campaigns.resume(env.db, cid, env.clock(), transport="graph")
    env.drive()
    assert env.campaign_row(cid).status == campaigns.FINISHED
    assert env.recipient(cid, "ada@example.com")["status"] == campaigns.R_SENT
    assert graph.call_count == len(EMAILS)


def test_test_send_signed_out_just_before_the_token(tmp_path: Path, graph: respx.Route) -> None:
    """A test send whose account was disconnected before the token: nothing sent, and the error
    says to connect it (no Microsoft prefix)."""
    graph.mock(return_value=httpx.Response(202))
    env = GraphEnv(tmp_path)

    def sign_out() -> None:
        env.auth.account = None

    env.auth.on_token = sign_out

    async def go() -> gate.SentTest:
        try:
            return await gate.send_test(
                env.db,
                env.settings,
                env.transport,
                draft_id="d" * 32,
                content=CONTENT,
                values={"first_name": "Ada", "email": "ada@example.com"},
                sender=ME,
                clock=env.clock,
            )
        finally:
            await env.transport.aclose()

    test = asyncio.run(go())
    assert test.ok is False and not test.outcome_unknown and not graph.called
    assert test.error == "No Outlook account is connected. Connect it on the Account page."


def test_worker_stops_when_the_account_changed(tmp_path: Path, graph: respx.Route) -> None:
    graph.mock(return_value=httpx.Response(202))
    env = GraphEnv(tmp_path)
    # Same address in another case: the same mailbox.
    cid = env.campaign(EMAILS[:1], sender_address="jake@OUTLOOK.com")
    env.drive()
    assert env.campaign_row(cid).status == campaigns.FINISHED
    assert graph.call_count == 1

    other = Sender("other@outlook.com", "Other")
    env.auth.account = other
    cid = env.campaign(EMAILS, sender_address=ME.address)
    env.drive()
    c = env.campaign_row(cid)
    assert c.status == campaigns.STOPPED_ERROR
    assert c.last_error and "other@outlook.com" in c.last_error and ME.address in c.last_error
    assert graph.call_count == 1 and env.auth.calls == 1  # nothing more sent
    row = env.recipient(cid, "ada@example.com")
    assert row["status"] == campaigns.R_PENDING and row["attempts"] == 0

    # Resuming as the new account needs a passing test of this content to it.
    problem, address = gate.resume_problem(env.db, env.settings, c, other)
    assert problem and "send a test of this message to other@outlook.com" in problem
    assert address is None
    assert gate.resume_problem(env.db, env.settings, c, ME) == (None, None)  # reconnected
    with env.db.connect() as conn:
        conn.execute(
            "INSERT INTO test_sends (draft_id, content_hash, sent_at, to_addr, ok) "
            "VALUES ('d', ?, ?, ?, 1)",
            (c.content_hash, campaigns.iso(env.clock()), other.address),
        )
    problem, address = gate.resume_problem(env.db, env.settings, c, other)
    assert problem is None and address == other.address
    campaigns.resume(env.db, cid, env.clock(), transport="graph", sender_address=address)
    env.drive()
    c = env.campaign_row(cid)
    assert c.status == campaigns.FINISHED and c.sender_address == other.address
    assert graph.call_count == 3


def test_worker_account_switched_just_before_the_token(tmp_path: Path, graph: respx.Route) -> None:
    """The worker's account check passed, then the account changed before the token was fetched:
    the message (From the old address) isn't sent and the campaign stops."""
    graph.mock(return_value=httpx.Response(202))
    env = GraphEnv(tmp_path)
    cid = env.campaign(EMAILS, sender_address=ME.address)

    def switch() -> None:
        env.auth.account = OTHER

    env.auth.on_token = switch
    env.drive()
    c = env.campaign_row(cid)
    assert c.status == campaigns.STOPPED_ERROR
    # The account change is named, not blamed on Microsoft (fix A4).
    assert c.last_error == campaigns.sender_changed_message(ME.address, OTHER.address)
    assert not c.last_error.startswith(sender.AUTH_STOP_MESSAGE)
    assert not graph.called and env.auth.expected == [ME.address]
    row = env.recipient(cid, "ada@example.com")
    assert row["status"] == campaigns.R_PENDING and row["attempts"] == 0
    assert "Outlook account changed to other@outlook.com" in row["last_error"]

    # Reconnected as the original account: resume sends to everyone, ada included.
    env.auth.on_token = None
    env.auth.account = ME
    campaigns.resume(env.db, cid, env.clock(), transport="graph")
    env.drive()
    assert env.campaign_row(cid).status == campaigns.FINISHED
    assert env.recipient(cid, "ada@example.com")["status"] == campaigns.R_SENT
    assert graph.call_count == 2


def test_test_send_account_switched_just_before_the_token(
    tmp_path: Path, graph: respx.Route
) -> None:
    """A test send whose account changed before the token: nothing sent, and the error names
    the account change (no "Microsoft won't take mail" prefix)."""
    graph.mock(return_value=httpx.Response(202))
    env = GraphEnv(tmp_path)

    def switch() -> None:
        env.auth.account = OTHER

    env.auth.on_token = switch

    async def go() -> gate.SentTest:
        try:
            return await gate.send_test(
                env.db,
                env.settings,
                env.transport,
                draft_id="d" * 32,
                content=CONTENT,
                values={"first_name": "Ada", "email": "ada@example.com"},
                sender=ME,
                clock=env.clock,
            )
        finally:
            await env.transport.aclose()

    test = asyncio.run(go())
    assert test.ok is False and not test.outcome_unknown and not graph.called
    assert test.error == (
        f"The connected Outlook account changed to {OTHER.address} (this message is from "
        f"{ME.address}). Nothing was sent."
    )


def test_worker_reauth_stops_the_campaign(tmp_path: Path, graph: respx.Route) -> None:
    env = GraphEnv(tmp_path)
    env.auth.token = ReauthRequired("The Microsoft sign-in has expired or was revoked.")
    cid = env.campaign(EMAILS)
    env.drive()
    c = env.campaign_row(cid)
    assert c.status == campaigns.STOPPED_ERROR
    assert c.last_error and c.last_error.startswith(sender.AUTH_STOP_MESSAGE)
    assert "expired or was revoked" in c.last_error
    assert c.last_error.endswith(sender.AUTH_STOP_AFTER)  # an ordinary auth stop, unchanged
    assert not graph.called
    assert env.recipient(cid, "ada@example.com")["attempts"] == 0
    # Fixed: resume sends.
    env.auth.token = TOKEN
    graph.mock(return_value=httpx.Response(202))
    campaigns.resume(env.db, cid, env.clock(), transport="graph")
    env.drive()
    assert env.campaign_row(cid).status == campaigns.FINISHED


def test_worker_account_blocked_stops_the_campaign(tmp_path: Path, graph: respx.Route) -> None:
    graph.mock(return_value=error(403, "ErrorMessageSubmissionBlocked"))
    env = GraphEnv(tmp_path)
    cid = env.campaign(EMAILS)
    env.drive()
    c = env.campaign_row(cid)
    assert c.status == campaigns.STOPPED_ERROR
    assert c.last_error and "ErrorMessageSubmissionBlocked" in c.last_error
    assert graph.call_count == 1  # stopped at the first refusal


def test_worker_unknown_is_not_retried(tmp_path: Path, graph: respx.Route) -> None:
    graph.mock(side_effect=httpx.ReadTimeout("read"))
    env = GraphEnv(tmp_path)
    cid = env.campaign(["ada@example.com"])
    env.drive()
    row = env.recipient(cid, "ada@example.com")
    assert row["status"] == campaigns.R_FAILED and "check Sent Items" in row["last_error"]
    assert graph.call_count == 1


@pytest.mark.parametrize("status", [500, 502, 504])
def test_worker_server_error_with_account_code_never_resends(
    tmp_path: Path, graph: respx.Route, status: int
) -> None:
    """A 5xx carrying an account-level code: the recipient is unknown (counted, not retried),
    the campaign stops, and resuming sends to the others only."""
    graph.mock(return_value=error(status, "ErrorMessageSubmissionBlocked"))
    env = GraphEnv(tmp_path)
    cid = env.campaign(EMAILS)
    env.drive()
    c = env.campaign_row(cid)
    assert c.status == campaigns.STOPPED_ERROR
    assert c.last_error and c.last_error.startswith(sender.AUTH_STOP_MESSAGE)
    assert "blocking this Outlook account" in c.last_error
    assert "ada@example.com" in c.last_error and "check Sent Items" in c.last_error
    assert graph.call_count == 1  # stopped at once, bob not tried
    ada = env.recipient(cid, "ada@example.com")
    assert ada["status"] == campaigns.R_FAILED and ada["attempts"] == 1
    assert ada["next_attempt_at"] is None
    assert ada["last_error"].startswith(f"outcome unknown (Microsoft answered HTTP {status} ")
    assert ada["last_error"].endswith(campaigns.UNKNOWN_SUFFIX)
    assert env.recipient(cid, "bob@example.com")["status"] == campaigns.R_PENDING
    with env.db.connect() as conn:
        outcomes = [r[0] for r in conn.execute("SELECT outcome FROM send_log ORDER BY id")]
    assert outcomes == [None]  # unknown: counts against the daily cap
    assert len(sender.sent_in_window(env.db, env.clock())) == 1

    # Fixed: resuming sends to bob only; ada is never sent again.
    graph.mock(return_value=httpx.Response(202))
    campaigns.resume(env.db, cid, env.clock(), transport="graph")
    env.drive()
    assert env.campaign_row(cid).status == campaigns.FINISHED
    assert graph.call_count == 2
    to = [
        str(message_from_bytes(base64.b64decode(call.request.content), policy=policy.default)["To"])
        for call in graph.calls
    ]
    assert to == ["N0 <ada@example.com>", "N1 <bob@example.com>"]
    assert env.recipient(cid, "ada@example.com")["status"] == campaigns.R_FAILED
    assert env.recipient(cid, "bob@example.com")["status"] == campaigns.R_SENT


def test_worker_503_with_account_code_is_resent_after_resume(
    tmp_path: Path, graph: respx.Route
) -> None:
    """503 is a refusal (like the throttles): an account-level code there stops the campaign and
    the recipient, certainly not sent, goes on resume."""
    graph.mock(return_value=error(503, "ErrorExceededMessageLimit", **{"Retry-After": "5"}))
    env = GraphEnv(tmp_path)
    cid = env.campaign(EMAILS[:1])
    env.drive()
    c = env.campaign_row(cid)
    assert c.status == campaigns.STOPPED_ERROR and c.last_error and "limit" in c.last_error
    row = env.recipient(cid, "ada@example.com")
    assert row["status"] == campaigns.R_PENDING and row["attempts"] == 0
    graph.mock(return_value=httpx.Response(202))
    campaigns.resume(env.db, cid, env.clock(), transport="graph")
    env.drive()
    assert env.campaign_row(cid).status == campaigns.FINISHED
    assert graph.call_count == 2
