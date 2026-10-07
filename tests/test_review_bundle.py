from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

import autoform_cli.render as render_module
from autoform_cli.audit import audit_blueprint
from autoform_cli.graph import Graph, load_graph
from autoform_cli.readback import Readback, load_readbacks
from autoform_cli.render import (
    PUBLICATION_MANIFEST,
    PublicationError,
    _review_disclosure,
    render_site,
)
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
from tests.skeleton_fixtures import declaration as fixture_declaration
from tests.test_readback import write_readback


_ARTICLE_ID = "af_0123456789abcdef01234567"


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
        nodes=(
            NodeSkeleton(
                node_id=node_id,
                article_path=article_path,
                declarations=(declaration,),
                passage="Source theorem.",
                passage_locator="roadmap/basics/sources/book.txt#L2-L2",
            ),
        ),
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


def test_bundle_round_trip_binds_the_complete_prepared_evidence(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, _extracted(graph))
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
    _edit(
        article,
        "A supremum is **unique**.",
        "A claim.\n\n```text\n## not a section\n``` trailing text\n"
        "## still inside the fence\n```\n\nAfter the fence.",
    )

    graph = load_graph(blueprint)
    node = graph.nodes["basics/result"]
    assert canonical_statement(graph, node).endswith("```\n\nAfter the fence.")
    assert "## still inside the fence" in canonical_statement(graph, node)


