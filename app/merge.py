"""Merge fields: ``{{first_name}}``, ``{{email}}`` and ``{{first_name|Fallback}}``.

A small regex scanner, not a template engine: user text is never evaluated, only searched for
placeholders. Rules:

- Field names are case-insensitive and may have spaces or tabs around them: ``{{ First_Name }}``.
- ``{{name|Fallback}}`` uses the fallback when the value is blank (empty or whitespace).
  The fallback is trimmed and may contain spaces and punctuation, but not ``{``, ``}``, ``|``
  or a line break.
- Every ``{{`` must start a valid merge field. There is no way to write a literal ``{{``; anything
  else (``{{7*7}}``, ``{{first_name.__class__}}``, ``{{first_name}``) is reported as malformed, and
  ``{%`` (template tags) is reported too. Single braces and a stray ``}}`` are ordinary text.
- Rendering is a single pass, so a value that itself contains ``{{email}}`` is not expanded.

Markdown (``app/content.py``): ``validate`` the raw body exactly as typed, before ``protect``, so
error messages quote the user's own text. Then convert ``protect(body).text``. The tokens are plain
letters and digits, so Markdown can't split, emphasise, escape or percent-encode the placeholders
(it would otherwise escape ``&`` in a fallback twice, URL-encode ``{{`` in links and split ``|`` in
tables). ``content`` then splits the output at the tokens (``Protected.tokens``) and renders only
those placeholders per recipient, with ``mode="url"`` (percent-encoded, then HTML-escaped) inside
link targets. ``restore`` + ``render`` on a whole converted text also works where no URL context
is involved. See "Merge fields in the Markdown body" in docs/PLAN.md.
"""

import html
import re
import secrets
import unicodedata
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from app.recipients import Recipient

FIELDS = ("first_name", "email")
Mode = Literal["html", "text", "subject", "url"]

_FIELD_RE = re.compile(r"\{\{[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?:\|([^{}|\r\n]*))?\}\}")
_HINT = "write {{first_name}}, {{email}} or {{first_name|Fallback}}"
_SNIPPET_WINDOW = 80
# Bidi embeddings/overrides/isolates and invisible characters, removed from subjects. ZWNJ (U+200C)
# and ZWJ (U+200D) stay: Persian, Indic scripts and emoji sequences need them.
_SUBJECT_STRIP = re.compile("[\u202a-\u202e\u2066-\u2069\u200b\u2060\ufeff]")
# Characters left as they are in ``url`` mode, besides letters, digits and ``_.-~``. Only ``@``, so
# addresses stay readable in ``mailto:``; it is harmless because placeholders may only come after
# the host (see app/content.py). Everything else, ``/ ? # & = + %`` and spaces included, is encoded.
_URL_SAFE = "@"


class MergeError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class Placeholder:
    start: int
    end: int
    source: str  # the text as written, e.g. "{{ First_Name | Friend }}"
    name: str  # lower-cased field name
    fallback: str | None  # trimmed; None if there was no "|"


def _snippet(text: str, pos: int, closer: str = "}}") -> str:
    # A fixed window, so a huge single line with many errors stays linear.
    line = text[pos : pos + _SNIPPET_WINDOW].split("\n", 1)[0]
    close = line.find(closer)
    if 0 <= close <= 40:
        return line[: close + len(closer)]
    return line[:20] + ("…" if len(line) > 20 else "")


def _scan(text: str) -> tuple[list[Placeholder], list[str]]:
    """All well-formed placeholders (known or not) and error messages for malformed ones."""
    found: list[Placeholder] = []
    errors: list[str] = []
    pos = 0
    while (pos := text.find("{{", pos)) != -1:
        if m := _FIELD_RE.match(text, pos):
            fallback = m.group(2)
            found.append(
                Placeholder(
                    m.start(),
                    m.end(),
                    m.group(0),
                    m.group(1).lower(),
                    None if fallback is None else fallback.strip(),
                )
            )
            pos = m.end()
        else:
            errors.append(f"Malformed merge field {_snippet(text, pos)} — {_HINT}.")
            pos += 2
    for m in re.finditer(r"\{%", text):
        snippet = _snippet(text, m.start(), "%}")
        errors.append(f"Template tags like {snippet} are not supported.")
    return found, errors


