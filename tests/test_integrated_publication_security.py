"""The reader's generated containers must not change authored code into HTML."""

from __future__ import annotations

from pathlib import Path

import html5lib
import pytest

from autoform_cli.markdown import frontmatter_end, site_converter
from autoform_cli.render import _integrated_page, _place_environments, _read_page


def _built_tree(page: str):
    # MkDocs consumes the generated metadata before invoking Python-Markdown.
    lines = page.splitlines()
    body = "\n".join(lines[frontmatter_end(lines):])
    return html5lib.parse(site_converter().convert(body), treebuilder="etree", namespaceHTMLElements=False)


@pytest.mark.parametrize("example", [
    "A literal tag: `</div></div><script>window.PWNED_A=1</script>` in code.",
    "An ordinary closing tag: `</div>` in code.",
    "Two-backtick code: ``</div><img src=x onerror=window.PWNED_B=1>``.",
    "Indented code:\n\n    </div></div><script>window.PWNED_C=1</script>\n",
    "```html\n</div></div><script>window.PWNED_D=1</script>\n```\n",
])
def test_integrated_reader_keeps_literal_code_inert_and_retains_its_suffix(example: str) -> None:
    authored = "# Code and mathematics\n\n" + example + (
        "\n\n## Retained mathematical section\n\nThe full suffix remains. "
        "The inequality $a<b$ is retained. [Another section](#retained-mathematical-section).\n"
    )
    published, findings, _notation = _read_page(authored)
    assert findings == ()
    page = Path("/site/roadmap/chapter.md")
    wrapped = _integrated_page(
        published, page=page, destination=Path("/site"), payload=Path("/site/dependencies.json"),
        scope="chapter", owner="chapter",
    )
    tree = _built_tree(wrapped)
    readers = [node for node in tree.iter() if "bp-reading-source" in node.get("class", "").split()]
    assert len(readers) == 1
    reader = readers[0]
    assert not any(node.tag in {"script", "img"} for node in reader.iter())
    assert not any(key.lower().startswith("on") for node in tree.iter() for key in node.attrib)
    scripts = [node for node in tree.iter() if node.tag == "script"]
    assert scripts and all(node.get("src") and not "".join(node.itertext()).strip() for node in scripts)
    assert "The full suffix remains." in "".join(reader.itertext())
    assert any(node.get("id") == "retained-mathematical-section" for node in reader.iter())
    assert ["".join(node.itertext()) for node in reader.iter()
            if "arithmatex" in node.get("class", "").split()] == [r"\(a<b\)"]
    code = ["".join(node.itertext()) for node in reader.iter() if node.tag == "code"]
    expected = html5lib.parse(site_converter().convert(authored), treebuilder="etree", namespaceHTMLElements=False)
    assert code == ["".join(node.itertext()) for node in expected.iter() if node.tag == "code"]


def test_integrated_reader_keeps_page_wide_heading_ids_and_markdown_link_validation() -> None:
    body = "# Same\n\n## Same\n\n`</div>` [Other page](other.md#result).\n\n## Same\n\nStill retained.\n"
    published, findings, _notation = _read_page(body)
    assert findings == ()
    wrapped = _integrated_page(
        published, page=Path("/site/roadmap/chapter.md"), destination=Path("/site"),
        payload=Path("/site/dependencies.json"), scope="chapter", owner="chapter",
    )
    tree = _built_tree(wrapped)
    assert [node.get("id") for node in tree.iter() if node.tag in {"h1", "h2"}] == ["same", "same_1", "same_2"]
    assert any(node.tag == "a" and node.get("href") == "other.html#result" for node in tree.iter())
    witnesses = [node for node in tree.iter() if "bp-body-link-validation" in node.get("class", "").split()]
    assert witnesses and all("hidden" in node.attrib for node in witnesses)
    assert any(node.tag == "a" and node.get("href") == "other.md#result" for witness in witnesses for node in witness.iter())


def test_prepared_chapter_keeps_native_markdown_and_generated_card_markup() -> None:
    narrative = "# Chapter\n\nThe full introduction.\n\n## Results\n\n- [Top](top.md)\n\n[Other](other.md#result).\n"
    environment = (
        '<div class="bp-thmwrapper" id="top" markdown="1">\n'
        '<div class="bp-thmcontent" markdown="1">\n\nThe complete statement.\n\n</div>\n'
        '<details class="bp-dependencies"><summary>Dependencies</summary></details>\n</div>'
    )
    lines = narrative.splitlines()
    chapter = _place_environments(
        narrative, lines.index("## Results"), {lines.index("- [Top](top.md)"): "top"},
        summary='<div class="bp-progress">Progress</div>', environments={"top": environment}, protect=True,
    )
    wrapped = _integrated_page(
        chapter, page=Path("/site/roadmap/chapter.md"), destination=Path("/site"),
        payload=Path("/site/dependencies.json"), scope="chapter", prepared=True,
    )
    assert "# Chapter\n" in wrapped and "## Results\n" in wrapped
    assert "[Other](other.md#result)." in wrapped
    assert environment in wrapped
    assert "bp-body-link-validation" not in wrapped
    assert wrapped.count('id="top"') == 1


