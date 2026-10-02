from __future__ import annotations

import hashlib
import json
import os
import pickle
import stat
import subprocess
import sys
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoform_cli.readback import (
    SKELETON_HEADING,
    load_readbacks,
    prepare_readback,
    publish_readback,
    readback_conflicts,
    readback_findings,
    readback_path,
    write_readback,
)
from autoform_cli.skeleton import (
    PACKET_MANIFEST,
    DeclarationSkeleton,
    NodeSkeleton,
    SkeletonReport,
    TrustedDeclaration,
    load_skeleton_report,
    write_packets,
)

_PROP = '{"generated":[],"root":{"safety":"safe","type":{"sort":{"zero":null}}}}'
_BODY = '{"generated":[],"root":{"safety":"safe","type":{"sort":{"zero":null}},"value":{"bvar":0}}}'
_OTHER_BODY = '{"generated":[],"root":{"safety":"safe","type":{"sort":{"zero":null}},"value":{"bvar":1}}}'
_ARTICLE_ID = "af_0123456789abcdef01234567"
_BLUEPRINT_HASH = "sha256:" + "0" * 64


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _trusted(source: str, semantic: str = _BODY) -> TrustedDeclaration:
    comments: list[tuple[int, int]] = []
    if source.startswith("/--"):
        end = source.index("-/") + 2
        comments.append((0, len(source[:end].encode("utf-8"))))
    line_comment = source.find("--", 3)
    if line_comment >= 0:
        comments.append((len(source[:line_comment].encode("utf-8")), len(source.encode("utf-8"))))
    return TrustedDeclaration(
        name="Skel.IsSup",
        kind="def",
        module="Skel.Defs",
        path="Skel/Defs.lean",
        start_line=3,
        end_line=4,
        signature="Skel.IsSup {S : Type} [LinearOrder S] (E : Set S) (b : S) : Prop",
        raw_signature="Skel.IsSup {S : Type} [LinearOrder S] (E : Set S) (b : S) : Prop",
        semantic=semantic,
        depends=(),
        source=source,
        source_comments=tuple(comments),
    )


def _declaration(
    source: str = "/-- doc -/\ndef IsSup (E : Set S) (b : S) : Prop := ∀ x ∈ E, x ≤ b",
    semantic: str = _BODY,
) -> DeclarationSkeleton:
    """A theorem resting on one definition; ``semantic`` is the definition's elaborated body."""

    return DeclarationSkeleton(
        name="Skel.sup_unique",
        kind="theorem",
        module="Skel.Main",
        path="Skel/Main.lean",
        start_line=10,
        end_line=14,
        signature="Skel.sup_unique {E : Set ℝ} {a b : ℝ} (ha : Skel.IsSup E a) (hb : Skel.IsSup E b) : a = b",
        raw_signature="Skel.sup_unique {E : Set ℝ} {a b : ℝ} (ha : Skel.IsSup E a) (hb : Skel.IsSup E b) : a = b",
        semantic=_PROP,
        lean_version="4.32.2",
        depends=("Skel.IsSup",),
        trusted=(_trusted(source, semantic),),
        assumed=(),
        assumed_semantics=(),
        boundary_modules=(),
        axioms=("propext",),
        axiom_semantics=(("propext", _PROP),),
    )


def _report(declaration: DeclarationSkeleton | None = None) -> SkeletonReport:
    declaration = declaration or _declaration()
    node = NodeSkeleton(
        node_id="basics/sup-unique", article_path="roadmap/basics/sup-unique.md", declarations=(declaration,)
    )
    return SkeletonReport(
        blueprint_hash=_BLUEPRINT_HASH,
        targets=((node.node_id, (declaration.name,)),),
        selection="all",
        selected_nodes=(node.node_id,),
        nodes=(node,),
        unresolved=(),
    )


def _blueprint(root: Path) -> Path:
    blueprint = root / "blueprint"
    chapter = blueprint / "roadmap" / "basics"
    chapter.mkdir(parents=True)
    (blueprint / "roadmap" / "README.md").write_text("# Roadmap\n\n- [Basics](basics/README.md)\n", encoding="utf-8")
    (chapter / "README.md").write_text("# Basics\n\nOne result.\n\n- [Sup unique](sup-unique.md)\n", encoding="utf-8")
    frontmatter = [
        "---",
        f"article_id: {_ARTICLE_ID}",
        "declaration: theorem",
        "lean: Skel.sup_unique",
        "statement: formalized",
    ]
    frontmatter.append("---")
    (chapter / "sup-unique.md").write_text(
        "\n".join(frontmatter) + "\n\n# Sup unique\n\nA supremum is unique.\n\n## Depends on\n\nNone.\n",
        encoding="utf-8",
    )
    coverage = blueprint / "coverage"
    coverage.mkdir()
    (coverage / "README.md").write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n| All | OUT | scratch |\n",
        encoding="utf-8",
    )
    return blueprint


