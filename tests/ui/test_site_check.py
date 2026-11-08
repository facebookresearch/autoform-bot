from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli import site_check
from autoform_cli.__main__ import main
from autoform_cli.render import render_site
from autoform_cli.scaffold import scaffold_project
from autoform_cli.site_check import SiteCheckError, check_site


class _CountingPages(dict):
    def __init__(self, pages: dict) -> None:
        super().__init__(pages)
        self.scans = 0

    def __iter__(self):
        self.scans += 1
        return super().__iter__()

    def keys(self):
        self.scans += 1
        return super().keys()

    def values(self):
        self.scans += 1
        return super().values()

    def items(self):
        self.scans += 1
        return super().items()


def _site(root: Path, pages: dict[str, str]) -> Path:
    for name, body in pages.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def test_check_reports_dead_links_missing_anchors_and_links_that_leave_the_site(tmp_path: Path) -> None:
    site = _site(
        tmp_path / "site",
        {
            "index.html": (
                '<link rel="stylesheet" href="missing.css"><script src="missing.js"></script>'
                '<a href="a.html">a</a> <a href="a.html#kept">kept</a> <a href="a.html#named">named</a>'
                '<a href="a.html#node=ch%2Fx&amp;view=list">explorer</a> <a href="a.html#a%3Db">encoded</a>'
                '<a href="a.html#gone">gone</a>'
                '<a href="missing.html">missing</a> <a href="roadmap/ch/README.html#x">readme</a>'
                '<a href="../outside.html">outside</a> <img src="figure.svg"> <a href="#top">top</a>'
                '<a href="#nowhere">nowhere</a> <span id="top"></span> <a href="http://[oops/thesis">mirror</a>'
                # Render repeats a body link in a hidden copy that MkDocs validates.
                '<div class="bp-body-link-validation" hidden><a href="a.html#gone"></a></div>'
            ),
            "a.html": '<h2 id="kept">Kept</h2><a name="named"></a><a href="index.html#named">wrong page</a>',
            "roadmap/ch/index.html": '<div id="x"></div>',
        },
    )

    report = check_site(site)

    assert report.pages == 3
    assert report.issues == (
        "a.html: index.html#named names no anchor #named on index.html",
        "index.html: missing.css names missing.css, which the site does not contain",
        "index.html: missing.js names missing.js, which the site does not contain",
        "index.html: a.html#a%3Db names no anchor #a=b on a.html",
        "index.html: a.html#gone names no anchor #gone on a.html",
        "index.html: missing.html names missing.html, which the site does not contain",
        "index.html: roadmap/ch/README.html#x names roadmap/ch/README.html, which the site does not contain",
        "index.html: ../outside.html leaves the site",
        "index.html: figure.svg names figure.svg, which the site does not contain",
        "index.html: #nowhere names no anchor #nowhere on index.html",
        "index.html: http://[oops/thesis is not a valid URL",
    )


def test_check_reports_a_file_many_pages_lack_once(tmp_path: Path) -> None:
    head = '<link rel="stylesheet" href="{0}assets/site.css"><a href="{0}gone.html">gone</a>'
    site = _site(tmp_path / "site", {"a.html": head.format(""), "b.html": head.format(""), "sub/c.html": head.format("../")})

    assert check_site(site).issues == (
        "a.html: assets/site.css names assets/site.css, which the site does not contain; 2 other page(s) link to it too",
        "a.html: gone.html names gone.html, which the site does not contain; 2 other page(s) link to it too",
    )


def test_check_warns_about_a_page_or_source_it_cannot_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site = _site(tmp_path / "site", {"index.html": '<a href="locked.html#x">locked</a>', "locked.html": '<p id="x"></p>'})
    source = _site(tmp_path / "site-src", {"locked.md": "# Locked\n"})
    read_text = Path.read_text

    def locked_read_text(self: Path, *args, **kwargs) -> str:
        if self.stem == "locked":
            raise PermissionError(13, "Permission denied", str(self))
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked_read_text)

    report = check_site(site, source)

    assert report.pages == 1
    assert report.issues == (
        f"locked.html: could not be read: [Errno 13] Permission denied: '{site / 'locked.html'}'",
        f"locked.md: could not be read: [Errno 13] Permission denied: '{source / 'locked.md'}'",
    )


