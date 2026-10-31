"""The site's header search, as MkDocs Material builds its index from rendered pages."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import html5lib
import pytest

from autoform_cli.render import render_site
from autoform_cli.scaffold import scaffold_project
from autoform_cli.site_check import check_site
from tests.test_lean_sources import _git_commit

_REPOSITORY = "https://github.com/owner/repo"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    scaffold_project(project, title="Search", repository_url=_REPOSITORY)
    roadmap = project / "blueprint" / "roadmap"
    (roadmap / "README.md").write_text(
        "---\n---\n\n# Roadmap\n\nOne chapter.\n\n## Chapters\n\n- [Chapter](ch/README.md)\n", encoding="utf-8"
    )
    (roadmap / "ch").mkdir()
    # No section headings, so before #390 every card fell in the page's first record.
    (roadmap / "ch" / "README.md").write_text(
        "---\n---\n\n# Chapter\n\nTwo results.\n\n- [First](first.md)\n- [Second](second.md)\n", encoding="utf-8"
    )
    article = "---\ndeclaration: theorem\nstatement: formalized\nlean: {lean}\n---\n\n# {title}\n\n{body}\n"
    # A "<" makes render convert this card's statement and notes itself before boxing them.
    first = (
        "For $a<b$, the first result holds.\n\n"
        "## Sources\n\n- [The source](https://example.org/paper)\n\n"
        "## Proof sketch\n\nInduction on firstsketchword.\n\n"
        "## Bound for $n<k$\n\nA firstboundword.\n\n"
        "## Depends on\n\nThis node has no prerequisites."
    )
    # No "<": the site's own conversion reads this card's notes.
    second = (
        "The second result holds.\n\n## Remarks\n\nA secondremarkword.\n\n"
        "## Case {a\n\nA secondbraceword.\n\n## Depends on\n\n- [First](first.md)"
    )
    for name, lean, body in (
        ("first", "Project.first, Project.first_aux", first),
        ("second", "Project.second", second),
    ):
        (roadmap / "ch" / f"{name}.md").write_text(
            article.format(lean=lean, title=name.title(), body=body), encoding="utf-8"
        )
    (project / "Project").mkdir()
    (project / "Project" / "Basic.lean").write_text(
        "namespace Project\n\ntheorem first : True := trivial\n\ntheorem first_aux : True := trivial\n\n"
        "theorem second : True := trivial\n\nend Project\n",
        encoding="utf-8",
    )
    return project


def _records(project: Path) -> list[dict[str, str]]:
    mkdocs_build = pytest.importorskip("mkdocs.commands.build")
    from mkdocs.config import load_config

    mkdocs_build.build(load_config(config_file=str(project / "mkdocs.yml")))
    index = json.loads((project / "site" / "search" / "search_index.json").read_text(encoding="utf-8"))
    return index["docs"]


@pytest.mark.parametrize("code_links", (True, False), ids=("code-links", "no-code-links"))
def test_each_statement_card_is_one_search_record_holding_its_lean_names(tmp_path: Path, code_links: bool) -> None:
    project = _project(tmp_path)
    if code_links:
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        ref = _git_commit(project, "fixture")
        render_site(
            project / "blueprint", project / "site-src", lean_root=project, repository_url=_REPOSITORY, ref=ref
        )
    else:
        render_site(project / "blueprint", project / "site-src")
    chapter = (project / "site-src/roadmap/ch/README.md").read_text(encoding="utf-8")
    # With verified code links, the names are in no visible text, only in the icons' labels.
    assert ('class="bp-code-link"' in chapter) is code_links

    records = _records(project)

    chapter_records = [record for record in records if record["location"].startswith("roadmap/ch/index.html")]
    anchors = [record["location"].partition("#")[2] for record in chapter_records]
    assert anchors.count("first") == anchors.count("second") == 1
    cards = {record["location"].partition("#")[2]: record for record in chapter_records}
    first, second = cards["first"], cards["second"]
    assert (first["title"], second["title"]) == ("First", "Second")
    for name in ("Project.first", "Project.first_aux"):
        assert name in first["text"]
    assert "Project.second" in second["text"]
    # The notes, their headings' words included, stay in their card's record.
    for words in ("Sources", "Proof sketch", "firstsketchword", "Bound for", "firstboundword"):
        assert words in first["text"], words
    for words in ("Remarks", "secondremarkword", "Case {a", "secondbraceword"):
        assert words in second["text"], words
    # No notes heading starts a record of its own.
    assert not {"sources", "proof-sketch", "bound-for-nk", "remarks", "case-a"} & set(anchors)
    # A card is no entry in the page's table of contents, and every link and anchor still resolves.
    page = (project / "site/roadmap/ch/index.html").read_text(encoding="utf-8")
    assert 'href="#first"' not in page.split('class="md-content"')[0]
    assert check_site(project / "site", project / "site-src").issues == ()


@pytest.mark.parametrize("title", ("Theorem", "Definition"))
def test_a_kind_only_title_keeps_its_own_search_record_and_canonical_anchor(tmp_path: Path, title: str) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/ch/first.md"
    text = article.read_text(encoding="utf-8").replace("# First\n", f"# {title}\n")
    if title == "Definition":
        text = text.replace("declaration: theorem\n", "declaration: definition\n")
    article.write_text(text, encoding="utf-8")
    render_site(project / "blueprint", project / "site-src")

    records = _records(project)
    first = next(record for record in records if record["location"] == "roadmap/ch/index.html#first")
    assert first["title"] == title and first["title"] != "Chapter"
    assert "Project.first" in first["text"] and "firstsketchword" in first["text"]
    assert check_site(project / "site", project / "site-src").issues == ()
    page = (project / "site/roadmap/ch/index.html").read_text(encoding="utf-8")
    tree = html5lib.parse(page, treebuilder="etree", namespaceHTMLElements=False)
    heading = next(node for node in tree.iter("h4") if node.get("id") == "first")
    hidden = [node for node in heading.iter("span") if "hidden" in node.attrib]
    assert len(hidden) == 1 and "".join(hidden[0].itertext()) == title


def test_isolating_notes_headings_keeps_every_card_and_heading_whole(tmp_path: Path) -> None:
    project = _project(tmp_path)
    render_site(project / "blueprint", project / "site-src")
    _records(project)

    page = (project / "site/roadmap/ch/index.html").read_text(encoding="utf-8")
    tree = html5lib.parse(page, treebuilder="etree", namespaceHTMLElements=False)
    article = next(el for el in tree.iter("article") if "md-content__inner" in el.get("class", "").split())
    cards = [el for el in article.iter("div") if "bp-thmwrapper" in el.get("class", "").split()]
    assert [card.get("data-autoform-node-id") for card in cards] == ["ch/first", "ch/second"]
    # A "<" in a notes heading once closed its wrapper early and pushed the rest of the page out of the article.
    assert any("bp-book-nav" in el.get("class", "").split() for el in article.iter())
    second = cards[1]
    assert any("bp-dependencies" in el.get("class", "").split() for el in second.iter())
    headings = {"".join(el.itertext()).strip(): el.get("id") for el in article.iter("h6")}
    assert headings["Sources"] == "sources"
    assert headings["Proof sketch"] == "proof-sketch"
    assert headings["Remarks"] == "remarks"
    # A brace in a heading is the author's text, not an attribute list.
    assert headings["Case {a"] == "case-a"
