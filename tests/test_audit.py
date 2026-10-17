from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import autoform_cli.audit as audit_module
from autoform_cli.audit import audit_blueprint, audit_graph
from autoform_cli.elaboration_limits import limits_in_force
from autoform_cli.graph import Graph, Node, load_graph
from autoform_cli.lean import LeanSourceError, index_project


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


def test_audit_rejects_implementation_notes_left_after_proof(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    article_id = "af_0123456789abcdef01234567"
    _article(
        blueprint,
        "result.md",
        article_id=article_id,
        declaration="theorem",
        statement="formalized",
        proof="formalized",
        lean="Project.result",
    )
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    note = notes / f"{article_id}.md"
    note.write_text("route\n", encoding="utf-8")

    findings = _finding_map(blueprint)["roadmap/result.md"]
    assert findings == [
        (
            "stale-implementation-note",
            f"completed implementation scope still has .implementation-notes/{article_id}.md; delete it",
        )
    ]

    note.unlink()
    assert audit_blueprint(blueprint).clean


def test_audit_allows_implementation_notes_while_work_is_unproved(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    article_id = "af_0123456789abcdef01234567"
    _article(
        blueprint,
        "result.md",
        article_id=article_id,
        declaration="theorem",
        statement="formalized",
        lean="Project.result",
    )
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / f"{article_id}.md").write_text("next route\n", encoding="utf-8")

    assert audit_blueprint(blueprint).clean


@pytest.mark.parametrize(("proof_complete", "stale"), [(False, False), (True, True)])
def test_container_note_is_stale_when_its_formalizable_subtree_is_proved(
    tmp_path: Path, proof_complete: bool, stale: bool
) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    container_id = "af_0123456789abcdef01234567"
    _article(blueprint, "chapter/README.md", article_id=container_id)
    child_metadata = {
        "article_id": "af_89abcdef0123456701234567",
        "declaration": "theorem",
        "statement": "formalized",
        "lean": "Project.result",
    }
    if proof_complete:
        child_metadata["proof"] = "formalized"
    _article(blueprint, "chapter/result.md", **child_metadata)
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / f"{container_id}.md").write_text("chapter route\n", encoding="utf-8")

    codes = {finding.code for finding in audit_blueprint(blueprint).findings}

    assert ("stale-implementation-note" in codes) is stale


def test_container_note_stays_while_a_nested_formalizable_article_is_unproved(
    tmp_path: Path,
) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    container_id = "af_0123456789abcdef01234567"
    _article(blueprint, "chapter/README.md", article_id=container_id)
    _article(
        blueprint,
        "chapter/done.md",
        article_id="af_100000000000000000000000",
        declaration="theorem",
        statement="formalized",
        proof="formalized",
        lean="Project.done",
    )
    _article(blueprint, "chapter/section/README.md", article_id="af_200000000000000000000000")
    _article(
        blueprint,
        "chapter/section/open.md",
        article_id="af_300000000000000000000000",
        declaration="theorem",
        statement="formalized",
        lean="Project.open",
    )
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / f"{container_id}.md").write_text("chapter route\n", encoding="utf-8")

    assert audit_blueprint(blueprint).clean


@pytest.mark.parametrize("leaf", ["a.md", "z.md"])
def test_container_note_stays_while_a_direct_article_is_unproved_beside_a_proved_section(
    tmp_path: Path, leaf: str
) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    container_id = "af_0123456789abcdef01234567"
    _article(blueprint, "chapter/README.md", article_id=container_id)
    _article(
        blueprint,
        f"chapter/{leaf}",
        article_id="af_100000000000000000000000",
        declaration="theorem",
        statement="formalized",
        lean="Project.open",
    )
    _article(blueprint, "chapter/section/README.md", article_id="af_200000000000000000000000")
    _article(
        blueprint,
        "chapter/section/done.md",
        article_id="af_300000000000000000000000",
        declaration="theorem",
        statement="formalized",
        proof="formalized",
        lean="Project.done",
    )
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / f"{container_id}.md").write_text("chapter route\n", encoding="utf-8")

    assert audit_blueprint(blueprint).clean


@pytest.mark.parametrize("unproved_first", [True, False])
def test_container_note_stays_while_any_sibling_is_unproved(
    tmp_path: Path, unproved_first: bool
) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    container_id = "af_0123456789abcdef01234567"
    _article(blueprint, "chapter/README.md", article_id=container_id)
    proved = {"declaration": "theorem", "statement": "formalized", "proof": "formalized"}
    unproved = {"declaration": "theorem", "statement": "formalized"}
    first, second = (unproved, proved) if unproved_first else (proved, unproved)
    _article(blueprint, "chapter/a.md", article_id="af_100000000000000000000000", lean="Project.a", **first)
    _article(blueprint, "chapter/b.md", article_id="af_200000000000000000000000", lean="Project.b", **second)
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / f"{container_id}.md").write_text("chapter route\n", encoding="utf-8")

    assert audit_blueprint(blueprint).clean


