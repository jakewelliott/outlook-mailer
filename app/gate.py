"""The test-send gate: may a bulk send of this content start?

``check_can_start`` is called by the start page and again when the campaign is created. It returns
the reasons sending can't start yet (empty list = allowed).

Stage 10 (this file as it is now): with ``MAIL_TRANSPORT=mock`` there is no gate, since nothing
is really sent. With ``graph`` sending is refused, because the Microsoft transport doesn't exist
yet. **Stage 12 wires the real gate**: require a successful test send of ``content_hash`` (the
current content, see ``compose.content_hash``) to the signed-in address, for both transports or
at least for ``graph``.
"""

from app.config import Settings


def check_can_start(settings: Settings, draft_id: str, content_hash: str) -> list[str]:
    """Reasons a bulk send of ``content_hash`` from ``draft_id`` can't start; empty if it can."""
    if settings.mail_transport == "mock":
        return []
    return [
        "Sending through Microsoft isn't available yet. Until then only MAIL_TRANSPORT=mock "
        "(which writes .eml files) can run a send."
    ]
