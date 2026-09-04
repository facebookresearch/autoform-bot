"""Deterministically audit a Markdown-native Autoform roadmap.

The audit is deliberately local and read-only. It derives its answer from the
blueprint Markdown and, when supplied, a local Lean source tree. It never
contacts a network service or writes generated state back into the blueprint.
"""

from __future__ import annotations

import json
import stat
import statistics
import tempfile
from bisect import bisect_right
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from . import status
from ._tree_snapshot import (
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
    bind_directory_tree,
)
from .coverage import CoverageSummary, load_coverage
from .graph import Graph, GraphValidationError, Node, load_graph
from .lean import (
    SourceIndex,
    declaration_keywords,
    declaration_names,
    index_project,
    mathlib_module_name,
)
from .markdown import FENCE as _FENCE
from .markdown import frontmatter_end as _frontmatter_end
from .markdown import HEADING as _HEADING
from .markdown import HTML_COMMENT as _HTML_COMMENT
from .markdown import local_target_issue as _local_target_issue
from .markdown import markdown_links as _markdown_links

#: More siblings than this at one level is a table of contents, not a chapter.
_MAX_DIRECT_CHILDREN = 24

#: A node is reported as oversized only once its finished Lean work clears both
#: an absolute floor and a large multiple of this project's own median. The
#: multiple self-calibrates -- a project whose nodes are routinely long is
#: measured against itself, and a project with too few finished nodes to have a
#: meaningful median cannot clear the multiple at all.
_NODE_SIZE_FLOOR = 200
_NODE_SIZE_MULTIPLE = 4


def _visible_snapshot_path(relative: PurePosixPath) -> bool:
    return not any(part.startswith(".") for part in relative.parts)


def _audit_snapshot_includes(relative: PurePosixPath, mode: int) -> bool:
    return _visible_snapshot_path(relative) and (
        not stat.S_ISREG(mode)
        or relative.suffix.casefold() == ".md"
        or relative.parts[:1] == ("sources",)
    )


_AUDIT_SNAPSHOT_SELECTION = TreeSelection(
    include=_audit_snapshot_includes,
    descend=_visible_snapshot_path,
    placeholder=lambda relative, mode: (
        _visible_snapshot_path(relative) and stat.S_ISREG(mode)
    ),
)

@dataclass(frozen=True, order=True, slots=True)
class AuditFinding:
    """One actionable roadmap problem at a stable blueprint-relative path."""

    article_path: str
    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class AuditResult:
    """The complete, canonically ordered result of one audit."""

    findings: tuple[AuditFinding, ...] = ()
    coverage: CoverageSummary | None = None

    @property
    def clean(self) -> bool:
        """Whether the audit found no roadmap problems."""

        return not self.findings

    def as_dict(self) -> dict[str, object]:
        """Return a stable, machine-readable representation without host paths."""

        return {
            "clean": self.clean,
            "coverage": self.coverage.as_dict() if self.coverage is not None else None,
            "findings": [asdict(finding) for finding in self.findings],
        }

    def to_json(self) -> str:
        """Serialize the result canonically for snapshots and automation."""

        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def audit_blueprint(
    blueprint_dir: str | Path,
    *,
    lean_root: str | Path | None = None,
    _expected_blueprint_identity: tuple[int, int] | None = None,
    _expected_roadmap_identity: tuple[int, int] | None = None,
) -> AuditResult:
    """Audit *blueprint_dir* using only local, committed-style source files.

    Graph syntax errors are returned as structured findings rather than raised.
    Semantic checks run only after :func:`load_graph` has produced a valid graph.
    """

    _graph, result = load_audit_graph(
        blueprint_dir,
        lean_root=lean_root,
        _expected_blueprint_identity=_expected_blueprint_identity,
        _expected_roadmap_identity=_expected_roadmap_identity,
    )
    return result


