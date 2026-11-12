import fcntl
import os
import stat
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import unsubscribes
from app.unsubscribes import (
    Entry,
    UnsubscribeWatcher,
    address_keys,
    count_addresses,
    load_entries,
    load_unsubscribed,
    parse_unsubscribed,
    read_raw,
    record_unsubscribe,
)

AT = datetime(2026, 12, 1, 10, 0, 5, tzinfo=timezone(timedelta(hours=-5)))


def test_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_unsubscribed(tmp_path) == frozenset()


def test_parse_lines(tmp_path: Path) -> None:
    (tmp_path / "unsubscribed.txt").write_text(
        "Ann@Example.com\t2026-12-01T10:00:00-05:00\n"
        "\n"
        "garbage line without tab or at sign\n"
        "not-an-email\t2026-12-01T10:00:00-05:00\n"
        "\t2026-12-01T10:00:00-05:00\n"
        "bo@example.com\tnot a date\n"
        "  cy@example.com  \t2026-12-01T10:00:00-05:00\r\n"
        "dee@example.com\n",
        encoding="utf-8",
    )
    assert load_unsubscribed(tmp_path) == {
        "ann@example.com",
        "bo@example.com",
        "cy@example.com",
        "dee@example.com",  # an address on its own still counts: fail safe
    }


def test_invalid_utf8_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "unsubscribed.txt").write_bytes(b"a@example.com\t2026\n\xff\xfe\n")
    assert load_unsubscribed(tmp_path) == {"a@example.com"}


def test_spaces_inside_address_ignored() -> None:
    assert parse_unsubscribed("with space@example.com\tx\n") == frozenset()


def test_idn_entries_match_both_forms() -> None:
    keys = parse_unsubscribed("ann@xn--bcher-kva.example\n")
    assert "ann@bücher.example" in keys and "ann@xn--bcher-kva.example" in keys
    assert address_keys("Ann@BÜCHER.example") & keys


def test_watcher_rereads_when_the_file_changes(tmp_path: Path) -> None:
    watcher = UnsubscribeWatcher(tmp_path)
    assert not watcher.contains("ann@example.com")
    path = tmp_path / "unsubscribed.txt"
    path.write_text("bob@example.com\n", encoding="utf-8")
    assert watcher.contains("BOB@example.com")
    with path.open("a", encoding="utf-8") as f:
        f.write("ann@example.com\t2026-12-01T10:00:00-05:00\n")
    assert watcher.contains("ann@example.com")
    path.unlink()
    assert not watcher.contains("bob@example.com")


def test_watcher_sees_a_same_size_edit_that_keeps_the_mtime(tmp_path: Path) -> None:
    path = tmp_path / "unsubscribed.txt"
    path.write_text("ann@example.com\t2026-12-01T10:00:00-05:00\n", encoding="utf-8")
    watcher = UnsubscribeWatcher(tmp_path)
    assert watcher.contains("ann@example.com")
    before = path.stat()
    with path.open("r+b") as f:  # in place: same inode, same size
        f.write(b"bob")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert watcher.contains("bob@example.com")
    assert not watcher.contains("ann@example.com")


def test_watcher_picks_up_a_recorded_unsubscribe(tmp_path: Path) -> None:
    watcher = UnsubscribeWatcher(tmp_path)
    record_unsubscribe(tmp_path, "bob@example.com", AT)
    assert watcher.contains("bob@example.com")
    record_unsubscribe(tmp_path, "cy@example.com", AT)
    assert watcher.contains("cy@example.com") and watcher.contains("BOB@example.com")


# --- recording ---------------------------------------------------------------------------------


def lines(tmp_path: Path) -> list[str]:
    return (tmp_path / "unsubscribed.txt").read_text(encoding="utf-8").splitlines(keepends=True)


