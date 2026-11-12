"""The test send ("Send test to me") and the gate it opens, in mock and graph mode."""

import asyncio
import base64
import html
import re
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from email import message_from_bytes, policy
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import campaigns, compose, gate, sender
from app.config import Settings, load_settings
from app.db import Database
from app.main import create_app
from app.msauth import Sender
from app.transport import SendResult, make_transport
from app.transport.graph import ACCOUNT_CODES, SEND_MAIL_URL
from app.unsubscribe_tokens import verify_token
from tests.test_graph import GRAPH_ENV, ME, FakeAuth
from tests.test_sender import CONTENT, T0, FakeClock
from tests.test_ui_campaigns import STATE, pass_gate
from tests.test_ui_compose import BASE, make_draft, post, save

TEST_ID = re.compile(r"[?&]test=(\d+)")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app_settings(valid_env: dict[str, str], tmp_path: Path) -> Settings:
    return load_settings({**valid_env, "DATA_DIR": str(tmp_path), "DAILY_SEND_CAP": "5"})


@pytest.fixture
def client(app_settings: Settings, clock: FakeClock) -> Iterator[TestClient]:
    with TestClient(create_app(app_settings, clock=clock, run_worker=False), base_url=BASE) as c:
        yield c


def db_of(client: TestClient) -> Database:
    return client.app.state.db  # type: ignore[attr-defined]


def click_test(client: TestClient, draft: str, r: str = "0") -> httpx.Response:
    return post(client, f"/send/{draft}/test-send", {"r": r, "scheme": "light"})


def outbox(tmp_path: Path) -> list[Any]:
    files = sorted((tmp_path / "outbox").glob("*.eml")) if (tmp_path / "outbox").exists() else []
    return [message_from_bytes(f.read_bytes(), policy=policy.default) for f in files]


def start_page(client: TestClient, draft: str) -> str:
    return html.unescape(client.get(f"/send/{draft}/start").text)


def compose_page(client: TestClient, draft: str, query: str = "") -> str:
    return html.unescape(client.get(f"/send/{draft}/compose{query}").text)


# --- mock mode ---------------------------------------------------------------------------------


def test_start_locked_until_a_test_send(client: TestClient, tmp_path: Path) -> None:
    draft = make_draft(client)
    save(client, draft)
    page = compose_page(client, draft)
    assert 'id="send-test"' in page and "Send a test to yourself first" in page
    assert gate.MOCK_TEST_ADDRESS in page
    page = start_page(client, draft)
    assert "Send a test to yourself first" in page and 'id="start-send"' not in page
    r = post(client, f"/send/{draft}/start", {"content_hash": "0" * 64})
    assert r.status_code == 409
    assert campaigns.active_campaign_id(db_of(client)) is None
    assert outbox(tmp_path) == []


def test_test_send_flow(client: TestClient, tmp_path: Path, app_settings: Settings) -> None:
    draft = make_draft(client)
    save(client, draft)
    r = click_test(client, draft, r="0")
    assert r.status_code == 303, r.text
    assert r.headers["location"].endswith("#test-send")
    match = TEST_ID.search(r.headers["location"])
    assert match
    page = compose_page(client, draft, r.headers["location"].split("compose", 1)[1].split("#")[0])
    assert f"Test sent to {gate.MOCK_TEST_ADDRESS} at 2026-12-01" in page
    assert 'id="test-result"' in page and "for this version" in page

    [msg] = outbox(tmp_path)
    assert str(msg["To"]) == gate.MOCK_TEST_ADDRESS
    assert msg["Subject"] == "[TEST] Merry Christmas, Ada"
    assert "Dear Ada" in msg.get_body(("plain",)).get_content()
    # The unsubscribe link is for Jake's address, never the customer's.
    header = str(msg["List-Unsubscribe"]).strip("<>")
    assert header.endswith("/one-click")
    link = header.removesuffix("/one-click")  # the page; the header has the one-click route
    assert verify_token(app_settings.unsubscribe_secret, link.rsplit("/", 1)[1]) == (
        gate.MOCK_TEST_ADDRESS
    )
    assert link in msg.get_body(("plain",)).get_content()
    assert "ada@example.com" not in str(msg["To"])

    t = gate.get_test(db_of(client), int(match.group(1)))
    assert t is not None and t.ok and t.to_addr == gate.MOCK_TEST_ADDRESS
    c = compose.get_content(db_of(client), draft)
    assert c is not None and t.content_hash == compose.content_hash(c)

    page = start_page(client, draft)
    assert 'id="start-send"' in page and "for this version" in page
    hash_match = STATE.search(client.get(f"/send/{draft}/start").text)
    assert hash_match
    r = post(client, f"/send/{draft}/start", {"content_hash": hash_match.group(1)})
    assert r.status_code == 303


