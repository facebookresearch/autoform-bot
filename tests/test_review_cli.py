from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli.__main__ import _current_review, main
from autoform_cli.graph import Graph, load_graph
from autoform_cli.markdown import site_converter
from autoform_cli.readback import TESTIMONY_MAX_BYTES, load_readbacks, readback_path, render_testimony
from autoform_cli.render import PublicationError, render_site
from autoform_cli.review import REVIEW_PACKET_SCHEMA, load_review_bundle, validate_review_bundle
from autoform_cli.skeleton import (
    NodeSkeleton,
    SkeletonError,
    SkeletonReport,
    blueprint_hash,
    extract_skeletons,
)
from tests.skeleton_fixtures import BLUEPRINT_HASH as _BLUEPRINT_HASH
from tests.skeleton_fixtures import declaration as fixture_declaration


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
    return SkeletonReport(
        blueprint_hash=_BLUEPRINT_HASH,
        targets=(("basics/result", ("Review.result",)),),
        selection="all",
        selected_nodes=("basics/result",),
        nodes=(_node("basics/result", "Review.result"),),
        unresolved=(),
    )


def _as_extracted(report: SkeletonReport, blueprint_dir: object, node_ids: object = None) -> SkeletonReport:
    """Stamp ``report`` with the blueprint as it is now, and scope it to
    ``node_ids`` when they are given, as a real extraction does."""

    report = replace(report, blueprint_hash=blueprint_hash(load_graph(blueprint_dir)))
    if node_ids is None:
        return report
    wanted = set(node_ids)
    return replace(
        report,
        selection="filtered",
        selected_nodes=tuple(node_ids),
        nodes=tuple(node for node in report.nodes if node.node_id in wanted),
        unresolved=tuple(issue for issue in report.unresolved if issue.node_id in wanted),
    )


_TESTIMONY = "For the unique proposition, the proposition is true.\n"


def _prepare_and_record(
    tmp_path: Path,
    blueprint: Path,
    *,
    packet: Path | None = None,
    testimony: str = _TESTIMONY,
    extra: tuple[str, ...] = (),
) -> tuple[int, Path, Path]:
    """Prepare a bundle and blind packets, then record ``testimony`` for the packet, through the CLI.

    Return the record's exit status, the bundle, and the packet recorded, which
    is the prepared one unless ``packet`` names another. ``extra`` is passed to
    both commands."""

    bundle, packets = tmp_path / "review.json", tmp_path / "packets"
    prepare = ["review", "prepare", str(blueprint), "--lean-root", str(tmp_path), "--output", str(bundle)]
    assert main([*prepare, "--packets", str(packets), *extra]) == 0
    if packet is None:
        packet = packets / json.loads((packets / "manifest.json").read_text(encoding="utf-8"))["packets"][0]["packet"]
    (tmp_path / "testimony.md").write_text(testimony, encoding="utf-8")
    record = ["review", "record", str(blueprint), "--lean-root", str(tmp_path), "--bundle", str(bundle)]
    code = main(
        [*record, "--article-id", "af_0123456789abcdef01234567", "--declaration", "Review.result",
         "--packet", str(packet), "--testimony", str(tmp_path / "testimony.md"), "--model", "test-model", *extra]
    )
    return code, bundle, packet


def _approve(article: Path, approval: str) -> None:
    """Set the article's ``review_approved`` to ``approval``, adding it after the
    statement if the article has none. The edit must change the article."""

    text = article.read_text(encoding="utf-8")
    if "review_approved: " in text:
        edited = re.sub(r"review_approved: \S+", f"review_approved: {approval}", text)
    else:
        edited = text.replace("statement: formalized\n", f"statement: formalized\nreview_approved: {approval}\n")
    assert edited != text
    article.write_text(edited, encoding="utf-8")


