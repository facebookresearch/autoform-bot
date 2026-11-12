"""The Lean a statement shows: its Lean row and code icons, its first declarations with their links and
hover cards, and its names for the site's search."""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from itertools import islice
from urllib.parse import quote

from ..graph import Node
from ..lean import (
    _CONTINUATION_IDENTIFIER,
    _DECLARATION_KEYWORDS,
    _DECLARATION_MODIFIERS,
    Declaration,
    SourceLinker,
    bound_names,
    declaration_names,
    declared_name,
)
from ..library.declarations import _header
from . import dag_viewer
from .markdown_html import _typed


def _lean_presentation(
    node: Node,
    linker: SourceLinker,
) -> tuple[str, list[tuple[str, str]], int, list[str]]:
    """Render direct source icons, falling back to quiet diagnostic metadata.

    A catalog can list hundreds of names, so it gets no icons: its Lean row
    links the first few by name, as its explorer node shows them, and counts
    the rest, which its declaration ledger lists.
    """
    icons: list[str] = []
    fallback: list[str] = []
    linked = 0
    unresolved: list[str] = []
    if not node.lean:
        return "", [], linked, unresolved

    for name in declaration_names(node.lean):
        url = linker.url(name)
        location = linker.location(name)
        code = f"<code>{html.escape(name)}</code>"
        if url and node.catalog == "module":
            fallback.append(f'<a href="{html.escape(url, quote=True)}">{code}</a>')
            linked += 1
        elif url:
            label = html.escape(f"View {name} in Lean source", quote=True)
            icons.append(
                f'<a class="bp-code-link" href="{html.escape(url, quote=True)}" '
                f'aria-label="{label}" title="{label}">{_code_icon()}</a>'
            )
            linked += 1
        elif location is not None:
            fallback.append(f'{code} <span class="bp-where">{html.escape(location.path.as_posix())}</span>')
            linked += 1
        elif ambiguous := linker.index.ambiguity(name):
            fallback.append(f'{code} <span class="bp-missing">defined more than once in the Lean sources</span>')
            unresolved.append(
                f"declaration defined more than once in the Lean sources: {node.id}: {name} ({ambiguous})"
            )
        else:
            fallback.append(f'{code} <span class="bp-missing">not found in the Lean sources</span>')
            unresolved.append(f"declaration not found in the Lean sources: {node.id}: {name}")

    code_links = f'<span class="bp-code-links">{"".join(icons)}</span>' if icons else ""
    shown = fallback[: dag_viewer._CATALOG_LEAN_NAMES] if node.catalog == "module" else fallback
    rest = len(fallback) - len(shown)
    more = f" and {rest} more, listed in its declaration ledger" if rest else ""
    rows = [("Lean", " · ".join(shown) + more)] if fallback else []
    return code_links, rows, linked, unresolved


#: How many of a statement's Lean names show their declaration under it.
_LEAN_BLOCK_NAMES = 3
#: The most lines a declaration shows open; a longer one folds whole.
_LEAN_BLOCK_LINES = 14
#: The most lines of a declaration the card for a name in it shows.
_LEAN_CARD_LINES = 12
#: The Lean a block marks: comments, strings and keywords, each named by its group, and the names it may link.
#: The declaration keywords and modifiers are the index's own.
_LEAN_TOKEN = re.compile(
    r"(?P<comment>/-.*?-/|--[^\n]*)"
    r'|(?P<string>"(?:\\.|[^"\\])*")'
    rf"|(?<![\w.'!?])(?P<keyword>{_DECLARATION_KEYWORDS}|{_DECLARATION_MODIFIERS}|example"
    r"|where|extends|deriving|fun|λ|let|have|show|from|by|at|with|match|if|then|else|do|return|for|in|calc"
    r"|termination_by|decreasing_by|Type|Prop|Sort|sorry)(?![\w'!?])"
    rf"|(?<![\w.'!?»])(?P<name>{_CONTINUATION_IDENTIFIER.pattern})",
    re.DOTALL,
)


#: What follows a field or an argument being given (``mul_comm := …``, ``(n := 3)``) or declared (``base : Nat``).
_GIVEN = re.compile(r"\s*:=")
_DECLARED = re.compile(r"\s*:(?!=)")
#: What comes before a field's or a named argument's name: ``(n := 3)``, ``{ a := 1, b := 2 }``, ``{ s with b := 2 }``.
_SLOT = re.compile(r"(?:[({,]|\bwith)\s*$")
#: An attribute list still open before a name: ``@[simp, ext``.
_ATTRIBUTE = re.compile(r"@\[[^\]]*$")
#: The packages whose modules Mathlib's documentation covers, from its navigation (2026-11-05), and its page for a
#: constant by full name, an exact lookup.
_DOCUMENTED = frozenset(
    {"Aesop", "Archive", "Batteries", "Counterexamples", "ImportGraph", "Init", "Lake", "Lean", "LeanSearchClient",
     "Mathlib", "Plausible", "ProofWidgets", "Qq", "Std"}
)
_MATHLIB_DOCS = "https://leanprover-community.github.io/mathlib4_docs/find/?pattern={}#doc"


