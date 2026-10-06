from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import autoform_cli.audit as audit_module
from autoform_cli.__main__ import main
from autoform_cli.audit import audit_blueprint
from autoform_cli.graph import load_graph
from autoform_cli.lean import LeanSourceError


def _ensure_chapter(blueprint: Path, relative: str) -> None:
    """Give a chapter directory its chapter page, as every real vault has.

    Containment comes from nested `README.md` articles, so a directory without
    one is refused at load. Fixtures that only happen to nest are not trying to
    model that fault; the tests that are do it explicitly.
    """
    parts = Path(relative).parts
    if len(parts) < 2:
        return
    chapter = blueprint / "roadmap" / parts[0]
    page = chapter / "README.md"
    if not page.exists():
        chapter.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "---\n---\n\n# " + parts[0].replace("-", " ").title() + "\n", encoding="utf-8"
        )


def _article(
    blueprint: Path,
    relative: str,
    prose: str = "A precise mathematical statement.",
    *,
    depends: bool = True,
    sources: tuple[str, ...] = (),
    **metadata: str,
) -> Path:
    _ensure_chapter(blueprint, relative)
    path = blueprint / "roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    title = path.stem.replace("-", " ").title()
    properties = [*(f"{key}: {value}" for key, value in metadata.items())]
    lines = ["---", *properties, "---", "", f"# {title}", "", prose]
    if sources:
        lines.extend(["", "## Sources", "", *(f"- [source]({target})" for target in sources)])
    if depends:
        lines.extend(["", "## Depends on", "", "This article has no prerequisites."])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _coverage(
    blueprint: Path,
    text: str = (
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Narrative | OUT | No formalization target |"
    ),
) -> Path:
    path = blueprint / "coverage" / "README.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Coverage\n\n{text}\n", encoding="utf-8")
    return path


def _lean_project(tmp_path: Path, spans: dict[str, int]) -> Path:
    """Write one Lean file per declaration, padded to the requested line count."""
    root = tmp_path / "lean"
    root.mkdir(exist_ok=True)
    for name, lines in spans.items():
        steps = "\n".join(f"  -- step {step}" for step in range(lines - 2))
        source = f"theorem {name} : True := by\n{steps}\n  trivial\n"
        (root / f"{name.rsplit('.', 1)[-1]}.lean").write_text(source, encoding="utf-8")
    return root


def _finding_map(blueprint: Path, *, lean_root: Path | None = None) -> dict[str, list[tuple[str, str]]]:
    result = audit_blueprint(blueprint, lean_root=lean_root)
    findings: dict[str, list[tuple[str, str]]] = {}
    for finding in result.findings:
        findings.setdefault(finding.article_path, []).append((finding.code, finding.reason))
    return findings


def test_clean_audit_has_stable_machine_readable_representation(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(
        blueprint,
        "result.md",
        declaration="theorem",
        statement="formalized",
        proof="formalized",
        lean="Project.result",
    )
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "Result.lean").write_text("theorem Project.result : True := trivial\n", encoding="utf-8")

    first = audit_blueprint(blueprint, lean_root=lean_root)
    second = audit_blueprint(blueprint, lean_root=lean_root)

    assert first.clean
    assert first.findings == ()
    assert first.clean
    assert first.coverage is not None
    assert first.coverage.counts == {"MAPPED": 0, "DECOMPOSED": 0, "DEFERRED": 0, "OUT": 1}
    assert first.as_dict()["findings"] == []
    assert second.to_json() == first.to_json()
    assert str(tmp_path) not in first.to_json()


