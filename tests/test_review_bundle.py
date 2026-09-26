from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli.audit import audit_blueprint
from autoform_cli.graph import load_graph
from autoform_cli.readback import Readback, load_readbacks, write_readback
from autoform_cli.review import (
    ReviewError,
    build_review_bundle,
    canonical_statement,
    load_review_bundle,
    review_findings,
    validate_review_article,
    validate_review_bundle,
    write_review_bundle,
    write_review_packets,
)
from autoform_cli.skeleton import DeclarationSkeleton, NodeSkeleton, SkeletonReport


_SEMANTIC = '{"type":{"sort":{"zero":null}}}'
_ARTICLE_ID = "af_0123456789abcdef01234567"


def _declaration(name: str = "Skel.sup_unique", signature: str | None = None) -> DeclarationSkeleton:
    return DeclarationSkeleton(
        name=name,
        kind="theorem",
        module="Skel.Main",
        path="Skel/Main.lean",
        start_line=3,
        end_line=4,
        signature=signature or f"{name} (a b : Nat) (h : a = b) : b = a",
        semantic=_SEMANTIC,
        lean_version="4.32.2",
        depends=(),
        trusted=(),
        assumed=(),
        assumed_semantics=(),
        boundary_modules=(),
        axioms=(),
        axiom_semantics=(),
    )


def _report(
    declaration: DeclarationSkeleton | None = None,
    *,
    node_id: str = "basics/result",
    article_path: str = "roadmap/basics/result.md",
    unresolved: tuple[str, ...] = (),
) -> SkeletonReport:
    return SkeletonReport(
        nodes=(
            NodeSkeleton(
                node_id=node_id,
                article_path=article_path,
                declarations=(declaration or _declaration(),),
                passage="Source theorem.",
                passage_locator="roadmap/basics/sources/book.txt#L2-L2",
            ),
        ),
        unresolved=unresolved,
    )


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


def _approve(blueprint: Path, value: str) -> None:
    article = blueprint / "roadmap" / "basics" / "result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "statement: formalized\n",
            f"statement: formalized\nreview_approved: {value}\n",
        ),
        encoding="utf-8",
    )


def test_bundle_round_trip_binds_the_complete_prepared_evidence(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, _report())
    article = bundle.article(_ARTICLE_ID)
    assert article is not None
    assert article.statement == "A supremum is **unique**."
    assert article.passage == "Source theorem."
    assert article.packet == _report().nodes[0].blind_text()
    assert [item.name for item in article.declarations] == ["Skel.sup_unique"]

    path = write_review_bundle(bundle, tmp_path / "review.json")
    assert load_review_bundle(path) == bundle

    data = json.loads(path.read_text(encoding="utf-8"))
    data["articles"][0]["statement"] = "A different claim."
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ReviewError, match="evidence hash does not match"):
        load_review_bundle(path)


def test_canonical_statement_keeps_headings_inside_a_fenced_block(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap" / "basics" / "result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "A supremum is **unique**.",
            "A claim.\n\n```text\n## not a section\n``` trailing text\n"
            "## still inside the fence\n```\n\nAfter the fence.",
        ),
        encoding="utf-8",
    )

    node = load_graph(blueprint).nodes["basics/result"]
    assert canonical_statement(node).endswith("```\n\nAfter the fence.")
    assert "## still inside the fence" in canonical_statement(node)


def test_canonical_statement_preserves_visible_comments_inside_fences(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap" / "basics" / "result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "A supremum is **unique**.",
            "<!-- hidden note -->\nA claim.\n\n```text\n<!-- visible code -->\n```",
        ),
        encoding="utf-8",
    )

    statement = canonical_statement(load_graph(blueprint).nodes["basics/result"])
    assert "hidden note" not in statement
    assert "<!-- visible code -->" in statement


def test_fenced_fake_source_section_cannot_replace_review_evidence(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap" / "basics" / "result.md"
    fake = blueprint / "roadmap" / "basics" / "sources" / "fake.txt"
    fake.write_text("Fake source.\n", encoding="utf-8")
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "A supremum is **unique**.",
            "A supremum is **unique**.\n\n```text\n``` trailing text\n"
            "## Sources\n[Fake](sources/fake.txt#L1-L1)\n```",
        ),
        encoding="utf-8",
    )

    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, _report())
    assert graph.nodes["basics/result"].sources == ("sources/book.txt#L2-L2",)
    assert bundle.articles[0].passage == "Source theorem."


@pytest.mark.parametrize(
    "report",
    [
        SkeletonReport(nodes=(), unresolved=()),
        _report(unresolved=("basics/result: probe failed",)),
        SkeletonReport(
            nodes=(
                NodeSkeleton(
                    node_id="basics/other",
                    article_path="roadmap/basics/other.md",
                    declarations=(_declaration(),),
                ),
            ),
            unresolved=(),
        ),
    ],
)
def test_bundle_refuses_partial_unresolved_or_wrong_mapping(
    tmp_path: Path,
    report: SkeletonReport,
) -> None:
    graph = load_graph(_blueprint(tmp_path))
    with pytest.raises(ReviewError):
        build_review_bundle(graph, report)