def test_sample_recipient_shows_fallbacks(client: TestClient, tmp_path: Path) -> None:
    draft = make_draft(client)
    save(client, draft)
    assert click_test(client, draft, r="sample").status_code == 303
    [msg] = outbox(tmp_path)
    assert msg["Subject"] == "[TEST] Merry Christmas, Friend"


def test_edit_locks_again(client: TestClient, clock: FakeClock) -> None:
    draft = make_draft(client)
    save(client, draft)
    assert click_test(client, draft).status_code == 303
    assert 'id="start-send"' in start_page(client, draft)
    save(client, draft, subject="Something else")
    page = compose_page(client, draft)
    assert "The message changed since your last test" in page
    page = start_page(client, draft)
    assert "The message changed since your last test" in page and 'id="start-send"' not in page
    clock.advance(31)
    assert click_test(client, draft).status_code == 303
    assert 'id="start-send"' in start_page(client, draft)
    # Going back to the earlier version: its test still counts (same content, same hash).
    save(client, draft)
    assert 'id="start-send"' in start_page(client, draft)


def test_rate_limits(client: TestClient, clock: FakeClock, tmp_path: Path) -> None:
    draft = make_draft(client)
    save(client, draft)
    assert click_test(client, draft).status_code == 303
    r = click_test(client, draft)
    assert r.status_code == 429
    assert "One test every 30 seconds" in html.unescape(r.text)
    clock.advance(29)
    assert click_test(client, draft).status_code == 429
    clock.advance(2)
    assert click_test(client, draft).status_code == 303
    assert len(outbox(tmp_path)) == 2
    # 20 per rolling 24 h, whatever their outcome.
    with db_of(client).connect() as conn:
        for i in range(gate.MAX_TESTS_PER_DAY - 2):
            conn.execute(
                "INSERT INTO test_sends (draft_id, content_hash, sent_at, to_addr, ok) "
                "VALUES (?, 'x', ?, 'a@example.com', 0)",
                (draft, campaigns.iso(clock() - timedelta(hours=1, seconds=i))),
            )
    clock.advance(31)
    r = click_test(client, draft)
    assert r.status_code == 429 and "At most 20 test sends per 24 hours" in r.text
    clock.advance(24 * 3600)
    assert click_test(client, draft).status_code == 303


def test_counted_against_the_daily_cap(client: TestClient, clock: FakeClock) -> None:
    draft = make_draft(client)
    save(client, draft)
    db = db_of(client)
    assert click_test(client, draft).status_code == 303
    with db.connect() as conn:
        rows = conn.execute("SELECT campaign_id, row_no, outcome FROM send_log").fetchall()
    assert [tuple(r) for r in rows] == [(None, None, "sent")]
    assert len(sender.sent_in_window(db, clock())) == 1
    assert "(1 used in the last 24 hours)" in start_page(client, draft)
    # Cap of 5 (fixture): fill it, then a test is refused.
    with db.connect() as conn:
        for _ in range(4):
            conn.execute(
                "INSERT INTO send_log (started_at, outcome) VALUES (?, 'sent')",
                (campaigns.iso(clock()),),
            )
    clock.advance(31)
    r = click_test(client, draft)
    assert r.status_code == 429 and "daily limit of 5" in r.text


