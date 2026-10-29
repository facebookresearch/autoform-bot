from __future__ import annotations

import random
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli.graph import (
    Graph,
    Node,
    _find_cycles,
    load_graph,
)
from autoform_cli import graph_pages, graph_views, render
from autoform_cli.audit import audit_graph
from autoform_cli.coverage import load_coverage, validate_coverage_roles
from autoform_cli.graph_views import chapter_view, group_nodes, project_view, scope_view
from autoform_cli.render import _book_page_order, render_site
from autoform_cli.review import ReviewError, ReviewFinding, build_review_bundle
from autoform_cli.runtime import (
    _validate_depths,
    _validate_runtime,
    build_runtime_graph,
    load_runtime_graph,
)
from autoform_cli.status import derive, topological_order
from tests.test_review_bundle import _extracted
from tests.test_review_bundle import _blueprint as _review_blueprint


class _CountingDict(dict):
    def __init__(self, values: dict[str, Node]) -> None:
        super().__init__(values)
        self.values_calls = 0
        self.lookups = 0

    def __contains__(self, key: object) -> bool:
        self.lookups += 1
        return super().__contains__(key)

    def __getitem__(self, key: str) -> Node:
        self.lookups += 1
        return super().__getitem__(key)

    def values(self):
        self.values_calls += 1
        return super().values()


class _CountingTuple(tuple):
    def __new__(cls, values):
        instance = super().__new__(cls, values)
        instance.iterations = 0
        return instance

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def _chain_graph(tmp_path: Path, count: int, *, containment: bool = False) -> Graph:
    roadmap = tmp_path / "blueprint" / "roadmap"
    nodes: dict[str, Node] = {}
    for index in range(count):
        node_id = f"n{index:04d}"
        next_id = f"n{index + 1:04d}"
        dependencies = (next_id,) if not containment and index + 1 < count else ()
        parent = f"n{index - 1:04d}" if containment and index else None
        nodes[node_id] = Node(
            id=node_id,
            title=node_id,
            path=roadmap / f"{node_id}.md",
            dependencies=dependencies,
            statement_dependencies=dependencies,
            parent=parent,
            depth=index if containment else 0,
            declaration="theorem" if containment and index + 1 == count else None,
        )
    return Graph(tmp_path / "blueprint", nodes)










def test_dependency_walk_handles_a_1200_node_chain(tmp_path: Path) -> None:
    graph = _chain_graph(tmp_path, 1_200)

    assert _find_cycles(graph.nodes) == []
    order = topological_order(graph)
    assert order[0] == "n1199"
    assert order[-1] == "n0000"
    assert len(derive(graph)) == 1_200


def test_runtime_depth_validation_is_linear_on_a_deep_hierarchy(tmp_path: Path) -> None:
    graph = _chain_graph(tmp_path, 1_200, containment=True)
    nodes = _CountingDict(graph.nodes)
    graph = Graph(graph.blueprint_dir, nodes)
    issues: list[str] = []

    _validate_depths(graph, issues)

    assert issues == []
    assert nodes.lookups < 10 * len(nodes)


def test_scope_view_handles_a_1200_level_containment_chain(tmp_path: Path) -> None:
    graph = _chain_graph(tmp_path, 1_200, containment=True)

    view = scope_view(graph, derive(graph), "n0000", include_external=False)

    assert view.member_ids == ("n1199",)


