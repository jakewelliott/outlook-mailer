"""Compose page, saving, settings prefill, and the preview routes with their own CSP."""

import html
import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import compose
from app.config import Settings, load_settings
from app.main import create_app
from app.security import CSP, email_preview_csp
from app.unsubscribe_tokens import make_token
from tests.conftest import VALID_ENV

BASE = "https://mail.example.com"
TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
DRAFT_RE = re.compile(r"/send/([0-9a-f]{32})/mapping$")
CSV = (
    b"Email,First Name\n"
    b"ada@example.com,Ada\n"
    b"bob@example.com,\n"
    b"eve@example.com,<script>alert('x')</script>\n"
)
FORM = {
    "subject": "Merry Christmas, {{first_name|Friend}}",
    "preheader": "News for {{first_name|you}}",
    "body": "Dear {{first_name|Friend}},\n\n**Thank you** for the year.",
    "headline": "Merry Christmas",
    "tagline": "and a Happy New Year",
    "business_name": "The Calico Barn",
    "postal_address": "123 Barn Road\r\nSpringfield, NC 27000",
    "is_advertisement": "1",
    "reason": "",
}


@pytest.fixture
def app_settings(valid_env: dict[str, str], tmp_path: Path) -> Settings:
    return load_settings({**valid_env, "DATA_DIR": str(tmp_path)})


@pytest.fixture
def client(app_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(app_settings), base_url=BASE) as c:
        yield c


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[TestClient, str]]:
    """One app with one saved draft, for the read-only preview tests."""
    settings = load_settings({**VALID_ENV, "DATA_DIR": str(tmp_path_factory.mktemp("shared"))})
    with TestClient(create_app(settings), base_url=BASE) as c:
        draft = make_draft(c)
        assert save(c, draft).status_code == 303
        yield c, draft


def csrf(client: TestClient) -> str:
    match = TOKEN_RE.search(client.get("/send/new").text)
    assert match
    return match.group(1)


def post(client: TestClient, url: str, data: dict[str, object], **kw: object) -> httpx.Response:
    return client.post(
        url,
        data={"csrf_token": csrf(client), **data},
        headers={"Origin": BASE},
        follow_redirects=False,
        **kw,  # type: ignore[arg-type]
    )


def make_draft(client: TestClient, data: bytes = CSV) -> str:
    r = client.post(
        "/send/new",
        data={"csrf_token": csrf(client)},
        files={"csv_file": ("list.csv", data, "text/csv")},
        headers={"Origin": BASE},
        follow_redirects=False,
    )
    match = DRAFT_RE.search(r.headers["location"])
    assert match
    draft = match.group(1)
    r = post(
        client,
        f"/send/{draft}/mapping",
        {
            "email": "Email",
            "first_name": "First Name",
            "status": "",
            "status_shown": "",
            "action": "save",
        },
    )
    assert r.status_code == 303, r.text
    return draft


def save(client: TestClient, draft: str, **changes: object) -> httpx.Response:
    """Submit the compose form; a change of None leaves the field out (an unticked checkbox)."""
    data = {k: v for k, v in {**FORM, **changes}.items() if v is not None}
    return post(client, f"/send/{draft}/compose", data)


def field_value(page: str, name: str) -> str:
    m = re.search(rf'<input type="text" id="{name}" name="{name}" value="([^"]*)"', page)
    if m is None:
        m = re.search(rf'<textarea id="{name}" name="{name}"[^>]*>(.*?)</textarea>', page, re.S)
    assert m, name
    return html.unescape(m.group(1))


def field_errors(page: str, name: str) -> list[str]:
    m = re.search(rf'<ul class="field-error" id="{name}-error">(.*?)</ul>', page, re.S)
    return [html.unescape(e) for e in re.findall(r"<li>(.*?)</li>", m.group(1))] if m else []


# --- compose page ------------------------------------------------------------------------------


def test_report_links_to_compose(client: TestClient) -> None:
    draft = make_draft(client)
    assert f'href="/send/{draft}/compose"' in client.get(f"/send/{draft}/report").text


def test_new_draft_has_defaults(client: TestClient) -> None:
    draft = make_draft(client)
    page = client.get(f"/send/{draft}/compose").text
    assert field_value(page, "subject") == ""
    assert field_value(page, "headline") == "Merry Christmas"
    assert field_value(page, "tagline") == "and a Happy New Year"
    assert field_value(page, "business_name") == ""
    assert 'name="is_advertisement" value="1" checked' in page
    assert "Save the message to see the preview." in page
    assert "<iframe" not in page
    assert 'href="/#markdown"' in page and 'href="/#merge-fields"' in page
    assert "You&#39;re receiving this email because you&#39;re a customer of your business" in page


