"""The message body: Markdown to an email-safe HTML fragment and plain text, with merge fields.

Supported syntax (anything else stays as typed text):

- Paragraphs are separated by a blank line. **A single line break is kept** (``<br>``), because
  the body is typed in a textarea where Enter is expected to start a new line.
- ``**bold**``, ``*italic*`` / ``_italic_``, ``~~strike~~`` (``<s>``), ``++underline++`` (``<u>``,
  a small inline rule below, following the same delimiter rules as ``~~``: it works inside words,
  needs no space just inside the markers, and an odd ``+++`` leaves one ``+`` as text).
- ``[text](url)`` and ``<https://...>``. Only ``https``, ``http`` and ``mailto`` links; anything
  else (``javascript:``, ``data:``, relative, ``//host``) is an error, not silently dropped. Bare
  URLs are not turned into links here (that needs another dependency); mail clients link them.
- Bullet (``-``, ``*``, ``+``) and numbered lists, headings ``#`` and ``##`` (``###`` and deeper
  are shown as ``##``).
- Raw HTML is escaped. Images are an error (the template has its own). Code blocks, inline code,
  block quotes, horizontal rules, ``Heading\\n---`` headings and reference links are switched off,
  so indented lines, backticks, ``>`` and ``---`` are ordinary text.

Merge fields: the raw body is checked with ``merge.validate`` first, so errors quote Jake's text;
then ``merge.protect`` hides the placeholders from Markdown. The converted output is split into
literal text and placeholders, and only the placeholders are filled in per recipient, so nothing
Markdown produces (e.g. ``{\\{`` becoming ``{{``) is ever read as a merge field. A placeholder in a
link target may only follow a fixed ``http(s)://host/`` (or ``?``/``#``) prefix, or be exactly
``mailto:{{email}}``; its value is percent-encoded (``merge.render(mode="url")``) and then
HTML-escaped. Link schemes are checked again in each recipient's HTML.
"""

import html
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.rules_inline.state_inline import Delimiter, StateInline
from markdown_it.tree import SyntaxTreeNode

from app import merge

# --- styles ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Theme:
    """Colours and fonts for the fragment; stage 7's template may pass its own."""

    text: str = "#2b2118"  # dark brown on cream
    heading: str = "#8c1c13"  # deep red
    link: str = "#1d5b3a"  # pine green
    body_font: str = "Arial, Helvetica, sans-serif"
    heading_font: str = "Georgia, 'Times New Roman', Times, serif"


DEFAULT_THEME = Theme()


def styles(theme: Theme = DEFAULT_THEME) -> dict[str, str]:
    """Inline ``style`` for each tag the fragment uses (``strong`` and ``em`` need none)."""
    body = f"font-family:{theme.body_font};font-size:16px;line-height:24px;color:{theme.text}"
    heading = f"font-family:{theme.heading_font};font-weight:bold;color:{theme.heading}"
    return {
        "p": f"margin:0 0 16px;{body}",
        "h1": f"margin:0 0 16px;{heading};font-size:28px;line-height:34px",
        "h2": f"margin:0 0 12px;{heading};font-size:22px;line-height:28px",
        "ul": f"margin:0 0 16px 24px;padding:0;{body}",
        "ol": f"margin:0 0 16px 24px;padding:0;{body}",
        "li": f"margin:0 0 8px;{body}",
        "a": f"color:{theme.link};text-decoration:underline",
        "s": "text-decoration:line-through",
        "u": "text-decoration:underline",
    }


# --- ++underline++ -----------------------------------------------------------------------------
# A copy of markdown-it's ``~~strike~~`` rule for ``+``: each ``++`` becomes a delimiter, the
# built-in ``balance_pairs`` rule matches openers with closers, and the post-processing step turns
# matched pairs into ``u_open`` / ``u_close`` tokens.

_PLUS = ord("+")