@pytest.mark.parametrize("layout", ["deferred-leaf", "deferred-container", "proved-deferred-leaf"])
def test_a_container_note_counts_a_leaf_marked_not_ready_as_the_site_does(tmp_path: Path, layout: str) -> None:
    # The site keeps a scope open for a leaf marked not_ready until it is proved, but not for a container.
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    container_id = "af_0123456789abcdef01234567"
    _article(blueprint, "chapter/README.md", article_id=container_id)
    proved = {"declaration": "theorem", "statement": "formalized", "proof": "formalized"}
    if layout != "proved-deferred-leaf":
        _article(
            blueprint, "chapter/done.md", article_id="af_100000000000000000000000", lean="Project.done", **proved
        )
    if layout == "deferred-leaf":
        _article(blueprint, "chapter/later.md", article_id="af_200000000000000000000000", not_ready="true")
    elif layout == "deferred-container":
        _article(
            blueprint, "chapter/section/README.md", article_id="af_300000000000000000000000", not_ready="true"
        )
        _article(
            blueprint,
            "chapter/section/also.md",
            article_id="af_400000000000000000000000",
            lean="Project.also",
            **proved,
        )
    else:  # the chapter's only target: a proved leaf still marked not_ready
        _article(
            blueprint,
            "chapter/later.md",
            article_id="af_200000000000000000000000",
            lean="Project.later",
            not_ready="true",
            **proved,
        )
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / f"{container_id}.md").write_text("chapter route\n", encoding="utf-8")

    findings = {(finding.article_path, finding.code) for finding in audit_blueprint(blueprint).findings}

    stale = {("roadmap/chapter/README.md", "stale-implementation-note")}
    assert findings == (set() if layout == "deferred-leaf" else stale)


def test_container_note_without_formalizable_descendants_is_not_stale(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    container_id = "af_0123456789abcdef01234567"
    _article(blueprint, "chapter/README.md", article_id=container_id)
    _article(blueprint, "chapter/overview.md", article_id="af_89abcdef0123456701234567")
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / f"{container_id}.md").write_text("chapter plan\n", encoding="utf-8")

    codes = {finding.code for finding in audit_blueprint(blueprint).findings}

    assert "stale-implementation-note" not in codes


def test_container_note_audit_handles_deep_containment_iteratively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blueprint = tmp_path / "blueprint"
    notes = blueprint / ".implementation-notes"
    notes.mkdir(parents=True)
    article_id = "af_0123456789abcdef01234567"
    (notes / f"{article_id}.md").write_text("deep route\n", encoding="utf-8")
    nodes: dict[str, Node] = {}
    for index in range(1_200):
        node_id = f"n{index}"
        nodes[node_id] = Node(
            id=node_id,
            title=node_id,
            path=blueprint / "roadmap" / f"{node_id}.md",
            dependencies=(),
            parent=f"n{index - 1}" if index else None,
            depth=index,
            article_id=article_id if index == 0 else None,
            declaration="theorem" if index == 1_199 else None,
            statement_formalized=index == 1_199,
            proof_formalized=index == 1_199,
            lean="Project.result" if index == 1_199 else None,
        )
    graph = Graph(blueprint_dir=blueprint, nodes=nodes)
    monkeypatch.setattr(
        audit_module,
        "_read_article",
        lambda _path: audit_module._ArticleShape("A precise mathematical statement.", True),
    )

    result = audit_graph(graph, coverage_findings=[])

    assert any(finding.code == "stale-implementation-note" for finding in result.findings)


@pytest.mark.parametrize(
    "filename",
    [
        "af_111111111111111111111111.md",
        "bad name.md",
        pytest.param(
            "bad:name.md",
            marks=pytest.mark.skipif(os.name == "nt", reason="colon is not a Windows filename"),
        ),
    ],
)
def test_audit_reports_invalid_note_at_its_blueprint_path(
    tmp_path: Path, filename: str
) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "result.md")
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / filename).write_text("orphan\n", encoding="utf-8")

    result = audit_blueprint(blueprint)

    assert [(finding.article_path, finding.code) for finding in result.findings] == [
        (f".implementation-notes/{filename}", "invalid-graph")
    ]


