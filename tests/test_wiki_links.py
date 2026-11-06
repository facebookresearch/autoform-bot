"""Captured authored references are an optional layer, not formal graph state."""

from __future__ import annotations

import copy
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import html5lib

from autoform_cli import graph_pages, markdown, render, site_index
from autoform_cli.graph import load_graph
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode
from autoform_cli.status import derive
from autoform_cli.publishable import publishable_article


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


def _converted_links(body: str) -> list[str]:
    converted = markdown.site_converter().convert(body)
    return [link.get("href") for link in ET.fromstring("<div>" + converted + "</div>").iter("a")]


@pytest.mark.parametrize(("opening", "closing"), [("`", "`"), ("$", "$"), (r"\(", r"\)")])
def test_unmatched_inline_delimiters_in_different_paragraphs_keep_real_site_links(opening: str, closing: str) -> None:
    view = _view("a", "b")
    paths, sources, metadata = _inputs("a", "b")
    body = f"# A\n\n{opening}start\n\n[B](b.md)\n\nend{closing}\n"
    assert _converted_links(body) == ["b.md"]
    sources["a"] = body.encode()
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 1),))


@pytest.mark.parametrize(("opening", "closing"), [("`", "`"), ("$", "$"), (r"\(", r"\)")])
def test_valid_multiline_inline_spans_within_a_paragraph_keep_the_site_code_and_math_masks(opening: str, closing: str) -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    body = f"# A\n\n{opening}start\n[B](b.md)\nend{closing}\n\n[C](c.md)\n"
    assert _converted_links(body) == ["c.md"]
    sources["a"] = body.encode()
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 2),))


@pytest.mark.parametrize("span", [r"\`left [B](b.md) right\`", r"\`literal\` before [B](b.md) after \`literal\`"])
def test_escaped_inline_ticks_are_literal_and_keep_real_site_links(span: str) -> None:
    view = _view("a", "b")
    paths, sources, metadata = _inputs("a", "b")
    body = "# A\n\n" + span + "\n"
    assert _converted_links(body) == ["b.md"]
    sources["a"] = body.encode()
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 1),))


def test_even_backslash_prefix_leaves_a_real_code_span_masked_as_the_site_reads_it() -> None:
    view = _view("a", "b")
    paths, sources, metadata = _inputs("a", "b")
    body = "# A\n\n" + r"\\`left [B](b.md) right`" + "\n"
    assert _converted_links(body) == []
    sources["a"] = body.encode()
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(())


@pytest.mark.parametrize("heading", ["##Sources", "##Depends on", "##Proof depends on"])
def test_actual_hash_headings_without_spaces_still_exclude_provenance_and_formal_references(heading: str) -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    body = f"# A\n\n{heading}\n\n[C](c.md)\n\n## Exposition\n\n[B](b.md)\n"
    assert _converted_links(body) == ["c.md", "b.md"]
    assert "<h2" in markdown.site_converter().convert(body)
    sources["a"] = body.encode()
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 1),))


@pytest.mark.parametrize("section", ["Sources", "Depends on", "Proof depends on"])
def test_a_later_h1_stays_in_the_provenance_section_above_it(section: str) -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    # The loader reads the link below the later H1 as a dependency or a source, as written.
    body = f"# A\n\n## {section}\n\n# Aside\n\n[C](c.md)\n\n## Exposition\n\n[B](b.md)\n"
    sources["a"] = body.encode()
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 1),))


@pytest.mark.parametrize("heading", ["References\n==========", "#References", "# References"])
def test_a_later_h1_named_as_a_provenance_section_starts_it(heading: str) -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    # The page shows the links below it as references; the first two spellings load on main too.
    sources["a"] = f"# A\n\nSee [B](b.md).\n\n{heading}\n\n- [C](c.md)\n".encode()
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 1),))


