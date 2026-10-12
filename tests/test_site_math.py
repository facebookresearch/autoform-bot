"""The site's mathematics: the MathJax configuration ``autoform render``
writes, the TeX and HTML an article may carry, and the styles that keep each
formula in its place."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.markdown import site_converter, statement_and_notes
from autoform_cli.publishable import publishable_article
from autoform_cli.render import PublicationError, render_site
from autoform_cli.scaffold import scaffold_project
from tests.test_render import _project
from tests.undecodable_json import _too_deep_to_decode

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = _ROOT / "skills/setup/assets/cabannes-thesis-project"
_BASE = "https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5"
_BUNDLE = f"{_BASE}/tex-mml-chtml.js"
_PAGE_PACKAGES = ["base", "ams", "noundefined", "boldsymbol", "cancel", "mathtools", "configmacros"]
_CARD_PACKAGES = ["base", "ams", "noundefined"]
_MACROS = {"RR": "\\mathbb{R}", "norm": ["\\left\\lVert #1 \\right\\rVert", 1]}

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
    assert {"ui/safe", "[tex]/boldsymbol", "[tex]/cancel", "[tex]/mathtools"} <= set(settings["load"])
    assert settings["page"]["packages"] == _PAGE_PACKAGES
    assert settings["card"]["packages"] == _CARD_PACKAGES
    assert "macros" not in settings["card"]
    assert settings["safe"] == {"allow": {"URLs": "none", "classes": "none", "cssIDs": "none", "styles": "none"}}


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
    assert _settings(script)["card"]["packages"] == _CARD_PACKAGES


def test_an_edited_configuration_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Its edits would be lost, so neither check nor render goes ahead."""

    kept = _SCAFFOLDED.replace("  tex: {\n", '  tex: {\n    macros: {RR: "\\\\mathbb{R}"},\n')
    blueprint = _vault(tmp_path, kept=kept)
    reason = "move any tex.macros to tex-macros.json and delete it"

    _refused(blueprint, capsys, reason, reason)


def test_project_macros_reach_article_formulas_only(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, macros=json.dumps(_MACROS))

    assert main(["check", str(blueprint)]) == 0
    settings = _settings((_render(blueprint) / "javascripts/mathjax.js").read_text(encoding="utf-8"))
    assert settings["page"]["macros"] == _MACROS
    assert "macros" not in settings["card"]


def _changed_after_capture(monkeypatch: pytest.MonkeyPatch, change: Callable[[], object]) -> None:
    """Make every capture of the blueprint call ``change`` once it has read it."""

    import autoform_cli.render as render_module

    capture = render_module._capture_publication

    def capture_then_change(*args, **kwargs):
        snapshot = capture(*args, **kwargs)
        change()
        return snapshot

    monkeypatch.setattr(render_module, "_capture_publication", capture_then_change)


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
    assert _settings((site / "javascripts/mathjax.js").read_text(encoding="utf-8"))["page"]["macros"] == published


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


_LONE = (
    "tex-macros.json: \\{} ends a body or default in a single backslash, which would join the text after it "
    "into one command; double it or remove it"
)

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
        ('{"f": ["#1", 10]}', "\\f takes 10 arguments; use 0 to 9"),
        ('{"f": ["#1", true]}', "\\f must be a body, [body, arguments], or [body, arguments, default]"),
        ('{"f": "\\\\DeclareMathOperator{\\\\leq}{>}"}', "\\f uses \\DeclareMathOperator, which would change"),
        ('{"f": ["#1", 1, "\\\\label{x}"]}', "\\f uses \\label, which would change other formulas"),
        ('{"g": ["\\\\mmlToken{mi}[mathcolor=#1]{x}", 1]}', "\\g uses \\mmlToken, which sets a symbol's color"),
        ('{"s": "\\\\cancel"}', "\\s leaves \\cancel room for an option, which sets the color"),
        ('{"s": ["\\\\bcancel[mathcolor=red]{#1}", 1]}', "\\s leaves \\bcancel room for an option"),
        ('{"s": ["\\\\xcancel #1", 1]}', "\\s leaves \\xcancel room for an option"),
        ('{"s": ["#1{x}", 1, "\\\\cancelto"]}', "\\s leaves \\cancelto room for an option"),
        ('{"t": ["#1[mathcolor=green]{x}", 1]}', "\\t puts a [ or another argument right after #1"),
        ('{"t": ["#1 #2", 2]}', "\\t puts a [ or another argument right after #1"),
        ('{"bs": "\\\\"}', _LONE.format("bs")),
        ('{"bs": "a\\\\\\\\\\\\"}', _LONE.format("bs")),
        ('{"L": ["#1label", 1, "\\\\"]}', _LONE.format("L")),
    ],
)
def test_project_macros_that_cannot_be_used_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], macros: str, reason: str
) -> None:
    # Decoded here, nearer the stack's base than check and render decode it.
    if macros == _NESTED and not _too_deep_to_decode(macros):
        pytest.skip("this stack holds a hundred thousand nested arrays")
    blueprint = _vault(tmp_path, macros=macros)

    _refused(blueprint, capsys, reason, reason)


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


