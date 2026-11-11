"""The transport interface the send engine (stage 10) uses.

Failures are results, not exceptions: ``send`` returns a ``SendResult`` saying what happened and
what the engine should do next (retry after a pause, give up on this recipient, or stop the
campaign because the sign-in is gone). That keeps retry and stop decisions in one place, the
engine, and lets the mock script every outcome. ``send`` only raises for a bug or a message that
breaks the rules (``app.message.MessageError``, e.g. not addressed to ``recipient``); the engine
should treat any exception as a failed attempt for that recipient, not crash the worker.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SendResult:
    """The outcome of one attempt to send one message.

    - ``ok``: accepted (Graph answers 202; the mock wrote the file). ``provider_id`` may say where.
    - ``retryable``: worth trying again later, after ``retry_after`` seconds if the provider said
      (``Retry-After``), else the engine's own backoff. Throttling, timeouts, 5xx.
    - ``auth_error``: the sign-in is missing, expired or revoked. Not retryable: the engine stops
      the campaign until Jake signs in again.
    - otherwise a permanent failure for this recipient (e.g. Graph rejects the message).
    """

    ok: bool
    retryable: bool = False
    retry_after: float | None = None
    error: str | None = None
    provider_id: str | None = None
    auth_error: bool = False

    def __post_init__(self) -> None:
        if self.ok and (self.retryable or self.auth_error or self.error is not None):
            raise ValueError("a successful result can't carry an error")
        if not self.ok and not self.error:
            raise ValueError("a failed result needs an error message")
        if self.auth_error and self.retryable:
            raise ValueError("an auth error is not retryable (it stops the campaign)")
        if self.retry_after is not None and (not self.retryable or self.retry_after < 0):
            raise ValueError("retry_after is a non-negative delay for retryable failures only")

    @classmethod
    def success(cls, provider_id: str | None = None) -> "SendResult":
        return cls(ok=True, provider_id=provider_id)

    @classmethod
    def retry(cls, error: str, retry_after: float | None = None) -> "SendResult":
        return cls(ok=False, retryable=True, retry_after=retry_after, error=error)

    @classmethod
    def permanent(cls, error: str) -> "SendResult":
        return cls(ok=False, error=error)

    @classmethod
    def auth(cls, error: str) -> "SendResult":
        return cls(ok=False, auth_error=True, error=error)


class Transport(ABC):
    """Sends one MIME message (from ``app.message.build_mime``) to its one recipient."""

    name: str

    @abstractmethod
    async def send(self, mime: bytes, *, recipient: str) -> SendResult:
        """Send ``mime``, which must be addressed to ``recipient`` only."""

    async def aclose(self) -> None:  # noqa: B027 (optional hook; most transports hold nothing)
        """Release connections. Safe to call more than once."""
