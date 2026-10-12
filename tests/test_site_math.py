"""The site's pages: the MathJax configuration ``autoform render`` writes,
the HTML, links, and attribute lists a published page may carry, the paths
render writes, and the styles that keep each formula in its place."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import pytest

from autoform_cli.__main__ import main
from autoform_cli.graph import load_graph
from autoform_cli.markdown import site_converter, statement_and_notes
from autoform_cli.mathjax import NEUTRALISED, mathjax_script
from autoform_cli.publishable import publishable_article
from autoform_cli.render import _GENERATED_FILES, PublicationError, _site_paths, render_site
from autoform_cli.scaffold import scaffold_project
from tests.test_render import _project
from tests.undecodable_json import _too_deep_to_decode

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = _ROOT / "skills/setup/assets/cabannes-thesis-project"
_BASE = "https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5"
_BUNDLE = f"{_BASE}/tex-mml-chtml.js"
_PAGE_PACKAGES = ["base", "ams", "noundefined", "boldsymbol", "mathtools", "configmacros"]
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


def _refused(blueprint: Path, capsys: pytest.CaptureFixture[str], reason: str, match: str | None = None) -> None:
    """Check fails naming reason, and render refuses the blueprint, with match
    in its message when one is given."""

    assert main(["check", str(blueprint)]) == 1
    assert reason in capsys.readouterr().out
    with pytest.raises(PublicationError, match=re.escape(match) if match else None):
        _render(blueprint)


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
    assert settings["load"] == ["ui/safe", "[tex]/boldsymbol", "[tex]/mathtools"]
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


@pytest.mark.parametrize("mkdocs", ["scaffolded", "example"])
def test_mkdocs_loads_mathjax_only_through_the_rendered_script(tmp_path: Path, mkdocs: str) -> None:
    """Neither a new project nor the example keeps a copy of the script or
    names a MathJax release of its own."""

    if mkdocs == "scaffolded":
        scaffold_project(tmp_path, title="Finite Flat")
        project = tmp_path
    else:
        project = _EXAMPLE
    configuration = (project / "mkdocs.yml").read_text(encoding="utf-8")

    assert "  - javascripts/mathjax.js\n" in configuration
    assert "mathjax@" not in configuration
    assert not (project / "blueprint/javascripts/mathjax.js").exists()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_a_configuration_init_wrote_is_replaced_without_a_word(tmp_path: Path, newline: str) -> None:
    """A project scaffolded with its own copy needs no edits."""

    blueprint = _vault(tmp_path, kept=_SCAFFOLDED.replace("\n", newline))

    assert main(["check", str(blueprint)]) == 0
    script = (_render(blueprint) / "javascripts/mathjax.js").read_text(encoding="utf-8")
    assert _settings(script)["page"]["packages"] == _PAGE_PACKAGES


def test_an_edited_configuration_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Its edits would be lost, so neither check nor render goes ahead."""

    kept = _SCAFFOLDED.replace("  tex: {\n", '  tex: {\n    macros: {RR: "\\\\mathbb{R}"},\n')
    blueprint = _vault(tmp_path, kept=kept)
    reason = "move any tex.macros to tex-macros.json and delete it"

    _refused(blueprint, capsys, reason, reason)


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