# --------------------------------------------------------------------------- #
# Hashes and packets
# --------------------------------------------------------------------------- #


def test_the_semantic_hash_ignores_presentation_and_the_evidence_hash_does_not() -> None:
    base = _declaration()
    reworded = _declaration(
        "/-- a clearer docstring -/\ndef IsSup (E : Set S) (b : S) : Prop := ∀ x ∈ E, x ≤ b  -- note"
    )
    respaced = _declaration("def IsSup (E : Set S) (b : S) : Prop :=\n  ∀ x ∈ E, x ≤ b")
    changed = _declaration("def IsSup (E : Set S) (b : S) : Prop := ∀ x ∈ E, x < b", semantic=_OTHER_BODY)

    assert base.hash.startswith("sha256:") and len(base.hash) == 71
    # meaning: unchanged by comments and layout, changed by the elaborated body
    assert base.hash == reworded.hash == respaced.hash != changed.hash
    # evidence: the exact packet text, so a comment leaves it alone but layout does not
    assert base.evidence_hash == reworded.evidence_hash != respaced.evidence_hash
    # a one-declaration article shares its declaration's meaning hash; its evidence hash covers the joint packet
    assert _report().nodes[0].hash == base.hash and _report().nodes[0].evidence_hash != base.evidence_hash


def test_a_multi_declaration_article_hashes_all_of_them() -> None:
    first = _declaration()
    second = DeclarationSkeleton(**{**{name: getattr(first, name) for name in first.__slots__}, "name": "Skel.other"})
    node = NodeSkeleton(node_id="basics/pair", article_path="roadmap/basics/pair.md", declarations=(first, second))

    assert node.hash not in {first.hash, second.hash}
    assert node.evidence_hash not in {first.evidence_hash, second.evidence_hash}


def test_blind_packets_strip_every_comment_and_name_nothing_else(tmp_path: Path) -> None:
    report = _report()

    written = write_packets(report, tmp_path / "packets")

    manifest = json.loads((tmp_path / "packets" / PACKET_MANIFEST).read_text(encoding="utf-8"))
    (entry,) = manifest["packets"]
    article = tmp_path / "packets" / entry["article_packet"]
    packet = tmp_path / "packets" / entry["packet"]
    assert set(written) == {article, packet}
    assert article.read_text(encoding="utf-8").startswith(
        "-- article with 1 declaration(s)\n-- theorem Skel.sup_unique"
    )
    text = packet.read_text(encoding="utf-8")
    assert "doc" not in text
    assert "-- signature: Skel.IsSup {S : Type} [LinearOrder S] (E : Set S) (b : S) : Prop" in text
    assert "def IsSup (E : Set S) (b : S) : Prop := ∀ x ∈ E, x ≤ b" in text
    assert "sup-unique" not in text and "roadmap" not in text
    declaration = report.nodes[0].declarations[0]
    assert entry["declaration"] == "Skel.sup_unique" and entry["node_id"] == "basics/sup-unique"
    assert entry["packet"].startswith("basics/sup-unique/Skel.sup_unique")
    # what a read-back records: the meaning it testifies about and the text it was written from
    assert entry["hash"] == declaration.hash and entry["packet_hash"] == declaration.evidence_hash


def test_the_source_and_hashes_round_trip_through_the_report(tmp_path: Path) -> None:
    report = _report()
    path = tmp_path / "skeleton.json"
    path.write_text(report.to_json(), encoding="utf-8")

    loaded = load_skeleton_report(path)

    assert loaded == report
    assert loaded.nodes[0].declarations[0].trusted[0].source == report.nodes[0].declarations[0].trusted[0].source
    node = json.loads(report.to_json())["nodes"][0]
    assert node["hash"] == report.nodes[0].hash and node["evidence_hash"] == report.nodes[0].evidence_hash


# --------------------------------------------------------------------------- #
# Read-back files
# --------------------------------------------------------------------------- #


def test_readbacks_are_filed_per_declaration_and_report_their_status(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _declaration()

    path = write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="test-model",
        text="Let $E$ …",
        packet_text=declaration.blind_text(),
    )

    assert path == readback_path(blueprint, _ARTICLE_ID, "Skel.sup_unique")
    # The file is a self-contained review card: title, the skeleton shown to
    # the auditor, then the testimony. Only the testimony is read back.
    card = path.read_text(encoding="utf-8")
    assert card.startswith(f'---\nschema: autoform-readback/v1\narticle_id: "{_ARTICLE_ID}"\n')
    assert "# Read-back\n" in card and "## Skeleton\n\n```lean\n" in card and "## Read-back\n\nLet $E$ …" in card
    assert "def IsSup (E : Set S) (b : S) : Prop := ∀ x ∈ E, x ≤ b" in card and "/-- doc -/" not in card
    readbacks = load_readbacks(blueprint)
    readback = readbacks[(_ARTICLE_ID, "Skel.sup_unique")]
    assert readback.article_id == _ARTICLE_ID and readback.valid
    assert (readback.model, readback.text) == ("test-model", "Let $E$ …")
    assert (readback.skeleton_hash, readback.packet_hash) == (declaration.hash, declaration.evidence_hash)
    assert readback.status(declaration) == "current"
    # same meaning, different packet text: revised; different meaning: stale
    assert readback.status(_declaration("def IsSup (E : Set S) (b : S) : Prop :=\n  ∀ x ∈ E, x ≤ b")) == "revised"
    assert (
        readback.status(_declaration("def IsSup (E : Set S) (b : S) : Prop := True", semantic=_OTHER_BODY)) == "stale"
    )
    assert readback_findings(
        _report(declaration),
        readbacks,
        article_ids={"basics/sup-unique": _ARTICLE_ID},
    ) == []


