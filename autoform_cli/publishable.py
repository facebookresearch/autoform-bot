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
    DIAGRAM_CLASS,
    FORMULA_CLASS,
    SITE_EXTENSION_CONFIGS,
    SITE_EXTENSIONS,
    INLINE_CODE,
    content_lines,
    published_lines,
    published_markdown,
)
from .mathjax import TEX_MACROS, attribute_commands, option_commands, stateful_commands

__all__ = ["entity_name", "html_names", "publishable_article"]

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


#: An HTML comment with its end: the text a site leaves out of an article.
_COMPLETE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

#: The id an article may give a heading with an attribute list.
_HEADING_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")

#: How the ids the site, its theme, and the scripts it loads give their own
#: elements begin; an article's heading may not take one.
_SITE_ID_PREFIXES = ("bp-", "autoform", "mjx-", "mermaid", "md-", "__")

#: An attribute list as written, around what the site's converter reads.
_ATTRIBUTE_LIST = r"\{{:?[ ]*{}[ ]*\}}"
#: The line that opens a fence the site's Markdown makes a Mermaid diagram
#: of: after indentation and blockquote markers, a fence whose language is
#: mermaid, written alone or as a class in its braces.
_MERMAID_DIAGRAM = re.compile(r"^[ \t>]*(?:`{3,}|~{3,})[ \t]*(?:\.?mermaid(?=[ \t]|$)|\{[^}\n]*\.mermaid(?=[ \t}]))")
_DIAGRAM_ERROR = (
    "Mermaid diagrams are not allowed: the site draws only the dependency graphs autoform render makes; "
    "show a diagram's source in a ```text fence instead"
)


def publishable_article(
    text: str, reserved: Iterable[str] = (), *, kind: str = "article"
) -> tuple[str, tuple[str, ...]]:
    """The text of an article a site may publish, and what refuses it.

    Articles are Markdown and TeX. Raw HTML in one would reach the published
    page as markup that can restyle it, show an approval nobody gave, or run
    script, and the reviewers who approve articles do not own the site's
    templates. Complete HTML comments are a common way to leave
    a note, so they are dropped rather than refused; the text returned is the
    article without them, with its lines written as the site writes them, so
    a comment a browser would end sooner than the pattern does exposes
    nothing. What remains is refused when :func:`_raw_html_errors` finds HTML
    in it outside code, or when the site's Markdown renderer would pass any of
    it through as raw HTML: the two parse markup differently, and the
    renderer's reading is what gets published. Every piece the site can
    publish is read, as :func:`~autoform_cli.markdown.published_markdown`
    gives it, so the text checked is the text published.

    It is refused, too, for a TeX command that changes formulas other than
    its own in a formula the site's Markdown marks, which is all the page's
    MathJax reads. The articles on a page are typeset with one TeX input, so
    a definition in one would change what the others show. So is
    ``\\mmlToken``, which colors a symbol as the formula says, like the
    site's marks, and a strike such as ``\\cancel`` that something other
    than its argument follows, which could be an option that colors it.

    And it is refused for an attribute list that does anything but give a
    heading an id, or, in a code fence's braces, name its language, which
    could make its text look like the site's own marks, cover the
    page, or be drawn as a diagram. The id may not look like the site's own, or
    be one of ``reserved``, the ids the site gives its own elements on the
    article's page.

    And it is refused for a link, an image, or a link definition, used or
    not, that leads anywhere but the site's own pages and http, https, and
    mailto addresses: a ``javascript:`` or ``data:`` one runs or shows what
    it holds on the site's origin.

    And it is refused for a Mermaid diagram. The site draws only the graphs
    render writes, with the loose security their links need, which would also
    run a diagram's ``click ... call`` as script and draw its labels' markup.

    ``kind`` names the text in what refuses it: an ``"article"``, or any
    other ``"page"`` an author wrote.
    """

    visible = published_lines(_COMPLETE_HTML_COMMENT.sub("", text))
    errors: list[str] = []
    attributes: list[tuple[str, str, list[tuple[str, str]]]] = []
    diagrams = False
    for source, markdown in published_markdown(visible):
        rendered, passed, assigned, unclosed, defined = _rendered_article(markdown)
        reading = _RenderedText()
        reading.feed(rendered)
        reading.close()
        found = [_lines_naming(text, _named_markup(error)) + error for error in _raw_html_errors(source, reading.code)]
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
        commands = stateful_commands("".join(reading.text))
        if commands:
            found.append(
                _lines_naming(text, _commands(commands))
                + "TeX commands that change other formulas are not allowed: " + ", ".join(commands)
                + f"; define notation in the vault's {TEX_MACROS} instead, and put a command you only name in code"
            )
        commands = attribute_commands("".join(reading.text))
        if commands:
            found.append(
                _lines_naming(text, _commands(commands))
                + "TeX commands that set what a symbol looks like are not allowed: " + ", ".join(commands)
                + "; write the symbol itself, and put a command you only name in code"
            )
        commands = option_commands("".join(reading.text))
        if commands:
            found.append(
                _lines_naming(text, _commands(commands))
                + "TeX strikes must be followed by their argument: " + ", ".join(commands)
                + "; an option in brackets, or one a macro supplies, sets the color and other attributes of the "
                + f"strike, so write {commands[0]}{{...}}"
            )
        errors.extend(found)
        attributes.extend(assigned)
        diagrams = diagrams or reading.diagrams > 0
    errors.extend(_attribute_errors(text, attributes, frozenset(reserved), kind))
    if diagrams:
        lines = [number for number, line in enumerate(text.splitlines(), start=1) if _MERMAID_DIAGRAM.match(line)]
        errors.extend([f"line {number}: {_DIAGRAM_ERROR}" for number in lines] or [_DIAGRAM_ERROR])
    return visible, tuple(dict.fromkeys(errors))


