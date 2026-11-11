"""Column mapping, the status value picker and the validation report. Pure functions, no I/O."""

import csv
import io
import re
from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

from email_validator import EmailNotValidError, validate_email

from app.csv_ingest import ParsedCsv

# Row categories. Each row with a problem gets exactly one of the first five (checked in this
# order); ``missing_first_name`` is a warning and the row is still sent.
MISSING_EMAIL = "missing_email"
INVALID_EMAIL = "invalid_email"
DUPLICATE = "duplicate"
SKIPPED_STATUS = "skipped_status"
UNSUBSCRIBED = "unsubscribed"
MISSING_FIRST_NAME = "missing_first_name"
CATEGORIES = (
    MISSING_EMAIL,
    INVALID_EMAIL,
    DUPLICATE,
    SKIPPED_STATUS,
    UNSUBSCRIBED,
    MISSING_FIRST_NAME,
)
CATEGORY_LABELS = {
    MISSING_EMAIL: "Missing email",
    INVALID_EMAIL: "Invalid email",
    DUPLICATE: "Duplicate email (first row kept)",
    SKIPPED_STATUS: "Skipped by subscription status",
    UNSUBSCRIBED: "Already unsubscribed",
    MISSING_FIRST_NAME: "Missing first name (sent with the fallback)",
}


class MappingError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class ColumnMapping:
    email: str
    first_name: str
    status: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"email": self.email, "first_name": self.first_name, "status": self.status}


def validate_mapping(
    headers: list[str], email: str | None, first_name: str | None, status: str | None
) -> ColumnMapping:
    """Check the dropdown choices against the header. Blank status means "no status column"."""
    errors: list[str] = []
    email, first_name, status = email or None, first_name or None, status or None
    for label, value, required in (
        ("Email address", email, True),
        ("First name", first_name, True),
        ("Subscription status", status, False),
    ):
        if value is None:
            if required:
                errors.append(f"Choose the {label} column.")
        elif value not in headers:
            errors.append(f"{label}: {value!r} is not a column in this file.")
    chosen = [v for v in (email, first_name, status) if v is not None]
    if len(set(chosen)) != len(chosen):
        errors.append(
            "Email address, First name and Subscription status must be different columns."
        )
    if errors:
        raise MappingError(errors)
    assert email is not None and first_name is not None
    return ColumnMapping(email, first_name, status)


# --- Status values -----------------------------------------------------------------------------


def status_key(value: str) -> str:
    """Grouping key: trimmed, case-insensitive, inner whitespace collapsed."""
    return " ".join(value.split()).casefold()


_OPT_OUT_PREFIXES = ("unsub", "notsub", "nonsub", "nosub", "desub", "bounc")
_OPT_OUT_TOKENS = frozenset(
    {"no", "not", "non", "none", "n", "false", "f", "0", "off", "never", "declined", "denied"}
    | {"don", "dont", "doesn", "doesnt"}  # "don't", "doesn't"
    # Not a confirmed subscription: safer to start unticked.
    | {"pending", "unconfirmed", "cleaned", "unknown"}
)


def looks_like_opt_out(value: str) -> bool:
    """True if a status value probably means "don't email" (its checkbox starts unchecked).

    Matching is on whole words (runs of letters and digits). Opt-out: blank; a word starting
    with ``unsub`` / ``notsub`` / ``nonsub`` (``unsubscribed``, ``not_subscribed`` written as one
    word) or ``bounc`` (``bounced``, ``hard bounce``); "opt(ed) out" in any spelling; a negative
    word (``no``, ``not``, ``false``, ``0``, ``never``, ``declined``…); or an unconfirmed state
    (``pending``, ``unconfirmed``, ``cleaned``, ``unknown``). ``opt`` on its own is not a signal,
    so "opted in", "opt-in" and "optin" stay checked; "not opted in" is an opt-out because of
    "not", while "Notified" is not. Square's real values are ``subscribed``, ``unsubscribed``,
    blank and ``bounced``. The user can override every default.
    """
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    if not tokens:
        return True
    joined = "".join(tokens)
    if "optout" in joined or "optedout" in joined:
        return True
    return any(t.startswith(_OPT_OUT_PREFIXES) or t in _OPT_OUT_TOKENS for t in tokens)


