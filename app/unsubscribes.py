"""Read-only access to ``DATA_DIR/unsubscribed.txt`` (writing it comes with the unsubscribe pages).

Format: one opt-out per line, ``email<TAB>ISO-8601 datetime``. Blank lines and lines whose first
field isn't an address are ignored. A line holding only an address (e.g. added by hand) still
counts: when in doubt, don't email someone who may have opted out.

An address can be written more than one way: an international domain as Unicode
(``bücher.example``) or as punycode (``xn--bcher-kva.example``). ``address_keys`` gives every form
of an address (``emails.normalise_key`` of the address as written, and of email-validator's
Unicode and ASCII forms when it is valid), and the parsed list holds all of them, so an opt-out
written either way matches a recipient written either way.
"""

import os
from pathlib import Path

from email_validator import EmailNotValidError, validate_email

from app.emails import normalise_key

FILENAME = "unsubscribed.txt"


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


def parse_unsubscribed(text: str) -> frozenset[str]:
    """Lower-case addresses found in the file's text (each in all its forms)."""
    emails: set[str] = set()
    for line in text.splitlines():
        email = normalise_key(line.split("\t", 1)[0])
        local, at, domain = email.partition("@")
        if at and local and domain and " " not in email:
            emails |= address_keys(email)
    return frozenset(emails)


def load_unsubscribed(data_dir: str | Path) -> frozenset[str]:
    """Addresses in ``unsubscribed.txt``; empty if the file doesn't exist."""
    try:
        text = (Path(data_dir) / FILENAME).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return frozenset()
    return parse_unsubscribed(text)


def is_unsubscribed(addr: str, unsubscribed: frozenset[str]) -> bool:
    return not address_keys(addr).isdisjoint(unsubscribed)


class UnsubscribeWatcher:
    """``unsubscribed.txt``, re-read whenever it changes (checked with ``os.stat`` on each call).

    The send worker asks before every message, so an opt-out added mid-campaign is caught.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self._stamp: tuple[int, int, int] | None = None
        self._keys: frozenset[str] = frozenset()

    def current(self) -> frozenset[str]:
        try:
            st = os.stat(self.data_dir / FILENAME)
        except FileNotFoundError:
            self._stamp, self._keys = None, frozenset()
            return self._keys
        stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
        if stamp != self._stamp:
            self._keys = load_unsubscribed(self.data_dir)
            self._stamp = stamp
        return self._keys

    def contains(self, addr: str) -> bool:
        return is_unsubscribed(addr, self.current())
