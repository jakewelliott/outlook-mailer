import html
import re
import time
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

from app.merge import (
    MergeError,
    Placeholder,
    find_fields,
    protect,
    recipient_values,
    render,
    validate,
)
from app.recipients import Recipient

JANE = {"first_name": "Jane", "email": "jane@example.com"}
BLANK = {"first_name": "  ", "email": "jane@example.com"}


# --- parsing ---------------------------------------------------------------------------------


def test_find_fields() -> None:
    text = "Hi {{ First_Name | Dear friend }}, {{email}}"
    assert find_fields(text) == [
        Placeholder(3, 33, "{{ First_Name | Dear friend }}", "first_name", "Dear friend"),
        Placeholder(35, 44, "{{email}}", "email", None),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Hello {{first_name}}",
        "Hello {{ first_name }}",
        "Hello {{\tFIRST_NAME\t}}",
        "Hello {{first_name|Friend}}, it's {{ email | n/a }}",
        "Hello {{first_name|}}",
        "Prices {from 5} to }} and { { not a field",
        "",
    ],
)
def test_valid(text: str) -> None:
    assert validate(text) == []


def test_unknown_field() -> None:
    assert validate("Dear {{last_name}}") == [
        "Unknown field {{last_name}} — available: first_name, email."
    ]


def test_errors_are_reported_once_each() -> None:
    errors = validate("{{x}} {{x}} {{y}}")
    assert errors == [
        "Unknown field {{x}} — available: first_name, email.",
        "Unknown field {{y}} — available: first_name, email.",
    ]


@pytest.mark.parametrize(
    ("text", "snippet"),
    [
        ("{{7*7}}", "{{7*7}}"),
        ("{{first_name.__class__}}", "{{first_name.__class__}}"),
        ("{{first_name}", "{{first_name}"),
        ("{{first_name|a|b}}", "{{first_name|a|b}}"),
        ("{{first_name|a}b}}", "{{first_name|a}b}}"),
        ("{{{first_name}}}", "{{{first_name}}"),
        ("{{}}", "{{}}"),
        ("{{first name}}", "{{first name}}"),
        ("{{first_name|line\nbreak}}", "{{first_name|line"),
        ("{{ config.items() }}", "{{ config.items() }}"),
    ],
)
def test_malformed(text: str, snippet: str) -> None:
    errors = validate(text)
    assert errors and errors[0].startswith(f"Malformed merge field {snippet} —")


def test_unknown_jinja_like_name() -> None:
    errors = validate("{{ config }}")
    assert errors == ["Unknown field {{ config }} — available: first_name, email."]


def test_template_tags_reported() -> None:
    errors = validate("{% for x in y %}hi{% endfor %}")
    assert errors == [
        "Template tags like {% for x in y %} are not supported.",
        "Template tags like {% endfor %} are not supported.",
    ]


def test_long_malformed_snippet_is_cut() -> None:
    (error,) = validate("{{" + "x" * 100)
    assert "{{" + "x" * 18 + "…" in error


def test_huge_malformed_single_line_is_fast() -> None:
    start = time.perf_counter()
    errors = validate("{{a" * 100_000)  # 300 KB, one line, 100,000 malformed fields
    assert time.perf_counter() - start < 2
    assert errors and errors[0].startswith("Malformed merge field {{a{{a")


# --- rendering -------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["html", "text", "subject"])
def test_basic_render(mode: str) -> None:
    # Only values are escaped; the text around them is passed through as is.
    out = render("<b>Hi</b> {{ first_name }}, {{EMAIL}}", {**JANE, "first_name": "A&B"}, mode=mode)
    assert out == (
        "<b>Hi</b> A&amp;B, jane@example.com"
        if mode == "html"
        else "<b>Hi</b> A&B, jane@example.com"
    )


def test_fallback_used_only_when_blank() -> None:
    assert render("Hi {{first_name|Friend}}", JANE, mode="text") == "Hi Jane"
    assert render("Hi {{first_name|Friend}}", BLANK, mode="text") == "Hi Friend"
    assert render("Hi {{first_name|Friend}}", {"email": "x@y"}, mode="text") == "Hi Friend"
    assert render("Hi {{first_name| dear friend }}!", {"first_name": ""}, mode="text") == (
        "Hi dear friend!"
    )
    assert render("Hi {{first_name}}.", BLANK, mode="text") == "Hi ."