def test_prefill_from_settings(client: TestClient) -> None:
    first = make_draft(client)
    r = save(client, first, is_advertisement=None, reason="You bought a quilt.", tagline="")
    assert r.status_code == 303
    second = make_draft(client)
    page = client.get(f"/send/{second}/compose").text
    assert field_value(page, "business_name") == "The Calico Barn"
    assert field_value(page, "postal_address") == "123 Barn Road\nSpringfield, NC 27000"
    assert field_value(page, "reason") == "You bought a quilt."
    assert field_value(page, "tagline") == ""
    assert 'name="is_advertisement" value="1">' in page  # unticked
    # The message itself is per draft.
    assert field_value(page, "subject") == "" and field_value(page, "body") == ""


def test_save_persists_and_redirects(client: TestClient, app_settings: Settings) -> None:
    draft = make_draft(client)
    r = save(client, draft, r="1", scheme="dark")
    assert r.status_code == 303
    assert r.headers["location"] == f"/send/{draft}/compose?r=1&scheme=dark&saved=1#preview"
    db = client.app.state.db  # type: ignore[attr-defined]
    stored = compose.get_content(db, draft)
    assert stored is not None
    assert stored.subject == FORM["subject"]
    assert stored.postal_address == "123 Barn Road\nSpringfield, NC 27000"
    assert stored.is_advertisement is True
    assert compose.get_settings(db)["business_name"] == "The Calico Barn"

    page = client.get(r.headers["location"]).text
    assert "Saved. The preview below shows this version." in page
    assert field_value(page, "body") == FORM["body"]
    assert f'src="/send/{draft}/preview.html?r=1&amp;scheme=dark"' in page
    assert f'src="/send/{draft}/preview.txt?r=1&amp;scheme=dark"' in page
    assert re.search(r'<iframe id="preview-html" class="preview-frame" sandbox title', page)
    assert '<option value="1" selected>' in page
    assert '<option value="dark" selected>' in page
    assert "Email size:" in page


def test_validation_errors_shown_with_input_kept(client: TestClient) -> None:
    draft = make_draft(client)
    r = save(
        client,
        draft,
        subject="Hi {{frist_name}}",
        preheader="{{oops",
        body="See [this](javascript:alert(1)) <b>now</b>",
        business_name="",
        postal_address="   ",
        headline="",
    )
    assert r.status_code == 400
    page = r.text
    assert "Not saved: 6 problems to fix" in page
    assert field_errors(page, "subject") == [
        "Unknown field {{frist_name}} — available: first_name, email."
    ]
    assert field_errors(page, "preheader")
    assert any("javascript" in e for e in field_errors(page, "body"))
    assert field_errors(page, "business_name") == ["The business or sender name is required."]
    assert field_errors(page, "postal_address") == ["The postal address is required (CAN-SPAM)."]
    assert field_errors(page, "headline") == ["The headline is required."]
    assert 'aria-describedby="subject-error" aria-invalid="true"' in page
    # Input kept as typed.
    assert field_value(page, "subject") == "Hi {{frist_name}}"
    assert field_value(page, "body") == "See [this](javascript:alert(1)) <b>now</b>"
    assert field_value(page, "postal_address") == "   "
    assert "&lt;b&gt;now&lt;/b&gt;" in page and "<b>now</b>" not in page
    # Nothing saved.
    assert compose.get_content(client.app.state.db, draft) is None  # type: ignore[attr-defined]
    assert compose.get_settings(client.app.state.db) == {}  # type: ignore[attr-defined]


def test_failed_save_keeps_last_saved_preview(client: TestClient) -> None:
    draft = make_draft(client)
    assert save(client, draft).status_code == 303
    r = save(client, draft, subject="")
    assert r.status_code == 400
    assert field_value(r.text, "subject") == ""
    assert f"/send/{draft}/preview.html?" in r.text  # still previews the saved version
    assert compose.get_content(client.app.state.db, draft).subject == FORM["subject"]  # type: ignore[attr-defined,union-attr]


def test_hostile_input_is_escaped(client: TestClient) -> None:
    draft = make_draft(client)
    evil = '"><script>alert(1)</script>'
    r = save(client, draft, subject=evil, business_name=evil, postal_address=evil, reason=evil)
    assert r.status_code == 303
    page = client.get(f"/send/{draft}/compose?r=2").text
    assert "<script>alert" not in page
    assert field_value(page, "subject") == evil
    # The recipient with a hostile first name is listed escaped.
    assert "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;" in page

    preview = client.get(f"/send/{draft}/preview.html?r=2")
    assert preview.status_code == 200
    assert "<script>" not in preview.text
    assert "&lt;script&gt;" in preview.text
    text = client.get(f"/send/{draft}/preview.txt?r=2")
    assert text.headers["content-type"].startswith("text/plain")


