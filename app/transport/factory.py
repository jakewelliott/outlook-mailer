"""Pick the transport named by ``MAIL_TRANSPORT``."""

from pathlib import Path

from app.config import Settings
from app.transport.base import Transport
from app.transport.mock import MockTransport

OUTBOX_DIR = "outbox"


def make_transport(settings: Settings) -> Transport:
    if settings.mail_transport == "mock":
        return MockTransport(Path(settings.data_dir) / OUTBOX_DIR)
    if settings.mail_transport == "graph":
        raise NotImplementedError("The Microsoft Graph transport arrives in stage 12.")
    raise ValueError(f"unknown MAIL_TRANSPORT {settings.mail_transport!r}")