def _underline(state: StateInline, silent: bool) -> bool:
    if silent or state.src[state.pos] != "+":
        return False
    scanned = state.scanDelims(state.pos, True)
    length = scanned.length
    if length < 2:
        return False
    if length % 2:  # "+++" is "+" then "++"
        state.push("text", "", 0).content = "+"
        length -= 1
    for _ in range(length // 2):
        state.push("text", "", 0).content = "++"
        state.delimiters.append(
            Delimiter(
                marker=_PLUS,
                length=0,  # no "rule of 3", as for ~~
                token=len(state.tokens) - 1,
                end=-1,
                open=scanned.can_open,
                close=scanned.can_close,
            )
        )
    state.pos += scanned.length
    return True


def _underline_pairs(state: StateInline, delimiters: list[Delimiter]) -> None:
    lone_markers: list[int] = []
    for opener in delimiters:
        if opener.marker != _PLUS or opener.end == -1:
            continue
        closer = delimiters[opener.end]
        for index, kind, nesting in ((opener.token, "u_open", 1), (closer.token, "u_close", -1)):
            token = state.tokens[index]
            token.type, token.tag, token.nesting = kind, "u", nesting
            token.markup, token.content = "++", ""
        before = state.tokens[closer.token - 1]
        if before.type == "text" and before.content == "+":
            lone_markers.append(closer.token - 1)
    # The lone "+" of an odd run in a closer belongs after the closing tag(s).
    while lone_markers:
        i = lone_markers.pop()
        j = i + 1
        while j < len(state.tokens) and state.tokens[j].type == "u_close":
            j += 1
        j -= 1
        if i != j:
            state.tokens[i], state.tokens[j] = state.tokens[j], state.tokens[i]


def _underline_post(state: StateInline) -> None:
    _underline_pairs(state, state.delimiters)
    for meta in state.tokens_meta:
        if meta and "delimiters" in meta:
            _underline_pairs(state, meta["delimiters"])


def _parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "breaks": True, "linkify": False})
    md.disable(["code", "fence", "blockquote", "hr", "reference", "lheading", "html_block"])
    md.disable(["backticks", "html_inline"])
    md.enable("strikethrough")
    md.inline.ruler.after("strikethrough", "underline", _underline)
    md.inline.ruler2.after("strikethrough", "underline", _underline_post)
    md.inline.add_terminator_char("+")
    # markdown-it turns links it considers unsafe back into text; we check every link ourselves
    # and report it instead.
    md.validateLink = lambda url: True  # type: ignore[method-assign]
    return md


_MD = _parser()

# --- links -----------------------------------------------------------------------------------

_HTTP = re.compile(r"(https?)://([^/?#]*)(.*)", re.IGNORECASE | re.DOTALL)
_SAFE_PREFIX = re.compile(r"(?:https?://|mailto:)", re.IGNORECASE)
_HREF = re.compile(r'\bhref="([^"]*)"')
_SCHEMES = "links must start with https://, http:// or mailto:"
_FIELD_RULE = (
    "a merge field can only come after a fixed web address such as https://example.com/ "
    "(for example https://example.com/?email={{email}}), or be exactly mailto:{{email}}"
)


class ContentError(merge.MergeError):
    """The body (or subject) can't be sent; ``errors`` are messages for Jake."""


def _is_email_field(token: str, protected: merge.Protected) -> bool:
    source = protected.tokens.get(token)
    return source is not None and merge.find_fields(source)[0].name == "email"


def _link_error(href: str, protected: merge.Protected) -> str | None:
    """Why the link target ``href`` (still holding protect() tokens) can't be used, if it can't."""
    shown = protected.restore(_MD.normalizeLinkText(href))
    if not href.strip():
        return "A link has no address — write it as [text](https://example.com)."
    tokens = [t for t in protected.tokens if t in href]
    http = _HTTP.fullmatch(href)
    if not tokens:
        if (http and http.group(2)) or (href[:7].lower() == "mailto:" and href[7:]):
            return None
        return f"The link to “{shown}” can't be used: {_SCHEMES}."
    if href[:7].lower() == "mailto:" and _is_email_field(href[7:], protected):
        return None
    if http and http.group(2) and not any(t in http.group(2) for t in tokens):
        return None
    return f"The link to “{shown}” has a merge field in the wrong place: {_FIELD_RULE}."


# --- parsing and checks ------------------------------------------------------------------------


