"""The site's pages: the MathJax configuration ``autoform render`` writes,
the HTML, links, and attribute lists a published page may carry, the paths
render writes, and the styles that keep each formula in its place."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import pytest

from autoform_cli.__main__ import main
from autoform_cli.graph import load_graph
from autoform_cli.markdown import published_page, site_converter, statement_and_notes
from autoform_cli.mathjax import NEUTRALISED, mathjax_script
from autoform_cli.publishable import publishable_article, shown_as_typed
from autoform_cli.render import _GENERATED_FILES, PublicationError, _site_paths, render_site
from autoform_cli.scaffold import scaffold_project
from tests.test_render import _project

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = _ROOT / "skills/setup/assets/cabannes-thesis-project"
_BASE = "https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5"
_BUNDLE = f"{_BASE}/tex-mml-chtml.js"
_PAGE_PACKAGES = ["base", "ams", "noundefined", "boldsymbol", "mathtools", "braket", "mhchem", "amscd", "configmacros"]
_MACROS = {"RR": "\\mathbb{R}", "norm": ["\\left\\lVert #1 \\right\\rVert", 1]}
#: What the page's TeX input maps the commands it turns off to.
_OFF = {name: "\\notallowed" for name in NEUTRALISED}

#: The javascripts/mathjax.js ``autoform init`` wrote before render wrote its own.
_SCAFFOLDED = """window.MathJax = {
  tex: {
    inlineMath: [["$", "$"], ["\\\\(", "\\\\)"]],
    displayMath: [["$$", "$$"], ["\\\\[", "\\\\]"]]
  }
};
"""


def _vault(tmp_path: Path, *, macros: str | None = None, kept: str | None = None) -> Path:
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    # The file the fixture's Sources link names, which check requires.
    (blueprint / "sources.md").write_text("# Paper\n", encoding="utf-8")
    if macros is not None:
        (blueprint / "tex-macros.json").write_text(macros, encoding="utf-8")
    if kept is not None:
        (blueprint / "javascripts").mkdir()
        (blueprint / "javascripts/mathjax.js").write_bytes(kept.encode("utf-8"))
    return blueprint


def _render(blueprint: Path) -> Path:
    out = blueprint.parents[1] / "out"
    render_site(blueprint, out, lean_root=blueprint.parent)
    return out


def _warned(
    blueprint: Path, capsys: pytest.CaptureFixture[str], reason: str, *, stops_render: bool = False
) -> Path | None:
    """Check passes and warns naming ``reason``. Render then publishes the
    site, with the same warning, and returns it; or, ``stops_render``, it
    stops naming ``reason`` before writing anything."""

    assert main(["check", str(blueprint)]) == 0
    assert reason in capsys.readouterr().out
    out = blueprint.parents[1] / "out"
    if stops_render:
        with pytest.raises(PublicationError, match=re.escape(reason)):
            render_site(blueprint, out, lean_root=blueprint.parent)
        return None
    report = render_site(blueprint, out, lean_root=blueprint.parent)
    assert any(reason in warning for warning in report.warnings), report.warnings
    return out


def _settings(script: str) -> dict:
    """The settings object the generated script is configured by."""

    start = script.index("var SETTINGS = ") + len("var SETTINGS = ")
    return json.JSONDecoder().raw_decode(script, start)[0]


def _with_article(blueprint: Path, text: str) -> None:
    top = blueprint / "roadmap/top.md"
    top.write_text(top.read_text(encoding="utf-8").replace("The main result.", text), encoding="utf-8")


def test_render_writes_the_site_mathjax_configuration(tmp_path: Path) -> None:
    """The renderer owns the configuration and the release it loads."""

    script = (_render(_vault(tmp_path)) / "javascripts/mathjax.js").read_text(encoding="utf-8")
    settings = _settings(script)

    assert settings["version"] == "3.2.2"
    assert settings["base"] == _BASE
    assert settings["bundle"] == _BUNDLE
    assert settings["load"] == ["ui/safe", "[tex]/boldsymbol", "[tex]/mathtools", "[tex]/braket", "[tex]/mhchem", "[tex]/amscd"]
    assert settings["page"]["packages"] == _PAGE_PACKAGES
    assert settings["safe"] == {"allow": {"URLs": "none", "classes": "none", "cssIDs": "none", "styles": "none"}}


def test_the_page_turns_off_tex_that_reaches_past_its_own_formula() -> None:
    """Every article on a page is typeset with one TeX input, so a command
    that would change what the others show, or color a symbol like the
    site's status marks, is mapped to an undefined command, which shows in
    red; a project macro cannot take one of their names."""

    settings = _settings(mathjax_script(None, json.dumps(_MACROS).encode())[0])

    assert settings["page"]["macros"] == {**_MACROS, **_OFF}
    assert {"label", "mmlToken", "DeclareMathOperator", "newtagform", "mathtoolsset"} <= set(NEUTRALISED)


def test_mkdocs_lists_the_rendered_script_before_mathjax(tmp_path: Path) -> None:
    """A new project keeps no copy of the script, and mkdocs.yml lists the
    rendered script before the MathJax bundle, so the script configures
    MathJax before the bundle starts, and the bundle is the release the
    script checks for."""

    scaffold_project(tmp_path, title="Finite Flat")
    configuration = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")

    assert configuration.index("  - javascripts/mathjax.js\n") < configuration.index(f"  - {_BUNDLE}\n")
    assert not (tmp_path / "blueprint/javascripts/mathjax.js").exists()


def test_the_generated_script_is_valid_javascript(tmp_path: Path) -> None:
    """Nothing typesets a formula if the script does not parse, and the page
    shows no more than a console error."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    script = _render(_vault(tmp_path, macros=json.dumps(_MACROS))) / "javascripts/mathjax.js"

    result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_a_configuration_init_wrote_is_replaced_without_a_word(tmp_path: Path, newline: str) -> None:
    """A project scaffolded with its own copy needs no edits."""

    blueprint = _vault(tmp_path, kept=_SCAFFOLDED.replace("\n", newline))

    assert main(["check", str(blueprint)]) == 0
    script = (_render(blueprint) / "javascripts/mathjax.js").read_text(encoding="utf-8")
    assert _settings(script)["page"]["packages"] == _PAGE_PACKAGES


def test_an_edited_configuration_is_replaced_with_a_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Its edits are lost, so check and render say so, and the site gets the
    script render writes."""

    kept = _SCAFFOLDED.replace("  tex: {\n", '  tex: {\n    macros: {RR: "\\\\mathbb{R}"},\n')
    blueprint = _vault(tmp_path, kept=kept)

    site = _warned(blueprint, capsys, "move any tex.macros to tex-macros.json and delete it")
    assert _settings((site / "javascripts/mathjax.js").read_text(encoding="utf-8"))["page"]["macros"] == _OFF


def _changed_after_capture(monkeypatch: pytest.MonkeyPatch, change: Callable[[], object]) -> None:
    """Make every capture of the blueprint call ``change`` once it has read it."""

    import autoform_cli.render as render_module

    capture = render_module._capture_blueprint

    def capture_then_change(*args, **kwargs):
        snapshot = capture(*args, **kwargs)
        change()
        return snapshot

    monkeypatch.setattr(render_module, "_capture_blueprint", capture_then_change)


@pytest.mark.parametrize("later", ["{", json.dumps({"CC": "\\mathbb{C}"})], ids=["invalid", "different"])
def test_check_and_render_use_the_macros_they_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], later: str
) -> None:
    """Check judges the tex-macros.json it captured, and render builds the
    script from the one it captured and publishes, so a later change to the
    file reaches none of them."""

    blueprint = _vault(tmp_path, macros=json.dumps(_MACROS))
    macros = blueprint / "tex-macros.json"
    _changed_after_capture(monkeypatch, lambda: macros.write_text(later, encoding="utf-8"))

    assert main(["check", str(blueprint)]) == 0, capsys.readouterr().out
    macros.write_text(json.dumps(_MACROS), encoding="utf-8")
    site = _render(blueprint)

    published = json.loads((site / "tex-macros.json").read_text(encoding="utf-8"))
    assert published == _MACROS
    assert _settings((site / "javascripts/mathjax.js").read_text(encoding="utf-8"))["page"]["macros"] == {**published, **_OFF}


def test_macros_made_valid_after_capture_are_still_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid macros captured are left out, with a warning, even if the file
    is fixed before the script is built, so the script always matches the
    tex-macros.json published beside it."""

    blueprint = _vault(tmp_path, macros="{")
    macros = blueprint / "tex-macros.json"
    _changed_after_capture(monkeypatch, lambda: macros.write_text(json.dumps(_MACROS), encoding="utf-8"))

    assert main(["check", str(blueprint)]) == 0
    assert "warning: tex-macros.json: not valid JSON" in capsys.readouterr().out
    macros.write_text("{", encoding="utf-8")
    report = render_site(blueprint, blueprint.parents[1] / "out", lean_root=blueprint.parent)
    site = report.output_dir
    assert any("tex-macros.json: not valid JSON" in warning for warning in report.warnings)
    assert (site / "tex-macros.json").read_text(encoding="utf-8") == "{"
    assert _settings((site / "javascripts/mathjax.js").read_text(encoding="utf-8"))["page"]["macros"] == _OFF