def test_check_skips_links_that_depend_on_where_the_site_is_served(tmp_path: Path) -> None:
    site = _site(
        tmp_path / "site",
        {
            "index.html": (
                '<a href="https://example.org/x.html#y">web</a> <a href="mailto:a@b.c">mail</a>'
                '<a href="//cdn.example.org/a.js">cdn</a> <a href="/root.html">root</a>'
                '<script src="javascript:void(0)"></script> <a href="">self</a> <a href="#">top</a>'
                '<a href="sub/">sub</a> <a href="sub/#in">in</a> <a href=".">here</a>'
                '<a href="a%20b.html#c%20d">spaced</a> <link rel="stylesheet" href="assets/site.css?v=1">'
            ),
            "sub/index.html": '<p id="in"></p>',
            "a b.html": '<p id="c d"></p>',
            "assets/site.css": "",
        },
    )

    assert check_site(site).issues == ()


def test_check_compares_each_page_with_the_cards_render_wrote(tmp_path: Path) -> None:
    card = '<div class="bp-thmwrapper theorem-style-plain bp-stated" id="{0}" data-autoform-node-id="ch/{0}" markdown="1">'
    source = _site(
        tmp_path / "site-src",
        {
            "roadmap/ch/README.md": "# Chapter\n\n" + "\n".join(card.format(name) for name in ("a", "b", "c")) + "\n",
            "roadmap/other.md": "# Other\n\n" + card.format("d") + "\n",
            "roadmap/gone.md": "# Gone\n\n" + card.format("e") + "\n",
            "notes.md": "# Notes\n\nNo cards.\n",
        },
    )
    built = '<div class="bp-thmwrapper theorem-style-plain bp-stated" id="{0}" data-autoform-node-id="ch/{0}"></div>'
    site = _site(
        tmp_path / "site",
        {
            "roadmap/ch/index.html": built.format("a"),
            "roadmap/other/index.html": built.format("d"),
            "notes.html": "<p>No cards.</p>",
        },
    )

    report = check_site(site, source)

    assert report.issues == (
        "roadmap/ch/index.html: 2 of the 3 statement card(s) in roadmap/ch/README.md did not survive the build: "
        "ch/b, ch/c",
        "roadmap/gone.md: no built page holds its 1 statement card(s)",
    )


def test_check_reads_each_page_once_and_never_scans_the_pages_for_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = 1_200
    pages = {
        f"page{index:04d}.html": f'<h1 id="p{index}">Page</h1><a href="page{(index + 1) % count:04d}.html#p{(index + 1) % count}">next</a>'
        for index in range(count)
    }
    site = _site(tmp_path / "site", pages)
    fed: list[int] = []
    feed = site_check._PageParser.feed

    def counting_feed(self, data: str) -> None:
        fed.append(len(data))
        feed(self, data)

    monkeypatch.setattr(site_check._PageParser, "feed", counting_feed)
    counted: list[_CountingPages] = []
    link_problem = site_check._link_problem

    def counting_link_problem(site: Path, pages: dict, *args):
        if not counted:
            counted.append(_CountingPages(pages))
        return link_problem(site, counted[0], *args)

    monkeypatch.setattr(site_check, "_link_problem", counting_link_problem)

    report = check_site(site)

    assert report.pages == count
    assert report.issues == ()
    assert len(fed) == count
    assert counted[0].scans == 0