def test_fallback_is_escaped_in_html() -> None:
    text = 'Hi {{first_name|Tom & "Jerry" <3>}}'
    assert render(text, BLANK, mode="html") == "Hi Tom &amp; &quot;Jerry&quot; &lt;3&gt;"
    assert render(text, BLANK, mode="text") == 'Hi Tom & "Jerry" <3>'


def test_url_mode_percent_encodes_values_and_fallbacks() -> None:
    values = {"first_name": "a b#c&\"/?=+%<'", "email": "a+b@example.com"}
    out = render("https://e.com/?n={{first_name}}&e={{email}}", values, mode="url")
    assert out == "https://e.com/?n=a%20b%23c%26%22%2F%3F%3D%2B%25%3C%27&e=a%2Bb@example.com"
    assert render("?n={{first_name|Dear friend}}", BLANK, mode="url") == "?n=Dear%20friend"
    assert (
        render("{{first_name}}", {"first_name": "Zoë 🎄"}, mode="url") == "Zo%C3%AB%20%F0%9F%8E%84"
    )
    assert render("{{first_name}}", {"first_name": "{{email}}"}, mode="url") == "%7B%7Bemail%7D%7D"
    assert render("{{first_name}}", {"first_name": "a\x00\r\nb\tc"}, mode="url") == "a%0Ab%09c"


def test_values_are_trimmed() -> None:
    assert render("[{{first_name}}]", {"first_name": " Jane \n"}, mode="text") == "[Jane]"


@pytest.mark.parametrize(
    "text", ["{{7*7}}", "{{ config }}", "{{first_name.__class__}}", "{% if 1 %}x{% endif %}"]
)
def test_render_refuses_invalid_text(text: str) -> None:
    with pytest.raises(MergeError) as exc:
        render(text, JANE, mode="text")
    assert exc.value.errors == validate(text)
    assert "49" not in str(exc.value)


def test_render_refuses_unknown_mode() -> None:
    with pytest.raises(ValueError):
        render("x", JANE, mode="markdown")  # type: ignore[arg-type]


HOSTILE = [
    "<script>alert(1)</script>",
    "Tom & Jerry",
    'O\'Brien "the" great',
    "{{email}}",
    "{{7*7}} {% raw %}",
    "Line one\nLine two\r\nthree",
    "🎄 Jane 👨‍👩‍👧",
    "שָׁלוֹם",
    "مرحبا Jane",
]


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_values_text_mode_raw_and_single_pass(name: str) -> None:
    out = render("Hi {{first_name}}!", {"first_name": name, "email": "e@x"}, mode="text")
    assert out == "Hi " + name.replace("\r", "").strip() + "!"  # CR is a control char


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_values_html_mode_escaped(name: str) -> None:
    out = render("<p>Hi {{first_name}}!</p>", {"first_name": name, "email": "e@x"}, mode="html")
    inner = out.removeprefix("<p>Hi ").removesuffix("!</p>")
    assert not any(c in inner for c in "<>\"'")
    assert html.unescape(inner) == name.replace("\r", "").strip()


