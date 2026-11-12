"""Microsoft sign-in, token cache and Account page. MSAL and Graph are faked: no network.

The fake relies on two MSAL internals, checked against msal 1.39.0 (the lowest version
pyproject allows): ``TokenCache.add`` with an event dict (``client_id``, ``scope``,
``token_endpoint``, ``response``, ``params``, ``data``) to store a token response the way
``acquire_token_by_device_flow`` does, and ``ClientApplication._decorate_scope`` to reject
reserved scopes as MSAL does. If an MSAL upgrade breaks these tests, check those two first.
"""

import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import msal
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import msauth
from app.config import Settings, load_settings
from app.db import Database
from app.main import create_app
from app.msauth import (
    CACHE_FILE,
    CONSUMER_TENANT_ID,
    GRAPH_ME,
    SCOPES,
    AccountChanged,
    MicrosoftAuth,
    NotSignedIn,
    ReauthRequired,
    SignInError,
    TokenUnavailable,
    describe_error,
    get_sender,
)

BASE = "https://mail.example.com"
CLIENT_ID = "00001111-aaaa-2222-bbbb-3333cccc4444"
USER_CODE = "UCODE7Q9X"
DEVICE_CODE = "DEVICECODE-very-secret-0123456789"
ACCESS_TOKEN = "eyJ0eXAi.ACCESS-TOKEN-secret.sig"
REFRESH_TOKEN = "M.C5_BAY.REFRESH-TOKEN-secret"
# Deliberately not microsoft.com/link: the app must show whatever MSAL returns.
VERIFY_URI = "https://www.microsoft.com/link?from=msal-test"
TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def token_response(tid: str = CONSUMER_TENANT_ID, username: str = "jake@outlook.com") -> dict:
    oid = "oid-" + username  # one ID per account, as Microsoft gives
    claims = {"tid": tid, "oid": oid, "preferred_username": username, "name": "Jake"}
    return {
        "token_type": "Bearer",
        "scope": "Mail.Send User.Read",
        "expires_in": 3600,
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "id_token_claims": claims,
        "client_info": b64({"uid": oid, "utid": tid}),
    }


@dataclass
class Scenario:
    """What the fake MSAL app does. ``release`` lets a test hold the device-flow poll open."""

    flow: dict[str, Any] = field(
        default_factory=lambda: {
            "user_code": USER_CODE,
            "device_code": DEVICE_CODE,
            "verification_uri": VERIFY_URI,
            "expires_in": 900,
            "interval": 5,
            "message": f"To sign in, open {VERIFY_URI} and enter the code {USER_CODE}.",
        }
    )
    result: dict[str, Any] = field(default_factory=token_response)
    hold: bool = False
    release: threading.Event = field(default_factory=threading.Event)
    polling: threading.Event = field(default_factory=threading.Event)
    silent: Callable[[], Any] = lambda: {"access_token": "silent-" + ACCESS_TOKEN}
    construct_error: Exception | None = None
    initiate_gate: threading.Event | None = None  # set: initiate_device_flow waits for it
    initiating: threading.Event = field(default_factory=threading.Event)
    apps: list["FakeApp"] = field(default_factory=list)
    scopes: list[list[str]] = field(default_factory=list)
    forced: list[bool] = field(default_factory=list)  # force_refresh of each silent call


class FakeApp:
    scenario: Scenario

    def __init__(self, client_id: str, *, authority: str, token_cache: Any, timeout: Any) -> None:
        if self.scenario.construct_error is not None:
            raise self.scenario.construct_error
        self.client_id, self.authority, self.cache, self.timeout = (
            client_id,
            authority,
            token_cache,
            timeout,
        )
        self.scenario.apps.append(self)

    def initiate_device_flow(self, scopes: list[str]) -> dict[str, Any]:
        # Real MSAL raises ValueError for reserved scopes; do the same.
        msal.ClientApplication._decorate_scope(SimpleNamespace(_exclude_scopes=frozenset()), scopes)
        self.scenario.scopes.append(scopes)
        self.scenario.initiating.set()
        if self.scenario.initiate_gate is not None:
            assert self.scenario.initiate_gate.wait(30), "test never released the device flow"
        flow = dict(self.scenario.flow)
        if "user_code" in flow:
            flow["expires_at"] = time.time() + flow["expires_in"]
        return flow

    def acquire_token_by_device_flow(
        self, flow: dict[str, Any], exit_condition: Callable[[dict[str, Any]], bool]
    ) -> dict[str, Any]:
        self.scenario.polling.set()
        if self.scenario.hold:
            while not self.scenario.release.wait(0.01):
                if exit_condition(flow):
                    return {"error": "authorization_pending"}
        result = dict(self.scenario.result)
        if "access_token" in result:
            self.cache.add(
                {
                    "client_id": self.client_id,
                    "scope": list(SCOPES),
                    "token_endpoint": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                    "response": dict(result),
                    "params": {},
                    "data": {},
                }
            )
        return result

    def get_accounts(self) -> list[dict[str, Any]]:
        return list(self.cache.search(msal.TokenCache.CredentialType.ACCOUNT))

    def acquire_token_silent_with_error(
        self, scopes: list[str], account: Any, force_refresh: bool = False
    ) -> Any:
        msal.ClientApplication._decorate_scope(SimpleNamespace(_exclude_scopes=frozenset()), scopes)
        self.scenario.scopes.append(scopes)
        self.scenario.forced.append(force_refresh)
        return self.scenario.silent()


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch) -> Scenario:
    s = Scenario()
    fake = type("BoundFakeApp", (FakeApp,), {"scenario": s})
    monkeypatch.setattr(msauth.msal, "PublicClientApplication", fake)
    return s


@pytest.fixture
def me() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        yield router.get(GRAPH_ME).mock(
            return_value=httpx.Response(
                200,
                json={
                    "displayName": "Jake Elliott",
                    "mail": None,
                    "userPrincipalName": "Jake@Outlook.com",
                },
            )
        )


def graph_settings(tmp_path: Path, **env: str) -> Settings:
    return load_settings(
        {
            "PUBLIC_BASE_URL": BASE,
            "UNSUBSCRIBE_SECRET": "x" * 32,
            "AUTH_DISABLED": "1",
            "MAIL_TRANSPORT": "graph",
            "MS_CLIENT_ID": CLIENT_ID,
            "DATA_DIR": str(tmp_path),
            **env,
        }
    )


KEY = Fernet.generate_key().decode()


def make_auth(tmp_path: Path, **env: str) -> MicrosoftAuth:
    settings = graph_settings(tmp_path, **env)
    db = Database(tmp_path / "app.db")
    db.init()
    return MicrosoftAuth(settings, db)


def wait_done(auth: MicrosoftAuth, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while auth.status().state == "signing_in":
        assert time.monotonic() < deadline, "sign-in thread didn't finish"
        time.sleep(0.01)


def sign_in(auth: MicrosoftAuth) -> None:
    auth.start_sign_in()
    wait_done(auth)
    assert auth.status().state == "signed_in", auth.status().error


def quick(fn: Callable[[], Any], limit: float = 15.0) -> Any:
    """Call ``fn`` in a thread; fail if it hasn't returned within ``limit`` seconds. Returns its
    result or re-raises its exception.

    Every caller races a fake-MSAL gate that waits 30 s (or, for ``shutdown``, a poll that never
    ends by itself), so a call that really blocks takes at least 30 s and still fails here. Under
    load single whole tests took 5 to 12 s (three suites at once) and ``shutdown`` may wait 3 s by
    design, so the old 5 s could fail a call that didn't block; 15 s is above that and still half
    the gates. Keep it well below 30 s, or a real block would no longer be detected."""
    out: list[Any] = []
    thread, errors = in_thread(lambda: out.append(fn()))
    thread.join(limit)
    assert not thread.is_alive(), f"{fn} blocked"
    if errors:
        raise errors[0]
    return out[0]


def in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, list[BaseException]]:
    """Run ``fn`` in a started thread; exceptions it raises are collected in the list."""
    errors: list[BaseException] = []

    def run() -> None:
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


