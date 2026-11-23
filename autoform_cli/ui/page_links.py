"""Links from a page: to each node, to the files of `sources/` the site reaches in the repository, and the
relative links an author writes, rewritten for where the page is published."""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .._fs import is_within as _is_within
from .._fs import relative as _relative
from .._fs import resolve as _resolve
from ..graph import SOURCES_DIR
from ..lean import SourceLinker
from ..markdown import (
    _BRACKET_ESCAPE,
    _CODE_SPAN,
    _INLINE_MATH,
    _LABEL_ALONE,
    _blank,
    _blank_code,
    _definition_at,
    _display_formula,
    _unquoted,
    reference_images,
    reference_links,
    relative_link,
)
from ..markdown import _LINK_DEFINITION as _SITE_LINK_DEFINITION
from ..markdown import content_lines as _content_lines
from ..markdown import outside_fences as _outside_fences
from ..markdown import text_blocks as _text_blocks
from .capture import _is_hidden
from .markdown_html import _as_published, _body_converter
from .site_files import _PUBLISHED_SOURCE_SUFFIXES


# Links are matched in a line masked by _link_mask, where brackets in code such
# as ``[0,1]^n`` and in math such as ``$(0,1]$`` are blanks. Any other bracket in
# a label must be paired, so an earlier "[1]" or "[x]" on the line stays text.
_LINK_LABEL = r"(?:[^\[\]]|\[[^\[\]]*\])*"
#: An inline link. A bare destination or a title may hold one level of
#: balanced parentheses, a quoted title may leave a `(` open, and an escaped
#: parenthesis is text, as in Python-Markdown. A rewrite drops the title of a
#: link it moves, so a match that ran past the link's own `)` would drop text.
_MARKDOWN_LINK = re.compile(
    rf"(?<!!)\[(?P<label>{_LINK_LABEL})\]\(\s*"
    r"(?P<target><[^>]+>|(?:\\.|[^\\()\s]|\((?:\\.|[^\\()\s])*\))+)"
    r"""(?:\s+(?:"[^")]*"\s*|'[^')]*'\s*|(?:\\.|[^\\()]|\((?:\\.|[^\\()])*\))*))?\)"""
)
#: An inline image, read as an inline link is.
_MARKDOWN_IMAGE = re.compile("!" + _MARKDOWN_LINK.pattern.removeprefix("(?<!!)"))
#: A reference-style link definition, `[label]: target "title"`. Markdown
#: resolves `[Paper][paper]` through one of these, so a rewrite that only sees
#: inline links leaves the destination behind and publishes a dead link. As in
#: Python-Markdown, at most a quoted or parenthesized title follows the target:
#: a line such as `[^1]: Grounded in [Paper](...)` is prose, not a definition.
#: Python-Markdown expands tabs before it reads one, so a tab around the title
#: counts as spaces do.
_LINK_DEFINITION = re.compile(
    r'^(?P<indent>[ ]{0,3})\[(?P<label>[^\]]+)\]:[ \t]*'
    r'(?P<target><[^>\r\n]+>|[^\s]+)'
    r'(?P<rest>[ \t]*(?:(?P<quote>["\'])(?:.*)(?P=quote)[ \t]*|\(.*\)[ \t]*)?)$'
)
#: Quote and list markers, in any order, before a definition the site reads in
#: a quote or a list item, matched once tabs are expanded. The block reader
#: joins a line that carries on a paragraph to it, so a marker on a line of its
#: own opens a quote or an item.
_DEFINITION_CONTAINER = re.compile(r"^(?: {0,3}(?:> ?|(?:[*+-]|\d+\.) +))+")
#: A block marker an image's kept text may start with: a heading or quote
#: marker, a bullet or rule character, or an ordered item's period.
_KEPT_TEXT_MARKER = re.compile(r"^ {0,3}\d*(?P<char>(?<=\d)\.|(?<!\d)[#>*+-])")