def test_review_cli_prepares_records_and_checks_exact_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blueprint = _blueprint(tmp_path)
    skeleton = _skeleton()
    extraction_scopes: list[object] = []
    probe_timeouts: list[object] = []

    def extract(*args: object, **kwargs: object) -> SkeletonReport:
        extraction_scopes.append(kwargs.get("node_ids"))
        probe_timeouts.append(kwargs.get("timeout"))
        return _as_extracted(skeleton, args[0], kwargs.get("node_ids"))

    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", extract)

    code, bundle_path, packet = _prepare_and_record(tmp_path, blueprint, extra=("--timeout", "1800"))
    assert code == 0
    capsys.readouterr()
    bundle = load_review_bundle(bundle_path)
    manifest = json.loads((tmp_path / "packets" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == REVIEW_PACKET_SCHEMA
    assert packet == tmp_path / "packets" / manifest["packets"][0]["packet"]
    assert packet.parent.name == "blind"
    assert packet.name == skeleton.nodes[0].declarations[0].evidence_hash.removeprefix("sha256:") + ".lean"
    assert "Review.result" not in packet.as_posix() and "basics" not in packet.as_posix()
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

    _approve(blueprint / "roadmap/basics/result.md", bundle.review_hash("af_0123456789abcdef01234567", cards))
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
        return _as_extracted(skeleton, args[0], kwargs.get("node_ids"))

    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", extract)
    code, bundle_path, _ = _prepare_and_record(tmp_path, blueprint)
    assert code == 0
    capsys.readouterr()
    extraction_scopes.clear()

    check = ["review", "check", str(blueprint), "--lean-root", str(tmp_path)]
    assert main(check) == 1
    assert "review-unapproved" in capsys.readouterr().out
    assert extraction_scopes == [None]

    # The derived bundle is the prepared one: an approval of either holds for both.
    approval = load_review_bundle(bundle_path).review_hash("af_0123456789abcdef01234567", load_readbacks(blueprint))
    _approve(blueprint / "roadmap/basics/result.md", approval)
    assert main(check) == 0
    assert "OK: statement reviews match" in capsys.readouterr().out
    assert extraction_scopes == [None, None]

    site = tmp_path / "site"
    assert main(["render", str(blueprint), "--lean-root", str(tmp_path), "--review", "--output", str(site)]) == 0
    assert extraction_scopes == [None, None, None]
    pages = "\n".join(path.read_text(encoding="utf-8") for path in site.rglob("*.md"))
    assert "For the unique proposition, the proposition is true." in pages
    assert "bp-readback-current" in pages


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
def test_check_and_render_refuse_the_blank_passage_prepare_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    blank: str,
) -> None:
    """A bundle derived without prepare is held to the rules prepare writes by.
    An article not marked cited may cite only blank lines, empty or spaces;
    prepare refuses that passage, so check and render refuse it too, even with
    a current card and the approval an unchecked derived bundle would offer,
    rather than publish an empty passage."""

    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap/basics/result.md"
    article.write_text(
        article.read_text(encoding="utf-8") + "\n## Sources\n\n[Book](sources/book.txt#L2-L2)\n", encoding="utf-8"
    )
    source = blueprint / "roadmap/basics/sources/book.txt"
    source.parent.mkdir()

    def cite(line: str) -> None:
        """Make ``line`` the cited line, and extract the passage as Lean would."""

        source.write_text(f"Heading\n{line}\n", encoding="utf-8")
        node = replace(_skeleton().nodes[0], passage=line, passage_locator="roadmap/basics/sources/book.txt#L2-L2")
        report = replace(_skeleton(), nodes=(node,))
        monkeypatch.setattr(
            "autoform_cli.__main__.extract_skeletons",
            lambda *args, **kwargs: _as_extracted(report, args[0], kwargs.get("node_ids")),
        )

    cite("Source theorem.")
    code, bundle_path, _ = _prepare_and_record(tmp_path, blueprint)
    assert code == 0
    prepare = ["review", "prepare", str(blueprint), "--lean-root", str(tmp_path), "--output", str(bundle_path),
               "--packets", str(tmp_path / "packets")]

    # The card binds the packet, not the passage, so it stays current.
    cite(blank)
    prepared = load_review_bundle(bundle_path)
    offered = replace(prepared, articles=(replace(prepared.articles[0], passage=blank),))
    _approve(article, offered.review_hash("af_0123456789abcdef01234567", load_readbacks(blueprint)))
    capsys.readouterr()

    assert main(prepare) == 2
    refused = capsys.readouterr().err
    assert refused == "error: malformed source passage for basics/result\n"

    assert main(["review", "check", str(blueprint), "--lean-root", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.err == refused
    assert "OK:" not in captured.out

    site = tmp_path / "site"
    assert main(["render", str(blueprint), "--lean-root", str(tmp_path), "--review", "--output", str(site)]) == 1
    assert capsys.readouterr().out == refused
    assert not site.exists()


def _published_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    testimony: str,
    *,
    article_extra: str = "",
) -> tuple[str, str]:
    """File ``testimony`` through the CLI, render the review site, and convert
    the chapter page as the published site does. Return the page's HTML and
    the card's testimony within it."""

    blueprint = _blueprint(tmp_path)
    if article_extra:
        article = blueprint / "roadmap/basics/result.md"
        article.write_text(
            article.read_text(encoding="utf-8").replace("itself.\n", f"itself.\n\n{article_extra}\n"),
            encoding="utf-8",
        )
    skeleton = _skeleton()
    monkeypatch.setattr(
        "autoform_cli.__main__.extract_skeletons",
        lambda *args, **kwargs: _as_extracted(skeleton, args[0], kwargs.get("node_ids")),
    )
    code, _, _ = _prepare_and_record(tmp_path, blueprint, testimony=testimony)
    assert code == 0, capsys.readouterr().err
    site = tmp_path / "site"
    assert main(["render", str(blueprint), "--lean-root", str(tmp_path), "--review", "--output", str(site)]) == 0
    capsys.readouterr()
    page = (site / "roadmap/basics/README.md").read_text(encoding="utf-8")
    published = site_converter().convert(page)
    title = '<span class="bp-readback-status">current</span></div>'
    start = published.index(title) + len(title)
    return published, published[start : published.index("\n</div>\n</details>", start)]


@pytest.mark.parametrize(
    ("testimony", "shown"),
    [
        # A block quote around indented code: code, so never typeset.
        (
            "The statement says $P$ holds.\n\n>     $\\phantom{not}$ x\n",
            '<blockquote><pre class="highlight"><code>$\\phantom{not}$ x</code></pre>',
        ),
        # Code and prose escaped once, not twice.
        (
            "Here `a < b && c` holds, and AT&T.\n\n```lean\ntheorem t : 1 < 2 := by decide\n```\n",
            '<p>Here <code>a &lt; b &amp;&amp; c</code> holds, and AT&amp;T.</p>'
            '<pre class="highlight"><code class="language-lean">theorem t : 1 &lt; 2 := by decide</code></pre>',
        ),
        (
            "- For $a<b$, `c`\n- and\n\n    1. nested\n\n| a | b |\n|:--|--:|\n| $x$ | y |\n\n"
            "$$\nx \\le y\n$$\n\nIt costs \\$5.\n",
            '<span class="arithmatex">\\(a&lt;b\\)</span>',
        ),
    ],
)
def test_the_site_shows_the_testimony_exactly_as_it_was_validated(
    testimony: str,
    shown: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The card validates what render_testimony makes of the testimony, so
    the published page must show those bytes and nothing the page's own
    Markdown makes of them."""

    _, card = _published_card(tmp_path, monkeypatch, capsys, testimony)

    assert card == render_testimony(testimony)
    assert shown in card


def test_testimony_cannot_link_through_the_page_s_link_definitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    published, card = _published_card(
        tmp_path,
        monkeypatch,
        capsys,
        "I checked [the Lean statement][src] and [src] against the text.\n",
        article_extra="See the source.\n\n[src]: https://example.test/elsewhere",
    )

    assert "https://example.test/elsewhere" not in published
    assert card == "<p>I checked [the Lean statement][src] and [src] against the text.</p>"


def test_render_takes_one_source_of_review_evidence(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = _blueprint(tmp_path)

    with pytest.raises(SystemExit) as refused:
        main(["render", str(blueprint), "--review", "--review-bundle", str(tmp_path / "review.json")])
    assert refused.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err

    assert main(["render", str(blueprint), "--review", "--output", str(tmp_path / "site")]) == 2
    assert "--review requires --lean-root" in capsys.readouterr().err


def test_audit_takes_one_source_of_review_evidence(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = _blueprint(tmp_path)

    with pytest.raises(SystemExit) as refused:
        main(["audit", str(blueprint), "--review", "--review-bundle", str(tmp_path / "review.json")])
    assert refused.value.code == 2
    assert "argument --review-bundle: not allowed with argument --review" in capsys.readouterr().err

    assert main(["audit", str(blueprint), "--review"]) == 2
    assert capsys.readouterr().err == "error: --review requires --lean-root\n"


def test_record_needs_every_record_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = _blueprint(tmp_path)

    with pytest.raises(SystemExit) as refused:
        main(
            ["review", "record", str(blueprint), "--lean-root", str(tmp_path), "--bundle", str(tmp_path / "review.json"),
             "--article-id", "af_0123456789abcdef01234567", "--declaration", "Review.result",
             "--packet", str(tmp_path / "packet.json"), "--model", "test-model"]
        )
    assert refused.value.code == 2
    assert "the following arguments are required: --testimony" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "expected_status"),
    [
        (
            ["audit", "blueprint", "--lean-root", ".", "--review-bundle", "review.json"],
            2,
        ),
        (["audit", "blueprint", "--lean-root", ".", "--review"], 2),
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
        "autoform_cli.__main__.extract_skeletons",
        lambda *args, **kwargs: _as_extracted(skeleton, args[0], kwargs.get("node_ids")),
    )
    packet = tmp_path / "changed.lean"
    packet.write_bytes(skeleton.nodes[0].declarations[0].blind_text().encode("utf-8") + b"\n")

    code, _, _ = _prepare_and_record(tmp_path, blueprint, packet=packet, testimony="A testimony.\n")
    assert code == 2
    assert "packet bytes do not match" in capsys.readouterr().err


def test_review_prepare_reports_output_filesystem_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blueprint = _blueprint(tmp_path)
    monkeypatch.setattr(
        "autoform_cli.__main__.extract_skeletons",
        lambda *args, **kwargs: _as_extracted(_skeleton(), args[0], kwargs.get("node_ids")),
    )
    output = tmp_path / "review.json"
    output.mkdir()

    assert main(["review", "prepare", str(blueprint), "--lean-root", str(tmp_path), "--output", str(output)]) == 2
    assert "error:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Records
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
        declarations=(fixture_declaration(name),),
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


def _packets(root: Path) -> Path:
    """Where `_prepared_records` has review prepare write the packets."""

    return root / "records" / "review-packets"


def _prepared_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extraction: _Extraction
) -> tuple[Path, Path, dict[str, dict[str, str]]]:
    """Prepare two articles and write a testimony for each packet; return the
    flags `review record` takes for each, keyed by declaration."""

    blueprint = _two_article_blueprint(tmp_path)
    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", extraction)
    bundle_path = tmp_path / "review.json"
    packets = _packets(tmp_path)
    assert main(
        ["review", "prepare", str(blueprint), "--lean-root", str(tmp_path), "--output", str(bundle_path), "--packets", str(packets)]
    ) == 0
    entries = json.loads((packets / "manifest.json").read_text(encoding="utf-8"))["packets"]
    records = {}
    for entry in entries:
        testimony = packets.parent / f"{entry['declaration']}.md"
        testimony.write_text(f"The statement {entry['declaration']} asserts True.\n", encoding="utf-8")
        records[entry["declaration"]] = {
            "article_id": entry["article_id"],
            "declaration": entry["declaration"],
            "packet": str(packets / entry["packet"]),
            "testimony": str(testimony),
        }
    return blueprint, bundle_path, records


def _record(blueprint: Path, bundle: Path, record: dict[str, str], root: Path) -> int:
    expected = ["--expected-card-hash", record["expected_card_hash"]] if "expected_card_hash" in record else []
    return main(
        ["review", "record", str(blueprint), "--lean-root", str(root), "--bundle", str(bundle), "--model", "test-model",
         "--article-id", record["article_id"], "--declaration", record["declaration"],
         "--packet", record["packet"], "--testimony", record["testimony"], *expected]
    )


def test_a_record_extracts_only_its_own_article(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.result"], tmp_path) == 0

    assert extraction.scopes == [None, ("basics/result",)]
    cards = load_readbacks(blueprint)
    assert set(cards) == {(_RESULT_ID, "Review.result")}
    assert capsys.readouterr().out.endswith(": recorded read-back for Review.result\n")

    # Filing identical content is a no-op, so the same record runs again cleanly.
    assert _record(blueprint, bundle, records["Review.result"], tmp_path) == 0
    assert load_readbacks(blueprint) == cards


def test_a_blank_testimony_is_refused_before_any_lean_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    Path(records["Review.result"]["testimony"]).write_text("   \n", encoding="utf-8")
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.result"], tmp_path) == 2

    assert load_readbacks(blueprint) == {}
    assert extraction.scopes == [None]  # only `review prepare` extracted
    err = capsys.readouterr().err
    assert "Review.result: a read-back requires nonempty testimony" in err


def test_a_record_is_checked_against_its_prepared_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A statement edited since `review prepare` stops the record."""

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction())
    path = blueprint / "roadmap" / "basics" / "result.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("\n\n## Depends on", "\n\nA new claim.\n\n## Depends on"), encoding="utf-8")
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.result"], tmp_path) == 2

    assert load_readbacks(blueprint) == {}
    assert "prepared review evidence differs from the current statement" in capsys.readouterr().err


def test_an_article_changed_during_extraction_files_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The record pairs one extraction with one state of its article."""

    article = tmp_path / "blueprint" / "roadmap" / "basics" / "other.md"
    calls: list[int] = []

    def edit_during_the_record() -> None:
        calls.append(1)
        if len(calls) == 2:  # the first extraction is `review prepare`
            article.write_text(article.read_text(encoding="utf-8") + "\nAn edit.\n", encoding="utf-8")

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction(edit_during_the_record))
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.other"], tmp_path) == 2

    assert load_readbacks(blueprint) == {}
    assert "changed during extraction; nothing was filed" in capsys.readouterr().err


def _delete_other(blueprint: Path) -> None:
    chapter = blueprint / "roadmap" / "basics"
    (chapter / "other.md").unlink()
    readme = chapter / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8").replace("- [Other](other.md)\n", ""), encoding="utf-8")


def _renumber_other(blueprint: Path) -> None:
    other = blueprint / "roadmap" / "basics" / "other.md"
    other.write_text(other.read_text(encoding="utf-8").replace(_OTHER_ID, "af_" + "1" * 24), encoding="utf-8")


@pytest.mark.parametrize("edit", [_delete_other, _renumber_other], ids=["deleted", "renumbered"])
@pytest.mark.parametrize("during_the_record", [False, True], ids=["after-prepare", "during-extraction"])
def test_an_article_gone_since_prepare_is_named_rather_than_the_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    edit: Callable[[Path], None],
    during_the_record: bool,
) -> None:
    """The bundle still has the declaration; the blueprint no longer has its article_id.

    The fake extraction makes no check of its own, so an edit during it reaches
    the reload after it, as one made after the real extraction's check would.
    The real extraction reports an edit during it first, as any blueprint change
    (test_an_article_gone_during_a_record_extraction_is_named_when_the_record_runs_again).
    """

    calls: list[int] = []

    def edit_during_the_record() -> None:
        calls.append(1)
        if during_the_record and len(calls) == 2:  # the first extraction is `review prepare`
            edit(tmp_path / "blueprint")

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction(edit_during_the_record))
    if not during_the_record:
        edit(blueprint)
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.other"], tmp_path) == 2

    err = capsys.readouterr().err
    assert load_readbacks(blueprint) == {}
    assert err == (
        f"error: Review.other: article_id {_OTHER_ID} is no longer in the blueprint; to record it, rerun review "
        "prepare with --packets and take --article-id from the new packet manifest; if that manifest names a "
        "different packet for it, pass that --packet and a --testimony written from it\n"
    )
    assert len(calls) == (2 if during_the_record else 1)


def test_a_chapter_that_cannot_be_listed_is_named_rather_than_its_articles_called_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Its articles are still there, so preparing again cannot help."""

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("permissions do not bind root")
    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    chapter = (blueprint / "roadmap" / "basics").resolve()
    chapter.chmod(0)
    try:
        capsys.readouterr()
        assert _record(blueprint, bundle, records["Review.result"], tmp_path) == 2
        recorded = capsys.readouterr().err
        assert main(["review", "check", str(blueprint), "--lean-root", str(tmp_path), "--bundle", str(bundle)]) == 2
        checked = capsys.readouterr().err
    finally:
        chapter.chmod(0o755)

    for err in (recorded, checked):
        assert f"error: cannot list roadmap directory {chapter}: Permission denied\n" in err
        assert "no longer in the blueprint" not in err
    assert extraction.scopes == [None]  # only `review prepare` extracted
    assert load_readbacks(blueprint) == {}


@pytest.mark.parametrize("named", [False, True], ids=["naming-no-card", "naming-its-card"])
def test_a_record_whose_article_id_is_gone_is_told_which_flags_to_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], named: bool
) -> None:
    """The refusal names the flags, and the record files once they take the new
    article_id from the packet manifest that review prepare writes with --packets."""

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction())
    record = records["Review.other"]
    assert _record(blueprint, bundle, record, tmp_path) == 0
    old_card = load_readbacks(blueprint)[(_OTHER_ID, "Review.other")].file_hash
    _renumber_other(blueprint)
    capsys.readouterr()

    assert _record(blueprint, bundle, dict(record, expected_card_hash=old_card) if named else record, tmp_path) == 2
    drop_hash = " and drop --expected-card-hash" if named else ""
    assert capsys.readouterr().err == (
        f"error: Review.other: article_id {_OTHER_ID} is no longer in the blueprint; to record it, rerun review "
        f"prepare with --packets and take --article-id from the new packet manifest{drop_hash}; if that manifest "
        "names a different packet for it, pass that --packet and a --testimony written from it\n"
    )

    packets = _packets(tmp_path)
    prepare = ["review", "prepare", str(blueprint), "--lean-root", str(tmp_path), "--output", str(bundle)]
    assert main([*prepare, "--packets", str(packets)]) == 0
    entries = json.loads((packets / "manifest.json").read_text(encoding="utf-8"))["packets"]
    (entry,) = [entry for entry in entries if entry["declaration"] == "Review.other"]
    assert _record(blueprint, bundle, dict(record, article_id=entry["article_id"]), tmp_path) == 0
    assert (entry["article_id"], "Review.other") in load_readbacks(blueprint)


