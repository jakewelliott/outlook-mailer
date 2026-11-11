"""Email template lint, footer and snapshot tests, and the public /img/ route.

Snapshot: ``tests/snapshots/christmas.html`` is the expected HTML for ``SAMPLE_BODY`` /
``SAMPLE_FOOTER``. After an intended template change, regenerate it (and ``docs/sample-email.html``,
the same sample with the real domain, for looking at in a browser) with::

    UPDATE_SNAPSHOTS=1 pytest tests/test_render.py

then review the diff before committing.
"""

import dataclasses
import os
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import content, render
from app.config import Settings
from app.main import create_app
from app.render import FooterError, FooterInfo, render_email, render_text

ROOT = Path(__file__).parent.parent
SNAPSHOT = Path(__file__).parent / "snapshots" / "christmas.html"
SAMPLE_DOC = ROOT / "docs" / "sample-email.html"
BASE = "https://mail.example.com"
REAL_BASE = "https://the-calico-barn.jakewelliott.com"
UNSUB = f"{BASE}/u/am9AZXhhbXBsZS5jb20.c2lnbmF0dXJl"

SAMPLE_BODY = (
    "Dear {{first_name|Friend}},\n\n"
    "Thank you for a **wonderful year** at The Calico Barn! As the snow settles over the fields, "
    "we wanted to share a little *holiday cheer* and a few things happening at the barn this "
    "December.\n\n"
    "## Holiday hours\n\n"
    "- Saturdays in December: 9am to 5pm\n"
    "- Christmas Eve: 9am to noon\n"
    "- Closed Christmas Day and ~~Boxing Day~~ the 26th\n\n"
    "## Gifts from the barn\n\n"
    "Our hand-quilted table runners and ++fresh pine wreaths++ are back. Browse them at "
    "[our shop](https://the-calico-barn.jakewelliott.com/shop) or stop by for a cup of cider.\n\n"
    "Wishing you a merry Christmas and a happy New Year,\n"
    "Jake and everyone at The Calico Barn\n"
)
SAMPLE_FOOTER = FooterInfo(
    business_name="The Calico Barn",
    postal_address="123 Barn Road\nPO Box 45\nSpringfield, NC 27000",
)
SAMPLE_VALUES = {"first_name": "Ada", "email": "ada@example.com"}


def sample_html(base: str = BASE, unsub: str = UNSUB) -> str:
    fragment, _ = content.prepare(SAMPLE_BODY).for_recipient(SAMPLE_VALUES)
    return render_email(
        fragment,
        subject="Merry Christmas from The Calico Barn",
        preheader="Holiday hours, wreaths and a thank-you from the barn",
        footer=SAMPLE_FOOTER,
        unsubscribe_url=unsub,
        public_base_url=base,
    )


# --- a tiny lint parser ------------------------------------------------------------------------


