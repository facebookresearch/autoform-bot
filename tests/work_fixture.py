"""Write the small blueprint chapter, base to blocked, that the work and debrief tests read."""

from __future__ import annotations

from pathlib import Path

from tests.facts_fixture import edit_annotated, write_annotated


def _article(
    project: Path,
    name: str,
    *,
    title: str,
    metadata: list[str],
    depends: str = "",
    proof_depends: str = "",
) -> None:
    path = project / "blueprint/roadmap/chapter" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ["---", *metadata, "---", "", f"# {title}", "", "A precise statement."]
    if depends:
        text.extend(["", "## Depends on", "", f"- [dependency]({depends})"])
    if proof_depends:
        text.extend(["", "## Proof depends on", "", f"- [dependency]({proof_depends})"])
    write_annotated(path, "\n".join(text) + "\n")


def _edit(project: Path, name: str, old: str, new: str) -> Path:
    path = project / "blueprint/roadmap/chapter" / name
    edit_annotated(path, old, new)
    return path


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    _article(project, "README.md", title="Chapter", metadata=["article_id: af_0000000000000000000000c0"])
    _article(
        project,
        "base.md",
        title="Base",
        metadata=[
            "article_id: af_000000000000000000000001",
            "declaration: definition",
            "statement: formalized",
            "lean: Project.base",
        ],
    )
    _article(
        project,
        "state.md",
        title="State me",
        metadata=[
            "article_id: af_000000000000000000000002",
            "declaration: theorem",
        ],
        depends="base.md",
    )
    _article(
        project,
        "prove.md",
        title="Prove me",
        metadata=[
            "article_id: af_000000000000000000000003",
            "declaration: theorem",
            "statement: formalized",
            "lean: Project.prove",
        ],
        depends="base.md",
    )
    _article(
        project,
        "blocked.md",
        title="Blocked",
        metadata=[
            "article_id: af_000000000000000000000004",
            "declaration: theorem",
        ],
        depends="state.md",
    )
    return project