@pytest.mark.parametrize(
    ("body", "edges"),
    [
        ("# A\n\nSee [B](b.md).\n\n# Aside\n\n[C](c.md)\n", ((0, 1), (0, 2))),
        # The page shows B under Notes, not References, and the loader reads it in no section.
        ("# A\n\nText.\n\nReferences\n==========\n\n- [C](c.md)\n\nNotes\n=====\n\nSee [B](b.md).\n", ((0, 1),)),
    ],
    ids=["outside-a-section", "after-a-later-references"],
)
def test_an_unnamed_later_h1_keeps_the_section_the_title_or_an_h2_opened(
    body: str, edges: tuple[tuple[int, int], ...]
) -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    sources["a"] = body.encode()
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(edges)


def test_a_title_below_a_provenance_section_ends_it() -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    # The first H1 is the title wherever it stands, and it ends the section above it, as in the graph.
    sources["a"] = b"## Sources\n\n- [C](c.md)\n\n# A\n\nSee [B](b.md).\n"
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 1),))


@pytest.mark.parametrize("heading", [
    "Sources\n-------", "**Sources**\n-------", "Depends on\n----------", "Proof depends on\n----------------",
    "## **Sources**", "## References", "**References**\n----------", "## Bibliography", "Bibliography\n------------",
])
def test_published_section_semantics_keep_setext_and_formatted_provenance_out_of_wiki_links(heading: str) -> None:
    view = _view("a", "b", "c", edges=(ViewEdge("a", "b", 2, 3),))
    paths, sources, metadata = _inputs("a", "b", "c")
    body = f"# A\n\n{heading}\n\n[C](c.md)\n\n## Exposition\n\n[B](b.md)\n"
    tree = markdown.render_tree(body)
    assert tree is not None and list(tree.iter("h2"))
    assert _converted_links(body) == ["c.md", "b.md"]
    sources["a"] = body.encode()
    before = copy.deepcopy((view, sources, paths, metadata))
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 1),))
    assert (view, sources, paths, metadata) == before


def test_nested_setext_provenance_does_not_hide_unquoted_mathematical_references() -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    body = "# A\n\n> Sources\n> -------\n>\n> [C](c.md)\n\nSee [B](b.md).\n"
    assert _converted_links(body) == ["c.md", "b.md"]
    sources["a"] = body.encode()
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 1),))


@pytest.mark.parametrize(("body", "skipped"), [
    ("# A\n\nA statement with no links.\n", True),
    ("# A\n\nA statement.\n\n## Sources\n\n- [Paper](https://example.org/paper)\n- [C](c.md)\n", True),
    ("# A\n\nText.\n\n## Depends on\n\n- [B](b.md)\n\n## Sources\n\n### Primary\n\n- [C](c.md)\n", True),
    ("# A\n\nSee [B](b.md).\n\n## Sources\n\n- [C](c.md)\n", False),
    ("# A\n\nText.\n\n## Sources\n\n- [C](c.md)\n\n## Notes\n\n[B](b.md)\n", False),
    ("# A\n\nText.\n\n## Sources\n\n- [C](c.md)\n\nNotes\n-----\n\n[B](b.md)\n", False),
    ("# A\n\nText.\n\n##Sources\n\n- [C](c.md)\n", False),
    ("# A\n\nText.\n\n# References\n\n- [C](c.md)\n", True),
    ("# A\n\nText.\n\n# Sources\n\n- [C](c.md)\n", True),
    ("# A\n\nText.\n## Sources\n- [C](c.md)\n", False),
    ("# A\n\nText.\n{: .wide }\n\n## Sources\n\n- [C](c.md)\n", False),
    ("# A\n\nText &amp; more.\n\n## Sources\n\n- [C](c.md)\n", False),
    ("# A\n\n" + "".join("    " * depth + "- item\n" for depth in range(130)) + "\n## Sources\n\n- [C](c.md)\n", False),
])
def test_an_article_that_links_only_from_provenance_skips_the_render_with_the_same_targets(body: str, skipped: bool) -> None:
    lines = site_index._wiki_lines(body)
    assert lines is not None
    assert site_index._no_wiki_targets(lines) is skipped
    if skipped:
        assert site_index._wiki_targets(lines) == []


