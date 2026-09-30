from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli.__main__ import _current_review, main
from autoform_cli.graph import Graph, load_graph
from autoform_cli.readback import load_readbacks
from autoform_cli.render import PublicationError, render_site
from autoform_cli.review import REVIEW_PACKET_SCHEMA, load_review_bundle
from autoform_cli.skeleton import (
    DeclarationSkeleton,
    NodeSkeleton,
    SkeletonError,
    SkeletonReport,
    blueprint_hash,
    extract_skeletons,
)

_BLUEPRINT_HASH = "sha256:" + "0" * 64


def _blueprint(root: Path) -> Path:
    blueprint = root / "blueprint"
    chapter = blueprint / "roadmap" / "basics"
    chapter.mkdir(parents=True)
    (blueprint / "README.md").write_text("# Blueprint\n", encoding="utf-8")
    (blueprint / "roadmap" / "README.md").write_text(
        "# Roadmap\n\n- [Basics](basics/README.md)\n",
        encoding="utf-8",
    )
    (chapter / "README.md").write_text(
        "# Basics\n\n- [Result](result.md)\n",
        encoding="utf-8",
    )
    (chapter / "result.md").write_text(
        "---\n"
        "article_id: af_0123456789abcdef01234567\n"
        "declaration: theorem\n"
        "lean: Review.result\n"
        "statement: formalized\n"
        "---\n\n"
        "# Result\n\nEvery object is equal to itself.\n\n"
        "## Depends on\n\nNone.\n",
        encoding="utf-8",
    )
    coverage = blueprint / "coverage" / "README.md"
    coverage.parent.mkdir()
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Scope | OUT | Test fixture |\n",
        encoding="utf-8",
    )
    return blueprint


def _skeleton() -> SkeletonReport:
    declaration = DeclarationSkeleton(
        name="Review.result",
        kind="theorem",
        module="Review",
        path="Review.lean",
        start_line=1,
        end_line=1,
        signature="Review.result : True",
        raw_signature="Review.result : True",
        semantic='{"generated":[],"root":{"safety":"safe","type":{"sort":{"zero":null}}}}',
        lean_version="4.32.2",
        depends=(),
        trusted=(),
        assumed=(),
        assumed_semantics=(),
        boundary_modules=(),
        axioms=(),
        axiom_semantics=(),
    )
    return SkeletonReport(
        blueprint_hash=_BLUEPRINT_HASH,
        targets=(("basics/result", (declaration.name,)),),
        selection="all",
        selected_nodes=("basics/result",),
        nodes=(
            NodeSkeleton(
                node_id="basics/result",
                article_path="roadmap/basics/result.md",
                declarations=(declaration,),
            ),
        ),
        unresolved=(),
    )


def _as_extracted(report: SkeletonReport, blueprint_dir: object) -> SkeletonReport:
    """Stamp ``report`` with the blueprint as it is now, as a real extraction does."""

    return replace(report, blueprint_hash=blueprint_hash(load_graph(blueprint_dir)))


