import csv
import io
from pathlib import Path

import pytest

from app.csv_ingest import ParsedCsv, parse_csv
from app.recipients import (
    DUPLICATE,
    INVALID_EMAIL,
    MISSING_EMAIL,
    MISSING_FIRST_NAME,
    SKIPPED_STATUS,
    UNSUBSCRIBED,
    ColumnMapping,
    Issue,
    MappingError,
    default_selection,
    issues_csv,
    looks_like_opt_out,
    safe_cell,
    status_key,
    status_values,
    validate_mapping,
    validate_rows,
)

FIXTURES = Path(__file__).parent / "fixtures"
SQUARE = ColumnMapping("Email Address", "First Name", "Email Subscription Status")


def parse(data: bytes) -> ParsedCsv:
    return parse_csv(data, max_upload_mb=5, max_recipients=2000)


@pytest.fixture
def square() -> ParsedCsv:
    return parse((FIXTURES / "square_export.csv").read_bytes())


# --- Mapping -----------------------------------------------------------------------------------

HEADERS = ["Email Address", "First Name", "Status"]


def test_mapping_valid() -> None:
    assert validate_mapping(HEADERS, "Email Address", "First Name", "") == ColumnMapping(
        "Email Address", "First Name", None
    )
    assert validate_mapping(HEADERS, "Email Address", "First Name", "Status").status == "Status"


@pytest.mark.parametrize(
    ("email", "first", "status", "message"),
    [
        (None, "First Name", None, "Choose the Email address column."),
        ("", "First Name", None, "Choose the Email address column."),
        ("Email Address", None, None, "Choose the First name column."),
        ("Nope", "First Name", None, "Email address: 'Nope' is not a column in this file."),
        ("Email Address", "First Name", "Gone", "Subscription status: 'Gone' is not a column"),
        ("Email Address", "Email Address", None, "must be different columns"),
        ("Email Address", "First Name", "Email Address", "must be different columns"),
    ],
)
def test_mapping_errors(
    email: str | None, first: str | None, status: str | None, message: str
) -> None:
    with pytest.raises(MappingError) as exc:
        validate_mapping(HEADERS, email, first, status)
    assert any(message in e for e in exc.value.errors)


def test_mapping_both_required_reported_together() -> None:
    with pytest.raises(MappingError) as exc:
        validate_mapping(HEADERS, None, None, None)
    assert len(exc.value.errors) == 2


# --- Status heuristic --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "unsubscribed",
        "Unsubscribed",
        "UNSUB",
        "not_subscribed",
        "Not subscribed",
        "notsubscribed",
        "nonsubscribed",
        "no",
        "No",
        "N",
        "false",
        "FALSE",
        "0",
        "opt-out",
        "opted out",
        "Opted-Out",
        "optout",
        "opt_out",
        "not opted in",
        "Do not email",
        "don't email",
        "never",
        "declined",
        "bounced",
        "Bounced",
        "bounce",
        "hard bounce",
        "Soft Bounce",
        "soft_bounce",
        "bounced email",
        "pending",
        "Pending",
        "unconfirmed",
        "cleaned",
        "unknown",
        "Unknown",
    ],
)
def test_opt_out_values(value: str) -> None:
    assert looks_like_opt_out(value)


@pytest.mark.parametrize(
    "value",
    [
        "subscribed",
        "Subscribed",
        "SUBSCRIBED",
        "opted in",
        "Opted-In",
        "opt-in",
        "optin",
        "opt in",
        "yes",
        "Y",
        "true",
        "1",
        "active",
        "Notified",  # "not" only counts as a whole word
        "Nora",
    ],
)
def test_opt_in_values(value: str) -> None:
    assert not looks_like_opt_out(value)


def test_status_key_groups_case_and_spacing() -> None:
    assert status_key("  Not   Subscribed ") == status_key("not subscribed") == "not subscribed"


def test_status_values_square(square: ParsedCsv) -> None:
    values = status_values(square, "Email Subscription Status")
    assert [(v.key, v.label, v.count, v.opt_out) for v in values] == [
        ("subscribed", "subscribed", 5, False),
        ("", "", 2, True),
        ("unsubscribed", "unsubscribed", 2, True),
        ("bounced", "bounced", 1, True),
    ]
    assert default_selection(values) == ["subscribed"]


def test_status_label_is_most_common_spelling() -> None:
    parsed = parse(b"e,f,s\na@x.com,A,Yes\nb@x.com,B,yes\nc@x.com,C,yes\n")
    [value] = status_values(parsed, "s")
    assert (value.key, value.label, value.count) == ("yes", "yes", 3)


# --- Validation --------------------------------------------------------------------------------


def test_square_defaults(square: ParsedCsv) -> None:
    selected = default_selection(status_values(square, SQUARE.status or ""))
    report = validate_rows(square, SQUARE, selected, frozenset())
    assert [(r.row, r.email, r.first_name) for r in report.eligible] == [
        (2, "holly.berry@example.com", "Holly"),
        (3, "Nick@example.org", "Nicholas"),
        (7, "jack.frost@example.com", ""),
    ]
    assert report.total_rows == 10
    assert report.counts == {
        MISSING_EMAIL: 1,
        INVALID_EMAIL: 1,
        DUPLICATE: 1,
        SKIPPED_STATUS: 4,
        UNSUBSCRIBED: 0,
        MISSING_FIRST_NAME: 1,
    }
    by_row = {i.row: i for i in report.issues}
    assert by_row[6].category == INVALID_EMAIL and by_row[6].email == "noel@example"
    assert by_row[8].category == DUPLICATE and "row 3" in by_row[8].message
    assert by_row[9].category == MISSING_EMAIL
    assert by_row[11].message == "Status is blank."
    assert by_row[5].message == "Status is 'bounced'."
    assert [i.row for i in report.issues] == sorted(i.row for i in report.issues)


