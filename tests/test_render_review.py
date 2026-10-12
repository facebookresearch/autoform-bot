from dataclasses import replace
from pathlib import Path
import re

import markdown as markdown_renderer
import pytest

from autoform_cli.markdown import site_converter
from autoform_cli.readback import Readback, load_readbacks
from autoform_cli.render import _readback_block, _skeleton_block
from autoform_cli.review import ReviewDeclaration
from tests.test_readback_testimony import _ATTRIBUTE_LISTS, _declaration, _file


def test_review_displays_the_exact_hashed_blind_packet() -> None:
    declaration = _declaration()
    packet = ReviewDeclaration(declaration.name, declaration.hash, declaration.evidence_hash, declaration.blind_text())

    rendered = "\n".join(_skeleton_block(declaration, packet))

    assert "reviewer hint" not in rendered
    assert "Review.result : True" in rendered
    assert declaration.evidence_hash in rendered


def test_readback_markdown_cannot_inject_raw_html() -> None:
    declaration = _declaration()
    readback = Readback(
        article_id="af_0123456789abcdef01234567",
        declaration=declaration.name,
        skeleton_hash=declaration.hash,
        packet_hash=declaration.evidence_hash,
        model="reviewer",
        text="**Result:** $P$. <script>alert(1)</script> [run](javascript:alert(1)) ![x](https://example.test/x.png)",
        path=Path("card.md"),
        shown_hash=declaration.evidence_hash,
    )

    rendered = "\n".join(_readback_block(declaration, readback))

    assert "**Result:** $P$." in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    published = markdown_renderer.markdown(
        rendered, extensions=["attr_list", "md_in_html", "pymdownx.arithmatex", "pymdownx.superfences"]
    )
    assert "href=" not in published
    assert "<img" not in published
    assert "<pre><code>" in published

    invalid = "\n".join(
        _readback_block(declaration, replace(readback, validation_errors=("raw HTML is not allowed",)))
    )
    assert "bp-readback-invalid" in invalid
    assert "invalid · raw HTML is not allowed" in invalid


@pytest.mark.parametrize("testimony", _ATTRIBUTE_LISTS)
def test_attribute_lists_in_a_card_are_shown_as_typed_on_the_page(testimony: str, tmp_path: Path) -> None:
    """The site's converter, which reads attribute lists, does not read a card
    again on the page."""

    _file(tmp_path, testimony)
    declaration = _declaration()
    card = load_readbacks(tmp_path)[("af_0123456789abcdef01234567", declaration.name)]
    box = ['<div class="bp-thmwrapper" markdown="1">', "", *_readback_block(declaration, card), "</div>"]
    published = site_converter().convert("\n".join(box))

    assert "{" in published
    tags = re.findall(r"<[a-z]+\s[^>]*>", published)
    assert {name for tag in tags for name in re.findall(r"\s([^\s=>]+)=", tag)} == {"class"}
    assert set(" ".join(re.findall(r'class="([^"]*)"', published)).split()) <= {
        "bp-thmwrapper", "bp-readback", "bp-readback-current", "bp-readback-invalid", "bp-readback-title",
        "bp-readback-status", "bp-readback-body", "arithmatex",
    }
