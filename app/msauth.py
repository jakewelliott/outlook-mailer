"""Microsoft sign-in with a device code, the token cache on disk, and the signed-in sender.

``MicrosoftAuth`` (one per app, ``app.state.msauth``) wraps MSAL's ``PublicClientApplication``
(authority ``MS_AUTHORITY``, normally ``/consumers``) with a ``SerializableTokenCache``:

- ``start_sign_in`` asks Microsoft for a device code and returns what the user needs: the code,
  MSAL's ``verification_uri`` (shown as given, never hardcoded), when it expires and MSAL's
  message. A background thread then polls ``acquire_token_by_device_flow`` (it blocks) until the
  user finishes, declines, the code expires (15 minutes) or ``cancel_sign_in`` is called. One
  sign-in at a time. The flow lives in memory only.
- A sign-in uses its own fresh cache. Only when it succeeds (and the profile from Graph ``/me``
  is a personal Microsoft account with a valid address) does that cache replace the current one,
  so there is never more than one account and cancelling a reconnect keeps the old sign-in.
  The address and display name are stored in the ``settings`` table (``get_sender``), for the
  ``From`` of every message (stage 12).
- ``get_access_token`` gets a token silently for the one cached account, or raises
  ``NotSignedIn`` / ``ReauthRequired`` (both ``AuthError``: stage 12 maps them to
  ``SendResult.auth``) or ``TokenUnavailable`` (Microsoft couldn't be reached; try later).
- The cache is ``DATA_DIR/msal_cache.json``, written atomically (temp file, then rename) with
  mode 0600 whenever MSAL changed it. With ``TOKEN_CACHE_KEY`` it is Fernet-encrypted; without,
  it is plain JSON and the Account page recommends setting the key. A file that can't be read
  (key changed or removed) means signed out, with a message; it is left in place until the next
  sign-in or sign-out replaces or deletes it. A plain file found when a key is set is read and
  rewritten encrypted.
- ``sign_out`` forgets the account: empty cache, file deleted, sender cleared.

Only ``start_sign_in``, the polling thread and ``get_access_token`` talk to Microsoft;
``status``, ``indicator`` and ``sender`` never do, and never wait for a call that does (see the
locks in ``MicrosoftAuth``), so pages can render them anywhere. After ``ReauthRequired`` the nav
shows "reconnect needed" until a sign-in or a silent renewal succeeds. With
``MAIL_TRANSPORT=mock`` nothing here calls Microsoft at all. Tokens, device codes, user codes
and cache contents are never logged.
"""

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import msal
from cryptography.fernet import Fernet, InvalidToken
from email_validator import EmailNotValidError, validate_email

from app.config import Settings
from app.db import Database
from app.emails import normalise_key

log = logging.getLogger("outlook_mailer.msauth")

# MSAL adds the reserved scopes (openid, profile, offline_access) itself and raises ValueError
# if they are passed in. offline_access is what gives the refresh token.
SCOPES = ("Mail.Send", "User.Read")
CACHE_FILE = "msal_cache.json"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
# The tenant ID every personal Microsoft account (outlook.com, hotmail.com, live.com) has.
CONSUMER_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"
# Below app.sender.SHUTDOWN_GRACE_SECONDS (15 s), like the Graph send timeout.
HTTP_TIMEOUT_SECONDS = 10
SENDER_ADDRESS_KEY = "ms_sender_address"
SENDER_NAME_KEY = "ms_sender_name"
TROUBLESHOOTING_URL = (
    "https://github.com/jakewelliott/outlook-mailer/blob/main/docs/entra-setup.md#troubleshooting"
)

STATE_MOCK = "mock"
STATE_NOT_CONFIGURED = "not_configured"
STATE_SIGNED_OUT = "signed_out"
STATE_SIGNING_IN = "signing_in"
STATE_SIGNED_IN = "signed_in"


class AuthError(Exception):
    """No usable sign-in: the user has to (re)connect on the Account page."""


class NotSignedIn(AuthError):
    pass


class ReauthRequired(AuthError):
    """The cached sign-in can't be renewed (expired, revoked, password changed...)."""


class AccountChanged(AuthError):
    """The connected account isn't the one the caller expected (it was switched meanwhile).

    ``expected``: the address the caller asked for; ``connected``: the one connected now.
    """

    def __init__(self, expected: str, connected: str) -> None:
        self.expected = expected
        self.connected = connected
        super().__init__(
            f"The connected Outlook account changed to {connected} (this message is from "
            f"{expected}). Nothing was sent."
        )