def test_compose_needs_csrf(client: TestClient) -> None:
    draft = make_draft(client)
    r = client.post(
        f"/send/{draft}/compose",
        data={**FORM, "csrf_token": "wrong"},
        headers={"Origin": BASE},
    )
    assert r.status_code == 403
    r = client.post(f"/send/{draft}/compose", data=FORM, headers={"Origin": BASE})
    assert r.status_code == 403
    r = client.post(
        f"/send/{draft}/compose",
        data={**FORM, "csrf_token": csrf(client)},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert compose.get_content(client.app.state.db, draft) is None  # type: ignore[attr-defined]


def test_unknown_draft(client: TestClient) -> None:
    assert client.get(f"/send/{'0' * 32}/compose").status_code == 404
    assert save(client, "0" * 32).status_code == 404
    assert client.get(f"/send/{'0' * 32}/preview.html").status_code == 404
    assert client.get("/send/nope/preview.txt").status_code == 404


def test_compose_without_mapping_redirects(client: TestClient) -> None:
    r = client.post(
        "/send/new",
        data={"csrf_token": csrf(client)},
        files={"csv_file": ("list.csv", CSV, "text/csv")},
        headers={"Origin": BASE},
        follow_redirects=False,
    )
    draft = DRAFT_RE.search(r.headers["location"]).group(1)  # type: ignore[union-attr]
    r = client.get(f"/send/{draft}/compose", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/send/{draft}/mapping"


# --- preview -----------------------------------------------------------------------------------


def test_preview_for_recipients_and_sample(shared: tuple[TestClient, str]) -> None:
    client, draft = shared
    ada = client.get(f"/send/{draft}/preview.html?r=0")
    assert ada.status_code == 200
    assert ada.headers["content-type"].startswith("text/html")
    assert "Dear Ada," in ada.text
    assert "News for Ada" in ada.text
    token = make_token("x" * 32, "ada@example.com")
    assert f'href="{BASE}/u/{token}"' in ada.text
    assert f'src="{BASE}/img/holly-v1.png"' in ada.text

    bob = client.get(f"/send/{draft}/preview.html?r=1").text
    assert "Dear Friend," in bob

    sample = client.get(f"/send/{draft}/preview.html?r=sample").text
    assert "Dear Friend," in sample and "News for you" in sample
    assert make_token("x" * 32, "friend@example.com") in sample

    text = client.get(f"/send/{draft}/preview.txt?r=0").text
    assert text.startswith("Subject: Merry Christmas, Ada\nPreview text: News for Ada\n\n")
    assert "Dear Ada,\n\nThank you for the year." in text
    assert f"Unsubscribe: {BASE}/u/{token}" in text

    # The default recipient is the first one.
    assert "Dear Ada," in client.get(f"/send/{draft}/preview.html").text


def test_preview_headers(shared: tuple[TestClient, str]) -> None:
    client, draft = shared
    for path in (f"/send/{draft}/preview.html?r=0", f"/send/{draft}/preview.txt?r=0"):
        r = client.get(path)
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        assert csp == email_preview_csp(BASE)
        assert csp == (
            "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' "
            "https://mail.example.com; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        )
        assert "script-src" not in csp
        assert r.headers["x-frame-options"] == "SAMEORIGIN"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-store"
    # Every other page keeps the strict policy, the compose page included.
    for path in (f"/send/{draft}/compose", "/", "/send/new"):
        r = client.get(path)
        assert r.headers["content-security-policy"] == CSP
        assert r.headers["x-frame-options"] == "DENY"
    assert "frame-src 'self'" in CSP


def test_preview_error_responses_keep_preview_headers(client: TestClient) -> None:
    draft = make_draft(client)
    r = client.get(f"/send/{draft}/preview.html?r=0")
    assert r.status_code == 404
    assert "Save the message" in r.text
    assert r.headers["x-frame-options"] == "SAMEORIGIN"


@pytest.mark.parametrize(
    "query",
    [
        "r=3",
        "r=50",
        "r=-1",
        "r=abc",
        "r=1e1",
        "r=0001",
        "r=%201",
        "r=%C2%B2",  # superscript two: str.isdigit() is True, int() fails
        "r=%D9%A3",  # Arabic-Indic three: int() reads it as 3
        "r=%EF%BC%91",  # fullwidth one: int() reads it as 1
        "scheme=blue",
    ],
)
def test_preview_only_for_valid_indices(shared: tuple[TestClient, str], query: str) -> None:
    client, draft = shared
    assert client.get(f"/send/{draft}/preview.html?{query}").status_code == 404
    if query.startswith("r="):
        assert client.get(f"/send/{draft}/preview.txt?{query}").status_code == 404
        # The compose page and a save fall back to the first recipient instead.
        page = client.get(f"/send/{draft}/compose?{query}")
        assert page.status_code == 200
        assert f"/send/{draft}/preview.html?r=0&amp;scheme=light" in page.text


@pytest.mark.parametrize("r", ["\u00b2", "\u0663", "99"])
def test_save_ignores_bad_preview_choice(client: TestClient, r: str) -> None:
    draft = make_draft(client)
    resp = save(client, draft, r=r)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/send/{draft}/compose?r=0&scheme=light&saved=1#preview"


def test_compose_ignores_bad_preview_choice(shared: tuple[TestClient, str]) -> None:
    client, draft = shared
    page = client.get(f"/send/{draft}/compose?r=99&scheme=blue").text
    assert f"/send/{draft}/preview.html?r=0&amp;scheme=light" in page


def test_dark_preview(shared: tuple[TestClient, str]) -> None:
    client, draft = shared
    light = client.get(f"/send/{draft}/preview.html?r=0&scheme=light").text
    dark = client.get(f"/send/{draft}/preview.html?r=0&scheme=dark").text
    assert '<meta name="color-scheme" content="dark">' in dark
    assert '<meta name="color-scheme" content="dark">' not in light
    assert dark.count("!important") > light.count("!important")


def test_preview_needs_auth(valid_env: dict[str, str], tmp_path: Path) -> None:
    secured = load_settings(
        {
            **valid_env,
            "DATA_DIR": str(tmp_path),
            "AUTH_DISABLED": "0",
            "CF_ACCESS_TEAM_DOMAIN": "team.cloudflareaccess.com",
            "CF_ACCESS_AUD": "aud",
        }
    )
    c = TestClient(create_app(secured), base_url=BASE)
    for path in ("/send/" + "a" * 32 + "/preview.html", "/send/" + "a" * 32 + "/compose"):
        r = c.get(path)
        assert r.status_code == 403
        # A denied request never gets the preview policy.
        assert r.headers["content-security-policy"] == CSP


# --- size --------------------------------------------------------------------------------------


def test_size_warning(client: TestClient) -> None:
    draft = make_draft(client)
    para = "Plenty of words in this paragraph to fill the email up nicely. " * 10
    body = "\n\n".join(f"## Part {i}\n\n{para}" for i in range(95))  # about 97 KB
    r = save(client, draft, body=body)
    assert r.status_code == 303, compose.validate(compose.DraftContent(body=body))
    page = client.get(r.headers["location"]).text
    size = re.search(r'<p class="([^"]*)" id="size"', page)
    assert size and size.group(1) == "warn-box", page[page.find('id="size"') - 100 :][:400]
    assert "close to the 102 KB point" in page


def test_size_limit_blocks_saving(client: TestClient) -> None:
    draft = make_draft(client)
    para = "Plenty of words in this paragraph to fill the email up nicely. " * 10
    body = "\n\n".join(f"## Part {i}\n\n{para}" for i in range(130))  # about 128 KB
    r = save(client, draft, body=body)
    assert r.status_code == 400
    assert any("over the 102 KB limit" in e for e in field_errors(r.text, "body"))
    assert field_value(r.text, "body") == body
    assert compose.get_content(client.app.state.db, draft) is None  # type: ignore[attr-defined]


def test_form_maxlength_matches_server_limits(shared: tuple[TestClient, str]) -> None:
    client, draft = shared
    page = client.get(f"/send/{draft}/compose").text
    for name in FORM:
        if name == "is_advertisement":
            continue
        tag = re.search(rf'<(?:input|textarea)[^>]*\bname="{name}"[^>]*>', page)
        assert tag, name
        assert f'maxlength="{compose.FORM_LIMITS[name]}"' in tag.group(0), name
    assert set(compose.FORM_LIMITS) == set(FORM) - {"is_advertisement"}


def test_footer_limits_are_the_form_limits() -> None:
    long_name = "n" * (compose.FORM_LIMITS["business_name"] + 1)
    c = compose.DraftContent(**{**FORM, "business_name": long_name, "is_advertisement": True})
    assert "business_name" in compose.validate(c)
    ok = compose.DraftContent(**{**FORM, "business_name": long_name[:-1], "is_advertisement": True})
    assert "business_name" not in compose.validate(ok)
