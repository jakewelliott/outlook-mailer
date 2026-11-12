"""New send, step 4: write the message, fill in the footer and preview the email.

The compose page works without JavaScript: Save validates everything and either shows the form
again with each error next to its field (keeping what was typed) or saves and redirects back. The
preview always shows the last *saved* content, for one recipient from the validation report (or
a sample with no first name, to see the fallbacks), in light or dark.

The rendered email is served by ``/send/{id}/preview.html`` with its own narrow CSP (see
``security.use_email_preview_headers``) and shown in an ``<iframe sandbox>``; the subject line and
plain-text version come from ``/send/{id}/preview.txt`` in a second frame. ``app.js`` swaps the
frames' addresses when the recipient or colour scheme changes, so unsaved edits stay on the page.
"""

import re
from functools import partial
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app import campaigns, compose, merge, render
from app.compose import DraftContent
from app.config import Settings
from app.db import Database
from app.recipients import Recipient, ValidationReport
from app.security import ADMIN_DEPENDENCIES, use_email_preview_headers
from app.ui import templates
from app.ui_send import ACTIVE, load_report

PREVIEW_RECIPIENTS = 50
SAMPLE = "sample"
SAMPLE_VALUES = {"first_name": "", "email": "friend@example.com"}
Scheme = Literal["light", "dark"]

router = APIRouter(dependencies=ADMIN_DEPENDENCIES, default_response_class=HTMLResponse)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _db(request: Request) -> Database:
    return request.app.state.db


def _choices(report: ValidationReport) -> list[Recipient]:
    return report.eligible[:PREVIEW_RECIPIENTS]


def _pick(report: ValidationReport, r: str | None) -> str | None:
    """The recipient choice for ``r``: ``"0"``…``"49"`` or ``"sample"``. A missing ``r`` means the
    first recipient (or the sample if there is none); an invalid one gives None."""
    if not r:
        return "0" if report.eligible else SAMPLE
    if r == SAMPLE:
        return SAMPLE
    # ASCII digits only: str.isdigit() accepts "²" and other digits int() rejects or reads.
    if re.fullmatch(r"\d{1,3}", r, re.ASCII) and int(r) < len(_choices(report)):
        return str(int(r))
    return None


def _scheme(value: str | None) -> Scheme | None:
    if value in (None, "", "light"):
        return "light"
    return "dark" if value == "dark" else None


def _values(report: ValidationReport, choice: str) -> dict[str, str]:
    if choice == SAMPLE:
        return dict(SAMPLE_VALUES)
    return merge.recipient_values(_choices(report)[int(choice)])


def _render(
    request: Request, c: DraftContent, values: dict[str, str], dark: bool = False
) -> compose.RenderedEmail:
    s = _settings(request)
    return compose.render_message(
        c,
        values,
        public_base_url=s.public_base_url,
        unsubscribe_secret=s.unsubscribe_secret,
        force_dark=dark,
    )


def _size(request: Request, c: DraftContent, report: ValidationReport) -> compose.SizeCheck:
    s = _settings(request)
    return compose.size_check(
        c,
        report.eligible,
        public_base_url=s.public_base_url,
        unsubscribe_secret=s.unsubscribe_secret,
    )


def _page(
    request: Request,
    draft_id: str,
    report: ValidationReport,
    form: DraftContent,
    *,
    saved: DraftContent | None,
    errors: compose.Errors,
    choice: str,
    scheme: Scheme,
    just_saved: bool = False,
    status_code: int = 200,
) -> Response:
    preview_errors: compose.Errors = compose.validate(saved) if saved is not None else {}
    size = None
    if saved is not None and not preview_errors:
        size = _size(request, saved, report)
    query = urlencode({"r": choice, "scheme": scheme})
    return templates.TemplateResponse(
        request,
        "ui/send_compose.html.j2",
        {
            "active": ACTIVE,
            "draft_id": draft_id,
            "report": report,
            "c": form,
            "errors": errors,
            "error_count": sum(len(v) for v in errors.values()),
            "saved": saved,
            "preview_errors": preview_errors,
            "size": size,
            "size_limit_kb": compose.SIZE_LIMIT_BYTES // compose.KB,
            "limits": compose.FORM_LIMITS,
            "choices": _choices(report),
            "choice": choice,
            "scheme": scheme,
            "sample": SAMPLE,
            "sample_email": SAMPLE_VALUES["email"],
            "query": query,
            "just_saved": just_saved,
            "campaign_id": campaigns.campaign_for_draft(_db(request), draft_id),
            "default_reason": render.DEFAULT_REASON.format(
                name=form.business_name.strip() or "your business"
            ),
        },
        status_code=status_code,
    )


