from __future__ import annotations

import base64
import hashlib
import pickle
import random
from dataclasses import asdict, fields, replace
from pathlib import Path

import pytest

from autoform_cli.graph import (
    Graph,
    Node,
    _find_cycles,
    _find_rollup_cycles,
    _TrackedNodeDict,
    load_graph,
)
from autoform_cli.graph_views import scope_view
from autoform_cli.render import _book_page_order
from autoform_cli.runtime import (
    RuntimeGraph,
    RuntimeProjectionError,
    _source_revision,
    _validate_depths,
    _validate_runtime,
    build_runtime_graph,
    load_runtime_graph,
)
from autoform_cli.status import derive, topological_order


# Protocol-5 pickle produced by Graph/Node at parent commit d9e29c210b385b52889ff243422f2d0342778b60.
_PARENT_GRAPH_PICKLE = base64.b64decode(
    "gAWVkgEAAAAAAACMEmF1dG9mb3JtX2NsaS5ncmFwaJSMBUdyYXBolJOUKYGUXZQojAdwYXRobGlilIwJUG9zaXhQYXRolJOUjA5s"
    "ZWdhY3ktcHJvamVjdJSMCWJsdWVwcmludJSGlFKUfZQojAdyb2FkbWFwlGgAjAROb2RllJOUKYGUXZQoaA2MB1JvYWRtYXCUaAco"
    "aAhoCWgNjAlSRUFETUUubWSUdJRSlCkpKYwHYXJ0aWNsZZROTomJiU5OiU5OKU5LAE6MQDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAw"
    "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDCUZWKMBWNoaWxklGgPKYGUXZQoaBiMBUNoaWxklGgHKGgI"
    "aAloDYwIY2hpbGQubWSUdJRSlCkpKWgWTk6JiYlOTolOTiloDUsBToxAMTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTEx"
    "MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMZRlYnVlYi4="
)


class _CountingDict(_TrackedNodeDict):
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


def test_graph_children_cache_tracks_public_mutations_without_changing_order(tmp_path: Path) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    nodes = _CountingDict(
        {
            "root": Node("root", "Root", roadmap / "README.md", ()),
            "second": Node("second", "Second", roadmap / "second.md", (), parent="root"),
            "first": Node("first", "First", roadmap / "first.md", (), parent="root"),
        }
    )
    graph = Graph(tmp_path / "blueprint", nodes)
    revision = graph._children_revision

    assert tuple(field.name for field in fields(Graph)) == ("blueprint_dir", "nodes")
    assert "_children_by_parent" not in repr(graph)
    for _ in range(1_200):
        assert graph.children("root") == ("second", "first")
        assert graph.children("missing") == ()

    assert graph._children_revision == revision

    graph.nodes["third"] = Node("third", "Third", roadmap / "third.md", (), parent="root")
    assert graph.children("root") == ("second", "first", "third")

    graph.nodes["second"] = replace(graph.nodes["second"], parent=None)
    assert graph.children("root") == ("first", "third")

    graph.nodes.pop("first")
    assert graph.children("root") == ("third",)


def test_graph_children_cache_tracks_public_dict_reinitialization(tmp_path: Path) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    root = Node("root", "Root", roadmap / "README.md", ())
    old_child = Node("old", "Old", roadmap / "old.md", (), parent="root")
    graph = Graph(tmp_path / "blueprint", {"root": root, "old": old_child})
    assert graph.children("root") == ("old",)
    cached_revision = graph._children_revision

    new_child = Node("new", "New", roadmap / "new.md", (), parent="root")
    graph.nodes.__init__({"old": replace(old_child, parent=None), "new": new_child})

    assert graph.nodes == {"root": root, "old": replace(old_child, parent=None), "new": new_child}
    assert graph.nodes.revision > cached_revision
    assert graph.children("root") == ("new",)


def test_parent_format_graph_pickle_restores_cache_and_builds_runtime(tmp_path: Path) -> None:
    graph = pickle.loads(_PARENT_GRAPH_PICKLE)
    assert isinstance(graph.nodes, _TrackedNodeDict)
    assert graph.children("roadmap") == ("child",)

    project = tmp_path / "project"
    blueprint = project / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    sources = {
        "roadmap": (roadmap / "README.md", b"# Roadmap\n"),
        "child": (roadmap / "child.md", b"# Child\n"),
    }
    object.__setattr__(graph, "blueprint_dir", blueprint)
    for node_id, (path, content) in sources.items():
        path.write_bytes(content)
        graph.nodes[node_id] = replace(
            graph.nodes[node_id],
            path=path,
            source_sha256=hashlib.sha256(content).hexdigest(),
        )

    runtime = build_runtime_graph(graph, project_root=project)

    assert runtime.get("child") is not None
    assert runtime.get("child").parent == "roadmap"  # type: ignore[union-attr]


