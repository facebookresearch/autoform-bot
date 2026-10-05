"""Compile an Autoform dependency graph from its Markdown blueprint.

Markdown is both the human wiki and the sole authored graph representation:
node paths are stable ids, frontmatter carries checked facts, and links under
the two dependency headings are typed edges. ``Graph`` is only a validated
in-memory projection. It rejects broken links and cycles instead of persisting
a second graph file that could drift from the book.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import MISSING, dataclass, fields
from pathlib import Path
from urllib.parse import unquote, urlsplit

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"(`+).*?\1")
_LEAN_DECLARATION_NAME = re.compile(r"(?:«[^»]*(?:»|$)|[^\s,«])+")
ARTICLE_ID_PATTERN = re.compile(r"af_[0-9a-f]{24}\Z")
_FRONTMATTER_KEYS = frozenset(
    {
        "article_id",
        "catalog",
        "declaration",
        "lean",
        "statement",
        "proof",
        "mathlib",
        "mathlib_declaration",
        "mathlib_file",
        "not_ready",
        "origin",
        "discussion",
    }
)
_FORMALIZED = "formalized"
_TRUE = frozenset({"true", "yes"})
_FALSE = frozenset({"false", "no"})
_CATALOG_DECLARATION_KEYS = frozenset(
    {"declaration", "mathlib", "mathlib_declaration", "mathlib_file"}
)

#: ``## Depends on`` carries the prerequisites needed to *state* a node;
#: ``## Proof depends on`` carries the extra prerequisites its *proof* needs.
#: Both are graph edges, mirroring where leanblueprint places ``\uses``.
_STATEMENT_SECTION = "depends on"
_PROOF_SECTION = "proof depends on"
_SOURCES_SECTION = "sources"


class GraphValidationError(ValueError):
    """A blueprint could not be interpreted as a valid dependency graph."""

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True, slots=True)
class Node:
    """One Markdown article in a blueprint.

    Only the ``statement``/``proof``/``mathlib``/``not_ready`` assertions are
    recorded here. Everything a reader thinks of as progress -- ready to state,
    ready to prove, fully proved -- is derived from the graph by
    :mod:`autoform_cli.status`, so it can never go stale.
    """

    id: str
    title: str
    path: Path
    dependencies: tuple[str, ...]
    statement_dependencies: tuple[str, ...] = ()
    proof_dependencies: tuple[str, ...] = ()
    kind: str = "node"
    lean: str | None = None
    declaration: str | None = None
    catalog: str | None = None
    statement_formalized: bool = False
    proof_formalized: bool = False
    mathlib: bool = False
    mathlib_declaration: str | None = None
    mathlib_file: str | None = None
    not_ready: bool = False
    discussion: str | None = None
    origin: str | None = None
    sources: tuple[str, ...] = ()
    parent: str | None = None
    depth: int = 0
    article_id: str | None = None
    source_sha256: str | None = None
    catalog: str | None = None

    @property
    def formalizable(self) -> bool:
        """Whether this article names a concrete Lean declaration."""
        return self.declaration is not None

    def __getstate__(self) -> list[object]:
        """Keep the public slotted record append-compatible for old pickles."""

        return [getattr(self, item.name) for item in fields(self)]

    def __setstate__(self, state: list[object]) -> None:
        node_fields = fields(self)
        if len(state) > len(node_fields):
            raise ValueError("Node pickle has unsupported fields")
        for item, value in zip(node_fields, state, strict=False):
            object.__setattr__(self, item.name, value)
        for item in node_fields[len(state) :]:
            if item.default is MISSING:
                raise ValueError(f"Node pickle is missing required field {item.name!r}")
            object.__setattr__(self, item.name, item.default)


@dataclass(frozen=True, slots=True)
class Graph:
    """A validated blueprint graph, keyed by stable node id."""

    blueprint_dir: Path
    nodes: dict[str, Node]
    _children_by_parent: dict[str | None, tuple[str, ...]] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _children_node_count: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._rebuild_children()

    def _rebuild_children(self) -> None:
        grouped: dict[str | None, list[str]] = {}
        for node in self.nodes.values():
            grouped.setdefault(node.parent, []).append(node.id)
        object.__setattr__(
            self,
            "_children_by_parent",
            {parent: tuple(children) for parent, children in grouped.items()},
        )
        object.__setattr__(self, "_children_node_count", len(self.nodes))

    @property
    def edge_count(self) -> int:
        return sum(len(node.dependencies) for node in self.nodes.values())

    def children(self, node_id: str) -> tuple[str, ...]:
        """Return the direct contained articles of *node_id*."""
        # ``Graph`` historically preserves a caller's plain mutable node dict.
        # Refresh after additions/removals while keeping the normal validated,
        # stable graph lookup O(1).
        if len(self.nodes) != self._children_node_count:
            self._rebuild_children()
        return self._children_by_parent.get(node_id, ())


@dataclass(frozen=True, slots=True)
class _ParsedNode:
    id: str
    title: str
    path: Path
    statement_targets: tuple[str, ...]
    proof_targets: tuple[str, ...]
    source_targets: tuple[str, ...]
    metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class _NodeSource:
    id: str
    path: Path
    text: str
    source_sha256: str


def load_graph(blueprint_dir: str | Path) -> Graph:
    """Load and validate Markdown nodes beneath *blueprint_dir*."""

    blueprint = Path(blueprint_dir).expanduser().resolve()
    if not blueprint.is_dir():
        raise GraphValidationError([f"blueprint directory does not exist: {blueprint}"])

    issues: list[str] = []
    parsed: list[_ParsedNode] = []
    canonical_ids: dict[Path, str] = {}
    node_ids: dict[str, Path] = {}
    sources, discovery_issues = _discover_nodes(blueprint)
    issues.extend(discovery_issues)
    article_ids: dict[str, str] = {}
    source_hashes = {source.id: source.source_sha256 for source in sources}

    for source in sources:
        canonical = source.path.resolve()
        if canonical in canonical_ids:
            issues.append(f"{source.id}: duplicates node {canonical_ids[canonical]!r}")
            continue
        if source.id in node_ids:
            issues.append(f"{source.id}: duplicate node id also used by {node_ids[source.id]}")
            continue
        canonical_ids[canonical] = source.id
        node_ids[source.id] = canonical
        node, node_issues = _parse_node(source.id, canonical, source.text)
        issues.extend(node_issues)
        if node is not None:
            article_id = node.metadata.get("article_id")
            if article_id is not None:
                previous = article_ids.get(article_id)
                if previous is not None:
                    issues.append(
                        f"{node.id}: duplicate article_id {article_id!r} also used by {previous}"
                    )
                else:
                    article_ids[article_id] = node.id
            parsed.append(node)

    if issues:
        raise GraphValidationError(issues)

    parents = _article_parents(parsed)
    nodes: dict[str, Node] = {}
    for parsed_node in parsed:

        def resolve(targets: tuple[str, ...], node: _ParsedNode = parsed_node) -> list[str]:
            resolved: list[str] = []
            for target in targets:
                dependency, issue = _resolve_target(node, target, blueprint, canonical_ids)
                if issue:
                    issues.append(issue)
                elif dependency == node.id:
                    issues.append(f"{node.id}: dependency on itself")
                elif dependency not in resolved:
                    resolved.append(dependency)
            return resolved

        statement_dependencies = resolve(parsed_node.statement_targets)
        proof_dependencies = resolve(parsed_node.proof_targets)
        dependencies = list(statement_dependencies)
        dependencies.extend(
            dependency for dependency in proof_dependencies if dependency not in dependencies
        )
        metadata = parsed_node.metadata
        if metadata.get("catalog") is not None:
            if (
                metadata.get("statement") == _FORMALIZED
                or metadata.get("proof") == _FORMALIZED
            ) and not _LEAN_DECLARATION_NAME.findall(metadata.get("lean", "")):
                issues.append(
                    f"{parsed_node.id}: formalized module catalog must list exact "
                    "compiled names in 'lean'"
                )
            if not _catalog_has_local_ledger(parsed_node, blueprint):
                issues.append(
                    f"{parsed_node.id}: module catalog must link a declaration ledger "
                    "under blueprint/sources from its '## Sources' section"
                )
        nodes[parsed_node.id] = Node(
            id=parsed_node.id,
            title=parsed_node.title,
            path=parsed_node.path,
            dependencies=tuple(dependencies),
            statement_dependencies=tuple(statement_dependencies),
            proof_dependencies=tuple(proof_dependencies),
            kind="article",
            declaration=metadata.get("declaration"),
            catalog=metadata.get("catalog"),
            lean=metadata.get("lean"),
            statement_formalized=metadata.get("statement") == _FORMALIZED,
            proof_formalized=metadata.get("proof") == _FORMALIZED,
            mathlib=metadata.get("mathlib") in _TRUE,
            mathlib_declaration=metadata.get("mathlib_declaration"),
            mathlib_file=metadata.get("mathlib_file"),
            not_ready=metadata.get("not_ready") in _TRUE,
            discussion=metadata.get("discussion"),
            origin=metadata.get("origin"),
            sources=parsed_node.source_targets,
            parent=parents[parsed_node.id],
            depth=_article_depth(parsed_node.id, parents),
            article_id=metadata.get("article_id"),
            source_sha256=source_hashes[parsed_node.id],
        )

    if not issues:
        issues.extend(_find_cycles(nodes))
    if not issues:
        issues.extend(_find_rollup_cycles(nodes))
    if issues:
        raise GraphValidationError(issues)
    return Graph(blueprint_dir=blueprint, nodes=nodes)


def _catalog_has_local_ledger(node: _ParsedNode, blueprint: Path) -> bool:
    sources = (blueprint / "sources").resolve()
    for target in node.source_targets:
        split = urlsplit(target)
        if split.scheme or split.netloc or split.query or not split.path:
            continue
        path = Path(unquote(split.path))
        if path.is_absolute():
            continue
        try:
            candidate = (node.path.parent / path).resolve()
            candidate.relative_to(sources)
        except (OSError, ValueError):
            continue
        if candidate.is_file():
            return True
    return False


def _discover_nodes(blueprint: Path) -> tuple[list[_NodeSource], list[str]]:
    roadmap_root = blueprint / "roadmap"
    if not roadmap_root.is_dir():
        return [], [f"roadmap directory does not exist: {roadmap_root}"]

    issues: list[str] = []
    sources: list[_NodeSource] = []
    roadmap_root = roadmap_root.resolve()
    entries = sorted(roadmap_root.rglob("*"))
    for path in entries:
        if path.is_file() and path.name.casefold() == "readme.md" and path.name != "README.md":
            relative = path.relative_to(roadmap_root).as_posix()
            issues.append(
                f"{relative}: noncanonical README filename; container pages must be named exactly README.md "
                "for portable behavior on case-sensitive filesystems"
            )

    for path in entries:
        if not path.is_file() or path.suffix != ".md":
            continue
        try:
            content = path.read_bytes()
            text = content.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            relative = path.relative_to(roadmap_root).as_posix()
            issues.append(f"{relative}: cannot read roadmap page: {exc}")
            continue
        node_id = _article_id(path, roadmap_root)
        canonical = path.resolve()
        if not _is_within(canonical, roadmap_root):
            issues.append(f"{node_id}: node file escapes the roadmap directory")
            continue
        sources.append(
            _NodeSource(node_id, canonical, text, hashlib.sha256(content).hexdigest())
        )

    issues.extend(_chapter_issues(roadmap_root))
    return sources, issues


def _chapter_issues(roadmap_root: Path) -> list[str]:
    """Reject a chapter directory that names no chapter.

    Containment is inferred from nested ``README.md`` articles, so a directory
    without one is invisible to the hierarchy: its pages attach to the root and
    the published book has no chapters at all. Every node still parses and
    every link still resolves, which is why this has to be asserted separately
    -- a real project reached publication with 71 of 72 articles at the root
    and a clean ``autoform check``.

    A load failure rather than an audit finding: audit is advisory, the
    generated CI never runs it, and it reports after the fact. The layout
    decides what the book is, so it belongs at the gate every author and both
    workflows already pass through.

    Only directories directly under ``roadmap/`` are chapters. Deeper ones --
    the ``definitions/`` and ``theorems/`` buckets a chapter files its articles
    into -- are a filing convention, and need no chapter page of their own.

    The count is recursive even though the chapter page is not. A chapter whose
    articles all sit in those buckets, ``orphan/theorems/leaf.md`` with nothing
    beside it, has no direct Markdown at all; counting only direct children
    read that as an empty directory and let exactly the layout this rejects
    through, with the articles attaching to the root and never reaching the
    generated nav.
    """

    try:
        chapters = sorted(path for path in roadmap_root.iterdir() if path.is_dir())
    except OSError:
        return []
    issues = []
    for chapter in chapters:
        articles = [path for path in chapter.rglob("*.md") if path.is_file()]
        if not articles:
            continue
        if (chapter / "README.md").is_file():
            continue
        names = articles
        issues.append(
            f"{chapter.name}: chapter directory holds {len(names)} article(s) but no "
            f"README.md, so they attach to the roadmap root instead of a chapter; "
            f"add {chapter.name}/README.md with the chapter's H1 title"
        )
    return issues


def _article_id(path: Path, roadmap_root: Path) -> str:
    relative = path.relative_to(roadmap_root)
    if relative.name == "README.md":
        parent = relative.parent.as_posix()
        return parent if parent != "." else "roadmap"
    return relative.with_suffix("").as_posix()


def _article_parents(parsed: list[_ParsedNode]) -> dict[str, str | None]:
    """Infer strict single-parent containment from nested README articles."""
    by_path = {node.path.resolve(): node.id for node in parsed}
    parents: dict[str, str | None] = {}
    for node in parsed:
        candidate = node.path.parent
        if node.path.name == "README.md":
            candidate = candidate.parent
        parent: str | None = None
        while candidate != candidate.parent:
            readme = (candidate / "README.md").resolve()
            if readme in by_path:
                parent = by_path[readme]
                break
            candidate = candidate.parent
        parents[node.id] = parent
    return parents


def _article_depth(node_id: str, parents: dict[str, str | None]) -> int:
    depth = 0
    parent = parents[node_id]
    while parent is not None:
        depth += 1
        parent = parents[parent]
    return depth


def _parse_node(node_id: str, path: Path, text: str) -> tuple[_ParsedNode | None, list[str]]:
    lines = text.splitlines()
    metadata, body_start, issues = _parse_frontmatter(node_id, lines)
    title: str | None = None
    title_count = 0
    targets: dict[str, list[str]] = {
        _STATEMENT_SECTION: [],
        _PROOF_SECTION: [],
        _SOURCES_SECTION: [],
    }
    section: str | None = None
    fence: tuple[str, int] | None = None
    body = _HTML_COMMENT.sub("", "\n".join(lines[body_start:]))

    for line in body.splitlines():
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            marker_kind = marker[0]
            if fence is None:
                fence = (marker_kind, len(marker))
            elif marker_kind == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            heading_text = heading.group(2).strip()
            if level == 1:
                title_count += 1
                if title is None:
                    title = heading_text
            if level <= 2:
                heading_key = heading_text.casefold()
                section = heading_key if level == 2 and heading_key in targets else None
            continue
        if section is not None:
            for match in _LINK.finditer(_INLINE_CODE.sub("", line)):
                target = match.group(1)
                if target.startswith("<") and target.endswith(">"):
                    target = target[1:-1]
                targets[section].append(target)

    if title is None:
        issues.append(f"{node_id}: missing H1 title")
    elif title_count > 1:
        issues.append(f"{node_id}: multiple H1 titles")
    if issues:
        return None, issues
    parsed = _ParsedNode(
        node_id,
        title,
        path,
        tuple(targets[_STATEMENT_SECTION]),
        tuple(targets[_PROOF_SECTION]),
        tuple(targets[_SOURCES_SECTION]),
        metadata,
    )
    return parsed, []


def _parse_frontmatter(node_id: str, lines: list[str]) -> tuple[dict[str, str], int, list[str]]:
    if not lines or lines[0].strip() != "---":
        return {}, 0, []

    issues: list[str] = []
    metadata: dict[str, str] = {}
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return {}, len(lines), [f"{node_id}: unterminated frontmatter"]

    for line_number, raw in enumerate(lines[1:end], start=2):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            issues.append(f"{node_id}:{line_number}: expected 'key: value' in frontmatter")
            continue
        key, value = (part.strip() for part in stripped.split(":", 1))
        if key not in _FRONTMATTER_KEYS:
            issues.append(f"{node_id}:{line_number}: unsupported frontmatter key {key!r}")
            continue
        if key in metadata:
            issues.append(f"{node_id}:{line_number}: duplicate frontmatter key {key!r}")
            continue
        value = _unquote_scalar(value)
        if not value:
            issues.append(f"{node_id}:{line_number}: empty frontmatter value for {key!r}")
            continue
        value, issue = _normalize_value(node_id, line_number, key, value)
        if issue:
            issues.append(issue)
            continue
        metadata[key] = value

    if "catalog" in metadata:
        for key in sorted(_CATALOG_DECLARATION_KEYS.intersection(metadata)):
            issues.append(
                f"{node_id}: 'catalog' cannot be combined with declaration-specific "
                f"frontmatter key {key!r}"
            )

    return metadata, end + 1, issues


def _normalize_value(node_id: str, line_number: int, key: str, value: str) -> tuple[str, str | None]:
    """Canonicalize an assertion value, or explain why it is not one."""
    location = f"{node_id}:{line_number}"
    folded = value.casefold()
    if key == "article_id":
        if not ARTICLE_ID_PATTERN.fullmatch(value):
            return value, f"{location}: malformed article_id {value!r}"
        return value, None
    if key == "catalog":
        if folded != "module":
            return value, f"{location}: 'catalog' accepts only 'module'"
        return folded, None
    if key in {"statement", "proof"}:
        if folded != _FORMALIZED:
            return value, f"{location}: {key!r} accepts only {_FORMALIZED!r}; omit the key otherwise"
        return folded, None
    if key in {"mathlib", "not_ready"}:
        if folded not in _TRUE | _FALSE:
            return value, f"{location}: {key!r} accepts only true or false"
        return folded, None
    if key == "origin":
        if folded not in {"cited", "bridged", "background"}:
            return value, f"{location}: 'origin' accepts cited, bridged, or background"
        return folded, None
    return value, None


def _unquote_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _resolve_target(
    node: _ParsedNode,
    target: str,
    blueprint: Path,
    canonical_ids: dict[Path, str],
) -> tuple[str | None, str | None]:
    split = urlsplit(target)
    if split.scheme or split.netloc or split.query:
        return None, f"{node.id}: dependency target must be a relative Markdown path: {target!r}"
    raw_path = unquote(split.path)
    if not raw_path:
        return None, f"{node.id}: dependency target must name a Markdown file: {target!r}"
    relative = Path(raw_path)
    if relative.is_absolute() or relative.suffix != ".md":
        return None, f"{node.id}: dependency target must be a relative .md file: {target!r}"

    resolved = (node.path.parent / relative).resolve()
    if not _is_within(resolved, blueprint):
        return None, f"{node.id}: dependency target escapes the blueprint directory: {target!r}"
    if not resolved.is_file():
        return None, f"{node.id}: dependency target does not exist: {target!r}"
    dependency = canonical_ids.get(resolved)
    if dependency is None:
        return None, f"{node.id}: dependency target is not a node: {target!r}"
    return dependency, None


def _find_cycles(nodes: dict[str, Node]) -> list[str]:
    state: dict[str, int] = {}
    stack: list[str] = []
    stack_indexes: dict[str, int] = {}
    issues: list[str] = []
    seen_issues: set[str] = set()

    for root_id in sorted(nodes):
        if state.get(root_id, 0) != 0:
            continue
        state[root_id] = 1
        stack_indexes[root_id] = len(stack)
        stack.append(root_id)
        frames = [(root_id, 0)]
        while frames:
            node_id, dependency_index = frames[-1]
            dependencies = nodes[node_id].dependencies
            if dependency_index == len(dependencies):
                frames.pop()
                stack.pop()
                stack_indexes.pop(node_id)
                state[node_id] = 2
                continue

            dependency = dependencies[dependency_index]
            frames[-1] = (node_id, dependency_index + 1)
            dependency_state = state.get(dependency, 0)
            if dependency_state == 0:
                state[dependency] = 1
                stack_indexes[dependency] = len(stack)
                stack.append(dependency)
                frames.append((dependency, 0))
            elif dependency_state == 1:
                cycle = stack[stack_indexes[dependency] :] + [dependency]
                message = f"dependency cycle: {' -> '.join(cycle)}"
                if message not in seen_issues:
                    seen_issues.add(message)
                    issues.append(message)
    return issues


def _find_rollup_cycles(nodes: dict[str, Node]) -> list[str]:
    """Reject cycles introduced by contracting articles at any hierarchy level."""
    children: dict[str | None, list[str]] = {}
    parents: dict[str, str | None] = {}
    for node in nodes.values():
        children.setdefault(node.parent, []).append(node.id)
        parents[node.id] = node.parent

    depths: dict[str, int] = {}
    roots: dict[str, str] = {}
    for node_id in nodes:
        if node_id in depths:
            continue
        trail: list[str] = []
        seen: set[str] = set()
        current: str | None = node_id
        while current is not None and current not in depths:
            if current in seen or current not in parents:
                raise ValueError("article containment is not a forest")
            seen.add(current)
            trail.append(current)
            current = parents[current]
        depth = depths[current] if current is not None else -1
        root = roots[current] if current is not None else trail[-1]
        for descendant in reversed(trail):
            depth += 1
            depths[descendant] = depth
            roots[descendant] = root

    ancestors: list[dict[str, str | None]] = [parents]
    maximum_depth = max(depths.values(), default=0)
    while 1 << len(ancestors) <= maximum_depth:
        previous = ancestors[-1]
        ancestors.append(
            {node_id: previous[parent] if parent is not None else None for node_id, parent in previous.items()}
        )

    def lift(node_id: str, distance: int) -> str:
        level = 0
        while distance:
            if distance & 1:
                parent = ancestors[level][node_id]
                if parent is None:
                    raise ValueError("article containment depth is inconsistent")
                node_id = parent
            distance >>= 1
            level += 1
        return node_id

    def lowest_common_ancestor(first: str, second: str) -> str | None:
        if roots[first] != roots[second]:
            return None
        if depths[first] < depths[second]:
            first, second = second, first
        first = lift(first, depths[first] - depths[second])
        if first == second:
            return first
        for level in range(len(ancestors) - 1, -1, -1):
            first_parent = ancestors[level][first]
            second_parent = ancestors[level][second]
            if first_parent != second_parent:
                if first_parent is None or second_parent is None:
                    continue
                first = first_parent
                second = second_parent
        return parents[first]

    def direct_child(scope: str | None, node_id: str) -> str:
        scope_depth = depths[scope] if scope is not None else -1
        return lift(node_id, depths[node_id] - scope_depth - 1)

    projections: dict[str | None, dict[str, set[str]]] = {}
    for target in nodes.values():
        for dependency in target.dependencies:
            scope = lowest_common_ancestor(target.id, dependency)
            if scope == target.id or scope == dependency:
                continue
            target_child = direct_child(scope, target.id)
            source_child = direct_child(scope, dependency)
            projections.setdefault(scope, {}).setdefault(target_child, set()).add(source_child)

    issues: list[str] = []
    seen_issues: set[str] = set()
    for scope, siblings in children.items():
        if len(siblings) < 2:
            continue
        projected = projections.get(scope)
        if not projected:
            continue
        dependencies = {sibling: projected.get(sibling, set()) for sibling in siblings}
        state: dict[str, int] = {}
        stack: list[str] = []
        stack_indexes: dict[str, int] = {}
        ordered_dependencies = {
            article_id: tuple(sorted(prerequisites)) for article_id, prerequisites in dependencies.items()
        }

        for root_id in sorted(dependencies):
            if state.get(root_id, 0) != 0:
                continue
            state[root_id] = 1
            stack_indexes[root_id] = len(stack)
            stack.append(root_id)
            frames = [(root_id, 0)]
            while frames:
                article_id, dependency_index = frames[-1]
                prerequisites = ordered_dependencies[article_id]
                if dependency_index == len(prerequisites):
                    frames.pop()
                    stack.pop()
                    stack_indexes.pop(article_id)
                    state[article_id] = 2
                    continue

                prerequisite = prerequisites[dependency_index]
                frames[-1] = (article_id, dependency_index + 1)
                prerequisite_state = state.get(prerequisite, 0)
                if prerequisite_state == 0:
                    state[prerequisite] = 1
                    stack_indexes[prerequisite] = len(stack)
                    stack.append(prerequisite)
                    frames.append((prerequisite, 0))
                elif prerequisite_state == 1:
                    cycle = stack[stack_indexes[prerequisite] :] + [prerequisite]
                    label = scope or "root"
                    message = f"rolled-up dependency cycle in {label}: {' -> '.join(cycle)}"
                    if message not in seen_issues:
                        seen_issues.add(message)
                        issues.append(message)
    return issues


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


__all__ = [
    "Graph",
    "GraphValidationError",
    "Node",
    "load_graph",
]