def test_no_status_column_sends_to_everyone(square: ParsedCsv) -> None:
    mapping = ColumnMapping("Email Address", "First Name")
    report = validate_rows(square, mapping, None, frozenset())
    assert report.counts[SKIPPED_STATUS] == 0
    assert len(report.eligible) == 7  # 10 rows minus missing, invalid and duplicate


def test_status_filter_sends_to_ticked_values(square: ParsedCsv) -> None:
    report = validate_rows(square, SQUARE, ["subscribed", "unsubscribed"], frozenset())
    assert "ivy@example.net" in [r.email for r in report.eligible]
    assert report.counts[SKIPPED_STATUS] == 2  # bounced (row 5) and blank (row 11)


def test_status_filter_is_case_insensitive() -> None:
    parsed = parse(b"email,first,s\na@x.com,A,Subscribed\nb@x.com,B, SUBSCRIBED \nc@x.com,C,no\n")
    report = validate_rows(parsed, ColumnMapping("email", "first", "s"), ["subscribed"], set())
    assert [r.email for r in report.eligible] == ["a@x.com", "b@x.com"]


def test_ticking_blank_status_sends_rows_with_an_email(square: ParsedCsv) -> None:
    report = validate_rows(square, SQUARE, ["subscribed", ""], frozenset())
    eligible = [r.email for r in report.eligible]
    assert "eve@example.com" in eligible  # row 11: email, blank status
    by_row = {i.row: i for i in report.issues}
    # Row 9 has neither an email nor a status: still reported as missing email, not sent.
    assert by_row[9].category == MISSING_EMAIL
    assert len(eligible) == 4
    assert report.counts[SKIPPED_STATUS] == 3  # unsubscribed x2, bounced


def test_unsubscribed_list_is_honoured(square: ParsedCsv) -> None:
    report = validate_rows(square, SQUARE, ["subscribed"], frozenset({"nick@example.org"}))
    assert "Nick@example.org" not in [r.email for r in report.eligible]
    assert report.counts[UNSUBSCRIBED] == 1
    # The later duplicate row is still a duplicate, not a second chance.
    assert report.counts[DUPLICATE] == 1


def test_duplicates_differing_in_case() -> None:
    parsed = parse(b"email,first\nAnn@Example.com,Ann\nann@example.COM,Annie\nANN@EXAMPLE.COM,A\n")
    report = validate_rows(parsed, ColumnMapping("email", "first"), None, frozenset())
    assert [r.first_name for r in report.eligible] == ["Ann"]
    assert report.counts[DUPLICATE] == 2


def test_duplicate_of_a_skipped_row_is_not_sent() -> None:
    parsed = parse(b"email,first,s\na@example.com,A,unsubscribed\na@example.com,A,subscribed\n")
    report = validate_rows(parsed, ColumnMapping("email", "first", "s"), ["subscribed"], set())
    assert report.eligible == []
    assert report.counts[SKIPPED_STATUS] == 1
    assert report.counts[DUPLICATE] == 1


@pytest.mark.parametrize(
    "bad", ["plainaddress", "a@b", "a@@example.com", "a b@example.com", "<a@example.com>", "@x.com"]
)
def test_invalid_emails(bad: str) -> None:
    parsed = parse(f"email,first\n{bad},X\nok@example.com,Y\n".encode())
    report = validate_rows(parsed, ColumnMapping("email", "first"), None, frozenset())
    assert report.counts[INVALID_EMAIL] == 1
    assert [r.email for r in report.eligible] == ["ok@example.com"]


def test_empty_first_names_are_warnings_only() -> None:
    parsed = parse(b"email,first\na@example.com,\nb@example.com,  \nc@example.com,Cy\n")
    report = validate_rows(parsed, ColumnMapping("email", "first"), None, frozenset())
    assert len(report.eligible) == 3
    assert report.counts[MISSING_FIRST_NAME] == 2


# --- Issues CSV --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=HYPERLINK(1)", "'=HYPERLINK(1)"),
        ("+1", "'+1"),
        ("-1", "'-1"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("\tx", "'\tx"),
        ("\rx", "'\rx"),
        ("a=b", "a=b"),
        ("", ""),
    ],
)
def test_safe_cell(value: str, expected: str) -> None:
    assert safe_cell(value) == expected


def test_issues_csv_is_injection_safe() -> None:
    issues = [Issue(2, INVALID_EMAIL, "=cmd|' /C calc'!A0", "@Bob", "-x")]
    rows = list(csv.reader(io.StringIO(issues_csv(issues))))
    assert rows[0] == ["row", "problem", "email", "first_name", "detail"]
    assert rows[1] == ["2", "Invalid email", "'=cmd|' /C calc'!A0", "'@Bob", "'-x"]
