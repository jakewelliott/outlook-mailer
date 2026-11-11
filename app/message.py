"""One recipient's email as MIME bytes, and the base64 body Graph ``/me/sendMail`` takes.

``build_mime`` turns a ``compose.RenderedEmail`` into ``multipart/alternative`` (plain text first,
then HTML, both UTF-8) with exactly one address, in ``To``. There is never a ``Cc``, ``Bcc`` or
``Reply-To``. Headers: ``From``, ``To``, ``Subject``, ``Date``, ``Message-ID`` (at the
``PUBLIC_BASE_URL`` host, taken from the unsubscribe link), ``MIME-Version``,
``List-Unsubscribe: <PUBLIC_BASE_URL/u/<token>>`` and, for an https link,
``List-Unsubscribe-Post: List-Unsubscribe=One-Click`` (RFC 8058 requires https).

``From``: Graph sends from the signed-in mailbox whatever the MIME says, and Exchange may refuse a
message whose ``From`` is another address (send-as). RFC 5322 requires a ``From``, so it is always
written: the Graph transport (stage 12) must pass the signed-in address; without one (mock
transport, tests) it is ``PLACEHOLDER_FROM``.

Encoding: each part is quoted-printable, or base64 when that is shorter (text that is mostly not
Latin script). Both keep lines at 76 characters and the message 7-bit, which every hop accepts.
Headers are folded at 78 characters with RFC 2047 encoded words for non-ASCII text and display
names, except ``List-Unsubscribe``: Python would fold a long URL into encoded words, which mail
apps do not decode in that header, so it is written raw on one line (refused over 998).

Header injection: a value with a control character or a Unicode line/paragraph separator raises
``MessageError``. The merge already removes them; this is the second line of defence. Python
decodes RFC 2047 encoded words in a value assigned to ``Subject`` *after* that check, so
``=?utf-8?q?=0D=0A...?=`` would become a real line break: in the subject and in display names
``=?`` gets a space (``= ?``, as ``content.render_subject`` does for the preview), and the stored
subject is checked again after assignment. Display names also lose ``"<>,;:@\\()[]`` (Python
encodes a non-ASCII name as one encoded word, punctuation included, and a parser that decodes
before parsing would find extra addresses in it) and bidi/invisible characters
(``merge.strip_invisible``).
"""

import base64
import unicodedata
import urllib.parse
from datetime import UTC, datetime
from email import message_from_bytes, policy, quoprimime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from email_validator import EmailNotValidError, validate_email

from app.compose import RenderedEmail
from app.content import neutralise_encoded_words
from app.merge import strip_invisible

PLACEHOLDER_FROM = "sender@example.invalid"
# Graph has no documented sendMail size limit for MIME; 4 MB is its usual request ceiling
# (docs/RESEARCH.md 2.6). A 102 KB email is a few hundred KB of base64.
GRAPH_MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_LINE = 998  # RFC 5322 hard limit, without the CRLF

# SMTP: CRLF line endings, folding at 78. cte_type 7bit: never emit raw 8-bit data.
# refold_source none: headers stored raw (List-Unsubscribe) are written as they are.
POLICY = policy.SMTP.clone(cte_type="7bit", refold_source="none")

# Address headers other than From. Of these, only To is ever written.
_OTHER_ADDRESS_HEADERS = (
    "to",
    "cc",
    "bcc",
    "reply-to",
    "sender",
    "resent-to",
    "resent-cc",
    "resent-bcc",
)


# Address syntax characters, replaced by spaces in display names (see the module docstring).
_NAME_PUNCTUATION = str.maketrans(dict.fromkeys('"<>,;:@\\()[]', " "))


class MessageError(ValueError):
    """The message can't be built or sent as it is (bad address, header value or size)."""


def _has_control(value: str) -> bool:
    return any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in value)


def _check_header_value(label: str, value: str) -> str:
    if _has_control(value):
        raise MessageError(f"The {label} contains a line break or control character.")
    return value


def ascii_address(addr: str, label: str = "address") -> str:
    """``addr`` as an ASCII addr-spec (IDN domain in punycode). Raises ``MessageError``.

    An address whose local part isn't ASCII needs SMTPUTF8, which this app doesn't use.
    """
    _check_header_value(label, addr)
    try:
        result = validate_email(addr, check_deliverability=False)
    except EmailNotValidError as exc:
        raise MessageError(f"The {label} is not a valid email address: {exc}") from None
    if result.ascii_email is None:
        raise MessageError(f"The {label} has non-ASCII characters before the @ (not supported).")
    return result.ascii_email