def test_a_gone_record_whose_packet_changed_files_only_once_it_names_the_new_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Packets are named by their content, so when the statement changes with the
    article_id, the new packet manifest names another packet. The record that
    takes only the new article_id still names the packet its testimony was
    written from, which the new review prepare removed, and nothing is filed
    until it names the new packet. That its testimony is then written from the
    new packet is advice the record cannot check."""

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction())
    record = records["Review.other"]
    unchanged = _node

    def restated(node_id: str, name: str) -> NodeSkeleton:
        node = unchanged(node_id, name)
        if name != "Review.other":
            return node
        (declaration,) = node.declarations
        changed = replace(declaration, signature=f"{name} : False", raw_signature=f"{name} : False")
        return replace(node, declarations=(changed,))

    monkeypatch.setitem(globals(), "_node", restated)
    _renumber_other(blueprint)
    packets = _packets(tmp_path)
    prepare = ["review", "prepare", str(blueprint), "--lean-root", str(tmp_path), "--output", str(bundle)]
    assert main([*prepare, "--packets", str(packets)]) == 0
    entries = json.loads((packets / "manifest.json").read_text(encoding="utf-8"))["packets"]
    (entry,) = [entry for entry in entries if entry["declaration"] == "Review.other"]
    assert record["packet"] != str(packets / entry["packet"])
    record["article_id"] = entry["article_id"]
    capsys.readouterr()

    assert _record(blueprint, bundle, record, tmp_path) == 2
    assert "error: Review.other: cannot read the packet " in capsys.readouterr().err
    assert load_readbacks(blueprint) == {}

    record["packet"] = str(packets / entry["packet"])
    Path(record["testimony"]).write_text("The statement Review.other asserts False.\n", encoding="utf-8")
    assert _record(blueprint, bundle, record, tmp_path) == 0
    card = load_readbacks(blueprint)[(entry["article_id"], "Review.other")]
    assert card.packet_hash == entry["packet_hash"]
    assert "asserts False" in card.text


def test_a_re_review_whose_article_id_is_gone_is_named_before_its_card_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A card's path is keyed by its article_id, so the card a re-review would
    replace stays under the old one: the record is named as gone rather than
    asked for that card's hash, and files under the new article_id without it.
    review check then names the old card's file to delete, and stops naming it
    once it is gone."""

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction())
    for record in records.values():
        assert _record(blueprint, bundle, record, tmp_path) == 0
    filed = load_readbacks(blueprint)
    record = records["Review.other"]
    testimony = Path(record["testimony"])
    testimony.write_text(testimony.read_text(encoding="utf-8") + "Revised.\n", encoding="utf-8")
    _renumber_other(blueprint)
    capsys.readouterr()

    assert _record(blueprint, bundle, record, tmp_path) == 2
    err = capsys.readouterr().err
    assert f"error: Review.other: article_id {_OTHER_ID} is no longer in the blueprint" in err
    assert "already exists with different content" not in err

    old_card = filed[(_OTHER_ID, "Review.other")].file_hash
    assert _record(blueprint, bundle, dict(record, expected_card_hash=old_card), tmp_path) == 2
    assert (
        "rerun review prepare with --packets and take --article-id from the new packet manifest and drop "
        "--expected-card-hash; if that manifest names a different packet"
    ) in capsys.readouterr().err

    packets = _packets(tmp_path)
    prepare = ["review", "prepare", str(blueprint), "--lean-root", str(tmp_path), "--output", str(bundle)]
    assert main([*prepare, "--packets", str(packets)]) == 0
    entries = json.loads((packets / "manifest.json").read_text(encoding="utf-8"))["packets"]
    (entry,) = [entry for entry in entries if entry["declaration"] == "Review.other"]
    assert record["packet"] == str(packets / entry["packet"])
    capsys.readouterr()

    assert _record(blueprint, bundle, dict(record, article_id=entry["article_id"]), tmp_path) == 0
    cards = load_readbacks(blueprint)
    assert set(cards) == set(filed) | {(entry["article_id"], "Review.other")}
    assert cards[(_OTHER_ID, "Review.other")] == filed[(_OTHER_ID, "Review.other")]

    old = filed[(_OTHER_ID, "Review.other")].path
    capsys.readouterr()
    assert _check(blueprint, tmp_path) == 1
    orphaned = [line for line in capsys.readouterr().out.splitlines() if "readback-orphaned" in line]
    assert orphaned == [
        f"error: {_OTHER_ID}: readback-orphaned: read-back filed for Review.other under article_id {_OTHER_ID} is "
        f"not named by the prepared review bundle; delete {old}, or restore the article_id and declaration it names"
    ]
    old.unlink()
    _check(blueprint, tmp_path)
    assert "readback-orphaned" not in capsys.readouterr().out


