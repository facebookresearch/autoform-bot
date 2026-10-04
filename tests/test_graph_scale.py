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
from autoform_cli.graph_views import chapter_view, group_nodes, project_view, scope_view
from autoform_cli.render import _book_page_order
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