def find_fields(text: str) -> list[Placeholder]:
    """The well-formed placeholders in ``text``, in order (unknown names included)."""
    return _scan(text)[0]


def validate(text: str) -> list[str]:
    """Human-readable problems with the merge fields in ``text``; empty if it can be rendered."""
    found, errors = _scan(text)
    available = ", ".join(FIELDS)
    for p in found:
        if p.name not in FIELDS:
            errors.append(f"Unknown field {p.source} — available: {available}.")
    return list(dict.fromkeys(errors))  # drop repeats, keep order


def recipient_values(recipient: Recipient) -> dict[str, str]:
    return {"first_name": recipient.first_name, "email": recipient.email}


def _clean_value(value: str) -> str:
    """Drop control characters (Unicode Cc) from a recipient value, except tab and line feed."""
    return "".join(c for c in value if c in "\t\n" or unicodedata.category(c) != "Cc")


def strip_invisible(value: str) -> str:
    """``value`` without bidi embeddings/overrides/isolates and invisible characters (not ZWJ/ZWNJ).

    Used for subjects and for display names in the MIME headers (``app/message.py``).
    """
    return _SUBJECT_STRIP.sub("", value)


def _clean_subject(value: str) -> str:
    value = re.sub(r"\s+", " ", value)  # CR, LF, tabs, U+2028 etc. become spaces
    value = "".join(c for c in value if unicodedata.category(c) != "Cc")
    value = strip_invisible(value)
    return re.sub(r" {2,}", " ", value).strip()


def render(text: str, values: Mapping[str, str | None], *, mode: Mode) -> str:
    """Fill in the placeholders. Raises ``MergeError`` if ``validate(text)`` reports anything.

    In every mode, control characters other than tab and line feed are removed from values (not
    from the text around them). ``html`` then escapes values and fallbacks, ``text`` inserts them
    as they are, and ``subject`` also removes control, bidi and invisible characters (keeping ZWJ
    and ZWNJ) and collapses whitespace in the whole result. ``url`` percent-encodes values and
    fallbacks (UTF-8) for use inside a link target; it does not HTML-escape, so escape the whole
    URL when putting it in an attribute.
    """
    if mode not in ("html", "text", "subject", "url"):
        raise ValueError(f"Unknown mode {mode!r}")
    if errors := validate(text):
        raise MergeError(errors)
    out: list[str] = []
    pos = 0
    for p in find_fields(text):
        value = _clean_value(values.get(p.name) or "").strip()
        if not value and p.fallback is not None:
            value = p.fallback
        if mode == "html":
            value = html.escape(value)
        elif mode == "url":
            value = urllib.parse.quote(value, safe=_URL_SAFE)
        out += [text[pos : p.start], value]
        pos = p.end
    out.append(text[pos:])
    result = "".join(out)
    return _clean_subject(result) if mode == "subject" else result


@dataclass(frozen=True)
class Protected:
    """Text with each placeholder swapped for an opaque token; see the module docstring."""

    text: str
    tokens: dict[str, str]  # token -> original placeholder source

    def restore(self, converted: str) -> str:
        for token, source in self.tokens.items():
            converted = converted.replace(token, source)
        return converted


def protect(text: str) -> Protected:
    """Swap placeholders for letter-and-digit tokens that Markdown leaves alone."""
    nonce = secrets.token_hex(4).upper()
    while f"MF{nonce}" in text:
        nonce = secrets.token_hex(4).upper()
    out: list[str] = []
    tokens: dict[str, str] = {}
    pos = 0
    for i, p in enumerate(find_fields(text)):
        token = f"MF{nonce}Z{i}Z"
        tokens[token] = p.source
        out += [text[pos : p.start], token]
        pos = p.end
    out.append(text[pos:])
    return Protected("".join(out), tokens)
