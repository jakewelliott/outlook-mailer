"""The send worker: pacing, daily cap, retries, stops, opt-outs, exactly-once, controls, clean-up.

Driven step by step with a fake clock (``SendWorker.step`` returns the wait), so nothing sleeps.
"""

import asyncio
import csv
import dataclasses
import io
import itertools
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from pathlib import Path

import pytest

from app import campaigns, compose, drafts, sender
from app.campaigns import CampaignError
from app.compose import DraftContent
from app.config import Settings, load_settings
from app.db import Database
from app.message import MessageError
from app.recipients import Recipient
from app.sender import SendWorker
from app.transport import MockTransport, SendResult
from tests.conftest import VALID_ENV

T0 = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)
CONTENT = DraftContent(
    subject="Merry Christmas, {{first_name|Friend}}",
    preheader="News for {{first_name|you}}",
    body="Dear {{first_name|Friend}},\n\n**Thanks** for a great year.",
    business_name="The Calico Barn",
    postal_address="123 Barn Road\nSpringfield, NC 27000",
)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


Decide = Callable[[str, int], SendResult | None]


class Env:
    def __init__(
        self,
        tmp_path: Path,
        *,
        rate: int = 6,
        cap: int = 250,
        decide: Decide | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.created_with = "mock"
        self.clock = FakeClock()
        self.settings: Settings = load_settings(
            {
                **VALID_ENV,
                "DATA_DIR": str(tmp_path),
                "SEND_RATE_PER_MINUTE": str(rate),
                "DAILY_SEND_CAP": str(cap),
            }
        )
        self.db = Database(tmp_path / "app.db")
        self.db.init()
        self.outbox = tmp_path / "outbox"
        self.transport = MockTransport(self.outbox, decide=decide, clock=self.clock)
        self.worker = SendWorker(self.db, self.settings, self.transport, clock=self.clock)

    def campaign(self, emails: list[str], names: list[str] | None = None) -> int:
        draft = drafts.create_draft(self.db, self.tmp_path, "list.csv", b"Email\nx@example.com\n")
        names = names or [f"Name{i}" for i in range(len(emails))]
        recipients = [
            Recipient(i + 2, e, n) for i, (e, n) in enumerate(zip(emails, names, strict=True))
        ]
        self.draft_id = draft.id
        return campaigns.create_campaign(
            self.db, draft.id, CONTENT, recipients, self.clock(), transport=self.created_with
        )

    def step(self) -> float | None:
        return asyncio.run(self.worker.step())

    def drive(self, until: Callable[[], bool] | None = None, max_steps: int = 500) -> None:
        """Step, advancing the clock by each returned wait, until idle (or ``until`` is true
        after a step; the clock is then not advanced)."""

        async def go() -> None:
            for _ in range(max_steps):
                delay = await self.worker.step()
                if delay is None or (until is not None and until()):
                    return
                self.clock.advance(delay)
            raise AssertionError("the worker did not settle")

        asyncio.run(go())

    def status(self, cid: int) -> str:
        c = campaigns.get_campaign(self.db, cid)
        assert c is not None
        return c.status

    def recipients(self, cid: int) -> dict[str, dict[str, object]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM campaign_recipients WHERE campaign_id = ? ORDER BY row_no", (cid,)
            ).fetchall()
        return {r["email"]: dict(r) for r in rows}

    def send_times(self) -> list[datetime]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT started_at FROM send_log ORDER BY id").fetchall()
        return [datetime.fromisoformat(r[0]) for r in rows]

    def outbox_to(self) -> list[str]:
        files = sorted(self.outbox.glob("*.eml")) if self.outbox.exists() else []
        return [str(message_from_bytes(f.read_bytes(), policy=policy.default)["To"]) for f in files]

    def csv_exists(self) -> bool:
        draft = drafts.get_draft(self.db, self.draft_id)
        assert draft is not None
        return drafts.read_csv(self.tmp_path, draft) is not None

    def events(self, cid: int) -> list[str]:
        return [e.message for e in campaigns.events(self.db, cid, limit=1000)]


EMAILS = ["ada@example.com", "bob@example.com", "cy@example.com"]


# --- the happy path, pacing, outbox ------------------------------------------------------------


def test_sends_everyone_paced_then_finishes(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=6)
    cid = env.campaign(EMAILS, ["Ada", "", "Cy"])
    assert env.status(cid) == campaigns.QUEUED
    env.drive()
    assert env.status(cid) == campaigns.FINISHED
    times = env.send_times()
    assert [b - a for a, b in itertools.pairwise(times)] == [timedelta(seconds=10)] * 2
    rows = env.recipients(cid)
    assert {r["status"] for r in rows.values()} == {campaigns.R_SENT}
    assert all(r["attempts"] == 1 and r["sent_at"] and r["provider_id"] for r in rows.values())
    # Exactly one .eml per recipient, each to that recipient only.
    assert env.outbox_to() == ["Ada <ada@example.com>", "bob@example.com", "Cy <cy@example.com>"]
    assert not env.csv_exists()
    assert env.step() is None  # idle


def test_pacing_waits_between_sends(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=4)  # one every 15 s
    env.campaign(EMAILS)
    assert env.step() == 0.0  # first send at once
    assert env.step() == pytest.approx(15.0)
    env.clock.advance(5)
    assert env.step() == pytest.approx(10.0)  # still waiting, nothing sent
    assert len(env.send_times()) == 1


def test_message_is_rendered_for_each_recipient(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.campaign(["ada@example.com"], ["Ada"])
    env.drive()
    (eml,) = env.outbox.glob("*.eml")
    msg = message_from_bytes(eml.read_bytes(), policy=policy.default)
    assert msg["Subject"] == "Merry Christmas, Ada"
    assert msg["From"].addresses[0].display_name == "The Calico Barn"
    assert msg["List-Unsubscribe"].startswith("<https://mail.example.com/u/")
    assert msg.get_all("Cc") is None and msg.get_all("Bcc") is None


# --- daily cap ---------------------------------------------------------------------------------


def test_daily_cap_pauses_and_resumes_by_itself(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60, cap=2)
    cid = env.campaign(EMAILS)
    env.drive(until=lambda: env.status(cid) == campaigns.PAUSED_DAILY_CAP)
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None
    first = env.send_times()[0]
    assert len(env.send_times()) == 2
    assert c.resume_at == first + timedelta(hours=24)
    wait = env.step()
    assert wait == pytest.approx((c.resume_at - env.clock()).total_seconds())
    assert any("Daily limit of 2" in e for e in env.events(cid))
    env.clock.advance(wait or 0)
    env.drive()
    assert env.status(cid) == campaigns.FINISHED
    assert env.send_times()[2] == first + timedelta(hours=24)
    assert any("room again" in e for e in env.events(cid))


def test_daily_cap_counts_all_campaigns(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60, cap=3)
    first = env.campaign(EMAILS[:2])
    env.drive()
    assert env.status(first) == campaigns.FINISHED
    second = env.campaign(["dee@example.com", "eve@example.com"])
    env.drive(until=lambda: env.status(second) == campaigns.PAUSED_DAILY_CAP)
    assert len(env.send_times()) == 3
    assert env.recipients(second)["eve@example.com"]["status"] == campaigns.R_PENDING


def test_failed_sends_do_not_use_the_cap(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60, cap=1, decide=lambda r, n: SendResult.permanent("no"))
    cid = env.campaign(EMAILS[:2])
    env.drive()
    assert env.status(cid) == campaigns.FINISHED
    assert len(sender.sent_in_window(env.db, env.clock())) == 0


# --- retries and failures ----------------------------------------------------------------------


def test_retry_backoff(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60, decide=lambda r, n: SendResult.retry("timeout") if n < 3 else None)
    cid = env.campaign(["ada@example.com"])
    assert env.step() == 0.0
    row = env.recipients(cid)["ada@example.com"]
    assert row["status"] == campaigns.R_PENDING and row["attempts"] == 1
    assert row["next_attempt_at"] == campaigns.iso(T0 + timedelta(seconds=30))
    assert env.step() == pytest.approx(30.0)
    env.clock.advance(30)
    assert env.step() == 0.0
    row = env.recipients(cid)["ada@example.com"]
    assert row["next_attempt_at"] == campaigns.iso(env.clock() + timedelta(seconds=120))
    env.drive()
    row = env.recipients(cid)["ada@example.com"]
    assert row["status"] == campaigns.R_SENT and row["attempts"] == 3
    assert env.status(cid) == campaigns.FINISHED
    assert len(env.outbox_to()) == 1


def test_retry_after_holds_every_recipient(tmp_path: Path) -> None:
    """Retry-After: no transport call for anyone before it, the throttled recipient retries
    then, and the throttle is neither an attempt nor a consecutive failure."""
    env = Env(
        tmp_path,
        rate=60,
        decide=lambda r, n: (
            SendResult.retry("429 throttled", retry_after=300)
            if r == "ada@example.com" and n == 1
            else None
        ),
    )
    cid = env.campaign(EMAILS)
    assert env.step() == 0.0  # ada: throttled
    not_before = T0 + timedelta(seconds=300)
    row = env.recipients(cid)["ada@example.com"]
    assert row["status"] == campaigns.R_PENDING and row["attempts"] == 0
    assert row["next_attempt_at"] == campaigns.iso(not_before)
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.consecutive_failures == 0 and c.consecutive_throttles == 1
    # bob is due, but nothing goes out before the Retry-After time.
    assert env.step() == pytest.approx(300.0)
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.next_send_at == not_before
    env.clock.advance(120)
    assert env.step() == pytest.approx(180.0)
    assert len(env.send_times()) == 1
    env.drive()
    times = env.send_times()
    assert times[1] == not_before and all(t >= not_before for t in times[1:])
    assert env.status(cid) == campaigns.FINISHED
    assert env.recipients(cid)["ada@example.com"]["status"] == campaigns.R_SENT
    assert env.recipients(cid)["ada@example.com"]["attempts"] == 1
    assert any("asked to wait" in e for e in env.events(cid))


def test_retry_after_survives_a_restart(tmp_path: Path) -> None:
    env = Env(
        tmp_path,
        rate=60,
        decide=lambda r, n: SendResult.retry("throttled", retry_after=600) if n == 1 else None,
    )
    cid = env.campaign(EMAILS)
    env.step()
    not_before = T0 + timedelta(seconds=600)
    # Restart: a new Database object and worker on the same file, a transport that would succeed.
    env.clock.advance(60)
    env.db.close()
    db = Database(tmp_path / "app.db")
    db.init()
    calls: list[datetime] = []

    def decide(recipient: str, attempt: int) -> SendResult | None:
        calls.append(env.clock())
        return None

    fresh = MockTransport(env.outbox, decide=decide, clock=env.clock)
    env.db = db
    env.worker = SendWorker(db, env.settings, fresh, clock=env.clock)
    assert env.step() == pytest.approx(540.0)
    env.drive()
    assert env.status(cid) == campaigns.FINISHED
    assert calls and min(calls) >= not_before
    assert all(t >= not_before for t in env.send_times()[1:])


def test_retry_after_ignores_a_clock_set_back(tmp_path: Path) -> None:
    env = Env(
        tmp_path, decide=lambda r, n: SendResult.retry("busy", retry_after=60) if n == 1 else None
    )
    env.campaign(EMAILS[:2])
    env.step()
    env.clock.advance(-3600)  # the clock jumps back an hour: still at most 60 s to wait
    assert env.step() == pytest.approx(60.0)


def test_repeated_throttling_stops_the_campaign(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60, decide=lambda r, n: SendResult.retry("busy", retry_after=5))
    cid = env.campaign(EMAILS)
    env.drive()
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.status == campaigns.STOPPED_ERROR
    assert c.last_error and "asked to wait 10 times in a row" in c.last_error
    assert len(env.send_times()) == sender.MAX_CONSECUTIVE_THROTTLES
    rows = env.recipients(cid)
    assert {r["status"] for r in rows.values()} == {campaigns.R_PENDING}
    assert all(r["attempts"] == 0 for r in rows.values())
    campaigns.resume(env.db, cid, env.clock(), transport="mock")
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.consecutive_throttles == 0


def test_other_recipients_go_while_one_waits_to_retry(tmp_path: Path) -> None:
    env = Env(
        tmp_path,
        rate=60,
        decide=lambda r, n: SendResult.retry("busy") if r == "ada@example.com" and n == 1 else None,
    )
    cid = env.campaign(EMAILS)
    env.drive()
    order = [p.name.split("-", 2)[2] for p in sorted(env.outbox.glob("*.eml"))]
    assert order[:2] == ["bob@example.com.eml", "cy@example.com.eml"]
    assert env.recipients(cid)["ada@example.com"]["status"] == campaigns.R_SENT


def test_retries_give_up_after_three_attempts(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60, decide=lambda r, n: SendResult.retry("still down"))
    cid = env.campaign(["ada@example.com"])
    env.drive()
    row = env.recipients(cid)["ada@example.com"]
    assert row["status"] == campaigns.R_FAILED
    assert row["attempts"] == sender.MAX_ATTEMPTS == 3
    assert env.transport.attempts["ada@example.com"] == 3
    assert env.status(cid) == campaigns.FINISHED


def test_permanent_failure_is_not_retried(tmp_path: Path) -> None:
    env = Env(
        tmp_path,
        decide=lambda r, n: SendResult.permanent("rejected") if r == "bob@example.com" else None,
    )
    cid = env.campaign(EMAILS)
    env.drive()
    rows = env.recipients(cid)
    assert rows["bob@example.com"]["status"] == campaigns.R_FAILED
    assert rows["bob@example.com"]["attempts"] == 1
    assert rows["bob@example.com"]["last_error"] == "rejected"
    assert env.transport.attempts["bob@example.com"] == 1
    assert rows["cy@example.com"]["status"] == campaigns.R_SENT
    assert env.status(cid) == campaigns.FINISHED


def test_auth_error_stops_the_campaign(tmp_path: Path) -> None:
    signed_in = {"ok": False}

    def decide(recipient: str, attempt: int) -> SendResult | None:
        return None if signed_in["ok"] else SendResult.auth("token expired")

    env = Env(tmp_path, decide=decide)
    cid = env.campaign(EMAILS)
    env.drive()
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.status == campaigns.STOPPED_ERROR
    assert c.last_error and "token expired" in c.last_error
    assert c.last_error.startswith(sender.AUTH_STOP_MESSAGE)
    row = env.recipients(cid)["ada@example.com"]
    assert row["status"] == campaigns.R_PENDING and row["attempts"] == 0
    assert env.step() is None  # nothing more is tried
    assert env.transport.attempts["ada@example.com"] == 1

    signed_in["ok"] = True
    campaigns.resume(env.db, cid, env.clock(), transport="mock")
    env.drive()
    assert env.status(cid) == campaigns.FINISHED
    assert len(env.outbox_to()) == 3


def test_consecutive_failures_stop_the_campaign(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60, decide=lambda r, n: SendResult.permanent("bad"))
    emails = [f"p{i}@example.com" for i in range(7)]
    cid = env.campaign(emails)
    env.drive()
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.status == campaigns.STOPPED_ERROR
    assert c.last_error and "5 send attempts in a row failed" in c.last_error
    n = campaigns.counts(env.db, cid)
    assert n[campaigns.R_FAILED] == sender.MAX_CONSECUTIVE_FAILURES == 5
    assert n[campaigns.R_PENDING] == 2
    # Resuming resets the count.
    campaigns.resume(env.db, cid, env.clock(), transport="mock")
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.consecutive_failures == 0


def test_a_success_resets_the_failure_count(tmp_path: Path) -> None:
    def decide(recipient: str, attempt: int) -> SendResult | None:
        return None if recipient.startswith("ok") else SendResult.permanent("bad")

    env = Env(tmp_path, rate=60, decide=decide)
    emails = [f"bad{i}@example.com" for i in range(4)] + ["ok@example.com"]
    emails += [f"bad{i}@example.org" for i in range(4)]
    cid = env.campaign(emails)
    env.drive()
    assert env.status(cid) == campaigns.FINISHED


class Exploding(MockTransport):
    async def send(
        self, mime: bytes, *, recipient: str, expected_sender: str | None = None
    ) -> SendResult:
        self.attempts[recipient] += 1
        if self.attempts[recipient] == 1:
            raise RuntimeError("socket closed")
        return await super().send(mime, recipient=recipient)


def test_transport_exception_is_outcome_unknown(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60)
    env.worker.transport = Exploding(env.outbox, clock=env.clock)
    cid = env.campaign(["ada@example.com", "bob@example.com"])
    env.drive()
    rows = env.recipients(cid)
    ada = rows["ada@example.com"]
    assert ada["status"] == campaigns.R_FAILED and ada["attempts"] == 1
    assert "Unexpected error while sending: socket closed" in str(ada["last_error"])
    assert str(ada["last_error"]).endswith(campaigns.UNKNOWN_SUFFIX)
    assert env.worker.transport.attempts["ada@example.com"] == 1  # never retried
    assert rows["bob@example.com"]["status"] == campaigns.R_FAILED  # bob's first call raised too
    assert env.status(cid) == campaigns.FINISHED


def test_unknown_outcome(tmp_path: Path) -> None:
    env = Env(
        tmp_path,
        rate=60,
        cap=2,
        decide=lambda r, n: SendResult.unknown("read timeout") if r == "ada@example.com" else None,
    )
    cid = env.campaign(EMAILS)
    env.drive(until=lambda: env.status(cid) == campaigns.PAUSED_DAILY_CAP)
    ada = env.recipients(cid)["ada@example.com"]
    assert ada["status"] == campaigns.R_FAILED and ada["attempts"] == 1
    assert ada["last_error"] == (
        "outcome unknown (read timeout) — may or may not have been delivered; check Sent Items"
    )
    assert env.transport.attempts["ada@example.com"] == 1
    with env.db.connect() as conn:
        outcomes = [r[0] for r in conn.execute("SELECT outcome FROM send_log ORDER BY id")]
    assert outcomes == [None, "sent"]
    # It counts towards the cap (2): cy waits for the window.
    assert len(sender.sent_in_window(env.db, env.clock())) == 2
    assert env.recipients(cid)["cy@example.com"]["status"] == campaigns.R_PENDING


def test_unknown_outcomes_count_as_consecutive_failures(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60, decide=lambda r, n: SendResult.unknown("connection reset"))
    cid = env.campaign([f"p{i}@example.com" for i in range(7)])
    env.drive()
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.status == campaigns.STOPPED_ERROR
    assert campaigns.counts(env.db, cid)[campaigns.R_FAILED] == 5


def test_unknown_with_stop_reason_stops_and_is_never_resent(tmp_path: Path) -> None:
    blocked = {"on": True}

    def decide(recipient: str, attempt: int) -> SendResult | None:
        if blocked["on"]:
            return SendResult.unknown("HTTP 500 Blocked", stop_reason="The account is blocked.")
        return None

    env = Env(tmp_path, rate=60, cap=3, decide=decide)
    cid = env.campaign(EMAILS)
    env.drive()
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.status == campaigns.STOPPED_ERROR
    assert c.last_error == (
        f"{sender.AUTH_STOP_MESSAGE} The account is blocked. Row 2 (ada@example.com) may or may "
        "not have received the email and won't be sent it again; check Sent Items. "
        f"{sender.AUTH_STOP_AFTER}"
    )
    assert c.consecutive_failures == 1
    rows = env.recipients(cid)
    ada = rows["ada@example.com"]
    assert ada["status"] == campaigns.R_FAILED and ada["attempts"] == 1
    assert ada["next_attempt_at"] is None
    assert ada["last_error"] == (
        "outcome unknown (HTTP 500 Blocked) — may or may not have been delivered; check Sent Items"
    )
    assert rows["bob@example.com"]["status"] == campaigns.R_PENDING
    assert env.transport.attempts == {"ada@example.com": 1}  # nobody else tried
    assert env.step() is None
    assert len(sender.sent_in_window(env.db, env.clock())) == 1  # counts against the cap

    blocked["on"] = False
    campaigns.resume(env.db, cid, env.clock(), transport="mock")
    env.drive()
    assert env.status(cid) == campaigns.FINISHED
    assert env.transport.attempts["ada@example.com"] == 1  # never resent
    assert env.outbox_to() == ["Name1 <bob@example.com>", "Name2 <cy@example.com>"]
    assert env.recipients(cid)["ada@example.com"]["status"] == campaigns.R_FAILED
    # The cap counted ada's unknown send: 3 in the window.
    assert len(sender.sent_in_window(env.db, env.clock())) == 3


def test_unknown_with_stop_reason_while_paused_stays_paused(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS)

    def decide(recipient: str, attempt: int) -> SendResult | None:
        campaigns.pause(env.db, cid, env.clock())  # Jake pauses during the call
        return SendResult.unknown("HTTP 502", stop_reason="The account is blocked.")

    env.transport.decide = decide
    env.drive()
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.status == campaigns.PAUSED
    assert c.last_error and "The account is blocked." in c.last_error
    assert env.recipients(cid)["ada@example.com"]["status"] == campaigns.R_FAILED
    assert env.transport.attempts == {"ada@example.com": 1}


def test_render_exception_is_a_failed_attempt(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60)

    def boom(*args: object) -> bytes:
        raise RuntimeError("template broke")

    env.worker._build = boom  # type: ignore[method-assign]
    cid = env.campaign(["ada@example.com"])
    env.drive()
    row = env.recipients(cid)["ada@example.com"]
    assert row["status"] == campaigns.R_FAILED
    assert row["attempts"] == 3
    assert "template broke" in str(row["last_error"])
    assert env.send_times() == []  # the transport was never called
    assert env.status(cid) == campaigns.FINISHED


def test_worker_run_survives_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path)
    calls = []

    async def failing_step() -> float | None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        env.worker._stopping = True
        return None

    monkeypatch.setattr(env.worker, "step", failing_step)
    monkeypatch.setattr(sender, "ERROR_PAUSE_SECONDS", 0.01)
    asyncio.run(env.worker.run())
    assert len(calls) == 2


# --- opt-outs at send time ---------------------------------------------------------------------


def test_opt_out_added_mid_campaign_is_skipped(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS)
    assert env.step() == 0.0  # ada sent
    (tmp_path / "unsubscribed.txt").write_text(
        "BOB@Example.com\t2026-12-01T10:00:00-05:00\n", encoding="utf-8"
    )
    env.drive()
    rows = env.recipients(cid)
    assert rows["bob@example.com"]["status"] == campaigns.R_SKIPPED
    assert rows["bob@example.com"]["attempts"] == 0
    assert env.outbox_to() == ["Name0 <ada@example.com>", "Name2 <cy@example.com>"]
    assert "bob@example.com" not in env.transport.attempts


def test_opt_out_in_punycode_matches_unicode_address(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(["jose@bücher.example", "ann@example.com"])
    (tmp_path / "unsubscribed.txt").write_text(
        "jose@xn--bcher-kva.example\t2026-12-01T10:00:00-05:00\n", encoding="utf-8"
    )
    env.drive()
    assert env.recipients(cid)["jose@bücher.example"]["status"] == campaigns.R_SKIPPED


def test_opt_out_in_unicode_matches_punycode_address(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(["ann@xn--bcher-kva.example"])
    (tmp_path / "unsubscribed.txt").write_text("ann@BÜCHER.example\n", encoding="utf-8")
    env.drive()
    assert env.recipients(cid)["ann@xn--bcher-kva.example"]["status"] == campaigns.R_SKIPPED


def test_opt_out_checked_after_rendering(tmp_path: Path) -> None:
    """The list is read again right before the transport is called."""
    env = Env(tmp_path, rate=60)
    cid = env.campaign(["ada@example.com"])
    build = env.worker._build

    def build_then_opt_out(c: campaigns.Campaign, row: object, account: object) -> bytes:
        mime = build(c, row, account)  # type: ignore[arg-type]
        (tmp_path / "unsubscribed.txt").write_text("ada@example.com\n", encoding="utf-8")
        return mime

    env.worker._build = build_then_opt_out  # type: ignore[method-assign]
    env.drive()
    assert env.recipients(cid)["ada@example.com"]["status"] == campaigns.R_SKIPPED
    assert env.outbox_to() == []


# --- exactly-once across a restart -------------------------------------------------------------


class Hanging(MockTransport):
    """The first send never returns (the app dies while waiting for Microsoft)."""

    def __init__(self, outbox: Path) -> None:
        super().__init__(outbox)
        self.started = asyncio.Event()

    async def send(
        self, mime: bytes, *, recipient: str, expected_sender: str | None = None
    ) -> SendResult:
        self.attempts[recipient] += 1
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_restart_mid_send_marks_failed_and_never_resends(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS)
    hanging = Hanging(env.outbox)
    env.worker.transport = hanging

    async def crash() -> None:
        task = asyncio.create_task(env.worker.step())
        await hanging.started.wait()
        task.cancel()  # the process dies mid-send
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(crash())
    assert env.recipients(cid)["ada@example.com"]["status"] == campaigns.R_SENDING

    # Restart: recovery, then a fresh worker and transport.
    assert campaigns.recover_interrupted(env.db, env.clock()) == 1
    row = env.recipients(cid)["ada@example.com"]
    assert row["status"] == campaigns.R_FAILED
    assert row["last_error"] == campaigns.INTERRUPTED_ERROR
    fresh = MockTransport(env.outbox, clock=env.clock)
    env.worker = SendWorker(env.db, env.settings, fresh, clock=env.clock)
    env.clock.advance(60)
    env.drive()
    assert env.status(cid) == campaigns.FINISHED
    assert "ada@example.com" not in fresh.attempts
    assert env.outbox_to() == ["Name1 <bob@example.com>", "Name2 <cy@example.com>"]
    # The interrupted send still counts towards the daily cap (it may have gone out).
    assert len(sender.sent_in_window(env.db, env.clock())) == 3


# --- the transport a campaign was started with ------------------------------------------------


def graph_settings(env: Env) -> Settings:
    return load_settings(
        {
            **VALID_ENV,
            "MAIL_TRANSPORT": "graph",
            "MS_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
            "DATA_DIR": str(env.tmp_path),
            "SEND_RATE_PER_MINUTE": "60",
        }
    )


@pytest.mark.parametrize("state", ["queued", "paused", "paused_daily_cap", "stopped_error"])
def test_other_transport_never_sends_a_campaign(tmp_path: Path, state: str) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS)
    with env.db.connect() as conn:
        conn.execute(
            "UPDATE campaigns SET status = ?, resume_at = ? WHERE id = ?",
            (state, campaigns.iso(T0 - timedelta(hours=1)), cid),
        )
    # The app restarts with MAIL_TRANSPORT=graph (a transport that would send).
    real = MockTransport(tmp_path / "real", clock=env.clock)
    env.worker = SendWorker(env.db, graph_settings(env), real, clock=env.clock)
    env.clock.advance(86400)
    assert env.step() is None
    assert not real.attempts and env.send_times() == []
    assert env.status(cid) == state
    if state in campaigns.CAN_RESUME:
        with pytest.raises(CampaignError, match="started in mock mode; cancel it"):
            campaigns.resume(env.db, cid, env.clock(), transport="graph")
        assert env.status(cid) == state
    campaigns.cancel(env.db, tmp_path, cid, env.clock())
    assert env.status(cid) == campaigns.CANCELLED
    assert not real.attempts


def test_campaign_records_its_transport(tmp_path: Path) -> None:
    env = Env(tmp_path)
    cid = env.campaign(EMAILS[:1])
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.transport == "mock"
    assert campaigns.transport_mismatch(c, "mock") is None


# --- recording outcomes ------------------------------------------------------------------------


def test_record_write_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sender, "RECORD_RETRY_SECONDS", (0.0, 0.0, 0.0))
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS[:2])
    record = env.worker._record
    failures = {"left": 2}

    def flaky(*args: object, **kwargs: object) -> None:
        if failures["left"]:
            failures["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        record(*args, **kwargs)  # type: ignore[arg-type]

    env.worker._record = flaky  # type: ignore[method-assign]
    env.drive()
    assert env.status(cid) == campaigns.FINISHED
    assert {r["status"] for r in env.recipients(cid).values()} == {campaigns.R_SENT}
    assert len(env.outbox_to()) == 2


def test_unsaved_outcome_blocks_sending_and_finishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sender, "RECORD_RETRY_SECONDS", (0.0,))
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS[:2])
    record = env.worker._record
    broken = {"on": True}

    def maybe(*args: object, **kwargs: object) -> None:
        if broken["on"]:
            raise sqlite3.OperationalError("disk I/O error")
        record(*args, **kwargs)  # type: ignore[arg-type]

    env.worker._record = maybe  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError):
        env.step()
    assert env.recipients(cid)["ada@example.com"]["status"] == campaigns.R_SENDING
    env.clock.advance(60)
    with pytest.raises(sqlite3.OperationalError):
        env.step()  # still can't save it: nothing else is sent
    assert len(env.send_times()) == 1
    broken["on"] = False
    env.drive()
    rows = env.recipients(cid)
    assert rows["ada@example.com"]["status"] == campaigns.R_SENT  # the real outcome, saved late
    assert rows["bob@example.com"]["status"] == campaigns.R_SENT
    assert env.status(cid) == campaigns.FINISHED