def _sources_base(
    blueprint: Path, repo_root: Path, linker: SourceLinker, tracked: Sequence[str] | None = None
) -> "_SourceBase | None":
    """Where `blueprint/sources/` lives in the repository, if it can be linked.

    Source notes are pages of the site, with the images they show. The rest of
    the folder, scans, extracts and data, is reached in the repository, as are
    a hidden note and the folder itself, so the site does not mirror it.
    *tracked*, every file the commit holds below the blueprint, says which
    files there a link can reach.

    Returns ``None`` when there are no repository coordinates to build a link
    from, or the blueprint's files are not in the linked commit. The folder is
    then published as any other, because a site with no way to reach its scans
    is worse than one that copies them.
    """
    if not linker.repository_url or not linker.ref or not linker.links_blueprint:
        return None
    try:
        relative = (blueprint / SOURCES_DIR).resolve().relative_to(repo_root).as_posix()
    except ValueError:
        # The vault is outside the repository being linked, so no blob URL
        # describes it. Better no link than one that 404s.
        return None
    spellings: dict[str, str] = {}
    for path, committed in linker.spellings.items():
        # A link may name a folder as well as a note, so each leading path maps too.
        parts, committed_parts = path.split("/"), committed.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = unicodedata.normalize("NFC", "/".join(parts[:depth]))
            spellings.setdefault(prefix, "/".join(committed_parts[:depth]))
    held = None
    if tracked is not None:
        # A link may name a folder as well as a file, so each leading path counts too.
        held = frozenset(
            "/".join(parts[:depth])
            for parts in (unicodedata.normalize("NFC", name).split("/") for name in tracked)
            if parts[0] == SOURCES_DIR
            for depth in range(1, len(parts) + 1)
        )
    return _SourceBase(linker.repository_url, linker.ref, relative, spellings, held)


@dataclass(frozen=True, slots=True)
class _SourceBase:
    """Where `blueprint/sources` lives in the repository, as parts.

    Kept as parts rather than a formatted URL because a file link and a
    directory link differ by one path segment -- GitHub serves directories
    under `/tree/`, files under `/blob/`. Deriving one from the other by
    replacing the first `/blob/` in the finished string rewrote the wrong
    segment whenever the repository's own URL contained one.
    """

    repository_url: str
    ref: str
    relative: str
    spellings: Mapping[str, str] = field(default_factory=dict)
    """Committed captured paths and their leading folders, keyed by NFC form, since a link spells a path as typed."""
    held: frozenset[str] | None = None
    """What the commit holds under `sources/`, files and their leading folders, below the blueprint in NFC form;
    ``None`` when Git did not list it."""

    def holds(self, relative: Path, path: Path) -> bool:
        """Whether the commit holds *relative*, a path below the blueprint that the copy would hold at *path*."""
        if self.held is None:
            # The copy has no hidden path, so a hidden one is taken as held, as check warns it must be.
            return _is_hidden(relative) or os.path.exists(path)
        return unicodedata.normalize("NFC", relative.as_posix()) in self.held

    def href(self, tail: tuple[str, ...]) -> str:
        verb = "blob" if tail else "tree"
        parts = "/".join((self.relative, *tail)).split("/")
        # A folder with no captured file below it, such as one holding only a `.gitkeep`, has no entry of its own,
        # so only the leading folders it shares with a captured file take their committed spelling.
        for depth in range(len(parts), 0, -1):
            committed = self.spellings.get(unicodedata.normalize("NFC", "/".join(parts[:depth])))
            if committed is not None:
                parts[:depth] = committed.split("/")
                break
        path = "/".join(parts)
        return f"{self.repository_url}/{verb}/{quote(self.ref, safe='')}/{quote(path, safe='/')}"


def _source_href(sources_base: _SourceBase, tail: tuple[str, ...]) -> str:
    """A repository URL for a file under ``sources/`` the site does not publish, or for the folder itself."""
    return sources_base.href(tail)


