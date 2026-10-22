from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.graph import (
    GraphValidationError,
    _containment_cycles,
    atlas_omissions,
    catalogs_without_ledger,
    load_graph,
)
from autoform_cli.graph_views import project_view
from autoform_cli.runtime import RuntimeProjectionError, load_runtime_graph
from autoform_cli.status import derive


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


def test_extracts_reader_facing_summary_for_the_wiki_inspector(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(
        blueprint,
        "topic.md",
        """# Compactness theorem

This **theorem** identifies [compact objects](source.md) by a finite-cover condition
and explains why the characterization matters.

## Depends on
""",
        area="Geometry & Topology",
    )

    node = load_graph(blueprint).nodes["topic"]

    assert node.summary == (
        "This theorem identifies compact objects by a finite-cover condition "
        "and explains why the characterization matters."
    )
    assert node.area == "Geometry & Topology"


def test_summary_skips_a_block_the_page_shows_as_indented_code(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(
        blueprint,
        "topic.md",
        "# Compactness theorem\n\n    compact_iff_finite_subcover\n\n"
        "A space is compact when every open cover has a finite subcover.\n",
    )

    assert load_graph(blueprint).nodes["topic"].summary == (
        "A space is compact when every open cover has a finite subcover."
    )


def test_summary_reads_reference_links_and_definitions_as_the_page_does(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(
        blueprint,
        "topic.md",
        "# T\n\n[paper]: ../sources/paper.md\n\n"
        "The statement cites [the paper][paper], [Smith][] and [[1] Jones](../sources/jones.md).\n"
        "[smith]: ../sources/smith.md\nThe page starts a new paragraph here.\n",
    )

    assert load_graph(blueprint).nodes["topic"].summary == "The statement cites the paper, Smith and [1] Jones."


def test_summary_keeps_the_mathematics_and_code_a_tag_would_span(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(
        blueprint,
        "topic.md",
        "# Ball\n\nFor $0 < a \\le b$ and $r > 0$ the <em>ball</em> `B<r>` is convex.\n"
        "If a < b and c > d, the set $\\{(x, y) :\nx<y, y>0\\}$ is open. The code `Map<String,\nInt>` is shown.\n",
    )

    # A bare `<` opens no tag, and inline math and code may wrap across the paragraph's lines.
    assert load_graph(blueprint).nodes["topic"].summary == (
        "For $0 < a \\le b$ and $r > 0$ the ball `B<r>` is convex. "
        "If a < b and c > d, the set $\\{(x, y) : x<y, y>0\\}$ is open. The code `Map<String, Int>` is shown."
    )


def test_an_escaped_backtick_opens_no_code_span_in_a_summary(tmp_path: Path) -> None:
    """The page reads \\` as a backtick, so it does not pair with a later code span's backtick.

    Inside a span a backslash is literal, so ``a\\`` still closes there, as on the page.
    """
    blueprint = tmp_path / "blueprint"
    _node(
        blueprint,
        "topic.md",
        "# Ticks\n\nA \\` tick, a <em>ball</em> and **bold**\ntext before `B<r>`, then `a\\` and <b>c</b>.\n",
    )

    assert load_graph(blueprint).nodes["topic"].summary == (
        "A \\` tick, a ball and bold text before `B<r>`, then `a\\` and c."
    )


def test_summary_clips_a_long_paragraph_at_a_word(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    words = " ".join(f"word{index}" for index in range(200))
    _node(blueprint, "topic.md", f"# Topic\n\n{words}\n")

    summary = load_graph(blueprint).nodes["topic"].summary

    assert summary == " ".join(f"word{index}" for index in range(61)) + "…"


def test_authored_atlas_manifest_groups_nodes_independently_of_paths(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "algebra.md", "# Algebra\n")
    _node(blueprint, "topology.md", "# Topology\n")
    (blueprint / "atlas.json").write_text(
        json.dumps(
            {
                "schema": "autoform-atlas/v1",
                "areas": {
                    "Algebra": ["algebra"],
                    "Geometry & Topology": ["topology"],
                },
            }
        ),
        encoding="utf-8",
    )

    graph = load_graph(blueprint)

    assert graph.nodes["algebra"].area == "Algebra"
    assert graph.nodes["topology"].area == "Geometry & Topology"


def test_atlas_manifest_keeps_the_first_area_and_names_what_it_ignores(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "algebra.md", "# Algebra\n")
    (blueprint / "atlas.json").write_text(
        json.dumps(
            {
                "schema": "autoform-atlas/v1",
                "areas": {
                    "One": ["algebra", "missing"],
                    "Two": ["algebra"],
                },
            }
        ),
        encoding="utf-8",
    )

    graph = load_graph(blueprint)

    assert graph.nodes["algebra"].area == "One"
    assert atlas_omissions(graph) == [
        "'missing' under 'One' is ignored, since no roadmap node has that id",
        "'algebra' under 'Two' is ignored, since 'One' already holds it",
    ]


def test_dangling_atlas_manifest_link_is_not_treated_as_absent(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "result.md", "# Result\n")
    try:
        (blueprint / "atlas.json").symlink_to(tmp_path / "missing-atlas.json")
    except OSError:
        pytest.skip("the test filesystem cannot create symbolic links")

    assert atlas_omissions(load_graph(blueprint)) == [
        "ignored, since it is not a regular file inside the blueprint"
    ]


@pytest.mark.parametrize(
    ("atlas", "omission"),
    [
        pytest.param(b" " * 1_000_001, "ignored, since it exceeds 1000000 bytes", id="oversize"),
        pytest.param(b"{", "ignored, since it is not valid JSON: ", id="invalid-json"),
        pytest.param(b"[" * 200_000, "ignored, since it is not valid JSON: ", id="too-deep"),
        pytest.param(
            b'{"name": "unrelated tool data", "regions": []}',
            "ignored, since its schema is not 'autoform-atlas/v1'",
            id="other-schema",
        ),
        pytest.param(
            b'{"schema": "autoform-atlas/v1", "areas": ["topic"]}',
            "ignored, since its 'areas' is not an object",
            id="areas-not-an-object",
        ),
        pytest.param(
            b'{"schema": "autoform-atlas/v1", "areas": {"Algebra": "topic"}}',
            "area 'Algebra' is ignored; it needs a name and an array of roadmap node ids",
            id="area-without-an-array",
        ),
        pytest.param(
            b'{"schema": "autoform-atlas/v1", "areas": {"Algebra": ["topic"]}}',
            "'topic' under 'Algebra' is ignored, since its frontmatter area is 'Topology'",
            id="frontmatter-wins",
        ),
    ],
)
def test_atlas_manifest_problems_are_check_warnings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], atlas: bytes, omission: str
) -> None:
    """Earlier versions ignored blueprint/atlas.json, so nothing in it may stop a command."""
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "topic.md", "# Topic\n", area="Topology")
    (blueprint / "atlas.json").write_bytes(atlas)

    assert load_graph(blueprint).nodes["topic"].area == "Topology"
    assert main(["check", str(blueprint)]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith(f"warning: atlas.json: {omission}")


def test_loads_a_non_dispatchable_module_catalog_status(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    ledger = blueprint / "sources/module.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("# Module declarations\n", encoding="utf-8")
    _node(
        blueprint,
        "module.md",
        "# Existing module\n\n## Sources\n\n- [Ledger](../sources/module.md)\n",
        catalog="module",
        lean="Project.alpha Project.beta",
        statement="formalized",
        proof="formalized",
    )

    node = load_graph(blueprint).nodes["module"]
    assert node.catalog == "module"
    assert not node.formalizable
    assert node.statement_formalized
    assert node.proof_formalized


@pytest.mark.parametrize(
    ("link", "status", "last_line"),
    [
        pytest.param("../sources/module.md", 0, "    1 module inventory", id="ledger"),
        pytest.param(
            "../notes/ledger.md",
            0,
            "warning: module: module catalog links no declaration ledger under blueprint/sources/ from '## Sources'",
            id="outside-sources",
        ),
        pytest.param(
            "https://example.com/ledger.md",
            0,
            "warning: module: module catalog links no declaration ledger under blueprint/sources/ from '## Sources'",
            id="external",
        ),
        pytest.param(
            "../sources/missing.md",
            1,
            "error: module: source target does not resolve to a file: '../sources/missing.md'",
            id="missing",
        ),
        # Only audit fails a hidden note under sources/.
        pytest.param(
            "../sources/.private/module.md",
            0,
            "warning: module: source target names a hidden path, which only a repository link reaches, so it must "
            "be committed: '../sources/.private/module.md'",
            id="hidden",
        ),
    ],
)
def test_check_warns_about_a_catalog_that_links_no_ledger_under_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], link: str, status: int, last_line: str
) -> None:
    """A catalog's ledger is a source link: only a dead one fails check, as for any article."""
    blueprint = tmp_path / "blueprint"
    for ledger in ("sources/module.md", "sources/.private/module.md", "notes/ledger.md"):
        (blueprint / ledger).parent.mkdir(parents=True)
        (blueprint / ledger).write_text("# Module declarations\n", encoding="utf-8")
    _node(blueprint, "module.md", f"# Existing module\n\n## Sources\n\n- [Ledger]({link})\n", catalog="module")

    assert main(["check", str(blueprint)]) == status
    assert capsys.readouterr().out.splitlines()[-1] == last_line


@pytest.mark.parametrize(
    "link",
    [
        pytest.param("http://[oops/paper.md", id="invalid-url"),
        pytest.param("../notes/" + "a" * 300 + ".md", id="long-name"),
        pytest.param("../notes/loop.md", id="symlink-loop"),
    ],
)
def test_a_catalog_ledger_listed_after_a_link_that_cannot_be_resolved_still_counts(
    tmp_path: Path, link: str
) -> None:
    # Each link once crashed the ledger rule: urlsplit rejects the first, Path.is_file
    # raises on the long name before Python 3.14, and resolve raises on the loop
    # before 3.13. The rule now reads links without touching files, and none of
    # these is under sources/, so it must read past each one to the ledger.
    blueprint = tmp_path / "blueprint"
    ledger = blueprint / "sources/module.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("# Module declarations\n", encoding="utf-8")
    if link.endswith("loop.md"):
        (blueprint / "notes").mkdir()
        try:
            (blueprint / "notes/loop.md").symlink_to("loop.md")
        except OSError:
            pytest.skip("symlinks are unavailable")
    _node(
        blueprint,
        "module.md",
        f"# Existing module\n\n## Sources\n\n- [Other]({link})\n- [Ledger](../sources/module.md)\n",
        catalog="module",
    )

    assert catalogs_without_ledger(load_graph(blueprint)) == []


def test_formalized_module_catalog_rejects_an_empty_lean_name_list(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    ledger = blueprint / "sources/module.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("# Module declarations\n", encoding="utf-8")
    _node(
        blueprint,
        "module.md",
        "# Existing module\n\n## Sources\n\n- [Ledger](../sources/module.md)\n",
        catalog="module",
        lean=",",
        statement="formalized",
        proof="formalized",
    )

    with pytest.raises(GraphValidationError, match="needs the lean: names of the public declarations it inventories"):
        load_graph(blueprint)


@pytest.mark.parametrize(
    "metadata",
    [
        {"declaration": "theorem"},
        {"mathlib": "true"},
        {"mathlib_declaration": "Mathlib.result"},
        {"mathlib_file": "Mathlib/Result.lean"},
    ],
)
def test_module_catalog_rejects_declaration_specific_metadata(
    tmp_path: Path,
    metadata: dict[str, str],
) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "module.md", "# Existing module\n", catalog="module", **metadata)

    with pytest.raises(GraphValidationError, match="catalog.*cannot be combined"):
        load_graph(blueprint)


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
        (_node_text("# Title\n", catalog="book"), "'catalog' accepts only 'module'"),
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
        (_node_text("# Result\n## Depends on\n[x](http://[oops/a.md)\n"), "relative Markdown path"),
        (_node_text("# Result\n## Depends on\n[x](%00.md)\n"), "contains an invalid path"),
        pytest.param(_node_text("# Result\n## Depends on\n[x](" + "a" * 300 + ".md)\n"), "does not exist", id="long-name"),
    ],
)
def test_rejects_invalid_nodes(tmp_path: Path, body: str, message: str) -> None:
    blueprint = tmp_path / "blueprint"
    _roadmap_page(blueprint, "result.md", body)
    (tmp_path / "outside.md").write_text("# Outside\n", encoding="utf-8")

    with pytest.raises(GraphValidationError, match=message):
        load_graph(blueprint)


