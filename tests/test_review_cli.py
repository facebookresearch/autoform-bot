from __future__ import annotations

import html
import json
import os
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli.__main__ import _current_review, main
from autoform_cli.graph import load_graph
from autoform_cli.markdown import site_converter
from autoform_cli.readback import load_readbacks
from autoform_cli.render import render_site
from autoform_cli.review import REVIEW_PACKET_SCHEMA, ReviewError
from autoform_cli.skeleton import NodeSkeleton, SkeletonReport, blueprint_hash
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


def test_review_evidence_comes_from_lean_or_a_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = str(_blueprint(tmp_path))
    site = ["-o", str(tmp_path / "site")]
    assert main(["render", blueprint, "--review", *site]) == 2
    assert "--review requires --lean-root or --skeleton-report" in capsys.readouterr().err
    for flag in (["--skeleton-report", "report.json"], ["--policy", "github"]):
        assert main(["render", blueprint, *flag, *site]) == 2
        assert f"{flag[0]} requires --review" in capsys.readouterr().err
    for flags in ([], ["--lean-root", ".", "--skeleton-report", "report.json"]):
        with pytest.raises(SystemExit) as refused:
            main(["review", "check", blueprint, *flags])
        assert refused.value.code == 2
    assert not (tmp_path / "site").exists()


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
    assert "skeleton hash does not match the prepared declaration" in findings[1]["reason"]

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
    assert f"error: review: readback-unlistable: cannot list read-back directory {cards}: Permission denied" in captured.err
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

    That read is the check's last snapshot comparison: from there on it only
    judges what it has already read.
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


def test_check_judges_the_statement_its_snapshot_parsed(
    tmp_path: Path, extraction: _Extraction, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The statement is judged as the snapshot parsed it, beside that snapshot's
    approval. Here the tree fails the check before the edit and after it, but a
    check reading the statement again would pair the restored statement with the
    old approval, a state that was never on disk, and print OK."""

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
    assert _check(blueprint, tmp_path) == 1
    captured = capsys.readouterr()
    assert "review-drift" in captured.out and "OK:" not in captured.out


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
    """Every comparison has passed, so the verdict is about the graph and cards
    the check read first. An edit landing now, to a page no review reads or to a
    card, changes nothing."""

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
    assert [(item["node_id"], item["label"], item["policy_enforced"], item["reason"]) for item in approvals] == [
        ("basics/other", "self-approved", False, None),
        ("basics/result", "self-approved", False, None),
    ]
    assert _render(blueprint, tmp_path) == 0
    page = (tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8")
    assert page.count('<span class="bp-review-self-approved">self-approved · sha256:') == 2
    assert re.search(r"(?<!self-)approved ·", page) is None


def test_render_refuses_articles_edited_after_its_review_check(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    """render_site loads the articles itself, after the check: they must be the
    ones the checked extraction saw, or the cards it was handed describe
    another state."""

    blueprint = _approved_articles(tmp_path, capsys)
    _, skeleton, _, cards = _current_review(blueprint, lean_root=tmp_path)
    _approve(blueprint / "roadmap" / "basics" / "result.md", "sha256:" + "f" * 64)

    with pytest.raises(ReviewError, match="the blueprint changed after its review evidence was extracted"):
        render_site(blueprint, tmp_path / "site", lean_root=tmp_path, skeleton=skeleton, readbacks=cards)
    assert not (tmp_path / "site").exists()


def test_the_site_shows_a_card_as_it_was_validated(
    tmp_path: Path, extraction: _Extraction, capsys: pytest.CaptureFixture[str]
) -> None:
    """The page's Markdown reads nothing in a card: the testimony is published
    escaped, line for line, with only the formulas split_testimony found marked
    for the card's own TeX input, beside the exact packet it was written from."""

    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap/basics/result.md"
    article.write_text(
        article.read_text(encoding="utf-8") + "\n[src]: https://example.test/elsewhere\n", encoding="utf-8"
    )
    record = _prepare(blueprint, tmp_path)["Review.result"]
    Path(record["testimony"]).write_text(
        "For $a<b$, **this** and [src] are as typed.\n\n    <b>x</b> & `y`\n$$\nx \\le y\n$$\nIt costs \\$5.\n",
        encoding="utf-8",
    )
    assert _record(blueprint, record) == 0
    assert _render(blueprint, tmp_path) == 0

    published = site_converter().convert((tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8"))
    body = (
        'For <span class="bp-card-formula">\\(a&lt;b\\)</span>, **this** and [src] are as typed.&#10;&#10;'
        "    &lt;b&gt;x&lt;/b&gt; &amp; `y`&#10;"
        '<span class="bp-card-formula">\\[&#10;x \\le y&#10;\\]</span>&#10;It costs \\$5.'
    )
    assert f'<div class="bp-readback-body">{body}</div>' in published
    assert '<span class="bp-readback-status">current</span>' in published
    assert "example.test" not in published
    packet = load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].shown_text
    assert f'<pre class="bp-lean"><code>{html.escape(packet.rstrip())}</code></pre>' in published


def test_check_and_render_take_the_skeleton_report_of_a_job_that_built_lean(
    tmp_path: Path, extraction: _Extraction, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pages builds Lean in one job and hands the report to the job holding the
    token, which must never run Lean; the report must be of this checkout."""

    blueprint = _approved_articles(tmp_path, capsys)
    report = tmp_path / "skeleton-report.json"
    report.write_text(extraction(blueprint).to_json(), encoding="utf-8")
    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", _no_lean)
    reported = ["--skeleton-report", str(report)]

    assert main(["review", "check", str(blueprint), *reported]) == 0
    assert "OK: 2 statement review(s) complete and current" in capsys.readouterr().out
    assert main(["render", str(blueprint), "--review", *reported, "-o", str(tmp_path / "site")]) == 0
    page = (tmp_path / "site/roadmap/basics/README.md").read_text(encoding="utf-8")
    assert "The statement Review.result asserts True." in page
    assert '<span class="bp-readback-status">current</span>' in page

    _approve(blueprint / "roadmap" / "basics" / "result.md", "sha256:" + "f" * 64)
    assert main(["review", "check", str(blueprint), *reported]) == 2
    refused = capsys.readouterr().err
    assert f"error: {report} was extracted from blueprint sha256:" in refused
    assert "not from this checkout's sha256:" in refused
    assert main(["render", str(blueprint), "--review", *reported, "-o", str(tmp_path / "site2")]) == 1
    assert f"error: {report} was extracted from blueprint sha256:" in capsys.readouterr().out
    assert not (tmp_path / "site2").exists()


def _no_lean(*args: object, **kwargs: object) -> SkeletonReport:
    raise AssertionError("this command must not run Lean")
