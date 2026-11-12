"""The transport interface the send engine (stage 10) uses.

Failures are results, not exceptions: ``send`` returns a ``SendResult`` saying what happened and
what the engine should do next (wait and retry, give up on this recipient, report that nobody
knows whether it went out, or stop the campaign because the sign-in is gone). That keeps retry
and stop decisions in one place, the engine, and lets the mock script every outcome. ``send``
only raises for a bug or a message that breaks the rules (``app.message.MessageError``, e.g. not
addressed to ``recipient``, checked before anything is sent). The engine treats a
``MessageError`` as permanent and **any other exception as outcome unknown** (it may have been
raised after the request went out), never as retryable.

How the Graph transport maps what happens to a result (``app.transport.graph`` has the full
table). The rule: retry only when the message certainly wasn't accepted; when in doubt,
``unknown`` (a double email can't be taken back, a missing one can be sent by hand after
checking Sent Items).

- ``success``: 202 Accepted.
- ``retry``: the connection couldn't be made or timed out *before the request was sent*
  (DNS, connect error, connect/pool timeout); 429 or 503, with ``retry_after`` from the
  ``Retry-After`` header (seconds, or an HTTP date converted to seconds; 60 s when missing).
- ``unknown``: read or write timeout once sending has started, the connection dropped before
  the whole response arrived, 500/502/504 (and any other 5xx but 503/509), whatever error code
  the body carries. When that code is an account-level one, the result also carries a
  ``stop_reason``: the recipient is reported unknown (never resent) *and* the campaign stops.
- ``auth``: 401, or the token can't be refreshed (``invalid_grant``, no account in the cache),
  and a refusal that says the *account* can't send (403, or a 4xx/429/503/509 carrying a Graph
  error code such as ``ErrorMessageSubmissionBlocked`` or ``ErrorAccessDenied``): every later
  message would fail too, so the campaign stops until Jake fixes it, and the recipient is tried
  again on resume. Only answers that say the request was refused can be ``auth``, since the
  recipient is resent.
- ``permanent``: any other 4xx (the message itself was refused), e.g. 400 or 413.

The Graph request timeout must stay below ``app.sender.SHUTDOWN_GRACE_SECONDS`` (15 s), which is
below the container's stop grace period (30 s), so a send in flight at shutdown finishes and is
recorded.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SendResult:
    """The outcome of one attempt to send one message.

    - ``ok``: accepted (Graph answers 202; the mock wrote the file). ``provider_id`` may say where.
    - ``retryable``: certainly not sent and worth trying again later. With ``retry_after`` (the
      provider's ``Retry-After``, in seconds) it means *throttled*: the engine sends nothing to
      anyone before then. Without it the engine uses its own backoff for this recipient.
    - ``outcome_unknown`` (``SendResult.unknown``): it may or may not have been sent (e.g. a
      timeout after the request went out). Never retried; the recipient is reported failed with
      "check Sent Items", and the send still counts against the daily cap. With ``stop_reason``
      (only allowed here) the engine also stops the campaign for that reason, e.g. a 500 that
      says the account is blocked: later messages would fail too, but this one may have gone.
    - ``auth_error``: the sign-in is missing, expired or revoked, or the account was refused
      before anything was sent. Not retryable: the engine stops the campaign until Jake fixes it,
      and the recipient is sent on resume.
      With ``sender_changed`` (``(was, now)``, only allowed here) the reason is that the
      connected Outlook account changed from ``was`` to ``now`` since the caller checked it, so
      the engine says that instead of blaming Microsoft.
    - otherwise a permanent failure for this recipient (e.g. Graph rejects the message).

    At most one of ``retryable``, ``outcome_unknown`` and ``auth_error`` is set on a failure.
    """

    ok: bool
    retryable: bool = False
    retry_after: float | None = None
    error: str | None = None
    provider_id: str | None = None
    auth_error: bool = False
    outcome_unknown: bool = False
    stop_reason: str | None = None
    sender_changed: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        if self.ok and (
            self.retryable or self.auth_error or self.outcome_unknown or self.error is not None
        ):
            raise ValueError("a successful result can't carry an error")
        if not self.ok and not self.error:
            raise ValueError("a failed result needs an error message")
        if sum((self.retryable, self.auth_error, self.outcome_unknown)) > 1:
            raise ValueError("a result is at most one of retryable, unknown and auth error")
        if self.retry_after is not None and (not self.retryable or self.retry_after < 0):
            raise ValueError("retry_after is a non-negative delay for retryable failures only")
        if self.stop_reason is not None and (not self.outcome_unknown or not self.stop_reason):
            raise ValueError("stop_reason is a non-empty reason for unknown outcomes only")
        if self.sender_changed is not None and not self.auth_error:
            raise ValueError("sender_changed is for auth errors only")

    @property
    def throttled(self) -> bool:
        """The provider asked us to wait (``Retry-After``)."""
        return self.retryable and self.retry_after is not None

    @classmethod
    def success(cls, provider_id: str | None = None) -> "SendResult":
        return cls(ok=True, provider_id=provider_id)

    @classmethod
    def retry(cls, error: str, retry_after: float | None = None) -> "SendResult":
        return cls(ok=False, retryable=True, retry_after=retry_after, error=error)

    @classmethod
    def unknown(cls, error: str, stop_reason: str | None = None) -> "SendResult":
        return cls(ok=False, outcome_unknown=True, error=error, stop_reason=stop_reason)

    @classmethod
    def permanent(cls, error: str) -> "SendResult":
        return cls(ok=False, error=error)

    @classmethod
    def auth(cls, error: str, sender_changed: tuple[str, str] | None = None) -> "SendResult":
        return cls(ok=False, auth_error=True, error=error, sender_changed=sender_changed)


class Transport(ABC):
    """Sends one MIME message (from ``app.message.build_mime``) to its one recipient."""

    name: str

    @abstractmethod
    async def send(
        self, mime: bytes, *, recipient: str, expected_sender: str | None = None
    ) -> SendResult:
        """Send ``mime``, which must be addressed to ``recipient`` only. ``expected_sender``: the
        account the caller checked it may send from (Graph refuses if that changed since)."""

    async def open(self) -> None:  # noqa: B027 (optional hook)
        """Get ready to send (the app lifespan calls it at startup). Safe to call more than once."""

    async def aclose(self) -> None:  # noqa: B027 (optional hook; most transports hold nothing)
        """Release connections. Safe to call more than once."""
