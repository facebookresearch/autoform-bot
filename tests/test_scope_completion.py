from __future__ import annotations

from pathlib import Path

from autoform_cli.graph import Graph, Node
from autoform_cli.graph_views import full_view, project_view, scope_view
from autoform_cli.mermaid import classdef_lines, render_diagram, render_view_diagram
from autoform_cli.status import STATES, derive

GREEN = next(state for state in STATES if state.key == "fully_proved")


def _graph(tmp_path: Path) -> Graph:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"

    def node(node_id: str, title: str, parent: str | None, **kwargs) -> Node:
        return Node(id=node_id, title=title, path=roadmap / f"{node_id}.md", dependencies=(), parent=parent, **kwargs)

    proved = {"declaration": "theorem", "statement_formalized": True, "proof_formalized": True}
    nodes = {
        "roadmap": node("roadmap", "Roadmap", None),
        "done": node("done", "Finished chapter", "roadmap"),
        "done/sec": node("done/sec", "Finished section", "done"),
        "done/sec/a": node("done/sec/a", "Proved result", "done/sec", **proved),
        # A leaf that declares nothing (a note or a phantom gate) is not a target.
        "done/note": node("done/note", "Note", "done"),
        "open": node("open", "Open chapter", "roadmap"),
        "open/b": node("open/b", "Proved result", "open", **proved),
        "open/c": node("open/c", "Stated result", "open", declaration="theorem", statement_formalized=True),
    }
    return Graph(blueprint_dir=blueprint, nodes=nodes)


def test_scope_is_complete_only_when_every_target_beneath_it_is_fully_proved(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    statuses = derive(graph)

    project = {node.id: node for node in project_view(graph, statuses).nodes}
    assert project["scope:done"].complete
    assert project["scope:done"].item_count == 1
    assert project["scope:done"].members == ("done/sec/a",)
    assert not project["scope:open"].complete
    assert project["scope:open"].item_count == 2

    nested = {node.id: node for node in scope_view(graph, statuses, "done").nodes}
    assert nested["scope:done/sec"].complete


def test_node_level_views_draw_containers_as_scopes(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    statuses = derive(graph)

    nodes = {node.id: node for node in full_view(graph, statuses).nodes}
    assert nodes["done"].kind == "scope"
    assert nodes["done"].complete
    assert nodes["done"].members == ("done",)
    assert nodes["done"].status_counts == (("fully_proved", 1),)
    assert nodes["open"].kind == "scope" and not nodes["open"].complete
    assert nodes["done/sec/a"].kind == "node"
    assert nodes["done/note"].kind == "node"


def test_complete_scopes_render_in_the_fully_proved_palette(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    statuses = derive(graph)

    diagram = render_view_diagram(project_view(graph, statuses), links={})
    assert 'n0["Finished chapter<br/><small>1 item · 1 fully proved</small>"]:::scope_complete' in diagram
    assert ":::scope\n" in diagram
    assert f"classDef scope_complete fill:{GREEN.fill},stroke:{GREEN.stroke}" in diagram
    dark = "\n".join(classdef_lines(dark=True))
    assert f"classDef scope_complete fill:{GREEN.dark_fill},stroke:{GREEN.dark_stroke}" in dark

    vault = render_diagram(graph, statuses, tmp_path / "blueprint" / "dependencies.md")
    assert '["Finished chapter"]:::scope_complete' in vault
    assert '["Open chapter"]:::scope' in vault
    assert '"Finished chapter — complete"' in vault
    assert "Roadmap — ready to state" not in vault
