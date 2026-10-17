from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli import dag_viewer
from autoform_cli.graph import Graph, Node
from autoform_cli.graph_pages import _selected_statuses, write_graph_pages
from autoform_cli.graph_views import chapter_view, full_view, project_view, scope_view
from autoform_cli.status import derive


def _graph(tmp_path: Path) -> Graph:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    for group, title in (("a", "Foundations"), ("b", "Main chapter"), ("c", "Applications")):
        page = roadmap / group / "README.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(f"---\nkind: roadmap\n---\n\n# {title}\n", encoding="utf-8")

    def node(node_id: str, title: str, **kwargs) -> Node:
        return Node(
            id=node_id,
            title=title,
            path=roadmap / f"{node_id}.md",
            dependencies=kwargs.pop("dependencies", ()),
            **kwargs,
        )

    nodes = {
        "a/base": node(
            "a/base",
            "Base object",
            declaration="def",
            statement_formalized=True,
        ),
        "b/bridge": node(
            "b/bridge",
            "Bridge lemma",
            dependencies=("a/base",),
            statement_dependencies=("a/base",),
            declaration="lemma",
        ),
        "b/top": node(
            "b/top",
            "Main theorem",
            dependencies=("b/bridge", "a/base"),
            statement_dependencies=("b/bridge",),
            proof_dependencies=("a/base",),
            declaration="theorem",
        ),
        "c/use": node(
            "c/use",
            "Application",
            dependencies=("b/top",),
            statement_dependencies=("b/top",),
            declaration="theorem",
        ),
    }
    return Graph(blueprint_dir=blueprint, nodes=nodes)


