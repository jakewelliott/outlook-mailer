"""Ways to send a built message: ``mock`` (writes ``.eml`` files) and ``graph`` (Microsoft)."""

from app.transport.base import SendResult, Transport
from app.transport.factory import make_transport
from app.transport.graph import GraphTransport
from app.transport.mock import MockTransport

__all__ = ["GraphTransport", "MockTransport", "SendResult", "Transport", "make_transport"]
