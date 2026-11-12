"""SQLite database in ``DATA_DIR/app.db`` (stdlib ``sqlite3``).

Schema changes are a list of migrations; ``PRAGMA user_version`` records how many have run.
Later stages append to ``MIGRATIONS``; never edit one that has shipped.
"""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: drafts (an uploaded CSV and its column mapping)
    """
    CREATE TABLE drafts (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        csv_filename TEXT,
        original_name TEXT NOT NULL,
        mapping TEXT,
        status_values TEXT
    );
    """,
    # 2: the message written for a draft (compose page), and app-wide settings (key/value)
    """
    CREATE TABLE draft_content (
        draft_id TEXT PRIMARY KEY REFERENCES drafts(id) ON DELETE CASCADE,
        updated_at TEXT NOT NULL,
        subject TEXT NOT NULL,
        preheader TEXT NOT NULL,
        body TEXT NOT NULL,
        business_name TEXT NOT NULL,
        postal_address TEXT NOT NULL,
        is_advertisement INTEGER NOT NULL,
        reason TEXT NOT NULL,
        headline TEXT NOT NULL,
        tagline TEXT NOT NULL
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 3: send jobs (stage 10). A campaign snapshots a draft's content and eligible recipients,
    # and records the MAIL_TRANSPORT it was started with (only that transport may send it).
    # send_log has one row per transport call, for pacing and the rolling 24 h cap across all
    # campaigns (outcome NULL = in flight, interrupted or unknown: counted as sent, to be safe).
    # send_throttle (one row at most) holds the provider's Retry-After as an absolute time: no
    # transport call for anyone before not_before.
    """
    CREATE TABLE campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        draft_id TEXT NOT NULL REFERENCES drafts(id),
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        status TEXT NOT NULL CHECK (status IN (
            'queued', 'sending', 'paused', 'paused_daily_cap', 'stopped_error', 'cancelled',
            'finished'
        )),
        transport TEXT NOT NULL CHECK (transport IN ('graph', 'mock')),
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        total INTEGER NOT NULL,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        consecutive_throttles INTEGER NOT NULL DEFAULT 0,
        next_send_at TEXT,
        resume_at TEXT,
        last_error TEXT
    );
    CREATE INDEX campaigns_status ON campaigns (status);
    CREATE TABLE campaign_recipients (
        campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
        row_no INTEGER NOT NULL,
        email TEXT NOT NULL,
        first_name TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'pending', 'sending', 'sent', 'failed', 'skipped_unsubscribed', 'not_sent'
        )),
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        provider_id TEXT,
        sent_at TEXT,
        next_attempt_at TEXT,
        PRIMARY KEY (campaign_id, row_no)
    );
    CREATE INDEX campaign_recipients_status ON campaign_recipients (campaign_id, status);
    CREATE TABLE campaign_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
        at TEXT NOT NULL,
        message TEXT NOT NULL
    );
    CREATE INDEX campaign_events_campaign ON campaign_events (campaign_id, id);
    CREATE TABLE send_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
        row_no INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        outcome TEXT CHECK (outcome IN ('sent', 'failed'))
    );
    CREATE INDEX send_log_started_at ON send_log (started_at);
    CREATE TABLE send_throttle (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        set_at TEXT NOT NULL,
        not_before TEXT NOT NULL
    );
    """,
]


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")


class Database:
    """The app's database. ``init`` opens one *keeper* connection that stays open until
    ``close``: while any connection is open SQLite keeps the WAL and shared-memory files, so each
    short-lived ``connect`` doesn't recreate (and checkpoint) them. ``synchronous=NORMAL`` is
    safe in WAL mode (a power cut can lose the last commits, never corrupt the file) and saves an
    fsync per commit."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._keeper: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def init(self) -> None:
        """Create the file, switch to WAL, run pending migrations and open the keeper
        connection. Idempotent."""
        with self._lock:
            if self._keeper is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False: closed from whichever thread shuts the app down. It is
            # never used for queries after init.
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                migrate(conn)
            except BaseException:
                conn.close()
                raise
            self._keeper = conn

    def close(self) -> None:
        """Close the keeper connection (at shutdown). ``connect`` reopens it if needed."""
        with self._lock:
            keeper, self._keeper = self._keeper, None
        if keeper is not None:
            keeper.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success and rolls back on error, then closes."""
        if self._keeper is None:
            self.init()
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()
