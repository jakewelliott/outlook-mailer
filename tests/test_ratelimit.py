"""The per-client rate limiter of the public pages (fake clock) and how clients are told apart."""

import pytest
from starlette.requests import Request

from app.ratelimit import NO_PEER, PROXIED_UNKNOWN, RateLimiter, client_key


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def limiter(clock: FakeMonotonic, **kw: float) -> RateLimiter:
    args = {"capacity": 3, "refill_per_second": 0.5, "max_clients": 100, **kw}
    return RateLimiter(clock=clock, **args)  # type: ignore[arg-type]


def test_burst_then_refill() -> None:
    clock = FakeMonotonic()
    rl = limiter(clock)
    assert [rl.allow("a") for _ in range(4)] == [True, True, True, False]
    assert rl.retry_after("a") == 2  # one request back after 1 / 0.5 s
    clock.now += 1.5
    assert not rl.allow("a")
    clock.now += 0.5
    assert rl.allow("a") and not rl.allow("a")
    clock.now += 1000  # refills to capacity, no more
    assert [rl.allow("a") for _ in range(4)] == [True, True, True, False]


def test_clients_are_separate() -> None:
    clock = FakeMonotonic()
    rl = limiter(clock)
    for _ in range(3):
        rl.allow("a")
    assert not rl.allow("a")
    assert rl.allow("b")


def test_clock_going_back_refills_nothing() -> None:
    clock = FakeMonotonic()
    rl = limiter(clock)
    for _ in range(3):
        rl.allow("a")
    clock.now -= 500
    assert not rl.allow("a")


def test_memory_is_bounded() -> None:
    clock = FakeMonotonic()
    rl = limiter(clock, max_clients=5)
    for i in range(1000):
        rl.allow(f"10.0.{i // 256}.{i % 256}")
    assert len(rl) == 5


def test_least_recently_seen_is_dropped_first() -> None:
    clock = FakeMonotonic()
    rl = limiter(clock, max_clients=2, capacity=1)
    assert rl.allow("a") and rl.allow("b")
    assert not rl.allow("a")  # a is now the most recent
    assert rl.allow("c")  # drops b
    assert not rl.allow("a")  # a kept its empty bucket
    assert rl.allow("b")  # b starts again


@pytest.mark.parametrize("kw", [{"capacity": 0}, {"refill_per_second": 0}, {"max_clients": 0}])
def test_bad_parameters(kw: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        limiter(FakeMonotonic(), **kw)


def request(
    headers: dict[str, str], client: tuple[str, int] | None = ("192.0.2.9", 5000)
) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/u/x",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": client,
    }
    return Request(scope)


@pytest.mark.parametrize(
    ("headers", "client", "key"),
    [
        ({"CF-Connecting-IP": "203.0.113.7"}, ("192.0.2.9", 5000), "203.0.113.7"),
        # X-Forwarded-For is never the key, whatever it says.
        (
            {"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "1.1.1.1"},
            ("1.1.1.1", 5000),
            "203.0.113.7",
        ),
        ({}, ("192.0.2.9", 5000), "192.0.2.9"),  # no proxy header: the real peer
        # uvicorn --proxy-headers put the (client-chosen) leftmost X-Forwarded-For entry in the
        # scope, so the peer can't be trusted: one shared bucket.
        ({"X-Forwarded-For": "6.6.6.6"}, ("6.6.6.6", 5000), PROXIED_UNKNOWN),
        ({"CF-Connecting-IP": "not-an-ip", "X-Forwarded-For": "6.6.6.6"}, None, PROXIED_UNKNOWN),
        ({"CF-Connecting-IP": "not-an-ip"}, ("192.0.2.9", 5000), "192.0.2.9"),
        ({"CF-Connecting-IP": "2001:db8:1:2:3:4:5:6"}, None, "2001:db8:1:2::/64"),
        ({"CF-Connecting-IP": "::ffff:203.0.113.7"}, None, "203.0.113.7"),
        ({}, None, NO_PEER),
        ({}, ("testclient", 50000), "peer:testclient"),
    ],
)
def test_client_key(headers: dict[str, str], client: tuple[str, int] | None, key: str) -> None:
    assert client_key(request(headers, client)) == key


def test_ipv6_hosts_in_one_64_share_a_bucket() -> None:
    a = client_key(request({"CF-Connecting-IP": "2001:db8:1:2::1"}))
    b = client_key(request({"CF-Connecting-IP": "2001:db8:1:2:ffff::9"}))
    c = client_key(request({"CF-Connecting-IP": "2001:db8:1:3::1"}))
    assert a == b != c


def test_retry_after_is_the_exact_wait_rounded_up() -> None:
    clock = FakeMonotonic()
    rl = limiter(clock, capacity=1, refill_per_second=1 / 3)
    assert rl.allow("a") and not rl.allow("a")
    assert rl.retry_after("a") == 3  # 1 / (1/3) is 3, not 4
    clock.now += 0.5
    rl.allow("a")
    assert rl.retry_after("a") == 3  # 2.5 s left: rounded up
    clock.now += 2.0
    rl.allow("a")
    assert rl.retry_after("a") == 1  # 0.5 s left
    rl60 = limiter(clock, capacity=1, refill_per_second=1 / 60)
    rl60.allow("b")
    assert rl60.retry_after("b") == 60


def test_has_room_takes_nothing() -> None:
    clock = FakeMonotonic()
    rl = limiter(clock, capacity=1)
    assert rl.has_room("a") and rl.has_room("a")
    assert rl.allow("a")
    assert not rl.has_room("a") and not rl.allow("a")
    clock.now += 2
    assert rl.has_room("a") and rl.allow("a")