def test_review_cli_prepares_records_and_checks_exact_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blueprint = _blueprint(tmp_path)
    skeleton = _skeleton()
    extraction_scopes: list[object] = []
    probe_timeouts: list[float] = []

    def run_probe(probe: str, root: Path, *, timeout: float) -> str:
        probe_timeouts.append(timeout)
        return ""

    def extract(*args: object, **kwargs: object) -> SkeletonReport:
        extraction_scopes.append(kwargs.get("node_ids"))
        runner = kwargs.get("runner")
        assert callable(runner)
        runner("", tmp_path)
        return _as_extracted(skeleton, args[0])

    monkeypatch.setattr("autoform_cli.__main__.run_probe", run_probe)
    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", extract)
    bundle_path = tmp_path / "review.json"
    packets = tmp_path / "packets"

    assert main(
        [
            "review",
            "prepare",
            str(blueprint),
            "--lean-root",
            str(tmp_path),
            "--output",
            str(bundle_path),
            "--packets",
            str(packets),
            "--timeout",
            "1800",
        ]
    ) == 0
    capsys.readouterr()
    bundle = load_review_bundle(bundle_path)
    manifest = json.loads((packets / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == REVIEW_PACKET_SCHEMA
    packet = packets / manifest["packets"][0]["packet"]
    assert packet.parent.name == "blind"
    assert packet.name == skeleton.nodes[0].declarations[0].evidence_hash.removeprefix("sha256:") + ".lean"
    assert "Review.result" not in packet.as_posix() and "basics" not in packet.as_posix()
    testimony = tmp_path / "testimony.md"
    testimony.write_text("For the unique proposition, the proposition is true.\n", encoding="utf-8")

    assert main(
        [
            "review",
            "record",
            str(blueprint),
            "--lean-root",
            str(tmp_path),
            "--bundle",
            str(bundle_path),
            "--article-id",
            "af_0123456789abcdef01234567",
            "--declaration",
            "Review.result",
            "--packet",
            str(packet),
            "--testimony",
            str(testimony),
            "--model",
            "test-model",
            "--timeout",
            "1800",
        ]
    ) == 0
    capsys.readouterr()
    assert extraction_scopes == [None, ("basics/result",)]
    cards = load_readbacks(blueprint)
    assert cards[("af_0123456789abcdef01234567", "Review.result")].shown_text == packet.read_text(
        encoding="utf-8"
    )

    assert main(
        [
            "review",
            "check",
            str(blueprint),
            "--lean-root",
            str(tmp_path),
            "--bundle",
            str(bundle_path),
            "--timeout",
            "1800",
        ]
    ) == 1
    output = capsys.readouterr().out
    assert "review-unapproved" in output

    approval = bundle.review_hash("af_0123456789abcdef01234567", cards)
    article = blueprint / "roadmap/basics/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "statement: formalized\n",
            f"statement: formalized\nreview_approved: {approval}\n",
        ),
        encoding="utf-8",
    )
    assert main(
        [
            "review",
            "check",
            str(blueprint),
            "--lean-root",
            str(tmp_path),
            "--bundle",
            str(bundle_path),
            "--timeout",
            "1800",
        ]
    ) == 0
    assert "OK: statement reviews match" in capsys.readouterr().out
    assert probe_timeouts == [1800.0] * 4