def _anchored_links(
    targets: dict[str, tuple[Path, str]],
    page: Path,
    *,
    extension: str = ".html",
) -> Mapping[str, str]:
    """Link every node to its statement on the published chapter page.

    Use ``.md`` for links MkDocs will parse -- it validates and rewrites those
    itself -- and ``.html`` for raw HTML, which it never sees. A
    statement on the current page is just a fragment. Read only the nodes the
    page shows: links are built when read, and iterating or copying the mapping
    walks the whole graph on every page.
    """
    return _PageLinks(targets, page, extension)


def _node_links(targets: dict[str, tuple[Path, str]], page: Path, node_ids: Iterable[str]) -> dict[str, str]:
    """Link *node_ids* from *page*, as a mapping an explorer page may copy.

    Copying a page's whole :func:`_anchored_links` walks the graph, so doing
    it on every page made render quadratic.
    """
    links = _anchored_links(targets, page)
    return {node_id: links[node_id] for node_id in node_ids}


class _PageLinks(Mapping[str, str]):
    """The links from one page to every node, each built when read.

    A page names a few nodes but *targets* holds the whole graph, and a site
    has a page per node, so building every link up front made rendering
    quadratic in the size of the graph.
    """

    def __init__(self, targets: dict[str, tuple[Path, str]], page: Path, extension: str) -> None:
        self._targets = targets
        self._page = page
        self._resolved_page: Path | None = None
        self._extension = extension
        self._hrefs: dict[Path, str] = {}

    def __getitem__(self, node_id: str) -> str:
        target, anchor = self._targets[node_id]
        # Many nodes share a chapter page, and resolving a path walks the disk,
        # so each target page is linked once. The current page is cached as ""
        # and links as a bare fragment.
        href = self._hrefs.get(target)
        if href is None:
            if self._resolved_page is None:
                self._resolved_page = _resolve(self._page)
            if _resolve(target) == self._resolved_page:
                href = ""
            else:
                href = relative_link(target, self._page, self._extension)
                if self._extension == ".html":
                    href = _as_published(href)
            self._hrefs[target] = href
        return f"{href}#{anchor}" if anchor else href or "#"

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._targets

    def __iter__(self) -> Iterator[str]:
        return iter(self._targets)

    def __len__(self) -> int:
        return len(self._targets)


def _link_mask(line: str, *, quoted: bool = False) -> str:
    """*line*, or a block's lines joined, with its code spans, inline math and bracket escapes blanked.

    The site reads code and math before links and an escaped bracket as text,
    as check does. Blanking keeps every offset, so a link matched in the mask
    reads its label and target back from *line*. With *quoted*, each line's
    quote markers are blanked too, as the site reads a quote's lines without
    them, so a destination on a later line of a quote is found.
    """
    masked = _BRACKET_ESCAPE.sub(_blank, _code_and_math_mask(line))
    if not quoted:
        return masked
    pieces = []
    for piece in masked.split("\n"):
        content = _unquoted(piece)[0]
        pieces.append(" " * (len(piece) - len(content)) + content)
    return "\n".join(pieces)


def _code_and_math_mask(line: str) -> str:
    """*line*, or a block's lines joined, with what the site reads before links blanked: code spans and inline math."""
    return _INLINE_MATH.sub(_blank, _CODE_SPAN.sub(_blank_code, line))


def _inline_destination(line: str, match: re.Match[str]) -> str:
    """The destination of *match*, an inline link or image in *line*, as the site reads it.

    Python-Markdown drops the backslash before a character it escapes, so
    ``base\\_lemma.md`` names ``base_lemma.md``, but not in code or math, which
    it reads first. A reference definition's destination keeps its backslashes
    on the site, so it is read as written.
    """
    start, end = match.span("target")
    escaped = _body_converter().ESCAPED_CHARS
    masked = _code_and_math_mask(line)[start:end]
    dropped = {escape.start() for escape in re.finditer(r"\\(.)", masked) if escape.group(1) in escaped}
    return "".join(char for index, char in enumerate(line[start:end]) if index not in dropped)


