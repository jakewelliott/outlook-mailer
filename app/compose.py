"""The message written for a draft, app-wide settings, and the one rendering path.

``DraftContent`` is what the compose page edits: subject, preheader, Markdown body, the CAN-SPAM
footer and the header text. It is stored per draft in ``draft_content``. The footer fields and the
header text are also saved as app-wide settings (``settings`` table, key/value), so a new draft
starts with the last business name and postal address used.

``render_message`` is the path the preview uses and sending (stages 9 and 10) must use too:
``content.prepare`` -> ``PreparedBody.for_recipient`` -> ``render.render_email`` /
``render.render_text``, with a real unsubscribe URL (``unsubscribe_tokens``).

Gmail clips HTML over about 102 KB ("[Message clipped]"), which hides the footer and its
unsubscribe link. ``size_check`` renders for the longest first name and address on the list (and
for an empty first name, which gets the merge fallbacks), so the number shown is a near-upper
bound (see ``size_check`` for the small gap).
"""

import hashlib
import html
import json
import sqlite3
import unicodedata
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime

from app import content, merge, render
from app.db import Database
from app.emails import normalise_key
from app.recipients import Recipient
from app.unsubscribe_tokens import unsubscribe_url

KB = 1024
SIZE_WARN_BYTES = 90 * KB
SIZE_LIMIT_BYTES = 102 * KB
MAX_SUBJECT, MAX_PREHEADER, MAX_BODY = 250, 250, 100_000
MAX_HEADLINE, MAX_TAGLINE = 60, 100

# The compose form's maxlength per field: the server-side limit, so the browser stops typing where
# saving would fail. The server measures the footer after collapsing spaces and dropping blank
# lines, so the browser limit is the same or stricter, never looser.
FORM_LIMITS = {
    "subject": MAX_SUBJECT,
    "preheader": MAX_PREHEADER,
    "body": MAX_BODY,
    "headline": MAX_HEADLINE,
    "tagline": MAX_TAGLINE,
    "business_name": render.MAX_BUSINESS_NAME,
    "postal_address": render.MAX_ADDRESS,
    "reason": render.MAX_REASON,
}

# Footer and header fields, saved app-wide as well as on the draft.
SETTING_FIELDS = (
    "business_name",
    "postal_address",
    "is_advertisement",
    "reason",
    "headline",
    "tagline",
)


@dataclass(frozen=True)
class DraftContent:
    """Form values as typed (not normalised), so a re-shown form keeps the user's input."""

    subject: str = ""
    preheader: str = ""
    body: str = ""
    business_name: str = ""
    postal_address: str = ""
    is_advertisement: bool = True
    reason: str = ""
    headline: str = render.DEFAULT_HEADLINE
    tagline: str = render.DEFAULT_TAGLINE

    @classmethod
    def from_form(cls, form: Mapping[str, object]) -> "DraftContent":
        """Read the compose form. Text fields: CRLF becomes LF. A ticked checkbox sends "1"."""

        def text(name: str) -> str:
            value = form.get(name)
            return value.replace("\r\n", "\n").replace("\r", "\n") if isinstance(value, str) else ""

        return cls(
            subject=text("subject"),
            preheader=text("preheader"),
            body=text("body"),
            business_name=text("business_name"),
            postal_address=text("postal_address"),
            is_advertisement=form.get("is_advertisement") == "1",
            reason=text("reason"),
            headline=text("headline"),
            tagline=text("tagline"),
        )

    def footer(self) -> render.FooterInfo:
        """Raises ``render.FooterError``."""
        return render.FooterInfo(
            business_name=self.business_name,
            postal_address=self.postal_address,
            is_advertisement=self.is_advertisement,
            reason=self.reason,
        )


# --- validation --------------------------------------------------------------------------------

Errors = dict[str, list[str]]  # field name -> messages


def _has_control(text: str, allow: str = "") -> bool:
    return any(unicodedata.category(ch).startswith("C") and ch not in allow for ch in text)


def _footer_errors(c: DraftContent) -> Errors:
    """FooterInfo's messages, attributed to fields by checking each with valid stand-ins."""
    errors: Errors = {}
    probes = {
        "business_name": dict(business_name=c.business_name, postal_address="x"),
        "postal_address": dict(business_name="x", postal_address=c.postal_address),
        "reason": dict(business_name="x", postal_address="x", reason=c.reason),
    }
    for name, kwargs in probes.items():
        try:
            render.FooterInfo(**kwargs)
        except render.FooterError as exc:
            errors[name] = exc.errors
    return errors


