from __future__ import annotations

import pickle
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
    _find_rollup_cycles,
    load_graph,
)
from autoform_cli import graph_pages, graph_views, render
from autoform_cli.audit import audit_graph
from autoform_cli.graph_views import chapter_view, group_nodes, project_view, scope_view
from autoform_cli.render import _book_page_order, render_site
from autoform_cli.runtime import (
    _validate_depths,
    _validate_runtime,
    build_runtime_graph,
    load_runtime_graph,
)
from autoform_cli.status import derive, topological_order


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










def test_dependency_and_rollup_walks_handle_a_1200_node_chain(tmp_path: Path) -> None:
    graph = _chain_graph(tmp_path, 1_200)

    assert _find_cycles(graph.nodes) == []
    assert _find_rollup_cycles(graph.nodes) == []
    order = topological_order(graph)
    assert order[0] == "n1199"
    assert order[-1] == "n0000"
    assert len(derive(graph)) == 1_200


def test_rollup_projection_is_subquadratic_on_a_deep_branching_hierarchy(
    tmp_path: Path,
) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    raw_nodes: dict[str, Node] = {}
    for index in range(400):
        container = f"container{index:04d}"
        leaf = f"leaf{index:04d}"
        parent = f"container{index - 1:04d}" if index else None
        dependencies = (f"leaf{index - 1:04d}",) if index else ()
        raw_nodes[container] = Node(
            container,
            container,
            roadmap / container / "README.md",
            dependencies,
            parent=parent,
        )
        raw_nodes[leaf] = Node(leaf, leaf, roadmap / f"{leaf}.md", (), parent=container)
    nodes = _CountingDict(raw_nodes)

    assert _find_rollup_cycles(nodes) == []
    assert nodes.values_calls <= 2
    assert nodes.lookups < 20 * len(nodes) * len(nodes).bit_length()


def _reference_rollup_cycles(nodes: dict[str, Node]) -> list[str]:
    children: dict[str | None, list[str]] = {}
    for node in nodes.values():
        children.setdefault(node.parent, []).append(node.id)

    def direct_child(scope: str | None, node_id: str) -> str | None:
        current = node_id
        while nodes[current].parent != scope:
            parent = nodes[current].parent
            if parent is None:
                return None
            current = parent
        return current

    issues: list[str] = []
    for scope, siblings in children.items():
        if len(siblings) < 2:
            continue
        dependencies = {sibling: set() for sibling in siblings}
        for target in nodes.values():
            target_child = direct_child(scope, target.id)
            if target_child not in dependencies:
                continue
            for dependency in target.dependencies:
                source_child = direct_child(scope, dependency)
                if source_child in dependencies and source_child != target_child:
                    dependencies[target_child].add(source_child)
        state: dict[str, int] = {}
        stack: list[str] = []

        def visit(article_id: str) -> None:
            state[article_id] = 1
            stack.append(article_id)
            for prerequisite in sorted(dependencies[article_id]):
                if state.get(prerequisite, 0) == 0:
                    visit(prerequisite)
                elif state.get(prerequisite) == 1:
                    start = stack.index(prerequisite)
                    cycle = stack[start:] + [prerequisite]
                    label = scope or "root"
                    message = f"rolled-up dependency cycle in {label}: {' -> '.join(cycle)}"
                    if message not in issues:
                        issues.append(message)
            stack.pop()
            state[article_id] = 2

        for article_id in sorted(dependencies):
            if state.get(article_id, 0) == 0:
                visit(article_id)
    return issues


def test_rollup_projection_preserves_exact_randomized_cycle_diagnostics(tmp_path: Path) -> None:
    generator = random.Random(87231)
    roadmap = tmp_path / "blueprint" / "roadmap"
    for case in range(300):
        node_ids = [f"n{index:02d}" for index in range(generator.randrange(1, 28))]
        nodes: dict[str, Node] = {}
        for index, node_id in enumerate(node_ids):
            parent = None if index == 0 or generator.random() < 0.22 else node_ids[generator.randrange(index)]
            dependencies = tuple(
                candidate for candidate in node_ids if candidate != node_id and generator.random() < 0.07
            )
            nodes[node_id] = Node(
                node_id,
                node_id,
                roadmap / f"{node_id}.md",
                dependencies,
                parent=parent,
            )

        assert _find_rollup_cycles(nodes) == _reference_rollup_cycles(nodes), case


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


