"""Full mathematical reading and graph navigation share canonical book routes."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

import html5lib
from mkdocs.utils.meta import get_data

from autoform_cli import dag_viewer, graph_pages, render
from autoform_cli.graph import load_graph
from autoform_cli.markdown import site_converter
from autoform_cli.status import derive


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _project(tmp_path: Path, *, topic: str = "chapter", hidden: bool = False) -> tuple[Path, str, str]:
    project = tmp_path / "project"
    blueprint = project / "blueprint"
    _write(blueprint / "README.md", "# Overview\n\nOne mathematical library.\n\n- [Mathematics](roadmap/README.md)\n")
    _write(blueprint / "roadmap/README.md", f"# All mathematics\n\n- [Topic]({topic}/README.md)\n")
    _write(blueprint / "coverage/README.md", "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n| Scope | MAPPED | Author review |\n")
    _write(blueprint / "roadmap" / topic / "README.md", (
        "---\narea: Analysis\nnavigation: module\n---\n\n# Operator theory\n\n"
        "This complete narrative introduces the operators and their notation.\n\n"
        "- [Statements](part-01/README.md)\n- [Mathematical note](note.md)\n"
    ))
    group = f"{topic}/part-01"
    _write(blueprint / "roadmap" / group / "README.md", (
        "---\nnavigation: transparent\n---\n\n# Operator statements\n\n"
        "The hypotheses are written before each conclusion.\n\n- [Base](base.md)\n- [Main theorem](main.md)\n"
    ))
    _write(blueprint / "roadmap" / group / "base.md", (
        "---\ndeclaration: def\nstatement: formalized\nlean: Demo.base\n---\n\n"
        "# Base\n\nThe base value is zero.\n\n## Depends on\n\nNo prerequisites.\n"
    ))
    explanation = "Every step in this mathematical explanation is recorded explicitly. " * 12
    _write(blueprint / "roadmap" / group / "main.md", (
        "---\ndeclaration: theorem\nstatement: formalized\nproof: formalized\nlean: Demo.main\n---\n\n"
        "# Main theorem\n\nThe proposition $0 = 0$ holds. " + explanation +
        "This final sentence must remain present in the reading view.\n\n"
        "## Proof notes\n\nThe proof uses reflexivity.\n\n## Depends on\n\n- [Base](base.md)\n"
    ))
    _write(blueprint / "roadmap" / topic / "note.md", (
        "# A mathematical note\n\nThis independent narrative remains a complete book page.\n"
    ))
    _write(project / "Demo.lean", "namespace Demo\ndef base : Nat := 0\ntheorem main : 0 = 0 := rfl\nend Demo\n")
    if hidden:
        _write(blueprint / "roadmap/private/README.md", "---\npublished: false\n---\n\n# Private plans\n")
        _write(blueprint / "roadmap/private/secret.md", "# PRIVATE_READING_MARKER\n\nPrivate mathematical notes.\n")
        _write(blueprint / "atlas.json", '{"schema":"autoform-atlas/v1","areas":{"Secret area":["private/secret"]}}')
    return project, group, explanation


def _tree(page: Path):
    body, _metadata = get_data(page.read_text(encoding="utf-8"))
    converter = site_converter()
    html = converter.convert(body)
    return html5lib.parse(html, treebuilder="etree", namespaceHTMLElements=False)


def _class(tree, name: str):
    return [element for element in tree.iter() if name in element.get("class", "").split()]


def test_complete_book_content_and_authoritative_cards_survive_without_javascript(tmp_path: Path) -> None:
    project, group, explanation = _project(tmp_path)
    blueprint, output = project / "blueprint", tmp_path / "out"
    before = {path.relative_to(blueprint): path.read_bytes() for path in blueprint.rglob("*.md")}

    render.render_site(blueprint, output, lean_root=project)

    for relative in ("README.md", "roadmap/README.md", "roadmap/chapter/README.md", "roadmap/chapter/note.md", f"roadmap/{group}/README.md"):
        tree = _tree(output / relative)
        assert len(_class(tree, "bp-explorer-page")) == 1
        (reading,) = _class(tree, "bp-reading-source")
        (viewer,) = _class(tree, "bp-dag-viewer")
        assert reading.get("data-page-title")
        assert viewer.get("data-graph-src") and viewer.get("data-catalog-src") and viewer.get("data-page-href")
        assert viewer.get("data-scope-id")
    tree = _tree(output / "roadmap" / group / "README.md")
    (reading,) = _class(tree, "bp-reading-source")
    text = "".join(reading.itertext())
    assert explanation.strip() in text
    assert "This final sentence must remain present in the reading view." in text
    assert "The proof uses reflexivity." in text
    assert len(text) > 420
    cards = _class(reading, "bp-thmwrapper")
    assert [(card.get("data-autoform-node-id"), card.get("id")) for card in cards] == [
        (f"{group}/base", "base"), (f"{group}/main", "main"),
    ]
    assert not (output / "roadmap" / group / "main.md").exists()
    assert not _class(tree, "bp-context-link")
    assert before == {path.relative_to(blueprint): path.read_bytes() for path in blueprint.rglob("*.md")}


def test_routes_preserve_encoded_book_anchors_and_real_scope_metadata(tmp_path: Path) -> None:
    topic = "Analysis % café"
    project, group, _explanation = _project(tmp_path, topic=topic)
    output = tmp_path / "out"

    render.render_site(project / "blueprint", output, lean_root=project)

    index_path = output / graph_pages.SEARCH_INDEX
    index = json.loads(index_path.read_text(encoding="utf-8"))
    records = {record["id"]: record for record in index["nodes"]}
    theorem = records[f"{group}/main"]
    assert theorem["route"] == "../roadmap/Analysis%20%25%20caf%C3%A9/part-01/index.html#main"
    assert theorem["url"].startswith("scopes/Analysis%20%25%20caf%C3%A9/part-01.html#node=")
    assert theorem["data_url"] == "scopes/Analysis%20%25%20caf%C3%A9/part-01.json"
    assert theorem["scope"] == group and theorem["parent"] == group
    assert "area" not in theorem  # No area guessed from the directory name.
    assert records[topic]["scope"] == topic and records[topic]["area"] == "Analysis"
    assert records[topic]["navigation"] == "module"
    assert records[group]["scope"] == group and records[group]["navigation"] == "transparent"
    assert index["relations"] == [[f"{group}/base", f"{group}/main", 1, 0]]
    for record in records.values():
        assert (index_path.parent / unquote(record["data_url"])).resolve().is_file()
    alias = _tree(output / "dependencies/scopes" / f"{group}.md")
    (viewer,) = _class(alias, "bp-dag-viewer")
    assert viewer.get("data-page-href") == "../../../roadmap/Analysis%20%25%20caf%C3%A9/part-01/index.html"
    assert "This final sentence" not in "".join(alias.itertext())
    assert any("Continue to the mathematical page" in "".join(link.itertext()) for link in alias.iter("a"))
    assert urlsplit(theorem["route"]).fragment == "main"


def test_shared_style_is_planned_written_and_not_duplicated_in_pages(tmp_path: Path) -> None:
    project, group, _explanation = _project(tmp_path)
    output = tmp_path / "out"

    render.render_site(project / "blueprint", output, lean_root=project)

    graph = load_graph(project / "blueprint")
    assert dag_viewer.STYLE_PATH in render._site_paths(graph)
    style = output / dag_viewer.STYLE_PATH
    assert style.read_text(encoding="utf-8") == dag_viewer._STYLE
    manifest = json.loads((output / render.PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert dag_viewer.STYLE_PATH in manifest["files"]
    for page in (output / "roadmap" / group / "README.md", output / "dependencies.md"):
        text = page.read_text(encoding="utf-8")
        assert '<style data-autoform-dag-style>' not in text
        tree = _tree(page)
        links = [element for element in tree.iter("link") if element.get("data-autoform-dag-style") is not None]
        assert len(links) == 1
    summary = (output / "SUMMARY.md").read_text(encoding="utf-8")
    assert "- Mathematics" in summary and "- Graph" not in summary
    assert "Dependency explorer" not in summary


def test_unpublished_reading_and_route_data_never_enter_the_site(tmp_path: Path) -> None:
    project, _group, _explanation = _project(tmp_path, hidden=True)
    output = tmp_path / "out"

    render.render_site(project / "blueprint", output, lean_root=project)

    assert not (output / "roadmap/private").exists()
    assert not (output / "atlas.json").exists()
    index = json.loads((output / graph_pages.SEARCH_INDEX).read_text(encoding="utf-8"))
    assert not any(record["id"].startswith("private") for record in index["nodes"])
    assert "Secret area" not in json.dumps(index)
    for page in (output / "roadmap").rglob("*.md"):
        assert "PRIVATE_READING_MARKER" not in page.read_text(encoding="utf-8")


def test_standalone_graph_writer_keeps_legacy_callbacks_bounded(tmp_path: Path) -> None:
    project, _group, _explanation = _project(tmp_path)
    graph = load_graph(project / "blueprint")
    requested: list[Path] = []

    def node_links(page: Path, node_ids) -> dict[str, str]:
        requested.append(page)
        return {node_id: f"book.html#{node_id}" for node_id in node_ids}

    pages = graph_pages.write_graph_pages(graph, derive(graph), tmp_path / "out", node_links=node_links)

    assert sorted(requested) == sorted(pages)


def test_typed_page_keeps_its_true_title_and_complete_reading_source(tmp_path: Path) -> None:
    page = tmp_path / "roadmap/note.md"
    full = "---\n" + render.header_title("Full source title") + "\n---\n\n<pre>All original source remains as typed.</pre>\n"
    framed = render._integrated_page(
        full, page=page, destination=tmp_path, payload=tmp_path / "dependencies.json",
        scope="roadmap", owner="note",
    )
    _write(page, framed)

    tree = _tree(page)
    (reading,) = _class(tree, "bp-reading-source")
    assert reading.get("data-page-title") == "Full source title"
    assert reading.get("data-autoform-node-id") == "note"
    assert "All original source remains as typed." in "".join(reading.itertext())
    assert not list(reading.iter("h1"))  # The client has the true title without inventing one from code.