class Lint(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.styles: list[str] = []
        self.comments: list[str] = []
        self.text: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self._in_style = tag == "style"

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        (self.styles if self._in_style else self.text).append(data)

    def handle_comment(self, data: str) -> None:
        self.comments.append(data)

    def all(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.tags if name == tag]


def lint(html: str) -> Lint:
    parser = Lint()
    parser.feed(html)
    parser.close()
    return parser


def links(html: str) -> dict[str, str | None]:
    """Link text -> href for every <a> (text collected up to the closing tag)."""
    found: dict[str, str | None] = {}

    class P(HTMLParser):
        href: str | None = None
        buf: list[str] = []  # noqa: RUF012

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "a":
                self.href, self.buf = dict(attrs).get("href"), []

        def handle_data(self, data: str) -> None:
            if self.href is not None:
                self.buf.append(data)

        def handle_endtag(self, tag: str) -> None:
            if tag == "a" and self.href is not None:
                found["".join(self.buf).strip()] = self.href
                self.href = None

    P(convert_charrefs=True).feed(html)
    return found


def visible_text(html: str) -> str:
    return " ".join(" ".join(lint(html).text).split())


# --- lint ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def html() -> str:
    return sample_html()


def test_no_scripts_external_css_or_fonts(html: str) -> None:
    doc = lint(html)
    assert not doc.all("script")
    assert not doc.all("link")
    assert not doc.all("iframe") and not doc.all("object") and not doc.all("embed")
    css = "\n".join(doc.styles).lower()
    assert "@import" not in css and "@font-face" not in css
    assert "fonts.googleapis" not in html
    assert not any(k.startswith("on") for _, attrs in doc.tags for k in attrs)


def test_no_data_uris(html: str) -> None:
    assert "data:" not in html.lower()


def test_images_have_alt_size_and_absolute_hosted_src(html: str) -> None:
    imgs = lint(html).all("img")
    assert imgs
    for img in imgs:
        assert img.get("alt") is not None
        assert (img.get("width") or "").isdigit() and (img.get("height") or "").isdigit()
        assert (img.get("src") or "").startswith(f"{BASE}/img/")


def test_every_image_reference_is_hosted(html: str) -> None:
    """Background images (CSS, ``background=`` and VML in Outlook comments) too."""
    refs = re.findall(r"url\(['\"]?([^'\")]+)", html)
    refs += re.findall(r'\b(?:src|background)="([^"]+)"', html)
    assert any("christmas-banner" in r for r in refs)
    for ref in refs:
        assert ref.startswith(f"{BASE}/img/"), ref
        name = ref.removeprefix(f"{BASE}/img/")
        assert (render.EMAIL_IMG_DIR / name).is_file(), name


def test_size_budget_with_long_body() -> None:
    para = (
        "This is a fairly long paragraph about **our holiday season**, with a "
        "[link](https://example.com/page) and some *emphasis* so it looks like a real message. "
    ) * 4
    items = "\n".join(f"- Item number {i} with a few words" for i in range(12))
    body = "\n\n".join([f"## Section {i}\n\n{para}\n\n{items}" for i in range(14)])
    fragment, _ = content.prepare(body).for_recipient(SAMPLE_VALUES)
    out = render_email(
        fragment,
        subject="s",
        preheader="p",
        footer=SAMPLE_FOOTER,
        unsubscribe_url=UNSUB,
        public_base_url=BASE,
    )
    assert len(fragment) > 40_000  # really a long body
    assert len(out.encode()) < 102 * 1024


def test_template_overhead_is_small(html: str) -> None:
    fragment, _ = content.prepare(SAMPLE_BODY).for_recipient(SAMPLE_VALUES)
    assert len(html.encode()) - len(fragment.encode()) < 20 * 1024


def test_style_blocks_kept_for_dark_mode_and_media_queries(html: str) -> None:
    css = "\n".join(lint(html).styles)
    assert "prefers-color-scheme: dark" in css
    assert "[data-ogsc]" in css and "[data-ogsb]" in css
    assert "max-width: 600px" in css
    assert '<meta name="color-scheme" content="light dark">' in html


def test_css_is_inlined(html: str) -> None:
    doc = lint(html)
    (name,) = [a for t, a in doc.tags if t == "p" and "hero-name" in (a.get("class") or "")]
    assert "letter-spacing: 3px" in (name.get("style") or "")


def test_outlook_techniques_kept(html: str) -> None:
    comments = "\n".join(lint(html).comments)
    assert "o:PixelsPerInch" in comments and "OfficeDocumentSettings" in comments
    assert "v:rect" in comments and "v:fill" in comments
    assert '[if mso]>\n<table align="center"' in html
    assert 'xmlns:v="urn:schemas-microsoft-com:vml"' in html


def test_body_and_preheader_present(html: str) -> None:
    text = visible_text(html)
    assert "Dear Ada," in text
    assert "Holiday hours, wreaths and a thank-you from the barn" in text
    assert "<title>Merry Christmas from The Calico Barn</title>" in html


def test_no_preheader_block_when_blank() -> None:
    out = render_email(
        "<p>x</p>",
        subject="s",
        preheader="  ",
        footer=SAMPLE_FOOTER,
        unsubscribe_url=UNSUB,
        public_base_url=BASE,
    )
    assert "‌" not in out


# --- footer ----------------------------------------------------------------------------------


def test_footer_contents(html: str) -> None:
    text = visible_text(html)
    assert "The Calico Barn" in text
    assert "123 Barn Road" in text and "PO Box 45" in text and "Springfield, NC 27000" in text
    assert "This message is an advertisement." in text
    assert "because you're a customer of The Calico Barn" in text
    assert links(html)["Unsubscribe"] == UNSUB
    assert "mailto:" not in html


def test_address_lines_become_line_breaks(html: str) -> None:
    assert "123 Barn Road<br>PO Box 45<br>Springfield, NC 27000" in html


def test_advertisement_line_toggles() -> None:
    footer = FooterInfo("Shop", "1 Road", is_advertisement=False, reason="You asked for it.")
    out = render_email(
        "<p>x</p>",
        subject="s",
        preheader="",
        footer=footer,
        unsubscribe_url=UNSUB,
        public_base_url=BASE,
    )
    assert "advertisement" not in out
    assert "You asked for it." in visible_text(out)
    assert "advertisement" not in render_text("x", footer, UNSUB)


def test_unsubscribe_url_is_escaped() -> None:
    url = f"{BASE}/u/tok?a=1&b=2"
    out = render_email(
        "<p>x</p>",
        subject="s",
        preheader="",
        footer=SAMPLE_FOOTER,
        unsubscribe_url=url,
        public_base_url=BASE,
    )
    assert 'href="https://mail.example.com/u/tok?a=1&amp;b=2"' in out
    assert links(out)["Unsubscribe"] == url


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "/u/relative", f'{BASE}/u/x"onclick="y', f"{BASE}/u/a b", ""]
)
def test_bad_unsubscribe_url_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        render_email(
            "<p>x</p>",
            subject="s",
            preheader="",
            footer=SAMPLE_FOOTER,
            unsubscribe_url=url,
            public_base_url=BASE,
        )
    with pytest.raises(ValueError):
        render_text("x", SAMPLE_FOOTER, url)


