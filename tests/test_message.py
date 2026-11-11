"""MIME builder: structure, encodings, headers, one recipient only, injection, Graph payload."""

import base64
import dataclasses
import re
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses

import pytest

from app import compose, message
from app.compose import DraftContent, RenderedEmail
from app.message import MessageError, build_mime, check_single_recipient, graph_payload

BASE = "https://mail.example.com"
SECRET = "x" * 32
GOOD = DraftContent(
    subject="Merry Christmas, {{first_name|Friend}}",
    preheader="Hours and wreaths for {{first_name|you}}",
    body="Dear {{first_name|Friend}},\n\n**Thanks** for a great year.",
    business_name="The Calico Barn",
    postal_address="123 Barn Road\nSpringfield, NC 27000",
)
WHEN = datetime(2026, 12, 1, 15, 30, tzinfo=UTC)


def rendered(first_name: str = "Ada", email: str = "ada@example.com") -> RenderedEmail:
    return compose.render_message(
        GOOD,
        {"first_name": first_name, "email": email},
        public_base_url=BASE,
        unsubscribe_secret=SECRET,
    )


def build(r: RenderedEmail | None = None, **kw: object) -> bytes:
    args: dict[str, object] = {
        "to_addr": "ada@example.com",
        "to_name": "Ada",
        "from_addr": "jake@outlook.com",
        "from_name": "The Calico Barn",
        "date": WHEN,
        **kw,
    }
    return build_mime(r or rendered(), **args)  # type: ignore[arg-type]


def parse(raw: bytes) -> EmailMessage:
    msg = message_from_bytes(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)
    return msg


def lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def raw_headers(raw: bytes) -> str:
    return raw.split(b"\r\n\r\n", 1)[0].decode("ascii")


# --- structure and bodies --------------------------------------------------------------------


def test_multipart_alternative_text_then_html() -> None:
    r = rendered()
    msg = parse(build(r))
    assert msg.get_content_type() == "multipart/alternative"
    parts = list(msg.iter_parts())
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    for part in parts:
        assert part.get_content_charset() == "utf-8"
        assert part["Content-Transfer-Encoding"] in ("quoted-printable", "base64")
        assert "MIME-Version" not in part
    assert lf(parts[0].get_content()) == r.text.rstrip("\n") + "\n"
    assert lf(parts[1].get_content()) == r.html.rstrip("\n") + "\n"
    assert not msg.defects and not any(p.defects for p in parts)


def test_crlf_line_endings_and_short_lines() -> None:
    raw = build()
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert raw.isascii()  # 7-bit clean
    for line in raw.split(b"\r\n"):
        if line.startswith(b"List-Unsubscribe: "):
            assert len(line) <= message.MAX_LINE
        else:
            assert len(line) <= 78, line


def test_long_body_lines_are_folded() -> None:
    long = "word " * 400 + "é" * 300  # one 2300-character line
    r = RenderedEmail(subject="s", html=f"<p>{long}</p>", text=long, unsubscribe_url=f"{BASE}/u/t")
    raw = build(r)
    assert max(len(line) for line in raw.split(b"\r\n")) <= 78
    parts = list(parse(raw).iter_parts())
    assert lf(parts[0].get_content()) == long + "\n"
    assert lf(parts[1].get_content()) == f"<p>{long}</p>\n"


def test_quoted_printable_for_latin_text_base64_for_other_scripts() -> None:
    latin = RenderedEmail(subject="s", html="<p>Zoë</p>", text="Zoë", unsubscribe_url=f"{BASE}/u/t")
    assert {p["Content-Transfer-Encoding"] for p in parse(build(latin)).iter_parts()} == {
        "quoted-printable"
    }
    cjk = "圣诞快乐" * 200
    other = RenderedEmail(subject="s", html=cjk, text=cjk, unsubscribe_url=f"{BASE}/u/t")
    parts = list(parse(build(other)).iter_parts())
    assert {p["Content-Transfer-Encoding"] for p in parts} == {"base64"}
    assert lf(parts[0].get_content()) == cjk + "\n"


# --- headers ---------------------------------------------------------------------------------


