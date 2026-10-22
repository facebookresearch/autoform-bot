"""Write the vault's dependency summary and structure pages as plain Markdown."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from . import graph_views, status
from .dag_viewer import MAX_MAP_NODES
from .graph import GraphValidationError, load_graph
from .markdown import blueprint_root, relative_link
from .readback import READBACKS_DIR

if TYPE_CHECKING:
    from .graph import Graph
    from .graph_views import ViewEdge


GENERATED_STRUCTURE_MARKER = "---\nkind: structure\nautoform_generated: true\n---"
GENERATED_GRAPH_MARKER = "---\nkind: graph\nautoform_generated: true\n---"
#: The sentence every page the Mermaid exporter wrote opened its diagram with.
_MERMAID_PAGE_SENTENCE = (
    "Arrows point from a prerequisite to what depends on it; a dashed\n"
    "arrow marks a prerequisite that only the proof needs. Select a node to open it.\n"
)
#: States a contributor could pick up today.
_READY = frozenset({"can_prove", "can_state"})


class VisualizationError(ValueError):
    """Raised when visualization output would overwrite authored content."""


def _destination(path: Path) -> Path:
    """Canonicalize the parent without following the final destination symlink."""
    path = path.absolute()
    return path.parent.resolve() / path.name


def _replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _atomic_write(destination: Path, contents: str) -> None:
    """Atomically replace *destination* from a temporary file beside it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        _replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _preflight_structure(destination: Path) -> None:
    if not destination.exists() and not destination.is_symlink():
        return
    try:
        existing = destination.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise VisualizationError(
            f"refusing to overwrite existing structure page without the generated marker: {destination}"
        ) from error
    if not existing.startswith(f"{GENERATED_STRUCTURE_MARKER}\n"):
        raise VisualizationError(
            f"refusing to overwrite authored structure page without the generated marker: {destination}"
        )


def _preflight_graph(destination: Path) -> None:
    """Refuse to replace a dependency page someone wrote.

    The summary replaces a missing page, one it wrote itself, and the Mermaid
    page earlier versions of this command wrote, which carried no marker.
    """
    if not destination.exists() and not destination.is_symlink():
        return
    try:
        existing = destination.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise VisualizationError(
            f"refusing to overwrite existing dependency page without the generated marker: {destination}"
        ) from error
    # A hand-written kind: graph page would pass for one on its front matter alone.
    mermaid_page = (
        existing.startswith("---\nkind: graph\n---\n\n# ")
        and "\n```mermaid\ngraph LR\n" in existing
        and _MERMAID_PAGE_SENTENCE in existing
    )
    if not (existing.startswith(f"{GENERATED_GRAPH_MARKER}\n") or mermaid_page):
        raise VisualizationError(
            f"refusing to overwrite authored dependency page without the generated marker: {destination}"
        )


#: Math and code spans, whose brackets belong to them.
_SPANS = re.compile(r"(\$\$.+?\$\$|\$[^$\n]+\$|`[^`\n]*`)")


def _cell(text: str) -> str:
    """Keep *text* from closing a table cell; titles otherwise stay as written, math included."""
    return text.replace("|", "\\|")


def _label(text: str) -> str:
    """Escape a link label's brackets outside math and code, so "[a, b)" cannot end the link early."""
    return "".join(
        part if index % 2 else part.replace("[", "\\[").replace("]", "\\]")
        for index, part in enumerate(_SPANS.split(text))
    )