def load_audit_graph(
    blueprint_dir: str | Path,
    *,
    lean_root: str | Path | None = None,
    lean_index: SourceIndex | None = None,
    _expected_blueprint_identity: tuple[int, int] | None = None,
    _expected_roadmap_identity: tuple[int, int] | None = None,
) -> tuple[Graph | None, AuditResult]:
    """Return a graph and audit derived from one immutable blueprint capture."""

    blueprint = Path(blueprint_dir).expanduser().resolve()
    if not blueprint.is_dir():
        return _audit_snapshot_graph(
            blueprint,
            lean_root=lean_root,
            lean_index=lean_index,
            expected_blueprint_identity=_expected_blueprint_identity,
            expected_roadmap_identity=_expected_roadmap_identity,
        )
    expected_children = (
        {"roadmap": _expected_roadmap_identity}
        if _expected_roadmap_identity is not None
        else None
    )
    try:
        with bind_directory_tree(
            blueprint,
            expected_identity=_expected_blueprint_identity,
            expected_children=expected_children,
            selection=_AUDIT_SNAPSHOT_SELECTION,
        ) as bound:
            snapshot = bound.capture()
            entry_findings = _snapshot_entry_findings(snapshot)
            with tempfile.TemporaryDirectory(prefix="autoform-audit-") as temporary:
                snapshot_root = Path(temporary) / "blueprint"
                snapshot.materialize_regular_files(snapshot_root)
                graph, result = _audit_snapshot_graph(
                    snapshot_root,
                    lean_root=lean_root,
                    lean_index=lean_index,
                )
                if graph is not None:
                    graph = _rebase_captured_graph(graph, blueprint)
            bound.verify()
            graph_is_invalid = any(
                finding.code == "invalid-graph" for finding in entry_findings
            )
            return (
                None if graph_is_invalid else graph,
                _result(
                    [*result.findings, *entry_findings],
                    coverage=result.coverage,
                ),
            )
    except TreeSnapshotError as error:
        return None, _result(
            [AuditFinding(".", "invalid-graph", str(error))]
        )


def _rebase_captured_graph(graph: Graph, blueprint: Path) -> Graph:
    """Return a captured graph whose public paths name the requested blueprint."""

    nodes = {
        node_id: replace(
            node,
            path=blueprint / node.path.relative_to(graph.blueprint_dir),
        )
        for node_id, node in graph.nodes.items()
    }
    rebased = Graph(blueprint_dir=blueprint, nodes=nodes)
    object.__setattr__(
        rebased,
        "_source_bytes",
        {
            node_id: content
            for node_id in graph.nodes
            if (content := graph.source_bytes(node_id)) is not None
        },
    )
    return rebased


def _snapshot_entry_findings(snapshot: TreeSnapshot) -> list[AuditFinding]:
    """Preserve unsafe-entry diagnostics that cannot survive materialization."""

    findings: list[AuditFinding] = []
    for relative, reason in snapshot.unsupported_entries():
        roadmap_entry = relative == "roadmap" or relative.startswith("roadmap/")
        findings.append(
            AuditFinding(
                relative,
                "invalid-graph" if roadmap_entry else "unsafe-blueprint-entry",
                (
                    f"roadmap path is invalid: {reason}"
                    if roadmap_entry
                    else f"blueprint path is unsafe: {reason}"
                ),
            )
        )
    return findings


def _audit_snapshot(
    blueprint: Path,
    *,
    lean_root: str | Path | None,
    expected_blueprint_identity: tuple[int, int] | None = None,
    expected_roadmap_identity: tuple[int, int] | None = None,
) -> AuditResult:
    """Audit one immutable, private blueprint snapshot."""

    return _audit_snapshot_graph(
        blueprint,
        lean_root=lean_root,
        expected_blueprint_identity=expected_blueprint_identity,
        expected_roadmap_identity=expected_roadmap_identity,
    )[1]


