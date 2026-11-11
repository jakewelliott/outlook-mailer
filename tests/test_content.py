import html
import re

import pytest

from app import content
from app.content import (
    ContentError,
    PreparedBody,
    Theme,
    check_body,
    check_subject,
    prepare,
    render_subject,
    styles,
)
from app.merge import MergeError, render
from tests.test_merge import HOSTILE

JANE = {"first_name": "Jane", "email": "jane@example.com"}
BLANK = {"first_name": " ", "email": "jane@example.com"}
STYLE = styles()


def convert(body: str, values: dict[str, str] = JANE) -> tuple[str, str]:
    return prepare(body).for_recipient(values)


def unstyled(fragment: str) -> str:
    """The fragment without style attributes or newlines, for short assertions."""
    return re.sub(r' style="[^"]*"', "", fragment).replace("\n", "")


def tag(name: str, attrs: str = "") -> str:
    return f'<{name}{attrs} style="{STYLE[name]}">'


# --- syntax ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "fragment", "text"),
    [
        ("Hello", "<p>Hello</p>", "Hello"),
        ("One\n\nTwo", "<p>One</p><p>Two</p>", "One\n\nTwo"),
        ("One\nTwo", "<p>One<br>Two</p>", "One\nTwo"),
        ("One  \nTwo", "<p>One<br>Two</p>", "One\nTwo"),
        ("**bold**", "<p><strong>bold</strong></p>", "bold"),
        ("__bold__", "<p><strong>bold</strong></p>", "bold"),
        ("*italic* _italic_", "<p><em>italic</em> <em>italic</em></p>", "italic italic"),
        ("~~strike~~", "<p><s>strike</s></p>", "strike"),
        ("++under++", "<p><u>under</u></p>", "under"),
        ("***both***", "<p><em><strong>both</strong></em></p>", "both"),
        ("**a *b* c**", "<p><strong>a <em>b</em> c</strong></p>", "a b c"),
        ("*a **b** c*", "<p><em>a <strong>b</strong> c</em></p>", "a b c"),
        ("++**b** ~~s~~++", "<p><u><strong>b</strong> <s>s</s></u></p>", "b s"),
        ("**++u++**", "<p><strong><u>u</u></strong></p>", "u"),
        ("# Big", "<h1>Big</h1>", "Big"),
        ("## Smaller", "<h2>Smaller</h2>", "Smaller"),
        ("### Deeper\n\n###### Deepest", "<h2>Deeper</h2><h2>Deepest</h2>", "Deeper\n\nDeepest"),
        ("# Title\nText", "<h1>Title</h1><p>Text</p>", "Title\n\nText"),
        ("- a\n- b", "<ul><li>a</li><li>b</li></ul>", "- a\n- b"),
        ("* a\n+ b", "<ul><li>a</li></ul><ul><li>b</li></ul>", "- a\n\n- b"),
        ("1. a\n2. b", "<ol><li>a</li><li>b</li></ol>", "1. a\n2. b"),
        ("3. a\n1. b", '<ol start="3"><li>a</li><li>b</li></ol>', "3. a\n4. b"),
        ("Intro:\n- a", "<p>Intro:</p><ul><li>a</li></ul>", "Intro:\n\n- a"),
        (
            "- a\n  - b\n- c\n  next",
            "<ul><li>a<ul><li>b</li></ul></li><li>c<br>next</li></ul>",
            "- a\n  - b\n- c\n  next",
        ),
        ("1. a\n\n   more", "<ol><li><p>a</p><p>more</p></li></ol>", "1. a\n   more"),
        (
            "[our shop](https://example.com/shop)",
            '<p><a href="https://example.com/shop">our shop</a></p>',
            "our shop (https://example.com/shop)",
        ),
        (
            "[**bold** link](http://example.com)",
            '<p><a href="http://example.com"><strong>bold</strong> link</a></p>',
            "bold link (http://example.com)",
        ),
        (
            "<https://example.com/a?b=1&c=2>",
            '<p><a href="https://example.com/a?b=1&amp;c=2">https://example.com/a?b=1&amp;c=2</a></p>',
            "https://example.com/a?b=1&c=2",
        ),
        (
            "[https://example.com](https://example.com)",
            '<p><a href="https://example.com">https://example.com</a></p>',
            "https://example.com",
        ),
        (
            "[Write to us](mailto:shop@example.com)",
            '<p><a href="mailto:shop@example.com">Write to us</a></p>',
            "Write to us (mailto:shop@example.com)",
        ),
        (
            "<shop@example.com>",
            '<p><a href="mailto:shop@example.com">shop@example.com</a></p>',
            "shop@example.com",
        ),
        (
            "Tom &amp; Jerry \\*not italic\\*",
            "<p>Tom &amp; Jerry *not italic*</p>",
            "Tom & Jerry *not italic*",
        ),
    ],
)
def test_syntax(body: str, fragment: str, text: str) -> None:
    out_html, out_text = convert(body)
    assert unstyled(out_html) == fragment
    assert out_text == text