def _parse(body: str) -> tuple[merge.Protected, SyntaxTreeNode, list[str]]:
    errors = merge.validate(body)
    if not body.strip():
        errors.insert(0, "The message is empty.")
    protected = merge.protect(body)
    tree = SyntaxTreeNode(_MD.parse(protected.text))
    for node in tree.walk():
        if node.type == "link" and (error := _link_error(str(node.attrs["href"]), protected)):
            errors.append(error)
        elif node.type == "image":
            src = protected.restore(_MD.normalizeLinkText(str(node.attrs["src"])))
            errors.append(
                f"Pictures can't be added to the message (“{src}”); the email template has its "
                "own. To link to it instead, remove the ! before the [."
            )
    return protected, tree, list(dict.fromkeys(errors))


def check_body(body: str) -> list[str]:
    """Every problem with the Markdown body as typed (merge fields and links); empty if OK."""
    return _parse(body)[2]


def check_subject(subject: str) -> list[str]:
    errors = merge.validate(subject)
    if not subject.strip():
        errors.insert(0, "The subject is empty.")
    return errors


def render_subject(subject: str, values: Mapping[str, str | None]) -> str:
    """One recipient's subject line. Raises ``MergeError`` if ``check_subject`` finds problems."""
    return merge.render(subject, values, mode="subject")


# --- rendering -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Field:
    source: str  # the placeholder as written, e.g. "{{first_name|Friend}}"
    url: bool  # inside a link target


_Part = str | _Field


class _Out:
    """Collects output, splitting protect() tokens out as fields."""

    def __init__(self, protected: merge.Protected) -> None:
        self.parts: list[_Part] = []
        self._sources = protected.tokens
        self._tokens = re.compile("|".join(map(re.escape, protected.tokens)) or r"(?!)")

    def add(self, text: str, *, url: bool = False) -> None:
        pos = 0
        for m in self._tokens.finditer(text):
            self._literal(text[pos : m.start()])
            self.parts.append(_Field(self._sources[m.group()], url))
            pos = m.end()
        self._literal(text[pos:])

    def _literal(self, text: str) -> None:
        if not text:
            return
        if self.parts and isinstance(self.parts[-1], str):
            self.parts[-1] += text
        else:
            self.parts.append(text)


def _attr(value: str) -> str:
    """Escape for a double-quoted attribute (``'`` in font names may stay as it is)."""
    return html.escape(value, quote=False).replace('"', "&quot;")


def _heading_tag(node: SyntaxTreeNode) -> str:
    return "h1" if node.tag == "h1" else "h2"


class _HtmlRenderer:
    def __init__(self, out: _Out, style: Mapping[str, str]) -> None:
        self.out = out
        self.style = style

    def _open(self, tag: str, extra: str = "") -> None:
        self.out.add(f'<{tag}{extra} style="{_attr(self.style[tag])}">')

    def blocks(self, nodes: Sequence[SyntaxTreeNode]) -> None:
        for node in nodes:
            self.block(node)

    def block(self, node: SyntaxTreeNode) -> None:
        match node.type:
            case "paragraph" if node.hidden:  # the text of a tight list item
                self.inline(node.children[0].children)
            case "paragraph" | "heading":
                tag = "p" if node.type == "paragraph" else _heading_tag(node)
                self._open(tag)
                self.inline(node.children[0].children)
                self.out.add(f"</{tag}>\n")
            case "bullet_list" | "ordered_list":
                tag = "ul" if node.type == "bullet_list" else "ol"
                start = int(node.attrs.get("start", 1))
                self._open(tag, f' start="{start}"' if start != 1 else "")
                self.out.add("\n")
                self.blocks(node.children)
                self.out.add(f"</{tag}>\n")
            case "list_item":
                self._open("li")
                self.blocks(node.children)
                self.out.add("</li>\n")
            case other:
                raise ValueError(f"Unexpected Markdown block {other!r}")

    def inline(self, nodes: Sequence[SyntaxTreeNode]) -> None:
        for node in nodes:
            match node.type:
                case "text":
                    self.out.add(html.escape(node.content, quote=False))
                case "softbreak" | "hardbreak":
                    self.out.add("<br>\n")
                case "strong" | "em":
                    self.out.add(f"<{node.type}>")
                    self.inline(node.children)
                    self.out.add(f"</{node.type}>")
                case "s" | "u":
                    self._open(node.type)
                    self.inline(node.children)
                    self.out.add(f"</{node.type}>")
                case "link":
                    self.out.add('<a href="')
                    self.out.add(html.escape(str(node.attrs["href"])), url=True)
                    self.out.add(f'" style="{_attr(self.style["a"])}">')
                    self.inline(node.children)
                    self.out.add("</a>")
                case other:
                    raise ValueError(f"Unexpected Markdown inline {other!r}")


