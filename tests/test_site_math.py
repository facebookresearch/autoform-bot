"""The site's mathematics: the MathJax configuration ``autoform render``
writes, the TeX and HTML an article may carry, and the styles that keep each
formula in its place."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.markdown import site_converter, statement_and_notes
from autoform_cli.readback import publishable_article
from autoform_cli.render import PublicationError, render_site
from autoform_cli.scaffold import scaffold_project
from tests.mathjax_package import NODE, mathjax_package
from tests.test_render import _project
from tests.test_review_cli import _too_deep_to_decode

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = _ROOT / "skills/setup/assets/cabannes-thesis-project"
_BASE = "https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5"
_BUNDLE = f"{_BASE}/tex-mml-chtml.js"
_PAGE_PACKAGES = ["base", "ams", "noundefined", "boldsymbol", "cancel", "mathtools", "configmacros"]
_CARD_PACKAGES = ["base", "ams", "noundefined"]
_MACROS = {"RR": "\\mathbb{R}", "norm": ["\\left\\lVert #1 \\right\\rVert", 1]}

#: Every javascripts/mathjax.js ``autoform init`` has written, newest first.
_SCAFFOLDED = (
    """window.MathJax = {
  loader: {load: ["ui/safe"]},
  options: {
    safeOptions: {
      allow: {URLs: "none", classes: "none", cssIDs: "none", styles: "none"}
    }
  },
  tex: {
    // Only the standard notation statements need. Leaving out require,
    // autoload, newcommand, and configmacros keeps \\require, macro
    // definitions, and the packages MathJax would load on demand (html,
    // color, bbox, action, unicode, enclose, cancel, and others) out of
    // reach; noundefined shows an unknown command in red instead of an error.
    packages: ["base", "ams", "noundefined"],
    inlineMath: [["$", "$"], ["\\\\(", "\\\\)"]],
    displayMath: [["$$", "$$"], ["\\\\[", "\\\\]"]]
  }
};
""",
    """window.MathJax = {
  loader: {load: ["ui/safe"]},
  options: {
    safeOptions: {
      allow: {URLs: "none", classes: "none", cssIDs: "none", styles: "none"}
    }
  },
  tex: {
    packages: {"[-]": ["require"]},
    inlineMath: [["$", "$"], ["\\\\(", "\\\\)"]],
    displayMath: [["$$", "$$"], ["\\\\[", "\\\\]"]]
  }
};
""",
    """window.MathJax = {
  tex: {
    inlineMath: [["$", "$"], ["\\\\(", "\\\\)"]],
    displayMath: [["$$", "$$"], ["\\\\[", "\\\\]"]]
  }
};
""",
)


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
@pytest.mark.parametrize("kept", _SCAFFOLDED)
def test_a_configuration_init_wrote_is_replaced_without_a_word(tmp_path: Path, kept: str, newline: str) -> None:
    """A project scaffolded with its own copy needs no edits."""

    blueprint = _vault(tmp_path, kept=kept.replace("\n", newline))

    assert main(["check", str(blueprint)]) == 0
    script = (_render(blueprint) / "javascripts/mathjax.js").read_text(encoding="utf-8")
    assert _settings(script)["card"]["packages"] == _CARD_PACKAGES


def test_an_edited_configuration_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Its edits would be lost, so neither check nor render goes ahead."""

    kept = _SCAFFOLDED[0].replace('"noundefined"],', '"noundefined"],\n    macros: {RR: "\\\\mathbb{R}"},')
    blueprint = _vault(tmp_path, kept=kept)
    reason = "move any tex.macros to tex-macros.json and delete it"

    assert main(["check", str(blueprint)]) == 1
    assert reason in capsys.readouterr().out
    with pytest.raises(PublicationError, match=reason):
        _render(blueprint)


def test_project_macros_reach_article_formulas_only(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, macros=json.dumps(_MACROS))

    assert main(["check", str(blueprint)]) == 0
    settings = _settings((_render(blueprint) / "javascripts/mathjax.js").read_text(encoding="utf-8"))
    assert settings["page"]["macros"] == _MACROS
    assert "macros" not in settings["card"]


def _changed_after_capture(monkeypatch: pytest.MonkeyPatch, path: Path, later: str) -> None:
    """Make the next capture of the blueprint rewrite ``path`` as ``later``
    once it has read it."""

    import autoform_cli.render as render_module

    capture = render_module._capture_publication

    def capture_then_change(*args, **kwargs):
        snapshot = capture(*args, **kwargs)
        path.write_text(later, encoding="utf-8")
        return snapshot

    monkeypatch.setattr(render_module, "_capture_publication", capture_then_change)


@pytest.mark.parametrize("later", ["{", json.dumps({"CC": "\\mathbb{C}"})], ids=["invalid", "different"])
def test_the_script_has_the_macros_the_site_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, later: str
) -> None:
    """The script is built from the tex-macros.json render captured and
    publishes, so a later change to the file reaches neither."""

    blueprint = _vault(tmp_path, macros=json.dumps(_MACROS))
    _changed_after_capture(monkeypatch, blueprint / "tex-macros.json", later)

    site = _render(blueprint)

    published = json.loads((site / "tex-macros.json").read_text(encoding="utf-8"))
    assert published == _MACROS
    assert _settings((site / "javascripts/mathjax.js").read_text(encoding="utf-8"))["page"]["macros"] == published


@pytest.mark.parametrize("later", ["{", json.dumps({"CC": "\\mathbb{C}"})], ids=["invalid", "different"])
def test_check_judges_the_macros_it_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], later: str
) -> None:
    blueprint = _vault(tmp_path, macros=json.dumps(_MACROS))
    _changed_after_capture(monkeypatch, blueprint / "tex-macros.json", later)

    assert main(["check", str(blueprint)]) == 0, capsys.readouterr().out


