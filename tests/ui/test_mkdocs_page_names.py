"""Page names as MkDocs 1.6 reads them (#670): a page's suffix is spelled in lower case, MkDocs builds one page at a
URL, and the home page only from a page named README or index. Render publishes what MkDocs builds, links and lists
what it publishes there, and warns, with the rename that fixes it, where a vault's names differ."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autoform_cli.graph import load_graph
from autoform_cli.lean import PUBLICATION_MANIFEST
from autoform_cli.scaffold import scaffold_project
from autoform_cli.ui.render import publication_issues, render_site
from tests.test_lean_sources import _git_commit


def _rendered(tmp_path: Path, pages: dict[str, str], *, home: bool = True) -> tuple[list[str], dict]:
    """Render's warnings and publication.json's inventory for a scaffolded project with ``pages`` added to its
    blueprint, and without its README.md unless ``home``."""
    scaffold_project(tmp_path, title="Names")
    blueprint = tmp_path / "blueprint"
    if not home:
        (blueprint / "README.md").unlink()
    for name, text in pages.items():
        (blueprint / name).parent.mkdir(parents=True, exist_ok=True)
        (blueprint / name).write_text(text, encoding="utf-8")
    report = render_site(blueprint, tmp_path / "site-src", lean_root=tmp_path)
    # Check reports what render publishes.
    assert [warning for warning in report.warnings if "MkDocs" in warning] == [
        warning for warning in publication_issues(load_graph(blueprint), blueprint) if "MkDocs" in warning
    ]
    manifest = json.loads((tmp_path / "site-src" / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    return [warning for warning in report.warnings if "MkDocs" in warning], manifest["inventory"]


def test_a_page_suffix_in_another_case_leaves_the_file_out_with_its_rename(tmp_path: Path) -> None:
    """MkDocs would copy such a file as it stands, comments and all, and build no page from it."""
    warnings, inventory = _rendered(
        tmp_path,
        {
            "notes/Page.MD": "# Page\n\n<!-- A note to self. -->\n",
            "notes/Other.Markdown": "# Other\n",
            "notes/links.md": "# Links\n\nSee [the page](Page.MD) and [the other](Other.Markdown).\n",
        },
    )

    assert warnings == [
        (
            "notes/Other.Markdown: MkDocs builds a page only from a suffix in lower case, so the site leaves this file "
            "out; rename it Other.markdown"
        ),
        (
            "notes/Page.MD: MkDocs builds a page only from a suffix in lower case, so the site leaves this file out; "
            "rename it Page.md"
        ),
    ]
    assert inventory["omitted"]["notes/Page.MD"] == inventory["omitted"]["notes/Other.Markdown"] == "unsupported-type"
    assert not (tmp_path / "site-src/notes/Page.MD").exists()
    # A link to a file the site leaves out keeps only its text.
    links = (tmp_path / "site-src/notes/links.md").read_text(encoding="utf-8")
    assert "See the page and the other." in links and "Page.MD" not in links


def test_a_note_with_such_a_suffix_is_linked_at_the_verified_commit(tmp_path: Path) -> None:
    """Once the commit is verified, the repository reaches what the site leaves out under sources/, such a note
    too, with nothing to warn about."""
    scaffold_project(tmp_path, title="Names")
    blueprint = tmp_path / "blueprint"
    (blueprint / "sources/Notes.MD").write_text("# Notes\n", encoding="utf-8")
    (blueprint / "sources/README.md").write_text("# Sources\n\nSee [the notes](Notes.MD).\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    ref = _git_commit(tmp_path, "fixture")

    report = render_site(
        blueprint, tmp_path / "site-src", lean_root=tmp_path, repository_url="https://github.com/owner/repo", ref=ref
    )

    at_commit = f"https://github.com/owner/repo/blob/{ref}/blueprint/sources/Notes.MD"
    manifest = json.loads((tmp_path / "site-src" / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["inventory"]["linked"]["sources/Notes.MD"] == at_commit
    assert f"See [the notes]({at_commit})." in (tmp_path / "site-src/sources/README.md").read_text(encoding="utf-8")
    assert not [warning for warning in report.warnings if "Notes.MD" in warning]


def test_of_two_pages_at_one_url_the_site_keeps_the_one_mkdocs_keeps(tmp_path: Path) -> None:
    """MkDocs keeps the last in its order, index.md over README.md and a.md over a.markdown, and warns about the
    other, which fails its strict build."""
    warnings, inventory = _rendered(
        tmp_path,
        {
            "notes/README.md": "# Notes\n",
            "notes/index.md": "# Index\n",
            "notes/a.markdown": "# Older\n",
            "notes/a.md": "# Newer\n",
            "notes/links.md": "# Links\n\n[The readme](README.md), [the index](index.md) and [a](a.markdown).\n",
        },
    )

    assert warnings == [
        (
            "notes/README.md: MkDocs would build it at notes/index.html, where the site publishes notes/index.md, so "
            "the site leaves it out; rename one of them"
        ),
        (
            "notes/a.markdown: MkDocs would build it at notes/a.html, where the site publishes notes/a.md, so the site "
            "leaves it out; rename one of them"
        ),
    ]
    assert {
        name: inventory["published"].get(name) or inventory["omitted"][name]
        for name in ("notes/README.md", "notes/index.md", "notes/a.markdown", "notes/a.md")
    } == {
        "notes/README.md": "mkdocs-excluded",
        "notes/index.md": "notes/index.html",
        "notes/a.markdown": "mkdocs-excluded",
        "notes/a.md": "notes/a.html",
    }
    assert not (tmp_path / "site-src/notes/README.md").exists()
    links = (tmp_path / "site-src/notes/links.md").read_text(encoding="utf-8")
    assert "The readme, [the index](index.md) and a." in links


def test_an_articles_page_is_kept_over_another_page_at_its_url(tmp_path: Path) -> None:
    """Even where MkDocs would keep the other: it keeps index.markdown over README.md, the chapter's page."""
    warnings, inventory = _rendered(
        tmp_path,
        {
            "roadmap/index.markdown": "# Index\n",
            "roadmap/guide.md": "---\n---\n\n# Guide\n\nA narrative page.\n",
            "roadmap/guide.markdown": "# Twin\n",
        },
    )

    assert warnings == [
        (
            "roadmap/guide.markdown: MkDocs would build it at roadmap/guide.html, where the site publishes "
            "roadmap/guide.md, so the site leaves it out; rename one of them"
        ),
        (
            "roadmap/index.markdown: MkDocs would build it at roadmap/index.html, where the site publishes "
            "roadmap/README.md, so the site leaves it out; rename one of them"
        ),
    ]
    assert (inventory["published"]["roadmap/README.md"], inventory["published"]["roadmap/guide.md"]) == (
        "roadmap/index.html",
        "roadmap/guide.html",
    )
    assert (
        inventory["omitted"]["roadmap/index.markdown"]
        == inventory["omitted"]["roadmap/guide.markdown"]
        == ("mkdocs-excluded")
    )