def test_audit_reports_formalizable_structure_and_inconsistent_checked_facts(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(
        blueprint,
        "chapter/README.md",
        prose="",
        depends=False,
        declaration="theorem",
        proof="formalized",
    )
    _article(blueprint, "chapter/child.md", declaration="lemma")

    findings = _finding_map(blueprint)["roadmap/chapter/README.md"]
    codes = {code for code, _reason in findings}

    assert codes == {
        "formalizable-container",
        "missing-depends-section",
        "missing-statement-text",
        "proof-without-statement",
    }
    assert all(reason for _code, reason in findings)


def test_audit_requires_mathlib_declaration_and_declaration_intent_on_evidenced_leaf(
    tmp_path: Path,
) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "upstream.md", mathlib="true")
    _article(blueprint, "local.md", statement="formalized", lean="Project.local")
    _article(blueprint, "exposition.md")

    findings = _finding_map(blueprint)

    upstream_codes = {code for code, _reason in findings["roadmap/upstream.md"]}
    local_codes = {code for code, _reason in findings["roadmap/local.md"]}
    assert upstream_codes == {"mathlib-without-declaration", "missing-declaration-intent"}
    assert local_codes == {"missing-declaration-intent"}
    assert "roadmap/exposition.md" not in findings


def test_audit_validates_local_source_links_without_network_access(tmp_path: Path, monkeypatch) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    source = blueprint / "sources" / "paper.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Paper\n\n## Theorem\n", encoding="utf-8")
    _article(
        blueprint,
        "chapter/result.md",
        declaration="theorem",
        origin="cited",
        sources=(
            "../../sources/paper.md#theorem",
            "../../sources/missing.md",
            "../../../outside.md",
            "https://example.invalid/paper",
            "ftp://example.invalid/paper",
            "//example.invalid/paper",
            "%00",
        ),
    )

    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("audit attempted network access")

    monkeypatch.setattr("socket.create_connection", fail_network)
    findings = _finding_map(blueprint)["roadmap/chapter/result.md"]

    assert findings == [
        ("malformed-source-link", "source link contains an invalid path: '%00'"),
        ("source-escapes-blueprint", "source link escapes the blueprint: '../../../outside.md'"),
        ("source-not-found", "source link does not resolve to a file: '../../sources/missing.md'"),
        ("unsupported-source-link", "source link uses a network location: '//example.invalid/paper'"),
        ("unsupported-source-link", "source link uses unsupported scheme: 'ftp://example.invalid/paper'"),
    ]


def test_audit_rejects_missing_markdown_source_anchor(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    source = blueprint / "sources" / "paper.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Paper\n\n## Actual theorem\n", encoding="utf-8")
    _article(
        blueprint,
        "result.md",
        declaration="theorem",
        origin="cited",
        sources=("../sources/paper.md#missing-theorem",),
    )

    findings = _finding_map(blueprint)["roadmap/result.md"]
    assert findings == [
        (
            "source-anchor-not-found",
            "source link fragment does not resolve: '../sources/paper.md#missing-theorem'",
        )
    ]


def test_audit_validates_lean_targets_only_when_root_is_supplied(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(
        blueprint,
        "missing.md",
        declaration="theorem",
        statement="formalized",
        lean="Project.missing",
    )
    _article(
        blueprint,
        "untargeted.md",
        declaration="lemma",
        statement="formalized",
    )
    _article(
        blueprint,
        "wrong-kind.md",
        declaration="theorem",
        statement="formalized",
        lean="Project.value",
    )
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "Value.lean").write_text("def Project.value : Nat := 1\n", encoding="utf-8")

    without_lean = _finding_map(blueprint)
    with_lean = _finding_map(blueprint, lean_root=lean_root)

    assert "roadmap/missing.md" not in without_lean
    assert with_lean["roadmap/missing.md"] == [
        ("lean-target-not-found", "Lean declaration target was not found: Project.missing")
    ]
    assert with_lean["roadmap/untargeted.md"] == [
        ("missing-lean-target", "formalized local work has no lean declaration target")
    ]
    assert with_lean["roadmap/wrong-kind.md"] == [
        ("lean-target-kind-mismatch", "Lean target kind def does not match declaration intent theorem")
    ]


