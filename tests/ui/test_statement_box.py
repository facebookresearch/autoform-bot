"""The relations a statement's box lists (:mod:`autoform_cli.ui.statement_box`), for graphs the loader builds."""

from __future__ import annotations

import copy
from pathlib import Path

import html5lib
import pytest

from autoform_cli import status
from autoform_cli.graph import Graph, load_graph
from autoform_cli.ui import statement_box


def _graph(tmp_path: Path, title: str = "A") -> Graph:
    """F uses A in its statement and its proof, each listed twice, which the loader reads as one use of each kind."""
    roadmap = tmp_path / "roadmap"
    roadmap.mkdir()
    (roadmap / "README.md").write_text("# Chapter\n\n- [A](A.md)\n- [F](F.md)\n", encoding="utf-8")
    (roadmap / "A.md").write_text(f"# {title}\n\nAn authored base.\n", encoding="utf-8")
    (roadmap / "F.md").write_text(
        "# F\n\nAn authored result.\n\n## Depends on\n\n- [A](A.md)\n- [A again](A.md)\n\n"
        "## Proof depends on\n\n- [A](A.md)\n- [A again](A.md)\n", encoding="utf-8",
    )
    graph = load_graph(tmp_path)
    assert graph.nodes["F"].statement_dependencies == ("A",) and graph.nodes["F"].proof_dependencies == ("A",)
    return graph


@pytest.mark.parametrize(("box", "other"), [("F", "A"), ("A", "F")], ids=["uses", "used-by"])
def test_a_reference_shared_by_statement_and_proof_names_both_kinds_in_one_row(tmp_path: Path, box: str, other: str) -> None:
    """F's box lists A among what it uses, and A's box lists F among what uses it: both rows describe F's use of A."""
    graph = _graph(tmp_path)
    before = copy.deepcopy(graph)
    markup = statement_box._dependency_disclosure(
        graph.nodes[box], graph=graph, statuses=status.derive(graph),
        used_by={"A": ["F"], "F": []}, informally_used_by={"A": [], "F": []}, links={"A": "A.html", "F": "F.html"},
    )
    tree = html5lib.parseFragment(markup, namespaceHTMLElements=False)
    link = next(link for link in tree.iter("a") if link.get("href") == f"{other}.html")
    assert "".join(link.itertext()) == other
    # The counts describe the link; they do not rename it.
    assert link.get("aria-label") is None
    assert link.get("title") == "1 statement dependency, 1 proof dependency"
    assert "Proof uses" not in "".join(tree.itertext())
    assert graph == before


def test_an_annotated_reference_keeps_its_titles_math_and_code_and_escapes_its_text(tmp_path: Path) -> None:
    graph = _graph(tmp_path, r'Groups $x$ and `$literal$` "quoted" & rings')
    before = copy.deepcopy(graph)
    options = {"graph": graph, "statuses": status.derive(graph), "links": {"A": "A.html", "F": "F.html"}}
    plain_markup = statement_box._node_references(["A"], **options)
    annotated_markup = statement_box._node_references(["A"], **options, relation_node=graph.nodes["F"])
    plain = next(html5lib.parseFragment(plain_markup, namespaceHTMLElements=False).iter("a"))
    annotated = next(html5lib.parseFragment(annotated_markup, namespaceHTMLElements=False).iter("a"))
    assert plain.get("title") is None and plain.get("href") == annotated.get("href") == "A.html"
    assert set(annotated.attrib) == {"class", "href", "title"} and annotated.get("aria-label") is None
    assert annotated.get("title") == "1 statement dependency, 1 proof dependency"
    # The annotation leaves the label as the plain reference draws it: the formula marked for MathJax, the code literal.
    for link in (plain, annotated):
        assert [node.text for node in link.iter("code")] == ["$literal$"]
        assert [node.text for node in link.iter("span")] == [r"\(x\)"]
        assert all(node.tag not in {"script", "img", "iframe"} for node in link.iter())
    assert "".join(plain.itertext()) == "".join(annotated.itertext())
    assert '&quot;quoted&quot; &amp; rings' in annotated_markup and plain_markup.split(">", 1)[1] in annotated_markup
    assert graph == before