def test_macros_made_valid_after_capture_are_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid macros captured are refused even if the file is fixed before
    the script is built, so the site never publishes a script without the
    macros beside a tex-macros.json that has none."""

    blueprint = _vault(tmp_path, macros="{")
    macros = blueprint / "tex-macros.json"
    _changed_after_capture(monkeypatch, lambda: macros.write_text(json.dumps(_MACROS), encoding="utf-8"))

    assert main(["check", str(blueprint)]) == 1
    assert "tex-macros.json: not valid JSON" in capsys.readouterr().out
    macros.write_text("{", encoding="utf-8")
    with pytest.raises(PublicationError, match="tex-macros.json: not valid JSON"):
        _render(blueprint)



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
def test_project_macros_that_cannot_be_used_are_refused(macros: str, reason: str) -> None:
    """Check and render refuse on these issues, as the edited and the
    captured configurations above show."""

    # Decoded here, nearer the stack's base than check and render decode it.
    if macros == _NESTED and not _too_deep_to_decode(macros):
        pytest.skip("this stack holds a hundred thousand nested arrays")

    assert any(reason in issue for issue in mathjax_script(None, macros.encode())[1])

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
def test_something_other_than_a_file_where_render_writes_one_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], make
) -> None:
    """One rule covers every file render writes, and the macros file it reads:
    a directory there, captured with what it holds, is named by check, and
    render refuses it before writing rather than stopping half way with an
    OSError. Capture refuses a symlink or special file anywhere but at a name
    it keeps out of the site, which it never reads."""

    blueprint = _vault(tmp_path)
    # A chapter page render writes over the vault's own is the vault's to keep.
    paths = [path for path in (*_site_paths(load_graph(blueprint)), "tex-macros.json") if not (blueprint / path).exists()]
    for relative in paths:
        make(blueprint / relative)
    expected = {
        _directory_with_a_file: lambda relative: (
            "tex-macros.json: is a directory, where autoform reads the project's macros from a file; remove it"
            if relative == "tex-macros.json"
            else f"{relative}: is a directory, where autoform render writes a file; remove it"
        ),
        _symlink: lambda relative: f"refusing symlink in blueprint publication: {relative}",
        _fifo: lambda relative: f"refusing non-regular blueprint publication input: {relative}: ",
    }[make]

    assert main(["check", str(blueprint)]) == 1
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
def test_what_render_captured_in_the_way_is_refused_once_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make, reason: str
) -> None:
    """Render copies what it captured, so a directory or file it captured in
    the way of a page it writes is refused even when the disk no longer has
    it by the time the paths are checked."""

    blueprint = _vault(tmp_path)
    blocking = blueprint / reason.split(":")[0]
    make(blocking)
    _changed_after_capture(monkeypatch, lambda: shutil.rmtree(blocking) if blocking.is_dir() else blocking.unlink())

    with pytest.raises(PublicationError, match=re.escape(reason)):
        _render(blueprint)


@pytest.mark.parametrize("folder", ["dependencies", "dependencies/nodes", "javascripts", "stylesheets", "assets"])
def test_a_file_where_render_needs_a_folder_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], folder: str
) -> None:
    blueprint = _vault(tmp_path)
    _file(blueprint / folder)
    reason = f"{folder}: is a file, where autoform render needs a folder for {folder}/"

    _refused(blueprint, capsys, reason, reason)



_OPENED = (
    "raw HTML is not allowed: the site would read a < before a letter as the start of a tag, and publish "
    "'</div>' after it as HTML; in a formula, put a space after <, as in $a < b$"
)
_ATTRIBUTE_RULE = "is not allowed: it would give the text an id, a class, or a style of its own; delete it"
#: An article's frontmatter, so the lines named count it as the file does.
_TOP = "---\ndeclaration: theorem\n---\n\n# Top\n\n"


@pytest.mark.parametrize(
    ("article", "reason"),
    [
        ("First.\n\nThen <b>bold</b>.", "line 9: raw HTML is not allowed: <b>, </b>;"),
        ("First.\n\nThen $a<b$.", f"line 9: {_OPENED}"),
        ("First.\n\nA remark.\n{: .bp-mark }", f"line 10: attribute list {{: .bp-mark }} {_ATTRIBUTE_RULE}"),
        ("First.\n\nSee [x](javascript:alert(1)).", "line 9: links are not allowed to javascript: addresses"),
        pytest.param(
            "First.\n\nThe &#" + "9" * 39000 + ";.",
            "line 9: HTML character references are not allowed: &#99999999999999... (39003 characters)",
            id="long-reference",
        ),
    ],
)
def test_a_refusal_names_the_line_of_the_article(article: str, reason: str) -> None:
    assert any(error.startswith(reason) for error in publishable_article(f"{_TOP}{article}\n")[1])


@pytest.mark.parametrize("frontmatter", ["---\n---\n\n", ""], ids=["frontmatter", "none"])
def test_a_refusal_in_a_chapter_names_the_chapter_and_the_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], frontmatter: str
) -> None:
    """Check reads a chapter whole and in the stretches between its
    statements, and both readings name the line in the file, so a refusal is
    reported once, whatever the header render puts in place of the
    frontmatter."""

    blueprint = _vault(tmp_path)
    chapter = blueprint / "roadmap/README.md"
    body = chapter.read_text(encoding="utf-8").removeprefix("---\n---\n\n")
    chapter.write_text(f"{frontmatter}{body}\nLast, <b>x</b>.\n", encoding="utf-8")
    line = len(chapter.read_text(encoding="utf-8").splitlines())

    assert main(["check", str(blueprint)]) == 1
    refused = [found for found in capsys.readouterr().out.splitlines() if found.startswith("error: roadmap:")]
    assert refused == [f"error: roadmap: line {line}: raw HTML is not allowed: <b>, </b>; {_RAW_HTML_FIX}"]


# The forms GitHub shows as formulas with their TeX as written.
_PROTECTED = "Inline $`a \\leq b`$, and displayed:\n\n```math\n\\sum_{i<n} i\n```"
_PROTECTED_HTML = ('<span class="arithmatex">\\(a \\leq b\\)</span>', '<div class="arithmatex">\\[\n\\sum_{i&lt;n} i\n\\]</div>')


def test_the_protected_formula_forms_are_formulas_on_the_site(tmp_path: Path) -> None:
    """GitHub shows $`...`$ and a ```math fence as formulas, and the site's
    Markdown, which mkdocs.yml configures as check does, reads them so too,
    rather than as a dollar sign and code, and a code block."""

    text, errors = publishable_article(_PROTECTED)
    reading = site_converter().convert(text)

    assert errors == ()
    assert all(formula in reading for formula in _PROTECTED_HTML)
    assert "<code" not in reading
    blueprint = _vault(tmp_path)
    _with_article(blueprint, _PROTECTED)
    assert main(["check", str(blueprint)]) == 0
    pages = [page.read_text(encoding="utf-8") for page in _render(blueprint).rglob("*.md")]
    (page,) = [page for page in pages if "a \\leq b" in page]
    assert all(formula in site_converter().convert(page) for formula in _PROTECTED_HTML)



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
        # Nothing on the site draws a Mermaid block it did not write.
        "```mermaid\ngraph LR\n  A --> B\n```",
        "Render draws the `mermaid` graphs.",
    ],
)
def test_what_an_article_may_still_say_is_accepted(article: str) -> None:
    assert publishable_article(f"# Top\n\n{article}\n")[1] == ()

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
def test_a_refusal_names_the_page_and_says_how_to_write_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], relative: str, written: str, expected: str
) -> None:
    """The fix for raw HTML says how Markdown writes what HTML is reached for."""

    blueprint = _vault(tmp_path)
    if relative == "base":
        _with_base_notes(blueprint, written)
    else:
        (blueprint / relative).write_text(written, encoding="utf-8")

    assert main(["check", str(blueprint)]) == 1
    assert expected in capsys.readouterr().out


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
    ("article", "line"),
    [
        ("Intro.\n\nLet $a<b$ hold.", 3),
        ("Intro.\n\n$$\\sum_{i<n} x$$", 3),
        ("Intro.\n\n$$\n\\sum_{i<n}\n$$", 4),
    ],
)
def test_a_tag_only_the_site_reads_in_a_formula_is_named_by_its_line(article: str, line: int) -> None:
    """A < before a letter in a formula is no tag to the pattern check reads
    HTML with, but the site's converter opens one there and publishes the
    markup render puts after the text as HTML. The refusal names the line
    the < is on, and how a formula keeps clear of it."""

    assert publishable_article(article)[1] == (f"line {line}: {_OPENED}",)


@pytest.mark.parametrize(
    ("reference", "shown"),
    [
        ("&#" + "9" * 39000 + ";", "&#99999999999999... (39003 characters) (U+FFFD REPLACEMENT CHARACTER)"),
        ("&#" + "0" * 5000 + "65;", "&#00000000000000... (5005 characters) (U+0041 LATIN CAPITAL LETTER A)"),
        ("&#x" + "f" * 39000 + ";", "&#xfffffffffffff... (39004 characters) (U+FFFD REPLACEMENT CHARACTER)"),
    ],
)
def test_a_long_character_reference_is_refused_by_name(reference: str, shown: str) -> None:
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
    """Markdown the site's own converter renders, which reads HTML, is refused
    for any HTML in it outside code, which it shows as typed, and for a
    character reference without its semicolon, which that converter completes."""

    errors = publishable_article(markup)[1]

    assert errors == () if reason is None else any(error.startswith(f"line 1: {reason}") for error in errors)


_FORGED = '<span class="bp-mark">FORGED MARK</span><script>document.title="INJECTED"</script>'


def _published(page: Path) -> str:
    """``page`` as MkDocs publishes it: its metadata read off, and the rest
    converted as the site's configuration converts it."""

    from mkdocs.utils.meta import get_data

    return site_converter().convert(get_data(page.read_text(encoding="utf-8"))[0])


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

_CHARACTERS = [")", "<", ">", '"', "\n", "`", "[", "]", "(", "$", "*", "\\", " "]
_WEB = "[Mathlib](https://leanprover-community.github.io/x.html)"
_KEPT = (
    "[the paper](../sources.md#lemma-3), [its text](../sources.md?plain=1#lemma-3), "
    "[as text](../sources.md?plain=1), [below](#later)"
)


def test_a_link_render_moves_keeps_its_destination_encoded(tmp_path: Path) -> None:
    """Render resolves a relative link's target decoded, as the file system
    names it, and writes path and fragment back encoded, in a link that stays
    a link, on every kind of page: a target that decodes to a parenthesis and
    tags is a dead link, not a script and a mark, and an angle-bracketed
    destination that loses its brackets is not split. Links between articles,
    into the sources, to an anchor, and out of the site keep their
    destinations, and a query stays out of the file's name, so the strict
    build still finds the file."""

    blueprint = _vault(tmp_path)
    (blueprint / "sources.md").write_text("# Paper\n", encoding="utf-8")
    moved = [(quote(f"a{character}b", safe=""), quote(character, safe="")) for character in _CHARACTERS]
    _with_article(
        blueprint,
        "\n\n".join(
            [
                f"See [the base](base.md), [its section](base.md#notes), {_KEPT}, [the chapter](README.md), and {_WEB}.",
                *(f"See [here]({target}.md) and [there](#{fragment}x)." for target, fragment in moved),
                "See [x](../sources.md#a<b`c).\n\nSee [y][r].\n\n[r]: <../sources.md#a) b<c`d>",
                _ENCODED_MARKUP,
            ]
        ),
    )
    chapter = blueprint / "roadmap/README.md"
    chapter.write_text(
        chapter.read_text(encoding="utf-8").replace("main result.", f"main result, {_ENCODED_MARKUP}"), encoding="utf-8"
    )
    (blueprint / "notes.md").write_text(f"# Notes\n\nThe notes, {_ENCODED_MARKUP}\n", encoding="utf-8")

    site = _render(blueprint)

    written = (site / "roadmap/README.md").read_text(encoding="utf-8")
    assert f"See [the base](#base), [its section](#base), {_KEPT}, [the chapter](#), and {_WEB}." in written
    page, notes = _published(site / "roadmap/README.md"), _published(site / "notes.md")
    for target, fragment in moved:
        assert f'<a href="{target}.md">here</a>' in page and f'<a href="#{fragment}x">there</a>' in page
    assert '<a href="../sources.md#a%3Cb%60c">x</a>' in page
    assert '<a href="../sources.md#a%29%20b%3Cc%60d">y</a>' in page
    markup = (
        '<a href="%29%3Cscript%3Edocument.title%3D1%3C/script%3E%3Cspan%20class%3D%22bp-mark%22%3EFORGED'
        '%3C/span%3E.md">here</a>'
    )
    assert (page.count(markup), notes.count(markup)) == (2, 1)
    assert not any("<script" in html or "FORGED</span>" in html for html in (page, notes))


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
    Check refuses the open fence, by its line, and render publishes
    nothing."""

    blueprint = _vault(tmp_path)
    _with_base_notes(blueprint, f"{fence}\n")
    _with_article(blueprint, f"The main result.\n\n{fence}\n{_FORGED} $\\gdef\\x{{1}}$\n{fence}")

    assert main(["check", str(blueprint)]) == 1
    out = capsys.readouterr().out
    assert "base: line 13: code fence is not closed" in out
    assert f"close it with a line of {fence}" in out
    with pytest.raises(PublicationError, match="code fence is not closed"):
        _render(blueprint)


def test_a_chapter_is_read_in_the_stretches_its_page_has(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Render replaces a slot with its statement, so the narrative on either
    side of it is read apart: a code span that crosses the slot in the
    narrative as written ends before it on the page, and what it held
    would be published as markup. Check reads each stretch as the page has
    it."""

    blueprint = _vault(tmp_path)
    _with_narrative(blueprint, f"## Definitions\n\n`a\n- [Base](base.md)\nb` and `c {_FORGED} d`")

    reason = "roadmap: line 12: raw HTML is not allowed: <span>, </span>, <script>"
    _refused(blueprint, capsys, reason, "raw HTML is not allowed")


def test_a_chapter_cannot_leave_a_fence_open_over_its_statements(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fence the narrative leaves open before a slot would run on into the
    statement render puts there."""

    blueprint = _vault(tmp_path)
    _with_narrative(blueprint, "## Definitions\n\n````\n\n- [Base](base.md)")

    assert main(["check", str(blueprint)]) == 1
    assert "roadmap: line 10: code fence is not closed" in capsys.readouterr().out


@pytest.mark.parametrize("notes", ["> ```\n> quoted\n", "- item\n\n    ```\n    indented\n"])
def test_a_fence_the_next_margin_line_ends_is_published(tmp_path: Path, notes: str) -> None:
    """An indented or quoted fence ends at the first line at the margin, and
    render's markup starts there, so leaving one open, in a leaf's notes or
    at the end of a page, hides nothing."""

    blueprint = _vault(tmp_path)
    _with_base_notes(blueprint, notes)
    (blueprint / "notes.md").write_text(f"# Notes\n\n{notes}", encoding="utf-8")

    assert main(["check", str(blueprint)]) == 0
    published = _published(_render(blueprint) / "roadmap/README.md")
    assert published.count('<span class="bp-mark"') == 2


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
    moment a reader followed a link to it. Check refuses any file not of a
    kind a browser only shows, by its path, and render publishes nothing; a
    figure, a PDF, a bibliography, plain text, and a copy of an asset render
    writes over still pass."""

    blueprint = _vault(tmp_path)
    (blueprint / "stylesheets").mkdir()
    (blueprint / "stylesheets/blueprint.css").write_text(".af-status { display: none }", encoding="utf-8")
    for relative, content in {**_RUNNABLE, **_SHOWN}.items():
        (blueprint / relative).parent.mkdir(parents=True, exist_ok=True)
        (blueprint / relative).write_bytes(content)

    assert main(["check", str(blueprint)]) == 1
    refused = {line.split(": ")[1] for line in capsys.readouterr().out.splitlines() if "the site publishes no " in line}
    assert refused == set(_RUNNABLE)
    with pytest.raises(PublicationError):
        _render(blueprint)
    for relative in _RUNNABLE:
        (blueprint / relative).unlink()
    site = _render(blueprint)
    assert all((site / relative).read_bytes() == content for relative, content in _SHOWN.items())
    assert "display: none" not in (site / "stylesheets/blueprint.css").read_text(encoding="utf-8")


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
def test_a_link_that_could_run_script_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], where: str, written: str, scheme: str
) -> None:
    """A link to a ``javascript:`` or ``data:`` address runs what it holds on
    the site's origin when a reader follows it, whatever the case of its
    scheme, and a link definition no link on its own text uses may be used
    by another text on the page. Check refuses every link and definition
    that leads anywhere but the site's own pages and http, https, and
    mailto addresses, and render publishes nothing."""

    blueprint = _vault(tmp_path)
    if where == "base":
        _with_base_notes(blueprint, written)
    elif where == "roadmap":
        _with_narrative(blueprint, written)
    else:
        (blueprint / where).write_text(written, encoding="utf-8")

    assert main(["check", str(blueprint)]) == 1
    out = capsys.readouterr().out
    assert re.search(rf"{re.escape(where)}: lines? \d+.*: links are not allowed to {scheme}: addresses", out), out
    with pytest.raises(PublicationError, match=f"links are not allowed to {scheme}:"):
        _render(blueprint)


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

    def recording(text: str) -> tuple[str, tuple[str, ...]]:
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