# Deeper than the JSON decoder goes on Python 3.10 to 3.13. From 3.14 it goes as
# deep as the stack allows, about 37,000 levels in 8 MiB.
_NESTED = '{"RR": ' + "[" * 100_000 + "]" * 100_000 + "}"


@pytest.mark.parametrize(
    ("macros", "reason"),
    [
        ("{", "tex-macros.json: not valid JSON"),
        pytest.param(_NESTED, "tex-macros.json: not valid JSON", id="nested"),
        ('{"RR": "x", "RR": "y"}', "'RR' is defined twice"),
        ('["RR"]', "tex-macros.json: must be a JSON object from macro names to definitions"),
        ('{"R R": "x"}', "'R R' is not a macro name; use letters only"),
        ('{"label": "x"}', "\\label is a command the site turns off; choose another name"),
        ('{"notallowed": "x"}', "\\notallowed is a command the site turns off; choose another name"),
        ('{"f": ["#1", 10]}', "\\f takes 10 arguments; use 0 to 9"),
        ('{"f": ["#1", true]}', "\\f must be a body, [body, arguments], or [body, arguments, default]"),
    ],
)
def test_project_macros_that_cannot_be_used_are_left_out(macros: str, reason: str) -> None:
    """Check and render warn about these, as the edited and the captured
    configurations above show."""

    assert any(reason in issue for issue in mathjax_script(None, macros.encode())[1])


def test_the_macros_that_can_be_used_are_kept_beside_one_left_out() -> None:
    """One bad macro does not take the project's other notation off the site."""

    script, warnings = mathjax_script(None, json.dumps({**_MACROS, "R R": "x", "label": "y"}).encode())

    assert len(warnings) == 2
    assert _settings(script)["page"]["macros"] == {**_MACROS, **_OFF}


def _directory_with_a_file(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "kept.txt").write_text("x\n", encoding="utf-8")


def _symlink(path: Path) -> None:
    """A link to a shipped configuration outside the vault."""

    path.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = path.parent / f"../../{path.name}.elsewhere"
    elsewhere.write_text(_SCAFFOLDED, encoding="utf-8")
    path.symlink_to(elsewhere.resolve())


def _fifo(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)


def _file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")


def test_the_guarded_paths_are_every_file_render_writes(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path)

    site = _render(blueprint)

    written = {
        path.relative_to(site).as_posix()
        for path in site.rglob("*")
        if path.is_file() and not (blueprint / path.relative_to(site)).is_file()
    }
    assert written <= set(_site_paths(load_graph(blueprint)))


@pytest.mark.parametrize("make", [_directory_with_a_file, _symlink, _fifo], ids=["directory", "symlink", "fifo"])
def test_something_other_than_a_file_where_render_writes_one_stops_render(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], make
) -> None:
    """One rule covers every file render writes: a directory there, captured
    with what it holds, is named by check's warnings, and render stops before
    writing anything. A directory where the macros file goes only leaves the
    site without project macros. Capture refuses a symlink or special file
    anywhere but at a name it keeps out of the site, which it never reads."""

    blueprint = _vault(tmp_path)
    # A chapter page render writes over the vault's own is the vault's to keep.
    paths = [path for path in (*_site_paths(load_graph(blueprint)), "tex-macros.json") if not (blueprint / path).exists()]
    for relative in paths:
        make(blueprint / relative)
    expected = {
        _directory_with_a_file: lambda relative: (
            "tex-macros.json: is a directory, so the site has no project macros; make it a JSON file"
            if relative == "tex-macros.json"
            else f"{relative}: is a directory, where autoform render writes a file; remove it"
        ),
        _symlink: lambda relative: f"refusing symlink in blueprint publication: {relative}",
        _fifo: lambda relative: f"refusing non-regular blueprint publication input: {relative}: ",
    }[make]

    assert main(["check", str(blueprint)]) == 0
    out = capsys.readouterr().out
    assert [
        relative
        for relative in paths
        if not (make in (_symlink, _fifo) and relative in _GENERATED_FILES) and expected(relative) not in out
    ] == []
    with pytest.raises(PublicationError):
        _render(blueprint)


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (_directory_with_a_file, "dependencies.md: is a directory, where autoform render writes a file; remove it"),
        (_file, "dependencies: is a file, where autoform render needs a folder for dependencies/"),
    ],
    ids=["directory", "file"],
)
def test_what_render_captured_in_the_way_stops_it_once_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make, reason: str
) -> None:
    """Render copies what it captured, so a directory or file it captured in
    the way of a page it writes stops it even when the disk no longer has
    it by the time the paths are checked."""

    blueprint = _vault(tmp_path)
    blocking = blueprint / reason.split(":")[0]
    make(blocking)
    _changed_after_capture(monkeypatch, lambda: shutil.rmtree(blocking) if blocking.is_dir() else blocking.unlink())

    with pytest.raises(PublicationError, match=re.escape(reason)):
        _render(blueprint)


@pytest.mark.parametrize("folder", ["dependencies", "dependencies/chapters", "javascripts", "stylesheets", "assets"])
def test_a_file_where_render_needs_a_folder_stops_render(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], folder: str
) -> None:
    blueprint = _vault(tmp_path)
    _file(blueprint / folder)

    _warned(blueprint, capsys, f"{folder}: is a file, where autoform render needs a folder for {folder}/", stops_render=True)


_ATTRIBUTE_RULE = "is not allowed: it would give the text an id, a class, or a style of its own; delete it"
#: An article's frontmatter, so the lines named count it as the file does.
_TOP = "---\ndeclaration: theorem\n---\n\n# Top\n\n"


@pytest.mark.parametrize(
    ("article", "reason"),
    [
        ("First.\n\nThen <b>bold</b>.", "line 9: raw HTML is not allowed: <b>, </b>;"),
        ("First.\n\nA remark.\n{: .bp-mark }", f"line 10: attribute list {{: .bp-mark }} {_ATTRIBUTE_RULE}"),
        ("First.\n\nSee [x](javascript:alert(1)).", "line 9: links are not allowed to javascript: addresses"),
        pytest.param(
            "First.\n\nThe &#" + "9" * 39000 + ";.",
            "line 9: HTML character references are not allowed: &#99999999999999... (39003 characters)",
            id="long-reference",
        ),
    ],
)
def test_a_warning_names_the_line_of_the_article(article: str, reason: str) -> None:
    assert any(error.startswith(reason) for error in publishable_article(f"{_TOP}{article}\n")[1])


@pytest.mark.parametrize("frontmatter", ["---\n---\n\n", ""], ids=["frontmatter", "none"])
def test_a_warning_in_a_chapter_names_the_chapter_and_the_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], frontmatter: str
) -> None:
    """Check reads a chapter whole and in the stretches between its
    statements, and both readings name the line in the file, so a finding is
    reported once, whatever the header render puts in place of the
    frontmatter."""

    blueprint = _vault(tmp_path)
    chapter = blueprint / "roadmap/README.md"
    body = chapter.read_text(encoding="utf-8").removeprefix("---\n---\n\n")
    chapter.write_text(f"{frontmatter}{body}\nLast, <b>x</b>.\n", encoding="utf-8")
    line = len(chapter.read_text(encoding="utf-8").splitlines())

    assert main(["check", str(blueprint)]) == 0
    warned = [found for found in capsys.readouterr().out.splitlines() if found.startswith("warning: roadmap:")]
    assert warned == [
        f"warning: roadmap: line {line}: raw HTML is not allowed: <b>, </b>; {_RAW_HTML_FIX}",
        "warning: roadmap: the site shows it as typed until these are fixed",
    ]


@pytest.mark.parametrize(
    "article",
    [
        # TeX is the page's MathJax's to judge: a command it turns off shows in red.
        "$\\DeclareMathOperator{\\leq}{>}$, $x \\label{one}$, and $\\mmlToken{mi}{x}$ are shown in red.",
        "Prose that names \\newcommand outside a formula is shown as typed.",
        "$\\left( x \\right) \\leqq y$, $\\operatorname{rank} A$, and $\\lvert x \\rvert$.",
        # Code is escaped, and a reference that names no character is shown as written.
        "A `List<T>` holds values of type `T`.",
        "```\n<b>bold</b> &amp; <!-- kept -->\n```",
        "Spent on R&D; and AT&T, as `&amp;` shows.",
        "<!-- a note to the authors -->",
        "For $a < b$ and $b > c$, AT&T.",
        "```lean\ntheorem lt : (1 : Nat) < 2 := by decide\n```",
        "```{.lean}\nx\n```",
        "``` { .lean }\nx\n```",
        '```lean title="Main.lean" linenums="1"\nx\n```',
        '```{.lean hl_lines="1"}\nx\n```',
        # An authored diagram is drawn.
        "```mermaid\ngraph LR\n  A --> B\n```",
        "Render draws the `mermaid` graphs.",
    ],
)
def test_what_an_article_may_still_say_is_published_as_written(article: str) -> None:
    assert publishable_article(f"# Top\n\n{article}\n")[1] == ()