def test_a_dependency_through_a_symlink_loop_is_reported_alike_on_every_python(tmp_path: Path) -> None:
    # The kernel stops at the loop, wherever the rest of the path would lead.
    blueprint = tmp_path / "blueprint"
    links = "[x](../loop.md)\n[y](../../outer.md)\n[z](../away/loop.md)\n[w](../loop.md/../away/note.md)\n"
    _node(blueprint, "result.md", f"# Result\n## Depends on\n{links}")
    (tmp_path / "away").mkdir()
    (tmp_path / "away" / "note.md").write_text("# Note\n", encoding="utf-8")
    try:
        (blueprint / "loop.md").symlink_to("loop.md")
        (tmp_path / "outer.md").symlink_to("outer.md")
        # A loop behind a symbolic link that leaves the blueprint, and one before it.
        (blueprint / "away").symlink_to(tmp_path / "away")
        (tmp_path / "away" / "loop.md").symlink_to("loop.md")
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(GraphValidationError) as caught:
        load_graph(blueprint)

    assert caught.value.issues == (
        "result: dependency target does not exist: '../loop.md'",
        "result: dependency target escapes the blueprint directory: '../../outer.md'",
        "result: dependency target does not exist: '../away/loop.md'",
        "result: dependency target does not exist: '../loop.md/../away/note.md'",
    )


