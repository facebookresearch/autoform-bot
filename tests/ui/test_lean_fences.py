"""Lean fences on the site (#772): render publishes a bare ``lean`` or ``lean4`` fence at a page's top level, or in a
statement's box, as one line of its own Lean marking, which MkDocs keeps as written, and leaves every other fence for
MkDocs to highlight, as before."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

from autoform_cli.markdown import site_converter
from autoform_cli.scaffold import scaffold_project
from autoform_cli.ui.render import (
    _lean_code,
    _lean_fences_as_blocks,
    _protect_markdown_in_html,
    _protect_narrative_markdown,
    _shared_references,
    render_site,
)

_BLOCK = re.compile(r'<pre class="bp-lean-fence"><code>(.*?)</code></pre>', re.DOTALL)
_HEADER = "---\n{}\n---\n"
#: Lean that a reading of a page's lines, or MkDocs, could take for something else, each in a fence that holds it.
_FENCES = {
    "blank lines": ("```lean", "theorem two : 1 + 1 = 2 := by\n\n  rfl\n\n\nexample : True := trivial"),
    "Markdown": ("```lean4", "- item\n# heading\n*x* and __y__\n    four spaces\n1. one\n> quote\n[x]: y\n| a | b |"),
    "HTML": ("``` lean", "a & b < c > d &amp; <b>bold</b> &lt; \"q\" 'q'"),
    "formulas": ("~~~lean", "f $ g x, \\( y \\) and $$ \\[ z \\]"),
    "fence markers": ("~~~~lean4 ", "~~~\n```\n````"),
    "tabs": ("````lean", "\tx\n  \t y"),
    "nothing": ("```lean", ""),
}


def _fence(opener: str, code: str) -> str:
    return f"{opener}\n{code}\n{opener.split('lean')[0].strip()}"


def _read_back(converted: str) -> list[str]:
    """Each block's code as a reader copies it: its tags stripped and its HTML unescaped."""
    return [html.unescape(re.sub(r"<[^>]*>", "", block)) for block in _BLOCK.findall(converted)]


@pytest.mark.parametrize(("opener", "code"), _FENCES.values(), ids=_FENCES)
def test_a_bare_lean_fence_reads_back_as_its_code(opener: str, code: str) -> None:
    page = f"{_HEADER}\n# Notes\n\nBefore.\n\n{_fence(opener, code)}\n\nAfter.\n"
    written = _lean_fences_as_blocks(page)
    body = written.removeprefix(_HEADER)

    # One line, so no reading of the page's lines finds a fence, a heading or a list in it.
    assert [line for line in body.splitlines() if "bp-lean-fence" in line] == [_BLOCK.search(body)[0]]
    assert written.replace(_BLOCK.search(body)[0], _fence(opener, code)) == page
    boxed = f'<div class="bp-thmcontent" markdown="1">\n\n{body}\n\n</div>'
    for converted in (site_converter().convert(body), site_converter().convert(boxed)):
        assert _read_back(converted) == [code]
        assert "highlight" not in converted and "arithmatex" not in converted


def test_lean_keywords_comments_and_strings_are_marked_and_names_are_not() -> None:
    written = _lean_fences_as_blocks(f'{_HEADER}\n```lean\nabbrev X := "s" -- c\n#check X\n```\n')
    marked = (
        '<span class="bp-lean-keyword">abbrev</span> X := <span class="bp-lean-string">&quot;s&quot;</span> '
        '<span class="bp-lean-comment">-- c</span>&#10;<span class="bp-lean-keyword">#check</span> X'
    )

    assert _BLOCK.findall(written) == [marked]


def test_lean_commands_are_keywords_where_a_token_starts() -> None:
    """``#check`` and its kin are keywords, in a fence as under a statement, unless a word or a ``#`` comes
    before or a word goes on after."""
    marked = _lean_code("#check a\n#eval b #print c (#reduce d) #synth e a#check ##check #checks")

    assert re.findall(r'<span class="bp-lean-keyword">([^<]*)</span>', marked) == [
        "#check",
        "#eval",
        "#print",
        "#reduce",
        "#synth",
    ]