@pytest.mark.parametrize(
    ("article", "named", "line"),
    [
        ("$\\newcommand{\\RR}{\\mathbb{R}} \\RR$ and $x \\label{one}$.", "\\newcommand, \\label", 3),
        ("$$\n\\DeclareMathOperator{\\rank}{rank}\n$$", "\\DeclareMathOperator", 4),
        ("$\\def\\x{1} \\x$", "\\def", 3),
    ],
)
def test_a_formula_that_defines_notation_is_reported_apart(article: str, named: str, line: int) -> None:
    """The site ignores notation an article defines, so such a formula shows
    an error in red; the page itself is published as written. Prose that
    names a command no formula uses is not reported; prose naming one a
    formula uses is reported on its own line too, so the prose here names
    another."""

    _page, errors, notation = publishable_article(f"# Top\n\n{article}\n\nProse names \\gdef.\n")

    assert errors == ()
    assert notation == (
        f"line {line}: formulas define or use {named}: the site ignores notation an article defines and turns these "
        "commands off, so each shows an error in red; put the project's notation in blueprint/tex-macros.json",
    )


@pytest.mark.parametrize(
    ("article", "reason"),
    [
        ("Code `<b>` and <b>live</b>.", "raw HTML is not allowed: <b>, </b>;"),
        ("Code `&amp;` and &amp; live.", "HTML character references are not allowed: &amp; (U+0026 AMPERSAND);"),
        ("Not &notit; at all.", "HTML character references are not allowed: &notit;;"),
    ],
)
def test_code_excuses_only_the_markup_it_shows(article: str, reason: str) -> None:
    errors = publishable_article(article)[1]

    assert any(error.startswith(f"line 1: {reason}") for error in errors), errors


_RAW_HTML_FIX = (
    "write it in Markdown (a blank line or a trailing backslash for a break, a heading or list for a section), "
    "or in code to show it as typed; in a formula, put a space after <"
)


@pytest.mark.parametrize(
    ("relative", "written", "expected"),
    [
        ("README.md", "# Project\n\nFirst line<br>second line.\n", f"README.md: line 3: raw HTML is not allowed: <br>; {_RAW_HTML_FIX}"),
        ("base", "A remark.\n{.bp-statement}", f"base: line 14: attribute list {{.bp-statement}} {_ATTRIBUTE_RULE}"),
    ],
)
def test_a_warning_names_the_page_and_says_how_to_write_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], relative: str, written: str, expected: str
) -> None:
    """The fix for raw HTML says how Markdown writes what HTML is reached for."""

    blueprint = _vault(tmp_path)
    if relative == "base":
        _with_base_notes(blueprint, written)
    else:
        (blueprint / relative).write_text(written, encoding="utf-8")

    assert main(["check", str(blueprint)]) == 0
    assert f"warning: {expected}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("article", "names"),
    [
        ("<?x <script>alert(1)</script>", "<?x, <script>, </script>"),
        ("<!x <script>", "<!x, <script>"),
        ("\\<- ?<!DOCTYPE<?php</div>\"$&#1234567890;$<x y='1'>", "<!DOCTYPE, <?php, </div>, <x>"),
    ],
)
def test_tags_after_a_declaration_on_the_same_line_are_named(article: str, names: str) -> None:
    errors = publishable_article(article)[1]

    assert f"line 1: raw HTML is not allowed: {names}; {_RAW_HTML_FIX}" in errors


@pytest.mark.parametrize(
    "article",
    [
        "Intro.\n\nLet $a<b$ hold.",
        "Intro.\n\n$$\\sum_{i<n} x$$",
        "Intro.\n\n$$\n\\sum_{i<n}\n$$",
        "Intro.\n\nFor $a<b>c$, and only r<n.",
    ],
)
def test_a_comparison_in_a_statement_is_published_as_written(article: str) -> None:
    """Render converts a statement holding a < on its own before boxing it,
    and the converter reads a formula first, so neither a comparison in a
    formula nor a bare r<n opens a tag; a formula's text excuses the markup
    it spells, as code does."""

    assert publishable_article(article)[1] == ()


@pytest.mark.parametrize(
    ("reference", "shown"),
    [
        ("&#" + "9" * 39000 + ";", "&#99999999999999... (39003 characters) (U+FFFD REPLACEMENT CHARACTER)"),
        ("&#" + "0" * 5000 + "65;", "&#00000000000000... (5005 characters) (U+0041 LATIN CAPITAL LETTER A)"),
        ("&#x" + "f" * 39000 + ";", "&#xfffffffffffff... (39004 characters) (U+FFFD REPLACEMENT CHARACTER)"),
    ],
)
def test_a_long_character_reference_is_named_short(reference: str, shown: str) -> None:
    """Python converts no number of more than 4300 digits, and a message that
    repeats one is no use."""

    errors = publishable_article(f"A reference {reference} here.")[1]

    assert errors == (f"line 1: HTML character references are not allowed: {shown}; type the character itself",)


@pytest.mark.parametrize(
    ("markup", "reason"),
    [
        ("Some <span style='display:none'>hidden</span> text.", "raw HTML is not allowed: <span>, </span>;"),
        ("Fish &amp; chips.", "HTML character references are not allowed: &amp; (U+0026 AMPERSAND);"),
        ("A zero width&#8203 space.", "HTML character references are not allowed: &#8203 (U+200B ZERO WIDTH SPACE);"),
        ("<!-- a note that never ends", "HTML comments are not allowed"),
        ("<img/src=x onerror=alert(1)>", "raw HTML is not allowed: the site would publish '<img/src=x"),
        ("<div markdown>x</div>", "raw HTML is not allowed: <div>, </div>;"),
        ("<script>alert(1)</script>", "raw HTML is not allowed: <script>, </script>;"),
        ("&lt;", "HTML character references are not allowed: &lt; (U+003C LESS-THAN SIGN);"),
        ("Code `<b>` is shown as typed.", None),
    ],
)
def test_markdown_the_site_converter_renders_is_checked_for_raw_html(markup: str, reason: str | None) -> None:
    """Markdown the site's own converter renders, which reads HTML, is reported
    for any HTML in it outside code, which it shows as typed, and for a
    character reference without its semicolon, which that converter completes."""

    errors = publishable_article(markup)[1]

    assert errors == () if reason is None else any(error.startswith(f"line 1: {reason}") for error in errors)


_FORGED = '<span class="bp-mark">FORGED MARK</span><script>document.title="INJECTED"</script>'


def _published(page: Path) -> str:
    """The complete mathematical content as MkDocs publishes it.

    Validate the one trusted renderer-owned script outside the reader, then
    inspect the entire reading subtree. Authored scripts remain forbidden;
    adding a shared application host cannot weaken those inertness checks.
    """

    import html5lib
    from mkdocs.utils.meta import get_data

    rendered = site_converter().convert(get_data(page.read_text(encoding="utf-8"))[0])
    tree = html5lib.parseFragment(rendered, namespaceHTMLElements=False)
    readers = [node for node in tree.iter() if "bp-reading-source" in node.get("class", "").split()]
    assert len(readers) == 1
    assert list(readers[0].iter("script")) == []
    scripts = list(tree.iter("script"))
    assert len(scripts) == 1
    root = next(parent for parent in page.parents if (parent / "publication.json").is_file())
    relative_script = Path(os.path.relpath(root / "javascripts/blueprint-dag.js", page.parent)).as_posix()
    assert scripts[0].attrib == {"defer": "defer", "src": quote(relative_script, safe="/%")}
    assert not list(scripts[0]) and not (scripts[0].text or "").strip()
    return html5lib.serialize(readers[0], omit_optional_tags=False, quote_attr_values="always")


def test_text_shown_as_typed_is_inert_on_the_site() -> None:
    """Everything in a page or a box published as typed is text: no tag,
    link, emphasis, or formula survives the site's converter, and blank lines
    stay inside the one block."""

    page = shown_as_typed(publishable_article(f"# T\n\n{_FORGED}\n\n\n*emph* [l](javascript:x) $a<b$\n")[0])
    box = shown_as_typed(f"Statement {_FORGED}", page=False)

    assert page.startswith("---\n{}\n---\n")
    for published in (site_converter().convert(page.removeprefix("---\n{}\n---\n")), site_converter().convert(box)):
        assert "<script" not in published and "<em>" not in published and "href" not in published
        assert published.count('<pre class="bp-as-typed">') == 1
        assert '<p class="bp-as-typed-note">' in published


def test_a_statement_keeps_the_indent_check_read_it_with(tmp_path: Path) -> None:
    """A statement that opens with an indented code block is code in its box
    too, as check read it, not the HTML its text spells out."""

    blueprint = _vault(tmp_path)
    _with_article(blueprint, f"    {_FORGED}\n\nEvery object is equal to itself.")

    assert main(["check", str(blueprint)]) == 0
    published = _published(_render(blueprint) / "roadmap/README.md")

    assert "<script>" not in published
    assert '<span class="bp-mark">FORGED' not in published
    assert '<pre><code>&lt;span class="bp-mark"&gt;FORGED MARK' in published


#: A link whose target, decoded, ends the link and spells out a script and a
#: status mark.
_ENCODED_MARKUP = (
    "see [here](%29%3Cscript%3Edocument.title%3D1%3C/script%3E"
    "%3Cspan%20class%3D%22bp-mark%22%3EFORGED%3C/span%3E.md)."
)


