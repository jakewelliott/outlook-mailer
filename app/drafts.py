"""Drafts: an uploaded CSV (kept in ``DATA_DIR/uploads``) plus its column mapping.

The CSV is stored as uploaded and re-parsed when needed, so the file on disk is the single
source of truth. ``delete_csv`` removes it once a campaign ends (the draft row stays).
"""

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.db import Database
from app.recipients import ColumnMapping

UPLOADS_DIR = "uploads"
_ID_RE = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class Draft:
    id: str
    created_at: str
    csv_filename: str | None
    original_name: str
    mapping: ColumnMapping | None
    status_values: list[str] | None  # selected status keys; None when no status column


def is_draft_id(value: str) -> bool:
    return bool(_ID_RE.fullmatch(value))


def uploads_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / UPLOADS_DIR


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def create_draft(db: Database, data_dir: str | Path, original_name: str, data: bytes) -> Draft:
    """Save the CSV as ``uploads/<id>.csv`` (mode 0600) and record a draft."""
    draft_id = secrets.token_hex(16)
    folder = uploads_dir(data_dir)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    csv_filename = f"{draft_id}.csv"
    _write_private(folder / csv_filename, data)
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    # Keep the name for display only; it never becomes part of a path.
    original_name = Path(original_name.replace("\\", "/")).name[:200] or "upload.csv"
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO drafts (id, created_at, csv_filename, original_name) VALUES (?, ?, ?, ?)",
            (draft_id, created_at, csv_filename, original_name),
        )
    return Draft(draft_id, created_at, csv_filename, original_name, None, None)


def get_draft(db: Database, draft_id: str) -> Draft | None:
    if not is_draft_id(draft_id):
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        return None
    mapping = ColumnMapping(**json.loads(row["mapping"])) if row["mapping"] else None
    status_values = json.loads(row["status_values"]) if row["status_values"] else None
    return Draft(
        row["id"],
        row["created_at"],
        row["csv_filename"],
        row["original_name"],
        mapping,
        status_values,
    )


def save_mapping(
    db: Database, draft_id: str, mapping: ColumnMapping, status_values: list[str] | None
) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE drafts SET mapping = ?, status_values = ? WHERE id = ?",
            (
                json.dumps(mapping.to_dict()),
                None if status_values is None else json.dumps(status_values),
                draft_id,
            ),
        )


def read_csv(data_dir: str | Path, draft: Draft) -> bytes | None:
    """The stored CSV, or None if it has been deleted."""
    if draft.csv_filename is None:
        return None
    try:
        return (uploads_dir(data_dir) / draft.csv_filename).read_bytes()
    except FileNotFoundError:
        return None


def delete_csv(db: Database, data_dir: str | Path, draft_id: str) -> None:
    """Delete a draft's CSV file and forget its name. Safe to call more than once."""
    draft = get_draft(db, draft_id)
    if draft is None or draft.csv_filename is None:
        return
    (uploads_dir(data_dir) / draft.csv_filename).unlink(missing_ok=True)
    with db.connect() as conn:
        conn.execute("UPDATE drafts SET csv_filename = NULL WHERE id = ?", (draft_id,))
