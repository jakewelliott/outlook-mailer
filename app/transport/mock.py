"""``MAIL_TRANSPORT=mock``: each message becomes a ``.eml`` file instead of an email.

Files go to ``DATA_DIR/outbox/<UTC timestamp>-<n>-<recipient>.eml`` (directory 0700, files 0600,
as they hold names and addresses). They open in any mail app. ``n`` counts messages written by
this transport; the recipient part keeps only ``a-z 0-9 . _ - @``.

For tests, ``decide(recipient, attempt)`` can script the outcome of each attempt (``attempt``
counts from 1 per recipient, however its address is capitalised): return a failed
``SendResult`` to fail that attempt without writing a file, or ``None`` / a successful result to
write it as usual.
"""

import asyncio
import os
import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.emails import normalise_key
from app.message import check_single_recipient
from app.transport.base import SendResult, Transport

Decide = Callable[[str, int], SendResult | None]

_UNSAFE = re.compile(r"[^a-z0-9._@-]+")
_MAX_NAME_PART = 64


def safe_recipient(recipient: str) -> str:
    """The recipient as a file-name part: lower case, unsafe runs as ``_``, at most 64 chars."""
    return _UNSAFE.sub("_", recipient.strip().lower())[:_MAX_NAME_PART].strip("._") or "unknown"


class MockTransport(Transport):
    name = "mock"

    def __init__(
        self,
        outbox: Path,
        *,
        decide: Decide | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.outbox = Path(outbox)
        self.decide = decide
        self.clock = clock or (lambda: datetime.now(UTC))
        self.attempts: Counter[str] = Counter()  # per recipient, by emails.normalise_key
        self.written: list[Path] = []
        self._count = 0

    async def send(self, mime: bytes, *, recipient: str) -> SendResult:
        check_single_recipient(mime, recipient)
        key = normalise_key(recipient)
        self.attempts[key] += 1
        if self.decide is not None:
            outcome = self.decide(recipient, self.attempts[key])
            if outcome is not None and not outcome.ok:
                return outcome
        self._count += 1
        stamp = self.clock().astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        name = f"{stamp}-{self._count:06d}-{safe_recipient(recipient)}.eml"
        try:
            path = await asyncio.to_thread(self._write, name, mime)
        except OSError as exc:
            return SendResult.retry(f"Could not write to the outbox: {exc.strerror or exc}")
        self.written.append(path)
        return SendResult.success(provider_id=path.name)

    def _write(self, name: str, mime: bytes) -> Path:
        self.outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.outbox, 0o700)
        path = self.outbox / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(fd, 0o600)  # whatever the umask
        with os.fdopen(fd, "wb") as f:
            f.write(mime)
        return path
