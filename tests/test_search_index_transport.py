"""Compact search transport preserves the authored graph, not a second graph."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
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
    parents = (ids[1], ids[0], ids[2])  # Forward reference, offset zero, and a self-parent.
    metadata = {node_id: {"route": f"../roadmap/topic/index.html#result-{position}", "scope": "topic",
                         "data_url": "scopes/topic.json", "parent": parents[position], "area": "Analysis", "result": True}
                for position, node_id in enumerate(ids)}
    original_view, original_links, original_metadata = deepcopy((view, links, metadata))
    digests: list[str] = []
    default = write_search_index(tmp_path / "default.json", view, context_links=links, metadata=metadata)
    compact = write_search_index(tmp_path / "compact.json", view, context_links=links, metadata=metadata,
                                 include_relations=True, sha256_out=digests)
    baseline = json.loads(default.read_text(encoding="utf-8"))
    payload = json.loads(compact.read_text(encoding="utf-8"))

    assert "relations" not in baseline
    assert {key: value for key, value in payload.items() if key != "relations"} == baseline
    expected_nodes = [
        {"id": node_id, "kind": "node", "status": "fully_proved", "title": f"Result {position}",
         "url": links[node_id], "lean": f"Project.result{position}",
         "summary": "The complete searchable statement uses $V$ over ℝ.",
         "route": f"../roadmap/topic/index.html#result-{position}", "scope": "topic",
         "data_url": "scopes/topic.json", "parent": (1, 0, 2)[position], "area": "Analysis", "result": True}
        for position, node_id in enumerate(ids)
    ]
    assert payload["schema"] == "autoform-dag-search/v1"
    assert payload["nodes"] == expected_nodes
    assert all(type(node["parent"]) is int for node in payload["nodes"])
    logical_nodes = [{**node, "parent": parents[position]} for position, node in enumerate(expected_nodes)]
    assert [{**node, "parent": payload["nodes"][node["parent"]]["id"]} for node in payload["nodes"]] == logical_nodes
    assert [node["id"] for node in payload["nodes"]] == list(ids)
    assert payload["relations"] == [[0, 1, 3, 0], [1, 0, 0, 11], [0, 0, 2, 7]]
    assert _decoded(payload) == [[edge.source, edge.target, edge.statement_count, edge.proof_count] for edge in view.edges]
    assert all(type(endpoint) is int for relation in payload["relations"] for endpoint in relation[:2])
    assert compact.read_bytes().endswith(b"\n")
    assert digests == [sha256(compact.read_bytes()).hexdigest()]
    expanded = {**payload, "nodes": logical_nodes}
    expanded_bytes = (json.dumps(expanded, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    expected_savings = sum(len(json.dumps(parent, ensure_ascii=False).encode()) - len(str(offset))
                           for parent, offset in zip(parents, (1, 0, 2)))
    assert len(expanded_bytes) - len(compact.read_bytes()) == expected_savings > 0
    again = write_search_index(tmp_path / "again.json", view, context_links=links, metadata=metadata, include_relations=True)
    assert again.read_bytes() == compact.read_bytes()
    assert (view, links, metadata) == (original_view, original_links, original_metadata)


@pytest.mark.parametrize("include_relations", [False, True])
@pytest.mark.parametrize(("details", "expected_details"), [
    pytest.param({"scope": "roadmap", "parent": "first"}, {"scope": "roadmap", "parent": 0}, id="offset-zero"),
    pytest.param({"scope": "roadmap", "parent": "0"}, {"scope": "roadmap", "parent": 1}, id="canonical-zero-string"),
    pytest.param({"scope": "0", "parent": "0"}, {"scope": "0", "parent": 1}, id="parent-equals-known-scope"),
    pytest.param({"scope": "roadmap", "parent": "last"}, {"scope": "roadmap", "parent": 2}, id="forward-reference"),
    pytest.param({"scope": "first", "parent": "first"}, {"scope": "first", "parent": 0}, id="self-scope"),
    pytest.param({"scope": "first"}, {"scope": "first"}, id="self-scope-no-parent"),
    pytest.param({"scope": "roadmap", "parent": "roadmap"},
                 {"scope": "roadmap", "parent": "roadmap"}, id="parent-equals-unknown-scope"),
    pytest.param({"scope": "roadmap"}, {"scope": "roadmap"}, id="no-parent-unknown-containment"),
    pytest.param({"parent": "unpublished"}, {"parent": "unpublished"}, id="unknown-parent-no-scope"),
    pytest.param({"parent": "9"}, {"parent": "9"}, id="unknown-numeric-looking-string"),
    # None metadata values were already omitted; compaction must not change that.
    pytest.param({"scope": "0", "parent": None}, {"scope": "0"}, id="none-parent-unchanged"),
    pytest.param({"scope": None, "parent": "0"}, {"parent": 1}, id="none-scope-known-parent"),
    pytest.param({"scope": 7, "parent": 7}, {"scope": 7, "parent": 7}, id="nonstring-parent"),
    pytest.param({"scope": "roadmap", "parent": False}, {"scope": "roadmap", "parent": False}, id="boolean-parent"),
    pytest.param({"scope": "roadmap", "parent": ["last"]},
                 {"scope": "roadmap", "parent": ["last"]}, id="unhashable-nonstring-parent"),
    pytest.param({"scope": "", "parent": ""}, {"scope": "", "parent": ""}, id="empty-string-parent"),
])
def test_parent_offsets_preserve_canonical_identity_and_legacy_metadata(
    tmp_path: Path, include_relations: bool, details: dict[str, object], expected_details: dict[str, object],
) -> None:
    ids = ("first", "0", "last")
    view = _view(ids)
    metadata = {ids[0]: details}
    original_view, original_metadata = deepcopy((view, metadata))
    baseline_path = write_search_index(tmp_path / "baseline.json", view, context_links={},
                                       include_relations=include_relations)
    path = write_search_index(tmp_path / "index.json", view, context_links={}, metadata=metadata,
                              include_relations=include_relations)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["nodes"][0] == {**baseline["nodes"][0], **expected_details}
    if "parent" in expected_details:
        assert type(payload["nodes"][0]["parent"]) is type(expected_details["parent"])
    assert payload["nodes"][1:] == baseline["nodes"][1:]
    assert {key: value for key, value in payload.items() if key != "nodes"} == {
        key: value for key, value in baseline.items() if key != "nodes"
    }
    if include_relations:
        assert _decoded(payload) == [[edge.source, edge.target, edge.statement_count, edge.proof_count]
                                     for edge in view.edges]
    assert (view, metadata) == (original_view, original_metadata)


@pytest.mark.parametrize("metadata", [None, {}, {"not-in-view": {"scope": "topic", "parent": "topic"}}],
                         ids=["default", "empty", "unrelated"])
def test_default_metadata_does_not_infer_a_parent_or_scope(
    tmp_path: Path, metadata: dict[str, dict[str, object]] | None,
) -> None:
    ids = ("z/topic/résultat", "a/topic/lemma", "unused/topic")
    view = _view(ids)
    options = {} if metadata is None else {"metadata": metadata}
    original_view, original_metadata = deepcopy((view, metadata))
    path = write_search_index(tmp_path / "index.json", view, context_links={}, include_relations=True, **options)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["nodes"] == [
        {"id": node_id, "kind": "node", "status": "fully_proved", "title": f"Result {position}",
         "url": None, "lean": f"Project.result{position}",
         "summary": "The complete searchable statement uses $V$ over ℝ."}
        for position, node_id in enumerate(ids)
    ]
    assert _decoded(payload) == [[edge.source, edge.target, edge.statement_count, edge.proof_count] for edge in view.edges]
    assert (view, metadata) == (original_view, original_metadata)


def test_parent_offsets_keep_full_summary_math_label_and_authored_fields(tmp_path: Path) -> None:
    ids = ("z/topic/résultat", "a/topic/lemma", "unused/topic")
    original = _view(ids)
    title = "Groups $Z/n$ and `x<y`"
    summary = ("The complete searchable statement uses $V$ over ℝ.\n" * 512) + "FINAL_SENTINEL: ∀ ε > 0."
    view = replace(original, scope="topic", summary="Authored view summary", nodes=(
        replace(original.nodes[0], title=title, summary=summary, status_key="planned"),
        replace(original.nodes[1], catalog="module"), original.nodes[2],
    ))
    links = {node_id: f"scopes/{position}.html" for position, node_id in enumerate(ids)}
    parents = (ids[1], "unindexed/authored-parent", ids[2])
    metadata = {node_id: {"route": f"../roadmap/topic/index.html?edition=1#result-{position}", "scope": "topic",
                         "parent": parents[position], "data_url": "scopes/topic.json", "area": "Authored Analysis",
                         "result": position != 1, "navigation": "transparent" if position == 1 else "normal"}
                for position, node_id in enumerate(ids)}
    original_view, original_links, original_metadata = deepcopy((view, links, metadata))
    path = write_search_index(tmp_path / "index.json", view, context_links=links, metadata=metadata,
                              include_relations=True)
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_nodes = []
    for position, node in enumerate(view.nodes):
        expected = {"id": node.id, "kind": "node", "status": "planned" if position == 0 else "fully_proved",
                    "title": node.title, "url": links[node.id], "lean": node.lean, "summary": node.summary,
                    "route": metadata[node.id]["route"], "scope": "topic", "data_url": "scopes/topic.json",
                    "parent": (1, "unindexed/authored-parent", 2)[position],
                    "area": "Authored Analysis", "result": position != 1,
                    "navigation": "transparent" if position == 1 else "normal"}
        if position == 0:
            expected["reading_title"] = {"format": "inline-parts-v1", "source": title,
                                         "parts": [["text", "Groups "], ["math", r"\(Z/n\)"],
                                                   ["text", " and "], ["code", "x<y"]]}
        if position == 1:
            expected["catalog"] = "module"
        expected_nodes.append(expected)

    assert len(summary) > 16_384  # The late searchable text must not be truncated to a label-sized limit.
    assert payload["nodes"] == expected_nodes
    logical_nodes = [{**node, "parent": payload["nodes"][node["parent"]]["id"]}
                     if type(node["parent"]) is int else node for node in payload["nodes"]]
    assert logical_nodes == [{**node, "parent": parents[position]} for position, node in enumerate(expected_nodes)]
    assert payload["relations"] == [[0, 1, 3, 0], [1, 0, 0, 11], [0, 0, 2, 7]]
    assert _decoded(payload) == [[edge.source, edge.target, edge.statement_count, edge.proof_count] for edge in view.edges]
    assert (view, links, metadata) == (original_view, original_links, original_metadata)


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