@router.get("/send/{draft_id}/compose")
def compose_form(
    request: Request,
    draft_id: str,
    r: str | None = None,
    scheme: str | None = None,
    saved: str | None = None,
) -> Response:
    result = load_report(request, draft_id)
    if isinstance(result, Response):
        return result
    draft, report = result
    stored = compose.get_content(_db(request), draft.id)
    form = stored if stored is not None else compose.defaults(_db(request))
    return _page(
        request,
        draft.id,
        report,
        form,
        saved=stored,
        errors={},
        choice=_pick(report, r) or _pick(report, None) or SAMPLE,
        scheme=_scheme(scheme) or "light",
        just_saved=saved == "1" and stored is not None,
    )


@router.post("/send/{draft_id}/compose")
async def compose_save(request: Request, draft_id: str) -> Response:
    form = await request.form()
    result = await run_in_threadpool(load_report, request, draft_id)
    if isinstance(result, Response):
        return result
    draft, report = result
    c = DraftContent.from_form(form)
    raw_r, raw_scheme = form.get("r"), form.get("scheme")
    choice = _pick(report, raw_r if isinstance(raw_r, str) else None) or _pick(report, None)
    scheme = _scheme(raw_scheme if isinstance(raw_scheme, str) else None) or "light"
    errors = compose.validate(c)
    if not errors:
        size = await run_in_threadpool(_size, request, c, report)
        if size.too_large:
            errors["body"] = [size.message() or ""]
    if errors:
        stored = await run_in_threadpool(compose.get_content, _db(request), draft.id)
        return await run_in_threadpool(  # renders the saved version for the size line
            partial(
                _page,
                request,
                draft.id,
                report,
                c,
                saved=stored,
                errors=errors,
                choice=choice or SAMPLE,
                scheme=scheme,
                status_code=400,
            )
        )
    await run_in_threadpool(compose.save_content, _db(request), draft.id, c)
    query = urlencode({"r": choice, "scheme": scheme, "saved": "1"})
    return RedirectResponse(f"/send/{draft.id}/compose?{query}#preview", status_code=303)


def _preview_source(
    request: Request, draft_id: str, r: str | None
) -> tuple[DraftContent, dict[str, str]] | Response:
    result = load_report(request, draft_id)
    if not isinstance(result, tuple):
        return PlainTextResponse("No such draft.", status_code=404)
    draft, report = result
    choice = _pick(report, r)
    if choice is None:
        return PlainTextResponse("No such recipient.", status_code=404)
    c = compose.get_content(_db(request), draft.id)
    if c is None or compose.validate(c):
        return PlainTextResponse("Save the message to see the preview.", status_code=404)
    return c, _values(report, choice)


@router.get("/send/{draft_id}/preview.html")
def preview_html(
    request: Request, draft_id: str, r: str | None = None, scheme: str | None = None
) -> Response:
    use_email_preview_headers(request)
    chosen = _scheme(scheme)
    if chosen is None:
        return PlainTextResponse("Unknown colour scheme.", status_code=404)
    source = _preview_source(request, draft_id, r)
    if isinstance(source, Response):
        return source
    c, values = source
    return HTMLResponse(_render(request, c, values, dark=chosen == "dark").html)


@router.get("/send/{draft_id}/preview.txt")
def preview_text(request: Request, draft_id: str, r: str | None = None) -> Response:
    use_email_preview_headers(request)
    source = _preview_source(request, draft_id, r)
    if isinstance(source, Response):
        return source
    c, values = source
    out = _render(request, c, values)
    preheader = merge.render(c.preheader, values, mode="subject")
    head = f"Subject: {out.subject}\n" + (f"Preview text: {preheader}\n" if preheader else "")
    return PlainTextResponse(f"{head}\n{out.text}")