def test_a_link_whose_target_decodes_to_markup_publishes_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Render resolves a relative link's target decoded, as the file system
    names it, and writes it back. Check reads each page with its links moved,
    so a target that decodes to a parenthesis and tags, which would end the
    link and spell out a script and a status mark, is reported, and the site
    shows the page as typed."""

    blueprint = _vault(tmp_path)
    (blueprint / "notes.md").write_text(f"# Notes\n\nThe notes, {_ENCODED_MARKUP}\n", encoding="utf-8")

    site = _warned(blueprint, capsys, "notes.md: the site shows it as typed until these are fixed")
    notes = _published(site / "notes.md")
    assert "<script" not in notes and "FORGED</span>" not in notes
    assert '<pre class="bp-as-typed">' in notes


def _with_base_notes(blueprint: Path, notes: str) -> None:
    base = blueprint / "roadmap/base.md"
    base.write_text(
        base.read_text(encoding="utf-8").replace("The base object.", f"The base object.\n\n## Remarks\n\n{notes}"),
        encoding="utf-8",
    )


def _with_narrative(blueprint: Path, text: str) -> None:
    chapter = blueprint / "roadmap/README.md"
    chapter.write_text(
        chapter.read_text(encoding="utf-8").replace("## Definitions\n\n- [Base](base.md)", text), encoding="utf-8"
    )


@pytest.mark.parametrize("fence", ["````", "```", "~~~~"])
def test_a_fence_left_open_cannot_run_into_the_next_article(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fence: str
) -> None:
    """Two leaves share their chapter's page, and the site reads fences over
    the whole page: a fence one leaves open would run through render's own
    markup, its status marks included, and end at the next leaf's first
    fence, so the code that leaf was checked with would be published live.
    Check reports the open fence, by its line, and the site shows that
    leaf's box as typed, so the other leaf's code stays code."""

    blueprint = _vault(tmp_path)
    _with_base_notes(blueprint, f"{fence}\n")
    _with_article(blueprint, f"The main result.\n\n{fence}\n{_FORGED} $\\gdef\\x{{1}}$\n{fence}")

    site = _warned(blueprint, capsys, "base: line 13: code fence is not closed")
    published = _published(site / "roadmap/README.md")
    assert "<script" not in published and '<span class="bp-mark">FORGED' not in published
    assert published.count('<span class="bp-mark"') == 2
    assert published.count('<pre class="bp-as-typed">') == 1
    # The box shows its statement and notes as typed, nothing the site adds.
    box = published[published.index('<pre class="bp-as-typed">') :].split("</pre>")[0]
    assert box.startswith('<pre class="bp-as-typed">The base object.') and "{}" not in box and "# Base" not in box


def test_a_chapter_is_read_in_the_stretches_its_page_has(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Render replaces a slot with its statement, so the narrative on either
    side of it is read apart: a code span that crosses the slot in the
    narrative as written ends before it on the page, and what it held
    would be published as markup. Check reads each stretch as the page has
    it."""

    blueprint = _vault(tmp_path)
    _with_narrative(blueprint, f"## Definitions\n\n`a\n- [Base](base.md)\nb` and `c {_FORGED} d`")

    site = _warned(blueprint, capsys, "roadmap: line 12: raw HTML is not allowed: <span>, </span>, <script>")
    published = _published(site / "roadmap/README.md")
    assert "<script" not in published and '<span class="bp-mark">FORGED' not in published


def test_a_chapter_cannot_leave_a_fence_open_over_its_statements(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fence the narrative leaves open before a slot would run on into the
    statement render puts there."""

    blueprint = _vault(tmp_path)
    _with_narrative(blueprint, "## Definitions\n\n````\n\n- [Base](base.md)")

    assert main(["check", str(blueprint)]) == 0
    assert "warning: roadmap: line 10: code fence is not closed" in capsys.readouterr().out


@pytest.mark.parametrize("notes", ["> ```\n> quoted\n", "- item\n\n    ```\n    indented\n"])
def test_a_fence_the_next_margin_line_ends_is_published(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], notes: str
) -> None:
    """An indented or quoted fence ends at the first line at the margin, and
    render's markup starts there, so leaving one open, in a leaf's notes or
    at the end of a page, hides nothing."""

    blueprint = _vault(tmp_path)
    _with_base_notes(blueprint, notes)
    (blueprint / "notes.md").write_text(f"# Notes\n\n{notes}", encoding="utf-8")

    assert main(["check", str(blueprint)]) == 0
    assert "code fence is not closed" not in capsys.readouterr().out
    site = _render(blueprint)
    published = _published(site / "roadmap/README.md")
    assert published.count('<span class="bp-mark"') == 2
    assert "bp-as-typed" not in published + (site / "notes.md").read_text(encoding="utf-8")


def test_a_statement_after_a_paragraph_is_a_box_of_its_own(tmp_path: Path) -> None:
    """A slot right below a line of prose is replaced by a statement set
    apart by blank lines, so the statement is its own box on the page, not
    a run of the paragraph."""

    blueprint = _vault(tmp_path)
    _with_narrative(blueprint, "## Definitions\n\nThe base object comes first:\n- [Base](base.md)\nand then the rest.")

    published = _published(_render(blueprint) / "roadmap/README.md")

    assert "<p>The base object comes first:</p>" in published
    assert "<p>and then the rest.</p>" in published
    assert '<div class="bp-thmcontent">\n<p>The base object.</p>\n</div>' in published


_RUNNABLE = {
    "help.html": b"<script>alert(document.cookie)</script>",
    "figures/plot.svg": b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    "sw.js": b"self.addEventListener('fetch', () => {});",
    "figures/page.XHTML": b"<html xmlns='http://www.w3.org/1999/xhtml'><script>1</script></html>",
    "stylesheets/extra.css": b".af-status { display: none }",
    "LICENSE": b"<script>1</script>",
}
_SHOWN = {
    "figures/plot.PNG": b"\x89PNG\r\n\x1a\n",
    "figures/photo.jpeg": b"\xff\xd8\xff",
    "paper.pdf": b"%PDF-1.7\n",
    "refs.bib": b"@book{x, title={X}}\n",
    "notes.txt": b"plain\n",
}


def test_only_a_file_a_browser_only_shows_is_published(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Render copies a vault's other files into the site as they are, so an
    HTML page, an SVG or a script there would be the site's own markup the
    moment a reader followed a link to it. The site leaves out any file not
    of a kind a browser only shows, and check and render name each by its
    path; a figure, a PDF, a bibliography, plain text, and a copy of an asset
    render writes over are published."""

    blueprint = _vault(tmp_path)
    (blueprint / "stylesheets").mkdir()
    (blueprint / "stylesheets/blueprint.css").write_text(".af-status { display: none }", encoding="utf-8")
    for relative, content in {**_RUNNABLE, **_SHOWN}.items():
        (blueprint / relative).parent.mkdir(parents=True, exist_ok=True)
        (blueprint / relative).write_bytes(content)

    assert main(["check", str(blueprint)]) == 0
    left_out = {line.split(": ")[1] for line in capsys.readouterr().out.splitlines() if "the site leaves out " in line}
    assert left_out == set(_RUNNABLE)
    site = _render(blueprint)
    assert not any((site / relative).exists() for relative in _RUNNABLE)
    assert all((site / relative).read_bytes() == content for relative, content in _SHOWN.items())
    assert ".af-status" not in (site / "stylesheets/blueprint.css").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("where", "written", "scheme"),
    [
        ("base", "See [the proof](javascript:alert(document.cookie)).", "javascript"),
        ("base", "See [the proof](JaVaScRiPt:alert(1)).", "javascript"),
        ("base", 'See [the proof](javascript:alert(1) "a title").', "javascript"),
        ("base", "![a figure](javascript:alert(1))", "javascript"),
        ("base", "See [the proof][p].\n\n[p]: data:text/html,x", "data"),
        ("base", "Nothing uses it here.\n\n[p]: <vbscript:x> 'a title'", "vbscript"),
        ("notes.md", "# Notes\n\nSee [the proof][p].\n\n[p]: DATA:text/html,x\n", "data"),
        ("roadmap", "## Definitions\n\n[home](javascript:alert(1))\n\n- [Base](base.md)", "javascript"),
    ],
)
def test_a_link_that_could_run_script_is_never_published(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], where: str, written: str, scheme: str
) -> None:
    """A link to a ``javascript:`` or ``data:`` address runs what it holds on
    the site's origin when a reader follows it, whatever the case of its
    scheme, and a link definition no link on its own text uses may be used
    by another text on the page. Check reports every link and definition
    that leads anywhere but the site's own pages and http, https, and
    mailto addresses, and the site shows its page as typed."""

    blueprint = _vault(tmp_path)
    if where == "base":
        _with_base_notes(blueprint, written)
    elif where == "roadmap":
        _with_narrative(blueprint, written)
    else:
        (blueprint / where).write_text(written, encoding="utf-8")

    assert main(["check", str(blueprint)]) == 0
    out = capsys.readouterr().out
    assert re.search(rf"{re.escape(where)}: lines? \d+.*: links are not allowed to {scheme}: addresses", out), out
    page = _published(_render(blueprint) / ("roadmap/README.md" if where in {"base", "roadmap"} else where))
    assert f'="{scheme}' not in page.lower()


