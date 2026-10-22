"""What the site publishes of an article: the Markdown its converter may render."""

from __future__ import annotations

import html
import re
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from typing import Iterable

import markdown as markdown_renderer
from markdown.extensions.attr_list import get_attrs_and_remainder

from .markdown import (
    FORMULA_CLASS,
    SITE_EXTENSION_CONFIGS,
    SITE_EXTENSIONS,
    INLINE_CODE,
    PAGE_HEADER,
    content_lines,
    published_markdown,
    published_page,
)
from .mathjax import NEUTRALISED, TEX_MACROS


#: Raw HTML in CommonMark's grammar: open and closing tags, and the openers of
#: comments, declarations, and processing instructions, closed or not.
_HTML_TAG = re.compile(
    r"<\?[^>\n]*>?|<![A-Za-z\[][^>\n]*>?"
    r"|</?[A-Za-z][A-Za-z0-9-]*"
    r"(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*(?:\s*=\s*(?:[^\s\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*\s*/?>"
)
_HTML_NAME = re.compile(r"<[!?](?:\[CDATA\[|[A-Za-z]*)|</?[A-Za-z][A-Za-z0-9-]*")
#: Character references as the site's Markdown converter reads them, which
#: takes a number without its semicolon and any name with one.
_LOOSE_HTML_ENTITY = re.compile(r"&(?:#[0-9]+;?|#[xX][0-9A-Fa-f]+;?|[A-Za-z][A-Za-z0-9]*;)")
#: A character reference by number, its leading zeros apart from its digits.
_NUMERIC_REFERENCE = re.compile(r"&#(?:([xX])0*([0-9A-Fa-f]+)|0*([0-9]+))")
#: How to write what raw HTML in a page was reached for.
_RAW_HTML_FIX = (
    "write it in Markdown (a blank line or a trailing backslash for a break, a heading or list for a section), "
    "or in code to show it as typed; in a formula, put a space after <"
)


def _raw_html_errors(text: str, code: Iterable[str] = ()) -> tuple[str, ...]:
    """Name the raw HTML in ``text`` outside the pieces of ``code`` the site's
    converter shows as code: tags, comments, declarations, processing
    instructions, and character references a browser reads, formulas
    included; a space after "<" keeps a formula clear.

    Each is found in the source and excused only by as many occurrences of it
    in code. A reference a browser shows as typed, such as ``&D;``, which
    names no character, is no HTML."""

    pieces = list(code)
    errors: list[str] = []
    if text.count("<!--") > sum(piece.count("<!--") for piece in pieces):
        errors.append("HTML comments are not allowed: they hide the text they enclose")
    tags = Counter(html_names(text))
    for piece in pieces:
        tags.subtract(html_names(piece))
    names = [name for name, count in tags.items() if count > 0]
    if names:
        errors.append("raw HTML is not allowed: " + ", ".join(names) + "; " + _RAW_HTML_FIX)
    # References are counted as the converter read them, cut to a length
    # Python converts, and named as they were written.
    written: dict[str, str] = {}
    references: Counter[str] = Counter()
    for entity in _LOOSE_HTML_ENTITY.findall(text):
        read = _bounded_references(entity)
        written.setdefault(read, entity)
        references[read] += 1
    for piece in pieces:
        references.subtract(_bounded_references(entity) for entity in _LOOSE_HTML_ENTITY.findall(piece))
    entities = [
        entity_name(written[read]) for read, count in references.items() if count > 0 and _live_reference(read)
    ]
    if entities:
        errors.append(
            "HTML character references are not allowed: " + ", ".join(entities) + "; type the character itself"
        )
    return tuple(errors)


def html_names(text: str) -> list[str]:
    """The name of each piece of HTML :data:`_HTML_TAG` finds in ``text``.

    A declaration or processing instruction runs, by the pattern, to the
    first ``>``, which a reader of the vault and the site's converter each
    place elsewhere; the tags written inside one are named too, so none goes
    unmentioned."""

    names: list[str] = []
    position = 0
    while (markup := _HTML_TAG.search(text, position)) is not None:
        name = _HTML_NAME.match(text, markup.start()).group()
        if name.startswith(("<!", "<?")):
            names.append(name)
            position = markup.start() + len(name)
        else:
            names.append(name + ">")
            position = markup.end()
    return names