def test_any_blueprint_edit_during_a_record_extraction_files_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The extraction checks the whole blueprint, so an edit to an article the
    record does not touch still aborts it, and running it again files the card."""

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction())
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

    assert _record(blueprint, bundle, records["Review.result"], project) == 2
    assert load_readbacks(blueprint) == {}
    assert "the blueprint changed while skeletons were being extracted" in capsys.readouterr().err

    assert _record(blueprint, bundle, records["Review.result"], project) == 0
    assert set(load_readbacks(blueprint)) == {(_RESULT_ID, "Review.result")}


@pytest.mark.parametrize("edit", [_delete_other, _renumber_other], ids=["deleted", "renumbered"])
def test_an_article_gone_during_a_record_extraction_is_named_when_the_record_runs_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], edit: Callable[[Path], None]
) -> None:
    """The extraction reports this edit as it does any other, so running the record
    again, rather than filing, names the article_id it took away."""

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction())
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text('name = "Review"\n', encoding="utf-8")
    probes: list[int] = []

    def probe_while_other_goes(graph: Graph, *, node_ids: tuple[str, ...] | None, **_: object) -> SkeletonReport:
        probes.append(1)
        if len(probes) == 1:
            edit(blueprint)
        return replace(_Extraction()(graph.blueprint_dir, node_ids=node_ids), blueprint_hash=blueprint_hash(graph))

    # The real extraction, with only the Lean probe replaced.
    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", extract_skeletons)
    monkeypatch.setattr("autoform_cli.skeleton.extract_graph_skeletons", probe_while_other_goes)
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.other"], project) == 2
    assert capsys.readouterr().err == (
        "error: the blueprint changed while skeletons were being extracted; retry after the project is idle\n"
    )

    assert _record(blueprint, bundle, records["Review.other"], project) == 2
    assert f"error: Review.other: article_id {_OTHER_ID} is no longer in the blueprint" in capsys.readouterr().err
    assert len(probes) == 1  # the rerun stopped before extracting
    assert load_readbacks(blueprint) == {}


def test_a_record_files_nothing_if_any_page_changes_after_its_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A record scoped to one article still pairs its extraction with the whole
    blueprint, so a page it does not select, edited before the reload, stops it."""

    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    chapter = blueprint / "roadmap" / "basics" / "README.md"

    def extract_then_edit_the_chapter(*args: object, **kwargs: object) -> SkeletonReport:
        report = extraction(*args, **kwargs)
        chapter.write_text(chapter.read_text(encoding="utf-8") + "\nAn unrelated edit.\n", encoding="utf-8")
        return report

    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", extract_then_edit_the_chapter)
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.result"], tmp_path) == 2
    assert load_readbacks(blueprint) == {}
    assert "the blueprint changed after its review evidence was extracted" in capsys.readouterr().err


def _file_alone(blueprint: Path, bundle: Path, root: Path, record: dict[str, str], text: str) -> None:
    """File a card for ``record`` with different testimony, as another reviewer would."""

    testimony = Path(record["testimony"]).with_name(f"elsewhere-{record['declaration']}.md")
    testimony.write_text(text, encoding="utf-8")
    assert _record(blueprint, bundle, dict(record, testimony=str(testimony)), root) == 0


def test_a_card_that_would_replace_another_is_refused_before_lean_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A re-review lands on the path of the card it supersedes. The record names
    that card, with the hash to pass, before it pays for an extraction."""

    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    record = records["Review.result"]
    key = (_RESULT_ID, "Review.result")
    _file_alone(blueprint, bundle, tmp_path, record, "An older reading of Review.result.\n")
    before = load_readbacks(blueprint)
    extractions = len(extraction.scopes)
    capsys.readouterr()

    assert _record(blueprint, bundle, record, tmp_path) == 2

    assert len(extraction.scopes) == extractions
    assert load_readbacks(blueprint) == before
    err = capsys.readouterr().err
    assert "error: Review.result: read-back already exists with different content" in err
    assert f"expected_card_hash={before[key].file_hash!r}" in err

    # Naming the card it replaces lets the same record run.
    record["expected_card_hash"] = before[key].file_hash
    assert _record(blueprint, bundle, record, tmp_path) == 0
    assert load_readbacks(blueprint)[key].file_hash != before[key].file_hash


def test_a_stale_expected_hash_is_refused_before_lean_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    record = records["Review.result"]
    _file_alone(blueprint, bundle, tmp_path, record, "An older reading.\n")
    stale = "sha256:" + "0" * 64
    record["expected_card_hash"] = stale
    extractions = len(extraction.scopes)
    capsys.readouterr()

    assert _record(blueprint, bundle, record, tmp_path) == 2

    assert len(extraction.scopes) == extractions
    assert f"read-back changed before replacement: expected {stale!r}" in capsys.readouterr().err


def _symlink_at(path: Path) -> None:
    path.symlink_to(path.with_name("elsewhere.md"))


def _fifo_at(path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("needs FIFOs")
    os.mkfifo(path)


def _oversized_at(path: Path) -> None:
    path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))


@pytest.mark.parametrize(
    ("unsafe", "reason"),
    [
        (_symlink_at, "cannot safely inspect existing read-back"),
        (Path.mkdir, "read-back destination is not a regular file"),
        (_fifo_at, "read-back destination is not a regular file"),
        (_oversized_at, "existing read-back is over the 4194304-byte limit for a card file"),
    ],
    ids=["symlink", "directory", "fifo", "oversized"],
)
def test_an_unsafe_card_is_refused_before_lean_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unsafe: Callable[[Path], None],
    reason: str,
) -> None:
    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    path = readback_path(blueprint.resolve(), _RESULT_ID, "Review.result")
    path.parent.mkdir(parents=True)
    unsafe(path)
    extractions = len(extraction.scopes)
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.result"], tmp_path) == 2

    assert len(extraction.scopes) == extractions
    err = capsys.readouterr().err
    assert f"error: Review.result: {reason}" in err and str(path) in err


@pytest.mark.parametrize("gone", [False, True], ids=["article-present", "article-gone"])
def test_a_platform_that_cannot_publish_is_refused_before_lean_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], gone: bool
) -> None:
    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    extractions = len(extraction.scopes)
    if gone:
        # Refused ahead of a gone record's advice, which only helps where cards can be filed.
        _renumber_other(blueprint)
    # As on Windows: no fcntl.
    monkeypatch.setattr("autoform_cli.readback.fcntl", None)
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.other"], tmp_path) == 2

    assert len(extraction.scopes) == extractions
    assert capsys.readouterr().err == "error: this platform cannot safely publish read-back cards\n"


def test_a_card_filed_while_lean_runs_stops_the_record_before_it_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The built card is checked for conflicts again before it is written."""

    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    record = records["Review.result"]

    def another_writer_while_lean_runs(*args: object, **kwargs: object) -> SkeletonReport:
        monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", extraction)
        _file_alone(blueprint, bundle, tmp_path, record, "A different reading.\n")
        return extraction(*args, **kwargs)

    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", another_writer_while_lean_runs)
    capsys.readouterr()

    assert _record(blueprint, bundle, record, tmp_path) == 2

    # The other writer's card is the only one: the record did not replace it.
    cards = load_readbacks(blueprint)
    assert set(cards) == {(_RESULT_ID, "Review.result")}
    assert cards[(_RESULT_ID, "Review.result")].text == "A different reading."
    assert "Review.result: read-back already exists with different content" in capsys.readouterr().err


