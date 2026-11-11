from pathlib import Path

from app.unsubscribes import load_unsubscribed, parse_unsubscribed


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
