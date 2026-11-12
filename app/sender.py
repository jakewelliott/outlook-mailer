"""The send worker: one asyncio task in the app process that works through the active campaign.

``SendWorker.step`` does one unit of work and returns how long to wait before the next one
(``None``: nothing to do until woken). ``run`` loops over it; tests call ``step`` directly with a
fake clock, so nothing really sleeps. The database work runs in a thread
(``asyncio.to_thread``) so it doesn't hold up the web pages. Each step, for the one runnable
campaign **started with the current** ``MAIL_TRANSPORT`` (a campaign started in mock mode is
never sent by Graph, or the other way round; it can only be cancelled):

1. A campaign paused for the daily cap waits until ``resume_at``. Otherwise it becomes
   ``sending``.
2. The next due recipient: the lowest row that is ``pending`` and whose retry time (if any) has
   come. None left: wait for the earliest retry, or finish the campaign (and delete the CSV).
   A campaign never finishes while a recipient is still ``sending``.
3. ``unsubscribed.txt`` is checked (re-read when it changes): an opt-out is
   ``skipped_unsubscribed``, without using a send slot.
4. Daily cap: ``DAILY_SEND_CAP`` messages per rolling 24 h **across all campaigns**, counted from
   ``send_log`` (sent, or outcome unknown). When reached the campaign is ``paused_daily_cap``
   until the oldest counted send leaves the window.
   Test sends (``app.gate``) write ``send_log`` rows too (no campaign), so they count here and
   in the pacing.
5. Pacing: at least ``60 / SEND_RATE_PER_MINUTE`` seconds between transport calls (``send_log``,
   so it holds across restarts and campaigns), and none before the provider's last
   ``Retry-After`` time (``send_throttle``, also persisted).
6. The message is rendered (``compose.render_message`` with the campaign's prepared body,
   ``message.build_mime``, ``message.check_single_recipient``), the opt-out list is checked once
   more, and then **exactly-once**: the recipient is set to ``sending`` and committed (with a
   ``send_log`` row) *before* the transport is called, and the outcome is written straight after
   (retried a few times if the write fails; until it succeeds nothing else is sent; writing it
   again after a write that did commit changes nothing, see ``_record``). If the app
   stops in between, the recipient stays ``sending``; at the next start
   ``campaigns.recover_interrupted`` marks it ``failed`` ("interrupted during send...") and it is
   never resent automatically, since the message may already have gone out.

Shutdown: once ``stop`` is called no new transport call starts. ``_stopping`` is checked after
the plan, after rendering, and once more right before ``transport.send`` (with no ``await``
between that check and the call); a recipient already marked ``sending`` by then is put back
(``pending``, or ``not_sent`` if the campaign was cancelled meanwhile), its attempt uncounted and
its ``send_log`` row deleted, so it neither counts against the cap nor is reported interrupted.

An outcome that still can't be written after ``RECORD_GIVE_UP_AFTER`` tries in all (the retries
in ``_save`` plus one per later step) is dropped with an error in the log; the recipient stays
``sending``. The next step then finds a ``sending`` recipient with no outcome held in memory and
stops the campaign (``stopped_error``, with the row in the reason): restart the app so
``recover_interrupted`` reports it ("may or may not have been delivered; check Sent Items"), then
resume.

Every write that depends on the campaign's status reads it inside the same transaction: either a
single guarded ``UPDATE``, or ``campaigns._begin_immediate`` (the write lock) before the read, as
in ``_record``. A pause or cancel from the UI therefore lands wholly before or after it; a
recipient is never left ``pending`` in a cancelled campaign.

Outcomes (see ``app.transport.base`` for how a transport chooses them):

- success -> ``sent``.
- throttled (retryable with ``Retry-After``) -> nothing is sent to anyone before that time; the
  recipient waits until then. Not counted as an attempt for the recipient nor as a failure;
  ``MAX_CONSECUTIVE_THROTTLES`` throttles in a row (no success between) stop the campaign.
- retryable without ``Retry-After`` -> retried after 30 s, 2 min, then 10 min, at most
  ``MAX_ATTEMPTS`` attempts, then ``failed``. Other recipients go meanwhile.
- outcome unknown -> ``failed`` ("may or may not have been delivered; check Sent Items"), never
  retried; its ``send_log`` outcome stays NULL so it counts against the daily cap. With a
  ``stop_reason`` (e.g. a Graph 500 saying the account is blocked) the campaign is also
  ``stopped_error`` for that reason; the recipient stays ``failed``, so resuming doesn't resend.
- permanent -> ``failed``.
- auth error -> the recipient goes back to ``pending`` (the attempt isn't counted) and the
  campaign is ``stopped_error`` until Jake reconnects (or unblocks) the account and resumes.
  In graph mode the worker also stops the campaign this way, without calling the transport,
  when no account is connected (``sender`` returns None). It stops it too (``stopped_error``,
  nothing sent, the recipient stays ``pending``) when the connected account isn't the one the
  campaign was tested with and started as (``campaigns.sender_address``); see
  ``gate.resume_problem`` for resuming it.

An exception while rendering is a retryable failed attempt (a ``MessageError``: permanent). An
exception from ``transport.send`` is outcome unknown (a ``MessageError``, raised before
anything is sent: permanent). ``MAX_CONSECUTIVE_FAILURES`` failures in a row (retryable, unknown
or permanent; any recipients) stop the campaign (``stopped_error``). A campaign the user paused
is never turned into ``stopped_error``: it stays paused, with the reason in its events. A pause
or cancel landing after the plan but before the transport call wins over a rendering failure
(or "no account connected"): it isn't recorded or counted, and the recipient stays ``pending``
(paused) or ``not_sent`` (cancelled), never ``failed``.

Pause, resume and cancel are database changes made by the UI (``app.campaigns``), which then call
``wake``; the worker re-reads the campaign at every step. A pause or cancel during a transport
call takes effect after it: the outcome of that call is always recorded.
"""

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app import campaigns, compose, content, drafts, merge
from app.campaigns import Campaign, add_event, iso, parse_iso
from app.compose import DraftContent
from app.config import Settings
from app.db import Database
from app.emails import normalise_key
from app.message import MessageError, build_mime, check_single_recipient
from app.msauth import NO_ACCOUNT, Sender
from app.recipients import Recipient
from app.transport.base import SendResult, Transport
from app.unsubscribes import UnsubscribeWatcher