def test_inline_styles() -> None:
    out, _ = convert("# T\n\n## S\n\nx ~~s~~ ++u++ [l](https://e.com)\n\n- a\n\n1. b")
    for name in ("h1", "h2", "p", "s", "u", "ul", "ol", "li"):
        assert tag(name) in out
    assert f'<a href="https://e.com" style="{STYLE["a"]}">' in out
    assert "class=" not in out and "<div" not in out
    assert out.count("<p ") == 1


def test_theme_colours_and_fonts() -> None:
    theme = Theme(text="#111111", heading="#222222", link="#333333", heading_font='"Odd" font')
    out, _ = prepare("# H\n\n[l](https://e.com) text", theme).for_recipient(JANE)
    assert "color:#222222" in out and "color:#333333" in out and "color:#111111" in out
    assert "font-family:&quot;Odd&quot; font" in out
    assert "#8c1c13" not in out


def test_default_palette() -> None:
    assert "color:#8c1c13" in STYLE["h1"]  # deep red
    assert "color:#1d5b3a" in STYLE["a"]  # pine green


# --- underline ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("un++der++line", "un<u>der</u>line"),
        ("++a++ and ++b++", "<u>a</u> and <u>b</u>"),
        ("++unclosed", "++unclosed"),
        ("closed++ only", "closed++ only"),
        ("C++ and C++", "C++ and C++"),
        ("1 + 1 = 2", "1 + 1 = 2"),
        ("a ++ b ++ c", "a ++ b ++ c"),
        ("+single+", "+single+"),
        ("++ spaced ++", "++ spaced ++"),
        ("+++odd+++", "+<u>odd</u>+"),
        ("++++four++++", "<u><u>four</u></u>"),
        ("+++", "+++"),
        ("++**b**++", "<u><strong>b</strong></u>"),
        ("++a\nb++", "<u>a<br>b</u>"),
        ("\\++not\\++", "++not++"),
    ],
)
def test_underline_edge_cases(body: str, fragment: str) -> None:
    assert unstyled(convert(body)[0]) == f"<p>{fragment}</p>"


def test_underline_text_drops_markers() -> None:
    assert convert("un++der++line, +++odd+++, C++")[1] == "underline, +odd+, C++"


# --- disabled syntax and raw HTML --------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("<script>alert(1)</script>", "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"),
        ("<img src=x onerror=alert(1)>", "<p>&lt;img src=x onerror=alert(1)&gt;</p>"),
        ("a <b>b</b> <!-- c -->", "<p>a &lt;b&gt;b&lt;/b&gt; &lt;!-- c --&gt;</p>"),
        ("<div>\nblock\n</div>", "<p>&lt;div&gt;<br>block<br>&lt;/div&gt;</p>"),
        ("    indented", "<p>indented</p>"),
        ("```\ncode\n```", "<p>```<br>code<br>```</p>"),
        ("`code`", "<p>`code`</p>"),
        ("> quote", "<p>&gt; quote</p>"),
        ("---", "<p>---</p>"),
        ("Not a heading\n---", "<p>Not a heading<br>---</p>"),
        ("[x][ref]\n\n[ref]: https://e.com", "<p>[x][ref]</p><p>[ref]: https://e.com</p>"),
        ("| a | b |\n|---|---|", "<p>| a | b |<br>|---|---|</p>"),
        ("https://example.com", "<p>https://example.com</p>"),
        ("#hashtag", "<p>#hashtag</p>"),
    ],
)
def test_raw_html_escaped_and_other_syntax_is_text(body: str, fragment: str) -> None:
    assert unstyled(convert(body)[0]) == fragment


