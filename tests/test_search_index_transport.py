"""Compact search transport preserves the authored graph, not a second graph."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli.dag_viewer import write_search_index
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode


def _view(ids: tuple[str, ...]) -> GraphView:
    return GraphView(
        kind="full",
        title="Transport fixture",
        nodes=tuple(
            ViewNode(
                id=node_id, title=f"Result {position}", kind="node", members=(node_id,),
                status_counts=(("fully_proved", 1),), lean=f"Project.result{position}",
                summary="The complete searchable statement uses $V$ over ℝ.",
            )
            for position, node_id in enumerate(ids)
        ),
        edges=(ViewEdge(ids[0], ids[1], 3, 0), ViewEdge(ids[1], ids[0], 0, 11), ViewEdge(ids[0], ids[0], 2, 7)),
    )


def _decoded(payload: dict) -> list[list[str | int]]:
    return [[payload["nodes"][source]["id"], payload["nodes"][target]["id"], statement, proof]
            for source, target, statement, proof in payload["relations"]]


def test_index_offsets_preserve_node_order_metadata_and_every_typed_edge(tmp_path: Path) -> None:
    ids = ("z/topic/résultat", "a/topic/lemma", "unused/topic")
    view = _view(ids)
    links = {node_id: f"scopes/{position}.html" for position, node_id in enumerate(ids)}
    metadata = {node_id: {"route": f"../roadmap/topic/index.html#result-{position}", "scope": "topic",
                         "data_url": "scopes/topic.json", "parent": "topic", "area": "Analysis", "result": True}
                for position, node_id in enumerate(ids)}
    default = write_search_index(tmp_path / "default.json", view, context_links=links, metadata=metadata)
    compact = write_search_index(tmp_path / "compact.json", view, context_links=links, metadata=metadata, include_relations=True)
    baseline = json.loads(default.read_text(encoding="utf-8"))
    payload = json.loads(compact.read_text(encoding="utf-8"))

    assert "relations" not in baseline
    assert {key: value for key, value in payload.items() if key != "relations"} == baseline
    assert [node["id"] for node in payload["nodes"]] == list(ids)
    assert payload["relations"] == [[0, 1, 3, 0], [1, 0, 0, 11], [0, 0, 2, 7]]
    assert _decoded(payload) == [[edge.source, edge.target, edge.statement_count, edge.proof_count] for edge in view.edges]
    assert all(type(endpoint) is int for relation in payload["relations"] for endpoint in relation[:2])
    assert compact.read_bytes().endswith(b"\n")
    again = write_search_index(tmp_path / "again.json", view, context_links=links, metadata=metadata, include_relations=True)
    assert again.read_bytes() == compact.read_bytes()


def test_long_article_identifiers_are_not_repeated_in_dependency_rows(tmp_path: Path) -> None:
    prefix = "/".join(f"authored-section-{position}" for position in range(30))
    ids = (f"{prefix}/first", f"{prefix}/second", f"{prefix}/unused")
    view = _view(ids)
    path = write_search_index(tmp_path / "index.json", view, context_links={}, include_relations=True)
    payload = json.loads(path.read_text(encoding="utf-8"))
    strings = {**payload, "relations": _decoded(payload)}
    previous = (json.dumps(strings, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()

    assert len(path.read_bytes()) < len(previous) / 2
    assert _decoded(payload) == [[edge.source, edge.target, edge.statement_count, edge.proof_count] for edge in view.edges]
    assert payload["nodes"][2]["id"] == ids[2]  # An isolate is not discarded to save space.


def test_an_unknown_dependency_endpoint_is_not_encoded_as_an_unchecked_offset(tmp_path: Path) -> None:
    view = GraphView(kind="full", title="Invalid", nodes=_view(("a", "b", "c")).nodes, edges=(ViewEdge("absent", "a", 1, 0),))
    with pytest.raises(KeyError, match="absent"):
        write_search_index(tmp_path / "index.json", view, context_links={}, include_relations=True)
    assert not (tmp_path / "index.json").exists()
