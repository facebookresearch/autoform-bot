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
from autoform_cli.markdown import statement_and_notes
from autoform_cli.readback import publishable_article
from autoform_cli.render import PublicationError, render_site
from autoform_cli.scaffold import scaffold_project
from tests.test_render import _project

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


@pytest.mark.parametrize(
    ("macros", "reason"),
    [
        ("{", "tex-macros.json: not valid JSON"),
        ('{"RR": "x", "RR": "y"}', "'RR' is defined twice"),
        ('["RR"]', "tex-macros.json: must be a JSON object from macro names to definitions"),
        ('{"R R": "x"}', "'R R' is not a macro name; use letters only"),
        ('{"f": ["#1", 10]}', "\\f takes 10 arguments; use 0 to 9"),
        ('{"f": ["#1", true]}', "\\f must be a body, [body, arguments], or [body, arguments, default]"),
        ('{"f": "\\\\DeclareMathOperator{\\\\leq}{>}"}', "\\f uses \\DeclareMathOperator, which would change"),
        ('{"f": ["#1", 1, "\\\\label{x}"]}', "\\f uses \\label, which would change other formulas"),
    ],
)
def test_project_macros_that_cannot_be_used_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], macros: str, reason: str
) -> None:
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
        ("javascripts/mathjax.js", _directory_with_a_file,
         "javascripts/mathjax.js: is a directory, where autoform render writes a file; remove it"),
        ("javascripts/mathjax.js", _symlink,
         "javascripts/mathjax.js: is a symlink, where autoform render writes a file; remove it"),
        ("javascripts/mathjax.js", _fifo,
         "javascripts/mathjax.js: is a special file, where autoform render writes a file; remove it"),
        ("javascripts", _file,
         "javascripts: is a file, where autoform render needs a folder for javascripts/blueprint-mermaid.js; "
         "remove it"),
        ("stylesheets/blueprint.css", _directory_with_a_file,
         "stylesheets/blueprint.css: is a directory, where autoform render writes a file; remove it"),
        ("assets/autoform.svg", _fifo,
         "assets/autoform.svg: is a special file, where autoform render writes a file; remove it"),
        ("tex-macros.json", _directory_with_a_file,
         "tex-macros.json: is a directory, where autoform reads the project's macros from a file; remove it"),
        ("tex-macros.json", _symlink,
         "tex-macros.json: is a symlink, where autoform reads the project's macros from a file; remove it"),
    ],
)
def test_something_other_than_a_file_where_the_site_has_one_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], relative: str, make, reason: str
) -> None:
    """Render writes the site's stylesheet, scripts, and logo over the vault's
    copies, and reads the project's macros from the vault, so a directory,
    symlink, or special file there is named by check rather than crashing,
    hanging, or being skipped by render."""

    blueprint = _vault(tmp_path)
    make(blueprint / relative)

    assert main(["check", str(blueprint)]) == 1
    assert reason in capsys.readouterr().out
    with pytest.raises(PublicationError):
        _render(blueprint)


@pytest.mark.parametrize(
    ("article", "commands"),
    [
        ("$\\DeclareMathOperator{\\leq}{>}$, so $a \\leq b$.", "\\DeclareMathOperator"),
        ("Here \\\\(\\\\newcommand{\\\\RR}{\\\\mathbb{R}}\\\\) is live.", "\\newcommand"),
        ("$$\n\\def\\x{1} \\let\\y\\x\n$$", "\\def, \\let"),
        ("$\\DeclarePairedDelimiter\\abs{\\lvert}{\\rvert}$", "\\DeclarePairedDelimiter"),
        ("$\\newtagform{p}{(}{)} \\usetagform{p}$", "\\newtagform, \\usetagform"),
        ("$x = 1 \\label{one}$", "\\label"),
        ("$\\require{mhchem}$", "\\require"),
        ("$\\gdef\\x{1}$ and $\\global\\edef\\y{2}$", "\\gdef, \\global, \\edef"),
    ],
)
def test_tex_that_changes_other_formulas_is_refused(article: str, commands: str) -> None:
    """Every article on a page is typeset with one TeX input, so a definition
    in one would change what the others show."""

    errors = publishable_article(article)[1]

    assert errors == (
        f"TeX commands that change other formulas are not allowed: {commands}; define notation in "
        "the vault's tex-macros.json instead, and put a command you only name in code",
    )


