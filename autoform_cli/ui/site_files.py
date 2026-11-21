"""The files render generates and where they go: the generated names, the suffixes the site publishes,
the assets and the builders that fill them, each chapter's page and each statement's anchor, a page's URL as
MkDocs serves it, and the captured files in the way of the generated ones."""

from __future__ import annotations

import unicodedata
from collections.abc import Collection, Iterable
from pathlib import Path, PurePosixPath

from .. import status
from .._fs import relative as _relative
from ..graph import Graph, _containers
from ..lean import PUBLICATION_MANIFEST
from ..markdown import GENERATED_VIEWS, PAGE_SUFFIXES, RETIRED_VIEWS
from . import dag_viewer, graph_pages, project_map
from ._assets import _GENERATED_BANNER, _filled_asset, _static_asset
from .mathjax import MATHJAX_SCRIPT


#: The coverage contract's page, compared without regard to case, as a disk
#: that ignores case serves it.
COVERAGE_PAGE = "coverage/readme.md"
#: Derived views this command rewrites; stale copies must not leak into the site.
_GENERATED_FILES = GENERATED_VIEWS | {graph_pages.PROJECT_PAYLOAD, PUBLICATION_MANIFEST}
#: The images a page may show.
_IMAGE_SUFFIXES = frozenset({".avif", ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".webp"})
#: The files a vault may publish as they are, besides its Markdown pages: a
#: browser shows these, or offers to save them, and runs nothing in them.
#: Anything else may be served as the site's own markup, so it is left out.
_STATIC_SUFFIXES = _IMAGE_SUFFIXES | {".pdf", ".bib", ".csv", ".json", ".lean", ".tex", ".txt"}
#: What the site publishes from sources/ once render verified the commit, which
#: holds the folder's other files: the notes and the images they show.
_PUBLISHED_SOURCE_SUFFIXES = frozenset({*PAGE_SUFFIXES, *_IMAGE_SUFFIXES})


#: Root entries every site holds; an authored root entry differing only in case would collide.
_GENERATED_ROOT_ENTRIES = frozenset(
    {"SUMMARY.md", graph_pages.PROJECT_PAYLOAD, "dependencies.md", "structure.md", PUBLICATION_MANIFEST}
    | {"assets", "dependencies", "javascripts", "stylesheets"}
)


STYLESHEET = "stylesheets/blueprint.css"
MERMAID_SCRIPT = "javascripts/blueprint-mermaid.js"
#: The Mermaid build the script loads for a page with an authored diagram, and
#: its subresource-integrity hash, so a CDN that serves other bytes leaves the
#: diagrams as written. A new release needs a new one, from the file in npm's
#: tarball: ``openssl dgst -sha384 -binary dist/mermaid.min.js | openssl base64 -A``.
MERMAID_LIBRARY = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
MERMAID_INTEGRITY = "sha384-rbtjAdnIQE/aQJGEgXrVUlMibdfTSa4PQju4HDhN3sR2PmaKFzhEafuePsl9H/9I"
DAG_SCRIPT = dag_viewer.SCRIPT_PATH
DAG_STYLE = dag_viewer.STYLE_PATH
MAP_SCRIPT = "javascripts/project-map.js"
MAP_STYLE = "stylesheets/project-map.css"
MAP_PAGE = "dependencies/map/index.html"
_TO_ROOT = "../" * MAP_PAGE.count("/")
LOGO = "assets/autoform.svg"
#: The files render writes into every site, whatever the vault holds there.
_ASSETS = (STYLESHEET, MERMAID_SCRIPT, MATHJAX_SCRIPT, DAG_SCRIPT, DAG_STYLE, LOGO)
#: Paths render writes at fixed places; an authored path differing only in case would collide.
_GENERATED_PATHS = _GENERATED_ROOT_ENTRIES | set(_ASSETS)


def _map_assets(title: str) -> tuple[tuple[str, str], ...]:
    """The whole-project map's script, style and page, for a roadmap called ``title``."""
    return (
        # The reducer, the layout module and the local graph's rules ship ahead of the controller in one file, so the page
        # makes one request.
        (MAP_SCRIPT, "\n".join(_static_asset(name) for name in ("tex-plain.js", "project-map-layout.js", "project-map-local.js",
                                                                   "project-map.js"))),
        (MAP_STYLE, _static_asset("project-map.css")),
        # One standalone page over the per-level files beside it.
        (MAP_PAGE, project_map.render_page(
            title=title, map_base=_TO_ROOT + project_map.MAP_DIRECTORY + "/",
            routes=_TO_ROOT + graph_pages.SEARCH_INDEX, home=_TO_ROOT, script=_TO_ROOT + MAP_SCRIPT,
            style=_TO_ROOT + MAP_STYLE, mathjax=_TO_ROOT + MATHJAX_SCRIPT,
        )),
    )