def test_check_and_render_derive_the_bundle_from_their_own_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What CI runs: no prepared bundle, and one extraction per command."""

    blueprint = _blueprint(tmp_path)
    skeleton = _skeleton()
    extraction_scopes: list[object] = []

    def extract(*args: object, **kwargs: object) -> SkeletonReport:
        extraction_scopes.append(kwargs.get("node_ids"))
        return _as_extracted(skeleton, args[0])

    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", extract)
    bundle_path = tmp_path / "review.json"
    packets = tmp_path / "packets"
    assert main(
        ["review", "prepare", str(blueprint), "--lean-root", str(tmp_path), "--output", str(bundle_path),
         "--packets", str(packets)]
    ) == 0
    packet = packets / json.loads((packets / "manifest.json").read_text(encoding="utf-8"))["packets"][0]["packet"]
    testimony = tmp_path / "testimony.md"
    testimony.write_text("For the unique proposition, the proposition is true.\n", encoding="utf-8")
    assert main(
        ["review", "record", str(blueprint), "--lean-root", str(tmp_path), "--bundle", str(bundle_path),
         "--article-id", "af_0123456789abcdef01234567", "--declaration", "Review.result",
         "--packet", str(packet), "--testimony", str(testimony), "--model", "test-model"]
    ) == 0
    capsys.readouterr()
    extraction_scopes.clear()

    check = ["review", "check", str(blueprint), "--lean-root", str(tmp_path)]
    assert main(check) == 1
    assert "review-unapproved" in capsys.readouterr().out
    assert extraction_scopes == [None]

    # The derived bundle is the prepared one: an approval of either holds for both.
    approval = load_review_bundle(bundle_path).review_hash("af_0123456789abcdef01234567", load_readbacks(blueprint))
    article = blueprint / "roadmap/basics/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "statement: formalized\n", f"statement: formalized\nreview_approved: {approval}\n"
        ),
        encoding="utf-8",
    )
    assert main(check) == 0
    assert "OK: statement reviews match" in capsys.readouterr().out
    assert extraction_scopes == [None, None]

    site = tmp_path / "site"
    assert main(["render", str(blueprint), "--lean-root", str(tmp_path), "--review", "--output", str(site)]) == 0
    assert extraction_scopes == [None, None, None]
    pages = "\n".join(path.read_text(encoding="utf-8") for path in site.rglob("*.md"))
    assert "For the unique proposition, the proposition is true." in pages
    assert "bp-readback-current" in pages


def test_render_takes_one_source_of_review_evidence(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = _blueprint(tmp_path)

    with pytest.raises(SystemExit) as refused:
        main(["render", str(blueprint), "--review", "--review-bundle", str(tmp_path / "review.json")])
    assert refused.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err

    assert main(["render", str(blueprint), "--review", "--output", str(tmp_path / "site")]) == 2
    assert "--review requires --lean-root" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "expected_status"),
    [
        (
            ["audit", "blueprint", "--lean-root", ".", "--review-bundle", "review.json"],
            2,
        ),
        (["render", "blueprint", "--lean-root", ".", "--review"], 1),
    ],
)
def test_review_consumers_forward_probe_timeout(
    command: list[str],
    expected_status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[float | None] = []

    def current_review(*args: object, **kwargs: object) -> object:
        timeouts.append(kwargs.get("timeout"))
        raise SkeletonError(["stop after observing timeout"])

    monkeypatch.setattr("autoform_cli.__main__._current_review", current_review)

    assert main([*command, "--timeout", "1800"]) == expected_status
    assert timeouts == [1800.0]


def test_review_record_rejects_packet_bytes_that_differ_from_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blueprint = _blueprint(tmp_path)
    skeleton = _skeleton()
    monkeypatch.setattr(
        "autoform_cli.__main__.extract_skeletons", lambda *args, **kwargs: _as_extracted(skeleton, args[0])
    )
    bundle_path = tmp_path / "review.json"
    packets = tmp_path / "packets"
    assert main(
        [
            "review",
            "prepare",
            str(blueprint),
            "--lean-root",
            str(tmp_path),
            "--output",
            str(bundle_path),
            "--packets",
            str(packets),
        ]
    ) == 0
    capsys.readouterr()
    packet = tmp_path / "changed.lean"
    packet.write_bytes(skeleton.nodes[0].declarations[0].blind_text().encode("utf-8") + b"\n")
    testimony = tmp_path / "testimony.md"
    testimony.write_text("A testimony.\n", encoding="utf-8")

    assert main(
        [
            "review",
            "record",
            str(blueprint),
            "--lean-root",
            str(tmp_path),
            "--bundle",
            str(bundle_path),
            "--article-id",
            "af_0123456789abcdef01234567",
            "--declaration",
            "Review.result",
            "--packet",
            str(packet),
            "--testimony",
            str(testimony),
            "--model",
            "test-model",
        ]
    ) == 2
    assert "packet bytes do not match" in capsys.readouterr().err


def test_review_prepare_reports_output_filesystem_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blueprint = _blueprint(tmp_path)
    monkeypatch.setattr(
        "autoform_cli.__main__.extract_skeletons", lambda *args, **kwargs: _as_extracted(_skeleton(), args[0])
    )
    output = tmp_path / "review.json"
    output.mkdir()

    assert main(
        [
            "review",
            "prepare",
            str(blueprint),
            "--lean-root",
            str(tmp_path),
            "--output",
            str(output),
        ]
    ) == 2
    assert "error:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Batched records
# --------------------------------------------------------------------------- #

_OTHER_ID = "af_fedcba9876543210fedcba98"
_RESULT_ID = "af_0123456789abcdef01234567"


def _two_article_blueprint(root: Path) -> Path:
    blueprint = _blueprint(root)
    chapter = blueprint / "roadmap" / "basics"
    (chapter / "README.md").write_text(
        "# Basics\n\n- [Result](result.md)\n- [Other](other.md)\n",
        encoding="utf-8",
    )
    (chapter / "other.md").write_text(
        "---\n"
        f"article_id: {_OTHER_ID}\n"
        "declaration: theorem\n"
        "lean: Review.other\n"
        "statement: formalized\n"
        "---\n\n"
        "# Other\n\nTruth holds.\n\n"
        "## Depends on\n\nNone.\n",
        encoding="utf-8",
    )
    return blueprint


def _node(node_id: str, name: str) -> NodeSkeleton:
    return NodeSkeleton(
        node_id=node_id,
        article_path=f"roadmap/{node_id}.md",
        declarations=(
            DeclarationSkeleton(
                name=name,
                kind="theorem",
                module="Review",
                path="Review.lean",
                start_line=1,
                end_line=1,
                signature=f"{name} : True",
                raw_signature=f"{name} : True",
                semantic='{"generated":[],"root":{"safety":"safe","type":{"sort":{"zero":null}}}}',
                lean_version="4.32.2",
                depends=(),
                trusted=(),
                assumed=(),
                assumed_semantics=(),
                boundary_modules=(),
                axioms=(),
                axiom_semantics=(),
            ),
        ),
    )


class _Extraction:
    """A fake extraction scoped like the real one, which counts its calls."""

    def __init__(self, on_extract: object = None) -> None:
        self.scopes: list[object] = []
        self.on_extract = on_extract

    def __call__(self, *args: object, **kwargs: object) -> SkeletonReport:
        node_ids = kwargs.get("node_ids")
        self.scopes.append(node_ids)
        if callable(self.on_extract):
            self.on_extract()
        nodes = (_node("basics/other", "Review.other"), _node("basics/result", "Review.result"))
        wanted = None if node_ids is None else set(node_ids)
        selected = tuple(node for node in nodes if wanted is None or node.node_id in wanted)
        # The hash describes the blueprint as this call reads it, after the
        # hook: an edit made there lands before the extraction's own snapshot.
        return SkeletonReport(
            blueprint_hash=blueprint_hash(load_graph(args[0])),
            targets=tuple(
                (node.node_id, tuple(declaration.name for declaration in node.declarations))
                for node in nodes
            ),
            selection="all" if wanted is None else "filtered",
            selected_nodes=tuple(node.node_id for node in selected),
            nodes=selected,
            unresolved=(),
        )


def _prepared_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extraction: _Extraction) -> tuple[Path, Path, Path]:
    """Prepare two articles and write a records manifest beside two testimonies."""

    blueprint = _two_article_blueprint(tmp_path)
    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", extraction)
    bundle_path = tmp_path / "review.json"
    packets = tmp_path / "packets"
    assert main(
        ["review", "prepare", str(blueprint), "--lean-root", str(tmp_path), "--output", str(bundle_path), "--packets", str(packets)]
    ) == 0
    entries = json.loads((packets / "manifest.json").read_text(encoding="utf-8"))["packets"]
    batch = tmp_path / "batch"
    batch.mkdir()
    records = []
    for entry in entries:
        testimony = batch / f"{entry['declaration']}.md"
        testimony.write_text(f"The statement {entry['declaration']} asserts True.\n", encoding="utf-8")
        records.append(
            {
                "article_id": entry["article_id"],
                "declaration": entry["declaration"],
                "packet": f"../packets/{entry['packet']}",
                "testimony": testimony.name,
            }
        )
    manifest = batch / "records.json"
    manifest.write_text(json.dumps({"schema": "autoform-review-records/v1", "records": records}), encoding="utf-8")
    return blueprint, bundle_path, manifest


def _record(blueprint: Path, bundle: Path, manifest: Path, root: Path) -> int:
    return main(
        [
            "review",
            "record",
            str(blueprint),
            "--lean-root",
            str(root),
            "--bundle",
            str(bundle),
            "--manifest",
            str(manifest),
            "--model",
            "test-model",
        ]
    )


def test_a_batch_files_every_card_against_one_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    extraction = _Extraction()
    blueprint, bundle, manifest = _prepared_batch(tmp_path, monkeypatch, extraction)
    capsys.readouterr()

    assert _record(blueprint, bundle, manifest, tmp_path) == 0

    # One extraction for the batch, scoped to exactly the articles it records.
    assert extraction.scopes == [None, ("basics/other", "basics/result")]
    cards = load_readbacks(blueprint)
    assert set(cards) == {(_OTHER_ID, "Review.other"), (_RESULT_ID, "Review.result")}
    assert capsys.readouterr().out.count("recorded read-back for") == 2

    # Filing identical content is a no-op, so the same batch runs again cleanly.
    assert _record(blueprint, bundle, manifest, tmp_path) == 0
    assert load_readbacks(blueprint) == cards


def test_one_bad_record_stops_the_batch_before_any_card_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    extraction = _Extraction()
    blueprint, bundle, manifest = _prepared_batch(tmp_path, monkeypatch, extraction)
    (manifest.parent / "Review.result.md").write_text("   \n", encoding="utf-8")
    capsys.readouterr()

    assert _record(blueprint, bundle, manifest, tmp_path) == 2

    assert load_readbacks(blueprint) == {}
    assert extraction.scopes == [None]  # only `review prepare` extracted
    err = capsys.readouterr().err
    assert "Review.result: a read-back requires nonempty testimony" in err


def test_an_article_changed_during_extraction_files_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The batch pairs one extraction with one state of the blueprint."""

    article = tmp_path / "blueprint" / "roadmap" / "basics" / "other.md"
    calls: list[int] = []

    def edit_during_the_record() -> None:
        calls.append(1)
        if len(calls) == 2:  # the first extraction is `review prepare`
            article.write_text(article.read_text(encoding="utf-8") + "\nAn edit.\n", encoding="utf-8")

    blueprint, bundle, manifest = _prepared_batch(tmp_path, monkeypatch, _Extraction(edit_during_the_record))
    capsys.readouterr()

    assert _record(blueprint, bundle, manifest, tmp_path) == 2

    assert load_readbacks(blueprint) == {}
    assert "changed during extraction; nothing was filed" in capsys.readouterr().err