def test_canonical_statement_preserves_visible_comments_inside_fences(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap" / "basics" / "result.md"
    _edit(article, "A supremum is **unique**.", "<!-- hidden note -->\nA claim.\n\n```text\n<!-- visible code -->\n```")

    graph = load_graph(blueprint)
    statement = canonical_statement(graph, graph.nodes["basics/result"])
    assert "hidden note" not in statement
    assert "<!-- visible code -->" in statement


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


@pytest.mark.parametrize(
    "report",
    [
        replace(
            _report(),
            nodes=(replace(_report().nodes[0], declarations=(), complete=False),),
            unresolved=(UnresolvedTarget("basics/result", "Skel.sup_unique", "probe failed"),),
        ),
        replace(
            _report(),
            targets=(("basics/other", ("Skel.sup_unique",)),),
            selected_nodes=("basics/other",),
            nodes=(
                NodeSkeleton(
                    node_id="basics/other",
                    article_path="roadmap/basics/other.md",
                    declarations=(_declaration(),),
                ),
            ),
        ),
    ],
)
def test_bundle_refuses_partial_unresolved_or_wrong_mapping(
    tmp_path: Path,
    report: SkeletonReport,
) -> None:
    graph = load_graph(_blueprint(tmp_path))
    with pytest.raises(ReviewError):
        build_review_bundle(graph, _extracted(graph, report))


def test_a_report_is_combined_only_with_the_blueprint_it_was_extracted_from(tmp_path: Path) -> None:
    """Any edit to an article, even one outside the review evidence such as its
    approval, makes a report extracted before it incoherent with the articles."""

    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    report = _extracted(graph)
    bundle = build_review_bundle(graph, report)
    _approve(blueprint, "sha256:" + "f" * 64)
    edited = load_graph(blueprint)

    with pytest.raises(ReviewError, match="the blueprint changed after its review evidence was extracted") as refused:
        build_review_bundle(edited, report)
    assert [finding.code for finding in refused.value.findings] == ["review-snapshot-changed"]
    assert [finding.code for finding in validate_review_bundle(edited, bundle, report)] == ["review-snapshot-changed"]
    assert [finding.code for finding in review_findings(edited, bundle, report, {})] == ["review-snapshot-changed"]
    assert [finding.code for finding in validate_review_article(edited, bundle, report, _ARTICLE_ID)] == [
        "review-snapshot-changed"
    ]
    assert build_review_bundle(edited, _extracted(edited)) == bundle


def test_bundle_requires_a_durable_article_id(tmp_path: Path) -> None:
    graph = load_graph(_blueprint(tmp_path, with_article_id=False))

    with pytest.raises(ReviewError, match=r"autoform migrate"):
        build_review_bundle(graph, _extracted(graph))


def test_bundle_requires_a_rendered_declaration_sized_article(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap" / "basics" / "result.md"
    _edit(article, "declaration: theorem\n", "")

    graph = load_graph(blueprint)
    with pytest.raises(ReviewError) as error:
        build_review_bundle(graph, _extracted(graph))

    assert "review-article-shape" in {finding.code for finding in error.value.findings}


def test_recording_refuses_an_article_that_is_no_longer_declaration_sized(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, _extracted(graph))
    article = blueprint / "roadmap" / "basics" / "result.md"
    _edit(article, "declaration: theorem\n", "")

    edited = load_graph(blueprint)
    report = _extracted(edited)
    assert [finding.code for finding in validate_review_bundle(edited, bundle, report)] == ["review-article-shape"]
    assert [finding.code for finding in validate_review_article(edited, bundle, report, _ARTICLE_ID)] == [
        "review-article-shape"
    ]


def test_bundle_requires_exact_source_passage_for_cited_lean_article(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap" / "basics" / "result.md"
    _edit(article, "#L2-L2", "")
    report = replace(_report(), nodes=(replace(_report().nodes[0], passage=None, passage_locator=None),))

    graph = load_graph(blueprint)
    with pytest.raises(ReviewError, match="no exact local source passage"):
        build_review_bundle(graph, _extracted(graph, report))


def test_bundle_rejects_whitespace_only_cited_passage(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    source = blueprint / "roadmap" / "basics" / "sources" / "book.txt"
    source.write_text("Heading\n   \n", encoding="utf-8")
    report = replace(_report(), nodes=(replace(_report().nodes[0], passage="   "),))

    graph = load_graph(blueprint)
    with pytest.raises(ReviewError, match="no exact local source passage"):
        build_review_bundle(graph, _extracted(graph, report))


def test_bundle_resolves_url_encoded_source_paths(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    source = blueprint / "roadmap" / "basics" / "sources" / "book.txt"
    encoded_source = source.with_name("book notes.txt")
    source.rename(encoded_source)
    article = blueprint / "roadmap" / "basics" / "result.md"
    _edit(article, "book.txt", "book%20notes.txt")
    node = replace(
        _report().nodes[0],
        passage_locator="roadmap/basics/sources/book notes.txt#L2-L2",
    )

    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, _extracted(graph, replace(_report(), nodes=(node,))))
    assert bundle.articles[0].passage == "Source theorem."


def test_validation_detects_article_source_and_lean_packet_drift(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    report = _report()
    bundle = build_review_bundle(graph, _extracted(graph, report))

    article = blueprint / "roadmap" / "basics" / "result.md"
    _edit(article, "A supremum is **unique**.", "A different claim.")
    graph = load_graph(blueprint)
    assert {item.code for item in validate_review_bundle(graph, bundle, _extracted(graph, report))} == {
        "review-bundle-drift"
    }

    _edit(article, "A different claim.", "A supremum is **unique**.")
    source = blueprint / "roadmap" / "basics" / "sources" / "book.txt"
    source.write_text("Heading\nChanged source.\n", encoding="utf-8")
    graph = load_graph(blueprint)
    codes = {item.code for item in validate_review_bundle(graph, bundle, _extracted(graph, report))}
    assert "review-source-drift" in codes

    source.write_text("Heading\nSource theorem.\n", encoding="utf-8")
    graph = load_graph(blueprint)
    changed = _extracted(graph, _report(_declaration(signature="Skel.sup_unique (a b : Nat) : a = b")))
    assert {item.code for item in validate_review_bundle(graph, bundle, changed)} == {"review-bundle-drift"}


def test_validation_binds_the_visible_article_title(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, _extracted(graph))
    article = blueprint / "roadmap" / "basics" / "result.md"
    _edit(article, "# Result", "# Different theorem")

    graph = load_graph(blueprint)
    assert [item.code for item in validate_review_bundle(graph, bundle, _extracted(graph))] == [
        "review-bundle-drift"
    ]


def test_approval_hash_binds_ordered_strict_testimony(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    report = _extracted(graph)
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
    report = _extracted(graph, report)

    assert review_findings(graph, bundle, report, cards) == []
    assert audit_blueprint(blueprint, skeleton=report, review_bundle=bundle).clean

    card_path = next(iter(cards.values())).path
    _edit(card_path, "Equality is symmetric.", "A changed account.")
    findings = review_findings(graph, bundle, report)
    assert [item.code for item in findings] == ["review-drift"]


def test_approval_is_unavailable_until_every_card_is_strict_and_current(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    report = _extracted(graph)
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
    _edit(card.path, "packet: sha256:", "packet: invalid-")
    assert [item.code for item in review_findings(graph, bundle, report)] == ["readback-invalid"]


def test_a_long_named_card_that_is_not_utf8_is_one_invalid_card_not_a_missing_card_and_an_orphan(
    tmp_path: Path,
) -> None:
    name = "Skel." + "α" * 40
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap" / "basics" / "result.md"
    _edit(article, "lean: Skel.sup_unique", f"lean: {name}")
    graph = load_graph(blueprint)
    report = _extracted(graph, _report(_declaration(name)))
    bundle = build_review_bundle(graph, report)
    declaration = report.nodes[0].declarations[0]
    path = write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="test-model",
        text="Equality is symmetric.",
        packet_text=declaration.blind_text(),
    )
    # Too long to spell in a filename, so only the card's frontmatter names it.
    assert path.name.startswith("declaration--")
    path.write_bytes(path.read_bytes() + b"\xff\xfe")

    findings = [item for item in review_findings(graph, bundle, report) if item.code.startswith("readback-")]
    assert [(item.node_id, item.code) for item in findings] == [("basics/result", "readback-invalid")]
    assert findings[0].reason == f"read-back for {name} is invalid: card is not UTF-8 text"
    disclosure = _review_disclosure(graph.nodes["basics/result"], report, bundle, load_readbacks(blueprint), {})
    assert "bp-readback-invalid" in disclosure
    assert '<span class="bp-readback-status">invalid · card is not UTF-8 text</span>' in disclosure
    assert "No read-back filed" not in disclosure


def test_approval_rejects_a_hand_constructed_card_with_invented_hashes(tmp_path: Path) -> None:
    graph = load_graph(_blueprint(tmp_path))
    bundle = build_review_bundle(graph, _extracted(graph))
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


def _codes(findings: object) -> list[str]:
    return [finding.code for finding in findings]  # type: ignore[attr-defined]


def test_bundle_requires_a_report_of_every_article(tmp_path: Path) -> None:
    graph = load_graph(_blueprint(tmp_path))
    filtered = replace(_extracted(graph), selection="filtered")

    with pytest.raises(ReviewError) as refused:
        build_review_bundle(graph, filtered)

    assert _codes(refused.value.findings) == ["review-report-scope"]


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


def test_article_validation_requires_a_report_scoped_to_that_article(tmp_path: Path) -> None:
    graph = load_graph(_blueprint(tmp_path))
    report = _extracted(graph)
    bundle = build_review_bundle(graph, report)
    assert validate_review_article(graph, bundle, replace(report, selection="filtered"), _ARTICLE_ID) == ()

    assert _codes(validate_review_article(graph, bundle, report, _ARTICLE_ID)) == ["review-report-scope"]


def test_article_validation_requires_the_articles_targets(tmp_path: Path) -> None:
    graph = load_graph(_blueprint(tmp_path))
    report = _extracted(graph)
    bundle = build_review_bundle(graph, report)
    forged = replace(report, selection="filtered")
    object.__setattr__(forged, "targets", (("basics/result", ("Skel.sup_unique", "Skel.extra")),))

    assert _codes(validate_review_article(graph, bundle, forged, _ARTICLE_ID)) == ["review-report-targets"]


def test_packet_writer_revalidates_hand_constructed_bundle(tmp_path: Path) -> None:
    graph = load_graph(_blueprint(tmp_path))
    bundle = build_review_bundle(graph, _extracted(graph))
    article = bundle.articles[0]
    declaration = replace(article.declarations[0], packet_hash="sha256:../../outside")
    malformed = replace(bundle, articles=(replace(article, declarations=(declaration,)),))

    with pytest.raises(ReviewError, match="malformed packet hash"):
        write_review_packets(malformed, tmp_path / "packets")
    assert not (tmp_path / "outside.lean").exists()


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
        monkeypatch.setattr("autoform_cli.skeleton._replace_outputs", interrupted)

    with pytest.raises(KeyboardInterrupt):
        write_review_packets(bundle, tmp_path / "packets")

    assert list(tmp_path.glob(".packets.autoform-stage-*")) == []
    assert not (tmp_path / "packets").exists()


def test_packet_writer_removes_a_stage_interrupted_while_it_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = load_graph(_blueprint(tmp_path))
    bundle = build_review_bundle(graph, _extracted(graph))
    packets = tmp_path / "packets"
    write_review_packets(bundle, packets)
    chmod = Path.chmod

    def interrupted(self: Path, *args: object, **kwargs: object) -> None:
        if ".autoform-stage-" in self.name:
            raise KeyboardInterrupt
        chmod(self, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", interrupted)

    with pytest.raises(KeyboardInterrupt):
        write_review_packets(bundle, packets)

    assert list(tmp_path.glob(".packets.autoform-stage-*")) == []
    assert (packets / "manifest.json").is_file()


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


def test_audit_refuses_to_trust_approval_without_bundle_and_fresh_skeleton(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, approved="sha256:" + "a" * 64)
    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, _extracted(graph))

    assert [item.code for item in audit_blueprint(blueprint).findings] == ["review-bundle-missing"]
    assert [item.code for item in audit_blueprint(blueprint, review_bundle=bundle).findings] == [
        "review-bundle-unverified"
    ]


def test_article_move_preserves_bundle_card_and_approval(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    original_graph = load_graph(blueprint)
    original_report = _extracted(original_graph)
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
    _edit(moved_path, "sources/book.txt#L2-L2", "../basics/sources/book.txt#L2-L2")
    current_graph = load_graph(blueprint)
    current_report = _extracted(
        current_graph,
        _report(
            node_id="moved/result",
            article_path="roadmap/moved/result.md",
        ),
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
    other = NodeSkeleton(
        node_id="basics/other",
        article_path="roadmap/basics/other.md",
        declarations=(other_declaration,),
    )
    full_report = SkeletonReport(
        blueprint_hash=_BLUEPRINT_HASH,
        targets=(
            (other.node_id, (other_declaration.name,)),
            (target.node_id, (target.declarations[0].name,)),
        ),
        selection="all",
        selected_nodes=(other.node_id, target.node_id),
        nodes=(other, target),
        unresolved=(),
    )
    graph = load_graph(blueprint)
    bundle = build_review_bundle(graph, _extracted(graph, full_report))
    scoped = replace(
        full_report,
        selection="filtered",
        selected_nodes=(target.node_id,),
        nodes=(target,),
    )

    _edit(other_path, "An unrelated claim.", "Changed elsewhere.")
    # A record extracts the article it files, and the report still names the
    # whole blueprint as it is then, other article included.
    graph = load_graph(blueprint)
    assert validate_review_article(graph, bundle, _extracted(graph, scoped), _ARTICLE_ID) == ()

    target_path = blueprint / "roadmap" / "basics" / "result.md"
    _edit(target_path, "A supremum is **unique**.", "The selected claim changed.")
    graph = load_graph(blueprint)
    assert [
        finding.code
        for finding in validate_review_article(graph, bundle, _extracted(graph, scoped), _ARTICLE_ID)
    ] == ["review-bundle-drift"]


def test_blueprint_hash_binds_the_bytes_of_each_cited_source(tmp_path: Path) -> None:
    """A report's passage is cut from its cited source, so a report extracted
    before that source changed describes a blueprint that no longer exists."""

    blueprint = _blueprint(tmp_path)
    report = _extracted(load_graph(blueprint))
    (blueprint / "roadmap" / "basics" / "sources" / "book.txt").write_text(
        "Heading\nChanged source.\n", encoding="utf-8"
    )
    graph = load_graph(blueprint)

    assert blueprint_hash(graph) != report.blueprint_hash
    with pytest.raises(ReviewError, match="the blueprint changed after its review evidence was extracted"):
        build_review_bundle(graph, report)


def test_review_evidence_cuts_the_passage_from_the_source_its_graph_captured(tmp_path: Path) -> None:
    """The passage beside a statement comes from the bytes the graph loaded,
    not from whatever the file holds when the evidence is built."""

    blueprint = _blueprint(tmp_path)
    graph = load_graph(blueprint)
    (blueprint / "roadmap" / "basics" / "sources" / "book.txt").write_text(
        "Heading\nChanged source.\n", encoding="utf-8"
    )

    bundle = build_review_bundle(graph, _extracted(graph))
    assert bundle.articles[0].passage == "Source theorem."
    assert validate_review_bundle(graph, bundle, _extracted(graph)) == ()


def _ordered_blueprint(root: Path) -> Path:
    """The review blueprint, with an Intro chapter its roadmap lists first."""

    blueprint = _blueprint(root)
    (blueprint / "roadmap" / "intro").mkdir()
    (blueprint / "roadmap" / "intro" / "README.md").write_text("# Intro\n\nNarrative.\n", encoding="utf-8")
    (blueprint / "roadmap" / "README.md").write_text(
        "# Roadmap\n\n- [Intro](intro/README.md)\n- [Basics](basics/README.md)\n", encoding="utf-8"
    )
    (blueprint / "README.md").write_text("# Landing\n\n[Roadmap](roadmap/README.md)\n", encoding="utf-8")
    return blueprint


def _manifest(site: Path) -> dict[str, object]:
    return json.loads((site / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))


def test_render_publishes_the_cited_source_it_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cited source rewritten after the review is validated is not copied in
    place of the passage the review showed, and the revision hash describes the
    bytes published."""

    blueprint = _ordered_blueprint(tmp_path)
    graph = load_graph(blueprint)
    report = _extracted(graph)
    bundle = build_review_bundle(graph, report)
    idle = tmp_path / "idle"
    render_site(blueprint, idle, skeleton=report, review_bundle=bundle)

    source = blueprint / "roadmap" / "basics" / "sources" / "book.txt"
    validate = validate_review_bundle

    def validate_then_rewrite(*args: object, **kwargs: object) -> object:
        findings = validate(*args, **kwargs)
        source.write_text("Heading\nPLANTED: every supremum is two.\n", encoding="utf-8")
        return findings

    monkeypatch.setattr(render_module, "validate_review_bundle", validate_then_rewrite)
    site = tmp_path / "site"
    render_site(blueprint, site, skeleton=report, review_bundle=bundle)

    assert (site / "roadmap" / "basics" / "sources" / "book.txt").read_text(encoding="utf-8") == (
        "Heading\nSource theorem.\n"
    )
    assert not [path for path in site.rglob("*") if path.is_file() and b"PLANTED" in path.read_bytes()]
    assert _manifest(site)["source_revision"] == _manifest(idle)["source_revision"]


def test_render_orders_the_book_by_the_roadmap_it_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The book's page order follows the roadmap page the site publishes, even
    when the file is rewritten after it was copied."""

    blueprint = _ordered_blueprint(tmp_path)
    graph = load_graph(blueprint)
    report = _extracted(graph)
    bundle = build_review_bundle(graph, report)
    readme = blueprint / "roadmap" / "README.md"
    order = render_module._book_page_order

    def rewrite_then_order(*args: object, **kwargs: object) -> object:
        readme.write_text("# Roadmap\n\n- [Basics](basics/README.md)\n- [Intro](intro/README.md)\n", encoding="utf-8")
        return order(*args, **kwargs)

    monkeypatch.setattr(render_module, "_book_page_order", rewrite_then_order)
    site = tmp_path / "site"
    render_site(blueprint, site, skeleton=report, review_bundle=bundle)

    published = (site / "roadmap" / "README.md").read_text(encoding="utf-8")
    assert published.index("[Intro]") < published.index("[Basics]")
    summary = (site / "SUMMARY.md").read_text(encoding="utf-8")
    assert summary.index("intro/README.md") < summary.index("basics/README.md")


def test_render_refuses_a_roadmap_page_that_appeared_after_the_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page the graph never parsed is not published beside the graph."""

    blueprint = _blueprint(tmp_path)
    load = load_graph

    def load_then_add(*args: object, **kwargs: object) -> Graph:
        graph = load(*args, **kwargs)
        (blueprint / "roadmap" / "basics" / "late.md").write_text("# Late\n\nUnparsed.\n", encoding="utf-8")
        return graph

    monkeypatch.setattr(render_module, "load_graph", load_then_add)
    site = tmp_path / "site"
    with pytest.raises(PublicationError, match="roadmap page appeared after the blueprint was loaded"):
        render_site(blueprint, site)
    assert not site.exists()


def test_render_publishes_the_coverage_contract_it_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blueprint = _blueprint(tmp_path)
    contract = blueprint / "coverage" / "README.md"
    validated = contract.read_bytes()
    load = render_module.load_coverage

    def load_then_rewrite(*args: object, **kwargs: object) -> object:
        loaded = load(*args, **kwargs)
        contract.write_text("# Coverage\n\nPLANTED: nothing is covered.\n", encoding="utf-8")
        return loaded

    monkeypatch.setattr(render_module, "load_coverage", load_then_rewrite)
    site = tmp_path / "site"
    render_site(blueprint, site)

    published = (site / "coverage" / "README.md").read_text(encoding="utf-8")
    assert "PLANTED" not in published
    assert "| All | OUT | scratch |" in published
    assert _manifest(site)["coverage"]["source_sha256"] == hashlib.sha256(validated).hexdigest()
