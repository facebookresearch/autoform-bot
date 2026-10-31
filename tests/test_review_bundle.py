from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli import render
from autoform_cli.approvals import approval_statuses
from autoform_cli.audit import audit_blueprint
from autoform_cli.graph import Graph, load_graph
from autoform_cli.markdown import statement_and_notes
from autoform_cli.publishable import publishable_article
from autoform_cli.readback import Readback, load_readbacks, readback_path
from autoform_cli.review import (
    ReviewError,
    build_review_bundle,
    canonical_statement,
    read_review_packet,
    review_findings,
    review_states,
    write_review_packets,
)
from autoform_cli.skeleton import (
    DeclarationSkeleton,
    NodeSkeleton,
    SkeletonError,
    SkeletonReport,
    UnresolvedTarget,
    blueprint_hash,
    write_packets,
)
from tests.skeleton_fixtures import BLUEPRINT_HASH as _BLUEPRINT_HASH
from tests.skeleton_fixtures import SEMANTIC
from tests.skeleton_fixtures import declaration as fixture_declaration
from tests.test_readback import write_readback
from tests.undecodable_json import UNDECODABLE_JSON


_ARTICLE_ID = "af_0123456789abcdef01234567"
_SOURCE = "roadmap/basics/sources/book.txt"


def _declaration(name: str = "Skel.sup_unique", signature: str | None = None) -> DeclarationSkeleton:
    rendered_signature = signature or f"{name} (a b : Nat) (h : a = b) : b = a"
    return fixture_declaration(
        name,
        module="Skel.Main",
        path="Skel/Main.lean",
        start_line=3,
        end_line=4,
        signature=rendered_signature,
        raw_signature=rendered_signature,
    )


def _report(
    declaration: DeclarationSkeleton | None = None,
    *,
    node_id: str = "basics/result",
    article_path: str = "roadmap/basics/result.md",
    unresolved: tuple[UnresolvedTarget, ...] = (),
) -> SkeletonReport:
    declaration = declaration or _declaration()
    return SkeletonReport(
        blueprint_hash=_BLUEPRINT_HASH,
        targets=((node_id, (declaration.name,)),),
        selection="all",
        selected_nodes=(node_id,),
        nodes=(NodeSkeleton(node_id, article_path, (declaration,), "Source theorem.", f"{_SOURCE}#L2-L2"),),
        unresolved=unresolved,
    )


def _extracted(graph: Graph, report: SkeletonReport | None = None) -> SkeletonReport:
    """``report`` (by default ``_report()``) stamped as extracted from ``graph``, as a real report is."""

    return replace(report or _report(), blueprint_hash=blueprint_hash(graph))


def _blueprint(
    root: Path,
    *,
    approved: str | None = None,
    with_article_id: bool = True,
) -> Path:
    blueprint = root / "blueprint"
    chapter = blueprint / "roadmap" / "basics"
    chapter.mkdir(parents=True)
    (blueprint / "roadmap" / "README.md").write_text("# Roadmap\n", encoding="utf-8")
    (chapter / "README.md").write_text("# Basics\n", encoding="utf-8")
    sources = blueprint / "roadmap" / "basics" / "sources"
    sources.mkdir()
    (sources / "book.txt").write_text("Heading\nSource theorem.\n", encoding="utf-8")
    metadata = [
        "---",
        "declaration: theorem",
        "lean: Skel.sup_unique",
        "origin: cited",
        "statement: formalized",
    ]
    if with_article_id:
        metadata.insert(1, f"article_id: {_ARTICLE_ID}")
    if approved is not None:
        metadata.append(f"review_approved: {approved}")
    metadata.append("---")
    (chapter / "result.md").write_text(
        "\n".join(metadata) + "\n\n# Result\n\nA supremum is **unique**.\n\n## Depends on\n\nNone.\n\n"
        "## Sources\n\n[Book](sources/book.txt#L2-L2)\n",
        encoding="utf-8",
    )
    coverage = blueprint / "coverage"
    coverage.mkdir()
    (coverage / "README.md").write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n| All | OUT | scratch |\n",
        encoding="utf-8",
    )
    return blueprint


def _edit(path: Path, old: str, new: str) -> None:
    """Replace ``old`` with ``new`` in the file at ``path``, which must contain it."""

    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def _approve(blueprint: Path, value: str) -> None:
    article = blueprint / "roadmap" / "basics" / "result.md"
    _edit(article, "statement: formalized\n", f"statement: formalized\nreview_approved: {value}\n")


def _file_card(blueprint: Path, declaration: DeclarationSkeleton, **options: str) -> Path:
    return write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration.name,
        statement_hash=declaration.statement_hash,
        packet_hash=declaration.evidence_hash,
        model="test-model",
        text="Equality is symmetric.",
        packet_text=declaration.blind_text(),
        **options,
    )