def test_a_page_at_a_leafs_name_is_no_twin(tmp_path: Path) -> None:
    """A leaf is published on its chapter's page, not at a name of its own."""
    warnings, inventory = _rendered(
        tmp_path,
        {
            "roadmap/result.md": "---\ndeclaration: theorem\n---\n\n# Result\n\nA statement.\n",
            "roadmap/result.markdown": "# Notes\n",
        },
    )

    assert warnings == []
    assert (inventory["published"]["roadmap/result.md"], inventory["published"]["roadmap/result.markdown"]) == (
        "roadmap/index.html#result",
        "roadmap/result.html",
    )


def test_two_articles_at_one_url_are_both_published_with_a_warning(tmp_path: Path) -> None:
    """Every article's page is published, so the strict build fails until one is renamed."""
    warnings, inventory = _rendered(tmp_path, {"roadmap/index.md": "---\n---\n\n# Index\n\nA narrative page.\n"})

    assert warnings == [
        (
            "roadmap/README.md: MkDocs builds it at roadmap/index.html, as it builds roadmap/index.md, and keeps one of "
            "them, so its strict build fails; rename one"
        )
    ]
    assert inventory["published"]["roadmap/README.md"] == inventory["published"]["roadmap/index.md"]


def test_pages_whose_urls_differ_only_in_case_are_both_published_with_a_warning(tmp_path: Path) -> None:
    warnings, inventory = _rendered(tmp_path, {"guides/README.md": "# Guides\n", "guides/Index.md": "# Index\n"})

    assert warnings == [
        (
            "guides/Index.md: MkDocs builds it at guides/Index.html and guides/README.md at guides/index.html, so on a "
            "disk that ignores case one is written over the other; rename one of them"
        )
    ]
    assert (inventory["published"]["guides/README.md"], inventory["published"]["guides/Index.md"]) == (
        "guides/index.html",
        "guides/Index.html",
    )


