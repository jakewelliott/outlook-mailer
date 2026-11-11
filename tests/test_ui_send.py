import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import Settings, load_settings
from app.main import create_app
from app.ui_send import UploadTooLarge, limit_upload_size

BASE = "https://mail.example.com"
FIXTURES = Path(__file__).parent / "fixtures"
SQUARE = (FIXTURES / "square_export.csv").read_bytes()
TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
DRAFT_RE = re.compile(r"/send/([0-9a-f]{32})/mapping$")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def app_settings(valid_env: dict[str, str], data_dir: Path) -> Settings:
    return load_settings({**valid_env, "DATA_DIR": str(data_dir), "MAX_UPLOAD_MB": "1"})


@pytest.fixture
def client(app_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(app_settings), base_url=BASE) as c:
        yield c


def token(client: TestClient) -> str:
    r = client.get("/send/new")
    assert r.status_code == 200
    match = TOKEN_RE.search(r.text)
    assert match
    return match.group(1)


def upload(
    client: TestClient, data: bytes, name: str = "list.csv", content_type: str = "text/csv"
) -> httpx.Response:
    return client.post(
        "/send/new",
        data={"csrf_token": token(client)},
        files={"csv_file": (name, data, content_type)},
        headers={"Origin": BASE},
        follow_redirects=False,
    )


def upload_ok(client: TestClient, data: bytes = SQUARE) -> str:
    r = upload(client, data)
    assert r.status_code == 303, r.text
    match = DRAFT_RE.search(r.headers["location"])
    assert match
    return match.group(1)


def post_mapping(client: TestClient, draft: str, fields: dict[str, object]) -> httpx.Response:
    return client.post(
        f"/send/{draft}/mapping",
        data={"csrf_token": token(client), **fields},
        headers={"Origin": BASE},
        follow_redirects=False,
    )


def selected_option(html: str, name: str) -> str | None:
    select = re.search(rf'<select id="{name}".*?</select>', html, re.S)
    assert select
    match = re.search(r'<option value="([^"]*)" selected>', select.group(0))
    return match.group(1) if match else None


def checked_values(html: str) -> set[str]:
    return set(re.findall(r'name="status_value" value="([^"]*)" checked', html))


def blank_row(html: str) -> tuple[str, str]:
    """Label and row count of the blank status value."""
    match = re.search(
        r'name="status_value" value="">\s*</td>\s*<td><label for="sv-\d+"><em>([^<]*)</em>'
        r"</label></td>\s*<td>(\d+)</td>",
        html,
    )
    assert match
    return match.group(1), match.group(2)


def test_upload_page(client: TestClient) -> None:
    r = client.get("/send/new")
    assert 'enctype="multipart/form-data"' in r.text
    assert 'accept=".csv,text/csv"' in r.text


def test_upload_needs_csrf(client: TestClient) -> None:
    r = client.post(
        "/send/new", files={"csv_file": ("a.csv", SQUARE, "text/csv")}, headers={"Origin": BASE}
    )
    assert r.status_code == 403


def test_upload_needs_access_when_auth_enabled(valid_env: dict[str, str], data_dir: Path) -> None:
    s = load_settings(
        {
            **valid_env,
            "AUTH_DISABLED": "0",
            "CF_ACCESS_TEAM_DOMAIN": "team.cloudflareaccess.com",
            "CF_ACCESS_AUD": "aud",
            "DATA_DIR": str(data_dir),
        }
    )
    c = TestClient(create_app(s), base_url=BASE)
    assert c.get("/send/new").status_code == 403
    assert c.get("/send/" + "0" * 32 + "/report").status_code == 403