def test_record_appends_email_tab_time(tmp_path: Path) -> None:
    assert record_unsubscribe(tmp_path, "ada@example.com", AT) is True
    assert lines(tmp_path) == ["ada@example.com\t2026-12-01T10:00:05-05:00\n"]
    assert stat.S_IMODE((tmp_path / "unsubscribed.txt").stat().st_mode) == 0o600


def test_record_appends_once(tmp_path: Path) -> None:
    assert record_unsubscribe(tmp_path, "ada@example.com", AT)
    assert record_unsubscribe(tmp_path, "ada@example.com", AT + timedelta(days=1)) is False
    assert record_unsubscribe(tmp_path, "bob@example.com", AT)
    assert lines(tmp_path) == [
        "ada@example.com\t2026-12-01T10:00:05-05:00\n",
        "bob@example.com\t2026-12-01T10:00:05-05:00\n",
    ]


@pytest.mark.parametrize(
    ("existing", "new"),
    [
        ("ADA@Example.COM\t2026-11-01T09:00:00-05:00\n", "ada@example.com"),
        ("  ada@example.com  \n", "ada@example.com"),  # by hand, no time
        ("jose@xn--bcher-kva.example\t2026-11-01T09:00:00-05:00\n", "jose@bücher.example"),
        ("jose@BÜCHER.example\n", "jose@xn--bcher-kva.example"),
    ],
)
def test_variant_already_present_is_not_appended(tmp_path: Path, existing: str, new: str) -> None:
    (tmp_path / "unsubscribed.txt").write_text(existing, encoding="utf-8")
    assert record_unsubscribe(tmp_path, new, AT) is False
    assert (tmp_path / "unsubscribed.txt").read_text(encoding="utf-8") == existing


def test_record_adds_a_missing_final_newline(tmp_path: Path) -> None:
    (tmp_path / "unsubscribed.txt").write_bytes(b"ann@example.com\t2026-11-01")
    assert record_unsubscribe(tmp_path, "bob@example.com", AT)
    assert lines(tmp_path) == [
        "ann@example.com\t2026-11-01\n",
        "bob@example.com\t2026-12-01T10:00:05-05:00\n",
    ]
    assert load_unsubscribed(tmp_path) == {"ann@example.com", "bob@example.com"}


def test_record_keeps_an_existing_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "unsubscribed.txt"
    path.write_text("", encoding="utf-8")
    path.chmod(0o640)
    record_unsubscribe(tmp_path, "bob@example.com", AT)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_record_refuses_tab_or_newline() -> None:
    for bad in ("a\tb@example.com", "a@example.com\nx@example.com"):
        with pytest.raises(ValueError):
            record_unsubscribe("/nonexistent", bad, AT)


def test_concurrent_records_append_once(tmp_path: Path) -> None:
    variants = ["ada@example.com", "ADA@example.com", " Ada@Example.com "] * 4
    barrier = threading.Barrier(len(variants))
    results: list[bool] = []

    def run(addr: str) -> None:
        barrier.wait()
        results.append(record_unsubscribe(tmp_path, addr.strip().lower(), AT))

    threads = [threading.Thread(target=run, args=(v,)) for v in variants]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False] * (len(variants) - 1) + [True]
    assert lines(tmp_path) == ["ada@example.com\t2026-12-01T10:00:05-05:00\n"]


class FlockSpy:
    """Records every ``fcntl.flock`` call (op, and whether it was refused) and passes it on."""

    def __init__(self, real: object) -> None:
        self.real = real
        self.calls: list[tuple[int, str]] = []

    def __call__(self, fd: int, op: int) -> None:
        try:
            self.real(fd, op)  # type: ignore[operator]
        except BlockingIOError:
            self.calls.append((op, "busy"))
            raise
        self.calls.append((op, "ok"))


EX_NB = fcntl.LOCK_EX | fcntl.LOCK_NB