def test_bundle_requires_a_durable_article_id(tmp_path: Path) -> None:
    graph = load_graph(_blueprint(tmp_path, with_article_id=False))

    with pytest.raises(ReviewError, match=r"autoform migrate"):
        build_review_bundle(graph, _report())


def test_bundle_requires_exact_source_passage_for_cited_lean_article(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap" / "basics" / "result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace("#L2-L2", ""),
        encoding="utf-8",
    )
    report = replace(_report(), nodes=(replace(_report().nodes[0], passage=None, passage_locator=None),))

    with pytest.raises(ReviewError, match="no exact local source passage"):
        build_review_bundle(load_graph(blueprint), report)


def test_bundle_rejects_whitespace_only_cited_passage(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    source = blueprint / "roadmap" / "basics" / "sources" / "book.txt"
    source.write_text("Heading\n   \n", encoding="utf-8")
    report = replace(_report(), nodes=(replace(_report().nodes[0], passage="   "),))

    with pytest.raises(ReviewError, match="no exact local source passage"):
        build_review_bundle(load_graph(blueprint), report)


def test_bundle_resolves_url_encoded_source_paths(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    source = blueprint / "roadmap" / "basics" / "sources" / "book.txt"
    encoded_source = source.with_name("book notes.txt")
    source.rename(encoded_source)
    article = blueprint / "roadmap" / "basics" / "result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace("book.txt", "book%20notes.txt"),
        encoding="utf-8",
    )
    node = replace(
        _report().nodes[0],
        passage_locator="roadmap/basics/sources/book notes.txt#L2-L2",
    )

    bundle = build_review_bundle(load_graph(blueprint), replace(_report(), nodes=(node,)))
    assert bundle.articles[0].passage == "Source theorem."


def test_validation_detects_article_source_and_lean_packet_drift(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    report = _report()
    bundle = build_review_bundle(graph, report)

    article = blueprint / "roadmap" / "basics" / "result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace("A supremum is **unique**.", "A different claim."),
        encoding="utf-8",
    )
    assert {item.code for item in validate_review_bundle(load_graph(blueprint), bundle, report)} == {
        "review-bundle-drift"
    }

    article.write_text(
        article.read_text(encoding="utf-8").replace("A different claim.", "A supremum is **unique**."),
        encoding="utf-8",
    )
    source = blueprint / "roadmap" / "basics" / "sources" / "book.txt"
    source.write_text("Heading\nChanged source.\n", encoding="utf-8")
    codes = {item.code for item in validate_review_bundle(load_graph(blueprint), bundle, report)}
    assert "review-source-drift" in codes

    source.write_text("Heading\nSource theorem.\n", encoding="utf-8")
    changed = _report(_declaration(signature="Skel.sup_unique (a b : Nat) : a = b"))
    assert {item.code for item in validate_review_bundle(load_graph(blueprint), bundle, changed)} == {
        "review-bundle-drift"
    }


def test_validation_binds_the_visible_article_title(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    report = _report()
    bundle = build_review_bundle(load_graph(blueprint), report)
    article = blueprint / "roadmap" / "basics" / "result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace("# Result", "# Different theorem"),
        encoding="utf-8",
    )

    assert [item.code for item in validate_review_bundle(load_graph(blueprint), bundle, report)] == [
        "review-bundle-drift"
    ]


def test_approval_hash_binds_ordered_strict_testimony(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    report = _report()
    bundle = build_review_bundle(graph, report)
    declaration = report.nodes[0].declarations[0]
    write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="test-model",
        text="Equality is symmetric.",
        packet_text=declaration.blind_text(),
    )
    cards = load_readbacks(blueprint)
    approval = bundle.review_hash(_ARTICLE_ID, cards)
    _approve(blueprint, approval)
    graph = load_graph(blueprint)

    assert review_findings(graph, bundle, report, cards) == []
    assert audit_blueprint(blueprint, skeleton=report, review_bundle=bundle).clean

    card_path = next(iter(cards.values())).path
    card_path.write_text(
        card_path.read_text(encoding="utf-8").replace("Equality is symmetric.", "A changed account."),
        encoding="utf-8",
    )
    findings = review_findings(graph, bundle, report)
    assert [item.code for item in findings] == ["review-drift"]


def test_approval_is_unavailable_until_every_card_is_strict_and_current(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    report = _report()
    bundle = build_review_bundle(graph, report)

    with pytest.raises(ReviewError, match="no read-back filed"):
        bundle.review_hash(_ARTICLE_ID, {})
    assert [item.code for item in review_findings(graph, bundle, report, {})] == ["readback-missing"]

    write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=report.nodes[0].declarations[0],
        model="test-model",
        text="Equality is symmetric.",
        packet_text=report.nodes[0].declarations[0].blind_text(),
    )
    card = next(iter(load_readbacks(blueprint).values()))
    card.path.write_text(
        card.path.read_text(encoding="utf-8").replace("packet: sha256:", "packet: invalid-"),
        encoding="utf-8",
    )
    assert [item.code for item in review_findings(graph, bundle, report)] == ["readback-invalid"]


def test_approval_rejects_a_hand_constructed_card_with_invented_hashes(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    bundle = build_review_bundle(load_graph(blueprint), _report())
    declaration = bundle.articles[0].declarations[0]
    fake = Readback(
        article_id=_ARTICLE_ID,
        declaration=declaration.name,
        skeleton_hash=declaration.skeleton_hash,
        packet_hash=declaration.packet_hash,
        model="invented",
        text=r"$\require{html}\href{javascript:alert(1)}{x}$",
        path=Path("not-a-card.md"),
        shown_hash=declaration.packet_hash,
        shown_text=declaration.packet,
        file_hash="sha256:" + "a" * 64,
    )

    with pytest.raises(ReviewError, match="active TeX|whole-file hash"):
        bundle.review_hash(_ARTICLE_ID, {(_ARTICLE_ID, declaration.name): fake})


def test_packet_writer_revalidates_hand_constructed_bundle(tmp_path: Path) -> None:
    bundle = build_review_bundle(load_graph(_blueprint(tmp_path)), _report())
    article = bundle.articles[0]
    declaration = replace(article.declarations[0], packet_hash="sha256:../../outside")
    malformed = replace(bundle, articles=(replace(article, declarations=(declaration,)),))

    with pytest.raises(ReviewError, match="malformed packet hash"):
        write_review_packets(malformed, tmp_path / "packets")
    assert not (tmp_path / "outside.lean").exists()


def test_audit_refuses_to_trust_approval_without_bundle_and_fresh_skeleton(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, approved="sha256:" + "a" * 64)
    bundle = build_review_bundle(load_graph(blueprint), _report())

    assert [item.code for item in audit_blueprint(blueprint).findings] == ["review-bundle-missing"]
    assert [item.code for item in audit_blueprint(blueprint, review_bundle=bundle).findings] == [
        "review-bundle-unverified"
    ]


def test_article_move_preserves_bundle_card_and_approval(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    original_graph = load_graph(blueprint)
    original_report = _report()
    bundle = build_review_bundle(original_graph, original_report)
    declaration = original_report.nodes[0].declarations[0]
    write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="test-model",
        text="Equality is symmetric.",
        packet_text=declaration.blind_text(),
    )
    cards = load_readbacks(blueprint)
    approval = bundle.review_hash(_ARTICLE_ID, cards)
    _approve(blueprint, approval)

    old_path = blueprint / "roadmap" / "basics" / "result.md"
    moved_dir = blueprint / "roadmap" / "moved"
    moved_dir.mkdir()
    (moved_dir / "README.md").write_text("# Moved\n", encoding="utf-8")
    moved_path = moved_dir / "result.md"
    old_path.rename(moved_path)
    moved_path.write_text(
        moved_path.read_text(encoding="utf-8").replace(
            "sources/book.txt#L2-L2",
            "../basics/sources/book.txt#L2-L2",
        ),
        encoding="utf-8",
    )
    current_graph = load_graph(blueprint)
    current_report = _report(
        node_id="moved/result",
        article_path="roadmap/moved/result.md",
    )

    assert validate_review_bundle(current_graph, bundle, current_report) == ()
    assert load_readbacks(blueprint) == cards
    assert bundle.review_hash(_ARTICLE_ID, cards) == approval
    assert review_findings(current_graph, bundle, current_report, cards) == []


def test_scoped_validation_ignores_unrelated_drift_but_rejects_target_drift(
    tmp_path: Path,
) -> None:
    blueprint = _blueprint(tmp_path)
    other_id = "af_89abcdef0123456789abcdef"
    other_path = blueprint / "roadmap" / "basics" / "other.md"
    other_path.write_text(
        "---\n"
        f"article_id: {other_id}\n"
        "declaration: theorem\n"
        "lean: Skel.other\n"
        "statement: formalized\n"
        "---\n\n"
        "# Other\n\nAn unrelated claim.\n\n## Depends on\n\nNone.\n",
        encoding="utf-8",
    )
    target = _report().nodes[0]
    other_declaration = _declaration("Skel.other")
    full_report = SkeletonReport(
        nodes=(
            target,
            NodeSkeleton(
                node_id="basics/other",
                article_path="roadmap/basics/other.md",
                declarations=(other_declaration,),
            ),
        ),
        unresolved=(),
    )
    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, full_report)
    scoped = SkeletonReport(nodes=(target,), unresolved=())

    other_path.write_text(
        other_path.read_text(encoding="utf-8").replace("An unrelated claim.", "Changed elsewhere."),
        encoding="utf-8",
    )
    assert validate_review_article(load_graph(blueprint), bundle, scoped, _ARTICLE_ID) == ()

    target_path = blueprint / "roadmap" / "basics" / "result.md"
    target_path.write_text(
        target_path.read_text(encoding="utf-8").replace(
            "A supremum is **unique**.",
            "The selected claim changed.",
        ),
        encoding="utf-8",
    )
    assert [
        finding.code
        for finding in validate_review_article(load_graph(blueprint), bundle, scoped, _ARTICLE_ID)
    ] == ["review-bundle-drift"]