@pytest.mark.parametrize(("make", "kind"), [(_directory_with_a_file, "a directory"), (_symlink, "a symlink")])
def test_something_other_than_a_file_where_the_site_has_one_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], make, kind: str
) -> None:
    """Render reads the project's macros from the vault, so a directory or
    symlink in the way is named by check rather than crashing, hanging, or
    being skipped by render."""

    blueprint = _vault(tmp_path)
    make(blueprint / "tex-macros.json")

    _refused(blueprint, capsys, f"tex-macros.json: is {kind}, where autoform reads the project's macros from a file; remove it")


#: Every file render writes for the test vault that is not one of the
#: vault's own, and the legacy derived views it keeps out of the site.
_WRITTEN_BY_RENDER = (
    "SUMMARY.md",
    "assets/autoform.svg",
    "dependencies.html",
    "dependencies.md",
    "dependencies/chapters/roadmap.md",
    "dependencies/full.md",
    "dependencies/nodes/base.md",
    "dependencies/nodes/top.md",
    "graph.html",
    "javascripts/blueprint-live.js",
    "javascripts/blueprint-mermaid.js",
    "javascripts/mathjax.js",
    "progress.md",
    "publication.json",
    "structure.md",
    "stylesheets/blueprint.css",
)


def test_the_guarded_paths_are_every_file_render_writes(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path)

    site = _render(blueprint)

    written = {
        path.relative_to(site).as_posix()
        for path in site.rglob("*")
        if path.is_file() and not (blueprint / path.relative_to(site)).is_file()
    }
    assert written <= set(_WRITTEN_BY_RENDER)


@pytest.mark.parametrize(("make", "kind"), [(_directory_with_a_file, "a directory"), (_symlink, "a symlink"), (_fifo, "a special file")])
@pytest.mark.parametrize("relative", _WRITTEN_BY_RENDER)
def test_something_other_than_a_file_where_render_writes_one_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], relative: str, make, kind: str
) -> None:
    """One rule covers every file render writes: a directory, symlink, or
    special file there is named by check, and render refuses it before
    writing rather than stopping half way with an OSError."""

    blueprint = _vault(tmp_path)
    make(blueprint / relative)
    reason = f"{relative}: is {kind}, where autoform render writes a file; remove it"

    # Capture refuses a symlink or special file it would publish first.
    _refused(blueprint, capsys, reason)


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


_CHANGES = "TeX commands that change other formulas are not allowed: "
_LOOKS = "TeX commands that set what a symbol looks like are not allowed: "
_DEFINE = "; define notation in the vault's tex-macros.json instead, and put a command you only name in code"
_MML_TOKEN = f"{_LOOKS}\\mmlToken; write the symbol itself, and put a command you only name in code"


def _strike(command: str) -> str:
    return (
        f"TeX strikes must be followed by their argument: {command}; an option in brackets, or one a macro "
        f"supplies, sets the color and other attributes of the strike, so write {command}{{...}}"
    )