def test_headers() -> None:
    r = rendered()
    raw = build(r)
    msg = parse(raw)
    assert msg["Subject"] == "Merry Christmas, Ada"
    assert msg["Date"].datetime == WHEN
    assert msg["MIME-Version"] == "1.0"
    assert re.fullmatch(r"<[^<>@\s]+@mail\.example\.com>", msg["Message-ID"])
    assert msg["From"].addresses[0].addr_spec == "jake@outlook.com"
    assert msg["From"].addresses[0].display_name == "The Calico Barn"
    assert msg["List-Unsubscribe"] == f"<{r.unsubscribe_url}>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    for name in ("Cc", "Bcc", "Reply-To", "Sender"):
        assert name not in msg


def test_message_ids_are_unique() -> None:
    assert parse(build())["Message-ID"] != parse(build())["Message-ID"]


def test_default_date_is_now() -> None:
    raw = build_mime(rendered(), to_addr="ada@example.com", to_name=None, from_addr=None,
                     from_name=None)  # fmt: skip
    sent = parse(raw)["Date"].datetime
    assert abs((datetime.now(UTC) - sent).total_seconds()) < 60


def test_list_unsubscribe_is_one_raw_line_even_when_long() -> None:
    # Python's folder would turn a long URL into RFC 2047 encoded words (which it decodes again
    # when parsing, so only the raw bytes show it). Mail apps don't decode that header.
    email = "a" * 60 + "@" + ".".join(c * 55 for c in "bcd") + ".example.com"
    r = rendered(email=email)
    assert len(r.unsubscribe_url) > 300
    headers = raw_headers(build(r, to_addr=email))
    assert f"\r\nList-Unsubscribe: <{r.unsubscribe_url}>\r\n" in headers
    assert "=?" not in headers.split("List-Unsubscribe: ", 1)[1].split("\r\n", 1)[0]


def test_no_one_click_header_for_http_links() -> None:
    r = RenderedEmail(subject="s", html="h", text="t", unsubscribe_url="http://localhost:8765/u/t")
    msg = parse(build(r))
    assert msg["List-Unsubscribe"] == "<http://localhost:8765/u/t>"
    assert "List-Unsubscribe-Post" not in msg
    assert msg["Message-ID"].endswith("@localhost>")


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://mail.example.com/u/a b",
        "https://mail.example.com/u/a>b",
        "https://mail.example.com/u/é",
        "https://mail.example.com/u/" + "a" * 1000,
        "https://mail.example.com/u/t\r\nBcc: evil@example.com",
    ],
)
def test_bad_unsubscribe_url_refused(url: str) -> None:
    with pytest.raises(MessageError):
        build(RenderedEmail(subject="s", html="h", text="t", unsubscribe_url=url))


def test_from_placeholder_when_sender_unknown() -> None:
    msg = parse(build(from_addr=None, from_name=None))
    assert str(msg["From"]) == message.PLACEHOLDER_FROM


def test_non_ascii_subject_round_trips_encoded() -> None:
    r = rendered(first_name="Zoë 🎄")
    raw = build(r)
    assert "=?utf-8?" in raw_headers(raw).split("Subject:", 1)[1].split("\r\n", 1)[0]
    assert parse(raw)["Subject"] == "Merry Christmas, Zoë 🎄"


# --- recipient -------------------------------------------------------------------------------


def all_header_text(msg: EmailMessage) -> dict[str, str]:
    return {k.lower(): str(v) for k, v in msg.items()}


@pytest.mark.parametrize(
    "name",
    [
        "Ada",
        "Zoë",
        "José María",
        "李小龍",
        '"Evil, <x@y>" ',
        "Evil, <x@y>",
        'O"Brien \\ "quoted"',
        "Smith, John",
        "bob@evil.example, carol@evil.example",
        "=?utf-8?q?hi?=",
        "Évil, <x@y> 🎄",
    ],
)
def test_display_name_is_quoted_and_only_one_recipient(name: str) -> None:
    raw = build(to_name=name)
    msg = parse(raw)
    (addr,) = msg["To"].addresses
    assert addr.addr_spec == "ada@example.com"
    expected = " ".join(re.sub(r'["<>,;:@\\()\[\]]', " ", name).split()).replace("=?", "= ?")
    assert addr.display_name == expected
    assert message.recipients_of(raw) == ["ada@example.com"]
    check_single_recipient(raw, "ada@example.com")
    # The address shows up in To and nowhere else in the headers.
    holders = [k for k, v in all_header_text(msg).items() if "ada@example.com" in v.lower()]
    assert holders == ["to"]
    assert not msg.defects


