"""Start page, campaign pages, progress JSON, controls (CSRF), results download, worker lifespan."""

import asyncio
import csv
import io
import re
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import campaigns, drafts, gate
from app.config import Settings, load_settings
from app.main import create_app
from app.transport import SendResult
from tests.test_sender import FakeClock
from tests.test_ui_compose import BASE, make_draft, post, save

STATE = re.compile(r'name="content_hash" value="([0-9a-f]{64})"')


@pytest.fixture
def app_settings(valid_env: dict[str, str], tmp_path: Path) -> Settings:
    return load_settings({**valid_env, "DATA_DIR": str(tmp_path), "SEND_RATE_PER_MINUTE": "60"})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(app_settings: Settings, clock: FakeClock) -> Iterator[TestClient]:
    """The worker is built but not started: tests step it with the fake clock."""
    with TestClient(create_app(app_settings, clock=clock, run_worker=False), base_url=BASE) as c:
        yield c


def step_all(client: TestClient, clock: FakeClock) -> None:
    worker = client.app.state.worker  # type: ignore[attr-defined]

    async def go() -> None:
        for _ in range(100):
            delay = await worker.step()
            if delay is None:
                return
            clock.advance(delay)

    asyncio.run(go())


def ready_draft(client: TestClient) -> str:
    draft = make_draft(client)
    assert save(client, draft).status_code == 303
    return draft


def start(client: TestClient, draft: str) -> int:
    page = client.get(f"/send/{draft}/start")
    assert page.status_code == 200, page.text
    match = STATE.search(page.text)
    assert match
    r = post(client, f"/send/{draft}/start", {"content_hash": match.group(1)})
    assert r.status_code == 303, r.text
    found = re.fullmatch(r"/campaigns/(\d+)", r.headers["location"])
    assert found
    return int(found.group(1))


# --- starting ----------------------------------------------------------------------------------


def test_compose_links_to_start(client: TestClient) -> None:
    draft = make_draft(client)
    assert f'href="/send/{draft}/start"' not in client.get(f"/send/{draft}/compose").text
    save(client, draft)
    assert f'href="/send/{draft}/start"' in client.get(f"/send/{draft}/compose").text


def test_start_page_shows_the_plan(client: TestClient) -> None:
    draft = ready_draft(client)
    page = client.get(f"/send/{draft}/start").text
    assert '<td id="start-count">3</td>' in page
    assert "60 per minute" in page
    assert "250 per rolling 24 hours" in page
    assert 'id="start-estimate">about 1 minute' in page or "under a minute" in page
    assert 'id="start-send"' in page


def test_start_page_needs_saved_content(client: TestClient) -> None:
    draft = make_draft(client)
    page = client.get(f"/send/{draft}/start").text
    assert "Write and save the message first." in page
    assert 'id="start-send"' not in page
    r = post(client, f"/send/{draft}/start", {"content_hash": "0" * 64})
    assert r.status_code == 409
    assert campaigns.active_campaign_id(client.app.state.db) is None  # type: ignore[attr-defined]


def test_start_creates_a_snapshot(client: TestClient) -> None:
    draft = ready_draft(client)
    cid = start(client, draft)
    db = client.app.state.db  # type: ignore[attr-defined]
    c = campaigns.get_campaign(db, cid)
    assert c is not None and c.status == campaigns.QUEUED and c.total == 3
    assert c.content.subject == "Merry Christmas, {{first_name|Friend}}"
    # Editing the draft afterwards doesn't change the campaign.
    save(client, draft, subject="Changed")
    c2 = campaigns.get_campaign(db, cid)
    assert c2 is not None and c2.content == c.content
    page = client.get(f"/send/{draft}/compose").text
    assert f'href="/campaigns/{cid}"' in page


def test_start_refuses_changed_content(client: TestClient) -> None:
    draft = ready_draft(client)
    match = STATE.search(client.get(f"/send/{draft}/start").text)
    assert match
    save(client, draft, subject="Something else")
    r = post(client, f"/send/{draft}/start", {"content_hash": match.group(1)})
    assert r.status_code == 409
    assert "The message changed" in r.text