def test_missing_stale_and_revised_readbacks_are_reported(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    old = _declaration("def IsSup (E : Set S) (b : S) : Prop := True", semantic=_OTHER_BODY)
    write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=old,
        model="m",
        text="Anything.",
        packet_text=old.blind_text(),
    )

    (stale,) = readback_findings(
        _report(),
        load_readbacks(blueprint),
        article_ids={"basics/sup-unique": _ARTICLE_ID},
    )
    assert (stale.code, stale.declaration) == ("readback-stale", "Skel.sup_unique")
    assert old.hash in stale.reason and _declaration().hash in stale.reason

    respaced = _declaration("def IsSup (E : Set S) (b : S) : Prop :=\n  ∀ x ∈ E, x ≤ b")
    old_path = readback_path(blueprint, _ARTICLE_ID, old.name)
    expected = load_readbacks(blueprint)[(_ARTICLE_ID, old.name)].file_hash
    write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=respaced,
        model="m",
        text="Anything.",
        packet_text=respaced.blind_text(),
        expected_card_hash=expected,
    )
    assert old_path.is_file()
    (revised,) = readback_findings(
        _report(),
        load_readbacks(blueprint),
        article_ids={"basics/sup-unique": _ARTICLE_ID},
    )
    assert revised.code == "readback-revised" and _declaration().evidence_hash in revised.reason

    (missing,) = readback_findings(_report(), {})
    assert missing.code == "readback-missing"


# --------------------------------------------------------------------------- #
# Cards are vault files and nothing else
# --------------------------------------------------------------------------- #


def test_card_paths_stay_inside_the_readbacks_tree(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    with pytest.raises(ValueError):
        readback_path(blueprint, "../escape", "Skel.sup_unique")
    with pytest.raises(ValueError):
        readback_path(blueprint, _ARTICLE_ID, "")
    with pytest.raises(ValueError):
        readback_path(blueprint, "basics//sup-unique", "Skel.sup_unique")
    encoded = readback_path(blueprint, _ARTICLE_ID, "../Skel.sup_unique")
    assert encoded.parent == blueprint / "readbacks" / _ARTICLE_ID
    assert "/" not in encoded.name and encoded.suffix == ".md"


def test_a_card_that_shows_lean_it_does_not_record_is_reported(tmp_path: Path) -> None:
    """The card is read in the vault, so the Lean it shows is the evidence."""

    blueprint = _blueprint(tmp_path)
    declaration = _declaration()
    path = write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="m",
        text="Fine.",
        packet_text=declaration.blind_text(),
    )
    assert load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].shows_what_it_attests
    assert (
        readback_findings(
            _report(),
            load_readbacks(blueprint),
            article_ids={"basics/sup-unique": _ARTICLE_ID},
        )
        == []
    )

    path.write_text(path.read_text(encoding="utf-8").replace("∀ x ∈ E, x ≤ b", "∀ x ∈ E, x < b"), encoding="utf-8")

    readback = load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")]
    assert not readback.shows_what_it_attests
    # the hashes still match the skeleton; only the displayed packet moved
    assert readback.status(_declaration()) == "invalid"
    (altered,) = readback_findings(
        _report(),
        load_readbacks(blueprint),
        article_ids={"basics/sup-unique": _ARTICLE_ID},
    )
    assert altered.code == "readback-altered" and readback.packet_hash in altered.reason


def test_testimony_for_a_declaration_the_blueprint_dropped_is_reported(tmp_path: Path) -> None:
    """A renamed statement must not leave testimony behind unmentioned."""

    blueprint = _blueprint(tmp_path)
    declaration = _declaration()
    write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="m",
        text="Fine.",
        packet_text=declaration.blind_text(),
    )
    renamed = DeclarationSkeleton(
        **{**{name: getattr(_declaration(), name) for name in _declaration().__slots__}, "name": "Skel.sup_unique'"}
    )

    findings = readback_findings(
        _report(renamed),
        load_readbacks(blueprint),
        article_ids={"basics/sup-unique": _ARTICLE_ID},
    )

    assert sorted(finding.code for finding in findings) == ["readback-missing", "readback-orphaned"]
    orphan = next(finding for finding in findings if finding.code == "readback-orphaned")
    assert orphan.declaration == "Skel.sup_unique" and "renamed or removed" in orphan.reason