# --- device flow -------------------------------------------------------------------------------


def test_start_returns_msal_values_and_uses_non_reserved_scopes(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    scenario.hold = True
    auth = make_auth(tmp_path)
    code = auth.start_sign_in()
    assert code.verification_uri == VERIFY_URI
    assert code.user_code == USER_CODE
    assert code.message == scenario.flow["message"]
    assert 890 < code.expires_at - time.time() <= 900
    assert scenario.scopes == [["Mail.Send", "User.Read"]]
    assert not {"offline_access", "openid", "profile"} & set(scenario.scopes[0])
    app = scenario.apps[0]
    assert app.client_id == CLIENT_ID
    assert app.authority == "https://login.microsoftonline.com/consumers"
    assert isinstance(app.cache, msal.SerializableTokenCache)
    assert app.timeout == msauth.HTTP_TIMEOUT_SECONDS
    status = auth.status()
    assert status.state == "signing_in" and status.code == code
    assert 890 < (status.seconds_left or 0) <= 900
    auth.cancel_sign_in()


def test_reserved_scopes_would_be_rejected_by_msal() -> None:
    fake_self = SimpleNamespace(_exclude_scopes=frozenset())
    with pytest.raises(ValueError, match="reserved"):
        msal.ClientApplication._decorate_scope(fake_self, ["Mail.Send", "offline_access"])
    decorated = msal.ClientApplication._decorate_scope(fake_self, list(SCOPES))
    assert "offline_access" in decorated  # MSAL adds it, so a refresh token is issued


def test_success_stores_sender_and_cache(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    auth = make_auth(tmp_path, TOKEN_CACHE_KEY=KEY)
    sign_in(auth)
    status = auth.status()
    assert status.sender == msauth.Sender("Jake@outlook.com", "Jake Elliott")
    assert status.notice == "Connected as Jake@outlook.com."
    assert get_sender(auth.db) == status.sender
    # Stored with the account it belongs to, which load checks against the cache.
    [account] = msauth._accounts(auth._cache)
    stored = get_sender(auth.db)
    assert stored is not None and stored.account_id == account["home_account_id"]
    assert me.calls.last.request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    path = tmp_path / CACHE_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_bytes()
    with pytest.raises(ValueError):
        json.loads(raw)  # encrypted, not plain JSON
    assert REFRESH_TOKEN.encode() not in raw
    assert REFRESH_TOKEN in Fernet(KEY).decrypt(raw).decode()
    # A new instance (restart) reads it back.
    again = MicrosoftAuth(auth.settings, auth.db)
    assert again.status().state == "signed_in"
    assert again.sender() == status.sender


def test_plaintext_cache_without_key(tmp_path: Path, scenario: Scenario, me: respx.Route) -> None:
    auth = make_auth(tmp_path)
    sign_in(auth)
    path = tmp_path / CACHE_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert REFRESH_TOKEN in json.dumps(json.loads(path.read_bytes()))
    assert auth.status().cache_plaintext


def test_cache_written_atomically(
    tmp_path: Path, scenario: Scenario, me: respx.Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, int]] = []
    real_replace = os.replace

    def replace(src: str, dst: str) -> None:
        calls.append((str(src), str(dst), stat.S_IMODE(os.stat(src).st_mode)))
        real_replace(src, dst)

    monkeypatch.setattr(msauth.os, "replace", replace)
    auth = make_auth(tmp_path, TOKEN_CACHE_KEY=KEY)
    sign_in(auth)
    assert len(calls) == 1
    src, dst, mode = calls[0]
    assert Path(src).parent == tmp_path and Path(src).name.startswith(".msal_cache.")
    assert dst == str(tmp_path / CACHE_FILE) and mode == 0o600
    assert not list(tmp_path.glob(".msal_cache.*"))
    # Not rewritten when MSAL didn't change anything.
    auth.get_access_token()
    assert len(calls) == 1


def test_only_one_sign_in_at_a_time(tmp_path: Path, scenario: Scenario) -> None:
    scenario.hold = True
    auth = make_auth(tmp_path)
    auth.start_sign_in()
    with pytest.raises(SignInError, match="already in progress"):
        auth.start_sign_in()
    assert len(scenario.apps) == 1
    auth.cancel_sign_in()


def test_cancel_discards_the_flow(tmp_path: Path, scenario: Scenario, me: respx.Route) -> None:
    scenario.hold = True
    auth = make_auth(tmp_path)
    auth.start_sign_in()
    assert scenario.polling.wait(30)
    thread = auth._slot.thread  # type: ignore[union-attr]
    assert auth.cancel_sign_in() is True
    status = auth.status()
    assert status.state == "signed_out" and status.notice == "Sign-in cancelled."
    # Even if Microsoft answers with a token after the cancel, it is not used.
    scenario.release.set()
    thread.join(30)  # type: ignore[union-attr]
    assert not thread.is_alive()  # type: ignore[union-attr]
    assert auth.status().state == "signed_out"
    assert get_sender(auth.db) is None
    assert not (tmp_path / CACHE_FILE).exists()
    assert auth.cancel_sign_in() is False


def test_cancel_stops_msal_polling(tmp_path: Path, scenario: Scenario) -> None:
    scenario.hold = True  # never released: only the exit condition can end the loop
    auth = make_auth(tmp_path)
    auth.start_sign_in()
    assert scenario.polling.wait(30)
    thread = auth._slot.thread  # type: ignore[union-attr]
    auth.cancel_sign_in()
    thread.join(30)  # type: ignore[union-attr]
    assert not thread.is_alive()  # type: ignore[union-attr]


def test_code_expiry_ends_the_sign_in(tmp_path: Path, scenario: Scenario) -> None:
    scenario.hold = True
    now = [time.time()]
    db = Database(tmp_path / "app.db")
    db.init()
    auth = MicrosoftAuth(graph_settings(tmp_path), db, clock=lambda: now[0])
    code = auth.start_sign_in()
    assert scenario.polling.wait(30)
    assert auth.status().state == "signing_in"
    now[0] = code.expires_at + 1  # the code has just expired
    wait_done(auth)  # the fake polls every 10 ms
    status = auth.status()
    assert status.state == "signed_out"
    assert status.error and "expired" in status.error


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"error": "authorization_declined"}, "declined"),
        ({"error": "expired_token"}, "expired"),
        (
            {
                "error": "invalid_client",
                "error_codes": [7000218],
                "error_description": "AADSTS7000218: x",
            },
            "public client flows",
        ),
        (
            {"error": "invalid_request", "error_description": "AADSTS700016: app not found"},
            "MS_CLIENT_ID",
        ),
        ({"error": "unauthorized_client", "error_codes": [50059]}, "MS_CLIENT_ID"),
        ({"error": "invalid_client", "error_codes": [70002]}, "public client flows"),
        ({"error": "access_denied", "error_codes": [65004]}, "declined"),
        ({"error": "invalid_grant", "error_codes": [700082]}, "Connect the account again"),
        ({"error": "weird_thing"}, "Microsoft sign-in failed. (weird_thing)"),
    ],
)
def test_error_mapping(result: dict[str, Any], expected: str) -> None:
    assert expected in describe_error(result)


