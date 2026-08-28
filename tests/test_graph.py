from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from autoform_cli.graph import (
    GraphValidationError,
    load_graph,
)


def _node_text(body: str, **metadata: str) -> str:
    properties = [*(f"{key}: {value}" for key, value in metadata.items())]
    return "\n".join(["---", *properties, "---", body])


def _roadmap_page(blueprint: Path, relative: str, body: str) -> Path:
    path = blueprint / "roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _ensure_chapter(blueprint: Path, relative: str) -> None:
    """Give a chapter directory its chapter page, as every real vault has.

    Containment comes from nested `README.md` articles, so a directory without
    one is refused at load. Fixtures that only happen to nest are not trying to
    model that fault; the tests that are do it explicitly.
    """
    parts = Path(relative).parts
    if len(parts) < 2:
        return
    chapter = blueprint / "roadmap" / parts[0]
    page = chapter / "README.md"
    if not page.exists():
        chapter.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "---\n---\n\n# " + parts[0].replace("-", " ").title() + "\n", encoding="utf-8"
        )


def _node(blueprint: Path, relative: str, body: str, **metadata: str) -> Path:
    _ensure_chapter(blueprint, relative)
    return _roadmap_page(blueprint, relative, _node_text(body, **metadata))


def test_loads_nested_wiki_and_metadata(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(
        blueprint,
        "foundations/base.md",
        "# Base theorem\n",
        declaration="theorem",
        statement="formalized",
        proof="formalized",
        lean="Autoform.Base",
    )
    _node(
        blueprint,
        "result.md",
        """# Main result

[This ordinary link is ignored](foundations/missing.md)

## Depends on

- [Base](foundations/base.md#proof)
- [Base again](foundations/base.md)

## Notes

[Also ignored](missing.md)
""",
    )

    graph = load_graph(blueprint)

    # The chapter page is an article too, so it is a node in its own right.
    assert list(graph.nodes) == ["foundations", "foundations/base", "result"]
    assert graph.nodes["foundations/base"].title == "Base theorem"
    assert graph.nodes["foundations/base"].kind == "article"
    assert graph.nodes["foundations/base"].declaration == "theorem"
    assert graph.nodes["foundations/base"].statement_formalized
    assert graph.nodes["foundations/base"].proof_formalized
    assert graph.nodes["foundations/base"].lean == "Autoform.Base"
    assert graph.nodes["result"].dependencies == ("foundations/base",)
    assert graph.nodes["result"].statement_dependencies == ("foundations/base",)
    assert graph.edge_count == 1


def test_resolves_links_relative_to_each_node(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n")
    _node(
        blueprint,
        "chapter/result.md",
        "# Result\n\n## Depends on\n\n[Base](../base.md)\n",
    )

    graph = load_graph(blueprint)

    assert graph.nodes["chapter/result"].dependencies == ("base",)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_node_text("No title\n"), "missing H1 title"),
        (_node_text("# First title\n# Second title\n"), "multiple H1 titles"),
        ("---\ndeclaration: theorem\n# Title\n", "unterminated frontmatter"),
        (_node_text("# Title\n", owner="me"), "unsupported frontmatter key"),
        (_node_text("# Result\n## Depends on\n[x](missing.md)\n"), "does not exist"),
        (_node_text("# Result\n## Depends on\n[x](note.txt)\n"), "relative .md file"),
        (
            _node_text("# Result\n## Depends on\n[x](https://example.com/x.md)\n"),
            "relative Markdown path",
        ),
        (
            _node_text("# Result\n## Depends on\n[x](../../outside.md)\n"),
            "escapes the blueprint directory",
        ),
    ],
)
def test_rejects_invalid_nodes(tmp_path: Path, body: str, message: str) -> None:
    blueprint = tmp_path / "blueprint"
    _roadmap_page(blueprint, "result.md", body)
    (tmp_path / "outside.md").write_text("# Outside\n", encoding="utf-8")

    with pytest.raises(GraphValidationError, match=message):
        load_graph(blueprint)


def test_rejects_self_edge(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "self.md", "# Self\n## Depends on\n[Self](self.md)\n")

    with pytest.raises(GraphValidationError, match="dependency on itself"):
        load_graph(blueprint)


