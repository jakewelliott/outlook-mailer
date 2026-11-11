"""Reading an uploaded CSV: encoding, delimiter, header row, limits, column guesses.

Pure functions, no I/O. ``parse_csv`` raises ``CsvError`` with a message meant for the user.
"""

import codecs
import csv
import io
import re
from dataclasses import dataclass

from charset_normalizer import from_bytes

DELIMITERS = (",", ";", "\t")
DELIMITER_NAMES = {",": "comma", ";": "semicolon", "\t": "tab"}
MB = 1024 * 1024


class CsvError(ValueError):
    """The file can't be used. The message is shown to the user."""


@dataclass(frozen=True)
class ParsedCsv:
    headers: list[str]
    # Each row has exactly ``len(headers)`` cells, stripped of surrounding whitespace.
    rows: list[list[str]]
    # Spreadsheet row number of each row (header is row 1; blank rows are skipped but counted).
    row_numbers: list[int]
    encoding: str
    delimiter: str

    def column(self, name: str) -> int:
        return self.headers.index(name)


def decode(data: bytes) -> tuple[str, str]:
    """Return ``(text, encoding)``. UTF-8 (with or without BOM), UTF-16 with BOM, else cp1252."""
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            raise CsvError("The file says it is UTF-16 but could not be read as UTF-16.") from None
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    # Excel on Windows saves "CSV" as cp1252. It decodes almost any byte string, so it is tried
    # before guessing; the guesser only gets files with bytes cp1252 doesn't define.
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        pass
    best = from_bytes(data).best()
    if best is None:
        raise CsvError("Could not work out the file's text encoding. Save it as CSV UTF-8.")
    return str(best), best.encoding


def sniff_delimiter(text: str) -> str:
    """Pick the delimiter that splits the header row into the most columns (comma on a tie)."""
    best, best_count = ",", 0
    for delimiter in DELIMITERS:
        try:
            first = next(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter), [])
        except csv.Error:
            continue
        if len(first) > best_count:
            best, best_count = delimiter, len(first)
    return best


def _clean_headers(raw: list[str], rows: list[list[str]]) -> list[str]:
    headers = [h.strip() for h in raw]
    # A trailing delimiter (common in Excel exports) leaves blank columns at the end; drop them
    # when they hold no data.
    while headers and not headers[-1]:
        i = len(headers) - 1
        if any(i < len(row) and row[i].strip() for row in rows):
            break
        headers.pop()
    if not headers:
        raise CsvError("The first row is empty. It must contain the column names.")
    blank = [str(i + 1) for i, h in enumerate(headers) if not h]
    if blank:
        raise CsvError(
            f"{'Column' if len(blank) == 1 else 'Columns'} {', '.join(blank)} "
            f"{'has' if len(blank) == 1 else 'have'} no name in the first row. Every column with "
            "data needs a name in the header row."
        )
    # Disambiguate duplicates ("Email", "Email (2)") so every column can be picked by name.
    seen: set[str] = set()
    unique: list[str] = []
    for header in headers:
        name, n = header, 1
        while name.casefold() in seen:
            n += 1
            name = f"{header} ({n})"
        seen.add(name.casefold())
        unique.append(name)
    return unique


def too_large_message(max_upload_mb: int) -> str:
    return f"The file is larger than {max_upload_mb} MB (MAX_UPLOAD_MB). Split it up."


def parse_csv(data: bytes, *, max_upload_mb: int, max_recipients: int) -> ParsedCsv:
    """Decode and parse an uploaded CSV. Raises ``CsvError``."""
    if len(data) > max_upload_mb * MB:
        raise CsvError(too_large_message(max_upload_mb))
    if not data.strip():
        raise CsvError("The file is empty.")
    text, encoding = decode(data)
    if "\x00" in text:
        raise CsvError("This doesn't look like a CSV text file. Export or save it as CSV.")
    delimiter = sniff_delimiter(text)

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    raw_rows: list[list[str]] = []
    numbers: list[int] = []
    try:
        header = next(reader)
        for number, row in enumerate(reader, start=2):
            if not any(cell.strip() for cell in row):
                continue
            raw_rows.append(row)
            numbers.append(number)
            if len(raw_rows) > max_recipients:
                raise CsvError(
                    f"The file has more than {max_recipients} rows; the limit is "
                    f"{max_recipients} (MAX_RECIPIENTS). Split it into smaller lists."
                )
    except csv.Error as exc:
        raise CsvError(
            f"The file could not be read as CSV near line {reader.line_num}: {exc}. Check for a "
            "stray or unclosed quote mark."
        ) from None
    except StopIteration:
        raise CsvError("The file is empty.") from None

    headers = _clean_headers(header, raw_rows)
    if not raw_rows:
        raise CsvError("The file has a header row but no data rows.")
    width = len(headers)
    rows = [[cell.strip() for cell in (row + [""] * width)[:width]] for row in raw_rows]
    return ParsedCsv(headers, rows, numbers, encoding, delimiter)


# --- Column guesses (preselect the dropdowns; the user can change them) ------------------------


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.casefold())


_STATUS_WORDS = ("subscri", "optin", "optout", "optedin", "optedout", "consent", "marketing")
_EMAIL_EXACT = {"email", "emailaddress", "mail", "emailaddr", "primaryemail", "emailid"}
_FIRST_EXACT = {"firstname", "first", "fname", "givenname", "forename", "firstnames"}
_FIRST_CONTAINS = ("firstname", "givenname", "forename")


def _pick(headers: list[str], exact: set[str], contains: tuple[str, ...]) -> str | None:
    normed = [_norm(h) for h in headers]
    for header, n in zip(headers, normed, strict=True):
        if n in exact:
            return header
    for header, n in zip(headers, normed, strict=True):
        if any(word in n for word in contains):
            return header
    return None


def guess_columns(headers: list[str]) -> dict[str, str | None]:
    """Best guesses for ``email``, ``first_name`` and ``status`` (None when nothing fits)."""
    status = _pick(headers, set(), _STATUS_WORDS)
    others = [h for h in headers if h != status]
    email = _pick(others, _EMAIL_EXACT, ("email",))
    first_name = _pick([h for h in others if h != email], _FIRST_EXACT, _FIRST_CONTAINS)
    return {"email": email, "first_name": first_name, "status": status}