def test_book_order_follows_a_1200_deep_book(tmp_path: Path) -> None:
    """Each page holds the next page and a note, and links the page first, so
    the walk goes 1,200 levels down before it reads any note. The notes sort
    first, so a walk that ignored links would read each one at its level."""
    blueprint = tmp_path.resolve() / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    (blueprint / "README.md").write_text("# Book\n\n[First](roadmap/page0000.md)\n", encoding="utf-8")
    nodes: dict[str, Node] = {}
    for index in range(1_200):
        node_id, parent = f"page{index:04d}", f"page{index - 1:04d}" if index else None
        path = roadmap / f"{node_id}.md"
        links = f"\n[Next](page{index + 1:04d}.md)\n[Note](a-note{index:04d}.md)\n" if index + 1 < 1_200 else ""
        path.write_text(f"# {node_id}\n{links}", encoding="utf-8")
        nodes[node_id] = Node(node_id, node_id, path, (), parent=parent)
        if index + 1 < 1_200:
            note = f"a-note{index:04d}"
            (roadmap / f"{note}.md").write_text(f"# {note}\n", encoding="utf-8")
            nodes[note] = Node(note, note, roadmap / f"{note}.md", (), parent=node_id)
    graph = Graph(blueprint, nodes)

    ordered = _book_page_order(blueprint, blueprint, graph)

    assert len(ordered) == 1 + 1_200 + 1_199
    assert ordered[0] == blueprint / "README.md"
    assert ordered[1_200] == roadmap / "page1199.md"
    assert ordered[1_201] == roadmap / "a-note1198.md"
    assert ordered[-1] == roadmap / "a-note0000.md"






def test_runtime_validation_does_not_scan_for_children_per_node(tmp_path: Path) -> None:
    project = tmp_path / "project"
    roadmap = project / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / "item.md").write_text("# Item\n", encoding="utf-8")
    runtime = load_runtime_graph(project)
    base = runtime.nodes[0]
    nodes = _CountingTuple(
        replace(
            base,
            id=f"n{index:04d}",
            article_path=f"blueprint/roadmap/n{index:04d}.md",
            parent=f"n{index - 1:04d}" if index else None,
            depth=index,
        )
        for index in range(1_200)
    )
    runtime = replace(
        runtime,
        nodes=nodes,
        article_count=len(nodes),
        formalizable_count=0,
        dispatchable_count=0,
        dependency_count=0,
        maximum_depth=len(nodes) - 1,
    )
    scans_after_construction = nodes.iterations

    _validate_runtime(runtime)

    assert nodes.iterations - scans_after_construction < 10


def _containment_graph(tmp_path: Path, chapters: int, articles: int) -> Graph:
    roadmap = tmp_path / "blueprint" / "roadmap"
    nodes = {"roadmap": Node("roadmap", "Roadmap", roadmap / "README.md", ())}
    previous: str | None = None
    for chapter_index in range(chapters):
        chapter = f"c{chapter_index:03d}"
        nodes[chapter] = Node(chapter, chapter, roadmap / chapter / "README.md", (), parent="roadmap", depth=1)
        for article_index in range(articles):
            node_id = f"{chapter}/a{article_index:03d}"
            dependencies = (previous,) if previous is not None else ()
            nodes[node_id] = Node(
                node_id,
                node_id,
                roadmap / f"{node_id}.md",
                dependencies,
                statement_dependencies=dependencies,
                parent=chapter,
                depth=2,
                declaration="theorem",
            )
            previous = node_id
    return Graph(tmp_path / "blueprint", nodes)


def _raises_promptly(call: Callable[[], object]) -> BaseException | None:
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            call()
        except BaseException as error:  # noqa: BLE001 - reported to the caller
            outcome.append(error)
        else:
            outcome.append(None)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "view did not terminate on a containment cycle"
    return outcome[0]


@pytest.mark.parametrize(
    "render",
    (
        lambda graph: group_nodes(graph),
        lambda graph: project_view(graph, derive(graph)),
        lambda graph: chapter_view(graph, derive(graph), "a"),
        lambda graph: scope_view(graph, derive(graph), "a"),
    ),
    ids=("group_nodes", "project_view", "chapter_view", "scope_view"),
)
def test_views_reject_hand_built_containment_cycles(tmp_path: Path, render) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    graph = Graph(
        tmp_path / "blueprint",
        {
            "roadmap": Node("roadmap", "Roadmap", roadmap / "README.md", ()),
            "a": Node("a", "A", roadmap / "a" / "README.md", (), parent="b"),
            "b": Node("b", "B", roadmap / "b" / "README.md", (), parent="a"),
            "leaf": Node("leaf", "Leaf", roadmap / "a" / "leaf.md", ("roadmap",), parent="a"),
        },
    )

    error = _raises_promptly(lambda: render(graph))

    assert isinstance(error, ValueError)
    assert "containment is not a forest" in str(error)