@pytest.mark.parametrize(
    ("article", "reason"),
    [
        # Every article on a page is typeset with one TeX input, so a
        # definition in one would change what the others show.
        ("$\\DeclareMathOperator{\\leq}{>}$, so $a \\leq b$.", f"{_CHANGES}\\DeclareMathOperator{_DEFINE}"),
        ("Here \\(\\newcommand{\\RR}{\\mathbb{R}}\\) is live.", f"{_CHANGES}\\newcommand{_DEFINE}"),
        ("$$\n\\def\\x{1} \\let\\y\\x\n$$", f"{_CHANGES}\\def, \\let{_DEFINE}"),
        ("$\\DeclarePairedDelimiter\\abs{\\lvert}{\\rvert}$", f"{_CHANGES}\\DeclarePairedDelimiter{_DEFINE}"),
        ("$\\newtagform{p}{(}{)} \\usetagform{p}$", f"{_CHANGES}\\newtagform, \\usetagform{_DEFINE}"),
        ("$x = 1 \\label{one}$", f"{_CHANGES}\\label{_DEFINE}"),
        ("$\\require{mhchem}$", f"{_CHANGES}\\require{_DEFINE}"),
        ("$\\gdef\\x{1}$ and $\\global\\edef\\y{2}$", f"{_CHANGES}\\gdef, \\global, \\edef{_DEFINE}"),
        ("$\\mathtoolsset{showonlyrefs}$", f"{_CHANGES}\\mathtoolsset{_DEFINE}"),
        # The forms GitHub keeps the TeX of are formulas on the site too.
        ("$`\\def\\x{1}`$ is code to GitHub's Markdown.", f"{_CHANGES}\\def{_DEFINE}"),
        ("```math\n\\gdef\\x{1} \\label{one}\n```", f"{_CHANGES}\\gdef, \\label{_DEFINE}"),
        # Base's \mmlToken gives a symbol the color and background a formula
        # writes, which the site's filter keeps, so an article could color a
        # symbol like the site's status marks; so can cancel's strikes, given
        # a color in brackets.
        ("$\\mmlToken{mi}[mathcolor=#31A24C]{\\checkmark}$ approved", _MML_TOKEN),
        ("$$\n\\mmlToken{mtext}[mathbackground=white]{x}\n$$", _MML_TOKEN),
        ("Here \\(\\mmlToken{mo}{+}\\) is live.", _MML_TOKEN),
        ("$`\\mmlToken{mi}[mathcolor=red]{x}`$ approved", _MML_TOKEN),
        ("```math\n\\mmlToken{mo}{+}\n```", _MML_TOKEN),
        ("$\\cancel[mathcolor=#31A24C]{x}$ approved", _strike("\\cancel")),
        ("$$\n\\bcancel [mathbackground=white]{x}\n$$", _strike("\\bcancel")),
        ("$`\\xcancel[color=red,data-thickness=900]{x}`$", _strike("\\xcancel")),
        ("```math\n\\cancelto[mathcolor=red]{0}{x}\n```", _strike("\\cancelto")),
        # A strike a macro's argument ends with takes what follows the macro.
        ("$\\id{\\cancel}[mathcolor=red]{x}$", _strike("\\cancel")),
        ("$\\id[\\cancel][mathcolor=red]{x}$", _strike("\\cancel")),
    ],
)
def test_tex_that_reaches_past_its_own_formula_is_refused(article: str, reason: str) -> None:
    errors = publishable_article(article)[1]
    # Each is refused on the line it is on.
    line = "line 2" if article.startswith(("$$\n", "```math\n")) else "line 1"

    assert errors == (f"{line}: {reason}",)


def test_a_strike_with_its_argument_is_published(tmp_path: Path) -> None:
    """A strike given its argument, in braces or as one symbol, and a macro
    that gives it one, still pass."""

    blueprint = _vault(tmp_path, macros=json.dumps({"crossed": ["\\cancel{#1}", 1], "pair": ["[#1, #2]", 2]}))
    _with_article(blueprint, "$\\cancel{x} + \\bcancel y = \\cancelto{0}{z} \\crossed{w} \\pair{a}{b}$")

    assert main(["check", str(blueprint)]) == 0
    assert "\\cancelto{0}{z}" in (_render(blueprint) / "roadmap/README.md").read_text(encoding="utf-8")


_OPENED = (
    "raw HTML is not allowed: the site would read a < before a letter as the start of a tag, and publish "
    "'</div>' after it as HTML; in a formula, put a space after <, as in $a < b$"
)
_DIAGRAM_RULE = (
    "Mermaid diagrams are not allowed: the site draws only the dependency graphs autoform render makes; "
    "show a diagram's source in a ```text fence instead"
)