class _RepositoryImage(str):
    """An image's destination, among the targets :func:`_rewrite_links` drops,
    that the site links in the repository rather than copying, such as a scan
    under ``sources/`` once render verified the commit."""


class _MissingSource(str):
    """A destination under ``sources/``, among the targets :func:`_rewrite_links`
    drops, that the verified commit does not hold, such as a scan kept out of git."""


class _MissingSourceImage(_MissingSource):
    """The same, as an image's destination."""


def _rewrite_links(
    text: str,
    *,
    source_dir: Path,
    page: Path,
    blueprint: Path,
    destination: Path,
    node_sources: dict[Path, str],
    targets: dict[str, tuple[Path, str]],
    sources_base: "_SourceBase | None" = None,
    left_out: frozenset[Path] = frozenset(),
    unpublished: tuple[Path, ...] = (),
    dropped: list[str] | None = None,
    links: Mapping[str, str] | None = None,
    resolved: dict[str, Path] | None = None,
) -> str:
    """Resolve a page's relative links against where it is being published.

    Three things move. Node files stop being pages once their chapter absorbs
    them, so a link naming one becomes an anchor. A node's body is hoisted out
    of its own directory onto the chapter page, so its remaining relative links
    have to be recomputed from there. And what the site does not publish from
    ``sources/`` (a hidden note, the folder itself, scans and data) is reached
    in the repository when *sources_base* says where; an image of one keeps
    only its text, since a page cannot show a file from there.

    The site holds only the blueprint, so a link or image to a file outside it,
    such as a source note's evidence link to a Lean file, keeps its text and
    loses its target instead of dangling.

    A caller rewriting several texts from one capture may share work between
    them: *links*, which must be :func:`_anchored_links` for this *page*, and
    *resolved*, each path written so far with what it resolved to.
    """
    # A coverage page can name most of the graph, so one mapping serves the
    # whole text and links each target page it reaches once.
    if links is None:
        links = _anchored_links(targets, page, extension=".md")
    if resolved is None:
        resolved = {}
    # A reference image takes its destination from a definition anywhere on the page.
    references = _page_definitions(text)

    def moved_target(raw: str, *, image: bool = False) -> str | None:
        """Where *raw* should point once published, None to leave it alone, or ""
        to keep only the text of each link and image naming it, and drop its definition."""
        bare = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        # The query and fragment are not part of the file's name: they are
        # kept as written, beside the path render moves.
        located, separator, fragment = bare.partition("#")
        path, asked, query = located.partition("?")
        try:
            if not path or urlsplit(path).scheme or path.startswith("/"):
                return None
            written = source_dir / unquote(path)
            # Keyed by the text written: Windows compares paths without
            # regard to case, and a missing file resolves in the case written.
            key = str(written)
            candidate = resolved.get(key)
            if candidate is None:
                candidate = resolved[key] = written.resolve()
        except ValueError:
            # An unbalanced IPv6 bracket or a decoded NUL names no file to
            # point at, so the link is published as written, like a URL.
            return None
        # Capture leaves out hidden paths and unpublished folders, so the site has
        # no page there. A hidden source note is reached through the repository instead.
        try:
            relative = _relative(candidate, blueprint)
        except ValueError:
            relative = None
        if relative is not None and (
            any(_is_within(candidate, folder) for folder in unpublished)
            or (_is_hidden(relative) and relative.parts[:1] != (SOURCES_DIR,))
        ):
            if dropped is not None:
                dropped.append(raw)
            return ""
        node_id = node_sources.get(candidate)
        if node_id is not None:
            href = links[node_id]
            if not targets[node_id][1] and separator:
                href = f"{'' if href == '#' else href}#{fragment}"
            before, hashed, anchor = href.partition("#")  # the query goes before the anchor
            return f"{before}{asked}{query}{hashed}{anchor}"
        if relative is None:
            # Nothing outside the blueprint is published: "" keeps only the text,
            # and its image or definition goes too.
            return ""
        # A file the verified commit lacks under sources/, such as a scan kept
        # out of git, has no repository link and no copy on the site, so its link
        # keeps only its text, with a warning, rather than failing a strict build.
        if (
            sources_base is not None
            and relative.parts[:1] == (SOURCES_DIR,)
            and not sources_base.holds(relative, candidate)
        ):
            if dropped is not None:
                dropped.append(_MissingSourceImage(raw) if image else _MissingSource(raw))
            return ""
        # A note and its images are pages and files of the site; the repository
        # reaches the rest of sources/, a file of a type the site leaves out, and
        # one the commit holds that the checkout lacks.
        if (
            sources_base is not None
            and relative.parts[:1] == (SOURCES_DIR,)
            and (
                _is_hidden(relative)
                or not os.path.exists(candidate)
                or os.path.isdir(candidate)
                or candidate in left_out
                or candidate.suffix.lower() not in _PUBLISHED_SOURCE_SUFFIXES
            )
        ):
            if image:
                # A page cannot show a file from the repository, so its image keeps its text.
                if dropped is not None:
                    dropped.append(_RepositoryImage(raw))
                return ""
            return f"{_source_href(sources_base, relative.parts[1:])}{asked}{query}{separator}{fragment}"
        if candidate in left_out:
            # Nor is a file the site leaves out; its image or definition goes too.
            return ""
        # atlas.json stays off the site, so a link to it keeps only its text, and its definition goes too.
        if relative.as_posix().casefold() == "atlas.json":
            return ""
        published = destination / relative
        # relative_link percent-encodes for raw HTML and explorer payloads.
        # This is Markdown: keep its decoded spelling so an
        # unchanged link stays byte-for-byte unchanged, and ``bracketed`` can
        # protect a moved path containing a space or parenthesis.
        href = unquote(relative_link(published, page, candidate.suffix))
        # Decoded, a first segment that holds a colon, such as a folder named
        # ``javascript:x``, would read as the URL's scheme.
        if ":" in href.split("/", 1)[0]:
            href = f"./{href}"
        return f"{href}{asked}{query}{separator}{fragment}"

    def rewrite(line: str) -> str:
        # A link, an image and a definition each contain a bracket, which
        # masking never adds, so a line without one has nothing to move. A
        # block the site reads as a display formula holds TeX, whatever its
        # brackets look like.
        if "[" not in line or _display_formula(line.split("\n")):
            return line
        # A definition whose target starts on the line after its label moves
        # that target where it stands; one that goes leaves each of its lines
        # their quote markers, so a quote still holds the lines after it.
        lines = line.split("\n")
        if (later := _later_target(lines)) is not None:
            definition, start, stop = later
            href = moved_target(definition.group(2))
            span = definition.group(0).count("\n") + 1
            if href == "":
                first = lines[0].expandtabs(4)
                marker = first[: container.end()] if (container := _DEFINITION_CONTAINER.match(first)) else ""
                kept = [re.sub(r"-(?= *$)", "*", marker)] + [
                    piece[: len(piece) - len(_unquoted(piece)[0])].rstrip() for piece in lines[1:span]
                ]
            elif href:
                href = re.sub(r"\s", lambda space: quote(space.group()), href)
                kept = [lines[0], lines[1][:start] + href + lines[1][stop:], *lines[2:span]]
            else:
                kept = lines[:span]
            return "\n".join([*kept, *([rewrite("\n".join(lines[span:]))] if lines[span:] else [])])
        # A definition whose title is on the next line is one block, and is
        # rewritten a line at a time, as before; one that goes takes its title.
        if "\n" in line and _LINK_DEFINITION.match(line.partition("\n")[0]):
            first, *titles = map(rewrite, line.split("\n"))
            return "\n".join([first, *titles]) if first else "\n" * line.count("\n")
        # The site reads a definition after quote and list markers too, in any
        # order once tabs are expanded, and the lines an item carries on with
        # as one paragraph; one it drops leaves the quote or item empty.
        first, newline, rest = line.partition("\n")
        expanded = first.expandtabs(4)
        container = _DEFINITION_CONTAINER.match(expanded)
        if container is not None and _LINK_DEFINITION.match(inner := expanded[container.end() :]):
            if (written := rewrite(inner)) != inner:
                marker = expanded[: container.end()]
                # A bare `-` on a list's later line would underline the line
                # before it as a heading; `*` keeps the item in the same list.
                first = f"{marker if written else re.sub(r'-(?= *$)', '*', marker)}{written}"
            return f"{first}{newline}{rewrite(rest)}"
        # A reference definition carries the destination for every `[x][label]`
        # on the page. Rewriting only inline links leaves it pointing at the
        # unpublished path and the rendered link dangles.
        definition = _LINK_DEFINITION.match(line)
        # Lines read together are one definition only where the site reads one.
        if definition is not None and "\n" in line and not _SITE_LINK_DEFINITION.match(line):
            definition = None
        if definition is not None:
            href = moved_target(definition.group("target"))
            if href == "":
                # A label over two lines keeps both, so every later line stays where it was written.
                return "\n" * line.count("\n")
            if href:
                indent, label = definition.group("indent"), definition.group("label")
                # Python-Markdown reads a definition's destination only up to
                # whitespace, in angle brackets or not, so whitespace is encoded.
                href = re.sub(r"\s", lambda space: quote(space.group()), href)
                return f"{indent}[{label}]: {href}{definition.group('rest') or ''}"
            return line
        # An image of a file the site leaves out keeps only its alternative text.
        pieces: list[str] = []
        end = 0
        for match in _MARKDOWN_IMAGE.finditer(_link_mask(line, quoted=True)):
            raw = _inline_destination(line, match)
            if moved_target(raw, image=True) == "":
                pieces += [line[end : match.start()], _kept_text(line[match.start("label") : match.end("label")])]
                end = match.end()
        line = "".join(pieces) + line[end:]
        # So does a reference image of a file a page cannot show, or of one whose
        # definition goes. A definition the site links in the repository stays,
        # for a link that uses it.
        for start, text_start, text_end, stop, raw in reversed(reference_images(line, references) if references else []):
            if moved_target(raw, image=True) == "":
                line = line[:start] + _kept_text(line[text_start:text_end]) + line[stop:]
        # A reference link whose definition goes keeps only its text, as an
        # inline link does, escaped as an image's text is.
        for start, text_start, text_end, stop, raw in reversed(reference_links(line, references) if references else []):
            if moved_target(raw) == "":
                line = line[:start] + _kept_text(line[text_start:text_end]) + line[stop:]
        pieces = []
        end = 0
        for match in _MARKDOWN_LINK.finditer(_link_mask(line, quoted=True)):
            raw = _inline_destination(line, match)
            href = moved_target(raw)
            if href is None or raw in (href, f"<{href}>"):  # nothing moved: keep the link as written
                continue
            label = line[match.start("label") : match.end("label")]
            # A destination on a later line keeps the break and quote markers before it, and a title on a
            # later line goes but leaves its breaks, so every later line of a moved link stays where it was
            # written. A link cut down to its label does not, as a break there could end the paragraph.
            lead = line[match.end("label") + 2 : match.start("target")]
            lead = lead if "\n" in lead else ""
            breaks = "\n" * line.count("\n", match.end("target"), match.end())
            pieces += [line[end : match.start()], f"[{label}]({lead}{_bracketed(href)}{breaks})" if href else label]
            end = match.end()
        return "".join(pieces) + line[end:]

    return _outside_fences(text, rewrite, page=True, joined=True)