def _state(blueprint: Path, report: SkeletonReport | None = None):
    """The review state ``review check`` judges for the article, against ``report``."""

    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, _extracted(graph, report))
    return review_states(graph, bundle, load_readbacks(blueprint))["basics/result"]


def _findings(blueprint: Path, report: SkeletonReport | None = None):
    graph = load_graph(blueprint)
    return review_findings(graph, build_review_bundle(graph, _extracted(graph, report)), load_readbacks(blueprint))


def _codes(findings: object) -> list[str]:
    return [finding.code for finding in findings]  # type: ignore[attr-defined]


def test_bundle_binds_the_complete_evidence(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, _extracted(graph))
    article = bundle.article(_ARTICLE_ID)
    assert article is not None
    assert article.statement == "A supremum is **unique**."
    assert article.passage == "Source theorem."
    assert article.packet == _report().nodes[0].blind_text()
    assert [item.name for item in article.declarations] == ["Skel.sup_unique"]


@pytest.mark.parametrize("edit", ["statement", "title", "source", "packet"])
def test_evidence_hash_binds_statement_title_passage_and_packet(tmp_path: Path, edit: str) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    before = build_review_bundle(graph, _extracted(graph)).articles[0].evidence_hash
    article = blueprint / "roadmap" / "basics" / "result.md"
    report = _report()
    if edit == "statement":
        _edit(article, "A supremum is **unique**.", "A different claim.")
    elif edit == "title":
        _edit(article, "# Result", "# Different theorem")
    elif edit == "source":
        (blueprint / "roadmap" / "basics" / "sources" / "book.txt").write_text("Heading\nChanged.\n", encoding="utf-8")
        report = replace(report, nodes=(replace(report.nodes[0], passage="Changed."),))
    else:
        report = _report(_declaration(signature="Skel.sup_unique (a b : Nat) : a = b"))

    graph = load_graph(blueprint)
    assert build_review_bundle(graph, _extracted(graph, report)).articles[0].evidence_hash != before


@pytest.mark.parametrize(
    ("old", "new", "statement"),
    [
        # Prose above the title is statement text to the audit and the site.
        ("# Result\n\nA supremum is **unique**.\n", "A supremum is **unique**.\n\n# Result\n", "A supremum is **unique**."),
        # A fence that never closes is text on the page and hides no section.
        ("A supremum is **unique**.", "A supremum is **unique**.\n\n```\ncode", "A supremum is **unique**.\n\n```\ncode"),
        # A comment closed on the title line leaves nothing open below it.
        ("# Result\n", "<!-- drafting note\n--> # Result\n", "A supremum is **unique**."),
        # A heading inside a fence, even after a line that only looks like its close, is code.
        (
            "A supremum is **unique**.",
            "A claim.\n\n```text\n## not a section\n``` trailing text\n## still inside the fence\n```\n\nAfter.",
            "A claim.\n\n```text\n## not a section\n``` trailing text\n## still inside the fence\n```\n\nAfter.",
        ),
    ],
)
def test_canonical_statement_is_the_span_the_audit_and_the_box_read(
    tmp_path: Path, old: str, new: str, statement: str
) -> None:
    blueprint = _blueprint(tmp_path)
    _edit(blueprint / "roadmap" / "basics" / "result.md", old, new)

    graph = load_graph(blueprint)
    node = graph.nodes["basics/result"]
    codes = {finding.code for finding in audit_blueprint(blueprint).findings}
    assert canonical_statement(node) == statement
    assert statement_and_notes(publishable_article(node.path.read_text(encoding="utf-8"))[0])[0] == statement
    assert not codes & {"missing-statement-text", "empty-statement-text", "placeholder-statement-text"}


def test_the_statement_box_shows_exactly_the_approved_statement(tmp_path: Path) -> None:
    """A comment in a fenced or indented code block is code on the site, so the
    approval binds it; a comment in prose is left out of both."""

    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap" / "basics" / "result.md"
    _edit(
        article,
        "A supremum is **unique**.",
        "<!-- hidden note -->\nA claim.\n\n```text\n<!-- fenced code -->\n```\n\n    code <!-- indented code -->",
    )

    graph = load_graph(blueprint)
    statement = canonical_statement(graph.nodes["basics/result"])
    files = render._captured_files(render._capture_blueprint(blueprint)[0], blueprint)
    site = render._publication(graph, blueprint, files, destination=blueprint, sources_base=None)
    assert site.statements["basics/result"][0] == statement
    assert "hidden note" not in statement
    assert "<!-- fenced code -->" in statement and "<!-- indented code -->" in statement


def test_fenced_fake_source_section_cannot_replace_review_evidence(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap" / "basics" / "result.md"
    fake = blueprint / "roadmap" / "basics" / "sources" / "fake.txt"
    fake.write_text("Fake source.\n", encoding="utf-8")
    _edit(
        article,
        "A supremum is **unique**.",
        "A supremum is **unique**.\n\n```text\n``` trailing text\n"
        "## Sources\n[Fake](sources/fake.txt#L1-L1)\n```",
    )

    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, _extracted(graph))
    assert graph.nodes["basics/result"].sources == ("sources/book.txt#L2-L2",)
    assert bundle.articles[0].passage == "Source theorem."