def _audit_snapshot_graph(
    blueprint: Path,
    *,
    lean_root: str | Path | None,
    lean_index: SourceIndex | None = None,
    expected_blueprint_identity: tuple[int, int] | None = None,
    expected_roadmap_identity: tuple[int, int] | None = None,
) -> tuple[Graph | None, AuditResult]:
    """Audit one immutable tree and retain its parsed graph for sibling checks."""

    if blueprint.is_dir():
        coverage, coverage_findings = _coverage_findings(blueprint)
    else:
        coverage, coverage_findings = None, []
    try:
        graph = load_graph(
            blueprint,
            _expected_blueprint_identity=expected_blueprint_identity,
            _expected_roadmap_identity=expected_roadmap_identity,
        )
    except GraphValidationError as error:
        findings = [
            AuditFinding(
                _validation_article_path(blueprint, issue),
                "invalid-graph",
                _stable_validation_reason(blueprint, issue),
            )
            for issue in error.issues
        ]
        return None, _result([*findings, *coverage_findings], coverage=coverage)
    return (
        graph,
        audit_graph(
            graph,
            lean_root=lean_root,
            lean_index=lean_index,
            coverage=coverage,
            coverage_findings=coverage_findings,
        ),
    )


def audit_graph(
    graph: Graph,
    *,
    lean_root: str | Path | None = None,
    lean_index: SourceIndex | None = None,
    coverage: CoverageSummary | None = None,
    coverage_findings: list[AuditFinding] | None = None,
) -> AuditResult:
    """Audit an already loaded graph without modifying it or its source files."""

    findings: list[AuditFinding] = []
    derived = status.derive(graph)

    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        article_path = _relative_path(node.path, graph.blueprint_dir)
        children = graph.children(node_id)
        article = _read_article(node.path, content=graph.source_bytes(node.id))

        if node.formalizable:
            if children:
                findings.append(
                    AuditFinding(
                        article_path,
                        "formalizable-container",
                        "formalizable article has contained articles; declaration-sized articles must be leaves",
                    )
                )
            if not article.statement_text:
                findings.append(
                    AuditFinding(
                        article_path,
                        "missing-statement-text",
                        "formalizable article has no statement text between its H1 and first H2 section",
                    )
                )
            if not article.has_depends_section:
                findings.append(
                    AuditFinding(
                        article_path,
                        "missing-depends-section",
                        "formalizable article has no explicit '## Depends on' section",
                    )
                )
            if node.origin == "cited" and not node.sources:
                findings.append(
                    AuditFinding(
                        article_path,
                        "missing-source-link",
                        "cited formalizable article has no link under '## Sources'",
                    )
                )

        if len(children) > _MAX_DIRECT_CHILDREN:
            findings.append(
                AuditFinding(
                    article_path,
                    "overfull-container",
                    f"article directly contains {len(children)} articles, more than the "
                    f"{_MAX_DIRECT_CHILDREN}-article limit; group them into chapters",
                )
            )

        if node.proof_formalized and not node.statement_formalized:
            findings.append(
                AuditFinding(
                    article_path,
                    "proof-without-statement",
                    "proof is marked formalized but the statement is not marked formalized",
                )
            )

        if node.mathlib and not declaration_names(node.mathlib_declaration or ""):
            findings.append(
                AuditFinding(
                    article_path,
                    "mathlib-without-declaration",
                    "mathlib is true but mathlib_declaration metadata is missing",
                )
            )
        if node.mathlib and not node.mathlib_file:
            findings.append(
                AuditFinding(
                    article_path,
                    "mathlib-without-file",
                    "mathlib is true but mathlib_file metadata is missing",
                )
            )
        elif node.mathlib and mathlib_module_name(node.mathlib_file or "") is None:
            findings.append(
                AuditFinding(
                    article_path,
                    "invalid-mathlib-file",
                    "mathlib_file must be a canonical Mathlib/**/*.lean source path",
                )
            )

        formalization_evidence = (
            bool(node.lean)
            or node.statement_formalized
            or node.proof_formalized
            or node.mathlib
            or bool(node.mathlib_declaration)
            or bool(node.mathlib_file)
            or derived[node_id].proved
        )
        if not children and formalization_evidence and not node.declaration:
            findings.append(
                AuditFinding(
                    article_path,
                    "missing-declaration-intent",
                    "formalization-bearing leaf has no declaration intent metadata",
                )
            )

        findings.extend(_source_findings(graph, node, article_path))

    if coverage_findings is None:
        coverage, coverage_findings = _coverage_findings(graph.blueprint_dir)
    findings.extend(coverage_findings)
    if lean_index is not None:
        findings.extend(_lean_findings(graph, lean_index=lean_index))
    elif lean_root is not None:
        findings.extend(_lean_findings(graph, lean_root=lean_root))
    return _result(findings, coverage=coverage)


