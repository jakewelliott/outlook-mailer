from pathlib import Path

import pytest

from app.csv_ingest import (
    MB,
    CsvError,
    ParsedCsv,
    decode,
    guess_columns,
    parse_csv,
    sniff_delimiter,
)

FIXTURES = Path(__file__).parent / "fixtures"
SQUARE_HEADER = [
    "Reference ID", "First Name", "Last Name", "Email Address", "Phone Number", "Nickname",
    "Company Name", "Street Address 1", "Street Address 2", "City", "State", "Postal Code",
    "Birthday", "Memo", "Square Customer ID", "Creation Source", "First Visit", "Last Visit",
    "Transaction Count", "Lifetime Spend", "Email Subscription Status", "Instant Profile",
    "Alternate Phone", "Notes",
]  # fmt: skip


def parse(data: bytes, max_upload_mb: int = 5, max_recipients: int = 2000) -> ParsedCsv:
    return parse_csv(data, max_upload_mb=max_upload_mb, max_recipients=max_recipients)


def test_square_export() -> None:
    parsed = parse((FIXTURES / "square_export.csv").read_bytes())
    assert parsed.headers == SQUARE_HEADER
    assert parsed.delimiter == ","
    assert len(parsed.rows) == 10
    assert all(len(row) == 24 for row in parsed.rows)
    nick = parsed.rows[1]
    assert nick[parsed.column("Company Name")] == "Klaus, Sons & Elves"
    assert nick[parsed.column("Memo")] == 'Prefers "fast" delivery'
    assert nick[parsed.column("Notes")] == "Line one\nline two"
    # Row numbers count records, so the multi-line record is still row 3.
    assert parsed.row_numbers[:3] == [2, 3, 4]
    assert guess_columns(parsed.headers) == {
        "email": "Email Address",
        "first_name": "First Name",
        "status": "Email Subscription Status",
    }


def test_utf8_bom() -> None:
    parsed = parse(b"\xef\xbb\xbfEmail,First Name\r\na@example.com,Zo\xc3\xab\r\n")
    assert parsed.encoding == "utf-8-sig"
    assert parsed.headers == ["Email", "First Name"]  # no BOM glued to the first header
    assert parsed.rows == [["a@example.com", "Zoë"]]


def test_cp1252_accented_names() -> None:
    data = "Email;Prénom\r\nrene@example.com;René\r\nfrancois@example.com;François\r\n"
    parsed = parse(data.encode("cp1252"))
    assert parsed.encoding == "cp1252"
    assert parsed.delimiter == ";"
    assert parsed.headers == ["Email", "Prénom"]
    assert [r[1] for r in parsed.rows] == ["René", "François"]


def test_utf16_with_bom() -> None:
    parsed = parse("Email\tFirst\na@example.com\tAnn\n".encode("utf-16"))
    assert parsed.delimiter == "\t"
    assert parsed.rows == [["a@example.com", "Ann"]]


def test_semicolon_with_commas_in_data() -> None:
    parsed = parse(b"email;name\na@example.com;Smith, Ann\n")
    assert parsed.delimiter == ";"
    assert parsed.rows == [["a@example.com", "Smith, Ann"]]


def test_tab_delimited() -> None:
    parsed = parse(b"Email Address\tFirst Name\tStatus\na@example.com\tAnn\tsubscribed\n")
    assert parsed.delimiter == "\t"
    assert parsed.headers == ["Email Address", "First Name", "Status"]


def test_quoted_commas_and_newlines() -> None:
    parsed = parse(
        b'email,first,note\n"a@example.com","Ann","one, two\nthree"\nb@example.com,Bo,x\n'
    )
    assert parsed.rows[0] == ["a@example.com", "Ann", "one, two\nthree"]
    assert parsed.row_numbers == [2, 3]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("a,b;c", ","), ("a;b;c,d", ";"), ("a\tb\tc", "\t"), ("single", ","), ("a;b", ";")],
)
def test_sniff_delimiter(text: str, expected: str) -> None:
    assert sniff_delimiter(text + "\n1,2\n") == expected