log = logging.getLogger("outlook_mailer.sender")

BACKOFF_SECONDS = (30.0, 120.0, 600.0)
MAX_ATTEMPTS = 3
MAX_CONSECUTIVE_FAILURES = 5
# Retry-After answers in a row (no success between) before the campaign stops.
MAX_CONSECUTIVE_THROTTLES = 10
# Waits before retrying the write of an outcome that failed (e.g. the database was locked).
RECORD_RETRY_SECONDS = (0.5, 2.0, 5.0)
# Failed writes of one outcome (in all, across steps) before it is dropped; see the docstring.
RECORD_GIVE_UP_AFTER = 10
DAILY_WINDOW = timedelta(hours=24)
# After an unexpected error in the worker itself (e.g. the database is locked).
ERROR_PAUSE_SECONDS = 30.0
# On shutdown, how long an in-flight send may take to finish before the task is cancelled.
# Must be more than the transport's request timeout (Graph, stage 12) and less than the
# container's stop grace period (compose.yaml stop_grace_period: 30s).
SHUTDOWN_GRACE_SECONDS = 15.0
# Put before the transport's reason when an auth result stops a campaign.
AUTH_STOP_MESSAGE = "Microsoft won't take mail from the Outlook account right now:"
AUTH_STOP_AFTER = "Once that is fixed, resume this send."
# A signed-out stop (``SendResult.signed_out``): the reason, then this (no AUTH_STOP_MESSAGE).
SIGNED_OUT_AFTER = "Then resume this send."
NOT_SIGNED_IN = NO_ACCOUNT

Clock = Callable[[], datetime]
SenderSource = Callable[[], Sender | None]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _seconds(delta: timedelta) -> float:
    return max(0.0, delta.total_seconds())


def sent_in_window(db: Database, now: datetime) -> list[datetime]:
    """Start times of the sends counted against the daily cap in the 24 h before ``now``, oldest
    first: sent, or outcome unknown (in flight or interrupted). All campaigns."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT started_at FROM send_log WHERE started_at > ? "
            "AND (outcome IS NULL OR outcome = 'sent') ORDER BY started_at",
            (iso(now - DAILY_WINDOW),),
        ).fetchall()
    return [datetime.fromisoformat(r[0]) for r in rows]


@dataclass(frozen=True)
class _Row:
    row_no: int
    email: str
    first_name: str
    attempts: int


@dataclass(frozen=True)
class _Outcome:
    """One attempt's result, to be written by ``SendWorker._record``."""

    campaign: Campaign
    row: _Row
    result: SendResult
    at: datetime
    log_id: int | None


Step = float | None | tuple[Campaign, _Row]


