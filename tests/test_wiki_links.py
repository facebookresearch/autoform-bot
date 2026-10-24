"""Captured authored references are an optional layer, not formal graph state."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from autoform_cli import dag_viewer, graph_pages, render
from autoform_cli.graph import load_graph
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode
from autoform_cli.status import derive


def _view(*names: str, edges: tuple[ViewEdge, ...] = ()) -> GraphView:
    return GraphView(
        kind="full", title="Mathematics", edges=edges,
        nodes=tuple(
            ViewNode(name, name, "node", (name,), (("planned", 1),), status_key="planned")
            for name in names
        ),
    )


def _inputs(*names: str):
    paths = {name: f"roadmap/{name}.md" for name in names}
    sources = {name: f"# {name}\n\nA mathematical statement.\n".encode() for name in names}
    metadata = {name: {"route": f"../roadmap/{name}.html", "parent": None} for name in names}
    return paths, sources, metadata


def test_authored_inline_reference_and_encoded_fragment_links_keep_exact_index_order() -> None:
    view = _view("beta", "alpha", "gamma", edges=(ViewEdge("beta", "alpha", 2, 3),))
    paths, sources, metadata = _inputs("beta", "alpha", "gamma")
    paths["beta"] = "roadmap/B % café.md"
    sources["alpha"] = (
        b"# Alpha\n\nSee [B](B%20%25%20caf%C3%A9.md#theorem) and [again](B%20%25%20caf%C3%A9.md?q=1).\n"
        b"Compare [other][g] and [shortcut].\n\n[g]: gamma.md#proof\n[shortcut]: gamma.md\n"
    )
    sources["beta"] = b"# Beta\n\nSee [Alpha](alpha.md).\n"
    before = copy.deepcopy((view, paths, sources, metadata))

    links = dag_viewer.extract_wiki_links(view, captured_sources=sources, source_paths=paths, metadata=metadata)

    assert links == dag_viewer.WikiLinks(((0, 1), (1, 0), (1, 2)))
    assert (view, paths, sources, metadata) == before
    assert view.edges == (ViewEdge("beta", "alpha", 2, 3),)
    assert [node.status_counts for node in view.nodes] == [(("planned", 1),)] * 3


def test_code_comments_images_headings_frontmatter_and_formal_source_sections_are_not_references() -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    sources["a"] = b"""---
origin: '[Metadata](c.md)'
---
# [Heading](c.md)

`[Code](c.md)` and ![Image](c.md).
<!-- [Hidden](c.md) -->
    [Indented](c.md)

```markdown
[Fenced](c.md)
```