def test_views_index_containment_once_instead_of_scanning_per_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _containment_graph(tmp_path, 20, 30)
    statuses = derive(graph)
    expected_project = project_view(graph, statuses)
    expected_scope = scope_view(graph, statuses, "c003")

    def unindexed(self: Graph, node_id: str) -> tuple[str, ...]:
        raise AssertionError(f"view scanned the graph for children of {node_id}")

    monkeypatch.setattr(Graph, "children", unindexed)

    assert project_view(graph, statuses) == expected_project
    assert scope_view(graph, statuses, "c003") == expected_scope
    assert chapter_view(graph, statuses, "c003") == expected_scope
    assert len(group_nodes(graph)) == 20


def _nested_graph(tmp_path: Path, chapters: int, sections: int, *, seed: int = 0) -> Graph:
    """Chapters holding sections holding one theorem each, with seeded cross-scope edges."""
    roadmap = tmp_path / "blueprint" / "roadmap"
    randomizer = random.Random(seed)
    nodes = {"roadmap": Node("roadmap", "Roadmap", roadmap / "README.md", ())}
    leaves: list[str] = []
    for chapter_index in range(chapters):
        chapter = f"c{chapter_index:04d}"
        nodes[chapter] = Node(chapter, chapter, roadmap / chapter / "README.md", (), parent="roadmap", depth=1)
        for section_index in range(sections):
            section = f"{chapter}/s{section_index:02d}"
            nodes[section] = Node(section, section, roadmap / section / "README.md", (), parent=chapter, depth=2)
            leaf = f"{section}/t"
            earlier = randomizer.sample(leaves, min(len(leaves), 3))
            statement = tuple(earlier[:2])
            proof = tuple(earlier[1:])
            nodes[leaf] = Node(
                leaf,
                leaf,
                roadmap / f"{leaf}.md",
                tuple(dict.fromkeys((*statement, *proof))),
                statement_dependencies=statement,
                proof_dependencies=proof,
                parent=section,
                depth=3,
                declaration="theorem",
            )
            leaves.append(leaf)
    return Graph(tmp_path / "blueprint", nodes)


@pytest.mark.parametrize("include_external", (True, False))
def test_bulk_scope_views_equal_the_single_scope_view_of_every_container(
    tmp_path: Path,
    include_external: bool,
) -> None:
    graph = _nested_graph(tmp_path, 6, 4, seed=60)
    statuses = derive(graph)
    containers = [node_id for node_id in graph.nodes if graph.children(node_id)]

    views = graph_views.scope_views(graph, statuses, include_external=include_external)

    assert list(views) == containers
    for scope in containers:
        assert views[scope] == scope_view(graph, statuses, scope, include_external=include_external)


def test_bulk_scope_views_equal_single_scope_views_on_irregular_forests(tmp_path: Path) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    randomizer = random.Random(60)
    for _ in range(40):
        # Several roots, leaves beside containers, and edges that end on containers.
        ids = [f"n{index:02d}" for index in range(randomizer.randint(2, 24))]
        nodes: dict[str, Node] = {}
        for index, node_id in enumerate(ids):
            parent = randomizer.choice([None, *ids[:index]]) if index else None
            earlier = randomizer.sample(ids[:index], min(index, randomizer.randint(0, 3)))
            nodes[node_id] = Node(
                node_id,
                node_id,
                roadmap / f"{node_id}.md",
                tuple(earlier),
                statement_dependencies=tuple(earlier[:1]),
                proof_dependencies=tuple(earlier),
                parent=parent,
            )
        graph = Graph(tmp_path / "blueprint", nodes)
        statuses = derive(graph)

        for include_external in (True, False):
            views = graph_views.scope_views(graph, statuses, include_external=include_external)

            assert list(views) == [node_id for node_id in ids if graph.children(node_id)]
            for scope, view in views.items():
                assert view == scope_view(graph, statuses, scope, include_external=include_external)