def test_malformed_hashes_and_symlinked_cards_are_not_testimony(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _declaration()
    path = write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="m",
        text="Fine.",
        packet_text=declaration.blind_text(),
    )
    path.write_text(path.read_text(encoding="utf-8").replace(_declaration().hash, "approved"), encoding="utf-8")

    readback = load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")]
    assert readback.skeleton_hash is None and readback.status(_declaration()) == "invalid"
    (invalid,) = readback_findings(
        _report(),
        load_readbacks(blueprint),
        article_ids={"basics/sup-unique": _ARTICLE_ID},
    )
    assert invalid.code == "readback-invalid" and "malformed skeleton hash" in invalid.reason

    outside = tmp_path / "outside.md"
    outside.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    link = readback_path(blueprint, _ARTICLE_ID, "Skel.other")
    link.symlink_to(outside)
    assert ("basics/sup-unique", "Skel.other") not in load_readbacks(blueprint)
    # a stray temporary file is never a half-written card
    assert not path.with_name(path.name + ".tmp").exists()


@pytest.mark.parametrize(
    ("damage", "expected_reason"),
    [
        ("missing-schema", "missing frontmatter field 'schema'"),
        ("wrong-schema", "unsupported read-back schema"),
        ("unknown-field", "unknown frontmatter field 'surprise'"),
        ("duplicate-field", "frontmatter field 'model' appears more than once"),
        ("missing-packet", "no packet hash"),
        ("malformed-packet", "malformed packet hash"),
        ("missing-skeleton-block", "exactly one skeleton block"),
        ("empty-testimony", "nonempty read-back testimony"),
        ("empty-model", "nonempty model label"),
        ("wrong-article", "does not match the card path article_id"),
        ("wrong-declaration", "does not match the card path"),
    ],
)
def test_incomplete_or_misidentified_cards_are_explicitly_invalid(
    tmp_path: Path,
    damage: str,
    expected_reason: str,
) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _declaration()
    path = write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="m",
        text="Fine.",
        packet_text=declaration.blind_text(),
    )
    card = path.read_text(encoding="utf-8")
    if damage == "missing-schema":
        damaged = card.replace("schema: autoform-readback/v1\n", "")
    elif damage == "wrong-schema":
        damaged = card.replace("schema: autoform-readback/v1", "schema: future")
    elif damage == "unknown-field":
        damaged = card.replace("---\n", "---\nsurprise: value\n", 1)
    elif damage == "duplicate-field":
        damaged = card.replace('model: "m"\n', 'model: "m"\nmodel: "another"\n')
    elif damage == "missing-packet":
        damaged = card.replace(f"packet: {declaration.evidence_hash}\n", "")
    elif damage == "malformed-packet":
        damaged = card.replace(declaration.evidence_hash, "not-a-hash")
    elif damage == "missing-skeleton-block":
        start = card.index(SKELETON_HEADING)
        damaged = card[:start] + card[card.index("## Read-back", start) :]
    elif damage == "empty-testimony":
        damaged = card[: card.index("## Read-back") + len("## Read-back")] + "\n"
    elif damage == "empty-model":
        damaged = card.replace('model: "m"\n', "model:\n")
    elif damage == "wrong-article":
        damaged = card.replace(_ARTICLE_ID, "af_aaaaaaaaaaaaaaaaaaaaaaaa", 1)
    else:
        damaged = card.replace('declaration: "Skel.sup_unique"', 'declaration: "Skel.other"')
    path.write_text(damaged, encoding="utf-8")

    readback = load_readbacks(blueprint)[(_ARTICLE_ID, declaration.name)]
    assert not readback.valid
    assert readback.status(declaration) == "invalid"
    (finding,) = readback_findings(
        _report(),
        load_readbacks(blueprint),
        article_ids={"basics/sup-unique": _ARTICLE_ID},
    )
    assert finding.code == "readback-invalid"
    assert expected_reason in finding.reason


@pytest.mark.parametrize("recorded", ["Skel.other", "Skel." + "β" * 40])
def test_a_long_named_card_recording_another_declaration_is_invalid_for_the_one_its_path_names(
    tmp_path: Path, recorded: str
) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = replace(_declaration(), name="Skel." + "α" * 40)
    path = _file_card(blueprint, "Fine.", declaration=declaration)
    # Too long to spell in a filename, so the path names it only by a digest.
    assert path.name.startswith("declaration--")
    card = path.read_text(encoding="utf-8")
    path.write_text(card.replace(f'declaration: "{declaration.name}"', f'declaration: "{recorded}"'), encoding="utf-8")

    findings = readback_findings(
        _report(declaration), load_readbacks(blueprint), article_ids={"basics/sup-unique": _ARTICLE_ID}
    )
    # As for a short name: invalid for the declaration the writer would find it under, not missing.
    assert [(finding.declaration, finding.code) for finding in findings] == [(declaration.name, "readback-invalid")]
    assert f"frontmatter declaration {recorded!r} does not match the card path" in findings[0].reason