def summary_page(
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    destination: Path,
    *,
    title: str = "Dependency summary",
) -> str:
    """The vault's dependency summary, from the projection the explorer's project map draws.

    Plain Markdown, so it reads the same on GitHub, in Obsidian and to an
    agent: the roadmap's statuses, one row per chapter with its cross-chapter
    dependencies, the articles themselves while there are few enough to list,
    and how to open the explorer, which draws the whole graph.
    """
    view = graph_views.project_view(graph, statuses)
    labels = {state.key: state.label for state in (*status.STATES, *status.INVENTORY_STATES)}
    titles = {node.id: node.title for node in view.nodes}

    def link(text: str, target: Path) -> str:
        return f"[{_label(text)}]({relative_link(target, destination, '.md')})"

    def related(edges: list[ViewEdge], end: str) -> str:
        names = sorted(
            _cell(titles[getattr(edge, end)]) + ("" if edge.statement_count else " (proof only)")
            for edge in edges
        )
        return ", ".join(names) or "none"

    # A chapter's items are its targets and module inventories, so those are its articles.
    articles = {
        graph_views._scope_node_id(group): [
            leaf
            for leaf in leaves
            if graph_views.is_scope_target(graph.nodes[leaf]) or graph.nodes[leaf].catalog is not None
        ]
        for group, leaves in graph_views.group_nodes(graph).items()
    }
    members = sum(len(listed) for listed in articles.values())
    lines = [GENERATED_GRAPH_MARKER, "", f"# {title}", ""]
    if not view.nodes:
        lines.extend(["The roadmap has no articles yet.", ""])
    else:
        lines.extend(
            [
                f"{members} article{'s' if members != 1 else ''} in {len(view.nodes)} "
                f"chapter{'s' if len(view.nodes) != 1 else ''}, with "
                f"{len(view.edges)} dependenc{'ies' if len(view.edges) != 1 else 'y'} between chapters. "
                "A chapter depends on another when one of its articles rests on one of the "
                "other's; \"proof only\" marks a dependency only a proof needs.",
                "",
            ]
        )

    # The chapters' own counts, so the table totals their items.
    counts: Counter[str] = Counter()
    for node in view.nodes:
        counts.update(dict(node.status_counts))
    rows = [state for state in (*status.STATES, *status.INVENTORY_STATES) if counts[state.key]]
    if rows:
        lines.extend(["| Status | Count | Meaning |", "| --- | ---: | --- |"])
        lines.extend(f"| {state.label} | {counts[state.key]} | {status.MEANINGS[state.key]} |" for state in rows)
        lines.append("")

    if view.nodes:
        incoming: dict[str, list[ViewEdge]] = defaultdict(list)
        outgoing: dict[str, list[ViewEdge]] = defaultdict(list)
        for edge in view.edges:
            incoming[edge.target].append(edge)
            outgoing[edge.source].append(edge)
        lines.extend(
            [
                "## Chapters",
                "",
                "| Chapter | Items | Status | Ready | Depends on | Used by |",
                "| --- | ---: | --- | ---: | --- | --- |",
            ]
        )
        for node in view.nodes:
            group = node.id.removeprefix("scope:")
            name = _cell(link(node.title, graph.nodes[group].path) if group in graph.nodes else node.title)
            states = " · ".join(f"{count} {labels.get(key, key)}" for key, count in node.status_counts) or "none"
            ready = sum(count for key, count in node.status_counts if key in _READY)
            lines.append(
                f"| {name} | {node.item_count} | {states} | {ready} | "
                f"{related(incoming[node.id], 'source')} | {related(outgoing[node.id], 'target')} |"
            )
        lines.append("")

    # A roadmap with more articles than the explorer draws on one map lists its
    # chapters only; the explorer lists every article.
    if 0 < members <= MAX_MAP_NODES:
        lines.extend(["## Articles", ""])
        for node in view.nodes:
            if not articles.get(node.id):
                continue
            lines.extend([f"**{node.title}**", ""])
            for member in articles[node.id]:
                article = graph.nodes[member]
                state = graph_views.display_state(article, statuses[member]).label
                lines.append(f"- {link(article.title, article.path)} · {graph_views.kind_label(article)} · {state}")
            lines.append("")
    elif members:
        lines.extend(
            [
                f"The roadmap has more articles than the {MAX_MAP_NODES} this page lists; "
                "the explorer shows every one.",
                "",
            ]
        )

    lines.extend(
        [
            "## Explorer",
            "",
            "The published site draws the whole graph as an interactive explorer, on its home "
            "page and under Graph. To open it locally, render the site, build it, and serve it, "
            "where `<AUTOFORM_PLUGIN_ROOT>` is the Autoform plugin's root:",
            "",
            "```bash",
            'uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform render blueprint \\',
            "  --output site-src --lean-root .",
            "uv run --with mkdocs --with mkdocs-material --with mkdocs-literate-nav \\",
            "  --with pymdown-extensions mkdocs build --strict",
            'uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform dashboard . --site-dir site',
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def export_graph(
    blueprint_dir: Path,
    output: Path | None = None,
    *,
    title: str = "Dependency summary",
) -> Path:
    """Load ``blueprint_dir`` and write its dependency summary; return the written path."""
    blueprint_dir = blueprint_root(blueprint_dir)
    destination = _destination(output or blueprint_dir / "dependencies.md")
    _preflight_graph(destination)
    graph = load_graph(blueprint_dir)
    statuses = status.derive(graph)
    _atomic_write(destination, summary_page(graph, statuses, destination, title=title))
    return destination


def export_structure(blueprint_dir: Path, output: Path | None = None) -> Path:
    """Write the vault's own structure page, for reading inside Obsidian.

    Obsidian's file explorer already shows the tree, so the part worth writing
    down is the part it cannot know: the derived state of each article, which
    comes from the dependency graph rather than from anything in the file. The
    flat-vault warning travels with it, because a vault with every article
    directly under ``roadmap/`` publishes a book with no chapters and looks
    perfectly ordinary in the explorer.

    Plain Markdown, no HTML: the site's stylesheet does not exist here.
    """
    blueprint_dir = blueprint_root(blueprint_dir)
    destination = _destination(output or blueprint_dir / "structure.md")
    _preflight_structure(destination)
    graph = load_graph(blueprint_dir)
    statuses = status.derive(graph)
    by_path = {node.path.resolve(): node for node in graph.nodes.values()}

    files = [
        path
        for path in sorted(blueprint_dir.rglob("*.md"))
        if not any(part.startswith(".") for part in path.relative_to(blueprint_dir).parts)
        and path.name not in {"dependencies.md", destination.name}
        # A read-back card is not a page, and may be for an article the site leaves out.
        and path.relative_to(blueprint_dir).parts[:1] != (READBACKS_DIR,)
    ]
    directories: set[Path] = set()
    for path in files:
        for parent in path.relative_to(blueprint_dir).parents:
            if parent != Path("."):
                directories.add(parent)

    lines: list[str] = []
    for entry in sorted(directories | {p.relative_to(blueprint_dir) for p in files}):
        indent = "    " * (len(entry.parts) - 1)
        if entry in directories:
            lines.append(f"{indent}- **{entry.name}/**")
            continue
        node = by_path.get((blueprint_dir / entry).resolve())
        if node is None:
            lines.append(f"{indent}- [{entry.name}]({entry.as_posix()}) · prose")
            continue
        state = graph_views.display_state(node, statuses[node.id])
        kind = graph_views.kind_label(node)
        lines.append(
            f"{indent}- [{node.title}]({entry.as_posix()}) · {kind} · {state.label}"
        )

    depths = {len(p.relative_to(blueprint_dir).parts) - 1 for p in by_path}
    warning = (
        "> [!warning] Every article sits directly under `roadmap/`.\n"
        "> Chapters come from directories, so this vault publishes as one\n"
        "> undivided list. Group the articles into subdirectories.\n\n"
        if len(by_path) > 3 and depths <= {1}
        else ""
    )
    page = (
        f"{GENERATED_STRUCTURE_MARKER}\n\n"
        "# Vault structure\n\n"
        "Every Markdown file in this vault, with the state the dependency graph\n"
        "derives for it. Chapters come from directories, so the shape of this\n"
        "tree is the shape of the published book.\n\n"
        f"{warning}"
        + "\n".join(lines)
        + "\n"
    )
    _atomic_write(destination, page)
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("blueprint_dir", type=Path, help="directory containing roadmap Markdown nodes")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output Markdown (default: <blueprint-dir>/dependencies.md)",
    )
    parser.add_argument("--title", default="Dependency summary", help="page heading")
    parser.add_argument(
        "--structure",
        action="store_true",
        help="also generate <blueprint-dir>/structure.md when absent or previously generated",
    )
    args = parser.parse_args(argv)
    try:
        structure = None
        graph_output = _destination(
            args.output or blueprint_root(args.blueprint_dir) / "dependencies.md"
        )
        if args.structure:
            structure = _destination(blueprint_root(args.blueprint_dir) / "structure.md")
            if graph_output == structure:
                raise VisualizationError(
                    f"graph and structure outputs must be different paths: {structure}"
                )
            _preflight_structure(structure)
        output = export_graph(args.blueprint_dir, args.output, title=args.title)
        if structure is not None:
            structure = export_structure(args.blueprint_dir, structure)
    except (GraphValidationError, VisualizationError) as error:
        parser.exit(2, f"error: {error}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