def test_a_record_whose_named_card_is_removed_before_it_writes_files_once_it_names_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A record may replace only the card it names. When another writer removes
    that card after the checks, the record files once it names no card."""

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction())
    record = records["Review.result"]
    key = (_RESULT_ID, "Review.result")
    _file_alone(blueprint, bundle, tmp_path, record, "An earlier reading.\n")
    record["expected_card_hash"] = load_readbacks(blueprint)[key].file_hash
    publish = main.__globals__["publish_readback"]

    def remove_the_named_card_then_publish(card: object) -> Path:
        load_readbacks(blueprint)[key].path.unlink()
        return publish(card)

    monkeypatch.setattr("autoform_cli.__main__.publish_readback", remove_the_named_card_then_publish)
    capsys.readouterr()

    assert _record(blueprint, bundle, record, tmp_path) == 2

    err = capsys.readouterr().err
    assert err.startswith("error: Review.result: read-back changed before replacement: expected 'sha256:")
    assert err.endswith(", found no card\n")

    # The record still names the removed card, so it is refused until it names none.
    monkeypatch.setattr("autoform_cli.__main__.publish_readback", publish)
    assert _record(blueprint, bundle, record, tmp_path) == 2
    assert ", found no card\n" in capsys.readouterr().err
    del record["expected_card_hash"]
    assert _record(blueprint, bundle, record, tmp_path) == 0
    assert set(load_readbacks(blueprint)) == {key}


def test_a_failed_write_stops_the_record_until_its_cause_is_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("permissions do not bind root")
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction())
    record = records["Review.result"]
    # The card's directory cannot be made.
    cards = blueprint / "readbacks"
    cards.mkdir(exist_ok=True)
    cards.chmod(0o555)
    try:
        capsys.readouterr()
        assert _record(blueprint, bundle, record, tmp_path) == 2
        assert f"{cards / _RESULT_ID}: Permission denied" in capsys.readouterr().err
        assert load_readbacks(blueprint) == {}
    finally:
        cards.chmod(0o755)

    assert _record(blueprint, bundle, record, tmp_path) == 0
    assert set(load_readbacks(blueprint)) == {(_RESULT_ID, "Review.result")}


def _before_the_article_is_read_again(
    monkeypatch: pytest.MonkeyPatch, effect: Callable[[], None]
) -> Callable[..., object]:
    """Make the record run ``effect`` once its extraction has been checked, as
    it builds its card and before it reads its article again; return the real
    card builder, so that a test can restore it."""

    prepare = main.__globals__["prepare_readback"]

    def run_effect_then_prepare(*args: object, **kwargs: object) -> object:
        effect()
        return prepare(*args, **kwargs)

    monkeypatch.setattr("autoform_cli.__main__.prepare_readback", run_effect_then_prepare)
    return prepare


@pytest.mark.parametrize("deleted", [False, True], ids=["edited", "deleted"])
def test_an_article_edited_before_its_card_is_written_stops_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], deleted: bool
) -> None:
    """The article is read again just before its card is written."""

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction())
    other = (blueprint / "roadmap" / "basics" / "other.md").resolve()

    def edit_the_article() -> None:
        if deleted:
            other.unlink()
        else:
            other.write_text(other.read_text(encoding="utf-8") + "\nAn edit.\n", encoding="utf-8")

    _before_the_article_is_read_again(monkeypatch, edit_the_article)
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.other"], tmp_path) == 2

    captured = capsys.readouterr()
    assert load_readbacks(blueprint) == {}
    assert captured.out == ""
    if deleted:
        assert captured.err == (
            f"error: Review.other: article {other} was deleted after its evidence was checked, so its card was not "
            "filed; restore the article, or drop its records, and rerun the record\n"
        )
    else:
        assert captured.err == (
            f"error: Review.other: article {other} changed after its evidence was checked, so its card was not "
            "filed; rerun the record, after review prepare if the change is to that evidence\n"
        )


def _dangle(article: Path) -> None:
    article.unlink()
    article.symlink_to("missing.md")


def _replace_chapter_with_a_file(article: Path) -> None:
    for child in article.parent.iterdir():
        child.unlink()
    article.parent.rmdir()
    article.parent.write_text("", encoding="utf-8")


def _replace_with_a_directory(article: Path) -> None:
    article.unlink()
    article.mkdir()


def _link_to_a_device(article: Path) -> None:
    # /dev/null reads as empty, so a check that let devices through fails
    # rather than hangs, as it would reading /dev/zero.
    article.unlink()
    article.symlink_to("/dev/null")


def _replace_with_a_socket(article: Path) -> None:
    article.unlink()
    # Bound by a relative name: a socket's path may be only about 100 bytes long.
    cwd = os.getcwd()
    os.chdir(article.parent)
    try:
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(article.name)
    finally:
        os.chdir(cwd)


def _link_to_itself(article: Path) -> None:
    article.unlink()
    article.symlink_to(article.name)


def _make_unreadable(article: Path) -> None:
    article.chmod(0)


_GONE = "so its card was not filed; restore the article, or drop its records, and rerun the record"


@pytest.mark.parametrize(
    ("damage", "what"),
    [
        (_dangle, f"now links to a missing file, {_GONE}"),
        (_replace_chapter_with_a_file, f"was deleted after its evidence was checked, {_GONE}"),
        (_replace_with_a_directory, f"is no longer a regular file, {_GONE}"),
        pytest.param(
            _link_to_a_device,
            f"is no longer a regular file, {_GONE}",
            marks=pytest.mark.skipif(not os.path.exists("/dev/null"), reason="needs /dev/null"),
        ),
        pytest.param(
            _replace_with_a_socket,
            f"is no longer a regular file, {_GONE}",
            marks=pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets"),
        ),
        (_link_to_itself, f"is no longer a regular file, {_GONE}"),
        pytest.param(
            _make_unreadable,
            "cannot be read (Permission denied), so its card was not filed; rerun the record once it can be read",
            marks=pytest.mark.skipif(
                os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() == 0,
                reason="chmod 000 must make files unreadable",
            ),
        ),
    ],
    ids=[
        "a-dangling-link",
        "chapter-replaced-by-a-file",
        "replaced-by-a-directory",
        "linked-to-a-device",
        "replaced-by-a-socket",
        "a-link-to-itself",
        "made-unreadable",
    ],
)
def test_an_article_that_cannot_be_read_again_names_the_record_and_why(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    damage: Callable[[Path], None],
    what: str,
) -> None:
    """Whatever stops an article being read again before its card is written,
    the error names the record and why."""

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction())
    other = (blueprint / "roadmap" / "basics" / "other.md").resolve()
    _before_the_article_is_read_again(monkeypatch, lambda: damage(other))
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.other"], tmp_path) == 2

    assert capsys.readouterr().err == f"error: Review.other: article {other} {what}\n"
    assert load_readbacks(blueprint) == {}


def _replace_with_a_fifo(article: Path) -> None:
    article.unlink()
    os.mkfifo(article)


def _link_to_a_fifo(article: Path) -> None:
    article.unlink()
    os.mkfifo(article.with_name("other.pipe"))
    article.symlink_to("other.pipe")


def _record_without_blocking(
    blueprint: Path, bundle: Path, record: dict[str, str], root: Path, fifos: tuple[Path, ...]
) -> list[int]:
    """Run the record in a thread, assert that it did not block, and return its exit statuses.

    A record stuck opening one of ``fifos`` is let go, so the test fails rather
    than hangs. Each FIFO is opened without waiting, since a record held
    anywhere else leaves it no reader to wait for."""

    exits: list[int] = []
    run = threading.Thread(target=lambda: exits.append(_record(blueprint, bundle, record, root)), daemon=True)
    run.start()
    run.join(30)
    blocked = run.is_alive()
    if blocked:
        for path in fifos:
            try:
                os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
        run.join(30)
    assert not blocked
    return exits


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
@pytest.mark.parametrize("damage", [_replace_with_a_fifo, _link_to_a_fifo], ids=["a-fifo", "a-link-to-a-fifo"])
def test_a_fifo_put_at_an_article_stops_the_record_without_blocking_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    damage: Callable[[Path], None],
) -> None:
    """Opening a FIFO to read it waits for a writer, so an article that has
    become one, or a link to one, is refused without being read."""

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction())
    other = (blueprint / "roadmap" / "basics" / "other.md").resolve()
    _before_the_article_is_read_again(monkeypatch, lambda: damage(other))
    capsys.readouterr()

    exits = _record_without_blocking(blueprint, bundle, records["Review.other"], tmp_path, (other,))

    assert exits == [2]
    assert capsys.readouterr().err == f"error: Review.other: article {other} is no longer a regular file, {_GONE}\n"
    assert load_readbacks(blueprint) == {}


def test_an_article_that_is_a_link_is_read_again_through_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An article may be a link to another file inside roadmap/. Pointing the
    link at a different file changes the article as an edit does, though the
    file it pointed to is untouched."""

    class CanonicalExtraction(_Extraction):
        """Names each article by the file it resolves to, as the real extraction does."""

        def __call__(self, *args: object, **kwargs: object) -> SkeletonReport:
            report = super().__call__(*args, **kwargs)
            graph = load_graph(args[0])
            paths = {node.id: node.path.relative_to(graph.blueprint_dir).as_posix() for node in graph.nodes.values()}
            nodes = tuple(replace(node, article_path=paths[node.node_id]) for node in report.nodes)
            return replace(report, nodes=nodes)

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, CanonicalExtraction())
    chapter = blueprint.resolve() / "roadmap" / "basics"
    other = chapter / "other.md"
    text = other.read_text(encoding="utf-8")
    (chapter / "other-1.txt").write_text(text, encoding="utf-8")
    (chapter / "other-2.txt").write_text(text.replace("Truth holds.", "Falsehood holds."), encoding="utf-8")
    other.unlink()
    other.symlink_to("other-1.txt")
    prepare = ["review", "prepare", str(blueprint), "--lean-root", str(tmp_path), "--output", str(bundle)]
    assert main(prepare) == 0

    def point_the_other_article_elsewhere() -> None:
        other.unlink()
        other.symlink_to("other-2.txt")

    prepare_card = _before_the_article_is_read_again(monkeypatch, point_the_other_article_elsewhere)
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.other"], tmp_path) == 2

    assert load_readbacks(blueprint) == {}
    assert capsys.readouterr().err == (
        f"error: Review.other: article {other} changed after its evidence was checked, so its card was not filed; "
        "rerun the record, after review prepare if the change is to that evidence\n"
    )

    # The change is to the statement, so the record alone is refused, and
    # after review prepare it files.
    monkeypatch.setattr("autoform_cli.__main__.prepare_readback", prepare_card)
    assert _record(blueprint, bundle, records["Review.other"], tmp_path) == 2
    assert "prepared review evidence differs" in capsys.readouterr().err
    assert main(prepare) == 0
    assert _record(blueprint, bundle, records["Review.other"], tmp_path) == 0
    assert set(load_readbacks(blueprint)) == {(_OTHER_ID, "Review.other")}