def test_any_blueprint_edit_during_a_record_extraction_files_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The extraction checks the whole blueprint, so an edit to an article the
    record does not touch still aborts it, and running it again files the card."""

    blueprint, bundle, manifest = _prepared_batch(tmp_path, monkeypatch, _Extraction())
    records = json.loads(manifest.read_text(encoding="utf-8"))["records"]
    alone = manifest.parent / "result-only.json"
    alone.write_text(
        json.dumps(
            {"schema": "autoform-review-records/v1", "records": [r for r in records if r["article_id"] == _RESULT_ID]}
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text('name = "Review"\n', encoding="utf-8")
    other = blueprint / "roadmap" / "basics" / "other.md"
    edits: list[int] = []

    def probe_while_other_is_edited(graph: Graph, *, node_ids: tuple[str, ...] | None, **_: object) -> SkeletonReport:
        if not edits:
            edits.append(1)
            other.write_text(other.read_text(encoding="utf-8") + "\nAn unrelated edit.\n", encoding="utf-8")
        return replace(_Extraction()(graph.blueprint_dir, node_ids=node_ids), blueprint_hash=blueprint_hash(graph))

    # The real extraction, with only the Lean probe replaced.
    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", extract_skeletons)
    monkeypatch.setattr("autoform_cli.skeleton.extract_graph_skeletons", probe_while_other_is_edited)
    capsys.readouterr()

    assert _record(blueprint, bundle, alone, project) == 2
    assert load_readbacks(blueprint) == {}
    assert "the blueprint changed while skeletons were being extracted" in capsys.readouterr().err

    assert _record(blueprint, bundle, alone, project) == 0
    assert set(load_readbacks(blueprint)) == {(_RESULT_ID, "Review.result")}


def test_a_record_files_nothing_if_any_article_changes_after_its_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A record scoped to some articles still pairs its extraction with the whole
    blueprint, so a page it does not select, edited before the reload, stops it."""

    extraction = _Extraction()
    blueprint, bundle, manifest = _prepared_batch(tmp_path, monkeypatch, extraction)
    chapter = blueprint / "roadmap" / "basics" / "README.md"

    def extract_then_edit_the_chapter(*args: object, **kwargs: object) -> SkeletonReport:
        report = extraction(*args, **kwargs)
        chapter.write_text(chapter.read_text(encoding="utf-8") + "\nAn unrelated edit.\n", encoding="utf-8")
        return report

    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", extract_then_edit_the_chapter)
    capsys.readouterr()

    assert _record(blueprint, bundle, manifest, tmp_path) == 2
    assert load_readbacks(blueprint) == {}
    assert "the blueprint changed after its review evidence was extracted" in capsys.readouterr().err


