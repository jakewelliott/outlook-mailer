import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.main import create_app
from app.security import (
    ADMIN_DEPENDENCIES,
    CSP,
    CSRF_COOKIE,
    WS_DENIED_CODE,
    csrf_protect,
    csrf_token,
    is_public_path,
)
from app.ui import NAV

TEAM = "team.cloudflareaccess.com"
ISS = f"https://{TEAM}"
AUD = "test-aud-tag"
CERTS_URL = f"{ISS}/cdn-cgi/access/certs"
BASE = "https://mail.example.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(*pairs: tuple[str, rsa.RSAPrivateKey]) -> dict[str, Any]:
    keys = []
    for kid, key in pairs:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        keys.append({**jwk, "kid": kid, "alg": "RS256", "use": "sig"})
    return {"keys": keys, "public_cert": {"kid": pairs[0][0], "cert": "unused"}}


def make_token(
    key: rsa.RSAPrivateKey = KEY, kid: str = "k1", alg: str = "RS256", **overrides: Any
) -> str:
    now = int(time.time())
    claims = {
        "aud": [AUD],
        "iss": ISS,
        "email": "jake@example.com",
        "sub": "user-id",
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        **overrides,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


@pytest.fixture
def auth_settings(valid_env: dict[str, str]) -> Settings:
    return load_settings(
        {**valid_env, "AUTH_DISABLED": "0", "CF_ACCESS_TEAM_DOMAIN": TEAM, "CF_ACCESS_AUD": AUD}
    )


@pytest.fixture
def certs() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock.get(CERTS_URL).mock(return_value=httpx.Response(200, json=jwks(("k1", KEY))))


def add_test_routes(app: FastAPI) -> None:
    router = APIRouter(dependencies=ADMIN_DEPENDENCIES)

    @router.get("/_test/form", response_class=PlainTextResponse)
    def form(request: Request) -> str:
        return csrf_token(request)

    @router.post("/_test/submit", response_class=PlainTextResponse)
    def submit() -> str:
        return "ok"

    app.include_router(router)


@pytest.fixture
def client(auth_settings: Settings, certs: respx.Route) -> TestClient:
    app = create_app(auth_settings)
    add_test_routes(app)
    return TestClient(app, base_url=BASE)


def auth(token: str | None = None) -> dict[str, str]:
    return {"Cf-Access-Jwt-Assertion": token or make_token()}


# --- Access JWT ---------------------------------------------------------------------------


def test_admin_requires_header(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 403
    assert "Cloudflare Access" in r.text


def test_valid_token_shows_email(client: TestClient) -> None:
    r = client.get("/", headers=auth())
    assert r.status_code == 200
    assert "jake@example.com" in r.text


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        make_token(key=OTHER_KEY),  # bad signature
        make_token(aud=["other-aud"]),
        make_token(iss="https://evil.cloudflareaccess.com"),
        make_token(exp=int(time.time()) - 3600, iat=int(time.time()) - 7200),
        make_token(nbf=int(time.time()) + 3600),
        make_token(exp=None),
        make_token(email=None),
        make_token(kid="unknown"),
    ],
    ids=[
        "garbage",
        "bad-signature",
        "wrong-aud",
        "wrong-iss",
        "expired",
        "not-yet-valid",
        "no-exp",
        "no-email",
        "unknown-kid",
    ],
)
def test_invalid_tokens_rejected(client: TestClient, token: str) -> None:
    r = client.get("/", headers=auth(token))
    assert r.status_code == 403
    assert "jake@example.com" not in r.text
    assert "Signature" not in r.text and "audience" not in r.text.lower()


def test_hs256_with_public_key_rejected(client: TestClient) -> None:
    token = jwt.encode(
        {"aud": AUD, "iss": ISS, "email": "x@example.com", "exp": int(time.time()) + 60},
        "k" * 32,
        algorithm="HS256",
        headers={"kid": "k1"},
    )
    assert client.get("/", headers=auth(token)).status_code == 403


def test_cookie_is_not_accepted(client: TestClient) -> None:
    client.cookies.set("CF_Authorization", make_token())
    assert client.get("/").status_code == 403


def test_keys_are_cached(client: TestClient, certs: respx.Route) -> None:
    for _ in range(3):
        assert client.get("/", headers=auth()).status_code == 200
    assert certs.call_count == 1