@pytest.mark.parametrize(
    "page",
    [
        pytest.param("- item\n\n  ```lean\n  x\n  ```\n", id="in a list"),
        pytest.param("- item\n```lean\nx\n```\n", id="after a list item"),
        pytest.param("> ```lean\n> x\n> ```\n", id="in a quote"),
        pytest.param("> quote\n```lean\nx\n```\n", id="after a quote"),
        pytest.param("Before.\n```lean\nx\n```\n", id="after a paragraph"),
        pytest.param("```lean\nx\n```\nAfter.\n", id="before a paragraph"),
        pytest.param("```{.lean}\nx\n```\n", id="attribute list"),
        pytest.param('```lean title="Main.lean"\nx\n```\n', id="title"),
        pytest.param('```lean hl_lines="1"\nx\n```\n', id="hl_lines"),
        pytest.param('```lean linenums="1"\nx\n```\n', id="linenums"),
        pytest.param("```lean\nx\n\nmore\n", id="unterminated"),
        # Render reads the rest of the page as the code of a fence never closed.
        pytest.param("````lean\nx\n\n```lean\ny\n```\n", id="after a fence never closed"),
        pytest.param("```lean\nx\n  ```\n\nmore\n", id="indented closing marker"),
        pytest.param(" ```lean\nx\n```\n", id="indented opening marker"),
        pytest.param("```python\nx\n```\n", id="another language"),
        pytest.param("```Lean\nx\n```\n", id="Lean capitalised"),
    ],
)
def test_any_other_fence_stays_as_it_was(page: str) -> None:
    # After a bare fence, which render rewrites, so it reads the whole page.
    written = _lean_fences_as_blocks(f"{_HEADER}\n```lean\nbare\n```\n\n{page}")

    assert written == f'{_HEADER}\n<pre class="bp-lean-fence"><code>bare</code></pre>\n\n{page}'


def test_a_fence_that_opens_the_page_is_at_its_top_level() -> None:
    assert _BLOCK.findall(_lean_fences_as_blocks(f"{_HEADER}```lean\nx\n```\n")) == ["x"]


def test_a_fence_closes_at_the_first_closing_marker_long_enough() -> None:
    written = _lean_fences_as_blocks(f"{_HEADER}\n````lean\n```\n~~~~\n`````\n\n```lean\ny\n```\n")

    assert _read_back(written) == ["```\n~~~~", "y"]


@pytest.mark.parametrize(("code", "converted"), [("example : 1 < 2 := by decide", True), ("a ≤ b", False)])
def test_a_less_than_sign_in_a_fence_still_decides_whether_render_converts_the_page(code: str, converted: bool) -> None:
    """Render converts a stretch itself when the author's text holds a "<", one in a fence included, as before."""
    page = f"{_HEADER}\n# Notes\n\n```lean\n{code}\n```\n"
    written = _lean_fences_as_blocks(page)
    body = written.removeprefix(_HEADER)

    assert (_protect_narrative_markdown(page) != page) is converted
    assert (_protect_narrative_markdown(written) != written) is converted
    assert (_protect_markdown_in_html(body) != body) is converted
    assert (_shared_references(written) is not None) is converted
    assert _read_back(_protect_narrative_markdown(written)) == [code]


def test_mkdocs_publishes_each_fence_as_render_wrote_it(tmp_path: Path) -> None:
    """Built with MkDocs and the theme: each bare fence on a page, in a statement and in its notes reads back as its
    code, a fence in a list keeps its highlighting, and a page shown as typed shows its fences as typed."""
    pytest.importorskip("material")
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    scaffold_project(tmp_path, title="Fences")
    blueprint = tmp_path / "blueprint"
    (blueprint / "notes.md").write_text(
        "# Notes\n\n"
        + "\n\n".join(_fence(opener, code) for opener, code in _FENCES.values())
        + "\n\n- In a list:\n\n  ```lean\n  listed\n  ```\n",
        encoding="utf-8",
    )
    (blueprint / "typed.md").write_text("# Typed\n\n<b>raw</b>\n\n```lean\nas typed\n```\n", encoding="utf-8")
    (blueprint / "roadmap/README.md").write_text(
        "# Roadmap\n\nThe chapter.\n\n- [Plain](plain.md)\n- [Ordered](ordered.md)\n", encoding="utf-8"
    )
    # Render converts the box of a statement whose Lean holds a "<" itself, and leaves the other's to MkDocs.
    for name, lean in (("plain", "theorem plain : True := trivial"), ("ordered", "example : 1 < 2 := by decide")):
        (blueprint / f"roadmap/{name}.md").write_text(
            f"---\ndeclaration: theorem\n---\n\n# {name.title()}\n\nThe {name} result.\n\n```lean\n{lean}\n```\n\n"
            f"## Remarks\n\n```lean4\n#check {name}\n```\n\n## Depends on\n\nNo prerequisites.\n",
            encoding="utf-8",
        )

    render_site(blueprint, tmp_path / "site-src", lean_root=tmp_path)
    chapter = (tmp_path / "site-src/roadmap/README.md").read_text(encoding="utf-8")
    assert "\nThe plain result.\n" in chapter and "<p>The ordered result.</p>" in chapter
    typed = (tmp_path / "site-src/typed.md").read_text(encoding="utf-8")
    assert "as typed" in typed and "bp-lean-fence" not in typed
    build(load_config(str(tmp_path / "mkdocs.yml")))

    notes = (tmp_path / "site/notes.html").read_text(encoding="utf-8")
    assert _read_back(notes) == [code for _, code in _FENCES.values()]
    assert notes.count('<div class="highlight">') == 1 and "listed" in notes.split('<div class="highlight">')[1]
    built = (tmp_path / "site/roadmap/index.html").read_text(encoding="utf-8")
    assert _read_back(built) == [
        "theorem plain : True := trivial",
        "#check plain",
        "example : 1 < 2 := by decide",
        "#check ordered",
    ]