def test_render_refuses_a_definition_in_an_article(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path)
    _with_article(blueprint, "Let $\\DeclareMathOperator{\\leq}{>}$ hold.")

    assert main(["check", str(blueprint)]) == 1
    with pytest.raises(PublicationError, match=r"top: TeX commands that change other formulas"):
        _render(blueprint)


@pytest.mark.parametrize(
    "article",
    [
        "Write `\\newcommand` in a project's macros file instead.",
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

    assert any(error.startswith(reason) for error in errors), errors


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

    assert f"raw HTML is not allowed: {names}; in a formula, put a space after <" in errors


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

    assert errors == (f"HTML character references are not allowed: {shown}; type the character itself",)


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


@pytest.mark.parametrize("separator", ["\u2028", "\x0b", "\x0c", "\x1e", "\x85"])
def test_a_line_break_markdown_does_not_read_is_checked_as_the_site_writes_it(
    tmp_path: Path, separator: str
) -> None:
    """The site writes every line break Python reads as a newline, so text
    after one is checked on a line of its own, where it is HTML, not code."""

    blueprint = _vault(tmp_path)
    _with_article(blueprint, f"Every object is equal to itself.\n\n    x = 1{separator}{_FORGED}")

    assert main(["check", str(blueprint)]) == 1
    with pytest.raises(PublicationError, match=r"top: raw HTML is not allowed: <span>, </span>, <script>, </script>"):
        _render(blueprint)


@pytest.mark.parametrize("separator", ["\u2028", "\x0c", "\x85"])
@pytest.mark.parametrize(
    ("payload", "refusal"),
    [
        (_FORGED, r"roadmap: raw HTML is not allowed: <span>, </span>, <script>, </script>"),
        (r"$\DeclareMathOperator{\leq}{>}$", r"roadmap: TeX commands that change other formulas are not allowed: \\DeclareMathOperator"),
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
    with pytest.raises(PublicationError, match=r"roadmap: raw HTML is not allowed: <span>, </span>, <script>, </script>"):
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


def test_every_formula_paints_inside_its_own_box(tmp_path: Path) -> None:
    """No formula can cover a card, a mark, or other text, and a wide one
    scrolls with what holds it rather than being cut off at the window."""

    css = (_render(_vault(tmp_path)) / "stylesheets/blueprint.css").read_text(encoding="utf-8")
    rules = {
        selector.strip(): body
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
    }

    assert "contain: paint" in rules['.md-typeset mjx-container[jax="CHTML"]']
    inline = rules['.md-typeset mjx-container[jax="CHTML"]:not([display="true"])']
    assert "display: inline-block" in inline
    assert "min-width: max-content" in rules['.md-typeset mjx-container[jax="CHTML"][display="true"]']
    assert "overflow-x: auto" in rules[".bp-readback"]


# Runs javascripts/mathjax.js in node against a local MathJax, as a page would,
# and reports what each document it made typeset. argv: the script, a page,
# the next page as Material's instant navigation shows it, and "new" or "old":
# whether mkdocs.yml also lists the bundle, after the script.
_HARNESS = r"""
"use strict";
const fs = require("fs");
const path = require("path");
const [scriptPath, firstPath, secondPath, project] = process.argv.slice(2);
const report = {injected: [], errors: [], passes: [], inventory: [], version: null, base: null, load: null};
global.window = globalThis;
let contentLoaded = null;
global.document = {
  readyState: project === "old" ? "loading" : "complete",
  head: {appendChild: (element) => report.injected.push(element.src)},
  createElement: () => ({}),
  addEventListener: (type, listener) => { if (type === "DOMContentLoaded") contentLoaded = listener; },
};
console.error = (...args) => report.errors.push(args.map(String).join(" "));
let shown = null;
global.document$ = {subscribe: (listener) => { shown = listener; }};

(0, eval)(fs.readFileSync(scriptPath, "utf8"));
report.base = MathJax.loader.paths.mathjax;
report.load = MathJax.loader.load.slice();
// The bundle carries these, and the menu, which needs a browser.
MathJax.loader.load.push("input/tex", "input/mml", "output/chtml", "a11y/assistive-mml");
MathJax.startup.document = fs.readFileSync(firstPath, "utf8");
// The page has been parsed; MathJax would take its document from here.
delete global.document;
const main = require(path.join(process.env.AUTOFORM_MATHJAX_DIR, "es5", "node-main.js"));
if (contentLoaded) contentLoaded();

let docs = [];
function describe() {
  return docs.map((doc) => {
    const card = doc.options.elements ? MathJax.startup.adaptor.getAttribute(doc.options.elements[0], "id") : null;
    return {
      card,
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

main.init({}).then(async () => {
  report.version = MathJax.version;
  if (!shown) return;
  const original = MathJax._.mathjax.mathjax.document;
  MathJax._.mathjax.mathjax.document = function (root, options) {
    const doc = original.call(this, root, options);
    docs.push(doc);
    return doc;
  };
  // document$ shows the first page again to each subscriber.
  await shown();
  report.passes.push(describe());
  inventory(docs[0].inputJax[0]);
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
<p id="a3"><span class="arithmatex">\(\label{shared} \nothere\)</span> $p \leq q$</p>
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


def _node_report(tmp_path: Path, script: str, project: str) -> dict:
    files = {"harness.js": _HARNESS, "mathjax.js": script, "first.html": _FIRST_PAGE, "second.html": _SECOND_PAGE}
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    done = subprocess.run(
        ["node", "harness.js", "mathjax.js", "first.html", "second.html", project],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize("project", ["new", "old"])
def test_the_rendered_configuration_typesets_each_card_alone(tmp_path: Path, project: str) -> None:
    """MathJax itself, run on the script render wrote, as a page loads it now
    and as a project scaffolded with the bundle in mkdocs.yml loads it."""

    mathjax = os.environ.get("AUTOFORM_MATHJAX_DIR")
    if shutil.which("node") is None or not mathjax:
        pytest.skip("needs node and AUTOFORM_MATHJAX_DIR, an unpacked mathjax package")
    blueprint = _vault(tmp_path / "vault", macros=json.dumps(_MACROS))
    script = (_render(blueprint) / "javascripts/mathjax.js").read_text(encoding="utf-8")
    # The release tested is the release the site loads.
    assert json.loads((Path(mathjax) / "package.json").read_text(encoding="utf-8"))["version"] == "3.2.2"

    report = _node_report(tmp_path, script, project)

    assert report["errors"] == []
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


def test_the_rendered_configuration_refuses_another_release(tmp_path: Path) -> None:
    mathjax = os.environ.get("AUTOFORM_MATHJAX_DIR")
    if shutil.which("node") is None or not mathjax:
        pytest.skip("needs node and AUTOFORM_MATHJAX_DIR, an unpacked mathjax package")
    script = (_render(_vault(tmp_path / "vault")) / "javascripts/mathjax.js").read_text(encoding="utf-8")

    report = _node_report(tmp_path, script.replace('"version": "3.2.2"', '"version": "3.2.1"'), "old")

    assert report["passes"] == []
    assert report["errors"] == [
        "autoform: this page loaded MathJax 3.2.2, but javascripts/mathjax.js was written for 3.2.1; formulas "
        "are left as typed. Load MathJax only through javascripts/mathjax.js in mkdocs.yml."
    ]