def _too_deep_to_decode(text: str) -> bool:
    try:
        json.loads(text)
    except RecursionError:
        return True
    return False


def _undecodable_json(damage: str) -> str:
    """JSON that json.loads refuses with a RecursionError or a plain ValueError;
    skips the calling test where this interpreter would decode it."""

    if damage == "nested":
        text = '{"schema": ' + "[" * 100_000 + "]" * 100_000 + "}"
        if not _too_deep_to_decode(text):
            pytest.skip("this stack holds a hundred thousand nested arrays")
        return text
    digits = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if not digits:
        pytest.skip("this interpreter converts an integer of any length")
    return '{"schema": 1' + "0" * digits + "}"


def test_a_bundle_nested_too_deep_to_decode_is_refused_before_any_lean_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Decoded, a hundred thousand nested arrays are deeper than the JSON decoder
    goes: about 1,000 levels on Python 3.10 and 3.11, and 10,000 on 3.12 and 3.13.
    From 3.14 it goes as deep as the stack allows, about 37,000 levels in 8 MiB,
    and a stack that holds all of them skips the test."""

    # Decoded here, nearer the stack's base than the command decodes it.
    nested = _undecodable_json("nested")
    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    bundle.write_text(nested, encoding="utf-8")
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.result"], tmp_path) == 2

    err = capsys.readouterr().err
    # The cause reads "maximum recursion depth exceeded" up to 3.13 and
    # "Stack overflow (used N kB)" on 3.14; the words after it are the same.
    assert err.startswith(f"error: cannot read review bundle {bundle}: ")
    assert err.endswith(" while decoding a JSON array from a unicode string\n")
    assert extraction.scopes == [None]  # only `review prepare` extracted
    assert load_readbacks(blueprint) == {}


def _damage_bundle(bundle: Path, damage: str) -> None:
    if damage == "a-number-too-long":
        bundle.write_text(_undecodable_json("a-number-too-long"), encoding="utf-8")
    else:
        text = bundle.read_text(encoding="utf-8")
        assert '"title":"' in text
        bundle.write_text(text.replace('"title":"', '"title":"\\ud800', 1), encoding="utf-8")


@pytest.mark.parametrize(
    ("command", "refused"), [("review check", 2), ("audit", 2), ("render", 1), ("review record", 2)]
)
@pytest.mark.parametrize("damage", ["a-number-too-long", "a-lone-surrogate"])
def test_a_bundle_with_a_number_too_long_or_a_lone_surrogate_is_refused_as_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    damage: str,
    command: str,
    refused: int,
) -> None:
    """json.loads raises a plain ValueError, not a JSONDecodeError, for an integer
    of more digits than sys.get_int_max_str_digits(), 4300 by default, and decodes
    "\\ud800" to half of a surrogate pair, which the evidence hash cannot encode as
    UTF-8. An interpreter with no such limit (before 3.10.7, or with it set to 0)
    converts an integer of any length, and skips the long-number cases."""

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction())
    _damage_bundle(bundle, damage)
    capsys.readouterr()

    if command == "review check":
        code = main(["review", "check", str(blueprint), "--lean-root", str(tmp_path), "--bundle", str(bundle)])
    elif command == "audit":
        code = _audit(blueprint, tmp_path)
    elif command == "render":
        site = tmp_path / "site"
        code = main(
            [
                "render",
                str(blueprint),
                "--lean-root",
                str(tmp_path),
                "--review-bundle",
                str(bundle),
                "--output",
                str(site),
            ]
        )
    else:
        code = _record(blueprint, bundle, records["Review.result"], tmp_path)

    captured = capsys.readouterr()
    # render prints its errors on stdout.
    reported = captured.out if command == "render" else captured.err
    assert code == refused
    assert reported.startswith(f"error: cannot read review bundle {bundle}: ")
    if damage == "a-lone-surrogate":
        reason = "it escapes a lone surrogate, '\\ud800', which UTF-8 cannot encode"
        assert reported == f"error: cannot read review bundle {bundle}: {reason}\n"


@pytest.mark.parametrize("role", ["packet", "testimony"])
def test_an_unreadable_packet_or_testimony_is_named_before_any_lean_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], role: str
) -> None:
    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    record = records["Review.result"]
    Path(record[role]).unlink()
    capsys.readouterr()

    assert _record(blueprint, bundle, record, tmp_path) == 2

    assert capsys.readouterr().err.startswith(f"error: Review.result: cannot read the {role} {record[role]}: ")
    assert extraction.scopes == [None]  # only `review prepare` extracted
    assert load_readbacks(blueprint) == {}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
@pytest.mark.parametrize("role", ["packet", "testimony"])
def test_a_fifo_given_as_a_packet_or_testimony_is_refused_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], role: str
) -> None:
    """Opening a FIFO to read it waits for a writer, so a packet or testimony
    that is one is refused unread, and named with its declaration and role."""

    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    record = records["Review.result"]
    path = Path(record[role])
    path.unlink()
    os.mkfifo(path)
    capsys.readouterr()

    exits = _record_without_blocking(blueprint, bundle, record, tmp_path, (path,))

    assert exits == [2]
    assert capsys.readouterr().err == f"error: Review.result: cannot read the {role} {path}: not a regular file\n"
    assert extraction.scopes == [None]  # only `review prepare` extracted
    assert load_readbacks(blueprint) == {}


@pytest.mark.parametrize("over", [False, True], ids=["at-the-limit", "past-the-limit"])
def test_a_testimony_is_read_no_further_than_one_byte_past_twice_its_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], over: bool
) -> None:
    """A testimony is measured with its line endings read as text, which at
    most halves it, so it is read no further than the first byte past twice
    the limit. Here the file runs on to 64 MiB, which reading it whole would
    hold in memory."""

    import io
    import tracemalloc

    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    testimony = Path(records["Review.result"]["testimony"])
    testimony.write_bytes(b"a" * (2 * TESTIMONY_MAX_BYTES + 1 if over else TESTIMONY_MAX_BYTES))
    if over:
        os.truncate(testimony, 64 * 1024 * 1024)  # sparse: no disk, only memory if read
    positions: list[int] = []

    class Unbuffered(io.FileIO):
        """The testimony, read as asked; where it stands when closed is how far it was read."""

        def close(self) -> None:
            if not self.closed:
                positions.append(self.tell())
            super().close()

    monkeypatch.setattr(
        "autoform_cli.__main__.open",
        lambda path, *args, opener=None, **kwargs: (
            Unbuffered(path, opener=opener) if path == testimony else open(path, *args, opener=opener, **kwargs)
        ),
        raising=False,
    )
    capsys.readouterr()

    tracemalloc.start()
    try:
        assert _record(blueprint, bundle, records["Review.result"], tmp_path) == (2 if over else 0)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    err = capsys.readouterr().err
    if over:
        assert err == (
            f"error: Review.result: unsafe read-back testimony: {testimony} is over the "
            f"{TESTIMONY_MAX_BYTES}-byte limit\n"
        )
        assert positions == [2 * TESTIMONY_MAX_BYTES + 1]
        assert peak < 2 * 1024 * 1024
        assert extraction.scopes == [None]  # only `review prepare` extracted
        assert load_readbacks(blueprint) == {}
    else:
        assert positions == [TESTIMONY_MAX_BYTES]
        assert err == ""
        assert (_RESULT_ID, "Review.result") in load_readbacks(blueprint)


@pytest.mark.parametrize("over", [False, True], ids=["at-the-limit", "past-the-limit"])
def test_a_testimony_is_measured_with_crlf_read_as_lf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], over: bool
) -> None:
    """Saved with CRLF line endings, a testimony within the limit once read as
    text is filed, though the file is over it."""

    extraction = _Extraction()
    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    testimony = Path(records["Review.result"]["testimony"])
    lines = ["a" * 127] * (TESTIMONY_MAX_BYTES // 128)
    lines[0] += "a" if over else ""
    testimony.write_bytes("".join(line + "\r\n" for line in lines).encode())
    assert testimony.stat().st_size > TESTIMONY_MAX_BYTES
    capsys.readouterr()

    assert _record(blueprint, bundle, records["Review.result"], tmp_path) == (2 if over else 0)

    err = capsys.readouterr().err
    if over:
        assert err == (
            f"error: Review.result: unsafe read-back testimony: {testimony} is over the "
            f"{TESTIMONY_MAX_BYTES}-byte limit\n"
        )
        assert extraction.scopes == [None]  # only `review prepare` extracted
        assert load_readbacks(blueprint) == {}
    else:
        assert err == ""
        assert load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].text == "\n".join(lines)


def test_a_testimony_ends_a_line_at_crlf_or_cr_as_at_lf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read as a text file is, so the card says the same whichever line endings the file was saved with."""

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, _Extraction())
    Path(records["Review.result"]["testimony"]).write_bytes(b"The statement\r\nReview.result\rasserts True.\r\n")

    assert _record(blueprint, bundle, records["Review.result"], tmp_path) == 0

    card = load_readbacks(blueprint)[(_RESULT_ID, "Review.result")]
    assert card.text == "The statement\nReview.result\nasserts True."
    # The card is parsed as text, so its text alone would not show a "\r" filed.
    assert b"\r" not in card.path.read_bytes()


