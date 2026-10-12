"""Evidence for human review of formalized statements.

Review evidence is derived, every time it is needed, from the current
blueprint and a freshly extracted skeleton report. It contains only immutable
evidence: the authored statement, the cited source passage, and the exact Lean
packets. Read-backs remain separate committed testimony. The approval hash
combines one article's evidence with its current, strictly validated
read-backs, so changing either side invalidates the approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Mapping

from .graph import Graph, Node
from .lean import REVIEW_PACKET_SCHEMA, declaration_names
from .markdown import article_parts, published_page
from .readback import Readback, readback_for, readback_keys
from .skeleton import (
    PACKET_MANIFEST,
    SEMANTIC_SCHEMA,
    SKELETON_SCHEMA,
    DeclarationSkeleton,
    NodeSkeleton,
    OutputTransaction,
    SkeletonReport,
    _article_path,
    blueprint_hash,
    source_passage,
    validate_managed_output,
)


REVIEW_ARTICLE_SCHEMA = "autoform-review-article/v1"
REVIEW_APPROVAL_SCHEMA = "autoform-review-approval/v1"
_HASH_PREFIX = "sha256:"
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_UNSUPPORTED_SCHEMA = "skeleton report uses an unsupported report or semantic schema"


@dataclass(frozen=True, order=True, slots=True)
class ReviewFinding:
    """One reason review evidence cannot be trusted or approved."""

    node_id: str
    code: str
    reason: str


class ReviewError(ValueError):
    """Review evidence was malformed, incomplete, or inconsistent."""

    def __init__(self, findings: list[ReviewFinding] | tuple[ReviewFinding, ...]):
        self.findings = tuple(sorted(findings))
        self.issues = tuple(finding.reason for finding in self.findings)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True, slots=True)
class ReviewDeclaration:
    """One exact declaration packet in an article's review evidence."""

    name: str
    #: The declaration's statement hash, which a proof edit leaves unchanged.
    skeleton_hash: str
    packet_hash: str
    packet: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReviewArticle:
    """The complete pre-testimony evidence displayed for one article."""

    article_id: str
    node_id: str
    title: str
    statement: str
    passage: str | None
    passage_locator: str | None
    packet: str
    declarations: tuple[ReviewDeclaration, ...]

    @property
    def evidence_hash(self) -> str:
        """Hash the canonical review display before testimony is added."""

        return _hash_json(self._material())

    def declaration(self, name: str) -> ReviewDeclaration | None:
        return next((item for item in self.declarations if item.name == name), None)

    def _material(self) -> dict[str, object]:
        """Return approval evidence, excluding changeable location diagnostics."""

        return {
            "article_id": self.article_id,
            "declarations": [item.as_dict() for item in self.declarations],
            "packet": self.packet,
            "passage": self.passage,
            "passage_locator": self.passage_locator,
            "schema": REVIEW_ARTICLE_SCHEMA,
            "statement": self.statement,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class ReviewBundle:
    """The evidence for every Lean-mapped article, in ``article_id`` order."""

    articles: tuple[ReviewArticle, ...]

    def article(self, article_id: str) -> ReviewArticle | None:
        """Look up evidence by its durable article identity."""

        return next((article for article in self.articles if article.article_id == article_id), None)


@dataclass(frozen=True, slots=True)
class ReviewState:
    """Where one article's review stands. :func:`review_states` is the one
    place it is judged, so every reader of an approval agrees."""

    node_id: str
    #: The article's ``review_approved``, if it records one.
    approved: str | None
    #: The hash to approve, or ``None`` while ``findings`` stand.
    review_hash: str | None
    #: Why the article's read-backs are not all valid and current.
    findings: tuple[ReviewFinding, ...]

    @property
    def status(self) -> str:
        """``incomplete``, or ``unverifiable`` if an approval is recorded, until
        every read-back is valid and current; then ``unapproved``, ``drift``,
        or ``approved``."""

        if self.review_hash is None:
            return "incomplete" if self.approved is None else "unverifiable"
        if self.approved is None:
            return "unapproved"
        return "approved" if self.approved == self.review_hash else "drift"


def canonical_statement(graph: Graph, node: Node) -> str:
    """Return the article's statement as the site's statement box publishes it,
    before the box demotes its headings.

    That is :func:`~autoform_cli.markdown.article_parts`'s statement of the
    page :func:`~autoform_cli.markdown.published_page` makes: the span the
    audit checks, read after the HTML comments outside code are gone, while a
    comment in a fenced or indented code block stays, as the site shows it.
    Frontmatter and the title identify the article and are bound separately.
    The text is the one ``graph`` parsed ``node`` from: the article is read
    again and must still have the bytes `load_graph` hashed, since a statement
    written since would be judged against that parse's approval, a pairing that
    never existed on disk.
    """

    return article_parts(published_page(_parsed_text(node))).statement


def _parsed_text(node: Node) -> str:
    """The text of ``node``'s article, which must still hash as `load_graph` recorded."""

    try:
        data = read_regular_file(node.path)
    except OSError:
        data = None
    if data is None or (node.source_sha256 is not None and hashlib.sha256(data).hexdigest() != node.source_sha256):
        reason = "the article changed after the blueprint was loaded; rerun once the blueprint is idle"
        raise ReviewError([ReviewFinding(node.id, "review-snapshot-changed", reason)])
    return data.decode("utf-8")


def build_review_bundle(graph: Graph, skeleton: SkeletonReport) -> ReviewBundle:
    """Build the review evidence after proving the report is clean and complete.

    The report must have been extracted from exactly this blueprint: its
    ``blueprint_hash`` must name ``graph``. A report paired with articles it
    never saw is incoherent evidence, and is refused before anything in it is
    compared with them.
    """

    findings = _snapshot_findings(graph, skeleton) or _report_findings(graph, skeleton)
    if findings:
        raise ReviewError(findings)

    review_nodes = sorted(
        (node for node in graph.nodes.values() if node.lean is not None),
        key=lambda node: node.article_id or "",
    )
    # _report_findings found a record for each, and an article_id.
    return ReviewBundle(tuple(_current_article(graph, node, skeleton.node(node.id)) for node in review_nodes))


def review_states(
    graph: Graph,
    bundle: ReviewBundle,
    readbacks: Mapping[tuple[str, str], Readback],
) -> dict[str, ReviewState]:
    """Each article's review state, by node id.

    ``bundle`` must have been built from ``graph``, and ``readbacks`` are the
    cards it is judged with.
    """

    states: dict[str, ReviewState] = {}
    for article in bundle.articles:
        findings = tuple(_article_readback_findings(article, readbacks))
        states[article.node_id] = ReviewState(
            node_id=article.node_id,
            approved=graph.nodes[article.node_id].review_approved,
            review_hash=None if findings else _review_hash(article, readbacks),
            findings=findings,
        )
    return states


def review_findings(
    graph: Graph,
    bundle: ReviewBundle,
    readbacks: Mapping[tuple[str, str], Readback],
) -> list[ReviewFinding]:
    """Why the statement reviews are not all complete, current, and approved."""

    findings: list[ReviewFinding] = []
    for state in review_states(graph, bundle, readbacks).values():
        findings.extend(state.findings)
        status = state.status
        if status == "unverifiable":
            reason = "review_approved is present but the required read-backs are not all valid and current"
            findings.append(ReviewFinding(state.node_id, "review-approval-unverifiable", reason))
        elif status == "unapproved":
            reason = (
                f"complete review evidence is ready but review_approved is not recorded; approve {state.review_hash}"
            )
            findings.append(ReviewFinding(state.node_id, "review-unapproved", reason))
        elif status == "drift":
            reason = f"review_approved is {state.approved} but the current complete review is {state.review_hash}"
            findings.append(ReviewFinding(state.node_id, "review-drift", reason))

    expected = {
        key
        for article in bundle.articles
        for item in article.declarations
        for key in readback_keys(article.article_id, item.name)
    }
    current_nodes = {node.article_id: node for node in graph.nodes.values() if node.article_id is not None}
    for article_id, name in sorted(set(readbacks) - expected):
        node = current_nodes.get(article_id)
        findings.append(
            ReviewFinding(
                node.id if node is not None else article_id,
                "readback-orphaned",
                f"read-back filed for {name} under article_id {article_id} is not named by the review evidence; "
                f"delete {readbacks[(article_id, name)].path}, or restore the article_id and declaration it names",
            )
        )

    bundled = {article.article_id for article in bundle.articles}
    for node_id, node in sorted(graph.nodes.items()):
        if node.review_approved is None:
            continue
        if node.lean is None:
            reason = "review_approved is present on an article that is not mapped to Lean"
        elif node.article_id not in bundled:
            reason = "review_approved is present but the article has no review evidence"
        else:
            continue
        findings.append(ReviewFinding(node_id, "review-approval-orphaned", reason))
    return sorted(findings)


def write_review_packets(bundle: ReviewBundle, directory: str | Path) -> list[Path]:
    """Atomically publish opaque packet files and a coordinator-only manifest.

    Article and declaration names belong in the manifest, not in the filename
    handed to a blind reader. A packet file is named by the hash of its bytes,
    so identical packets share one immutable file.
    """

    requested = Path(directory).expanduser()
    if requested.is_symlink():
        raise ReviewError(
            [ReviewFinding("", "review-packets-unsafe", f"refusing symlink packet output: {requested}")]
        )
    root = Path(os.path.abspath(requested))
    entries: list[dict[str, str]] = []
    written: set[Path] = set()
    with OutputTransaction() as transaction:
        try:
            identity = validate_managed_output(root, kind="packets", schema=REVIEW_PACKET_SCHEMA)
            stage = transaction.directory(root, identity)
        except Exception as exc:
            issues = getattr(exc, "issues", (str(exc),))
            raise ReviewError(
                [ReviewFinding("", "review-packets-unsafe", str(issue)) for issue in issues]
            ) from exc

        (stage / "blind").mkdir()
        for article in bundle.articles:
            for declaration in article.declarations:
                content = declaration.packet.encode("utf-8")
                packet_hash = _hash_bytes(content)
                relative = Path("blind") / f"{packet_hash.removeprefix(_HASH_PREFIX)}.lean"
                (stage / relative).write_bytes(content)
                written.add(root / relative)
                entries.append(
                    {
                        "article_id": article.article_id,
                        "declaration": declaration.name,
                        "node_id": article.node_id,
                        "packet": relative.as_posix(),
                        "packet_hash": packet_hash,
                        "skeleton_hash": declaration.skeleton_hash,
                    }
                )
        manifest = {"kind": "packets", "packets": entries, "schema": REVIEW_PACKET_SCHEMA}
        (stage / PACKET_MANIFEST).write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        transaction.commit()
    return sorted(written)


def read_review_packet(directory: str | Path, article_id: str, declaration: str) -> tuple[str, str, str]:
    """The skeleton hash, packet hash, and packet text that
    :func:`write_review_packets` wrote to ``directory`` for one declaration.

    The packet file is the one its hash names, not whatever path the manifest
    gives, and :func:`~autoform_cli.readback.prepare_readback` checks that its
    bytes are the ones that hash names. Nothing here judges whether the packet
    is still current: a card filed from a stale manifest is reported invalid
    by ``review check``, and no approval can be computed from it.
    """

    root = Path(directory).expanduser()
    manifest_path = root / PACKET_MANIFEST

    def refused(reason: str) -> ReviewError:
        return ReviewError([ReviewFinding(article_id, "review-packet-invalid", reason)])

    try:
        # A number too long to convert and malformed JSON are ValueErrors;
        # arrays nested deeper than the decoder recurses, a RecursionError.
        manifest = json.loads(read_regular_file(manifest_path).decode("utf-8"))
    except (OSError, RecursionError, ValueError) as exc:
        raise refused(f"cannot read the packet manifest {manifest_path}: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != REVIEW_PACKET_SCHEMA
        or not isinstance(manifest.get("packets"), list)
    ):
        raise refused(f"{manifest_path} is not a manifest `review prepare` wrote")
    entries = [
        entry
        for entry in manifest["packets"]
        if isinstance(entry, dict) and entry.get("article_id") == article_id and entry.get("declaration") == declaration
    ]
    if len(entries) != 1:
        reason = f"{manifest_path} has no single packet for {declaration} under article_id {article_id}"
        raise ReviewError([ReviewFinding(article_id, "review-selection-missing", reason)])
    skeleton_hash, packet_hash = entries[0].get("skeleton_hash"), entries[0].get("packet_hash")
    if not all(isinstance(value, str) and _HASH.fullmatch(value) for value in (skeleton_hash, packet_hash)):
        raise refused(f"{manifest_path} records a malformed hash for {declaration}")
    path = root / "blind" / f"{packet_hash.removeprefix(_HASH_PREFIX)}.lean"
    try:
        packet = read_regular_file(path).decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise refused(f"cannot read the packet {path}: {exc}") from exc
    return skeleton_hash, packet_hash, packet


def read_regular_file(path: Path) -> bytes:
    """The bytes of the regular file at ``path``."""

    # Opened without waiting for a writer, so a FIFO is refused unread
    # instead of holding the command.
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    with open(path, "rb", opener=lambda name, flags: os.open(name, flags | nonblocking)) as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"not a regular file: {path}")
        return handle.read()