def _file_alone(blueprint: Path, bundle: Path, manifest: Path, root: Path, record: dict[str, str], text: str) -> None:
    """File one card for ``record`` with different testimony, as another reviewer would."""

    testimony = manifest.parent / f"elsewhere-{record['declaration']}.md"
    testimony.write_text(text, encoding="utf-8")
    alone = manifest.parent / f"alone-{record['declaration']}.json"
    alone.write_text(
        json.dumps({"schema": "autoform-review-records/v1", "records": [dict(record, testimony=testimony.name)]}),
        encoding="utf-8",
    )
    assert _record(blueprint, bundle, alone, root) == 0


def test_a_card_that_would_replace_another_is_refused_before_lean_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A re-review lands on the path of the card it supersedes. The batch names
    every such card, with the hash to pass, before it pays for an extraction."""

    extraction = _Extraction()
    blueprint, bundle, manifest = _prepared_batch(tmp_path, monkeypatch, extraction)
    records = json.loads(manifest.read_text(encoding="utf-8"))["records"]
    for record in records:
        _file_alone(blueprint, bundle, manifest, tmp_path, record, f"An older reading of {record['declaration']}.\n")
    before = load_readbacks(blueprint)
    extractions = len(extraction.scopes)
    capsys.readouterr()

    assert _record(blueprint, bundle, manifest, tmp_path) == 2

    assert len(extraction.scopes) == extractions
    assert load_readbacks(blueprint) == before
    err = capsys.readouterr().err
    assert err.count("read-back already exists with different content") == 2
    for record in records:
        current = before[(record["article_id"], record["declaration"])].file_hash
        assert f"{record['declaration']}: read-back already exists with different content" in err
        assert f"expected_card_hash={current!r}" in err

    # Naming the cards it replaces lets the same batch run.
    for record in records:
        record["expected_card_hash"] = before[(record["article_id"], record["declaration"])].file_hash
    manifest.write_text(json.dumps({"schema": "autoform-review-records/v1", "records": records}), encoding="utf-8")
    assert _record(blueprint, bundle, manifest, tmp_path) == 0
    after = load_readbacks(blueprint)
    assert all(after[key].file_hash != before[key].file_hash for key in before)


def test_a_stale_expected_hash_is_refused_before_lean_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    extraction = _Extraction()
    blueprint, bundle, manifest = _prepared_batch(tmp_path, monkeypatch, extraction)
    records = json.loads(manifest.read_text(encoding="utf-8"))["records"]
    _file_alone(blueprint, bundle, manifest, tmp_path, records[0], "An older reading.\n")
    stale = "sha256:" + "0" * 64
    records[0]["expected_card_hash"] = stale
    manifest.write_text(json.dumps({"schema": "autoform-review-records/v1", "records": records}), encoding="utf-8")
    extractions = len(extraction.scopes)
    capsys.readouterr()

    assert _record(blueprint, bundle, manifest, tmp_path) == 2

    assert len(extraction.scopes) == extractions
    assert f"read-back changed before replacement: expected {stale!r}" in capsys.readouterr().err


def test_a_batch_interrupted_while_publishing_says_what_it_filed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every check runs before the first card is written. Only a concurrent
    writer can stop the batch midway, and running it again finishes it."""

    blueprint, bundle, manifest = _prepared_batch(tmp_path, monkeypatch, _Extraction())
    records = json.loads(manifest.read_text(encoding="utf-8"))["records"]
    later = records[1]
    publish = main.__globals__["publish_readback"]
    published: list[Path] = []

    def another_writer_between_cards(card: object) -> Path:
        path = publish(card)
        published.append(path)
        if len(published) == 1:
            # Someone else files a different card for the second declaration
            # after the batch checked it and before the batch writes it.
            monkeypatch.setattr("autoform_cli.__main__.publish_readback", publish)
            _file_alone(blueprint, bundle, manifest, tmp_path, later, "A different reading.\n")
            monkeypatch.setattr("autoform_cli.__main__.publish_readback", another_writer_between_cards)
        return path

    monkeypatch.setattr("autoform_cli.__main__.publish_readback", another_writer_between_cards)
    capsys.readouterr()

    assert _record(blueprint, bundle, manifest, tmp_path) == 2

    captured = capsys.readouterr()
    assert captured.out.count("recorded read-back for") == 2  # ours, then the other writer's
    assert "1 of 2 read-back(s) were filed before the failure below" in captured.err
    assert "read-back already exists with different content" in captured.err

    # Naming the card it replaces lets the same batch finish.
    monkeypatch.setattr("autoform_cli.__main__.publish_readback", publish)
    current = load_readbacks(blueprint)[(later["article_id"], later["declaration"])].file_hash
    records[1]["expected_card_hash"] = current
    manifest.write_text(json.dumps({"schema": "autoform-review-records/v1", "records": records}), encoding="utf-8")
    assert _record(blueprint, bundle, manifest, tmp_path) == 0
    assert len(load_readbacks(blueprint)) == 2


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"schema": "other", "records": []}, "expected schema autoform-review-records/v1"),
        ({"schema": "autoform-review-records/v1", "records": []}, "records must be a non-empty list"),
        (
            {"schema": "autoform-review-records/v1", "records": [{"article_id": _RESULT_ID, "declaration": "Review.result"}]},
            "needs exactly article_id, declaration, packet, testimony",
        ),
        (
            {
                "schema": "autoform-review-records/v1",
                "records": [
                    {"article_id": _RESULT_ID, "declaration": "Review.result", "packet": "p", "testimony": "t", "extra": 1}
                ],
            },
            "needs exactly article_id, declaration, packet, testimony",
        ),
        (
            {
                "schema": "autoform-review-records/v1",
                "records": [
                    {"article_id": _RESULT_ID, "declaration": "Review.result", "packet": "p", "testimony": "t"},
                    {"article_id": _RESULT_ID, "declaration": "Review.result", "packet": "p", "testimony": "u"},
                ],
            },
            "Review.result is already recorded earlier in this manifest",
        ),
        (
            {
                "schema": "autoform-review-records/v1",
                "records": [
                    {
                        "article_id": _RESULT_ID,
                        "declaration": "Review.result",
                        "packet": "p",
                        "testimony": "t",
                        "expected_card_hash": "sha256:nothex",
                    }
                ],
            },
            "invalid expected_card_hash",
        ),
    ],
)
def test_a_malformed_manifest_is_refused_before_any_lean_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
    message: str,
) -> None:
    extraction = _Extraction()
    blueprint, bundle, _ = _prepared_batch(tmp_path, monkeypatch, extraction)
    manifest = tmp_path / "bad.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    capsys.readouterr()

    assert _record(blueprint, bundle, manifest, tmp_path) == 2

    assert message in capsys.readouterr().err
    assert extraction.scopes == [None]  # only `review prepare` extracted