def test_check_rejects_a_sources_link_through_symlink_loops(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No program can open `loop.md/../paper.md`, though `..` would cancel the loop as text.
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "result.md", "# Result\n\n## Sources\n\n- [Paper](../sources/s1.md)\n")
    sources = blueprint / "sources"
    sources.mkdir()
    (sources / "paper.md").write_text("# Paper\n", encoding="utf-8")
    try:
        (sources / "loop.md").symlink_to("loop.md")
        (sources / "s1.md").symlink_to("loop.md/../s2.md")
        (sources / "s2.md").symlink_to("loop.md/../paper.md")
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert main(["check", str(blueprint)]) == 1
    assert capsys.readouterr().out == "error: result: source target does not resolve to a file: '../sources/s1.md'\n"


def test_a_dependency_through_a_link_that_cannot_be_read_cannot_be_resolved(
    tmp_path: Path, unreadable_link: Callable[[Path, Path | str], None]
) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "result.md", "# Result\n## Depends on\n[x](../locked/a.md)\n")
    (tmp_path / "away").mkdir()
    (tmp_path / "away" / "a.md").write_text("# A\n", encoding="utf-8")
    unreadable_link(blueprint / "locked", tmp_path / "away")

    with pytest.raises(GraphValidationError) as caught:
        load_graph(blueprint)

    assert caught.value.issues == ("result: dependency target cannot be resolved: '../locked/a.md'",)


def test_a_blueprint_behind_a_link_that_cannot_be_read_loads(
    tmp_path: Path, unreadable_link: Callable[[Path, Path | str], None]
) -> None:
    # A link above the blueprint that cannot be read changes nothing inside it.
    real = tmp_path / "real" / "blueprint"
    _node(real, "result.md", "# Result\n## Depends on\n[x](other.md)\n")
    _node(real, "other.md", "# Other\n")
    unreadable_link(tmp_path / "via", tmp_path / "real")
    blueprint = tmp_path / "via" / "blueprint"

    graph = load_graph(blueprint)

    assert graph.blueprint_dir == real.resolve()
    assert graph.nodes["result"].dependencies == ("other",)
    assert main(["check", str(blueprint)]) == 0


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="permissions do not bind root")
def test_a_dependency_under_an_unreadable_directory_is_an_issue(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "result.md", "# Result\n## Depends on\n[x](../locked/a.md)\n")
    locked = blueprint / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(GraphValidationError) as caught:
            load_graph(blueprint)
    finally:
        locked.chmod(0o755)

    assert caught.value.issues == ("result: dependency target does not exist: '../locked/a.md'",)


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


def test_accepts_cycle_created_only_by_chapter_contraction(tmp_path: Path) -> None:
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

    graph = load_graph(blueprint)

    assert graph.nodes["b/middle"].dependencies == ("a/first",)
    assert graph.nodes["a/last"].dependencies == ("b/middle",)
    assert {(edge.source, edge.target) for edge in project_view(graph, derive(graph)).edges} == {
        ("scope:a", "scope:b"),
        ("scope:b", "scope:a"),
    }


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


def test_reads_code_only_labels_and_references_defined_anywhere_on_the_page(tmp_path: Path) -> None:
    """Issue #302: each of these renders as a link, so each is an edge."""
    blueprint = tmp_path / "blueprint"
    for name in ("code", "full", "shortcut"):
        _node(blueprint, f"{name}.md", f"# {name.title()}\n")
    _node(
        blueprint,
        "only.md",
        "# Only\n\n## Depends on\n- [`Code.name`](code.md)\n- [Full][full]\n- [Shortcut] and [hidden]\n\n"
        "## Notes\n[full]: full.md\n[shortcut]: shortcut.md\n\n```\n[hidden]: missing.md\n```\n",
    )

    assert load_graph(blueprint).nodes["only"].dependencies == ("code", "full", "shortcut")


def test_a_comment_opener_inside_a_code_fence_hides_no_dependency(tmp_path: Path) -> None:
    # The page shows the section below the fence, so the graph must keep its edge.
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n")
    _node(
        blueprint,
        "only.md",
        """# Only

```html
<!-- an example of markup
```

## Depends on
- [Base](base.md)

````markdown
```python
[Inside the outer fence](missing.md)
```
````
""",
    )

    assert load_graph(blueprint).nodes["only"].dependencies == ("base",)


@pytest.mark.parametrize(
    "fence",
    [
        "```lean\nexample : True := trivial\n``` -- end\n",
        "```lean\nexample : True := trivial\n",
        "~~~\n[Shown as text](base.md)\n",
    ],
)
def test_a_fence_that_never_closes_hides_no_dependency(tmp_path: Path, fence: str) -> None:
    # The page draws a fence without a closing line as plain text, so the links
    # below it are on the page and stay in the graph.
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n")
    _node(blueprint, "only.md", f"# Only\n\n## Depends on\n\n{fence}\n- [Base](base.md)\n")

    assert set(load_graph(blueprint).nodes["only"].dependencies) == {"base"}


def test_a_fence_closes_only_on_a_bare_fence_line(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n")
    _node(
        blueprint,
        "only.md",
        "# Only\n\n## Depends on\n\n```lean\n``` -- not the end\n[Hidden](missing.md)\n```\n\n- [Base](base.md)\n",
    )

    assert load_graph(blueprint).nodes["only"].dependencies == ("base",)


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


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="permissions do not bind root")
@pytest.mark.parametrize("relative", ["", "chapter"], ids=["roadmap", "chapter"])
def test_a_roadmap_directory_that_cannot_be_listed_is_refused(tmp_path: Path, relative: str) -> None:
    """A glob skipped it, and its articles were missing from the graph without a word."""
    blueprint = tmp_path / "blueprint"
    _roadmap_page(blueprint, "README.md", "# Roadmap\n")
    _node(blueprint, "chapter/result.md", "# Result\n")
    directory = (blueprint / "roadmap" / relative).resolve()
    directory.chmod(0)
    try:
        with pytest.raises(GraphValidationError) as caught:
            load_graph(blueprint)
    finally:
        directory.chmod(0o755)

    assert f"cannot list roadmap directory {directory}: Permission denied" in caught.value.issues


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
        ({"statement": "bogus"}, "'statement' accepts only 'formalized' or 'retracted'"),
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


