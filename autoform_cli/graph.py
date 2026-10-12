"""Compile an Autoform dependency graph from its Markdown blueprint.

Markdown is both the human wiki and the sole authored graph representation:
node paths are stable ids, frontmatter carries checked facts, and links under
the two dependency headings are typed edges. ``Graph`` is only a validated
in-memory projection. It rejects broken links and cycles instead of persisting
a second graph file that could drift from the book.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .lean import declaration_names
from .markdown import (
    _is_hidden_path,
    directory_path,
    link_definitions,
    link_targets,
    markdown_links,
    mask_code_and_comments,
    resolved_path,
)


_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
ARTICLE_ID_PATTERN = re.compile(r"af_[0-9a-f]{24}\Z")
_FRONTMATTER_KEYS = frozenset(
    {
        "article_id",
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
        "review_approved",
        "open_statements",
        "published",
    }
)
_SKELETON_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FORMALIZED = "formalized"
_RETRACTED = "retracted"
_TRUE = frozenset({"true", "yes"})
_FALSE = frozenset({"false", "no"})

#: ``## Depends on`` carries the prerequisites needed to *state* a node;
#: ``## Proof depends on`` carries the extra prerequisites its *proof* needs.
#: Both are graph edges, mirroring where leanblueprint places ``\uses``.
_STATEMENT_SECTION = "depends on"
_PROOF_SECTION = "proof depends on"
_SOURCES_SECTION = "sources"
IMPLEMENTATION_NOTES_DIR = ".implementation-notes"


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
    statement_formalized: bool = False
    #: ``statement: retracted``: a revision retracted the statement while
    #: ``lean:`` still names the old declaration, which stays in the build
    #: until Formalize restates the article.
    statement_retracted: bool = False
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
    #: Hash of the complete review surface a person approved. Unlike the
    #: skeleton alone, this binds the article, source, and read-backs.
    review_approved: str | None = None

    @property
    def formalizable(self) -> bool:
        """Whether this article names a concrete Lean declaration."""
        return self.declaration is not None


@dataclass(frozen=True, slots=True)
class Graph:
    """A validated blueprint graph, keyed by stable node id."""

    blueprint_dir: Path
    nodes: dict[str, Node]
    #: ``open_statements: allowed`` in ``roadmap/README.md``: a theorem's
    #: statement may land with a ``sorry`` proof. Absent or ``forbidden`` keeps
    #: the strict policy, where CI rejects every ``sorry``.
    open_statements: bool = False
    #: Folders whose ``README.md`` says ``published: false``: check validates
    #: them, and render leaves them out of the site.
    unpublished: tuple[Path, ...] = ()

    @property
    def edge_count(self) -> int:
        return sum(len(node.dependencies) for node in self.nodes.values())

    def children(self, node_id: str) -> tuple[str, ...]:
        """Return the direct contained articles of *node_id*."""
        return tuple(node.id for node in self.nodes.values() if node.parent == node_id)


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

    given = Path(blueprint_dir).expanduser()
    if not os.path.isdir(given):
        raise GraphValidationError([f"blueprint directory does not exist: {given}"])
    # Resolved as its links are, even behind a link above it that cannot be read.
    blueprint = directory_path(given)
    if blueprint is None:
        raise GraphValidationError([f"blueprint directory cannot be resolved: {blueprint_dir}"])

    issues: list[str] = []
    parsed: list[_ParsedNode] = []
    canonical_ids: dict[Path, str] = {}
    node_ids: dict[str, Path] = {}
    sources, discovery_issues = _discover_nodes(blueprint)
    issues.extend(discovery_issues)
    article_ids: dict[str, str] = {}
    source_hashes = {source.id: source.source_sha256 for source in sources}
    policy_page = (blueprint / "roadmap" / "README.md").resolve()
    open_statements = False

    for source in sources:
        canonical = source.path.resolve()
        if canonical in canonical_ids:
            issues.append(f"{source.id}: duplicates node {canonical_ids[canonical]!r}")
            continue
        if source.id in node_ids:
            issues.append(f"{source.id}: duplicate node id also used by {node_ids[source.id]}")
            continue
        if ARTICLE_ID_PATTERN.fullmatch(source.id):
            # Selectors and claim keys accept either form, so the two must not overlap.
            issues.append(f"{source.id}: node id has the form of an article_id; rename its file or directory")
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
            policy = node.metadata.get("open_statements")
            if policy is not None:
                if canonical != policy_page:
                    issues.append(
                        f"{node.id}: open_statements is a project policy; set it only in roadmap/README.md"
                    )
                else:
                    open_statements = policy == "allowed"
            if "published" in node.metadata and (canonical.name != "README.md" or canonical == policy_page):
                issues.append(
                    f"{node.id}: published is a folder setting; set it in a folder's README.md below roadmap/"
                )
            parsed.append(node)

    if not issues:
        issues.extend(_implementation_note_issues(blueprint, article_ids))
    if issues:
        raise GraphValidationError(issues)

    parents = _article_parents(parsed, blueprint / "roadmap")
    issues.extend(_containment_cycles(parents))
    if issues:
        raise GraphValidationError(issues)
    unpublished = tuple(sorted(node.path.parent for node in parsed if node.metadata.get("published") in _FALSE))
    issues.extend(_unpublished_link_issues(sources, unpublished))
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
        nodes[parsed_node.id] = Node(
            id=parsed_node.id,
            title=parsed_node.title,
            path=parsed_node.path,
            dependencies=tuple(dependencies),
            statement_dependencies=tuple(statement_dependencies),
            proof_dependencies=tuple(proof_dependencies),
            kind="article",
            declaration=metadata.get("declaration"),
            lean=metadata.get("lean"),
            statement_formalized=metadata.get("statement") == _FORMALIZED,
            statement_retracted=metadata.get("statement") == _RETRACTED,
            proof_formalized=metadata.get("proof") == _FORMALIZED,
            mathlib=metadata.get("mathlib") in _TRUE,
            mathlib_declaration=metadata.get("mathlib_declaration"),
            mathlib_file=metadata.get("mathlib_file"),
            not_ready=metadata.get("not_ready") in _TRUE,
            discussion=metadata.get("discussion"),
            origin=metadata.get("origin"),
            sources=tuple(dict.fromkeys(parsed_node.source_targets)),
            parent=parents[parsed_node.id],
            depth=_article_depth(parsed_node.id, parents),
            article_id=metadata.get("article_id"),
            source_sha256=source_hashes[parsed_node.id],
            review_approved=metadata.get("review_approved"),
        )

    if not issues:
        issues.extend(_find_cycles(nodes))
    if issues:
        raise GraphValidationError(issues)
    return Graph(blueprint_dir=blueprint, nodes=nodes, open_statements=open_statements, unpublished=unpublished)


def _unpublished_link_issues(sources: list[_NodeSource], unpublished: tuple[Path, ...]) -> list[str]:
    """Name each link from a published page into an unpublished folder, which the site lacks.

    Render rewrites a page's links as text, applying their ``..`` steps, so they
    are read that way here: a dependency, a source and a prose link alike.
    """
    issues = []
    for source in sources:
        if any(_is_within(source.path, folder) for folder in unpublished):
            continue
        for _, target in markdown_links(source.text):
            try:
                split = urlsplit(target)
            except ValueError:
                continue  # never a path into the folder; other checks report it
            spelled = Path(os.path.normpath(source.path.parent / unquote(split.path)))
            if not (split.scheme or split.netloc) and any(_is_within(spelled, folder) for folder in unpublished):
                issues.append(f"{source.id}: links to {target!r}, in an unpublished folder render omits")
    return issues


def unpublished_content(files: tuple[tuple[str, bytes], ...]) -> tuple[tuple[str, ...], frozenset[str]]:
    """The folders captured *files* mark ``published: false``, and the article IDs inside them.

    For render, which leaves those folders out before it loads the graph but
    still knows their articles' implementation notes. A README that cannot be
    read here fails ``load_graph`` too.
    """
    folders = []
    for name, data in files:
        parts = name.split("/")
        if len(parts) > 2 and parts[0] == "roadmap" and parts[-1] == "README.md":
            try:
                metadata = _parse_frontmatter(name, data.decode("utf-8").splitlines())[0]
            except UnicodeError:
                continue
            if metadata.get("published") in _FALSE:
                folders.append(name.removesuffix("/README.md"))
    article_ids = set()
    for name, data in files:
        if name.endswith(".md") and any(name.startswith(f"{folder}/") for folder in folders):
            try:
                article_id = _parse_frontmatter(name, data.decode("utf-8").splitlines())[0].get("article_id")
            except UnicodeError:
                continue
            if article_id is not None:
                article_ids.add(article_id)
    return tuple(folders), frozenset(article_ids)


def _discover_nodes(blueprint: Path) -> tuple[list[_NodeSource], list[str]]:
    roadmap_root = blueprint / "roadmap"
    if not roadmap_root.is_dir():
        return [], [f"roadmap directory does not exist: {roadmap_root}"]

    symlink_issues = roadmap_symlink_issues(blueprint)
    if symlink_issues:
        # Containment and ids are read from paths, so one symbolic link can make two
        # pages each other's parent; refuse the layout before anything is read.
        return [], symlink_issues

    issues: list[str] = []
    sources: list[_NodeSource] = []
    roadmap_root = roadmap_root.resolve()

    def unlistable(exc: OSError) -> None:
        issues.append(f"cannot list roadmap directory {exc.filename}: {exc.strerror}")

    # Unlike a glob, a walk reports each directory it cannot list. Render omits
    # hidden entries, so they are no part of the roadmap here either.
    entries: list[Path] = []
    for directory, directories, files in os.walk(roadmap_root, onerror=unlistable):
        for name in directories:
            if name.casefold() == IMPLEMENTATION_NOTES_DIR.casefold():
                misplaced = (Path(directory) / name).relative_to(roadmap_root.parent).as_posix()
                issues.append(
                    f"{misplaced}: implementation notes directory belongs at the blueprint root, "
                    f"as {IMPLEMENTATION_NOTES_DIR}; nothing reads it here"
                )
        directories[:] = [name for name in directories if not name.startswith(".")]
        entries.extend(Path(directory, name) for name in files if not name.startswith("."))
    entries.sort()
    issues.sort()  # a walk visits directories in arbitrary order
    for path in entries:
        if path.suffix != ".md" and path.name.casefold() != "readme.md":
            continue
        relative = path.relative_to(roadmap_root).as_posix()
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                continue
            if path.name.casefold() == "readme.md" and path.name != "README.md":
                issues.append(
                    f"{relative}: noncanonical README filename; container pages must be named exactly README.md "
                    "for portable behavior on case-sensitive filesystems"
                )
            if path.suffix != ".md":
                continue
            content = path.read_bytes()
            text = content.decode("utf-8")
        except FileNotFoundError:
            continue  # removed since the walk
        except (OSError, UnicodeError) as exc:
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


def roadmap_symlink_issues(blueprint: Path) -> list[str]:
    """Report the symbolic links among ``roadmap/`` and the paths beneath it that are not hidden.

    Graph loading and runtime projection share this policy, so ``autoform check``
    accepts exactly the roadmaps the runtime does.
    """

    roadmap = blueprint / "roadmap"
    # os.path.islink is False for a path it cannot stat, which discovery then reports; Path.is_symlink raises
    # before 3.14.
    # Hidden entries, such as an editor's lock symlink, are no part of the roadmap.
    paths = [roadmap] if roadmap.is_symlink() else [
        path for path in sorted(roadmap.rglob("*")) if os.path.islink(path) and not _is_hidden_path(path, roadmap)
    ]
    return [f"roadmap contains a symbolic link: {path.relative_to(blueprint).as_posix()}" for path in paths]


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
        chapters = sorted(path for path in roadmap_root.iterdir() if path.is_dir() and not path.name.startswith("."))
    except OSError:
        return []
    issues = []
    for chapter in chapters:
        articles = [
            path for path in chapter.rglob("*.md") if os.path.isfile(path) and not _is_hidden_path(path, chapter)
        ]
        if not articles:
            continue
        if os.path.isfile(chapter / "README.md"):
            continue
        names = articles
        issues.append(
            f"{chapter.name}: chapter directory holds {len(names)} article(s) but no "
            f"README.md, so they attach to the roadmap root instead of a chapter; "
            f"add {chapter.name}/README.md with the chapter's H1 title"
        )
    return issues


def _implementation_note_issues(
    blueprint: Path, article_ids: dict[str, str]
) -> list[str]:
    """Validate hidden, per-article implementation notes.

    Notes are keyed by durable ``article_id`` rather than roadmap paths, so an
    article move does not orphan its handoff and independent articles never
    share a file. The hidden directory is already omitted by old and current
    renderers, keeping the format backward-compatible with pinned projects.
    """
    issues: list[str] = []
    try:
        aliases = [
            path.name
            for path in blueprint.iterdir()
            if path.name.casefold() == IMPLEMENTATION_NOTES_DIR.casefold()
            and path.name != IMPLEMENTATION_NOTES_DIR
        ]
    except OSError as error:
        return [f"cannot inspect implementation notes: {error}"]
    for alias in sorted(aliases):
        issues.append(
            f"{alias}: noncanonical implementation notes directory; "
            f"use exactly {IMPLEMENTATION_NOTES_DIR}"
        )

    root = blueprint / IMPLEMENTATION_NOTES_DIR
    if root.is_symlink():
        issues.append(
            f"{IMPLEMENTATION_NOTES_DIR}: implementation notes directory must not be a symlink"
        )
        return issues
    if not root.exists():
        return issues
    if not root.is_dir():
        issues.append(
            f"{IMPLEMENTATION_NOTES_DIR}: implementation notes path must be a directory"
        )
        return issues

    try:
        entries = sorted(root.iterdir())
    except OSError as error:
        issues.append(f"{IMPLEMENTATION_NOTES_DIR}: cannot read directory: {error}")
        return issues
    for path in entries:
        relative = f"{IMPLEMENTATION_NOTES_DIR}/{path.name}"
        # Hidden entries, such as an editor's lock symlink, are never notes.
        if path.name.startswith("."):
            continue
        if path.is_symlink():
            issues.append(f"{relative}: implementation note must be a regular file")
            continue
        if not path.is_file():
            issues.append(f"{relative}: implementation note must be a regular file")
            continue
        if path.suffix != ".md" or not ARTICLE_ID_PATTERN.fullmatch(path.stem):
            issues.append(
                f"{relative}: implementation note must be named <article_id>.md"
            )
            continue
        if path.stem not in article_ids:
            issues.append(
                f"{relative}: implementation note names no roadmap article; restore the "
                "article_id of an article that still exists, or delete the note after "
                "moving anything still useful to a surviving article's note"
            )
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            issues.append(f"{relative}: cannot read implementation note: {error}")
            continue
        if not text.replace("\ufeff", "").replace("\u200b", "").strip():
            issues.append(
                f"{relative}: empty implementation note; delete it instead"
            )
    return issues


def _article_id(path: Path, roadmap_root: Path) -> str:
    relative = path.relative_to(roadmap_root)
    if relative.name == "README.md":
        parent = relative.parent.as_posix()
        return parent if parent != "." else "roadmap"
    return relative.with_suffix("").as_posix()


def _article_parents(parsed: list[_ParsedNode], roadmap_root: Path) -> dict[str, str | None]:
    """Infer strict single-parent containment from nested README articles."""
    by_path = {node.path.resolve(): node.id for node in parsed}
    parents: dict[str, str | None] = {}
    for node in parsed:
        candidate = node.path.parent
        if node.path.name == "README.md":
            candidate = candidate.parent
        parent: str | None = None
        while _is_within(candidate, roadmap_root):
            readme = (candidate / "README.md").resolve()
            if readme in by_path:
                parent = by_path[readme]
                break
            candidate = candidate.parent
        parents[node.id] = parent
    return parents


def _containment_cycles(parents: dict[str, str | None]) -> list[str]:
    """Report each cycle of containing READMEs once, before depths are computed."""
    issues: list[str] = []
    for node_id in sorted(parents):
        chain = [node_id]
        seen = {node_id}
        parent = parents[node_id]
        while parent is not None and parent not in seen:
            chain.append(parent)
            seen.add(parent)
            parent = parents[parent]
        if parent == node_id and node_id == min(chain):
            issues.append(f"{node_id}: containment cycle: {' -> '.join([*chain, node_id])}")
    return issues


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

    # Fences, comments and indented code are read as the page and the audit
    # read them, so a section a reader sees never loses its links here, and a
    # link the page shows as code is not read as one.
    body = mask_code_and_comments(lines[body_start:])
    # A reference link takes its target from a definition anywhere on the page.
    references = link_definitions(body)
    for line in body:
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
            targets[section].extend(link_targets(line, references))

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

    if metadata.get("statement") == _RETRACTED:
        if "lean" not in metadata:
            issues.append(
                f"{node_id}: statement: retracted needs the lean: declaration it retracts;"
                " without lean:, omit statement"
            )
        if metadata.get("proof") == _FORMALIZED:
            issues.append(f"{node_id}: proof: formalized needs statement: formalized, not retracted")
        if metadata.get("mathlib") in _TRUE:
            issues.append(f"{node_id}: a mathlib: true article cannot record statement: retracted")
    # Load errors rather than audit findings: the generated workflows never run
    # the audit, so either state would otherwise publish as proved with no Lean
    # statement behind it. `mathlib: true` exempts neither.
    if metadata.get("proof") == _FORMALIZED and metadata.get("statement") not in {_FORMALIZED, _RETRACTED}:
        issues.append(f"{node_id}: proof: formalized needs statement: formalized")
    formalized = [key for key in ("statement", "proof") if metadata.get(key) == _FORMALIZED]
    if formalized and not declaration_names(metadata.get("lean", "")):
        issues.append(f"{node_id}: {formalized[0]}: formalized needs the lean: declaration that formalizes it")
    return metadata, end + 1, issues


def _normalize_value(node_id: str, line_number: int, key: str, value: str) -> tuple[str, str | None]:
    """Canonicalize an assertion value, or explain why it is not one."""
    location = f"{node_id}:{line_number}"
    folded = value.casefold()
    if key == "article_id":
        if not ARTICLE_ID_PATTERN.fullmatch(value):
            return value, f"{location}: malformed article_id {value!r}"
        return value, None
    if key == "statement":
        if folded not in {_FORMALIZED, _RETRACTED}:
            return value, (
                f"{location}: 'statement' accepts only {_FORMALIZED!r} or {_RETRACTED!r}; omit the key otherwise"
            )
        return folded, None
    if key == "proof":
        if folded != _FORMALIZED:
            return value, f"{location}: {key!r} accepts only {_FORMALIZED!r}; omit the key otherwise"
        return folded, None
    if key in {"mathlib", "not_ready", "published"}:
        if folded not in _TRUE | _FALSE:
            return value, f"{location}: {key!r} accepts only true or false"
        return folded, None
    if key == "origin":
        if folded not in {"cited", "bridged", "background"}:
            return value, f"{location}: 'origin' accepts cited, bridged, or background"
        return folded, None
    if key == "review_approved":
        if not _SKELETON_HASH.fullmatch(folded):
            return value, f"{location}: '{key}' must be a `sha256:<64 hex>` hash from `autoform review`"
        return folded, None
    if key == "open_statements":
        if folded not in {"allowed", "forbidden"}:
            return value, f"{location}: 'open_statements' accepts allowed or forbidden"
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
    try:
        split = urlsplit(target)
    except ValueError:
        return None, f"{node.id}: dependency target must be a relative Markdown path: {target!r}"
    if split.scheme or split.netloc or split.query:
        return None, f"{node.id}: dependency target must be a relative Markdown path: {target!r}"
    raw_path = unquote(split.path)
    if not raw_path:
        return None, f"{node.id}: dependency target must name a Markdown file: {target!r}"
    if "\x00" in raw_path:
        return None, f"{node.id}: dependency target contains an invalid path: {target!r}"
    relative = Path(raw_path)
    if relative.is_absolute() or relative.suffix != ".md":
        return None, f"{node.id}: dependency target must be a relative .md file: {target!r}"

    # A path that leaves the blueprint as spelled escapes it; nothing outside is looked at.
    spelled = Path(os.path.normpath(node.path.parent / relative))
    if not _is_within(spelled, blueprint):
        return None, f"{node.id}: dependency target escapes the blueprint directory: {target!r}"
    # Render's copy has no hidden folder, so it cannot step through one and back out either.
    if any(part.startswith(".") and part != ".." for part in relative.parts):
        return None, f"{node.id}: dependency target names a hidden path, which render omits: {target!r}"
    resolved = resolved_path(node.path.parent / relative)
    if resolved is None:
        # The kernel reaches a file behind a link only `readlink` refuses.
        why = "cannot be resolved" if os.path.exists(node.path.parent / relative) else "does not exist"
        return None, f"{node.id}: dependency target {why}: {target!r}"
    if not _is_within(resolved, blueprint):
        return None, f"{node.id}: dependency target escapes the blueprint directory: {target!r}"
    # Before Python 3.14, `Path.is_file` raises under an unreadable directory.
    if not os.path.isfile(resolved):
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
    "unpublished_content",
]
