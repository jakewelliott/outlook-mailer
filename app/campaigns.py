"""Campaigns (send jobs): storage, controls, results and clean-up. The worker is ``app.sender``.

A campaign is created from a draft with a snapshot of its saved content (``compose.to_json``) and
of the eligible recipients from the validation report, so later edits to the draft or the CSV
don't change what is sent. Only one campaign may be unfinished at a time. It also records the
``MAIL_TRANSPORT`` it was started with: only that transport sends it, and resuming it under
another one is refused (it can still be paused or cancelled).

Campaign states: ``queued`` (waiting for the worker), ``sending``, ``paused`` (by the user),
``paused_daily_cap`` (resumes by itself at ``resume_at``), ``stopped_error`` (sign-in lost, or too
many failures in a row; resume or cancel), and the final ``cancelled`` and ``finished``.

Recipient states: ``pending``, ``sending`` (the transport has been called and the outcome isn't
recorded yet), ``sent``, ``failed``, ``skipped_unsubscribed`` and ``not_sent`` (still pending when
the campaign was cancelled).

When a campaign finishes or is cancelled, the draft's uploaded CSV is deleted; the campaign
tables (the send log) are kept.
"""

import csv
import io
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import compose, drafts
from app.compose import DraftContent
from app.db import Database
from app.recipients import Recipient, safe_cell

QUEUED = "queued"
SENDING = "sending"
PAUSED = "paused"
PAUSED_DAILY_CAP = "paused_daily_cap"
STOPPED_ERROR = "stopped_error"
CANCELLED = "cancelled"
FINISHED = "finished"
UNFINISHED = (QUEUED, SENDING, PAUSED, PAUSED_DAILY_CAP, STOPPED_ERROR)
RUNNABLE = (QUEUED, SENDING, PAUSED_DAILY_CAP)
FINAL = (CANCELLED, FINISHED)
STATUS_LABELS = {
    QUEUED: "Waiting to start",
    SENDING: "Sending",
    PAUSED: "Paused",
    PAUSED_DAILY_CAP: "Paused: daily limit reached",
    STOPPED_ERROR: "Stopped by an error",
    CANCELLED: "Cancelled",
    FINISHED: "Finished",
}
CAN_PAUSE = (QUEUED, SENDING, PAUSED_DAILY_CAP)
CAN_RESUME = (PAUSED, STOPPED_ERROR)
CAN_CANCEL = UNFINISHED

R_PENDING = "pending"
R_SENDING = "sending"
R_SENT = "sent"
R_FAILED = "failed"
R_SKIPPED = "skipped_unsubscribed"
R_NOT_SENT = "not_sent"
RECIPIENT_STATUSES = (R_PENDING, R_SENDING, R_SENT, R_FAILED, R_SKIPPED, R_NOT_SENT)
RECIPIENT_LABELS = {
    R_PENDING: "Waiting",
    R_SENDING: "Sending now",
    R_SENT: "Sent",
    R_FAILED: "Failed",
    R_SKIPPED: "Skipped (unsubscribed)",
    R_NOT_SENT: "Not sent (cancelled)",
}

INTERRUPTED_ERROR = "interrupted during send — may or may not have been delivered; check Sent Items"
UNKNOWN_SUFFIX = "may or may not have been delivered; check Sent Items"
# Drafts with no campaign whose CSV is older than this are deleted at startup.
ABANDONED_DRAFT_AGE = timedelta(days=7)
EVENTS_SHOWN = 20


class CampaignError(ValueError):
    """The campaign can't be created or changed as asked. The message is for the user."""