def test_project_view_collapses_chapters_without_flattening_statuses(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    view = project_view(graph, derive(graph))

    assert view.kind == "project"
    assert [(node.id, node.title, node.members) for node in view.nodes] == [
        ("scope:a", "Foundations", ("a/base",)),
        ("scope:b", "Main chapter", ("b/bridge", "b/top")),
        ("scope:c", "Applications", ("c/use",)),
    ]
    main = view.nodes[1]
    assert main.status_counts == (("can_state", 1), ("planned", 1))
    assert main.internal_dependency_count == 1
    assert [(edge.source, edge.target, edge.statement_count, edge.proof_count) for edge in view.edges] == [
        ("scope:a", "scope:b", 1, 1),
        ("scope:b", "scope:c", 1, 0),
    ]


def test_root_articles_beside_a_chapter_count_only_their_own_links(tmp_path: Path) -> None:
    """The root scope holds the articles directly under roadmap/, not every relation below it.

    It took the root container's count, so a project whose one relation sits
    inside a chapter reported two internal links, and the root chapter's lead
    counted only the root's own articles although its map draws the chapter too.
    """
    roadmap = tmp_path / "blueprint/roadmap"

    def node(node_id: str, parent: str, *dependencies: str) -> Node:
        return Node(
            node_id,
            node_id.title(),
            roadmap / f"{node_id}.md",
            dependencies,
            statement_dependencies=dependencies,
            parent=parent,
            declaration="theorem",
        )

    graph = Graph(
        tmp_path / "blueprint",
        {
            "roadmap": Node("roadmap", "Roadmap", roadmap / "README.md", ()),
            "intro": node("intro", "roadmap"),
            "chapter": Node("chapter", "Chapter", roadmap / "chapter/README.md", (), parent="roadmap"),
            "chapter/a": node("chapter/a", "chapter"),
            "chapter/b": node("chapter/b", "chapter", "chapter/a"),
        },
    )
    statuses = derive(graph)
    project = project_view(graph, statuses)

    internal = {view_node.id: view_node.internal_dependency_count for view_node in project.nodes}
    assert internal == {"scope:roadmap": 0, "scope:chapter": 1}
    assert sum(internal.values()) + sum(edge.dependency_count for edge in project.edges) == graph.edge_count
    write_graph_pages(graph, statuses, tmp_path / "site", node_links=lambda page, ids: dict.fromkeys(ids, "book.html"))
    lead = (tmp_path / "site/dependencies/chapters/roadmap.md").read_text(encoding="utf-8")
    assert "This map covers 3 items in this chapter." in lead


def test_checked_catalog_has_a_neutral_inventory_presentation_status(tmp_path: Path) -> None:
    roadmap = tmp_path / "blueprint/roadmap"
    graph = Graph(
        tmp_path / "blueprint",
        {
            "result": Node(
                "result",
                "Result",
                roadmap / "result.md",
                (),
                declaration="theorem",
            ),
            "catalog": Node(
                "catalog",
                "Existing module",
                roadmap / "catalog.md",
                (),
                catalog="module",
                statement_formalized=True,
                proof_formalized=True,
            ),
        },
    )
    statuses = derive(graph)

    assert statuses["catalog"].key == "fully_proved"
    project = project_view(graph, statuses)
    assert project.nodes[0].status_counts == (
        ("can_state", 1),
        ("inventory_checked", 1),
    )
    assert project.nodes[0].status_key == "can_state"

    complete = full_view(graph, statuses)
    inventory = next(node for node in complete.nodes if node.id == "catalog")
    assert inventory.catalog == "module"
    assert inventory.status_counts == (("inventory_checked", 1),)
    assert inventory.status_key == "planned"


def test_status_rings_count_only_leaves_that_declare_something(tmp_path: Path) -> None:
    """Rings agree with the legend beside them.

    A prose leaf is not a formalization target, so it colours no ring. An
    unverified catalog is inventory, not work in its target state, so it fills
    a pending-inventory segment and leaves the scope's status alone.
    """
    roadmap = tmp_path / "blueprint/roadmap"

    def node(node_id: str, parent: str | None, **kwargs) -> Node:
        return Node(node_id, node_id.title(), roadmap / f"{node_id}.md", (), parent=parent, **kwargs)

    graph = Graph(
        tmp_path / "blueprint",
        {
            "roadmap": node("roadmap", None),
            "mixed": node("mixed", "roadmap"),
            "mixed/result": node("mixed/result", "mixed", declaration="theorem"),
            "mixed/note": node("mixed/note", "mixed"),
            "mixed/catalog": node("mixed/catalog", "mixed", catalog="module", statement_formalized=True),
            "prose": node("prose", "roadmap"),
            "prose/essay": node("prose/essay", "prose"),
        },
    )
    statuses = derive(graph)
    assert statuses["mixed/note"].key == "can_state"
    assert statuses["mixed/catalog"].key == "can_prove"

    project = project_view(graph, statuses)
    scopes = {view_node.id: view_node for view_node in project.nodes}
    assert scopes["scope:mixed"].status_counts == (("can_state", 1), ("inventory_pending", 1))
    assert scopes["scope:mixed"].status_key == "can_state"
    assert scopes["scope:prose"].status_counts == ()
    assert scopes["scope:prose"].status_key == "planned"
    legend = _selected_statuses(graph, statuses, project, {"roadmap", "mixed", "prose"})
    assert {node_id: status.key for node_id, status in legend.items()} == {"mixed/result": "can_state"}

    complete = {view_node.id: view_node for view_node in full_view(graph, statuses).nodes}
    assert complete["mixed"].status_counts == (("can_state", 1), ("inventory_pending", 1))
    assert complete["mixed/catalog"].status_counts == (("inventory_pending", 1),)
    assert complete["mixed/catalog"].status_key == "planned"
    assert complete["prose"].status_counts == ()
    assert complete["prose"].status_key == "planned"


def test_a_scope_header_counts_the_items_its_status_rows_list(tmp_path: Path) -> None:
    """The explorer heads a scope's status rows with "N items in this scope".

    N is the payload's member_count, so it is the sum of those rows: a prose
    leaf is in neither, and a checked catalog is one item in its inventory row.
    """
    roadmap = tmp_path / "blueprint/roadmap"

    def node(node_id: str, parent: str | None, **kwargs) -> Node:
        return Node(node_id, node_id.title(), roadmap / f"{node_id}.md", (), parent=parent, **kwargs)

    graph = Graph(
        tmp_path / "blueprint",
        {
            "roadmap": node("roadmap", None),
            "mixed": node("mixed", "roadmap"),
            "mixed/result": node("mixed/result", "mixed", declaration="theorem"),
            "mixed/essay": node("mixed/essay", "mixed"),
            "mixed/catalog": node(
                "mixed/catalog",
                "mixed",
                catalog="module",
                statement_formalized=True,
                proof_formalized=True,
            ),
        },
    )
    payload = tmp_path / "dependencies.json"
    dag_viewer.write_payload(payload, project_view(graph, derive(graph)), links={})
    scope = json.loads(payload.read_text(encoding="utf-8"))["nodes"][0]

    assert scope["id"] == "scope:mixed"
    assert scope["status_counts"] == [["can_state", 1], ["inventory_checked", 1]]
    assert scope["member_count"] == sum(count for _, count in scope["status_counts"]) == 2


def test_chapter_view_keeps_external_relations_as_boundaries(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    view = chapter_view(graph, derive(graph), "b")

    assert view.kind == "chapter"
    assert view.scope == "b"
    assert {node.id for node in view.nodes} == {
        "b/bridge",
        "b/top",
        "boundary:a",
        "boundary:c",
    }


@pytest.mark.parametrize(
    ("source", "target"),
    (("roadmap", "chapter/result"), ("chapter/result", "roadmap")),
)
def test_roadmap_root_dependencies_have_nodes_in_every_view(
    tmp_path: Path,
    source: str,
    target: str,
) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    nodes = {
        "roadmap": Node("roadmap", "Roadmap", roadmap / "README.md", (), parent=None),
        "chapter": Node(
            "chapter",
            "Chapter",
            roadmap / "chapter/README.md",
            (),
            parent="roadmap",
            depth=1,
        ),
        "chapter/result": Node(
            "chapter/result",
            "Result",
            roadmap / "chapter/result.md",
            (source,),
            statement_dependencies=(source,),
            parent="chapter",
            depth=2,
            declaration="theorem",
        ),
    }
    if target == "roadmap":
        nodes["roadmap"] = Node(
            "roadmap",
            "Roadmap",
            roadmap / "README.md",
            (source,),
            statement_dependencies=(source,),
            parent=None,
        )
        nodes["chapter/result"] = Node(
            "chapter/result",
            "Result",
            roadmap / "chapter/result.md",
            (),
            parent="chapter",
            depth=2,
            declaration="theorem",
        )
    graph = Graph(tmp_path / "blueprint", nodes)
    statuses = derive(graph)

    project = project_view(graph, statuses)
    assert {node.id for node in project.nodes} == {"scope:roadmap", "scope:chapter"}
    assert next(node for node in project.nodes if node.id == "scope:roadmap").members == ()
    assert {(edge.source, edge.target) for edge in project.edges} == {
        (
            "scope:roadmap" if source == "roadmap" else "scope:chapter",
            "scope:roadmap" if target == "roadmap" else "scope:chapter",
        )
    }
    assert {endpoint for edge in project.edges for endpoint in (edge.source, edge.target)} <= {
        node.id for node in project.nodes
    }

    chapter = chapter_view(graph, statuses, "chapter")
    boundary = next(node for node in chapter.nodes if node.id == "boundary:roadmap")
    assert boundary.members == ("roadmap",)
    assert {endpoint for edge in chapter.edges for endpoint in (edge.source, edge.target)} <= {
        node.id for node in chapter.nodes
    }

    complete = full_view(graph, statuses)
    assert set(complete.member_ids) == set(graph.nodes)
    assert {(edge.source, edge.target) for edge in complete.edges} == {(source, target)}
    chapter_node = next(node for node in complete.nodes if node.id == "chapter")
    assert chapter_node.kind == "scope"
    assert chapter_node.members == ("chapter",)
    assert chapter_node.status_key == statuses["chapter/result"].key


def test_scope_view_collapses_nested_articles_and_rolls_up_dependencies(tmp_path: Path) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    nodes = {
        "roadmap": Node("roadmap", "Book", roadmap / "README.md", (), parent=None),
        "chapter": Node("chapter", "Chapter", roadmap / "chapter/README.md", (), parent="roadmap", depth=1),
        "section": Node("section", "Section", roadmap / "chapter/section/README.md", (), parent="chapter", depth=2),
        "section/base": Node(
            "section/base",
            "Base",
            roadmap / "chapter/section/base.md",
            (),
            parent="section",
            depth=3,
            declaration="def",
            statement_formalized=True,
        ),
        "result": Node(
            "result",
            "Result",
            roadmap / "chapter/result.md",
            ("section/base",),
            statement_dependencies=("section/base",),
            parent="chapter",
            depth=2,
            declaration="theorem",
        ),
    }
    graph = Graph(tmp_path / "blueprint", nodes)
    view = scope_view(graph, derive(graph), "chapter")

    assert [(node.id, node.kind, node.members) for node in view.nodes] == [
        ("scope:section", "scope", ("section/base",)),
        ("result", "node", ("result",)),
    ]
    assert [(edge.source, edge.target) for edge in view.edges] == [("scope:section", "result")]


def test_full_view_preserves_every_fine_node_and_edge(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    view = full_view(graph, derive(graph))

    assert view.kind == "full"
    assert set(view.member_ids) == set(graph.nodes)
    assert sum(edge.dependency_count for edge in view.edges) == graph.edge_count