def test_audit_reports_lean_targets_declared_deprecated(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    targets = {
        "same-line.md": "Project.sameLine",
        "line-above.md": "Project.lineAbove",
        "multi-line.md": "Project.multiLine",
        "fresh.md": "Project.fresh",
        "decoy.md": "Project.decoy",
        "after-previous.md": "Project.afterPrevious",
        "after-alias.md": "Project.afterAlias",
    }
    for relative, name in targets.items():
        _article(blueprint, relative, declaration="theorem", statement="formalized", lean=name)
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    source = [
        "theorem Project.fresh : True := trivial",
        "",
        '@[deprecated Project.fresh (since := "2025-01-01")] theorem Project.sameLine : True := trivial',
        "",
        "/-- Kept for one release. -/",
        "@[simp]",
        '@[deprecated Project.fresh (since := "2025-01-01")]',
        "protected theorem Project.lineAbove : True := trivial",
        "",
        "@[simp,",
        '  deprecated "use [Project.fresh] instead" (since := "2025-01-01")]',
        "-- removed after the next release",
        "theorem Project.multiLine : True := trivial",
        "",
        '-- @[deprecated Project.fresh (since := "2025-01-01")]',
        "/- @[deprecated Project.fresh] -/",
        '@[to_additive "deprecated", deprecated_alias]',
        "theorem Project.decoy : True := by",
        '  have : "@[deprecated]" = "@[deprecated]" := rfl',
        "  trivial",
        "",
        # An earlier declaration's attributes stay with it, indexed or not.
        "@[deprecated Project.fresh] theorem Project.previous : True := trivial",
        "theorem Project.afterPrevious : True := trivial",
        "",
        "@[deprecated Project.fresh]",
        "alias Project.oldAlias := Project.fresh",
        "theorem Project.afterAlias : True := trivial",
    ]
    (lean_root / "Old.lean").write_text("\n".join(source) + "\n", encoding="utf-8")

    without_lean = _finding_map(blueprint)
    with_lean = _finding_map(blueprint, lean_root=lean_root)

    def deprecated(name: str) -> list[tuple[str, str]]:
        return [("lean-target-deprecated", f"lean target {name} is deprecated; point lean: at its replacement")]

    assert without_lean == {}
    assert with_lean == {
        "roadmap/same-line.md": deprecated("Project.sameLine"),
        "roadmap/line-above.md": deprecated("Project.lineAbove"),
        "roadmap/multi-line.md": deprecated("Project.multiLine"),
    }


def test_audit_reads_deprecation_from_the_captured_source_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(
        blueprint,
        "result.md",
        declaration="theorem",
        statement="formalized",
        lean="Project.result",
    )
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    source = lean_root / "Project.lean"
    source.write_text(
        "@[deprecated Project.fresh] theorem Project.result : True := trivial\n",
        encoding="utf-8",
    )
    real_snapshot_project_sources = audit_module.snapshot_project_sources

    def snapshot_then_rewrite(root: Path):
        snapshot = real_snapshot_project_sources(root)
        source.write_text("theorem Project.result : True := trivial\n", encoding="utf-8")
        return snapshot

    monkeypatch.setattr(audit_module, "snapshot_project_sources", snapshot_then_rewrite)

    findings = _finding_map(blueprint, lean_root=lean_root)

    assert findings["roadmap/result.md"] == [
        (
            "lean-target-deprecated",
            "lean target Project.result is deprecated; point lean: at its replacement",
        )
    ]


def test_audit_checks_irreducible_def_kinds(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    for declaration, target in (
        ("def", "sealed"),
        ("definition", "sealed"),
        ("irreducible_def", "sealed"),
        ("irreducible_def", "plain"),
    ):
        _article(
            blueprint,
            f"{declaration}-{target}.md",
            declaration=declaration,
            statement="formalized",
            lean=f"Project.{target}",
        )
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "Value.lean").write_text(
        "irreducible_def Project.sealed : Nat := 1\ndef Project.plain : Nat := 1\n", encoding="utf-8"
    )

    assert _finding_map(blueprint, lean_root=lean_root) == {
        "roadmap/irreducible_def-plain.md": [
            ("lean-target-kind-mismatch", "Lean target kind def does not match declaration intent irreducible_def")
        ]
    }


def test_audit_reports_invalid_lean_root_once(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(
        blueprint,
        "result.md",
        declaration="theorem",
        statement="formalized",
        lean="Project.result",
    )

    result = audit_blueprint(blueprint, lean_root=tmp_path / "absent")

    assert [(finding.article_path, finding.code) for finding in result.findings] == [
        (".", "invalid-lean-root")
    ]


def test_audit_reports_coverage_gaps_provable_from_files(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "result.md", declaration="theorem")

    missing = audit_blueprint(blueprint)
    assert [(finding.article_path, finding.code) for finding in missing.findings] == [
        ("coverage/README.md", "missing-coverage-contract")
    ]

    _coverage(
        blueprint,
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Completed result | MAPPED | [Missing](../roadmap/nope.md) |\n"
        "| Experiments | OUT | [Remote](//example.invalid/coverage) |",
    )
    findings = _finding_map(blueprint)["coverage/README.md"]

    assert findings == [
        ("coverage-not-found", "coverage link does not resolve to a file: '../roadmap/nope.md' (line 5)"),
        (
            "declared-coverage-gap",
            "coverage area 'Completed result' is mapped but not dispositioned (line 5)",
        ),
        (
            "unsupported-coverage-link",
            "coverage link uses a network location: '//example.invalid/coverage' (line 6)",
        ),
    ]


def test_unreadable_coverage_reason_is_stable(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "result.md", declaration="theorem")
    coverage = _coverage(blueprint)
    coverage.write_bytes(b"\xff")

    result = audit_blueprint(blueprint)

    finding = next(item for item in result.findings if item.code == "invalid-coverage-contract")
    assert finding.reason == "coverage contract cannot be read as UTF-8"
    assert not any(item.code == "unreadable-coverage-file" for item in result.findings)
    assert str(tmp_path) not in result.to_json()


def test_audit_checks_all_supplemental_coverage_markdown(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "result.md", declaration="theorem")
    _coverage(blueprint)
    notes = blueprint / "coverage" / "notes" / "details.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("[Missing](../../roadmap/absent.md)\n", encoding="utf-8")
    (blueprint / "coverage" / "binary.md").write_bytes(b"\xff")

    findings = _finding_map(blueprint)

    assert findings["coverage/binary.md"] == [
        ("unreadable-coverage-file", "coverage file cannot be read as UTF-8")
    ]
    assert findings["coverage/notes/details.md"] == [
        (
            "coverage-not-found",
            "coverage link does not resolve to a file: '../../roadmap/absent.md' (line 1)",
        )
    ]


def test_supplemental_coverage_checks_run_when_contract_is_invalid(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "result.md", declaration="theorem")
    _coverage(blueprint, "not a contract")
    notes = blueprint / "coverage" / "notes.md"
    notes.write_text(
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Supplemental | OUT | [Missing](../roadmap/absent.md) |\n",
        encoding="utf-8",
    )

    findings = _finding_map(blueprint)

    assert [code for code, _ in findings["coverage/README.md"]] == [
        "invalid-coverage-contract"
    ]
    assert findings["coverage/notes.md"] == [
        (
            "coverage-not-found",
            "coverage link does not resolve to a file: '../roadmap/absent.md' (line 3)",
        )
    ]


def test_invalid_blueprint_result_does_not_leak_host_path(tmp_path: Path) -> None:
    result = audit_blueprint(tmp_path / "absent")

    assert not result.clean
    assert str(tmp_path) not in result.to_json()
    assert len(result.findings) == 1
    assert result.findings[0].article_path == "."
    assert result.findings[0].reason == "blueprint directory does not exist: ."


def test_audit_preserves_valid_coverage_when_graph_is_invalid(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "bad.md", declaration="theorem")
    (blueprint / "roadmap/bad.md").write_text("# First\n# Second\n", encoding="utf-8")

    result = audit_blueprint(blueprint)

    assert result.coverage is not None
    assert result.coverage.counts["OUT"] == 1
    assert any(finding.code == "invalid-graph" for finding in result.findings)


def test_audit_returns_graph_validation_errors_with_article_paths(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "bad.md", declaration="theorem")
    path = blueprint / "roadmap" / "bad.md"
    path.write_text("---\n---\nNo H1\n", encoding="utf-8")

    result = audit_blueprint(blueprint)

    assert not result.clean
    assert result.findings[0].article_path == "roadmap/bad.md"
    assert result.findings[0].code == "invalid-graph"
    assert result.findings[0].reason == "bad: missing H1 title"
    assert json.loads(result.to_json()) == result.as_dict()


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="permissions do not bind root")
def test_audit_reports_a_chapter_it_can_list_but_not_enter(tmp_path: Path) -> None:
    """Looking for each unreadable page's article path inside the chapter raised a traceback."""
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "chapter/result.md", declaration="theorem")
    chapter = blueprint / "roadmap" / "chapter"
    chapter.chmod(0o644)
    try:
        result = audit_blueprint(blueprint)
    finally:
        chapter.chmod(0o755)

    assert [(finding.article_path, finding.code, finding.reason) for finding in result.findings] == [
        (
            f"roadmap/chapter/{name}",
            "invalid-graph",
            f"chapter/{name}: cannot read roadmap page: [Errno 13] Permission denied: './roadmap/chapter/{name}'",
        )
        for name in ("README.md", "result.md")
    ]