def test_test_send_holds_back_bulk_pacing(app_settings: Settings, clock: FakeClock) -> None:
    """A test send is a transport call: the worker's pacing waits after it."""
    with TestClient(create_app(app_settings, clock=clock, run_worker=False), base_url=BASE) as c:
        draft = make_draft(c)
        save(c, draft)
        assert click_test(c, draft).status_code == 303
        worker = c.app.state.worker  # type: ignore[attr-defined]
        assert worker._pacing_ready_at(clock()) == clock() + worker.spacing


def test_throttle_refuses_test_sends(client: TestClient, clock: FakeClock) -> None:
    draft = make_draft(client)
    save(client, draft)
    with db_of(client).connect() as conn:
        campaigns.set_throttle(conn, clock(), clock() + timedelta(minutes=5))
    r = click_test(client, draft)
    assert r.status_code == 429 and "Microsoft asked to wait" in r.text


def test_failed_test_does_not_open_the_gate(client: TestClient, clock: FakeClock) -> None:
    draft = make_draft(client)
    save(client, draft)
    transport = client.app.state.transport  # type: ignore[attr-defined]
    transport.decide = lambda r, n: SendResult.permanent("refused for the test")
    r = click_test(client, draft)
    assert r.status_code == 303
    page = compose_page(client, draft, "?test=1")
    assert "The test wasn't sent: refused for the test" in page
    assert "Send a test to yourself first" in page  # the gate stays locked
    assert 'id="start-send"' not in start_page(client, draft)
    with db_of(client).connect() as conn:
        assert conn.execute("SELECT outcome FROM send_log").fetchone()[0] == "failed"
    assert sender.sent_in_window(db_of(client), clock()) == []


def test_unknown_outcome_counts_but_does_not_pass(client: TestClient, clock: FakeClock) -> None:
    draft = make_draft(client)
    save(client, draft)
    transport = client.app.state.transport  # type: ignore[attr-defined]
    transport.decide = lambda r, n: SendResult.unknown("read timeout")
    r = click_test(client, draft)
    assert r.status_code == 303
    page = compose_page(client, draft, "?" + r.headers["location"].split("?", 1)[1].split("#")[0])
    assert "Not sure whether the test to you@example.com" in page and "read timeout" in page
    assert "The test wasn't sent" not in page
    assert "Send a test to yourself first" in page  # the gate stays locked
    assert 'id="start-send"' not in start_page(client, draft)
    assert len(sender.sent_in_window(db_of(client), clock())) == 1
    t = gate.latest_for_draft(db_of(client), draft)
    assert t is not None and t.ok is False and t.outcome_unknown