def test_a_card_copied_to_a_long_named_card_path_leaves_the_card_it_copies_valid(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "Fine.")
    stray = readback_path(blueprint, _ARTICLE_ID, "Skel." + "α" * 40)
    stray.write_bytes(path.read_bytes())

    loaded = load_readbacks(blueprint)
    assert loaded[(_ARTICLE_ID, "Skel.sup_unique")].valid
    (orphan,) = readback_findings(_report(), loaded, article_ids={"basics/sup-unique": _ARTICLE_ID})
    assert (orphan.code, orphan.declaration) == ("readback-orphaned", stray.stem)


def test_a_card_whose_suffix_differs_only_in_case_is_reported_for_the_declaration_its_path_names(
    tmp_path: Path,
) -> None:
    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "Fine.")
    variant = path.rename(path.with_suffix(".MD"))

    # On a volume that ignores case the writer reaches this file, so it is not missing.
    (finding,) = readback_findings(_report(), load_readbacks(blueprint), article_ids={"basics/sup-unique": _ARTICLE_ID})
    assert finding.code == "readback-invalid"
    assert f"does not match the card path {variant.relative_to(path.parent.parent).as_posix()!r}" in finding.reason


def test_card_body_rejects_interstitial_content(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _declaration()
    path = write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="m",
        text="Fine.",
        packet_text=declaration.blind_text(),
    )
    card = path.read_text(encoding="utf-8")
    path.write_text(
        card.replace("\n## Read-back\n", "\n## Unverified claim\n\nInjected.\n\n## Read-back\n"),
        encoding="utf-8",
    )

    readback = load_readbacks(blueprint)[(_ARTICLE_ID, declaration.name)]
    assert not readback.valid
    assert "immediately after the skeleton block" in "; ".join(readback.validation_errors)


def test_card_parser_ignores_readback_heading_inside_packet(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = replace(
        _declaration(),
        signature='Skel.sup_unique : String := "## Read-back"',
    )
    write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="m",
        text="The declaration returns the heading text.",
        packet_text=declaration.blind_text(),
    )

    readback = load_readbacks(blueprint)[(_ARTICLE_ID, declaration.name)]
    assert readback.valid
    assert readback.shown_text == declaration.blind_text()


def test_card_loader_rejects_crlf_rewrite_and_hashes_actual_bytes(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _declaration()
    path = write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="m",
        text="Fine.",
        packet_text=declaration.blind_text(),
    )
    rewritten = path.read_bytes().replace(b"\n", b"\r\n")
    path.write_bytes(rewritten)

    readback = load_readbacks(blueprint)[(_ARTICLE_ID, declaration.name)]
    assert not readback.valid
    assert "not in canonical" in "; ".join(readback.validation_errors)
    assert readback.file_hash == "sha256:" + hashlib.sha256(rewritten).hexdigest()


