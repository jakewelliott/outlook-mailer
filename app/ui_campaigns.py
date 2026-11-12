"""Starting a send, its progress page, controls and results download.

- ``/send/{draft}/start``: confirmation (recipients, pacing, estimated time, daily cap). The POST
  re-checks everything (content, size, the test-send gate, no other unfinished send) and creates
  the campaign from a snapshot of the saved content and the eligible recipients.
- ``/campaigns``: every send. ``/campaigns/{id}``: progress, controls and the latest events.
  ``app.js`` polls ``/campaigns/{id}/progress`` (JSON) every few seconds and reloads the page when
  the state changes; without JavaScript the page has a Refresh link. No websockets.
- ``/campaigns/{id}/pause``, ``resume``, ``cancel``: POST with the CSRF token.
- ``/campaigns/{id}/results.csv``: one row per recipient.
"""

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app import campaigns, compose, gate, sender
from app.campaigns import Campaign, CampaignError
from app.compose import DraftContent
from app.config import Settings
from app.db import Database
from app.drafts import Draft
from app.recipients import ValidationReport
from app.security import ADMIN_DEPENDENCIES
from app.ui import templates
from app.ui_send import ACTIVE as SEND_ACTIVE
from app.ui_send import load_report

ACTIVE = "/campaigns"
POLL_SECONDS = 3

router = APIRouter(dependencies=ADMIN_DEPENDENCIES, default_response_class=HTMLResponse)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _db(request: Request) -> Database:
    return request.app.state.db


def _now(request: Request) -> datetime:
    return request.app.state.clock()


def _zone(request: Request) -> Any:
    return campaigns.local_zone(_settings(request).tz)


def _wake(request: Request) -> None:
    worker = request.app.state.worker
    if worker is not None:
        worker.wake()


def _message(request: Request, title: str, text: str, status_code: int) -> Response:
    return templates.TemplateResponse(
        request,
        "ui/message.html.j2",
        {"active": ACTIVE, "title": title, "text": text},
        status_code=status_code,
    )


# --- starting ----------------------------------------------------------------------------------


def duration_text(seconds: float) -> str:
    if seconds < 60:
        return "under a minute"
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours, rest = divmod(minutes, 60)
    text = f"about {hours} hour{'s' if hours != 1 else ''}"
    return text + (f" {rest} minute{'s' if rest != 1 else ''}" if rest else "")


def estimate(n: int, rate_per_minute: int, cap: int, used: int) -> str:
    """How long ``n`` messages take at the pacing and the rolling daily cap (``used`` = already
    counted in the last 24 h)."""
    spacing = 60.0 / rate_per_minute
    room = max(0, cap - used)
    if n <= room:
        return duration_text(max(0, n - 1) * spacing)
    days = math.ceil((n - room) / cap)
    return (
        f"more than a day: at most {cap} messages go out per 24 hours, so the send pauses by "
        f"itself and carries on about {days} day{'s' if days != 1 else ''} later"
        + (" (more than once)" if days > 1 else "")
    )


@dataclass
class StartPlan:
    draft: Draft
    report: ValidationReport
    content: DraftContent | None
    content_hash: str
    errors: list[str]
    used_today: int
    estimate: str
    test: gate.GateStatus | None = None


def _plan(request: Request, draft_id: str) -> StartPlan | Response:
    result = load_report(request, draft_id)
    if isinstance(result, Response):
        return result
    draft, report = result
    s, db = _settings(request), _db(request)
    errors: list[str] = []
    c = compose.get_content(db, draft.id)
    digest = ""
    test = None
    if c is None:
        errors.append("Write and save the message first.")
    elif compose.validate(c):
        errors.append("The saved message has problems. Fix them on the compose page.")
    else:
        digest = compose.content_hash(c)
        size = compose.size_check(
            c,
            report.eligible,
            public_base_url=s.public_base_url,
            unsubscribe_secret=s.unsubscribe_secret,
        )
        if size.too_large:
            errors.append(size.message() or "The email is too large.")
        account = request.app.state.msauth.sender()
        test = gate.status(db, s, account, draft.id, digest, _zone(request))
        errors.extend(gate.check_can_start(s, db, draft.id, digest, sender=account))
    if not report.eligible:
        errors.append("Nobody on the list can be emailed.")
    existing = campaigns.campaign_for_draft(db, draft.id)
    if existing is not None:
        errors.append(f"This list has already been used for send #{existing}.")
    active = campaigns.active_campaign_id(db)
    if active is not None and active != existing:
        errors.append(f"Another send (#{active}) hasn't finished. Finish or cancel it first.")
    if request.app.state.worker is None:
        errors.append("The send worker isn't running (see the log for why).")
    used = len(sender.sent_in_window(db, _now(request)))
    return StartPlan(
        draft,
        report,
        c,
        digest,
        list(dict.fromkeys(errors)),
        used,
        estimate(len(report.eligible), s.send_rate_per_minute, s.daily_send_cap, used),
        test,
    )