def test_protected_narrative_keeps_canonical_ids_code_math_and_cross_slot_references() -> None:
    narrative = (
        '# Top\n\nA literal `</div></div><script>unsafe()</script>` and [Paper][ref].\n\n'
        '## Results\n\n- [Top](top.md)\n\n## Retained section\n\nThe inequality $a<b$ remains.\n\n'
        '[ref]: paper.md#result "Source"\n'
    )
    environment = '<div class="bp-thmwrapper" id="top" markdown="1">\n\nThe complete statement.\n\n</div>'
    lines = narrative.splitlines()
    arguments = {
        "lead": lines.index("## Results"), "slots": {lines.index("- [Top](top.md)"): "top"},
        "summary": '<div class="bp-progress">Progress</div>', "environments": {"top": environment},
    }
    canonical = _place_environments(narrative, **arguments)
    chapter = _place_environments(narrative, **arguments, protect=True)
    wrapped = _integrated_page(
        chapter, page=Path("/site/roadmap/chapter.md"), destination=Path("/site"),
        payload=Path("/site/dependencies.json"), scope="chapter", prepared=True,
    )
    reader = next(node for node in _built_tree(wrapped).iter() if "bp-reading-source" in node.get("class", "").split())
    expected = html5lib.parse(site_converter().convert(canonical), treebuilder="etree", namespaceHTMLElements=False)
    assert [node.get("id") for node in reader.iter() if node.tag.startswith("h") and node.tag[1:].isdigit()] == [
        node.get("id") for node in expected.iter() if node.tag.startswith("h") and node.tag[1:].isdigit()
    ]
    ids = [node.get("id") for node in reader.iter() if node.get("id")]
    assert len(ids) == len(set(ids)) and ids.count("top") == 1
    assert ["".join(node.itertext()) for node in reader.iter("code")] == [
        "".join(node.itertext()) for node in expected.iter("code")
    ]
    assert not list(reader.iter("script"))
    assert any(node.tag == "a" and node.get("href") == "paper.html#result" and node.text == "Paper" for node in reader.iter())
    assert ["".join(node.itertext()) for node in reader.iter() if "arithmatex" in node.get("class", "").split()] == [r"\(a<b\)"]
    assert environment in wrapped


def test_compiled_runtime_failure_preserves_publication_and_cli_error_shape(tmp_path: Path, monkeypatch, capsys) -> None:
    from autoform_cli import dag_viewer
    from autoform_cli.__main__ import main
    from autoform_cli.render import PublicationError, render_site
    from autoform_cli.scaffold import scaffold_project

    project = tmp_path / "project"
    scaffold_project(project, title="Publication runtime fixture")
    output = tmp_path / "site"
    # This test isolates the publication transaction, not runtime verification.
    monkeypatch.setattr(dag_viewer, "viewer_script", lambda: "/* verified runtime fixture */\n")
    render_site(project / "blueprint", output)
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}

    def unavailable():
        raise ValueError("compiled explorer bundle does not match its packaged sources")

    monkeypatch.setattr(dag_viewer, "viewer_script", unavailable)
    with pytest.raises(PublicationError, match="packaged explorer runtime is unavailable"):
        render_site(project / "blueprint", output)
    assert {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()} == before
    assert main(["render", str(project / "blueprint"), "--output", str(output)]) != 0
    messages = capsys.readouterr()
    assert "packaged explorer runtime is unavailable" in messages.out + messages.err
    assert "Traceback" not in messages.out + messages.err
    assert {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()} == before


def test_a_post_slot_horizontal_rule_cannot_hide_unprotected_code_as_metadata() -> None:
    narrative = '# Chapter\n\n- [Top](top.md)\n---\nA literal `</div><script>unsafe()</script>`.\n---\n\nRetained suffix.\n'
    lines = narrative.splitlines()
    chapter = _place_environments(
        narrative, len(lines), {lines.index("- [Top](top.md)"): "top"}, summary="",
        environments={"top": '<div class="bp-thmwrapper" id="top" markdown="1">Statement.</div>'}, protect=True,
    )
    wrapped = _integrated_page(
        chapter, page=Path("/site/roadmap/chapter.md"), destination=Path("/site"),
        payload=Path("/site/dependencies.json"), scope="chapter", prepared=True,
    )
    reader = next(node for node in _built_tree(wrapped).iter() if "bp-reading-source" in node.get("class", "").split())
    assert not list(reader.iter("script"))
    assert "Retained suffix." in "".join(reader.itertext())
    assert ["".join(node.itertext()) for node in reader.iter("code")] == ["</div><script>unsafe()</script>"]
