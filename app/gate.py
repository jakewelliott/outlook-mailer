"""The test send, and the gate it opens: may a bulk send of this content start?

Bulk sending is locked until a **test send of the current content** (``compose.content_hash``)
to Jake's own address has been accepted, for both transports, so the flow is the same in mock
mode. The test goes to the signed-in Outlook address (graph) or to ``MOCK_TEST_ADDRESS`` (mock:
it lands in the outbox like every mock message). With graph, a test only counts for the address
it went to, so connecting a different account needs a new test. Any edit changes the hash and
locks sending again.

``send_test`` renders the saved content exactly as a bulk send would, for one recipient's merge
values (or the compose page's sample), except that:

- the subject starts with ``TEST_SUBJECT_PREFIX`` ("[TEST] ");
- it is addressed to Jake, and its unsubscribe link (body and ``List-Unsubscribe``) is for
  Jake's address, never the customer's, so clicking it can't unsubscribe a customer.

Test sends share the mailbox's quota with bulk sends, so each one writes a ``send_log`` row with
no campaign: it counts against ``DAILY_SEND_CAP`` (unknown outcomes too) and the worker's pacing
waits after it, and a ``Retry-After`` from Microsoft holds back everything. A test is refused
while a campaign is ``sending`` (pause it first: the worker doesn't recheck the pacing when it
marks a recipient, so a test squeezed in between could break the spacing) and, after a bulk
send, until ``60 / SEND_RATE_PER_MINUTE`` seconds have passed ("wait N seconds"). Tests are
also limited on their own: at most one per ``MIN_SECONDS_BETWEEN_TESTS`` and
``MAX_TESTS_PER_DAY`` per rolling 24 h (attempts, whatever their outcome). The clock is read and
the limits are checked under the database write lock, and the ``test_sends`` row and its
``send_log`` row are written in that same transaction *before* the transport is called, so two
clicks can't both send, and an interrupted test counts against the cap but never as a success.
A clock set back (the last test more than ``CLOCK_SKEW_SECONDS`` in the future) doesn't block
tests; a last test a little in the future counts as just sent.

A graph campaign records the address its test went to (``campaigns.sender_address``); the worker
stops it if another account is connected, and resuming it as the new account needs a passing
test of the campaign's content to that address (``resume_problem``).
"""

import asyncio
import dataclasses
import logging
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from app import campaigns, compose
from app.campaigns import Campaign, iso, parse_iso
from app.compose import DraftContent
from app.config import Settings
from app.db import Database
from app.emails import normalise_key
from app.message import MessageError, build_mime
from app.msauth import Sender
from app.sender import DAILY_WINDOW
from app.transport.base import SendResult, Transport

log = logging.getLogger("outlook_mailer.gate")

TEST_SUBJECT_PREFIX = "[TEST] "
MIN_SECONDS_BETWEEN_TESTS = 30
MAX_TESTS_PER_DAY = 20
# A last test up to this far in the future counts as just sent (two requests whose clock readings
# landed out of order); further than that, the clock was set back and it doesn't block.
CLOCK_SKEW_SECONDS = 60
# Mock mode has no account: the test is addressed here (it only goes to the outbox).
MOCK_TEST_ADDRESS = "you@example.com"

NOT_CONNECTED = "Connect your Outlook account on the Account page first, then send a test to it."


@dataclass(frozen=True)
class SentTest:
    id: int
    draft_id: str
    content_hash: str
    sent_at: datetime
    to_addr: str
    ok: bool | None  # None: in flight, or interrupted (not a success)
    outcome_unknown: bool  # ok is False, but it may have gone out
    provider_id: str | None
    error: str | None


class CantSendTest(Exception):
    """A test send can't be made now; the message is for the user."""

    def __init__(self, message: str, *, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited


def own_address(settings: Settings, sender: Sender | None) -> str | None:
    """Where test sends go: the signed-in address (graph; None if not connected) or the mock
    placeholder."""
    if settings.mail_transport == "mock":
        return MOCK_TEST_ADDRESS
    return sender.address if sender is not None else None


def _row(r: sqlite3.Row | None) -> SentTest | None:
    if r is None:
        return None
    return SentTest(
        id=r["id"],
        draft_id=r["draft_id"],
        content_hash=r["content_hash"],
        sent_at=datetime.fromisoformat(r["sent_at"]),
        to_addr=r["to_addr"],
        ok=None if r["ok"] is None else bool(r["ok"]),
        outcome_unknown=bool(r["outcome_unknown"]),
        provider_id=r["provider_id"],
        error=r["error"],
    )


def get_test(db: Database, test_id: int) -> SentTest | None:
    with db.connect() as conn:
        return _row(conn.execute("SELECT * FROM test_sends WHERE id = ?", (test_id,)).fetchone())


def passed(db: Database, content_hash: str, to_addr: str) -> SentTest | None:
    """The latest accepted test of ``content_hash`` to ``to_addr`` (any draft), if any."""
    key = normalise_key(to_addr)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM test_sends WHERE content_hash = ? AND ok = 1 ORDER BY id DESC",
            (content_hash,),
        ).fetchall()
    return next((t for t in map(_row, rows) if t and normalise_key(t.to_addr) == key), None)