def test_error_text_shows_codes_not_description() -> None:
    text = describe_error(
        {
            "error": "invalid_client",
            "error_codes": [7000218],
            "error_description": "AADSTS7000218: secret Trace ID: abc <script>",
        }
    )
    assert "AADSTS7000218" in text and "invalid_client" in text
    assert "Trace" not in text and "<script>" not in text


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"error": "authorization_declined"}, "declined"),
        ({"error": "expired_token"}, "expired"),
        ({"error": "invalid_client", "error_codes": [7000218]}, "AADSTS7000218"),
    ],
)
def test_failed_sign_in_shows_message(
    tmp_path: Path, scenario: Scenario, result: dict[str, Any], expected: str
) -> None:
    scenario.result = result
    auth = make_auth(tmp_path)
    auth.start_sign_in()
    wait_done(auth)
    status = auth.status()
    assert status.state == "signed_out"
    assert status.error and expected in status.error
    assert not (tmp_path / CACHE_FILE).exists()


def test_start_error_from_microsoft(tmp_path: Path, scenario: Scenario) -> None:
    scenario.flow = {"error": "invalid_client", "error_codes": [700016]}
    auth = make_auth(tmp_path)
    with pytest.raises(SignInError, match="MS_CLIENT_ID"):
        auth.start_sign_in()
    assert auth.status().state == "signed_out"
    assert "AADSTS700016" in (auth.status().error or "")


def test_start_when_microsoft_unreachable(tmp_path: Path, scenario: Scenario) -> None:
    scenario.construct_error = ConnectionError("no route")
    auth = make_auth(tmp_path)
    with pytest.raises(SignInError, match="Couldn't reach Microsoft"):
        auth.start_sign_in()


def test_work_account_rejected(tmp_path: Path, scenario: Scenario, me: respx.Route) -> None:
    scenario.result = token_response(tid="72f988bf-86f1-41af-91ab-2d7cd011db47")
    auth = make_auth(tmp_path)
    auth.start_sign_in()
    wait_done(auth)
    status = auth.status()
    assert status.state == "signed_out" and "work or school" in (status.error or "")
    assert not me.called
    assert get_sender(auth.db) is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, json={"error": {"code": "Forbidden"}}),
        httpx.Response(
            200, json={"displayName": "X", "mail": None, "userPrincipalName": "not an email"}
        ),
        httpx.Response(200, text="not json"),
    ],
)
def test_profile_problems(tmp_path: Path, scenario: Scenario, response: httpx.Response) -> None:
    scenario.result = token_response(username="also not an email")
    with respx.mock() as router:
        router.get(GRAPH_ME).mock(return_value=response)
        auth = make_auth(tmp_path)
        auth.start_sign_in()
        wait_done(auth)
    status = auth.status()
    assert status.state == "signed_out" and status.error
    assert get_sender(auth.db) is None
    assert not (tmp_path / CACHE_FILE).exists()