@pytest.mark.parametrize(
    ("article", "reason"),
    [
        ("First.\n\nThen $\\def\\x{1}$.", f"error: top: line 13: {_CHANGES}\\def; define notation"),
        ("First.\n\nThen $\\mmlToken{mi}{x}$.", f"error: top: line 13: {_LOOKS}\\mmlToken; write the symbol"),
        (
            "Then $\\def\\x{1}$,\n\nand\n\n$$\n\\let\\y\\x\n$$",
            f"error: top: lines 11, 16: {_CHANGES}\\def, \\let; define notation",
        ),
        # A command only named in code is not the one refused.
        ("Write `\\def` in code.\n\nThen $\\def\\x{1}$.", f"error: top: line 13: {_CHANGES}\\def; define"),
        # Nor is a longer command that starts with its name.
        ("Prose names \\defined.\n\nThen $\\def\\x{1}$.", f"error: top: line 13: {_CHANGES}\\def; define"),
        ("First.\n\nThen <b>bold</b>.", "error: top: line 13: raw HTML is not allowed: <b>, </b>;"),
        # A formula written as code is a formula, not code.
        ("Write `\\def` in code.\n\nThen $`\\def\\x{1}`$.", f"error: top: line 13: {_CHANGES}\\def; define"),
        ("First.\n\n```math\n\\mmlToken{mi}{x}\n```", f"error: top: line 14: {_LOOKS}\\mmlToken; write"),
        ("First.\n\nThen $a<b$.", f"error: top: line 13: {_OPENED}"),
        ("First.\n\n```mermaid\ngraph LR\n  A --> B\n```", f"error: top: line 13: {_DIAGRAM_RULE}"),
        pytest.param(
            "First.\n\nThe &#" + "9" * 39000 + ";.",
            "error: top: line 13: HTML character references are not allowed: &#99999999999999... (39003 characters)",
            id="long-reference",
        ),
    ],
)
def test_a_refusal_names_the_article_and_the_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], article: str, reason: str
) -> None:
    """The statement is on line 11 of the article, after its metadata."""

    blueprint = _vault(tmp_path)
    _with_article(blueprint, article)

    _refused(blueprint, capsys, reason, reason.removeprefix("error: "))


def test_a_refusal_in_a_chapter_names_the_chapter_and_the_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = _vault(tmp_path)
    chapter = blueprint / "roadmap/README.md"
    chapter.write_text(chapter.read_text(encoding="utf-8") + "\nLast, $\\mmlToken{mi}{x}$.\n", encoding="utf-8")
    line = len(chapter.read_text(encoding="utf-8").splitlines())

    assert main(["check", str(blueprint)]) == 1
    assert f"error: roadmap: line {line}: {_LOOKS}\\mmlToken;" in capsys.readouterr().out


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
        "Write `\\newcommand` in a project's macros file instead, and `\\mmlToken` nowhere.",
        "Prose that names \\newcommand, \\label{x}, or \\mmlToken outside a formula is shown as typed.",
        "After $a \\leq b$ ends, prose that names \\newcommand is shown as typed.",
        # An escaped delimiter is a character, and what follows it no formula.
        "Here \\\\(\\\\newcommand{\\\\RR}{\\\\mathbb{R}}\\\\) and \\\\(\\\\mmlToken{mo}{+}\\\\) are shown as typed.",
        "```latex\n\\def\\x{1}\n\\DeclareMathOperator{\\rank}{rank}\n```",
        "$\\left( x \\right) \\leqq y$, $\\operatorname{rank} A$, and $\\lvert x \\rvert$.",
    ],
)
def test_tex_that_changes_nothing_else_is_accepted(article: str) -> None:
    assert publishable_article(article)[1] == ()


@pytest.mark.parametrize(
    "article",
    [
        "A `List<T>` holds values of type `T`.",
        "```\n<b>bold</b> &amp; <!-- kept -->\n```",
        "Spent on R&D; and AT&T, as `&amp;` shows.",
    ],
)
def test_markup_shown_as_typed_is_accepted(article: str) -> None:
    """Code is escaped, and a reference that names no character is shown as
    written, so neither is HTML on the site."""

    assert publishable_article(article)[1] == ()


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
        (
            "notes.md",
            "# Notes\n\nA remark.\n{.bp-statement}\n",
            "notes.md: line 4: attribute list {.bp-statement} is not allowed: a page may only give a heading an id",
        ),
        (
            "base",
            "A remark.\n{.bp-statement}",
            "base: line 14: attribute list {.bp-statement} is not allowed: an article may only give a heading an id",
        ),
    ],
)
def test_a_refusal_says_what_the_page_is_and_how_to_write_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], relative: str, written: str, expected: str
) -> None:
    """A page other than an article is called a page, and the fix for raw
    HTML says how Markdown writes what HTML is reached for."""

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
        ("For $a < b$ and $b > c$, AT&T.", None),
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


def _with_page(blueprint: Path, place: str, text: str) -> str:
    """Put ``text`` in the blueprint at ``place``, and return the page of the
    site source it is published on."""

    if place == "statement":
        _with_article(blueprint, text)
        return "roadmap/README.md"
    if place == "narrative":
        chapter = blueprint / "roadmap/README.md"
        chapter.write_text(
            chapter.read_text(encoding="utf-8").replace("before the main result.", f"before the main result, {text}"),
            encoding="utf-8",
        )
        return "roadmap/README.md"
    (blueprint / "notes.md").write_text(f"# Notes\n\nThe notes, {text}\n", encoding="utf-8")
    return "notes.md"


