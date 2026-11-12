"""Authored Markdown inside the HTML render writes: the converter that turns it into HTML first, links
as MkDocs serves their pages, and text escaped so the page's Markdown leaves it as typed."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit, urlunsplit

from markdown.treeprocessors import Treeprocessor

from ..markdown import site_converter


def _as_published(href: str) -> str:
    """MkDocs serves ``README.md`` as ``index.html``, and a README spelled
    otherwise, such as ``readme.md``, under its own name.

    Links it parses are rewritten for us, but raw HTML never reaches it, so it
    has to name the published file itself.
    """
    # As a string, so a leading ./ that keeps a colon from reading as a scheme stays.
    head, _slash, name = href.rpartition("/")
    if name == "README.html":
        return f"{head}/index.html" if head else "index.html"
    return href


class _FenceSafeHtml(HTMLParser):
    """Keep displayed fence markers from opening fences on the second pass.

    HTML entities decode to the exact displayed code/TeX. Raw script/style
    text is not entity-decoded by HTML, so it retains its original spelling.
    """

    _runs = re.compile(r"`{3,}|~{3,}")
    _quoted = re.compile(r"(['\"])(.*?)\1", re.DOTALL)

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    @classmethod
    def escaped(cls, value: str) -> str:
        return cls._runs.sub(lambda match: "".join(f"&#{ord(char)};" for char in match.group()), value)

    def handle_starttag(self, tag, attrs):
        value = self.get_starttag_text()
        # Quoted attribute values also decode entities; names are untouched.
        self.parts.append(self._quoted.sub(
            lambda match: match.group(1) + self.escaped(match.group(2)) + match.group(1), value
        ))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data if self.cdata_elem else self.escaped(data))

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")

    def unknown_decl(self, data):
        self.parts.append(f"<![{data}{']]>' if data.startswith('CDATA[') else ']>'}")


class _PublishedBodyLinks(Treeprocessor):
    """Give parsed Markdown links their final URLs before they become HTML.

    MkDocs does not rewrite links inside raw HTML. Authored raw HTML links are
    not Markdown tree elements, so their spelling remains untouched.
    """

    def __init__(self, md):
        super().__init__(md)
        self.validation_targets: list[tuple[str, str]] = []

    def run(self, root):
        for element in root.iter():
            if element.tag not in {"a", "img"}:
                continue
            key = "href" if element.tag == "a" else "src"
            href = element.get(key, "")
            try:
                parts = urlsplit(href)
            except ValueError:
                self.validation_targets.append((element.tag, href))
                continue
            if not parts.scheme and not parts.netloc:
                # Keep the original parsed link/image and .md destination for
                # MkDocs' configured missing-file/anchor checks on the final
                # page. Raw authored HTML links are not tree elements here.
                self.validation_targets.append((element.tag, href))
                if (element.tag == "a" and not parts.path.startswith("/")
                        and parts.path.lower().endswith(".md") and "\0" not in unquote(parts.path)):
                    path = _as_published(parts.path[:-3] + ".html")
                    element.set("href", urlunsplit(parts._replace(path=path)))


@lru_cache(maxsize=1)
def _body_converter():
    converter = site_converter()
    # The final page assigns heading IDs once, across all its statements.
    # attr_list still keeps an author's explicit {#id} here.
    converter.treeprocessors.deregister("toc")
    # After unescaping (priority 0), as MkDocs reads links: before it, a
    # destination written `a\_b.md` still holds an escape placeholder.
    converter.treeprocessors.register(_PublishedBodyLinks(converter), "published-body-links", -1)
    return converter


def _convert_body(
    text: str, references: Mapping[str, tuple[str, str]] | None = None,
) -> tuple[str, dict[str, tuple[str, str]], list[tuple[str, str]]]:
    """*text* converted as on the final page, with the reference definitions it
    knew and the links it left for MkDocs to check.

    One converter is reused, as markdown's render_html reuses its own. Its reset
    clears the definitions but not the links collected, and a conversion that
    fails midway leaves it unable to convert the next text.
    """
    converter = _body_converter()
    converter.reset()
    links = converter.treeprocessors["published-body-links"]
    links.validation_targets = []
    if references:
        converter.references.update(references)
    try:
        rendered = converter.convert(text)
    except Exception:
        _body_converter.cache_clear()
        raise
    return rendered, dict(converter.references), links.validation_targets


def _protect_markdown_in_html(
    text: str, *, references: Mapping[str, tuple[str, str]] | None = None, force: bool = False,
) -> str:
    """Parse an affected authored chunk before introducing HTML containers.

    HTML extraction precedes arithmatex and indented code. Inside markdown=1,
    r<n can consume the container's closing tag and lose its whole suffix.
    Parsing the fragment first lets the configured Markdown engine distinguish
    code, math, HTML and autolinks and emit the correct escaping for each.
    The vault and generated wrapper/navigation markup are never rewritten.
    """
    if not force and "<" not in text:
        return text
    rendered, _, targets = _convert_body(text, references)
    if "```" in rendered or "~~~" in rendered:
        protected = _FenceSafeHtml()
        protected.feed(rendered)
        protected.close()
        rendered = "".join(protected.parts)
    if targets:
        # The visible fragment is raw HTML, which MkDocs does not validate.
        # Native Markdown witnesses retain the original validation behavior,
        # including images, decoded paths, queries, fragments and warning
        # levels. A hidden subtree prevents extra layout or keyboard targets;
        # whitespace-only labels add no visible text.
        witnesses = []
        for kind, href in dict.fromkeys(targets):
            target = f"<{href}>" if "<" not in href and ">" not in href else href
            witnesses.append(f"{'!' if kind == 'img' else ''}[ ]({target})")
        rendered += '\n\n<div class="bp-body-link-validation" hidden markdown="1">\n\n'
        rendered += "\n\n".join(witnesses) + "\n\n</div>"
    return rendered


def _protect_narrative_markdown(
    text: str, *, references: Mapping[str, tuple[str, str]] | None = None, force: bool = False,
    page_header: bool = True,
) -> str:
    """Protect authored narrative, never an already assembled renderer page.

    Metadata remains metadata. The canonical converter handles only affected
    authored ranges; normal Markdown retains MkDocs' own link/anchor processing.
    """
    if not force and "<" not in text:
        return text
    lines = text.splitlines(keepends=True)
    end = 0
    if page_header and lines and lines[0].strip() == "---":
        end = next((index + 1 for index in range(1, len(lines)) if lines[index].strip() == "---"), 0)
    header, body = "".join(lines[:end]), "".join(lines[end:])
    return header + _protect_markdown_in_html(body, references=references, force=force)


#: What the page's Markdown would act on in raw HTML: it ends a line at a
#: carriage return as at a line feed, and expands tabs from its own columns.
#: Character references for them it leaves alone.
_AS_TYPED = str.maketrans({"\r": "&#13;", "\t": "&#9;"})
_ONE_LINE = str.maketrans({"\r": "&#13;", "\n": "&#10;", "\t": "&#9;"})


def _typed(text: str, *, one_line: bool = False) -> str:
    """``text`` as HTML the page's Markdown leaves as typed: escaped, with
    fence markers, carriage returns and tabs as character references, and
    with ``one_line`` its line feeds too."""

    return _FenceSafeHtml.escaped(html.escape(text)).translate(_ONE_LINE if one_line else _AS_TYPED)
