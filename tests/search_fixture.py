"""Write blueprint articles, and the small convexity blueprint, that the search tests query."""

from __future__ import annotations

from pathlib import Path


def _article(
    project: Path,
    relative: str,
    *,
    title: str,
    body: str = "A precise statement.",
    metadata: tuple[str, ...] = (),
    depends: tuple[str, ...] = (),
) -> Path:
    path = project / "blueprint/roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", *metadata, "---", "", f"# {title}", "", body]
    if depends:
        lines.extend(["", "## Depends on", "", *(f"- [dependency]({target})" for target in depends)])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    _article(project, "README.md", title="Book")
    _article(project, "convexity/README.md", title="Convex analysis")
    _article(
        project,
        "convexity/hyperplane.md",
        title="Separating hyperplane",
        body="Two disjoint convex sets are separated by an affine functional.",
        metadata=("declaration: theorem", "statement: formalized", "lean: Project.separation"),
    )
    _article(
        project,
        "convexity/support.md",
        title="Supporting functional",
        body="A closed convex set is the intersection of its supporting half-spaces.",
        metadata=("declaration: lemma",),
        depends=("hyperplane.md",),
    )
    return project