#: An attribute list as written, around what the site's converter reads.
_ATTRIBUTE_LIST = r"\{{:?[ ]*{}[ ]*\}}"


def publishable_article(text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """The page a site may publish for an article, what keeps it from being
    published as written, and the notation its formulas define.

    Articles are Markdown and TeX. Raw HTML in one would reach the published
    page as markup that can restyle it, show an approval nobody gave, or run
    script, and the reviewers who approve articles do not own the site's
    templates. The page is :func:`~autoform_cli.markdown.published_page`'s:
    the frontmatter, which the theme would print unescaped, is replaced, and
    complete HTML comments outside code, a common way to leave a note, are
    dropped rather than reported, so a comment a browser would end sooner than
    the pattern does exposes nothing; one in code is shown as typed, as in the
    vault. What remains is reported when :func:`_raw_html_errors` finds HTML in
    it outside code, or when the site's Markdown renderer would pass any of it
    through as raw HTML: the two parse markup differently, and the renderer's
    reading is what gets published. Every piece the site can publish is read,
    as :func:`~autoform_cli.markdown.published_markdown` gives it, so the text
    checked is the text published.

    And it is reported for an attribute list, or a code fence's braces that do
    more than name its language, which could give its text an id, a class or
    a style that makes it look like the site's own marks, cover the page, or
    be drawn as a diagram.

    And it is reported for a link, an image, or a link definition, used or
    not, that leads anywhere but the site's own pages and http, https, and
    mailto addresses: a ``javascript:`` or ``data:`` one runs or shows what
    it holds on the site's origin.

    Render publishes a page with any of these as :func:`shown_as_typed`, and
    check warns about each; neither stops. The notation is a separate list:
    a formula that defines a command, or uses one the site turns off, shows
    an error in red where it stands, and nothing else on the page changes.
    """

    visible = published_page(text)
    errors: list[str] = []
    attributes: list[tuple[str, str]] = []
    defining: dict[str, None] = {}
    for source, markdown in published_markdown(visible):
        rendered, passed, assigned, unclosed, defined = _rendered_article(markdown)
        reading = _RenderedText()
        reading.feed(rendered)
        reading.close()
        defining.update(dict.fromkeys(_TEX_DEFINITION.findall("\n".join(reading.formulas))))
        # A formula the converter read is escaped as code is, so its text
        # excuses the markup it spells, as a code span's does.
        shown = [*reading.code, *reading.formulas]
        found = [_lines_naming(text, _named_markup(error)) + error for error in _raw_html_errors(source, shown)]
        if passed and not found:
            blocks = dict.fromkeys(passed)
            shown = ", ".join(repr(block[:40]) for block in blocks)
            starts = [re.compile(re.escape(line.strip())) for block in blocks for line in block.splitlines()[:1] if line.strip()]
            named = _lines_naming(text, starts)
            opened = _lines_naming(text, [_TAG_OPENING])
            if not named and opened:
                # What passed is the site's own markup, which a < before a
                # letter, as in a formula, opened a tag that runs on over.
                found.append(
                    opened
                    + "raw HTML is not allowed: the site would read a < before a letter as the start of a tag, "
                    + f"and publish {shown} after it as HTML; in a formula, put a space after <, as in $a < b$"
                )
            else:
                found.append(
                    named
                    + f"raw HTML is not allowed: the site would publish {shown} as HTML; write it in Markdown, or in code to show it as typed"
                )
        if unclosed is not None:
            opener = unclosed.strip()
            numbers = [number for number, line in enumerate(text.splitlines(), start=1) if line.expandtabs(4).strip() == opener]
            fence = re.match(r"[> ]*(`{3,}|~{3,})", unclosed)
            found.append(
                (f"line {numbers[-1]}: " if numbers else "")
                + "code fence is not closed: the site would read it on into what autoform render puts after this "
                + f"text, to the next fence on the page; close it with a line of {fence.group(1) if fence else '```'}"
            )
        schemes = list(dict.fromkeys(filter(None, map(_unsafe_scheme, [*reading.destinations, *defined]))))
        if schemes:
            found.append(
                _lines_naming(text, [re.compile(re.escape(scheme) + r"[\t\n\r]*:", re.IGNORECASE) for scheme in schemes])
                + "links are not allowed to " + ", ".join(f"{scheme}:" for scheme in schemes)
                + " addresses: the site links only to its own pages and to http:, https:, and mailto: addresses; "
                + "link to a page or such an address, or put the address in code to show it as typed"
            )
        errors.extend(found)
        attributes.extend(assigned)
    errors.extend(_attribute_errors(text, attributes))
    notation: tuple[str, ...] = ()
    if defining:
        named = [re.compile(re.escape(f"\\{name}") + "(?![A-Za-z])") for name in defining]
        notation = (
            _lines_naming(text, named)
            + "formulas define or use "
            + ", ".join(f"\\{name}" for name in defining)
            + ": the site ignores notation an article defines and turns these commands off, so each shows "
            + f"an error in red; put the project's notation in blueprint/{TEX_MACROS}",
        )
    return visible, tuple(dict.fromkeys(errors)), notation


#: A command a formula would use to change the formulas around it, or one the
#: site's MathJax turns off; :mod:`~autoform_cli.mathjax` lists why.
_TEX_DEFINITION = re.compile(
    r"\\("
    + "|".join(
        sorted(
            {"newcommand", "renewcommand", "providecommand", "def", "gdef", "edef", "xdef", "let", "require"}
            | set(NEUTRALISED),
            key=len,
            reverse=True,
        )
    )
    + r")(?![A-Za-z])"
)


def shown_as_typed(text: str, *, page: bool = True) -> str:
    """``text``, Markdown that :func:`publishable_article` reported, published
    as typed: every character escaped in one preformatted block, under a note
    that says why, so none of it becomes markup, a link, or a formula. The
    header that stands for a page's frontmatter is dropped first; with
    ``page``, the result is a whole page under a header of its own."""

    note = (
        '<p class="bp-as-typed-note">autoform render shows this text as typed: it holds markup the site does '
        "not publish. Run <code>autoform check</code> to see what.</p>"
    )
    body = text.removeprefix(PAGE_HEADER)
    block = f'{note}\n<pre class="bp-as-typed">{html.escape(body.strip(chr(10)), quote=False)}</pre>\n'
    return (PAGE_HEADER + "\n" + block) if page else block


#: The schemes a published link may name. One with none is relative, so it
#: stays on the site, or protocol-relative, which a browser opens over http or https.
_LINK_SCHEMES = frozenset({"http", "https", "mailto"})
#: A URL's scheme as a browser reads it, once it has dropped the tabs and
#: line breaks in the URL and the spaces and control characters around it.
_URL_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):")