def test_audit_reports_a_container_holding_too_many_articles(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "README.md", depends=False)
    for index in range(25):
        _article(blueprint, f"unit-{index:02d}.md", declaration="theorem")

    findings = _finding_map(blueprint)["roadmap/README.md"]

    assert findings == [
        (
            "overfull-container",
            "article directly contains 25 articles, more than the 24-article limit; "
            "group them into chapters",
        )
    ]


def test_audit_reports_nodes_that_are_large_outliers_for_their_project(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "README.md", depends=False)
    spans = {f"Project.small{index}": 6 for index in range(5)}
    spans["Project.big"] = 420
    for name in spans:
        _article(
            blueprint,
            f"{name.rsplit('.', 1)[-1]}.md",
            declaration="theorem",
            statement="formalized",
            proof="formalized",
            lean=name,
        )
    lean_root = _lean_project(tmp_path, spans)

    findings = _finding_map(blueprint, lean_root=lean_root)

    assert findings == {
        "roadmap/big.md": [
            (
                "node-too-large",
                "node's Lean declarations span 420 lines against this project's "
                "6-line median; split it into pull-request-sized nodes",
            )
        ]
    }


def test_audit_measures_source_spans_from_the_captured_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "README.md", depends=False)
    spans = {f"Project.small{index}": 6 for index in range(5)}
    spans["Project.big"] = 420
    for name in spans:
        _article(
            blueprint,
            f"{name.rsplit('.', 1)[-1]}.md",
            declaration="theorem",
            statement="formalized",
            proof="formalized",
            lean=name,
        )
    lean_root = _lean_project(tmp_path, spans)
    real_snapshot_project_sources = audit_module.snapshot_project_sources

    def snapshot_then_mutate(root: Path):
        snapshot = real_snapshot_project_sources(root)
        (root / "big.lean").write_text(
            "theorem Project.big : True := trivial\n", encoding="utf-8"
        )
        return snapshot

    monkeypatch.setattr(audit_module, "snapshot_project_sources", snapshot_then_mutate)

    findings = _finding_map(blueprint, lean_root=lean_root)

    assert findings["roadmap/big.md"] == [
        (
            "node-too-large",
            "node's Lean declarations span 420 lines against this project's "
            "6-line median; split it into pull-request-sized nodes",
        )
    ]


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        (None, "Lean sources could not be indexed"),
        (
            "permission denied: Project/Secret.lean",
            "Lean sources could not be indexed: permission denied: Project/Secret.lean",
        ),
    ],
)
def test_audit_reports_source_index_io_failure_without_host_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str | None, message: str
) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "result.md", declaration="theorem", lean="Project.result")
    lean_root = tmp_path / "lean"
    lean_root.mkdir()

    def fail_snapshot(root: Path):
        if reason is not None:
            raise LeanSourceError(reason)
        raise OSError(f"private host detail: {root}")

    monkeypatch.setattr(audit_module, "snapshot_project_sources", fail_snapshot)

    result = audit_blueprint(blueprint, lean_root=lean_root)

    assert [(finding.article_path, finding.code, finding.reason) for finding in result.findings] == [
        (".", "unreadable-lean-sources", message)
    ]
    assert str(tmp_path) not in result.to_json()