# --------------------------------------------------------------------------- #
# One snapshot per check
# --------------------------------------------------------------------------- #


def _approved_articles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extraction: _Extraction) -> Path:
    """Record and approve both articles, so that `review check` passes."""

    blueprint, bundle, records = _prepared_records(tmp_path, monkeypatch, extraction)
    for record in records.values():
        assert _record(blueprint, bundle, record, tmp_path) == 0
    cards = load_readbacks(blueprint)
    prepared = load_review_bundle(bundle)
    for name, article_id in (("result", _RESULT_ID), ("other", _OTHER_ID)):
        _approve(blueprint / "roadmap" / "basics" / f"{name}.md", prepared.review_hash(article_id, cards))
    return blueprint


def _check(blueprint: Path, root: Path) -> int:
    return main(["review", "check", str(blueprint), "--lean-root", str(root)])


def test_check_judges_the_blueprint_its_extraction_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An approval changed after the check loaded the graph, but before the
    extraction took its snapshot, must not pass on the strength of the old one."""

    extraction = _Extraction()
    blueprint = _approved_articles(tmp_path, monkeypatch, extraction)
    assert _check(blueprint, tmp_path) == 0
    capsys.readouterr()
    article = blueprint / "roadmap" / "basics" / "result.md"

    def approve_something_else() -> None:
        _approve(article, "sha256:" + "f" * 64)

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
    blueprint = _approved_articles(tmp_path, monkeypatch, extraction)
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


@pytest.mark.parametrize(
    ("prepared", "stale_exit", "stale_reason"),
    [
        (False, 1, "review-drift"),
        (True, 2, "prepared review evidence differs from the current statement"),
    ],
    ids=["derived-bundle", "prepared-bundle"],
)
def test_check_judges_the_statement_its_snapshot_parsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    prepared: bool,
    stale_exit: int,
    stale_reason: str,
) -> None:
    """The statement is judged as the snapshot parsed it, beside that snapshot's
    approval. Here the tree fails the check before the edit and after it, but a
    check reading the statement again would pair the restored statement with the
    old approval, a state that was never on disk, and print OK. The check
    judges the state it loaded and fails as it did before the edit."""

    blueprint = _approved_articles(tmp_path, monkeypatch, _Extraction())
    article = blueprint / "roadmap" / "basics" / "result.md"
    _write_statement(article, "Every object is different from itself.")
    check = ["review", "check", str(blueprint), "--lean-root", str(tmp_path)]
    if prepared:
        check += ["--bundle", str(tmp_path / "review.json")]
    capsys.readouterr()
    assert main(check) == stale_exit
    captured = capsys.readouterr()
    assert stale_reason in captured.out + captured.err

    def restore_the_statement_and_approve_something_else() -> None:
        _write_statement(article, "Every object is equal to itself.")
        _approve(article, "sha256:" + "e" * 64)

    _once_the_cards_are_rechecked(monkeypatch, restore_the_statement_and_approve_something_else)
    assert main(check) == stale_exit
    captured = capsys.readouterr()
    assert stale_reason in captured.out + captured.err
    assert "OK:" not in captured.out

    # The tree the edit left holds an approval of nothing, which an idle check names.
    assert main(check) == 1
    assert "review-drift" in capsys.readouterr().out


def _edit_chapter_page(blueprint: Path) -> None:
    page = blueprint / "roadmap" / "basics" / "README.md"
    page.write_text(page.read_text(encoding="utf-8") + "\nMore prose.\n", encoding="utf-8")


def _edit_result_card(blueprint: Path) -> None:
    _edit_card(load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].path)


@pytest.mark.parametrize("change", [_edit_chapter_page, _edit_result_card], ids=["page", "card"])
def test_check_judges_the_graph_and_cards_it_compared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: Callable[[Path], None],
) -> None:
    """Every comparison has passed, so the verdict is about the graph and cards
    the check read first. An edit landing now, to a page no review reads or to a
    card, changes nothing. A check that validated against the blueprint loaded
    again would refuse the page edit, and one that judged the cards read again
    would reject the card edit."""

    blueprint = _approved_articles(tmp_path, monkeypatch, _Extraction())
    _once_the_cards_are_rechecked(monkeypatch, lambda: change(blueprint))
    capsys.readouterr()

    assert _check(blueprint, tmp_path) == 0
    assert "OK:" in capsys.readouterr().out


def test_render_review_shows_the_readbacks_its_check_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cards rendered are the snapshot the review was validated against, not a later read."""

    blueprint = _approved_articles(tmp_path, monkeypatch, _Extraction())

    def read_again(*args: object, **kwargs: object) -> object:
        raise AssertionError("render read the read-backs again after validating the review")

    monkeypatch.setattr("autoform_cli.render.load_readbacks", read_again)
    site = tmp_path / "site"
    assert main(["render", str(blueprint), "--lean-root", str(tmp_path), "--review", "--output", str(site)]) == 0
    pages = "\n".join(path.read_text(encoding="utf-8") for path in site.rglob("*.md"))
    assert "The statement Review.result asserts True." in pages
    assert '<span class="bp-review-self-approved">self-approved · sha256:' in pages


def test_render_refuses_articles_edited_after_the_review_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """render_site loads the articles itself, after the check: they must be the ones
    the checked extraction saw, or the cards it was handed describe another state."""

    blueprint = _approved_articles(tmp_path, monkeypatch, _Extraction())
    _, skeleton, bundle, cards = _current_review(blueprint, lean_root=tmp_path, bundle_path=None)
    article = blueprint / "roadmap" / "basics" / "result.md"
    _approve(article, "sha256:" + "f" * 64)
    site = tmp_path / "site"

    with pytest.raises(PublicationError, match="the blueprint changed after its review evidence was extracted"):
        render_site(blueprint, site, lean_root=tmp_path, skeleton=skeleton, review_bundle=bundle, readbacks=cards)
    assert not site.exists()


