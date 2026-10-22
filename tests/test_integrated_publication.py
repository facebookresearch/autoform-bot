"""Full mathematical reading and graph navigation share canonical book routes."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

import html5lib
import pytest
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
    assert theorem["result"] is True and records[f"{group}/base"]["result"] is True
    assert theorem["route"] == "../roadmap/Analysis%20%25%20caf%C3%A9/part-01/index.html#main"
    assert theorem["url"].startswith("scopes/Analysis%20%25%20caf%C3%A9/part-01.html#node=")
    assert theorem["data_url"] == "scopes/Analysis%20%25%20caf%C3%A9/part-01.json"
    assert theorem["scope"] == group and theorem["parent"] == group
    assert "area" not in theorem  # No area guessed from the directory name.
    assert records[topic]["scope"] == topic and records[topic]["area"] == "Analysis"
    assert records[topic]["navigation"] == "module"
    assert records[group]["scope"] == group and records[group]["navigation"] == "transparent"
    assert all(records[node_id]["result"] is False for node_id in ("roadmap", topic, group, f"{topic}/note"))
    assert index["relations"] == [[f"{group}/base", f"{group}/main", 1, 0]]
    for record in records.values():
        assert (index_path.parent / unquote(record["data_url"])).resolve().is_file()
    alias = _tree(output / "dependencies/scopes" / f"{group}.md")
    (viewer,) = _class(alias, "bp-dag-viewer")
    assert viewer.get("data-page-href") == "../../../roadmap/Analysis%20%25%20caf%C3%A9/part-01/index.html"
    assert "This final sentence" not in "".join(alias.itertext())
    assert any("Continue to the mathematical page" in "".join(link.itertext()) for link in alias.iter("a"))
    assert urlsplit(theorem["route"]).fragment == "main"


def test_emitted_result_flags_use_declarations_not_progress_or_lean_availability(tmp_path: Path) -> None:
    project, group, _explanation = _project(tmp_path)
    blueprint, output = project / "blueprint", tmp_path / "out"
    root = blueprint / "roadmap/README.md"
    _write(root, "---\ndeclaration: def\n---\n\n" + root.read_text(encoding="utf-8"))
    chapter = blueprint / "roadmap" / group
    _write(chapter / "ready.md", "---\ndeclaration: def\n---\n\n# Ready definition\n\nA concrete definition, not yet in Lean.\n")
    _write(chapter / "planned.md", (
        "---\ndeclaration: theorem\n---\n\n# Planned theorem\n\nA concrete theorem with an unstated prerequisite.\n\n"
        "## Depends on\n\n- [Ready definition](ready.md)\n"
    ))
    _write(chapter / "deferred.md", "---\nnot_ready: true\n---\n\n# Deferred scope\n\nNo concrete declaration has been chosen.\n")
    _write(chapter / "source.md", "---\nnavigation: source\n---\n\n# Original textbook\n\nThis page records provenance.\n")
    _write(blueprint / "sources/ledger.md", "# Declaration ledger\n\nThe existing module declarations.\n")
    ledger = "\n## Sources\n\n- [Declaration ledger](../../../sources/ledger.md)\n"
    _write(chapter / "pending.md", "---\ncatalog: module\n---\n\n# Pending inventory\n" + ledger)
    _write(chapter / "checked.md", (
        "---\ncatalog: module\nlean: Demo.main\nstatement: formalized\nproof: formalized\n---\n\n# Checked inventory\n" + ledger
    ))
    coverage = blueprint / "coverage/README.md"
    _write(coverage, coverage.read_text(encoding="utf-8") + (
        f"| Existing modules | INVENTORIED | [Pending](../roadmap/{group}/pending.md), [Checked](../roadmap/{group}/checked.md) |\n"
    ))

    render.render_site(blueprint, output, lean_root=project)

    index = json.loads((output / graph_pages.SEARCH_INDEX).read_text(encoding="utf-8"))
    records = {record["id"]: record for record in index["nodes"]}
    for name, expected_status in (("ready", "can_state"), ("planned", "planned")):
        record = records[f"{group}/{name}"]
        assert record["status"] == expected_status and "lean" not in record
        assert record["result"] is True
    for name, expected_status in (("pending", "inventory_pending"), ("checked", "inventory_checked")):
        record = records[f"{group}/{name}"]
        assert record["status"] == expected_status and record["result"] is False
        assert record["catalog"] == "module"
    assert all("catalog" not in records[node_id] for node_id in ("roadmap", "chapter/note", f"{group}/ready", f"{group}/main"))
    graph = load_graph(blueprint)
    assert graph.nodes["roadmap"].formalizable  # A declared container is still organization, not a leaf result.
    assert records["roadmap"]["result"] is False
    for node_id in ("chapter", group, "chapter/note", f"{group}/deferred", f"{group}/source"):
        assert records[node_id]["result"] is False
    assert records[f"{group}/main"]["result"] is True
    assert all(type(record["result"]) is bool for record in records.values())


def test_emitted_result_ids_and_book_anchors_preserve_reserved_looking_prefixes(tmp_path: Path) -> None:
    project, _group, _explanation = _project(tmp_path)
    blueprint, output = project / "blueprint", tmp_path / "out"
    ids = ("Geometry", "scope:Geometry", "boundary:Geometry")
    for node_id in ids:
        _write(blueprint / "roadmap" / f"{node_id}.md", (
            f"---\ndeclaration: theorem\n---\n\n# Exact {node_id}\n\nThe distinct statement belonging to {node_id}.\n"
        ))

    render.render_site(blueprint, output, lean_root=project)

    index = json.loads((output / graph_pages.SEARCH_INDEX).read_text(encoding="utf-8"))
    records = {record["id"]: record for record in index["nodes"]}
    chapter = _tree(output / "roadmap/README.md")
    cards = {card.get("data-autoform-node-id"): card for card in _class(chapter, "bp-thmwrapper")}
    assert len({records[node_id]["route"] for node_id in ids}) == len(ids)
    for node_id in ids:
        record, card = records[node_id], cards[node_id]
        assert record["id"] == node_id and record["result"] is True
        assert record["route"] == "../roadmap/index.html#" + card.get("id")
        assert record["scope"] == record["parent"] == "roadmap"
        assert unquote(urlsplit(record["url"]).fragment) == "node=" + node_id
        assert f"The distinct statement belonging to {node_id}." in "".join(card.itertext())


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


def test_legacy_aliases_keep_exact_canonical_links_in_a_strict_mkdocs_build(tmp_path: Path) -> None:
    mkdocs_build = pytest.importorskip("mkdocs.commands.build")
    from mkdocs.config import load_config

    project, group, _explanation = _project(tmp_path)
    output, built = tmp_path / "out", tmp_path / "site"
    render.render_site(project / "blueprint", output, lean_root=project)
    config = tmp_path / "mkdocs.yml"
    _write(config, (
        f'site_name: Strict canonical publication\ndocs_dir: "{output}"\nsite_dir: "{built}"\n'
        'use_directory_urls: false\nstrict: true\nmarkdown_extensions:\n  - md_in_html\n'
    ))

    # Strict doc-link validation must accept every alias: the published HTML
    # route is not a missing Markdown source file. Do not suppress warnings.
    mkdocs_build.build(load_config(config_file=str(config)))

    for relative, href in (
        ("dependencies.html", "roadmap/index.html"),
        (f"dependencies/scopes/{group}.html", "../../../roadmap/chapter/part-01/index.html"),
    ):
        tree = html5lib.parse((built / relative).read_text(encoding="utf-8"), treebuilder="etree", namespaceHTMLElements=False)
        (viewer,) = _class(tree, "bp-dag-viewer")
        assert viewer.get("data-page-href") == href
        assert any(link.get("href") == href and "Continue to the mathematical page" in "".join(link.itertext()) for link in tree.iter("a"))
        assert (built / relative).parent.joinpath(href).resolve().is_file()


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
