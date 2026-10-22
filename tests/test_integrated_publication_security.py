"""The reader's generated containers must not change authored code into HTML."""

from __future__ import annotations

from pathlib import Path

import html5lib
import pytest

from autoform_cli.markdown import frontmatter_end, site_converter
from autoform_cli.render import _integrated_page, _read_page


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