def test_unknown_with_stop_reason_shows_account_advice(
    client: TestClient, clock: FakeClock
) -> None:
    # Fix A1: a Graph 5xx with an account-level code is unknown with a stop_reason; the test
    # result must say what to do about the account, and the gate stays locked. The advice comes
    # after the status (shown once), worded for a test, not a campaign resume.
    draft = make_draft(client)
    save(client, draft)
    code = "HTTP 500 ErrorMessageSubmissionBlocked"
    stop_reason = ACCOUNT_CODES["ErrorMessageSubmissionBlocked"].format(code=code)
    transport = client.app.state.transport  # type: ignore[attr-defined]
    transport.decide = lambda r, n: SendResult.unknown(
        f"Microsoft answered {code}", stop_reason=stop_reason
    )
    r = click_test(client, draft)
    assert r.status_code == 303
    page = compose_page(client, draft, "?" + r.headers["location"].split("?", 1)[1].split("#")[0])
    m = re.search(r'<p class="warn-box" role="alert" id="test-result">(.*?)</p>', page, re.S)
    assert m is not None
    assert m.group(1) == (
        "Not sure whether the test to you@example.com at 2026-12-01 10:00:00 EST went out "
        "(Microsoft answered "
        "HTTP 500 ErrorMessageSubmissionBlocked). Check your inbox and Sent Items. Microsoft is "
        "blocking this Outlook account from sending. This usually follows 'unusual activity': "
        "sign in at outlook.com, complete any verification it asks for and wait a while before "
        "sending another test; check that ordinary emails from outlook.com go out. It doesn't "
        "unlock sending to the list: send another test."
    )
    assert m.group(1).count(code) == 1 and ".)." not in m.group(1)
    assert "resum" not in m.group(1).lower()
    assert "Send a test to yourself first" in page  # the gate stays locked
    assert 'id="start-send"' not in start_page(client, draft)
    t = gate.latest_for_draft(db_of(client), draft)
    assert t is not None and t.ok is False and t.outcome_unknown
    assert t.reason == f"Microsoft answered {code}"
    assert t.advice is not None and t.advice.startswith("Microsoft is blocking")
    with db_of(client).connect() as conn:
        assert conn.execute("SELECT outcome FROM send_log").fetchone()[0] is None


@pytest.mark.parametrize("message", sorted(set(ACCOUNT_CODES.values())))
def test_account_advice_for_test_drops_code_and_resume(message: str) -> None:
    advice = gate.account_advice_for_test(
        "Microsoft answered HTTP 500 X", message.format(code="HTTP 500 X")
    )
    assert "HTTP 500" not in advice and "resum" not in advice.lower() and "()" not in advice


def test_needs_saved_content_and_csrf(client: TestClient, tmp_path: Path) -> None:
    draft = make_draft(client)
    r = click_test(client, draft)
    assert r.status_code == 409 and "Test not sent: Save the message" in r.text
    save(client, draft)
    r = client.post(
        f"/send/{draft}/test-send",
        data={"r": "0"},
        headers={"Origin": BASE},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert outbox(tmp_path) == []


def test_unknown_draft(client: TestClient) -> None:
    assert click_test(client, "f" * 32).status_code == 404


def test_check_can_start(app_settings: Settings, tmp_path: Path) -> None:
    db = Database(tmp_path / "app.db")
    db.init()
    assert gate.check_can_start(app_settings, db, "d" * 32, "h" * 64, sender=None)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO test_sends (draft_id, content_hash, sent_at, to_addr, ok) "
            "VALUES (?, ?, '2026-12-01T00:00:00+00:00', ?, 1)",
            ("d" * 32, "h" * 64, gate.MOCK_TEST_ADDRESS.upper()),
        )
    assert gate.check_can_start(app_settings, db, "d" * 32, "h" * 64, sender=None) == []
    # Another draft with the same content: the same email, so the test counts.
    assert gate.check_can_start(app_settings, db, "e" * 32, "h" * 64, sender=None) == []
    assert gate.check_can_start(app_settings, db, "d" * 32, "i" * 64, sender=None)
    graph = load_settings({**GRAPH_ENV, "DATA_DIR": str(tmp_path)})
    # Graph: not connected; then connected, but the test went to the mock address.
    assert gate.check_can_start(graph, db, "d" * 32, "h" * 64, sender=None) == [gate.NOT_CONNECTED]
    [why] = gate.check_can_start(graph, db, "d" * 32, "h" * 64, sender=ME)
    assert "not Jake@outlook.com" in why


# --- graph mode --------------------------------------------------------------------------------


@pytest.fixture
def graph() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        yield router.post(SEND_MAIL_URL)


def graph_client(tmp_path: Path, clock: FakeClock, account: Sender | None) -> TestClient:
    settings = load_settings({**GRAPH_ENV, "DATA_DIR": str(tmp_path)})
    db = Database(tmp_path / "app.db")
    auth = FakeAuth(settings, db, account=account)
    return TestClient(
        create_app(settings, clock=clock, run_worker=False, msauth=auth), base_url=BASE
    )