@pytest.mark.parametrize("protocol", range(6))
def test_current_graph_pickle_round_trips_at_every_supported_protocol(
    tmp_path: Path,
    protocol: int,
) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    root = Node("root", "Root", roadmap / "README.md", ())
    child = Node("child", "Child", roadmap / "child.md", (), parent="root")
    graph = Graph(tmp_path / "blueprint", {"root": root, "child": child})
    graph.nodes["child"] = child
    assert graph.children("root") == ("child",)

    restored = pickle.loads(pickle.dumps(graph, protocol=protocol))

    assert restored == graph
    assert repr(restored) == repr(graph)
    assert restored.nodes.revision >= graph.nodes.revision
    assert restored.children("root") == ("child",)
    cached_revision = restored.nodes.revision
    restored.nodes["child"] = replace(restored.nodes["child"], parent=None)
    restored.nodes.__setstate__(cached_revision)
    assert restored.nodes.revision > cached_revision
    assert restored.children("root") == ()
    with pytest.raises(TypeError):
        restored._children_by_parent["root"] = ("child",)  # type: ignore[index]


def test_dependency_and_rollup_walks_handle_a_1200_node_chain(tmp_path: Path) -> None:
    graph = _chain_graph(tmp_path, 1_200)

    assert _find_cycles(graph.nodes) == []
    assert _find_rollup_cycles(graph.nodes) == []
    order = topological_order(graph)
    assert order[0] == "n1199"
    assert order[-1] == "n0000"
    assert len(derive(graph)) == 1_200


def test_graph_loading_uses_depth_bounded_descriptors(tmp_path: Path) -> None:
    resource = pytest.importorskip("resource")
    roadmap = tmp_path / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / "README.md").write_text("# Roadmap\n", encoding="utf-8")
    for index in range(80):
        chapter = roadmap / f"chapter-{index:03d}"
        chapter.mkdir()
        (chapter / "README.md").write_text(f"# Chapter {index}\n", encoding="utf-8")

    previous = resource.getrlimit(resource.RLIMIT_NOFILE)
    if previous[0] < 64:
        pytest.skip("descriptor limit is already below the regression threshold")
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, previous[1]))
        graph = load_graph(tmp_path / "blueprint")
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, previous)

    assert len(graph.nodes) == 81


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


def test_runtime_lookup_does_not_rescan_the_node_tuple(tmp_path: Path) -> None:
    project = tmp_path / "project"
    roadmap = project / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / "item.md").write_text("# Item\n", encoding="utf-8")
    runtime = load_runtime_graph(project)
    nodes = _CountingTuple(runtime.nodes)
    runtime = replace(runtime, nodes=nodes)
    scans_after_construction = nodes.iterations

    for _ in range(1_200):
        assert runtime.get("item") is nodes[0]
        assert runtime.get("missing") is None

    assert nodes.iterations == scans_after_construction


def test_runtime_lookup_cache_preserves_the_public_dataclass_contract(tmp_path: Path) -> None:
    project = tmp_path / "project"
    roadmap = project / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / "item.md").write_text("# Item\n", encoding="utf-8")
    runtime = load_runtime_graph(project)
    field_names = (
        "schema",
        "authority",
        "source_revision",
        "blueprint_path",
        "nodes",
        "article_count",
        "formalizable_count",
        "dispatchable_count",
        "dependency_count",
        "maximum_depth",
    )

    assert tuple(field.name for field in fields(RuntimeGraph)) == field_names
    assert set(asdict(runtime)) == set(field_names)
    assert "_nodes_by_id" not in repr(runtime)
    reconstructed = RuntimeGraph(*(getattr(runtime, name) for name in field_names))
    assert reconstructed == runtime
    assert repr(reconstructed) == repr(runtime)
    with pytest.raises(TypeError):
        runtime._nodes_by_id["replacement"] = runtime.nodes[0]  # type: ignore[index]


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


def test_runtime_uses_article_bytes_captured_during_graph_load(tmp_path: Path) -> None:
    project = tmp_path / "project"
    roadmap = project / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True)
    article = roadmap / "item.md"
    article.write_text("# Item\n\nOriginal.\n", encoding="utf-8")
    graph = load_graph(project / "blueprint")
    article.write_text("# Item\n\nChanged.\n", encoding="utf-8")

    runtime = build_runtime_graph(graph, project_root=project)

    assert runtime.source_revision == _source_revision(
        {"item": "roadmap/item.md"},
        {"item": b"# Item\n\nOriginal.\n"},
    )


def test_runtime_rejects_a_graph_node_without_a_source_digest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    roadmap = project / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / "item.md").write_text("# Item\n", encoding="utf-8")
    graph = load_graph(project / "blueprint")
    graph.nodes["item"] = replace(graph.nodes["item"], source_sha256=None)

    with pytest.raises(RuntimeProjectionError) as error:
        build_runtime_graph(graph, project_root=project)

    assert error.value.issues == ("item: article source digest is unavailable",)
