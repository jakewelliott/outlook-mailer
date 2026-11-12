"""In-memory per-client rate limit for the public unsubscribe pages.

A token bucket per client: ``capacity`` requests at once, refilled at ``refill_per_second``. At
most ``max_clients`` buckets are kept (least recently seen dropped first), so many addresses
can't grow memory without bound; a dropped client simply starts again with a full bucket. The
clock is injectable (monotonic seconds) so tests are deterministic.

Which client a request comes from (``client_key``):

1. The ``CF-Connecting-IP`` header, if it holds an IP address. Cloudflare sets it on every request
   it forwards and overwrites any value the client sent. Only someone reaching the container's
   port directly (the LAN) could choose it, which is acceptable.
2. Without it, the TCP peer. But with ``--proxy-headers`` uvicorn replaces the peer address in the
   ASGI scope with the leftmost ``X-Forwarded-For`` entry, which the client chooses, and the real
   peer is no longer available. So when an ``X-Forwarded-For`` header is present, the peer can't be
   trusted and all such requests share one bucket (``PROXIED_UNKNOWN``). Without that header
   uvicorn leaves the scope's peer alone and it is the real socket peer. Behind the tunnel that
   peer is cloudflared, so without ``CF-Connecting-IP`` every visitor shares one bucket: Cloudflare
   must pass the header (don't turn on "Remove visitor IP headers"). ``app.unsubscribe_pages``
   logs a warning when it is missing.

``X-Forwarded-For`` itself is never used as a key. IPv6 clients are grouped by /64, since one
host normally holds a whole /64.
"""

import ipaddress
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from starlette.requests import Request

CF_CONNECTING_IP = "cf-connecting-ip"
PROXIED_UNKNOWN = "proxied:unknown"
NO_PEER = "peer:unknown"


def _ip_key(value: str) -> str | None:
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def client_key(request: Request) -> str:
    """The rate-limit key for ``request``; see the module docstring."""
    cf = request.headers.get(CF_CONNECTING_IP)
    if cf is not None:
        key = _ip_key(cf)
        if key is not None:
            return key
    if "x-forwarded-for" in request.headers:
        return PROXIED_UNKNOWN
    peer = request.scope.get("client")
    if not peer:
        return NO_PEER
    host = str(peer[0])
    return _ip_key(host) or f"peer:{host[:64]}"


@dataclass
class _Bucket:
    tokens: float
    at: float


class RateLimiter:
    def __init__(
        self,
        *,
        capacity: int,
        refill_per_second: float,
        max_clients: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or refill_per_second <= 0 or max_clients < 1:
            raise ValueError("capacity, refill rate and max_clients must be positive")
        self.capacity = float(capacity)
        self.refill_per_second = refill_per_second
        self.max_clients = max_clients
        self.clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._buckets)

    def _bucket(self, key: str) -> _Bucket:
        """``key``'s bucket, refilled to now (created full if new). Call with the lock held."""
        now = self.clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(self.capacity, now)
            self._buckets[key] = bucket
            while len(self._buckets) > self.max_clients:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)
            elapsed = max(0.0, now - bucket.at)  # a clock going back refills nothing
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
            bucket.at = now
        return bucket

    def allow(self, key: str) -> bool:
        """Take one request from ``key``'s bucket; False if it is empty."""
        with self._lock:
            bucket = self._bucket(key)
            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return True
            return False

    def has_room(self, key: str) -> bool:
        """Whether ``allow(key)`` would succeed now, without taking anything."""
        with self._lock:
            return self._bucket(key).tokens >= 1

    def retry_after(self, key: str) -> int:
        """Whole seconds until ``key`` may make another request (at least 1).

        The exact wait rounded up: 1 / (1/3 per second) is 3, not 4 (``round`` first removes the
        float error that would push an exact whole number up by one).
        """
        with self._lock:
            bucket = self._buckets.get(key)
            missing = 1.0 if bucket is None else max(0.0, 1.0 - bucket.tokens)
        return max(1, math.ceil(round(missing / self.refill_per_second, 6)))