def _page_definitions(text: str) -> dict[str, str]:
    """The reference definitions the site reads in *text* once
    :func:`_rewrite_links` has rewritten it, by label, each with its target as
    written: one the site reads as written, in a quote too, and one the
    rewrite makes readable, such as a target in angle brackets that holds a
    space. A later definition of a label replaces an earlier one, as on the
    site."""

    # The lines the rewrite reads as definitions, found in the blocks it is
    # given as it finds them, each with the definition it reads there, after
    # any quote or list marker once tabs are expanded.
    rewritten: dict[str, str] = {}

    def collect(line: str) -> str:
        first, newline, rest = line.partition("\n")
        expanded = first.expandtabs(4)
        container = _DEFINITION_CONTAINER.match(expanded)
        if newline and _LINK_DEFINITION.match(first):
            for piece in line.split("\n"):
                collect(piece)
        elif container is not None and _LINK_DEFINITION.match(expanded[container.end() :]):
            rewritten[first] = expanded[container.end() :]
            collect(rest)
        elif _LINK_DEFINITION.match(line) and (not newline or _SITE_LINK_DEFINITION.match(line)):
            rewritten[line] = line
        return line

    _outside_fences(text, collect, page=True, joined=True)
    definitions: dict[str, str] = {}
    lines = _content_lines(text)
    # A label alone starts a definition only where a block does: in a paragraph, a list marker before it is
    # text, and a definition above that took it for its target ends its block below it.
    starts = {first for first, _ in _text_blocks(lines)}
    for index, line in enumerate(lines):
        # The site expands tabs before it reads a definition.
        read = _SITE_LINK_DEFINITION.match(_unquoted(line)[0].expandtabs(4))
        if read is None and index in starts and (later := _later_target(lines[index : index + 3])) is not None:
            read = later[0]
        if read is not None:
            definitions[read.group(1).strip().lower()] = read.group(2)
        elif (inner := rewritten.get(line)) is not None and (written := _LINK_DEFINITION.match(inner)):
            definitions[written.group("label").strip().lower()] = written.group("target")
    return definitions