def test_macros_made_valid_after_capture_are_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid macros captured are refused even if the file is fixed before
    the script is built, so the site never publishes a script without the
    macros beside a tex-macros.json that has none."""

    blueprint = _vault(tmp_path, macros="{")
    _changed_after_capture(monkeypatch, blueprint / "tex-macros.json", json.dumps(_MACROS))

    assert main(["check", str(blueprint)]) == 1
    assert "tex-macros.json: not valid JSON" in capsys.readouterr().out
    (blueprint / "tex-macros.json").write_text("{", encoding="utf-8")
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

    assert main(["check", str(blueprint)]) == 1
    assert reason in capsys.readouterr().out
    with pytest.raises(PublicationError, match=re.escape(reason)):
        _render(blueprint)


def _directory_with_a_file(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "kept.txt").write_text("x\n", encoding="utf-8")


def _symlink(path: Path) -> None:
    """A link to a shipped configuration outside the vault."""

    path.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = path.parent / f"../../{path.name}.elsewhere"
    elsewhere.write_text(_SCAFFOLDED[0], encoding="utf-8")
    path.symlink_to(elsewhere.resolve())


def _fifo(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)


def _file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("relative", "make", "reason"),
    [
        ("javascripts", _file,
         "javascripts: is a file, where autoform render needs a folder for javascripts/blueprint-live.js; "
         "remove it"),
        ("tex-macros.json", _directory_with_a_file,
         "tex-macros.json: is a directory, where autoform reads the project's macros from a file; remove it"),
        ("tex-macros.json", _symlink,
         "tex-macros.json: is a symlink, where autoform reads the project's macros from a file; remove it"),
    ],
)
def test_something_other_than_a_file_where_the_site_has_one_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], relative: str, make, reason: str
) -> None:
    """Render writes its scripts under javascripts/ and reads the project's
    macros from the vault, so a file, directory, or symlink in the way is named
    by check rather than crashing, hanging, or being skipped by render."""

    blueprint = _vault(tmp_path)
    make(blueprint / relative)

    assert main(["check", str(blueprint)]) == 1
    assert reason in capsys.readouterr().out
    with pytest.raises(PublicationError):
        _render(blueprint)


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

    assert main(["check", str(blueprint)]) == 1
    assert reason in capsys.readouterr().out
    # Capture refuses a symlink or special file it would publish first.
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

    import autoform_cli.render as render_module

    blueprint = _vault(tmp_path)
    blocking = blueprint / reason.split(":")[0]
    make(blocking)
    capture = render_module._capture_publication

    def capture_then_remove(*args, **kwargs):
        snapshot = capture(*args, **kwargs)
        shutil.rmtree(blocking) if blocking.is_dir() else blocking.unlink()
        return snapshot

    monkeypatch.setattr(render_module, "_capture_publication", capture_then_remove)

    with pytest.raises(PublicationError, match=re.escape(reason)):
        _render(blueprint)


@pytest.mark.parametrize("folder", ["dependencies", "dependencies/nodes", "javascripts", "stylesheets", "assets"])
def test_a_file_where_render_needs_a_folder_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], folder: str
) -> None:
    blueprint = _vault(tmp_path)
    _file(blueprint / folder)
    reason = f"{folder}: is a file, where autoform render needs a folder for {folder}/"

    assert main(["check", str(blueprint)]) == 1
    assert reason in capsys.readouterr().out
    with pytest.raises(PublicationError, match=re.escape(reason)):
        _render(blueprint)


@pytest.mark.parametrize(
    ("article", "commands"),
    [
        ("$\\DeclareMathOperator{\\leq}{>}$, so $a \\leq b$.", "\\DeclareMathOperator"),
        ("Here \\(\\newcommand{\\RR}{\\mathbb{R}}\\) is live.", "\\newcommand"),
        ("$$\n\\def\\x{1} \\let\\y\\x\n$$", "\\def, \\let"),
        ("$\\DeclarePairedDelimiter\\abs{\\lvert}{\\rvert}$", "\\DeclarePairedDelimiter"),
        ("$\\newtagform{p}{(}{)} \\usetagform{p}$", "\\newtagform, \\usetagform"),
        ("$x = 1 \\label{one}$", "\\label"),
        ("$\\require{mhchem}$", "\\require"),
        ("$\\gdef\\x{1}$ and $\\global\\edef\\y{2}$", "\\gdef, \\global, \\edef"),
        ("$\\mathtoolsset{showonlyrefs}$", "\\mathtoolsset"),
        # The forms GitHub keeps the TeX of are formulas on the site too.
        ("$`\\def\\x{1}`$ is code to GitHub's Markdown.", "\\def"),
        ("```math\n\\gdef\\x{1} \\label{one}\n```", "\\gdef, \\label"),
    ],
)
def test_tex_that_changes_other_formulas_is_refused(article: str, commands: str) -> None:
    """Every article on a page is typeset with one TeX input, so a definition
    in one would change what the others show."""

    errors = publishable_article(article)[1]
    # Each is refused on the line it is on.
    line = "line 2" if article.startswith(("$$\n", "```math\n")) else "line 1"

    assert errors == (
        f"{line}: TeX commands that change other formulas are not allowed: {commands}; define notation in "
        "the vault's tex-macros.json instead, and put a command you only name in code",
    )


@pytest.mark.parametrize(
    "article",
    [
        "$\\mmlToken{mi}[mathcolor=#31A24C]{\\checkmark}$ approved",
        "$$\n\\mmlToken{mtext}[mathbackground=white]{x}\n$$",
        "Here \\(\\mmlToken{mo}{+}\\) is live.",
        "$`\\mmlToken{mi}[mathcolor=red]{x}`$ approved",
        "```math\n\\mmlToken{mo}{+}\n```",
    ],
)
def test_tex_that_sets_what_a_symbol_looks_like_is_refused(article: str) -> None:
    """Base's \\mmlToken gives a symbol the color, background, and other
    attributes a formula writes, so an article could color a symbol like the
    site's status marks. The site's filter drops the classes, styles, ids, and
    links it could set, but not its colors."""

    errors = publishable_article(article)[1]
    line = "line 2" if article.startswith(("$$\n", "```math\n")) else "line 1"

    assert errors == (
        f"{line}: TeX commands that set what a symbol looks like are not allowed: \\mmlToken; write the symbol "
        "itself, "
        "and put a command you only name in code",
    )


@pytest.mark.parametrize(
    ("article", "command"),
    [
        ("$\\cancel[mathcolor=#31A24C]{x}$ approved", "\\cancel"),
        ("$$\n\\bcancel [mathbackground=white]{x}\n$$", "\\bcancel"),
        ("$`\\xcancel[color=red,data-thickness=900]{x}`$", "\\xcancel"),
        ("```math\n\\cancelto[mathcolor=red]{0}{x}\n```", "\\cancelto"),
        # A strike a macro's argument ends with takes what follows the macro.
        ("$\\id{\\cancel}[mathcolor=red]{x}$", "\\cancel"),
        ("$\\id[\\cancel][mathcolor=red]{x}$", "\\cancel"),
    ],
)
def test_a_strike_cannot_be_given_an_option(article: str, command: str) -> None:
    """Cancel's strikes take a color, a background, a padding, and a
    thickness in brackets after the command, which the site's filter keeps,
    so an article could color one like the site's status marks."""

    errors = publishable_article(article)[1]
    line = "line 2" if article.startswith(("$$\n", "```math\n")) else "line 1"

    assert errors == (
        f"{line}: TeX strikes must be followed by their argument: {command}; an option in brackets, or one a "
        f"macro supplies, sets the color and other attributes of the strike, so write {command}{{...}}",
    )


def test_a_strike_with_its_argument_is_published(tmp_path: Path) -> None:
    """A strike given its argument, in braces or as one symbol, and a macro
    that gives it one, still pass."""

    blueprint = _vault(tmp_path, macros=json.dumps({"crossed": ["\\cancel{#1}", 1], "pair": ["[#1, #2]", 2]}))
    _with_article(blueprint, "$\\cancel{x} + \\bcancel y = \\cancelto{0}{z} \\crossed{w} \\pair{a}{b}$")

    assert main(["check", str(blueprint)]) == 0
    assert "\\cancelto{0}{z}" in (_render(blueprint) / "roadmap/README.md").read_text(encoding="utf-8")


def test_render_refuses_a_definition_in_an_article(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path)
    _with_article(blueprint, "Let $\\DeclareMathOperator{\\leq}{>}$ hold.")

    assert main(["check", str(blueprint)]) == 1
    with pytest.raises(PublicationError, match=r"top: line 11: TeX commands that change other formulas"):
        _render(blueprint)


_CHANGES = "TeX commands that change other formulas are not allowed: "
_LOOKS = "TeX commands that set what a symbol looks like are not allowed: "


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
    ],
)
def test_a_refusal_names_the_article_and_the_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], article: str, reason: str
) -> None:
    """The statement is on line 11 of the article, after its metadata."""

    blueprint = _vault(tmp_path)
    _with_article(blueprint, article)

    assert main(["check", str(blueprint)]) == 1
    assert reason in capsys.readouterr().out
    with pytest.raises(PublicationError, match=re.escape(reason.removeprefix("error: "))):
        _render(blueprint)


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