@dataclass(frozen=True, slots=True)
class _ArticleShape:
    statement_text: bool
    has_depends_section: bool


def _read_article(path: Path, *, content: bytes | None = None) -> _ArticleShape:
    if content is None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return _ArticleShape(False, False)
    else:
        try:
            text = content.decode("utf-8")
        except UnicodeError:
            return _ArticleShape(False, False)

    lines = text.splitlines()
    start = _frontmatter_end(lines)
    body = _HTML_COMMENT.sub("", "\n".join(lines[start:]))
    seen_h1 = False
    before_first_h2 = True
    statement_text = False
    has_depends_section = False
    fence: tuple[str, int] | None = None

    for line in body.splitlines():
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip().casefold()
            if level == 1:
                seen_h1 = True
            elif level == 2:
                before_first_h2 = False
                if title == "depends on":
                    has_depends_section = True
            continue
        if seen_h1 and before_first_h2 and line.strip():
            statement_text = True

    return _ArticleShape(statement_text, has_depends_section)


def _source_findings(graph: Graph, node: Node, article_path: str) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for target in node.sources:
        issue = _local_target_issue(node.path, target, graph.blueprint_dir, label="source")
        if issue is not None:
            code, reason = issue
            findings.append(AuditFinding(article_path, code, reason))
    return findings


def _coverage_findings(
    blueprint: Path,
) -> tuple[CoverageSummary | None, list[AuditFinding]]:
    coverage_root = blueprint / "coverage"
    contract = coverage_root / "README.md"
    coverage, issues = load_coverage(blueprint)
    findings = [
        AuditFinding(
            "coverage/README.md",
            issue.code,
            f"{issue.reason}{f' (line {issue.line})' if issue.line else ''}",
        )
        for issue in issues
    ]

    if coverage is not None:
        for entry in coverage.entries:
            if entry.disposition == "MAPPED":
                findings.append(
                    AuditFinding(
                        "coverage/README.md",
                        "declared-coverage-gap",
                        f"coverage area {entry.area!r} is mapped but not dispositioned (line {entry.line})",
                    )
                )

    coverage_files = sorted(path for path in coverage_root.rglob("*.md") if path.is_file())
    for path in coverage_files:
        article_path = _relative_path(path, blueprint)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            # load_coverage already gives the canonical contract its more precise
            # invalid-coverage-contract diagnostic.
            if path != contract:
                findings.append(
                    AuditFinding(
                        article_path,
                        "unreadable-coverage-file",
                        "coverage file cannot be read as UTF-8",
                    )
                )
            continue

        for line_number, target in _markdown_links(text):
            issue = _local_target_issue(path, target, blueprint, label="coverage")
            if issue is not None:
                code, reason = issue
                findings.append(
                    AuditFinding(article_path, code, f"{reason} (line {line_number})")
                )
    return coverage, findings