def _plain(nodes: Sequence[SyntaxTreeNode]) -> str:
    """The text of inline nodes, without markup (for comparing a link's text with its URL)."""
    return "".join(
        node.content
        if node.type == "text"
        else "\n"
        if "break" in node.type
        else _plain(node.children)
        for node in nodes
    )


class _TextRenderer:
    def __init__(self, out: _Out, protected: merge.Protected) -> None:
        self.out = out
        self.protected = protected

    def blocks(self, nodes: Sequence[SyntaxTreeNode], indent: str, separator: str) -> None:
        """Write blocks; the cursor is already indented for the first one."""
        for i, node in enumerate(nodes):
            if i:
                self.out.add(separator + indent)
            self.block(node, indent)

    def block(self, node: SyntaxTreeNode, indent: str) -> None:
        match node.type:
            case "paragraph" | "heading":
                self.inline(node.children[0].children, indent)
            case "bullet_list" | "ordered_list":
                start = int(node.attrs.get("start", 1))
                for n, item in enumerate(node.children):
                    if n:
                        self.out.add("\n" + indent)
                    marker = "- " if node.type == "bullet_list" else f"{start + n}. "
                    self.out.add(marker)
                    self.blocks(item.children, indent + " " * len(marker), "\n")
            case other:
                raise ValueError(f"Unexpected Markdown block {other!r}")

    def inline(self, nodes: Sequence[SyntaxTreeNode], indent: str) -> None:
        for node in nodes:
            match node.type:
                case "text":
                    self.out.add(node.content)
                case "softbreak" | "hardbreak":
                    self.out.add("\n" + indent)
                case "link":
                    href = str(node.attrs["href"])
                    text = self.protected.restore(_plain(node.children))
                    url = self.protected.restore(href)
                    if text == url:
                        self.out.add(href, url=True)
                    elif url.lower() == f"mailto:{text.lower()}":
                        self.inline(node.children, indent)
                    else:
                        self.inline(node.children, indent)
                        self.out.add(" (")
                        self.out.add(href, url=True)
                        self.out.add(")")
                case _:  # strong, em, s, u: keep the text, drop the markers
                    self.inline(node.children, indent)


def _fill(
    parts: Sequence[_Part], values: Mapping[str, str | None], *, as_html: bool
) -> Iterator[str]:
    for part in parts:
        if isinstance(part, str):
            yield part
        elif part.url:
            url = merge.render(part.source, values, mode="url")
            yield html.escape(url) if as_html else url
        else:
            yield merge.render(part.source, values, mode="html" if as_html else "text")


@dataclass(frozen=True)
class PreparedBody:
    """A checked and converted body; ``for_recipient`` fills in the merge fields."""

    html_parts: tuple[_Part, ...]
    text_parts: tuple[_Part, ...]

    def for_recipient(self, values: Mapping[str, str | None]) -> tuple[str, str]:
        """``(html_fragment, plain_text)`` for one recipient."""
        fragment = "".join(_fill(self.html_parts, values, as_html=True))
        for m in _HREF.finditer(
            fragment
        ):  # backstop; the checks in prepare() should make this moot
            if not _SAFE_PREFIX.match(html.unescape(m.group(1))):
                raise ContentError([f"Unsafe link {m.group(1)!r} in the merged message."])
        return fragment, "".join(_fill(self.text_parts, values, as_html=False))


def prepare(body: str, theme: Theme = DEFAULT_THEME) -> PreparedBody:
    """Check and convert the body once. Raises ``ContentError`` with every problem found."""
    protected, tree, errors = _parse(body)
    if errors:
        raise ContentError(errors)
    html_out, text_out = _Out(protected), _Out(protected)
    _HtmlRenderer(html_out, styles(theme)).blocks(tree.children)
    _TextRenderer(text_out, protected).blocks(tree.children, "", "\n\n")
    return PreparedBody(tuple(html_out.parts), tuple(text_out.parts))