#: The schemes a published link may name; one with none stays on the site.
_LINK_SCHEMES = frozenset({"http", "https", "mailto"})
#: A URL's scheme as a browser reads it, once it has dropped the tabs and
#: line breaks in the URL and the spaces and control characters around it.
_URL_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):")


def _unsafe_scheme(url: str) -> str | None:
    """The scheme of ``url``, lowercased, unless a published link may name
    it; ``None`` for a URL with none, which leads to the site's own pages.
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


def _commands(commands: Iterable[str]) -> list[re.Pattern[str]]:
    """Patterns for the TeX ``commands`` as written, a name not running on
    into the letters after it."""

    return [
        re.compile(re.escape(command) + ("(?![A-Za-z])" if command[1:].isalpha() else ""))
        for command in commands
    ]


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


def _attribute_errors(
    text: str,
    attributes: Iterable[tuple[str, str, list[tuple[str, str]]]],
    reserved: frozenset[str],
    kind: str = "article",
) -> list[str]:
    """Name each attribute list the site's converter applied in ``text`` that
    does more than give a heading an id of its own, or a code fence its
    language, by its line in ``text``, which is an article or a page, as
    ``kind`` says."""

    errors: list[str] = []
    for tag, written, pairs in attributes:
        identifier = pairs[0][1] if len(pairs) == 1 and pairs[0][0] == "id" else None
        if tag == "fence":
            reason = (
                'is not allowed: a code fence may only name its language, as in "```lean" or "```{.lean}"; '
                "keep only the language"
            )
        elif tag not in {"h1", "h2", "h3", "h4", "h5", "h6"} or identifier is None:
            reason = (
                f'is not allowed: {"an article" if kind == "article" else "a page"} may only give a heading an id, '
                'as in "## Title {#title}"; '
                "delete it or keep only a heading's id"
            )
        elif not _HEADING_ID.fullmatch(identifier) or identifier.lower().startswith(_SITE_ID_PREFIXES):
            errors.extend(
                f"{where}heading id {identifier!r} is not allowed: start it with a letter, use only letters, "
                "digits, hyphens, and underscores, up to 64 characters, and do not start it with "
                + ", ".join(_SITE_ID_PREFIXES[:-1]) + f", or {_SITE_ID_PREFIXES[-1]}"
                for where, _ in _attribute_places(text, written)
            )
            continue
        elif identifier in reserved:
            errors.extend(
                f"{where}heading id {identifier!r} is taken: the site gives it to an element of its own on this "
                "page; choose another"
                for where, _ in _attribute_places(text, written)
            )
            continue
        else:
            continue
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
) -> tuple[str, list[str], list[tuple[str, str, list[tuple[str, str]]]], str | None, list[str]]:
    """The site's rendering of ``text``, what its renderer passes through
    from ``text`` as raw HTML, each attribute list it applies, the line
    that opens a code fence at the margin still open where ``text`` ends, if
    one is, and the destination of each link definition in it, used or not:
    a page is converted whole, so another text on it may use one. An
    attribute list is given as the element's tag, the list as read, and the
    attributes it sets; a fence's braces that set more than its language
    count as one, with ``fence`` for the tag and no attributes.

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
    assigned: list[tuple[str, str, list[tuple[str, str]]]] = []

    def recorded(element: object, written: str, *, strict: bool = False) -> str:
        pairs, remainder = get_attrs_and_remainder(written)
        if not (strict and remainder):
            assigned.append((element.tag, written, pairs))  # type: ignore[attr-defined]
        return assign(element, written, strict=strict)

    attribute_lists.assign_attrs = recorded  # type: ignore[attr-defined]
    # A fence's braces are an attribute list too: what they set besides the
    # language lands on the code block's element.
    read_braces = fences.handle_attrs  # type: ignore[attr-defined]

    def braced(match: re.Match[str]) -> bool:
        okay = read_braces(match)
        if okay and (fences.classes or fences.id or fences.attrs):  # type: ignore[attr-defined]
            assigned.append(("fence", match.group("attrs"), []))
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
    """The text of a rendered article: each piece of code, which shows as
    typed, and the text the page's MathJax reads, which is the text of the
    formulas the site's Markdown marked, by :data:`FORMULA_CLASS`, outside
    the elements MathJax skips.

    MathJax reads text a string at a time, and an element ends one, apart
    from ``<wbr>`` and comments; ``<br>`` stands in it for a line break. So a
    line break stands in :attr:`text` for every tag but ``<wbr>``, and a
    command found in it is one MathJax would read."""

    _SKIPPED = frozenset({"annotation", "annotation-xml", "code", "noscript", "pre", "script", "style", "textarea"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.code: list[str] = []
        self.text: list[str] = []
        self._open: list[str] = []
        # The tags open since a formula began, its own first.
        self._formula: list[str] = []
        # How many elements the site's diagram script could draw.
        self.diagrams = 0
        # Where each link and image leads, as a browser reads it.
        self.destinations: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._end_string(tag)
        classes = (dict(attrs).get("class") or "").split()
        if self._formula or FORMULA_CLASS in classes:
            self._formula.append(tag)
        if DIAGRAM_CLASS in classes:
            self.diagrams += 1
        self.destinations.extend(value for name, value in attrs if name in {"href", "src"} and value is not None)
        if tag in self._SKIPPED:
            if tag in {"code", "pre"} and not self._in_code():
                self.code.append("")
            self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        self._end_string(tag)
        if tag in self._open:
            del self._open[len(self._open) - 1 - self._open[::-1].index(tag) :]
        if tag in self._formula:
            del self._formula[len(self._formula) - 1 - self._formula[::-1].index(tag) :]

    def handle_data(self, data: str) -> None:
        if not self._open:
            if self._formula:
                self.text.append(data)
        elif self._in_code():
            self.code[-1] += data

    def _end_string(self, tag: str) -> None:
        if tag != "wbr":
            self.text.append("\n")

    def _in_code(self) -> bool:
        return "code" in self._open or "pre" in self._open


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