def _lean_findings(
    graph: Graph,
    *,
    lean_root: str | Path | None = None,
    lean_index: SourceIndex | None = None,
) -> list[AuditFinding]:
    if lean_index is None:
        assert lean_root is not None
        root = Path(lean_root).expanduser().resolve()
        if not root.is_dir():
            return [
                AuditFinding(
                    ".",
                    "invalid-lean-root",
                    "Lean root does not exist or is not a directory",
                )
            ]
        try:
            lean_index = index_project(root)
        except OSError as error:
            return [AuditFinding(".", "invalid-lean-root", str(error))]
    findings: list[AuditFinding] = []
    spans = _source_spans(lean_index)
    sizes: dict[str, int] = {}
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        article_path = _relative_path(node.path, graph.blueprint_dir)
        names = declaration_names(node.lean or "")
        if (node.statement_formalized or node.proof_formalized) and not names:
            findings.append(
                AuditFinding(
                    article_path,
                    "missing-lean-target",
                    "formalized local work has no lean declaration target",
                )
            )
            continue

        resolved = []
        for name in names:
            declaration = lean_index.find(name)
            if declaration is None:
                findings.append(
                    AuditFinding(
                        article_path,
                        "lean-target-not-found",
                        f"Lean declaration target was not found: {name}",
                    )
                )
            else:
                resolved.append(declaration)

        expected = declaration_keywords(node.declaration)
        mismatched = [
            declaration
            for declaration in resolved
            if expected is not None and declaration.keyword not in expected
        ]
        if mismatched:
            actual = ", ".join(sorted({declaration.keyword for declaration in mismatched}))
            findings.append(
                AuditFinding(
                    article_path,
                    "lean-target-kind-mismatch",
                    f"Lean target kind {actual} does not match declaration intent {node.declaration}",
                )
            )

        if resolved:
            sizes[node_id] = sum(spans[declaration.name] for declaration in resolved)

    findings.extend(_size_findings(graph, sizes))
    return findings


def _source_spans(index: SourceIndex) -> dict[str, int]:
    """Measure each declaration's source span, up to the next declaration.

    This is the retrospective size signal: unlike anything authored in the
    article, it is what the node actually cost once it was formalized.
    """

    starts: dict[Path, list[int]] = {}
    for declaration in index.declarations.values():
        starts.setdefault(declaration.path, []).append(declaration.line)
    tails: dict[Path, int] = {}
    for path, lines in starts.items():
        lines.sort()
        tails[path] = (
            index.line_counts[path]
            if path in index.line_counts
            else _line_count(index.root / path)
        )

    spans: dict[str, int] = {}
    for declaration in index.declarations.values():
        lines = starts[declaration.path]
        following = bisect_right(lines, declaration.line)
        end = lines[following] - 1 if following < len(lines) else tails[declaration.path]
        spans[declaration.name] = max(1, end - declaration.line + 1)
    return spans


def _line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError):
        return 0


def _size_findings(graph: Graph, sizes: dict[str, int]) -> list[AuditFinding]:
    """Flag finished nodes that are large outliers against this project's own work."""

    if not sizes:
        return []
    median = statistics.median(sizes.values())
    limit = max(_NODE_SIZE_FLOOR, _NODE_SIZE_MULTIPLE * median)
    return [
        AuditFinding(
            _relative_path(graph.nodes[node_id].path, graph.blueprint_dir),
            "node-too-large",
            f"node's Lean declarations span {size} lines against this project's "
            f"{median:g}-line median; split it into pull-request-sized nodes",
        )
        for node_id, size in sorted(sizes.items())
        if size >= limit
    ]


def _stable_validation_reason(blueprint: Path, issue: str) -> str:
    blueprint_text = str(blueprint.resolve())
    return issue.replace(blueprint_text, ".")


def _validation_article_path(blueprint: Path, issue: str) -> str:
    node_id = issue.split(":", 1)[0]
    if not node_id or " " in node_id or node_id in {"dependency cycle", "rolled-up dependency cycle"}:
        return "."

    roadmap = blueprint / "roadmap"
    if node_id == "roadmap":
        candidate = roadmap / "README.md"
    else:
        readme = roadmap / node_id / "README.md"
        candidate = readme if readme.exists() else roadmap / f"{node_id}.md"
    if candidate.exists() or roadmap.exists():
        return _relative_path(candidate, blueprint)
    return "."


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return "."


def _result(
    findings: list[AuditFinding],
    *,
    coverage: CoverageSummary | None = None,
) -> AuditResult:
    return AuditResult(tuple(sorted(set(findings))), coverage)


__all__ = [
    "AuditFinding",
    "AuditResult",
    "audit_blueprint",
    "audit_graph",
    "load_audit_graph",
]
