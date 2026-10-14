"""Deterministically audit a Markdown-native Autoform roadmap.

The audit is deliberately local and read-only. It derives its answer from the
blueprint Markdown and, when supplied, a local Lean source tree. It never
contacts a network service or writes generated state back into the blueprint.
"""

from __future__ import annotations

import json
import os
import re
import statistics
from bisect import bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path

from . import status
from .coverage import CoverageSummary, load_coverage
from .elaboration_limits import limits_in_force
from .graph import (
    IMPLEMENTATION_NOTES_DIR,
    Graph,
    GraphValidationError,
    Node,
    load_graph,
)
from .lean import (
    _DECLARATION,
    Declaration,
    LeanSourceError,
    SourceIndex,
    _without_lean_comments,
    declaration_names,
    index_failure_message,
    resolve_lean_root,
    snapshot_project_sources,
)
from .markdown import article_parts as _article_parts
from .markdown import content_lines as _content_lines
from .markdown import first_unclosed_fence as _first_unclosed_fence
from .markdown import frontmatter_end as _frontmatter_end
from .markdown import has_substance as _has_substance
from .markdown import HEADING as _HEADING
from .markdown import is_placeholder as _is_placeholder
from .markdown import local_target_issue as _local_target_issue
from .markdown import markdown_links as _markdown_links
from .markdown import visible_prose as _visible_prose

#: More siblings than this at one level is a table of contents, not a chapter.
_MAX_DIRECT_CHILDREN = 24

#: A node is reported as oversized only once its finished Lean work clears both
#: an absolute floor and a large multiple of this project's own median. The
#: multiple self-calibrates -- a project whose nodes are routinely long is
#: measured against itself, and a project with too few finished nodes to have a
#: meaningful median cannot clear the multiple at all.
_NODE_SIZE_FLOOR = 200
_NODE_SIZE_MULTIPLE = 4

#: Batteries' commands for a statement whose proof or construction is deferred.
_WANTED_KEYWORDS = frozenset({"theorem_wanted", "proof_wanted", "def_wanted", "instance_wanted"})
_THEOREM_KEYWORDS = frozenset({"lemma", "theorem", "theorem_wanted", "proof_wanted"})
_DEF_KEYWORDS = frozenset({"def", "def_wanted", "irreducible_def"})

_DECLARATION_KEYWORDS = {
    "abbrev": frozenset({"abbrev"}),
    "axiom": frozenset({"axiom"}),
    "class": frozenset({"class"}),
    "corollary": _THEOREM_KEYWORDS,
    "def": _DEF_KEYWORDS,
    "definition": _DEF_KEYWORDS,
    "inductive": frozenset({"inductive"}),
    "instance": frozenset({"instance", "instance_wanted"}),
    "irreducible_def": frozenset({"irreducible_def"}),
    "lemma": _THEOREM_KEYWORDS,
    "opaque": frozenset({"opaque"}),
    "proposition": _THEOREM_KEYWORDS,
    "structure": frozenset({"structure"}),
    "theorem": _THEOREM_KEYWORDS,
}

#: One ``@[...]`` attribute list; a string literal inside it may hold brackets.
_ATTRIBUTE_LIST = r'@\[(?:[^\]"]|"(?:[^"\\]|\\.)*")*\]'
#: The attribute lists and modifiers a declaration keyword closes, starting at
#: a line start, so contiguous attribute lines above the keyword's line count.
_DECLARATION_HEADER = re.compile(
    rf"(?:\A|\n)[ \t]*((?:{_ATTRIBUTE_LIST}\s*)*)"
    r"(?:(?:public|private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*\Z"
)
_STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')
_DEPRECATED_ATTRIBUTE = re.compile(r"(?:\[|,)\s*(?:(?:scoped|local)\s+)?deprecated\b")
_IMPLEMENTATION_NOTE_ISSUE_MARKERS = (
    ": noncanonical implementation notes directory",
    ": implementation notes directory",
    ": implementation notes path",
    ": cannot read directory",
    ": implementation note must",
    ": implementation note names",
    ": cannot read implementation note",
    ": empty implementation note",
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
) -> AuditResult:
    """Audit *blueprint_dir* using only local, committed-style source files.

    Graph syntax errors are returned as structured findings rather than raised.
    Semantic checks run only after :func:`load_graph` has produced a valid graph.
    """

    blueprint = Path(blueprint_dir).expanduser().resolve()
    if blueprint.is_dir():
        coverage, coverage_findings = _coverage_findings(blueprint)
    else:
        coverage, coverage_findings = None, []
    try:
        graph = load_graph(blueprint)
    except GraphValidationError as error:
        findings = [
            AuditFinding(
                _validation_article_path(blueprint, issue),
                "invalid-graph",
                _stable_validation_reason(blueprint, issue),
            )
            for issue in error.issues
        ]
        return _result([*findings, *coverage_findings], coverage=coverage)
    return audit_graph(
        graph,
        lean_root=lean_root,
        coverage=coverage,
        coverage_findings=coverage_findings,
    )