def test_full_flow(client: TestClient, data_dir: Path) -> None:
    draft = upload_ok(client)
    stored = data_dir / "uploads" / f"{draft}.csv"
    assert stored.read_bytes() == SQUARE
    assert oct(stored.stat().st_mode & 0o777) == "0o600"

    page = client.get(f"/send/{draft}/mapping").text
    assert selected_option(page, "email") == "Email Address"
    assert selected_option(page, "first_name") == "First Name"
    assert selected_option(page, "status") == "Email Subscription Status"
    assert "— none: send to everyone —" in page
    assert checked_values(page) == {"subscribed"}
    assert blank_row(page) == ("(blank — no status)", "2")  # unticked by default
    assert "bounced" in page

    r = post_mapping(
        client,
        draft,
        {
            "email": "Email Address",
            "first_name": "First Name",
            "status": "Email Subscription Status",
            "status_shown": "Email Subscription Status",
            "status_value": ["subscribed"],
            "action": "save",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/send/{draft}/report"

    report = client.get(f"/send/{draft}/report").text
    assert "3 of 10 rows will be sent the email" in report
    assert "noel@example" in report
    assert "Same address as row 3." in report
    assert f'href="/send/{draft}/issues.csv"' in report
    assert f'href="/send/{draft}/compose"' in report

    # The saved selection is shown again when coming back to the mapping page.
    assert checked_values(client.get(f"/send/{draft}/mapping").text) == {"subscribed"}

    issues = client.get(f"/send/{draft}/issues.csv")
    assert issues.headers["content-type"].startswith("text/csv")
    assert 'filename="list-issues.csv"' in issues.headers["content-disposition"]
    assert issues.text.startswith("\ufeffrow,problem,email,first_name,detail")
    assert client.get(f"/send/{draft}/compose").status_code == 200


def test_required_dropdowns_enforced(client: TestClient) -> None:
    draft = upload_ok(client)
    r = post_mapping(
        client, draft, {"email": "", "first_name": "", "status": "", "status_shown": ""}
    )
    assert r.status_code == 400
    assert "Choose the Email address column." in r.text
    assert "Choose the First name column." in r.text
    assert client.get(f"/send/{draft}/report", follow_redirects=False).status_code == 303


def test_no_status_column(client: TestClient) -> None:
    draft = upload_ok(client)
    r = post_mapping(
        client,
        draft,
        {"email": "Email Address", "first_name": "First Name", "status": "", "status_shown": ""},
    )
    assert r.status_code == 303
    assert "7 of 10 rows will be sent the email" in client.get(f"/send/{draft}/report").text


def test_ticking_blank_status(client: TestClient) -> None:
    draft = upload_ok(client)
    r = post_mapping(
        client,
        draft,
        {
            "email": "Email Address",
            "first_name": "First Name",
            "status": "Email Subscription Status",
            "status_shown": "Email Subscription Status",
            "status_value": ["subscribed", ""],
            "action": "save",
        },
    )
    assert r.status_code == 303
    mapping = client.get(f"/send/{draft}/mapping").text
    assert checked_values(mapping) == {"subscribed", ""}
    report = client.get(f"/send/{draft}/report").text
    # Row 11 (email, blank status) is sent; row 9 (no email, blank status) is missing email.
    assert "4 of 10 rows will be sent the email" in report
    assert "<tr><td>11</td><td>eve@example.com</td><td>Eve</td></tr>" in report
    missing = "<td>Missing email</td><td></td><td>No email address.</td>"
    assert f"<tr><td>9</td>{missing}</tr>" in report
    assert "Status is blank." not in report


def test_changing_status_column_lists_its_values_first(client: TestClient) -> None:
    draft = upload_ok(client, b"email,first,consent,other\na@x.com,A,yes,p\nb@x.com,B,no,q\n")
    # Status changed from (none) to "other": nothing is saved, the page shows the new values.
    r = post_mapping(
        client,
        draft,
        {"email": "email", "first_name": "first", "status": "other", "status_shown": ""},
    )
    assert r.status_code == 200
    assert checked_values(r.text) == {"p", "q"}
    assert 'name="status_shown" value="other"' in r.text
    assert client.get(f"/send/{draft}/report", follow_redirects=False).status_code == 303


def test_status_needs_one_value_ticked(client: TestClient) -> None:
    draft = upload_ok(client, b"email,first,consent\na@x.com,A,yes\nb@x.com,B,no\n")
    fields = {"email": "email", "first_name": "first", "status": "consent"}
    r = post_mapping(client, draft, {**fields, "status_shown": "consent"})
    assert r.status_code == 400
    assert "Tick at least one status value" in r.text
    r = post_mapping(client, draft, {**fields, "status_shown": "consent", "status_value": ["no"]})
    assert r.status_code == 303
    assert "1 of 2 rows" in client.get(f"/send/{draft}/report").text


def test_unsubscribed_file_skips(client: TestClient, data_dir: Path) -> None:
    (data_dir / "unsubscribed.txt").write_text("A@X.com\t2026-12-01T00:00:00+00:00\n")
    draft = upload_ok(client, b"email,first\na@x.com,A\nb@x.com,B\n")
    post_mapping(client, draft, {"email": "email", "first_name": "first", "status_shown": ""})
    report = client.get(f"/send/{draft}/report").text
    assert "1 of 2 rows" in report
    assert "This address is in unsubscribed.txt." in report


def test_csv_content_is_escaped(client: TestClient) -> None:
    draft = upload_ok(
        client, b"email,<b>first</b>,s\n<script>@x.com,<i>A</i>,<img src=x>\nb@x.com,B,ok\n"
    )
    mapping = client.get(f"/send/{draft}/mapping").text
    assert "<b>first</b>" not in mapping and "&lt;b&gt;first&lt;/b&gt;" in mapping
    post_mapping(
        client, draft, {"email": "email", "first_name": "<b>first</b>", "status_shown": ""}
    )
    report = client.get(f"/send/{draft}/report").text
    assert "<script>" not in report and "&lt;script&gt;@x.com" in report


def test_too_many_status_values(client: TestClient) -> None:
    rows = "".join(f"u{i}@x.com,N{i}\n" for i in range(60))
    draft = upload_ok(client, ("email,first\n" + rows).encode())
    r = post_mapping(
        client,
        draft,
        {"email": "email", "first_name": "first", "status": "email", "status_shown": ""},
    )
    assert "doesn&#39;t look like a subscription status column" in r.text
    assert 'name="status_value"' not in r.text


@pytest.mark.parametrize(
    ("data", "name", "content_type", "message"),
    [
        (b"email,first\n", "list.csv", "text/csv", "no data rows"),
        (b"", "list.csv", "text/csv", "The file is empty."),
        (b"email,first\na@x.com,A\n", "photo.png", "image/png", "doesn&#39;t look like a CSV"),
        (b"email,first\n" + b"a@x.com,A\n" * 2001, "big.csv", "text/csv", "more than 2000 rows"),
    ],
)
def test_upload_errors(
    client: TestClient, data: bytes, name: str, content_type: str, message: str
) -> None:
    r = upload(client, data, name, content_type)
    assert r.status_code == 400
    assert message in r.text


def test_excel_content_type_accepted(client: TestClient) -> None:
    r = upload(client, b"email,first\na@x.com,A\n", "list", "application/vnd.ms-excel")
    assert r.status_code == 303


def test_upload_over_size_limit(client: TestClient) -> None:
    big = b"email,first\n" + b"a@x.com,A\n" * 120_000  # > 1 MB
    r = upload(client, big)
    assert r.status_code == 413
    assert "larger than 1 MB" in r.text


def test_unknown_draft(client: TestClient) -> None:
    assert client.get("/send/" + "0" * 32 + "/mapping").status_code == 404
    assert client.get("/send/not-an-id/report").status_code == 404


def test_deleted_csv(client: TestClient, data_dir: Path) -> None:
    draft = upload_ok(client)
    (data_dir / "uploads" / f"{draft}.csv").unlink()
    assert client.get(f"/send/{draft}/mapping").status_code == 410


@pytest.mark.parametrize(
    ("length", "too_large"),
    [
        (b"", False),
        (b"100", False),
        (b"\xb2", False),  # latin-1 "²": str.isdigit() is True but int() fails
        (b"1\xb9", False),
        (b"99999999999", True),
        (b"9" * 30, True),
    ],
)
def test_upload_size_guard_reads_only_ascii_digits(
    app_settings: Settings, length: bytes, too_large: bool
) -> None:
    app = SimpleNamespace(state=SimpleNamespace(settings=app_settings))
    request = Request({"type": "http", "headers": [(b"content-length", length)], "app": app})
    if too_large:
        with pytest.raises(UploadTooLarge):
            asyncio.run(limit_upload_size(request))
    else:
        asyncio.run(limit_upload_size(request))