def test_extraction_renders_only_articles_that_link_outside_their_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    sources["a"] = b"# A\n\nText.\n\n## Sources\n\n- [C](c.md)\n"
    sources["b"] = b"# B\n\nSee [C](c.md).\n"
    rendered, render = [], site_index._wiki_targets
    monkeypatch.setattr(site_index, "_wiki_targets", lambda lines: rendered.append(lines[0]) or render(lines))
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((1, 2),))
    assert rendered == ["# B"]


def test_setext_heading_attributes_keep_valid_incoming_links_but_mark_outbound_extraction_unavailable() -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    sources["a"] = b"# A\n\n[B](b.md).\n"
    sources["b"] = b"# B\n\nSources {.note}\n-------\n\n[C](c.md).\n"
    assert publishable_article(sources["b"].decode())[1]
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 1),), (1,))


@pytest.mark.parametrize("heading", ["## Sources {#sources}", "##Sources {#sources}", "## Sources {.note} ##"])
def test_applied_heading_attributes_mark_only_outgoing_data_unavailable_and_keep_published_incoming(heading: str) -> None:
    view = _view("a", "b", "c")
    paths, sources, metadata = _inputs("a", "b", "c")
    body = f"# B\n\n{heading}\n\n[C](c.md).\n"
    assert _converted_links(body) == ["c.md"]
    assert publishable_article(body)[1]
    sources["a"] = b"# A\n\n[B](b.md).\n"
    sources["b"] = body.encode()
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 1),), (1,))


def test_optional_source_omissions_keep_capture_api_compatibility_and_known_incoming_targets() -> None:
    view = _view("a", "b", edges=(ViewEdge("a", "b", 2, 3),))
    paths, sources, metadata = _inputs("a", "b")
    sources["a"] = b"# A\n\n[B](b.md).\n"
    sources["b"] = b"# B\n\n[A](a.md). [Unsafe](javascript:alert(1)).\n"
    assert publishable_article(sources["b"].decode())[1]
    before = copy.deepcopy((view, paths, sources, metadata))
    arguments = dict(captured_sources=sources, source_paths=paths, metadata=metadata)
    baseline = site_index.extract_wiki_links(view, **arguments)
    assert baseline == site_index.WikiLinks(((0, 1), (1, 0)))
    assert site_index.extract_wiki_links(view, **arguments, unavailable_source_ids=()) == baseline
    assert site_index.extract_wiki_links(view, **arguments, unavailable_source_ids=("b",)) == site_index.WikiLinks(((0, 1),), (1,))
    assert (view, paths, sources, metadata) == before


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

    links = site_index.extract_wiki_links(view, captured_sources=sources, source_paths=paths, metadata=metadata)

    assert links == site_index.WikiLinks(((0, 1), (1, 0), (1, 2)))
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

    assert site_index.extract_wiki_links(
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
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 1),))


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
    links = site_index.extract_wiki_links(view, captured_sources=sources, source_paths=paths, metadata=metadata)
    assert links == site_index.WikiLinks((), (0,))
    output = tmp_path / "wiki-links.json"
    site_index.write_wiki_links(output, links, index_sha256="a" * 64)
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
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 1),), (1,))


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

    links = site_index.extract_wiki_links(view, captured_sources=sources, source_paths=paths, metadata=metadata)

    assert links == site_index.WikiLinks(((0, 1),))


