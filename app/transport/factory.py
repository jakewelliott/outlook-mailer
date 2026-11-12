"""Pick the transport named by ``MAIL_TRANSPORT``."""

from pathlib import Path

from app.config import Settings
from app.msauth import MicrosoftAuth
from app.transport.base import Transport
from app.transport.graph import GraphTransport
from app.transport.mock import MockTransport

OUTBOX_DIR = "outbox"


def make_transport(settings: Settings, msauth: MicrosoftAuth | None = None) -> Transport:
    """``mock``: the outbox. ``graph``: Microsoft Graph with ``msauth``'s sign-in (required)."""
    if settings.mail_transport == "mock":
        return MockTransport(Path(settings.data_dir) / OUTBOX_DIR)
    if settings.mail_transport == "graph":
        if msauth is None:
            raise ValueError("the graph transport needs the Microsoft sign-in (msauth)")
        return GraphTransport(msauth)
    raise ValueError(f"unknown MAIL_TRANSPORT {settings.mail_transport!r}")