## Depends on
[Statement](c.md)
### Nested
[Still a dependency](c.md)
## Proof depends on
[Proof](c.md)
## Sources
[Provenance](c.md)
## Exposition
See [B](b.md).
"""

    assert dag_viewer.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ).references == ((0, 1),)


@pytest.mark.parametrize("formula", [
    "$[C](c.md)$", r"\([C](c.md)\)",
    "$$\n[C](c.md)\n$$", "\\[\n[C](c.md)\n\\]",
    "\\begin{align}\n[C](c.md)\n\\end{align}",
])
def test_literal_inline_and_display_tex_links_never_become_wiki_references(formula: str) -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    sources["a"] = f"# A\n\n{formula}\n\nSee [B](b.md).\n".encode()
    assert dag_viewer.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == dag_viewer.WikiLinks(((0, 1),))


@pytest.mark.parametrize("unsupported", [
    "<pre>[C](c.md)</pre>", '<div hidden>\n[C](c.md)\n</div>',
    '<a href="c.md">C</a>', "$$\n[C](c.md)", "\\[\n[C](c.md)",
    "```markdown\n[C](c.md)", "~~~\n[C](c.md)",
])
def test_unsupported_html_or_unclosed_display_math_is_explicitly_partial(
    unsupported: str, tmp_path: Path,
) -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    sources["a"] = f"# A\n\n{unsupported}\n\nSee [B](b.md).\n".encode()
    links = dag_viewer.extract_wiki_links(view, captured_sources=sources, source_paths=paths, metadata=metadata)
    assert links == dag_viewer.WikiLinks((), (0,))
    output = tmp_path / "wiki-links.json"
    dag_viewer.write_wiki_links(output, links, index_sha256="a" * 64)
    payload = json.loads(output.read_bytes())
    assert payload["availability"] == "partial" and payload["unavailable_sources"] == [0]
    assert "unavailable or unsupported" in payload["reason"]


@pytest.mark.parametrize("unsupported", [
    "<pre>[C](c.md)</pre>", "$$\n[C](c.md)", "```markdown\n[C](c.md)",
])
def test_valid_incoming_links_to_published_unsupported_articles_remain_backlinks(unsupported: str) -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    sources["a"] = b"# A\n\n[B](b.md).\n"
    sources["b"] = f"# B\n\n{unsupported}\n\n[C](c.md).\n".encode()
    assert dag_viewer.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == dag_viewer.WikiLinks(((0, 1),), (1,))


def test_roles_inherited_source_tracks_and_authored_placement_do_not_become_wiki_edges() -> None:
    names = ("a", "b", "scope", "child", "book", "book-child", "collection", "range")
    view = _view(*names)
    paths, sources, metadata = _inputs(*names)
    metadata["a"]["parent"] = metadata["b"]["parent"] = "scope"
    metadata["child"]["parent"] = "a"
    metadata["book"].update(navigation="source")
    metadata["book-child"]["parent"] = "book"
    metadata["collection"].update(navigation="collection")
    metadata["range"].update(navigation="transparent")
    sources["a"] = b"# A\n\n[Peer](b.md), [Parent](scope.md), [Child](child.md), [Self](a.md).\n"
    sources["b"] = b"# B\n\n[Book](book.md), [Track article](book-child.md), [Collection](collection.md), [Range](range.md).\n"
    sources["book-child"] = b"# Source article\n\n[A](a.md)\n"
    sources["scope"] = b"# Scope\n\n[A](a.md) and [B](b.md).\n"

    links = dag_viewer.extract_wiki_links(view, captured_sources=sources, source_paths=paths, metadata=metadata)

    assert links == dag_viewer.WikiLinks(((0, 1),))


@pytest.mark.parametrize("target", [
    "https://example.com/b.md", "//example.com/b.md", "/roadmap/b.md", "../../b.md",
    "../sources/b.md", ".hidden/b.md", "%2Ehidden/b.md", "%2Froadmap/b.md", "%5Cb.md",
    "missing.md", "b.html", "#b", "b.md%00", "%FF.md", "javascript:b.md",
])
def test_nonpublished_or_unsafe_targets_cannot_establish_references(target: str) -> None:
    view = _view("a", "b")
    paths, sources, metadata = _inputs("a", "b")
    sources["a"] = f"# A\n\n[Target]({target})\n".encode()
    assert dag_viewer.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ).references == ()


def test_missing_invalid_hidden_and_external_publication_captures_are_honestly_unavailable() -> None:
    names = ("a", "missing", "invalid", "hidden", "external", "unpublished")
    view = _view(*names)
    paths, sources, metadata = _inputs(*names)
    sources["a"] = b"# A\n\n[M](missing.md) [I](invalid.md) [H](hidden.md) [E](external.md) [U](unpublished.md)\n"
    del sources["missing"]
    sources["invalid"] = b"\xff"
    paths["hidden"] = "roadmap/.hidden/article.md"
    metadata["external"]["route"] = "https://example.com/article.html"
    del metadata["unpublished"]["route"]

    links = dag_viewer.extract_wiki_links(view, captured_sources=sources, source_paths=paths, metadata=metadata)

    assert links == dag_viewer.WikiLinks((), (1, 2, 3, 4, 5))


def test_extractor_uses_only_retained_bytes_not_live_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    view = _view("a", "b")
    paths, sources, metadata = _inputs("a", "b")
    (tmp_path / "a.md").write_text("# A\n\nLIVE_BODY_HAS_NO_REFERENCE\n", encoding="utf-8")
    sources["a"] = b"# A\n\nCaptured [B](b.md).\n"
    paths = {name: Path(path) for name, path in paths.items()}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the extractor must not reopen or resolve authored paths")

    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert dag_viewer.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == dag_viewer.WikiLinks(((0, 1),))


def test_no_capture_and_ambiguous_or_cyclic_sources_have_no_invented_fallback() -> None:
    view = _view("a", "b", "c", "d")
    absent = dag_viewer.extract_wiki_links(view)
    assert absent.references == () and absent.unavailable_sources == (0, 1, 2, 3)
    assert absent.reason and "not supplied" in absent.reason
    paths, sources, metadata = _inputs("a", "b", "c", "d")
    paths["b"] = paths["a"]
    metadata["c"]["parent"] = "d"
    metadata["d"]["parent"] = "c"
    sources["a"] = b"# A\n\n[B](b.md) [C](c.md)\n"
    assert dag_viewer.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == dag_viewer.WikiLinks((), (0, 1, 2, 3))


def test_optional_asset_binds_exact_index_bytes_and_leaves_dependencies_and_status_unchanged(tmp_path: Path) -> None:
    view = _view("b", "a", edges=(ViewEdge("b", "a", 2, 3),))
    paths, sources, metadata = _inputs("b", "a")
    sources["a"] = b"# A\n\n[B](b.md).\n"
    baseline, indexed, sidecar = (tmp_path / name for name in ("baseline.json", "index.json", "wiki-links.json"))
    arguments = dict(context_links={"a": "a.html", "b": "b.html"}, metadata=metadata, include_relations=True)
    dag_viewer.write_search_index(baseline, view, **arguments)
    digests: list[str] = []
    dag_viewer.write_search_index(indexed, view, **arguments, sha256_out=digests)
    dag_viewer.write_wiki_links(
        sidecar, dag_viewer.extract_wiki_links(view, captured_sources=sources, source_paths=paths, metadata=metadata),
        index_sha256=digests[0],
    )

    assert baseline.read_bytes() == indexed.read_bytes()
    assert indexed.read_bytes() == (
        b'{"nodes":[{"id":"b","kind":"node","route":"../roadmap/b.html","status":"planned","title":"b","url":"b.html"},'
        b'{"id":"a","kind":"node","route":"../roadmap/a.html","status":"planned","title":"a","url":"a.html"}],'
        b'"relations":[[0,1,2,3]],"schema":"autoform-dag-search/v1"}\n'
    )
    assert digests == [hashlib.sha256(indexed.read_bytes()).hexdigest()]
    assert indexed.read_bytes().endswith(b"\n")
    index = json.loads(indexed.read_bytes())
    assert index["relations"] == [[0, 1, 2, 3]]
    assert [node["status"] for node in index["nodes"]] == ["planned", "planned"]
    assert json.loads(sidecar.read_bytes()) == {
        "schema": "autoform-wiki-links/v1", "index_sha256": digests[0],
        "availability": "complete", "references": [[1, 0]], "unavailable_sources": [],
    }


def test_oversized_optional_layer_is_explicitly_omitted_not_truncated_or_index_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dag_viewer, "MAX_WIKI_LINK_BYTES", 500)
    names = tuple(f"n{index}" for index in range(120))
    view = _view(*names)
    paths, sources, metadata = _inputs(*names)
    sources["n0"] = ("# First\n\n" + " ".join(f"[N]({name}.md)" for name in names[1:])).encode()
    links = dag_viewer.extract_wiki_links(view, captured_sources=sources, source_paths=paths, metadata=metadata)
    assert links.reason and "exceeds 500 bytes" in links.reason and links.references == ()
    warnings: list[str] = []
    output = tmp_path / "wiki-links.json"
    dag_viewer.write_wiki_links(output, links, index_sha256="a" * 64, warnings_out=warnings)
    payload = json.loads(output.read_bytes())
    assert payload["availability"] == "unavailable" and payload["references"] == []
    assert payload["reason"] and warnings and output.stat().st_size < 500

    # The writer also protects callers supplying an already-derived layer.
    warnings.clear()
    dag_viewer.write_wiki_links(
        output, dag_viewer.WikiLinks(tuple((index, index + 1) for index in range(120))),
        index_sha256="b" * 64, warnings_out=warnings,
    )
    payload = json.loads(output.read_bytes())
    assert payload["availability"] == "unavailable" and payload["references"] == []
    assert "exceeds 500 bytes" in payload["reason"] and len(warnings) == 1


def _blueprint(tmp_path: Path) -> Path:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / "README.md").write_text("# Mathematics\n\n- [A](a.md)\n- [B](b.md)\n", encoding="utf-8")
    (roadmap / "a.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# A\n\nSee [B](b.md).\n\n## Depends on\n\nNo prerequisites.\n",
        encoding="utf-8",
    )
    (roadmap / "b.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# B\n\nA proposition.\n\n## Depends on\n\n- [A](a.md)\n",
        encoding="utf-8",
    )
    return blueprint


def test_graph_pages_opt_in_is_registered_and_no_capture_callers_keep_original_index(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    statuses = derive(graph)
    plain, captured = tmp_path / "plain", tmp_path / "captured"
    def node_links(_page, names):
        return {name: f"../roadmap/README.html#{name}" for name in names}
    plain_written = graph_pages.write_graph_pages(graph, statuses, plain, node_links=node_links, book_targets={})
    captured_sources = {node_id: node.path.read_bytes() for node_id, node in graph.nodes.items()}
    source_paths = {node_id: node.path.relative_to(blueprint) for node_id, node in graph.nodes.items()}
    captured_written = graph_pages.write_graph_pages(
        graph, statuses, captured, node_links=node_links, book_targets={},
        captured_sources=captured_sources, source_paths=source_paths,
    )

    assert not (plain / graph_pages.WIKI_LINKS).exists()
    assert all(path.suffix == ".md" for path in plain_written)
    assert captured / graph_pages.WIKI_LINKS in captured_written
    assert sum(path.suffix == ".md" for path in captured_written) == len(plain_written)
    assert sum(path != captured / graph_pages.WIKI_LINKS for path in captured_written) == len(plain_written)
    assert (plain / graph_pages.SEARCH_INDEX).read_bytes() == (captured / graph_pages.SEARCH_INDEX).read_bytes()
    assert graph_pages.WIKI_LINKS not in graph_pages.graph_page_paths(graph)
    assert graph_pages.WIKI_LINKS in graph_pages.graph_page_paths(graph, wiki_links=True)
    payload = json.loads((captured / graph_pages.WIKI_LINKS).read_bytes())
    index_bytes = (captured / graph_pages.SEARCH_INDEX).read_bytes()
    ids = [node["id"] for node in json.loads(index_bytes)["nodes"]]
    assert payload["index_sha256"] == hashlib.sha256(index_bytes).hexdigest()
    assert [[ids[source], ids[target]] for source, target in payload["references"]] == [["a", "b"]]


def test_optional_asset_collision_uses_existing_captured_output_policy(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    path = blueprint / graph_pages.WIKI_LINKS
    assert graph_pages.WIKI_LINKS in render._site_paths(graph)
    # Exact generated-file names are owned outputs, as for the existing index.
    assert render._in_the_way(blueprint, graph, {path: b"previous derived bytes"}) == []
    assert render._in_the_way(blueprint, graph, {(path / "authored.md"): b"authored bytes"}) == [
        "dependencies/wiki-links.json: is a directory, where autoform render writes a file; remove it",
    ]