@pytest.mark.parametrize(
    "written",
    ["[x](<javascript:alert(1)>)", "[x]( \x01javascript:alert(1))", "[x][r]\n\n[r]:\n  JAVASCRIPT:1", "[x](java&#x09;script:1)"],
)
def test_an_article_cannot_hide_a_link_scheme(written: str) -> None:
    """However the destination is written, its scheme is read as a browser
    reads it."""

    assert any("links are not allowed to javascript:" in issue for issue in publishable_article(written)[1])


def test_a_link_to_a_page_or_a_web_address_is_published(tmp_path: Path) -> None:
    """Links to http, https, and mailto addresses, to the site's own pages
    and their headings, and an address shown in code all pass."""

    blueprint = _vault(tmp_path)
    _with_base_notes(
        blueprint,
        "See [the paper](https://example.org/p.pdf), [mirror](HTTP://example.org), "
        "[mail](mailto:a@example.org), <https://example.org/x>, [top](top.md#the-main-result), "
        "[here](#remarks), and `javascript:void(0)`.\n\n[d]: https://example.org/d",
    )

    assert main(["check", str(blueprint)]) == 0
    html = _published(_render(blueprint) / "roadmap/README.md")
    assert 'href="https://example.org/p.pdf"' in html
    assert 'href="mailto:a@example.org"' in html
    assert "<code>javascript:void(0)</code>" in html


def test_check_judges_the_text_render_publishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check judges each page as render writes it, with its links moved, so
    what render publishes is what check saw."""

    import autoform_cli.render as render_module

    judged: list[str] = []

    def recording(text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        judged.append(text)
        return publishable_article(text)

    blueprint = _vault(tmp_path)
    _with_article(blueprint, "See [the base](base.md).")
    (blueprint / "notes.md").write_text("# Notes\n\nSee [the result](roadmap/top.md).\n", encoding="utf-8")
    monkeypatch.setattr(render_module, "publishable_article", recording)

    assert main(["check", str(blueprint)]) == 0

    assert any("See [the base](#base)." in text for text in judged)
    assert any("See [the result](roadmap/README.md#top)." in text for text in judged)
    assert not any("base.md" in text or "roadmap/top.md" in text for text in judged)


_UNSAFE_PAGE = "<script>document.title='X'</script>\n\nText\n{: .bp-mark .bp-thmcontent }\n"


@pytest.mark.parametrize("relative", ["README.md", "coverage/README.md", "notes.md", "more/notes.mdown"])
def test_a_page_that_is_not_an_article_is_held_to_the_same_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], relative: str
) -> None:
    """The landing page, the coverage contract, and any other Markdown page
    are published on the site's origin as articles are, so check reports
    script and attribute lists in them, naming the file, and the site shows
    them as typed."""

    blueprint = _vault(tmp_path)
    page = blueprint / relative
    page.parent.mkdir(parents=True, exist_ok=True)
    before = page.read_text(encoding="utf-8") if page.exists() else "# Notes\n"
    page.write_text(f"{before.rstrip()}\n\n{_UNSAFE_PAGE}", encoding="utf-8")

    assert main(["check", str(blueprint)]) == 0
    out = capsys.readouterr().out
    lines = len(before.rstrip().splitlines()) + 2
    assert f"warning: {relative}: line {lines}: raw HTML is not allowed: <script>, </script>" in out
    assert f"warning: {relative}: line {lines + 3}: attribute list {{: .bp-mark .bp-thmcontent }}" in out
    shown = "the landing page shows only its title and lead, as text" if relative == "README.md" else (
        "the site shows it as typed until these are fixed"
    )
    assert f"warning: {relative}: {shown}" in out
    published = (_render(blueprint) / relative).read_text(encoding="utf-8")
    assert "<script>" not in published
    if relative != "README.md":
        assert "&lt;script&gt;document.title='X'&lt;/script&gt;" in published
    else:
        # The landing page keeps only the title and the lead of the author's
        # text, around its dashboard, and the hero escapes both once.
        from mkdocs.utils.meta import get_data

        assert get_data(published)[1]["title"] == "Overview"
        assert "Text {: .bp-mark .bp-thmcontent }." in published
        assert "pre&gt;" not in published


def test_a_link_to_what_capture_leaves_out_keeps_only_its_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #415: a page outside roadmap/ that linked into an unpublished folder,
    or to a hidden file, published a link the strict build could not resolve,
    and check, whose graph holds the folder's articles, said nothing."""

    blueprint = _vault(tmp_path)
    drafts = blueprint / "roadmap/drafts"
    drafts.mkdir()
    (drafts / "README.md").write_text("---\npublished: false\n---\n\n# Drafts\n", encoding="utf-8")
    (blueprint / ".private").mkdir()
    (blueprint / ".private/plan.md").write_text("# Plan\n", encoding="utf-8")
    targets = ("roadmap/drafts/README.md", ".private/plan.md", "roadmap/drafts/figure.png")
    (blueprint / "notes.md").write_text(
        f"# Notes\n\nSee [the drafts]({targets[0]}), [the plan]({targets[1]}) and ![a figure]({targets[2]}).\n",
        encoding="utf-8",
    )
    expected = {
        f"notes.md: links to {target!r}, which is hidden or in an unpublished folder, so the site keeps only "
        "the link's text"
        for target in targets
    }

    assert main(["check", str(blueprint)]) == 0
    warned = {line.removeprefix("warning: ") for line in capsys.readouterr().out.splitlines() if "links to" in line}
    assert warned == expected
    out = blueprint.parents[1] / "out"
    report = render_site(blueprint, out, lean_root=blueprint.parent)
    assert {warning for warning in report.warnings if "links to" in warning} == expected
    assert "See the drafts, the plan and a figure." in (out / "notes.md").read_text(encoding="utf-8")


def test_the_landing_page_is_checked_without_the_dashboard_render_adds(tmp_path: Path) -> None:
    """Render adds HTML of its own around the landing page's text; only the
    author's text is checked, so a plain landing page passes."""

    from autoform_cli.render import publication_issues

    blueprint = _vault(tmp_path)

    assert publication_issues(load_graph(blueprint), blueprint) == []
    page = (_render(blueprint) / "README.md").read_text(encoding="utf-8")
    assert 'class="bp-landing-intro"' in page
    assert 'class="bp-reading-source"' in page and 'class="bp-explorer-page"' in page


def test_a_page_that_is_not_utf8_is_left_out_by_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A link to the page keeps only its text, so the strict build finds nothing missing."""

    blueprint = _vault(tmp_path)
    (blueprint / "notes.md").write_bytes(b"# Notes\n\n\xff\n")
    _with_article(blueprint, "The main result, see [the notes](../notes.md).")

    site = _warned(blueprint, capsys, "notes.md: is not UTF-8 text, as a Markdown page must be, so the site leaves it out")
    assert not (site / "notes.md").exists()
    chapter = (site / "roadmap/README.md").read_text(encoding="utf-8")
    assert "The main result, see the notes." in chapter and "notes.md" not in chapter


def test_the_scaffolded_pages_pass_the_page_check(tmp_path: Path) -> None:
    from autoform_cli.render import publication_issues

    scaffold_project(tmp_path, title="Finite Flat")
    blueprint = tmp_path / "blueprint"

    assert publication_issues(load_graph(blueprint), blueprint) == []


_HTML = "raw HTML is not allowed: <span>, </span>, <script>, </script>"


@pytest.mark.parametrize("separator", ["\u2028", "\x0b", "\x0c", "\x1e", "\x85"])
def test_a_line_break_markdown_does_not_read_is_checked_as_the_site_writes_it(separator: str) -> None:
    """The site writes every line break Python reads as a newline, so text
    after one is checked on a line of its own, where it is HTML, not code."""

    assert any(_HTML in error for error in publishable_article(f"Claim.\n\n    x = 1{separator}{_FORGED}\n")[1])


def test_a_chapter_is_checked_whole_with_its_dependency_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A chapter page is published whole, line by line, its dependency
    section included, so it is checked that way too: after the break, the
    text is a paragraph, not code."""

    blueprint = _vault(tmp_path)
    chapter = blueprint / "roadmap/README.md"
    chapter.write_text(chapter.read_text(encoding="utf-8") + f"\n## Depends on\n\n    x = 1\u2028{_FORGED}\n", encoding="utf-8")

    site = _warned(blueprint, capsys, f"roadmap: line 19: {_HTML}")
    assert "<script" not in _published(site / "roadmap/README.md")