_UNSAFE_PAGE = "<script>document.title='X'</script>\n\nText\n{: .bp-readback .bp-readback-current }\n"


@pytest.mark.parametrize("relative", ["README.md", "coverage/README.md", "notes.md", "more/notes.mdown"])
def test_a_page_that_is_not_an_article_is_held_to_the_same_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], relative: str
) -> None:
    """The landing page, the coverage contract, and any other Markdown page
    are published on the site's origin as articles are, so check refuses
    script and attribute lists in them, naming the file, and render refuses
    to publish them."""

    blueprint = _vault(tmp_path)
    page = blueprint / relative
    page.parent.mkdir(parents=True, exist_ok=True)
    before = page.read_text(encoding="utf-8") if page.exists() else "# Notes\n"
    page.write_text(f"{before.rstrip()}\n\n{_UNSAFE_PAGE}", encoding="utf-8")

    assert main(["check", str(blueprint)]) == 1
    out = capsys.readouterr().out
    lines = len(before.rstrip().splitlines()) + 2
    assert f"error: {relative}: line {lines}: raw HTML is not allowed: <script>, </script>" in out
    assert f"error: {relative}: line {lines + 3}: attribute list {{: .bp-readback .bp-readback-current }}" in out
    with pytest.raises(PublicationError):
        _render(blueprint)