def test_second_writer_waits_for_the_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Writer a is held after deciding to append. While it waits, the lock is provably held (a
    non-blocking probe on another descriptor is refused), and writer b is refused the lock and
    polls; only its first poll lets a finish, so b can only succeed after a's line is written.
    No timing: b runs in this thread and the order is forced."""
    holding, release = threading.Event(), threading.Event()
    held: list[bool] = []

    def hook() -> None:
        if not held:
            held.append(True)
            holding.set()
            assert release.wait(30)

    monkeypatch.setattr(unsubscribes, "_before_append", hook)
    results: dict[str, bool] = {}
    a = threading.Thread(
        target=lambda: results.update(a=record_unsubscribe(tmp_path, "ada@example.com", AT))
    )
    a.start()
    assert holding.wait(30)

    with (tmp_path / "unsubscribed.txt").open("rb") as probe, pytest.raises(BlockingIOError):
        fcntl.flock(probe.fileno(), EX_NB)

    spy = FlockSpy(fcntl.flock)
    monkeypatch.setattr(unsubscribes.fcntl, "flock", spy)
    sleeps: list[float] = []

    def first_poll(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            assert spy.calls == [(EX_NB, "busy")]  # b was refused while a held the lock
            release.set()
            a.join(30)
            assert not a.is_alive()

    monkeypatch.setattr(unsubscribes, "_sleep", first_poll)
    results["b"] = record_unsubscribe(tmp_path, "ada@example.com", AT)
    assert spy.calls == [(EX_NB, "busy"), (EX_NB, "ok")]
    assert sleeps == [unsubscribes.LOCK_POLL_SECONDS]
    assert results == {"a": True, "b": False}
    assert lines(tmp_path) == ["ada@example.com\t2026-12-01T10:00:05-05:00\n"]


def test_record_waits_for_another_process_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exclusive flock held elsewhere (another process, a careful script) is respected: the
    record is refused the lock, and only reads the file once the other holder has written its
    line and let go (which happens inside the record's first poll)."""
    path = tmp_path / "unsubscribed.txt"
    path.write_text("", encoding="utf-8")
    real_flock = fcntl.flock
    spy = FlockSpy(real_flock)
    with path.open("ab") as f:
        real_flock(f.fileno(), fcntl.LOCK_EX)

        def other_holder_finishes(seconds: float) -> None:
            assert spy.calls == [(EX_NB, "busy")]
            f.write(b"ada@example.com\t2026-11-30T08:00:00-05:00\n")
            f.flush()
            real_flock(f.fileno(), fcntl.LOCK_UN)

        monkeypatch.setattr(unsubscribes.fcntl, "flock", spy)
        monkeypatch.setattr(unsubscribes, "_sleep", other_holder_finishes)
        assert record_unsubscribe(tmp_path, "ada@example.com", AT) is False
    assert spy.calls == [(EX_NB, "busy"), (EX_NB, "ok")]
    assert lines(tmp_path) == ["ada@example.com\t2026-11-30T08:00:00-05:00\n"]


class FakeWait:
    """The module's lock-wait clock and sleep: each sleep moves the clock on by its length."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 0.0
        self.sleeps = 0
        monkeypatch.setattr(unsubscribes, "_monotonic", lambda: self.now)
        monkeypatch.setattr(unsubscribes, "_sleep", self.sleep)

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


def test_lock_wait_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "unsubscribed.txt"
    path.write_bytes(b"bob@example.com\n")
    wait = FakeWait(monkeypatch)
    watcher = UnsubscribeWatcher(tmp_path)
    with path.open("rb") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        with pytest.raises(unsubscribes.LockTimeout):
            record_unsubscribe(tmp_path, "ada@example.com", AT)
        assert wait.now >= unsubscribes.LOCK_WAIT_SECONDS
        expected = unsubscribes.LOCK_WAIT_SECONDS / unsubscribes.LOCK_POLL_SECONDS
        assert abs(wait.sleeps - expected) <= 1
        # Readers give up too, and say so: a locked file is never read as "nobody opted out".
        for read in (lambda: load_unsubscribed(tmp_path), watcher.current):
            with pytest.raises(unsubscribes.LockTimeout):
                read()
        assert issubclass(unsubscribes.LockTimeout, OSError)
    assert path.read_bytes() == b"bob@example.com\n"
    assert watcher.contains("bob@example.com")


def test_watcher_keeps_failing_until_it_can_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a read error the watcher doesn't fall back to its last (or an empty) answer."""
    path = tmp_path / "unsubscribed.txt"
    path.write_bytes(b"bob@example.com\n")
    watcher = UnsubscribeWatcher(tmp_path)
    assert watcher.contains("bob@example.com")
    FakeWait(monkeypatch)
    with path.open("rb") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        for _ in range(2):
            with pytest.raises(unsubscribes.LockTimeout):
                watcher.contains("ann@example.com")
    assert watcher.contains("bob@example.com")