def _unsafe_scheme(url: str) -> str | None:
    """The scheme of ``url``, lowercased, unless a published link may name
    it; ``None`` for a URL with none: a relative link, which stays on the
    site, or a protocol-relative one, which a browser opens over http or https.
    Any other, ``javascript:`` and ``data:`` among them, could run script
    on the site's origin or show a page it does not serve."""

    read = re.sub(r"[\t\n\r]", "", url.strip("".join(map(chr, range(0x21)))))
    scheme = _URL_SCHEME.match(read)
    if scheme is None or scheme.group(1).lower() in _LINK_SCHEMES:
        return None
    return scheme.group(1).lower()


#: A ``<`` the site's converter may read as opening a tag.
_TAG_OPENING = re.compile(r"<[A-Za-z]")


def _lines_naming(text: str, patterns: list[re.Pattern[str]]) -> str:
    """A ``line N: `` or ``lines N, M: `` prefix naming the lines of ``text``
    one of ``patterns`` matches: outside code and comments first, a formula
    written as code, ``$`...`$``, counting as outside, and anywhere when that
    finds none; empty when no line has one."""

    def outside(line: str) -> str:
        def kept(code: re.Match[str]) -> str:
            formula = line[code.start() - 1 : code.start()] == "$" == line[code.end() : code.end() + 1]
            return code.group() if formula else ""

        return INLINE_CODE.sub(kept, line)

    shown = [outside(line) for line in content_lines(text)]
    for lines in (shown, text.splitlines()):
        numbers = [str(number) for number, line in enumerate(lines, start=1) if any(p.search(line) for p in patterns)]
        if numbers:
            return f"{'line' if len(numbers) == 1 else 'lines'} {', '.join(numbers)}: "
    return ""


