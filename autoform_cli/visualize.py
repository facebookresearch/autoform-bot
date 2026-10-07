"""Export a blueprint dependency graph as a Mermaid Markdown page."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from typing import Sequence

from . import mermaid, status
from .graph import GraphValidationError, load_graph


GENERATED_STRUCTURE_MARKER = "---\nkind: structure\nautoform_generated: true\n---"


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


def export_graph(
    blueprint_dir: Path,
    output: Path | None = None,
    *,
    link_extension: str = ".md",
    title: str = "Dependency graph",
) -> Path:
    """Load and export ``blueprint_dir``; return the written Markdown path."""
    blueprint_dir = Path(blueprint_dir).resolve()
    destination = _destination(output or blueprint_dir / "dependencies.md")
    graph = load_graph(blueprint_dir)
    statuses = status.derive(graph)
    page = mermaid.render_page(
        graph,
        statuses,
        destination,
        link_extension=link_extension,
        title=title,
    )
    _atomic_write(destination, page)
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
    blueprint_dir = Path(blueprint_dir).resolve()
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
        kind = node.declaration or node.kind
        lines.append(
            f"{indent}- [{node.title}]({entry.as_posix()}) · {kind} · {statuses[node.id].label}"
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
    parser.add_argument(
        "--link-extension",
        choices=(".md", ".html"),
        default=".md",
        help="node-link extension: .md for the vault or .html for a built site",
    )
    parser.add_argument("--title", default="Dependency graph", help="page heading")
    parser.add_argument(
        "--structure",
        action="store_true",
        help="also generate <blueprint-dir>/structure.md when absent or previously generated",
    )
    args = parser.parse_args(argv)
    try:
        structure = None
        graph_output = _destination(
            args.output or Path(args.blueprint_dir).resolve() / "dependencies.md"
        )
        if args.structure:
            structure = _destination(Path(args.blueprint_dir).resolve() / "structure.md")
            if graph_output == structure:
                raise VisualizationError(
                    f"graph and structure outputs must be different paths: {structure}"
                )
            _preflight_structure(structure)
        output = export_graph(
            args.blueprint_dir,
            args.output,
            link_extension=args.link_extension,
            title=args.title,
        )
        if structure is not None:
            structure = export_structure(args.blueprint_dir, structure)
    except (GraphValidationError, VisualizationError) as error:
        parser.exit(2, f"error: {error}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