def test_records_a_retracted_statement(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "result.md", "# Result\n", declaration="theorem", statement="Retracted", lean="Ns.result")

    node = load_graph(blueprint).nodes["result"]

    assert (node.statement_retracted, node.statement_formalized, node.proof_formalized) == (True, False, False)


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({}, "result: statement: retracted needs the lean: declaration it retracts; without lean:, omit statement"),
        (
            {"lean": "Ns.result", "proof": "formalized"},
            "result: proof: formalized needs statement: formalized, not retracted",
        ),
        (
            {"lean": "Ns.result", "mathlib": "true"},
            "result: a mathlib: true article cannot record statement: retracted",
        ),
    ],
)
def test_rejects_a_retracted_statement_that_cannot_keep_its_declaration(
    tmp_path: Path, metadata: dict[str, str], message: str
) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "result.md", "# Result\n", declaration="theorem", statement="retracted", **metadata)

    with pytest.raises(GraphValidationError) as raised:
        load_graph(blueprint)

    assert raised.value.issues == (message,)


@pytest.mark.parametrize(
    ("metadata", "issues"),
    [
        (
            {"proof": "formalized", "lean": "Ns.result"},
            ("result: proof: formalized needs statement: formalized",),
        ),
        (
            {"proof": "formalized", "lean": "Ns.result", "mathlib": "true"},
            ("result: proof: formalized needs statement: formalized",),
        ),
        (
            {"statement": "formalized"},
            ("result: statement: formalized needs the lean: declaration that formalizes it",),
        ),
        (
            {"statement": "formalized", "proof": "formalized", "mathlib": "true"},
            ("result: statement: formalized needs the lean: declaration that formalizes it",),
        ),
        (
            {"statement": "formalized", "lean": ","},
            ("result: statement: formalized needs the lean: declaration that formalizes it",),
        ),
        (
            {"proof": "formalized"},
            (
                "result: proof: formalized needs statement: formalized",
                "result: proof: formalized needs the lean: declaration that formalizes it",
            ),
        ),
    ],
)
def test_rejects_formalized_work_the_lean_does_not_show(
    tmp_path: Path, metadata: dict[str, str], issues: tuple[str, ...]
) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "result.md", "# Result\n", declaration="theorem", **metadata)

    with pytest.raises(GraphValidationError) as raised:
        load_graph(blueprint)

    assert raised.value.issues == issues


def test_a_link_shown_as_indented_code_is_neither_edge_nor_source(tmp_path: Path) -> None:
    # The page shows an indented block as code, so its links are not on the
    # page. A list item's indented continuation is still part of the list.
    blueprint = tmp_path / "blueprint"
    source = blueprint / "sources" / "paper.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Paper\n", encoding="utf-8")
    for name in ("base", "other", "third"):
        _node(blueprint, f"chapter/{name}.md", f"# {name.title()}\n")
    _node(
        blueprint,
        "chapter/result.md",
        "# Result\n\n## Depends on\n\nThe syntax:\n\n    [Base](base.md)\n\n- [Other](other.md)\n\n"
        "    [Third](third.md)\n\n## Sources\n\nFor example:\n\n    [Paper](../../sources/paper.md#result)\n",
        origin="cited",
    )

    node = load_graph(blueprint).nodes["chapter/result"]
    assert set(node.dependencies) == {"chapter/other", "chapter/third"}
    assert node.sources == ()


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
    _node(blueprint, "base.md", "# Base\n", declaration="theorem")
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


