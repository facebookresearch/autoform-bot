from __future__ import annotations

import html
import json
import os
from pathlib import Path

import pytest

from autoform_cli import lean_probe
from autoform_cli.__main__ import main
from autoform_cli.scaffold import scaffold_project
from autoform_cli.ui import site_check
from autoform_cli.ui.mathjax import mathjax_script
from autoform_cli.ui.render import render_site
from autoform_cli.ui.site_check import SiteCheckError, check_site
from tests.dom_fixture import NODE
from tests.ui.test_site_mathjax_node import mathjax_package


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


def _need_node() -> None:
    if NODE is None:
        unavailable = pytest.fail if os.environ.get("AUTOFORM_REQUIRE_MATHJAX_TESTS") == "1" else pytest.skip
        unavailable("Node.js is not on PATH")


def _typesetting() -> Path:
    """The MathJax package ``AUTOFORM_MATHJAX_DIR`` names, checked as
    tests/ui/test_site_mathjax_node.py checks it, with Node.js on PATH; site
    check needs no DOM bridge."""

    package = mathjax_package(node=False)
    _need_node()
    return package


def _formula(tex: str, *, display: bool = False) -> str:
    """A formula as the site's Markdown marks it."""

    tex = html.escape(tex, quote=False)
    return f'<div class="arithmatex">\\[{tex}\\]</div>' if display else f'<span class="arithmatex">\\({tex}\\)</span>'


def _typeset_site(root: Path, pages: dict[str, str], macros: dict[str, object] | None = None) -> Path:
    """A built site of *pages*, with the ``javascripts/mathjax.js`` render writes for *macros*."""

    script, _ = mathjax_script(None, None if macros is None else json.dumps(macros).encode())
    return _site(root, {**pages, "javascripts/mathjax.js": script})


def _unpacked(package: Path, main: str = "") -> Path:
    """What site check takes for an unpacked MathJax 3.2.2 package, without one;
    *main* is its ``es5/node-main.js``."""

    (package / "es5").mkdir(parents=True)
    (package / "package.json").write_text('{"version": "3.2.2"}', encoding="utf-8")
    (package / "es5" / "node-main.js").write_text(main, encoding="utf-8")
    return package


def test_check_reports_each_formula_mathjax_rejects_once(tmp_path: Path) -> None:
    unbalanced = _formula("\\frac{1}{2")
    site = _typeset_site(
        tmp_path / "site",
        {
            "a.html": _formula("x^2 + y_1 < 3") + unbalanced,
            "b.html": unbalanced + _formula("\\aleph_1}", display=True),
            # The same formula, in a text with space around it, is one formula too.
            "c.html": '<span class="arithmatex"> \\(\\frac{1}{2\\) </span>',
        },
    )

    assert check_site(site, mathjax_dir=_typesetting()).issues == (
        "a.html: MathJax cannot typeset $\\frac{1}{2$: Missing close brace; 2 other page(s) have it too",
        "b.html: MathJax cannot typeset $$\\aleph_1}$$: Extra close brace or missing open brace",
    )


def test_check_names_a_command_mathjax_does_not_know_once(tmp_path: Path) -> None:
    # One missing macro is one fix, however many formulas use it.
    site = _typeset_site(
        tmp_path / "site",
        {
            "a.html": _formula("x") + _formula("\\llbracket a"),
            "b.html": _formula("\\llbracket b") + _formula("\\llbracket a"),
        },
    )

    assert check_site(site, mathjax_dir=_typesetting()).issues == (
        "a.html: \\llbracket is undefined, so the site shows it in red, first in $\\llbracket a$; "
        "it is in 2 formula(s) on 2 page(s): define it in tex-macros.json",
    )