def test_every_unreadable_input_is_named_before_any_lean_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing file is reported beside every other bad input, not alone."""

    extraction = _Extraction()
    blueprint, bundle, manifest = _prepared_batch(tmp_path, monkeypatch, extraction)
    records = json.loads(manifest.read_text(encoding="utf-8"))["records"]
    records[0]["packet"] = "../packets/blind/nowhere.lean"
    (manifest.parent / records[1]["testimony"]).unlink()
    manifest.write_text(json.dumps({"schema": "autoform-review-records/v1", "records": records}), encoding="utf-8")
    capsys.readouterr()

    assert _record(blueprint, bundle, manifest, tmp_path) == 2

    err = capsys.readouterr().err
    assert f"{records[0]['declaration']}: cannot read the packet" in err
    assert f"{records[1]['declaration']}: cannot read the testimony" in err
    assert extraction.scopes == [None]  # only `review prepare` extracted
    assert load_readbacks(blueprint) == {}


def test_record_takes_a_manifest_or_one_record_but_not_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = ["review", "record", str(tmp_path), "--lean-root", str(tmp_path), "--bundle", str(tmp_path / "b.json"), "--model", "m"]

    assert main([*common, "--manifest", str(tmp_path / "m.json"), "--article-id", _RESULT_ID]) == 2
    assert "--manifest replaces" in capsys.readouterr().err
    assert main([*common, "--article-id", _RESULT_ID]) == 2
    assert "needs --manifest, or all of" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# One snapshot per check
# --------------------------------------------------------------------------- #


def _approved_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extraction: _Extraction) -> Path:
    """Record and approve both articles, so that `review check` passes."""

    blueprint, bundle, manifest = _prepared_batch(tmp_path, monkeypatch, extraction)
    assert _record(blueprint, bundle, manifest, tmp_path) == 0
    cards = load_readbacks(blueprint)
    prepared = load_review_bundle(bundle)
    for name, article_id in (("result", _RESULT_ID), ("other", _OTHER_ID)):
        article = blueprint / "roadmap" / "basics" / f"{name}.md"
        article.write_text(
            article.read_text(encoding="utf-8").replace(
                "statement: formalized\n",
                f"statement: formalized\nreview_approved: {prepared.review_hash(article_id, cards)}\n",
            ),
            encoding="utf-8",
        )
    return blueprint


def _check(blueprint: Path, root: Path) -> int:
    return main(["review", "check", str(blueprint), "--lean-root", str(root)])


def test_check_judges_the_blueprint_its_extraction_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An approval changed after the check loaded the graph, but before the
    extraction took its snapshot, must not pass on the strength of the old one."""

    extraction = _Extraction()
    blueprint = _approved_batch(tmp_path, monkeypatch, extraction)
    assert _check(blueprint, tmp_path) == 0
    capsys.readouterr()
    article = blueprint / "roadmap" / "basics" / "result.md"

    def approve_something_else() -> None:
        text = article.read_text(encoding="utf-8")
        article.write_text(
            re.sub(r"review_approved: \S+", "review_approved: sha256:" + "f" * 64, text), encoding="utf-8"
        )

    extraction.on_extract = approve_something_else
    assert _check(blueprint, tmp_path) == 2
    captured = capsys.readouterr()
    assert "an article changed while the review evidence was being extracted" in captured.err
    assert "OK:" not in captured.out

    # The tree does hold an invalid approval, which a check of an idle tree names.
    extraction.on_extract = None
    assert _check(blueprint, tmp_path) == 1
    assert "review-drift" in capsys.readouterr().out