@pytest.mark.parametrize("place", ["statement", "narrative", "page"])
def test_a_link_render_moves_publishes_no_markup(tmp_path: Path, place: str) -> None:
    """Render resolves a relative link's target decoded, as the file system
    names it, and writes it back encoded, so a target that decodes to a
    parenthesis and tags is a dead link on the site, not a script and a
    mark."""

    blueprint = _vault(tmp_path)
    page = _with_page(blueprint, place, _ENCODED_MARKUP)

    published = _published(_render(blueprint) / page)

    assert "<script" not in published
    assert "FORGED</span>" not in published
    assert '<a href="%29%3Cscript%3Edocument.title%3D1%3C/script%3E%3Cspan%20class%3D%22bp-mark%22%3EFORGED%3C/span%3E.md">here</a>' in published


@pytest.mark.parametrize("character", [")", "<", ">", '"', "\n", "`", "[", "]", "(", "$", "*", "\\", " "])
def test_a_link_render_moves_keeps_its_target_encoded(tmp_path: Path, character: str) -> None:
    """Whatever a target decodes to, the destination render writes is the
    encoded path, in a link that stays a link."""

    from urllib.parse import quote

    blueprint = _vault(tmp_path)
    target = quote(f"a{character}b", safe="")
    _with_article(blueprint, f"See [here]({target}.md) and [there](#{quote(character, safe='')}x).")

    published = _published(_render(blueprint) / "roadmap/README.md")

    assert f'<a href="{target}.md">here</a>' in published
    assert f'<a href="#{quote(character, safe="")}x">there</a>' in published


def test_links_render_moves_resolve_as_before(tmp_path: Path) -> None:
    """Links between articles, into the sources, to an anchor, and out of the
    site keep the destinations they had. A query is no part of the file's
    name, so render moves the path and keeps the query, and the strict build
    still finds the file."""

    blueprint = _vault(tmp_path)
    (blueprint / "sources.md").write_text("# Paper\n", encoding="utf-8")
    links = (
        "[the paper](../sources.md#lemma-3), [its text](../sources.md?plain=1#lemma-3), "
        "[as text](../sources.md?plain=1), [below](#later)"
    )
    _with_article(
        blueprint,
        f"See [the base](base.md), [its section](base.md#notes), {links}, [the chapter](README.md), and "
        "[Mathlib](https://leanprover-community.github.io/x.html).",
    )

    written = (_render(blueprint) / "roadmap/README.md").read_text(encoding="utf-8")

    assert (
        f"See [the base](#base), [its section](#base), {links}, [the chapter](#), and "
        "[Mathlib](https://leanprover-community.github.io/x.html)."
    ) in written


@pytest.mark.parametrize(
    ("text", "href"),
    [
        ("See [x][r].\n\n[r]: <../sources.md#a) b<c`d>", "../sources.md#a%29%20b%3Cc%60d"),
        ("See [x](../sources.md#a<b`c).", "../sources.md#a%3Cb%60c"),
    ],
)
def test_a_link_render_moves_keeps_its_fragment_encoded(tmp_path: Path, text: str, href: str) -> None:
    """Render writes a moved link's fragment encoded as well as its path: an
    angle-bracketed destination loses its brackets when it is rewritten, so
    a raw parenthesis, space, ``<`` or backtick in the fragment would end or
    split the link."""

    blueprint = _vault(tmp_path)
    (blueprint / "sources.md").write_text("# Paper\n", encoding="utf-8")
    _with_article(blueprint, text)

    published = _published(_render(blueprint) / "roadmap/README.md")

    assert f'<a href="{href}">x</a>' in published


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


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("help.html", b"<script>alert(document.cookie)</script>"),
        ("figures/plot.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
        ("sw.js", b"self.addEventListener('fetch', () => {});"),
        ("figures/page.XHTML", b"<html xmlns='http://www.w3.org/1999/xhtml'><script>1</script></html>"),
        ("stylesheets/extra.css", b".af-status { display: none }"),
        ("LICENSE", b"<script>1</script>"),
    ],
)
def test_a_file_a_browser_could_run_is_not_published(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], relative: str, content: bytes
) -> None:
    """Render copies a vault's other files into the site as they are, so an
    HTML page, an SVG or a script there would be the site's own markup the
    moment a reader followed a link to it. Check refuses any file not of a
    kind a browser only shows, by its path, and render publishes nothing."""

    blueprint = _vault(tmp_path)
    (blueprint / relative).parent.mkdir(parents=True, exist_ok=True)
    (blueprint / relative).write_bytes(content)

    _refused(blueprint, capsys, f"{relative}: the site publishes no ", relative)


