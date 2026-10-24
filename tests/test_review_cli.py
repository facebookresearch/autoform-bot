from __future__ import annotations

import html
import json
import os
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli import __main__ as cli, render
from autoform_cli.__main__ import _print_refusal, main
from autoform_cli.graph import load_graph
from autoform_cli.markdown import site_converter
from autoform_cli.readback import load_readbacks
from autoform_cli.lean import declaration_names
from autoform_cli.render import render_site
from autoform_cli.review import REVIEW_PACKET_SCHEMA, ReviewError, ReviewFinding
from autoform_cli.skeleton import (
    NodeSkeleton,
    SkeletonReport,
    UnresolvedTarget,
    _article_path,
    blueprint_hash,
    source_passage,
)
from tests.skeleton_fixtures import declaration as fixture_declaration


_RESULT_ID = "af_0123456789abcdef01234567"
_OTHER_ID = "af_fedcba9876543210fedcba98"
_NEEDS_FIFO = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")


def _blueprint(root: Path) -> Path:
    blueprint = root / "blueprint"
    chapter = blueprint / "roadmap" / "basics"
    chapter.mkdir(parents=True)
    (blueprint / "README.md").write_text("# Blueprint\n", encoding="utf-8")
    (blueprint / "roadmap" / "README.md").write_text("# Roadmap\n\n- [Basics](basics/README.md)\n", encoding="utf-8")
    (chapter / "README.md").write_text("# Basics\n\n- [Result](result.md)\n- [Other](other.md)\n", encoding="utf-8")
    for name, article_id, title, statement in (
        ("result", _RESULT_ID, "Result", "Every object is equal to itself."),
        ("other", _OTHER_ID, "Other", "Truth holds."),
    ):
        (chapter / f"{name}.md").write_text(
            f"---\narticle_id: {article_id}\ndeclaration: theorem\nlean: Review.{name}\nstatement: formalized\n---\n\n"
            f"# {title}\n\n{statement}\n\n## Depends on\n\nNone.\n",
            encoding="utf-8",
        )
    coverage = blueprint / "coverage" / "README.md"
    coverage.parent.mkdir()
    coverage.write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n| Scope | OUT | Test fixture |\n",
        encoding="utf-8",
    )
    return blueprint


class _Extraction:
    """A fake extraction of both articles, stamped with the blueprint as it reads
    it, that records each call's keywords; ``on_extract`` runs first, so an edit
    made there lands before the extraction's own snapshot."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.on_extract: Callable[[], None] | None = None
        self.signatures: dict[str, str] = {}

    def __call__(self, blueprint_dir: object, **kwargs: object) -> SkeletonReport:
        self.calls.append(kwargs)
        if self.on_extract is not None:
            self.on_extract()
        nodes = tuple(
            NodeSkeleton(
                node_id=f"basics/{name}",
                article_path=f"roadmap/basics/{name}.md",
                declarations=(self._declaration(f"Review.{name}"),),
            )
            for name in ("other", "result")
        )
        return SkeletonReport(
            blueprint_hash=blueprint_hash(load_graph(blueprint_dir)),
            targets=tuple((node.node_id, (node.declarations[0].name,)) for node in nodes),
            selection="all",
            selected_nodes=tuple(node.node_id for node in nodes),
            nodes=nodes,
            unresolved=(),
        )

    def _declaration(self, name: str):
        signature = self.signatures.get(name)
        if signature is None:
            return fixture_declaration(name)
        return fixture_declaration(name, signature=signature, raw_signature=signature, semantic='{"root":"restated"}')


@pytest.fixture
def extraction(monkeypatch: pytest.MonkeyPatch) -> _Extraction:
    fake = _Extraction()
    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", fake)
    return fake


def _whole_extraction(blueprint_dir: object, **kwargs: object) -> SkeletonReport:
    """A fake extraction of every Lean-mapped article in the vault, a folder
    that is not published included, each passage cut as the real one cuts it."""

    graph = load_graph(blueprint_dir)
    nodes = []
    for node in sorted(graph.nodes.values(), key=lambda item: item.id):
        if node.lean is not None:
            passage, locator = source_passage(node, graph.blueprint_dir)
            nodes.append(
                NodeSkeleton(
                    node_id=node.id,
                    article_path=_article_path(node, graph),
                    declarations=tuple(fixture_declaration(name) for name in declaration_names(node.lean)),
                    passage=passage,
                    passage_locator=locator,
                )
            )
    return SkeletonReport(
        blueprint_hash=blueprint_hash(graph),
        targets=tuple((item.node_id, tuple(declaration.name for declaration in item.declarations)) for item in nodes),
        selection="all",
        selected_nodes=tuple(item.node_id for item in nodes),
        nodes=tuple(nodes),
        unresolved=(),
    )


@pytest.fixture
def whole_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", _whole_extraction)


def _prepare(blueprint: Path, root: Path, *extra: str) -> dict[str, dict[str, str]]:
    """Run `review prepare` and write a testimony for each packet; return the
    flags `review record` takes for each, keyed by declaration."""

    packets = root / "packets"
    assert main(["review", "prepare", str(blueprint), "--lean-root", str(root), "--packets", str(packets), *extra]) == 0
    records = {}
    for entry in json.loads((packets / "manifest.json").read_text(encoding="utf-8"))["packets"]:
        testimony = root / f"{entry['declaration']}.txt"
        testimony.write_text(f"The statement {entry['declaration']} asserts True.\n", encoding="utf-8")
        records[entry["declaration"]] = {
            "packets": str(packets),
            "article_id": entry["article_id"],
            "declaration": entry["declaration"],
            "testimony": str(testimony),
        }
    return records


def _record(blueprint: Path, record: dict[str, str]) -> int:
    expected = ["--expected-card-hash", record["expected_card_hash"]] if "expected_card_hash" in record else []
    return main(
        ["review", "record", str(blueprint), "--packets", record["packets"], "--model", "test-model",
         "--article-id", record["article_id"], "--declaration", record["declaration"],
         "--testimony", record["testimony"], *expected]
    )


def _check(blueprint: Path, root: Path, *extra: str) -> int:
    return main(["review", "check", str(blueprint), "--lean-root", str(root), *extra])


def _render(blueprint: Path, root: Path, *extra: str) -> int:
    return main(["render", str(blueprint), "--lean-root", str(root), "--review", "-o", str(root / "site"), *extra])


def _approve(article: Path, approval: str) -> None:
    """Set the article's ``review_approved`` to ``approval``. The edit must change the article."""

    text = article.read_text(encoding="utf-8")
    if "review_approved: " in text:
        edited = re.sub(r"review_approved: \S+", f"review_approved: {approval}", text)
    else:
        edited = text.replace("statement: formalized\n", f"statement: formalized\nreview_approved: {approval}\n")
    assert edited != text
    article.write_text(edited, encoding="utf-8")