def test_frontmatter_never_reaches_the_built_page(tmp_path: Path) -> None:
    """The theme prints a page's title and description unescaped, so render
    replaces a page's frontmatter, and the leading key: value lines MkDocs
    would read as metadata without one, with a header of its own: a quote in
    a description cannot add an attribute to the built page."""

    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    scaffold_project(tmp_path, title="Finite Flat")
    refresh = "'0;url=https://evil.example/\" http-equiv=\"refresh'"
    (tmp_path / "blueprint/notes.md").write_text(
        f"---\ntitle: Frontmatter title\ndescription: {refresh}\n---\n\n# Notes\n\nText.\n", encoding="utf-8"
    )
    (tmp_path / "blueprint/more.md").write_text(f"description: {refresh}\n\n# More\n\nText.\n", encoding="utf-8")
    render_site(tmp_path / "blueprint", tmp_path / "site-src", lean_root=tmp_path)
    build(load_config(str(tmp_path / "mkdocs.yml")))

    for name, title in (("notes", "Notes"), ("more", "More")):
        page = (tmp_path / f"site/{name}.html").read_text(encoding="utf-8")
        assert not [tag for tag in re.findall(r"<meta [^>]*>", page) if "evil.example" in tag], name
        assert "Frontmatter title" not in page
        assert f"<title>{title} - Finite Flat</title>" in page


@pytest.mark.parametrize(
    ("article", "expected"),
    [
        # CommonMark closes a fence only with a line of its marker alone.
        (
            "---\n---\n\n# Result\n\nClaim.\n\n```text\n``` trailing text\n## still fenced\n```\n\n## Sources\n\nA book.\n",
            ("Claim.\n\n```text\n``` trailing text\n## still fenced\n```", "###### Sources\n\nA book."),
        ),
        (
            "---\n---\n\n# Result\n\nClaim.\n\n<!--\n## hidden section\n-->\nClaim B.\n\n## Sources\n\nA book.\n",
            ("Claim.\n\n<!--\n###### hidden section\n-->\nClaim B.", "###### Sources\n\nA book."),
        ),
        (
            "---\ndeclaration: theorem\n---\n\n# Top\n\n\n    x = 1\n\nClaim.\n\n## Sources\n\nA book.\n\n"
            "## Depends on\n\n- [Base](base.md)\n",
            ("    x = 1\n\nClaim.", "###### Sources\n\nA book."),
        ),
        # Check reads every article line by line, so blank lines at its end are lines like any other.
        ("# Top\n\nClaim.\n\n\n", ("Claim.", "")),
        ("---\nkind: x\n---\n", ("", "")),
    ],
)
def test_the_box_publishes_the_statement_and_notes_check_reads(article: str, expected: tuple[str, str]) -> None:
    assert statement_and_notes(article) == expected


@pytest.mark.parametrize(
    ("article", "shown"),
    [
        ("Claim.\n{: .bp-mark .bp-thmcontent }", "{: .bp-mark .bp-thmcontent }"),
        ('Claim.\n{: style="position:fixed;inset:0;z-index:1000" }', '{: style="position:fixed;inset:0;z-index:1000" }'),
        (
            "Claim.\n{: #autorun tabindex=\"-1\" autofocus=\"autofocus\" onfocus=\"document.title='x'\" }",
            "{: #autorun tabindex=\"-1\" autofocus=\"autofocus\" onfocus=\"document.title='x'\" }",
        ),
        ("Claim.\n{onclick=\"alert(1)\"}", "{onclick=\"alert(1)\"}"),
        ("A *claim*{ .bp-mark } here.", "{ .bp-mark }"),
        ("$x${hidden}", "{hidden}"),
        ("- item\n  {: .bp-mark }", "{: .bp-mark }"),
        ("| a {: .bp-mark } |\n| --- |\n| b |", "{: .bp-mark }"),
        ("Text `code`{.bp-mark} more.", "{.bp-mark}"),
        ("## Result {#main-result}", "{#main-result}"),
        ("## Result {: #bp-mark }", "{: #bp-mark }"),
    ],
)
def test_an_attribute_list_is_reported(article: str, shown: str) -> None:
    """An attribute list could make an article's text look like a card, a
    mark, or an approval, take the id of the site's own element, or cover
    the page; a heading's id comes from its title."""

    errors = publishable_article(f"# Top\n\n{article}\n")[1]

    line = 3 + article[: article.index(shown)].count("\n")
    assert errors == (f"line {line}: attribute list {shown} {_ATTRIBUTE_RULE}",)


_FENCE_RULE = (
    'a code fence may set only its language and options such as title, as in "```lean" or "```{.lean}"; '
    "drop its classes, id, and attributes"
)


@pytest.mark.parametrize(
    ("article", "shown"),
    [
        ("```{.text .bp-mark .bp-thmcontent}\nApproved.\n```", "{.text .bp-mark .bp-thmcontent}"),
        ("```text {.bp-mark #claim data-kind=result}\nx\n```", "{.bp-mark #claim data-kind=result}"),
        ('```text {style="position:fixed;inset:0;z-index:1000"}\nx\n```', '{style="position:fixed;inset:0;z-index:1000"}'),
        ("``` { .lean #anchor }\nx\n```", "{ .lean #anchor }"),
        ("- item\n\n    ```{.text .bp-mark}\n    x\n    ```", "{.text .bp-mark}"),
        ("> ~~~{.text .mermaid}\n> graph LR\n> ~~~", "{.text .mermaid}"),
        ("```{.text .mermaid .bp-graph}\ngraph LR\n```", "{.text .mermaid .bp-graph}"),
    ],
)
def test_a_fence_header_that_sets_more_than_a_language_is_reported(article: str, shown: str) -> None:
    """A class, an id, or another attribute in a fence's braces lands on the
    code block's element, as one in an attribute list does, so it could make
    the block look like a card or a mark, cover the page, or be drawn as a
    diagram."""

    errors = publishable_article(f"# Top\n\n{article}\n")[1]

    line = 3 + article[: article.index(shown)].count("\n")
    assert errors == (f"line {line}: attribute list {shown} is not allowed: {_FENCE_RULE}",)


@pytest.mark.parametrize(
    "heading",
    ["Top <img src=x onerror=alert(1)>", "Top `<img src=x onerror=alert(1)>`", "Top $<img src=x onerror=alert(1)>$"],
    ids=["raw", "code", "formula"],
)
def test_a_title_reaches_the_places_render_writes_it_as_text(tmp_path: Path, heading: str) -> None:
    """Render writes article titles into explorer page headings, the
    navigation, and page metadata, and the theme prints the last two as they
    are, so a title reaches each as text, whatever markup or code it holds."""

    blueprint = _vault(tmp_path)
    for path in (blueprint / "roadmap/top.md", blueprint / "README.md", blueprint / "roadmap/README.md"):
        text = path.read_text(encoding="utf-8")
        path.write_text(re.sub(r"^# .*$", f"# {heading}", text, count=1, flags=re.MULTILINE), encoding="utf-8")

    site = _render(blueprint)

    from mkdocs.utils.meta import get_data

    graph_heading = next(
        line for line in (site / "dependencies.md").read_text(encoding="utf-8").splitlines() if line.startswith("# ")
    )
    summary = (site / "SUMMARY.md").read_text(encoding="utf-8")
    published = [
        site_converter().convert(graph_heading),
        summary,
        get_data((site / "README.md").read_text(encoding="utf-8"))[1]["title"],
        # A chapter shown as typed keeps its title in its header.
        get_data((site / "roadmap/README.md").read_text(encoding="utf-8"))[1].get("title", ""),
    ]
    assert not any("<img" in text for text in published), published
    # The chapter is in the navigation, under its own title.
    assert re.search(r"^ +- \[Top\b.*\]\(roadmap/README\.md\)$", summary, flags=re.MULTILINE), summary


def test_check_reads_past_a_hidden_article_render_never_captures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The graph finds a hidden article, which capture skips and render never
    publishes, so check has no page of it to read."""

    blueprint = _vault(tmp_path)
    (blueprint / "roadmap/.draft.md").write_text("---\n---\n\n# Draft\n\nNot yet.\n", encoding="utf-8")

    assert main(["check", str(blueprint)]) == 0, capsys.readouterr().out


def test_a_link_to_a_file_the_site_leaves_out_keeps_only_its_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A strict build fails on a link to a missing file, so an image, a link,
    or a link definition that names a file the site leaves out keeps only its
    text, and the site still builds."""

    blueprint = _vault(tmp_path)
    (blueprint / "roadmap/figures").mkdir()
    (blueprint / "roadmap/figures/square.svg").write_text("<svg/>", encoding="utf-8")
    _with_article(
        blueprint,
        "The main result: ![a square](figures/square.svg), [the figure](figures/square.svg), and [it][fig].\n\n"
        "[fig]: figures/square.svg",
    )

    site = _warned(blueprint, capsys, "roadmap/figures/square.svg: the site leaves out .svg files")
    chapter = (site / "roadmap/README.md").read_text(encoding="utf-8")
    assert "square.svg" not in chapter
    assert "The main result: a square, the figure, and [it][fig]." in chapter