def test_render_shows_the_statement_its_review_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A statement written after render validated the review must not appear in
    that review's box as the approved statement; the render publishes the
    statement it validated."""

    blueprint = _approved_articles(tmp_path, monkeypatch, _Extraction())
    article = blueprint / "roadmap" / "basics" / "result.md"

    def validate_then_rewrite(*args: object, **kwargs: object) -> object:
        findings = validate_review_bundle(*args, **kwargs)
        _write_statement(article, "Every object is different from itself.")
        return findings

    monkeypatch.setattr("autoform_cli.render.validate_review_bundle", validate_then_rewrite)
    site = tmp_path / "site"
    capsys.readouterr()

    assert main(["render", str(blueprint), "--lean-root", str(tmp_path), "--review", "--output", str(site)]) == 0
    pages = "\n".join(path.read_text(encoding="utf-8") for path in site.rglob("*.md"))
    assert "different from itself" not in pages
    assert "Every object is equal to itself." in pages


def _audit(blueprint: Path, root: Path, *, evidence: tuple[str, ...] | None = None) -> int:
    """Audit over Lean sources that declare both targets, so that only review
    evidence can fail it. That evidence is the bundle `_prepared_records` wrote,
    unless ``evidence`` gives other flags."""

    (root / "Review.lean").write_text(
        "namespace Review\n\ntheorem result : True := trivial\n\ntheorem other : True := trivial\n\nend Review\n",
        encoding="utf-8",
    )
    if evidence is None:
        evidence = ("--review-bundle", str(root / "review.json"))
    return main(["audit", str(blueprint), "--lean-root", str(root), *evidence])


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

    blueprint = _approved_articles(tmp_path, monkeypatch, _Extraction())
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

    blueprint = _approved_articles(tmp_path, monkeypatch, _Extraction())
    card = load_readbacks(blueprint)[(_RESULT_ID, "Review.result")].path
    article = blueprint / "roadmap" / "basics" / "result.md"

    def edit_the_card_and_approve_it() -> None:
        _edit_card(card)
        approval = load_review_bundle(tmp_path / "review.json").review_hash(_RESULT_ID, load_readbacks(blueprint))
        _approve(article, approval)

    _after_the_check(monkeypatch, edit_the_card_and_approve_it)
    capsys.readouterr()

    assert _audit(blueprint, tmp_path) == 1
    out = capsys.readouterr().out
    assert "review-snapshot-changed: the blueprint changed after its review evidence was extracted" in out
    assert "OK:" not in out


def test_audit_review_derives_the_evidence_review_check_derives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Where review check passes without a prepared bundle, audit --review
    passes too, deriving the evidence from one extraction of its own. Given no
    evidence, audit still fails, with one finding for the blueprint rather than
    one per approved article, and says how to supply it."""

    extraction = _Extraction()
    blueprint = _approved_articles(tmp_path, monkeypatch, extraction)
    assert _check(blueprint, tmp_path) == 0
    capsys.readouterr()

    assert _audit(blueprint, tmp_path, evidence=()) == 1
    missing = (
        "review_approved is present but no review evidence was supplied; pass --review to derive it in this run, "
        "or --review-bundle with a prepared bundle, each with --lean-root"
    )
    assert [line for line in capsys.readouterr().out.splitlines() if line.startswith("error:")] == [
        f"error: .: review-bundle-missing: {missing}"
    ]

    extraction.scopes.clear()
    assert _audit(blueprint, tmp_path, evidence=("--review",)) == 0
    assert "OK: roadmap audit passed" in capsys.readouterr().out
    assert extraction.scopes == [None]


def test_audit_review_reports_a_stale_card_as_review_check_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once Review.result states something else, its card testifies about a
    meaning no longer extracted. audit --review reports for it exactly what
    review check reports, at the article's path."""

    extraction = _Extraction()
    blueprint = _approved_articles(tmp_path, monkeypatch, extraction)
    signature = "Review.result : False"
    meaning = '{"generated":[],"root":{"safety":"safe","type":{"const":{"str":[null,"False"]},"levels":[]}}}'

    def restated(*args: object, **kwargs: object) -> SkeletonReport:
        report = extraction(*args, **kwargs)
        nodes = []
        for node in report.nodes:
            if node.node_id == "basics/result":
                (declaration,) = node.declarations
                declaration = replace(declaration, signature=signature, raw_signature=signature, semantic=meaning)
                node = replace(node, declarations=(declaration,))
            nodes.append(node)
        return replace(report, nodes=tuple(nodes))

    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", restated)
    capsys.readouterr()

    assert main(["review", "check", str(blueprint), "--lean-root", str(tmp_path), "--json"]) == 1
    checked = json.loads(capsys.readouterr().out)["findings"]
    assert _audit(blueprint, tmp_path, evidence=("--review", "--json")) == 1
    audited = json.loads(capsys.readouterr().out)["findings"]

    assert [(item["node_id"], item["code"]) for item in checked] == [
        ("basics/result", "readback-invalid"),
        ("basics/result", "review-approval-unverifiable"),
    ]
    assert [(item["article_path"], item["code"], item["reason"]) for item in audited] == [
        (f"roadmap/{item['node_id']}.md", item["code"], item["reason"]) for item in checked
    ]


def test_a_pasted_current_hash_is_only_self_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The blocker: `review check` prints the hash, so an agent can paste it and
    pass. Matching the hash proves the review is current, not who approved it."""

    def no_network(*args: object, **kwargs: object) -> object:
        raise AssertionError("an unauthenticated check or render used the network")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    # _approved_articles pastes exactly the hash the tooling computes.
    blueprint = _approved_articles(tmp_path, monkeypatch, _Extraction())

    assert _check(blueprint, tmp_path) == 0
    output = capsys.readouterr().out
    assert "basics/result: self-approved · sha256:" in output
    assert "approved by" not in output

    site = tmp_path / "site"
    assert main(["render", str(blueprint), "--lean-root", str(tmp_path), "--review", "--output", str(site)]) == 0
    pages = "\n".join(path.read_text(encoding="utf-8") for path in site.rglob("*.md"))
    assert pages.count('<span class="bp-review-self-approved">self-approved · sha256:') == 2
    assert "bp-review-approved" not in pages
    assert re.search(r"(?<!self-)approved ·", pages) is None


def _no_lean(*args: object, **kwargs: object) -> SkeletonReport:
    raise AssertionError("this command must not run Lean")


def _reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **scope: object) -> tuple[Path, Path]:
    """Approve both articles, then write the report a Lean job would hand on, and forbid Lean."""

    extraction = _Extraction()
    blueprint = _approved_articles(tmp_path, monkeypatch, extraction)
    report = tmp_path / "artifact" / "skeleton-report.json"
    report.parent.mkdir()
    report.write_text(extraction(blueprint, **scope).to_json(), encoding="utf-8")
    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", _no_lean)
    return blueprint, report


def test_check_and_render_take_the_skeleton_report_of_a_job_that_built_lean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint, report = _reported(tmp_path, monkeypatch)

    assert main(["review", "check", str(blueprint), "--skeleton-report", str(report)]) == 0
    assert "OK: statement reviews match" in capsys.readouterr().out

    site = tmp_path / "site"
    assert main(["render", str(blueprint), "--review", "--skeleton-report", str(report), "--output", str(site)]) == 0
    pages = "\n".join(path.read_text(encoding="utf-8") for path in site.rglob("*.md"))
    assert "The statement Review.result asserts True." in pages
    assert "bp-readback-current" in pages


def test_a_card_directory_that_cannot_be_listed_is_an_error_not_missing_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("permissions do not bind root")
    blueprint, report = _reported(tmp_path, monkeypatch)
    cards = (blueprint / "readbacks" / "af_0123456789abcdef01234567").resolve()
    cards.chmod(0o000)
    try:
        assert main(["review", "check", str(blueprint), "--skeleton-report", str(report)]) == 2
    finally:
        cards.chmod(0o755)
    captured = capsys.readouterr()
    assert f"error: cannot list read-back directory {cards}: Permission denied" in captured.err
    assert "no read-back filed" not in captured.err
    assert "OK:" not in captured.out


def test_a_skeleton_report_of_another_blueprint_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint, report = _reported(tmp_path, monkeypatch)
    article = blueprint / "roadmap" / "basics" / "result.md"
    article.write_text(article.read_text(encoding="utf-8") + "\nA later remark.\n", encoding="utf-8")

    assert main(["review", "check", str(blueprint), "--skeleton-report", str(report)]) == 2
    captured = capsys.readouterr()
    assert f"{report} was extracted from blueprint sha256:" in captured.err
    assert "not from this checkout's sha256:" in captured.err
    assert "OK:" not in captured.out

    assert main(["render", str(blueprint), "--review", "--skeleton-report", str(report), "--output",
                 str(tmp_path / "site")]) == 1
    assert f"{report} was extracted from blueprint sha256:" in capsys.readouterr().out


def test_a_skeleton_report_of_some_articles_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint, report = _reported(tmp_path, monkeypatch, node_ids=["basics/result"])

    assert main(["review", "check", str(blueprint), "--skeleton-report", str(report)]) == 2
    assert f"{report} covers selected articles only; review needs every article" in capsys.readouterr().err


def test_review_evidence_comes_from_lean_or_a_report_but_not_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = _blueprint(tmp_path)
    report = tmp_path / "skeleton-report.json"

    for flags in ([], ["--lean-root", str(tmp_path), "--skeleton-report", str(report)]):
        with pytest.raises(SystemExit) as refused:
            main(["review", "check", str(blueprint), *flags])
        assert refused.value.code == 2
    capsys.readouterr()

    assert main(["render", str(blueprint), "--skeleton-report", str(report), "--output", str(tmp_path / "site")]) == 2
    assert "--skeleton-report requires --review or --review-bundle" in capsys.readouterr().err