def test_an_image_or_a_document_is_published_as_it_is(tmp_path: Path) -> None:
    """A figure, a PDF, a bibliography, plain text, and a copy of an asset
    render writes over still pass."""

    blueprint = _vault(tmp_path)
    (blueprint / "stylesheets").mkdir()
    (blueprint / "stylesheets/blueprint.css").write_text(".af-status { display: none }", encoding="utf-8")
    kept = {
        "figures/plot.PNG": b"\x89PNG\r\n\x1a\n",
        "figures/photo.jpeg": b"\xff\xd8\xff",
        "paper.pdf": b"%PDF-1.7\n",
        "refs.bib": b"@book{x, title={X}}\n",
        "notes.txt": b"plain\n",
    }
    for relative, content in kept.items():
        (blueprint / relative).parent.mkdir(parents=True, exist_ok=True)
        (blueprint / relative).write_bytes(content)

    assert main(["check", str(blueprint)]) == 0
    site = _render(blueprint)
    for relative, content in kept.items():
        assert (site / relative).read_bytes() == content
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

    def recording(text: str, reserved: frozenset[str], *, kind: str = "article") -> tuple[str, list[str]]:
        judged.append(text)
        return publishable_article(text, reserved, kind=kind)

    blueprint = _vault(tmp_path)
    _with_article(blueprint, "See [the base](base.md).")
    (blueprint / "notes.md").write_text("# Notes\n\nSee [the result](roadmap/top.md).\n", encoding="utf-8")
    monkeypatch.setattr(render_module, "publishable_article", recording)

    assert main(["check", str(blueprint)]) == 0

    assert any("See [the base](#base)." in text for text in judged)
    assert any("See [the result](roadmap/README.md#top)." in text for text in judged)
    assert not any("base.md" in text or "roadmap/top.md" in text for text in judged)


_UNSAFE_PAGE = (
    "<script>document.title='X'</script>\n\n"
    "Text\n{: .bp-readback .bp-readback-current }\n\n"
    "$\\DeclareMathOperator{\\leq}{>}$\n"
)


@pytest.mark.parametrize("relative", ["README.md", "coverage/README.md", "notes.md", "more/notes.mdown"])
def test_a_page_that_is_not_an_article_is_held_to_the_same_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], relative: str
) -> None:
    """The landing page, the coverage contract, and any other Markdown page
    are published on the site's origin as articles are, so check refuses
    script, attribute lists, and stateful TeX in them, naming the file, and
    render refuses to publish them."""

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
    assert f"error: {relative}: line {lines + 5}: TeX commands that change other formulas" in out
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


@pytest.mark.parametrize("separator", ["\u2028", "\x0b", "\x0c", "\x1e", "\x85"])
def test_a_line_break_markdown_does_not_read_is_checked_as_the_site_writes_it(
    tmp_path: Path, separator: str
) -> None:
    """The site writes every line break Python reads as a newline, so text
    after one is checked on a line of its own, where it is HTML, not code."""

    blueprint = _vault(tmp_path)
    _with_article(blueprint, f"Every object is equal to itself.\n\n    x = 1{separator}{_FORGED}")

    assert main(["check", str(blueprint)]) == 1
    with pytest.raises(PublicationError, match=r"top: line \d+: raw HTML is not allowed: <span>, </span>, <script>, </script>"):
        _render(blueprint)


@pytest.mark.parametrize("separator", ["\u2028", "\x0c", "\x85"])
@pytest.mark.parametrize(
    ("payload", "refusal"),
    [
        (_FORGED, r"roadmap: line \d+: raw HTML is not allowed: <span>, </span>, <script>, </script>"),
        (
            r"$\DeclareMathOperator{\leq}{>}$",
            r"roadmap: line \d+: TeX commands that change other formulas are not allowed: \\DeclareMathOperator",
        ),
    ],
)
def test_a_page_with_a_line_break_markdown_does_not_read_is_checked_as_the_site_writes_it(
    tmp_path: Path, separator: str, payload: str, refusal: str
) -> None:
    """A chapter page is published whole, line by line, its dependency
    section included, so it is checked that way too: after the break, the
    text is a paragraph, not code."""

    blueprint = _vault(tmp_path)
    chapter = blueprint / "roadmap/README.md"
    chapter.write_text(
        chapter.read_text(encoding="utf-8") + f"\n## Depends on\n\n    x = 1{separator}{payload}\n", encoding="utf-8"
    )

    assert main(["check", str(blueprint)]) == 1
    with pytest.raises(PublicationError, match=refusal):
        _render(blueprint)