@pytest.mark.parametrize("target", [
    "https://example.com/b.md", "//example.com/b.md", "/roadmap/b.md", "../../b.md",
    "../sources/b.md", ".hidden/b.md", "%2Ehidden/b.md", "%2Froadmap/b.md", "%5Cb.md",
    "missing.md", "b.html", "#b", "b.md%00", "%FF.md", "javascript:b.md",
])
def test_nonpublished_or_unsafe_targets_cannot_establish_references(target: str) -> None:
    view = _view("a", "b")
    paths, sources, metadata = _inputs("a", "b")
    sources["a"] = f"# A\n\n[Target]({target})\n".encode()
    assert site_index.extract_wiki_links(
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

    links = site_index.extract_wiki_links(view, captured_sources=sources, source_paths=paths, metadata=metadata)

    assert links == site_index.WikiLinks((), (1, 2, 3, 4, 5))


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
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks(((0, 1),))


def test_no_capture_and_ambiguous_or_cyclic_sources_have_no_invented_fallback() -> None:
    view = _view("a", "b", "c", "d")
    absent = site_index.extract_wiki_links(view)
    assert absent.references == () and absent.unavailable_sources == (0, 1, 2, 3)
    assert absent.reason and "not supplied" in absent.reason
    paths, sources, metadata = _inputs("a", "b", "c", "d")
    paths["b"] = paths["a"]
    metadata["c"]["parent"] = "d"
    metadata["d"]["parent"] = "c"
    sources["a"] = b"# A\n\n[B](b.md) [C](c.md)\n"
    assert site_index.extract_wiki_links(
        view, captured_sources=sources, source_paths=paths, metadata=metadata,
    ) == site_index.WikiLinks((), (0, 1, 2, 3))


def test_optional_asset_binds_exact_index_bytes_and_leaves_dependencies_and_status_unchanged(tmp_path: Path) -> None:
    view = _view("b", "a", edges=(ViewEdge("b", "a", 2, 3),))
    paths, sources, metadata = _inputs("b", "a")
    sources["a"] = b"# A\n\n[B](b.md).\n"
    baseline, indexed, sidecar = (tmp_path / name for name in ("baseline.json", "index.json", "wiki-links.json"))
    arguments = dict(context_links={"a": "a.html", "b": "b.html"}, metadata=metadata, include_relations=True)
    site_index.write_search_index(baseline, view, **arguments)
    digests: list[str] = []
    site_index.write_search_index(indexed, view, **arguments, sha256_out=digests)
    site_index.write_wiki_links(
        sidecar, site_index.extract_wiki_links(view, captured_sources=sources, source_paths=paths, metadata=metadata),
        index_sha256=digests[0],
    )

    assert baseline.read_bytes() == indexed.read_bytes()
    assert indexed.read_bytes() == (
        b'{"nodes":[{"id":"b","kind":"node","route":"../roadmap/b.html","status":"planned","title":"b"},'
        b'{"id":"a","kind":"node","route":"../roadmap/a.html","status":"planned","title":"a"}],'
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


def test_a_large_layer_is_published_whole(tmp_path: Path) -> None:
    """No size cap: every reference is written, however many there are."""
    names = tuple(f"n{index}" for index in range(120))
    view = _view(*names)
    paths, sources, metadata = _inputs(*names)
    sources["n0"] = ("# First\n\n" + " ".join(f"[N]({name}.md)" for name in names[1:])).encode()
    links = site_index.extract_wiki_links(view, captured_sources=sources, source_paths=paths, metadata=metadata)
    assert links.reason is None and links.references == tuple((0, index) for index in range(1, 120))
    warnings: list[str] = []
    output = tmp_path / "wiki-links.json"
    site_index.write_wiki_links(output, links, index_sha256="a" * 64, warnings_out=warnings)
    payload = json.loads(output.read_bytes())
    assert payload["availability"] == "complete" and len(payload["references"]) == 119
    assert "reason" not in payload and warnings == []


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


def test_graph_page_source_omission_flags_change_only_the_optional_layer(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    path = blueprint / "roadmap/b.md"
    path.write_text(path.read_text().replace("A proposition.", "A proposition. [A](a.md) [Unsafe](javascript:alert(1))."))
    graph = load_graph(blueprint)
    statuses = derive(graph)
    sources = {node_id: node.path.read_bytes() for node_id, node in graph.nodes.items()}
    paths = {node_id: node.path.relative_to(blueprint) for node_id, node in graph.nodes.items()}
    args = dict(node_links=lambda _page, names: {name: f"../roadmap/README.html#{name}" for name in names},
                book_targets={}, captured_sources=sources, source_paths=paths)
    baseline, omitted = tmp_path / "baseline", tmp_path / "omitted"
    graph_pages.write_graph_pages(graph, statuses, baseline, **args)
    graph_pages.write_graph_pages(graph, statuses, omitted, **args, unavailable_source_ids=("b",))
    assert (baseline / graph_pages.SEARCH_INDEX).read_bytes() == (omitted / graph_pages.SEARCH_INDEX).read_bytes()
    original = json.loads((baseline / graph_pages.WIKI_LINKS).read_bytes())
    changed = json.loads((omitted / graph_pages.WIKI_LINKS).read_bytes())
    index = json.loads((omitted / graph_pages.SEARCH_INDEX).read_bytes())
    ids = [node["id"] for node in index["nodes"]]
    assert [[ids[a], ids[b]] for a, b in original["references"]] == [["a", "b"], ["b", "a"]]
    assert [[ids[a], ids[b]] for a, b in changed["references"]] == [["a", "b"]]
    assert changed["unavailable_sources"] == [ids.index("b")]
    assert changed["availability"] == "partial" and original["availability"] == "complete"
    for relative in graph_pages.graph_page_paths(graph):
        assert (baseline / relative).read_bytes() == (omitted / relative).read_bytes()


@pytest.mark.parametrize("unsupported", [
    "## Sources {#private}\n\n[A](a.md).",
    "[Unsafe](javascript:alert(1)).",
    "```{.lean #private}\nexample\n```",
])
def test_real_render_forwards_publication_findings_without_reparsing_or_reopening_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsupported: str,
) -> None:
    blueprint = _blueprint(tmp_path)
    coverage = blueprint / "coverage/README.md"
    coverage.parent.mkdir()
    coverage.write_text("# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n"
                        "| Project scope | MAPPED | Source audit pending |\n", encoding="utf-8")
    path = blueprint / "roadmap/b.md"
    original = path.read_text()
    path.write_text(original.replace("A proposition.", f"AUTHORED_LINK_MARKER [A](a.md).\n\n{unsupported}"), encoding="utf-8")
    extractor = site_index.extract_wiki_links
    found: list[tuple[str, ...]] = []

    def retained_only(*args, **kwargs):
        found.append(tuple(sorted(kwargs["unavailable_source_ids"])))
        with monkeypatch.context() as guarded:
            def forbidden(*_args, **_kwargs):
                raise AssertionError("wiki extraction must reuse captured publication findings without rereading paths or parsing publication")
            guarded.setattr(Path, "read_bytes", forbidden)
            guarded.setattr(Path, "read_text", forbidden)
            guarded.setattr(render, "publishable_article", forbidden)
            return extractor(*args, **kwargs)

    monkeypatch.setattr(site_index, "extract_wiki_links", retained_only)
    output = tmp_path / "site-src"
    report = render.render_site(blueprint, output)
    assert found == [("b",)]
    assert any("b:" in warning and "shows it as typed" in warning for warning in report.warnings)
    index_bytes = (output / graph_pages.SEARCH_INDEX).read_bytes()
    index = json.loads(index_bytes)
    ids = [node["id"] for node in index["nodes"]]
    layer = json.loads((output / graph_pages.WIKI_LINKS).read_bytes())
    assert layer["index_sha256"] == hashlib.sha256(index_bytes).hexdigest()
    assert layer["availability"] == "partial" and layer["unavailable_sources"] == [ids.index("b")]
    assert [[ids[a], ids[b]] for a, b in layer["references"]] == [["a", "b"]]
    assert index["relations"] == [[ids.index("a"), ids.index("b"), 1, 0]]
    assert {node["id"]: node["status"] for node in index["nodes"] if node["id"] in {"a", "b"}} == {"a": "can_state", "b": "planned"}
    converted = markdown.site_converter().convert((output / "roadmap/README.md").read_text())
    tree = html5lib.parseFragment(converted)
    card = next(node for node in tree.iter() if node.get("data-autoform-node-id") == "b"
                and "bp-thmwrapper" in node.get("class", "").split())
    typed = next(node for node in card.iter() if node.tag == "{http://www.w3.org/1999/xhtml}pre")
    assert "AUTHORED_LINK_MARKER [A](" in "".join(typed.itertext())
    assert not list(typed.iter("{http://www.w3.org/1999/xhtml}a"))


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