def _address(addr: str | None, name: str | None, label: str) -> Address:
    # The placeholder is ours (and .invalid, which email-validator always refuses).
    addr_spec = PLACEHOLDER_FROM if addr is None else ascii_address(addr, label)
    display = strip_invisible(_check_header_value(f"{label} name", name or ""))
    # A non-ASCII name is written as one encoded word, punctuation and all; a parser that decodes
    # first would read "Jöhn, <evil@x.com>" as more addresses. Names don't need these characters.
    display = display.translate(_NAME_PUNCTUATION)
    display = " ".join(display.split())
    # A name that looks like an RFC 2047 encoded word would be decoded by mail apps; break it up.
    display = neutralise_encoded_words(display)
    return Address(display_name=display, addr_spec=addr_spec)


def _host(unsubscribe_url: str) -> str:
    """The ASCII host of the unsubscribe link (= ``PUBLIC_BASE_URL``), for the Message-ID.

    An IP literal isn't supported (``config`` refuses an IPv6 ``PUBLIC_BASE_URL``): ``[::1]`` would
    need the brackets of an RFC 5322 domain literal, which ``make_msgid`` doesn't add.
    """
    host = urllib.parse.urlsplit(unsubscribe_url).hostname or ""
    if ":" in host:
        raise MessageError("The unsubscribe link has an IPv6 address; it needs a host name.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        host = ""
    if not host:
        raise MessageError("The unsubscribe link has no usable host name.")
    return host


def _check_unsubscribe_url(url: str) -> str:
    _check_header_value("unsubscribe link", url)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        raise MessageError("The unsubscribe link must be an http(s) URL.")
    if not url.isascii() or any(ch in url for ch in " <>\"'\\"):
        raise MessageError("The unsubscribe link has characters a header can't carry.")
    if len("List-Unsubscribe: <>") + len(url) > MAX_LINE:
        raise MessageError("The unsubscribe link is too long for a header.")
    return url


def _cte(text: str) -> str:
    """Quoted-printable, or base64 when that comes out shorter."""
    data = text.encode("utf-8")
    qp = quoprimime.body_length(data)
    b64 = (len(data) + 2) // 3 * 4
    return "base64" if b64 < qp else "quoted-printable"


def build_mime(
    rendered: RenderedEmail,
    *,
    to_addr: str,
    to_name: str | None,
    from_addr: str | None,
    from_name: str | None,
    date: datetime | None = None,
) -> bytes:
    """The email for one recipient as CRLF MIME bytes. Raises ``MessageError``.

    ``date`` defaults to now (UTC); tests pass a fixed one.
    """
    subject = neutralise_encoded_words(_check_header_value("subject", rendered.subject))
    unsubscribe = _check_unsubscribe_url(rendered.unsubscribe_url)

    msg = EmailMessage(policy=POLICY)
    msg["From"] = _address(from_addr, from_name, "sender address")
    msg["To"] = _address(to_addr, to_name, "recipient address")
    msg["Subject"] = subject
    # Python decodes encoded words in what it is given; make sure nothing was (see the docstring).
    stored = str(msg["Subject"])
    if stored != subject or _has_control(stored):
        raise MessageError("The subject contains a line break or control character.")
    msg["Date"] = format_datetime(date or datetime.now(UTC))
    msg["Message-ID"] = make_msgid(domain=_host(unsubscribe))
    msg["MIME-Version"] = "1.0"
    msg.set_raw("List-Unsubscribe", f"<{unsubscribe}>")
    if unsubscribe.startswith("https://"):
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    msg.set_content(rendered.text, subtype="plain", charset="utf-8", cte=_cte(rendered.text))
    msg.add_alternative(rendered.html, subtype="html", charset="utf-8", cte=_cte(rendered.html))
    for part in msg.iter_parts():
        del part["MIME-Version"]  # add_alternative copies it onto a part; it belongs on top only
    return msg.as_bytes()


def recipients_of(mime: bytes) -> list[str]:
    """Every address in an address header other than From, lower-cased."""
    parsed = message_from_bytes(mime, policy=policy.default)
    found = []
    for name in _OTHER_ADDRESS_HEADERS:
        for header in parsed.get_all(name, []):
            found.extend(a.addr_spec.lower() for a in header.addresses)
    return found


def check_single_recipient(mime: bytes, recipient: str) -> None:
    """Raise ``MessageError`` unless ``recipient`` is the one and only addressee of ``mime``.

    Transports call this before sending, so a message can't go to anyone but the row it is for.
    """
    expected = ascii_address(recipient.strip(), "recipient address").lower()
    found = recipients_of(mime)
    if found != [expected]:
        raise MessageError("The message must be addressed to exactly its one recipient.")


def graph_payload(mime: bytes) -> str:
    """The ``/me/sendMail`` request body for ``mime``: base64, sent as ``Content-Type: text/plain``.

    Raises ``MessageError`` above ``GRAPH_MAX_REQUEST_BYTES``.
    """
    payload = base64.b64encode(mime).decode("ascii")
    if len(payload) > GRAPH_MAX_REQUEST_BYTES:
        raise MessageError("The email is too large to send through Microsoft Graph (over 4 MB).")
    return payload