def test_never_finishes_while_a_recipient_is_sending(tmp_path: Path) -> None:
    """A ``sending`` row with no outcome held in memory stops the campaign until a restart."""
    env = Env(tmp_path)
    cid = env.campaign(EMAILS[:1])
    with env.db.connect() as conn:
        conn.execute(
            "UPDATE campaign_recipients SET status = ? WHERE campaign_id = ?",
            (campaigns.R_SENDING, cid),
        )
    assert env.step() is None
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.status == campaigns.STOPPED_ERROR
    assert c.last_error and "Row 2 (ada@example.com)" in c.last_error
    assert env.csv_exists()
    assert env.send_times() == []


def test_unsaved_outcome_is_given_up_after_n_tries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sender, "RECORD_RETRY_SECONDS", (0.0,))  # 2 tries in _save
    monkeypatch.setattr(sender, "RECORD_GIVE_UP_AFTER", 4)
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS[:2])
    tries = {"n": 0}

    def broken(*args: object, **kwargs: object) -> None:
        tries["n"] += 1
        raise sqlite3.OperationalError("disk I/O error")

    env.worker._record = broken  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError):
        env.step()  # tries 1-2
    env.clock.advance(60)
    with pytest.raises(sqlite3.OperationalError):
        env.step()  # try 3
    assert len(env.send_times()) == 1
    env.clock.advance(60)
    assert env.step() is None  # try 4: given up, and the campaign is stopped
    assert tries["n"] == 4
    assert "GIVING UP" in caplog.text
    assert env.worker._unrecorded is None
    assert env.status(cid) == campaigns.STOPPED_ERROR
    assert env.recipients(cid)["ada@example.com"]["status"] == campaigns.R_SENDING
    env.clock.advance(60)
    assert env.step() is None  # no retrying forever, nothing more sent
    assert tries["n"] == 4 and len(env.send_times()) == 1

    # Restart: the row is reported as interrupted, never resent; the rest goes after resume.
    assert campaigns.recover_interrupted(env.db, env.clock()) == 1
    env.worker = SendWorker(env.db, env.settings, env.transport, clock=env.clock)
    campaigns.resume(env.db, cid, env.clock(), transport="mock")
    env.drive()
    rows = env.recipients(cid)
    assert rows["ada@example.com"]["status"] == campaigns.R_FAILED
    assert rows["ada@example.com"]["last_error"] == campaigns.INTERRUPTED_ERROR
    assert rows["bob@example.com"]["status"] == campaigns.R_SENT
    assert env.transport.attempts["ada@example.com"] == 1
    assert env.status(cid) == campaigns.FINISHED