def _header_errors(label: str, value: str, limit: int, *, required: bool) -> list[str]:
    errors = []
    value = value.strip()
    if required and not value:
        errors.append(f"The {label} is required.")
    if len(value) > limit:
        errors.append(f"The {label} is too long (at most {limit} characters).")
    if _has_control(value):
        errors.append(f"The {label} contains a control character.")
    if "{{" in value or "{%" in value:
        errors.append(f"Merge fields can't be used in the {label}.")
    return errors


def validate(c: DraftContent) -> Errors:
    """Every problem, by form field; empty if the content can be saved and sent."""
    errors: Errors = {}

    def add(name: str, messages: list[str]) -> None:
        if messages:
            errors.setdefault(name, []).extend(messages)

    add("subject", content.check_subject(c.subject))
    if len(c.subject) > MAX_SUBJECT:
        add("subject", [f"The subject is too long (at most {MAX_SUBJECT} characters)."])
    if "\n" in c.subject:
        add("subject", ["The subject must be one line."])
    add("preheader", merge.validate(c.preheader))
    if len(c.preheader) > MAX_PREHEADER:
        add("preheader", [f"The preview text is too long (at most {MAX_PREHEADER} characters)."])
    if len(c.body) > MAX_BODY:
        add("body", [f"The message is too long (at most {MAX_BODY} characters)."])
    else:
        add("body", content.check_body(c.body))
    for name, messages in _footer_errors(c).items():
        add(name, messages)
    add("headline", _header_errors("headline", c.headline, MAX_HEADLINE, required=True))
    add("tagline", _header_errors("tagline", c.tagline, MAX_TAGLINE, required=False))
    return errors


# --- rendering ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str
    text: str
    unsubscribe_url: str  # the link in the body; reuse it for the List-Unsubscribe header


def render_message(
    c: DraftContent,
    values: Mapping[str, str | None],
    *,
    public_base_url: str,
    unsubscribe_secret: str,
    prepared: content.PreparedBody | None = None,
    force_dark: bool = False,
    unsubscribe_for: str | None = None,
) -> RenderedEmail:
    """One recipient's subject, HTML and plain text. ``c`` must pass ``validate``.

    ``prepared`` may be passed to reuse ``content.prepare(c.body)`` across recipients.
    ``force_dark`` is for the admin preview only (see ``render.render_email``).
    ``unsubscribe_for``: the address the unsubscribe link is for, if not ``values["email"]``
    (a test send shows a customer's merge values but goes to Jake, so its link must be his).
    """
    prepared = prepared or content.prepare(c.body)
    fragment, text = prepared.for_recipient(values)
    footer = c.footer()
    unsub_email = unsubscribe_for if unsubscribe_for is not None else values.get("email") or ""
    unsub = unsubscribe_url(public_base_url, unsubscribe_secret, unsub_email)
    subject = content.render_subject(c.subject, values)
    page = render.render_email(
        fragment,
        subject=subject,
        preheader=merge.render(c.preheader, values, mode="subject"),
        footer=footer,
        unsubscribe_url=unsub,
        public_base_url=public_base_url,
        headline=c.headline.strip(),
        tagline=c.tagline.strip(),
        force_dark=force_dark,
    )
    return RenderedEmail(
        subject=subject,
        html=page,
        text=render.render_text(text, footer, unsub),
        unsubscribe_url=unsub,
    )


def _url_length(value: str) -> int:
    """The most room a value can take in the HTML: HTML-escaped or percent-encoded in a link."""
    return max(len(html.escape(value).encode()), len(urllib.parse.quote(value, safe="@")))


def _token_length(email: str) -> int:
    """UTF-8 bytes of the address as the unsubscribe token encodes it."""
    return len(normalise_key(email).encode())


def worst_case_values(recipients: Iterable[Recipient]) -> dict[str, str]:
    """The longest first name and the longest address on the list (may be from different rows).

    Lengths are measured as they appear in the HTML (``_url_length``). The address keeps a real
    one's length; it is only used to measure size.
    """
    first_name, email = "", "friend@example.com"
    for r in recipients:
        if _url_length(r.first_name) > _url_length(first_name):
            first_name = r.first_name
        if _url_length(r.email) > _url_length(email):
            email = r.email
    return {"first_name": first_name, "email": email}


def _longest_token_email(recipients: Iterable[Recipient], default: str) -> str:
    return max((r.email for r in recipients), key=_token_length, default=default)


