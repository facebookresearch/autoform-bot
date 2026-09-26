from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.readback import load_readbacks
from autoform_cli.review import REVIEW_PACKET_SCHEMA, load_review_bundle
from autoform_cli.skeleton import DeclarationSkeleton, NodeSkeleton, SkeletonReport


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
        semantic='{"const":"True"}',
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
        nodes=(
            NodeSkeleton(
                node_id="basics/result",
                article_path="roadmap/basics/result.md",
                declarations=(declaration,),
            ),
        ),
        unresolved=(),
    )


def test_review_cli_prepares_records_and_checks_exact_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blueprint = _blueprint(tmp_path)
    skeleton = _skeleton()
    extraction_scopes: list[object] = []

    def extract(*args: object, **kwargs: object) -> SkeletonReport:
        extraction_scopes.append(kwargs.get("node_ids"))
        return skeleton

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
        ]
    ) == 0
    assert "OK: statement reviews match" in capsys.readouterr().out


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
        return skeleton

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


def test_review_record_rejects_packet_bytes_that_differ_from_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blueprint = _blueprint(tmp_path)
    skeleton = _skeleton()
    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", lambda *args, **kwargs: skeleton)
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
    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", lambda *args, **kwargs: _skeleton())
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
                semantic='{"const":"True"}',
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
        return SkeletonReport(nodes=tuple(n for n in nodes if wanted is None or n.node_id in wanted), unresolved=())


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
    blueprint, bundle, manifest = _prepared_batch(tmp_path, monkeypatch, _Extraction())
    (manifest.parent / "Review.result.md").write_text("   \n", encoding="utf-8")
    capsys.readouterr()

    assert _record(blueprint, bundle, manifest, tmp_path) == 2

    assert load_readbacks(blueprint) == {}
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
