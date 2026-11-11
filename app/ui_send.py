"""New send, steps 1 to 3: upload the CSV, map the columns, see the validation report.

Every step works without JavaScript. Choosing a different status column needs a round trip to
list its values: the "Show values" button (pressed automatically by ``app.js`` when the status
dropdown changes) re-renders the mapping page without saving.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile

from app import drafts
from app.config import Settings
from app.csv_ingest import (
    MB,
    CsvError,
    ParsedCsv,
    guess_columns,
    parse_csv,
    too_large_message,
)
from app.db import Database
from app.drafts import Draft
from app.recipients import (
    CATEGORIES,
    CATEGORY_LABELS,
    MappingError,
    StatusValue,
    ValidationReport,
    default_selection,
    issues_csv,
    status_values,
    validate_mapping,
    validate_rows,
)
from app.security import ADMIN_DEPENDENCIES
from app.ui import templates
from app.unsubscribes import load_unsubscribed

ACTIVE = "/send/new"
ISSUES_SHOWN = 20
# More distinct values than this means the column is not a status column (e.g. names).
MAX_STATUS_VALUES = 50
UPLOAD_FIELD = "csv_file"
CSV_EXTENSIONS = (".csv", ".txt", ".tsv")
# Windows browsers often label .csv files as Excel.
CSV_CONTENT_TYPES = {
    "text/csv",
    "text/plain",
    "text/tab-separated-values",
    "application/csv",
    "application/vnd.ms-excel",
}
# Multipart framing and the CSRF field on top of the file itself.
FORM_OVERHEAD_BYTES = 64 * 1024


class UploadTooLarge(Exception):
    pass


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _db(request: Request) -> Database:
    return request.app.state.db


def _limit_bytes(request: Request) -> int:
    return _settings(request).max_upload_mb * MB


async def limit_upload_size(request: Request) -> None:
    """Refuse an oversized upload from its Content-Length, before the body is parsed."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > _limit_bytes(request) + FORM_OVERHEAD_BYTES:
        raise UploadTooLarge


# The size check must run before ``csrf_protect`` parses the multipart body.
upload_router = APIRouter(
    dependencies=[Depends(limit_upload_size), *ADMIN_DEPENDENCIES],
    default_response_class=HTMLResponse,
)
router = APIRouter(dependencies=ADMIN_DEPENDENCIES, default_response_class=HTMLResponse)


def _page(request: Request, name: str, status_code: int = 200, **context: object) -> Response:
    return templates.TemplateResponse(
        request, name, {"active": ACTIVE, **context}, status_code=status_code
    )


def _upload_page(request: Request, error: str | None = None, status_code: int = 200) -> Response:
    return _page(
        request,
        "ui/send_upload.html.j2",
        status_code,
        error=error,
        settings=_settings(request),
        field=UPLOAD_FIELD,
    )


def upload_too_large(request: Request, exc: Exception) -> Response:
    return _upload_page(request, too_large_message(_settings(request).max_upload_mb), 413)


def _message(request: Request, title: str, text: str, status_code: int) -> Response:
    return _page(request, "ui/message.html.j2", status_code, title=title, text=text)


def _not_found(request: Request) -> Response:
    return _message(
        request, "Upload not found", "That upload doesn't exist. Start a new send.", 404
    )


def _parse(request: Request, data: bytes) -> ParsedCsv:
    s = _settings(request)
    return parse_csv(data, max_upload_mb=s.max_upload_mb, max_recipients=s.max_recipients)


def _load(request: Request, draft_id: str) -> tuple[Draft, ParsedCsv] | Response:
    """The draft and its parsed CSV, or an error page."""
    draft = drafts.get_draft(_db(request), draft_id)
    if draft is None:
        return _not_found(request)
    data = drafts.read_csv(_settings(request).data_dir, draft)
    if data is None:
        return _message(
            request, "CSV deleted", "This upload's CSV file has been deleted. Upload it again.", 410
        )
    try:
        return draft, _parse(request, data)
    except CsvError as exc:  # e.g. the limits were lowered since the upload
        return _message(request, "CSV can't be used", str(exc), 422)


# --- Step 1: upload ----------------------------------------------------------------------------


@router.get("/send/new")
def upload_form(request: Request) -> Response:
    return _upload_page(request)


def _looks_like_csv(upload: UploadFile) -> bool:
    name = (upload.filename or "").lower()
    content_type = (upload.content_type or "").split(";")[0].strip().lower()
    return name.endswith(CSV_EXTENSIONS) or content_type in CSV_CONTENT_TYPES


@upload_router.post("/send/new")
async def upload(request: Request) -> Response:
    upload = (await request.form()).get(UPLOAD_FIELD)
    if not isinstance(upload, UploadFile) or not upload.filename:
        return _upload_page(request, "Choose a CSV file to upload.", 400)
    if not _looks_like_csv(upload):
        return _upload_page(
            request, "That doesn't look like a CSV file. In Excel use Save As, then CSV.", 400
        )
    data = await upload.read(_limit_bytes(request) + 1)
    try:
        await run_in_threadpool(_parse, request, data)
    except CsvError as exc:
        return _upload_page(request, str(exc), 400)
    draft = await run_in_threadpool(
        drafts.create_draft, _db(request), _settings(request).data_dir, upload.filename, data
    )
    return RedirectResponse(f"/send/{draft.id}/mapping", status_code=303)