def _unresolved(blueprint: Path) -> SkeletonReport:
    return replace(
        _report(),
        nodes=(replace(_report().nodes[0], declarations=(), complete=False),),
        unresolved=(UnresolvedTarget("basics/result", "Skel.sup_unique", "probe failed"),),
    )


def _wrong_article(blueprint: Path) -> SkeletonReport:
    return replace(
        _report(),
        targets=(("basics/other", ("Skel.sup_unique",)),),
        selected_nodes=("basics/other",),
        nodes=(NodeSkeleton("basics/other", "roadmap/basics/other.md", (_declaration(),)),),
    )


def _article_edit(old: str, new: str):
    def edit(blueprint: Path) -> SkeletonReport:
        _edit(blueprint / "roadmap" / "basics" / "result.md", old, new)
        return _report()

    return edit


@pytest.mark.parametrize(
    ("damage", "code"),
    [
        (_unresolved, "review-report-unresolved"),
        (_wrong_article, "review-report-missing"),
        (lambda blueprint: replace(_report(), selection="filtered"), "review-report-scope"),
        (_article_edit(f"article_id: {_ARTICLE_ID}\n", ""), "review-article-id-missing"),
        (_article_edit("declaration: theorem\n", ""), "review-article-shape"),
    ],
    ids=["unresolved", "wrong-article", "filtered", "no-article-id", "not-declaration-sized"],
)
def test_bundle_refuses_incomplete_evidence(tmp_path: Path, damage, code: str) -> None:
    blueprint = _blueprint(tmp_path)
    report = damage(blueprint)
    graph = load_graph(blueprint)

    with pytest.raises(ReviewError) as refused:
        build_review_bundle(graph, _extracted(graph, report))

    assert code in _codes(refused.value.findings)


@pytest.mark.parametrize("edit", ["approval", "source"])
def test_a_report_is_combined_only_with_the_blueprint_it_was_extracted_from(tmp_path: Path, edit: str) -> None:
    """Any edit to an article, even one outside the review evidence such as its
    approval, or to the passage an article cites, makes a report extracted
    before it incoherent with the articles."""

    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    report = _extracted(graph)
    bundle = build_review_bundle(graph, report)
    if edit == "approval":
        _approve(blueprint, "sha256:" + "f" * 64)
    else:
        (blueprint / "roadmap" / "basics" / "sources" / "book.txt").write_text("Heading\nChanged.\n", encoding="utf-8")
    edited = load_graph(blueprint)

    assert blueprint_hash(edited) != report.blueprint_hash
    with pytest.raises(ReviewError, match="the blueprint changed after its review evidence was extracted") as refused:
        build_review_bundle(edited, report)
    assert _codes(refused.value.findings) == ["review-snapshot-changed"]
    if edit == "approval":
        assert build_review_bundle(edited, _extracted(edited)) == bundle


@pytest.mark.parametrize(
    ("blank", "origin"),
    [(False, "cited"), (True, "cited"), (True, "bridged")],
    ids=["no-line-range", "whitespace-only", "whitespace-only-not-cited"],
)
def test_bundle_requires_an_exact_source_passage(tmp_path: Path, blank: bool, origin: str) -> None:
    """A cited article must show its passage, and no article may cite blank lines."""

    blueprint = _blueprint(tmp_path)
    _edit(blueprint / "roadmap" / "basics" / "result.md", "origin: cited", f"origin: {origin}")
    if blank:
        (blueprint / "roadmap" / "basics" / "sources" / "book.txt").write_text("Heading\n   \n", encoding="utf-8")
        node = replace(_report().nodes[0], passage="   ")
    else:
        _edit(blueprint / "roadmap" / "basics" / "result.md", "#L2-L2", "")
        node = replace(_report().nodes[0], passage=None, passage_locator=None)

    graph = load_graph(blueprint)
    with pytest.raises(ReviewError, match="no exact local source passage"):
        build_review_bundle(graph, _extracted(graph, replace(_report(), nodes=(node,))))


def test_bundle_resolves_url_encoded_source_paths(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    source = blueprint / "roadmap" / "basics" / "sources" / "book.txt"
    encoded_source = source.with_name("book notes.txt")
    source.rename(encoded_source)
    article = blueprint / "roadmap" / "basics" / "result.md"
    _edit(article, "book.txt", "book%20notes.txt")
    node = replace(_report().nodes[0], passage_locator="roadmap/basics/sources/book notes.txt#L2-L2")

    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, _extracted(graph, replace(_report(), nodes=(node,))))
    assert bundle.articles[0].passage == "Source theorem."