def test_graph_test_send_to_the_signed_in_address(
    tmp_path: Path, clock: FakeClock, graph: respx.Route
) -> None:
    graph.mock(return_value=httpx.Response(202, headers={"request-id": "req-1"}))
    with graph_client(tmp_path, clock, ME) as c:
        draft = make_draft(c)
        save(c, draft)
        page = compose_page(c, draft)
        assert 'id="test-to">Jake@outlook.com<' in page
        assert click_test(c, draft).status_code == 303
        assert graph.call_count == 1
        msg = message_from_bytes(
            base64.b64decode(graph.calls.last.request.content), policy=policy.default
        )
        assert msg["To"].addresses[0].addr_spec == "Jake@outlook.com"
        assert msg["From"].addresses[0].addr_spec == "Jake@outlook.com"
        assert msg["Subject"].startswith("[TEST] ")
        t = gate.latest_for_draft(db_of(c), draft)
        assert t is not None and t.ok and t.provider_id == "req-1"
        assert 'id="start-send"' in start_page(c, draft)
        # Another account connected: the test no longer counts.
        c.app.state.msauth.account = Sender("other@outlook.com", None)  # type: ignore[attr-defined]
        page = start_page(c, draft)
        assert "not other@outlook.com" in page and 'id="start-send"' not in page


def test_graph_signed_out(tmp_path: Path, clock: FakeClock, graph: respx.Route) -> None:
    with graph_client(tmp_path, clock, None) as c:
        assert c.app.state.worker is not None  # type: ignore[attr-defined]
        draft = make_draft(c)
        save(c, draft)
        page = compose_page(c, draft)
        assert "Connect your Outlook account" in page and 'id="send-test"' not in page
        r = click_test(c, draft)
        assert r.status_code == 409 and "Connect your Outlook account" in html.unescape(r.text)
        assert gate.NOT_CONNECTED in start_page(c, draft)
        assert not graph.called


def test_graph_refused_test_shows_the_reason(
    tmp_path: Path, clock: FakeClock, graph: respx.Route
) -> None:
    graph.mock(
        return_value=httpx.Response(403, json={"error": {"code": "ErrorMessageSubmissionBlocked"}})
    )
    with graph_client(tmp_path, clock, ME) as c:
        draft = make_draft(c)
        save(c, draft)
        r = click_test(c, draft)
        assert r.status_code == 303
        page = compose_page(c, draft, "?" + r.headers["location"].split("?", 1)[1].split("#")[0])
        assert "The test wasn't sent" in page and "ErrorMessageSubmissionBlocked" in page
        assert 'id="start-send"' not in start_page(c, draft)


def test_graph_test_not_sent_if_the_account_changes_meanwhile(
    tmp_path: Path, clock: FakeClock, graph: respx.Route
) -> None:
    graph.mock(return_value=httpx.Response(202))
    with graph_client(tmp_path, clock, ME) as c:
        auth = c.app.state.msauth  # type: ignore[attr-defined]
        draft = make_draft(c)
        save(c, draft)

        def switch() -> None:  # after the test was built for ME, before the token
            auth.account = Sender("other@outlook.com", "Other")

        auth.on_token = switch
        r = click_test(c, draft)
        assert r.status_code == 303
        assert not graph.called and auth.expected == [ME.address]
        page = compose_page(c, draft, "?" + r.headers["location"].split("?", 1)[1].split("#")[0])
        assert "The test wasn't sent" in page and "account changed" in page