def test_bulk_scope_views_reject_hand_built_containment_cycles(tmp_path: Path) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    graph = Graph(
        tmp_path / "blueprint",
        {
            "a": Node("a", "A", roadmap / "a" / "README.md", (), parent="b"),
            "b": Node("b", "B", roadmap / "b" / "README.md", (), parent="a"),
        },
    )

    error = _raises_promptly(lambda: graph_views.scope_views(graph, derive(graph)))

    assert isinstance(error, ValueError)
    assert "containment is not a forest" in str(error)


def _scope_view_lookups(tmp_path: Path, chapters: int) -> int:
    graph = _nested_graph(tmp_path / str(chapters), chapters, 10)
    statuses = derive(graph)
    nodes = _CountingDict(graph.nodes)
    graph_views.scope_views(Graph(graph.blueprint_dir, nodes), statuses)
    return nodes.lookups


def test_bulk_scope_views_scale_linearly_with_the_number_of_containers(tmp_path: Path) -> None:
    small = _scope_view_lookups(tmp_path, 20)
    large = _scope_view_lookups(tmp_path, 40)

    # Doubling the containers doubles the relations too.  Scanning every
    # relation once per container therefore quadruples the node lookups.
    assert large <= 2.5 * small


def _index_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chapters: int) -> dict[str, int]:
    graph = _nested_graph(tmp_path / str(chapters), chapters, 3)
    nodes = _CountingDict(graph.nodes)
    builds = {"_containment_children": 0, "_relations": 0}
    contexts: dict[str, tuple[Path, str]] = {}
    destination = tmp_path / str(chapters) / "site"

    def counted(name: str):
        original = getattr(graph_views, name)

        def wrapper(*args, **kwargs):
            builds[name] += 1
            return original(*args, **kwargs)

        return wrapper

    with monkeypatch.context() as patch:
        for name in builds:
            patch.setattr(graph_views, name, counted(name))
        graph_pages.write_graph_pages(
            Graph(graph.blueprint_dir, nodes),
            derive(graph),
            destination,
            node_links=lambda page, node_ids: {
                node_id: "book.html" for node_id in node_ids
            },
            contexts_out=contexts,
        )
    assert set(contexts) == set(graph.nodes)
    assert contexts["roadmap"] == (destination / "dependencies.json", "roadmap")
    assert contexts["c0000/s00/t"] == (destination / "dependencies/scopes/c0000/s00.json", "c0000/s00")
    assert contexts["c0000/s00"][1] == "c0000/s00"
    return {**builds, "graph walks": nodes.values_calls}