def test_unknown_kid_refetches_once_then_throttles(client: TestClient, certs: respx.Route) -> None:
    assert client.get("/", headers=auth()).status_code == 200
    certs.mock(return_value=httpx.Response(200, json=jwks(("k1", KEY), ("k2", OTHER_KEY))))
    # Fetched less than MIN_REFETCH_INTERVAL ago, so a new kid isn't fetched yet.
    assert client.get("/", headers=auth(make_token(OTHER_KEY, kid="k2"))).status_code == 403
    guard_keys = _key_set(client)
    guard_keys._fetched_at -= 60
    guard_keys._attempted_at -= 60
    assert client.get("/", headers=auth(make_token(OTHER_KEY, kid="k2"))).status_code == 200
    assert certs.call_count == 2


def test_outage_with_cached_keys_throttles_refetches(
    client: TestClient, certs: respx.Route
) -> None:
    assert client.get("/", headers=auth()).status_code == 200
    assert certs.call_count == 1
    keys = _key_set(client)
    certs.mock(side_effect=httpx.ConnectTimeout("down"))
    # Keys are stale: the next request tries once, fails, and keeps using the cached keys.
    keys._fetched_at -= 3601
    keys._attempted_at -= 3601
    assert client.get("/", headers=auth()).status_code == 200
    assert certs.call_count == 2
    # During the outage neither stale keys nor unknown kids refetch within the interval.
    for _ in range(3):
        assert client.get("/", headers=auth()).status_code == 200
        assert client.get("/", headers=auth(make_token(OTHER_KEY, kid="k2"))).status_code == 403
    assert certs.call_count == 2
    # Once the interval has passed since the last *attempt*, one more try.
    keys._attempted_at -= 31
    assert client.get("/", headers=auth(make_token(OTHER_KEY, kid="k2"))).status_code == 403
    assert client.get("/", headers=auth(make_token(OTHER_KEY, kid="k2"))).status_code == 403
    assert certs.call_count == 3


def _key_set(client: TestClient) -> Any:
    app = client.app
    node = app.middleware_stack
    while node is not None and type(node).__name__ != "RequestGuard":
        node = getattr(node, "app", None)
    assert node is not None
    return node.verifier.keys


def test_cert_fetch_failure_is_503(auth_settings: Settings) -> None:
    with respx.mock() as mock:
        mock.get(CERTS_URL).mock(side_effect=httpx.ConnectTimeout("boom"))
        r = TestClient(create_app(auth_settings), base_url=BASE).get("/", headers=auth())
    assert r.status_code == 503


def test_cert_server_error_is_503(auth_settings: Settings) -> None:
    with respx.mock() as mock:
        mock.get(CERTS_URL).mock(return_value=httpx.Response(500))
        r = TestClient(create_app(auth_settings), base_url=BASE).get("/", headers=auth())
    assert r.status_code == 503


def test_unknown_paths_need_auth_too(client: TestClient) -> None:
    assert client.get("/no-such-page").status_code == 403
    assert client.get("/static/app.css").status_code == 403
    assert client.get("/no-such-page", headers=auth()).status_code == 404