def _logo() -> str:
    """The Autoform mark: the smallest blueprint there is, drawn as an A.

    Two prerequisites at the base and one result above them is the smallest
    non-trivial dependency graph, and its edges happen to be the strokes of an
    A. So the letter and the thing it stands for are the same drawing.

    It is set white on a gradient tile rather than as line art because the same
    file serves as the 16px favicon, where a thin stroke disappears; a filled
    tile also needs no light and dark variants, since it carries its own
    background onto either header.
    """

    return _static_asset("autoform.svg")


def _mermaid_script() -> str:
    """Draw the diagrams authors write, locked down, on the pages that have one.

    The site's dependency maps are the explorer, so every diagram this script
    sees is a fence an author wrote. Mermaid draws it in strict mode, which
    drops click callbacks and sanitizes link URLs, with labels as plain text
    rather than HTML; a link to a URL keeps working. It uses Mermaid's own
    theme for each colour scheme and redraws when the scheme changes. A page
    without a diagram never loads Mermaid: the script fetches the library only
    for a page with a fence, unless the site's own configuration loads it.
    What the script fetches runs only as the pinned build's bytes, and a copy
    the browser refuses leaves the diagrams as written; a copy the site's
    configuration loads has only the check its tag carries. Diagrams are drawn
    one at a time, since the theme is Mermaid's global setting, and one that
    fails leaves the others drawn.
    """
    return _GENERATED_BANNER + _filled_asset(
        "blueprint-mermaid.js", MERMAID_LIBRARY=MERMAID_LIBRARY, MERMAID_INTEGRITY=MERMAID_INTEGRITY
    )


def _site_paths(graph: Graph) -> tuple[str, ...]:
    """Every file render writes into the site for ``graph`` other than the
    blueprint's own, relative to the site's root: the derived pages, the
    publication manifest, and the site's assets."""

    return tuple(
        sorted(
            {
                *(_GENERATED_FILES - RETIRED_VIEWS),
                STRUCTURE_PAGE,
                "SUMMARY.md",
                *(_group_page(group).as_posix() for group in _group_nodes(graph)),
                *graph_pages.graph_page_paths(graph, wiki_links=True),
                *project_map.map_paths(),
                MAP_SCRIPT, MAP_STYLE, MAP_PAGE,
                *_ASSETS,
            }
        )
    )


def _written_pages(written_over: Collection[str], groups: Iterable[str]) -> dict[str, str]:
    """The pages among ``written_over``, each by its :func:`_page_url`: MkDocs
    publishes one page at a URL, and a disk that ignores case writes render's
    page over a vault's page spelled in another case. The page of a chapter in
    ``groups``, each with a narrative, is built from that narrative, so it is
    not one of them."""

    chapters = {_group_page(group).as_posix() for group in groups}
    return {
        url: path
        for path in written_over
        if path not in chapters and (url := _page_url(path, (*PAGE_SUFFIXES, ".html"))) is not None
    }


def _page_url(path: str, suffixes: Collection[str] = PAGE_SUFFIXES) -> str | None:
    """Where MkDocs publishes the file at ``path``, without its suffix and
    :func:`_folded`, when the suffix is one of ``suffixes``: a page spelled
    exactly ``README`` is its folder's ``index``. ``None`` for any other file."""

    page = PurePosixPath(path)
    if page.suffix.lower() not in suffixes:
        return None
    stem = "index" if page.stem == "README" and page.suffix.lower() in PAGE_SUFFIXES else page.stem
    return _folded((page.parent / stem).as_posix())


def _in_the_way(blueprint: Path, graph: Graph, snapshot: dict[Path, bytes]) -> list[str]:
    """A captured file below one of :func:`_site_paths`, or at one of its
    folders. Render writes these over the captured files it copied, so it
    names the path before writing anything; capture has already refused a
    symlink or a special file anywhere."""

    captured = {_relative(source, blueprint).as_posix() for source in snapshot}
    folders = {"/".join(parts[:end]) for parts in (path.split("/") for path in captured) for end in range(1, len(parts))}
    blocked: dict[str, str] = {}
    for relative in _site_paths(graph):
        found = (relative, "a directory") if relative in folders else None
        if found is None:
            parts = relative.split("/")
            found = next(
                (("/".join(parts[:end]), "a file") for end in range(1, len(parts)) if "/".join(parts[:end]) in captured),
                None,
            )
        if found is None or found[0] in blocked:
            continue
        where, kind = found
        purpose = "writes a file" if where == relative else f"needs a folder for {relative}"
        blocked[where] = f"{where}: is {kind}, where autoform render {purpose}; remove it"
    return list(blocked.values())