def test_raw_html_stays_raw_text_in_plain_part() -> None:
    assert convert("<script>alert(1)</script>")[1] == "<script>alert(1)</script>"


def test_images_are_an_error() -> None:
    assert check_body("![tree](https://example.com/tree.png)") == [
        "Pictures can't be added to the message (“https://example.com/tree.png”); the email "
        "template has its own. To link to it instead, remove the ! before the [."
    ]


# --- link checks -------------------------------------------------------------------------------

BAD_SCHEME = "can't be used: links must start with https://, http:// or mailto:."
BAD_FIELD = "has a merge field in the wrong place"


@pytest.mark.parametrize(
    ("body", "shown"),
    [
        ("[x](javascript:alert(1))", "javascript:alert(1)"),
        ("[x](JavaScript:alert(1))", "JavaScript:alert(1)"),
        ("[x](<java\tscript:alert(1)>)", "java\tscript:alert(1)"),
        ("<javascript:alert(1)>", "javascript:alert(1)"),
        ("[x](vbscript:msgbox)", "vbscript:msgbox"),
        ("[x](data:text/html;base64,PHNjcmlwdD4=)", "data:text/html;base64,PHNjcmlwdD4="),
        ("[x](file:///etc/passwd)", "file:///etc/passwd"),
        ("[x](/relative/page)", "/relative/page"),
        ("[x](page.html)", "page.html"),
        ("[x](//evil.example)", "//evil.example"),
        ("[x](#top)", "#top"),
        ("[x](https://)", "https://"),
        ("[x](https:///path)", "https:///path"),
        ("[x](ftp://example.com)", "ftp://example.com"),
        ("[x](mailto:)", "mailto:"),
    ],
)
def test_unsafe_links_are_errors(body: str, shown: str) -> None:
    assert check_body(body) == [f"The link to “{shown}” {BAD_SCHEME}"]
    with pytest.raises(ContentError) as exc:
        prepare(body)
    assert exc.value.errors == check_body(body)


def test_empty_link_is_an_error() -> None:
    assert check_body("[x]()") == [
        "A link has no address — write it as [text](https://example.com)."
    ]


def test_every_link_error_is_reported() -> None:
    errors = check_body("[a](javascript:x) [b](/rel) [ok](https://e.com) {{last_name}}")
    assert len(errors) == 3
    assert errors[0].startswith("Unknown field {{last_name}}")


@pytest.mark.parametrize(
    "body",
    [
        "[x](https://example.com/?e={{email}})",
        "[x](https://example.com/?e={{ Email }}&n={{first_name|friend}})",
        "[x](http://example.com/{{first_name}})",
        "[x](https://example.com#{{first_name}})",
        "[x](https://example.com?n={{first_name}})",
        "[x](HTTPS://Example.com/{{first_name}})",
        "[x](mailto:{{email}})",
        "[x](MAILTO:{{ email | shop@example.com }})",
        "<https://example.com/u/{{email}}>",
    ],
)
def test_allowed_placeholders_in_links(body: str) -> None:
    assert check_body(body) == []