def test_audit_reports_a_misplaced_notes_directory_where_it_is(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    article_id = "af_0123456789abcdef01234567"
    _article(blueprint, "result.md", article_id=article_id, declaration="theorem")
    misplaced = blueprint / "roadmap" / ".implementation-notes"
    misplaced.mkdir()
    (misplaced / f"{article_id}.md").write_text("Mathlib gap.\n", encoding="utf-8")

    paths = {finding.article_path for finding in audit_blueprint(blueprint).findings}

    assert f"roadmap/.implementation-notes/{article_id}.md" in paths
    assert ".implementation-notes" not in paths


def test_audit_reports_formalizable_structure(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(
        blueprint,
        "chapter/README.md",
        prose="",
        depends=False,
        declaration="theorem",
    )
    _article(blueprint, "chapter/child.md", declaration="lemma")

    findings = _finding_map(blueprint)["roadmap/chapter/README.md"]
    codes = {code for code, _reason in findings}

    assert codes == {
        "formalizable-container",
        "missing-depends-section",
        "missing-statement-text",
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
    (blueprint / ".drafts").mkdir()
    (blueprint / ".drafts" / "notes.md").write_text("# Notes\n", encoding="utf-8")
    _article(
        blueprint,
        "chapter/result.md",
        declaration="theorem",
        origin="cited",
        sources=(
            "../../sources/paper.md#theorem",
            "../../sources/missing.md",
            "../../../outside.md",
            "../../.drafts/notes.md",
            "https://example.invalid/paper",
            "ftp://example.invalid/paper",
            "//example.invalid/paper",
            "%00",
            "http://[oops/paper",
        ),
    )

    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("audit attempted network access")

    monkeypatch.setattr("socket.create_connection", fail_network)
    findings = _finding_map(blueprint)["roadmap/chapter/result.md"]

    assert findings == [
        ("hidden-source-target", "source link names a hidden path, which render omits: '../../.drafts/notes.md'"),
        ("malformed-source-link", "source link contains an invalid path: '%00'"),
        ("malformed-source-link", "source link is not a valid URL: 'http://[oops/paper'"),
        ("source-escapes-blueprint", "source link escapes the blueprint: '../../../outside.md'"),
        ("source-not-found", "source link does not resolve to a file: '../../sources/missing.md'"),
        ("unsupported-source-link", "source link uses a network location: '//example.invalid/paper'"),
        ("unsupported-source-link", "source link uses unsupported scheme: 'ftp://example.invalid/paper'"),
    ]


def test_audit_reports_a_source_link_through_a_symlink_loop_alike_on_every_python(tmp_path: Path) -> None:
    # Before Python 3.13 resolving a loop raises; from 3.13 it returns the looping path.
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    (blueprint / "sources").mkdir()
    try:
        (blueprint / "sources" / "loop.md").symlink_to("loop.md")
        (tmp_path / "outer.md").symlink_to("outer.md")
    except OSError:
        pytest.skip("symlinks are unavailable")
    sources = ("../sources/loop.md", "../../outer.md")
    _article(blueprint, "result.md", declaration="theorem", origin="cited", sources=sources)

    assert _finding_map(blueprint)["roadmap/result.md"] == [
        ("source-escapes-blueprint", "source link escapes the blueprint: '../../outer.md'"),
        ("source-not-found", "source link does not resolve to a file: '../sources/loop.md'"),
    ]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="permissions do not bind root")
def test_audit_reports_a_source_link_under_an_unreadable_directory_as_missing(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    sources = ("../sources/locked/paper.txt",)
    _article(blueprint, "result.md", declaration="theorem", origin="cited", sources=sources)
    locked = blueprint / "sources" / "locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        findings = _finding_map(blueprint)["roadmap/result.md"]
    finally:
        locked.chmod(0o755)

    assert findings == [("source-not-found", "source link does not resolve to a file: '../sources/locked/paper.txt'")]


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
    assert with_lean["roadmap/wrong-kind.md"] == [
        ("lean-target-kind-mismatch", "Lean target kind def does not match declaration intent theorem")
    ]


def test_audit_checks_the_public_declaration_beside_an_earlier_private_twin(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "result.md", declaration="theorem", statement="formalized", lean="Project.result")
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "A.lean").write_text("private def Project.result : Nat := 1\n", encoding="utf-8")
    (lean_root / "B.lean").write_text("theorem Project.result : True := trivial\n", encoding="utf-8")

    assert "roadmap/result.md" not in _finding_map(blueprint, lean_root=lean_root)


def test_audit_reports_a_target_defined_more_than_once_and_checks_nothing_else_of_it(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "result.md", declaration="theorem", statement="formalized", lean="Project.result")
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "A.lean").write_text("def Project.result : Nat := 1\n", encoding="utf-8")
    (lean_root / "B.lean").write_text("private def Project.result : Nat := 1\n", encoding="utf-8")
    (lean_root / "C.lean").write_text("\ndef Project.result : Nat := 1\n", encoding="utf-8")

    assert _finding_map(blueprint, lean_root=lean_root)["roadmap/result.md"] == [
        (
            "lean-target-ambiguous",
            "Lean declaration target is defined more than once: Project.result (A.lean:1, B.lean:1, C.lean:2)",
        )
    ]


def test_audit_accepts_wanted_targets_as_statements_but_not_proofs(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    articles = {
        "stated.md": {"declaration": "theorem", "statement": "formalized", "lean": "W.stated"},
        "corollary.md": {"declaration": "corollary", "statement": "formalized", "lean": "W.legacy"},
        "proved.md": {"declaration": "theorem", "statement": "formalized", "proof": "formalized", "lean": "W.proved"},
        "signature.md": {"declaration": "def", "lean": "W.signature"},
        "defined.md": {"declaration": "definition", "statement": "formalized", "lean": "W.defined"},
        "instance.md": {"declaration": "instance", "lean": "W.instFoo"},
        "wrong-kind.md": {"declaration": "theorem", "statement": "formalized", "lean": "W.signature"},
    }
    for relative, metadata in articles.items():
        _article(blueprint, relative, **metadata)
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    source = [
        "namespace W",
        "public theorem_wanted stated : True",
        "proof_wanted legacy : True",
        "theorem_wanted proved : True",
        "def_wanted signature : Nat",
        "def_wanted defined : Nat",
        "instance_wanted instFoo : Inhabited Nat",
        "end W",
    ]
    (lean_root / "Wanted.lean").write_text("\n".join(source) + "\n", encoding="utf-8")

    def wanted(name: str, keyword: str, claim: str) -> list[tuple[str, str]]:
        return [
            (
                "lean-target-wanted",
                f"lean target {name} is a {keyword}, which defers its proof or construction, so it cannot back {claim}",
            )
        ]

    assert _finding_map(blueprint, lean_root=lean_root) == {
        "roadmap/proved.md": wanted("W.proved", "theorem_wanted", "proof: formalized"),
        "roadmap/defined.md": wanted(
            "W.defined", "def_wanted", "statement: formalized, which proves a definition"
        ),
        "roadmap/wrong-kind.md": [
            ("lean-target-kind-mismatch", "Lean target kind def_wanted does not match declaration intent theorem")
        ],
    }


def test_audit_reports_lean_targets_declared_deprecated(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    targets = {
        "same-line.md": "Project.sameLine",
        "line-above.md": "Project.lineAbove",
        "multi-line.md": "Project.multiLine",
        "public.md": "Project.publicOld",
        "fresh.md": "Project.fresh",
        "decoy.md": "Project.decoy",
        "after-previous.md": "Project.afterPrevious",
        "old-alias.md": "Project.oldAlias",
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
        "@[deprecated Project.fresh]",
        "public theorem Project.publicOld : True := trivial",
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
        "roadmap/public.md": deprecated("Project.publicOld"),
        "roadmap/old-alias.md": deprecated("Project.oldAlias"),
    }


def test_audit_resolves_an_alias_s_target_as_lean_does(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    # Each intent is wrong for the declaration the alias reaches, so every
    # alias the audit resolves shows as a finding. The last five cannot be
    # placed, so any intent matches them.
    targets = {
        "same-namespace.md": ("definition", "Project.sameNamespace"),
        "chained.md": ("theorem", "Project.chained"),
        "private.md": ("definition", "Project.revealed"),
        "later.md": ("definition", "Project.early"),
        "iff.md": ("definition", "Project.forward"),
        "local-iff.md": ("definition", "Project.axForward"),
        "alias-of-iff.md": ("definition", "Project.axForward'"),
        "inner-first.md": ("definition", "Project.Inner.innerFirst"),
        "protected.md": ("definition", "Project.skipsProtected"),
        "outer-only.md": ("definition", "Project.Inner.outerOnly"),
        "other-file.md": ("definition", "Project.remote"),
        "shadowed.md": ("theorem", "Nat.myLemma"),
        "external.md": ("definition", "Project.external"),
    }
    for relative, (declaration, name) in targets.items():
        _article(blueprint, relative, declaration=declaration, statement="formalized", lean=name)
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    source = [
        "theorem t : True := trivial",
        "theorem later : True := trivial",
        "def succ_le_succ : Nat := 0",
        "namespace Project",
        "theorem base : True := trivial",
        "def value : Nat := 0",
        "protected def t : Nat := 0",
        "private theorem secret : True := trivial",
        "axiom ax_iff : True ↔ True",
        "alias sameNamespace := base",
        "alias middle := value",
        "alias chained := middle",
        "alias revealed := secret",
        # `Project.later` does not exist yet where the alias is elaborated.
        "alias early := later",
        "def later : Nat := 0",
        # Batteries makes each name of an iff alias a theorem, and an alias of one.
        "alias ⟨forward, _⟩ := Nat.lt_iff_add_one_le",
        "alias ⟨axForward, _⟩ := ax_iff",
        "alias axForward' := axForward",
        # Lean skips `Project.t` here unless the module system made it private.
        "alias skipsProtected := t",
        "alias external := Nat.succ_le_succ",
        "namespace Inner",
        "theorem value : True := trivial",
        "alias innerFirst := value",
        "alias outerOnly := base",
        "end Inner",
        "end Project",
        # Lean means Mathlib's `Nat.succ_le_succ` here, not the root `def`.
        "namespace Nat",
        "alias myLemma := succ_le_succ",
        "end Nat",
    ]
    (lean_root / "Aliases.lean").write_text("\n".join(source) + "\n", encoding="utf-8")
    # Below `Project.base`'s line, so only the file tells them apart.
    (lean_root / "Other.lean").write_text(
        "\n" * 9 + "namespace Project\nalias remote := base\nend Project\n", encoding="utf-8"
    )

    def mismatch(kind: str, intent: str) -> list[tuple[str, str]]:
        reason = f"Lean target kind alias of {kind} does not match declaration intent {intent}"
        return [("lean-target-kind-mismatch", reason)]

    assert _finding_map(blueprint, lean_root=lean_root) == {
        "roadmap/same-namespace.md": mismatch("theorem", "definition"),
        "roadmap/chained.md": mismatch("def", "theorem"),
        "roadmap/private.md": mismatch("theorem", "definition"),
        "roadmap/later.md": mismatch("theorem", "definition"),
        "roadmap/iff.md": mismatch("theorem", "definition"),
        "roadmap/local-iff.md": mismatch("theorem", "definition"),
        "roadmap/alias-of-iff.md": mismatch("theorem", "definition"),
        "roadmap/inner-first.md": mismatch("theorem", "definition"),
    }


def test_audit_reads_an_alias_s_limits_and_wanted_command_from_its_target(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "small.md", declaration="theorem", statement="formalized", lean="Project.small")
    _article(
        blueprint, "pending.md", declaration="theorem", statement="formalized", proof="formalized", lean="Project.ofWanted"
    )
    _article(blueprint, "forward.md", declaration="theorem", statement="formalized", lean="Project.bigForward")
    _article(blueprint, "wide.md", declaration="theorem", statement="formalized", lean="wide")
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    source = [
        "namespace Project",
        "set_option maxHeartbeats 0 in",
        "theorem big : True := by",
        "  trivial",
        "alias small := big",
        "theorem_wanted pending : True",
        "alias ofWanted := pending",
        "set_option maxHeartbeats 0 in",
        "theorem big_iff : True ↔ True := Iff.rfl",
        "alias ⟨bigForward, _⟩ := big_iff",
        "end Project",
    ]
    (lean_root / "Aliases.lean").write_text("\n".join(source) + "\n", encoding="utf-8")
    # An alias the index cannot place still has its own limits.
    (lean_root / "Wide.lean").write_text("set_option maxHeartbeats 400000\nalias wide := Nat.succ_le_succ\n", encoding="utf-8")

    assert _finding_map(blueprint, lean_root=lean_root) == {
        "roadmap/small.md": [
            (
                "lean-target-unbounded-limit",
                "lean target Project.big has no maxHeartbeats limit: `set_option maxHeartbeats 0` at "
                "Aliases.lean:2; set a finite limit",
            )
        ],
        "roadmap/pending.md": [
            (
                "lean-target-wanted",
                "lean target Project.ofWanted is an alias of a theorem_wanted, which defers its proof or "
                "construction, so it cannot back proof: formalized",
            )
        ],
        "roadmap/forward.md": [
            (
                "lean-target-unbounded-limit",
                "lean target Project.big_iff has no maxHeartbeats limit: `set_option maxHeartbeats 0` at "
                "Aliases.lean:8; set a finite limit",
            )
        ],
        "roadmap/wide.md": [
            (
                "lean-target-unscoped-limit",
                "lean target wide inherits the file-wide `set_option maxHeartbeats 400000` at Wide.lean:1; "
                "put `set_option maxHeartbeats N in` on the declarations that need it",
            )
        ],
    }


def test_audit_measures_an_alias_by_its_target_s_span(tmp_path: Path) -> None:
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
            # The target named beside its alias still counts once.
            lean="Project.bigAlias Project.big" if name == "Project.big" else name,
        )
    lean_root = _lean_project(tmp_path, spans)
    with (lean_root / "big.lean").open("a", encoding="utf-8") as source:
        source.write("alias Project.bigAlias := Project.big\n")

    assert _finding_map(blueprint, lean_root=lean_root) == {
        "roadmap/big.md": [
            (
                "node-too-large",
                "node's Lean declarations span 420 lines against this project's "
                "6-line median; split it into pull-request-sized nodes",
            )
        ]
    }


def test_an_alias_ends_the_source_span_of_the_declaration_above_it(tmp_path: Path) -> None:
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "Big.lean").write_text(
        "theorem big : True := by\n  trivial\nalias small := big\n\n", encoding="utf-8"
    )

    spans = audit_module._source_spans(index_project(lean_root))
    assert {declaration.name: span for declaration, span in spans.items()} == {"big": 2, "small": 2}


def test_a_private_twin_ends_the_source_span_of_the_declaration_above_it(tmp_path: Path) -> None:
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    # The public twin sorts before Big.lean.
    (lean_root / "A.lean").write_text("theorem helper : True := trivial\n", encoding="utf-8")
    (lean_root / "Big.lean").write_text(
        "theorem big : True := by\n  trivial\nprivate theorem helper : True := by\n  trivial\n", encoding="utf-8"
    )

    spans = audit_module._source_spans(index_project(lean_root))
    assert sorted((key.path.as_posix(), key.line, span) for key, span in spans.items()) == [
        ("A.lean", 1, 1),
        ("Big.lean", 1, 2),
        ("Big.lean", 3, 2),
    ]


def test_audit_reports_unbounded_and_inherited_elaboration_limits(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    names = (
        "fileWide",
        "scoped",
        "chained",
        "consumer",
        "afterConsumer",
        "lowered",
        "sectionWide",
        "afterSection",
        "unbounded",
    )
    for name in names:
        _article(blueprint, f"{name}.md", declaration="theorem", statement="formalized", lean=f"Project.{name}")
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    source = [
        "set_option maxHeartbeats 800000",
        "",
        "namespace Project",
        "",
        "theorem fileWide : True := trivial",
        "",
        "set_option maxHeartbeats 400000 in",
        "-- `simp` needs the extra budget here",
        "theorem scoped : True := trivial",
        "",
        "open Nat in",
        "set_option synthInstance.maxHeartbeats 0 in",
        "theorem chained : True := trivial",
        "",
        # A wrapper reaches only the next declaration.
        "set_option maxHeartbeats 400000 in",
        "theorem consumer : True := trivial",
        "theorem afterConsumer : True := trivial",
        "",
        "section",
        "set_option maxHeartbeats 100000",
        "theorem lowered : True := by",
        "  set_option maxHeartbeats 0 in",
        "  trivial",
        "end",
        "",
        "end Project",
        "",
        "@[expose] public noncomputable section",
        "set_option maxRecDepth 4096",
        "theorem Project.sectionWide : True := trivial",
        "end",
        "",
        "-- set_option maxHeartbeats 0",
        "theorem Project.afterSection : True := trivial",
        "",
        "set_option maxHeartbeats 0",
        "set_option maxHeartbeats 0",
        "theorem Project.unbounded : True := trivial",
    ]
    (lean_root / "Limits.lean").write_text("\n".join(source) + "\n", encoding="utf-8")

    def unscoped(name: str, scope: str, option: str, line: int) -> tuple[str, str]:
        return (
            "lean-target-unscoped-limit",
            f"lean target Project.{name} inherits the {scope}-wide `set_option {option}` at Limits.lean:{line}; "
            f"put `set_option {option.split()[0]} N in` on the declarations that need it",
        )

    def unbounded(name: str, option: str, line: int) -> tuple[str, str]:
        return (
            "lean-target-unbounded-limit",
            f"lean target Project.{name} has no {option} limit: `set_option {option} 0` at Limits.lean:{line}; "
            "set a finite limit",
        )

    file_wide = ("file", "maxHeartbeats 800000", 1)
    assert _finding_map(blueprint) == {}
    assert _finding_map(blueprint, lean_root=lean_root) == {
        "roadmap/fileWide.md": [unscoped("fileWide", *file_wide)],
        "roadmap/chained.md": [
            unbounded("chained", "synthInstance.maxHeartbeats", 12),
            unscoped("chained", *file_wide),
        ],
        "roadmap/afterConsumer.md": [unscoped("afterConsumer", *file_wide)],
        "roadmap/lowered.md": [unbounded("lowered", "maxHeartbeats", 22)],
        "roadmap/sectionWide.md": [
            unscoped("sectionWide", *file_wide),
            unscoped("sectionWide", "section", "maxRecDepth 4096", 29),
        ],
        "roadmap/afterSection.md": [unscoped("afterSection", *file_wide)],
        "roadmap/unbounded.md": [unbounded("unbounded", "maxHeartbeats", 37)],
    }


def test_the_limit_scan_reads_commands_at_the_declaration_s_indentation() -> None:
    # This file indents its commands inside the namespace. A later wrapper is
    # not part of an earlier proof, and settings at that indentation count.
    lines = [
        "namespace Project",
        "  set_option maxHeartbeats 800000",
        "  theorem first : True := trivial",
        "",
        "  set_option maxHeartbeats 0 in",
        "  theorem second : True := by",
        "    set_option maxRecDepth 2000 in",
        "    trivial",
        "end Project",
    ]

    def found(line: int) -> list[tuple[str, int, str]]:
        return [(setting.option, setting.value, setting.scope) for setting in limits_in_force(lines, line)]

    assert found(3) == [("maxHeartbeats", 800000, "section")]
    assert found(6) == [("maxHeartbeats", 0, "declaration"), ("maxRecDepth", 2000, "proof")]


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
    kinds = (("def", "sealed"), ("definition", "sealed"), ("irreducible_def", "sealed"), ("irreducible_def", "plain"))
    for declaration, target in kinds:
        lean = f"Project.{target}"
        _article(blueprint, f"{declaration}-{target}.md", declaration=declaration, statement="formalized", lean=lean)
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
    """Checking each page raised a traceback, in load_graph and again in audit's search for its article path."""
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


def test_audit_points_a_noncanonical_readme_at_the_page_itself(tmp_path: Path) -> None:
    """A load error naming a page pointed at '<page>.md', here README.MD.md."""
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "chapter/result.md", declaration="theorem")
    chapter = blueprint / "roadmap" / "chapter"
    (chapter / "README.md").rename(chapter / "README.MD")

    result = audit_blueprint(blueprint)

    assert [finding.article_path for finding in result.findings if finding.reason.startswith("chapter/README.MD:")] == [
        "roadmap/chapter/README.MD"
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
        "---\n---\n\n# Paper\n\n## A result {#main-result .highlight data-kind=result}\n\nText.\n",
        encoding="utf-8",
    )

    codes = {finding.code for finding in audit_blueprint(blueprint).findings}

    assert "source-anchor-not-found" not in codes


@pytest.mark.parametrize(
    "prose",
    [
        "",
        "<!-- the statement goes here -->",
        "```lean\ntheorem draft : True := trivial\n```",
        "    theorem draft : True := trivial",
        "### Setting",
    ],
)
def test_audit_requires_statement_prose_outside_code_comments_and_headings(tmp_path: Path, prose: str) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "chapter/result.md", prose=prose, declaration="theorem")

    codes = {code for code, _reason in _finding_map(blueprint)["roadmap/chapter/result.md"]}

    assert codes == {"missing-statement-text"}


@pytest.mark.parametrize(
    "prose",
    [
        "### Setting\n\nLet $X$ be a set.",
        "Let $X$ be a set.\n\n```lean\ntheorem draft : True := trivial\n```",
    ],
)
def test_audit_accepts_a_statement_with_subheadings_or_code(tmp_path: Path, prose: str) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "chapter/result.md", prose=prose, declaration="theorem")

    assert audit_blueprint(blueprint).clean