class SendWorker:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        transport: Transport,
        *,
        clock: Clock | None = None,
        sender: SenderSource | None = None,
    ) -> None:
        self.db = db
        # Graph mode: the signed-in account (``MicrosoftAuth.sender``), the From of every message.
        # None (mock): no account; the From is ``message.PLACEHOLDER_FROM``.
        self.sender = sender
        self.settings = settings
        self.transport = transport
        self.clock = clock or utcnow
        # Only campaigns started with this MAIL_TRANSPORT are sent.
        self.transport_name = settings.mail_transport
        self.spacing = timedelta(seconds=60.0 / settings.send_rate_per_minute)
        self.cap = settings.daily_send_cap
        self.unsubscribed = UnsubscribeWatcher(settings.data_dir)
        self.zone = campaigns.local_zone(settings.tz)
        self._bodies: dict[int, tuple[DraftContent, content.PreparedBody]] = {}
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        # An outcome whose write failed even after retries: written before anything else.
        self._unrecorded: _Outcome | None = None
        self._unrecorded_tries = 0

    # --- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        """Start ``run`` as a background task on the running loop."""
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._task = self._loop.create_task(self.run(), name="send-worker")

    async def stop(self) -> None:
        """Let an in-flight send finish (up to ``SHUTDOWN_GRACE_SECONDS``), then stop."""
        self._stopping = True
        self.wake()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), SHUTDOWN_GRACE_SECONDS)
        except TimeoutError:
            log.warning("Send worker did not stop in time; cancelling it.")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._unrecorded is not None:
            o = self._unrecorded
            log.error(
                "The outcome for row %s of send #%s could not be saved; at the next start it is "
                "reported as interrupted (check Sent Items).",
                o.row.row_no,
                o.campaign.id,
            )

    def wake(self) -> None:
        """Re-check now (a campaign was started, paused, resumed or cancelled). Thread-safe."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._wake.set()
        else:
            loop.call_soon_threadsafe(self._wake.set)

    async def run(self) -> None:
        while not self._stopping:
            try:
                delay = await self.step()
            except Exception:
                log.exception("Send worker error; trying again in %.0f s", ERROR_PAUSE_SECONDS)
                delay = ERROR_PAUSE_SECONDS
            if self._stopping:
                break
            if delay is None or delay > 0:
                await self._wait(delay)
            else:
                await asyncio.sleep(0)

    async def _wait(self, delay: float | None) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        self._wake.clear()

    # --- one step ------------------------------------------------------------------------------

    async def step(self) -> float | None:
        """Do the next piece of work. Returns seconds until the next step, None when idle."""
        if self._unrecorded is not None:
            o = self._unrecorded
            try:
                await asyncio.to_thread(
                    self._record, o.campaign, o.row, o.result, o.at, log_id=o.log_id
                )
            except Exception:
                self._unrecorded_tries += 1
                if self._unrecorded_tries < RECORD_GIVE_UP_AFTER:
                    raise
                log.exception(
                    "GIVING UP on saving the outcome (%s) for row %s (%s) of send #%s after %d "
                    "tries. The row stays 'sending'; the send is stopped, and after a restart the "
                    "row is reported as interrupted (check Sent Items).",
                    "sent" if o.result.ok else o.result.error,
                    o.row.row_no,
                    o.row.email,
                    o.campaign.id,
                    self._unrecorded_tries,
                )
            else:
                log.info("The outcome for row %s was saved after all.", o.row.row_no)
            self._unrecorded = None
            self._unrecorded_tries = 0
        plan = await asyncio.to_thread(self._plan, self.clock())
        if not isinstance(plan, tuple):
            return plan
        if self._stopping:  # stop() was called while planning: send nothing more
            return None
        await self._attempt(*plan)
        return 0.0

    def _plan(self, now: datetime) -> Step:
        """Everything up to the send: the wait, or the campaign and recipient to send to."""
        c = self._runnable()
        if c is None:
            return None
        orphan = self._in_flight(c.id)
        if orphan is not None:
            # A recipient is ``sending`` but this worker holds no outcome for it: its write was
            # given up (see the module docstring). Only a restart can report it; stop until then.
            self._stop_orphaned(c, orphan, now)
            return None
        if c.status == campaigns.PAUSED_DAILY_CAP and c.resume_at and now < c.resume_at:
            return _seconds(c.resume_at - now)
        if c.status != campaigns.SENDING:
            self._start_sending(c, now)

        row = self._next_due(c.id, now)
        if row is None:
            retry_at = self._earliest_retry(c.id)
            if retry_at is None:
                self._finish(c, now)
                return 0.0
            self._set_next_send(c, max(retry_at, self._pacing_ready_at(now)))
            return _seconds(retry_at - now)

        if self.unsubscribed.contains(row.email):
            self._skip(c, row, now)
            return 0.0

        resume_at = self._cap_resume_at(now)
        if resume_at is not None:
            self._pause_for_cap(c, resume_at, now)
            return _seconds(resume_at - now)

        ready_at = self._pacing_ready_at(now)
        if ready_at > now:
            self._set_next_send(c, ready_at)
            return _seconds(ready_at - now)

        return c, row

    # --- reading -------------------------------------------------------------------------------

    def _runnable(self) -> Campaign | None:
        marks = ", ".join("?" for _ in campaigns.RUNNABLE)
        with self.db.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM campaigns WHERE status IN ({marks}) AND transport = ? "
                "ORDER BY id LIMIT 1",
                (*campaigns.RUNNABLE, self.transport_name),
            ).fetchone()
        return None if row is None else campaigns._campaign(row)

    def _next_due(self, campaign_id: int, now: datetime) -> _Row | None:
        with self.db.connect() as conn:
            r = conn.execute(
                "SELECT row_no, email, first_name, attempts FROM campaign_recipients "
                "WHERE campaign_id = ? AND status = ? "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY row_no LIMIT 1",
                (campaign_id, campaigns.R_PENDING, iso(now)),
            ).fetchone()
        return None if r is None else _Row(r["row_no"], r["email"], r["first_name"], r["attempts"])

    def _earliest_retry(self, campaign_id: int) -> datetime | None:
        with self.db.connect() as conn:
            r = conn.execute(
                "SELECT MIN(next_attempt_at) FROM campaign_recipients "
                "WHERE campaign_id = ? AND status = ?",
                (campaign_id, campaigns.R_PENDING),
            ).fetchone()
        return parse_iso(r[0])

    def _in_flight(self, campaign_id: int) -> tuple[int, str] | None:
        """``(row_no, email)`` of a recipient left ``sending``, if any. Between steps this worker
        has none of its own (an outcome is written, or held in memory, before the next step)."""
        with self.db.connect() as conn:
            r = conn.execute(
                "SELECT row_no, email FROM campaign_recipients WHERE campaign_id = ? "
                "AND status = ? ORDER BY row_no LIMIT 1",
                (campaign_id, campaigns.R_SENDING),
            ).fetchone()
        return None if r is None else (r["row_no"], r["email"])

    @staticmethod
    def _status(conn: sqlite3.Connection, campaign_id: int) -> str:
        return conn.execute("SELECT status FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()[
            0
        ]

    def _cap_resume_at(self, now: datetime) -> datetime | None:
        """None if another message fits in the rolling window, else when one will."""
        sent = sent_in_window(self.db, now)
        if len(sent) < self.cap:
            return None
        return sent[len(sent) - self.cap] + DAILY_WINDOW

    def _pacing_ready_at(self, now: datetime) -> datetime:
        """When the next transport call may happen: the spacing after the last one, and not
        before the provider's ``Retry-After`` time. A clock set back never means a longer wait."""
        with self.db.connect() as conn:
            last = parse_iso(conn.execute("SELECT MAX(started_at) FROM send_log").fetchone()[0])
            throttle = campaigns.throttle_until(conn)
        ready = now if last is None else min(last, now) + self.spacing
        if throttle is not None:
            set_at, not_before = throttle
            ready = max(ready, min(not_before, min(set_at, now) + (not_before - set_at)))
        return ready

    # --- state changes -------------------------------------------------------------------------

    def _start_sending(self, c: Campaign, now: datetime) -> None:
        message = {
            campaigns.QUEUED: "Sending started." if c.started_at is None else "Sending resumed.",
            campaigns.PAUSED_DAILY_CAP: "The daily limit has room again; sending resumed.",
        }[c.status]
        with self.db.connect() as conn:
            changed = conn.execute(
                "UPDATE campaigns SET status = ?, started_at = COALESCE(started_at, ?), "
                "resume_at = NULL WHERE id = ? AND status = ?",
                (campaigns.SENDING, iso(now), c.id, c.status),
            ).rowcount
            if changed:
                add_event(conn, c.id, now, message)

    def _set_next_send(self, c: Campaign, at: datetime) -> None:
        if c.next_send_at == at:
            return
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE campaigns SET next_send_at = ? WHERE id = ? AND status = ?",
                (iso(at), c.id, campaigns.SENDING),
            )

    def _pause_for_cap(self, c: Campaign, resume_at: datetime, now: datetime) -> None:
        with self.db.connect() as conn:
            changed = conn.execute(
                "UPDATE campaigns SET status = ?, resume_at = ?, next_send_at = ? "
                "WHERE id = ? AND status = ?",
                (
                    campaigns.PAUSED_DAILY_CAP,
                    iso(resume_at),
                    iso(resume_at),
                    c.id,
                    campaigns.SENDING,
                ),
            ).rowcount
            if changed:
                add_event(
                    conn,
                    c.id,
                    now,
                    f"Daily limit of {self.cap} messages per 24 hours reached. "
                    f"Sending resumes by itself at {campaigns.local_time(resume_at, self.zone)}.",
                )

    def _stop_orphaned(self, c: Campaign, orphan: tuple[int, str], now: datetime) -> None:
        row_no, email = orphan
        with self.db.connect() as conn:
            campaigns._begin_immediate(conn)
            status = self._status(conn, c.id)
            if status not in campaigns.RUNNABLE:  # paused or cancelled meanwhile
                return
            self._stop(
                conn,
                c.id,
                status,
                now,
                f"Row {row_no} ({email}): the result of its send couldn't be saved. Restart the "
                f"app: the row is then marked failed ({campaigns.UNKNOWN_SUFFIX}). Then resume.",
            )

    def _stop_sender_changed(self, c: Campaign, was: str, now_as: str, now: datetime) -> None:
        with self.db.connect() as conn:
            campaigns._begin_immediate(conn)
            status = self._status(conn, c.id)
            if status not in campaigns.RUNNABLE:  # paused or cancelled meanwhile
                return
            self._stop(conn, c.id, status, now, campaigns.sender_changed_message(was, now_as))

    def _skip(self, c: Campaign, row: _Row, now: datetime) -> None:
        with self.db.connect() as conn:
            changed = conn.execute(
                "UPDATE campaign_recipients SET status = ?, next_attempt_at = NULL "
                "WHERE campaign_id = ? AND row_no = ? AND status = ?",
                (campaigns.R_SKIPPED, c.id, row.row_no, campaigns.R_PENDING),
            ).rowcount
            if changed:
                add_event(
                    conn, c.id, now, f"Row {row.row_no} ({row.email}): skipped, unsubscribed."
                )

    def _finish(self, c: Campaign, now: datetime) -> None:
        with self.db.connect() as conn:
            changed = conn.execute(
                "UPDATE campaigns SET status = ?, finished_at = ?, next_send_at = NULL "
                "WHERE id = ? AND status = ? AND NOT EXISTS (SELECT 1 FROM campaign_recipients "
                "WHERE campaign_id = ? AND status IN (?, ?))",
                (
                    campaigns.FINISHED,
                    iso(now),
                    c.id,
                    campaigns.SENDING,
                    c.id,
                    campaigns.R_PENDING,
                    campaigns.R_SENDING,
                ),
            ).rowcount
            if changed:
                n = dict(
                    conn.execute(
                        "SELECT status, COUNT(*) FROM campaign_recipients WHERE campaign_id = ? "
                        "GROUP BY status",
                        (c.id,),
                    ).fetchall()
                )
                add_event(
                    conn,
                    c.id,
                    now,
                    f"Finished: {n.get(campaigns.R_SENT, 0)} sent, "
                    f"{n.get(campaigns.R_FAILED, 0)} failed, "
                    f"{n.get(campaigns.R_SKIPPED, 0)} skipped (unsubscribed).",
                )
        self._bodies.pop(c.id, None)
        if changed:
            drafts.delete_csv(self.db, self.settings.data_dir, c.draft_id)

    # --- sending -------------------------------------------------------------------------------

    def _body(self, c: Campaign) -> tuple[DraftContent, content.PreparedBody]:
        if c.id not in self._bodies:
            self._bodies[c.id] = (c.content, content.prepare(c.content.body))
        return self._bodies[c.id]

    def _build(self, c: Campaign, row: _Row, account: Sender | None) -> bytes:
        draft_content, prepared = self._body(c)
        values = merge.recipient_values(Recipient(row.row_no, row.email, row.first_name))
        rendered = compose.render_message(
            draft_content,
            values,
            public_base_url=self.settings.public_base_url,
            unsubscribe_secret=self.settings.unsubscribe_secret,
            prepared=prepared,
        )
        mime = build_mime(
            rendered,
            to_addr=row.email,
            to_name=row.first_name or None,
            # Graph sends as the signed-in mailbox: From is its address. The display name is the
            # business name (Outlook.com may show the account's profile name instead).
            from_addr=account.address if account is not None else None,
            from_name=draft_content.business_name.strip()
            or (account.name if account is not None else None)
            or None,
        )
        check_single_recipient(mime, row.email)
        return mime

    async def _attempt(self, c: Campaign, row: _Row) -> None:
        account = None
        if self.sender is not None:
            account = await asyncio.to_thread(self.sender)
            if account is None:  # signed out: stop the campaign, nothing is sent
                result = SendResult.auth(NOT_SIGNED_IN, signed_out=True)
                await self._save(_Outcome(c, row, result, self.clock(), None))
                return
            if c.sender_address is not None and normalise_key(account.address) != normalise_key(
                c.sender_address
            ):
                await asyncio.to_thread(
                    self._stop_sender_changed, c, c.sender_address, account.address, self.clock()
                )
                return
        try:
            mime = await asyncio.to_thread(self._build, c, row, account)
        except MessageError as exc:
            await self._save(_Outcome(c, row, SendResult.permanent(str(exc)), self.clock(), None))
            return
        except Exception as exc:
            log.exception("Could not build the message for row %s", row.row_no)
            result = SendResult.retry(f"Could not build the message: {exc}")
            await self._save(_Outcome(c, row, result, self.clock(), None))
            return

        if self._stopping:
            return
        log_id = await asyncio.to_thread(self._last_check_and_mark, c, row, self.clock())
        if log_id is None:  # opted out, stopping, or cancelled (or changed) since the plan
            return
        if self._stopping:  # stop() was called while marking: undo it, send nothing
            await asyncio.to_thread(self._unmark_sending, c, row, log_id)
            return
        # No await between the check above and the call: once stop() can run, the send began.
        try:
            result = await self.transport.send(
                mime,
                recipient=row.email,
                expected_sender=account.address if account is not None else None,
            )
        except MessageError as exc:  # refused before anything was sent
            result = SendResult.permanent(str(exc))
        except Exception as exc:  # may have been raised after the request went out
            log.exception("Transport error for row %s", row.row_no)
            result = SendResult.unknown(f"Unexpected error while sending: {exc}")
        await self._save(_Outcome(c, row, result, self.clock(), log_id))

    async def _save(self, o: _Outcome) -> None:
        """Record ``o``, retrying the write a few times. If it still fails the outcome is kept
        in memory, written at the start of the next step, and the error raised."""
        for pause in (*RECORD_RETRY_SECONDS, None):
            try:
                await asyncio.to_thread(
                    self._record, o.campaign, o.row, o.result, o.at, log_id=o.log_id
                )
                return
            except Exception:
                if pause is None:
                    self._unrecorded = o
                    self._unrecorded_tries = len(RECORD_RETRY_SECONDS) + 1
                    raise
                log.warning(
                    "Could not save the outcome for row %s; trying again in %.1f s",
                    o.row.row_no,
                    pause,
                    exc_info=True,
                )
                await asyncio.sleep(pause)

    def _last_check_and_mark(self, c: Campaign, row: _Row, now: datetime) -> int | None:
        if self.unsubscribed.contains(row.email):  # one last look, right before sending
            self._skip(c, row, now)
            return None
        if self._stopping:  # read from this thread: saves marking and unmarking
            return None
        return self._mark_sending(c, row, now)

    def _unmark_sending(self, c: Campaign, row: _Row, log_id: int) -> None:
        """Undo ``_mark_sending`` for a send that never started (the worker is stopping): the
        attempt isn't counted and the ``send_log`` row goes, so it doesn't count against the cap.
        ``not_sent`` instead of ``pending`` if the campaign was cancelled meanwhile."""
        with self.db.connect() as conn:
            campaigns._begin_immediate(conn)
            status = self._status(conn, c.id)
            back = campaigns.R_NOT_SENT if status == campaigns.CANCELLED else campaigns.R_PENDING
            conn.execute(
                "UPDATE campaign_recipients SET status = ?, attempts = attempts - 1 "
                "WHERE campaign_id = ? AND row_no = ? AND status = ?",
                (back, c.id, row.row_no, campaigns.R_SENDING),
            )
            conn.execute("DELETE FROM send_log WHERE id = ?", (log_id,))
        log.info("Shutting down: row %s of send #%s was not sent.", row.row_no, c.id)

    def _mark_sending(self, c: Campaign, row: _Row, now: datetime) -> int | None:
        """Set the recipient to ``sending`` and log the send, committed before the transport
        is called. Returns the ``send_log`` id, or None if the recipient is no longer due."""
        with self.db.connect() as conn:
            changed = conn.execute(
                "UPDATE campaign_recipients SET status = ?, attempts = attempts + 1 "
                "WHERE campaign_id = ? AND row_no = ? AND status = ? "
                "AND (SELECT status FROM campaigns WHERE id = ?) = ?",
                (
                    campaigns.R_SENDING,
                    c.id,
                    row.row_no,
                    campaigns.R_PENDING,
                    c.id,
                    campaigns.SENDING,
                ),
            ).rowcount
            if not changed:
                return None
            cur = conn.execute(
                "INSERT INTO send_log (campaign_id, row_no, started_at) VALUES (?, ?, ?)",
                (c.id, row.row_no, iso(now)),
            )
            log_id = cur.lastrowid
        assert log_id is not None
        return log_id

    def _record(
        self, c: Campaign, row: _Row, result: SendResult, now: datetime, *, log_id: int | None
    ) -> None:
        """Write the outcome of one attempt, in one transaction. ``log_id`` None: the transport
        wasn't called (the message couldn't be built), so the attempt is counted here.

        The write lock is taken before the campaign's status is read, so a pause or cancel from
        the UI lands wholly before or after this. Cancelled by then: an outcome that would wait
        (retry, throttle, sign-in problem) leaves the recipient ``not_sent``, like the cancel did
        for the others; sent, failed and unknown are recorded as they are.

        An outcome from before the transport call (``log_id`` None: the message couldn't be
        built, or no account is connected) is dropped if the campaign is no longer ``sending``
        or the recipient no longer ``pending``: the pause or cancel landed before it, as it
        would have for ``_mark_sending``. The recipient stays as that left it (``pending``, or
        ``not_sent`` after a cancel, never ``failed``), with nothing counted (fix A2).

        Writing the same outcome again is a no-op (fix A3): ``_save`` and the next step write
        it again after an error, and the first write may have committed before the error was
        raised. A transport outcome is written only while the recipient is still ``sending``
        with this ``log_id`` as its latest ``send_log`` row (only ``_record`` moves it off
        ``sending``); a pre-send one only while the recipient is ``pending`` with the attempts
        it had when planned (a recorded build failure counts one)."""
        attempts = row.attempts + 1
        who = f"Row {row.row_no} ({row.email})"
        with self.db.connect() as conn:
            campaigns._begin_immediate(conn)
            status = self._status(conn, c.id)
            current = conn.execute(
                "SELECT status, attempts, (SELECT MAX(id) FROM send_log WHERE campaign_id = ? "
                "AND row_no = ?) AS log_id FROM campaign_recipients "
                "WHERE campaign_id = ? AND row_no = ?",
                (c.id, row.row_no, c.id, row.row_no),
            ).fetchone()
            if log_id is not None and (
                current["status"] != campaigns.R_SENDING or current["log_id"] != log_id
            ):
                log.info(
                    "The outcome for row %s of send #%s is already saved; not recording it again.",
                    row.row_no,
                    c.id,
                )
                return
            if log_id is None and (
                status != campaigns.SENDING
                or current["status"] != campaigns.R_PENDING
                or current["attempts"] != row.attempts
            ):
                log.info(
                    "Row %s of send #%s was not sent (the send is now %s, the row %s); not "
                    "recording: %s",
                    row.row_no,
                    c.id,
                    status,
                    current["status"],
                    result.error,
                )
                return
            final = status in campaigns.FINAL
            waiting = campaigns.R_NOT_SENT if status == campaigns.CANCELLED else campaigns.R_PENDING
            if log_id is not None:
                # Unknown stays NULL: it may have gone out, so it counts against the daily cap.
                outcome = "sent" if result.ok else None if result.outcome_unknown else "failed"
                conn.execute("UPDATE send_log SET outcome = ? WHERE id = ?", (outcome, log_id))

            def recipient(**values: object) -> None:
                values = {"attempts": attempts, **values}
                sets = ", ".join(f"{k} = ?" for k in values)
                conn.execute(
                    f"UPDATE campaign_recipients SET {sets} WHERE campaign_id = ? AND row_no = ?",
                    (*values.values(), c.id, row.row_no),
                )

            if result.ok:
                recipient(
                    status=campaigns.R_SENT,
                    sent_at=iso(now),
                    provider_id=result.provider_id,
                    last_error=None,
                    next_attempt_at=None,
                )
                conn.execute(
                    "UPDATE campaigns SET consecutive_failures = 0, consecutive_throttles = 0 "
                    "WHERE id = ?",
                    (c.id,),
                )
                add_event(conn, c.id, now, f"{who}: sent.")
                return

            error = result.error or "unknown error"
            if result.auth_error:
                # Not the recipient's fault: the attempt doesn't count against them.
                recipient(
                    status=waiting, attempts=row.attempts, last_error=error, next_attempt_at=None
                )
                if result.sender_changed is not None:
                    # The account was switched after the check in _attempt: not Microsoft's
                    # refusal, so the same reason as that check gives.
                    add_event(conn, c.id, now, f"{who}: not sent: {error}")
                    reason = campaigns.sender_changed_message(*result.sender_changed)
                elif result.signed_out:
                    # Nothing is connected (or the saved sign-in can't be used): not
                    # Microsoft's refusal either (fix C3).
                    add_event(conn, c.id, now, f"{who}: not sent: {error}")
                    reason = f"{error} {SIGNED_OUT_AFTER}"
                else:
                    add_event(conn, c.id, now, f"{who}: not sent, sign-in problem: {error}")
                    reason = f"{AUTH_STOP_MESSAGE} {error} {AUTH_STOP_AFTER}"
                self._stop(conn, c.id, status, now, reason)
                return

            if result.throttled:
                # The server asked everyone to wait: not a failure, and not the recipient's
                # attempt. Nothing is sent to anyone before not_before.
                not_before = now + timedelta(seconds=result.retry_after or 0.0)
                campaigns.set_throttle(conn, now, not_before)
                recipient(
                    status=waiting,
                    attempts=row.attempts,
                    last_error=error,
                    next_attempt_at=None if final else iso(not_before),
                )
                throttles = conn.execute(
                    "UPDATE campaigns SET consecutive_throttles = consecutive_throttles + 1 "
                    "WHERE id = ? RETURNING consecutive_throttles",
                    (c.id,),
                ).fetchone()[0]
                add_event(
                    conn,
                    c.id,
                    now,
                    f"{who}: the mail server asked to wait ({error}); nothing is sent before "
                    f"{campaigns.local_time(not_before, self.zone)}.",
                )
                if throttles >= MAX_CONSECUTIVE_THROTTLES:
                    self._stop(
                        conn,
                        c.id,
                        status,
                        now,
                        f"The mail server asked to wait {throttles} times in a row, so sending "
                        "stopped. Try again later: resume (or cancel).",
                    )
                return

            failures = conn.execute(
                "UPDATE campaigns SET consecutive_failures = consecutive_failures + 1 "
                "WHERE id = ? RETURNING consecutive_failures",
                (c.id,),
            ).fetchone()[0]
            if result.outcome_unknown:
                error = f"outcome unknown ({error}) — {campaigns.UNKNOWN_SUFFIX}"
                recipient(status=campaigns.R_FAILED, last_error=error, next_attempt_at=None)
                add_event(conn, c.id, now, f"{who}: {error}. Not retried.")
                if result.stop_reason:
                    # The account is in trouble, but this message may have gone out: the
                    # recipient stays failed (not resent on resume) and the campaign stops.
                    self._stop(
                        conn,
                        c.id,
                        status,
                        now,
                        f"{AUTH_STOP_MESSAGE} {result.stop_reason} {who} may or may not have "
                        "received the email and won't be sent it again; check Sent Items. "
                        f"{AUTH_STOP_AFTER}",
                    )
                    return
            elif result.retryable and attempts < MAX_ATTEMPTS:
                retry_at = now + timedelta(seconds=BACKOFF_SECONDS[attempts - 1])
                recipient(
                    status=waiting,
                    last_error=error,
                    next_attempt_at=None if final else iso(retry_at),
                )
                add_event(
                    conn,
                    c.id,
                    now,
                    f"{who}: attempt {attempts} failed ({error}); "
                    + (
                        f"retrying at {campaigns.local_time(retry_at, self.zone)}."
                        if not final
                        else "not retried."
                    ),
                )
            else:
                recipient(status=campaigns.R_FAILED, last_error=error, next_attempt_at=None)
                tries = f"after {attempts} attempts" if attempts > 1 else "permanently"
                add_event(conn, c.id, now, f"{who}: failed {tries}: {error}")
            if failures >= MAX_CONSECUTIVE_FAILURES:
                self._stop(
                    conn,
                    c.id,
                    status,
                    now,
                    f"{failures} send attempts in a row failed, so sending stopped. "
                    f"Last error: {error}. Fix the cause, then resume (or cancel).",
                )

    def _stop(
        self, conn: sqlite3.Connection, campaign_id: int, status: str, now: datetime, why: str
    ) -> None:
        """Stop the campaign (``stopped_error``) for ``why``. A finished or cancelled campaign is
        left alone; one the user paused meanwhile stays paused, with the reason recorded."""
        if status in campaigns.FINAL:
            return
        if status == campaigns.PAUSED:
            conn.execute("UPDATE campaigns SET last_error = ? WHERE id = ?", (why, campaign_id))
            add_event(conn, campaign_id, now, f"{why} (The send stays paused.)")
            return
        conn.execute(
            "UPDATE campaigns SET status = ?, last_error = ?, next_send_at = NULL, "
            "resume_at = NULL WHERE id = ?",
            (campaigns.STOPPED_ERROR, why, campaign_id),
        )
        add_event(conn, campaign_id, now, f"Stopped: {why}")