def test_check_leaves_what_check_names_and_takes_the_project_macros(tmp_path: Path) -> None:
    # Check names the turned-off \\label and \\require, from no package the page loads; \\R is the project's.
    site = _typeset_site(
        tmp_path / "site",
        {"a.html": _formula("\\label{e} x") + _formula("\\require{cancel} y") + _formula("\\R") + _formula("\\nope")},
        macros={"R": "\\mathbb{R}"},
    )

    assert check_site(site, mathjax_dir=_typesetting()).issues == (
        "a.html: \\nope is undefined, so the site shows it in red, first in $\\nope$; "
        "it is in 1 formula(s) on 1 page(s): define it in tex-macros.json",
    )


def test_check_leaves_the_formulas_of_a_read_back_card_to_its_own_input(tmp_path: Path) -> None:
    # The testimony validator owns a card's formulas, which render marks for the card's input alone.
    inside = _formula("\\nope")
    card = (
        f'<div class="bp-readback bp-readback-current"><div>{inside}</div>'
        '<span class="bp-card-formula">\\(\\nope\\)</span></div>'
    )
    site = _typeset_site(tmp_path / "site", {"a.html": card + _formula("\\nopetoo")})

    assert check_site(site, mathjax_dir=_typesetting()).issues == (
        "a.html: \\nopetoo is undefined, so the site shows it in red, first in $\\nopetoo$; "
        "it is in 1 formula(s) on 1 page(s): define it in tex-macros.json",
    )


@pytest.mark.parametrize("stop", ["settings", "package", "node", "time", "crash", "output"])
def test_check_warns_once_and_still_passes_when_it_cannot_typeset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, stop: str
) -> None:
    pages = {"a.html": _formula("x") + _formula("y") + '<a href="gone.html">gone</a>'}
    site = _site(tmp_path / "site", pages) if stop == "settings" else _typeset_site(tmp_path / "site", pages)
    # Node.js runs a MathJax that never starts, or one that throws, named by a relative path as a local run gives it.
    main_js = {
        "time": "module.exports = {init: () => new Promise(() => setInterval(() => {}, 1000))};",
        "crash": 'throw new Error("not MathJax");',
    }.get(stop, "")
    monkeypatch.chdir(tmp_path)
    package = Path("mathjax") if stop == "package" else _unpacked(Path("mathjax"), main_js)
    if stop == "node":
        monkeypatch.setattr(lean_probe.shutil, "which", lambda name: None)
    if main_js:
        _need_node()
    if stop == "time":
        monkeypatch.setattr(site_check, "_TYPESET_START", 0.5)
        monkeypatch.setattr(site_check, "_TYPESET_PER_FORMULA", 0.0)
    if stop == "output":
        monkeypatch.setattr(lean_probe, "run_node", lambda arguments, **options: "MathJax 3.2.2\n[]")

    assert main(["site", "check", str(site), "--mathjax", str(package)]) == 0
    unchecked = "so the site's 2 distinct formula(s) were not typeset"
    warning = {
        "settings": f"javascripts/mathjax.js: holds no settings render wrote for MathJax 3.2.2, {unchecked}",
        "package": f"{package} is not an unpacked MathJax 3.2.2 package, {unchecked}",
        "node": f"Node.js is not on PATH, {unchecked}",
        "time": f"Node.js timed out after 0.5 seconds, {unchecked}",
        "crash": f"Node.js stopped: not MathJax, {unchecked}",
        "output": f"MathJax wrote no results site check can read, {unchecked}",
    }[stop]
    assert capsys.readouterr().out == (
        f"{site}: 1 pages, 2 problem(s)\n"
        "warning: a.html: gone.html names gone.html, which the site does not contain\n"
        f"warning: {warning}\n"
    )


def test_typesetting_gets_more_time_for_more_formulas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _unpacked(tmp_path / "mathjax")
    limits: list[float] = []

    def run_node(arguments: list[str], *, cwd: Path, timeout: float) -> str:
        limits.append(timeout)
        return "[]"

    monkeypatch.setattr(lean_probe, "run_node", run_node)

    for count in (1, 2_000):
        formulas = "".join(_formula(f"x_{{{n}}}") for n in range(count))
        site = _typeset_site(tmp_path / f"site{count}", {"a.html": formulas})
        assert check_site(site, mathjax_dir=package).issues == ()
    assert limits == [30.005, 40.0]