def test_specific_escapes() -> None:
    def html_name(name: str) -> str:
        return render("{{first_name}}", {"first_name": name}, mode="html")

    assert html_name("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert html_name('O\'Brien "x" & co') == "O&#x27;Brien &quot;x&quot; &amp; co"
    assert html_name("{{email}}") == "{{email}}"  # inserted literally, not expanded
    assert html_name("🎄 שָׁלוֹם") == "🎄 שָׁלוֹם"


def test_subject_mode_strips_control_chars_and_collapses_whitespace() -> None:
    values = {"first_name": "Jane\r\nBcc: evil@example.com\x00\x1b[31m\u2028x", "email": "e@x"}
    out = render("  Hello\t{{first_name}} \n ", values, mode="subject")
    assert out == "Hello Jane Bcc: evil@example.com[31m x"
    assert not any(c in out for c in "\r\n\t\x00\x1b\u2028")


def test_subject_mode_keeps_emoji_and_rtl() -> None:
    values = {"first_name": "👨‍👩‍👧 שָׁלוֹם"}
    assert render("🎄 {{first_name}}", values, mode="subject") == "🎄 👨‍👩‍👧 שָׁלוֹם"


def test_subject_mode_strips_bidi_and_invisible_chars() -> None:
    hidden = "‪‫‬‭‮⁦⁧⁨⁩​⁠﻿"
    values = {"first_name": f"Ja{hidden}ne", "email": "e@x"}
    assert render("Hi {{first_name}}", values, mode="subject") == "Hi Jane"
    assert render(f"Hi{hidden} {{{{first_name}}}}", values, mode="subject") == "Hi Jane"


def test_subject_mode_keeps_zwj_and_zwnj() -> None:
    family = "\U0001f468‍\U0001f469‍\U0001f467"  # one emoji built with ZWJ
    persian = "می‌خواهم"  # "I want", needs ZWNJ
    out = render("{{first_name}}", {"first_name": f"{family} {persian}"}, mode="subject")
    assert out == f"{family} {persian}"
    assert out.count("‍") == 2 and "‌" in out


@pytest.mark.parametrize("mode", ["html", "text"])
def test_control_chars_removed_from_values_only(mode: str) -> None:
    values = {"first_name": "Ja\x00n\x1b[31me\r\x7f\x85\tA\nB", "email": "e@x"}
    out = render("\x07{{first_name}}\x07", values, mode=mode)
    assert out == "\x07Jan[31me\tA\nB\x07"  # Jake's own text is left alone


def test_value_of_only_control_chars_uses_fallback() -> None:
    assert render("Hi {{first_name|Friend}}", {"first_name": "\x00\x1b"}, mode="text") == (
        "Hi Friend"
    )


def test_recipient_values() -> None:
    values = recipient_values(Recipient(row=2, email="jane@example.com", first_name="Jane"))
    assert render("{{first_name}} {{email}}", values, mode="text") == "Jane jane@example.com"


# --- Markdown interplay (how stage 6 will use this) ------------------------------------------


@pytest.mark.parametrize("preset", ["commonmark", "default"])
def test_placeholders_survive_markdown_via_protect(preset: str) -> None:
    body = (
        "Hi **{{ first_name | Tom & Jerry }}**, _{{first_name}}_\n\n"
        "| a | b |\n|---|---|\n| {{first_name|x}} | {{email}} |\n\n"
        "[link](https://example.com/?e={{email}})"
    )
    protected = protect(body)
    assert "{{" not in protected.text
    converted = protected.restore(MarkdownIt(preset).render(protected.text))
    out = render(converted, {"first_name": "<b>Ann</b>", "email": "a@x.com"}, mode="html")
    assert "<strong>&lt;b&gt;Ann&lt;/b&gt;</strong>" in out
    assert "<em>&lt;b&gt;Ann&lt;/b&gt;</em>" in out
    assert 'href="https://example.com/?e=a@x.com"' in out
    if preset == "default":  # tables are on here; the placeholder stays in one cell
        assert "<td>&lt;b&gt;Ann&lt;/b&gt;</td>" in out
    # Fallback escaped exactly once (Markdown never saw it).
    out = render(converted, {"email": "a@x.com"}, mode="html")
    assert "<strong>Tom &amp; Jerry</strong>" in out


def test_without_protect_markdown_would_mangle_placeholders() -> None:
    # Why protect() exists: Markdown escapes the fallback and URL-encodes braces in links.
    out = MarkdownIt("commonmark").render("{{first_name|A & B}} [x](https://e.com/{{email}})")
    assert "{{first_name|A &amp; B}}" in out
    assert "%7B%7Bemail%7D%7D" in out


def test_protect_tokens_are_unique_and_not_in_source() -> None:
    body = "{{email}} {{email}} " * 6
    protected = protect(body)
    assert len(protected.tokens) == 12
    assert protected.restore(protected.text) == body
    assert all(token.isalnum() for token in protected.tokens)


def test_protect_leaves_invalid_text_for_validate() -> None:
    protected = protect("{{7*7}} {{first_name}}")
    assert protected.text.startswith("{{7*7}} MF")
    assert validate(protected.restore(protected.text)) == validate("{{7*7}} {{first_name}}")


def test_help_page_examples_are_valid() -> None:
    page = (Path(__file__).parents[1] / "app/templates/ui/home.html.j2").read_text()
    examples = re.findall(r"<code>(\{\{[^<]*\}\})</code>", page)
    assert len(examples) >= 4
    for example in examples:
        assert validate(example) == [], example
