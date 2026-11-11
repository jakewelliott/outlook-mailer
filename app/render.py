"""The finished email: the Christmas HTML template around the body, and the plain-text version.

``render_email`` puts one recipient's body fragment (from ``content.PreparedBody.for_recipient``)
into ``templates/email/christmas.html.j2`` (Cerberus-based, see ``LICENSE-cerberus.txt``), then
inlines the CSS with css-inline. The ``<style>`` blocks are kept for media queries and dark mode.
``render_text`` builds the plain-text part with the same footer.

Every email carries the CAN-SPAM footer (``FooterInfo``): business name, postal address, the
advertisement notice (on by default), why the recipient gets the email, and the unsubscribe link.

Images come from ``app/static/email-img`` and are served publicly at ``/img/<name>`` (see
``main.py``); emails link to them with absolute ``PUBLIC_BASE_URL/img/...`` URLs, never ``data:``
URIs or CID attachments. File names carry a version (``-v1``) because the files are cached as
immutable: to change an image, add a new file name rather than replacing the old file, so emails
already sent keep their images.
"""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import css_inline
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

from app.content import DEFAULT_THEME, Theme

APP_DIR = Path(__file__).parent
EMAIL_TEMPLATES_DIR = APP_DIR / "templates" / "email"
EMAIL_IMG_DIR = APP_DIR / "static" / "email-img"
IMG_PATH = "/img/"
TEMPLATE = "christmas.html.j2"

# name in the template -> file in EMAIL_IMG_DIR
IMAGES = {"banner": "christmas-banner-v1.jpg", "holly": "holly-v1.png"}

DEFAULT_HEADLINE = "Merry Christmas"
DEFAULT_TAGLINE = "and a Happy New Year"
DEFAULT_REASON = (
    "You're receiving this email because you're a customer of {name} or signed up for our emails."
)

# Colours around the body (the body's own colours come from ``content.Theme``).
LIGHT = {
    "page": "#f4ecdc",  # cream
    "card": "#fffaf1",  # lighter cream card
    "pine_dark": "#0e261a",  # header band behind the photo (matches its dark left side)
    "gold": "#c9a227",
    "gold_light": "#e8c96a",
    "cream": "#fdf6e6",
    "footer": "#8c1c13",  # deep red
    "footer_text": "#fdf3e1",
}
DARK = {
    "page": "#15110d",
    "card": "#241c16",
    "text": "#efe6d6",
    "heading": "#f2a79c",
    "link": "#9fd8b3",
    "footer": "#5e1510",
    "footer_text": "#f6e9d2",
}

# Zero-width non-joiner + no-break space after the preheader, so mail apps don't pull body text
# into the inbox preview. Real characters, not entities, so autoescape leaves them alone.
PREHEADER_SPACER = "\u200c\u00a0" * 40

_BASE_URL = re.compile(r"https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?")
_URL = re.compile(r"https?://[^\s\"'<>\\`]+")
_MAX_NAME, _MAX_ADDRESS, _MAX_ADDRESS_LINES, _MAX_REASON = 100, 300, 6, 300