@pytest.mark.parametrize("name", ["Jöhn, <evil@x.com>", 'Zoë "evil@x.com" ;'])
def test_non_ascii_name_hides_no_address_from_decode_first_parsers(name: str) -> None:
    raw = build(to_name=name)
    to = raw_headers(raw).split("\r\nTo: ", 1)[1].split("\r\n", 1)[0]
    assert "=?utf-8?" in to
    # A parser that decodes the encoded words first and then looks for addresses.
    decoded = str(make_header(decode_header(to)))
    assert getaddresses([decoded], strict=False) == [(decoded.split(" <")[0], "ada@example.com")]
    assert "evil" in decoded  # the name is kept, minus its punctuation
    assert message.recipients_of(raw) == ["ada@example.com"]


def test_bidi_and_invisible_characters_removed_from_names() -> None:
    name = "\u202eAda\u200b \u2066Lovelace\u2069\ufeff Zo\u200de\u200c"
    (addr,) = parse(build(to_name=name))["To"].addresses
    assert addr.display_name == "Ada Lovelace Zo\u200de\u200c"


def test_blank_name_gives_bare_address() -> None:
    for name in (None, "", "   "):
        assert str(parse(build(to_name=name))["To"]) == "ada@example.com"


def test_idn_domain_is_punycoded() -> None:
    raw = build(to_addr="ada@bücher.example", to_name="Ada")
    assert parse(raw)["To"].addresses[0].addr_spec == "ada@xn--bcher-kva.example"
    check_single_recipient(raw, "ada@bücher.example")


@pytest.mark.parametrize(
    "to_addr",
    [
        "zoë@example.com",  # needs SMTPUTF8
        "not-an-address",
        "ada@example.com, eve@example.com",
        "Ada <ada@example.com>",
        "ada@example.com\r\nBcc: eve@example.com",
        "",
    ],
)
def test_bad_recipient_address_refused(to_addr: str) -> None:
    with pytest.raises(MessageError):
        build(to_addr=to_addr)


@pytest.mark.parametrize("field", ["to_name", "from_name"])
@pytest.mark.parametrize(
    "value",
    [
        "Ada\r\nBcc: eve@example.com",
        "Ada\nBcc: eve@example.com",
        "Ada\rX",
        "Ada\x00",
        "Ada\tB",
        "Ada\u2028Bcc: eve@example.com",
        "Ada\x85X",
    ],
)
def test_header_injection_in_names_refused(field: str, value: str) -> None:
    with pytest.raises(MessageError):
        build(**{field: value})


def test_header_injection_in_subject_refused() -> None:
    r = RenderedEmail(
        subject="Hi\r\nBcc: eve@example.com", html="h", text="t", unsubscribe_url=f"{BASE}/u/t"
    )
    with pytest.raises(MessageError):
        build(r)


@pytest.mark.parametrize(
    "subject",
    [
        "Hi =?utf-8?q?x=0D=0ABcc:_evil@x.com?=",
        "Hi =?utf-8?q?=00?=",
        "Hi =?utf-8?q?a=E2=80=A8b?=",
    ],
)
def test_encoded_words_in_subject_are_not_decoded(subject: str) -> None:
    r = RenderedEmail(subject=subject, html="h", text="t", unsubscribe_url=f"{BASE}/u/t")
    raw = build(r)
    msg = parse(raw)
    assert msg["Subject"] == subject.replace("=?", "= ?")
    assert "Bcc" not in msg and "\x00" not in msg["Subject"] and "\u2028" not in msg["Subject"]
    assert message.recipients_of(raw) == ["ada@example.com"]