# Fix A3: an outcome written again (its write committed, then an error was raised) is a no-op.
REAPPLY_RESULTS = {
    "sent": SendResult.success(),
    "retry": SendResult.retry("timeout"),
    "throttled": SendResult.retry("429", retry_after=300),
    "permanent": SendResult.permanent("bad"),
    "unknown": SendResult.unknown("timeout"),
    "unknown_stop": SendResult.unknown("500", stop_reason="Account blocked."),
    "auth": SendResult.auth("revoked"),
}
PRE_SEND = {"build_retry", "build_message_error", "signed_out"}
REAPPLY_KINDS = [*REAPPLY_RESULTS, *sorted(PRE_SEND)]


def reapply_env(tmp_path: Path, kind: str) -> Env:
    """Ada's first attempt has the ``kind`` outcome; everyone else (and ada later) succeeds."""
    result = REAPPLY_RESULTS.get(kind)

    def decide(r: str, n: int) -> SendResult | None:
        return result if r == "ada@example.com" and n == 1 else None

    env = Env(tmp_path, rate=60, decide=decide)
    if kind in ("build_retry", "build_message_error"):
        real_build = env.worker._build
        exc = RuntimeError("template broke") if kind == "build_retry" else MessageError("bad")
        built = {"ada": 0}

        def build(c: object, row: sender._Row, account: object) -> bytes:
            if row.email == "ada@example.com" and not built["ada"]:
                built["ada"] += 1
                raise exc
            return real_build(c, row, account)  # type: ignore[arg-type]

        env.worker._build = build  # type: ignore[method-assign]
    elif kind == "signed_out":
        env.worker.sender = lambda: None
    return env