def test_check_cli_separates_module_inventories_from_target_statuses(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "result.md", "# Result\n", declaration="theorem")
    ledger = blueprint / "sources/catalog.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("# Catalog declarations\n", encoding="utf-8")
    _node(
        blueprint,
        "catalog.md",
        "# Existing module\n\n## Sources\n\n- [Ledger](../sources/catalog.md)\n",
        catalog="module",
        lean="Project.catalogEntry",
        statement="formalized",
        proof="formalized",
    )

    result = subprocess.run(
        [sys.executable, "-m", "autoform_cli", "check", str(blueprint)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "OK: 2 articles, 0 dependencies",
        "    1 ready to state",
        "    1 module inventory",
    ]
    assert "fully proved" not in result.stdout


def test_check_counts_declaration_targets_and_every_lean_name_it_resolves(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A chapter page and a prose leaf are no targets, and a catalog's names each count."""
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "chapter/result.md", "# Result\n", declaration="theorem")
    _node(blueprint, "chapter/notes.md", "# Notes\n")
    (blueprint / "sources").mkdir()
    (blueprint / "sources/catalog.md").write_text("# Catalog declarations\n", encoding="utf-8")
    catalog = _node(
        blueprint,
        "catalog.md",
        "# Existing module\n\n## Sources\n\n- [Ledger](../sources/catalog.md)\n",
        catalog="module",
        lean="Inv.a Inv.b Inv.c",
    )
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "Inv.lean").write_text(
        "".join(f"theorem Inv.{name} : True := trivial\n" for name in "abc"), encoding="utf-8"
    )

    assert main(["check", str(blueprint), "--lean-root", str(lean_root)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "OK: 4 articles, 0 dependencies",
        "    1 ready to state",
        "    1 module inventory",
        "    3 declaration name(s) resolved in the Lean sources",
    ]
    # A catalog's unresolved name may be one Lean generates, which no index records.
    catalog.write_text(catalog.read_text(encoding="utf-8").replace("Inv.c", "Inv.c Inv.mk"), encoding="utf-8")
    assert main(["check", str(blueprint), "--lean-root", str(lean_root)]) == 1
    assert capsys.readouterr().out.splitlines()[-2:] == [
        f"error: catalog: declaration not found in {lean_root}: Inv.mk; "
        "a name Lean generates is not indexed and belongs in the catalog's ledger",
        _LIBRARY_DECLARATION_NOTE,
    ]


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


def test_check_warns_about_a_container_that_declares_something(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #221: audit reports this as formalizable-container, but the generated workflows run check.

    The site still builds, so like #325's warnings it keeps the exit code. A declaring README.md with
    nothing below it is a leaf, and a container below the top level is caught.
    """
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "chapter/section/leaf.md", "# Leaf\n", declaration="theorem")
    _node(blueprint, "chapter/other/README.md", "# Other\n", declaration="theorem")
    assert main(["check", str(blueprint)]) == 0
    assert "warning:" not in capsys.readouterr().out

    _node(blueprint, "chapter/section/README.md", "# Section\n", declaration="theorem")

    assert main(["check", str(blueprint)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("OK: ")
    assert [line for line in lines if line.startswith("warning:")] == lines[-1:] == [
        "warning: chapter/section: formalizable article has contained articles; declaration-sized articles must "
        "be leaves; move the declaration and its lean, statement and proof fields into a leaf article"
    ]


def test_check_rejects_sources_links_that_name_no_file_in_the_blueprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #207: the Pages workflow runs check, not audit.

    In the issue's project, without `sources/`, such a link surfaced only as a
    strict MkDocs failure naming the chapter page that absorbed the article; with
    the directory, a missing note became a repository link that 404s. Check now
    applies audit's rule that a Sources path names a file in the blueprint, and
    leaves fragments and other schemes to audit; it only warns that work and
    search omit a link with another scheme.
    """
    blueprint = tmp_path / "blueprint"
    sources = "# Result\n\n## Sources\n\n- [Paper](../sources/paper.md#lemma-3)\n"
    _node(blueprint, "result.md", sources)

    assert main(["check", str(blueprint)]) == 1
    assert capsys.readouterr().out == (
        "error: result: source target does not resolve to a file: '../sources/paper.md#lemma-3'\n"
    )

    (blueprint / "sources").mkdir()
    (blueprint / "sources" / "paper.md").write_text("# Paper\n", encoding="utf-8")
    sources += "- [Book](https://example.org/book)\n- [Author](mailto:author@example.org)\n"
    _node(blueprint, "result.md", sources)

    assert main(["check", str(blueprint)]) == 0
    # The note, its fragment and the https link stay in the projection.
    assert [line for line in capsys.readouterr().out.splitlines() if line.startswith("warning:")] == [
        "warning: result: work and search omit this source target, which is neither an http(s) link "
        "nor a path inside the blueprint: 'mailto:author@example.org'"
    ]

    # Issue #304: render omits hidden files, so a link that names one fails
    # although the file exists. Only a repository link reaches a hidden note
    # under sources/, so check only warns about one that exists. Nothing needs
    # to step through a hidden folder, so that fails too.
    for note in (".drafts/notes.md", "sources/.private/paper.md"):
        (blueprint / note).parent.mkdir(exist_ok=True)
        (blueprint / note).write_text("# Notes\n", encoding="utf-8")
    hidden = "- [Draft](../.drafts/notes.md)\n- [Stepped](../.drafts/../sources/paper.md)\n"
    hidden += "- [Private](../sources/.private/paper.md)\n- [Through](../.drafts/../sources/.private/paper.md)\n"
    _node(blueprint, "result.md", sources + hidden + "- [Lost](../sources/.private/lost.md)\n")

    assert main(["check", str(blueprint)]) == 1
    assert capsys.readouterr().out == (
        "error: result: source target names a hidden path, which render omits: '../.drafts/notes.md'\n"
        "error: result: source target names a hidden path, which render omits: '../.drafts/../sources/paper.md'\n"
        "error: result: source target names a hidden path, which render omits: "
        "'../.drafts/../sources/.private/paper.md'\n"
        "error: result: source target does not resolve to a file: '../sources/.private/lost.md'\n"
    )

    (tmp_path / "slides.pdf").write_bytes(b"%PDF-1.4\n")
    rejected = (
        "- [Notes](../sources/)\n- [Slides](../../slides.pdf)\n- [Bad](../sources/%00.md)\n"
        "- [Host](http://[oops/paper.md)\n"
    )
    _node(blueprint, "result.md", sources + rejected)

    assert main(["check", str(blueprint)]) == 1
    assert capsys.readouterr().out == (
        "error: result: source target does not resolve to a file: '../sources/'\n"
        "error: result: source target escapes the blueprint directory: '../../slides.pdf'\n"
        "error: result: source target contains an invalid path: '../sources/%00.md'\n"
        "error: result: source target is not a valid URL: 'http://[oops/paper.md'\n"
    )


def test_check_warns_about_a_hidden_note_under_sources(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Render can link it to the repository, so unlike a hidden target elsewhere it resolves once committed."""
    blueprint = tmp_path / "blueprint"
    note = blueprint / "sources" / ".private" / "paper.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Paper\n", encoding="utf-8")
    _node(blueprint, "result.md", "# Result\n\n## Sources\n\n- [Private](../sources/.private/paper.md#lemma-3)\n")

    assert main(["check", str(blueprint)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("OK: ")
    assert [line for line in lines if line.startswith("warning:")] == lines[-1:] == [
        "warning: result: source target names a hidden path, which only a repository link reaches, so it must be "
        "committed: '../sources/.private/paper.md#lemma-3'"
    ]


def test_check_ignores_hidden_roadmap_entries_as_render_does(tmp_path: Path) -> None:
    """Issue #206: render omits hidden entries, so check must count the same articles."""
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n")
    _node(blueprint, ".helper.md", "# Helper\n")
    # A hidden folder needs no chapter page, since nothing in it is published.
    _roadmap_page(blueprint, ".drafts/draft.md", _node_text("# Draft\n"))
    # Nor does a folder whose only page is hidden.
    _roadmap_page(blueprint, "drafts/.wip.md", _node_text("# Work in progress\n"))
    try:
        # Emacs leaves this lock symlink beside a page with unsaved edits.
        (blueprint / "roadmap" / ".#base.md").symlink_to("user@host.1234:1700000000")
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert list(load_graph(blueprint).nodes) == ["base"]


def test_a_dependency_on_a_hidden_page_names_the_hidden_path(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, ".draft.md", "# Draft\n")
    _node(blueprint, "cited.md", "# Cited\n")
    # Render's copy has no .x to step through, though the vault has one.
    (blueprint / "roadmap" / ".x").mkdir()
    _node(blueprint, "base.md", "# Base\n## Depends on\n[Draft](.draft.md)\n[Cited](.x/../cited.md)\n")

    with pytest.raises(GraphValidationError) as caught:
        load_graph(blueprint)

    assert caught.value.issues == (
        "base: dependency target names a hidden path, which render omits: '.draft.md'",
        "base: dependency target names a hidden path, which render omits: '.x/../cited.md'",
    )


def test_an_unpublished_folder_is_checked_and_named(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n")
    _node(blueprint, "drafts/README.md", "# Drafts\n", published="false")
    _node(blueprint, "drafts/idea.md", "# Idea\n## Depends on\n[Base](../base.md)\n")

    graph = load_graph(blueprint)

    assert sorted(graph.nodes) == ["base", "drafts", "drafts/idea"]
    assert graph.unpublished == ((blueprint / "roadmap" / "drafts").resolve(),)


def test_published_is_a_folder_setting(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _roadmap_page(blueprint, "README.md", _node_text("# Roadmap\n", published="false"))
    _node(blueprint, "base.md", "# Base\n", published="false")

    with pytest.raises(GraphValidationError) as caught:
        load_graph(blueprint)

    assert caught.value.issues == tuple(
        f"{node}: published is a folder setting; set it in a folder's README.md below roadmap/"
        for node in ("roadmap", "base")
    )


def test_a_published_page_may_not_link_into_an_unpublished_folder(tmp_path: Path) -> None:
    """The site lacks the folder, so a strict MkDocs build fails on any link into it."""
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "drafts/README.md", "# Drafts\n", published="false")
    _node(blueprint, "drafts/idea.md", "# Idea\n")
    # Unpublished pages may link to each other, across folders too.
    _node(blueprint, "notes/README.md", "# Notes\n", published="false")
    _node(blueprint, "notes/aside.md", "# Aside\n## Depends on\n[Idea](../drafts/idea.md)\n")
    _node(
        blueprint,
        "base.md",
        "# Base\n\nSee [the drafts](drafts/README.md).\n\n## Depends on\n[Idea](drafts/idea.md)\n\n"
        "## Sources\n\n- [Idea][idea]\n\n[idea]: <drafts/idea.md#idea>\n\n`[Not a link](drafts/)`\n",
    )

    with pytest.raises(GraphValidationError) as caught:
        load_graph(blueprint)

    assert caught.value.issues == tuple(
        f"base: links to {target!r}, in an unpublished folder render omits"
        for target in ("drafts/README.md", "drafts/idea.md", "drafts/idea.md#idea")
    )


def test_check_rejects_a_sources_link_to_a_generated_view(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Render writes its own dependencies.md and omits the vault's copy, so the link cannot be checked against it.
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "result.md", "# Result\n\n## Sources\n\n- [Graph](../dependencies.md)\n")
    (blueprint / "dependencies.md").write_text("# Dependencies\n", encoding="utf-8")

    assert main(["check", str(blueprint)]) == 1
    assert capsys.readouterr().out == (
        "error: result: source target names a generated view, which render omits: '../dependencies.md'\n"
    )


def test_check_warns_about_a_mathlib_file_search_reports_as_null(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "result.md", "# Result\n", mathlib_file="../Outside.lean")
    _node(blueprint, "kept.md", "# Kept\n", mathlib_file="Mathlib\\Kept.lean")

    assert main(["check", str(blueprint)]) == 0
    assert [line for line in capsys.readouterr().out.splitlines() if line.startswith("warning:")] == [
        "warning: result: search reports mathlib_file as null, since it is not a portable relative path: "
        "'../Outside.lean'"
    ]


def test_check_warns_about_an_article_whose_byte_order_mark_hides_its_frontmatter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    marked = _node(blueprint, "result.md", "# Result\n", lean="Demo.result")
    marked.write_bytes(b"\xef\xbb\xbf" + marked.read_bytes())
    _node(blueprint, "kept.md", "# Kept\n", lean="Demo.kept")

    assert main(["check", str(blueprint)]) == 0
    lines = capsys.readouterr().out.splitlines()
    # Like #325's warnings, it follows the whole summary.
    assert lines[0].startswith("OK: ")
    assert [line for line in lines if line.startswith("warning:")] == lines[-1:] == [
        "warning: result: starts with a byte-order mark, so frontmatter on its first line is read as text "
        "and its keys are ignored; remove the mark"
    ]


@pytest.mark.parametrize(
    ("other", "errors"),
    [
        ("private theorem Project.base : True := trivial\n", []),
        (
            "theorem Project.base : True := trivial\n",
            ["error: base: declaration defined more than once in {root}: Project.base (A.lean:1, B.lean:2)"],
        ),
    ],
)
def test_check_resolves_a_name_beside_a_private_twin_and_rejects_two_public_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], other: str, errors: list[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n", lean="Project.base")
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "A.lean").write_text(other, encoding="utf-8")
    (lean_root / "B.lean").write_text("\ndef Project.base : Nat := 0\n", encoding="utf-8")

    assert main(["check", str(blueprint), "--lean-root", str(lean_root)]) == (1 if errors else 0)
    lines = capsys.readouterr().out.splitlines()
    assert [line for line in lines if line.startswith("error:")] == [error.format(root=lean_root) for error in errors]


_LIBRARY_DECLARATION_NOTE = (
    "note: check resolves lean: names only in the project's own Lean files, not in Mathlib or other dependencies "
    "under .lake; if a name is a shared library's declaration, record it in the article's implementation note instead"
)


@pytest.mark.parametrize(
    ("lean", "expected"),
    [
        (
            "Lib.base Lib.other",
            [
                "error: base: declaration not found in {root}: Lib.base",
                "error: base: declaration not found in {root}: Lib.other",
                _LIBRARY_DECLARATION_NOTE,
            ],
        ),
        (
            "Project.base",
            ["error: base: declaration defined more than once in {root}: Project.base (A.lean:1, B.lean:1)"],
        ),
    ],
    ids=["library-declarations", "two-public-declarations"],
)
def test_check_says_where_a_library_declaration_goes_when_a_name_is_not_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], lean: str, expected: list[str]
) -> None:
    """Issue #346: a trial put library declarations in `lean:`, and the error alone did not say where they go."""
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n", lean=lean)
    lean_root = tmp_path / "lean"
    (lean_root / ".lake/packages/lib").mkdir(parents=True)
    (lean_root / ".lake/packages/lib/Lib.lean").write_text(
        "def Lib.base : Nat := 0\ndef Lib.other : Nat := 0\n", encoding="utf-8"
    )
    (lean_root / "A.lean").write_text("def Project.base : Nat := 0\n", encoding="utf-8")
    (lean_root / "B.lean").write_text("theorem Project.base : True := trivial\n", encoding="utf-8")

    assert main(["check", str(blueprint), "--lean-root", str(lean_root)]) == 1
    lines = capsys.readouterr().out.splitlines()
    expected = [line.format(root=lean_root) for line in expected]
    # The errors, then one note however many names are missing, end the output; a name defined twice gets no note.
    assert [line for line in lines if line.startswith(("error:", "note:"))] == lines[-len(expected) :] == expected


def test_check_prints_its_warnings_after_the_whole_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n\n## Sources\n\n- [Author](mailto:author@example.org)\n", lean="Project.base")
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "Base.lean").write_text("def Project.base : Nat := 0\n", encoding="utf-8")

    assert main(["check", str(blueprint), "--lean-root", str(lean_root)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sum(line.startswith("warning:") for line in lines) == 1
    assert lines[-2:] == [
        "    1 declaration name(s) resolved in the Lean sources",
        "warning: base: work and search omit this source target, which is neither an http(s) link "
        "nor a path inside the blueprint: 'mailto:author@example.org'",
    ]


def test_check_reads_links_with_a_code_only_label_or_a_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #302: the site links both shapes, so check validates each target once and the graph keeps the edge."""
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "foo-bar.md", "# Foo bar\n", declaration="def")
    _node(
        blueprint,
        "baz.md",
        "# Baz\n\n## Depends on\n- [`Foo.bar`](foo-bar.md)\n\n"
        "## Sources\n- [`il:thm:3.2`](../sources/missing.md)\n- [Paper, Lemma 1][paper]\n- [Paper, Lemma 2][paper]\n\n"
        "[paper]: ../sources/paper.md\n",
        declaration="theorem",
    )

    assert main(["check", str(blueprint)]) == 1
    assert capsys.readouterr().out == (
        "error: baz: source target does not resolve to a file: '../sources/missing.md'\n"
        "error: baz: source target does not resolve to a file: '../sources/paper.md'\n"
    )
    assert load_graph(blueprint).nodes["baz"].dependencies == ("foo-bar",)


def test_check_refuses_a_symlinked_readme_instead_of_hanging(tmp_path: Path) -> None:
    """Issue #58: this README made `a` and `a/b` each other's parent, and check never returned."""
    blueprint = tmp_path / "blueprint"
    _roadmap_page(blueprint, "README.md", "# Root\n")
    _roadmap_page(blueprint, "a/b/README.md", "# B\n")
    _roadmap_page(blueprint, "a/b/notes.txt", "# A\n")
    try:
        (blueprint / "roadmap" / "a" / "README.md").symlink_to("b/notes.txt")
    except OSError:
        pytest.skip("symlinks are unavailable")

    result = subprocess.run(
        [sys.executable, "-m", "autoform_cli", "check", str(blueprint)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1
    assert "error: roadmap contains a symbolic link: roadmap/a/README.md" in result.stdout
    assert str(tmp_path) not in result.stdout


def test_check_and_runtime_refuse_the_same_roadmap_symlinks(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n")
    outside = tmp_path / "figure.png"
    outside.write_bytes(b"png")
    try:
        (blueprint / "roadmap" / "figure.png").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(GraphValidationError, match="roadmap contains a symbolic link: roadmap/figure.png"):
        load_graph(blueprint)
    with pytest.raises(RuntimeProjectionError, match="roadmap contains a symbolic link: roadmap/figure.png"):
        load_runtime_graph(blueprint)


def test_check_and_runtime_refuse_a_symlinked_roadmap_directory(tmp_path: Path) -> None:
    _roadmap_page(tmp_path / "real", "README.md", "# Root\n")
    blueprint = tmp_path / "blueprint"
    blueprint.mkdir()
    try:
        (blueprint / "roadmap").symlink_to(tmp_path / "real" / "roadmap", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(GraphValidationError, match="roadmap contains a symbolic link: roadmap$"):
        load_graph(blueprint)
    with pytest.raises(RuntimeProjectionError, match="roadmap contains a symbolic link: roadmap$"):
        load_runtime_graph(blueprint)


def test_a_readme_symlink_outside_the_roadmap_contains_nothing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _roadmap_page(project / "blueprint", "README.md", "# Root\n")
    _roadmap_page(project / "blueprint", "a.md", "# A\n")
    try:
        (project / "README.md").symlink_to("blueprint/roadmap/README.md")
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert load_graph(project / "blueprint").nodes["roadmap"].parent is None


def test_load_graph_refuses_a_containment_cycle_before_depths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blueprint = tmp_path / "blueprint"
    _roadmap_page(blueprint, "README.md", "# Root\n")
    _roadmap_page(blueprint, "a.md", "# A\n")
    monkeypatch.setattr("autoform_cli.graph._article_parents", lambda *_: {"roadmap": "a", "a": "roadmap"})
    monkeypatch.setattr("autoform_cli.graph._article_depth", lambda *_: pytest.fail("depth walked a containment cycle"))

    with pytest.raises(GraphValidationError, match="a: containment cycle: a -> roadmap -> a"):
        load_graph(blueprint)


def test_containment_cycles_are_reported_once_per_cycle() -> None:
    parents: dict[str, str | None] = {"a": "a/b", "a/b": "a", "c": "a", "roadmap": None}

    assert _containment_cycles(parents) == ["a: containment cycle: a -> a/b -> a"]
    assert _containment_cycles({"a": "roadmap", "roadmap": None}) == []


def test_check_cli_reports_source_index_io_failure_before_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n", lean="Project.base")

    def fail_index(root: Path):
        raise OSError(f"private host detail: {root}")

    monkeypatch.setattr("autoform_cli.__main__.index_project", fail_index)

    assert main(["check", str(blueprint), "--lean-root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "error: Lean sources could not be indexed\n"
    assert captured.err == ""
    assert str(tmp_path) not in captured.out


def test_check_cli_names_an_unsafe_lean_source_without_host_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n", lean="Project.base")
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    outside = tmp_path / "Outside.lean"
    outside.write_text("def Project.base : Nat := 0\n", encoding="utf-8")
    try:
        (lean_root / "Linked.lean").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert main(["check", str(blueprint), "--lean-root", str(lean_root)]) == 1
    captured = capsys.readouterr()
    assert captured.out == (
        "error: Lean sources could not be indexed: "
        "unsafe Lean source Linked.lean: symbolic links are not supported\n"
    )
    assert str(tmp_path) not in captured.out


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


@pytest.mark.parametrize("name", ["agents.md", "AGENTS.md"])
def test_agents_filenames_remain_ordinary_articles(tmp_path: Path, name: str) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / name).write_text(f"# {name}\n", encoding="utf-8")

    graph = load_graph(tmp_path / "blueprint")

    assert list(graph.nodes) == [Path(name).stem]


def test_implementation_notes_are_keyed_by_durable_article_id(tmp_path: Path) -> None:
    """A hidden per-article note survives a roadmap path move."""
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    (roadmap / "chapter").mkdir(parents=True)
    (roadmap / "moved").mkdir()
    (roadmap / "README.md").write_text("---\n---\n\n# Roadmap\n", encoding="utf-8")
    (roadmap / "chapter" / "README.md").write_text("---\n---\n\n# Chapter\n", encoding="utf-8")
    (roadmap / "moved" / "README.md").write_text("---\n---\n\n# Moved\n", encoding="utf-8")
    article_id = "af_0123456789abcdef01234567"
    (roadmap / "chapter" / "result.md").write_text(
        f"---\narticle_id: {article_id}\ndeclaration: theorem\n---\n\n# Result\n",
        encoding="utf-8",
    )
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / f"{article_id}.md").write_text(
        "Mathlib has no `Project.result` yet.\n", encoding="utf-8"
    )
    (notes / ".DS_Store").write_bytes(b"finder metadata")

    graph = load_graph(blueprint)
    assert sorted(graph.nodes) == ["chapter", "chapter/result", "moved", "roadmap"]

    (roadmap / "chapter" / "result.md").rename(roadmap / "moved" / "result.md")

    moved = load_graph(blueprint)
    assert moved.nodes["moved/result"].article_id == article_id


@pytest.mark.parametrize(
    ("filename", "contents", "message"),
    [
        ("result.md", "route\n", "must be named <article_id>.md"),
        ("af_0123456789abcdef01234567.txt", "route\n", "must be named <article_id>.md"),
        (
            "af_111111111111111111111111.md",
            "route\n",
            "names no roadmap article; restore the article_id of an article that still exists",
        ),
        ("af_0123456789abcdef01234567.md", "\n", "empty implementation note"),
        ("af_0123456789abcdef01234567.md", "\ufeff\u200b\n", "empty implementation note"),
    ],
)
def test_implementation_notes_fail_closed(
    tmp_path: Path, filename: str, contents: str, message: str
) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    article_id = "af_0123456789abcdef01234567"
    (roadmap / "result.md").write_text(
        f"---\narticle_id: {article_id}\ndeclaration: theorem\n---\n\n# Result\n",
        encoding="utf-8",
    )
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / filename).write_text(contents, encoding="utf-8")

    with pytest.raises(GraphValidationError, match=message):
        load_graph(blueprint)


def test_implementation_notes_directory_has_canonical_spelling(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    article_id = "af_0123456789abcdef01234567"
    (roadmap / "result.md").write_text(
        f"---\narticle_id: {article_id}\ndeclaration: theorem\n---\n\n# Result\n",
        encoding="utf-8",
    )
    notes = blueprint / ".Implementation-Notes"
    notes.mkdir()
    (notes / f"{article_id}.md").write_text("route\n", encoding="utf-8")

    with pytest.raises(GraphValidationError, match="noncanonical implementation notes"):
        load_graph(blueprint)


def test_implementation_note_must_be_utf8(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    article_id = "af_0123456789abcdef01234567"
    (roadmap / "result.md").write_text(
        f"---\narticle_id: {article_id}\ndeclaration: theorem\n---\n\n# Result\n",
        encoding="utf-8",
    )
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / f"{article_id}.md").write_bytes(b"\xff")

    with pytest.raises(GraphValidationError, match="cannot read implementation note"):
        load_graph(blueprint)


def test_implementation_note_must_not_be_a_symlink(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    article_id = "af_0123456789abcdef01234567"
    (roadmap / "result.md").write_text(
        f"---\narticle_id: {article_id}\ndeclaration: theorem\n---\n\n# Result\n",
        encoding="utf-8",
    )
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    target = tmp_path / "note.md"
    target.write_text("route\n", encoding="utf-8")
    try:
        (notes / f"{article_id}.md").symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(GraphValidationError, match="must be a regular file"):
        load_graph(blueprint)


def test_hidden_entries_beside_implementation_notes_are_ignored(tmp_path: Path) -> None:
    """Editors leave hidden lock symlinks, such as Emacs's `.#<name>`, beside a note."""
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    article_id = "af_0123456789abcdef01234567"
    (roadmap / "result.md").write_text(
        f"---\narticle_id: {article_id}\ndeclaration: theorem\n---\n\n# Result\n",
        encoding="utf-8",
    )
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / f"{article_id}.md").write_text("route\n", encoding="utf-8")
    try:
        (notes / f".#{article_id}.md").symlink_to("user@host.4242:1760000000")
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert list(load_graph(blueprint).nodes) == ["result"]


def test_an_implementation_note_for_a_hidden_page_names_no_article(tmp_path: Path) -> None:
    # Render leaves the hidden page out, and check agrees.
    blueprint = tmp_path / "blueprint"
    article_id = "af_0123456789abcdef01234567"
    _node(blueprint, "base.md", "# Base\n")
    _node(blueprint, ".parked.md", "# Parked\n", article_id=article_id)
    (blueprint / ".implementation-notes").mkdir()
    (blueprint / ".implementation-notes" / f"{article_id}.md").write_text("route\n", encoding="utf-8")

    with pytest.raises(GraphValidationError, match="implementation note names no roadmap article"):
        load_graph(blueprint)


def test_implementation_notes_directory_must_not_be_a_symlink(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    article_id = "af_0123456789abcdef01234567"
    (roadmap / "result.md").write_text(
        f"---\narticle_id: {article_id}\ndeclaration: theorem\n---\n\n# Result\n",
        encoding="utf-8",
    )
    target = tmp_path / "notes"
    target.mkdir()
    try:
        (blueprint / ".implementation-notes").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(GraphValidationError, match="directory must not be a symlink"):
        load_graph(blueprint)


def test_article_error_does_not_misreport_its_note_as_orphaned(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    article_id = "af_0123456789abcdef01234567"
    (roadmap / "result.md").write_text(
        f"---\narticle_id: {article_id}\ndeclaration: theorem\n---\n\nNo H1.\n",
        encoding="utf-8",
    )
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / f"{article_id}.md").write_text("route\n", encoding="utf-8")

    with pytest.raises(GraphValidationError) as caught:
        load_graph(blueprint)

    assert caught.value.issues == ("result: missing H1 title",)


@pytest.mark.parametrize(
    ("value", "allowed"),
    [
        (None, False),
        ("allowed", True),
        ("forbidden", False),
        ('"allowed"', True),
        ("'forbidden'", False),
        ("ALLOWED", True),
        ("Forbidden", False),
    ],
)
def test_open_statements_is_read_from_the_roadmap_root(
    tmp_path: Path, value: str | None, allowed: bool
) -> None:
    """Absent means forbidden; values unquote and casefold like every other scalar."""
    blueprint = tmp_path / "blueprint"
    policy = "" if value is None else f"open_statements: {value}\n"
    _roadmap_page(blueprint, "README.md", f"---\n{policy}---\n\n# Roadmap\n")
    _node(blueprint, "result.md", "# Result\n", declaration="theorem")

    assert load_graph(blueprint).open_statements is allowed


def test_rejects_an_open_statements_value_other_than_allowed_or_forbidden(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _roadmap_page(blueprint, "README.md", "---\nopen_statements: yes\n---\n\n# Roadmap\n")

    with pytest.raises(GraphValidationError) as caught:
        load_graph(blueprint)

    assert caught.value.issues == ("roadmap:2: 'open_statements' accepts allowed or forbidden",)


@pytest.mark.parametrize(("relative", "node_id"), [("result.md", "result"), ("chapter/README.md", "chapter")])
def test_open_statements_set_outside_the_roadmap_root_is_refused(
    tmp_path: Path, relative: str, node_id: str
) -> None:
    """The policy belongs to the project, so a chapter or article cannot opt in on its own."""
    blueprint = tmp_path / "blueprint"
    _roadmap_page(blueprint, "README.md", "---\n---\n\n# Roadmap\n")
    _node(blueprint, relative, "# Result\n", open_statements="forbidden")

    with pytest.raises(GraphValidationError) as caught:
        load_graph(blueprint)

    assert caught.value.issues == (
        f"{node_id}: open_statements is a project policy; set it only in roadmap/README.md",
    )