def test_reconnect_cancelled_keeps_old_sign_in(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    auth = make_auth(tmp_path)
    sign_in(auth)
    scenario.hold = True
    scenario.release.clear()
    auth.start_sign_in()
    auth.cancel_sign_in()
    assert auth.status().state == "signed_in"
    assert auth.get_access_token().startswith("silent-")


# --- tokens ------------------------------------------------------------------------------------


def test_get_access_token(tmp_path: Path, scenario: Scenario, me: respx.Route) -> None:
    auth = make_auth(tmp_path)
    with pytest.raises(NotSignedIn):
        auth.get_access_token()
    sign_in(auth)
    assert auth.get_access_token() == "silent-" + ACCESS_TOKEN
    assert scenario.scopes[-1] == ["Mail.Send", "User.Read"]


def test_get_access_token_for_an_expected_account(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    auth = make_auth(tmp_path)
    sign_in(auth)
    assert auth.get_access_token(expected_address="jake@OUTLOOK.com").startswith("silent-")
    calls = len(scenario.scopes)
    with pytest.raises(AccountChanged, match=r"changed to Jake@outlook\.com"):
        auth.get_access_token(expected_address="other@outlook.com")
    assert len(scenario.scopes) == calls  # no token was fetched for it


@pytest.mark.parametrize(
    "silent", [lambda: None, lambda: {"error": "invalid_grant", "error_codes": [70008]}]
)
def test_get_access_token_reauth(
    tmp_path: Path, scenario: Scenario, me: respx.Route, silent: Callable[[], Any]
) -> None:
    auth = make_auth(tmp_path)
    sign_in(auth)
    scenario.silent = silent
    with pytest.raises(ReauthRequired):
        auth.get_access_token()
    status = auth.status()
    assert status.state == "signed_in" and "Connect the account again" in (status.error or "")
    assert isinstance(ReauthRequired("x"), msauth.AuthError)
    assert status.reauth and auth.indicator() == "reconnect needed"
    # A successful silent renewal clears it.
    scenario.silent = lambda: {"access_token": "t"}
    assert auth.get_access_token() == "t"
    status = auth.status()
    assert not status.reauth and status.error is None and auth.indicator() == "connected"


def test_reconnect_clears_reauth(tmp_path: Path, scenario: Scenario, me: respx.Route) -> None:
    auth = make_auth(tmp_path)
    sign_in(auth)
    scenario.silent = lambda: None
    with pytest.raises(ReauthRequired):
        auth.get_access_token()
    # Starting and cancelling a reconnect doesn't hide that the sign-in is unusable.
    scenario.hold = True
    auth.start_sign_in()
    auth.cancel_sign_in()
    assert auth.indicator() == "reconnect needed"
    scenario.hold = False
    sign_in(auth)
    assert not auth.status().reauth and auth.indicator() == "connected"


def test_get_access_token_unavailable(tmp_path: Path, scenario: Scenario, me: respx.Route) -> None:
    auth = make_auth(tmp_path)
    sign_in(auth)

    def boom() -> Any:
        raise ConnectionError("down")

    scenario.silent = boom
    with pytest.raises(TokenUnavailable):
        auth.get_access_token()
    scenario.silent = lambda: {"error": "temporarily_unavailable"}
    with pytest.raises(TokenUnavailable):
        auth.get_access_token()


def test_get_access_token_is_serialised(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    auth = make_auth(tmp_path)
    sign_in(auth)
    inside = 0
    peak = 0
    guard = threading.Lock()

    def silent() -> Any:
        nonlocal inside, peak
        with guard:
            inside += 1
            peak = max(peak, inside)
        time.sleep(0.01)
        with guard:
            inside -= 1
        return {"access_token": "t"}

    scenario.silent = silent
    threads = [threading.Thread(target=auth.get_access_token) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 1


# --- cache file problems -----------------------------------------------------------------------


def test_wrong_key_means_signed_out(tmp_path: Path, scenario: Scenario, me: respx.Route) -> None:
    sign_in(make_auth(tmp_path, TOKEN_CACHE_KEY=KEY))
    other = make_auth(tmp_path, TOKEN_CACHE_KEY=Fernet.generate_key().decode())
    status = other.status()
    assert status.state == "signed_out"
    assert status.error and "TOKEN_CACHE_KEY has changed" in status.error
    with pytest.raises(NotSignedIn):
        other.get_access_token()
    assert (tmp_path / CACHE_FILE).exists()  # left alone; the old key would still work


def test_key_removed_means_signed_out(tmp_path: Path, scenario: Scenario, me: respx.Route) -> None:
    sign_in(make_auth(tmp_path, TOKEN_CACHE_KEY=KEY))
    status = make_auth(tmp_path).status()
    assert status.state == "signed_out"
    assert status.error and "TOKEN_CACHE_KEY isn't set" in status.error


def test_plain_cache_is_encrypted_once_key_is_set(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    sign_in(make_auth(tmp_path))
    auth = make_auth(tmp_path, TOKEN_CACHE_KEY=KEY)
    assert auth.status().state == "signed_in"
    raw = (tmp_path / CACHE_FILE).read_bytes()
    assert REFRESH_TOKEN in Fernet(KEY).decrypt(raw).decode()


def test_damaged_cache(tmp_path: Path, scenario: Scenario) -> None:
    (tmp_path / CACHE_FILE).write_text("[1, 2")
    status = make_auth(tmp_path).status()
    assert status.state == "signed_out" and status.error


# --- sign-out ----------------------------------------------------------------------------------


def test_sign_out_clears_file_and_sender(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    auth = make_auth(tmp_path, TOKEN_CACHE_KEY=KEY)
    sign_in(auth)
    auth.sign_out()
    assert not (tmp_path / CACHE_FILE).exists()
    assert get_sender(auth.db) is None
    assert auth.status().state == "signed_out"
    with pytest.raises(NotSignedIn):
        auth.get_access_token()
    assert make_auth(tmp_path, TOKEN_CACHE_KEY=KEY).status().state == "signed_out"


# --- the pages never wait for Microsoft --------------------------------------------------------


def test_state_readable_while_a_sign_in_starts(tmp_path: Path, scenario: Scenario) -> None:
    scenario.hold = True
    scenario.initiate_gate = threading.Event()
    auth = make_auth(tmp_path)
    thread, errors = in_thread(auth.start_sign_in)
    assert scenario.initiating.wait(30)  # waiting for Microsoft's device code
    assert quick(auth.status).state == "signed_out"
    assert quick(auth.indicator) == "not connected"
    assert quick(auth.sender) is None
    with pytest.raises(SignInError, match="already being started"):
        quick(auth.start_sign_in)
    assert "already being started" in (auth.status().notice or "")  # shown on the page
    assert len(scenario.apps) == 1
    scenario.initiate_gate.set()
    thread.join(30)
    assert not thread.is_alive()
    assert not errors
    assert auth.status().state == "signing_in"
    with pytest.raises(SignInError, match="already in progress"):
        auth.start_sign_in()
    auth.cancel_sign_in()


def test_cancel_while_a_sign_in_starts(tmp_path: Path, scenario: Scenario) -> None:
    scenario.initiate_gate = threading.Event()
    auth = make_auth(tmp_path)
    thread, errors = in_thread(auth.start_sign_in)
    assert scenario.initiating.wait(30)
    assert quick(auth.cancel_sign_in) is True
    scenario.initiate_gate.set()
    thread.join(30)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], SignInError)
    assert "cancelled" in str(errors[0])
    status = auth.status()
    assert status.state == "signed_out" and status.notice == "Sign-in cancelled."
    assert status.error is None
    assert not scenario.polling.is_set()  # the device flow was never polled
    auth.start_sign_in()  # the slot is free again
    wait_done(auth)


def test_failed_start_frees_the_slot(tmp_path: Path, scenario: Scenario) -> None:
    scenario.construct_error = ConnectionError("no route")
    auth = make_auth(tmp_path)
    with pytest.raises(SignInError, match="Couldn't reach Microsoft"):
        auth.start_sign_in()
    with pytest.raises(SignInError, match="Couldn't reach Microsoft"):
        auth.start_sign_in()  # not "already being started"


def test_state_readable_during_token_renewal(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    auth = make_auth(tmp_path)
    sign_in(auth)
    entered, gate = threading.Event(), threading.Event()

    def silent() -> Any:
        entered.set()
        assert gate.wait(30)
        return {"access_token": "t"}

    scenario.silent = silent
    thread, errors = in_thread(auth.get_access_token)
    assert entered.wait(30)  # renewing, holding the MSAL lock
    assert quick(auth.status).state == "signed_in"
    assert quick(auth.indicator) == "connected"
    assert quick(auth.sender) == msauth.Sender("Jake@outlook.com", "Jake Elliott")
    # A reconnect doesn't wait for the renewal either (it uses its own cache).
    scenario.hold = True
    quick(auth.start_sign_in)
    assert quick(auth.cancel_sign_in) is True
    gate.set()
    thread.join(30)
    assert not thread.is_alive()
    assert not errors


def test_app_shutdown_does_not_block_the_event_loop(
    tmp_path: Path, scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(graph_settings(tmp_path))
    release = threading.Event()
    released: list[bool] = []

    def slow_shutdown() -> None:  # like joining a sign-in thread that takes a while
        released.append(release.wait(30))

    monkeypatch.setattr(app.state.msauth, "shutdown", slow_shutdown)

    async def run() -> None:
        async def release_soon() -> None:  # can only run if the loop isn't blocked
            await asyncio.sleep(0.01)
            release.set()

        async with app.router.lifespan_context(app):
            task = asyncio.create_task(release_soon())
        await task

    asyncio.run(run())
    assert released == [True]


def test_real_shutdown_ends_a_sign_in(tmp_path: Path, scenario: Scenario) -> None:
    scenario.hold = True  # never released: only the exit condition ends the poll
    auth = make_auth(tmp_path)
    auth.start_sign_in()
    assert scenario.polling.wait(30)
    thread = auth._slot.thread  # type: ignore[union-attr]
    quick(auth.shutdown)
    # shutdown() waits up to 3 s of wall clock; a loaded machine may need longer. The poll
    # never ends by itself, so the thread ending at all proves shutdown stopped it.
    thread.join(30)  # type: ignore[union-attr]
    assert not thread.is_alive()  # type: ignore[union-attr]
    assert auth.status().state == "signed_out"


# --- logging -----------------------------------------------------------------------------------


def test_secrets_never_logged(
    tmp_path: Path, scenario: Scenario, me: respx.Route, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    auth = make_auth(tmp_path, TOKEN_CACHE_KEY=KEY)
    sign_in(auth)
    auth.get_access_token()
    scenario.silent = lambda: {"error": "invalid_grant", "error_description": REFRESH_TOKEN}
    with pytest.raises(ReauthRequired):
        auth.get_access_token()
    auth.sign_out()
    scenario.result = {"error": "authorization_declined", "error_description": DEVICE_CODE}
    auth.start_sign_in()
    wait_done(auth)
    assert caplog.records  # something was logged
    for secret in (USER_CODE, DEVICE_CODE, ACCESS_TOKEN, REFRESH_TOKEN, KEY):
        assert secret not in caplog.text
    assert USER_CODE not in repr(auth.status())


# --- mock mode ---------------------------------------------------------------------------------


def test_mock_mode_never_calls_microsoft(
    tmp_path: Path, scenario: Scenario, valid_env: dict[str, str]
) -> None:
    scenario.construct_error = AssertionError("MSAL must not be used in mock mode")
    settings = load_settings({**valid_env, "DATA_DIR": str(tmp_path)})
    auth = MicrosoftAuth(settings, Database(tmp_path / "app.db"))
    assert auth.status().state == "mock"
    assert auth.indicator() == "mock"
    with pytest.raises(SignInError, match="mock"):
        auth.start_sign_in()
    with pytest.raises(NotSignedIn):
        auth.get_access_token()
    auth.sign_out()
    assert not scenario.apps


# --- Account page ------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, scenario: Scenario) -> Iterator[TestClient]:
    with TestClient(create_app(graph_settings(tmp_path)), base_url=BASE) as c:
        yield c


def csrf(client: TestClient) -> str:
    match = TOKEN_RE.search(client.get("/account").text)
    assert match
    return match.group(1)


def post(client: TestClient, url: str) -> httpx.Response:
    return client.post(
        url, data={"csrf_token": csrf(client)}, headers={"Origin": BASE}, follow_redirects=False
    )


def wait_page(client: TestClient) -> None:
    wait_done(client.app.state.msauth)  # type: ignore[attr-defined]


def test_page_signed_out(client: TestClient) -> None:
    r = client.get("/account")
    assert r.status_code == 200
    assert 'data-state="signed_out"' in r.text
    assert "Connect Outlook account" in r.text
    assert 'action="/account/connect"' in r.text
    assert "account.live.com/consent/Manage" in r.text
    assert "90 days" in r.text
    assert 'id="cache-plaintext"' in r.text  # no TOKEN_CACHE_KEY
    assert 'id="ms-indicator">not connected<' in r.text
    assert "<script>" not in r.text and "style=" not in r.text


def test_page_signing_in_and_connected(
    client: TestClient, scenario: Scenario, me: respx.Route
) -> None:
    scenario.hold = True
    r = post(client, "/account/connect")
    assert r.status_code == 303 and r.headers["location"] == "/account"
    page = client.get("/account").text
    assert 'data-state="signing_in"' in page
    assert f'<code class="user-code" id="user-code">{USER_CODE}</code>' in page
    assert (
        f'<a href="{VERIFY_URI.replace("&", "&amp;")}" target="_blank" rel="noopener noreferrer"'
        in page
    )
    assert re.search(r'id="time-left">1[45]:\d\d<', page)
    assert 'action="/account/cancel"' in page
    assert 'href="/account">Refresh</a>' in page
    data = client.get("/account/status").json()
    assert data["state"] == "signing_in" and 0 < data["seconds_left"] <= 900
    assert USER_CODE not in json.dumps(data)

    scenario.release.set()
    wait_page(client)
    page = client.get("/account").text
    assert 'data-state="signed_in"' in page
    assert "Sending as <strong>Jake@outlook.com</strong> (Jake Elliott)" in page
    assert "<details>" in page and 'action="/account/sign-out"' in page
    assert 'id="ms-indicator">connected<' in page
    assert client.get("/account/status").json() == {"state": "signed_in", "seconds_left": None}
    assert "Connected as Jake@outlook.com." in client.get("/").text

    r = post(client, "/account/sign-out")
    assert r.status_code == 303
    page = client.get("/account").text
    assert 'data-state="signed_out"' in page and "Signed out." in page


def test_page_cancel(client: TestClient, scenario: Scenario) -> None:
    scenario.hold = True
    post(client, "/account/connect")
    assert post(client, "/account/cancel").status_code == 303
    page = client.get("/account").text
    assert 'data-state="signed_out"' in page and "Sign-in cancelled." in page


def test_page_error_links_troubleshooting(client: TestClient, scenario: Scenario) -> None:
    scenario.result = {"error": "invalid_client", "error_codes": [7000218]}
    post(client, "/account/connect")
    wait_page(client)
    page = client.get("/account").text
    assert "Allow public client flows" in page and "AADSTS7000218" in page
    assert f'href="{msauth.TROUBLESHOOTING_URL}" target="_blank" rel="noopener noreferrer"' in page


def test_page_reauth_offers_connect_again(
    client: TestClient, scenario: Scenario, me: respx.Route
) -> None:
    post(client, "/account/connect")
    wait_page(client)
    scenario.silent = lambda: None
    with pytest.raises(ReauthRequired):
        client.app.state.msauth.get_access_token()  # type: ignore[attr-defined]
    page = client.get("/account").text
    assert 'data-state="signed_in"' in page and "Connect again" in page
    assert 'id="ms-indicator">reconnect needed<' in page


@pytest.mark.parametrize("url", ["/account/connect", "/account/cancel", "/account/sign-out"])
def test_posts_need_csrf(client: TestClient, scenario: Scenario, url: str) -> None:
    r = client.post(url, data={}, headers={"Origin": BASE}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post(
        url, data={"csrf_token": "forged"}, headers={"Origin": BASE}, follow_redirects=False
    )
    assert r.status_code == 403
    r = client.post(
        url,
        data={"csrf_token": csrf(client)},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert not scenario.apps  # nothing reached Microsoft


def test_page_mock_mode(tmp_path: Path, scenario: Scenario, valid_env: dict[str, str]) -> None:
    scenario.construct_error = AssertionError("MSAL must not be used in mock mode")
    settings = load_settings({**valid_env, "DATA_DIR": str(tmp_path)})
    with TestClient(create_app(settings), base_url=BASE) as c:
        page = c.get("/account").text
        assert 'id="account-mock"' in page and "no Microsoft sign-in is needed" in page
        assert 'action="/account/connect"' not in page
        assert 'id="cache-plaintext"' not in page
        assert 'id="ms-indicator">mock<' in page
        assert "Not needed in mock mode." in c.get("/").text
        token = TOKEN_RE.search(c.get("/send/new").text)
        assert token
        r = c.post(
            "/account/connect",
            data={"csrf_token": token.group(1)},
            headers={"Origin": BASE},
            follow_redirects=False,
        )
        assert r.status_code == 303
    assert not scenario.apps


def test_page_not_configured(tmp_path: Path, scenario: Scenario) -> None:
    # load_settings requires MS_CLIENT_ID for graph; a hand-built Settings can still lack it.
    settings = Settings(
        public_base_url=BASE,
        unsubscribe_secret="x" * 32,
        auth_disabled=True,
        mail_transport="graph",
        data_dir=str(tmp_path),
    )
    with TestClient(create_app(settings), base_url=BASE) as c:
        page = c.get("/account").text
    assert 'id="account-not-configured"' in page
    assert 'action="/account/connect"' not in page
    assert not scenario.apps


# --- saving a sign-in fails ----------------------------------------------------------------------

NEW_REFRESH = "M.C5_BAY.NEW-REFRESH-TOKEN-secret"
# The default fake account: the first sign-in of every test (the "old" one when a test reconnects).
JAKE_SENDER = msauth.Sender("Jake@outlook.com", "Jake Elliott")
NEW_SENDER = msauth.Sender("new@outlook.com", "New Person")


def failing_store(monkeypatch: pytest.MonkeyPatch) -> None:
    def store(db: Database, sender: msauth.Sender | None) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(msauth, "_store_sender", store)


def next_account_is_new(scenario: Scenario, me: respx.Route) -> None:
    """The next sign-in is a different account (``NEW_SENDER``, ``NEW_REFRESH``)."""
    scenario.result = {**token_response(username="new@outlook.com"), "refresh_token": NEW_REFRESH}
    profile = {"displayName": "New Person", "mail": "new@outlook.com"}
    me.mock(return_value=httpx.Response(200, json=profile))


def start_held(auth: MicrosoftAuth, scenario: Scenario) -> threading.Thread:
    """Start a sign-in whose poll waits for ``scenario.release``; returns its thread."""
    scenario.hold = True
    scenario.release.clear()
    auth.start_sign_in()
    slot = auth._slot
    assert isinstance(slot, msauth._Flow) and slot.thread is not None
    return slot.thread


def finish(thread: threading.Thread) -> None:
    thread.join(30)
    assert not thread.is_alive()


def gate_store(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """``_store_sender`` sets the first event, then waits for the second before writing."""
    entered, release = threading.Event(), threading.Event()
    real_store = msauth._store_sender

    def store(db: Database, sender: msauth.Sender | None) -> None:
        entered.set()
        assert release.wait(30)
        real_store(db, sender)

    monkeypatch.setattr(msauth, "_store_sender", store)
    return entered, release


def catch_thread_errors(monkeypatch: pytest.MonkeyPatch) -> list[BaseException]:
    """Collect exceptions that escape any thread (e.g. the sign-in thread) from now on. Call it
    in the test body: pytest installs its own hook when the test starts."""
    errors: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_value))
    return errors


class SignalledLock:
    """Stands in for ``_msal_lock``: ``waiting`` is set when a thread starts to take it."""

    def __init__(self, lock: Any) -> None:
        self.lock = lock
        self.waiting = threading.Event()

    def __enter__(self) -> Any:
        self.waiting.set()
        return self.lock.__enter__()

    def __exit__(self, *exc: Any) -> Any:
        return self.lock.__exit__(*exc)


def watch_join(thread: threading.Thread) -> tuple[threading.Event, list[float | None]]:
    """Wrap ``thread.join``: the event is set when someone starts to join it, and the list gets
    each call's timeout."""
    joining, timeouts = threading.Event(), []
    real_join = thread.join

    def join(timeout: float | None = None) -> None:
        timeouts.append(timeout)
        joining.set()
        real_join(timeout)

    thread.join = join  # type: ignore[method-assign]
    return joining, timeouts


def restart(auth: MicrosoftAuth) -> MicrosoftAuth:
    """A new instance on the same DATA_DIR and database, as after an app restart."""
    return MicrosoftAuth(auth.settings, auth.db)


def test_commit_file_failure_keeps_old_sign_in(
    tmp_path: Path,
    scenario: Scenario,
    me: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_errors = catch_thread_errors(monkeypatch)
    auth = make_auth(tmp_path)
    sign_in(auth)
    old_cache, old_client = auth._cache, auth._client
    before = (tmp_path / CACHE_FILE).read_bytes()
    next_account_is_new(scenario, me)
    stored: list[Any] = []
    real_store = msauth._store_sender
    with monkeypatch.context() as m:
        m.setattr(msauth, "_store_sender", lambda db, s: (stored.append(s), real_store(db, s)))

        def write(self: Any, state: str) -> None:
            raise OSError(13, "Permission denied")

        m.setattr(msauth._CacheFile, "write", write)
        thread = start_held(auth, scenario)
        scenario.release.set()
        finish(thread)
    status = auth.status()
    assert status.state == "signed_in" and status.sender == JAKE_SENDER
    assert "couldn't be saved in DATA_DIR" in (status.error or "")
    assert auth._cache is old_cache and auth._client is old_client
    assert stored == []  # the database isn't touched
    assert (tmp_path / CACHE_FILE).read_bytes() == before
    assert get_sender(auth.db) == JAKE_SENDER
    assert auth.get_access_token().startswith("silent-")
    again = restart(auth)
    assert again.status().state == "signed_in" and again.sender() == JAKE_SENDER
    assert again.get_access_token().startswith("silent-")
    assert not thread_errors


def test_commit_file_failure_when_signed_out(
    tmp_path: Path, scenario: Scenario, me: respx.Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = make_auth(tmp_path)

    def write(self: Any, state: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(msauth._CacheFile, "write", write)
    auth.start_sign_in()
    wait_done(auth)
    status = auth.status()
    assert status.state == "signed_out" and "couldn't be saved in DATA_DIR" in (status.error or "")
    assert not (tmp_path / CACHE_FILE).exists()
    assert get_sender(auth.db) is None
    with pytest.raises(NotSignedIn):
        auth.get_access_token()
    assert restart(auth).status().state == "signed_out"


def test_commit_db_failure_keeps_old_sign_in(
    tmp_path: Path, scenario: Scenario, me: respx.Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = make_auth(tmp_path)
    sign_in(auth)
    old_cache, old_client = auth._cache, auth._client
    next_account_is_new(scenario, me)
    with monkeypatch.context() as m:
        failing_store(m)
        auth.start_sign_in()
        wait_done(auth)
    status = auth.status()
    assert status.state == "signed_in" and status.sender == JAKE_SENDER
    assert "couldn't be saved in the database" in (status.error or "")
    assert auth._cache is old_cache and auth._client is old_client
    raw = (tmp_path / CACHE_FILE).read_text()
    assert REFRESH_TOKEN in raw and NEW_REFRESH not in raw  # the old sign-in is back on disk
    assert auth.get_access_token().startswith("silent-")
    assert get_sender(auth.db) == JAKE_SENDER
    # After a restart: the previous account, and the file and database agree.
    again = restart(auth)
    assert again.status().state == "signed_in" and again.sender() == JAKE_SENDER
    saved = again._cache.serialize()
    assert REFRESH_TOKEN in saved and NEW_REFRESH not in saved
    assert again.get_access_token().startswith("silent-")


def test_commit_db_failure_when_signed_out_leaves_no_file(
    tmp_path: Path, scenario: Scenario, me: respx.Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = make_auth(tmp_path)
    failing_store(monkeypatch)
    auth.start_sign_in()
    wait_done(auth)
    status = auth.status()
    assert status.state == "signed_out" and "couldn't be saved in the database" in (
        status.error or ""
    )
    assert not (tmp_path / CACHE_FILE).exists()
    assert get_sender(auth.db) is None
    assert restart(auth).status().state == "signed_out"


def test_commit_restore_failure_deletes_the_file(
    tmp_path: Path, scenario: Scenario, me: respx.Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new sign-in is on disk, the database write fails and so does putting the old file
    back: the file is deleted, so file and database can't disagree after a restart."""
    auth = make_auth(tmp_path)
    sign_in(auth)
    next_account_is_new(scenario, me)
    real_write = msauth._CacheFile.write
    writes: list[int] = []

    def write(self: Any, state: str) -> None:
        writes.append(1)
        if len(writes) > 1:  # the restore
            raise OSError(28, "No space left on device")
        real_write(self, state)

    with monkeypatch.context() as m:
        failing_store(m)
        m.setattr(msauth._CacheFile, "write", write)
        auth.start_sign_in()
        wait_done(auth)
    assert len(writes) == 2
    assert not (tmp_path / CACHE_FILE).exists()
    assert "couldn't be saved" in (auth.status().error or "")
    assert get_sender(auth.db) == JAKE_SENDER
    # Not signed in as the new account under the old address: signed out.
    again = restart(auth)
    assert again.status().state == "signed_out" and again.sender() is None
    with pytest.raises(NotSignedIn):
        again.get_access_token()


def test_commit_restore_and_delete_failure_is_contained(
    tmp_path: Path,
    scenario: Scenario,
    me: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even the delete fails: nothing escapes the sign-in thread and the running app keeps the
    previous sign-in. (The file is then left as the new account: nothing more can be done.)"""
    thread_errors = catch_thread_errors(monkeypatch)
    auth = make_auth(tmp_path)
    sign_in(auth)
    old_cache = auth._cache
    next_account_is_new(scenario, me)
    real_write = msauth._CacheFile.write
    writes: list[int] = []
    deletes: list[int] = []

    def write(self: Any, state: str) -> None:
        writes.append(1)
        if len(writes) > 1:
            raise OSError(5, "Input/output error")
        real_write(self, state)

    def delete(self: Any) -> None:
        deletes.append(1)
        raise OSError(5, "Input/output error")

    with monkeypatch.context() as m:
        failing_store(m)
        m.setattr(msauth._CacheFile, "write", write)
        m.setattr(msauth._CacheFile, "delete", delete)
        thread = start_held(auth, scenario)
        scenario.release.set()
        finish(thread)
    assert len(writes) == 2 and len(deletes) == 1
    assert not thread_errors
    status = auth.status()
    assert status.state == "signed_in" and status.sender == JAKE_SENDER
    assert "couldn't be saved in the database" in (status.error or "")
    assert auth._cache is old_cache
    assert auth._slot is None
    assert auth.get_access_token().startswith("silent-")


# --- cancel, sign-out and shutdown while a sign-in is being saved --------------------------------


def test_cancel_during_commit_is_refused(
    tmp_path: Path,
    scenario: Scenario,
    me: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first sign-in (no previous one), as ``JAKE_SENDER``: once it is being saved a cancel
    is refused and the save completes."""
    thread_errors = catch_thread_errors(monkeypatch)
    auth = make_auth(tmp_path)
    entered, release = gate_store(monkeypatch)
    thread = start_held(auth, scenario)
    scenario.release.set()
    assert entered.wait(30)  # succeeded, now saving
    assert quick(auth.cancel_sign_in) is False  # too late to cancel
    assert quick(auth.status).state == "signing_in"
    release.set()
    finish(thread)
    status = auth.status()
    assert status.state == "signed_in" and status.notice == "Connected as Jake@outlook.com."
    assert get_sender(auth.db) == JAKE_SENDER
    assert restart(auth).sender() == JAKE_SENDER
    assert not thread_errors


def test_cancel_while_the_save_waits_for_the_msal_lock(
    tmp_path: Path,
    scenario: Scenario,
    me: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Microsoft accepted and the profile was read, but a token renewal holds the MSAL lock, so
    the sign-in thread is waiting for it and saving hasn't started: a cancel still wins and the
    previous sign-in stays, in memory and on disk."""
    thread_errors = catch_thread_errors(monkeypatch)
    auth = make_auth(tmp_path)
    sign_in(auth)
    before = (tmp_path / CACHE_FILE).read_bytes()
    next_account_is_new(scenario, me)
    entered, gate = threading.Event(), threading.Event()

    def silent() -> Any:
        entered.set()
        assert gate.wait(30)
        return {"access_token": "t"}

    scenario.silent = silent
    renewal, errors = in_thread(auth.get_access_token)
    assert entered.wait(30)  # holding the MSAL lock
    # Only the sign-in thread takes the lock from here on, so `waiting` means it has got past
    # Microsoft and the profile and is about to save.
    lock = SignalledLock(auth._msal_lock)
    auth._msal_lock = lock  # type: ignore[assignment]
    thread = start_held(auth, scenario)
    scenario.release.set()
    assert lock.waiting.wait(30)
    assert quick(auth.cancel_sign_in) is True
    gate.set()
    finish(renewal)
    finish(thread)
    assert not errors and not thread_errors
    status = auth.status()
    assert status.state == "signed_in" and status.sender == JAKE_SENDER
    assert status.notice == "Sign-in cancelled."
    assert (tmp_path / CACHE_FILE).read_bytes() == before
    assert get_sender(auth.db) == JAKE_SENDER
    assert restart(auth).sender() == JAKE_SENDER


def test_sign_out_during_commit_signs_out_after_it(
    tmp_path: Path,
    scenario: Scenario,
    me: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sign-out waits for the save (it needs the MSAL lock), then removes the new account."""
    thread_errors = catch_thread_errors(monkeypatch)
    auth = make_auth(tmp_path)
    sign_in(auth)
    next_account_is_new(scenario, me)
    entered, release = gate_store(monkeypatch)
    thread = start_held(auth, scenario)
    scenario.release.set()
    assert entered.wait(30)  # the new cache is on disk, the sender not yet
    lock = SignalledLock(auth._msal_lock)
    auth._msal_lock = lock  # type: ignore[assignment]
    signing_out, errors = in_thread(auth.sign_out)
    assert lock.waiting.wait(30)  # sign_out is waiting for the save
    assert quick(auth.status).state == "signing_in"  # the pages don't wait meanwhile
    release.set()
    finish(signing_out)
    finish(thread)
    assert not errors and not thread_errors
    status = auth.status()
    assert status.state == "signed_out" and status.notice and "Signed out" in status.notice
    assert auth._slot is None
    assert not (tmp_path / CACHE_FILE).exists()
    assert get_sender(auth.db) is None
    with pytest.raises(NotSignedIn):
        auth.get_access_token()
    assert restart(auth).status().state == "signed_out"


def test_shutdown_during_commit_waits_for_it(
    tmp_path: Path,
    scenario: Scenario,
    me: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sign-in being saved can't be stopped, so shutdown waits (up to 12 s) for the sign-in
    thread to finish the save (fix B4)."""
    thread_errors = catch_thread_errors(monkeypatch)
    auth = make_auth(tmp_path)
    sign_in(auth)
    next_account_is_new(scenario, me)
    entered, release = gate_store(monkeypatch)
    thread = start_held(auth, scenario)
    scenario.release.set()
    assert entered.wait(30)  # the new cache is on disk, the sender not yet
    joining, timeouts = watch_join(thread)
    stopping, errors = in_thread(auth.shutdown)
    assert joining.wait(30)  # shutdown is waiting for the sign-in thread...
    assert timeouts[0] == msauth.SHUTDOWN_SAVE_WAIT_SECONDS == 12
    assert stopping.is_alive()  # ...which can't finish until released, so it hasn't returned
    release.set()
    finish(stopping)
    finish(thread)
    assert not errors and not thread_errors
    # Saved in full: the new account in memory, in the file and in the database.
    assert auth.status().state == "signed_in" and auth.sender() == NEW_SENDER
    assert get_sender(auth.db) == NEW_SENDER
    assert NEW_REFRESH in (tmp_path / CACHE_FILE).read_text()
    again = restart(auth)
    assert again.status().state == "signed_in" and again.sender() == NEW_SENDER
    assert NEW_REFRESH in again._cache.serialize()


def test_shutdown_gives_up_on_a_hung_save(
    tmp_path: Path,
    scenario: Scenario,
    me: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The save hangs (here: the database write): shutdown returns after its wait with a
    warning. If the app then exits, the next start finds the new account in the file and the
    old address in the database and is signed out, never sending as the wrong account."""
    thread_errors = catch_thread_errors(monkeypatch)
    monkeypatch.setattr(msauth, "SHUTDOWN_SAVE_WAIT_SECONDS", 0.05)
    auth = make_auth(tmp_path)
    sign_in(auth)
    next_account_is_new(scenario, me)
    entered, release = gate_store(monkeypatch)
    thread = start_held(auth, scenario)
    scenario.release.set()
    assert entered.wait(30)  # the new cache is on disk, the sender not yet
    with caplog.at_level(logging.WARNING, logger="outlook_mailer.msauth"):
        quick(auth.shutdown)
    assert thread.is_alive()  # still saving
    assert "still running at shutdown" in caplog.text
    # The app exits here: the file holds the new account, the database the old address.
    assert NEW_REFRESH in (tmp_path / CACHE_FILE).read_text()
    assert get_sender(auth.db) == JAKE_SENDER
    calls = len(scenario.forced)
    again = restart(auth)
    status = again.status()
    assert status.state == "signed_out" and status.sender is None
    assert "isn't for the sending address" in (status.error or "")
    assert again.sender() is None
    with pytest.raises(NotSignedIn):
        again.get_access_token(expected_address=JAKE_SENDER.address)
    assert len(scenario.forced) == calls  # no token was even asked for
    release.set()
    finish(thread)
    assert not thread_errors


# --- the cache file and the stored sender must agree --------------------------------------------


def file_and_db_disagree(tmp_path: Path, scenario: Scenario, me: respx.Route) -> MicrosoftAuth:
    """Signed in as Jake, then as the new account; then the database is put back to Jake, as
    when the app stops between a sign-in's file write and its database write."""
    auth = make_auth(tmp_path)
    sign_in(auth)
    jake = get_sender(auth.db)
    assert jake is not None and jake.account_id
    next_account_is_new(scenario, me)
    sign_in(auth)
    msauth._store_sender(auth.db, jake)
    return auth


def assert_not_used(auth: MicrosoftAuth, scenario: Scenario, message: str) -> None:
    calls = len(scenario.forced)
    status = auth.status()
    assert status.state == "signed_out" and status.sender is None
    assert message in (status.error or "")
    assert auth.sender() is None and auth.indicator() == "not connected"
    with pytest.raises(NotSignedIn):
        auth.get_access_token()
    with pytest.raises(NotSignedIn):
        auth.get_access_token(expected_address=JAKE_SENDER.address)
    assert len(scenario.forced) == calls  # Microsoft was never asked for a token


def test_restart_with_another_accounts_cache_is_signed_out(
    tmp_path: Path, scenario: Scenario, me: respx.Route, caplog: pytest.LogCaptureFixture
) -> None:
    auth = file_and_db_disagree(tmp_path, scenario, me)
    with caplog.at_level(logging.WARNING, logger="outlook_mailer.msauth"):
        again = restart(auth)
        assert_not_used(again, scenario, "isn't for the sending address the app has stored")
    assert "doesn't match the stored sender" in caplog.text
    assert NEW_REFRESH in (tmp_path / CACHE_FILE).read_text()  # left alone, not used
    # Connecting again fixes it, and stays fixed after a restart.
    sign_in(again)
    assert again.sender() == NEW_SENDER and again.status().error is None
    assert again.get_access_token().startswith("silent-")
    later = restart(again)
    assert later.sender() == NEW_SENDER and later.status().error is None
    assert later.get_access_token().startswith("silent-")


def test_mismatch_shown_on_the_pages(tmp_path: Path, scenario: Scenario, me: respx.Route) -> None:
    file_and_db_disagree(tmp_path, scenario, me)
    with TestClient(create_app(graph_settings(tmp_path)), base_url=BASE) as c:
        page = c.get("/account").text
        assert 'data-state="signed_out"' in page and 'id="account-error"' in page
        assert "for the sending address the app has stored (Jake@outlook.com)" in page
        assert "Connect Outlook account" in page
        home = c.get("/").text
        assert 'id="home-ms-problem"' in home and "Connect the account again." in home
        assert "Connected as" not in home


def test_cache_without_a_stored_sender_is_not_used(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    """The first sign-in's file was written but the app stopped before the database write."""
    auth = make_auth(tmp_path)
    sign_in(auth)
    msauth._store_sender(auth.db, None)
    assert_not_used(restart(auth), scenario, "without its sending address")
    assert (tmp_path / CACHE_FILE).exists()


def test_stored_sender_without_a_cache_is_signed_out(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    auth = make_auth(tmp_path)
    sign_in(auth)
    (tmp_path / CACHE_FILE).unlink()
    assert_not_used(restart(auth), scenario, "for Jake@outlook.com is missing")


def test_cache_with_two_accounts_is_not_used(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    auth = make_auth(tmp_path)
    sign_in(auth)
    with auth._msal_lock:
        auth._cache.add(
            {
                "client_id": CLIENT_ID,
                "scope": list(SCOPES),
                "token_endpoint": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                "response": token_response(username="new@outlook.com"),
                "params": {},
                "data": {},
            }
        )
        auth._save(strict=True)
    assert len(msauth._accounts(restart(auth)._cache)) == 0  # not loaded
    assert_not_used(restart(auth), scenario, "more than one account")


def test_sender_saved_before_account_ids_matches_by_username(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    """A sender stored without the account ID (before this check existed): the account's
    sign-in name is compared with the address instead, ignoring case."""
    auth = make_auth(tmp_path)
    sign_in(auth)  # the account's username is jake@outlook.com
    msauth._store_sender(auth.db, msauth.Sender("Jake@Outlook.com", "Jake Elliott"))
    again = restart(auth)
    assert again.status().state == "signed_in" and again.status().error is None
    assert again.get_access_token().startswith("silent-")
    msauth._store_sender(auth.db, msauth.Sender("someone@outlook.com", "Someone"))
    assert_not_used(restart(auth), scenario, "(someone@outlook.com)")


def test_get_access_token_refuses_another_accounts_token(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    """Defence in depth: should the sender in memory ever not belong to the cached account,
    no token is handed out, even when the caller's expected address matches the sender."""
    auth = make_auth(tmp_path)
    sign_in(auth)
    other = msauth.Sender("other@outlook.com", "Other", account_id="someone-else")
    with auth._state_lock:
        auth._sender = other
    calls = len(scenario.forced)
    with pytest.raises(NotSignedIn):
        auth.get_access_token(expected_address=other.address)
    assert len(scenario.forced) == calls
    status = auth.status()
    assert status.state == "signed_out" and "isn't for the sending address" in (status.error or "")


def test_refused_start_shows_a_notice(
    client: TestClient, scenario: Scenario, me: respx.Route
) -> None:
    scenario.hold = True
    post(client, "/account/connect")
    post(client, "/account/connect")  # refused: one sign-in at a time
    page = client.get("/account").text
    assert 'id="account-notice"' in page and "already in progress" in page
    post(client, "/account/cancel")


def test_note_token_rejected(tmp_path: Path, scenario: Scenario, me: respx.Route) -> None:
    auth = make_auth(tmp_path)
    auth.note_token_rejected()
    assert auth.indicator() == "not connected"  # nothing to reconnect
    sign_in(auth)
    assert auth.get_access_token()
    assert scenario.forced[-1] is False  # normally the cached token is fine
    auth.note_token_rejected()
    assert auth.indicator() == "reconnect needed"
    assert auth.get_access_token()  # a forced refresh that succeeds clears it
    assert scenario.forced[-1] is True
    assert auth.indicator() == "connected"
    assert auth.get_access_token() and scenario.forced[-1] is False  # one-shot


def test_token_rejected_needs_a_real_refresh(
    tmp_path: Path, scenario: Scenario, me: respx.Route
) -> None:
    """After a 401 the cached token can't clear "reconnect needed": the next call forces a
    refresh, and only its success, an accepted send or a sign-in clears it."""
    auth = make_auth(tmp_path)
    sign_in(auth)
    auth.note_token_rejected()

    def refresh_refused() -> Any:
        # The cache would still hand out the old token; Microsoft refuses the refresh.
        if scenario.forced[-1]:
            return {"error": "invalid_grant", "error_codes": [70008]}
        return {"access_token": "cached"}

    scenario.silent = refresh_refused
    with pytest.raises(ReauthRequired):
        auth.get_access_token()
    assert scenario.forced[-1] is True and auth.indicator() == "reconnect needed"
    with pytest.raises(ReauthRequired):  # still forced, still refused
        auth.get_access_token()
    assert scenario.forced[-1] is True and auth.indicator() == "reconnect needed"
    # Microsoft unreachable: still forced next time, still "reconnect needed".
    scenario.silent = lambda: {"error": "temporarily_unavailable"}
    with pytest.raises(TokenUnavailable):
        auth.get_access_token()
    assert auth.indicator() == "reconnect needed"
    # An accepted send clears it.
    auth.note_token_accepted()
    assert auth.indicator() == "connected"
    scenario.silent = lambda: {"access_token": "t"}
    assert auth.get_access_token() == "t" and scenario.forced[-1] is False
    # A new sign-in clears it too.
    auth.note_token_rejected()
    sign_in(auth)
    assert auth.indicator() == "connected"