def test_writer_requires_the_actual_packet_and_nonempty_testimony(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _declaration()

    with pytest.raises(ValueError, match="packet does not match"):
        write_readback(
            blueprint,
            article_id=_ARTICLE_ID,
            declaration=declaration,
            model="m",
            text="Fine.",
            packet_text=declaration.blind_text().replace("≤", "<"),
        )
    with pytest.raises(ValueError, match="nonempty testimony"):
        write_readback(
            blueprint,
            article_id=_ARTICLE_ID,
            declaration=declaration,
            model="m",
            text=" \n",
            packet_text=declaration.blind_text(),
        )
    with pytest.raises(ValueError, match="nonempty model"):
        write_readback(
            blueprint,
            article_id=_ARTICLE_ID,
            declaration=declaration,
            model=" ",
            text="Fine.",
            packet_text=declaration.blind_text(),
        )
    assert not readback_path(blueprint, _ARTICLE_ID, declaration.name).exists()


def test_writer_refuses_symlinked_parent_and_ignores_predictable_stage_link(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _declaration()
    outside = tmp_path / "outside"
    outside.mkdir()
    readbacks = blueprint / "readbacks"
    readbacks.mkdir()
    (readbacks / _ARTICLE_ID).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        write_readback(
            blueprint,
            article_id=_ARTICLE_ID,
            declaration=declaration,
            model="m",
            text="Fine.",
            packet_text=declaration.blind_text(),
        )
    assert list(outside.iterdir()) == []

    (readbacks / _ARTICLE_ID).unlink()
    parent = readbacks / _ARTICLE_ID
    parent.mkdir()
    path = readback_path(blueprint, _ARTICLE_ID, declaration.name)
    valuable = outside / "valuable.md"
    valuable.write_text("keep\n", encoding="utf-8")
    predictable = path.with_name(path.name + ".tmp")
    predictable.symlink_to(valuable)

    write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="m",
        text="Fine.",
        packet_text=declaration.blind_text(),
    )
    assert valuable.read_text(encoding="utf-8") == "keep\n"
    assert predictable.is_symlink()


def test_concurrent_writers_do_not_silently_overwrite_each_other(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _declaration()

    def file(testimony: str) -> str:
        try:
            write_readback(
                blueprint,
                article_id=_ARTICLE_ID,
                declaration=declaration,
                model="m",
                text=testimony,
                packet_text=declaration.blind_text(),
            )
        except ValueError:
            return "refused"
        return "filed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(file, ("First testimony.", "Second testimony.")))

    assert sorted(results) == ["filed", "refused"]
    readback = load_readbacks(blueprint)[(_ARTICLE_ID, declaration.name)]
    assert readback.text in {"First testimony.", "Second testimony."}
    assert readback.valid


def test_compare_and_swap_preserves_a_concurrent_edit(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _declaration()
    path = write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="m",
        text="First.",
        packet_text=declaration.blind_text(),
    )
    expected = load_readbacks(blueprint)[(_ARTICLE_ID, declaration.name)].file_hash
    path.write_text(path.read_text(encoding="utf-8") + "Concurrent edit.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed before replacement"):
        write_readback(
            blueprint,
            article_id=_ARTICLE_ID,
            declaration=declaration,
            model="m",
            text="Replacement.",
            packet_text=declaration.blind_text(),
            expected_card_hash=expected,
        )
    assert path.read_text(encoding="utf-8").endswith("Concurrent edit.\n")


def _file_card(blueprint: Path, text: str, **options) -> Path:
    declaration = options.pop("declaration", _declaration())
    return write_readback(
        blueprint,
        article_id=options.pop("article_id", _ARTICLE_ID),
        declaration=declaration,
        model="m",
        text=text,
        packet_text=declaration.blind_text(),
        **options,
    )


def _staged_names(directory: Path) -> list[str]:
    return [name for name in os.listdir(directory) if name.startswith(".autoform-readback-")]


def test_the_card_path_never_goes_empty_while_a_card_is_replaced(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "First.")
    expected = load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].file_hash
    declaration = _declaration()
    create_only = prepare_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="m",
        text="Create-only.",
        packet_text=declaration.blind_text(),
    )
    seen: dict[str, object] = {}
    replace_file = os.replace

    # Just before and just after the new card is renamed over the old, a
    # reader looks, and a create-only writer checks and tries to publish.
    def look() -> None:
        loaded = load_readbacks(blueprint).get((_ARTICLE_ID, "Skel.sup_unique"))
        seen.setdefault("loaded", []).append(None if loaded is None or not loaded.valid else loaded.text)
        seen.setdefault("conflicts", []).append(readback_conflicts([create_only]))
        try:
            publish_readback(create_only)
        except ValueError as exc:
            seen.setdefault("create-only", []).append(str(exc))
        else:
            seen.setdefault("create-only", []).append("published")

    def look_around_the_rename(*args, **kwargs) -> None:
        look()
        replace_file(*args, **kwargs)
        look()

    monkeypatch.setattr(os, "replace", look_around_the_rename)
    monkeypatch.setattr("autoform_cli.readback._LOCK_TIMEOUT", 0.05, raising=False)

    assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
    assert seen["loaded"] == ["First.", "Replacement."]
    assert all(seen["conflicts"])
    assert len(seen["create-only"]) == 2 and all("did not finish" in message for message in seen["create-only"])
    assert load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].text == "Replacement."
    assert _staged_names(path.parent) == []


def _crash_publication(tmp_path: Path, repo_root: Path, card: object, *, rename: str) -> None:
    """Publish ``card`` in a child process that dies just ``rename`` (before or after) it renames the card into place."""

    prepared = tmp_path / "prepared.pickle"
    prepared.write_bytes(pickle.dumps(card))
    script = (
        "import os, pickle, sys\n"
        "from autoform_cli import readback\n"
        "replace_file = os.replace\n"
        "def die(*args, **kwargs):\n"
        "    if sys.argv[2] == 'after':\n"
        "        replace_file(*args, **kwargs)\n"
        "    os._exit(17)\n"
        "os.replace = die\n"
        "with open(sys.argv[1], 'rb') as stream:\n"
        "    readback.publish_readback(pickle.load(stream))\n"
    )
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(prepared), rename],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (str(repo_root), os.environ.get("PYTHONPATH"))))},
        check=False,
    )
    assert crashed.returncode == 17


def _replacement(blueprint: Path, expected: str):
    declaration = _declaration()
    return prepare_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="m",
        text="Replacement.",
        packet_text=declaration.blind_text(),
        expected_card_hash=expected,
    )