@pytest.mark.parametrize("name", ["readme.md", "Readme.markdown"])
def test_a_root_readme_spelled_otherwise_is_no_home_page(tmp_path: Path, name: str) -> None:
    """Nor does the landing page take its title and lead from it, on any disk."""
    # A folder needs no index, so notes/readme.md is no matter.
    warnings, inventory = _rendered(
        tmp_path, {name: "# Home\n\nThe lead.\n", "notes/readme.md": "# Notes\n"}, home=False
    )

    stem, suffix = name.split(".")
    assert warnings == [
        (
            f"{name}: MkDocs builds the home page only from a page named README or index, so the site has none and this "
            f"page is at {stem}.html; rename it README.{suffix}"
        )
    ]
    assert inventory["published"][name] == f"{stem}.html"
    assert "bp-landing" not in (tmp_path / "site-src" / name).read_text(encoding="utf-8")


def test_a_flagged_root_readme_spelled_otherwise_is_shown_as_typed(tmp_path: Path) -> None:
    """It is a page like any other, not the landing page, which would take only its title and lead."""
    scaffold_project(tmp_path, title="Names")
    blueprint = tmp_path / "blueprint"
    (blueprint / "README.md").unlink()
    (blueprint / "readme.md").write_text("# Home\n\n<b>Raw</b> HTML.\n", encoding="utf-8")

    report = render_site(blueprint, tmp_path / "site-src", lean_root=tmp_path)

    assert "readme.md: the site shows it as typed until these are fixed" in report.warnings
    assert not [warning for warning in report.warnings if "landing page" in warning]


def test_mkdocs_builds_each_page_where_the_inventory_lists_it(tmp_path: Path) -> None:
    """A strict MkDocs build with the theme publishes every file the inventory lists, at its URL."""
    pytest.importorskip("material")
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    _, inventory = _rendered(
        tmp_path,
        {
            "notes/Page.MD": "# Page\n",
            "notes/README.md": "# Notes\n",
            "notes/index.md": "# Index\n",
            "notes/a.markdown": "# Older\n",
            "notes/a.md": "# Newer\n\n[Page](Page.MD), [readme](README.md) and [older](a.markdown).\n",
            "roadmap/guide.md": "---\n---\n\n# Guide\n\nA narrative page.\n",
            "roadmap/guide.markdown": "# Twin\n",
            "roadmap/index.markdown": "# Index\n",
            "guides/README.md": "# Guides\n",
            "guides/Index.md": "# Index\n",
        },
    )
    build(load_config(str(tmp_path / "mkdocs.yml"), strict=True))

    site = tmp_path / "site"
    assert [url for url in inventory["published"].values() if not (site / url.partition("#")[0]).is_file()] == []
    assert "Index" in (site / "notes/index.html").read_text(encoding="utf-8")