def test_the_protected_formula_forms_are_typeset(tmp_path: Path) -> None:
    """MathJax, run on the script render wrote, typesets both, as it does
    every other formula of an article."""

    reading = site_converter().convert(publishable_article(_PROTECTED)[0])
    page = f"<html><head></head><body><article>{reading}</article></body></html>"

    report = _node_report(tmp_path, _node_script(tmp_path), "new", first=page, second=page)

    assert report["errors"] == []
    (article,) = report["passes"][0]
    assert [math["tex"] for math in article["math"]] == ["a \\leq b", "\n\\sum_{i<n} i\n"]
    assert _LEQ in article["math"][0]["mml"]
    assert all("merror" not in math["mml"] for math in article["math"])


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


_OPENED = (
    "raw HTML is not allowed: the site would read a < before a letter as the start of a tag, and publish "
    "'</div>' after it as HTML; in a formula, put a space after <, as in $a < b$"
)


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


def test_check_names_the_line_of_a_tag_only_the_site_reads(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = _vault(tmp_path)
    _with_article(blueprint, "The main result, for $a<b$.")

    assert main(["check", str(blueprint)]) == 1
    assert f"top: line 11: {_OPENED}" in capsys.readouterr().out


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


def test_check_and_render_refuse_a_long_character_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = _vault(tmp_path)
    _with_article(blueprint, "The main result &#" + "9" * 39000 + ";.")

    assert main(["check", str(blueprint)]) == 1
    assert "(39003 characters)" in capsys.readouterr().out
    with pytest.raises(PublicationError, match=r"\(39003 characters\)"):
        _render(blueprint)


_FORGED = '<span class="bp-mark">FORGED MARK</span><script>document.title="INJECTED"</script>'


def _published(page: Path) -> str:
    """``page`` as MkDocs publishes it: its metadata read off, and the rest
    converted as the site's configuration converts it."""

    from mkdocs.utils.meta import get_data

    from autoform_cli.markdown import site_converter

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
    site keep the destinations they had."""

    blueprint = _vault(tmp_path)
    (blueprint / "sources.md").write_text("# Paper\n", encoding="utf-8")
    _with_article(
        blueprint,
        "See [the base](base.md), [its section](base.md#notes), [the paper](../sources.md#lemma-3), "
        "[below](#later), [the chapter](README.md), and [Mathlib](https://leanprover-community.github.io/x.html).",
    )

    site = _render(blueprint)
    written = (site / "roadmap/README.md").read_text(encoding="utf-8")

    assert (
        "See [the base](#base), [its section](#base), [the paper](../sources.md#lemma-3), "
        "[below](#later), [the chapter](#), and [Mathlib](https://leanprover-community.github.io/x.html)."
    ) in written


def test_a_link_render_moves_keeps_its_query(tmp_path: Path) -> None:
    """A query is no part of the file's name: render moves the path and
    keeps the query, so the link still names the file, and the strict build
    finds it."""

    blueprint = _vault(tmp_path)
    (blueprint / "sources.md").write_text("# Paper\n", encoding="utf-8")
    _with_article(blueprint, "See [the paper](../sources.md?plain=1#lemma-3) and [as text](../sources.md?plain=1).")

    written = (_render(blueprint) / "roadmap/README.md").read_text(encoding="utf-8")

    assert "See [the paper](../sources.md?plain=1#lemma-3) and [as text](../sources.md?plain=1)." in written


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

    assert main(["check", str(blueprint)]) == 1
    assert "roadmap: line 12: raw HTML is not allowed: <span>, </span>, <script>" in capsys.readouterr().out
    with pytest.raises(PublicationError, match="raw HTML is not allowed"):
        _render(blueprint)


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

    assert main(["check", str(blueprint)]) == 1
    assert f"{relative}: the site publishes no " in capsys.readouterr().out
    with pytest.raises(PublicationError, match=re.escape(relative)):
        _render(blueprint)


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


_DIAGRAM_RULE = (
    "Mermaid diagrams are not allowed: the site draws only the dependency graphs autoform render makes; "
    "show a diagram's source in a ```text fence instead"
)


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


def test_check_and_render_refuse_a_mermaid_diagram(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = _vault(tmp_path)
    _with_article(blueprint, "The main result.\n\n```mermaid\ngraph LR\n  A --> B\n```")
    message = f"top: line 13: {_DIAGRAM_RULE}"

    assert main(["check", str(blueprint)]) == 1
    assert f"error: {message}\n" in capsys.readouterr().out
    with pytest.raises(PublicationError, match=re.escape(message)):
        _render(blueprint)


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

    assert main(["check", str(blueprint)]) == 1
    assert f"error: {message}\n" in capsys.readouterr().out
    with pytest.raises(PublicationError, match=re.escape(message)):
        _render(blueprint)


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


# Runs javascripts/mathjax.js in node against a local MathJax, as a page would,
# and reports what each document it made typeset. argv: the script, a page,
# the next page as Material's instant navigation shows it, "new" or "old":
# whether mkdocs.yml also lists the bundle, after the script, "after" when a
# project script listed after it assigns the configuration Material's
# documentation gives, the card ("page" for the articles) whose menu changes
# two settings after the first pass, or "none", and what the document MathJax
# starts with does before the first pass, as its menu does with saved
# settings: "renders" when it renders, "loads" when its menu is loading, or
# "quiet". The bundle's menu needs a browser, so each document gets one with
# the same interface.
_HARNESS = r"""
"use strict";
const fs = require("fs");
const path = require("path");
const [scriptPath, firstPath, secondPath, project, override, changed, startup, mode, mutation] = process.argv.slice(2);
// What a later script changes in the configuration, and when: before the
// page is parsed, so before the bundle is fetched ("fetch"), after it is
// fetched and before it starts ("start"), or, in a project that lists the
// bundle, after it has started and before the page is parsed ("loaded"),
// through window.MathJax.config ("config") or window.MathJax itself.
const [changing, moment, through] = mutation.split("@");
const report = {
  injected: [], errors: [], passes: [], inventory: [], version: null, base: null, load: null, menus: null,
  startup: null, resets: [], changed: null, stopped: null,
};
global.window = globalThis;
let contentLoaded = null;
const parsedListeners = [];
global.addEventListener = (type, listener, capture) => {
  if (type === "DOMContentLoaded" && capture && global.document.readyState === "loading") parsedListeners.push(listener);
};
global.document = {
  readyState: project === "old" || moment === "fetch" ? "loading" : "complete",
  head: {appendChild: (element) => report.injected.push(element.src)},
  createElement: () => ({}),
  addEventListener: (type, listener) => { if (type === "DOMContentLoaded") contentLoaded = listener; },
};
console.error = (...args) => report.errors.push(args.map(String).join(" "));
let shown = null;
global.document$ = {subscribe: (listener) => { shown = listener; }};

// A MathJax listed in mkdocs.yml before the script, which has started by the
// time the script runs, and would typeset the whole page as it is.
if (mode === "early") {
  global.MathJax = {loader: {load: ["input/tex", "output/chtml"]}, startup: {document: fs.readFileSync(firstPath, "utf8")}};
  const page = global.document;
  delete global.document;
  const early = require(path.join(process.env.AUTOFORM_MATHJAX_DIR, "es5", "node-main.js"));
  const started = global.MathJax;
  global.document = page;
  (0, eval)(fs.readFileSync(scriptPath, "utf8"));
  delete global.document;
  early.init({}).then(() => started.startup.promise).then(() => {
    report.version = started.version;
    report.startup = {math: Array.from(started.startup.document.math).length, subscribed: shown !== null};
  }).catch((error) => {
    report.errors.push("harness: " + error.stack);
  }).finally(() => {
    process.stdout.write(JSON.stringify(report));
  });
  return;
}

(0, eval)(fs.readFileSync(scriptPath, "utf8"));
report.base = MathJax.loader.paths.mathjax;
report.load = MathJax.loader.load.slice();
// What node needs that a browser has, given to MathJax once it has
// started, as its own defaults are, since the script refuses to start a
// MathJax whose configuration another script changed: the components the
// bundle carries, and the menu, which needs a browser; the page; and the
// menu for each document.
const node = {
  loader: {load: report.load.concat(["input/tex", "input/mml", "output/chtml", "a11y/assistive-mml", "output/svg"])},
  startup: {document: fs.readFileSync(firstPath, "utf8"), ready: nodeReady},
};
const changes = {
  pageReady: (config) => { config.startup.pageReady = () => { report.errors.push("harness: pageReady replaced"); }; },
  typeset: (config) => { config.startup.typeset = true; },
  options: (config) => { config.options = {ignoreHtmlClass: ".*|", processHtmlClass: "arithmatex"}; },
  tex: (config) => { config.tex.inlineMath.push(["$", "$"]); },
  paths: (config) => { config.loader.paths.mathjax = "https://example.com/mathjax"; },
  load: (config) => { config.loader.load.push("[tex]/physics"); },
  added: (config) => { config.startup.ready = () => {}; },
  deleted: (config) => { delete config.startup.typeset; },
  inherited: (config) => { Object.setPrototypeOf(config.startup, {ready: () => {}}); },
  accessor: (config) => {
    const startup = config.startup;
    const ready = startup.pageReady;
    let reads = 0;
    Object.defineProperty(startup, "pageReady", {enumerable: true, get: () => reads++ ? () => {} : ready});
  },
  // A proxy shows every check what it was given, and MathJax what it says.
  proxyStartup: (config) => {
    const replaced = () => { report.errors.push("harness: pageReady replaced"); };
    config.startup = new Proxy(config.startup, {get: (target, key) => key === "pageReady" ? replaced : target[key]});
  },
  proxySnippet: (config) => {
    const said = {
      tex: {inlineMath: [["\\(", "\\)"]], displayMath: [["\\[", "\\]"]]},
      options: {ignoreHtmlClass: ".*|", processHtmlClass: "arithmatex"},
    };
    Object.keys(said).forEach((name) => {
      config[name] = new Proxy(config[name] || {}, {get: (target, key) => key in said[name] ? said[name][key] : target[key]});
    });
  },
};
function change() {
  report.changed = window.MathJax.version === undefined;
  changes[changing](through === "config" ? window.MathJax.config : window.MathJax);
}
if (moment === "fetch") change();
// Every document gets a menu, the one MathJax starts with too, which is not
// listed with the documents of a pass.
let docs = [];
let first = null;
let made = null;
function nodeReady() {
  const original = MathJax._.mathjax.mathjax.document;
  MathJax._.mathjax.mathjax.document = function (root, options) {
    const doc = original.call(this, root, options);
    if (changed !== "none" || startup !== "quiet" || mode === "retry") doc.menu = new Menu(doc);
    if (first === null) first = doc;
    else docs.push(doc);
    // The page's document stops once, as a document does while a component
    // one of its formulas needs is loading, and meanwhile a reader picks
    // another renderer, which the menu loads.
    if (mode === "retry" && docs.length === 1 && report.passes.length === 0) {
      const render = doc.render;
      let stopped = false;
      doc.render = function () {
        if (!stopped) {
          stopped = true;
          Menu.loadingPromises.set("output/svg", new Promise((resolve) => setTimeout(() => {
            Menu.loadingPromises.delete("output/svg");
            made = docs.length;
            resolve();
          }, 40)));
          MathJax._.util.Retries.retryAfter(new Promise((resolve) => setTimeout(resolve, 20)));
        }
        return render.call(this);
      };
    }
    return doc;
  };
  MathJax.startup.defaultReady();
  // When the renderer forgets what it kept, by the documents of the pass made by then.
  if (mode === "spy") {
    const output = MathJax.startup.output;
    const reset = output.reset;
    output.reset = function () {
      report.resets.push(docs.length);
      return reset.apply(this, arguments);
    };
  }
  if (startup === "renders") first.render();
  if (startup === "loads") {
    Menu.loadingPromises.set("output/svg", new Promise((resolve) => setTimeout(() => {
      Menu.loadingPromises.delete("output/svg");
      made = docs.length;
      resolve();
    }, 20)));
  }
}
if (override === "after") {
  window.MathJax = {
    tex: {inlineMath: [["\\(", "\\)"]], displayMath: [["\\[", "\\]"]], processEscapes: true, processEnvironments: true},
    options: {ignoreHtmlClass: ".*|", processHtmlClass: "arithmatex"}
  };
}
// The page has been parsed, which the window hears of before the document;
// MathJax would take its document from here. A page that loads the bundle
// now fetches it once the script asks for it; a project scaffolded with the
// bundle in mkdocs.yml has run it before.
function parsed() {
  parsedListeners.forEach((listener) => listener());
  if (contentLoaded) contentLoaded();
}
if (project === "new") parsed();
if (moment === "start") change();
delete global.document;
let main = null;
if (project === "old" || report.injected.length) {
  try {
    main = require(path.join(process.env.AUTOFORM_MATHJAX_DIR, "es5", "node-main.js"));
  } catch (error) {
    report.stopped = error.message;
  }
}
if (project === "old") {
  if (moment === "loaded") change();
  parsed();
}
if (!main) {
  process.stdout.write(JSON.stringify(report));
  return;
}

// What the script uses of a document's menu: its settings, the variables a
// click sets, which save the settings, the renderers it has loaded, and
// what the menus are loading, with a redraw that stops partway, with the
// formulas gone, for settings other than the renderer.
class Menu {
  constructor(doc) {
    this.document = doc;
    this.settings = {renderer: "CHTML", assistiveMml: true, scale: "1", explorer: false};
    this.defaultSettings = Object.assign({}, this.settings);
    this.jax = {CHTML: doc.outputJax, SVG: null};
    this.applied = [];
    const lookup = (name) => name in this.settings ? {setValue: (value) => {
      this.settings[name] = value;
      this.applied.push([name, value]);
      if (name === "renderer") {
        if (!this.jax[value]) {
          MathJax.startup.useOutput(value.toLowerCase(), true);
          this.jax[value] = MathJax.startup.output = MathJax.startup.getOutputJax();
        }
        this.jax[value].setAdaptor(this.document.adaptor);
        this.document.outputJax = this.jax[value];
      } else if (name === "explorer") {
        this.explore();
      } else {
        this.document.state(MathJax._.core.MathItem.STATE.TYPESET - 1);
      }
      this.saveUserSettings();
    }} : undefined;
    this.menu = {pool: {lookup}};
  }
  // The first time, the explorer is loaded, which extends the handler, and
  // this menu's document is remade with it, its formulas moved there; after
  // that, the menu's redraw stops while the speech engine is not ready.
  explore() {
    const STATE = MathJax._.core.MathItem.STATE;
    if (Menu.explorer) {
      this.document.state(STATE.COMPILED - 1);
      MathJax._.util.Retries.retryAfter(Promise.resolve());
    }
    Menu.loadingPromises.set("a11y/explorer", Promise.resolve().then(() => {
      Menu.loadingPromises.delete("a11y/explorer");
      Menu.explorer = true;
      const startup = MathJax.startup;
      const mathjax = MathJax._.mathjax.mathjax;
      mathjax.handlers.unregister(startup.handler);
      startup.handler = startup.getHandler();
      startup.handler.documentClass = class extends startup.handler.documentClass {};
      mathjax.handlers.register(startup.handler);
      const old = this.document;
      this.document = startup.document = startup.getDocument();
      this.document.menu = this;
      for (const item of old.math) this.document.math.push(Object.assign(new this.document.options.MathItem(), item));
      this.document.processed = old.processed;
      this.document.state(STATE.COMPILED - 1);
    }));
  }
  saveUserSettings() {}
}
Menu.loadingPromises = new Map();
Menu.explorer = false;

// The card a document reads, by its id, or what stands for the articles.
function cardOf(doc, articles) {
  const element = doc.options.elements && doc.options.elements[0];
  const adaptor = MathJax.startup.adaptor;
  return element && adaptor.hasClass(element, "bp-readback") ? adaptor.getAttribute(element, "id") : articles;
}

function describe() {
  return docs.map((doc) => {
    const card = cardOf(doc, null);
    return {
      card,
      output: doc.outputJax.name,
      packages: doc.inputJax[0].parseOptions.options.packages.slice(),
      math: Array.from(doc.math).map((item) => ({tex: item.math, mml: MathJax.startup.toMML(item.root)})),
    };
  });
}

// Each command the page's input knows, with the function that handles it.
function inventory(tex) {
  const handlers = [];
  for (const entry of tex.parseOptions.handlers.get("macro")._configuration) {
    if (!(entry.item.map instanceof Map)) continue;
    for (const [name, macro] of entry.item.map) {
      if (typeof macro.func !== "function") continue;
      if (!handlers.includes(macro.func)) handlers.push(macro.func);
      report.inventory.push(["\\" + name, handlers.indexOf(macro.func)]);
    }
  }
}

main.init(node).then(async () => {
  report.version = MathJax.version;
  if (!shown) return;
  // document$ shows the first page again to each subscriber.
  await shown();
  report.passes.push(describe());
  report.startup = {math: Array.from(first.math).length, made};
  inventory(docs[0].inputJax[0]);
  if (changed !== "none") {
    // The documents of the pass, and the formulas and input each has.
    const group = docs.slice();
    const inputs = group.map((doc) => doc.inputJax[0]);
    const counts = group.map((doc) => Array.from(doc.math).length);
    const card = (doc) => cardOf(doc, "page");
    const menu = group.find((doc) => card(doc) === changed).menu;
    menu.menu.pool.lookup("renderer").setValue("SVG");
    menu.menu.pool.lookup("assistiveMml").setValue(false);
    menu.menu.pool.lookup("explorer").setValue(true);
    await new Promise((resolve) => setTimeout(resolve, 10));
    // Each menu's document now, which a menu may have remade.
    const now = group.map((doc) => doc.menu.document);
    const items = (doc) => Array.from(doc.math);
    report.menus = {
      menus: group.map((doc) => ({card: card(doc), settings: doc.menu.settings, applied: doc.menu.applied})),
      inputs: now.every((doc, index) => items(doc).length === counts[index] &&
        items(doc).every((item) => item.inputJax === inputs[index])) && new Set(inputs).size === group.length,
      handler: now.every((doc) => doc instanceof MathJax.startup.handler.documentClass),
      jax: group.every((doc) => doc.menu.jax === first.menu.jax),
      defaults: group.every((doc) => doc.menu.defaultSettings === first.menu.defaultSettings),
      output: now.every((doc) => doc.outputJax === first.menu.jax.SVG),
      shown: now.every((doc) => items(doc).every((item) => item.state() >= MathJax._.core.MathItem.STATE.INSERTED)),
    };
  }
  docs = [];
  const adaptor = MathJax.startup.adaptor;
  const body = adaptor.body(MathJax.startup.document.document);
  for (const child of adaptor.childNodes(body).slice()) adaptor.remove(child);
  const next = adaptor.body(adaptor.parse(fs.readFileSync(secondPath, "utf8"), "text/html"));
  for (const child of adaptor.childNodes(next).slice()) adaptor.append(body, child);
  await shown();
  report.passes.push(describe());
}).catch((error) => {
  report.errors.push("harness: " + error.stack);
}).finally(() => {
  process.stdout.write(JSON.stringify(report));
});
"""

# Articles first, as the page's input reads them, then the cards. Each card
# repeats an article's attack on the cards after it, and its own formulas use
# what only articles may.
_FIRST_PAGE = r"""<html><head></head><body><article>
<p id="a1"><span class="arithmatex">\(\DeclareMathOperator{\leq}{>}\)</span> <span class="arithmatex">\(a \leq b\)</span></p>
<p id="a2"><span class="arithmatex">\(\boldsymbol{x} \cancel{y} \coloneqq \RR \norm{v}\)</span></p>
<p id="a3"><span class="arithmatex">\(\label{shared} \nothere\)</span> <span class="arithmatex">\(p \leq q\)</span></p>
<div class="bp-readback bp-readback-current" id="c1"><div class="bp-readback-title">Read-back $\leq$</div><p><span class="arithmatex">\(a \leq b\)</span> \(raw \leq\) $dollar$</p><p class="arithmatex">\[\DeclareMathOperator{\leq}{>} \label{shared} c \leq d\]</p></div>
<div class="bp-readback bp-readback-current" id="c2"><p><span class="arithmatex">\(e \leq f \label{shared}\)</span> <span class="arithmatex">\(\boldsymbol{x} \cancel{y} \coloneqq \RR\)</span></p></div>
</article></body></html>
"""
_SECOND_PAGE = r"""<html><head></head><body><article>
<p id="a4"><span class="arithmatex">\(g \leq h \label{shared}\)</span></p>
<div class="bp-readback bp-readback-current" id="c3"><p><span class="arithmatex">\(i \leq j\)</span></p></div>
</article></body></html>
"""
_LEQ = "<mo>&#x2264;</mo>"
_OVERRIDDEN = (
    "autoform: a script assigned window.MathJax after javascripts/mathjax.js; it is ignored, and the site's "
    "configuration is kept. Configure MathJax only through javascripts/mathjax.js and tex-macros.json."
)


def _node_report(
    tmp_path: Path,
    script: str,
    project: str,
    first: str = _FIRST_PAGE,
    second: str = _SECOND_PAGE,
    override: str = "none",
    changed: str = "none",
    startup: str = "quiet",
    mode: str = "none",
    mutation: str = "none@none",
) -> dict:
    files = {"harness.js": _HARNESS, "mathjax.js": script, "first.html": first, "second.html": second}
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    done = subprocess.run(
        [
            NODE, "harness.js", "mathjax.js", "first.html", "second.html",
            project, override, changed, startup, mode, mutation,
        ],
        cwd=tmp_path,
        env=dict(os.environ, AUTOFORM_MATHJAX_DIR=str(mathjax_package())),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize("override", ["none", "after"])
@pytest.mark.parametrize("project", ["new", "old"])
def test_the_rendered_configuration_typesets_each_card_alone(tmp_path: Path, project: str, override: str) -> None:
    """MathJax itself, run on the script render wrote, as a page loads it now
    and as a project scaffolded with the bundle in mkdocs.yml loads it, and
    with a project script after it that assigns a configuration of its own."""

    mathjax = mathjax_package()
    blueprint = _vault(tmp_path / "vault", macros=json.dumps(_MACROS))
    script = (_render(blueprint) / "javascripts/mathjax.js").read_text(encoding="utf-8")
    # The release tested is the release the site loads.
    assert json.loads((Path(mathjax) / "package.json").read_text(encoding="utf-8"))["version"] == "3.2.2"

    report = _node_report(tmp_path, script, project, override=override)

    assert report["errors"] == ([] if override == "none" else [_OVERRIDDEN])
    assert report["injected"] == ([_BUNDLE] if project == "new" else [])
    assert report["base"] == _BASE
    assert report["version"] == "3.2.2"
    assert {"ui/safe", "[tex]/boldsymbol", "[tex]/cancel", "[tex]/mathtools"} <= set(report["load"])
    first, second = report["passes"]
    page, c1, c2 = first
    assert [document["card"] for document in first] == [None, "c1", "c2"]
    assert page["packages"] == _PAGE_PACKAGES
    assert c1["packages"] == c2["packages"] == _CARD_PACKAGES
    # The page's input reads no card; within the articles the attack works,
    # which is why check refuses it.
    assert [math["tex"] for math in page["math"]] == [
        "\\DeclareMathOperator{\\leq}{>}",
        "a \\leq b",
        "\\boldsymbol{x} \\cancel{y} \\coloneqq \\RR \\norm{v}",
        "\\label{shared} \\nothere",
        "p \\leq q",
    ]
    assert "<mo>&gt;</mo>" in page["math"][1]["mml"]
    articles = page["math"][2]["mml"]
    assert 'mathvariant="bold-italic"' in articles
    assert 'notation="updiagonalstrike"' in articles
    assert 'mathvariant="double-struck"' in articles
    assert "‖" in articles or "&#x2016;" in articles
    assert 'mathcolor="red"' not in articles
    assert 'mathcolor="red"' in page["math"][3]["mml"]
    # A card reads its formulas and nothing else, with nothing an article or
    # an earlier card defined.
    assert [math["tex"] for math in c1["math"]] == [
        "a \\leq b",
        "\\DeclareMathOperator{\\leq}{>} \\label{shared} c \\leq d",
    ]
    assert _LEQ in c1["math"][0]["mml"]
    assert [math["tex"] for math in c2["math"]] == ["e \\leq f \\label{shared}", "\\boldsymbol{x} \\cancel{y} \\coloneqq \\RR"]
    assert _LEQ in c2["math"][0]["mml"]
    assert "merror" not in c2["math"][0]["mml"]
    # What only articles may use is an unknown command in a card, shown in red.
    unknown = c2["math"][1]["mml"]
    for command in ("\\boldsymbol", "\\cancel", "\\coloneqq", "\\RR"):
        assert f'<mtext mathcolor="red">{command}</mtext>' in unknown
    # The next page, shown in place, gets new inputs again.
    assert [document["card"] for document in second] == [None, "c3"]
    assert _LEQ in second[0]["math"][0]["mml"]
    assert "merror" not in second[0]["math"][0]["mml"]
    assert _LEQ in second[1]["math"][0]["mml"]
    # Every command that shares its handler with one check refuses is refused too.
    from autoform_cli.mathjax import STATEFUL_TEX

    stateful = {handler for name, handler in report["inventory"] if name in STATEFUL_TEX}
    assert {name for name, handler in report["inventory"] if handler in stateful} <= STATEFUL_TEX
    assert {"\\DeclareMathOperator", "\\DeclarePairedDelimiter", "\\label", "\\newtagform"} <= {
        name for name, _ in report["inventory"]
    }


#: Typesets every command and environment the page's packages define with a
#: color in brackets in each place an option could go, and prints those that
#: color what they draw.
_OPTION_INVENTORY = r"""
"use strict";
const path = require("path");
const [packages] = process.argv.slice(2);
const names = JSON.parse(packages);
const config = {
  loader: {load: ["input/tex-base", ...names.filter((name) => name !== "base").map((name) => `[tex]/${name}`)]},
  tex: {packages: names},
};
require(path.join(process.env.AUTOFORM_MATHJAX_DIR, "es5", "node-main.js")).init(config).then((MathJax) => {
  MathJax.tex2mml("x");
  const tex = MathJax.startup.input[0];
  const commands = new Set();
  for (const kind of ["macro", "environment"]) {
    for (const entry of tex.parseOptions.handlers.get(kind)._configuration) {
      if (!(entry.item.map instanceof Map)) continue;
      for (const [name] of entry.item.map) commands.add(kind === "macro" ? "\\" + name : "env:" + name);
    }
  }
  const colored = new Set();
  for (const command of commands) {
    for (const key of ["mathcolor", "color", "mathbackground", "background"]) {
      const option = `[${key}=#31A24C]`;
      const forms = command.startsWith("env:")
        ? [`\\begin{${command.slice(4)}}${option}{x}{x} x \\end{${command.slice(4)}}`]
        : [0, 1, 2, 3].map((before) => command + "{x}".repeat(before).replace("{x}", before ? "{mi}" : "") + option + "{x}{x}");
      for (const form of forms) {
        let mml = "";
        try { mml = MathJax.tex2mml(form); } catch (error) {}
        if (/(color|background)="#31A24C"/.test(mml)) colored.add(command);
      }
    }
  }
  console.log(JSON.stringify({commands: commands.size, colored: [...colored].sort()}));
});
"""


def test_the_commands_an_option_colors_are_all_refused(tmp_path: Path) -> None:
    """The commands of the page's packages that color what they draw when
    given a color in brackets, found by typesetting each of them, are the
    ones check refuses, wholly or with an option."""

    mathjax = mathjax_package()
    from autoform_cli.mathjax import ATTRIBUTE_TEX, OPTION_TEX, PAGE_PACKAGES

    (tmp_path / "inventory.js").write_text(_OPTION_INVENTORY, encoding="utf-8")
    done = subprocess.run(
        [NODE, "inventory.js", json.dumps(list(PAGE_PACKAGES))],
        cwd=tmp_path,
        env=dict(os.environ, AUTOFORM_MATHJAX_DIR=str(mathjax)),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)

    assert report["commands"] > 900
    assert set(report["colored"]) == ATTRIBUTE_TEX | OPTION_TEX


def test_the_rendered_configuration_refuses_another_release(tmp_path: Path) -> None:
    mathjax_package()
    script = (_render(_vault(tmp_path / "vault")) / "javascripts/mathjax.js").read_text(encoding="utf-8")

    report = _node_report(tmp_path, script.replace('"version": "3.2.2"', '"version": "3.2.1"'), "old")

    assert report["passes"] == []
    assert report["errors"] == [
        "autoform: this page loaded MathJax 3.2.2, but javascripts/mathjax.js was written for 3.2.1; formulas "
        "are left as typed. Load MathJax only through javascripts/mathjax.js in mkdocs.yml."
    ]


def _node_script(tmp_path: Path, macros: str | None = None) -> str:
    """The script render wrote for a vault, or a skip without node and MathJax."""

    mathjax_package()
    return (_render(_vault(tmp_path / "vault", macros=macros)) / "javascripts/mathjax.js").read_text(encoding="utf-8")


def test_each_document_is_made_once_the_one_before_it_is_finished(tmp_path: Path) -> None:
    """A menu can start loading while a document waits, and no document is
    made while a menu is loading, so the next is made only after both."""

    report = _node_report(tmp_path, _node_script(tmp_path, json.dumps(_MACROS)), "new", mode="retry")

    assert report["errors"] == []
    # Only the page's document had been made when the load finished.
    assert report["startup"]["made"] == 1
    page, c1, c2 = report["passes"][0]
    articles = page["math"][2]["mml"]
    assert "merror" not in articles
    assert 'mathvariant="bold-italic"' in articles
    assert 'notation="updiagonalstrike"' in articles
    assert 'mathvariant="double-struck"' in articles
    assert "<mo>&gt;</mo>" in page["math"][1]["mml"]
    assert _LEQ in c1["math"][0]["mml"]
    for command in ("\\boldsymbol", "\\cancel", "\\coloneqq", "\\RR"):
        assert f'<mtext mathcolor="red">{command}</mtext>' in c2["math"][1]["mml"]


def test_no_class_makes_the_page_input_read_a_card(tmp_path: Path) -> None:
    """A document made outside MathJax's startup reads an element of class
    mathjax_process wherever it is unless told otherwise, inside a card too."""

    page = (
        '<html><head></head><body><article><p id="a1"><span class="arithmatex">\\(a\\)</span></p>'
        '<div class="bp-readback bp-readback-current" id="c1"><p><span class="mathjax_process">\\(b \\leq c\\)'
        '</span> <span class="arithmatex">\\(d\\)</span></p></div></article></body></html>'
    )

    report = _node_report(tmp_path, _node_script(tmp_path), "new", first=page, second=page)

    assert report["errors"] == []
    article, card = report["passes"][0]
    assert [math["tex"] for math in article["math"]] == ["a"]
    assert [math["tex"] for math in card["math"]] == ["d"]


def test_the_renderer_forgets_the_last_page_before_each_pass(tmp_path: Path) -> None:
    report = _node_report(tmp_path, _node_script(tmp_path), "new", mode="spy")

    assert report["errors"] == []
    assert len(report["passes"]) == 2
    # Once a pass, before its first document is made.
    assert report["resets"] == [0, 0]


def test_a_mathjax_started_before_the_script_typesets_nothing(tmp_path: Path) -> None:
    """A MathJax listed before the script started without the site's
    configuration and would typeset the cards with the articles' input."""

    report = _node_report(tmp_path, _node_script(tmp_path), "new", mode="early")

    assert report["errors"] == [
        "autoform: MathJax was loaded before javascripts/mathjax.js; formulas are left as typed. Load MathJax "
        "only through javascripts/mathjax.js in mkdocs.yml."
    ]
    assert report["version"] == "3.2.2"
    assert report["startup"] == {"math": 0, "subscribed": False}


_CHANGED = (
    "autoform: a script changed the MathJax configuration after javascripts/mathjax.js; formulas are left as "
    "typed. Configure MathJax only through javascripts/mathjax.js and tex-macros.json."
)


@pytest.mark.parametrize(
    "changing", ["pageReady", "typeset", "options", "tex", "paths", "load", "added", "deleted", "inherited", "accessor"]
)
@pytest.mark.parametrize("project, moment", [("new", "fetch"), ("new", "start"), ("old", "fetch")])
def test_a_configuration_changed_after_the_script_is_not_started(
    tmp_path: Path, project: str, moment: str, changing: str
) -> None:
    """window.MathJax is the script's configuration until MathJax starts, and
    a later script that changes it rather than assigning a new one, before
    the bundle is fetched or after, stops MathJax rather than deciding how
    the cards are read. A project that lists the bundle in mkdocs.yml has
    started it before the page is parsed, so the change is made before."""

    report = _node_report(tmp_path, _node_script(tmp_path), project, mutation=f"{changing}@{moment}")

    assert report["changed"] is True
    assert report["errors"] == [_CHANGED]
    # Not fetched when the change comes first; when it comes after, the
    # bundle stops as it starts, before it reads the configuration.
    assert report["injected"] == ([_BUNDLE] if (project, moment) == ("new", "start") else [])
    assert report["stopped"] == ("autoform: MathJax is not started" if project == "old" or moment == "start" else None)
    assert report["version"] is None
    assert report["passes"] == []


@pytest.mark.parametrize("changing", ["proxyStartup", "proxySnippet"])
@pytest.mark.parametrize("project, moment", [("new", "fetch"), ("new", "start"), ("old", "fetch")])
def test_a_configuration_a_proxy_stands_for_is_not_read(
    tmp_path: Path, project: str, moment: str, changing: str
) -> None:
    """A later script can put a proxy where the configuration's startup or
    options were, which shows the check what the script set and MathJax
    something else. MathJax starts on the site's configuration all the same,
    and reads the cards as it does with nothing changed: the document it
    starts with, which its menu renders with saved settings, reads no card."""

    script = _node_script(tmp_path)
    kept = _node_report(tmp_path, script, project, startup="renders")
    report = _node_report(tmp_path, script, project, startup="renders", mutation=f"{changing}@{moment}")

    assert report["changed"] is True
    assert report["errors"] == kept["errors"] == []
    assert report["version"] == "3.2.2"
    assert report["startup"] == kept["startup"]
    assert report["passes"] == kept["passes"] and len(report["passes"]) == 2


@pytest.mark.parametrize("through", ["config", "mathjax"])
@pytest.mark.parametrize("changing", ["pageReady", "options", "proxyStartup", "proxySnippet"])
def test_a_configuration_changed_after_a_listed_bundle_is_not_started(
    tmp_path: Path, changing: str, through: str
) -> None:
    """A project that lists the bundle in mkdocs.yml has started MathJax on
    the configuration before a script listed after it runs. Changing it then,
    through window.MathJax.config, stops MathJax before the page is read;
    window.MathJax is MathJax by then, and the same change made to it is not
    MathJax's configuration, so the cards are read as before."""

    report = _node_report(tmp_path, _node_script(tmp_path), "old", mutation=f"{changing}@loaded@{through}")

    assert report["changed"] is False
    assert report["injected"] == []
    assert report["stopped"] is None
    assert report["version"] == "3.2.2"
    if through == "config":
        assert report["errors"] == [_CHANGED]
        assert report["passes"] == []
    else:
        assert report["errors"] == []
        assert len(report["passes"]) == 2


# Text a page shows outside the formulas its Markdown marked: navigation, a
# table of contents, a lead, a title, and a graph's source, as the site
# prints them, each with TeX check never judged as a formula.
_UNMARKED = r"""<html><head></head><body>
<nav class="md-nav"><a href="#top">\(\DeclareMathOperator{\leq}{>}\) $\gdef\x{1}$</a></nav>
<div class="md-sidebar"><ul><li>$\label{toc}$</li></ul></div>
<article>
<div class="bp-hero"><p class="bp-hero-lead">Lead $\DeclareMathOperator{\leq}{>}$</p></div>
<div class="bp-thmwrapper"><div class="bp-thmheading"><span class="bp-thmtitle">Top `$\DeclareMathOperator{\leq}{>}$`</span></div>
<div class="mermaid">graph TD; a["$\label{m}$"]</div>
<p><span class="arithmatex">\(a \leq b\)</span></p>
</div></article></body></html>
"""


def test_the_page_input_reads_only_the_formulas_markdown_marked(tmp_path: Path) -> None:
    """Check judges the formulas the site's Markdown marks in an article, so
    those are all the page's input reads."""

    report = _node_report(tmp_path, _node_script(tmp_path), "new", first=_UNMARKED, second=_UNMARKED)

    assert report["errors"] == []
    for (page,) in report["passes"]:
        assert [math["tex"] for math in page["math"]] == ["a \\leq b"]
        assert _LEQ in page["math"][0]["mml"]


def test_a_title_or_discussion_shown_as_typed_is_not_typeset(tmp_path: Path) -> None:
    """Check reads a title and a discussion value as Markdown, where backticks
    make code, but the site prints them as typed, backticks and all."""

    blueprint = _vault(tmp_path / "vault")
    top = blueprint / "roadmap/top.md"
    top.write_text(
        top.read_text(encoding="utf-8")
        .replace("# Top", "# Top `$\\DeclareMathOperator{\\leq}{>}$`")
        .replace("discussion: 42", "discussion: `$\\label{x} \\DeclareMathOperator{\\leq}{>}$`"),
        encoding="utf-8",
    )
    chapter = blueprint / "roadmap/README.md"
    chapter.write_text(
        chapter.read_text(encoding="utf-8").replace("main result.", "main result, where $a \\leq b$."),
        encoding="utf-8",
    )
    assert main(["check", str(blueprint)]) == 0
    site = _render(blueprint)
    published = _published(site / "roadmap/README.md")
    assert "Top `$\\DeclareMathOperator{\\leq}{&gt;}$`" in published
    assert "`$\\label{x} \\DeclareMathOperator{\\leq}{&gt;}$`" in published
    page = f"<html><head></head><body><article>{published}</article></body></html>"
    mathjax_package()
    script = (site / "javascripts/mathjax.js").read_text(encoding="utf-8")

    report = _node_report(tmp_path, script, "new", first=page, second=page)

    assert report["errors"] == []
    articles = report["passes"][0][0]
    assert [math["tex"] for math in articles["math"]] == ["a \\leq b"]
    assert _LEQ in articles["math"][0]["mml"]


def test_no_formula_gives_its_output_a_class_style_id_or_link(tmp_path: Path) -> None:
    """Base's \\mmlToken sets any attribute a formula writes. Check refuses it
    in articles, and the testimony validator in cards, but each document's
    filter is what keeps the attributes off the page if one gets through:
    MathJax gives a document made outside its startup the filter's defaults,
    which pass classes that start with mjx-, colors and margins, and links."""

    mathjax_package()
    script = (_render(_vault(tmp_path / "vault")) / "javascripts/mathjax.js").read_text(encoding="utf-8")
    token = (
        "\\mmlToken{mi}[style='margin-top:-60em;color:red',class='mjx-x',href='https://evil.example/',"
        "id='mjx-x']{x}"
    )
    page = (
        f'<html><head></head><body><article><p><span class="arithmatex">\\({token}\\)</span></p>'
        f'<div class="bp-readback bp-readback-current" id="c1"><p><span class="arithmatex">\\({token}\\)'
        "</span></p></div></article></body></html>"
    )

    report = _node_report(tmp_path, script, "new", first=page, second=page)

    assert report["errors"] == []
    article, card = report["passes"][0]
    assert card["card"] == "c1"
    for document in (article, card):
        assert len(document["math"]) == 1
        mml = document["math"][0]["mml"]
        assert "<mi>x</mi>" in mml, mml


def _articles(*formulas: str) -> str:
    spans = "".join(f'<p><span class="arithmatex">\\({formula}\\)</span></p>' for formula in formulas)
    return f"<html><head></head><body><article>{spans}</article></body></html>"


#: Each tries to make a project macro spell \DeclareMathOperator out of what
#: follows it, and is followed by the formula that shows whether it did.
_SPLICES = (
    "\\op{\\D}{\\leq}{>}",
    "\\opt[\\D]{\\leq}{>}",
    "\\nl DeclareMathOperator{\\leq}{>}",
    "\\op\\D{\\leq}{>}",
)


def test_project_macros_cannot_join_into_a_command(tmp_path: Path) -> None:
    """MathJax expands a macro by splicing strings. A body that ends in a
    single backslash joins the text after it into one command, so it is
    refused; what is left spells nothing new, as MathJax itself shows."""

    mathjax_package()
    from autoform_cli.mathjax import _script

    accepted = {"op": ["#1eclareMathOperator", 1], "opt": ["#1eclareMathOperator", 1, "x"], "nl": "\\\\"}
    blueprint = _vault(tmp_path / "vault", macros=json.dumps(accepted))
    assert main(["check", str(blueprint)]) == 0
    script = (_render(blueprint) / "javascripts/mathjax.js").read_text(encoding="utf-8")
    page = _articles(*(formula for splice in _SPLICES for formula in (splice, "a \\leq b")))

    report = _node_report(tmp_path, script, "new", first=page, second=page)

    assert report["errors"] == []
    shown = report["passes"][0][0]["math"]
    assert [math["tex"] for math in shown[::2]] == list(_SPLICES)
    for splice, math in zip(_SPLICES, shown[1::2], strict=True):
        assert _LEQ in math["mml"] and "&gt;" not in math["mml"], splice

    # The refused forms, written past check, do declare it.
    for macros, splice in (
        ({"bs": "\\"}, "\\bs DeclareMathOperator{\\leq}{>}"),
        ({"L": ["#1DeclareMathOperator", 1, "\\"]}, "\\L{\\leq}{>}"),
    ):
        page = _articles(splice, "a \\leq b")
        report = _node_report(tmp_path, _script(macros), "new", first=page, second=page)
        assert report["errors"] == []
        assert "<mo>&gt;</mo>" in report["passes"][0][0]["math"][1]["mml"], splice


@pytest.mark.parametrize("changed", ["page", "c1", "c2"])
def test_a_menu_setting_changes_every_formula_on_the_page(tmp_path: Path, changed: str) -> None:
    """Each card is a document of its own, with a menu of its own, but a
    reader sees one page: a setting changed from any formula applies to all,
    the explorer a screen reader uses too, which remakes the document of the
    menu that loads it, and each card keeps its own TeX input."""

    mathjax_package()
    script = (_render(_vault(tmp_path / "vault")) / "javascripts/mathjax.js").read_text(encoding="utf-8")

    report = _node_report(tmp_path, script, "new", changed=changed)

    assert report["errors"] == []
    menus = report["menus"]
    assert [menu["card"] for menu in menus["menus"]] == ["page", "c1", "c2"]
    for menu in menus["menus"]:
        assert menu["settings"] == {"renderer": "SVG", "assistiveMml": False, "scale": "1", "explorer": True}
        assert menu["applied"] == [["renderer", "SVG"], ["assistiveMml", False], ["explorer", True]]
    assert menus["inputs"] and menus["handler"] and menus["jax"] and menus["defaults"]
    assert menus["output"] and menus["shown"]
    # The next page starts with the renderer the reader chose.
    first, second = report["passes"]
    assert [document["output"] for document in first] == ["CHTML", "CHTML", "CHTML"]
    assert [document["output"] for document in second] == ["SVG", "SVG"]


def test_the_document_mathjax_starts_with_holds_no_formula(tmp_path: Path) -> None:
    """MathJax makes a document for the page when it starts, with a menu,
    which renders it when it applies a saved setting. That document finds
    nothing, so every formula is read by a pass, with a new input, and its
    menu shares the reader's settings."""

    mathjax_package()
    script = (_render(_vault(tmp_path / "vault")) / "javascripts/mathjax.js").read_text(encoding="utf-8")

    report = _node_report(tmp_path, script, "new", startup="renders")

    assert report["errors"] == []
    assert report["startup"]["math"] == 0
    page, c1, c2 = report["passes"][0]
    assert len(page["math"]) == 5
    assert len(c1["math"]) == len(c2["math"]) == 2


def test_no_document_is_made_while_a_menu_is_loading(tmp_path: Path) -> None:
    """A menu that asks for a renderer another menu is loading is never told
    it has loaded, so the documents of a pass wait for the load the first
    menu starts for a saved setting."""

    mathjax_package()
    script = (_render(_vault(tmp_path / "vault")) / "javascripts/mathjax.js").read_text(encoding="utf-8")

    report = _node_report(tmp_path, script, "new", startup="loads")

    assert report["errors"] == []
    assert report["startup"]["made"] == 0
    assert [document["card"] for document in report["passes"][0]] == [None, "c1", "c2"]