def _named_markup(error: str) -> list[re.Pattern[str]]:
    """Patterns for the markup a :func:`_raw_html_errors` message names."""

    if error.startswith("HTML comments"):
        return [re.compile(re.escape("<!--"))]
    # The names run from the first ": " to the "; " before the fix. A
    # reference is shown with what it reads as, and a long one cut short.
    listed = error.partition(": ")[2]
    listed = listed.removesuffix("; " + _RAW_HTML_FIX) if listed.endswith(_RAW_HTML_FIX) else listed.rpartition("; ")[0]
    names = [name.split(" ")[0].removesuffix("...") for name in listed.split(", ")]
    return [
        re.compile(re.escape(name[:-1]) + "(?![A-Za-z0-9-])", re.IGNORECASE)
        if name.startswith("<") and name.endswith(">") and not name.startswith(("<!", "<?"))
        else re.compile(re.escape(name), re.IGNORECASE)
        for name in names
    ]


def _attribute_errors(text: str, attributes: Iterable[tuple[str, str]]) -> list[str]:
    """Name each attribute list the site's converter applied in ``text``, and
    each code fence's braces that set more than its language, by its line."""

    errors: list[str] = []
    for tag, written in attributes:
        reason = (
            'is not allowed: a code fence may set only its language and options such as title, as in "```lean" '
            'or "```{.lean}"; drop its classes, id, and attributes'
            if tag == "fence"
            else "is not allowed: it would give the text an id, a class, or a style of its own; delete it"
        )
        errors.extend(f"{where}attribute list {shown} {reason}" for where, shown in _attribute_places(text, written))
    return errors


def _attribute_places(text: str, written: str) -> list[tuple[str, str]]:
    """Where in ``text`` an attribute list reading ``written`` is, as a
    ``line N: `` prefix, and the list as it is written there; outside code
    first, and anywhere when it is in none."""

    pattern = re.compile(_ATTRIBUTE_LIST.format(re.escape(written.strip())))
    for lines in (content_lines(text), text.splitlines()):
        places = [
            (f"line {number}: ", found.group()[:80])
            for number, line in enumerate(lines, start=1)
            if (found := pattern.search(line)) is not None
        ]
        if places:
            return places
    return [("", "{" + written.strip()[:78] + "}")]


def _rendered_article(
    text: str,
) -> tuple[str, list[str], list[tuple[str, str]], str | None, list[str]]:
    """The site's rendering of ``text``, what its renderer passes through
    from ``text`` as raw HTML, each attribute list it applies, the line
    that opens a code fence at the margin still open where ``text`` ends, if
    one is, and the destination of each link definition in it, used or not:
    a page is converted whole, so another text on it may use one. An
    attribute list is given as the element's tag and the list as read; a
    fence's braces that set more than its language count as one, with
    ``fence`` for the tag.

    The renderer reads fences over a whole page before anything else, so a
    fence a piece leaves open is ended by whatever fence comes next on the
    page it is published on, and everything between, the site's own markup
    included, is code; the text after that fence is read anew.

    The renderer stashes raw HTML, character references, and highlighted code
    blocks alike. Code blocks are stashed while fences are read, before any
    raw HTML is, so whatever is stashed after that came from the text itself;
    a reference a browser shows as typed is left out, as is the empty
    placeholder a ``markdown="1"`` box leaves. References by number are cut
    to a length Python converts first, standing for what they did.
    """

    parser = markdown_renderer.Markdown(extensions=list(SITE_EXTENSIONS), extension_configs=SITE_EXTENSION_CONFIGS)
    fences = parser.preprocessors["fenced_code_block"]
    read_fences = fences.run
    highlighted = 0
    unclosed: str | None = None

    def counted(lines: list[str]) -> list[str]:
        nonlocal highlighted, unclosed
        read = read_fences(lines)
        # An indented or quoted fence ends at the first line the site puts
        # after the text, which starts at the margin; one at the margin runs on.
        if fences.fence is not None and not fences.ws_virtual_len and not fences.quote_level:  # type: ignore[attr-defined]
            # Every line after the opener is the open fence's content.
            unclosed = lines[len(lines) - len(fences.code) - 1]  # type: ignore[attr-defined]
        highlighted = len(parser.htmlStash.rawHtmlBlocks)
        return read

    fences.run = counted  # type: ignore[method-assign]
    attribute_lists = parser.treeprocessors["attr_list"]
    assign = attribute_lists.assign_attrs  # type: ignore[attr-defined]
    assigned: list[tuple[str, str]] = []

    def recorded(element: object, written: str, *, strict: bool = False) -> str:
        # A strict list with text it cannot read is left in the text.
        if not (strict and get_attrs_and_remainder(written)[1]):
            assigned.append((element.tag, written))  # type: ignore[attr-defined]
        return assign(element, written, strict=strict)

    attribute_lists.assign_attrs = recorded  # type: ignore[attr-defined]
    # A fence's braces are an attribute list too: what they set besides the
    # language lands on the code block's element.
    read_braces = fences.handle_attrs  # type: ignore[attr-defined]

    def braced(match: re.Match[str]) -> bool:
        okay = read_braces(match)
        if okay and (fences.classes or fences.id or fences.attrs):  # type: ignore[attr-defined]
            assigned.append(("fence", match.group("attrs")))
        return okay

    fences.handle_attrs = braced  # type: ignore[attr-defined]
    rendered = parser.convert(_bounded_references(text))
    passed = [
        block if isinstance(block, str) else "<element>"
        for block in parser.htmlStash.rawHtmlBlocks[highlighted:]
        if block != ""
    ]
    return rendered, [
        block for block in passed if not (_LOOSE_HTML_ENTITY.fullmatch(block) and not _live_reference(block))
    ], assigned, unclosed, [html.unescape(url) for url, _title in parser.references.values()]