def test_audit_measures_node_size_against_the_project_rather_than_a_fixed_limit(
    tmp_path: Path,
) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "README.md", depends=False)
    spans = {f"Project.long{index}": 300 for index in range(5)}
    for name in spans:
        _article(
            blueprint,
            f"{name.rsplit('.', 1)[-1]}.md",
            declaration="theorem",
            statement="formalized",
            proof="formalized",
            lean=name,
        )
    lean_root = _lean_project(tmp_path, spans)

    result = audit_blueprint(blueprint, lean_root=lean_root)

    # Every node clears the absolute floor, but none is an outlier here, so a
    # project whose units are uniformly long is not gated on its own norm.
    assert result.clean


def test_audit_is_read_only(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "result.md", declaration="theorem")
    before = {
        path.relative_to(blueprint).as_posix(): path.read_bytes()
        for path in sorted(blueprint.rglob("*"))
        if path.is_file()
    }

    audit_blueprint(blueprint)

    after = {
        path.relative_to(blueprint).as_posix(): path.read_bytes()
        for path in sorted(blueprint.rglob("*"))
        if path.is_file()
    }
    assert after == before


def test_an_explicit_attr_list_anchor_resolves(tmp_path: Path) -> None:
    """`attr_list` is enabled in the generated config, so `{#id}` is the anchor.

    Deriving the slug from the heading text regardless reported a link that
    MkDocs renders correctly as source-anchor-not-found.
    """
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "README.md", depends=False)
    _article(
        blueprint,
        "cited.md",
        declaration="theorem",
        origin="cited",
        sources=("../sources/paper.md#main-result",),
    )
    paper = blueprint / "sources" / "paper.md"
    paper.parent.mkdir(parents=True, exist_ok=True)
    paper.write_text(
        "---\n---\n\n# Paper\n\n## A result {#main-result}\n\nText.\n",
        encoding="utf-8",
    )

    codes = {finding.code for finding in audit_blueprint(blueprint).findings}

    assert "source-anchor-not-found" not in codes