def _later_target(lines: list[str]) -> tuple[re.Match[str], int, int] | None:
    """The reference definition the site reads where the first of *lines*
    holds only its label, after any quote or list markers, and its target
    starts on the second, with where that target stands in the second line;
    None for any other lines."""

    if len(lines) < 2:
        return None
    first = lines[0].expandtabs(4)
    container = _DEFINITION_CONTAINER.match(first)
    label = first[container.end() :] if container else first
    if not _LABEL_ALONE.match(label):
        return None
    definition = _definition_at([label, *lines[1:3]], (container.group(0) if container else "").count(">"))
    if definition is None:
        return None
    content = _unquoted(lines[1])[0]
    start = len(lines[1]) - len(content.lstrip(" \t"))
    return definition, start, start + len(definition.group(2))


def _kept_text(text: str) -> str:
    """*text*, an image's alternative text or a reference link's, kept where
    the image or link stood. Where it starts a line, the site would read a
    leading ``#``, ``>``, list marker or rule as block markup, so that
    character is escaped."""

    marker = _KEPT_TEXT_MARKER.match(text)
    return f"{text[: marker.start('char')]}\\{text[marker.start('char') :]}" if marker else text


def _bracketed(href: str) -> str:
    """*href* as a Markdown link's destination. A decoded path, or a query or
    fragment kept from angle brackets, can hold a space, a parenthesis or a
    quote. CommonMark allows no space in a bare destination, Python-Markdown
    needs its parentheses balanced, and it reads a quote as opening a title."""

    return f"<{href}>" if re.search(r"[\s()'\"]", href) else href