# --- Step 2: mapping ---------------------------------------------------------------------------


def _too_many_values(column: str, count: int) -> str:
    return (
        f'"{column}" has {count} different values, so it doesn\'t look like a subscription '
        "status column. Choose another column, or none."
    )


def _mapping_page(
    request: Request,
    draft: Draft,
    parsed: ParsedCsv,
    chosen: dict[str, str | None],
    selected: list[str] | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    values: list[StatusValue] = []
    errors = list(errors or [])
    status = chosen["status"]
    if status in parsed.headers:
        values = status_values(parsed, status)
        if len(values) > MAX_STATUS_VALUES:
            errors.append(_too_many_values(status, len(values)))
            values = []
        if selected is None:
            saved = draft.mapping
            if saved is not None and saved.status == status and draft.status_values is not None:
                selected = draft.status_values
            else:
                selected = default_selection(values)
    else:
        status = None
    return _page(
        request,
        "ui/send_mapping.html.j2",
        status_code,
        draft=draft,
        parsed=parsed,
        chosen={**chosen, "status": status},
        values=values,
        selected=set(selected or []),
        errors=errors,
    )


@router.get("/send/{draft_id}/mapping")
def mapping_form(request: Request, draft_id: str) -> Response:
    loaded = _load(request, draft_id)
    if isinstance(loaded, Response):
        return loaded
    draft, parsed = loaded
    chosen = draft.mapping.to_dict() if draft.mapping else guess_columns(parsed.headers)
    return _mapping_page(request, draft, parsed, chosen)


def _field(form: FormData, name: str) -> str | None:
    value = form.get(name)
    return value.strip() or None if isinstance(value, str) else None


@router.post("/send/{draft_id}/mapping")
async def mapping_save(request: Request, draft_id: str) -> Response:
    form = await request.form()
    loaded = await run_in_threadpool(_load, request, draft_id)
    if isinstance(loaded, Response):
        return loaded
    draft, parsed = loaded
    chosen = {
        "email": _field(form, "email"),
        "first_name": _field(form, "first_name"),
        "status": _field(form, "status"),
    }
    # The status checkboxes on the submitted page belong to the column in "status_shown". If the
    # dropdown now names another column (or "Show values" was pressed), list its values first.
    if _field(form, "action") == "refresh" or chosen["status"] != _field(form, "status_shown"):
        return _mapping_page(request, draft, parsed, chosen)

    picked = [v for v in form.getlist("status_value") if isinstance(v, str)]
    try:
        mapping = validate_mapping(parsed.headers, **chosen)
    except MappingError as exc:
        return _mapping_page(request, draft, parsed, chosen, picked, exc.errors, 400)

    selection: list[str] | None = None
    if mapping.status is not None:
        keys = [v.key for v in status_values(parsed, mapping.status)]
        if len(keys) > MAX_STATUS_VALUES:  # _mapping_page adds the error message
            return _mapping_page(request, draft, parsed, chosen, picked, None, 400)
        selection = [k for k in keys if k in set(picked)]
        if not selection:
            error = "Tick at least one status value, or set the status column to none."
            return _mapping_page(request, draft, parsed, chosen, picked, [error], 400)
    await run_in_threadpool(drafts.save_mapping, _db(request), draft.id, mapping, selection)
    return RedirectResponse(f"/send/{draft.id}/report", status_code=303)


# --- Step 3: validation report -----------------------------------------------------------------


def _report(request: Request, draft_id: str) -> tuple[Draft, ValidationReport] | Response:
    loaded = _load(request, draft_id)
    if isinstance(loaded, Response):
        return loaded
    draft, parsed = loaded
    if draft.mapping is None:
        return RedirectResponse(f"/send/{draft.id}/mapping", status_code=303)
    try:
        mapping = validate_mapping(parsed.headers, **draft.mapping.to_dict())
    except MappingError:
        return RedirectResponse(f"/send/{draft.id}/mapping", status_code=303)
    unsubscribed = load_unsubscribed(_settings(request).data_dir)
    return draft, validate_rows(parsed, mapping, draft.status_values, unsubscribed)


@router.get("/send/{draft_id}/report")
def report_page(request: Request, draft_id: str) -> Response:
    result = _report(request, draft_id)
    if isinstance(result, Response):
        return result
    draft, report = result
    return _page(
        request,
        "ui/send_report.html.j2",
        draft=draft,
        report=report,
        categories=CATEGORIES,
        labels=CATEGORY_LABELS,
        issues=report.issues[:ISSUES_SHOWN],
        sample=report.eligible[:10],
    )


@router.get("/send/{draft_id}/issues.csv")
def issues_download(request: Request, draft_id: str) -> Response:
    result = _report(request, draft_id)
    if isinstance(result, Response):
        return result
    draft, report = result
    stem = Path(draft.original_name).stem
    # The BOM makes Excel read the file as UTF-8. The filename is ASCII-only for the header.
    safe_stem = "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "_" for c in stem)
    return Response(
        "\ufeff" + issues_csv(report.issues),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_stem[:60]}-issues.csv"'},
    )


@router.get("/send/{draft_id}/compose")
def compose_placeholder(request: Request, draft_id: str) -> Response:
    if drafts.get_draft(_db(request), draft_id) is None:
        return _not_found(request)
    return _page(request, "ui/placeholder.html.j2", title="Compose")