@dataclass(frozen=True)
class SizeCheck:
    bytes: int

    @property
    def kb(self) -> float:
        return self.bytes / KB

    @property
    def too_large(self) -> bool:
        return self.bytes > SIZE_LIMIT_BYTES

    @property
    def warn(self) -> bool:
        return self.bytes > SIZE_WARN_BYTES

    def message(self) -> str | None:
        if self.too_large:
            return (
                f"The email is {self.kb:.0f} KB, over the {SIZE_LIMIT_BYTES // KB} KB limit: Gmail "
                "would cut it off (hiding the footer and unsubscribe link). Shorten the message."
            )
        if self.warn:
            return (
                f"The email is {self.kb:.0f} KB, close to the {SIZE_LIMIT_BYTES // KB} KB point "
                "where Gmail cuts messages off. Consider shortening it."
            )
        return None


def size_check(
    c: DraftContent,
    recipients: Iterable[Recipient],
    *,
    public_base_url: str,
    unsubscribe_secret: str,
) -> SizeCheck:
    """Near-upper bound on the UTF-8 size of the HTML any recipient on the list gets.

    ``c`` must be valid. A first name and an address change the size independently, so each is
    measured on its own: the longest first name against an empty one (whose merge fallbacks may be
    longer), then the longest address in the HTML against the longest in the unsubscribe token.
    Because the address is measured twice rather than as one combined worst case, an address that
    is second-longest both in the HTML (escaping, percent-encoding) and in the token can come out
    slightly larger than the figure. Lengths in both track the address length, so the gap is tiny
    next to the room the 90 KB warning leaves.
    """
    recipients = list(recipients)
    prepared = content.prepare(c.body)

    def size(values: dict[str, str]) -> int:
        out = render_message(
            c,
            values,
            public_base_url=public_base_url,
            unsubscribe_secret=unsubscribe_secret,
            prepared=prepared,
        )
        return len(out.html.encode())

    worst = worst_case_values(recipients)
    largest = size(worst)
    if worst["first_name"]:
        fallback = size({**worst, "first_name": ""})
        if fallback > largest:
            largest, worst = fallback, {**worst, "first_name": ""}
    token_email = _longest_token_email(recipients, worst["email"])
    if _token_length(token_email) > _token_length(worst["email"]):
        largest = max(largest, size({**worst, "email": token_email}))
    return SizeCheck(largest)


# --- storage -----------------------------------------------------------------------------------

_COLUMNS = tuple(f.name for f in fields(DraftContent))


def _from_row(row: sqlite3.Row) -> DraftContent:
    values = {name: row[name] for name in _COLUMNS}
    values["is_advertisement"] = bool(values["is_advertisement"])
    return DraftContent(**values)


def get_content(db: Database, draft_id: str) -> DraftContent | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM draft_content WHERE draft_id = ?", (draft_id,)).fetchone()
    return None if row is None else _from_row(row)


def get_settings(db: Database) -> dict[str, str]:
    with db.connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def defaults(db: Database) -> DraftContent:
    """A new draft's content: empty message, footer and header from the saved settings."""
    saved = get_settings(db)
    base = DraftContent()
    values: dict[str, object] = {}
    for name in SETTING_FIELDS:
        if name in saved:
            values[name] = saved[name] != "0" if name == "is_advertisement" else saved[name]
    return DraftContent(**{**asdict(base), **values})


def save_content(db: Database, draft_id: str, c: DraftContent) -> None:
    """Store the draft's content and remember the footer and header as app-wide settings.

    Settings are text; ``is_advertisement`` is stored as ``"1"`` or ``"0"``.
    """
    row = asdict(c)
    row["is_advertisement"] = int(c.is_advertisement)
    updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    columns = ", ".join(_COLUMNS)
    marks = ", ".join("?" for _ in _COLUMNS)
    updates = ", ".join(f"{name} = excluded.{name}" for name in ("updated_at", *_COLUMNS))
    with db.connect() as conn:
        conn.execute(
            f"INSERT INTO draft_content (draft_id, updated_at, {columns}) "
            f"VALUES (?, ?, {marks}) ON CONFLICT (draft_id) DO UPDATE SET {updates}",
            (draft_id, updated_at, *(row[name] for name in _COLUMNS)),
        )
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [(name, str(row[name])) for name in SETTING_FIELDS],
        )


# --- snapshots (send jobs) ---------------------------------------------------------------------


def to_json(c: DraftContent) -> str:
    """The content as canonical JSON (sorted keys), as stored in a campaign's snapshot."""
    return json.dumps(asdict(c), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def from_json(data: str) -> DraftContent:
    values = json.loads(data)
    return DraftContent(**{name: values[name] for name in _COLUMNS})


def content_hash(c: DraftContent) -> str:
    """SHA-256 of ``to_json(c)``: identifies a version of the content.

    A campaign records the hash of its snapshot; the test-send gate (stage 12) requires a
    successful test send of the same hash before a bulk send can start.
    """
    return hashlib.sha256(to_json(c).encode()).hexdigest()
