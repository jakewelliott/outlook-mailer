"""Transport interface, the mock transport's outbox and failure injection, and the factory."""

import asyncio
import stat
from datetime import UTC, datetime
from email import message_from_bytes, policy
from pathlib import Path

import pytest

from app.compose import RenderedEmail
from app.config import load_settings
from app.message import MessageError, build_mime
from app.transport import MockTransport, SendResult, Transport, make_transport
from app.transport.mock import safe_recipient

BASE = "https://mail.example.com"


def mime(to: str = "ada@example.com", name: str | None = "Ada") -> bytes:
    r = RenderedEmail(
        subject="Merry Christmas", html="<p>Hi</p>", text="Hi", unsubscribe_url=f"{BASE}/u/t"
    )
    return build_mime(r, to_addr=to, to_name=name, from_addr=None, from_name=None)


def send(t: Transport, raw: bytes, recipient: str) -> SendResult:
    return asyncio.run(t.send(raw, recipient=recipient))


# --- SendResult ------------------------------------------------------------------------------


def test_result_constructors() -> None:
    assert SendResult.success("id") == SendResult(ok=True, provider_id="id")
    r = SendResult.retry("throttled", retry_after=30)
    assert (r.ok, r.retryable, r.retry_after, r.auth_error) == (False, True, 30, False)
    p = SendResult.permanent("rejected")
    assert (p.ok, p.retryable, p.auth_error) == (False, False, False)
    a = SendResult.auth("signed out")
    assert (a.ok, a.retryable, a.auth_error) == (False, False, True)
    u = SendResult.unknown("read timeout")
    assert (u.ok, u.retryable, u.auth_error, u.outcome_unknown) == (False, False, False, True)
    assert r.throttled and not SendResult.retry("timeout").throttled and not u.throttled


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ok": True, "error": "x"},
        {"ok": True, "retryable": True},
        {"ok": False},
        {"ok": False, "error": "x", "auth_error": True, "retryable": True},
        {"ok": False, "error": "x", "retry_after": 5},
        {"ok": False, "error": "x", "retryable": True, "retry_after": -1},
        {"ok": True, "outcome_unknown": True},
        {"ok": False, "error": "x", "outcome_unknown": True, "retryable": True},
        {"ok": False, "error": "x", "outcome_unknown": True, "auth_error": True},
        {"ok": False, "error": "x", "outcome_unknown": True, "retry_after": 5},
    ],
)
def test_inconsistent_results_refused(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SendResult(**kwargs)  # type: ignore[arg-type]


# --- mock outbox -----------------------------------------------------------------------------


def test_mock_writes_valid_eml_with_private_permissions(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    t = MockTransport(outbox, clock=lambda: datetime(2026, 12, 1, 15, 30, 5, 123456, tzinfo=UTC))
    raw = mime()
    result = send(t, raw, "ada@example.com")
    assert result.ok and result.provider_id
    (path,) = t.written
    assert path.parent == outbox
    assert path.name == result.provider_id == "20261201T153005.123456Z-000001-ada@example.com.eml"
    assert path.read_bytes() == raw
    assert stat.S_IMODE(outbox.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    msg = message_from_bytes(path.read_bytes(), policy=policy.default)
    assert msg.get_content_type() == "multipart/alternative"
    assert msg["To"].addresses[0].addr_spec == "ada@example.com"


def test_mock_names_are_unique_and_safe(tmp_path: Path) -> None:
    t = MockTransport(tmp_path / "outbox", clock=lambda: datetime(2026, 12, 1, tzinfo=UTC))
    for _ in range(3):
        assert send(t, mime(), "ada@example.com").ok
    odd = "o'brien+x/y@example.com"
    assert send(t, mime(odd), odd).ok
    names = [p.name for p in t.written]
    assert len(set(names)) == 4
    assert names[3] == "20261201T000000.000000Z-000004-o_brien_x_y@example.com.eml"
    assert all(p.parent == tmp_path / "outbox" for p in t.written)


def test_safe_recipient() -> None:
    assert safe_recipient(" Ada@Example.COM ") == "ada@example.com"
    assert safe_recipient("../../etc/passwd") == "etc_passwd"
    assert safe_recipient("ü" * 10) == "unknown"
    assert len(safe_recipient("a" * 300 + "@example.com")) == 64


def test_mock_tightens_existing_outbox(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir(mode=0o755)
    outbox.chmod(0o755)
    assert send(MockTransport(outbox), mime(), "ada@example.com").ok
    assert stat.S_IMODE(outbox.stat().st_mode) == 0o700


def test_mock_creates_missing_parent_directories(tmp_path: Path) -> None:
    outbox = tmp_path / "data" / "outbox"
    assert send(MockTransport(outbox), mime(), "ada@example.com").ok
    assert stat.S_IMODE(outbox.stat().st_mode) == 0o700


def test_mock_refuses_message_for_someone_else(tmp_path: Path) -> None:
    t = MockTransport(tmp_path / "outbox")
    with pytest.raises(MessageError):
        send(t, mime("ada@example.com"), "eve@example.com")
    assert not (tmp_path / "outbox").exists()


def test_outbox_write_failure_is_retryable(tmp_path: Path) -> None:
    blocker = tmp_path / "outbox"
    blocker.write_text("not a directory")
    result = send(MockTransport(blocker), mime(), "ada@example.com")
    assert not result.ok and result.retryable and result.error


# --- failure injection -----------------------------------------------------------------------


def test_failure_injection(tmp_path: Path) -> None:
    def decide(recipient: str, attempt: int) -> SendResult | None:
        if recipient == "slow@example.com" and attempt < 3:
            return SendResult.retry("429 Too Many Requests", retry_after=12.5)
        if recipient == "bad@example.com":
            return SendResult.permanent("400 rejected")
        if recipient == "auth@example.com":
            return SendResult.auth("401 token expired")
        return None

    t = MockTransport(tmp_path / "outbox", decide=decide)
    first = send(t, mime("slow@example.com"), "slow@example.com")
    assert (first.ok, first.retryable, first.retry_after) == (False, True, 12.5)
    assert not send(t, mime("slow@example.com"), "slow@example.com").ok
    assert send(t, mime("slow@example.com"), "slow@example.com").ok
    bad = send(t, mime("bad@example.com"), "bad@example.com")
    assert (bad.ok, bad.retryable, bad.auth_error) == (False, False, False)
    auth = send(t, mime("auth@example.com"), "auth@example.com")
    assert (auth.ok, auth.retryable, auth.auth_error) == (False, False, True)
    assert send(t, mime(), "ada@example.com").ok
    assert send(t, mime(), " ADA@Example.com").ok
    assert t.attempts == {
        "slow@example.com": 3,
        "bad@example.com": 1,
        "auth@example.com": 1,
        "ada@example.com": 2,
    }
    assert [p.name.split("-", 2)[2] for p in t.written] == [
        "slow@example.com.eml",
        "ada@example.com.eml",
        "ada@example.com.eml",
    ]


# --- factory ---------------------------------------------------------------------------------


def test_factory_mock(valid_env: dict[str, str], tmp_path: Path) -> None:
    t = make_transport(load_settings({**valid_env, "DATA_DIR": str(tmp_path)}))
    assert isinstance(t, MockTransport) and t.name == "mock"
    assert t.outbox == tmp_path / "outbox"
    asyncio.run(t.aclose())


def test_factory_graph_not_yet(valid_env: dict[str, str]) -> None:
    s = load_settings({**valid_env, "MAIL_TRANSPORT": "graph", "MS_CLIENT_ID": "abc"})
    with pytest.raises(NotImplementedError, match="stage 12"):
        make_transport(s)