def _snapshot_findings(graph: Graph, skeleton: SkeletonReport) -> list[ReviewFinding]:
    """Refuse a report extracted from a blueprint other than ``graph``.

    Every other comparison between the two would mix states that never
    coexisted, so this finding stands alone.
    """

    if skeleton.blueprint_hash == blueprint_hash(graph):
        return []
    return [
        ReviewFinding(
            "",
            "review-snapshot-changed",
            "the blueprint changed after its review evidence was extracted; rerun once the blueprint is idle",
        )
    ]


def _article_shape_finding(node: Node, parents: set[str]) -> ReviewFinding | None:
    if node.formalizable and node.id not in parents:
        return None
    return ReviewFinding(
        node.id,
        "review-article-shape",
        "Lean-mapped review article must be a declaration-sized leaf rendered by the blueprint site",
    )


def _report_findings(graph: Graph, skeleton: SkeletonReport) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    # Indexed once, so no article's shape check rescans the graph for its children.
    parents = {item.parent for item in graph.nodes.values() if item.parent is not None}
    for node in sorted(graph.nodes.values(), key=lambda item: item.id):
        if node.lean is None:
            continue
        if node.article_id is None:
            reason = "Lean-mapped article has no durable article_id; run `autoform migrate` before preparing review evidence"
            findings.append(ReviewFinding(node.id, "review-article-id-missing", reason))
        shape = _article_shape_finding(node, parents)
        if shape is not None:
            findings.append(shape)
    if skeleton.schema != SKELETON_SCHEMA or skeleton.semantic_schema != SEMANTIC_SCHEMA:
        findings.append(ReviewFinding("", "review-report-schema", _UNSUPPORTED_SCHEMA))
    if skeleton.selection != "all":
        reason = "review evidence needs a skeleton report that selects every article, not a filtered one"
        findings.append(ReviewFinding("", "review-report-scope", reason))
    expected = _declaration_mapping(graph)
    if dict(skeleton.targets) != expected:
        reason = "skeleton report's target set does not match the graph's lean metadata"
        findings.append(ReviewFinding("", "review-report-targets", reason))
    report_ids = [node.node_id for node in skeleton.nodes]
    if len(report_ids) != len(set(report_ids)):
        findings.append(ReviewFinding("", "review-report-duplicate", "skeleton report contains duplicate article ids"))
    for issue in skeleton.unresolved:
        reason = f"skeleton extraction is incomplete: {issue.message}"
        findings.append(ReviewFinding(issue.node_id, "review-report-unresolved", reason))
    actual = {node.node_id for node in skeleton.nodes}
    for node_id in sorted(set(expected) - actual):
        reason = "skeleton report omits this Lean-mapped article"
        findings.append(ReviewFinding(node_id, "review-report-missing", reason))
    for node_id in sorted(actual - set(expected)):
        reason = "skeleton report contains an article not mapped to Lean"
        findings.append(ReviewFinding(node_id, "review-report-extra", reason))
    for node_id in sorted(set(expected) & actual):
        findings.extend(_node_findings(graph, graph.nodes[node_id], skeleton.node(node_id)))
    return sorted(findings)