def test_a_crash_mid_publication_leaves_nothing_that_blocks_a_retry(tmp_path: Path, repo_root: Path) -> None:
    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "First.")
    original = path.read_bytes()
    card = _replacement(blueprint, load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].file_hash)

    _crash_publication(tmp_path, repo_root, card, rename="before")

    assert path.read_bytes() == original
    (leftover,) = _staged_names(path.parent)
    assert not leftover.endswith(".md") and path.with_name(leftover).read_bytes() == card.content.encode("utf-8")
    assert load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].text == "First."
    assert publish_readback(card) == path
    assert load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].text == "Replacement."


def test_a_crash_after_the_rename_leaves_the_new_card_in_place(tmp_path: Path, repo_root: Path) -> None:
    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "First.")
    card = _replacement(blueprint, load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].file_hash)

    _crash_publication(tmp_path, repo_root, card, rename="after")

    assert load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].text == "Replacement."
    assert _staged_names(path.parent) == []


def test_publishers_of_the_same_card_both_succeed(tmp_path: Path, monkeypatch) -> None:
    fcntl = pytest.importorskip("fcntl")
    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "First.")
    expected = load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].file_hash
    waiting = threading.Event()
    results: list[str] = []

    def flock(descriptor: int, operation: int) -> None:
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError:
            waiting.set()
            raise

    def file() -> None:
        try:
            _file_card(blueprint, "Replacement.", expected_card_hash=expected)
        except ValueError as exc:
            results.append(str(exc))
        else:
            results.append("filed")

    second = threading.Thread(target=file)
    replace_file = os.replace

    # The second publisher starts just before the first renames its card into
    # place, and the first goes on once the second finishes or waits.
    def start_then_rename(*args, **kwargs) -> None:
        if second.ident is None:
            second.start()
            for _ in range(500):
                if waiting.is_set() or not second.is_alive():
                    break
                second.join(0.01)
        replace_file(*args, **kwargs)

    monkeypatch.setattr(os, "replace", start_then_rename)
    locking = SimpleNamespace(LOCK_EX=fcntl.LOCK_EX, LOCK_NB=fcntl.LOCK_NB, LOCK_UN=fcntl.LOCK_UN, flock=flock)
    monkeypatch.setattr("autoform_cli.readback.fcntl", locking, raising=False)

    assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
    second.join()
    assert results == ["filed"]
    assert load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].text == "Replacement."
    assert _staged_names(path.parent) == []


def test_an_interruption_before_the_rename_leaves_the_card_and_no_staging_file(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "First.")
    original = path.read_bytes()
    expected = load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].file_hash

    def interrupt(*args, **kwargs) -> None:
        raise KeyboardInterrupt

    # The interrupt lands as the new card is about to be renamed into place.
    monkeypatch.setattr(os, "replace", interrupt)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(KeyboardInterrupt):
            _file_card(blueprint, "Replacement.", expected_card_hash=expected)
    assert [str(warning.message) for warning in caught] == []
    assert path.read_bytes() == original
    assert _staged_names(path.parent) == []