def test_home_page_explains_test_send_and_reauth(
    tmp_path: Path, clock: FakeClock, graph: respx.Route
) -> None:
    with graph_client(tmp_path, clock, ME) as c:
        page = html.unescape(c.get("/").text)
        assert "Send test to me" in page and "[TEST]" in page
        assert "Connected as Jake@outlook.com." in page
        c.app.state.msauth.rejected = 1  # type: ignore[attr-defined]
        page = html.unescape(c.get("/").text)
        assert "Reconnect needed" in page


def test_clock_set_back_does_not_block_tests(client: TestClient, clock: FakeClock) -> None:
    draft = make_draft(client)
    save(client, draft)
    assert click_test(client, draft).status_code == 303
    clock.advance(-3600)
    assert click_test(client, draft).status_code == 303


def test_out_of_order_clock_readings_still_block(app_settings: Settings, tmp_path: Path) -> None:
    """Two requests whose clock readings land in the opposite order to their reservations: the
    later reservation's earlier time must not look like "the clock was set back"."""
    db = Database(tmp_path / "app.db")
    db.init()
    args = (db, app_settings, "d" * 32, "h" * 64, gate.MOCK_TEST_ADDRESS)
    gate._reserve(*args, lambda: T0 + timedelta(milliseconds=5))
    with pytest.raises(gate.CantSendTest, match="One test every 30 seconds") as exc:
        gate._reserve(*args, lambda: T0)
    assert exc.value.rate_limited and "wait 30 more seconds" in str(exc.value)
    # Within the skew allowance in the future still counts as just sent; beyond it, it doesn't.
    with pytest.raises(gate.CantSendTest):
        gate._reserve(*args, lambda: T0 - timedelta(seconds=gate.CLOCK_SKEW_SECONDS - 1))
    gate._reserve(*args, lambda: T0 - timedelta(seconds=gate.CLOCK_SKEW_SECONDS + 1))