def _node_findings(graph: Graph, node: Node, record: NodeSkeleton) -> Iterator[ReviewFinding]:
    """Why ``record`` is not ``node``'s current declarations, path, or cited passage, in that order."""

    expected = tuple(declaration_names(node.lean or ""))
    actual = tuple(item.name for item in record.declarations)
    if not expected or len(set(expected)) != len(expected):
        yield ReviewFinding(node.id, "review-mapping-invalid", "lean metadata must name distinct declarations")
    elif actual != expected:
        yield ReviewFinding(
            node.id,
            "review-mapping-drift",
            f"skeleton declarations {actual!r} do not match current lean metadata {expected!r}",
        )
    expected_path = _article_path(node, graph)
    if record.article_path != expected_path:
        yield ReviewFinding(
            node.id,
            "review-path-drift",
            f"skeleton article path {record.article_path!r} does not match {expected_path!r}",
        )
    current_passage, current_locator = source_passage(node, graph.blueprint_dir)
    # A cited article must show its passage; any article's passage must not be blank.
    if (current_passage is None and node.origin == "cited") or (
        current_passage is not None and not current_passage.strip()
    ):
        yield ReviewFinding(
            node.id,
            "review-source-passage-missing",
            "Lean article has no exact local source passage, or a blank one; cite a non-Markdown source with "
            "#L<start>-L<end>",
        )
    if (record.passage, record.passage_locator) != (current_passage, current_locator):
        yield ReviewFinding(
            node.id,
            "review-source-drift",
            "skeleton source passage does not match the article's current source locator",
        )


