"""Admin **Unsubscribes** page: the opt-outs in ``unsubscribed.txt`` and a download of the file.

Behind the Access guard like every admin route (``ADMIN_DEPENDENCIES``). The list is newest first;
a missing file is shown as an empty list and downloads as an empty file. The count is of different
addresses (``unsubscribes.count_addresses``: the same matching as the send-time check); when the
file lists one more than once (by hand, or another capitalisation), the page says how many entries
it has, and the table shows every entry.
"""

from datetime import datetime, tzinfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from app import campaigns
from app.security import ADMIN_DEPENDENCIES
from app.ui import templates
from app.unsubscribes import FILENAME, Entry, count_addresses, load_entries, read_raw

router = APIRouter(dependencies=ADMIN_DEPENDENCIES, default_response_class=HTMLResponse)


def _when(entry: Entry, zone: tzinfo) -> str:
    """The recorded time in the app's time zone; as written if it isn't an ISO-8601 time."""
    try:
        at = datetime.fromisoformat(entry.recorded)
    except ValueError:
        return entry.recorded
    if at.tzinfo is None:
        return entry.recorded
    return campaigns.local_time(at, zone)


@router.get("/unsubscribes")
def unsubscribes_page(request: Request) -> HTMLResponse:
    settings = request.app.state.settings
    zone = campaigns.local_zone(settings.tz)
    entries = load_entries(settings.data_dir)
    rows = [{"email": e.email, "when": _when(e, zone)} for e in reversed(entries)]
    return templates.TemplateResponse(
        request,
        "ui/unsubscribes.html.j2",
        {
            "active": "/unsubscribes",
            "rows": rows,
            "people": count_addresses(entries),
            "filename": FILENAME,
        },
    )


@router.get("/unsubscribes/" + FILENAME)
def unsubscribes_download(request: Request) -> Response:
    return Response(
        read_raw(request.app.state.settings.data_dir),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{FILENAME}"'},
    )