def test_audit_counts_statement_prose_written_above_the_title(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    path = _article(blueprint, "chapter/result.md", prose="", declaration="theorem")
    path.write_text(
        "---\ndeclaration: theorem\n---\n\nLet $X$ be a set.\n\n# Result\n\n## Depends on\n\nNone.\n",
        encoding="utf-8",
    )

    assert audit_blueprint(blueprint).clean


def test_audit_ignores_a_depends_heading_that_is_not_published(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(
        blueprint,
        "chapter/result.md",
        prose="A statement.\n\n```\n## Depends on\n```",
        depends=False,
        declaration="theorem",
    )

    codes = {code for code, _reason in _finding_map(blueprint)["roadmap/chapter/result.md"]}

    assert codes == {"missing-depends-section"}


def test_audit_reads_the_depends_heading_without_regard_to_case(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(
        blueprint,
        "chapter/result.md",
        prose="A statement.\n\n## DEPENDS ON\n\nNone.",
        depends=False,
        declaration="theorem",
    )

    assert audit_blueprint(blueprint).clean


def test_audit_reports_a_depends_section_lost_behind_an_unclosed_comment(tmp_path: Path) -> None:
    # The site shows the sections after an unclosed comment inside the theorem
    # box, so the audit must not call the article complete.
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "chapter/result.md", prose="A statement.\n\n<!-- todo", declaration="theorem")

    codes = {code for code, _reason in _finding_map(blueprint)["roadmap/chapter/result.md"]}

    assert codes == {"missing-depends-section"}


def test_audit_does_not_read_a_comment_begun_on_the_title_line_as_prose(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    path = _article(blueprint, "chapter/result.md", prose="", declaration="theorem")
    path.write_text(
        "---\ndeclaration: theorem\n---\n\n# Result <!-- TODO:\nwrite the statement -->\n\n"
        "## Depends on\n\nNone.\n",
        encoding="utf-8",
    )

    codes = {code for code, _reason in _finding_map(blueprint)["roadmap/chapter/result.md"]}

    assert codes == {"missing-statement-text"}


def _statement_codes(tmp_path: Path, prose: str) -> set[str]:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "chapter/result.md", prose=prose, declaration="theorem")
    return {code for code, _reason in _finding_map(blueprint).get("roadmap/chapter/result.md", [])}


@pytest.mark.parametrize(
    "prose",
    [
        "TODO",
        "TODO: state it",
        "##### Setting\n\nTODO",
        "Setting\n=======\n\nTODO",
        "> ### Setting\n> TODO",
        "```lean\ntheorem draft : True := trivial\n```\n\nTODO",
        "```mermaid\ngraph TD\n```\n\nTODO",
        "TODO: tighten the bound.\n\nFor every $n$, $n + 0 = n$.",
        "<!-- For every $n$, $n + 0 = n$. -->\nTBD",
        "Unknown: whether $P = NP$.",
    ],
)
def test_audit_reports_a_placeholder_statement(tmp_path: Path, prose: str) -> None:
    assert _statement_codes(tmp_path, prose) == {"placeholder-statement-text"}


@pytest.mark.parametrize(
    "prose",
    [
        "<span hidden>For every $n$, $n + 0 = n$.</span>",
        "---",
        "[ ](missing.md)",
        "![diagram](diagram.png)",
        "...",
        "Setting\n=======",
        "> ### Setting",
        "- ### Setting",
    ],
)
def test_audit_reports_a_statement_that_shows_a_reader_nothing(tmp_path: Path, prose: str) -> None:
    assert _statement_codes(tmp_path, prose) == {"empty-statement-text"}


@pytest.mark.parametrize(
    "prose",
    [
        "Pending Mathlib PR 1234, this is `Nat.add_zero`.",
        "TODO state it",
        "$$\nn + 0 = n\n$$",
        "`Nat.add_zero`",
        "<div>\n\n    For all n.\n\n</div>",
        "TODO-lists form a monoid.",
        "Unknown-variance case: the sample mean is normal.",
    ],
)
def test_audit_accepts_a_statement_that_says_something(tmp_path: Path, prose: str) -> None:
    assert _statement_codes(tmp_path, prose) == set()


def test_audit_reads_a_comment_opener_in_inline_code_as_the_site_does(tmp_path: Path) -> None:
    # The site prints the code span and the sentence, so the statement is not
    # a placeholder. Splitting the article still takes ``<!--`` for a comment,
    # a separate fault, and not one in the statement.
    assert "placeholder-statement-text" not in _statement_codes(tmp_path, "TODO `<!--` real statement here")


def test_a_statement_from_a_fence_that_never_closes_keeps_its_statement_finding(tmp_path: Path) -> None:
    assert _statement_codes(tmp_path, "```\nFor all n, P(n) holds.") == {"unclosed-code-fence"}
    assert _statement_codes(tmp_path, "```\nTODO") == {
        "placeholder-statement-text",
        "unclosed-code-fence",
    }


def test_audit_reports_an_unclosed_code_fence(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    path = _article(blueprint, "chapter/result.md", declaration="theorem")
    path.write_text(path.read_text(encoding="utf-8") + "\n```lean\n#check result\n", encoding="utf-8")

    assert _finding_map(blueprint)["roadmap/chapter/result.md"] == [
        ("unclosed-code-fence", "code fence opened on line 13 never closes")
    ]


def test_audit_names_only_the_first_fence_that_never_closes(tmp_path: Path) -> None:
    # Read as text, the first unclosed fence lets the later ~~~ open a second one; only the first is named.
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    path = _article(blueprint, "chapter/result.md", declaration="theorem")
    text = path.read_text(encoding="utf-8")
    path.write_text(text + "\n```lean\n#check result\n\n~~~\n#check other\n", encoding="utf-8")

    assert _finding_map(blueprint)["roadmap/chapter/result.md"] == [
        ("unclosed-code-fence", "code fence opened on line 13 never closes")
    ]


def test_audit_reports_an_unclosed_code_fence_in_a_chapter_article(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "chapter/result.md", declaration="theorem")
    readme = blueprint / "roadmap" / "chapter" / "README.md"
    readme.write_text("# Chapter\n\n```text\nsketch\n", encoding="utf-8")

    assert ("unclosed-code-fence", "code fence opened on line 3 never closes") in _finding_map(blueprint)[
        "roadmap/chapter/README.md"
    ]


def test_audit_does_not_treat_inline_backticks_as_a_fence(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    path = _article(blueprint, "chapter/result.md", declaration="theorem")
    path.write_text(path.read_text(encoding="utf-8") + "\n```x``` is inline code here.\n", encoding="utf-8")

    codes = {
        code
        for code, _reason in _finding_map(blueprint).get(path.relative_to(blueprint).as_posix(), [])
    }
    assert "unclosed-code-fence" not in codes


def test_audit_graph_does_not_reread_an_article_for_fences(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    path = _article(blueprint, "chapter/result.md", declaration="theorem")
    graph = load_graph(blueprint)
    path.unlink()

    result = audit_module.audit_graph(graph)

    assert "unclosed-code-fence" not in {finding.code for finding in result.findings}


def test_audit_accepts_a_closed_code_fence_after_a_frontmatter_comment(tmp_path: Path) -> None:
    # Read with the body, the frontmatter's `<!--` would close at the arrow and the closing fence would open a new one.
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    path = _article(blueprint, "chapter/result.md", declaration="theorem")
    text = path.read_text(encoding="utf-8").replace("---\n", "---\n# <!-- draft\n", 1)
    path.write_text(text + "\n```mermaid\ngraph TD\n  A --> B\n```\n", encoding="utf-8")

    codes = {finding.code for finding in audit_blueprint(blueprint).findings}

    assert "unclosed-code-fence" not in codes


def test_audit_reports_an_unclosed_code_fence_after_a_frontmatter_comment(tmp_path: Path) -> None:
    # The frontmatter is not published, so a `<!--` there cannot comment out the fence below it.
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    path = _article(blueprint, "chapter/result.md", declaration="theorem")
    text = path.read_text(encoding="utf-8").replace("---\n", "---\n# <!-- draft\n", 1)
    path.write_text(text + "\n```lean\n#check result\n", encoding="utf-8")

    assert _finding_map(blueprint)["roadmap/chapter/result.md"] == [
        ("unclosed-code-fence", "code fence opened on line 14 never closes")
    ]


def test_statement_findings_say_what_is_wrong(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "chapter/none.md", prose="", declaration="theorem")
    _article(blueprint, "chapter/todo.md", prose="TODO", declaration="theorem")
    _article(blueprint, "chapter/rule.md", prose="---", declaration="theorem")

    findings = _finding_map(blueprint)

    assert findings["roadmap/chapter/none.md"] == [
        ("missing-statement-text", "formalizable article has no statement text outside its H2 sections")
    ]
    assert findings["roadmap/chapter/todo.md"] == [
        (
            "placeholder-statement-text",
            "formalizable article's statement is a placeholder, or opens with one used as a marker",
        )
    ]
    assert findings["roadmap/chapter/rule.md"] == [
        ("empty-statement-text", "formalizable article's statement has no prose with a letter or digit")
    ]


def test_audit_leaves_a_placeholder_alone_outside_formalizable_articles(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "chapter/README.md", prose="TODO")
    _article(blueprint, "chapter/result.md", declaration="theorem")

    assert "roadmap/chapter/README.md" not in _finding_map(blueprint)


def test_bundled_example_has_no_statement_or_unclosed_fence_finding(repo_root: Path) -> None:
    blueprint = repo_root / "skills" / "setup" / "assets" / "cabannes-thesis-project" / "blueprint"

    codes = {finding.code for finding in audit_blueprint(blueprint).findings}

    assert not codes & {
        "missing-statement-text",
        "empty-statement-text",
        "placeholder-statement-text",
        "unclosed-code-fence",
    }
