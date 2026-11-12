from pathlib import Path

from app.unsubscribes import (
    UnsubscribeWatcher,
    address_keys,
    load_unsubscribed,
    parse_unsubscribed,
)


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