@pytest.mark.parametrize("base", ["ftp://x.com", "https://x.com/'", "https://x.com/<a>", "x"])
def test_bad_public_base_url_rejected(base: str) -> None:
    with pytest.raises(ValueError):
        render_email(
            "<p>x</p>",
            subject="s",
            preheader="",
            footer=SAMPLE_FOOTER,
            unsubscribe_url=UNSUB,
            public_base_url=base,
        )


def test_trailing_slash_in_base_url() -> None:
    assert render.image_urls(BASE + "/")["holly"] == f"{BASE}/img/holly-v1.png"


def test_hostile_values_are_escaped() -> None:
    evil = '<script>alert(1)</script>"><img src=x onerror=alert(1)>'
    footer = FooterInfo(evil, f"{evil}\n2 Road", reason=evil)
    fragment, _ = content.prepare("Hi {{first_name}} <b>raw</b>").for_recipient(
        {"first_name": evil, "email": "a@b.c"}
    )
    out = render_email(
        fragment,
        subject=evil,
        preheader=evil,
        footer=footer,
        unsubscribe_url=UNSUB,
        public_base_url=BASE,
        headline=evil,
        tagline=evil,
    )
    doc = lint(out)
    assert not doc.all("script")
    assert not doc.all("b")
    assert len(doc.all("img")) == 1  # only the holly
    assert not any(k.startswith("on") for _, attrs in doc.tags for k in attrs)
    assert visible_text(out).count("<script>alert(1)</script>") >= 6


def test_footer_validation() -> None:
    with pytest.raises(FooterError) as exc:
        FooterInfo("  ", " \n \n")
    assert len(exc.value.errors) == 2
    with pytest.raises(FooterError):
        FooterInfo("Shop\x00", "1 Road")
    with pytest.raises(FooterError):
        FooterInfo("x" * 101, "1 Road")
    with pytest.raises(FooterError):
        FooterInfo("Shop", "\n".join(["line"] * 7))
    with pytest.raises(FooterError):
        FooterInfo("Shop", "1 Road", reason="r" * 301)


def test_footer_normalised() -> None:
    footer = FooterInfo("  The   Barn ", " 1 Road \r\n\r\n  Town  \n", reason="  ")
    assert footer.business_name == "The Barn"
    assert footer.address_lines == ["1 Road", "Town"]
    assert footer.reason_text == render.DEFAULT_REASON.format(name="The Barn")


# --- plain text ------------------------------------------------------------------------------