def test_a_directory_at_the_card_path_is_left_there_and_refused(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    path = readback_path(blueprint, _ARTICLE_ID, "Skel.sup_unique")
    path.mkdir(parents=True)
    (path / "notes.txt").write_text("keep\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not a regular file"):
        _file_card(blueprint, "Replacement.", expected_card_hash="sha256:" + "1" * 64)
    assert (path / "notes.txt").read_text(encoding="utf-8") == "keep\n"
    assert _staged_names(path.parent) == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_at_the_card_path_is_refused_without_blocking(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    path = readback_path(blueprint, _ARTICLE_ID, "Skel.sup_unique")
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    refusals: list[str] = []

    def file() -> None:
        try:
            _file_card(blueprint, "First.")
        except ValueError as exc:
            refusals.append(str(exc))

    publisher = threading.Thread(target=file)
    publisher.start()
    publisher.join(5)
    blocked = publisher.is_alive()
    if blocked:
        os.close(os.open(path, os.O_WRONLY))
        publisher.join()
    assert not blocked
    assert len(refusals) == 1 and "not a regular file" in refusals[0]
    assert stat.S_ISFIFO(os.lstat(path).st_mode)
    assert _staged_names(path.parent) == []


def _swap_cards_after_listing(tmp_path: Path, monkeypatch) -> Path:
    """A vault in which, once the tree is listed, a card and a card directory
    become links to valid cards outside it; one other card is left alone."""

    blueprint = _blueprint(tmp_path)
    elsewhere = _blueprint(tmp_path / "elsewhere")
    other_article = "af_fedcba9876543210fedcba98"
    _file_card(blueprint, "Untouched.", declaration=replace(_declaration(), name="Skel.kept"))
    card = _file_card(blueprint, "Inside.")
    directory = _file_card(blueprint, "Inside.", article_id=other_article).parent
    outside_card = _file_card(elsewhere, "Outside.")
    outside_directory = _file_card(elsewhere, "Outside.", article_id=other_article).parent
    walk = os.walk

    def list_then_swap(top, *args, **kwargs):
        listed = list(walk(top, *args, **kwargs))
        card.unlink()
        card.symlink_to(outside_card)
        directory.rename(directory.with_name("replaced"))
        directory.symlink_to(outside_directory, target_is_directory=True)
        return iter(listed)

    monkeypatch.setattr(os, "walk", list_then_swap)
    return blueprint


def test_loading_never_follows_a_card_or_directory_swapped_for_a_symlink(tmp_path: Path, monkeypatch) -> None:
    blueprint = _swap_cards_after_listing(tmp_path, monkeypatch)
    # Every path-level check still sees what was there before the swap.
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)

    loaded = {key: readback.text for key, readback in load_readbacks(blueprint).items()}

    assert loaded == {(_ARTICLE_ID, "Skel.kept"): "Untouched."}


def _place_card(blueprint: Path, text: str, **options) -> Path:
    """Write a card as a checkout would, without publishing it."""

    declaration = options.pop("declaration", _declaration())
    card = prepare_readback(
        blueprint,
        article_id=options.pop("article_id", _ARTICLE_ID),
        declaration=declaration,
        model="m",
        text=text,
        packet_text=declaration.blind_text(),
    )
    card.path.parent.mkdir(parents=True, exist_ok=True)
    card.path.write_bytes(card.content.encode("utf-8"))
    return card.path


def test_loading_without_no_follow_descriptors_refuses_a_link_present_only_while_opening(
    tmp_path: Path, monkeypatch
) -> None:
    # As on Windows: the loader opens each card by path. A card and a card
    # directory are links only during that open, so every check before or after
    # it sees the vault's own files; the file opened must still be the one named.
    blueprint = _blueprint(tmp_path)
    elsewhere = _blueprint(tmp_path / "elsewhere")
    other_article = "af_fedcba9876543210fedcba98"
    _place_card(blueprint, "Untouched.", declaration=replace(_declaration(), name="Skel.kept"))
    card = _place_card(blueprint, "Inside.")
    directory = _place_card(blueprint, "Inside.", article_id=other_article).parent
    outside_card = _place_card(elsewhere, "Outside.")
    outside_directory = _place_card(elsewhere, "Outside.", article_id=other_article).parent
    try:
        (tmp_path / "link").symlink_to(outside_card)
    except OSError:
        pytest.skip("this system cannot create symlinks")
    open_path = os.open
    swapped: list[Path] = []

    def open_through_a_link(path, flags, *args, **kwargs):
        target = Path(path)
        if target.parts[-2:] == card.parts[-2:]:
            name, held = card, card.with_name("held")
            name.rename(held)
            name.symlink_to(outside_card)
        elif target.parent.name == other_article:
            name, held = directory, directory.with_name("held")
            name.rename(held)
            name.symlink_to(outside_directory, target_is_directory=True)
        else:
            return open_path(path, flags, *args, **kwargs)
        swapped.append(name)
        try:
            return open_path(path, flags, *args, **kwargs)
        finally:
            name.unlink()
            held.rename(name)

    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(os, "open", open_through_a_link)

    loaded = {key: readback.text for key, readback in load_readbacks(blueprint).items()}

    assert swapped == [card, directory]
    assert loaded == {(_ARTICLE_ID, "Skel.kept"): "Untouched."}


def test_case_distinct_declarations_have_distinct_card_paths(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    base = _declaration()
    upper = replace(base, name="Foo.x")
    lower = replace(base, name="foo.x")

    paths = [
        write_readback(
            blueprint,
            article_id=_ARTICLE_ID,
            declaration=declaration,
            model="m",
            text=f"Testimony for {declaration.name}.",
            packet_text=declaration.blind_text(),
        )
        for declaration in (upper, lower)
    ]

    assert paths[0] != paths[1]
    assert paths[0].name.casefold() != paths[1].name.casefold()
    assert set(load_readbacks(blueprint)) == {
        (_ARTICLE_ID, "Foo.x"),
        (_ARTICLE_ID, "foo.x"),
    }


def test_card_identity_survives_a_roadmap_move(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _declaration()
    card = write_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="m",
        text="Stable testimony.",
        packet_text=declaration.blind_text(),
    )
    article = blueprint / "roadmap" / "basics" / "sup-unique.md"
    moved = blueprint / "roadmap" / "renamed" / "result.md"
    moved.parent.mkdir()
    article.rename(moved)

    assert card == readback_path(blueprint, _ARTICLE_ID, declaration.name)
    assert load_readbacks(blueprint)[(_ARTICLE_ID, declaration.name)].text == "Stable testimony."