def latest_for_draft(db: Database, draft_id: str, *, ok_only: bool = False) -> SentTest | None:
    with db.connect() as conn:
        return _row(
            conn.execute(
                "SELECT * FROM test_sends WHERE draft_id = ?"
                + (" AND ok = 1" if ok_only else "")
                + " ORDER BY id DESC LIMIT 1",
                (draft_id,),
            ).fetchone()
        )


# --- the gate ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class GateStatus:
    ok: bool  # a test of this exact content to this address was accepted
    message: str
    test: SentTest | None  # the passing test, or the draft's last accepted one
    changed: bool = False  # an earlier version was tested, not this one


def status(
    db: Database,
    settings: Settings,
    sender: Sender | None,
    draft_id: str,
    content_hash: str,
    zone: tzinfo,
) -> GateStatus:
    to = own_address(settings, sender)
    if to is None:
        return GateStatus(False, NOT_CONNECTED, None)
    hit = passed(db, content_hash, to)
    if hit is not None:
        when = campaigns.local_time(hit.sent_at, zone)
        return GateStatus(True, f"Test sent to {hit.to_addr} at {when} for this version.", hit)
    last = latest_for_draft(db, draft_id, ok_only=True)
    if last is not None and last.content_hash != content_hash:
        return GateStatus(
            False,
            "The message changed since your last test. Send a new test (Send test to me on the "
            "compose page); sending to the list stays locked until then.",
            last,
            changed=True,
        )
    if last is not None:  # same content, but to another address (the account changed)
        return GateStatus(
            False,
            f"Your last test went to {last.to_addr}, not {to}. Send a new test to {to}.",
            last,
        )
    return GateStatus(
        False,
        "Send a test to yourself first (Send test to me on the compose page): sending to the "
        "list is locked until a test of this exact version has gone out.",
        None,
    )


def check_can_start(
    settings: Settings,
    db: Database,
    draft_id: str,
    content_hash: str,
    *,
    sender: Sender | None,
) -> list[str]:
    """Reasons a bulk send of ``content_hash`` can't start yet; empty if it can. Called by the
    start page and again when the campaign is created."""
    gate = status(db, settings, sender, draft_id, content_hash, campaigns.local_zone(settings.tz))
    return [] if gate.ok else [gate.message]


def resume_problem(
    db: Database, settings: Settings, c: Campaign, sender: Sender | None
) -> tuple[str | None, str | None]:
    """``(why c can't be resumed, None)``, or ``(None, the address it goes on as)``. A graph
    campaign started as one account resumes as another only once its content has passed a test
    to the new address. Signed out is left to the worker (it stops the campaign again)."""
    if settings.mail_transport != "graph" or c.sender_address is None or sender is None:
        return None, None
    if normalise_key(sender.address) == normalise_key(c.sender_address):
        return None, None
    if passed(db, c.content_hash, sender.address) is not None:
        return None, sender.address
    return campaigns.sender_changed_message(c.sender_address, sender.address), None


# --- sending a test ----------------------------------------------------------------------------


def _too_soon(now: datetime, last: datetime | None, spacing: float) -> float | None:
    """Seconds to wait if ``last`` was less than ``spacing`` seconds ago (or a little in the
    future: see ``CLOCK_SKEW_SECONDS``), else None."""
    if last is None:
        return None
    elapsed = (now - last).total_seconds()
    if -CLOCK_SKEW_SECONDS <= elapsed < spacing:
        return spacing - max(elapsed, 0.0)
    return None


def _seconds(wait: float) -> str:
    n = max(1, math.ceil(wait))
    return f"{n} more second" + ("" if n == 1 else "s")