def test_public_paths_need_no_auth(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
    # Not built yet, but reachable without auth (404, not 403).
    assert client.get("/u/sometoken").status_code == 404
    assert client.get("/img/holly.png").status_code == 404


@pytest.mark.parametrize(
    ("path", "public"),
    [
        ("/healthz", True),
        ("/u/abc", True),
        ("/img/a.png", True),
        ("/", False),
        ("/u", False),
        ("/img", False),
        ("/healthz/", False),
        ("/static/app.css", False),
        ("/uploads", False),
        ("/images/x", False),
    ],
)
def test_is_public_path(path: str, public: bool) -> None:
    assert is_public_path(path) is public


def test_auth_disabled_bypass(settings: Settings) -> None:
    client = TestClient(create_app(settings), base_url=BASE)
    r = client.get("/")
    assert r.status_code == 200
    assert "AUTH_DISABLED=1" in r.text
    assert client.get("/static/app.css").status_code == 200


# --- CSRF and Origin ----------------------------------------------------------------------


def csrf_session(client: TestClient, headers: dict[str, str]) -> str:
    r = client.get("/_test/form", headers=headers)
    assert r.status_code == 200
    assert CSRF_COOKIE in client.cookies
    return r.text


def post(client: TestClient, headers: dict[str, str], **kwargs: Any) -> httpx.Response:
    return client.post("/_test/submit", headers={"Origin": BASE, **headers}, **kwargs)


def test_csrf_valid_token_passes(client: TestClient) -> None:
    h = auth()
    token = csrf_session(client, h)
    assert post(client, h, data={"csrf_token": token}).status_code == 200
    assert post(client, {**h, "X-CSRF-Token": token}).status_code == 200


def test_csrf_missing_or_invalid_rejected(client: TestClient) -> None:
    h = auth()
    token = csrf_session(client, h)
    assert post(client, h).status_code == 403
    assert post(client, h, data={"csrf_token": "nope"}).status_code == 403
    # Change the first character to a different one. (Replacing the last one with "A" left the
    # token unchanged 1 time in 16: it carries only 4 bits, so "A" is one of 16 values.)
    tampered = ("B" if token[0] == "A" else "A") + token[1:]
    assert tampered != token
    assert post(client, h, data={"csrf_token": tampered}).status_code == 403


def test_csrf_token_bound_to_cookie_and_user(client: TestClient) -> None:
    h = auth()
    token = csrf_session(client, h)
    other_user = auth(make_token(email="mallory@example.com"))
    assert post(client, other_user, data={"csrf_token": token}).status_code == 403
    client.cookies.set(CSRF_COOKIE, "A" * 43)
    assert post(client, h, data={"csrf_token": token}).status_code == 403


def test_csrf_token_survives_restart(auth_settings: Settings, certs: respx.Route) -> None:
    h = auth()
    first = create_app(auth_settings)
    add_test_routes(first)
    c1 = TestClient(first, base_url=BASE)
    token = csrf_session(c1, h)
    second = create_app(auth_settings)
    add_test_routes(second)
    c2 = TestClient(second, base_url=BASE, cookies=dict(c1.cookies))
    assert post(c2, h, data={"csrf_token": token}).status_code == 200


def test_csrf_cookie_attributes(client: TestClient) -> None:
    r = client.get("/", headers=auth())
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{CSRF_COOKIE}=")
    for attr in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/"):
        assert attr in cookie
    # Not reissued once the browser has it.
    assert "set-cookie" not in client.get("/", headers=auth()).headers


@pytest.mark.parametrize(
    "origin_headers",
    [
        {"Origin": "https://evil.example.com"},
        {"Origin": "http://mail.example.com"},
        {"Origin": "null"},
        {"Origin": "", "Referer": "https://evil.example.com/page"},
        {"Origin": ""},
    ],
    ids=["other-host", "other-scheme", "null", "bad-referer", "none"],
)
def test_origin_mismatch_rejected(client: TestClient, origin_headers: dict[str, str]) -> None:
    h = auth()
    token = csrf_session(client, h)
    r = client.post("/_test/submit", headers={**h, **origin_headers}, data={"csrf_token": token})
    assert r.status_code == 403


def test_referer_accepted_without_origin(client: TestClient) -> None:
    h = auth()
    token = csrf_session(client, h)
    r = client.post(
        "/_test/submit",
        headers={**h, "Referer": f"{BASE}/send/new"},
        data={"csrf_token": token},
    )
    assert r.status_code == 200


def test_auth_disabled_allows_own_host_origin(settings: Settings) -> None:
    app = create_app(settings)
    add_test_routes(app)
    client = TestClient(app, base_url="http://localhost:8765")
    token = csrf_session(client, {})
    r = client.post(
        "/_test/submit", headers={"Origin": "http://localhost:8765"}, data={"csrf_token": token}
    )
    assert r.status_code == 200
    r = client.post(
        "/_test/submit", headers={"Origin": "http://evil:8765"}, data={"csrf_token": token}
    )
    assert r.status_code == 403
    fresh = TestClient(app, base_url="http://localhost:8765").get("/")
    assert "Secure" not in fresh.headers["set-cookie"]


def test_every_admin_route_has_csrf_dependency(settings: Settings) -> None:
    app = create_app(settings)
    for route in app.routes:
        if isinstance(route, APIRoute) and not is_public_path(route.path):
            calls = [d.call for d in route.dependant.dependencies]
            assert csrf_protect in calls, route.path


# --- Headers and pages --------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/healthz", "/static/app.css", "/no-such-page"])
def test_security_headers(client: TestClient, path: str) -> None:
    r = client.get(path, headers=auth())
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_security_headers_on_denied(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 403
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["cache-control"] == "no-store"


def test_admin_pages_not_cached(client: TestClient) -> None:
    assert client.get("/", headers=auth()).headers["cache-control"] == "no-store"
    assert "no-store" not in client.get("/healthz").headers.get("cache-control", "")


def test_help_page_lists_merge_fields_literally(settings: Settings) -> None:
    html = TestClient(create_app(settings)).get("/").text
    for field in ("{{first_name}}", "{{email}}", "{{first_name|Friend}}"):
        assert field in html
    for syntax in ("**bold**", "*italic*", "~~strike~~", "++underline++", "unsubscribed.txt"):
        assert syntax in html
    assert "6 per minute" in html and "250 in any 24 hours" in html


def test_help_page_has_no_inline_script_or_style(settings: Settings) -> None:
    html = TestClient(create_app(settings)).get("/").text
    assert re.search(r"<script(?![^>]*\bsrc=)", html) is None
    assert "<style" not in html and "style=" not in html
    assert 'href="/static/app.css"' in html and 'src="/static/app.js"' in html


def test_every_nav_page_is_a_real_route(valid_env: dict[str, str], tmp_path: Path) -> None:
    # Some of these pages create files in DATA_DIR; the default /data is only writable as root
    # (CI runs as a normal user).
    # There is no catch-all page, so a 200 means a real route serves the nav entry.
    c = TestClient(create_app(load_settings({**valid_env, "DATA_DIR": str(tmp_path)})))
    for path, _label in NAV:
        assert c.get(path).status_code == 200, path


# Admin pages must send Referrer-Policy: same-origin. Under no-referrer, Chromium and Firefox
# send "Origin: null" and no Referer on same-origin form POSTs, so the Origin check would reject
# every admin form (TestClient sets Origin by hand, so other tests can't catch that).
@pytest.mark.parametrize("path", ["/", "/static/app.css", "/no-such-page"])
def test_admin_referrer_policy_is_same_origin(client: TestClient, path: str) -> None:
    assert client.get(path, headers=auth()).headers["referrer-policy"] == "same-origin"
    assert client.get(path).headers["referrer-policy"] == "same-origin"  # denied too


@pytest.mark.parametrize("path", ["/healthz", "/u/sometoken", "/img/holly.png"])
def test_public_referrer_policy_is_no_referrer(client: TestClient, path: str) -> None:
    assert client.get(path).headers["referrer-policy"] == "no-referrer"


def test_origin_null_rejected_even_with_good_referer(client: TestClient) -> None:
    h = auth()
    token = csrf_session(client, h)
    r = client.post(
        "/_test/submit",
        headers={**h, "Origin": "null", "Referer": f"{BASE}/send/new"},
        data={"csrf_token": token},
    )
    assert r.status_code == 403


def test_healthz_head(client: TestClient) -> None:
    r = client.head("/healthz")
    assert r.status_code == 200
    assert r.content == b""


# --- Websockets ---------------------------------------------------------------------------


@pytest.fixture
def ws_client(auth_settings: Settings, certs: respx.Route) -> TestClient:
    app = create_app(auth_settings)

    @app.websocket("/_test/ws")
    async def admin_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(websocket.state.user_email)
        await websocket.close()

    @app.websocket("/u/_test_ws")
    async def public_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("public")
        await websocket.close()

    return TestClient(app, base_url=BASE)


@pytest.mark.parametrize("token", [None, "not-a-jwt", make_token(aud=["other-aud"])])
def test_admin_websocket_denied_without_valid_jwt(ws_client: TestClient, token: str | None) -> None:
    headers = {} if token is None else {"Cf-Access-Jwt-Assertion": token}
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        ws_client.websocket_connect("/_test/ws", headers=headers),
    ):
        pass
    assert exc_info.value.code == WS_DENIED_CODE


def test_admin_websocket_allowed_with_valid_jwt(ws_client: TestClient) -> None:
    with ws_client.websocket_connect("/_test/ws", headers=auth()) as ws:
        assert ws.receive_text() == "jake@example.com"


def test_unknown_websocket_path_denied(ws_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info, ws_client.websocket_connect("/anything"):
        pass
    assert exc_info.value.code == WS_DENIED_CODE


def test_public_websocket_needs_no_jwt(ws_client: TestClient) -> None:
    with ws_client.websocket_connect("/u/_test_ws") as ws:
        assert ws.receive_text() == "public"