def test_approval_hash_binds_ordered_strict_testimony(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    card = _file_card(blueprint, _report().nodes[0].declarations[0])
    unapproved = _state(blueprint)
    assert unapproved.status == "unapproved" and unapproved.review_hash is not None
    assert _codes(_findings(blueprint)) == ["review-unapproved"]

    _approve(blueprint, unapproved.review_hash)
    assert _state(blueprint).status == "approved"
    assert _findings(blueprint) == []

    _edit(card, "Equality is symmetric.", "A changed account.")
    assert _state(blueprint).status == "drift"
    assert _codes(_findings(blueprint)) == ["review-drift"]


def test_an_approval_survives_a_proof_edit_but_not_a_statement_edit(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _report().nodes[0].declarations[0]
    _file_card(blueprint, declaration)
    _approve(blueprint, _state(blueprint).review_hash)
    # `sorry` replaced by a proof on an axiom whose type names an external definition.
    proved = replace(
        declaration,
        axioms=("Skel.ax",),
        axiom_semantics=(("Skel.ax", SEMANTIC),),
        assumed=("Vendor.P",),
        assumed_semantics=(("Vendor.P", SEMANTIC),),
        boundary_modules=(("Vendor", "olean", "sha256:" + "1" * 64),),
    )
    assert proved.hash != declaration.hash
    assert _state(blueprint, _report(proved)).status == "approved"
    restated = replace(declaration, semantic='{"root":"restated"}')
    assert _state(blueprint, _report(restated)).status == "unverifiable"


def test_an_approval_survives_a_lean_or_mathlib_bump_that_leaves_the_packet_unchanged(tmp_path: Path) -> None:
    """The card and the approval bind the packet, the bytes the reviewer read,
    not the statement hash, which covers the Lean version and the libraries'
    builds."""

    blueprint = _blueprint(tmp_path)
    declaration = _report().nodes[0].declarations[0]
    _file_card(blueprint, declaration)
    _approve(blueprint, _state(blueprint).review_hash)
    bumped = replace(declaration, lean_version="v9.9.9")

    assert bumped.statement_hash != declaration.statement_hash
    assert bumped.blind_text() == declaration.blind_text()
    assert _state(blueprint, _report(bumped)).status == "approved"


@pytest.mark.parametrize(("macro", "status"), [("R", "drift"), ("Reals", "drift"), ("Q", "approved")])
def test_an_approval_binds_the_project_macros_its_statement_uses(tmp_path: Path, macro: str, status: str) -> None:
    """The site typesets the statement with them: a change to a macro it uses,
    or to one that macro uses, marks the approval stale; another does not."""

    blueprint = _blueprint(tmp_path)
    _edit(blueprint / "roadmap" / "basics" / "result.md", "A supremum is **unique**.", "For $x \\in \\R$, it is unique.")
    definitions = {"R": "\\Reals", "Reals": "\\mathbb{R}", "Q": "\\mathbb{Q}"}
    (blueprint / "tex-macros.json").write_text(json.dumps(definitions), encoding="utf-8")
    _file_card(blueprint, _report().nodes[0].declarations[0])
    _approve(blueprint, _state(blueprint).review_hash)
    assert _state(blueprint).status == "approved"

    definitions[macro] = "\\mathbb{C}"
    (blueprint / "tex-macros.json").write_text(json.dumps(definitions), encoding="utf-8")
    assert _state(blueprint).status == status


def test_the_bundle_binds_the_macros_its_caller_captured(tmp_path: Path) -> None:
    """Render passes the tex-macros.json it captured, so the evidence binds the
    macros the site shows, not the file as it is later."""

    blueprint = _blueprint(tmp_path)
    _edit(blueprint / "roadmap" / "basics" / "result.md", "A supremum is **unique**.", "For $x \\in \\R$, it is unique.")
    (blueprint / "tex-macros.json").write_text('{"R": "\\\\mathbb{R}"}', encoding="utf-8")
    graph = load_graph(blueprint)
    report = _extracted(graph)

    assert build_review_bundle(graph, report).articles[0].macros == (("R", '"\\\\mathbb{R}"'),)
    captured = build_review_bundle(graph, report, macros={"R": "\\mathbb{C}"}).articles[0]
    assert captured.macros == (("R", '"\\\\mathbb{C}"'),)


def test_a_card_filed_again_after_a_lean_bump_keeps_the_approval(tmp_path: Path) -> None:
    """The approval binds the testimony and the packet, not the card's bytes,
    whose statement line a bump changes."""

    blueprint = _blueprint(tmp_path)
    declaration = _report().nodes[0].declarations[0]
    card = _file_card(blueprint, declaration)
    _approve(blueprint, _state(blueprint).review_hash)
    bumped = replace(declaration, lean_version="v9.9.9")
    expected = load_readbacks(blueprint)[(_ARTICLE_ID, declaration.name)].file_hash
    _file_card(blueprint, bumped, expected_card_hash=expected)

    assert f"statement: {bumped.statement_hash}" in card.read_text(encoding="utf-8")
    assert _state(blueprint, _report(bumped)).status == "approved"


def test_a_macro_definition_holding_a_lone_surrogate_is_bound_without_raising(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    _edit(blueprint / "roadmap" / "basics" / "result.md", "A supremum is **unique**.", "For $x \\in \\R$, it is unique.")
    (blueprint / "tex-macros.json").write_text('{"R": "\\ud800"}', encoding="utf-8")
    _file_card(blueprint, _report().nodes[0].declarations[0])

    assert _state(blueprint).status == "unapproved"


def test_a_module_catalog_is_not_under_review(tmp_path: Path) -> None:
    """A catalog's `lean:` names inventory a module rather than state one
    result, so review leaves it out: it needs no article_id, no read-backs and
    no approval, and its names need not all resolve."""

    blueprint = _blueprint(tmp_path)
    (blueprint / "roadmap" / "basics" / "catalog.md").write_text(
        "---\ncatalog: module\nlean: Skel.cat_a, Skel.cat_b\n---\n\n# Catalog\n\nA module.\n\n## Depends on\n\nNone.\n",
        encoding="utf-8",
    )
    result = _report()
    catalog = NodeSkeleton("basics/catalog", "roadmap/basics/catalog.md", (_declaration("Skel.cat_a"),))
    report = replace(
        result,
        targets=(("basics/catalog", ("Skel.cat_a", "Skel.cat_b")), *result.targets),
        selected_nodes=("basics/catalog", *result.selected_nodes),
        nodes=(catalog, *result.nodes),
        unresolved=(UnresolvedTarget("basics/catalog", "Skel.cat_b", "probe failed"),),
    )
    _file_card(blueprint, result.nodes[0].declarations[0])

    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, _extracted(graph, report))
    assert [article.node_id for article in bundle.articles] == ["basics/result"]
    assert _codes(review_findings(graph, bundle, load_readbacks(blueprint))) == ["review-unapproved"]


def test_a_malformed_review_approved_is_drift_and_does_not_stop_loading(tmp_path: Path) -> None:
    """The graph loader never refuses it, so `check`, `search` and the rest keep
    working; `review check` reports it as drift, with the hash to approve."""

    blueprint = _blueprint(tmp_path, approved="sha256:abc")
    _file_card(blueprint, _report().nodes[0].declarations[0])

    assert load_graph(blueprint).nodes["basics/result"].review_approved == "sha256:abc"
    (drift,) = _findings(blueprint)
    assert drift.code == "review-drift" and drift.reason.startswith("review_approved is 'sha256:abc' but")


def test_evidence_hash_binds_the_passage_locator(tmp_path: Path) -> None:
    """The same text cited at other lines is other evidence: the locator is what
    a reviewer follows to the source."""

    blueprint = _blueprint(tmp_path)
    (blueprint / "roadmap" / "basics" / "sources" / "book.txt").write_text(
        "Source theorem.\nSource theorem.\n", encoding="utf-8"
    )
    graph = load_graph(blueprint)
    before = build_review_bundle(graph, _extracted(graph)).articles[0].evidence_hash
    _edit(blueprint / "roadmap" / "basics" / "result.md", "book.txt#L2-L2", "book.txt#L1-L1")
    report = _report()
    report = replace(report, nodes=(replace(report.nodes[0], passage_locator=f"{_SOURCE}#L1-L1"),))

    graph = load_graph(blueprint)
    after = build_review_bundle(graph, _extracted(graph, report)).articles[0]
    assert after.passage == "Source theorem." and after.evidence_hash != before


def test_an_article_with_no_statement_text_is_refused(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    _edit(blueprint / "roadmap" / "basics" / "result.md", "A supremum is **unique**.\n", "")

    graph = load_graph(blueprint)
    with pytest.raises(ReviewError) as refused:
        build_review_bundle(graph, _extracted(graph))
    assert _codes(refused.value.findings) == ["review-statement-missing"]


def test_only_a_current_approval_is_labelled(tmp_path: Path) -> None:
    """A stale approval authorizes nothing, so it carries no label at all."""

    blueprint = _blueprint(tmp_path)
    card = _file_card(blueprint, _report().nodes[0].declarations[0])
    _approve(blueprint, _state(blueprint).review_hash)

    def labels() -> dict[str, str]:
        graph = load_graph(blueprint)
        bundle = build_review_bundle(graph, _extracted(graph))
        return {node_id: item.label for node_id, item in approval_statuses(review_states(graph, bundle, load_readbacks(blueprint))).items()}

    assert labels() == {"basics/result": "self-approved"}
    _edit(card, "Equality is symmetric.", "A changed account.")
    assert labels() == {}
    # Nor does an approval whose cards are not all valid.
    _edit(card, "packet: sha256:", "packet: invalid-")
    assert _state(blueprint).status == "unverifiable" and labels() == {}


def test_approval_is_unavailable_until_every_card_is_strict_and_current(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    missing = _state(blueprint)
    assert (missing.status, missing.review_hash) == ("incomplete", None)
    assert _codes(_findings(blueprint)) == ["readback-missing"]

    _edit(_file_card(blueprint, _report().nodes[0].declarations[0]), "packet: sha256:", "packet: invalid-")
    assert _codes(_findings(blueprint)) == ["readback-invalid"]
    _approve(blueprint, "sha256:" + "a" * 64)
    assert _state(blueprint).status == "unverifiable"
    assert _codes(_findings(blueprint)) == ["readback-invalid", "review-approval-unverifiable"]


def test_an_approval_with_no_review_evidence_is_orphaned(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    (blueprint / "roadmap" / "basics" / "README.md").write_text(
        f"---\nreview_approved: sha256:{'b' * 64}\n---\n# Basics\n", encoding="utf-8"
    )

    orphaned = [finding for finding in _findings(blueprint) if finding.node_id == "basics"]
    assert [(finding.code, finding.reason) for finding in orphaned] == [
        ("review-approval-orphaned", "review_approved is present on an article that is not mapped to Lean")
    ]


def test_a_card_filed_from_a_stale_packet_is_invalid(tmp_path: Path) -> None:
    """`review record` runs no Lean, so it files a card for whatever packet it
    is given; the check, which extracts, refuses one that is not current."""

    blueprint = _blueprint(tmp_path)
    earlier = replace(_declaration(signature="Skel.sup_unique (a b : Nat) : a = b"), semantic='{"root":"earlier"}')
    _file_card(blueprint, earlier)

    (stale,) = _findings(blueprint)
    assert stale.code == "readback-invalid"
    assert stale.reason == "read-back for Skel.sup_unique is invalid: packet hash does not match the prepared declaration"


def test_a_card_whose_model_label_is_a_lone_surrogate_is_invalid_not_a_crash(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, _report().nodes[0].declarations[0])
    path.write_text(path.read_text(encoding="utf-8").replace('model: "test-model"', 'model: "x\\ud800"'), encoding="utf-8")

    (invalid,) = [finding for finding in _findings(blueprint) if finding.code.startswith("readback-")]
    assert invalid.code == "readback-invalid"
    assert "frontmatter field 'model' must be printable" in invalid.reason


def test_cards_the_evidence_does_not_name_are_orphaned(tmp_path: Path) -> None:
    """A renamed declaration leaves its old card behind, and a copy of a card at
    another declaration's path is reported, while the card it copies stays valid."""

    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, _report().nodes[0].declarations[0])
    stray = readback_path(blueprint, _ARTICLE_ID, "Skel." + "α" * 40)
    stray.write_bytes(path.read_bytes())
    assert _codes(_findings(blueprint)) == ["readback-orphaned", "review-unapproved"]

    _edit(blueprint / "roadmap" / "basics" / "result.md", "lean: Skel.sup_unique", "lean: Skel.renamed")
    findings = _findings(blueprint, _report(_declaration("Skel.renamed")))
    assert _codes(findings) == ["readback-missing", "readback-orphaned", "readback-orphaned"]
    orphans = findings[1].reason + findings[2].reason
    assert f"read-back filed for 'Skel.sup_unique' under article_id '{_ARTICLE_ID}'" in orphans


def test_a_long_named_card_that_is_not_utf8_is_one_invalid_card_not_a_missing_card_and_an_orphan(
    tmp_path: Path,
) -> None:
    name = "Skel." + "α" * 40
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap" / "basics" / "result.md"
    _edit(article, "lean: Skel.sup_unique", f"lean: {name}")
    path = _file_card(blueprint, _declaration(name))
    # Too long to spell in a filename, so only the card's frontmatter names it.
    assert path.name.startswith("declaration--")
    path.write_bytes(path.read_bytes() + b"\xff\xfe")

    findings = [item for item in _findings(blueprint, _report(_declaration(name))) if item.code.startswith("readback-")]
    assert [(item.node_id, item.code) for item in findings] == [("basics/result", "readback-invalid")]
    assert findings[0].reason == f"read-back for {name} is invalid: card is not UTF-8 text"
    graph = load_graph(blueprint)
    report = _extracted(graph, _report(_declaration(name)))
    disclosure = render._review_disclosures(blueprint, graph, report, load_readbacks(blueprint), {}, ())[0]["basics/result"]
    assert '<span class="bp-readback-status">invalid · card is not UTF-8 text</span>' in disclosure
    assert "No read-back filed" not in disclosure


def test_approval_rejects_a_hand_constructed_card_with_invented_hashes(tmp_path: Path) -> None:
    graph = load_graph(_blueprint(tmp_path))
    bundle = build_review_bundle(graph, _extracted(graph))
    declaration = bundle.articles[0].declarations[0]
    fake = Readback(
        article_id=_ARTICLE_ID,
        declaration=declaration.name,
        statement_hash=declaration.statement_hash,
        packet_hash=declaration.packet_hash,
        model="invented",
        text=r"$\require{html}\href{javascript:alert(1)}{x}$",
        path=Path("not-a-card.md"),
        shown_hash=declaration.packet_hash,
        shown_text=declaration.packet,
        file_hash="sha256:" + "a" * 64,
    )

    state = review_states(graph, bundle, {(_ARTICLE_ID, declaration.name): fake})["basics/result"]
    assert state.review_hash is None
    assert "whole-file hash does not match" in state.findings[0].reason


def test_bundle_requires_the_graphs_target_set(tmp_path: Path) -> None:
    graph = load_graph(_blueprint(tmp_path))
    report = _extracted(graph)
    with pytest.raises(SkeletonError, match="does not contain exactly its selected articles"):
        replace(report, targets=(), selected_nodes=())
    # A report built around the constructor's checks is still refused.
    forged = replace(report)
    object.__setattr__(forged, "targets", (("basics/result", ("Skel.sup_unique", "Skel.extra")),))

    with pytest.raises(ReviewError) as refused:
        build_review_bundle(graph, forged)

    assert _codes(refused.value.findings) == ["review-report-targets"]


def test_packets_are_named_by_the_hash_of_their_bytes(tmp_path: Path) -> None:
    """A hand-built packet hash names no file, so it cannot steer the write
    outside the packet directory."""

    graph = load_graph(_blueprint(tmp_path))
    bundle = build_review_bundle(graph, _extracted(graph))
    declaration = bundle.articles[0].declarations[0]
    forged = replace(declaration, packet_hash="sha256:../../outside")
    malformed = replace(bundle, articles=(replace(bundle.articles[0], declarations=(forged,)),))

    (written,) = write_review_packets(malformed, tmp_path / "packets")

    assert written == tmp_path / "packets" / "blind" / (declaration.packet_hash.removeprefix("sha256:") + ".lean")
    assert not (tmp_path / "outside.lean").exists()
    assert read_review_packet(tmp_path / "packets", _ARTICLE_ID, declaration.name) == (
        declaration.statement_hash,
        declaration.packet_hash,
        declaration.packet,
    )


@pytest.mark.parametrize("step", ["packet", "publish"])
def test_packet_writer_removes_its_stage_when_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    graph = load_graph(_blueprint(tmp_path))
    bundle = build_review_bundle(graph, _extracted(graph))
    write_bytes = Path.write_bytes

    def interrupted(*args: object) -> None:
        if step == "packet":
            write_bytes(*args)
        raise KeyboardInterrupt

    if step == "packet":
        monkeypatch.setattr(Path, "write_bytes", interrupted)
    else:
        monkeypatch.setattr("autoform_cli.review.replace_outputs", interrupted)

    with pytest.raises(KeyboardInterrupt):
        write_review_packets(bundle, tmp_path / "packets")

    assert list(tmp_path.glob(".packets.autoform-stage-*")) == []
    assert not (tmp_path / "packets").exists()


def test_packet_writers_do_not_overwrite_each_others_output(tmp_path: Path) -> None:
    graph = load_graph(_blueprint(tmp_path))
    bundle = build_review_bundle(graph, _extracted(graph))
    review_packets = tmp_path / "review-packets"
    write_review_packets(bundle, review_packets)

    with pytest.raises(SkeletonError, match="refusing to overwrite"):
        write_packets(_report(), review_packets)

    skeleton_packets = tmp_path / "skeleton-packets"
    write_packets(_report(), skeleton_packets)
    with pytest.raises(ReviewError, match="refusing to overwrite"):
        write_review_packets(bundle, skeleton_packets)


def test_the_packet_writer_refuses_a_manifest_that_is_a_link(tmp_path: Path) -> None:
    """A linked manifest could make any directory look like managed output, and be replaced."""

    graph = load_graph(_blueprint(tmp_path))
    bundle = build_review_bundle(graph, _extracted(graph))
    written = tmp_path / "written"
    write_review_packets(bundle, written)
    packets = tmp_path / "review-packets"
    packets.mkdir()
    (packets / "manifest.json").symlink_to(written / "manifest.json")

    with pytest.raises(ReviewError) as refused:
        write_review_packets(bundle, packets)
    assert _codes(refused.value.findings) == ["review-packets-unsafe"]
    assert (packets / "manifest.json").is_symlink() and sorted(path.name for path in packets.iterdir()) == ["manifest.json"]


def test_article_move_preserves_card_and_approval(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    cards = {_file_card(blueprint, _report().nodes[0].declarations[0])}
    approval = _state(blueprint).review_hash
    _approve(blueprint, approval)

    old_path = blueprint / "roadmap" / "basics" / "result.md"
    moved_dir = blueprint / "roadmap" / "moved"
    moved_dir.mkdir()
    (moved_dir / "README.md").write_text("# Moved\n", encoding="utf-8")
    moved_path = moved_dir / "result.md"
    old_path.rename(moved_path)
    _edit(moved_path, "sources/book.txt#L2-L2", "../basics/sources/book.txt#L2-L2")
    graph = load_graph(blueprint)
    report = _extracted(graph, _report(node_id="moved/result", article_path="roadmap/moved/result.md"))
    readbacks = load_readbacks(blueprint)

    assert {card.path for card in readbacks.values()} == cards
    state = review_states(graph, build_review_bundle(graph, report), readbacks)["moved/result"]
    assert (state.status, state.review_hash) == ("approved", approval)


def test_review_evidence_refuses_a_passage_edited_since_the_extraction(tmp_path: Path) -> None:
    """The passage beside a statement is the one extracted with it; a source
    edited since then is refused, never shown beside the old statement."""

    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    report = _extracted(graph)
    (blueprint / "roadmap" / "basics" / "sources" / "book.txt").write_text(
        "Heading\nChanged source.\n", encoding="utf-8"
    )

    with pytest.raises(ReviewError, match="the blueprint changed after its review evidence was extracted"):
        build_review_bundle(graph, report)
    # A report whose hash names the edited source but whose passage does not is refused too.
    with pytest.raises(ReviewError, match="skeleton source passage does not match"):
        build_review_bundle(graph, _extracted(graph))


def _no_manifest(packets: Path) -> None:
    (packets / "manifest.json").unlink()


def _manifest_text(text: str):
    def damage(packets: Path) -> None:
        (packets / "manifest.json").write_text(text, encoding="utf-8")

    return damage


def _manifest(change):
    def damage(packets: Path) -> None:
        path = packets / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        change(manifest)
        path.write_text(json.dumps(manifest), encoding="utf-8")

    return damage


def _packet(damage):
    def apply(packets: Path) -> None:
        (packet,) = (packets / "blind").iterdir()
        packet.unlink()
        damage(packet)

    return apply


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        (_no_manifest, "cannot read the packet manifest"),
        (_manifest_text('{"schema": '), "cannot read the packet manifest"),
        (lambda packets: _manifest_text(UNDECODABLE_JSON["nested"])(packets), "cannot read the packet manifest"),
        (lambda packets: _manifest_text(UNDECODABLE_JSON["a-number-too-long"])(packets), "cannot read the packet manifest"),
        (_manifest(lambda manifest: manifest.update(schema="autoform-skeleton-packets/v2")), "is not a manifest"),
        (_manifest(lambda manifest: manifest["packets"][0].update(declaration="Skel.other")), "has no single packet"),
        (_manifest(lambda manifest: manifest["packets"].append(manifest["packets"][0])), "has no single packet"),
        (_manifest(lambda manifest: manifest["packets"][0].update(statement_hash="sha256:x")), "malformed hash"),
        (_packet(lambda packet: None), "cannot read the packet "),
        pytest.param(
            _packet(os.mkfifo),
            "not a regular file",
            marks=pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs"),
        ),
    ],
    ids=["missing", "truncated", "nested", "long-number", "skeleton-manifest", "no-entry", "two-entries", "bad-hash",
         "packet-missing", "packet-fifo"],
)
def test_a_packet_is_read_only_as_review_prepare_wrote_it(tmp_path: Path, damage, reason: str) -> None:
    """A FIFO is refused unread, so the record does not wait for a writer."""

    graph = load_graph(_blueprint(tmp_path))
    packets = tmp_path / "packets"
    write_review_packets(build_review_bundle(graph, _extracted(graph)), packets)
    damage(packets)

    with pytest.raises(ReviewError, match=reason):
        read_review_packet(packets, _ARTICLE_ID, "Skel.sup_unique")


@pytest.mark.parametrize(
    ("path", "old"), [("roadmap/basics/result.md", "A supremum is **unique**."), (_SOURCE, "Source theorem.")]
)
def test_render_publishes_the_article_and_source_its_review_was_built_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, old: str
) -> None:
    """A statement or cited source rewritten once render has built the review
    is not published in place of what that review showed, and the revision
    hash describes the bytes published."""

    blueprint = _blueprint(tmp_path)
    report = _extracted(load_graph(blueprint))
    render.render_site(blueprint, tmp_path / "idle", review=lambda: report)
    build = render.build_review_bundle

    def build_then_rewrite(*args: object, **kwargs: object) -> object:
        bundle = build(*args, **kwargs)
        _edit(blueprint / path, old, "PLANTED")
        return bundle

    monkeypatch.setattr(render, "build_review_bundle", build_then_rewrite)
    render.render_site(blueprint, tmp_path / "site", review=lambda: report)

    assert not [file for file in (tmp_path / "site").rglob("*") if file.is_file() and b"PLANTED" in file.read_bytes()]
    revisions = [
        json.loads((tmp_path / site / render.PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["source_revision"]
        for site in ("idle", "site")
    ]
    assert revisions[0] == revisions[1]