def test_text_version() -> None:
    _, body_text = content.prepare(SAMPLE_BODY).for_recipient(SAMPLE_VALUES)
    text = render_text(body_text, SAMPLE_FOOTER, UNSUB)
    assert text.startswith("Dear Ada,")
    tail = text.split("\n--\n", 1)[1]
    assert tail == (
        "The Calico Barn\n123 Barn Road\nPO Box 45\nSpringfield, NC 27000\n\n"
        "This message is an advertisement.\n"
        "You're receiving this email because you're a customer of The Calico Barn "
        "or signed up for our emails.\n\n"
        f"Unsubscribe: {UNSUB}\n"
    )


# --- snapshot --------------------------------------------------------------------------------


def test_snapshot(html: str) -> None:
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(html, encoding="utf-8")
        real_unsub = f"{REAL_BASE}/u/" + UNSUB.rsplit("/u/", 1)[1]
        SAMPLE_DOC.write_text(sample_html(REAL_BASE, real_unsub), encoding="utf-8")
    assert SNAPSHOT.exists(), "run UPDATE_SNAPSHOTS=1 pytest tests/test_render.py"
    assert html == SNAPSHOT.read_text(encoding="utf-8"), (
        "Rendered email differs from tests/snapshots/christmas.html. If the change is intended, "
        "regenerate with UPDATE_SNAPSHOTS=1 pytest tests/test_render.py and review the diff."
    )


# --- /img/ route -----------------------------------------------------------------------------


@pytest.fixture
def client(settings: Settings) -> TestClient:
    # Access check switched on (no JWT is ever sent), to prove /img/ needs none.
    secured = dataclasses.replace(
        settings,
        auth_disabled=False,
        cf_access_team_domain="team.cloudflareaccess.com",
        cf_access_aud="aud",
    )
    return TestClient(create_app(secured))


@pytest.mark.parametrize(
    ("name", "ctype"), [("christmas-banner-v1.jpg", "image/jpeg"), ("holly-v1.png", "image/png")]
)
def test_img_public_with_cache_headers(client: TestClient, name: str, ctype: str) -> None:
    response = client.get(f"/img/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == ctype
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.content == (render.EMAIL_IMG_DIR / name).read_bytes()
    assert client.head(f"/img/{name}").status_code == 200


def test_admin_still_needs_auth(client: TestClient) -> None:
    assert client.get("/").status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/img/missing.png",
        "/img/",
        "/img/CREDITS.md",
        "/img/..%2fmain.py",
        "/img/%2e%2e/%2e%2e/app/main.py",
        "/img/..%2f..%2fpyproject.toml",
        "/img/%2e%2e/render.py",
    ],
)
def test_img_unknown_listing_and_traversal_404(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert b"import" not in response.content


def test_img_rejects_post(client: TestClient) -> None:
    assert client.post("/img/holly-v1.png").status_code == 405


def test_every_template_image_exists_and_is_small() -> None:
    total = 0
    for name in render.IMAGES.values():
        path = render.EMAIL_IMG_DIR / name
        assert path.suffix in {".png", ".jpg"}
        total += path.stat().st_size
    assert total < 300 * 1024


def test_img_without_slash_is_not_served(client: TestClient) -> None:
    assert client.get("/img", follow_redirects=False).status_code in (403, 404)


def test_theme_fonts_survive_in_style_blocks(html: str) -> None:
    css = "\n".join(lint(html).styles)
    assert "Georgia, 'Times New Roman', Times, serif" in css
    assert "&#39;" not in css
    doc = lint(html)
    (headline,) = [
        a for t, a in doc.tags if t == "h1" and "hero-headline" in (a.get("class") or "")
    ]
    assert "font-family: Georgia, 'Times New Roman', Times, serif" in (headline.get("style") or "")
    assert "font-size: 44px" in (headline.get("style") or "")


@pytest.mark.parametrize("font", ["x;}</style><script>", "a{b}", 'Georgia"', "a/*b*/"])
def test_unsafe_theme_value_refused(font: str) -> None:
    with pytest.raises(ValueError):
        render_email(
            "<p>x</p>",
            subject="s",
            preheader="",
            footer=SAMPLE_FOOTER,
            unsubscribe_url=UNSUB,
            public_base_url=BASE,
            theme=content.Theme(heading_font=font),
        )