@dataclass(frozen=True)
class StatusValue:
    key: str  # status_key() of the value; the checkbox value
    label: str  # most common original spelling, "" for blank
    count: int
    opt_out: bool  # looks_like_opt_out(); default unchecked


def status_values(parsed: ParsedCsv, column: str) -> list[StatusValue]:
    """Distinct values of ``column`` with counts, most common first."""
    index = parsed.column(column)
    counts: Counter[str] = Counter()
    spellings: dict[str, Counter[str]] = {}
    for row in parsed.rows:
        key = status_key(row[index])
        counts[key] += 1
        spellings.setdefault(key, Counter())[row[index]] += 1
    values = [
        StatusValue(key, spellings[key].most_common(1)[0][0], n, looks_like_opt_out(key))
        for key, n in counts.items()
    ]
    return sorted(values, key=lambda v: (-v.count, v.key))


def default_selection(values: Iterable[StatusValue]) -> list[str]:
    return [v.key for v in values if not v.opt_out]


# --- Validation report -------------------------------------------------------------------------


@dataclass(frozen=True)
class Recipient:
    row: int
    email: str
    first_name: str


@dataclass(frozen=True)
class Issue:
    row: int
    category: str
    email: str
    first_name: str
    message: str


@dataclass
class ValidationReport:
    total_rows: int
    eligible: list[Recipient] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 0))

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)
        self.counts[issue.category] += 1


def validate_rows(
    parsed: ParsedCsv,
    mapping: ColumnMapping,
    selected_status: Collection[str] | None,
    unsubscribed: Collection[str],
) -> ValidationReport:
    """Classify every row. ``selected_status`` holds status keys to send to (None = everyone).

    ``unsubscribed`` holds lower-case addresses. Duplicates are checked before status, so if
    the same address appears twice only the first row counts, even when that row is skipped.
    """
    email_i = parsed.column(mapping.email)
    first_i = parsed.column(mapping.first_name)
    status_i = parsed.column(mapping.status) if mapping.status else None
    selected = None if selected_status is None else set(selected_status)
    first_row: dict[str, int] = {}
    report = ValidationReport(total_rows=len(parsed.rows))

    for number, row in zip(parsed.row_numbers, parsed.rows, strict=True):
        raw, first_name = row[email_i], row[first_i]

        def problem(category: str, message: str) -> None:
            report.add(Issue(number, category, raw, first_name, message))  # noqa: B023

        if not raw:
            problem(MISSING_EMAIL, "No email address.")
            continue
        try:
            email = validate_email(raw, check_deliverability=False).normalized
        except EmailNotValidError as exc:
            problem(INVALID_EMAIL, str(exc))
            continue
        key = email.lower()
        if key in first_row:
            problem(DUPLICATE, f"Same address as row {first_row[key]}.")
            continue
        first_row[key] = number
        if status_i is not None and selected is not None:
            status = row[status_i]
            if status_key(status) not in selected:
                problem(SKIPPED_STATUS, f"Status is {status!r}." if status else "Status is blank.")
                continue
        if key in unsubscribed:
            problem(UNSUBSCRIBED, "This address is in unsubscribed.txt.")
            continue
        if not first_name:
            problem(MISSING_FIRST_NAME, "No first name; the merge field fallback will be used.")
        report.eligible.append(Recipient(number, email, first_name))
    return report


# --- Downloadable issues list ------------------------------------------------------------------

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: str) -> str:
    """Stop spreadsheets reading a cell as a formula (CSV injection)."""
    return "'" + value if value.startswith(_FORMULA_START) else value


def issues_csv(issues: Iterable[Issue]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["row", "problem", "email", "first_name", "detail"])
    for i in issues:
        writer.writerow(
            [
                str(i.row),
                CATEGORY_LABELS[i.category],
                safe_cell(i.email),
                safe_cell(i.first_name),
                safe_cell(i.message),
            ]
        )
    return out.getvalue()