class _RenderedText(HTMLParser):
    """The text of a rendered article's code, each piece of which shows as
    typed, and where each of its links and images leads, as a browser reads
    it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.code: list[str] = []
        self.destinations: list[str] = []
        #: The TeX of each formula the site's MathJax reads.
        self.formulas: list[str] = []
        self._open: list[str] = []
        self._formula: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.destinations.extend(value for name, value in attrs if name in {"href", "src"} and value is not None)
        if tag in {"code", "pre"}:
            if not self._open:
                self.code.append("")
            self._open.append(tag)
        if self._formula or FORMULA_CLASS in (dict(attrs).get("class") or "").split():
            if not self._formula:
                self.formulas.append("")
            self._formula.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._open:
            del self._open[len(self._open) - 1 - self._open[::-1].index(tag) :]
        if tag in self._formula:
            del self._formula[len(self._formula) - 1 - self._formula[::-1].index(tag) :]

    def handle_data(self, data: str) -> None:
        if self._open:
            self.code[-1] += data
        if self._formula:
            self.formulas[-1] += data


def _bounded_references(text: str) -> str:
    """``text`` with each character reference by number written in at most
    eight digits, standing for what it did: a number past the last code point
    stands for U+FFFD however long it is, and Python refuses to convert one
    of more than 4300 digits."""

    def bounded(reference: re.Match[str]) -> str:
        if reference[1]:
            return f"&#{reference[1]}{reference[2] if len(reference[2]) <= 6 else 'FFFFFFF'}"
        return f"&#{reference[3] if len(reference[3]) <= 7 else '99999999'}"

    return _NUMERIC_REFERENCE.sub(bounded, text)


def _live_reference(entity: str) -> bool:
    """Whether a browser shows ``entity`` as something other than its text:
    any reference by number does, and a name does when HTML defines it or the
    older name it starts with, as ``&notit;`` starts with ``&not``."""

    return entity.startswith("&#") or html.unescape(entity) != entity


def entity_name(entity: str) -> str:
    """``entity``, cut short when long, and, when it stands for one character,
    that character's code point and name."""

    character = html.unescape(_bounded_references(entity))
    shown = entity if len(entity) <= 40 else f"{entity[:16]}... ({len(entity)} characters)"
    if len(character) != 1:
        return shown
    return f"{shown} (U+{ord(character):04X} {unicodedata.name(character, 'unnamed')})"


__all__ = ["publishable_article", "shown_as_typed"]