def test_cli_prints_problems_as_warnings_and_never_fails_on_them(tmp_path: Path, capsys) -> None:
    site = _site(
        tmp_path / "site", {"index.html": '<a href="missing.html">missing</a><a href="#x&#10;warning: forged">x</a>'}
    )

    assert main(["site", "check", str(site)]) == 0
    assert capsys.readouterr().out == (
        f"{site}: 1 pages, 2 problem(s)\n"
        "warning: index.html: missing.html names missing.html, which the site does not contain\n"
        "warning: index.html: #x\\nwarning: forged names no anchor #xwarning: forged on index.html\n"
    )
    assert main(["site", "check", str(tmp_path / "absent")]) == 1
    assert capsys.readouterr().out == f"error: {tmp_path / 'absent'} is not a directory\n"
    with pytest.raises(SiteCheckError, match="is not a directory"):
        check_site(site, tmp_path / "no-source")


def test_check_reports_only_the_broken_anchor_in_a_rendered_project_mkdocs_built(tmp_path: Path) -> None:
    mkdocs_build = pytest.importorskip("mkdocs.commands.build")
    from mkdocs.config import load_config

    project = tmp_path / "project"
    scaffold_project(project, title="Site Check", repository_url="https://github.com/owner/repo")
    roadmap = project / "blueprint" / "roadmap"
    (roadmap / "README.md").write_text(
        "---\n---\n\n# Roadmap\n\nOne chapter.\n\n## Chapters\n\n- [Chapter](ch/README.md)\n", encoding="utf-8"
    )
    (roadmap / "ch").mkdir()
    (roadmap / "ch" / "README.md").write_text(
        "---\n---\n\n# Chapter\n\nThree results.\n\n## Results\n\n"
        "- [First](first.md)\n- [Second](second.md)\n- [Third](third.md)\n",
        encoding="utf-8",
    )
    article = (
        "---\ndeclaration: theorem\nstatement: formalized\n{proof}lean: Project.{name}\n---\n\n"
        "# {title}\n\n{body}\n\n## Depends on\n\n{depends}\n"
    )
    # Comparisons the site's Markdown conversion once read as tags, losing every later card on the page.
    first = "For $a<b>c$, use [the result][ref], not [this](#nowhere).\n\n## Sources\n\n[ref]: <second.md>"
    second = "Use only r<n.\n\n## Proof sketch\n\nFor $i<ell$, continue the proof."
    for name, proof, body, depends in (
        ("first", "proof: formalized\n", first, "This node has no prerequisites."),
        ("second", "proof: formalized\n", second, "- [First](first.md)"),
        ("third", "", "The last result.", "- [Second](second.md)"),
    ):
        (roadmap / "ch" / f"{name}.md").write_text(
            article.format(proof=proof, name=name, title=name.title(), body=body, depends=depends), encoding="utf-8"
        )
    render_site(project / "blueprint", project / "site-src")
    mkdocs_build.build(load_config(config_file=str(project / "mkdocs.yml")))

    report = check_site(project / "site", project / "site-src")

    landing = (project / "site-src/README.md").read_text(encoding="utf-8")
    assert '<div class="bp-next-target" data-autoform-node-id="ch/third">' in landing
    # The canonical theorem permalink reaches complete reading content and its
    # graph in one shell; the old explorer is only a compatibility entrance.
    published = (project / "site/index.html").read_text(encoding="utf-8")
    assert 'href="roadmap/ch/index.html#third"' in published
    assert 'class="bp-reading-source"' in published
    assert 'class="bp-dag-viewer"' in published
    chapter = (project / "site/roadmap/ch/index.html").read_text(encoding="utf-8")
    assert 'id="third"' in chapter and 'data-autoform-node-id="ch/third"' in chapter
    assert "The last result." in chapter
    alias = (project / "site/dependencies/chapters/ch.html").read_text(encoding="utf-8")
    assert 'data-page-href="../../roadmap/ch/index.html"' in alias
    assert "Continue to the mathematical page" in alias
    assert "The last result." not in alias
    assert report.pages > 0
    assert report.issues == ("roadmap/ch/index.html: #nowhere names no anchor #nowhere on roadmap/ch/index.html",)