def _approve_as_printed(blueprint: Path, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Copy into each article the hash `review check` says to approve, as a person would."""

    capsys.readouterr()
    assert _check(blueprint, root, "--json") == 1
    for finding in json.loads(capsys.readouterr().out)["findings"]:
        assert finding["code"] == "review-unapproved"
        _approve(blueprint / "roadmap" / f"{finding['node_id']}.md", finding["reason"].rsplit(" ", 1)[1])


def _approved_articles(root: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """Record and approve both articles, so that `review check` passes."""

    blueprint = _blueprint(root)
    for record in _prepare(blueprint, root).values():
        assert _record(blueprint, record) == 0
    _approve_as_printed(blueprint, root, capsys)
    return blueprint


def test_review_cli_prepares_records_and_checks_exact_evidence(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = _blueprint(tmp_path)
    records = _prepare(blueprint, tmp_path, "--timeout", "1800")
    manifest = json.loads((tmp_path / "packets" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == REVIEW_PACKET_SCHEMA
    entry = next(item for item in manifest["packets"] if item["declaration"] == "Review.result")
    packet = tmp_path / "packets" / entry["packet"]
    assert packet.parent.name == "blind"
    assert packet.name == fixture_declaration("Review.result").evidence_hash.removeprefix("sha256:") + ".lean"
    assert "Review" not in packet.name and "basics" not in packet.as_posix()
    assert capsys.readouterr().out == f"{tmp_path / 'packets'}: 2 blind packet(s) for 2 review article(s)\n"

    for record in records.values():
        assert _record(blueprint, record) == 0
    assert ": recorded read-back for Review.result\n" in capsys.readouterr().out
    cards = load_readbacks(blueprint)
    assert cards[(_RESULT_ID, "Review.result")].shown_text == packet.read_text(encoding="utf-8")
    # Filing identical content writes nothing, so the same record runs again cleanly.
    assert _record(blueprint, records["Review.result"]) == 0
    assert load_readbacks(blueprint) == cards

    assert _check(blueprint, tmp_path, "--timeout", "1800") == 1
    assert "review-unapproved" in capsys.readouterr().out
    _approve_as_printed(blueprint, tmp_path, capsys)
    assert _check(blueprint, tmp_path, "--timeout", "1800") == 0
    assert "OK: 2 statement review(s) complete and current\n" in capsys.readouterr().out
    assert _render(blueprint, tmp_path, "--timeout", "1800") == 0
    assert "The statement Review.result asserts True." in (tmp_path / "site/roadmap/basics/README.md").read_text(
        encoding="utf-8"
    )
    # The record runs no Lean: one extraction per prepare, per check, and per render.
    assert [call.get("timeout") for call in extraction.calls] == [1800.0, 1800.0, None, 1800.0, 1800.0]


@pytest.mark.parametrize(
    ("command", "missing"),
    [
        (["review", "prepare", "blueprint", "--lean-root", "."], "--packets"),
        (["review", "record", "blueprint", "--packets", "p", "--article-id", _RESULT_ID, "--declaration", "R",
          "--model", "m"], "--testimony"),
    ],
)
def test_review_commands_need_their_flags(
    command: list[str], missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as refused:
        main(command)
    assert refused.value.code == 2
    assert f"the following arguments are required: {missing}" in capsys.readouterr().err


def test_render_review_needs_a_lean_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["render", str(_blueprint(tmp_path)), "--review", "-o", str(tmp_path / "site")]) == 2
    assert "--review requires --lean-root" in capsys.readouterr().err


def test_check_and_render_refuse_the_blank_passage_prepare_refuses(
    tmp_path: Path, extraction: _Extraction, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An article not marked cited may cite only blank lines; prepare refuses
    that passage, and so do check and render, whatever the cards and approval
    say."""

    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap/basics/result.md"
    article.write_text(
        article.read_text(encoding="utf-8") + "\n## Sources\n\n[Book](sources/book.txt#L2-L2)\n", encoding="utf-8"
    )
    source = blueprint / "roadmap/basics/sources/book.txt"
    source.parent.mkdir()
    source.write_text("Heading\n   \n", encoding="utf-8")

    def with_the_blank_passage(blueprint_dir: object, **kwargs: object) -> SkeletonReport:
        report = extraction(blueprint_dir, **kwargs)
        cited = replace(report.nodes[1], passage="   ", passage_locator="roadmap/basics/sources/book.txt#L2-L2")
        return replace(report, nodes=(report.nodes[0], cited))

    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", with_the_blank_passage)

    prepare = ["review", "prepare", str(blueprint), "--lean-root", str(tmp_path), "--packets", str(tmp_path / "p")]
    assert main(prepare) == 2
    refused = capsys.readouterr().err
    assert "error: basics/result: review-source-passage-missing: Lean article has no exact local source passage, or a blank one" in refused
    assert _check(blueprint, tmp_path) == 2
    captured = capsys.readouterr()
    assert captured.err == refused and captured.out == ""
    assert _render(blueprint, tmp_path) == 1
    assert capsys.readouterr().out == refused
    assert not (tmp_path / "site").exists()


def test_a_record_from_a_stale_packet_is_filed_and_then_reported_invalid(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whether a card is current is the check's to judge, against its own
    extraction; a record re-prepared from the current packets replaces it."""

    blueprint = _blueprint(tmp_path)
    records = _prepare(blueprint, tmp_path)
    extraction.signatures["Review.result"] = "Review.result : False"
    assert _record(blueprint, records["Review.result"]) == 0

    capsys.readouterr()
    assert _check(blueprint, tmp_path, "--json") == 1
    findings = json.loads(capsys.readouterr().out)["findings"]
    assert [(item["node_id"], item["code"]) for item in findings] == [
        ("basics/other", "readback-missing"),
        ("basics/result", "readback-invalid"),
    ]
    assert findings[1]["reason"] == "read-back for Review.result is invalid: packet hash does not match the prepared declaration"

    record = _prepare(blueprint, tmp_path)["Review.result"]
    record["expected_card_hash"] = load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].file_hash
    assert _record(blueprint, record) == 0
    capsys.readouterr()
    assert _check(blueprint, tmp_path, "--json") == 1
    findings = json.loads(capsys.readouterr().out)["findings"]
    assert [(item["node_id"], item["code"]) for item in findings] == [
        ("basics/other", "readback-missing"),
        ("basics/result", "review-unapproved"),
    ]


def _gone_article(blueprint: Path, record: dict[str, str]) -> None:
    article = blueprint / "roadmap/basics/result.md"
    article.write_text(article.read_text(encoding="utf-8").replace(_RESULT_ID, "af_" + "9" * 24), encoding="utf-8")


def _testimony(content: bytes | None) -> Callable[[Path, dict[str, str]], None]:
    def damage(blueprint: Path, record: dict[str, str]) -> None:
        path = Path(record["testimony"])
        path.unlink()
        if content is not None:
            path.write_bytes(content)

    return damage


def _testimony_fifo(blueprint: Path, record: dict[str, str]) -> None:
    path = Path(record["testimony"])
    path.unlink()
    os.mkfifo(path)


def _alter_packet(blueprint: Path, record: dict[str, str]) -> None:
    for packet in (Path(record["packets"]) / "blind").iterdir():
        packet.write_text(packet.read_text(encoding="utf-8") + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("damage", "error"),
    [
        (_gone_article, f"error: current graph has no article with article_id {_RESULT_ID}\n"),
        (_testimony(None), "error: Review.result: cannot read the testimony "),
        pytest.param(_testimony_fifo, "error: Review.result: cannot read the testimony ", marks=_NEEDS_FIFO),
        (_testimony(b"\xff\n"), "error: Review.result: cannot read the testimony "),
        (_testimony("Zero\u200bwidth.\n".encode()), "error: Review.result: unsafe read-back testimony: "),
        (_alter_packet, "error: Review.result: read-back packet does not match its packet hash for Review.result\n"),
    ],
    ids=["article-gone", "testimony-missing", "testimony-fifo", "testimony-not-utf8", "unsafe-testimony",
         "packet-altered"],
)
def test_a_record_refuses_without_filing(
    tmp_path: Path,
    extraction: _Extraction,
    capsys: pytest.CaptureFixture[str],
    damage: Callable[[Path, dict[str, str]], None],
    error: str,
) -> None:
    blueprint = _blueprint(tmp_path)
    record = _prepare(blueprint, tmp_path)["Review.result"]
    damage(blueprint, record)
    capsys.readouterr()

    assert _record(blueprint, record) == 2

    captured = capsys.readouterr()
    assert captured.err.startswith(error) and captured.out == ""
    assert load_readbacks(blueprint) == {}
    assert len(extraction.calls) == 1  # only `review prepare` extracted


def test_a_record_files_a_formula_the_site_shows_as_typed_and_warns_about_it(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = _blueprint(tmp_path)
    record = _prepare(blueprint, tmp_path)["Review.result"]
    Path(record["testimony"]).write_text("It asserts $\\phantom{x}True$.\n", encoding="utf-8")
    capsys.readouterr()

    assert _record(blueprint, record) == 0

    assert capsys.readouterr().out.endswith(
        ": recorded read-back for Review.result\n"
        "warning: Review.result: the formula $\\phantom{x}True$ is shown as typed: "
        "TeX outside the read-back allowlist: \\phantom\n"
    )
    assert load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].valid


def test_a_testimony_ends_a_line_at_crlf_or_cr_as_at_lf(tmp_path: Path, extraction: _Extraction) -> None:
    """Read as a text file is, so the card says the same whichever line endings the file was saved with."""

    blueprint = _blueprint(tmp_path)
    record = _prepare(blueprint, tmp_path)["Review.result"]
    Path(record["testimony"]).write_bytes(b"The statement\r\nReview.result\rasserts True.\r\n")

    assert _record(blueprint, record) == 0

    card = load_readbacks(blueprint)[(_RESULT_ID, "Review.result")]
    assert card.text == "The statement\nReview.result\nasserts True."
    assert b"\r" not in card.path.read_bytes()


def test_a_record_replaces_only_the_card_it_names(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = _blueprint(tmp_path)
    record = _prepare(blueprint, tmp_path)["Review.result"]
    assert _record(blueprint, record) == 0
    key = (_RESULT_ID, "Review.result")
    earlier = load_readbacks(blueprint)[key]
    Path(record["testimony"]).write_text("A different reading.\n", encoding="utf-8")
    capsys.readouterr()

    assert _record(blueprint, record) == 2
    assert "Review.result: read-back already exists with different content" in capsys.readouterr().err
    assert load_readbacks(blueprint)[key] == earlier

    record["expected_card_hash"] = earlier.file_hash
    assert _record(blueprint, record) == 0
    assert load_readbacks(blueprint)[key].text == "A different reading."


def test_a_failed_write_stops_the_record_until_its_cause_is_cleared(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("permissions do not bind root")
    blueprint = _blueprint(tmp_path)
    record = _prepare(blueprint, tmp_path)["Review.result"]
    cards = blueprint / "readbacks"
    cards.mkdir()
    cards.chmod(0o555)
    try:
        capsys.readouterr()
        assert _record(blueprint, record) == 2
        assert f"{cards / _RESULT_ID}: Permission denied" in capsys.readouterr().err
        assert load_readbacks(blueprint) == {}
    finally:
        cards.chmod(0o755)

    assert _record(blueprint, record) == 0
    assert set(load_readbacks(blueprint)) == {(_RESULT_ID, "Review.result")}


def test_unlistable_directories_are_errors_not_missing_articles_or_cards(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("permissions do not bind root")
    blueprint = _approved_articles(tmp_path, capsys)
    record = _prepare(blueprint, tmp_path)["Review.result"]
    cards = (blueprint / "readbacks" / _RESULT_ID).resolve()
    chapter = (blueprint / "roadmap" / "basics").resolve()
    capsys.readouterr()

    cards.chmod(0)
    try:
        assert _check(blueprint, tmp_path) == 2
    finally:
        cards.chmod(0o755)
    captured = capsys.readouterr()
    assert f"error: readback-unlistable: cannot list read-back directory {cards}: Permission denied" in captured.err
    assert "no read-back filed" not in captured.err and "OK:" not in captured.out

    chapter.chmod(0)
    try:
        assert _record(blueprint, record) == 2
        recorded = capsys.readouterr().err
        assert _check(blueprint, tmp_path) == 2
        checked = capsys.readouterr().err
    finally:
        chapter.chmod(0o755)
    for err in (recorded, checked):
        assert f"error: cannot list roadmap directory {chapter}: Permission denied\n" in err
        assert "no article with article_id" not in err


# --------------------------------------------------------------------------- #
# One snapshot per check
# --------------------------------------------------------------------------- #


def test_check_judges_the_blueprint_its_extraction_read(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    """An approval changed after the check loaded the graph, but before the
    extraction took its snapshot, must not pass on the strength of the old one."""

    blueprint = _approved_articles(tmp_path, capsys)
    assert _check(blueprint, tmp_path) == 0
    capsys.readouterr()
    article = blueprint / "roadmap" / "basics" / "result.md"

    extraction.on_extract = lambda: _approve(article, "sha256:" + "f" * 64)
    assert _check(blueprint, tmp_path) == 2
    captured = capsys.readouterr()
    assert "the blueprint changed after its review evidence was extracted" in captured.err
    assert "OK:" not in captured.out

    # The tree does hold an invalid approval, which a check of an idle tree names.
    extraction.on_extract = None
    assert _check(blueprint, tmp_path) == 1
    assert "review-drift" in capsys.readouterr().out


def _edit_card(card: Path) -> None:
    card.write_text(card.read_text(encoding="utf-8").replace("asserts True.", "asserts nothing."), encoding="utf-8")


def _add_card(card: Path) -> None:
    card.with_name("copy-" + card.name).write_bytes(card.read_bytes())


@pytest.mark.parametrize("change", [_edit_card, _add_card, Path.unlink], ids=["edited", "added", "removed"])
def test_check_refuses_a_readback_changed_during_extraction(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str], change: Callable[[Path], None]
) -> None:
    """Cards are read before Lean runs and must be the same after it: the check
    judges neither the old cards nor the new ones against this extraction."""

    blueprint = _approved_articles(tmp_path, capsys)
    card = load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].path
    extraction.on_extract = lambda: change(card)
    capsys.readouterr()

    assert _check(blueprint, tmp_path) == 2

    captured = capsys.readouterr()
    assert "a read-back changed while the review evidence was being extracted" in captured.err
    assert captured.out == ""


def _once_the_cards_are_rechecked(monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]) -> None:
    """Run ``change`` once, right after a check reads the cards the second time.

    From there on the check compares only the cited sources and the Lean-mapped
    articles with what it read first, and judges everything else as it read it.
    """

    reads: list[object] = []

    def read_then_change(*args: object, **kwargs: object) -> object:
        cards = load_readbacks(*args, **kwargs)
        reads.append(args)
        if len(reads) == 2:
            change()
        return cards

    monkeypatch.setattr("autoform_cli.__main__.load_readbacks", read_then_change)


def _write_statement(article: Path, statement: str) -> None:
    text = article.read_text(encoding="utf-8")
    article.write_text(re.sub(r"(# Result\n\n)[^\n]*", lambda match: match.group(1) + statement, text), encoding="utf-8")


def test_check_refuses_a_statement_edited_after_its_snapshot(
    tmp_path: Path, extraction: _Extraction, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A statement edited after the graph parsed it fails the check as a changed
    snapshot. Here the tree fails the check before the edit and after it, but a
    check judging the restored statement would pair it with the old approval, a
    state that was never on disk, and print OK."""

    blueprint = _approved_articles(tmp_path, capsys)
    article = blueprint / "roadmap" / "basics" / "result.md"
    _write_statement(article, "Every object is different from itself.")
    capsys.readouterr()
    assert _check(blueprint, tmp_path) == 1
    assert "review-drift" in capsys.readouterr().out

    def restore_the_statement_and_approve_something_else() -> None:
        _write_statement(article, "Every object is equal to itself.")
        _approve(article, "sha256:" + "e" * 64)

    _once_the_cards_are_rechecked(monkeypatch, restore_the_statement_and_approve_something_else)
    assert _check(blueprint, tmp_path) == 2
    captured = capsys.readouterr()
    assert "review-snapshot-changed" in captured.err and "OK:" not in captured.out


def test_a_refusal_about_no_one_article_names_none(capsys: pytest.CaptureFixture[str]) -> None:
    reason = "the blueprint changed after its review evidence was extracted"
    _print_refusal(ReviewError([ReviewFinding("", "review-snapshot-changed", reason), ReviewFinding("a/b", "c", "d")]))

    assert capsys.readouterr().err == f"error: review-snapshot-changed: {reason}\nerror: a/b: c: d\n"


def _edit_chapter_page(blueprint: Path) -> None:
    page = blueprint / "roadmap" / "basics" / "README.md"
    page.write_text(page.read_text(encoding="utf-8") + "\nMore prose.\n", encoding="utf-8")


def _edit_result_card(blueprint: Path) -> None:
    _edit_card(load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].path)


@pytest.mark.parametrize("change", [_edit_chapter_page, _edit_result_card], ids=["page", "card"])
def test_check_judges_the_graph_and_cards_it_compared(
    tmp_path: Path,
    extraction: _Extraction,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: Callable[[Path], None],
) -> None:
    """No comparison left reads the cards or a page no review reads, so an edit
    landing now, to such a page or to a card, changes nothing: the verdict is
    about the graph and cards the check read first."""

    blueprint = _approved_articles(tmp_path, capsys)
    _once_the_cards_are_rechecked(monkeypatch, lambda: change(blueprint))
    capsys.readouterr()

    assert _check(blueprint, tmp_path) == 0
    assert "OK:" in capsys.readouterr().out


def test_a_pasted_current_hash_is_only_self_approved(
    tmp_path: Path, extraction: _Extraction, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The blocker: `review check` prints the hash, so an agent can paste it and
    pass. Matching the hash proves the review is current, not who approved it."""

    def no_network(*args: object, **kwargs: object) -> object:
        raise AssertionError("an unauthenticated check or render used the network")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    # _approved_articles pastes exactly the hash the check prints.
    blueprint = _approved_articles(tmp_path, capsys)

    assert _check(blueprint, tmp_path) == 0
    assert "basics/result: self-approved · sha256:" in capsys.readouterr().out
    assert _check(blueprint, tmp_path, "--json") == 0
    approvals = json.loads(capsys.readouterr().out)["approvals"]
    assert [(item["node_id"], item["label"]) for item in approvals] == [
        ("basics/other", "self-approved"),
        ("basics/result", "self-approved"),
    ]
    assert _render(blueprint, tmp_path) == 0
    page = (tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8")
    assert page.count('<span class="bp-review-self-approved">self-approved</span>') == 2
    # The hash to approve is a selectable row of the panel, not in its summary.
    assert len(re.findall(r'<span class="bp-key">Review hash</span><span class="bp-value"><code>sha256:[0-9a-f]{64}</code>', page)) == 2
    assert re.search(r"(?<!self-)approved<", page) is None


def test_render_refuses_articles_edited_after_its_review_check(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    """render_site loads the articles itself, after the check: they must be the
    ones the checked extraction saw."""

    blueprint = _approved_articles(tmp_path, capsys)
    skeleton = cli.extract_skeletons(blueprint, lean_root=tmp_path)
    _approve(blueprint / "roadmap" / "basics" / "result.md", "sha256:" + "f" * 64)

    with pytest.raises(ReviewError, match="the blueprint changed after its review evidence was extracted"):
        render_site(blueprint, tmp_path / "site", lean_root=tmp_path, review=lambda: skeleton)
    assert not (tmp_path / "site").exists()


def _approve_every_article(blueprint: Path, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for record in _prepare(blueprint, root).values():
        assert _record(blueprint, record) == 0
    _approve_as_printed(blueprint, root, capsys)


def _cite(article: Path, target: str) -> None:
    article.write_text(article.read_text(encoding="utf-8") + f"\n## Sources\n\n[Book]({target})\n", encoding="utf-8")


def _unpublished_idea(blueprint: Path) -> None:
    """Add the Lean-mapped article ``Review.idea`` in a folder that says ``published: false``."""

    drafts = blueprint / "roadmap" / "drafts"
    drafts.mkdir()
    (drafts / "README.md").write_text("---\npublished: false\n---\n\n# Drafts\n", encoding="utf-8")
    (drafts / "idea.md").write_text(
        "---\narticle_id: af_0123456789abcdef01234599\ndeclaration: theorem\nlean: Review.idea\n"
        "statement: formalized\n---\n\n# Idea\n\nAn idea holds.\n\n## Depends on\n\nNone.\n",
        encoding="utf-8",
    )


def test_render_shows_the_review_of_a_vault_with_an_unpublished_folder(
    tmp_path: Path, whole_extraction: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The evidence covers the whole vault, a folder render leaves out
    included, so render derives it from the vault and shows it for the
    articles it publishes."""

    blueprint = _blueprint(tmp_path)
    _unpublished_idea(blueprint)
    _approve_every_article(blueprint, tmp_path, capsys)

    assert _check(blueprint, tmp_path) == 0
    assert _render(blueprint, tmp_path) == 0
    page = (tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8")
    assert page.count('<details class="bp-review"') == 2
    assert not (tmp_path / "site/roadmap/drafts").exists()


def _render_restoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: Path, written: bytes, *, delete: bool = False
) -> None:
    """Render with the review of the blueprint as checked, after ``path`` was
    changed, and with it put back once render has its copy and before it
    reads the vault."""

    blueprint = tmp_path / "blueprint"
    skeleton = cli.extract_skeletons(blueprint, lean_root=tmp_path)
    original = path.read_bytes()
    if delete:
        path.unlink()
    else:
        path.write_bytes(written)
    load = render.load_graph

    def restore_then_load(where: Path) -> object:
        if Path(where) == blueprint:
            path.write_bytes(original)
        return load(where)

    monkeypatch.setattr(render, "load_graph", restore_then_load)
    render_site(blueprint, tmp_path / "site", lean_root=tmp_path, review=lambda: skeleton)


def test_render_refuses_an_article_edited_while_it_renders(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pages come from render's copy and the evidence from the vault, so
    an article that differs between the two reads is refused, though the
    vault ends as it was checked."""

    blueprint = _approved_articles(tmp_path, capsys)
    article = blueprint / "roadmap" / "basics" / "result.md"

    with pytest.raises(ReviewError, match="the article changed while the site was being rendered"):
        _render_restoring(tmp_path, monkeypatch, article, article.read_bytes() + b"\nAn edit.\n")
    assert not (tmp_path / "site").exists()


def test_render_refuses_a_published_article_its_copy_lacks(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    blueprint = _approved_articles(tmp_path, capsys)

    with pytest.raises(ReviewError, match="the article changed while the site was being rendered"):
        _render_restoring(tmp_path, monkeypatch, blueprint / "roadmap" / "basics" / "other.md", b"", delete=True)
    assert not (tmp_path / "site").exists()


def test_render_refuses_a_cited_passage_edited_while_it_renders(
    tmp_path: Path, whole_extraction: None, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The site publishes the copy's source file, so the passage a disclosure
    shows must be the one the copy cuts."""

    blueprint = _blueprint(tmp_path)
    _cite(blueprint / "roadmap/basics/result.md", "sources/book.txt#L2-L2")
    source = blueprint / "roadmap/basics/sources/book.txt"
    source.parent.mkdir()
    source.write_text("Heading\nThe original line.\n", encoding="utf-8")
    _approve_every_article(blueprint, tmp_path, capsys)

    with pytest.raises(ReviewError, match="the cited passage changed while the site was being rendered"):
        _render_restoring(tmp_path, monkeypatch, source, b"Heading\nAn edited line.\n")
    assert not (tmp_path / "site").exists()


def test_render_refuses_a_passage_the_site_does_not_publish(
    tmp_path: Path, whole_extraction: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hidden source note is reviewed, as check only warns about it, but
    render never reads it, so its passage cannot be shown beside the page."""

    blueprint = _blueprint(tmp_path)
    _cite(blueprint / "roadmap/basics/result.md", "../../sources/.private/book.txt#L2-L2")
    source = blueprint / "sources/.private/book.txt"
    source.parent.mkdir(parents=True)
    source.write_text("Heading\nA hidden line.\n", encoding="utf-8")
    _approve_every_article(blueprint, tmp_path, capsys)
    assert _check(blueprint, tmp_path) == 0
    capsys.readouterr()

    assert _render(blueprint, tmp_path) == 1
    assert "review-source-unpublished: cites a passage the site does not publish" in capsys.readouterr().out
    assert not (tmp_path / "site").exists()


def test_the_site_shows_a_card_as_it_was_validated(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    """The page's Markdown reads nothing in a card: the testimony is published
    escaped, line for line and tab for tab, with only the formulas
    split_testimony found marked for the card's own TeX input, beside the
    exact packet it was written from, fence lines and all."""

    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap/basics/result.md"
    article.write_text(
        article.read_text(encoding="utf-8") + "\n[src]: https://example.test/elsewhere\n", encoding="utf-8"
    )
    # A line of three backticks, as a string a statement rests on can hold.
    extraction.signatures["Review.result"] = "Review.result : True\n```"
    record = _prepare(blueprint, tmp_path)["Review.result"]
    Path(record["testimony"]).write_text(
        "For $a<b$, **this** and [src] are as typed.\n\n    <b>x</b>\t& `y`\n$$\nx \\le y\n$$\nIt costs \\$5.\n",
        encoding="utf-8",
    )
    assert _record(blueprint, record) == 0
    assert _render(blueprint, tmp_path) == 0

    published = site_converter().convert((tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8"))
    body = (
        'For <span class="bp-card-formula">\\(a&lt;b\\)</span>, **this** and [src] are as typed.&#10;&#10;'
        "    &lt;b&gt;x&lt;/b&gt;&#9;&amp; `y`&#10;"
        '<span class="bp-card-formula">\\[&#10;x \\le y&#10;\\]</span>&#10;It costs \\$5.'
    )
    assert f'<div class="bp-readback-body">{body}</div>' in published
    assert '<span class="bp-readback-status">current</span>' in published
    assert "example.test" not in published
    card = load_readbacks(blueprint)[(_RESULT_ID, "Review.result")]
    packets = re.findall(r'<pre class="bp-lean"><code>(.*?)</code></pre>', published, re.S)
    (packet,) = [html.unescape(shown) for shown in packets if "Review.result :" in html.unescape(shown)]
    assert packet == card.shown_text.rstrip("\n") and "\n```\n" in packet
    # The approval binds the packet alone, and the rows beside it say so.
    rows = re.findall(r'<span class="bp-key">(\w[\w ]*)</span><span class="bp-value">(.*?)</span></div>', published)
    bound = {key: value.rsplit(" · ", 1)[1] for key, value in rows if key in {"Packet", "Statement hash", "Assumes", "Axioms"}}
    assert bound == {
        "Packet": "what the approval binds",
        "Statement hash": "a record, not bound",
        "Assumes": "proof included, not bound",
        "Axioms": "proof included, not bound",
    }
    assert f"<code>{card.packet_hash}</code> · what the approval binds" in published
    assert f"<code>{card.statement_hash}</code> · a record, not bound" in published


def test_the_site_keeps_a_card_on_one_line_whatever_it_holds(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hand-edited card can hold carriage returns, which make it invalid and
    at which the page's Markdown would end a line: shown as references, they
    open no fence and set no attribute, and both panels stay whole."""

    blueprint = _blueprint(tmp_path)
    for record in _prepare(blueprint, tmp_path).values():
        assert _record(blueprint, record) == 0
    injected = "asserts True.\r```\r<b>x</b>\r{: .bp-x }\r"
    for key in ((_RESULT_ID, "Review.result"), (_OTHER_ID, "Review.other")):
        path = load_readbacks(blueprint)[key].path
        path.write_bytes(path.read_bytes().replace(b"asserts True.", injected.encode(), 1))

    assert _render(blueprint, tmp_path) == 0
    # Read as MkDocs reads a page, which ends a line at a carriage return.
    published = site_converter().convert((tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8"))
    assert published.count('<details class="bp-review"') == 2 and published.count('<pre class="bp-lean">') == 2
    assert "highlight" not in published and not re.search(r'class="[^"]*\bbp-x\b', published)
    bodies = re.findall(r'<div class="bp-readback-body">(.*?)</div>', published)
    assert len(bodies) == 2 and all(html.unescape(body).endswith(injected) for body in bodies)


def _review_panel(page: str, node: str) -> str:
    """The review disclosure under the statement ``node`` on a chapter page."""

    start = page.index(f'id="{node}"')
    return page[page.index('<details class="bp-review"', start) : page.index("</details>", start)]


def test_the_site_shows_a_formula_it_cannot_typeset_as_typed_and_warns(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    """A formula the read-back rules flag never makes testimony invalid, so the
    site shows it as typed, dollar signs and all, typesets the rest, and render
    warns, keeping its exit status."""

    blueprint = _blueprint(tmp_path)
    record = _prepare(blueprint, tmp_path)["Review.result"]
    Path(record["testimony"]).write_text("For $x \\le y$ and $a ~ <b>$, it costs $5 here.\n", encoding="utf-8")
    assert _record(blueprint, record) == 0
    capsys.readouterr()

    assert _render(blueprint, tmp_path) == 0
    out = capsys.readouterr().out
    published = site_converter().convert((tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8"))
    body = 'For <span class="bp-card-formula">\\(x \\le y\\)</span> and $a ~ &lt;b&gt;$, it costs $5 here.'
    assert f'<div class="bp-readback-body">{body}</div>' in published
    assert '<span class="bp-readback-status">current</span>' in published
    assert "<b>" not in _review_panel((tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8"), "result")
    prefix = "warning: basics/result: read-back for Review.result: "
    assert f"{prefix}the formula $a ~ <b>$ is shown as typed: ~, a space" in out
    assert f"{prefix}a dollar sign that no other closes is shown as typed" in out


@pytest.mark.parametrize(
    ("setup", "kind", "label", "hash_row"),
    [
        ("missing", "open", "not yet approved", False),
        ("unapproved", "open", "ready to approve", True),
        ("unverifiable", "drift", "approval cannot be verified because testimony is incomplete or invalid", False),
        ("drift", "drift", "the recorded approval is not the current complete review", True),
        ("pasted", "drift", "the recorded approval is not the current complete review", True),
        ("approved", "self-approved", "self-approved", True),
    ],
)
def test_the_site_labels_each_review_state(
    tmp_path: Path,
    extraction: _Extraction,
    capsys: pytest.CaptureFixture[str],
    setup: str,
    kind: str,
    label: str,
    hash_row: bool,
) -> None:
    """The summary names the state only; the hash to approve, when there is
    one, and the approval recorded are selectable rows of the panel, and each
    read-back comes before its packet."""

    blueprint = _blueprint(tmp_path)
    records = _prepare(blueprint, tmp_path)
    if setup == "unverifiable":
        # A formula the card's input would typeset, were the card valid.
        Path(records["Review.result"]["testimony"]).write_text("For $x \\le y$ it holds.\n", encoding="utf-8")
    if setup != "missing":
        for record in records.values():
            assert _record(blueprint, record) == 0
    if setup in {"unverifiable", "drift", "approved"}:
        _approve_as_printed(blueprint, tmp_path, capsys)
    if setup == "unverifiable":
        path = load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].path
        path.write_text(path.read_text(encoding="utf-8").replace("packet: sha256:", "packet: invalid-"), encoding="utf-8")
    elif setup == "drift":
        _edit_card(load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].path)
    elif setup == "pasted":
        _approve(blueprint / "roadmap" / "basics" / "result.md", "sha256:<i>x</i>")

    assert _render(blueprint, tmp_path) == 0
    panel = _review_panel((tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8"), "result")
    assert f'<span class="bp-review-{kind}">{html.escape(label)}</span></summary>' in panel
    assert "sha256:" not in re.search(r"<summary>.*?</summary>", panel, re.S)[0]
    assert ('<span class="bp-key">Approved</span>' in panel) == (setup in {"unverifiable", "drift", "pasted", "approved"})
    assert panel.index('<div class="bp-readback') < panel.index('<pre class="bp-lean">')
    lines = int(re.search(r"<summary>Review · 1 skeleton, (\d+) lines? to trust · ", panel)[1])
    assert f"1 skeleton, {lines} {'line' if lines == 1 else 'lines'} to trust" in panel
    assert ('<span class="bp-key">Review hash</span>' in panel) == hash_row
    if setup == "missing":
        assert "No read-back filed for this skeleton yet." in panel
    if setup == "unverifiable":
        assert '<div class="bp-readback bp-readback-invalid">' in panel and "bp-card-formula" not in panel
        assert "For $x \\le y$ it holds." in panel
    if setup == "pasted":
        assert "<code>sha256:&lt;i&gt;x&lt;/i&gt;</code>" in panel and "<i>" not in panel


def test_the_site_shows_a_passage_and_a_model_as_typed(
    tmp_path: Path, whole_extraction: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Markup in a cited passage or a model label is shown, not run, and fence
    lines in the passage do not open a fence on the page."""

    blueprint = _blueprint(tmp_path)
    source = blueprint / "sources/book.txt"
    source.parent.mkdir(parents=True)
    source.write_text("<i>x</i>\n```\nnot a fence\n```\n\tp\tq\n", encoding="utf-8")
    _cite(blueprint / "roadmap/basics/result.md", "../../sources/book.txt#L1-L5")
    for record in _prepare(blueprint, tmp_path).values():
        command = ["review", "record", str(blueprint), "--packets", record["packets"], "--model", "<i>m</i>"]
        command += ["--article-id", record["article_id"], "--declaration", record["declaration"]]
        assert main([*command, "--testimony", record["testimony"]]) == 0

    assert _render(blueprint, tmp_path) == 0
    published = site_converter().convert((tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8"))
    title = r'<div class="bp-skeleton-title">Source passage[^<]*</div>\s*'
    (shown,) = re.findall(title + r"<pre><code>(.*?)</code></pre>", published, re.S)
    assert html.unescape(shown) == "<i>x</i>\n```\nnot a fence\n```\n\tp\tq"
    assert "Read-back · &lt;i&gt;m&lt;/i&gt;" in published and "<i>m</i>" not in published


@pytest.mark.parametrize(
    ("path", "link", "content"),
    [
        ("sources/notes.js", "../../sources/notes.js#L1-L1", "A line of a script.\n"),
        ("sources/notes.txt#x.js", "../../sources/notes.txt%23x.js#L1-L1", "A line of a script.\n"),
        ("atlas.json", "../../atlas.json#L2-L2", '{"schema": "autoform-atlas/v1",\n "areas": {"Drafts": ["basics/result"]}}\n'),
        ("readbacks/notes.txt", "../../readbacks/notes.txt#L1-L1", "A note beside the cards.\n"),
        ("dependencies/full.json", "../../dependencies/full.json#L1-L1", '{"explorer": "written over"}\n'),
    ],
    ids=["script", "script-with-a-hash", "atlas", "readbacks", "written-over"],
)
def test_render_refuses_a_passage_from_a_file_the_site_leaves_out(
    tmp_path: Path, whole_extraction: None, capsys: pytest.CaptureFixture[str], path: str, link: str, content: str
) -> None:
    """The site never copies a script, a file under readbacks/, the atlas,
    which may name articles in unpublished folders, or a file render writes
    its own over, so a passage cut from one would be published only in the
    review panel."""

    blueprint = _blueprint(tmp_path)
    source = blueprint / path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(content, encoding="utf-8")
    _cite(blueprint / "roadmap/basics/result.md", link)
    _approve_every_article(blueprint, tmp_path, capsys)
    capsys.readouterr()

    assert _render(blueprint, tmp_path) == 1
    assert f"review-source-unpublished: cites a passage from {path}, a file the site leaves out" in (
        capsys.readouterr().out
    )
    assert not (tmp_path / "site").exists()


@pytest.mark.parametrize(
    ("path", "link"),
    [("sources/C#notes.txt", "../../sources/C%23notes.txt#L1-L1"), ("sources/book.markdown", "../../sources/book.markdown#L1-L1")],
    ids=["hash-in-the-name", "markdown-page"],
)
def test_render_shows_a_passage_from_a_file_the_site_publishes(
    tmp_path: Path, whole_extraction: None, capsys: pytest.CaptureFixture[str], path: str, link: str
) -> None:
    """A file whose name holds a #, or a page the site publishes, is no file
    the site leaves out."""

    blueprint = _blueprint(tmp_path)
    source = blueprint / path
    source.parent.mkdir(parents=True)
    source.write_text("A published line.\n", encoding="utf-8")
    _cite(blueprint / "roadmap/basics/result.md", link)
    _approve_every_article(blueprint, tmp_path, capsys)

    assert _render(blueprint, tmp_path) == 0
    panel = _review_panel((tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8"), "result")
    assert f"Source passage · {html.escape(path)}#L1-L1" in panel and "A published line." in panel


@pytest.mark.parametrize("spelled", ["READBACKS/notes\u00e9.txt", "READBACKS/notese\u0301.txt"], ids=["case", "case-and-form"])
def test_render_judges_a_passage_by_its_file_as_captured(
    tmp_path: Path,
    whole_extraction: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    spelled: str,
) -> None:
    """On a disk that ignores case and Unicode form, as macOS's does, a link
    resolves in either and its locator keeps the spelling it was written in,
    so the file it reaches is judged by its name as captured. The locator is
    spelled so here on any disk."""

    blueprint = _blueprint(tmp_path)
    source = blueprint / "readbacks/notes\u00e9.txt"
    source.parent.mkdir(parents=True)
    source.write_text("A note beside the cards.\n", encoding="utf-8")
    _cite(blueprint / "roadmap/basics/result.md", "../../readbacks/notes%C3%A9.txt#L1-L1")
    cut = render.source_passage

    def as_spelled(*args: object, **kwargs: object) -> tuple[str | None, str | None]:
        passage, locator = cut(*args, **kwargs)
        return passage, spelled + "#L1-L1" if locator == "readbacks/notes\u00e9.txt#L1-L1" else locator

    for module in ("render", "review", "skeleton"):
        monkeypatch.setattr(f"autoform_cli.{module}.source_passage", as_spelled)
    monkeypatch.setattr(f"{__name__}.source_passage", as_spelled)
    _approve_every_article(blueprint, tmp_path, capsys)
    capsys.readouterr()

    assert _render(blueprint, tmp_path) == 1
    out = capsys.readouterr().out
    assert "review-source-unpublished: cites a passage from readbacks/notes\u00e9.txt, a file the site leaves out" in out


def test_render_judges_the_cards_it_captured(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card edited once render has captured the vault is not what the site
    shows: the page shows and judges the captured card, as publication.json
    hashes it, and the next check judges the edit."""

    blueprint = _approved_articles(tmp_path, capsys)
    card = load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].path
    load = render._load_graph

    def load_then_edit(*args: object) -> object:
        graph = load(*args)
        _edit_card(card)
        return graph

    monkeypatch.setattr(render, "_load_graph", load_then_edit)
    assert _render(blueprint, tmp_path) == 0

    panel = _review_panel((tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8"), "result")
    assert "asserts True." in panel and "asserts nothing." not in panel
    assert '<span class="bp-review-self-approved">self-approved</span>' in panel
    capsys.readouterr()
    assert _check(blueprint, tmp_path) == 1
    assert "basics/result: review-drift" in capsys.readouterr().out


@pytest.mark.parametrize("refused", ["output", "capture", "coverage", "foreign-output", "lean-sources", "in-the-way"])
def test_render_refuses_its_output_and_inputs_before_running_lean(
    tmp_path: Path,
    extraction: _Extraction,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    refused: str,
) -> None:
    blueprint = _blueprint(tmp_path)
    output, reason = tmp_path / "site", ""
    if refused == "output":
        output, reason = blueprint / "site", "blueprint and output directories must be disjoint"
    elif refused == "capture":
        (blueprint / "roadmap/basics/link.md").symlink_to(blueprint / "README.md")
        reason = "refusing symlink in blueprint publication: roadmap/basics/link.md"
    elif refused == "coverage":
        (blueprint / "coverage/README.md").write_text("# Coverage\n\nNo table.\n", encoding="utf-8")
        reason = "coverage contract"
    elif refused == "foreign-output":
        output.mkdir()
        (output / "notes.txt").write_text("Not a site.\n", encoding="utf-8")
        reason = "refusing to overwrite a non-Autoform output directory"
    elif refused == "lean-sources":

        def unreadable(*args: object, **kwargs: object) -> object:
            raise OSError("planted read failure")

        monkeypatch.setattr(render, "snapshot_project_sources", unreadable)
        reason = "Lean sources could not be indexed"
    else:
        (blueprint / "stylesheets").write_text("A file where render needs a folder.\n", encoding="utf-8")
        reason = "stylesheets: is a file, where autoform render needs a folder"

    assert main(["render", str(blueprint), "--lean-root", str(tmp_path), "--review", "-o", str(output)]) == 1
    assert reason in capsys.readouterr().out
    assert extraction.calls == []


def test_render_takes_a_timeout_only_with_review(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = _blueprint(tmp_path)

    assert main(["render", str(blueprint), "--timeout", "5", "-o", str(tmp_path / "site")]) == 2
    assert capsys.readouterr().err == "error: --timeout requires --review\n"


def test_the_review_stays_on_the_chapter_pages(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    """Packets and testimony appear in the statement's panel, never in the
    explorer's data, the landing page, the structure page, or the search
    index."""

    blueprint = _approved_articles(tmp_path, capsys)
    assert _render(blueprint, tmp_path) == 0

    site = tmp_path / "site"
    pages = {
        path.relative_to(site).as_posix(): path.read_text(encoding="utf-8")
        for path in site.rglob("*")
        if path.is_file() and path.suffix in {".md", ".json", ".js"}
    }
    holding = sorted(name for name, text in pages.items() if "asserts True." in text or "bp-review" in text)
    assert holding == ["roadmap/basics/README.md"]
    assert pages["roadmap/basics/README.md"].count('<details class="bp-review" markdown="1" data-search-exclude>') == 2


def test_render_warns_only_about_the_cards_it_publishes(
    tmp_path: Path, whole_extraction: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A warning names a declaration, so none comes from a card of an article
    in a folder the site leaves out."""

    blueprint = _blueprint(tmp_path)
    _unpublished_idea(blueprint)
    for record in _prepare(blueprint, tmp_path).values():
        Path(record["testimony"]).write_text(f"{record['declaration']} holds for $a ~ b$.\n", encoding="utf-8")
        assert _record(blueprint, record) == 0
    capsys.readouterr()

    assert _render(blueprint, tmp_path) == 0
    out = capsys.readouterr().out
    assert "basics/result: read-back for Review.result: the formula $a ~ b$ is shown as typed" in out
    assert "Review.idea" not in out


def test_the_site_names_cards_from_the_vault_not_from_its_copy(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    """Render judges the cards in its private copy, so an error naming a card
    by its path would put a temporary directory on the page."""

    blueprint = _approved_articles(tmp_path, capsys)
    card = load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].path
    card.with_name("Review.result.md").write_bytes(card.read_bytes())

    assert _render(blueprint, tmp_path) == 0
    page = (tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8")
    duplicate = (
        f"multiple cards claim the same declaration identity: &#x27;readbacks/{_RESULT_ID}/{card.name}&#x27; "
        f"and &#x27;readbacks/{_RESULT_ID}/Review.result.md&#x27;"
    )
    assert duplicate in _review_panel(page, "result")
    assert "autoform-blueprint-" not in page and str(tmp_path) not in page


def test_render_shows_no_review_for_a_module_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A catalog inventories a module rather than stating one result, so
    review leaves it out, a name Lean cannot resolve included, and the site
    shows no review for it."""

    blueprint = _blueprint(tmp_path)
    chapter = blueprint / "roadmap" / "basics"
    (chapter / "catalog.md").write_text(
        "---\ncatalog: module\nlean: Review.cat_a, Review.cat_b\n---\n\n# Catalog\n\nA module.\n\n"
        "## Depends on\n\nNone.\n",
        encoding="utf-8",
    )
    readme = chapter / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "- [Catalog](catalog.md)\n", encoding="utf-8")

    def extract(blueprint_dir: object, **kwargs: object) -> SkeletonReport:
        report = _whole_extraction(blueprint_dir, **kwargs)
        nodes = tuple(
            replace(node, declarations=node.declarations[:1]) if node.node_id == "basics/catalog" else node
            for node in report.nodes
        )
        unresolved = (UnresolvedTarget("basics/catalog", "Review.cat_b", "probe failed"),)
        return replace(report, nodes=nodes, unresolved=unresolved)

    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", extract)
    _approve_every_article(blueprint, tmp_path, capsys)
    capsys.readouterr()

    assert _render(blueprint, tmp_path) == 0
    page = (tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8")
    assert 'id="catalog"' in page
    assert page.count('<details class="bp-review"') == 2
    assert "Review.cat_a" not in "".join(_review_panel(page, node) for node in ("result", "other"))


def test_render_judges_the_macros_it_captured(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    """The approval binds the project macros its statement uses, and the site
    typesets with the tex-macros.json render captured, so an edit to it while
    Lean runs leaves the page as captured; the next check judges the edit."""

    blueprint = _blueprint(tmp_path)
    macros = blueprint / "tex-macros.json"
    macros.write_text('{"R": "\\\\mathbb{R}"}', encoding="utf-8")
    _write_statement(blueprint / "roadmap" / "basics" / "result.md", "Every $x \\in \\R$ is equal to itself.")
    for record in _prepare(blueprint, tmp_path).values():
        assert _record(blueprint, record) == 0
    _approve_as_printed(blueprint, tmp_path, capsys)
    extraction.on_extract = lambda: macros.write_text('{"R": "\\\\mathbb{C}"}', encoding="utf-8")

    assert _render(blueprint, tmp_path) == 0
    panel = _review_panel((tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8"), "result")
    assert '<span class="bp-review-self-approved">self-approved</span>' in panel
    extraction.on_extract = None
    capsys.readouterr()
    assert _check(blueprint, tmp_path) == 1
    assert "basics/result: review-drift" in capsys.readouterr().out


def test_the_site_shows_each_name_a_statement_rests_on_as_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Lean writes any character but a closing guillemet between guillemets,
    a line end and a fence line included, so each name in the Assumes and
    Axioms rows is shown on one line, as typed, and opens no fence."""

    fake, name = _Extraction(), "Lib.\u00ab\n```\n\u00bb"

    def extract(blueprint_dir: object, **kwargs: object) -> SkeletonReport:
        report = fake(blueprint_dir, **kwargs)
        nodes = tuple(
            replace(node, declarations=tuple(replace(item, assumed=(name,), axioms=(name,)) for item in node.declarations))
            if node.node_id == "basics/result"
            else node
            for node in report.nodes
        )
        return replace(report, nodes=nodes)

    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", extract)
    blueprint = _approved_articles(tmp_path, capsys)
    chapter = blueprint / "roadmap" / "basics" / "README.md"
    chapter.write_text(chapter.read_text(encoding="utf-8") + "\n```text\nA fenced note.\n```\n", encoding="utf-8")

    assert _render(blueprint, tmp_path) == 0
    published = site_converter().convert((tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8"))
    assert published.count('<details class="bp-review"') == 2 and published.count('<pre class="bp-lean">') == 2
    assert published.count('class="highlight"') == 1
    rows = re.findall(r'<span class="bp-key">(?:Assumes|Axioms)</span><span class="bp-value"><code>(.*?)</code>', published)
    assert [html.unescape(row) for row in rows] == [name, name]