def audit_graph(
    graph: Graph,
    *,
    lean_root: str | Path | None = None,
    coverage: CoverageSummary | None = None,
    coverage_findings: list[AuditFinding] | None = None,
) -> AuditResult:
    """Audit an already loaded graph without modifying it or its source files."""

    findings: list[AuditFinding] = []
    derived = status.derive(graph)
    contained: dict[str, list[str]] = {}
    for node in graph.nodes.values():
        if node.parent is not None:
            contained.setdefault(node.parent, []).append(node.id)

    scope_has_formalizable: dict[str, bool] = {}
    scope_fully_implemented: dict[str, bool] = {}
    for node_id in sorted(
        graph.nodes, key=lambda candidate: graph.nodes[candidate].depth, reverse=True
    ):
        has_formalizable = False
        fully_implemented = True
        for child_id in contained.get(node_id, ()):
            child = graph.nodes[child_id]
            # As on the site, a leaf marked not ready is a target too, so its scope stays open until it is proved.
            if child.formalizable or (child.not_ready and child_id not in contained):
                has_formalizable = True
                fully_implemented = fully_implemented and derived[child_id].proved
            if scope_has_formalizable[child_id]:
                has_formalizable = True
                fully_implemented = (
                    fully_implemented and scope_fully_implemented[child_id]
                )
        scope_has_formalizable[node_id] = has_formalizable
        scope_fully_implemented[node_id] = fully_implemented

    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        article_path = _relative_path(node.path, graph.blueprint_dir)
        children = contained.get(node_id, ())
        # The text the graph parsed, so it is never judged beside metadata
        # parsed from other bytes.
        article = _read_article(graph.article_text(node))

        implementation_done = derived[node_id].proved or (
            scope_has_formalizable[node_id] and scope_fully_implemented[node_id]
        )
        if node.article_id is not None and implementation_done:
            note = graph.blueprint_dir / IMPLEMENTATION_NOTES_DIR / f"{node.article_id}.md"
            if note.is_file():
                findings.append(
                    AuditFinding(
                        article_path,
                        "stale-implementation-note",
                        "completed implementation scope still has "
                        f"{IMPLEMENTATION_NOTES_DIR}/{node.article_id}.md; delete it",
                    )
                )

        if article.unclosed_fence is not None:
            findings.append(
                AuditFinding(
                    article_path,
                    "unclosed-code-fence",
                    f"code fence opened on line {article.unclosed_fence} never closes",
                )
            )

        if node.formalizable:
            if children:
                findings.append(
                    AuditFinding(
                        article_path,
                        "formalizable-container",
                        "formalizable article has contained articles; declaration-sized articles must be leaves",
                    )
                )
            statement_finding = _statement_finding(article.statement)
            if statement_finding is not None:
                findings.append(AuditFinding(article_path, *statement_finding))
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

        if node.mathlib and not declaration_names(node.mathlib_declaration or ""):
            findings.append(
                AuditFinding(
                    article_path,
                    "mathlib-without-declaration",
                    "mathlib is true but mathlib_declaration metadata is missing",
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
    if lean_root is not None:
        findings.extend(_lean_findings(graph, lean_root))
    return _result(findings, coverage=coverage)


@dataclass(frozen=True, slots=True)
class _ArticleShape:
    statement: str
    has_depends_section: bool
    #: The one-based source line of the first fence that never closes.
    unclosed_fence: int | None = None


def _read_article(text: str) -> _ArticleShape:
    parts = _article_parts(text)
    lines = text.splitlines()
    start = _frontmatter_end(lines)
    unclosed = _first_unclosed_fence(lines[start:])
    return _ArticleShape(
        statement=parts.statement,
        has_depends_section=any(section.title.casefold() == "depends on" for section in parts.sections),
        unclosed_fence=None if unclosed is None else start + unclosed + 1,
    )


def _statement_finding(statement: str) -> tuple[str, str] | None:
    """Return the code and reason for what is wrong with a statement's text, if anything.

    The text is judged as the site publishes it, so a hidden element, an image
    or a bare rule states nothing however much source it takes.
    """

    prose = _visible_prose(statement)
    if not _has_substance(prose):
        # A subheading names what follows; alone it states nothing.
        if not any(line.strip() and not _HEADING.match(line) for line in _content_lines(statement)):
            return "missing-statement-text", "formalizable article has no statement text outside its H2 sections"
        return "empty-statement-text", "formalizable article's statement has no prose with a letter or digit"
    if _is_placeholder(prose):
        return (
            "placeholder-statement-text",
            "formalizable article's statement is a placeholder, or opens with one used as a marker",
        )
    return None


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
            (
                "missing-coverage-contract"
                if issue.reason == "coverage contract is missing"
                else "invalid-coverage-contract"
            ),
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


def _lean_findings(graph: Graph, lean_root: str | Path) -> list[AuditFinding]:
    try:
        root = resolve_lean_root(lean_root)
    except LeanSourceError as error:
        return [AuditFinding(".", "invalid-lean-root", str(error))]

    findings: list[AuditFinding] = []
    try:
        snapshot = snapshot_project_sources(root)
    except OSError as error:
        return [
            AuditFinding(
                ".",
                "unreadable-lean-sources",
                index_failure_message(error),
            )
        ]
    index = snapshot.index
    spans = _source_spans(index)
    sizes: dict[str, int] = {}
    sources = _captured_source_lines(snapshot.source_files)
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        article_path = _relative_path(node.path, graph.blueprint_dir)
        names = declaration_names(node.lean or "")
        resolved = []
        for name in names:
            declaration = index.find(name)
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

        for declaration in resolved:
            if _declared_deprecated(declaration, sources):
                findings.append(
                    AuditFinding(
                        article_path,
                        "lean-target-deprecated",
                        f"lean target {declaration.name} is deprecated; point lean: at its replacement",
                    )
                )

        # An alias's proof is its target's, so the limits and size are the
        # target's too. An alias whose target the index cannot place is
        # measured like any other declaration.
        bodies = list(dict.fromkeys(index.reached(declaration) or declaration for declaration in resolved))
        for body in bodies:
            findings.extend(_limit_findings(article_path, body, sources))

        expected = _DECLARATION_KEYWORDS.get((node.declaration or "").casefold())
        kinds = [_kind(declaration, index) for declaration in resolved]
        # An alias whose target the index cannot place may have any kind.
        if expected and resolved and not any(kind is None or kind in expected for kind in kinds):
            described = {
                f"alias of {kind}" if declaration.alias else str(kind) for declaration, kind in zip(resolved, kinds)
            }
            actual = ", ".join(sorted(described))
            findings.append(
                AuditFinding(
                    article_path,
                    "lean-target-kind-mismatch",
                    f"Lean target kind {actual} does not match declaration intent {node.declaration}",
                )
            )

        # A wanted target states its result and nothing more. Without a body it
        # elaborates to a placeholder without sorry, so no sorry check catches it.
        if node.proof_formalized:
            claim = "proof: formalized"
        elif node.statement_formalized and status.is_definition(node):
            claim = "statement: formalized, which proves a definition"
        else:
            claim = None
        if claim is not None:
            for declaration, kind in zip(resolved, kinds):
                if kind in _WANTED_KEYWORDS:
                    what = f"an alias of a {kind}" if declaration.alias else f"a {kind}"
                    findings.append(
                        AuditFinding(
                            article_path,
                            "lean-target-wanted",
                            f"lean target {declaration.name} is {what}, which defers "
                            f"its proof or construction, so it cannot back {claim}",
                        )
                    )

        if resolved:
            sizes[node_id] = sum(spans[body.name] for body in bodies)

    findings.extend(_size_findings(graph, sizes))
    return findings


def _kind(declaration: Declaration, index: SourceIndex) -> str | None:
    """The keyword whose kind ``declaration`` has; an alias has its target's.

    Each name of ``alias ⟨mp, mpr⟩`` is a theorem whatever its target is, and
    so is an alias of one. Any other alias whose target the index cannot place
    has no known kind: ``None``.
    """

    while declaration.alias is not None:
        if declaration.alias.iff:
            return "theorem"
        target = index.alias_target(declaration)
        if target is None:
            return None
        declaration = target
    return declaration.keyword


def _captured_source_lines(
    source_files: tuple[tuple[Path, bytes], ...],
) -> dict[Path, list[str] | None]:
    """Decode the already captured source generation for lexical checks."""

    sources: dict[Path, list[str] | None] = {}
    for path, data in source_files:
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            sources[path] = None
        else:
            sources[path] = _without_lean_comments(text).splitlines()
    return sources


def _declared_deprecated(declaration: Declaration, sources: dict[Path, list[str] | None]) -> bool:
    """Whether the source declaration carries the ``deprecated`` attribute lexically.

    Only ``@[...]`` lists before the keyword count: on the declaration's line,
    or on the contiguous attribute lines directly above it. Comments are
    blanked first, so a commented-out attribute does not count.
    """

    lines = sources.get(declaration.path)
    if lines is None or not 0 < declaration.line <= len(lines):
        return False
    line = lines[declaration.line - 1]
    keyword = _DECLARATION.match(line)
    if keyword is None:
        return False
    # The attribute lines cannot reach above the previous declaration, so the
    # search never rescans the whole file.
    first = declaration.line - 1
    while first and not _DECLARATION.match(lines[first - 1]):
        first -= 1
    header = _DECLARATION_HEADER.search("\n".join([*lines[first : declaration.line - 1], line[: keyword.start(1)]]))
    return header is not None and any(
        _DEPRECATED_ATTRIBUTE.search(_STRING_LITERAL.sub('""', attributes))
        for attributes in re.findall(_ATTRIBUTE_LIST, header.group(1))
    )


def _limit_findings(
    article_path: str,
    declaration: Declaration,
    sources: dict[Path, list[str] | None],
) -> list[AuditFinding]:
    """Flag a target with no elaboration limit, or a raised one it only inherits.

    A raised limit put on the declaration itself is the accepted Mathlib
    practice and is not reported; one inherited from a file- or section-wide
    ``set_option`` also covers every neighbour, needed or not.
    """

    lines = sources.get(declaration.path)
    if lines is None:
        return []
    location = declaration.path.as_posix()
    findings: list[AuditFinding] = []
    for setting in limits_in_force(lines, declaration.line):
        option = f"set_option {setting.option} {setting.value}"
        if setting.unbounded:
            findings.append(
                AuditFinding(
                    article_path,
                    "lean-target-unbounded-limit",
                    f"lean target {declaration.name} has no {setting.option} limit: "
                    f"`{option}` at {location}:{setting.line}; set a finite limit",
                )
            )
        elif setting.raised and setting.scope in {"file", "section"}:
            findings.append(
                AuditFinding(
                    article_path,
                    "lean-target-unscoped-limit",
                    f"lean target {declaration.name} inherits the {setting.scope}-wide "
                    f"`{option}` at {location}:{setting.line}; put "
                    f"`set_option {setting.option} N in` on the declarations that need it",
                )
            )
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
        tails[path] = index.line_counts.get(path, 0)

    spans: dict[str, int] = {}
    for declaration in index.declarations.values():
        lines = starts[declaration.path]
        following = bisect_right(lines, declaration.line)
        end = lines[following] - 1 if following < len(lines) else tails[declaration.path]
        spans[declaration.name] = max(1, end - declaration.line + 1)
    return spans


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
    issue_root = issue.split("/", 1)[0].split(":", 1)[0]
    if issue_root.casefold() == IMPLEMENTATION_NOTES_DIR.casefold():
        for marker in _IMPLEMENTATION_NOTE_ISSUE_MARKERS:
            boundary = issue.rfind(marker)
            if boundary >= 0:
                return issue[:boundary]

    node_id = issue.split(":", 1)[0]
    if not node_id or " " in node_id or node_id in {"dependency cycle", "rolled-up dependency cycle"}:
        return "."

    roadmap = blueprint / "roadmap"
    if node_id == "roadmap":
        candidate = roadmap / "README.md"
    else:
        readme = roadmap / node_id / "README.md"
        # os.path.exists is False for a path it cannot stat; Path.exists raises before 3.14.
        candidate = readme if os.path.exists(readme) else roadmap / f"{node_id}.md"
        if node_id.casefold().endswith(".md") and not os.path.exists(candidate):
            candidate = roadmap / node_id  # discovery names a page it could not load by its file
    if roadmap.exists():
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


__all__ = ["AuditFinding", "AuditResult", "audit_blueprint", "audit_graph"]