def test_an_attr_list_that_does_more_than_name_a_heading_is_refused(tmp_path: Path, capsys) -> None:
    """An article's attribute list may give a heading its id and nothing
    else: a class or another attribute could restyle the page, so check
    refuses it by line and says what to keep."""
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "README.md", depends=False)
    _article(
        blueprint,
        "cited.md",
        "## A result {#main-result .highlight data-kind=result}\n\nText.",
        declaration="theorem",
    )

    assert main(["check", str(blueprint)]) == 1
    assert (
        "error: cited: line 7: attribute list {#main-result .highlight data-kind=result} is not allowed: "
        'an article may only give a heading an id, as in "## Title {#title}"; delete it or keep only a '
        "heading's id\n"
    ) in capsys.readouterr().out


def test_audit_reads_article_text_only_as_its_graph_parsed_it(tmp_path: Path, monkeypatch) -> None:
    """An article rewritten after the graph was loaded is not audited as a blend.

    Before the write the article has no '## Depends on' section; the write adds
    one but marks the proof formalized without the statement. Each state has a
    finding, but the old metadata beside the new text has none, so auditing that
    blend would report a clean roadmap that no file ever held. The audit judges
    the state the graph loaded.
    """
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "result.md", depends=False, declaration="theorem")
    assert _finding_map(blueprint) == {
        "roadmap/result.md": [
            ("missing-depends-section", "formalizable article has no explicit '## Depends on' section")
        ]
    }

    def load_then_rewrite(*args, **kwargs):
        graph = load_graph(*args, **kwargs)
        _article(blueprint, "result.md", declaration="theorem", proof="formalized")
        return graph

    monkeypatch.setattr("autoform_cli.audit.load_graph", load_then_rewrite)
    findings = _finding_map(blueprint)
    monkeypatch.undo()

    assert {path: [code for code, _reason in items] for path, items in findings.items()} == {
        "roadmap/result.md": ["missing-depends-section"]
    }
    assert {code for code, _reason in _finding_map(blueprint)["roadmap/result.md"]} == {"proof-without-statement"}