def test_every_roadmap_markdown_file_is_an_article(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _roadmap_page(blueprint, "notes.md", "# Notes\n")
    _node(blueprint, "result.md", "# Result\n## Depends on\n[Notes](notes.md)\n")

    graph = load_graph(blueprint)
    assert graph.nodes["notes"].title == "Notes"
    assert graph.nodes["result"].dependencies == ("notes",)


def test_rejects_cycle(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "a.md", "# A\n## Depends on\n[B](b.md)\n")
    _node(blueprint, "b.md", "# B\n## Depends on\n[A](a.md)\n")

    with pytest.raises(GraphValidationError, match=r"dependency cycle: a -> b -> a"):
        load_graph(blueprint)


def test_rejects_cycle_created_only_by_chapter_contraction(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _roadmap_page(blueprint, "a/README.md", "# A\n")
    _roadmap_page(blueprint, "b/README.md", "# B\n")
    _node(blueprint, "a/first.md", "# A first\n")
    _node(
        blueprint,
        "b/middle.md",
        "# B middle\n## Depends on\n[A first](../a/first.md)\n",
    )
    _node(
        blueprint,
        "a/last.md",
        "# A last\n## Depends on\n[B middle](../b/middle.md)\n",
    )

    with pytest.raises(GraphValidationError, match=r"rolled-up dependency cycle in root: a -> b -> a"):
        load_graph(blueprint)


def test_ignores_links_in_code_fences_and_other_sections(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(
        blueprint,
        "only.md",
        """# Only

## Notes
[Missing](missing.md)

## Depends on
`[Inline example](missing.md)`
<!-- [Commented out](missing.md) -->
<!--
[Multiline comment](missing.md)
-->
```markdown
[Also missing](missing.md)
```
""",
    )

    assert load_graph(blueprint).nodes["only"].dependencies == ()


def test_requires_blueprint_and_roadmap_directories(tmp_path: Path) -> None:
    with pytest.raises(GraphValidationError, match="blueprint directory does not exist"):
        load_graph(tmp_path / "absent")

    blueprint = tmp_path / "blueprint"
    blueprint.mkdir()
    with pytest.raises(GraphValidationError, match="roadmap directory does not exist"):
        load_graph(blueprint)


def test_loads_container_articles_and_infers_single_parent_hierarchy(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _roadmap_page(
        blueprint,
        "README.md",
        "---\n---\n\n# Roadmap\n",
    )
    _roadmap_page(blueprint, "chapter/README.md", "# Chapter\n")
    _node(blueprint, "chapter/result.md", "# Result\n")

    graph = load_graph(blueprint)
    assert graph.nodes["roadmap"].parent is None
    assert graph.nodes["chapter"].parent == "roadmap"
    assert graph.nodes["chapter/result"].parent == "chapter"
    assert graph.nodes["chapter/result"].depth == 2


@pytest.mark.parametrize("relative", ["readme.md", "ReadMe.md", "README.MD", "chapter/readme.md"])
def test_rejects_noncanonical_readme_case_with_actionable_error(tmp_path: Path, relative: str) -> None:
    blueprint = tmp_path / "blueprint"
    _roadmap_page(blueprint, relative, "# Wrongly cased container\n")
    if relative.startswith("chapter/"):
        _roadmap_page(blueprint, "chapter/result.md", "# Result\n")

    with pytest.raises(GraphValidationError) as caught:
        load_graph(blueprint)

    message = str(caught.value)
    assert relative in message
    assert "must be named exactly README.md" in message


def test_splits_statement_and_proof_dependencies(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "objects.md", "# Objects\n")
    _node(blueprint, "lemma.md", "# Lemma\n")
    _node(
        blueprint,
        "result.md",
        """# Result

## Depends on

- [Objects](objects.md)

## Proof depends on

- [Lemma](lemma.md)
""",
    )

    node = load_graph(blueprint).nodes["result"]

    assert node.statement_dependencies == ("objects",)
    assert node.proof_dependencies == ("lemma",)
    # Both kinds are still edges, so cycles and staleness are caught either way.
    assert node.dependencies == ("objects", "lemma")


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({"statement": "yes"}, "accepts only 'formalized'"),
        ({"proof": "sorry"}, "accepts only 'formalized'"),
        ({"mathlib": "maybe"}, "accepts only true or false"),
        ({"not_ready": "1"}, "accepts only true or false"),
        ({"origin": "unknown"}, "accepts cited, bridged, or background"),
    ],
)
def test_rejects_invalid_assertions(tmp_path: Path, metadata: dict[str, str], message: str) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "result.md", "# Result\n", **metadata)

    with pytest.raises(GraphValidationError, match=message):
        load_graph(blueprint)


def test_records_origin_and_source_links_without_treating_them_as_edges(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    source = blueprint / "sources" / "paper.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Paper\n", encoding="utf-8")
    _node(
        blueprint,
        "chapter/result.md",
        "# Result\n\n## Sources\n\n[Paper](../../sources/paper.md#result)\n",
        origin="cited",
    )

    node = load_graph(blueprint).nodes["chapter/result"]
    assert node.origin == "cited"
    assert node.sources == ("../../sources/paper.md#result",)
    assert node.dependencies == ()


def test_check_cli(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n")
    result = subprocess.run(
        [sys.executable, "-m", "autoform_cli", "check", str(blueprint)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[0] == "OK: 1 articles, 0 dependencies"
    assert lines[1].strip() == "1 ready to state"


def test_check_cli_reports_validation_errors(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "bad.md", "no heading\n")
    result = subprocess.run(
        [sys.executable, "-m", "autoform_cli", "check", str(blueprint)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "error: bad: missing H1 title" in result.stdout


def test_a_chapter_directory_with_no_chapter_page_is_refused(tmp_path: Path) -> None:
    """The layout decides what the book is, so check has to assert it.

    Containment comes from nested `README.md` articles, so a directory without
    one is invisible: its pages attach to the roadmap root and the book
    publishes with no chapters. Every node parses and every link resolves,
    which is how a real project shipped 71 of 72 articles at the root with a
    clean run.
    """
    roadmap = tmp_path / "blueprint" / "roadmap"
    (roadmap / "orphaned").mkdir(parents=True)
    (roadmap / "README.md").write_text("---\n---\n\n# Roadmap\n", encoding="utf-8")
    for name in ("first", "second"):
        (roadmap / "orphaned" / f"{name}.md").write_text(
            f"---\ndeclaration: theorem\n---\n\n# {name.title()}\n", encoding="utf-8"
        )

    with pytest.raises(GraphValidationError) as caught:
        load_graph(tmp_path / "blueprint")

    message = str(caught.value)
    assert "orphaned: chapter directory holds 2 article(s) but no README.md" in message
    assert "add orphaned/README.md" in message


def test_a_filing_bucket_inside_a_chapter_is_left_alone(tmp_path: Path) -> None:
    """`definitions/` and `theorems/` are a convention, not a missing level."""
    roadmap = tmp_path / "blueprint" / "roadmap"
    (roadmap / "chapter" / "theorems").mkdir(parents=True)
    (roadmap / "README.md").write_text("---\n---\n\n# Roadmap\n", encoding="utf-8")
    (roadmap / "chapter" / "README.md").write_text("---\n---\n\n# Chapter\n", encoding="utf-8")
    (roadmap / "chapter" / "theorems" / "result.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# Result\n", encoding="utf-8"
    )

    graph = load_graph(tmp_path / "blueprint")

    assert "chapter/theorems/result" in graph.nodes


def test_a_chapter_whose_articles_are_all_in_buckets_is_still_refused(tmp_path: Path) -> None:
    """Counting only direct children let the whole fault back through.

    `orphan/theorems/leaf.md` with nothing beside it leaves `orphan/` with no
    direct Markdown at all, which read as an empty directory. The article then
    attached to the roadmap root and never reached the generated nav.
    """
    roadmap = tmp_path / "blueprint" / "roadmap"
    (roadmap / "orphan" / "theorems").mkdir(parents=True)
    (roadmap / "README.md").write_text("---\n---\n\n# Roadmap\n", encoding="utf-8")
    (roadmap / "orphan" / "theorems" / "leaf.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# Leaf\n", encoding="utf-8"
    )

    with pytest.raises(GraphValidationError) as caught:
        load_graph(tmp_path / "blueprint")

    assert "orphan: chapter directory holds 1 article(s) but no README.md" in str(caught.value)