def _lean_blocks(
    node: Node,
    linker: SourceLinker,
    *,
    links: Mapping[str, str],
    formalizing: Mapping[str, str],
) -> list[str]:
    """The statement's first Lean declarations as written, under its statement.

    A theorem or lemma shows its statement and not its proof; anything else shows whole, a definition's
    body included. A long declaration folds. A catalog shows none, as it shows no code icons.

    A name in them that the project declares links to the statement that formalizes it, else to its
    source, and the block carries a card for each, hidden, that the book shows on hover.

    With the build's ``.ilean`` file for the declaration's file (:meth:`SourceLinker.names`), a name links
    exactly the constant Lean resolved it to: a project declaration as above, or a constant of a package
    Mathlib's documentation covers to its page there, with no card. Any other name stays text: a local, a
    binder, a ``variable``, a constructor or field being declared, notation, a constant of another package.
    A project constant the index does not hold, such as a constructor, and every name of a declaration without
    usable data, is looked up among the project's own declarations (:meth:`SourceLinker.resolve`).
    """
    if node.catalog is not None:
        return []
    blocks: list[str] = []
    named: dict[str, Declaration] = {}
    found = filter(None, map(linker.excerpt, declaration_names(node.lean or "")))
    for declaration, source in islice(found, _LEAN_BLOCK_NAMES):
        text, indent = source, 0
        if declaration.keyword in ("theorem", "lemma"):
            # The header leaves out the first line's indent, which the build's columns count.
            text, indent = _header(source, 0), len(source) - len(source.lstrip())
        bound = bound_names(text)
        usages = linker.names(declaration, source)
        exact = None if usages is None else {at - indent: use for at, use in usages.items() if 0 <= at - indent < len(text)}

        def project(target: Declaration, prefix: str, rest: str) -> str:
            named.setdefault(target.name, target)
            href = links[formalizing[target.name]] if target.name in formalizing else linker.url(target.name)
            name, rest = html.escape(target.name, quote=True), _typed(rest)
            if href is None:
                return f'<span class="bp-lean-ref" tabindex="0" data-lean-name="{name}">{_typed(prefix)}</span>{rest}'
            href = html.escape(href, quote=True)
            return f'<a class="bp-lean-ref" href="{href}" data-lean-name="{name}">{_typed(prefix)}</a>{rest}'

        def looked_up(written: str) -> str | None:
            resolved = None if written.split(".", 1)[0] in bound else linker.resolve(written, declaration)
            # A one-letter name is almost always a variable the file declares, which the index cannot see. The card,
            # the link and the formalizing statement go by name, so a declaration the index files under another of
            # its name's declarations, or under none, links nothing rather than the wrong one.
            if resolved is None or len(resolved[1]) < 2 or linker.location(resolved[0].name) != resolved[0]:
                return None
            target, prefix = resolved
            return project(target, prefix, written[len(prefix) :])

        def reference(written: str, at: int) -> str | None:
            if exact is None:
                return looked_up(written)
            return _exact_reference(written, exact.get(at), declaration, linker, project, looked_up)

        code = f'<pre class="bp-lean"><code>{_lean_code(text, reference)}</code></pre>'
        count = text.count("\n") + 1
        if count > _LEAN_BLOCK_LINES:
            summary = f"<code>{_typed(declaration.name, one_line=True)}</code> · {count} lines"
            code = f'<details class="bp-lean-fold"><summary>{summary}</summary>{code}</details>'
        blocks.append(code)
    if not blocks:
        return []
    cards = [card for target in named.values() if (card := _lean_card(target, linker))]
    hidden = ['<div class="bp-lean-names" hidden>', *cards, "</div>"] if cards else []
    return ["", '<div class="bp-lean-source" data-search-exclude="1">', *blocks, *hidden, "</div>", ""]