def commit_then_raise(env: Env, times: int) -> dict[str, int]:
    """The first ``times`` writes of an outcome commit and *then* raise (e.g. closing the
    connection failed), so the worker believes they failed and writes them again."""
    record = env.worker._record
    calls = {"n": 0}

    def record_then_fail(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        record(*args, **kwargs)  # type: ignore[arg-type]
        if calls["n"] <= times:
            raise sqlite3.OperationalError("disk I/O error")

    env.worker._record = record_then_fail  # type: ignore[method-assign]
    return calls


def snapshot(env: Env) -> dict[str, list[tuple[object, ...]]]:
    """Everything an outcome can change: the campaign's counters, the recipients, the send
    log (the daily cap and pacing), the throttle and the events."""
    queries = {
        "campaigns": "SELECT * FROM campaigns ORDER BY id",  # draft_id dropped below
        "recipients": "SELECT * FROM campaign_recipients ORDER BY campaign_id, row_no",
        "send_log": "SELECT * FROM send_log ORDER BY id",
        "throttle": "SELECT * FROM send_throttle",
        "events": "SELECT campaign_id, at, message FROM campaign_events ORDER BY id",
    }
    with env.db.connect() as conn:
        return {
            k: [tuple(v for c, v in zip(r.keys(), r, strict=True) if c != "draft_id") for r in rows]
            for k, rows in ((k, conn.execute(q).fetchall()) for k, q in queries.items())
        }


@pytest.mark.parametrize("kind", REAPPLY_KINDS)
def test_outcome_rewritten_by_the_save_retry_counts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """``_save``'s retry writes an outcome again after a write that committed but raised: same
    database as a single write (one event, one failure or throttle counted, one stop)."""
    monkeypatch.setattr(sender, "RECORD_RETRY_SECONDS", (0.0,))
    once = reapply_env(tmp_path / "once", kind)
    once.campaign(EMAILS)
    once.step()
    twice = reapply_env(tmp_path / "twice", kind)
    twice.campaign(EMAILS)
    calls = commit_then_raise(twice, times=1)
    twice.step()
    assert calls["n"] == 2  # written, then written again
    assert snapshot(twice) == snapshot(once)


@pytest.mark.parametrize("kind", REAPPLY_KINDS)
def test_outcome_replayed_by_the_next_step_counts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """The outcome held in memory (every try in ``_save`` committed but raised) is replayed by
    the next step: no second event or count. Later, genuinely new attempts record normally."""
    monkeypatch.setattr(sender, "RECORD_RETRY_SECONDS", ())  # one try in _save
    once = reapply_env(tmp_path / "once", kind)
    cid = once.campaign(EMAILS)
    once.step()
    once.step()
    twice = reapply_env(tmp_path / "twice", kind)
    twice.campaign(EMAILS)
    calls = commit_then_raise(twice, times=1)
    with pytest.raises(sqlite3.OperationalError):
        twice.step()
    assert twice.worker._unrecorded is not None
    twice.step()  # the replay, then the plan
    assert calls["n"] >= 2 and twice.worker._unrecorded is None  # replayed (then maybe bob)
    assert snapshot(twice) == snapshot(once)

    for env in (once, twice):
        if env.status(cid) == campaigns.STOPPED_ERROR:
            if kind == "signed_out":
                env.worker.sender = None
            campaigns.resume(env.db, cid, env.clock(), transport="mock")
        env.drive()
    assert snapshot(twice) == snapshot(once)
    assert once.status(cid) == campaigns.FINISHED
    ada = once.recipients(cid)["ada@example.com"]
    if kind in ("retry", "build_retry"):  # retried: both attempts counted, sent the second time
        assert (ada["status"], ada["attempts"]) == (campaigns.R_SENT, 2)


def test_replayed_auth_outcome_does_not_stop_a_resumed_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auth outcome stopped the campaign (committed, then the error), Jake resumed it
    during the worker's error pause, and then the outcome is replayed: it must not stop the
    campaign again nor add events."""
    monkeypatch.setattr(sender, "RECORD_RETRY_SECONDS", ())
    env = reapply_env(tmp_path, "auth")
    cid = env.campaign(EMAILS)
    commit_then_raise(env, times=1)
    with pytest.raises(sqlite3.OperationalError):
        env.step()
    assert env.status(cid) == campaigns.STOPPED_ERROR
    campaigns.resume(env.db, cid, env.clock(), transport="mock")
    stops = [e for e in env.events(cid) if e.startswith("Stopped")]
    env.drive()
    assert env.status(cid) == campaigns.FINISHED
    assert [e for e in env.events(cid) if e.startswith("Stopped")] == stops
    assert {r["status"] for r in env.recipients(cid).values()} == {campaigns.R_SENT}


def test_outcome_whose_write_rolled_back_is_written_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary case the guard must not block: the write fails before committing, so the
    replay is the one that records it."""
    monkeypatch.setattr(sender, "RECORD_RETRY_SECONDS", ())
    once = reapply_env(tmp_path / "once", "retry")
    once.campaign(EMAILS)
    once.step()
    once.step()
    twice = reapply_env(tmp_path / "twice", "retry")
    twice.campaign(EMAILS)
    record = twice.worker._record
    calls = {"n": 0}

    def fail_then_record(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        record(*args, **kwargs)  # type: ignore[arg-type]

    twice.worker._record = fail_then_record  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError):
        twice.step()
    twice.step()
    assert snapshot(twice) == snapshot(once)


# --- shutdown ----------------------------------------------------------------------------------


def _stop_while(env: Env, method: str, *, after: bool) -> None:
    """Run the worker; stop() it while ``method`` (run in a thread) is blocked, before or
    ``after`` calling the real one."""
    real = getattr(env.worker, method)
    entered = threading.Event()
    release = threading.Event()

    def slow(*args: object, **kwargs: object) -> object:
        if not after:
            entered.set()
            assert release.wait(5)
        result = real(*args, **kwargs)
        if after:
            entered.set()
            assert release.wait(5)
        return result

    setattr(env.worker, method, slow)

    async def go() -> None:
        env.worker.start()
        assert await asyncio.to_thread(entered.wait, 5)
        stopping = asyncio.create_task(env.worker.stop())
        await asyncio.sleep(0)  # stop() has set _stopping
        assert env.worker._stopping
        release.set()
        await stopping

    asyncio.run(go())


@pytest.mark.parametrize(
    ("method", "after"),
    [("_plan", True), ("_build", True), ("_mark_sending", False), ("_mark_sending", True)],
    ids=["plan", "build", "before-mark", "after-mark"],
)
def test_no_new_send_after_stop(tmp_path: Path, method: str, after: bool) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS[:1])
    _stop_while(env, method, after=after)
    assert not env.transport.attempts
    row = env.recipients(cid)["ada@example.com"]
    assert row["status"] == campaigns.R_PENDING
    assert row["attempts"] == 0
    assert env.send_times() == []  # nothing counts against the cap or pacing
    assert not any("interrupted" in e or "skipped" in e for e in env.events(cid))
    # The next start sends it normally.
    assert campaigns.recover_interrupted(env.db, env.clock()) == 0
    env.worker = SendWorker(env.db, env.settings, env.transport, clock=env.clock)
    env.drive()
    assert env.recipients(cid)["ada@example.com"]["status"] == campaigns.R_SENT
    assert env.transport.attempts["ada@example.com"] == 1