def _start_page(request: Request, plan: StartPlan, status_code: int = 200) -> Response:
    s = _settings(request)
    return templates.TemplateResponse(
        request,
        "ui/send_start.html.j2",
        {
            "active": SEND_ACTIVE,
            "plan": plan,
            "draft_id": plan.draft.id,
            "count": len(plan.report.eligible),
            "rate": s.send_rate_per_minute,
            "spacing": 60 / s.send_rate_per_minute,
            "cap": s.daily_send_cap,
            "transport": s.mail_transport,
            "existing": campaigns.campaign_for_draft(_db(request), plan.draft.id),
            "active_id": campaigns.active_campaign_id(_db(request)),
        },
        status_code=status_code,
    )


@router.get("/send/{draft_id}/start")
def start_form(request: Request, draft_id: str) -> Response:
    plan = _plan(request, draft_id)
    if isinstance(plan, Response):
        return plan
    return _start_page(request, plan)


@router.post("/send/{draft_id}/start")
async def start(request: Request, draft_id: str) -> Response:
    form = await request.form()
    plan = await run_in_threadpool(_plan, request, draft_id)
    if isinstance(plan, Response):
        return plan
    if plan.errors or plan.content is None:
        return await run_in_threadpool(_start_page, request, plan, 409)
    confirmed = form.get("content_hash")
    if confirmed != plan.content_hash:
        plan.errors.append(
            "The message changed after this page was opened. Check the details and confirm again."
        )
        return await run_in_threadpool(_start_page, request, plan, 409)
    # Graph: the address the passing test went to; the worker stops if another is connected.
    tested = plan.test.test if plan.test is not None and plan.test.ok else None
    graph = _settings(request).mail_transport == "graph"
    try:
        campaign_id = await run_in_threadpool(
            campaigns.create_campaign,
            _db(request),
            plan.draft.id,
            plan.content,
            plan.report.eligible,
            _now(request),
            transport=_settings(request).mail_transport,
            sender_address=tested.to_addr if graph and tested is not None else None,
        )
    except CampaignError as exc:
        plan.errors.append(str(exc))
        return await run_in_threadpool(_start_page, request, plan, 409)
    _wake(request)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


# --- progress ----------------------------------------------------------------------------------


def _campaign_id(value: str) -> int | None:
    # ASCII digits only; a sane length.
    return int(value) if re.fullmatch(r"[1-9][0-9]{0,11}", value, re.ASCII) else None


def _load(request: Request, value: str) -> Campaign | None:
    campaign_id = _campaign_id(value)
    return None if campaign_id is None else campaigns.get_campaign(_db(request), campaign_id)


def _not_found(request: Request) -> Response:
    return _message(request, "Send not found", "That send doesn't exist.", 404)


def progress_data(request: Request, c: Campaign) -> dict[str, Any]:
    db, zone, now = _db(request), _zone(request), _now(request)
    n = campaigns.counts(db, c.id)
    done = c.total - n[campaigns.R_PENDING] - n[campaigns.R_SENDING]
    next_send = None
    if c.status == campaigns.SENDING and c.next_send_at and c.next_send_at > now:
        next_send = campaigns.local_time(c.next_send_at, zone)
    throttled_until = None
    if c.unfinished:
        with db.connect() as conn:
            throttle = campaigns.throttle_until(conn)
        if throttle is not None and throttle[1] > now:
            throttled_until = campaigns.local_time(throttle[1], zone)
    return {
        "id": c.id,
        "status": c.status,
        "status_label": c.status_label,
        "final": c.status in campaigns.FINAL,
        "total": c.total,
        "done": done,
        "percent": math.floor(100 * done / c.total) if c.total else 100,
        "counts": n,
        "next_send_at": next_send,
        "throttled_until": throttled_until,
        "transport": c.transport,
        "transport_mismatch": (
            campaigns.transport_mismatch(c, _settings(request).mail_transport)
            if c.unfinished
            else None
        ),
        "resume_at": (
            campaigns.local_time(c.resume_at, zone)
            if c.status == campaigns.PAUSED_DAILY_CAP
            else None
        ),
        "last_error": c.last_error,
        "started_at": campaigns.local_time(c.started_at, zone) or None,
        "finished_at": campaigns.local_time(c.finished_at, zone) or None,
        "events": [
            {"at": campaigns.local_time(e.at, zone), "message": e.message}
            for e in campaigns.events(db, c.id)
        ],
    }


