"""``DATA_DIR/unsubscribed.txt``: reading it, and recording an opt-out in it.

Format: one opt-out per line, ``email<TAB>ISO-8601 datetime``. Blank lines and lines whose first
field isn't an address are ignored. A line holding only an address (e.g. added by hand) still
counts: when in doubt, don't email someone who may have opted out.

An address can be written more than one way: an international domain as Unicode
(``bücher.example``) or as punycode (``xn--bcher-kva.example``). ``address_keys`` gives every form
of an address (``emails.normalise_key`` of the address as written, and of email-validator's
Unicode and ASCII forms when it is valid), and the parsed list holds all of them, so an opt-out
written either way matches a recipient written either way.

Writing (``record_unsubscribe``, the unsubscribe pages) appends one line per address, never
rewrites the file, and holds an exclusive ``fcntl.flock`` on it while it checks for the address and
appends, so two requests (threads or processes) can't both append the same address or interleave
their lines. Readers here take a shared lock, so they never see half of an appended line. A person
editing the file by hand takes no lock; that is fine for the occasional edit.

Lock waits are bounded: the lock is tried without blocking (``LOCK_NB``) every
``LOCK_POLL_SECONDS`` for up to ``LOCK_WAIT_SECONDS``, then ``LockTimeout`` (an ``OSError``) is
raised, so a stuck lock holder can't hang a request or the send worker for ever. The public POST
answers it with a 503 "try again"; the send worker treats it like any other read error (the step
fails, nothing is sent, it tries again later). The clock and sleep are module attributes
(``_monotonic``, ``_sleep``) so tests can drive them.

The file is decoded as UTF-8 with a leading byte-order mark dropped (``utf-8-sig``): Windows
editors add one, and it would otherwise be glued to the first address so that it matched nothing.
"""

import fcntl
import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from email_validator import EmailNotValidError, validate_email

from app.emails import normalise_key

FILENAME = "unsubscribed.txt"
FILE_MODE = 0o600  # only when the app creates the file; an existing file keeps its mode
LOCK_WAIT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.05

_monotonic: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep


class LockTimeout(OSError):
    """``unsubscribed.txt`` stayed locked by someone else for ``LOCK_WAIT_SECONDS``."""