@pytest.mark.parametrize(
    "metadata",
    [
        # Without YAML frontmatter, MkDocs takes leading "key: value" lines
        # for metadata.
        "Note: an aside\n",
        # The graph reads no frontmatter after a byte order mark, but MkDocs
        # drops the mark, and it ends YAML frontmatter at "..." as well.
        "\ufeff---\nnote: an aside\n...\n",
    ],
)
def test_a_page_is_checked_without_the_metadata_mkdocs_reads_off(tmp_path: Path, metadata: str) -> None:
    """The page MkDocs publishes starts after the metadata it reads off, and
    that page is checked: there the indented line belongs to the list item
    and is HTML, not code."""

    blueprint = _vault(tmp_path)
    chapter = blueprint / "roadmap/README.md"
    body = chapter.read_text(encoding="utf-8").removeprefix("---\n---\n\n")
    chapter.write_text(f"{metadata}- item\n\n    {_FORGED}\n\n{body}", encoding="utf-8")

    assert main(["check", str(blueprint)]) == 1
    with pytest.raises(
        PublicationError, match=r"roadmap: line \d+: raw HTML is not allowed: <span>, </span>, <script>, </script>"
    ):
        _render(blueprint)


def test_check_and_render_share_one_reading_of_a_statement() -> None:
    """The statement and the sections after it are what the box publishes,
    and they are what check reads."""

    statement, notes = statement_and_notes(
        "---\ndeclaration: theorem\n---\n\n# Top\n\n\n    x = 1\n\nClaim.\n\n## Sources\n\nA book.\n\n"
        "## Depends on\n\n- [Base](base.md)\n"
    )

    assert statement == "    x = 1\n\nClaim."
    assert notes == "###### Sources\n\nA book."


def test_an_article_that_ends_in_blank_lines_is_read() -> None:
    """Check reads every article line by line, so blank lines at the end
    of one are lines like any other."""

    assert statement_and_notes("# Top\n\nClaim.\n\n\n") == ("Claim.", "")
    assert statement_and_notes("---\nkind: x\n---\n") == ("", "")


_ATTRIBUTE_RULE = 'an article may only give a heading an id, as in "## Title {#title}"; delete it or keep only a heading\'s id'
_ID_RULE = (
    "start it with a letter, use only letters, digits, hyphens, and underscores, up to 64 characters, "
    "and do not start it with bp-, autoform, mjx-, mermaid, md-, or __"
)


@pytest.mark.parametrize(
    ("article", "shown"),
    [
        ("Claim.\n{: .bp-readback .bp-readback-current }", "{: .bp-readback .bp-readback-current }"),
        ('Claim.\n{: style="position:fixed;inset:0;z-index:1000" }', '{: style="position:fixed;inset:0;z-index:1000" }'),
        (
            "Claim.\n{: #autorun tabindex=\"-1\" autofocus=\"autofocus\" onfocus=\"document.title='x'\" }",
            "{: #autorun tabindex=\"-1\" autofocus=\"autofocus\" onfocus=\"document.title='x'\" }",
        ),
        ("Claim.\n{: #claim }", "{: #claim }"),
        ("A *claim*{ .bp-mark } here.", "{ .bp-mark }"),
        ("$x${hidden}", "{hidden}"),
        ("- item\n  {: .bp-mark }", "{: .bp-mark }"),
        ("| a {: .bp-mark } |\n| --- |\n| b |", "{: .bp-mark }"),
        ("Text `code`{.bp-mark} more.", "{.bp-mark}"),
        ("## Result {#main-result .highlight data-kind=result}", "{#main-result .highlight data-kind=result}"),
        ("## Result {: #one #two }", "{: #one #two }"),
    ],
)
def test_an_attribute_list_other_than_a_heading_id_is_refused(article: str, shown: str) -> None:
    """An attribute list could make an article's text look like a card, a
    mark, or an approval, or cover the page; an id on a heading is all an
    article needs, for links to land on it."""

    errors = publishable_article(f"# Top\n\n{article}\n")[1]

    line = 3 + article[: article.index(shown)].count("\n")
    assert errors == (f"line {line}: attribute list {shown} is not allowed: {_ATTRIBUTE_RULE}",)


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


