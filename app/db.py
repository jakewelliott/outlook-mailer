"""SQLite database in ``DATA_DIR/app.db`` (stdlib ``sqlite3``).

Schema changes are a list of migrations; ``PRAGMA user_version`` records how many have run.
Later stages append to ``MIGRATIONS``; never edit one that has shipped.
"""

import sqlite3
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
]


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._ready = False

    def init(self) -> None:
        """Create the file, switch to WAL and run pending migrations. Idempotent."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            migrate(conn)
        finally:
            conn.close()
        self._ready = True

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success and rolls back on error, then closes."""
        if not self._ready:
            self.init()
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()
