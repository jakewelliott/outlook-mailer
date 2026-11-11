"""Read-only access to ``DATA_DIR/unsubscribed.txt`` (writing it comes with the unsubscribe pages).

Format: one opt-out per line, ``email<TAB>ISO-8601 datetime``. Blank lines and lines whose first
field isn't an address are ignored. A line holding only an address (e.g. added by hand) still
counts: when in doubt, don't email someone who may have opted out.
"""

from pathlib import Path

from app.emails import normalise_key

FILENAME = "unsubscribed.txt"


def parse_unsubscribed(text: str) -> frozenset[str]:
    """Lower-case addresses found in the file's text."""
    emails: set[str] = set()
    for line in text.splitlines():
        email = normalise_key(line.split("\t", 1)[0])
        local, at, domain = email.partition("@")
        if at and local and domain and " " not in email:
            emails.add(email)
    return frozenset(emails)


def load_unsubscribed(data_dir: str | Path) -> frozenset[str]:
    """Addresses in ``unsubscribed.txt``; empty if the file doesn't exist."""
    try:
        text = (Path(data_dir) / FILENAME).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return frozenset()
    return parse_unsubscribed(text)