def _reserve(
    db: Database,
    settings: Settings,
    draft_id: str,
    digest: str,
    to: str,
    clock: Callable[[], datetime],
) -> tuple[int, int]:
    """Check the limits and record the test as in flight. The clock is read after the write lock
    is taken, so tests are recorded in the order of their times. Returns
    ``(test id, send_log id)``."""
    zone = campaigns.local_zone(settings.tz)
    with db.connect() as conn:
        campaigns._begin_immediate(conn)
        now = clock()
        sending = conn.execute(
            "SELECT id FROM campaigns WHERE status = ? ORDER BY id LIMIT 1", (campaigns.SENDING,)
        ).fetchone()
        if sending is not None:
            raise CantSendTest(
                f"Send #{sending[0]} to the list is in progress. Pause it to send a test (then "
                "resume it).",
            )
        last = parse_iso(conn.execute("SELECT MAX(sent_at) FROM test_sends").fetchone()[0])
        wait = _too_soon(now, last, MIN_SECONDS_BETWEEN_TESTS)
        if wait is not None:
            raise CantSendTest(
                f"One test every {MIN_SECONDS_BETWEEN_TESTS} seconds: wait {_seconds(wait)}, "
                "then try again.",
                rate_limited=True,
            )
        spacing = 60.0 / settings.send_rate_per_minute
        last_send = parse_iso(conn.execute("SELECT MAX(started_at) FROM send_log").fetchone()[0])
        wait = _too_soon(now, last_send, spacing)
        if wait is not None:
            raise CantSendTest(
                f"A message went out moments ago, and sends are spaced {spacing:g} seconds "
                f"apart: wait {_seconds(wait)}, then try again.",
                rate_limited=True,
            )
        day = conn.execute(
            "SELECT COUNT(*) FROM test_sends WHERE sent_at > ?", (iso(now - DAILY_WINDOW),)
        ).fetchone()[0]
        if day >= MAX_TESTS_PER_DAY:
            raise CantSendTest(
                f"At most {MAX_TESTS_PER_DAY} test sends per 24 hours; try again later.",
                rate_limited=True,
            )
        counted = conn.execute(
            "SELECT COUNT(*) FROM send_log WHERE started_at > ? "
            "AND (outcome IS NULL OR outcome = 'sent')",
            (iso(now - DAILY_WINDOW),),
        ).fetchone()[0]
        if counted >= settings.daily_send_cap:
            raise CantSendTest(
                f"The daily limit of {settings.daily_send_cap} messages per 24 hours has been "
                "reached (test sends count too). Try again later.",
                rate_limited=True,
            )
        throttle = campaigns.throttle_until(conn)
        if throttle is not None and throttle[1] > now:
            raise CantSendTest(
                "Microsoft asked to wait before sending more; try again after "
                f"{campaigns.local_time(throttle[1], zone)}.",
                rate_limited=True,
            )
        log_id = conn.execute(
            "INSERT INTO send_log (campaign_id, row_no, started_at) VALUES (NULL, NULL, ?)",
            (iso(now),),
        ).lastrowid
        test_id = conn.execute(
            "INSERT INTO test_sends (draft_id, content_hash, sent_at, to_addr, log_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (draft_id, digest, iso(now), to, log_id),
        ).lastrowid
    assert test_id is not None and log_id is not None
    return test_id, log_id


def _finish(db: Database, test_id: int, log_id: int, result: SendResult, now: datetime) -> None:
    outcome = "sent" if result.ok else None if result.outcome_unknown else "failed"
    with db.connect() as conn:
        conn.execute("UPDATE send_log SET outcome = ? WHERE id = ?", (outcome, log_id))
        conn.execute(
            "UPDATE test_sends SET ok = ?, outcome_unknown = ?, provider_id = ?, error = ? "
            "WHERE id = ?",
            (
                int(result.ok),
                int(not result.ok and result.outcome_unknown),
                result.provider_id,
                result.error,
                test_id,
            ),
        )
        if result.throttled:
            campaigns.set_throttle(conn, now, now + timedelta(seconds=result.retry_after or 0.0))


def build_test(
    settings: Settings,
    c: DraftContent,
    values: dict[str, str],
    *,
    to: str,
    sender: Sender | None,
) -> bytes:
    """The test email: ``values``' merge fields, addressed (and unsubscribe-linked) to ``to``,
    subject prefixed. Raises ``MessageError``."""
    rendered = compose.render_message(
        c,
        values,
        public_base_url=settings.public_base_url,
        unsubscribe_secret=settings.unsubscribe_secret,
        unsubscribe_for=to,
    )
    rendered = dataclasses.replace(rendered, subject=TEST_SUBJECT_PREFIX + rendered.subject)
    return build_mime(
        rendered,
        to_addr=to,
        to_name=sender.name if sender is not None else None,
        from_addr=sender.address if sender is not None else None,
        from_name=c.business_name.strip() or (sender.name if sender is not None else None),
    )


async def send_test(
    db: Database,
    settings: Settings,
    transport: Transport,
    *,
    draft_id: str,
    content: DraftContent,
    values: dict[str, str],
    sender: Sender | None,
    clock: Callable[[], datetime],
) -> SentTest:
    """Send one test of ``content`` to Jake. Raises ``CantSendTest`` (not connected, a limit
    reached, or the message can't be built); otherwise returns the recorded test, whose ``ok``
    says whether Microsoft (or the outbox) accepted it."""
    to = own_address(settings, sender)
    if to is None:
        raise CantSendTest(NOT_CONNECTED)
    digest = compose.content_hash(content)
    try:
        mime = await asyncio.to_thread(build_test, settings, content, values, to=to, sender=sender)
    except MessageError as exc:
        raise CantSendTest(f"The test email can't be built: {exc}") from None
    test_id, log_id = await asyncio.to_thread(_reserve, db, settings, draft_id, digest, to, clock)
    try:
        result = await transport.send(
            mime, recipient=to, expected_sender=sender.address if sender is not None else None
        )
    except MessageError as exc:
        result = SendResult.permanent(str(exc))
    except Exception as exc:
        log.exception("Transport error during a test send")
        result = SendResult.unknown(f"Unexpected error while sending: {exc}")
    # The throttle (if any) is timed from when Microsoft answered.
    await asyncio.to_thread(_finish, db, test_id, log_id, result, clock())
    test = await asyncio.to_thread(get_test, db, test_id)
    assert test is not None
    return test