def test_a_flagged_statement_keeps_its_chapter_in_place(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A leaf's part of its chapter is its box: one statement shown as typed
    leaves the narrative, and every other statement, where the author put them."""

    blueprint = _vault(tmp_path)
    base = blueprint / "roadmap/base.md"
    base.write_text(base.read_text(encoding="utf-8").replace("The base object.", "The base <kbd>object</kbd>."), encoding="utf-8")

    site = _warned(blueprint, capsys, "base: the site shows it as typed until these are fixed")
    published = (site / "roadmap/README.md").read_text(encoding="utf-8")
    assert "Additional formalization targets" not in published
    assert published.count('<pre class="bp-as-typed">') == 1
    definitions, results = published.index("## Definitions"), published.index("## Results")
    assert definitions < published.index("The base &lt;kbd&gt;object&lt;/kbd&gt;.") < results < published.index("The main result.")


@pytest.mark.parametrize("relative", ["notes.md", "roadmap/top.md"], ids=["page", "statement"])
def test_a_page_nested_deeper_than_the_converter_reads_is_shown_as_typed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], relative: str
) -> None:
    """The site's converter recurses once for each level of a nested list, so
    a page too deep for it is a finding, not a crash of check or render."""

    blueprint = _vault(tmp_path)
    deep = "".join("    " * depth + "- level\n" for depth in range(400))
    if relative == "notes.md":
        (blueprint / relative).write_text(f"---\nkind: note\n---\n\n# Notes <!-- private -->\n\n{deep}", encoding="utf-8")
    else:
        _with_article(blueprint, f"The main result.\n\n{deep}")
    label = "notes.md" if relative == "notes.md" else "top"

    site = _warned(blueprint, capsys, f"{label}: the page nests lists deeper than the site's Markdown converter")
    published = (site / ("notes.md" if relative == "notes.md" else "roadmap/README.md")).read_text(encoding="utf-8")
    assert '<pre class="bp-as-typed">' in published
    # The block holds the page as render publishes it: no frontmatter, no comment.
    assert "kind: note" not in published and "private" not in published


def test_a_page_that_starts_with_a_byte_order_mark_keeps_its_frontmatter_hidden(tmp_path: Path) -> None:
    """MkDocs strips a byte-order mark only at the start of a file, where
    render's header now goes, so render drops the mark first."""

    from mkdocs.utils.meta import get_data

    blueprint = _vault(tmp_path)
    (blueprint / "notes.md").write_text("\ufeff---\nkind: note\n---\n\n# Notes\n\nText.\n", encoding="utf-8")

    published = (_render(blueprint) / "notes.md").read_text(encoding="utf-8")
    assert get_data(published)[1] == {"title": "Notes"}
    assert "kind: note" not in published and "\ufeff" not in published


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("# Notes\n\nText.\n", "Notes"),
        ("Notes\n=====\n\nText.\n", "Notes"),
        ("# Notes `</title><svg onload=alert(1)>`\n\nText.\n", "Notes &lt;/title&gt;&lt;svg onload=alert(1)&gt;"),
        ("# \U0001d53d-modules \U0001f600\n\nText.\n", "\U0001d53d-modules \U0001f600"),
        ("No heading here.\n", "Field notes"),
        ("Intro first.\n\n# Later heading\n", "Later heading"),
        ("# Notes <b>today</b>\n\nText.\n", "Notes today"),
        ("Text <b>today</b>.\n", "Field notes"),
    ],
    ids=["h1", "setext", "code-span", "astral", "no-h1", "h1-not-first", "shown-as-typed", "shown-as-typed-no-h1"],
)
def test_a_page_is_published_under_its_title_as_text(tmp_path: Path, text: str, title: str) -> None:
    """The theme prints a page's title as it is, and in <title> reads a
    reference back as its character, so render names the title in the page's
    header, escaped: its H1's, the first block's as MkDocs would take it, else
    the first one below, else the file's name, and a page shown as typed keeps
    its H1's."""

    from mkdocs.utils.meta import get_data

    blueprint = _vault(tmp_path)
    (blueprint / "field_notes.md").write_text(text, encoding="utf-8")

    published = (_render(blueprint) / "field_notes.md").read_text(encoding="utf-8")
    assert get_data(published)[1] == {"title": title}


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot name a file with < or >")
def test_a_page_named_with_markup_is_titled_as_text(tmp_path: Path) -> None:
    """MkDocs titles a page with no H1 by its file's name, which may hold markup."""

    from mkdocs.utils.meta import get_data

    blueprint = _vault(tmp_path)
    (blueprint / "<svg onload=alert(1)>.md").write_text("No heading here.\n", encoding="utf-8")

    published = (_render(blueprint) / "<svg onload=alert(1)>.md").read_text(encoding="utf-8")
    assert get_data(published)[1] == {"title": "&lt;svg onload=alert(1)&gt;"}


def test_a_graph_page_is_published_under_its_title_as_text(tmp_path: Path) -> None:
    """Render writes the roadmap's title into its explorer page's heading, so
    that page names the title in its header too."""

    from mkdocs.utils.meta import get_data

    blueprint = _vault(tmp_path)
    roadmap = blueprint / "roadmap/README.md"
    roadmap.write_text(
        roadmap.read_text(encoding="utf-8").replace("# Roadmap", "# Roadmap `</title><svg onload=alert(1)>`"),
        encoding="utf-8",
    )

    site = _render(blueprint)

    meta = get_data((site / "dependencies.md").read_text(encoding="utf-8"))[1]
    assert meta["title"].startswith("Roadmap &lt;/title&gt;&lt;svg onload=alert(1)&gt;")
    # A heading render writes in full keeps the title its navigation entry gives.
    assert "title" not in get_data((site / "dependencies/chapters/roadmap.md").read_text(encoding="utf-8"))[1]


def test_a_title_holding_a_backslash_escape_is_read_back_as_written(tmp_path: Path) -> None:
    """The navigation reads a chapter's title back from its header, where a
    backslash the title holds is doubled, so ``\\U`` and eight digits typed in
    an H1 stay text."""

    blueprint = _vault(tmp_path)
    chapter = blueprint / "roadmap/README.md"
    chapter.write_text(chapter.read_text(encoding="utf-8").replace("# Roadmap", "# Roadmap \\UDEADBEEF"), encoding="utf-8")

    summary = (_render(blueprint) / "SUMMARY.md").read_text(encoding="utf-8")
    assert "- [Roadmap &#92;UDEADBEEF](roadmap/README.md)" in summary