def test_graph_page_publication_builds_whole_graph_indexes_a_fixed_number_of_times(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    small = _index_builds(tmp_path, monkeypatch, 4)
    large = _index_builds(tmp_path, monkeypatch, 8)

    # Every section has its own page. Rebuilding the containers set for each
    # page's legend made the graph walks grow with the chapters.
    assert large == small
    assert max(small["_containment_children"], small["_relations"]) <= 4


def _written_blueprint(tmp_path: Path, sections: int = 1, chapters: int = 1, dependencies: bool = False) -> Path:
    blueprint = tmp_path / "blueprint"
    # One chapter keeps the "chapter" directory that other tests name.
    groups = {"chapter": "Chapter"} if chapters == 1 else {f"chapter{n}": f"Chapter {n}" for n in range(chapters)}
    names = [f"section{index}" for index in range(sections)]
    for group in groups:
        for name in names:
            (blueprint / "roadmap" / group / name).mkdir(parents=True)
    (blueprint / "README.md").write_text("# Book\n\n[Roadmap](roadmap/README.md)\n", encoding="utf-8")
    (blueprint / "roadmap" / "README.md").write_text(
        "# Roadmap\n\n" + "".join(f"[{title}]({group}/README.md)\n" for group, title in groups.items()),
        encoding="utf-8",
    )
    (blueprint / "coverage").mkdir()
    (blueprint / "coverage" / "README.md").write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n"
        "| Project scope | MAPPED | Source audit pending |\n"
        + "".join(
            f"| {group}/{name} | DECOMPOSED | [A](../roadmap/{group}/{name}/a.md) |\n"
            for group in groups
            for name in names
        ),
        encoding="utf-8",
    )
    for group, title in groups.items():
        chapter = blueprint / "roadmap" / group
        (chapter / "README.md").write_text(
            f"# {title}\n\n" + "".join(f"[{name}]({name}/README.md)\n" for name in names), encoding="utf-8"
        )
        for name in names:
            section = chapter / name
            (section / "README.md").write_text(f"# {name}\n\n[A](a.md)\n[B](b.md)\n", encoding="utf-8")
            (section / "a.md").write_text("---\ndeclaration: theorem\n---\n# A\n\nStatement.\n", encoding="utf-8")
            # With a dependency, each chapter page reads node links of its own, not only the maps.
            (section / "b.md").write_text(
                "---\ndeclaration: theorem\n---\n# B\n\nUses [A](a.md).\n"
                + ("\n## Depends on\n\n- [A](a.md)\n" if dependencies else ""),
                encoding="utf-8",
            )
    return blueprint


def _forbid_child_scans(monkeypatch: pytest.MonkeyPatch) -> None:
    def unindexed(self: Graph, node_id: str) -> tuple[str, ...]:
        raise AssertionError(f"scanned the graph for children of {node_id}")

    monkeypatch.setattr(Graph, "children", unindexed)


def test_site_rendering_does_not_scan_for_children_per_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _written_blueprint(tmp_path)
    _forbid_child_scans(monkeypatch)

    render_site(blueprint, tmp_path / "site")

    assert (tmp_path / "site" / "dependencies" / "scopes" / "chapter" / "section0.md").is_file()


def _render_calls(blueprint: Path, monkeypatch: pytest.MonkeyPatch, owner: object, name: str) -> int:
    """How often rendering *blueprint* calls ``owner.name``."""
    calls = 0
    original = getattr(owner, name)

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(owner, name, counted)
        render_site(blueprint, blueprint.parent / "site")
    return calls


def test_site_rendering_indexes_containers_a_fixed_number_of_times(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # render imports the index builder that graph_views' own helpers call.
    monkeypatch.setattr(graph_views, "_containers", lambda graph: render._containers(graph))
    small, large = (
        _render_calls(_written_blueprint(tmp_path / str(sections), sections), monkeypatch, render, "_containers")
        for sections in (2, 6)
    )

    assert large == small


def _path_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sections: int = 1, chapters: int = 1) -> int:
    blueprint = _written_blueprint(tmp_path / f"{chapters}x{sections}", sections, chapters, dependencies=True)
    return _render_calls(blueprint, monkeypatch, Path, "resolve")


@pytest.mark.parametrize("axis", ["sections", "chapters"])
def test_site_rendering_resolves_paths_linearly_in_sections_and_chapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    axis: str,
) -> None:
    small = _path_resolves(tmp_path, monkeypatch, **{axis: 8})
    large = _path_resolves(tmp_path, monkeypatch, **{axis: 16})

    # Every section and chapter has its own scope map page, and every section a
    # DECOMPOSED coverage row. Linking every node from every page made the
    # resolves grow with the pages times the nodes.
    assert large <= 2 * small


def test_site_rendering_never_iterates_a_pages_node_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A page's node links cover the whole graph and are built when read, so
    # iterating or copying them on every page makes render quadratic again.
    def iterated(self: object) -> None:
        raise AssertionError("a page's node links were iterated")

    monkeypatch.setattr(render._PageLinks, "__iter__", iterated)

    render_site(_written_blueprint(tmp_path, 2, chapters=2, dependencies=True), tmp_path / "site")