def _progress_page(
    request: Request, c: Campaign, error: str | None = None, status_code: int = 200
) -> Response:
    db = _db(request)
    mismatch = campaigns.transport_mismatch(c, _settings(request).mail_transport)
    return templates.TemplateResponse(
        request,
        "ui/campaign.html.j2",
        {
            "active": ACTIVE,
            "c": c,
            "p": progress_data(request, c),
            "labels": campaigns.RECIPIENT_LABELS,
            "statuses": campaigns.RECIPIENT_STATUSES,
            "errors": campaigns.recipient_errors(db, c.id),
            "draft_name": _draft_name(db, c.draft_id),
            "can_pause": c.status in campaigns.CAN_PAUSE,
            "can_resume": c.status in campaigns.CAN_RESUME and mismatch is None,
            "can_cancel": c.status in campaigns.CAN_CANCEL,
            "poll_seconds": POLL_SECONDS,
            "current_transport": _settings(request).mail_transport,
            "error": error,
        },
        status_code=status_code,
    )


def _draft_name(db: Database, draft_id: str) -> str:
    with db.connect() as conn:
        row = conn.execute("SELECT original_name FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return "" if row is None else row["original_name"]


@router.get("/campaigns")
def campaign_list(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "ui/campaigns.html.j2",
        {
            "active": ACTIVE,
            "items": campaigns.list_campaigns(_db(request)),
            "zone": _zone(request),
            "local_time": campaigns.local_time,
        },
    )


@router.get("/campaigns/{campaign_id}")
def campaign_page(request: Request, campaign_id: str) -> Response:
    c = _load(request, campaign_id)
    if c is None:
        return _not_found(request)
    return _progress_page(request, c)


@router.get("/campaigns/{campaign_id}/progress")
def campaign_progress(request: Request, campaign_id: str) -> Response:
    c = _load(request, campaign_id)
    if c is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(progress_data(request, c))


def _control(request: Request, campaign_id: str, action: str) -> Response:
    c = _load(request, campaign_id)
    if c is None:
        return _not_found(request)
    db, now = _db(request), _now(request)
    try:
        if action == "pause":
            campaigns.pause(db, c.id, now)
        elif action == "resume":
            s = _settings(request)
            problem, address = gate.resume_problem(db, s, c, request.app.state.msauth.sender())
            if problem is not None:
                raise CampaignError(problem)
            campaigns.resume(db, c.id, now, transport=s.mail_transport, sender_address=address)
        else:
            campaigns.cancel(db, _settings(request).data_dir, c.id, now)
    except CampaignError as exc:
        return _progress_page(request, campaigns.get_campaign(db, c.id) or c, str(exc), 409)
    _wake(request)
    return RedirectResponse(f"/campaigns/{c.id}", status_code=303)


@router.post("/campaigns/{campaign_id}/pause")
def campaign_pause(request: Request, campaign_id: str) -> Response:
    return _control(request, campaign_id, "pause")


@router.post("/campaigns/{campaign_id}/resume")
def campaign_resume(request: Request, campaign_id: str) -> Response:
    return _control(request, campaign_id, "resume")


@router.post("/campaigns/{campaign_id}/cancel")
def campaign_cancel(request: Request, campaign_id: str) -> Response:
    return _control(request, campaign_id, "cancel")


@router.get("/campaigns/{campaign_id}/results.csv")
def results_download(request: Request, campaign_id: str) -> Response:
    c = _load(request, campaign_id)
    if c is None:
        return _not_found(request)
    # The BOM makes Excel read the file as UTF-8.
    return Response(
        "﻿" + campaigns.results_csv(_db(request), c.id),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="send-{c.id}-results.csv"'},
    )