def iso(dt: datetime) -> str:
    """A UTC timestamp in one fixed format, so stored values sort and compare as text."""
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def local_zone(name: str) -> tzinfo:
    """The ``TZ`` setting as a time zone; UTC if it isn't a known zone name."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def local_time(dt: datetime | None, zone: tzinfo) -> str:
    """For people: ``2026-12-01 10:00:05 EST``."""
    return "" if dt is None else dt.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S %Z")


@dataclass(frozen=True)
class Campaign:
    id: int
    draft_id: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    status: str
    transport: str
    content: DraftContent
    content_hash: str
    total: int
    consecutive_failures: int
    consecutive_throttles: int
    next_send_at: datetime | None
    resume_at: datetime | None
    last_error: str | None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def unfinished(self) -> bool:
        return self.status in UNFINISHED


def _campaign(row: sqlite3.Row) -> Campaign:
    return Campaign(
        id=row["id"],
        draft_id=row["draft_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=parse_iso(row["started_at"]),
        finished_at=parse_iso(row["finished_at"]),
        status=row["status"],
        transport=row["transport"],
        content=compose.from_json(row["content"]),
        content_hash=row["content_hash"],
        total=row["total"],
        consecutive_failures=row["consecutive_failures"],
        consecutive_throttles=row["consecutive_throttles"],
        next_send_at=parse_iso(row["next_send_at"]),
        resume_at=parse_iso(row["resume_at"]),
        last_error=row["last_error"],
    )


def add_event(conn: sqlite3.Connection, campaign_id: int, now: datetime, message: str) -> None:
    conn.execute(
        "INSERT INTO campaign_events (campaign_id, at, message) VALUES (?, ?, ?)",
        (campaign_id, iso(now), message),
    )


def _begin_immediate(conn: sqlite3.Connection) -> None:
    """Take the write lock now, so a check followed by a write can't race another writer."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")


# --- creating and reading ----------------------------------------------------------------------


def unfinished_campaign(conn: sqlite3.Connection) -> int | None:
    marks = ", ".join("?" for _ in UNFINISHED)
    row = conn.execute(
        f"SELECT id FROM campaigns WHERE status IN ({marks}) ORDER BY id LIMIT 1", UNFINISHED
    ).fetchone()
    return None if row is None else row["id"]


def active_campaign_id(db: Database) -> int | None:
    """The unfinished campaign, if any (there is at most one)."""
    with db.connect() as conn:
        return unfinished_campaign(conn)


def campaign_for_draft(db: Database, draft_id: str) -> int | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM campaigns WHERE draft_id = ? ORDER BY id DESC LIMIT 1", (draft_id,)
        ).fetchone()
    return None if row is None else row["id"]


def create_campaign(
    db: Database,
    draft_id: str,
    content: DraftContent,
    recipients: Iterable[Recipient],
    now: datetime,
    *,
    transport: str,
) -> int:
    """Snapshot ``content`` and ``recipients`` into a new queued campaign that only
    ``transport`` (the ``MAIL_TRANSPORT`` it was started with) may send. Raises
    ``CampaignError`` if another campaign is unfinished or this draft was already sent."""
    recipients = list(recipients)
    if not recipients:
        raise CampaignError("There is nobody to send to.")
    with db.connect() as conn:
        _begin_immediate(conn)
        active = unfinished_campaign(conn)
        if active is not None:
            raise CampaignError(
                f"Another send (#{active}) hasn't finished. Finish or cancel it first."
            )
        row = conn.execute("SELECT id FROM campaigns WHERE draft_id = ?", (draft_id,)).fetchone()
        if row is not None:
            raise CampaignError(f"This list has already been used for send #{row['id']}.")
        cur = conn.execute(
            "INSERT INTO campaigns "
            "(draft_id, created_at, status, transport, content, content_hash, total) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                draft_id,
                iso(now),
                QUEUED,
                transport,
                compose.to_json(content),
                compose.content_hash(content),
                len(recipients),
            ),
        )
        campaign_id = cur.lastrowid
        assert campaign_id is not None
        conn.executemany(
            "INSERT INTO campaign_recipients (campaign_id, row_no, email, first_name, status) "
            "VALUES (?, ?, ?, ?, ?)",
            [(campaign_id, r.row, r.email, r.first_name, R_PENDING) for r in recipients],
        )
        add_event(conn, campaign_id, now, f"Send created for {len(recipients)} recipients.")
    return campaign_id


def get_campaign(db: Database, campaign_id: int) -> Campaign | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    return None if row is None else _campaign(row)


@dataclass(frozen=True)
class CampaignSummary:
    campaign: Campaign
    original_name: str
    counts: dict[str, int]


def list_campaigns(db: Database) -> list[CampaignSummary]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.*, d.original_name FROM campaigns c JOIN drafts d ON d.id = c.draft_id "
            "ORDER BY c.id DESC"
        ).fetchall()
        all_counts = _counts_by_campaign(conn)
    return [
        CampaignSummary(_campaign(r), r["original_name"], all_counts.get(r["id"], _empty_counts()))
        for r in rows
    ]


def _empty_counts() -> dict[str, int]:
    return dict.fromkeys(RECIPIENT_STATUSES, 0)