def test_the_landing_page_is_checked_without_the_dashboard_render_adds(tmp_path: Path) -> None:
    """Render adds HTML of its own around the landing page's text; only the
    author's text is checked, so a plain landing page passes."""

    blueprint = _vault(tmp_path)

    assert main(["check", str(blueprint)]) == 0
    assert '<div class="bp-landing"' in (_render(blueprint) / "README.md").read_text(encoding="utf-8")


def test_a_page_that_is_not_utf8_is_refused_by_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = _vault(tmp_path)
    (blueprint / "notes.md").write_bytes(b"# Notes\n\n\xff\n")

    assert main(["check", str(blueprint)]) == 1
    assert "error: notes.md: is not UTF-8 text, as a Markdown page must be; save it as UTF-8" in capsys.readouterr().out


def test_the_scaffolded_pages_pass_the_page_check(tmp_path: Path) -> None:
    from autoform_cli.graph import load_graph
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

    assert main(["check", str(blueprint)]) == 1
    assert f"roadmap: line 19: {_HTML}" in capsys.readouterr().out
    with pytest.raises(PublicationError, match="roadmap: line 19: "):
        _render(blueprint)


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
        ("Claim.\n{: .bp-readback .bp-readback-current }", "{: .bp-readback .bp-readback-current }"),
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
def test_an_attribute_list_is_refused(article: str, shown: str) -> None:
    """An attribute list could make an article's text look like a card, a
    mark, or an approval, take the id of the site's own element, or cover
    the page; a heading's id comes from its title."""

    errors = publishable_article(f"# Top\n\n{article}\n")[1]

    line = 3 + article[: article.index(shown)].count("\n")
    assert errors == (f"line {line}: attribute list {shown} {_ATTRIBUTE_RULE}",)