def _coverage_role_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sections: int) -> int:
    blueprint = _written_blueprint(tmp_path / str(sections), sections)
    graph = load_graph(blueprint)
    coverage, _issues = load_coverage(blueprint)
    assert coverage is not None
    calls = 0
    original = Path.resolve

    def counted(self: Path, strict: bool = False) -> Path:
        nonlocal calls
        calls += 1
        return original(self, strict)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", counted)
        validate_coverage_roles(graph, coverage)
    return calls


def test_coverage_roles_resolve_paths_linearly_in_the_number_of_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    small = _coverage_role_resolves(tmp_path, monkeypatch, 8)
    large = _coverage_role_resolves(tmp_path, monkeypatch, 16)

    # Every section adds a DECOMPOSED row. Resolving every article's path again
    # for each row took the role check from 216 to 816 resolves; counted across
    # a whole render, the same growth read as only 2.26 times.
    assert large <= 2 * small


def test_audit_does_not_scan_for_children_per_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = load_graph(_written_blueprint(tmp_path))
    expected = audit_graph(graph)
    _forbid_child_scans(monkeypatch)

    assert audit_graph(graph) == expected


def _review_refusal(graph: Graph) -> tuple[ReviewFinding, ...]:
    with pytest.raises(ReviewError) as error:
        build_review_bundle(graph, _extracted(graph))
    return tuple(error.value.findings)


def test_review_evidence_does_not_scan_for_children_per_article(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blueprint = _review_blueprint(tmp_path)
    graph = load_graph(blueprint)
    report = _extracted(graph)
    bundle = build_review_bundle(graph, report)
    (blueprint / "roadmap" / "basics" / "README.md").write_text(
        f"---\narticle_id: af_{'c' * 24}\ndeclaration: theorem\nlean: Skel.chapter\n---\n# Basics\n", encoding="utf-8"
    )
    container = load_graph(blueprint)
    refusal = _review_refusal(container)
    assert ("basics", "review-article-shape") in {(finding.node_id, finding.code) for finding in refusal}
    _forbid_child_scans(monkeypatch)

    assert build_review_bundle(graph, report) == bundle
    assert _review_refusal(container) == refusal


def test_runtime_projection_indexes_children_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    roadmap = project / "blueprint" / "roadmap"
    (roadmap / "chapter").mkdir(parents=True)
    (roadmap / "README.md").write_text("# Roadmap\n", encoding="utf-8")
    (roadmap / "chapter" / "README.md").write_text("# Chapter\n", encoding="utf-8")
    for index in range(40):
        (roadmap / "chapter" / f"a{index:02d}.md").write_text(
            f"---\ndeclaration: theorem\n---\n# A{index}\n", encoding="utf-8"
        )
    graph = load_graph(project / "blueprint")
    expected = build_runtime_graph(graph, project_root=project)

    def unindexed(self: Graph, node_id: str) -> tuple[str, ...]:
        raise AssertionError(f"runtime projection scanned the graph for children of {node_id}")

    monkeypatch.setattr(Graph, "children", unindexed)

    runtime = build_runtime_graph(graph, project_root=project)
    assert runtime == expected
    assert runtime.dispatchable_count == 40
    assert not runtime.get("chapter").dispatchable  # type: ignore[union-attr]


def test_runtime_accepts_hand_built_nodes_without_a_source_digest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    roadmap = project / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / "item.md").write_text("# Item\n", encoding="utf-8")
    graph = load_graph(project / "blueprint")
    hand_built = Graph(graph.blueprint_dir, {"item": replace(graph.nodes["item"], source_sha256=None)})

    runtime = build_runtime_graph(hand_built, project_root=project)

    item = runtime.get("item")
    assert item is not None
    assert item.source_sha256 is None
    (roadmap / "item.md").write_text("# Item\n\nEdited.\n", encoding="utf-8")
    edited = build_runtime_graph(hand_built, project_root=project)
    assert edited.source_revision != runtime.source_revision
    assert edited.source_revision == load_runtime_graph(project).source_revision
