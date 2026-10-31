"""Search-index capacity: an over-budget index leaves out summaries; every other field and the source data survive."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli import dag_viewer, graph_pages, status
from autoform_cli.graph import Graph, Node
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode


def _view(summary: str = "The identity holds.") -> GraphView:
    return GraphView("full", "Index budget", (
        ViewNode("a", "Égalité &amp; $x=x$", "node", ("a",), (("planned", 1),),
                 status_key="planned", summary=summary, lean="Demo.identity"),
        ViewNode("b", "Base", "node", ("b",), (("fully_proved", 1),), status_key="fully_proved"),
    ), (ViewEdge("b", "a", 2, 3),))


def _write(path: Path, view: GraphView, *, warnings: list[str] | None = None, digests: list[str] | None = None) -> Path:
    return dag_viewer.write_search_index(
        path, view, context_links={"a": "a.html", "b": "b.html"},
        metadata={"a": {"parent": "b", "scope": "b", "result": True}, "b": {"result": False}},
        include_relations=True, warnings_out=warnings, sha256_out=digests,
    )


@pytest.mark.parametrize(("size", "warns"), [(949, False), (950, True), (1000, True), (1001, True)])
def test_budget_boundary_warns_without_refusal_truncation_or_wire_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int, warns: bool,
) -> None:
    view = _view()
    initial = _write(tmp_path / "initial.json", view).read_bytes()
    assert len(initial) < size
    assert view.nodes[0].summary is not None
    summary = view.nodes[0].summary + "x" * (size - len(initial))
    view = replace(view, nodes=(replace(view.nodes[0], summary=summary), view.nodes[1]))
    baseline = _write(tmp_path / "baseline.json", view).read_bytes()
    assert len(baseline) == size
    # Scale only the private diagnostic capacity for tiny exact boundary cases.
    # Production and the client keep their 64 MiB limit.
    monkeypatch.setattr(dag_viewer, "_SEARCH_INDEX_BYTE_LIMIT", 1000)
    warnings, digests = [], []
    emitted = _write(tmp_path / "warned.json", view, warnings=warnings, digests=digests).read_bytes()
    assert emitted == baseline and digests == [hashlib.sha256(emitted).hexdigest()]
    assert len(warnings) == int(warns)
    if warns:
        assert f"{size:,} bytes" in warnings[0] and "1,000-byte" in warnings[0]
        assert "browser" in warnings[0] and "limit" in warnings[0]
    payload = json.loads(emitted)
    assert payload["schema"] == dag_viewer.SEARCH_SCHEMA
    assert [node["id"] for node in payload["nodes"]] == ["a", "b"]
    assert [node["status"] for node in payload["nodes"]] == ["planned", "fully_proved"]
    assert payload["relations"] == [[1, 0, 2, 3]] and payload["nodes"][0]["parent"] == 1
    assert payload["nodes"][0]["summary"] == summary and payload["nodes"][0]["lean"] == "Demo.identity"
    assert payload["nodes"][0]["reading_title"] == dag_viewer._reading_label(view.nodes[0].title)
    assert view.edges == (ViewEdge("b", "a", 2, 3),) and view.nodes[0].summary == summary


def test_warning_counts_utf8_bytes_once_without_rereading_source_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _view()
    baseline = _write(tmp_path / "baseline.json", view).read_bytes()
    assert len(baseline) > len(baseline.decode("utf-8"))
    monkeypatch.setattr(dag_viewer, "_SEARCH_INDEX_BYTE_LIMIT", len(baseline))
    original_read = Path.read_bytes

    def forbidden_read(*args: object, **kwargs: object) -> None:
        pytest.fail("Search-index publication reread a pathname for its byte evidence")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    monkeypatch.setattr(Path, "read_text", forbidden_read)
    warnings, digests = ["earlier diagnostic"], []
    target = _write(tmp_path / "captured.json", view, warnings=warnings, digests=digests)
    assert original_read(target) == baseline
    assert warnings[0] == "earlier diagnostic" and len(warnings) == 2
    assert f"{len(baseline):,} bytes" in warnings[1] and "100.0%" in warnings[1]
    assert digests == [hashlib.sha256(baseline).hexdigest()]


def test_unsuccessful_write_does_not_claim_a_published_budget_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dag_viewer, "_SEARCH_INDEX_BYTE_LIMIT", 1)

    def failed_write(*args: object, **kwargs: object) -> None:
        raise OSError("controlled index write failure")

    monkeypatch.setattr(Path, "write_text", failed_write)
    warnings = ["earlier diagnostic"]
    with pytest.raises(OSError, match="controlled index write failure"):
        _write(tmp_path / "failed.json", _view(), warnings=warnings)
    assert warnings == ["earlier diagnostic"]


def test_graph_publication_forwards_index_warning_without_changing_its_manifest_or_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = Graph(tmp_path / "blueprint", {
        "a": Node("a", "Identity $x=x$", tmp_path / "blueprint/roadmap/a.md", (), declaration="theorem"),
        "b": Node("b", "Consequence", tmp_path / "blueprint/roadmap/b.md", ("a",),
                  statement_dependencies=("a",), declaration="theorem"),
    })
    states = status.derive(graph)
    before = (dict(graph.nodes), dict(states))

    def links(page: Path, ids: Iterable[str]) -> dict[str, str]:
        return {node_id: "book.html#" + node_id for node_id in ids}

    quiet, warned = tmp_path / "quiet", tmp_path / "warned"
    baseline = graph_pages.write_graph_pages(graph, states, quiet, node_links=links)
    monkeypatch.setattr(dag_viewer, "_SEARCH_INDEX_BYTE_LIMIT", 1)
    warnings: list[str] = []
    produced = graph_pages.write_graph_pages(graph, states, warned, node_links=links, warnings_out=warnings)
    assert warnings and len(warnings) == 1 and "search index" in warnings[0]
    assert [path.relative_to(quiet) for path in baseline] == [path.relative_to(warned) for path in produced]
    for path in baseline:
        assert path.read_bytes() == (warned / path.relative_to(quiet)).read_bytes()
    assert (quiet / graph_pages.SEARCH_INDEX).read_bytes() == (warned / graph_pages.SEARCH_INDEX).read_bytes()
    assert (graph.nodes, states) == before


def test_production_limits_match_the_client_default_and_leave_summaries_room() -> None:
    assert dag_viewer._SEARCH_INDEX_BYTE_LIMIT == 64 * 1024 * 1024
    assert dag_viewer._SEARCH_INDEX_SUMMARY_BUDGET == 32 * 1024 * 1024
    source = (Path(dag_viewer.__file__).parent / "assets" / "blueprint-navigation.js").read_text(encoding="utf-8")
    # The warning measures against the limit the browser actually applies to a JSON fetch.
    assert "bounded(options.jsonLimit, 64 * 1024 * 1024, 1024, 64 * 1024 * 1024)" in source


@pytest.mark.parametrize("collect", [True, False])
def test_an_index_over_the_summary_budget_is_written_without_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collect: bool,
) -> None:
    view = _view()
    full = _write(tmp_path / "full.json", view).read_bytes()
    monkeypatch.setattr(dag_viewer, "_SEARCH_INDEX_SUMMARY_BUDGET", len(full) - 1)
    warnings: list[str] | None = ["earlier diagnostic"] if collect else None
    digests: list[str] = ["earlier digest"]
    emitted = _write(tmp_path / "lean.json", view, warnings=warnings, digests=digests).read_bytes()
    payload = json.loads(emitted)
    assert all("summary" not in node for node in payload["nodes"])
    expected = json.loads(full)
    for node in expected["nodes"]:
        node.pop("summary", None)
    # Titles, Lean names, statuses, containment and relations stay exactly as they were.
    assert payload == expected and len(emitted) < len(full)
    assert digests == ["earlier digest", hashlib.sha256(emitted).hexdigest()]
    if warnings is not None:
        assert warnings[0] == "earlier diagnostic" and len(warnings) == 2
        assert "leaves out statement summaries" in warnings[1] and f"{len(full):,} bytes" in warnings[1]
        assert f"{len(full) - 1:,}-byte budget" in warnings[1] and "titles and Lean names" in warnings[1]
    assert view.nodes[0].summary == "The identity holds."


def test_an_index_at_the_summary_budget_keeps_its_summaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    view = _view()
    full = _write(tmp_path / "full.json", view).read_bytes()
    monkeypatch.setattr(dag_viewer, "_SEARCH_INDEX_SUMMARY_BUDGET", len(full))
    warnings: list[str] = []
    assert _write(tmp_path / "kept.json", view, warnings=warnings).read_bytes() == full
    assert warnings == []