# --- byte-order mark -----------------------------------------------------------------------------

BOM = b"\xef\xbb\xbf"


def test_bom_is_ignored_when_reading(tmp_path: Path) -> None:
    (tmp_path / "unsubscribed.txt").write_bytes(BOM + b"ann@example.com\t2026\nbo@example.com\n")
    assert load_unsubscribed(tmp_path) == {"ann@example.com", "bo@example.com"}
    assert UnsubscribeWatcher(tmp_path).contains("ANN@example.com")
    assert load_entries(tmp_path)[0] == Entry("ann@example.com", "2026")
    assert read_raw(tmp_path).startswith(BOM)  # the download is the file as it is


def test_bom_only_address_line_counts(tmp_path: Path) -> None:
    (tmp_path / "unsubscribed.txt").write_bytes(BOM + b"ann@example.com")
    assert load_unsubscribed(tmp_path) == {"ann@example.com"}


def test_record_sees_the_first_address_after_a_bom(tmp_path: Path) -> None:
    path = tmp_path / "unsubscribed.txt"
    path.write_bytes(BOM + b"Ada@Example.com\t2026-11-01T09:00:00-05:00\r\n")
    assert record_unsubscribe(tmp_path, "ada@example.com", AT) is False
    assert record_unsubscribe(tmp_path, "bob@example.com", AT) is True
    assert path.read_bytes() == (
        BOM
        + b"Ada@Example.com\t2026-11-01T09:00:00-05:00\r\n"
        + b"bob@example.com\t2026-12-01T10:00:05-05:00\n"
    )


def test_a_bom_inside_the_file_is_ignored_too() -> None:
    # e.g. two files saved by Notepad and concatenated: the second one's BOM starts a line.
    assert parse_unsubscribed("ann@example.com\n\ufeffbo@example.com\tx\n") == {
        "ann@example.com",
        "bo@example.com",
    }


def test_count_addresses() -> None:
    entries = [
        Entry("ann@example.com", ""),
        Entry("ANN@Example.com", ""),
        Entry("jose@bücher.example", ""),
        Entry("jose@xn--bcher-kva.example", ""),
        Entry("bo@example.com", ""),
    ]
    assert count_addresses(entries) == 3
    assert count_addresses([]) == 0


def test_record_keeps_the_given_offset(tmp_path: Path) -> None:
    record_unsubscribe(tmp_path, "ada@example.com", datetime(2026, 12, 1, 15, 0, tzinfo=UTC))
    assert lines(tmp_path) == ["ada@example.com\t2026-12-01T15:00:00+00:00\n"]


def test_entries_and_raw(tmp_path: Path) -> None:
    assert load_entries(tmp_path) == [] and read_raw(tmp_path) == b""
    raw = b"Ann@Example.com\t2026-12-01T10:00:00-05:00\nnot an address\n\nbo@example.com\n"
    (tmp_path / "unsubscribed.txt").write_bytes(raw)
    assert load_entries(tmp_path) == [
        Entry("Ann@Example.com", "2026-12-01T10:00:00-05:00"),
        Entry("bo@example.com", ""),
    ]
    assert read_raw(tmp_path) == raw