@pytest.mark.parametrize(
    "article",
    ["```{.lean}\nx\n```", "``` { .lean }\nx\n```", '```lean title="Main.lean" linenums="1"\nx\n```', '```{.lean hl_lines="1"}\nx\n```'],
)
def test_a_fence_header_that_names_a_language_is_accepted(article: str) -> None:
    assert publishable_article(f"# Top\n\n{article}\n")[1] == ()


@pytest.mark.parametrize(
    ("article", "line"),
    [
        ('```mermaid\nflowchart LR\n  A["Read the card below"]\n  click A call eval("document.title=1")\n```', 3),
        ("``` {.mermaid}\ngraph LR\n```", 3),
        ("~~~mermaid\ngraph LR\n~~~", 3),
        ("- item\n\n    ```mermaid\n    graph LR\n    ```", 5),
        ("> ```mermaid\n> graph LR\n> ```", 3),
    ],
)
def test_a_mermaid_diagram_in_an_article_is_refused(article: str, line: int) -> None:
    """Mermaid with the loose security the site's graphs need runs a click's
    call as script and draws a label's markup, so an article cannot add a
    diagram."""

    assert publishable_article(f"# Top\n\n{article}\n")[1] == (f"line {line}: {_DIAGRAM_RULE}",)


def test_a_fence_that_imitates_a_graph_render_drew_is_refused() -> None:
    """The site's diagram script draws an element of class mermaid and the
    class render marks its graphs with."""

    errors = publishable_article("# Top\n\n```{.text .mermaid .bp-graph}\ngraph LR\n```\n")[1]

    assert errors == (
        f"line 3: attribute list {{.text .mermaid .bp-graph}} is not allowed: {_FENCE_RULE}",
        f"line 3: {_DIAGRAM_RULE}",
    )


@pytest.mark.parametrize("article", ["```text\ngraph LR\n  A --> B\n```", "Render draws the `mermaid` graphs."])
def test_mermaid_shown_as_code_is_accepted(article: str) -> None:
    assert publishable_article(f"# Top\n\n{article}\n")[1] == ()


@pytest.mark.parametrize(
    "identifier", ["bp-mark", "autoform-sweep", "mjx-eqn", "MJX-x", "mermaid-1", "md-content", "__drawer", "1st", "a:b", "a.b", "x" * 65]
)
def test_a_heading_id_like_the_sites_own_is_refused(identifier: str) -> None:
    errors = publishable_article(f"# Top\n\n## Result {{#{identifier}}}\n")[1]

    assert errors == (f"line 3: heading id {identifier!r} is not allowed: {_ID_RULE}",)


@pytest.mark.parametrize("heading", ["## A result {#main-result}", "## A result {: #main-result }", "### A_result-2 {#A_result-2}"])
def test_a_heading_id_is_accepted(heading: str) -> None:
    assert publishable_article(f"# Top\n\n{heading}\n\nText.\n")[1] == ()


@pytest.mark.parametrize("identifier", ["top", "base", "additional-formalization-targets"])
def test_a_heading_id_the_page_gives_its_own_element_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], identifier: str
) -> None:
    """A heading that took a statement's anchor would be where every link to
    that statement lands."""

    blueprint = _vault(tmp_path)
    _with_article(blueprint, f"The main result.\n\n## Remark {{#{identifier}}}\n\nA remark.")
    message = (
        f"top: line 13: heading id {identifier!r} is taken: the site gives it to an element of its own on "
        "this page; choose another"
    )

    _refused(blueprint, capsys, f"error: {message}\n", message)


def test_a_heading_id_is_published(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path)
    _with_article(blueprint, "The main result.\n\n## Remark {#a-remark}\n\nA remark.")

    assert main(["check", str(blueprint)]) == 0
    assert '<h6 id="a-remark">Remark</h6>' in _published(_render(blueprint) / "roadmap/README.md")


def test_every_formula_paints_inside_its_own_band(tmp_path: Path) -> None:
    """No formula can cover a card, a mark, a label, another line, or a side
    column, with either renderer and any theme; a lap keeps the ink beside its
    box inside the block that holds it; and a wide display or card scrolls,
    with a shade at the edge, rather than being cut off."""

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
    assert "overflow-x: auto" in rules[".bp-readback"]
    shade = rules[".bp-readback, .bp-readback div.arithmatex"]
    assert shade.count(" local") == 2 and shade.count(" scroll") == 2
    # The covers, and the card under them, are the color of the review panel a
    # card sits in, which in Material's dark scheme is not the page's.
    assert shade.count("var(--md-admonition-bg-color, var(--bp-page))") == 3
    assert "overflow-wrap: anywhere" in rules[".bp-review summary"]
