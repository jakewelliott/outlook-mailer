import sqlite3
import stat
from pathlib import Path

from app import drafts
from app.db import MIGRATIONS, Database
from app.recipients import ColumnMapping


def test_schema_created_with_wal_and_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "sub" / "app.db")
    db.init()
    db.init()  # idempotent
    conn = sqlite3.connect(tmp_path / "sub" / "app.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(drafts)")}
    assert cols == {"id", "created_at", "csv_filename", "original_name", "mapping", "status_values"}


def test_draft_lifecycle(tmp_path: Path) -> None:
    db = Database(tmp_path / "app.db")
    draft = drafts.create_draft(db, tmp_path, "C:\\Users\\jake\\../list.csv", b"email\nx\n")
    assert drafts.is_draft_id(draft.id)
    assert draft.original_name == "list.csv"
    path = tmp_path / "uploads" / f"{draft.id}.csv"
    assert path.read_bytes() == b"email\nx\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    mapping = ColumnMapping("email", "first", "status")
    drafts.save_mapping(db, draft.id, mapping, ["subscribed"])
    loaded = drafts.get_draft(db, draft.id)
    assert loaded is not None
    assert loaded.mapping == mapping and loaded.status_values == ["subscribed"]
    assert drafts.read_csv(tmp_path, loaded) == b"email\nx\n"

    drafts.save_mapping(db, draft.id, ColumnMapping("email", "first"), None)
    loaded = drafts.get_draft(db, draft.id)
    assert loaded is not None and loaded.status_values is None

    drafts.delete_csv(db, tmp_path, draft.id)
    drafts.delete_csv(db, tmp_path, draft.id)  # safe twice
    assert not path.exists()
    loaded = drafts.get_draft(db, draft.id)
    assert loaded is not None and loaded.csv_filename is None
    assert drafts.read_csv(tmp_path, loaded) is None


def test_get_draft_rejects_bad_ids(tmp_path: Path) -> None:
    db = Database(tmp_path / "app.db")
    assert drafts.get_draft(db, "../../etc/passwd") is None
    assert drafts.get_draft(db, "0" * 32) is None