_FENCE_RULE = 'a code fence may only name its language, as in "```lean" or "```{.lean}"; keep only the language'


@pytest.mark.parametrize(
    ("article", "shown"),
    [
        ("```{.text .bp-readback .bp-readback-current}\nApproved.\n```", "{.text .bp-readback .bp-readback-current}"),
        ("```text {.bp-mark #claim data-kind=result}\nx\n```", "{.bp-mark #claim data-kind=result}"),
        ('```text {style="position:fixed;inset:0;z-index:1000"}\nx\n```', '{style="position:fixed;inset:0;z-index:1000"}'),
        ("``` { .lean #anchor }\nx\n```", "{ .lean #anchor }"),
        ("- item\n\n    ```{.text .bp-mark}\n    x\n    ```", "{.text .bp-mark}"),
        ("> ~~~{.text .bp-graph}\n> graph LR\n> ~~~", "{.text .bp-graph}"),
    ],
)
def test_a_fence_header_that_sets_more_than_a_language_is_refused(article: str, shown: str) -> None:
    """A class, an id, or another attribute in a fence's braces lands on the
    code block's element, as one in an attribute list does, so it could make
    the block look like a card or a mark, cover the page, or be drawn as a
    diagram."""

    errors = publishable_article(f"# Top\n\n{article}\n")[1]

    line = 3 + article[: article.index(shown)].count("\n")
    assert errors == (f"line {line}: attribute list {shown} is not allowed: {_FENCE_RULE}",)



def test_a_fence_that_imitates_a_graph_render_drew_is_refused() -> None:
    """The site's diagram script draws an element of the class render marks
    its graphs with."""

    errors = publishable_article("# Top\n\n```{.text .mermaid .bp-graph}\ngraph LR\n```\n")[1]

    assert errors == (f"line 3: attribute list {{.text .mermaid .bp-graph}} is not allowed: {_FENCE_RULE}",)


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
    # Across, ink stops at the edge of the block an article formula is in; a
    # card's blocks are left to scroll with it.
    block = rules[":is(p, h1, h2, h3, h4, h5, h6, ul, ol, td, th):has(mjx-container):not(.bp-readback *)"]
    assert "overflow-x: clip" in block and "overflow-y" not in block