def _current_article(graph: Graph, node: Node, record: NodeSkeleton) -> ReviewArticle:
    """The review evidence for ``node`` from its extracted ``record``."""

    statement = canonical_statement(graph, node)
    if not statement.strip():
        reason = "article has no mathematical statement before its first H2"
        raise ReviewError([ReviewFinding(node.id, "review-statement-missing", reason)])
    return ReviewArticle(
        article_id=node.article_id,
        node_id=node.id,
        title=node.title,
        statement=statement,
        passage=record.passage,
        passage_locator=record.passage_locator,
        packet=record.blind_text(),
        declarations=tuple(_review_declaration(item) for item in record.declarations),
    )


def _declaration_mapping(graph: Graph) -> dict[str, tuple[str, ...]]:
    return {
        node_id: tuple(declaration_names(node.lean or ""))
        for node_id, node in graph.nodes.items()
        if node.lean is not None
    }


def _review_declaration(declaration: DeclarationSkeleton) -> ReviewDeclaration:
    return ReviewDeclaration(
        name=declaration.name,
        skeleton_hash=declaration.statement_hash,
        packet_hash=declaration.evidence_hash,
        packet=declaration.blind_text(),
    )


def _article_readback_findings(
    article: ReviewArticle,
    readbacks: Mapping[tuple[str, str], Readback],
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for declaration in article.declarations:
        card = readback_for(readbacks, article.article_id, declaration.name)
        if card is None:
            reason = f"no read-back filed for {declaration.name}"
            findings.append(ReviewFinding(article.node_id, "readback-missing", reason))
            continue
        errors = list(card.validate())
        # A card that could not be read is reported by that reason alone.
        if not card.unreadable:
            if card.article_id != article.article_id:
                errors.append("card identity does not match the prepared article_id")
            if card.declaration != declaration.name:
                errors.append("declaration metadata does not match the card path")
            if card.skeleton_hash != declaration.skeleton_hash:
                errors.append("skeleton hash does not match the prepared declaration")
            if card.packet_hash != declaration.packet_hash:
                errors.append("packet hash does not match the prepared declaration")
            if card.shown_hash != declaration.packet_hash:
                errors.append("displayed packet is missing or differs from the prepared declaration")
            if getattr(card, "shown_text", None) != declaration.packet:
                errors.append("displayed packet bytes differ from the prepared declaration")
        if errors:
            reason = f"read-back for {declaration.name} is invalid: " + "; ".join(dict.fromkeys(errors))
            findings.append(ReviewFinding(article.node_id, "readback-invalid", reason))
    return findings


def _review_hash(article: ReviewArticle, readbacks: Mapping[tuple[str, str], Readback]) -> str:
    """Hash one article together with its ordered read-backs, which must all be
    valid and current. This is the sole value a person records as
    ``review_approved``."""

    testimony = []
    for declaration in article.declarations:
        card = readback_for(readbacks, article.article_id, declaration.name)
        testimony.append(
            {
                "card_hash": card.file_hash,
                "declaration": declaration.name,
                "model": card.model,
                "packet": declaration.packet,
                "packet_hash": declaration.packet_hash,
                "skeleton_hash": declaration.skeleton_hash,
                "text": card.text,
            }
        )
    return _hash_json({"evidence": article.evidence_hash, "readbacks": testimony, "schema": REVIEW_APPROVAL_SCHEMA})


def _hash_bytes(material: bytes) -> str:
    return _HASH_PREFIX + hashlib.sha256(material).hexdigest()


def _hash_json(material: object) -> str:
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _hash_bytes(encoded)


__all__ = [
    "REVIEW_APPROVAL_SCHEMA",
    "REVIEW_ARTICLE_SCHEMA",
    "REVIEW_PACKET_SCHEMA",
    "ReviewArticle",
    "ReviewBundle",
    "ReviewDeclaration",
    "ReviewError",
    "ReviewFinding",
    "ReviewState",
    "build_review_bundle",
    "canonical_statement",
    "read_regular_file",
    "read_review_packet",
    "review_findings",
    "review_states",
    "write_review_packets",
]