def test_stop_after_mark_in_a_cancelled_campaign_leaves_not_sent(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS[:1])
    mark = env.worker._mark_sending

    def mark_then_cancel(*args: object, **kwargs: object) -> int | None:
        log_id = mark(*args, **kwargs)  # type: ignore[arg-type]
        campaigns.cancel(env.db, tmp_path, cid, env.clock())  # leaves the 'sending' row
        env.worker._stopping = True  # and the app is shutting down
        return log_id

    env.worker._mark_sending = mark_then_cancel  # type: ignore[method-assign]
    env.step()
    assert not env.transport.attempts
    row = env.recipients(cid)["ada@example.com"]
    assert row["status"] == campaigns.R_NOT_SENT and row["attempts"] == 0
    assert env.send_times() == []


@pytest.mark.parametrize(
    "result",
    [SendResult.auth("token expired"), SendResult.permanent("bad")],
    ids=["auth", "failures"],
)
def test_failures_during_a_pause_keep_it_paused(tmp_path: Path, result: SendResult) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS)
    with env.db.connect() as conn:  # the next failure is the 5th in a row
        conn.execute("UPDATE campaigns SET consecutive_failures = 4 WHERE id = ?", (cid,))

    def decide(recipient: str, attempt: int) -> SendResult | None:
        campaigns.pause(env.db, cid, env.clock())  # Jake clicks Pause while it's in flight
        return result

    env.transport.decide = decide
    env.step()
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.status == campaigns.PAUSED
    assert c.last_error
    assert any("The send stays paused" in e for e in env.events(cid))