@pytest.mark.parametrize(
    ("body", "shown"),
    [
        ("[x]({{first_name}})", "{{first_name}}"),
        ("[x]({{email}})", "{{email}}"),
        ("[x](https://{{first_name}}.com)", "https://{{first_name}}.com"),
        ("[x](https://example.{{first_name}}/)", "https://example.{{first_name}}/"),
        ("[x](https://example.com{{first_name}})", "https://example.com{{first_name}}"),
        ("[x](https://{{first_name}}@example.com/)", "https://{{first_name}}@example.com/"),
        ("[x](https://example.com@{{first_name}}/)", "https://example.com@{{first_name}}/"),
        ("[x]({{first_name}}://example.com)", "{{first_name}}://example.com"),
        ("[x](http{{first_name}}://example.com)", "http{{first_name}}://example.com"),
        ("[x](mailto:{{first_name}})", "mailto:{{first_name}}"),
        ("[x](mailto:{{email}}?cc=a@b.c)", "mailto:{{email}}?cc=a@b.c"),
        ("[x](mailto:a@b.c,{{email}})", "mailto:a@b.c,{{email}}"),
        ("[x](//{{first_name}})", "//{{first_name}}"),
        ("[x](/{{first_name}})", "/{{first_name}}"),
        ("<{{first_name}}:x>", "{{first_name}}:x"),
    ],
)
def test_rejected_placeholders_in_links(body: str, shown: str) -> None:
    (error,) = check_body(body)
    assert error.startswith(f"The link to “{shown}” {BAD_FIELD}: ")
    assert "https://example.com/?email={{email}}" in error and "mailto:{{email}}" in error


def test_placeholder_in_link_text_is_fine() -> None:
    out_html, out_text = convert("[Hi {{first_name}}](https://example.com)")
    assert unstyled(out_html) == '<p><a href="https://example.com">Hi Jane</a></p>'
    assert out_text == "Hi Jane (https://example.com)"


# --- merge fields -------------------------------------------------------------------------------


def test_raw_body_is_validated_first_and_errors_quote_it() -> None:
    errors = check_body("**Hi {{7*7}}** and [x](https://e.com/{{ last_name }}) {% raw %}")
    assert errors == [
        "Malformed merge field {{7*7}} — write {{first_name}}, {{email}} or "
        "{{first_name|Fallback}}.",
        "Template tags like {% raw %} are not supported.",
        "Unknown field {{ last_name }} — available: first_name, email.",
    ]


def test_empty_body() -> None:
    assert check_body(" \n ") == ["The message is empty."]


def test_fields_and_fallbacks_in_body() -> None:
    body = "# Hi {{ First_Name | dear friend }}\n\n**{{first_name|Tom & Jerry}}**, {{email}}"
    out_html, out_text = convert(body)
    assert unstyled(out_html) == "<h1>Hi Jane</h1><p><strong>Jane</strong>, jane@example.com</p>"
    out_html, out_text = convert(body, BLANK)
    assert unstyled(out_html) == (
        "<h1>Hi dear friend</h1><p><strong>Tom &amp; Jerry</strong>, jane@example.com</p>"
    )
    assert out_text == "Hi dear friend\n\nTom & Jerry, jane@example.com"


def test_values_in_links_are_percent_encoded_then_escaped() -> None:
    body = "[Shop](https://example.com/?n={{first_name}}&e={{email}})"
    values = {"first_name": 'a b#c&"', "email": "jane@example.com"}
    out_html, out_text = convert(body, values)
    url = "https://example.com/?n=a%20b%23c%26%22&e=jane@example.com"
    assert unstyled(out_html) == f'<p><a href="{html.escape(url)}">Shop</a></p>'
    assert "&amp;e=" in out_html
    assert out_text == f"Shop ({url})"


def test_fallback_in_link_is_encoded() -> None:
    out_html, out_text = convert("[x](https://e.com/{{first_name|Dear friend}})", BLANK)
    assert 'href="https://e.com/Dear%20friend"' in out_html
    assert out_text == "x (https://e.com/Dear%20friend)"


def test_mailto_email() -> None:
    body = "[{{email}}](mailto:{{email}}) or [write](mailto:{{email}})"
    out_html, out_text = convert(body, {"email": "a+b@example.com"})
    assert unstyled(out_html) == (
        '<p><a href="mailto:a%2Bb@example.com">a+b@example.com</a> or '
        '<a href="mailto:a%2Bb@example.com">write</a></p>'
    )
    assert out_text == "a+b@example.com or write (mailto:a%2Bb@example.com)"