def test_book_order_handles_a_1200_page_link_chain(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    (blueprint / "README.md").write_text(
        "# Book\n\n[First](roadmap/page0000.md)\n",
        encoding="utf-8",
    )
    nodes: dict[str, Node] = {}
    for index in range(1_200):
        node_id = f"page{index:04d}"
        path = roadmap / f"{node_id}.md"
        next_link = f"\n[Next](page{index + 1:04d}.md)\n" if index + 1 < 1_200 else ""
        path.write_text(f"# {node_id}\n{next_link}", encoding="utf-8")
        nodes[node_id] = Node(node_id, node_id, path, ())
    graph = Graph(blueprint, nodes)

    ordered = _book_page_order(blueprint, blueprint, graph)

    assert len(ordered) == 1_201
    assert ordered[0] == blueprint / "README.md"
    assert ordered[-1] == roadmap / "page1199.md"






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


def test_graph_keeps_the_callers_plain_node_mapping(tmp_path: Path) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    nodes = {"root": Node("root", "Root", roadmap / "README.md", ())}
    graph = Graph(tmp_path / "blueprint", nodes)

    assert graph.nodes is nodes
    assert type(graph.nodes) is dict
    nodes["late"] = Node("late", "Late", roadmap / "late.md", (), parent="root")
    assert graph.children("root") == ("late",)
    dict.__setitem__(nodes, "later", Node("later", "Later", roadmap / "later.md", (), parent="root"))
    assert graph.children("root") == ("late", "later")


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_graph_pickles_only_public_types(tmp_path: Path, protocol: int) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    graph = Graph(
        tmp_path / "blueprint",
        {
            "root": Node("root", "Root", roadmap / "README.md", ()),
            "child": Node("child", "Child", roadmap / "child.md", (), parent="root"),
        },
    )

    restored = pickle.loads(pickle.dumps(graph, protocol=protocol))

    assert restored == graph
    assert type(restored.nodes) is dict
    assert restored.children("root") == ("child",)


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
    builds = {"_containment_children": 0, "_relations": 0}

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
            graph,
            derive(graph),
            tmp_path / str(chapters) / "site",
            node_links=lambda page: {node_id: "book.html" for node_id in graph.nodes},
        )
    return builds


def test_graph_page_publication_builds_whole_graph_indexes_a_fixed_number_of_times(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    small = _index_builds(tmp_path, monkeypatch, 4)
    large = _index_builds(tmp_path, monkeypatch, 8)

    assert large == small
    assert max(small.values()) <= 4


def _written_blueprint(tmp_path: Path, sections: int = 1) -> Path:
    blueprint = tmp_path / "blueprint"
    chapter = blueprint / "roadmap" / "chapter"
    names = [f"section{index}" for index in range(sections)]
    for name in names:
        (chapter / name).mkdir(parents=True)
    (blueprint / "README.md").write_text("# Book\n\n[Roadmap](roadmap/README.md)\n", encoding="utf-8")
    (blueprint / "roadmap" / "README.md").write_text("# Roadmap\n\n[Chapter](chapter/README.md)\n", encoding="utf-8")
    (chapter / "README.md").write_text(
        "# Chapter\n\n" + "".join(f"[{name}]({name}/README.md)\n" for name in names), encoding="utf-8"
    )
    (blueprint / "coverage").mkdir()
    (blueprint / "coverage" / "README.md").write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n"
        "| Project scope | MAPPED | Source audit pending |\n",
        encoding="utf-8",
    )
    for name in names:
        section = chapter / name
        (section / "README.md").write_text(f"# {name}\n\n[A](a.md)\n[B](b.md)\n", encoding="utf-8")
        (section / "a.md").write_text("---\ndeclaration: theorem\n---\n# A\n\nStatement.\n", encoding="utf-8")
        (section / "b.md").write_text(
            "---\ndeclaration: theorem\n---\n# B\n\nUses [A](a.md).\n", encoding="utf-8"
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


def _container_index_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sections: int) -> int:
    blueprint = _written_blueprint(tmp_path / str(sections), sections)
    builds = 0
    original = render._containers

    def counted(graph: Graph) -> frozenset[str]:
        nonlocal builds
        builds += 1
        return original(graph)

    with monkeypatch.context() as patch:
        patch.setattr(render, "_containers", counted)
        render_site(blueprint, tmp_path / str(sections) / "site")
    return builds


def test_site_rendering_indexes_containers_a_fixed_number_of_times(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _container_index_builds(tmp_path, monkeypatch, 6) == _container_index_builds(tmp_path, monkeypatch, 2)


def test_audit_does_not_scan_for_children_per_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = load_graph(_written_blueprint(tmp_path))
    expected = audit_graph(graph)
    _forbid_child_scans(monkeypatch)

    assert audit_graph(graph) == expected


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