# --- controls ----------------------------------------------------------------------------------


def test_pause_resume_cancel(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS)
    env.step()
    campaigns.pause(env.db, cid, env.clock())
    assert env.status(cid) == campaigns.PAUSED
    assert env.step() is None
    env.clock.advance(3600)
    assert env.step() is None
    assert len(env.outbox_to()) == 1
    with pytest.raises(CampaignError):
        campaigns.pause(env.db, cid, env.clock())

    campaigns.resume(env.db, cid, env.clock(), transport="mock")
    assert env.status(cid) == campaigns.QUEUED
    env.step()
    assert env.status(cid) == campaigns.SENDING
    assert len(env.outbox_to()) == 2
    assert env.csv_exists()

    campaigns.cancel(env.db, tmp_path, cid, env.clock())
    assert env.status(cid) == campaigns.CANCELLED
    assert env.recipients(cid)["cy@example.com"]["status"] == campaigns.R_NOT_SENT
    assert env.recipients(cid)["bob@example.com"]["status"] == campaigns.R_SENT
    assert not env.csv_exists()
    env.clock.advance(60)
    assert env.step() is None
    assert len(env.outbox_to()) == 2
    with pytest.raises(CampaignError):
        campaigns.resume(env.db, cid, env.clock(), transport="mock")