def test_concurrent_double_click_sends_once(
    app_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both requests build their email at the same time; only one is reserved and sent, even
    with a clock whose second reading is earlier than its first."""
    db = Database(tmp_path / "app.db")
    db.init()
    transport = make_transport(app_settings)
    both_building = threading.Barrier(2, timeout=30)
    build = gate.build_test

    def slow_build(*a: object, **kw: object) -> bytes:
        both_building.wait()
        return build(*a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(gate, "build_test", slow_build)
    readings = iter([T0 + timedelta(milliseconds=5), T0, T0, T0])

    def clock() -> datetime:
        return next(readings)

    async def go() -> list[object]:
        one = gate.send_test(
            db,
            app_settings,
            transport,
            draft_id="d" * 32,
            content=CONTENT,
            values={"first_name": "Ada", "email": "ada@example.com"},
            sender=None,
            clock=clock,
        )
        two = gate.send_test(
            db,
            app_settings,
            transport,
            draft_id="d" * 32,
            content=CONTENT,
            values={"first_name": "Ada", "email": "ada@example.com"},
            sender=None,
            clock=clock,
        )
        return await asyncio.gather(one, two, return_exceptions=True)

    results = asyncio.run(go())
    sent = [r for r in results if isinstance(r, gate.SentTest)]
    refused = [r for r in results if isinstance(r, gate.CantSendTest)]
    assert len(sent) == 1 and len(refused) == 1, results
    assert sent[0].ok and "One test every 30 seconds" in str(refused[0])
    assert len(outbox(tmp_path)) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM test_sends").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM send_log").fetchone()[0] == 1


def test_refused_while_a_campaign_is_sending(client: TestClient, clock: FakeClock) -> None:
    ready_draft_and_campaign(client)
    with db_of(client).connect() as conn:
        conn.execute("UPDATE campaigns SET status = ?", (campaigns.SENDING,))
    other = make_draft(client)
    save(client, other)
    r = click_test(client, other)
    assert r.status_code == 409
    assert "to the list is in progress. Pause it to send a test" in html.unescape(r.text)
    with db_of(client).connect() as conn:
        conn.execute("UPDATE campaigns SET status = ?", (campaigns.PAUSED,))
    assert click_test(client, other).status_code == 303


def ready_draft_and_campaign(client: TestClient) -> str:
    draft = make_draft(client)
    save(client, draft)
    pass_gate(client, draft)
    hash_match = STATE.search(client.get(f"/send/{draft}/start").text)
    assert hash_match
    r = post(client, f"/send/{draft}/start", {"content_hash": hash_match.group(1)})
    assert r.status_code == 303, r.text
    return draft


def test_paced_after_a_bulk_send(client: TestClient, clock: FakeClock) -> None:
    """A test right after a bulk message waits for the bulk spacing (60 / SEND_RATE_PER_MINUTE)."""
    draft = make_draft(client)
    save(client, draft)
    spacing = 60 / db_settings(client).send_rate_per_minute
    with db_of(client).connect() as conn:
        conn.execute(
            "INSERT INTO send_log (started_at, outcome) VALUES (?, 'sent')",
            (campaigns.iso(clock() - timedelta(seconds=spacing - 4)),),
        )
    r = click_test(client, draft)
    assert r.status_code == 429
    text = html.unescape(r.text)
    assert f"sends are spaced {spacing:g} seconds apart: wait 4 more seconds" in text
    clock.advance(4)
    assert click_test(client, draft).status_code == 303


def db_settings(client: TestClient) -> Settings:
    return client.app.state.settings  # type: ignore[attr-defined]


def test_graph_start_records_the_tested_address(
    tmp_path: Path, clock: FakeClock, graph: respx.Route
) -> None:
    graph.mock(return_value=httpx.Response(202))
    with graph_client(tmp_path, clock, ME) as c:
        draft = make_draft(c)
        save(c, draft)
        assert click_test(c, draft).status_code == 303
        hash_match = STATE.search(c.get(f"/send/{draft}/start").text)
        assert hash_match
        r = post(c, f"/send/{draft}/start", {"content_hash": hash_match.group(1)})
        assert r.status_code == 303
        cid = int(r.headers["location"].rsplit("/", 1)[1])
        camp = campaigns.get_campaign(db_of(c), cid)
        assert camp is not None and camp.sender_address == ME.address
        # Another account connected: resuming is refused until a test to it passes.
        campaigns.pause(db_of(c), cid, clock())
        c.app.state.msauth.account = Sender("other@outlook.com", None)  # type: ignore[attr-defined]
        r = post(c, f"/campaigns/{cid}/resume", {})
        assert r.status_code == 409
        assert "send a test of this message to other@outlook.com" in html.unescape(r.text)
        camp = campaigns.get_campaign(db_of(c), cid)
        assert camp is not None and camp.status == campaigns.PAUSED


def test_test_send_unsubscribe_link_only_unsubscribes_jake(
    client: TestClient, tmp_path: Path, app_settings: Settings
) -> None:
    """Every unsubscribe link in a test (header, HTML, text) is for Jake's own address, so
    clicking it while checking the page can't unsubscribe the customer whose fields were used."""
    draft = make_draft(client)
    save(client, draft)
    assert click_test(client, draft, r="0").status_code == 303
    [msg] = outbox(tmp_path)
    header = str(msg["List-Unsubscribe"]).strip("<>").removesuffix("/one-click")
    html_part = msg.get_body(("html",)).get_content()
    text_part = msg.get_body(("plain",)).get_content()
    links = set(re.findall(r"https://mail\.example\.com/u/[A-Za-z0-9_.-]+", html_part + text_part))
    assert links == {header}
    token = header.rsplit("/", 1)[1]
    assert verify_token(app_settings.unsubscribe_secret, token) == gate.MOCK_TEST_ADDRESS
    assert client.post(f"/u/{token}/one-click").status_code == 200
    text = (tmp_path / "unsubscribed.txt").read_text(encoding="utf-8")
    assert text.startswith(gate.MOCK_TEST_ADDRESS + "\t") and "ada@example.com" not in text