def _counts_by_campaign(conn: sqlite3.Connection) -> dict[int, dict[str, int]]:
    result: dict[int, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT campaign_id, status, COUNT(*) AS n FROM campaign_recipients "
        "GROUP BY campaign_id, status"
    ):
        result.setdefault(row["campaign_id"], _empty_counts())[row["status"]] = row["n"]
    return result


def counts(db: Database, campaign_id: int) -> dict[str, int]:
    result = _empty_counts()
    with db.connect() as conn:
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM campaign_recipients WHERE campaign_id = ? "
            "GROUP BY status",
            (campaign_id,),
        ):
            result[row["status"]] = row["n"]
    return result


@dataclass(frozen=True)
class Event:
    at: datetime
    message: str


def events(db: Database, campaign_id: int, limit: int = EVENTS_SHOWN) -> list[Event]:
    """The latest events, newest first."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT at, message FROM campaign_events WHERE campaign_id = ? ORDER BY id DESC "
            "LIMIT ?",
            (campaign_id, limit),
        ).fetchall()
    return [Event(datetime.fromisoformat(r["at"]), r["message"]) for r in rows]


@dataclass(frozen=True)
class RecipientError:
    row_no: int
    email: str
    status: str
    attempts: int
    error: str


def recipient_errors(db: Database, campaign_id: int, limit: int = 20) -> list[RecipientError]:
    """Failed recipients and pending ones waiting to retry, with their last error."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT row_no, email, status, attempts, last_error FROM campaign_recipients "
            "WHERE campaign_id = ? AND last_error IS NOT NULL AND status IN (?, ?, ?) "
            "ORDER BY row_no LIMIT ?",
            (campaign_id, R_FAILED, R_PENDING, R_NOT_SENT, limit),
        ).fetchall()
    return [
        RecipientError(r["row_no"], r["email"], r["status"], r["attempts"], r["last_error"])
        for r in rows
    ]


# --- controls ----------------------------------------------------------------------------------


def _change(
    db: Database,
    campaign_id: int,
    allowed: tuple[str, ...],
    verb: str,
    now: datetime,
    apply: Callable[[sqlite3.Connection, Campaign], str],
) -> Campaign:
    with db.connect() as conn:
        _begin_immediate(conn)
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if row is None:
            raise CampaignError("That send doesn't exist.")
        campaign = _campaign(row)
        if campaign.status not in allowed:
            raise CampaignError(f"A send that is {campaign.status_label.lower()} can't be {verb}.")
        add_event(conn, campaign_id, now, apply(conn, campaign))
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    return _campaign(row)


def pause(db: Database, campaign_id: int, now: datetime) -> Campaign:
    def apply(conn: sqlite3.Connection, c: Campaign) -> str:
        conn.execute(
            "UPDATE campaigns SET status = ?, next_send_at = NULL, resume_at = NULL WHERE id = ?",
            (PAUSED, c.id),
        )
        return "Paused."

    return _change(db, campaign_id, CAN_PAUSE, "paused", now, apply)


def transport_mismatch(c: Campaign, transport: str) -> str | None:
    """Why ``c`` can't be sent by ``transport`` (the current ``MAIL_TRANSPORT``), or None."""
    if c.transport == transport:
        return None
    return f"This send was started in {c.transport} mode; cancel it and start a new one."


def resume(db: Database, campaign_id: int, now: datetime, *, transport: str) -> Campaign:
    """Resume a paused or stopped campaign. Refused if it was started with another transport
    (``transport`` is the current ``MAIL_TRANSPORT``): a mock run must never go out for real."""

    def apply(conn: sqlite3.Connection, c: Campaign) -> str:
        mismatch = transport_mismatch(c, transport)
        if mismatch:
            raise CampaignError(mismatch)
        conn.execute(
            "UPDATE campaigns SET status = ?, consecutive_failures = 0, "
            "consecutive_throttles = 0, last_error = NULL, resume_at = NULL WHERE id = ?",
            (QUEUED, c.id),
        )
        return "Resumed."

    return _change(db, campaign_id, CAN_RESUME, "resumed", now, apply)