def test_autolink_with_field_uses_encoded_url_in_text() -> None:
    out_html, out_text = convert("<https://e.com/u/{{first_name}}>", {"first_name": "A B"})
    assert unstyled(out_html) == '<p><a href="https://e.com/u/A%20B">https://e.com/u/A B</a></p>'
    assert out_text == "https://e.com/u/A%20B"


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_first_names_in_body_and_links(name: str) -> None:
    body = "Hi **{{first_name}}**, ++{{first_name}}++\n\n[{{first_name}}](https://e.com/?n={{first_name}})"
    values = {"first_name": name, "email": "e@example.com"}
    out_html, out_text = convert(body, values)
    clean = name.replace("\r", "").strip()
    # HTML: the name only ever appears escaped; no new tags, attributes or links.
    assert out_html.count("<") == unstyled(convert(body)[0]).count("<")
    assert re.findall(r'href="([^"]*)"', out_html) == [
        html.escape("https://e.com/?n=" + render("{{first_name}}", values, mode="url"))
    ]
    assert f"<strong>{html.escape(clean)}</strong>" in out_html
    assert "{{" not in out_html.replace(html.escape(clean), "")
    # Text: the name is inserted as typed (single pass, so "{{email}}" is not expanded).
    assert out_text.startswith(f"Hi {clean}, {clean}\n\n{clean} (https://e.com/?n=")
    assert "e@example.com" not in out_text


def test_markdown_in_values_is_not_interpreted() -> None:
    values = {"first_name": "**x** [a](javascript:1) <b>", "email": "e@example.com"}
    out_html, out_text = convert("Hi {{first_name}}", values)
    assert unstyled(out_html) == "<p>Hi **x** [a](javascript:1) &lt;b&gt;</p>"
    assert out_text == "Hi **x** [a](javascript:1) <b>"


def test_escaped_braces_are_not_merge_fields() -> None:
    # `{\{first_name}}` passes validate() (no "{{" as typed) and Markdown makes it "{{first_name}}";
    # only placeholders found in the raw body are ever filled in.
    out_html, out_text = convert("{\\{first_name}} &#123;&#123;email}}")
    assert unstyled(out_html) == "<p>{{first_name}} {{email}}</p>"
    assert out_text == "{{first_name}} {{email}}"


def test_prepared_body_is_reusable_for_many_recipients() -> None:
    prepared = prepare("Hi {{first_name|friend}} [x](https://e.com/{{email}})")
    assert prepared.for_recipient(JANE)[1] == "Hi Jane x (https://e.com/jane@example.com)"
    assert prepared.for_recipient({"email": "b@x.org"})[1] == "Hi friend x (https://e.com/b@x.org)"


def test_backstop_rejects_unsafe_merged_links() -> None:
    field = content._Field("{{first_name}}", url=True)
    # A PreparedBody that prepare() would never build: the whole href is a field.
    prepared = PreparedBody(('<a href="', field, '">x</a>'), ())
    for value in ("javascript:alert(1)", "//evil.example", "data:text/html,x"):
        with pytest.raises(ContentError):
            prepared.for_recipient({"first_name": value})
    ok = PreparedBody(('<a href="https://e.com/', field, '">x</a>'), ())
    assert ok.for_recipient({"first_name": "javascript:alert(1)"})[0] == (
        '<a href="https://e.com/javascript%3Aalert%281%29">x</a>'
    )


# --- subject -----------------------------------------------------------------------------------


def test_subject_helpers() -> None:
    assert check_subject("Merry Christmas, {{first_name|friend}}!") == []
    assert check_subject("  ") == ["The subject is empty."]
    assert check_subject("Hi {{name}}") == [
        "Unknown field {{name}} — available: first_name, email."
    ]
    values = {"first_name": "Jane\r\nBcc: x@y.z", "email": "e@x"}
    assert render_subject("Hi {{first_name}} **x**", values) == "Hi Jane Bcc: x@y.z **x**"
    with pytest.raises(MergeError):
        render_subject("Hi {{7*7}}", values)