def _group_nodes(graph: Graph) -> dict[str, list[str]]:
    """Group formalizable leaves and module catalogs under their nearest containing article.

    Graph views deliberately roll a subtree up to its top-level scope. Book
    publication is different: a statement belongs where the author placed its
    nearest narrative container, so nested sections remain real book sections.
    """
    grouped: dict[str, list[str]] = {}
    containers = _containers(graph)
    for node_id in status.topological_order(graph):
        node = graph.nodes[node_id]
        if not graph_pages.is_card(node, containers):
            continue
        group = node.parent or "roadmap"
        grouped.setdefault(group, []).append(node_id)
    return grouped


def _group_page(group: str) -> Path:
    """Where a milestone's consolidated chapter lives in the output tree."""
    return (
        Path("roadmap/README.md")
        if group in {"", "roadmap"}
        else Path("roadmap") / group / "README.md"
    )


STRUCTURE_PAGE = "structure.md"


def _anchor(node_id: str, group: str) -> str:
    """A stable in-page anchor for a node, unique within its chapter."""
    prefix = f"{group}/" if group else ""
    remainder = node_id[len(prefix) :] if prefix and node_id.startswith(prefix) else node_id
    return remainder.replace("/", "-")


def _folded(name: str) -> str:
    """``name`` as capture compares names for a collision."""

    return unicodedata.normalize("NFC", name).casefold()


def _stylesheet() -> str:
    """Blueprint styling: amsthm structure over Facebook's product surfaces.

    Both schemes are Facebook's shipped greys with Meta blue, set in Plus
    Jakarta Sans with JetBrains Mono for code. Dark is not light dimmed: it is
    the palette Facebook uses in dark mode, and it hangs off Material's colour
    scheme attribute so the theme's own toggle drives it.

    The status rules fill the markers that end ``assets/blueprint.css``.
    """
    # Dependency dots and swatches are a few pixels wide and carry no word, so
    # every state with a tinted light fill (the four finished states,
    # conditionally proved, not ready and inventory pending) fills them with
    # its stroke: the tints are too pale to tell from an outline there. The
    # light fill decides, so both schemes fill the same states.
    def dot(state: status.State, dark: bool = False) -> str:
        fill, stroke = (state.dark_fill, state.dark_stroke) if dark else (state.fill, state.stroke)
        return f"background: {fill if state.fill == '#FFFFFF' else stroke}; border-color: {stroke};"

    light = "\n".join(
        f".bp-{state.key} .bp-mark {{ color: {state.stroke}; }}\n"
        f".bp-{state.key} > .bp-thmcontent {{ border-left-color: {state.stroke}; }}\n"
        f".bp-ref-{state.key}::before, .bp-swatch-{state.key} {{ {dot(state)} }}"
        for state in status.STATES
    )
    dark = "\n".join(
        f"[data-md-color-scheme=slate] .bp-{state.key} .bp-mark {{ color: {state.dark_stroke}; }}\n"
        f"[data-md-color-scheme=slate] .bp-{state.key} > .bp-thmcontent "
        f"{{ border-left-color: {state.dark_stroke}; }}\n"
        f"[data-md-color-scheme=slate] .bp-ref-{state.key}::before, "
        f"[data-md-color-scheme=slate] .bp-swatch-{state.key} "
        f"{{ {dot(state, dark=True)} }}"
        for state in status.STATES
    )
    # The legends beside the explorer's rings list module inventories, so
    # their swatches take the colours the explorer draws inventories in.
    inventory = "\n".join(
        f".bp-swatch-{state.key} {{ {dot(state)} }}\n"
        f"[data-md-color-scheme=slate] .bp-swatch-{state.key} "
        f"{{ {dot(state, dark=True)} }}"
        for state in status.INVENTORY_STATES
    )
    return _GENERATED_BANNER + _filled_asset(
        "blueprint.css", LIGHT_STATE_RULES=light, DARK_STATE_RULES=dark, INVENTORY_STATE_RULES=inventory
    )