def test_a_page_shown_as_typed_keeps_its_title(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A page shown as typed has no H1 left for MkDocs or the navigation to
    read, so its header names the title its H1 gave, as text."""

    from mkdocs.utils.meta import get_data

    blueprint = _vault(tmp_path)
    (blueprint / "notes.md").write_text("# Notes <b>today</b>\n\nText.\n", encoding="utf-8")
    _with_narrative(blueprint, "## Definitions\n\nThe <b>base</b> object.\n\n- [Base](base.md)")

    site = _warned(blueprint, capsys, "notes.md: the site shows it as typed until these are fixed")
    assert get_data((site / "notes.md").read_text(encoding="utf-8"))[1]["title"] == "Notes today"
    assert get_data((site / "roadmap/README.md").read_text(encoding="utf-8"))[1]["title"] == "Roadmap"
    assert "- [Roadmap](roadmap/README.md)" in (site / "SUMMARY.md").read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot name a folder with a colon")
@pytest.mark.parametrize("name", ["javascript:alert(1)", "vbscript:x"])
def test_a_folder_named_like_a_scheme_is_linked_by_a_relative_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """Render writes its book navigation and dependency links from folder
    names. A link whose first folder is named like ``javascript:...`` would
    read as a script URL, so render percent-encodes the colon, or starts a
    Markdown link it keeps decoded with ./, and check has nothing to report."""

    blueprint = tmp_path / "project/blueprint"
    shutil.copytree(_EXAMPLE / "blueprint", blueprint)
    encoded = name.replace("(", "%28").replace(")", "%29")
    (blueprint / "roadmap/full-supervision").rename(blueprint / "roadmap" / name)
    for page in blueprint.rglob("*.md"):
        text = page.read_text(encoding="utf-8")
        page.write_text(
            text.replace("](full-supervision/", f"](./{encoded}/").replace("/full-supervision/", f"/{encoded}/"),
            encoding="utf-8",
        )
    atlas = blueprint / "atlas.json"
    atlas.write_text(atlas.read_text(encoding="utf-8").replace('"full-supervision"', json.dumps(name)), encoding="utf-8")

    assert main(["check", str(blueprint)]) == 0
    assert "warning" not in capsys.readouterr().out
    site = tmp_path / "out"
    render_site(blueprint, site, lean_root=blueprint.parent)

    scheme = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
    targets = [
        found.group(1) or found.group(2)
        for page in site.rglob("*.md")
        for found in re.finditer(r'href="([^"]*)"|\]\(<?([^)\s>]+)', page.read_text(encoding="utf-8"))
    ]
    assert f"{quote(name, safe='')}/index.html" in targets
    assert [target for target in targets if scheme.match(target) and not target.startswith(("http", "mailto:"))] == []


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot name a folder with a colon")
def test_a_rewritten_link_into_a_folder_named_like_a_scheme_stays_relative(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Render writes a Markdown link it moves decoded, so a link whose first
    folder is named like ``javascript:x`` would read as a script URL once
    rewritten. Render starts it with ./, so the page publishes as written."""

    blueprint = _vault(tmp_path)
    (blueprint / "roadmap/javascript:x").mkdir()
    (blueprint / "roadmap/javascript:x/data.txt").write_text("data\n", encoding="utf-8")
    _with_article(blueprint, "The main result, see [the data](./javascript:x/data.txt).")

    assert main(["check", str(blueprint)]) == 0
    assert "warning" not in capsys.readouterr().out
    chapter = (_render(blueprint) / "roadmap/README.md").read_text(encoding="utf-8")
    assert "see [the data](./javascript:x/data.txt)." in chapter


@pytest.mark.parametrize("name", ["dependencies.html", "graph.html", "progress.md"])
def test_a_directory_named_like_a_view_render_never_writes_is_published(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """Render keeps the views earlier renders wrote out of the site but never
    writes them, so a directory with one of their names is the vault's own."""

    blueprint = _vault(tmp_path)
    (blueprint / name).mkdir()
    (blueprint / name / "page.md").write_text("# Page\n\nText.\n", encoding="utf-8")

    assert main(["check", str(blueprint)]) == 0
    assert "is a directory" not in capsys.readouterr().out
    assert (_render(blueprint) / name / "page.md").is_file()


def test_a_macros_file_that_is_a_directory_leaves_the_site_without_project_macros(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = _vault(tmp_path)
    _directory_with_a_file(blueprint / "tex-macros.json")

    site = _warned(blueprint, capsys, "tex-macros.json: is a directory, so the site has no project macros")
    assert _settings((site / "javascripts/mathjax.js").read_text(encoding="utf-8"))["page"]["macros"] == _OFF


def test_an_obsidian_canvas_stays_in_the_vault_without_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A canvas is Obsidian's own file, as .obsidian is: the site leaves it
    out without a word, and a link to it keeps only its text."""

    blueprint = _vault(tmp_path)
    (blueprint / "Untitled.canvas").write_text('{"nodes": [], "edges": []}\n', encoding="utf-8")
    _with_article(blueprint, "The main result, [drawn](../Untitled.canvas).")

    assert main(["check", str(blueprint)]) == 0
    assert "canvas" not in capsys.readouterr().out
    site = _render(blueprint)
    assert not (site / "Untitled.canvas").exists()
    assert "The main result, drawn." in (site / "roadmap/README.md").read_text(encoding="utf-8")


def test_a_landing_page_named_in_lowercase_is_rendered(tmp_path: Path) -> None:
    """A disk that ignores case serves readme.md as README.md, which render
    reads back to build the landing page."""

    blueprint = _vault(tmp_path)
    (blueprint / "README.md").rename(blueprint / "readme.md")

    site = _render(blueprint)

    landing = next(path for path in site.iterdir() if path.name.lower() == "readme.md")
    if (site / "README.md").is_file():  # the disk ignores case
        assert 'class="bp-dag-intro bp-landing-intro"' in landing.read_text(encoding="utf-8")


def test_a_landing_page_that_is_not_utf8_is_left_out(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = _vault(tmp_path)
    (blueprint / "README.md").write_bytes(b"# Overview\n\n\xff\n")

    site = _warned(blueprint, capsys, "README.md: is not UTF-8 text, as a Markdown page must be, so the site leaves it out")
    assert not (site / "README.md").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot name a file with < or >")
def test_no_title_on_the_built_site_is_markup(tmp_path: Path) -> None:
    """Built with MkDocs and the theme: every title the site shows, in the page
    header, <title>, and the search index, is text, whatever the file's name
    or its H1 holds."""

    pytest.importorskip("material")
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    scaffold_project(tmp_path, title="Finite Flat")
    blueprint = tmp_path / "blueprint"
    (blueprint / "<svg onload=alert(1)>.md").write_text("No heading here.\n", encoding="utf-8")
    (blueprint / "code.md").write_text("# Code `</title><svg onload=alert(2)>`\n\nText.\n", encoding="utf-8")
    (blueprint / "typed.md").write_text("# Typed <b>x</b> `</title><svg onload=alert(3)>`\n", encoding="utf-8")

    render_site(blueprint, tmp_path / "site-src", lean_root=tmp_path)
    build(load_config(str(tmp_path / "mkdocs.yml")))

    built = tmp_path / "site"
    pages = [path for path in built.rglob("*.html")]
    assert len(pages) > 3
    assert [path.name for path in pages if "<svg onload" in path.read_text(encoding="utf-8")] == []
    index = json.loads((built / "search/search_index.json").read_text(encoding="utf-8"))
    assert [doc["title"] for doc in index["docs"] if "<svg" in doc["title"]] == []


def test_a_link_to_a_read_back_card_keeps_only_its_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A read-back card is never a page of its own, so a link to one keeps
    only its text, as a link to any other file the site leaves out does."""

    blueprint = _vault(tmp_path)
    card = blueprint / "readbacks/af_000000000000000000000000/Main.md"
    card.parent.mkdir(parents=True)
    card.write_text("A read-back.\n", encoding="utf-8")
    _with_article(blueprint, "The main result, [as read back](../readbacks/af_000000000000000000000000/Main.md).")

    assert main(["check", str(blueprint)]) == 0, capsys.readouterr().out
    site = _render(blueprint)
    chapter = (site / "roadmap/README.md").read_text(encoding="utf-8")
    assert "readbacks" not in chapter
    assert "The main result, as read back." in chapter
    assert not (site / "readbacks").exists()
    # Nor is a card a row of the vault's structure page.
    assert "Main.md" not in (site / "structure.md").read_text(encoding="utf-8")


def test_a_chapter_shown_as_typed_keeps_its_statements_outside_the_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The slots and sections of a narrative shown as typed are text, so the
    summary and every statement follow the block instead of landing in it."""

    blueprint = _vault(tmp_path)
    _with_narrative(blueprint, "## Definitions\n\nThe <b>base</b> object.\n\n- [Base](base.md)")

    site = _warned(blueprint, capsys, "roadmap: the site shows it as typed until these are fixed")
    published = _published(site / "roadmap/README.md")
    block = published[published.index('<pre class="bp-as-typed">') : published.index("</pre>")]
    assert "bp-thmwrapper" not in block
    assert published.count('class="bp-thmwrapper') == 2


@pytest.mark.parametrize(
    ("written", "published"),
    [
        ("A span `a <!-- b\n--> c` crosses a line.", "A span `a <!-- b\n--> c` crosses a line."),
        ("Not code: \\`<!-- note --> plain.", "Not code: \\` plain."),
        ("Two `` ` `` then <!-- gone -->.", "Two `` ` `` then ."),
    ],
)
def test_a_comment_is_dropped_only_outside_a_code_span(written: str, published: str) -> None:
    """A code span shows a comment as typed, and may cross a line; a backtick
    a backslash escapes opens none."""

    assert published_page(written).removeprefix("---\n{}\n---\n") == f"{published}\n"


@pytest.mark.parametrize(
    ("written", "published"),
    [
        ("Para.\n\n    <!-- alone -->", "Para.\n\n    <!-- alone -->"),
        ("Para.\n\n    <!-- first -->\n    code", "Para.\n\n    <!-- first -->\n    code"),
        ("Para.\n\n    code\n    <!-- last -->", "Para.\n\n    code\n    <!-- last -->"),
        ("Para.\n\n    <!-- opens\n    closes -->", "Para.\n\n    <!-- opens\n    closes -->"),
        ("- item\n\n    <!-- a list's -->", "- item\n\n    "),
        ("<!--\n    <!-- private\n    still private -->\n\nAfter.", "\n\nAfter."),
    ],
    ids=["alone", "first", "last", "two-lines", "in-a-list", "inside-a-comment"],
)
def test_a_comment_that_fills_a_line_of_indented_code_is_kept(written: str, published: str) -> None:
    """An indented code block shows a comment as typed, even on a line of its
    own at the start or end of the block; indented under a list item, the
    line is the item's text, and the comment goes, as does every line inside
    a comment opened earlier."""

    assert published_page(written).removeprefix("---\n{}\n---\n") == f"{published}\n"


def test_every_formula_paints_inside_its_own_band(tmp_path: Path) -> None:
    """No formula can cover a card, a mark, a label, another line, or a side
    column, with either renderer and any theme; a lap keeps the ink beside its
    box inside the block that holds it; and a wide display scrolls rather than
    being cut off."""

    css = (_render(_vault(tmp_path)) / "stylesheets/blueprint.css").read_text(encoding="utf-8")
    rules = {
        selector.strip(): body
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
    }

    # Containment is not the CHTML renderer's or Material's alone.
    for selector in rules:
        if "mjx-container" in selector:
            assert "jax=" not in selector and ".md-typeset" not in selector, selector
    every = rules["mjx-container"]
    assert "overflow-y: clip !important" in every
    assert "overflow-x: visible !important" in every
    assert "position: relative !important" in every
    assert "contain:" not in every
    assert "display: inline-block" in rules['mjx-container:not([display="true"])']
    assert "padding-block: 0.5em" in rules[':root mjx-container[jax][display="true"]']
    assert "overflow-x: auto" in rules["div.arithmatex"]
    # Material caps every svg in its pages at its container's width; one MathJax
    # draws keeps the size TeX set, rather than shrinking to fit or, on a phone,
    # to nothing.
    assert "max-width: none !important" in rules["mjx-container > svg"]
    # Across, ink stops at the edge of the block an article formula is in.
    block = rules[":is(p, h1, h2, h3, h4, h5, h6, ul, ol, td, th):has(mjx-container)"]
    assert "overflow-x: clip" in block and "overflow-y" not in block