class TokenUnavailable(Exception):
    """Microsoft couldn't be reached or answered with a temporary error. Not a sign-in problem."""


class SignInError(Exception):
    """A sign-in couldn't be started. The message is meant for the user."""


@dataclass(frozen=True)
class Sender:
    address: str
    name: str | None


@dataclass(frozen=True)
class DeviceCode:
    user_code: str = field(repr=False)
    verification_uri: str
    expires_at: float  # epoch seconds
    message: str = field(repr=False)


@dataclass(frozen=True)
class Status:
    state: str
    sender: Sender | None = None
    code: DeviceCode | None = None
    seconds_left: int | None = None
    error: str | None = None
    notice: str | None = None
    cache_plaintext: bool = False  # no TOKEN_CACHE_KEY: the cache is stored unencrypted
    reauth: bool = False  # signed in, but Microsoft said the sign-in must be renewed

    @property
    def link_ok(self) -> bool:
        """Show the verification URI as a link only if it is https (it always is from MSAL)."""
        return self.code is not None and urlsplit(self.code.verification_uri).scheme == "https"


# --- the sender (settings table) ---------------------------------------------------------------


def get_sender(db: Database) -> Sender | None:
    """The signed-in address and display name, or None when signed out."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key IN (?, ?)",
            (SENDER_ADDRESS_KEY, SENDER_NAME_KEY),
        ).fetchall()
    values = {row["key"]: row["value"] for row in rows}
    address = values.get(SENDER_ADDRESS_KEY)
    if not address:
        return None
    return Sender(address, values.get(SENDER_NAME_KEY) or None)


def _store_sender(db: Database, sender: Sender | None) -> None:
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM settings WHERE key IN (?, ?)", (SENDER_ADDRESS_KEY, SENDER_NAME_KEY)
        )
        if sender is not None:
            conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                [(SENDER_ADDRESS_KEY, sender.address), (SENDER_NAME_KEY, sender.name or "")],
            )


# --- MSAL errors -> friendly text --------------------------------------------------------------

_PUBLIC_CLIENT_OFF = (
    "Microsoft refused the sign-in because the app registration doesn't allow public client "
    "flows. In the Entra admin center open the app's Authentication page, turn on "
    "“Allow public client flows”, select Save, and try again. Don't add a secret."
)
_WRONG_CLIENT_ID = (
    "Microsoft doesn't recognise the app. Check that MS_CLIENT_ID is the Application (client) "
    "ID from the app registration's Overview page (not the tenant or object ID)."
)
_WRONG_ACCOUNT_TYPE = (
    "The app registration or the account isn't set up for personal Microsoft accounts. Sign in "
    "with your @outlook.com account, and check the registration's Supported account types."
)
_DECLINED = (
    "The sign-in was declined on Microsoft's permissions page. Start again and select Accept."
)
_EXPIRED = "The code expired before the sign-in was finished. Start again to get a new code."
_REAUTH = (
    "The Microsoft sign-in has expired or was revoked (a password change, about 90 days unused, "
    "or access removed). Connect the account again."
)
_TENANT_BLOCKED = (
    "Microsoft has blocked the app's directory as inactive. See the troubleshooting guide."
)

_BY_CODE = {
    7000218: _PUBLIC_CLIENT_OFF,
    70002: _PUBLIC_CLIENT_OFF,
    50059: _WRONG_CLIENT_ID,
    700016: _WRONG_CLIENT_ID,
    9002332: _WRONG_ACCOUNT_TYPE,
    50020: _WRONG_ACCOUNT_TYPE,
    500208: _WRONG_ACCOUNT_TYPE,
    65004: _DECLINED,
    70008: _REAUTH,
    700082: _REAUTH,
    50173: _REAUTH,
    5000225: _TENANT_BLOCKED,
}
_BY_ERROR = {
    "authorization_declined": _DECLINED,
    "access_denied": _DECLINED,
    "expired_token": _EXPIRED,
    "code_expired": _EXPIRED,
    "invalid_grant": _REAUTH,
    "interaction_required": _REAUTH,
    "consent_required": _REAUTH,
    "unauthorized_client": (
        "Microsoft refused the app for personal accounts. Check MS_CLIENT_ID and the app "
        "registration's Supported account types."
    ),
}
# Errors that mean the cached sign-in is unusable (as opposed to Microsoft being unavailable).
_REAUTH_ERRORS = frozenset(
    {
        "invalid_grant",
        "interaction_required",
        "consent_required",
        "login_required",
        "invalid_client",
        "unauthorized_client",
    }
)
_AADSTS_RE = re.compile(r"AADSTS(\d{4,8})")
_ERROR_NAME_RE = re.compile(r"[a-z_]{1,40}")


def _error_codes(result: dict[str, Any]) -> list[int]:
    codes = [c for c in result.get("error_codes") or [] if isinstance(c, int)]
    if not codes:
        description = result.get("error_description")
        if isinstance(description, str):
            codes = [int(c) for c in _AADSTS_RE.findall(description)]
    return codes


def describe_error(result: dict[str, Any]) -> str:
    """Friendly text for an MSAL error result. Includes only the error name and AADSTS codes,
    never the description (which can echo request details)."""
    codes = _error_codes(result)
    error = result.get("error")
    error = error if isinstance(error, str) and _ERROR_NAME_RE.fullmatch(error) else None
    text = next((_BY_CODE[c] for c in codes if c in _BY_CODE), None)
    if text is None and error is not None:
        text = _BY_ERROR.get(error)
    if text is None:
        text = "Microsoft sign-in failed."
    reference = ", ".join([*(f"AADSTS{c}" for c in codes[:3]), *([error] if error else [])])
    return f"{text} ({reference})" if reference else text


# --- the cache file ----------------------------------------------------------------------------


class _CacheFile:
    def __init__(self, path: Path, key: str | None) -> None:
        self.path = path
        self._fernet = Fernet(key) if key else None

    @property
    def encrypted(self) -> bool:
        return self._fernet is not None

    def read(self) -> tuple[str | None, str | None, bool]:
        """``(state, problem, rewrite)``: the serialized cache (None if there is none or it
        can't be read), a message for the user if it can't, and whether to rewrite it (a plain
        file found while a key is set)."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None, None, False
        except OSError as exc:
            log.error("Can't read the Microsoft token cache: %s", exc.strerror or type(exc))
            return None, "The saved Microsoft sign-in can't be read. Connect again.", False
        rewrite = False
        if self._fernet is not None:
            try:
                raw = self._fernet.decrypt(raw)
            except InvalidToken:
                if not _is_json_object(raw):
                    log.warning("The Microsoft token cache can't be decrypted with TOKEN_CACHE_KEY")
                    return (
                        None,
                        "The saved Microsoft sign-in can't be decrypted: TOKEN_CACHE_KEY has "
                        "changed since it was saved. Connect the account again (or put the old "
                        "key back).",
                        False,
                    )
                rewrite = True  # saved before the key was set: encrypt it now
        elif not _is_json_object(raw):
            log.warning("The Microsoft token cache is encrypted but TOKEN_CACHE_KEY isn't set")
            return (
                None,
                "The saved Microsoft sign-in is encrypted, but TOKEN_CACHE_KEY isn't set. Set the "
                "key it was saved with, or connect the account again.",
                False,
            )
        return raw.decode(), None, rewrite

    def write(self, state: str) -> None:
        data = state.encode()
        if self._fernet is not None:
            data = self._fernet.encrypt(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp creates the file with mode 0600 (owner read/write only).
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".msal_cache.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


def _is_json_object(raw: bytes) -> bool:
    try:
        return isinstance(json.loads(raw), dict)
    except ValueError:
        return False


def _accounts(cache: msal.SerializableTokenCache) -> list[dict[str, Any]]:
    return list(cache.search(msal.TokenCache.CredentialType.ACCOUNT))


# --- the sign-in -------------------------------------------------------------------------------


@dataclass
class _Starting:
    """The sign-in slot while ``start_sign_in`` waits for Microsoft's device code."""

    cancel: threading.Event = field(default_factory=threading.Event)


@dataclass
class _Flow:
    flow: dict[str, Any] = field(repr=False)  # holds the device code: never log it
    code: DeviceCode
    cache: msal.SerializableTokenCache = field(repr=False)
    client: Any = field(repr=False)
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    committing: bool = False  # succeeded and being saved: too late to cancel


AppFactory = Callable[[msal.SerializableTokenCache], Any]


class MicrosoftAuth:
    """See the module docstring. Thread-safe, with two locks (taken in this order if both are):

    - ``_msal_lock`` serialises use of the current cache and client: silent token renewal
      (network), saving the cache file, replacing the cache after a sign-in, signing out.
    - ``_state_lock`` guards the small state the pages read (sender, sign-in slot, messages,
      flags). It is never held across network, file or database I/O, so ``status``,
      ``indicator`` and ``sender``, which take only this lock, never wait for Microsoft.

    Starting a sign-in and polling the device flow use their own cache and client, so they hold
    neither lock while they talk to Microsoft. ``_load_lock`` guards the one-time ``load``.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        app_factory: AppFactory | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.db = db
        self._file = _CacheFile(Path(settings.data_dir) / CACHE_FILE, settings.token_cache_key)
        self._factory = app_factory or self._default_factory
        self._clock = clock
        self._msal_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._loaded = False
        # Used under _msal_lock (and by load, before anything else can use them).
        self._cache = msal.SerializableTokenCache()
        self._client: Any = None
        # Under _state_lock.
        self._slot: _Starting | _Flow | None = None  # one sign-in at a time
        self._sender: Sender | None = None
        self._has_account = False  # the current cache holds an account
        # The sign-in looks unusable: the last silent renewal said so, or Graph refused its token
        # (401). While set, get_access_token forces a refresh (a cached token proves nothing),
        # and only a successful forced refresh, an accepted send or a new sign-in clears it.
        self._reauth = False
        self._error: str | None = None
        self._notice: str | None = None
        self._cache_problem: str | None = None

    # --- configuration ---

    @property
    def mock(self) -> bool:
        return self.settings.mail_transport == "mock"

    @property
    def configured(self) -> bool:
        return not self.mock and bool(self.settings.ms_client_id)

    def _require_configured(self) -> None:
        if self.mock:
            raise SignInError("Sign-in isn't needed with MAIL_TRANSPORT=mock.")
        if not self.settings.ms_client_id:
            raise SignInError("MS_CLIENT_ID isn't set.")

    def _default_factory(self, cache: msal.SerializableTokenCache) -> Any:
        # Building the app fetches Microsoft's OpenID configuration, so it is network I/O.
        return msal.PublicClientApplication(
            self.settings.ms_client_id,
            authority=self.settings.ms_authority,
            token_cache=cache,
            timeout=HTTP_TIMEOUT_SECONDS,
        )

    # --- loading and saving ---

    def load(self) -> None:
        """Read the cache file and the stored sender. Only touches local files; idempotent.

        Everything that uses the cache calls this first, so nothing else touches the cache
        while it is loaded."""
        if self._loaded or not self.configured:
            return
        with self._load_lock:
            if self._loaded:
                return
            state, problem, rewrite = self._file.read()
            cache = msal.SerializableTokenCache()
            if state is not None:
                try:
                    cache.deserialize(state)
                except ValueError:
                    cache = msal.SerializableTokenCache()
                    problem = "The saved Microsoft sign-in is damaged. Connect the account again."
                    log.warning("The Microsoft token cache isn't valid JSON")
                    rewrite = False
            self._cache = cache
            if rewrite:
                self._cache.has_state_changed = True
                self._save()
            sender = get_sender(self.db)
            has_account = bool(_accounts(cache))
            with self._state_lock:
                self._cache_problem = problem
                self._sender = sender
                self._has_account = has_account
            self._loaded = True

    def _save(self, *, strict: bool = False) -> None:
        """Write the cache if MSAL changed it (``_msal_lock`` held). A failed write is logged
        (the tokens stay usable in memory) unless ``strict``, when it is raised."""
        if not self._cache.has_state_changed:
            return
        try:
            self._file.write(self._cache.serialize())  # serialize() resets has_state_changed
        except OSError as exc:
            self._cache.has_state_changed = True  # try again next time
            log.error("Can't save the Microsoft token cache: %s", exc.strerror or type(exc))
            if strict:
                raise

    def _signed_in(self) -> bool:
        """``_state_lock`` held."""
        return self._sender is not None and self._has_account

    def _app(self) -> Any:
        """``_msal_lock`` held."""
        if self._client is None:
            self._client = self._factory(self._cache)
        return self._client

    # --- reading the state (never calls Microsoft or waits for it) ---

    def status(self) -> Status:
        if self.mock:
            return Status(STATE_MOCK)
        if not self.configured:
            return Status(STATE_NOT_CONFIGURED)
        self.load()
        with self._state_lock:
            signed_in = self._signed_in()
            reauth = self._reauth and signed_in
            base: dict[str, Any] = {
                "error": self._error or (_REAUTH if reauth else None) or self._cache_problem,
                "notice": self._notice,
                "cache_plaintext": not self._file.encrypted,
                "reauth": reauth,
            }
            slot = self._slot
            if isinstance(slot, _Flow):
                left = max(0, int(slot.code.expires_at - self._clock()))
                return Status(
                    STATE_SIGNING_IN,
                    sender=self._sender if signed_in else None,
                    code=slot.code,
                    seconds_left=left,
                    **base,
                )
            # While a sign-in is only starting (no code yet) the previous state is shown.
            if signed_in:
                return Status(STATE_SIGNED_IN, sender=self._sender, **base)
            return Status(STATE_SIGNED_OUT, **base)

    def indicator(self) -> str:
        """``connected``, ``reconnect needed``, ``not connected``, ``signing in`` or ``mock``
        for the nav bar."""
        status = self.status()
        if status.state == STATE_SIGNED_IN and status.reauth:
            return "reconnect needed"
        return {
            STATE_SIGNED_IN: "connected",
            STATE_SIGNING_IN: "signing in",
            STATE_MOCK: "mock",
        }.get(status.state, "not connected")

    def sender(self) -> Sender | None:
        """The signed-in sender, or None."""
        if not self.configured:
            return None
        self.load()
        with self._state_lock:
            return self._sender if self._signed_in() else None

    # --- device code sign-in ---

    def start_sign_in(self) -> DeviceCode:
        """Ask Microsoft for a device code and start polling for the result in a thread.

        Raises ``SignInError`` (message for the user) if it can't start, including when a
        sign-in is already in progress or being started.
        """
        self._require_configured()
        self.load()
        starting = _Starting()
        with self._state_lock:
            refused = None
            if isinstance(self._slot, _Flow):
                refused = "A sign-in is already in progress. Finish or cancel it first."
            elif self._slot is not None:
                refused = "A sign-in is already being started. Wait a moment, then reload the page."
            if refused is not None:
                # Shown on the Account page (the route redirects there and ignores the error).
                self._notice = refused
                raise SignInError(refused)
            self._slot = starting
            self._error = self._notice = None
        try:
            pending = self._request_code()  # network: no lock held
        except BaseException as exc:
            with self._state_lock:
                if self._slot is starting:
                    self._slot = None
                    if isinstance(exc, SignInError):
                        self._error = str(exc)
            raise
        with self._state_lock:
            if self._slot is not starting or starting.cancel.is_set():
                # Cancelled, signed out or shut down while waiting for Microsoft.
                raise SignInError("The sign-in was cancelled.")
            self._slot = pending
            assert pending.thread is not None
            pending.thread.start()
        log.info("Microsoft sign-in started; waiting for the code to be entered")
        return pending.code

    def _request_code(self) -> _Flow:
        cache = msal.SerializableTokenCache()  # used only if this sign-in succeeds
        try:
            client = self._factory(cache)
            flow = client.initiate_device_flow(scopes=list(SCOPES))
        except Exception as exc:
            log.warning("Couldn't start the Microsoft sign-in: %s", type(exc).__name__)
            raise SignInError(
                "Couldn't reach Microsoft to start the sign-in. Check the server's internet "
                "connection and MS_AUTHORITY, then try again."
            ) from None
        if not isinstance(flow, dict) or "user_code" not in flow or "device_code" not in flow:
            error = describe_error(flow if isinstance(flow, dict) else {})
            log.warning("Microsoft refused to start the sign-in: %s", error)
            raise SignInError(error)
        code = DeviceCode(
            user_code=str(flow["user_code"]),
            verification_uri=str(flow.get("verification_uri") or ""),
            expires_at=float(flow.get("expires_at") or self._clock() + 900),
            message=str(flow.get("message") or ""),
        )
        pending = _Flow(flow=flow, code=code, cache=cache, client=client)
        pending.thread = threading.Thread(
            target=self._poll, args=(pending,), name="msal-device-flow", daemon=True
        )
        return pending

    def cancel_sign_in(self) -> bool:
        """Stop the sign-in in progress. Returns False if there was none (or it had already
        succeeded and was being saved)."""
        with self._state_lock:
            slot = self._slot
            if slot is None or (isinstance(slot, _Flow) and slot.committing):
                return False
            self._stop(slot)
            self._notice = "Sign-in cancelled."
            self._error = None
        log.info("Microsoft sign-in cancelled")
        return True

    def _stop(self, slot: _Starting | _Flow) -> None:
        """``_state_lock`` held."""
        slot.cancel.set()
        if isinstance(slot, _Flow):
            slot.flow["expires_at"] = 0  # MSAL's documented way to end the polling loop
        if self._slot is slot:
            self._slot = None

    def _poll(self, pending: _Flow) -> None:
        """Runs in the sign-in thread. Talks to Microsoft without holding a lock."""

        def done(flow: dict[str, Any]) -> bool:
            return pending.cancel.is_set() or flow.get("expires_at", 0) < self._clock()

        try:
            result = pending.client.acquire_token_by_device_flow(pending.flow, exit_condition=done)
        except Exception as exc:
            log.warning("Microsoft sign-in failed: %s", type(exc).__name__)
            result = None
        if pending.cancel.is_set():
            return
        error: str | None
        sender: Sender | None = None
        if not isinstance(result, dict):
            error = "Couldn't reach Microsoft during the sign-in. Try again."
        elif "access_token" in result:
            try:
                sender = self._profile(result)
                error = None
            except SignInError as exc:
                error = str(exc)
        elif result.get("error") in ("authorization_pending", "slow_down"):
            error = _EXPIRED  # the loop ended because the code expired
        else:
            error = describe_error(result)
        if error is not None or sender is None:
            with self._state_lock:
                if self._slot is not pending or pending.cancel.is_set():
                    return  # cancelled (or superseded) while the last request was in flight
                self._slot = None
                self._error = error
            log.warning("Microsoft sign-in didn't complete: %s", error)
            return
        with self._msal_lock:
            with self._state_lock:
                if self._slot is not pending or pending.cancel.is_set():
                    return
                pending.committing = True
            error = self._commit(pending, sender)
            with self._state_lock:
                if self._slot is pending:
                    self._slot = None
                if error is not None:
                    self._error = error
                    return
                self._sender = sender
                self._has_account = True
                self._reauth = False
                self._cache_problem = None
                self._error = None
                self._notice = f"Connected as {sender.address}."
        log.info("Microsoft account connected: %s", sender.address)

    def _commit(self, pending: _Flow, sender: Sender) -> str | None:
        """Make the sign-in's cache the current one and save it and the sender
        (``_msal_lock`` held). Returns an error message, or None."""
        old = self._cache, self._client
        self._cache, self._client = pending.cache, pending.client
        self._cache.has_state_changed = True
        try:
            self._save(strict=True)
        except OSError:
            self._cache, self._client = old
            return (
                "Signed in, but the sign-in couldn't be saved in DATA_DIR (check that the "
                "folder is writable by the app), so it wasn't kept. Fix that and connect again."
            )
        try:
            _store_sender(self.db, sender)
        except Exception as exc:
            log.error("Can't save the Microsoft sender: %s", type(exc).__name__)
            self._cache, self._client = old
            try:  # put the previous sign-in back on disk
                if _accounts(self._cache):
                    self._cache.has_state_changed = True
                    self._save(strict=True)
                else:
                    self._file.delete()
            except OSError:
                # The file holds the new sign-in but the database the old sender: delete it so
                # the two can't disagree (after a restart this means signed out).
                log.error("Can't restore the previous Microsoft token cache; deleting it")
                with contextlib.suppress(OSError):
                    self._file.delete()
            return "Signed in, but the address couldn't be saved in the database. Connect again."
        return None

    def _profile(self, result: dict[str, Any]) -> Sender:
        """Check the account is personal and read its address from Graph ``/me``."""
        claims = result.get("id_token_claims") or {}
        if claims.get("tid") != CONSUMER_TENANT_ID:
            raise SignInError(
                "That is a work or school account. Sign in with your personal @outlook.com account."
            )
        try:
            response = httpx.get(
                GRAPH_ME,
                headers={"Authorization": f"Bearer {result['access_token']}"},
                params={"$select": "displayName,mail,userPrincipalName"},
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            log.warning("Couldn't read the Microsoft profile: %s", type(exc).__name__)
            raise SignInError("Couldn't read your Microsoft profile. Try again.") from None
        if response.status_code != 200:
            raise SignInError(
                f"Couldn't read your Microsoft profile (HTTP {response.status_code}). Try again."
            )
        try:
            me = response.json()
        except ValueError:
            me = None
        if not isinstance(me, dict):
            raise SignInError("Microsoft returned an unreadable profile. Try again.")
        raw = me.get("mail") or me.get("userPrincipalName") or claims.get("preferred_username")
        try:
            address = validate_email(str(raw or ""), check_deliverability=False).normalized
        except EmailNotValidError:
            raise SignInError(
                "Your Microsoft account has no usable email address. Sign in with your "
                "@outlook.com account."
            ) from None
        name = me.get("displayName")
        name = " ".join(name.split())[:200] if isinstance(name, str) and name.strip() else None
        return Sender(address, name)

    # --- tokens ---

    def get_access_token(self, expected_address: str | None = None) -> str:
        """An access token for Graph (``Mail.Send``), renewed silently when needed.

        With ``expected_address``, raises ``AccountChanged`` (an ``AuthError``) unless the
        connected account is that address: checked under the lock a sign-in or sign-out takes to
        switch accounts, so the token returned belongs to it.

        Raises ``NotSignedIn`` or ``ReauthRequired`` (both ``AuthError``), or
        ``TokenUnavailable`` when Microsoft can't be reached. Calls are serialised (they share
        the cache); the pages' state is never locked meanwhile.
        """
        if not self.configured:
            raise NotSignedIn("Microsoft sign-in isn't configured.")
        self.load()
        with self._msal_lock:
            with self._state_lock:
                signed_in = self._signed_in()
                force = self._reauth
                current = self._sender
            if not signed_in:
                raise NotSignedIn("No Microsoft account is connected.")
            if (
                expected_address is not None
                and current is not None
                and normalise_key(current.address) != normalise_key(expected_address)
            ):
                raise AccountChanged(expected_address, current.address)
            try:
                app = self._app()
                accounts = app.get_accounts()
                if not accounts:
                    raise NotSignedIn("No Microsoft account is connected.")
                result = app.acquire_token_silent_with_error(
                    list(SCOPES), account=accounts[0], force_refresh=force
                )
            except AuthError:
                raise
            except Exception as exc:
                log.warning("Couldn't renew the Microsoft token: %s", type(exc).__name__)
                raise TokenUnavailable("Couldn't reach Microsoft to renew the sign-in.") from None
            finally:
                self._save()
            if result and "access_token" in result:
                if force:  # a new token from Microsoft, not the one Graph refused
                    with self._state_lock:
                        self._reauth = False
                return str(result["access_token"])
            if result is None or result.get("error") in _REAUTH_ERRORS:
                with self._state_lock:
                    self._reauth = True
                log.warning("The Microsoft sign-in needs to be renewed")
                raise ReauthRequired(_REAUTH)
            text = describe_error(result)
            log.warning("Microsoft couldn't renew the token: %s", text)
            raise TokenUnavailable(text)

    def note_token_rejected(self) -> None:
        """Graph answered 401 to a token from ``get_access_token``: show "reconnect needed", and
        make the next ``get_access_token`` force a refresh instead of handing out the cached
        token again. Cleared by a successful forced refresh, an accepted send or a sign-in."""
        with self._state_lock:
            if self._signed_in():
                self._reauth = True

    def note_token_accepted(self) -> None:
        """Graph accepted a send: the sign-in works, so "reconnect needed" no longer applies."""
        with self._state_lock:
            self._reauth = False

    # --- signing out ---

    def sign_out(self) -> None:
        """Cancel any sign-in, forget the account, delete the cache file, clear the sender."""
        if not self.configured:
            return
        self.load()
        with self._msal_lock:
            with self._state_lock:
                if self._slot is not None:
                    self._stop(self._slot)
            self._cache = msal.SerializableTokenCache()
            self._client = None
            self._file.delete()
            _store_sender(self.db, None)
            with self._state_lock:
                self._sender = None
                self._has_account = False
                self._reauth = False
                self._cache_problem = self._error = None
                self._notice = "Signed out. The saved Microsoft sign-in has been deleted."
        log.info("Microsoft account signed out")

    def shutdown(self) -> None:
        """At app shutdown: end a sign-in in progress (waits up to 3 s for its thread, so call
        it from a thread, not the event loop)."""
        with self._state_lock:
            slot = self._slot
            if slot is not None:
                self._stop(slot)
        if isinstance(slot, _Flow) and slot.thread is not None:
            slot.thread.join(timeout=3)