class FooterError(ValueError):
    """The footer can't be used; ``errors`` are messages for Jake."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _has_control(text: str, allow: str = "") -> bool:
    return any(unicodedata.category(ch).startswith("C") and ch not in allow for ch in text)


@dataclass(frozen=True)
class FooterInfo:
    """The CAN-SPAM footer. Values are plain text; they are escaped when rendered.

    ``postal_address`` may have several lines. A blank ``reason`` uses ``DEFAULT_REASON``.
    Surrounding spaces and blank address lines are removed. Raises ``FooterError`` listing
    every problem (business name and postal address are required).
    """

    business_name: str
    postal_address: str
    is_advertisement: bool = True
    reason: str = ""

    def __post_init__(self) -> None:
        name = " ".join(self.business_name.split()) if self.business_name else ""
        lines = [" ".join(line.split()) for line in (self.postal_address or "").splitlines()]
        address = "\n".join(line for line in lines if line)
        reason = " ".join(self.reason.split()) if self.reason else ""
        errors = []
        if not name:
            errors.append("The business or sender name is required.")
        elif len(name) > _MAX_NAME:
            errors.append(f"The business name is too long (at most {_MAX_NAME} characters).")
        if not address:
            errors.append("The postal address is required (CAN-SPAM).")
        elif len(address) > _MAX_ADDRESS or address.count("\n") >= _MAX_ADDRESS_LINES:
            errors.append(
                f"The postal address is too long (at most {_MAX_ADDRESS_LINES} lines and "
                f"{_MAX_ADDRESS} characters)."
            )
        if len(reason) > _MAX_REASON:
            errors.append(
                f"The 'why you're receiving this' text is too long (at most {_MAX_REASON})."
            )
        for label, value in (
            ("business name", name),
            ("postal address", address),
            ("reason", reason),
        ):
            if _has_control(value, allow="\n"):
                errors.append(f"The {label} contains a control character.")
        if errors:
            raise FooterError(errors)
        object.__setattr__(self, "business_name", name)
        object.__setattr__(self, "postal_address", address)
        object.__setattr__(self, "reason", reason)

    @property
    def address_lines(self) -> list[str]:
        return self.postal_address.split("\n")

    @property
    def reason_text(self) -> str:
        return self.reason or DEFAULT_REASON.format(name=self.business_name)


def base_url(public_base_url: str) -> str:
    """``PUBLIC_BASE_URL`` without a trailing slash, checked to be a plain http(s) URL.

    The URL ends up inside CSS ``url('...')`` and Outlook conditional comments, so only plain
    characters are accepted (``config.py`` already validated it; this is a backstop).
    """
    url = public_base_url.rstrip("/")
    if not _BASE_URL.fullmatch(url):
        raise ValueError(f"public_base_url must be a plain http(s) address, got {url!r}")
    return url


def image_urls(public_base_url: str) -> dict[str, str]:
    base = base_url(public_base_url)
    return {key: f"{base}{IMG_PATH}{name}" for key, name in IMAGES.items()}


def _check_unsubscribe_url(url: str) -> str:
    if not _URL.fullmatch(url):
        raise ValueError(f"unsubscribe_url must be an absolute http(s) URL, got {url!r}")
    return url


_env = Environment(
    loader=FileSystemLoader(EMAIL_TEMPLATES_DIR),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


_CSS_UNSAFE = re.compile(r"[<>{};\\\"&]|/\*|\*/")


def _css(value: str) -> Markup:
    """A theme colour or font stack, unescaped so it works inside ``<style>`` (where ``&#39;``
    would break ``'Times New Roman'``). Only for values from code, never user text: anything
    that could end a declaration, rule or the element is refused."""
    if _CSS_UNSAFE.search(value):
        raise ValueError(f"unsafe CSS value {value!r}")
    return Markup(value)


_env.filters["css"] = _css
# Keep the <style> blocks (media queries and dark mode can't be inlined); never fetch anything.
_inliner = css_inline.CSSInliner(keep_style_tags=True, load_remote_stylesheets=False)


def render_email(
    prepared_body_html: str,
    *,
    subject: str,
    preheader: str,
    footer: FooterInfo,
    unsubscribe_url: str,
    public_base_url: str,
    theme: Theme = DEFAULT_THEME,
    headline: str = DEFAULT_HEADLINE,
    tagline: str = DEFAULT_TAGLINE,
    divider: bool = True,
) -> str:
    """The complete HTML document for one recipient.

    ``prepared_body_html`` must be the trusted fragment from ``content.PreparedBody`` (already
    escaped and merged); it is inserted as-is. All other values are plain text and are escaped.
    ``theme`` should be the one the body was prepared with, so colours match.
    """
    html = _env.get_template(TEMPLATE).render(
        body=Markup(prepared_body_html),
        subject=subject,
        preheader=preheader.strip(),
        preheader_spacer=PREHEADER_SPACER,
        headline=headline,
        tagline=tagline,
        divider=divider,
        business_name=footer.business_name,
        address_lines=footer.address_lines,
        is_advertisement=footer.is_advertisement,
        reason=footer.reason_text,
        unsubscribe_url=_check_unsubscribe_url(unsubscribe_url),
        img=image_urls(public_base_url),
        theme=theme,
        c=LIGHT,
        dark=DARK,
    )
    return _inliner.inline(html)


def render_text(body_text: str, footer: FooterInfo, unsubscribe_url: str) -> str:
    """The plain-text part: the body, then the same footer as the HTML."""
    lines = [body_text.rstrip(), "", "--", footer.business_name, *footer.address_lines, ""]
    if footer.is_advertisement:
        lines.append("This message is an advertisement.")
    lines += [footer.reason_text, "", f"Unsubscribe: {_check_unsubscribe_url(unsubscribe_url)}"]
    return "\n".join(lines) + "\n"
