"""``MAIL_TRANSPORT=graph``: send each message through Microsoft Graph ``POST /me/sendMail``.

The request body is the whole MIME message base64-encoded (``message.graph_payload``), sent as
``Content-Type: text/plain`` (docs/RESEARCH.md 2.6). The access token comes from
``MicrosoftAuth.get_access_token``, called in a thread because MSAL may renew it over the network.
With ``expected_sender`` (the From the caller checked) it only hands out a token while that
address is still the connected account; otherwise (``AccountChanged``) nothing is sent and the
result is an auth error with ``sender_changed`` set, so the engine names the account change.

How each outcome maps to a ``SendResult`` (the policy is in ``app.transport.base``; the reasons
for the account-level codes are in docs/DECISIONS.md "Graph responses"):

=========================================================  =====================================
What happened                                              Result
=========================================================  =====================================
2xx (202 Accepted)                                         success, ``provider_id`` = request-id
``NotSignedIn`` / ``ReauthRequired``, 401                  auth (stop the campaign)
403, or an account-level ``error.code``                    auth (stop; the recipient is sent on
(``ACCOUNT_CODES``: blocked for sending, no permission,    resume), with a message saying what
send-as refused, mailbox quota or REST not enabled) with   to do
a 4xx, 429, 503 or 509 status (a refusal: nothing sent)
An account-level ``error.code`` with any other status      unknown for this recipient (never
(500, 502, 504, other 5xx, 1xx/3xx)                        resent), with ``stop_reason``: the
                                                           campaign stops too
``TokenUnavailable``, token step over budget               retry (no request was sent)
429, 503 (and 509)                                         retry, ``retry_after`` from
                                                           ``Retry-After`` (seconds or HTTP
                                                           date), else ``DEFAULT_RETRY_AFTER``
``ConnectError``, ``ConnectTimeout``, ``PoolTimeout``,     retry (the request never left)
``UnsupportedProtocol``, proxy errors
Read/write timeout, ``RemoteProtocolError``, read/write    unknown (it may have been accepted)
errors, the overall budget running out mid-request, 500,
502, 504 and any other 5xx, 1xx/3xx
Any other 4xx (400, 404, 413, ...)                         permanent, with Graph's ``error.code``
=========================================================  =====================================

Only a status that says the request was refused (4xx) or not processed (the 429/503/509
throttles, already trusted as "certainly not sent" for retries) may become auth, because an
auth result puts the recipient back to be sent again on resume. A 500/502/504 doesn't say
whether the message went out, so an account-level code there can stop the campaign but can't
make the recipient retryable (docs/DECISIONS.md "Graph 5xx with an account-level code").

Time budget: the token step and the request together are cut off after
``SEND_BUDGET_SECONDS`` (12 s), below ``app.sender.SHUTDOWN_GRACE_SECONDS`` (15 s), so a send in
flight at shutdown always ends and is recorded. The budget is measured on ``monotonic`` (a
fake clock in tests). Within it, httpx gets ``REQUEST_TIMEOUT``
(connect 5 s, read/write 10 s, pool 2 s), shortened to what is left of the budget.

Logs carry the status, Graph's error code and request-id only: never the token, the payload or
the response body. Error texts stored in the database carry the code, never Graph's message.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.message import check_single_recipient, graph_payload
from app.msauth import AccountChanged, AuthError, MicrosoftAuth, TokenUnavailable
from app.transport.base import SendResult, Transport

log = logging.getLogger("outlook_mailer.graph")

SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
SEND_BUDGET_SECONDS = 12.0
REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=2.0)
# Too little budget left after the token step to make a request worth starting.
MIN_REQUEST_SECONDS = 3.0
DEFAULT_RETRY_AFTER = 60.0
MAX_RETRY_AFTER = 24 * 3600.0

THROTTLE_STATUSES = frozenset({429, 503, 509})

_ACCOUNT_BLOCKED = (
    "Microsoft is blocking this Outlook account from sending ({code}). This usually follows "
    "'unusual activity': sign in at outlook.com, complete any verification it asks for and "
    "wait before resuming; check that ordinary emails from outlook.com go out."
)
_NO_PERMISSION = (
    "Microsoft refused to let the app send from this account ({code}): the Mail.Send "
    "permission is missing or was removed. Connect the account again on the Account page and "
    "select Accept on the permissions page."
)
_SEND_AS = (
    "Microsoft refused the sender address ({code}). Connect the account again on the Account "
    "page so the app sends from the signed-in address."
)
_QUOTA = (
    "Microsoft says the account has hit a limit ({code}): its sending limit for the day or its "
    "mailbox storage is full. Wait a day (or free space in the mailbox), then resume."
)
_NO_MAILBOX = (
    "Microsoft says this account has no mailbox the app can send from ({code}). Use an "
    "@outlook.com account that has been opened in Outlook on the web at least once."
)
_FORBIDDEN = (
    "Microsoft refused to send from this account ({code}). Check that you can send "
    "from outlook.com, then connect the account again on the Account page and resume."
)

# Error codes that mean every message from the account would fail: stop the campaign.
ACCOUNT_CODES: dict[str, str] = {
    "ErrorMessageSubmissionBlocked": _ACCOUNT_BLOCKED,
    "ErrorAccountSuspend": _ACCOUNT_BLOCKED,
    "ErrorAccountDisabled": _ACCOUNT_BLOCKED,
    "ErrorAccessDenied": _NO_PERMISSION,
    "AccessDenied": _NO_PERMISSION,
    "Authorization_RequestDenied": _NO_PERMISSION,
    "ErrorSendAsDenied": _SEND_AS,
    "SendAsDenied": _SEND_AS,
    "ErrorQuotaExceeded": _QUOTA,
    "ErrorSubmissionQuotaExceeded": _QUOTA,
    "ErrorExceededMessageLimit": _QUOTA,
    "MailboxNotEnabledForRESTAPI": _NO_MAILBOX,
    "MailboxNotSupportedForRESTAPI": _NO_MAILBOX,
}

_CODE_RE = re.compile(r"[A-Za-z0-9_.\-]{1,80}")

Now = Callable[[], datetime]
# Seconds on a monotonic clock: the time budget is measured with it (tests pass a fake one).
Monotonic = Callable[[], float]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def error_codes(response: httpx.Response) -> list[str]:
    """Graph's ``error.code`` and nested ``innerError`` codes, outermost first. Only strings that
    look like codes are kept; the message and the rest of the body are ignored."""
    try:
        body = response.json()
    except ValueError:
        return []
    codes: list[str] = []
    node: Any = body.get("error") if isinstance(body, dict) else None
    for _ in range(5):
        if not isinstance(node, dict):
            break
        code = node.get("code")
        if isinstance(code, str) and _CODE_RE.fullmatch(code):
            codes.append(code)
        node = node.get("innerError") or node.get("innererror")
    return codes


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """``Retry-After`` as seconds from ``now`` (delta-seconds or an HTTP date), capped at a day.
    None if missing or unreadable; a date in the past gives 0."""
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{1,10}(\.\d+)?", value):
        return min(float(value), MAX_RETRY_AFTER)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return min(max(0.0, (when - now).total_seconds()), MAX_RETRY_AFTER)


def _request_id(response: httpx.Response) -> str | None:
    value = response.headers.get("request-id") or response.headers.get("client-request-id")
    return value if value and _CODE_RE.fullmatch(value) else None


class GraphTransport(Transport):
    name = "graph"

    def __init__(
        self,
        msauth: MicrosoftAuth,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout = REQUEST_TIMEOUT,
        budget: float = SEND_BUDGET_SECONDS,
        now: Now = _utcnow,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self.msauth = msauth
        self.timeout = timeout
        self.budget = budget
        self.now = now
        self.monotonic = monotonic
        self._client = client
        self._owns_client = client is None

    async def open(self) -> None:
        """Create the shared HTTP client (app lifespan). Called again lazily by ``send``."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
            self._owns_client = True

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None and self._owns_client:
            await client.aclose()

    def _left(self, deadline: float) -> float:
        return max(0.0, deadline - self.monotonic())

    async def send(
        self, mime: bytes, *, recipient: str, expected_sender: str | None = None
    ) -> SendResult:
        """``expected_sender``: the address the message is from (the account the caller checked).
        If the connected account is no longer that one, nothing is sent (an auth result)."""
        check_single_recipient(mime, recipient)  # MessageError: nothing sent
        payload = graph_payload(mime)  # MessageError if too large
        await self.open()
        assert self._client is not None
        deadline = self.monotonic() + self.budget

        try:
            async with asyncio.timeout(self._left(deadline)):
                token = await asyncio.to_thread(
                    self.msauth.get_access_token, expected_address=expected_sender
                )
        except AccountChanged as exc:  # an AuthError: nothing sent, the engine says why
            return SendResult.auth(str(exc), sender_changed=(exc.expected, exc.connected))
        except AuthError as exc:
            return SendResult.auth(str(exc))
        except TokenUnavailable as exc:
            return SendResult.retry(f"Couldn't get a Microsoft token: {exc}")
        except TimeoutError:
            return SendResult.retry("Microsoft took too long to renew the sign-in.")
        except Exception as exc:  # MSAL bug or similar; no request was sent
            log.warning("Unexpected error getting a token: %s", type(exc).__name__)
            return SendResult.retry(f"Couldn't get a Microsoft token ({type(exc).__name__}).")

        left = self._left(deadline)
        if left < MIN_REQUEST_SECONDS:
            return SendResult.retry("Microsoft took too long to renew the sign-in.")
        timeout = httpx.Timeout(
            connect=min(self.timeout.connect or left, left),
            read=min(self.timeout.read or left, left),
            write=min(self.timeout.write or left, left),
            pool=min(self.timeout.pool or left, left),
        )
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "text/plain"}
        try:
            async with asyncio.timeout(left):
                response = await self._client.post(
                    SEND_MAIL_URL, content=payload.encode("ascii"), headers=headers, timeout=timeout
                )
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.UnsupportedProtocol,
            httpx.ProxyError,
        ) as exc:
            log.warning("Couldn't reach Microsoft Graph: %s", type(exc).__name__)
            return SendResult.retry(f"Couldn't reach Microsoft ({type(exc).__name__}).")
        except TimeoutError:
            log.warning("Microsoft Graph didn't answer within %.0f s", self.budget)
            return SendResult.unknown(f"no answer from Microsoft within {self.budget:.0f} s")
        except httpx.HTTPError as exc:
            log.warning("Microsoft Graph request failed after sending: %s", type(exc).__name__)
            return SendResult.unknown(f"connection problem while sending ({type(exc).__name__})")
        return self._result(response)

    def _result(self, response: httpx.Response) -> SendResult:
        status = response.status_code
        rid = _request_id(response)
        if 200 <= status < 300:
            self.msauth.note_token_accepted()
            return SendResult.success(provider_id=rid)
        codes = error_codes(response)
        log.warning(
            "Microsoft Graph sendMail answered %s (%s), request-id %s",
            status,
            ", ".join(codes) or "no code",
            rid or "-",
        )
        detail = f"HTTP {status}" + (f" {', '.join(codes)}" if codes else "")

        account = next((ACCOUNT_CODES[c] for c in codes if c in ACCOUNT_CODES), None)
        if account is not None:
            if 400 <= status < 500 or status in THROTTLE_STATUSES:
                # Refused, nothing sent: stop, and the recipient is sent on resume.
                return SendResult.auth(account.format(code=detail))
            # A 500/502/504 (or 1xx/3xx) proves nothing about this message: it may have gone
            # out, so it must never be resent. Still stop: the account is in trouble.
            return SendResult.unknown(
                f"Microsoft answered {detail}", stop_reason=account.format(code=detail)
            )
        if status == 401:
            self.msauth.note_token_rejected()
            return SendResult.auth(
                f"Microsoft didn't accept the sign-in ({detail}). Connect the account again on "
                "the Account page."
            )
        if status == 403:
            return SendResult.auth(_FORBIDDEN.format(code=detail))
        if status in THROTTLE_STATUSES:
            wait = parse_retry_after(response.headers.get("retry-after"), self.now())
            return SendResult.retry(
                f"Microsoft asked to wait ({detail})",
                retry_after=DEFAULT_RETRY_AFTER if wait is None else wait,
            )
        if 400 <= status < 500:
            return SendResult.permanent(f"Microsoft refused the message ({detail}).")
        return SendResult.unknown(f"Microsoft answered {detail}")