def test_pause_during_a_send_records_the_result(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS)

    def decide(recipient: str, attempt: int) -> SendResult | None:
        campaigns.pause(env.db, cid, env.clock())  # Jake clicks Pause while it's in flight
        return None

    env.transport.decide = decide
    env.step()
    assert env.status(cid) == campaigns.PAUSED
    assert env.recipients(cid)["ada@example.com"]["status"] == campaigns.R_SENT


def test_cancel_during_a_failing_send(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS)

    def decide(recipient: str, attempt: int) -> SendResult | None:
        campaigns.cancel(env.db, tmp_path, cid, env.clock())
        return SendResult.retry("timeout")

    env.transport.decide = decide
    env.step()
    assert env.status(cid) == campaigns.CANCELLED
    rows = env.recipients(cid)
    assert rows["ada@example.com"]["status"] == campaigns.R_NOT_SENT
    assert rows["ada@example.com"]["next_attempt_at"] is None
    assert env.step() is None


BUILD_ERRORS = {
    "message_error": MessageError("bad address"),  # permanent
    "exception": RuntimeError("template broke"),  # retryable
}


def build_then(env: Env, action: Callable[[], object], exc: Exception) -> None:
    """Make ``_build`` run ``action`` (Jake clicks Pause or Cancel while the message is being
    rendered, i.e. after the plan) and then fail with ``exc``."""

    def build(*args: object) -> bytes:
        action()
        raise exc

    env.worker._build = build  # type: ignore[method-assign]


def failures(env: Env, cid: int) -> int:
    with env.db.connect() as conn:
        return conn.execute(
            "SELECT consecutive_failures FROM campaigns WHERE id = ?", (cid,)
        ).fetchone()[0]


def results(env: Env, cid: int) -> dict[str, list[str]]:
    rows = list(csv.reader(io.StringIO(campaigns.results_csv(env.db, cid))))
    return {r[1]: r for r in rows[1:]}


@pytest.mark.parametrize("kind", list(BUILD_ERRORS))
def test_cancel_during_a_failing_build_leaves_not_sent(tmp_path: Path, kind: str) -> None:
    """Fix A2: a recipient cancelled before the transport was called is ``not_sent``, even if
    building their message then fails; the failure isn't counted anywhere."""
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS)
    build_then(
        env, lambda: campaigns.cancel(env.db, tmp_path, cid, env.clock()), BUILD_ERRORS[kind]
    )
    env.step()
    assert env.status(cid) == campaigns.CANCELLED
    rows = env.recipients(cid)
    assert {r["status"] for r in rows.values()} == {campaigns.R_NOT_SENT}
    ada = rows["ada@example.com"]
    assert ada["attempts"] == 0
    assert ada["last_error"] is None
    assert ada["next_attempt_at"] is None
    assert failures(env, cid) == 0
    assert not any("ada@example.com" in e for e in env.events(cid))
    assert env.send_times() == []
    assert results(env, cid)["ada@example.com"][3:5] == ["not_sent", "0"]
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.last_error is None
    env.clock.advance(3600)
    assert env.step() is None


@pytest.mark.parametrize("kind", list(BUILD_ERRORS))
def test_pause_during_a_failing_build_leaves_pending(tmp_path: Path, kind: str) -> None:
    """A pause landing after the plan: the build failure isn't recorded (the recipient stays
    ``pending``, attempt uncounted); after resuming it is built again and goes out."""
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS)
    real_build = env.worker._build
    build_then(env, lambda: campaigns.pause(env.db, cid, env.clock()), BUILD_ERRORS[kind])
    env.step()
    assert env.status(cid) == campaigns.PAUSED
    ada = env.recipients(cid)["ada@example.com"]
    assert ada["status"] == campaigns.R_PENDING
    assert ada["attempts"] == 0
    assert ada["last_error"] is None
    assert ada["next_attempt_at"] is None
    assert failures(env, cid) == 0
    env.worker._build = real_build  # type: ignore[method-assign]
    campaigns.resume(env.db, cid, env.clock(), transport="mock")
    env.drive()
    rows = env.recipients(cid)
    assert {r["status"] for r in rows.values()} == {campaigns.R_SENT}
    assert rows["ada@example.com"]["attempts"] == 1


def test_message_error_in_an_active_campaign_is_failed(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS)
    real_build = env.worker._build

    def build(c: object, row: sender._Row, account: object) -> bytes:
        if row.email == "bob@example.com":
            raise MessageError("bad address")
        return real_build(c, row, account)  # type: ignore[arg-type]

    env.worker._build = build  # type: ignore[method-assign]
    env.drive()
    assert env.status(cid) == campaigns.FINISHED
    bob = env.recipients(cid)["bob@example.com"]
    assert bob["status"] == campaigns.R_FAILED
    assert bob["attempts"] == 1
    assert bob["last_error"] == "bad address"
    assert results(env, cid)["bob@example.com"][3:5] == ["failed", "1"]
    assert results(env, cid)["cy@example.com"][3] == "sent"
    assert any("bob@example.com" in e and "failed permanently" in e for e in env.events(cid))
    assert env.outbox_to() == ["Name0 <ada@example.com>", "Name2 <cy@example.com>"]


def test_cancel_while_signed_out_leaves_not_sent(tmp_path: Path) -> None:
    """The other pre-send outcome (graph mode, no account): a cancel after the plan wins too."""
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS)

    def signed_out() -> None:
        campaigns.cancel(env.db, tmp_path, cid, env.clock())

    env.worker.sender = signed_out
    env.step()
    assert env.status(cid) == campaigns.CANCELLED
    rows = env.recipients(cid)
    assert {r["status"] for r in rows.values()} == {campaigns.R_NOT_SENT}
    assert rows["ada@example.com"]["last_error"] is None
    assert env.send_times() == []


RACE_RESULTS = {
    "retry": (SendResult.retry("timeout"), campaigns.R_NOT_SENT),
    "throttled": (SendResult.retry("429", retry_after=300), campaigns.R_NOT_SENT),
    "auth": (SendResult.auth("revoked"), campaigns.R_NOT_SENT),
    "sent": (SendResult.success(), campaigns.R_SENT),
    "permanent": (SendResult.permanent("bad"), campaigns.R_FAILED),
    "unknown": (SendResult.unknown("timeout"), campaigns.R_FAILED),
}