def test_start_needs_csrf(client: TestClient) -> None:
    draft = ready_draft(client)
    match = STATE.search(client.get(f"/send/{draft}/start").text)
    assert match
    r = client.post(
        f"/send/{draft}/start",
        data={"content_hash": match.group(1)},
        headers={"Origin": BASE},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_only_one_active_campaign(client: TestClient) -> None:
    first = start(client, ready_draft(client))
    other = ready_draft(client)
    page = client.get(f"/send/{other}/start").text
    assert f"Another send (#{first}) hasn&#39;t finished" in page
    assert 'id="start-send"' not in page
    r = post(client, f"/send/{other}/start", {"content_hash": "0" * 64})
    assert r.status_code == 409


def test_gate_allows_mock_only_for_now(app_settings: Settings, valid_env: dict[str, str]) -> None:
    assert gate.check_can_start(app_settings, "a" * 32, "0" * 64) == []
    graph = load_settings({**valid_env, "MAIL_TRANSPORT": "graph", "MS_CLIENT_ID": "x" * 36})
    assert gate.check_can_start(graph, "a" * 32, "0" * 64)


def test_graph_mode_has_no_worker_and_refuses_to_start(
    valid_env: dict[str, str], tmp_path: Path
) -> None:
    settings = load_settings(
        {
            **valid_env,
            "DATA_DIR": str(tmp_path),
            "MAIL_TRANSPORT": "graph",
            "MS_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
        }
    )
    with TestClient(create_app(settings), base_url=BASE) as c:
        assert c.app.state.worker is None  # type: ignore[attr-defined]
        draft = ready_draft(c)
        page = c.get(f"/send/{draft}/start").text
        assert "Sending through Microsoft isn&#39;t available yet" in page
        assert 'id="start-send"' not in page


# --- progress, controls, results ---------------------------------------------------------------


def test_progress_json(client: TestClient, clock: FakeClock) -> None:
    cid = start(client, ready_draft(client))
    data = client.get(f"/campaigns/{cid}/progress").json()
    assert data["status"] == "queued" and data["total"] == 3 and data["done"] == 0
    assert data["counts"] == dict.fromkeys(campaigns.RECIPIENT_STATUSES, 0) | {"pending": 3}
    step_all(client, clock)
    data = client.get(f"/campaigns/{cid}/progress").json()
    assert data["status"] == "finished" and data["final"] is True
    assert data["counts"]["sent"] == 3 and data["done"] == 3 and data["percent"] == 100
    assert data["events"][0]["message"].startswith("Finished: 3 sent")
    assert len(data["events"]) <= campaigns.EVENTS_SHOWN
    assert client.get("/campaigns/999/progress").status_code == 404
    assert client.get("/campaigns/abc/progress").status_code == 404


def test_campaign_pages(client: TestClient, clock: FakeClock) -> None:
    cid = start(client, ready_draft(client))
    page = client.get(f"/campaigns/{cid}").text
    assert 'data-url="/campaigns/1/progress"' in page
    assert f'href="/campaigns/{cid}"' in page  # the no-JS Refresh link
    assert 'id="pause"' in page and 'id="cancel"' in page and 'id="resume"' not in page
    listing = client.get("/campaigns").text
    assert f'href="/campaigns/{cid}"' in listing and "list.csv" in listing
    assert 'href="/campaigns" aria-current="page"' in listing
    assert client.get("/campaigns/42").status_code == 404


def test_controls_need_csrf(client: TestClient) -> None:
    cid = start(client, ready_draft(client))
    for action in ("pause", "resume", "cancel"):
        r = client.post(f"/campaigns/{cid}/{action}", headers={"Origin": BASE})
        assert r.status_code == 403
    assert campaigns.get_campaign(client.app.state.db, cid).status == "queued"  # type: ignore


def test_pause_resume_cancel_buttons(client: TestClient, clock: FakeClock, tmp_path: Path) -> None:
    draft = ready_draft(client)
    cid = start(client, draft)
    db = client.app.state.db  # type: ignore[attr-defined]
    assert post(client, f"/campaigns/{cid}/pause", {}).status_code == 303
    page = client.get(f"/campaigns/{cid}").text
    assert 'id="resume"' in page and 'id="pause"' not in page
    step_all(client, clock)
    assert campaigns.counts(db, cid)["sent"] == 0
    r = post(client, f"/campaigns/{cid}/pause", {})
    assert r.status_code == 409 and "can&#39;t be paused" in r.text
    assert post(client, f"/campaigns/{cid}/resume", {}).status_code == 303
    asyncio.run(client.app.state.worker.step())  # type: ignore[attr-defined]
    assert campaigns.counts(db, cid)["sent"] == 1
    assert post(client, f"/campaigns/{cid}/cancel", {}).status_code == 303
    n = campaigns.counts(db, cid)
    assert n["sent"] == 1 and n["not_sent"] == 2
    d = drafts.get_draft(db, draft)
    assert d is not None and d.csv_filename is None
    assert not list((tmp_path / "uploads").glob("*.csv"))
    page = client.get(f"/campaigns/{cid}").text
    assert 'id="cancel"' not in page and "Cancelled" in page


def test_results_download(client: TestClient, clock: FakeClock) -> None:
    cid = start(client, ready_draft(client))
    step_all(client, clock)
    r = client.get(f"/campaigns/{cid}/results.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert f'filename="send-{cid}-results.csv"' in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text.removeprefix("﻿"))))
    assert rows[0] == campaigns.RESULT_COLUMNS
    assert [row[1] for row in rows[1:]] == ["ada@example.com", "bob@example.com", "eve@example.com"]
    assert {row[3] for row in rows[1:]} == {"sent"}


def test_finish_deletes_the_csv(client: TestClient, clock: FakeClock, tmp_path: Path) -> None:
    draft = ready_draft(client)
    start(client, draft)
    assert list((tmp_path / "uploads").glob("*.csv"))
    step_all(client, clock)
    assert not list((tmp_path / "uploads").glob("*.csv"))
    # The draft's pages now say the CSV is gone; the campaign stays.
    assert client.get(f"/send/{draft}/report").status_code == 410
    assert client.get("/campaigns/1").status_code == 200


# --- the real worker in the app lifespan -------------------------------------------------------


def test_worker_sends_in_the_background(valid_env: dict[str, str], tmp_path: Path) -> None:
    settings = load_settings(
        {**valid_env, "DATA_DIR": str(tmp_path), "SEND_RATE_PER_MINUTE": "6000"}
    )
    with TestClient(create_app(settings), base_url=BASE) as c:
        cid = start(c, ready_draft(c))
        deadline = time.monotonic() + 20
        while c.get(f"/campaigns/{cid}/progress").json()["status"] != "finished":
            assert time.monotonic() < deadline, "the worker didn't finish"
            time.sleep(0.05)
    outbox = sorted((tmp_path / "outbox").glob("*.eml"))
    assert len(outbox) == 3


def test_startup_marks_interrupted_sends_failed(
    app_settings: Settings, clock: FakeClock, tmp_path: Path
) -> None:
    with TestClient(create_app(app_settings, clock=clock, run_worker=False), base_url=BASE) as c:
        cid = start(c, ready_draft(c))
        db = c.app.state.db  # type: ignore[attr-defined]
        with db.connect() as conn:
            conn.execute(
                "UPDATE campaign_recipients SET status = 'sending', attempts = 1 "
                "WHERE campaign_id = ? AND row_no = 2",
                (cid,),
            )
    with TestClient(create_app(app_settings, clock=clock, run_worker=False), base_url=BASE) as c:
        db = c.app.state.db  # type: ignore[attr-defined]
        with db.connect() as conn:
            row = conn.execute(
                "SELECT status, last_error FROM campaign_recipients WHERE row_no = 2"
            ).fetchone()
        assert row["status"] == "failed"
        assert row["last_error"] == campaigns.INTERRUPTED_ERROR
        step_all(c, clock)
        assert campaigns.counts(db, cid) == dict.fromkeys(campaigns.RECIPIENT_STATUSES, 0) | {
            "sent": 2,
            "failed": 1,
        }
    assert len(list((tmp_path / "outbox").glob("*.eml"))) == 2


# --- transport and throttling on the pages -----------------------------------------------------


def test_transport_shown_and_mismatch_refused(
    app_settings: Settings, valid_env: dict[str, str], clock: FakeClock, tmp_path: Path
) -> None:
    with TestClient(create_app(app_settings, clock=clock, run_worker=False), base_url=BASE) as c:
        cid = start(c, ready_draft(c))
        assert post(c, f"/campaigns/{cid}/pause", {}).status_code == 303
        page = c.get(f"/campaigns/{cid}").text
        assert '<strong id="transport">mock</strong>' in page
        assert 'id="transport-mismatch"' not in page
        assert "<td>mock</td>" in c.get("/campaigns").text
    graph = load_settings(
        {
            **valid_env,
            "DATA_DIR": str(tmp_path),
            "MAIL_TRANSPORT": "graph",
            "MS_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
        }
    )
    with TestClient(create_app(graph, clock=clock, run_worker=False), base_url=BASE) as c:
        page = c.get(f"/campaigns/{cid}").text
        assert 'id="transport-mismatch"' in page
        assert "This send was started in mock mode; cancel it and start a new one." in page
        assert 'id="resume"' not in page and 'id="cancel"' in page
        data = c.get(f"/campaigns/{cid}/progress").json()
        assert data["transport"] == "mock" and data["transport_mismatch"]
        r = post(c, f"/campaigns/{cid}/resume", {})
        assert r.status_code == 409 and "started in mock mode" in r.text
        db = c.app.state.db  # type: ignore[attr-defined]
        assert campaigns.get_campaign(db, cid).status == campaigns.PAUSED  # type: ignore[union-attr]
        assert post(c, f"/campaigns/{cid}/cancel", {}).status_code == 303
        assert campaigns.get_campaign(db, cid).status == campaigns.CANCELLED  # type: ignore[union-attr]


def test_progress_shows_retry_after_as_next_send(client: TestClient, clock: FakeClock) -> None:
    worker = client.app.state.worker  # type: ignore[attr-defined]
    worker.transport.decide = lambda r, n: (
        SendResult.retry("429", retry_after=120) if n == 1 and r.startswith("ada") else None
    )
    cid = start(client, ready_draft(client))
    asyncio.run(worker.step())  # ada: throttled
    asyncio.run(worker.step())  # bob: waits for the Retry-After time
    data = client.get(f"/campaigns/{cid}/progress").json()
    zone = campaigns.local_zone(client.app.state.settings.tz)  # type: ignore[attr-defined]
    expected = campaigns.local_time(clock() + timedelta(seconds=120), zone)
    assert data["throttled_until"] == data["next_send_at"] == expected
    page = client.get(f"/campaigns/{cid}").text
    assert "asked to slow down" in page