def cancel(db: Database, data_dir: str | Path, campaign_id: int, now: datetime) -> Campaign:
    """Cancel: sent stays sent, pending becomes ``not_sent``. Deletes the draft's CSV.

    A recipient in ``sending`` (the worker is waiting for the transport) is left for the worker,
    which records its outcome.
    """

    def apply(conn: sqlite3.Connection, c: Campaign) -> str:
        conn.execute(
            "UPDATE campaigns SET status = ?, finished_at = ?, next_send_at = NULL, "
            "resume_at = NULL WHERE id = ?",
            (CANCELLED, iso(now), c.id),
        )
        n = conn.execute(
            "UPDATE campaign_recipients SET status = ?, next_attempt_at = NULL "
            "WHERE campaign_id = ? AND status = ?",
            (R_NOT_SENT, c.id, R_PENDING),
        ).rowcount
        return f"Cancelled. {n} recipients not sent."

    campaign = _change(db, campaign_id, CAN_CANCEL, "cancelled", now, apply)
    drafts.delete_csv(db, data_dir, campaign.draft_id)
    return campaign


# --- provider throttling ---------------------------------------------------------------------


def throttle_until(conn: sqlite3.Connection) -> tuple[datetime, datetime] | None:
    """``(set_at, not_before)`` of the provider's last ``Retry-After``, if any."""
    row = conn.execute("SELECT set_at, not_before FROM send_throttle WHERE id = 1").fetchone()
    if row is None:
        return None
    return datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1])


def set_throttle(conn: sqlite3.Connection, now: datetime, not_before: datetime) -> None:
    """Record that nothing may be sent before ``not_before``. Never shortens a later time."""
    current = throttle_until(conn)
    if current is not None and current[1] >= not_before:
        return
    conn.execute(
        "INSERT INTO send_throttle (id, set_at, not_before) VALUES (1, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET set_at = excluded.set_at, "
        "not_before = excluded.not_before",
        (iso(now), iso(not_before)),
    )


# --- startup -----------------------------------------------------------------------------------


def recover_interrupted(db: Database, now: datetime) -> int:
    """Mark recipients left in ``sending`` (the app stopped mid-send) as failed. Never resent.

    The transport may or may not have delivered the message, and resending could email the
    person twice, so the safe choice is to report it and let Jake check Sent Items. Returns the
    number marked.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT campaign_id, row_no, email FROM campaign_recipients WHERE status = ?",
            (R_SENDING,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE campaign_recipients SET status = ?, last_error = ?, next_attempt_at = NULL "
                "WHERE campaign_id = ? AND row_no = ?",
                (R_FAILED, INTERRUPTED_ERROR, r["campaign_id"], r["row_no"]),
            )
            add_event(
                conn,
                r["campaign_id"],
                now,
                f"Row {r['row_no']} ({r['email']}): {INTERRUPTED_ERROR}.",
            )
    return len(rows)


def cleanup_csvs(db: Database, data_dir: str | Path, now: datetime) -> int:
    """Delete uploaded CSVs that are no longer needed. Returns how many drafts were cleaned.

    - drafts with no campaign created more than ``ABANDONED_DRAFT_AGE`` ago (abandoned);
    - drafts whose campaign is finished or cancelled (normally deleted at that moment; this
      catches a crash in between).
    """
    cutoff = now - ABANDONED_DRAFT_AGE
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT d.id, d.created_at, "
            "  (SELECT COUNT(*) FROM campaigns c WHERE c.draft_id = d.id) AS n, "
            "  (SELECT COUNT(*) FROM campaigns c WHERE c.draft_id = d.id AND c.status IN (?, ?)) "
            "    AS done "
            "FROM drafts d WHERE d.csv_filename IS NOT NULL",
            FINAL,
        ).fetchall()
    cleaned = 0
    for r in rows:
        try:
            created = datetime.fromisoformat(r["created_at"])
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        abandoned = r["n"] == 0 and created < cutoff
        ended = r["n"] > 0 and r["done"] == r["n"]
        if abandoned or ended:
            drafts.delete_csv(db, data_dir, r["id"])
            cleaned += 1
    return cleaned


# --- results -----------------------------------------------------------------------------------

RESULT_COLUMNS = ["row_no", "email", "first_name", "status", "attempts", "sent_at", "error"]


def results_csv(db: Database, campaign_id: int) -> str:
    """Every recipient and what happened, with the formula-injection guard on text cells."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(RESULT_COLUMNS)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT row_no, email, first_name, status, attempts, sent_at, last_error "
            "FROM campaign_recipients WHERE campaign_id = ? ORDER BY row_no",
            (campaign_id,),
        ).fetchall()
    for r in rows:
        writer.writerow(
            [
                str(r["row_no"]),
                safe_cell(r["email"]),
                safe_cell(r["first_name"]),
                r["status"],
                str(r["attempts"]),
                r["sent_at"] or "",
                safe_cell(r["last_error"] or ""),
            ]
        )
    return out.getvalue()