@pytest.mark.parametrize("when", ["before", "during"])
@pytest.mark.parametrize("kind", list(RACE_RESULTS))
def test_cancel_racing_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, when: str
) -> None:
    """A cancel landing just before ``_record``'s transaction, or while it holds the write lock
    (the cancel then waits for it), never leaves a recipient pending in a cancelled campaign."""
    result, expected = RACE_RESULTS[kind]
    env = Env(tmp_path, rate=60, decide=lambda r, n: result if r == "ada@example.com" else None)
    cid = env.campaign(EMAILS)
    real_begin = campaigns._begin_immediate
    armed = threading.Event()
    canceller: list[threading.Thread] = []

    def cancel() -> None:
        campaigns.cancel(env.db, tmp_path, cid, env.clock())

    def hooked(conn: sqlite3.Connection) -> None:
        if not armed.is_set():
            real_begin(conn)
            return
        armed.clear()
        if when == "before":
            cancel()
            real_begin(conn)
        else:
            real_begin(conn)
            canceller.append(threading.Thread(target=cancel))
            canceller[0].start()
            time.sleep(0.2)  # the cancel is now waiting for the lock

    monkeypatch.setattr(campaigns, "_begin_immediate", hooked)
    record = env.worker._record

    def armed_record(*args: object, **kwargs: object) -> None:
        armed.set()
        record(*args, **kwargs)  # type: ignore[arg-type]

    env.worker._record = armed_record  # type: ignore[method-assign]
    env.step()
    for t in canceller:
        t.join(5)
        assert not t.is_alive()
    assert env.status(cid) == campaigns.CANCELLED
    rows = env.recipients(cid)
    assert rows["ada@example.com"]["status"] == expected
    assert all(r["status"] != campaigns.R_PENDING for r in rows.values())
    assert all(r["next_attempt_at"] is None for r in rows.values())
    env.clock.advance(3600)
    assert env.step() is None
    assert set(env.transport.attempts) == {"ada@example.com"}


def test_skip_without_a_change_adds_no_event(tmp_path: Path) -> None:
    env = Env(tmp_path, rate=60)
    cid = env.campaign(EMAILS[:1])
    campaigns.cancel(env.db, tmp_path, cid, env.clock())  # ada is now not_sent
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None
    env.worker._skip(c, sender._Row(2, "ada@example.com", "Name0", 0), env.clock())
    assert env.recipients(cid)["ada@example.com"]["status"] == campaigns.R_NOT_SENT
    assert not any("skipped" in e for e in env.events(cid))


def test_stopped_campaign_can_be_cancelled(tmp_path: Path) -> None:
    env = Env(tmp_path, decide=lambda r, n: SendResult.auth("revoked"))
    cid = env.campaign(EMAILS)
    env.drive()
    assert env.status(cid) == campaigns.STOPPED_ERROR
    assert env.csv_exists()
    campaigns.cancel(env.db, tmp_path, cid, env.clock())
    assert not env.csv_exists()
    assert campaigns.counts(env.db, cid)[campaigns.R_NOT_SENT] == 3


def test_only_one_unfinished_campaign(tmp_path: Path) -> None:
    env = Env(tmp_path)
    first = env.campaign(EMAILS)
    with pytest.raises(CampaignError, match="hasn't finished"):
        env.campaign(["dee@example.com"])
    campaigns.pause(env.db, first, env.clock())
    with pytest.raises(CampaignError):
        env.campaign(["dee@example.com"])
    campaigns.cancel(env.db, tmp_path, first, env.clock())
    env.campaign(["dee@example.com"])


def test_a_draft_is_sent_once(tmp_path: Path) -> None:
    env = Env(tmp_path)
    cid = env.campaign(EMAILS[:1])
    env.drive()
    assert env.status(cid) == campaigns.FINISHED
    with pytest.raises(CampaignError, match="already been used"):
        campaigns.create_campaign(
            env.db,
            env.draft_id,
            CONTENT,
            [Recipient(2, "x@example.com", "")],
            env.clock(),
            transport="mock",
        )


def test_snapshot_is_independent_of_later_edits(tmp_path: Path) -> None:
    env = Env(tmp_path)
    cid = env.campaign(EMAILS[:1])
    c = campaigns.get_campaign(env.db, cid)
    assert c is not None and c.content == CONTENT

    assert c.content_hash == compose.content_hash(CONTENT)
    assert compose.content_hash(CONTENT) != compose.content_hash(
        dataclasses.replace(CONTENT, subject="Other")
    )


# --- clean-up ----------------------------------------------------------------------------------


def _backdate(db: Database, draft_id: str, when: datetime) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE drafts SET created_at = ? WHERE id = ?",
            (when.isoformat(timespec="seconds"), draft_id),
        )


def test_cleanup_deletes_abandoned_and_ended_csvs(tmp_path: Path) -> None:
    env = Env(tmp_path)
    now = datetime.now(UTC)
    old = drafts.create_draft(env.db, tmp_path, "old.csv", b"Email\na@example.com\n")
    _backdate(env.db, old.id, now - timedelta(days=8))
    recent = drafts.create_draft(env.db, tmp_path, "new.csv", b"Email\na@example.com\n")
    _backdate(env.db, recent.id, now - timedelta(days=6))
    # An old draft with an unfinished campaign keeps its CSV.
    running = env.campaign(EMAILS[:1])
    running_draft = env.draft_id
    _backdate(env.db, running_draft, now - timedelta(days=30))

    assert campaigns.cleanup_csvs(env.db, tmp_path, now) == 1

    def has_csv(draft_id: str) -> bool:
        d = drafts.get_draft(env.db, draft_id)
        assert d is not None
        return drafts.read_csv(tmp_path, d) is not None

    assert not has_csv(old.id)
    assert has_csv(recent.id)
    assert has_csv(running_draft)

    # A finished campaign whose CSV survived (crash before deleting it) is cleaned too.
    with env.db.connect() as conn:
        conn.execute("UPDATE campaigns SET status = 'finished' WHERE id = ?", (running,))
    assert campaigns.cleanup_csvs(env.db, tmp_path, now) == 1
    assert not has_csv(running_draft)


# --- results -----------------------------------------------------------------------------------


def test_results_csv_is_safe(tmp_path: Path) -> None:
    env = Env(
        tmp_path,
        decide=lambda r, n: SendResult.permanent("=HYPERLINK(1)") if r.startswith("b") else None,
    )
    cid = env.campaign(["ada@example.com", "bob@example.com"], ["=cmd|' /C calc'!A0", "+Bob"])
    env.drive()
    rows = list(csv.reader(io.StringIO(campaigns.results_csv(env.db, cid))))
    assert rows[0] == campaigns.RESULT_COLUMNS
    assert rows[1][:5] == ["2", "ada@example.com", "'=cmd|' /C calc'!A0", "sent", "1"]
    assert rows[1][5].startswith("2026-12-01T15:00")
    assert rows[2] == ["3", "bob@example.com", "'+Bob", "failed", "1", "", "'=HYPERLINK(1)"]