def test_whitespace_stripped_and_short_rows_padded() -> None:
    parsed = parse(b" Email , First Name \n  a@example.com  \n")
    assert parsed.headers == ["Email", "First Name"]
    assert parsed.rows == [["a@example.com", ""]]


def test_blank_rows_skipped_but_counted() -> None:
    parsed = parse(b"email,first\n\na@example.com,Ann\n,\nb@example.com,Bo\n")
    assert parsed.rows == [["a@example.com", "Ann"], ["b@example.com", "Bo"]]
    assert parsed.row_numbers == [3, 5]


def test_duplicate_headers_disambiguated() -> None:
    parsed = parse(b"Email,email,Name,Name,Name (2)\n1,2,3,4,5\n")
    assert parsed.headers == ["Email", "email (2)", "Name", "Name (2)", "Name (2) (2)"]


def test_trailing_empty_header_columns_dropped() -> None:
    parsed = parse(b"email,first,,\na@example.com,Ann,,\n")
    assert parsed.headers == ["email", "first"]
    assert parsed.rows == [["a@example.com", "Ann"]]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "empty"),
        (b"  \r\n\r\n", "empty"),
        (b"email,first\n", "no data rows"),
        (b"email,first\r\n\r\n", "no data rows"),
        (b"\na@example.com,Ann\n", "first row is empty"),
        (b",,\na,b,c\n", "Columns 1, 2, 3 have no name"),
        (b"email,,first\na,b,c\n", "Column 2 has no name"),
        (b"email,first,\na,b,c\n", "Column 3 has no name"),
        (b'email,first\n"a@example.com,Ann\n', "unclosed quote"),
        (b"email\x00,first\n", "doesn't look like a CSV"),
    ],
)
def test_rejected_files(data: bytes, message: str) -> None:
    with pytest.raises(CsvError, match=message):
        parse(data)


def test_over_size_limit() -> None:
    data = b"email,first\n" + b"a@example.com,Ann\n" * (MB // 18 + 10)
    with pytest.raises(CsvError, match=r"^The file is larger than 1 MB \(MAX_UPLOAD_MB\)\."):
        parse(data, max_upload_mb=1)


def test_over_row_limit() -> None:
    data = b"email,first\n" + b"a@example.com,Ann\n" * 4
    assert len(parse(data, max_recipients=4).rows) == 4
    with pytest.raises(CsvError, match=r"more than 3 rows.*MAX_RECIPIENTS"):
        parse(data, max_recipients=3)


def test_decode_prefers_utf8() -> None:
    assert decode("Zoë".encode()) == ("Zoë", "utf-8")


def test_decode_falls_back_beyond_cp1252() -> None:
    # 0x81 is undefined in cp1252, so the guesser decides. Either a decode or a clear error.
    try:
        text, encoding = decode(b"email,first\na@example.com,\x81\xe9\x81\n")
    except CsvError:
        return
    assert text.startswith("email,first")
    assert encoding


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (["E-mail", "Given name"], {"email": "E-mail", "first_name": "Given name", "status": None}),
        (["email", "firstname"], {"email": "email", "first_name": "firstname", "status": None}),
        (
            ["Name", "Email (work)", "First_Name", "Opt In"],
            {"email": "Email (work)", "first_name": "First_Name", "status": "Opt In"},
        ),
        (
            ["Subscription", "Email Address"],
            {"email": "Email Address", "first_name": None, "status": "Subscription"},
        ),
        (["Phone", "City"], {"email": None, "first_name": None, "status": None}),
        # An exact "email" header wins over one that only contains the word.
        (
            ["Email Notes", "Email", "fname"],
            {"email": "Email", "first_name": "fname", "status": None},
        ),
    ],
)
def test_guess_columns(headers: list[str], expected: dict[str, str | None]) -> None:
    assert guess_columns(headers) == expected