def _edit_card(card: Path) -> None:
    card.write_text(card.read_text(encoding="utf-8").replace("asserts True.", "asserts nothing."), encoding="utf-8")


def _add_card(card: Path) -> None:
    card.with_name("copy-" + card.name).write_bytes(card.read_bytes())


def _remove_card(card: Path) -> None:
    card.unlink()


@pytest.mark.parametrize("change", [_edit_card, _add_card, _remove_card], ids=["edited", "added", "removed"])
def test_check_refuses_a_readback_changed_during_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: Callable[[Path], None],
) -> None:
    """Cards are read before Lean runs and must be the same after it: the check
    judges neither the old cards nor the new ones against this extraction."""

    extraction = _Extraction()
    blueprint = _approved_batch(tmp_path, monkeypatch, extraction)
    card = load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].path
    extraction.on_extract = lambda: change(card)
    capsys.readouterr()

    assert _check(blueprint, tmp_path) == 2

    captured = capsys.readouterr()
    assert "a read-back changed while the review evidence was being extracted" in captured.err
    assert captured.out == ""


def test_render_review_shows_the_readbacks_its_check_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cards rendered are the snapshot the review was validated against, not a later read."""

    blueprint = _approved_batch(tmp_path, monkeypatch, _Extraction())

    def read_again(*args: object, **kwargs: object) -> object:
        raise AssertionError("render read the read-backs again after validating the review")

    monkeypatch.setattr("autoform_cli.render.load_readbacks", read_again)
    site = tmp_path / "site"
    assert main(["render", str(blueprint), "--lean-root", str(tmp_path), "--review", "--output", str(site)]) == 0
    pages = "\n".join(path.read_text(encoding="utf-8") for path in site.rglob("*.md"))
    assert "The statement Review.result asserts True." in pages
    assert "bp-review-approved" in pages


def test_render_refuses_articles_edited_after_the_review_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """render_site loads the articles itself, after the check: they must be the ones
    the checked extraction saw, or the cards it was handed describe another state."""

    blueprint = _approved_batch(tmp_path, monkeypatch, _Extraction())
    _, skeleton, bundle, cards = _current_review(blueprint, lean_root=tmp_path, bundle_path=None)
    article = blueprint / "roadmap" / "basics" / "result.md"
    text = article.read_text(encoding="utf-8")
    article.write_text(re.sub(r"review_approved: \S+", "review_approved: sha256:" + "f" * 64, text), encoding="utf-8")
    site = tmp_path / "site"

    with pytest.raises(PublicationError, match="the blueprint changed after its review evidence was extracted"):
        render_site(blueprint, site, lean_root=tmp_path, skeleton=skeleton, review_bundle=bundle, readbacks=cards)
    assert not site.exists()


def _audit(blueprint: Path, root: Path) -> int:
    """Audit against the bundle `_prepared_batch` wrote, over Lean sources that
    declare both targets, so that only review evidence can fail it."""

    (root / "Review.lean").write_text(
        "namespace Review\n\ntheorem result : True := trivial\n\ntheorem other : True := trivial\n\nend Review\n",
        encoding="utf-8",
    )
    return main(["audit", str(blueprint), "--lean-root", str(root), "--review-bundle", str(root / "review.json")])


def _after_the_check(monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]) -> None:
    """Run ``change`` once the review check has returned its snapshot."""

    def check_then_change(*args: object, **kwargs: object) -> object:
        checked = _current_review(*args, **kwargs)
        change()
        return checked

    monkeypatch.setattr("autoform_cli.__main__._current_review", check_then_change)


def test_audit_judges_the_readbacks_its_check_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A card edited after the check took its snapshot is not what audit judges:
    it judges the cards the check validated, and does not read the vault again."""

    blueprint = _approved_batch(tmp_path, monkeypatch, _Extraction())
    card = load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].path
    _after_the_check(monkeypatch, lambda: _edit_card(card))
    reads: list[object] = []

    def read_again(*args: object, **kwargs: object) -> object:
        reads.append(args)
        return load_readbacks(*args, **kwargs)

    monkeypatch.setattr("autoform_cli.review.load_readbacks", read_again)
    capsys.readouterr()

    assert _audit(blueprint, tmp_path) == 0
    assert "OK: roadmap audit passed" in capsys.readouterr().out
    assert reads == []


def test_audit_refuses_articles_edited_after_the_review_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A card and the approval that matches it, both changed after the check, would
    agree with each other; audit loads the articles itself and must refuse them,
    since the check validated neither."""

    blueprint = _approved_batch(tmp_path, monkeypatch, _Extraction())
    card = load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].path
    article = blueprint / "roadmap" / "basics" / "result.md"

    def edit_the_card_and_approve_it() -> None:
        _edit_card(card)
        approval = load_review_bundle(tmp_path / "review.json").review_hash(_RESULT_ID, load_readbacks(blueprint))
        text = article.read_text(encoding="utf-8")
        article.write_text(re.sub(r"review_approved: \S+", f"review_approved: {approval}", text), encoding="utf-8")

    _after_the_check(monkeypatch, edit_the_card_and_approve_it)
    capsys.readouterr()

    assert _audit(blueprint, tmp_path) == 1
    out = capsys.readouterr().out
    assert "review-snapshot-changed: the blueprint changed after its review evidence was extracted" in out
    assert "OK:" not in out
