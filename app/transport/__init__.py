"""Ways to send a built message: ``mock`` (writes ``.eml`` files) and, from stage 12, ``graph``."""

from app.transport.base import SendResult, Transport
from app.transport.factory import make_transport
from app.transport.mock import MockTransport

__all__ = ["MockTransport", "SendResult", "Transport", "make_transport"]