def _exact_reference(
    written: str,
    found: tuple[int, str, str] | None,
    within: Declaration,
    linker: SourceLinker,
    project: Callable[[Declaration, str, str], str],
    looked_up: Callable[[str], str | None],
) -> str | None:
    """*written*, a name in *within*'s Lean, linked to the constant an ``.ilean`` usage starting at it names
    (:meth:`SourceLinker.names`), or ``None``. The usage must end at a component's end, so dot notation past a name
    (``Nat.succ_le.mp``) leaves the rest as text.

    A constant the project declares in the file the usage names links as :func:`_lean_blocks` links a project
    name, unless it is *within* itself: its own name or a recursive call. A private one is named by its
    declaration. Another constant of a project file, one the index does not hold, such as a constructor, or files
    elsewhere, as a public declaration may share a private one's name, is *looked_up* as without the ``.ilean``
    file. A public constant of a package Mathlib's documentation covers links to its page there, by its full
    name, which its title shows.
    """
    if found is None:
        return None
    length, constant, module = found
    prefix, rest = written[:length], written[length:]
    if rest[:1] not in ("", "."):
        return None
    declared = declared_name(constant)
    target = linker.declared_in(declared, module)
    if target is not None:
        return None if target == within else project(target, prefix, rest)
    if linker.in_project(module):
        return looked_up(written)
    if declared != constant or module.split(".", 1)[0] not in _DOCUMENTED:
        return None
    href = _MATHLIB_DOCS.format(quote(constant, safe="."))
    title = html.escape(constant, quote=True)
    link = f'<a class="bp-lean-doc" href="{html.escape(href, quote=True)}" title="{title}">{_typed(prefix)}</a>'
    return link + _typed(rest)


def _lean_card(declaration: Declaration, linker: SourceLinker) -> str | None:
    """What the book shows on hovering *declaration*'s name: its statement, or its first lines, and its file."""
    excerpt = linker.excerpt(declaration.name)
    if excerpt is None:
        return None
    text = excerpt[1]
    if declaration.keyword in ("theorem", "lemma"):
        text = _header(text, 0)
    lines = text.split("\n")
    if len(lines) > _LEAN_CARD_LINES:
        text = "\n".join([*lines[:_LEAN_CARD_LINES], "  ⋯"])
    where = _typed(f"{declaration.path.as_posix()}:{declaration.line}", one_line=True)
    name = html.escape(declaration.name, quote=True)
    return (
        # Material's search ends an excluded element at the first closing tag of its kind, so each card excludes itself.
        f'<div data-lean-name="{name}" data-search-exclude="1">'
        f'<pre class="bp-lean"><code>{_lean_code(text)}</code></pre><p class="bp-lean-where">{where}</p></div>'
    )


def _lean_code(text: str, reference: Callable[[str, int], str | None] = lambda _written, _at: None) -> str:
    """``text``, Lean, as HTML the page's Markdown leaves as typed, its comments, strings and keywords marked,
    and each name *reference* links linked; *reference* gets the name as written and its offset in ``text``.

    None of these is a reference: tactics, after ``by`` or ``decreasing_by``, to the end; a name in an attribute
    list; a name before ``:=`` in a field's or a named argument's place (after ``(``, ``{``, ``,`` or ``with``,
    or at a line's start unless the line before ends in a result type's colon); a name starting a line before
    ``:``, a field being declared.
    """
    parts: list[str] = []
    end = 0
    tactics = False
    for token in _LEAN_TOKEN.finditer(text):
        tactics = tactics or token.lastgroup == "keyword" and token[0] in ("by", "decreasing_by")
        if token.lastgroup == "name":
            start = text.rfind("\n", 0, token.start()) + 1
            before = text[start : token.start()]
            opens_line = not before.strip()
            slot = opens_line and not text[:start].rstrip().endswith(":") or _SLOT.search(before)
            given = slot and _GIVEN.match(text, token.end()) or opens_line and _DECLARED.match(text, token.end())
            marked = None if tactics or given or _ATTRIBUTE.search(before) else reference(token[0], token.start())
            if marked is None:
                continue
        else:
            marked = f'<span class="bp-lean-{token.lastgroup}">{_typed(token[0])}</span>'
        parts += [_typed(text[end : token.start()]), marked]
        end = token.end()
    return "".join(parts) + _typed(text[end:])


def _search_names(node: Node) -> list[str]:
    """The card's exact Lean names, as hidden text the site's search indexes.

    A verified name shows only as its code icon's label, and search reads text,
    not attributes. A catalog's names stay out: it can list hundreds, and its
    Lean row already shows the first few.
    """
    names = declaration_names(node.lean or "") if node.catalog is None else ()
    if not names:
        return []
    return ['<div class="bp-search-names" hidden>' + " ".join(html.escape(name) for name in names) + "</div>"]


def _code_icon() -> str:
    """A small dependency-free code icon suitable at theorem-heading size."""
    return (
        '<svg class="bp-code-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
        '<path d="m8 9-3 3 3 3M16 9l3 3-3 3M14 5l-4 14"/>'
        "</svg>"
    )