def _lock(fd: int, op: int) -> None:
    """Take ``op`` (``LOCK_SH`` / ``LOCK_EX``) on ``fd``, waiting at most ``LOCK_WAIT_SECONDS``."""
    deadline = _monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(fd, op | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if _monotonic() >= deadline:
                raise LockTimeout(f"{FILENAME} is locked by another writer") from None
            _sleep(LOCK_POLL_SECONDS)


def address_keys(addr: str) -> frozenset[str]:
    """Comparison keys for ``addr``: as written, plus its Unicode and ASCII forms if valid."""
    keys = {normalise_key(addr)}
    try:
        result = validate_email(addr.strip(), check_deliverability=False)
    except EmailNotValidError:
        return frozenset(keys)
    keys.add(normalise_key(result.normalized))
    if result.ascii_email:
        keys.add(normalise_key(result.ascii_email))
    return frozenset(keys)


def _line_address(line: str) -> str | None:
    """The address a line holds (as written, trimmed), or None if it holds none."""
    raw = line.split("\t", 1)[0].strip().lstrip("\ufeff").strip()  # a BOM mid-file, too
    local, at, domain = raw.partition("@")
    if at and local and domain and " " not in raw:
        return raw
    return None


def parse_unsubscribed(text: str) -> frozenset[str]:
    """Lower-case addresses found in the file's text (each in all its forms)."""
    emails: set[str] = set()
    for line in text.splitlines():
        addr = _line_address(line)
        if addr is not None:
            emails |= address_keys(addr)
    return frozenset(emails)


@dataclass(frozen=True)
class Entry:
    email: str  # as written in the file
    recorded: str  # the rest of the line (normally the ISO-8601 time), trimmed; may be ""


def parse_entries(text: str) -> list[Entry]:
    """The file's opt-outs in file order (oldest first), for the admin page."""
    entries = []
    for line in text.splitlines():
        addr = _line_address(line)
        if addr is not None:
            rest = line.split("\t", 1)[1].strip() if "\t" in line else ""
            entries.append(Entry(addr, rest))
    return entries


def count_addresses(entries: list[Entry]) -> int:
    """How many different people ``entries`` hold: entries whose ``address_keys`` overlap an
    earlier entry's (other capitalisation, Unicode vs punycode domain) count once."""
    seen: set[str] = set()
    count = 0
    for entry in entries:
        keys = address_keys(entry.email)
        if seen.isdisjoint(keys):
            count += 1
        seen |= keys
    return count


def _read_bytes(path: Path) -> bytes:
    """The file's bytes, under a shared lock. Raises ``FileNotFoundError`` or ``LockTimeout``."""
    with path.open("rb") as f:
        _lock(f.fileno(), fcntl.LOCK_SH)
        try:
            return f.read()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_raw(data_dir: str | Path) -> bytes:
    """The file as it is (for the download); empty if it doesn't exist."""
    try:
        return _read_bytes(Path(data_dir) / FILENAME)
    except FileNotFoundError:
        return b""


def _decode(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace")  # drops a leading BOM


def load_unsubscribed(data_dir: str | Path) -> frozenset[str]:
    """Addresses in ``unsubscribed.txt``; empty if the file doesn't exist."""
    return parse_unsubscribed(_decode(read_raw(data_dir)))


def load_entries(data_dir: str | Path) -> list[Entry]:
    return parse_entries(_decode(read_raw(data_dir)))


def is_unsubscribed(addr: str, unsubscribed: frozenset[str]) -> bool:
    return not address_keys(addr).isdisjoint(unsubscribed)


# Called with the lock held, after deciding to append and before writing. Tests replace it to
# hold one writer at that point while another tries; it does nothing in the app.
def _before_append() -> None:
    pass


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def record_unsubscribe(data_dir: str | Path, email: str, at: datetime) -> bool:
    """Append ``email<TAB>at`` unless the address (in any form) is already in the file.

    Returns True if a line was appended, False if the address was already there. The check and
    the append happen under an exclusive lock and the line is fsynced before returning, so a
    "You're unsubscribed" page is only shown once the opt-out is on disk. A file whose last line
    has no newline (edited by hand) gets one first, so the new entry starts its own line.
    Raises ``OSError`` if the file can't be written (``LockTimeout`` if it stays locked).
    """
    if "\t" in email or "\n" in email or "\r" in email:
        raise ValueError("an address can't contain a tab or a line break")
    path = Path(data_dir) / FILENAME
    keys = address_keys(email)
    created = not path.exists()
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, FILE_MODE)
    try:
        _lock(fd, fcntl.LOCK_EX)
        with os.fdopen(os.dup(fd), "rb") as f:
            f.seek(0)
            existing = f.read()
        if not parse_unsubscribed(_decode(existing)).isdisjoint(keys):
            return False
        _before_append()
        line = f"{email}\t{at.isoformat(timespec='seconds')}\n".encode()
        if existing and not existing.endswith(b"\n"):
            line = b"\n" + line
        _write_all(fd, line)  # O_APPEND: always at the end, whatever else wrote meanwhile
        os.fsync(fd)
    finally:
        os.close(fd)  # also releases the lock
    if created:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)  # the new file's directory entry too
        finally:
            os.close(dir_fd)
    return True


class UnsubscribeWatcher:
    """``unsubscribed.txt``, re-read whenever its content changes.

    The send worker asks before every message, so an opt-out added mid-campaign is caught. Each
    call reads the file (a few KB) and compares a SHA-256 of it with the last one; the parse is
    redone only when it differs. Comparing content rather than size and mtime also catches an
    in-place edit that keeps both (same-length change within the mtime granularity, or an editor
    restoring the mtime).

    Any read error other than a missing file (including ``LockTimeout``) is raised, never taken as
    "nobody unsubscribed": the send worker's step then fails and nothing is sent until it can read.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self._digest: bytes | None = None
        self._keys: frozenset[str] = frozenset()

    def current(self) -> frozenset[str]:
        try:
            data = _read_bytes(self.data_dir / FILENAME)
        except FileNotFoundError:
            self._digest, self._keys = None, frozenset()
            return self._keys
        digest = hashlib.sha256(data).digest()
        if digest != self._digest:
            self._keys = parse_unsubscribed(_decode(data))
            self._digest = digest
        return self._keys

    def contains(self, addr: str) -> bool:
        return is_unsubscribed(addr, self.current())