def test_encoded_word_from_merge_value_reaches_subject_as_previewed() -> None:
    first = "=?utf-8?q?x=0D=0ABcc:_evil@x.com?="
    r = rendered(first_name=first)
    assert r.subject == "Merry Christmas, = ?utf-8?q?x=0D=0ABcc:_evil@x.com?="
    msg = parse(build(r))
    assert msg["Subject"] == r.subject
    assert "Bcc" not in msg


def test_literal_encoded_word_marker_in_subject_sent_as_previewed() -> None:
    c = dataclasses.replace(GOOD, subject="Sale =?50% off?= for {{first_name}}")
    r = compose.render_message(
        c,
        {"first_name": "Ada", "email": "ada@example.com"},
        public_base_url=BASE,
        unsubscribe_secret=SECRET,
    )
    assert r.subject == "Sale = ?50% off?= for Ada"
    assert parse(build(r))["Subject"] == r.subject


def test_subject_changed_by_header_parsing_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Belt and braces: if neutralising ever missed a case, the post-assignment check catches it.
    monkeypatch.setattr(message, "neutralise_encoded_words", lambda text: text)
    r = RenderedEmail(
        subject="Hi =?utf-8?q?x=0D=0ABcc:_evil@x.com?=",
        html="h",
        text="t",
        unsubscribe_url=f"{BASE}/u/t",
    )
    with pytest.raises(MessageError):
        build(r)


def test_ipv6_unsubscribe_host_refused() -> None:
    r = RenderedEmail(subject="s", html="h", text="t", unsubscribe_url="https://[::1]:8765/u/t")
    with pytest.raises(MessageError):
        build(r)


def test_check_single_recipient() -> None:
    raw = build()
    check_single_recipient(raw, "ADA@example.com ")
    with pytest.raises(MessageError):
        check_single_recipient(raw, "eve@example.com")
    extra = raw.replace(b"\r\nTo: ", b"\r\nCc: eve@example.com\r\nTo: ", 1)
    with pytest.raises(MessageError):
        check_single_recipient(extra, "ada@example.com")
    two = raw.replace(b"To: Ada <ada@example.com>", b"To: ada@example.com, eve@example.com", 1)
    assert two != raw
    with pytest.raises(MessageError):
        check_single_recipient(two, "ada@example.com")


# --- Graph payload ---------------------------------------------------------------------------


def test_graph_payload_is_base64_of_the_mime() -> None:
    raw = build()
    payload = graph_payload(raw)
    assert re.fullmatch(r"[A-Za-z0-9+/]+=*", payload)
    assert base64.b64decode(payload, validate=True) == raw


def test_largest_email_fits_graph_request_limit() -> None:
    # Worst case for size: 102 KB of HTML and a long text, all non-ASCII (3x in quoted-printable
    # before the switch to base64), then base64 again for Graph.
    limit = compose.SIZE_LIMIT_BYTES
    html = "é" * (limit // 2)
    text = "é" * (compose.MAX_BODY)
    r = RenderedEmail(subject="s" * 250, html=html, text=text, unsubscribe_url=f"{BASE}/u/t")
    payload = graph_payload(build(r))
    assert len(payload) < message.GRAPH_MAX_REQUEST_BYTES
    assert len(payload) < 1024 * 1024  # in fact well under 1 MB


def test_real_max_size_email_fits() -> None:
    body = ("Lots of **news** & offers, {{first_name|Friend}}. " * 40 + "\n\n") * 40
    c = dataclasses.replace(GOOD, body=body)
    r = compose.render_message(
        c, {"first_name": "Ada", "email": "ada@example.com"},
        public_base_url=BASE, unsubscribe_secret=SECRET,
    )  # fmt: skip
    assert len(r.html.encode()) > 80 * 1024
    raw = build(r)
    assert lf(list(parse(raw).iter_parts())[1].get_content()) == r.html.rstrip("\n") + "\n"
    assert len(graph_payload(raw)) < message.GRAPH_MAX_REQUEST_BYTES


def test_graph_payload_too_large_refused() -> None:
    with pytest.raises(MessageError):
        graph_payload(b"x" * (message.GRAPH_MAX_REQUEST_BYTES // 4 * 3 + 3))
