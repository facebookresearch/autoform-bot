"""Versioned evidence bundles for human review of formalized statements.

A review bundle is prepared from the current blueprint and a freshly extracted
skeleton report.  It contains only immutable evidence: the authored statement,
the cited source passage, and the exact Lean packets.  Read-backs remain
separate committed testimony.  The approval hash combines one bundle article
with the current, strictly validated read-backs, so changing either side
invalidates the approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from .graph import ARTICLE_ID_PATTERN, Graph, Node
from .lean import declaration_names
from .markdown import FENCE, FENCE_CLOSE, HEADING, frontmatter_end, strip_line_comments
from .readback import Readback, load_readbacks
from .skeleton import (
    PACKET_MANIFEST,
    SEMANTIC_SCHEMA,
    SKELETON_SCHEMA,
    DeclarationSkeleton,
    SkeletonReport,
    replace_managed_outputs,
    source_passage,
    stage_managed_output,
    validate_managed_output,
)


REVIEW_BUNDLE_SCHEMA = "autoform-review-bundle/v1"
REVIEW_ARTICLE_SCHEMA = "autoform-review-article/v1"
REVIEW_APPROVAL_SCHEMA = "autoform-review-approval/v1"
REVIEW_PACKET_SCHEMA = "autoform-review-packets/v1"
REVIEW_RECORDS_SCHEMA = "autoform-review-records/v1"
_HASH_PREFIX = "sha256:"
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")


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
    """One exact declaration packet in a prepared bundle."""

    name: str
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
    article_path: str
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

    def as_dict(self) -> dict[str, object]:
        return {
            **self._material(),
            "article_path": self.article_path,
            "evidence_hash": self.evidence_hash,
            "node_id": self.node_id,
        }


@dataclass(frozen=True, slots=True)
class ReviewBundle:
    """A strict, immutable snapshot of the evidence awaiting read-backs."""

    articles: tuple[ReviewArticle, ...]
    schema: str = REVIEW_BUNDLE_SCHEMA

    @property
    def hash(self) -> str:
        """Fingerprint this bundle as a whole for logs and transport checks."""

        return _hash_json(
            {
                "articles": [article.as_dict() for article in self.articles],
                "schema": self.schema,
            }
        )

    def article(self, article_id: str) -> ReviewArticle | None:
        """Look up prepared evidence by its durable article identity."""

        return next((article for article in self.articles if article.article_id == article_id), None)

    def declaration(self, article_id: str, name: str) -> ReviewDeclaration | None:
        article = self.article(article_id)
        return None if article is None else article.declaration(name)

    def review_hash(
        self,
        article_id: str,
        readbacks: Mapping[tuple[str, str], Readback],
    ) -> str:
        """Hash one article together with its ordered, valid read-backs.

        This is the sole value a human records as ``review_approved``.  Refuse
        to produce it when any testimony is missing or malformed.
        """

        article = self.article(article_id)
        if article is None:
            raise ReviewError(
                [
                    ReviewFinding(
                        article_id,
                        "review-bundle-missing",
                        "review bundle has no such article_id",
                    )
                ]
            )
        findings = _article_readback_findings(article, readbacks)
        if findings:
            raise ReviewError(findings)
        testimony = []
        for declaration in article.declarations:
            card = readbacks[(article_id, declaration.name)]
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
        return _hash_json(
            {
                "evidence": article.evidence_hash,
                "readbacks": testimony,
                "schema": REVIEW_APPROVAL_SCHEMA,
            }
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "articles": [article.as_dict() for article in self.articles],
            "hash": self.hash,
            "schema": self.schema,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_statement(node: Node) -> str:
    """Return visible Markdown after the H1 and before the first H2.

    Frontmatter and the title identify the article and are bound separately.
    Outer blank lines are formatting, while all Markdown inside the statement
    is preserved exactly after newline normalization.
    """

    try:
        text = node.path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReviewError(
            [ReviewFinding(node.id, "review-statement-unreadable", f"cannot read article statement: {exc}")]
        ) from exc
    lines = text.splitlines()
    statement: list[str] = []
    seen_h1 = False
    fence: tuple[str, int] | None = None
    in_comment = False
    for raw_line in lines[frontmatter_end(lines) :]:
        if fence is None and not in_comment and FENCE.match(raw_line):
            line = raw_line
        elif fence is None:
            line, in_comment = strip_line_comments(raw_line, in_comment)
        else:
            line = raw_line
        fence_match = FENCE.match(line) if fence is None else FENCE_CLOSE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            if seen_h1:
                statement.append(line)
            continue
        heading = None if fence is not None else HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            if not seen_h1:
                if level == 1:
                    seen_h1 = True
                continue
            if level == 2:
                break
        if seen_h1:
            statement.append(line)
    return "\n".join(statement).strip("\n")


def build_review_bundle(graph: Graph, skeleton: SkeletonReport) -> ReviewBundle:
    """Build prepared evidence after proving the report is clean and complete."""

    findings = _report_findings(graph, skeleton)
    if findings:
        raise ReviewError(findings)

    articles: list[ReviewArticle] = []
    review_nodes = sorted(
        (node for node in graph.nodes.values() if node.lean is not None),
        key=lambda node: node.article_id or "",
    )
    for node in review_nodes:
        node_id = node.id
        record = skeleton.node(node_id)
        assert record is not None
        assert node.article_id is not None
        statement = canonical_statement(node)
        if not statement.strip():
            raise ReviewError(
                [
                    ReviewFinding(
                        node_id,
                        "review-statement-missing",
                        "article has no mathematical statement between its H1 and first H2",
                    )
                ]
            )
        articles.append(
            ReviewArticle(
                article_id=node.article_id,
                node_id=node_id,
                article_path=_article_path(node, graph),
                title=node.title,
                statement=statement,
                passage=record.passage,
                passage_locator=record.passage_locator,
                packet=record.blind_text(),
                declarations=tuple(_review_declaration(item) for item in record.declarations),
            )
        )
    return ReviewBundle(tuple(articles))


def validate_review_bundle(
    graph: Graph,
    bundle: ReviewBundle,
    current_skeleton: SkeletonReport,
) -> tuple[ReviewFinding, ...]:
    """Compare prepared evidence with the current graph and fresh Lean report."""

    try:
        current = build_review_bundle(graph, current_skeleton)
    except ReviewError as exc:
        return exc.findings

    findings: list[ReviewFinding] = []
    current_ids = {article.article_id for article in current.articles}
    bundle_ids = {article.article_id for article in bundle.articles}
    for article_id in sorted(current_ids - bundle_ids):
        current_article = current.article(article_id)
        assert current_article is not None
        findings.append(
            ReviewFinding(
                current_article.node_id,
                "review-bundle-missing",
                f"prepared bundle has no evidence for article_id {article_id}",
            )
        )
    for article_id in sorted(bundle_ids - current_ids):
        prepared = bundle.article(article_id)
        assert prepared is not None
        findings.append(
            ReviewFinding(
                prepared.node_id,
                "review-bundle-orphaned",
                f"prepared bundle contains article_id {article_id}, which the graph no longer maps to Lean",
            )
        )
    for article_id in sorted(current_ids & bundle_ids):
        prepared = bundle.article(article_id)
        expected = current.article(article_id)
        assert prepared is not None and expected is not None
        if prepared._material() != expected._material():
            findings.append(
                ReviewFinding(
                    expected.node_id,
                    "review-bundle-drift",
                    "prepared review evidence differs from the current statement, source passage, declaration mapping, or Lean packet",
                )
            )
    return tuple(sorted(findings))


def validate_review_article(
    graph: Graph,
    bundle: ReviewBundle,
    current_skeleton: SkeletonReport,
    article_id: str,
) -> tuple[ReviewFinding, ...]:
    """Validate one article against a deliberately scoped fresh extraction.

    Recording one asynchronously produced read-back must not be blocked because
    another article changed meanwhile. The scoped report must contain exactly
    the selected article, so omitted evidence cannot accidentally pass as a
    successful check.
    """

    nodes = [node for node in graph.nodes.values() if node.article_id == article_id]
    if len(nodes) != 1:
        return (
            ReviewFinding(
                article_id,
                "review-article-missing",
                f"current graph has no unique article with article_id {article_id}",
            ),
        )
    node = nodes[0]
    if node.lean is None:
        return (
            ReviewFinding(
                node.id,
                "review-article-not-lean",
                f"article_id {article_id} is no longer mapped to Lean",
            ),
        )
    if current_skeleton.schema != SKELETON_SCHEMA or current_skeleton.semantic_schema != SEMANTIC_SCHEMA:
        return (
            ReviewFinding(
                node.id,
                "review-report-schema",
                "skeleton report uses an unsupported report or semantic schema",
            ),
        )
    if current_skeleton.unresolved:
        return tuple(
            ReviewFinding(
                node.id,
                "review-report-unresolved",
                f"scoped skeleton extraction is incomplete: {issue}",
            )
            for issue in current_skeleton.unresolved
        )
    if len(current_skeleton.nodes) != 1 or current_skeleton.nodes[0].node_id != node.id:
        return (
            ReviewFinding(
                node.id,
                "review-report-scope",
                "scoped skeleton report must contain exactly the selected current article",
            ),
        )
    report_node = current_skeleton.nodes[0]
    expected_names = tuple(declaration_names(node.lean))
    actual_names = tuple(declaration.name for declaration in report_node.declarations)
    if not expected_names or len(set(expected_names)) != len(expected_names):
        return (
            ReviewFinding(
                node.id,
                "review-mapping-invalid",
                "lean metadata must name distinct declarations",
            ),
        )
    if actual_names != expected_names:
        return (
            ReviewFinding(
                node.id,
                "review-mapping-drift",
                f"skeleton declarations {actual_names!r} do not match current lean metadata {expected_names!r}",
            ),
        )
    expected_path = _article_path(node, graph)
    if report_node.article_path != expected_path:
        return (
            ReviewFinding(
                node.id,
                "review-path-drift",
                f"skeleton article path {report_node.article_path!r} does not match {expected_path!r}",
            ),
        )
    current_passage, current_locator = source_passage(node, graph.blueprint_dir)
    if node.origin == "cited" and (current_passage is None or not current_passage.strip()):
        return (
            ReviewFinding(
                node.id,
                "review-source-passage-missing",
                "cited Lean article has no exact local source passage; cite a non-Markdown source with #L<start>-L<end>",
            ),
        )
    if (report_node.passage, report_node.passage_locator) != (current_passage, current_locator):
        return (
            ReviewFinding(
                node.id,
                "review-source-drift",
                "skeleton source passage does not match the article's current source locator",
            ),
        )
    try:
        statement = canonical_statement(node)
    except ReviewError as exc:
        return exc.findings
    if not statement.strip():
        return (
            ReviewFinding(
                node.id,
                "review-statement-missing",
                "article has no mathematical statement between its H1 and first H2",
            ),
        )
    current = ReviewArticle(
        article_id=article_id,
        node_id=node.id,
        article_path=expected_path,
        title=node.title,
        statement=statement,
        passage=report_node.passage,
        passage_locator=report_node.passage_locator,
        packet=report_node.blind_text(),
        declarations=tuple(_review_declaration(item) for item in report_node.declarations),
    )
    prepared = bundle.article(article_id)
    if prepared is None:
        return (
            ReviewFinding(
                node.id,
                "review-bundle-missing",
                f"prepared bundle has no evidence for article_id {article_id}",
            ),
        )
    if prepared._material() != current._material():
        return (
            ReviewFinding(
                node.id,
                "review-bundle-drift",
                "prepared review evidence differs from the current statement, source passage, declaration mapping, or Lean packet",
            ),
        )
    return ()


def review_findings(
    graph: Graph,
    bundle: ReviewBundle,
    current_skeleton: SkeletonReport,
    readbacks: Mapping[tuple[str, str], Readback] | None = None,
) -> list[ReviewFinding]:
    """Validate evidence, testimony, and each recorded human approval."""

    evidence_findings = list(validate_review_bundle(graph, bundle, current_skeleton))
    cards = load_readbacks(graph.blueprint_dir) if readbacks is None else readbacks
    identity_findings = _approval_identity_findings(graph, bundle)
    if evidence_findings:
        return sorted([*evidence_findings, *identity_findings])
    findings: list[ReviewFinding] = []
    expected_cards: set[tuple[str, str]] = set()
    invalid_articles: set[str] = set()
    for article in bundle.articles:
        expected_cards.update((article.article_id, item.name) for item in article.declarations)
        card_findings = _article_readback_findings(article, cards)
        findings.extend(card_findings)
        if card_findings:
            invalid_articles.add(article.article_id)
    current_nodes = {
        node.article_id: node
        for node in graph.nodes.values()
        if node.article_id is not None
    }
    for article_id, name in sorted(set(cards) - expected_cards):
        node = current_nodes.get(article_id)
        findings.append(
            ReviewFinding(
                node.id if node is not None else article_id,
                "readback-orphaned",
                f"read-back filed for {name} under article_id {article_id} is not named by the prepared review bundle",
            )
        )

    for article in bundle.articles:
        node = current_nodes[article.article_id]
        if article.article_id in invalid_articles:
            if node.review_approved is not None:
                findings.append(
                    ReviewFinding(
                        node.id,
                        "review-approval-unverifiable",
                        "review_approved is present but the required read-backs are not all valid and current",
                    )
                )
            continue
        expected = bundle.review_hash(article.article_id, cards)
        if node.review_approved is None:
            findings.append(
                ReviewFinding(
                    node.id,
                    "review-unapproved",
                    f"complete review evidence is ready but review_approved is not recorded; approve {expected}",
                )
            )
        elif node.review_approved != expected:
            findings.append(
                ReviewFinding(
                    node.id,
                    "review-drift",
                    f"review_approved is {node.review_approved} but the current complete review is {expected}",
                )
            )
    return sorted([*findings, *identity_findings])


def load_review_bundle(path: str | Path) -> ReviewBundle:
    """Load a bundle and reject unknown fields, malformed values, or bad hashes."""

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewError(
            [ReviewFinding("", "review-bundle-invalid", f"cannot read review bundle {path}: {exc}")]
        ) from exc
    if not isinstance(data, dict) or set(data) != {"articles", "hash", "schema"}:
        raise _invalid_bundle("review bundle has malformed top-level fields")
    if data.get("schema") != REVIEW_BUNDLE_SCHEMA or not isinstance(data.get("articles"), list):
        raise _invalid_bundle(f"review bundle is not {REVIEW_BUNDLE_SCHEMA}")
    articles = tuple(_article_from_dict(item) for item in data["articles"])
    if len({article.article_id for article in articles}) != len(articles):
        raise _invalid_bundle("review bundle contains duplicate article ids")
    if tuple(sorted(articles, key=lambda article: article.article_id)) != articles:
        raise _invalid_bundle("review bundle articles are not in canonical article_id order")
    bundle = ReviewBundle(articles)
    if data.get("hash") != bundle.hash:
        raise _invalid_bundle("review bundle hash does not match its contents")
    return bundle


def write_review_bundle(bundle: ReviewBundle, path: str | Path) -> Path:
    """Atomically write a bundle through a unique same-directory staging file."""

    _validate_bundle_for_write(bundle)
    requested = Path(path).expanduser().absolute()
    requested.parent.mkdir(parents=True, exist_ok=True)
    destination = requested.parent.resolve() / requested.name
    if destination.is_symlink():
        raise ReviewError([ReviewFinding("", "review-bundle-unsafe", f"refusing to replace symlink: {destination}")])
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(bundle.to_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def write_review_packets(bundle: ReviewBundle, directory: str | Path) -> list[Path]:
    """Atomically publish opaque packet files and a coordinator-only manifest.

    Article and declaration names belong in the manifest, not in the filename
    handed to a blind reader. Packet hashes are opaque identifiers and let
    identical packets share one immutable file.
    """

    _validate_bundle_for_write(bundle)
    requested = Path(directory).expanduser()
    if requested.is_symlink():
        raise ReviewError(
            [ReviewFinding("", "review-packets-unsafe", f"refusing symlink packet output: {requested}")]
        )
    root = Path(os.path.abspath(requested))
    try:
        identity = validate_managed_output(
            root,
            kind="packets",
            schema=REVIEW_PACKET_SCHEMA,
        )
        stage = stage_managed_output(root)
    except Exception as exc:
        issues = getattr(exc, "issues", (str(exc),))
        raise ReviewError(
            [ReviewFinding("", "review-packets-unsafe", str(issue)) for issue in issues]
        ) from exc

    entries: list[dict[str, str]] = []
    packet_bytes: dict[str, bytes] = {}
    try:
        blind = stage / "blind"
        blind.mkdir()
        for article in bundle.articles:
            for declaration in article.declarations:
                digest = declaration.packet_hash.removeprefix(_HASH_PREFIX)
                relative = Path("blind") / f"{digest}.lean"
                content = declaration.packet.encode("utf-8")
                previous = packet_bytes.get(relative.as_posix())
                if previous is None:
                    (stage / relative).write_bytes(content)
                    packet_bytes[relative.as_posix()] = content
                elif previous != content:
                    raise ReviewError(
                        [
                            ReviewFinding(
                                article.node_id,
                                "review-packet-collision",
                                "two different packets have the same content hash",
                            )
                        ]
                    )
                entries.append(
                    {
                        "article_id": article.article_id,
                        "declaration": declaration.name,
                        "node_id": article.node_id,
                        "packet": relative.as_posix(),
                        "packet_hash": declaration.packet_hash,
                        "skeleton_hash": declaration.skeleton_hash,
                    }
                )
        manifest = {
            "bundle_hash": bundle.hash,
            "kind": "packets",
            "packets": entries,
            "schema": REVIEW_PACKET_SCHEMA,
        }
        (stage / PACKET_MANIFEST).write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        replace_managed_outputs([(root, stage, identity)])
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return [root / relative for relative in sorted(packet_bytes)]


def _report_findings(graph: Graph, skeleton: SkeletonReport) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for node in sorted(graph.nodes.values(), key=lambda item: item.id):
        if node.lean is not None and node.article_id is None:
            findings.append(
                ReviewFinding(
                    node.id,
                    "review-article-id-missing",
                    "Lean-mapped article has no durable article_id; run `autoform migrate` before preparing review evidence",
                )
            )
    if skeleton.schema != SKELETON_SCHEMA or skeleton.semantic_schema != SEMANTIC_SCHEMA:
        findings.append(
            ReviewFinding(
                "",
                "review-report-schema",
                "skeleton report uses an unsupported report or semantic schema",
            )
        )
    report_ids = [node.node_id for node in skeleton.nodes]
    if len(report_ids) != len(set(report_ids)):
        findings.append(
            ReviewFinding(
                "",
                "review-report-duplicate",
                "skeleton report contains duplicate article ids",
            )
        )
    for issue in skeleton.unresolved:
        node_id = issue.partition(":")[0] if ":" in issue else ""
        findings.append(
            ReviewFinding(node_id, "review-report-unresolved", f"skeleton extraction is incomplete: {issue}")
        )
    expected = _declaration_mapping(graph)
    actual = {node.node_id: tuple(item.name for item in node.declarations) for node in skeleton.nodes}
    for node_id in sorted(set(expected) - set(actual)):
        findings.append(
            ReviewFinding(node_id, "review-report-missing", "skeleton report omits this Lean-mapped article")
        )
    for node_id in sorted(set(actual) - set(expected)):
        findings.append(
            ReviewFinding(node_id, "review-report-extra", "skeleton report contains an article not mapped to Lean")
        )
    for node_id in sorted(set(expected) & set(actual)):
        node = graph.nodes[node_id]
        record = skeleton.node(node_id)
        assert record is not None
        if not expected[node_id] or len(set(expected[node_id])) != len(expected[node_id]):
            findings.append(
                ReviewFinding(node_id, "review-mapping-invalid", "lean metadata must name distinct declarations")
            )
        elif actual[node_id] != expected[node_id]:
            findings.append(
                ReviewFinding(
                    node_id,
                    "review-mapping-drift",
                    f"skeleton declarations {actual[node_id]!r} do not match current lean metadata {expected[node_id]!r}",
                )
            )
        expected_path = _article_path(node, graph)
        if record.article_path != expected_path:
            findings.append(
                ReviewFinding(
                    node_id,
                    "review-path-drift",
                    f"skeleton article path {record.article_path!r} does not match {expected_path!r}",
                )
            )
        current_passage, current_locator = source_passage(node, graph.blueprint_dir)
        if node.origin == "cited" and (current_passage is None or not current_passage.strip()):
            findings.append(
                ReviewFinding(
                    node_id,
                    "review-source-passage-missing",
                    "cited Lean article has no exact local source passage; cite a non-Markdown source with #L<start>-L<end>",
                )
            )
        if (record.passage, record.passage_locator) != (current_passage, current_locator):
            findings.append(
                ReviewFinding(
                    node_id,
                    "review-source-drift",
                    "skeleton source passage does not match the article's current source locator",
                )
            )
    return sorted(findings)


def _declaration_mapping(graph: Graph) -> dict[str, tuple[str, ...]]:
    return {
        node_id: tuple(declaration_names(node.lean or ""))
        for node_id, node in graph.nodes.items()
        if node.lean is not None
    }


def _review_declaration(declaration: DeclarationSkeleton) -> ReviewDeclaration:
    return ReviewDeclaration(
        name=declaration.name,
        skeleton_hash=declaration.hash,
        packet_hash=declaration.evidence_hash,
        packet=declaration.blind_text(),
    )


def _article_readback_findings(
    article: ReviewArticle,
    readbacks: Mapping[tuple[str, str], Readback],
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for declaration in article.declarations:
        card = readbacks.get((article.article_id, declaration.name))
        if card is None:
            findings.append(
                ReviewFinding(
                    article.node_id,
                    "readback-missing",
                    f"no read-back filed for {declaration.name}",
                )
            )
            continue
        errors: list[str] = []
        errors.extend(card.validate())
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
            findings.append(
                ReviewFinding(
                    article.node_id,
                    "readback-invalid",
                    f"read-back for {declaration.name} is invalid: " + "; ".join(dict.fromkeys(errors)),
                )
            )
    return findings


def _article_from_dict(item: object) -> ReviewArticle:
    fields = {
        "article_id",
        "article_path",
        "declarations",
        "evidence_hash",
        "node_id",
        "packet",
        "passage",
        "passage_locator",
        "schema",
        "statement",
        "title",
    }
    if not isinstance(item, dict) or set(item) != fields or item.get("schema") != REVIEW_ARTICLE_SCHEMA:
        raise _invalid_bundle("review bundle contains a malformed article")
    node_id = _required_string(item.get("node_id"), "article id")
    article_id = _required_string(item.get("article_id"), f"stable id for {node_id}")
    if ARTICLE_ID_PATTERN.fullmatch(article_id) is None:
        raise _invalid_bundle(f"malformed stable article id for {node_id}")
    article_path = _required_string(item.get("article_path"), f"article path for {node_id}")
    statement = _required_string(item.get("statement"), f"statement for {node_id}")
    title = _required_string(item.get("title"), f"title for {node_id}")
    passage = _optional_string(item.get("passage"), f"source passage for {node_id}")
    locator = _optional_string(item.get("passage_locator"), f"source locator for {node_id}")
    if (passage is None) != (locator is None):
        raise _invalid_bundle(f"source passage and locator are inconsistent for {node_id}")
    packet = _required_string(item.get("packet"), f"joint packet for {node_id}")
    raw_declarations = item.get("declarations")
    if not isinstance(raw_declarations, list) or not raw_declarations:
        raise _invalid_bundle(f"declarations are missing for {node_id}")
    declarations = tuple(_declaration_from_dict(value, node_id) for value in raw_declarations)
    if len({value.name for value in declarations}) != len(declarations):
        raise _invalid_bundle(f"duplicate declarations for {node_id}")
    if packet != _joint_packet(declarations):
        raise _invalid_bundle(f"joint packet does not match declaration packets for {node_id}")
    article = ReviewArticle(
        article_id=article_id,
        node_id=node_id,
        article_path=article_path,
        title=title,
        statement=statement,
        passage=passage,
        passage_locator=locator,
        packet=packet,
        declarations=declarations,
    )
    if item.get("evidence_hash") != article.evidence_hash:
        raise _invalid_bundle(f"evidence hash does not match article {node_id}")
    return article


def _declaration_from_dict(item: object, node_id: str) -> ReviewDeclaration:
    fields = {"name", "packet", "packet_hash", "skeleton_hash"}
    if not isinstance(item, dict) or set(item) != fields:
        raise _invalid_bundle(f"malformed declaration in article {node_id}")
    declaration = ReviewDeclaration(
        name=_required_string(item.get("name"), f"declaration name for {node_id}"),
        skeleton_hash=_required_hash(item.get("skeleton_hash"), f"skeleton hash for {node_id}"),
        packet_hash=_required_hash(item.get("packet_hash"), f"packet hash for {node_id}"),
        packet=_required_string(item.get("packet"), f"declaration packet for {node_id}"),
    )
    if declaration.packet_hash != _hash_bytes(declaration.packet.encode("utf-8")):
        raise _invalid_bundle(f"packet hash does not match declaration {declaration.name}")
    return declaration


def _joint_packet(declarations: tuple[ReviewDeclaration, ...]) -> str:
    parts = [f"-- article with {len(declarations)} declaration(s)"]
    parts.extend(item.packet for item in declarations)
    return "\n".join(parts)


def _article_path(node: Node, graph: Graph) -> str:
    try:
        return node.path.relative_to(graph.blueprint_dir).as_posix()
    except ValueError:
        return node.path.name


def _approval_identity_findings(graph: Graph, bundle: ReviewBundle) -> list[ReviewFinding]:
    """Reject approvals that have no current Lean-backed bundle article."""

    bundled = {article.article_id for article in bundle.articles}
    findings: list[ReviewFinding] = []
    for node_id, node in sorted(graph.nodes.items()):
        if node.review_approved is None:
            continue
        if node.lean is None:
            reason = "review_approved is present on an article that is not mapped to Lean"
        elif node.article_id is None or node.article_id not in bundled:
            reason = "review_approved is present but its article_id is absent from the review bundle"
        else:
            continue
        findings.append(ReviewFinding(node_id, "review-approval-orphaned", reason))
    return findings


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid_bundle(f"missing or malformed {label}")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _invalid_bundle(f"malformed {label}")
    return value


def _required_hash(value: object, label: str) -> str:
    value = _required_string(value, label)
    if _HASH.fullmatch(value) is None:
        raise _invalid_bundle(f"malformed {label}")
    return value


def _hash_bytes(material: bytes) -> str:
    return _HASH_PREFIX + hashlib.sha256(material).hexdigest()


def _hash_json(material: object) -> str:
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _hash_bytes(encoded)


def _invalid_bundle(reason: str) -> ReviewError:
    return ReviewError([ReviewFinding("", "review-bundle-invalid", reason)])


def _validate_bundle_for_write(bundle: ReviewBundle) -> None:
    """Reject hand-constructed bundles before any public writer derives paths."""

    try:
        if bundle.schema != REVIEW_BUNDLE_SCHEMA or not isinstance(bundle.articles, tuple):
            raise _invalid_bundle("review bundle has a malformed schema or article collection")
        articles = tuple(_article_from_dict(article.as_dict()) for article in bundle.articles)
    except ReviewError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise _invalid_bundle(f"review bundle cannot be serialized safely: {exc}") from exc
    if articles != bundle.articles:
        raise _invalid_bundle("review bundle does not match its validated representation")
    if len({article.article_id for article in articles}) != len(articles):
        raise _invalid_bundle("review bundle contains duplicate article ids")
    if tuple(sorted(articles, key=lambda article: article.article_id)) != articles:
        raise _invalid_bundle("review bundle articles are not in canonical article_id order")


@dataclass(frozen=True, slots=True)
class RecordRequest:
    """One read-back to file: which declaration, what the auditor read, what it wrote.

    ``review prepare`` writes one blind packet per declaration; an auditor
    reads one packet and writes a read-back. A request asks ``review record``
    to file that read-back as the declaration's card, and names only inputs:
    the card itself is built and checked later, against current Lean evidence.
    """

    #: Durable id of the article the card belongs to, from its frontmatter.
    article_id: str
    #: Full Lean name of the declaration the read-back is about.
    declaration: str
    #: The exact packet file the auditor read; its bytes must equal the
    #: bundle's packet for this declaration.
    packet: Path
    #: The auditor's read-back, Markdown.
    testimony: Path
    #: Needed only to replace a card that already exists with different
    #: content: the hash of that card. A first filing, or refiling identical
    #: content, needs none. Naming what is replaced keeps two reviewers from
    #: overwriting each other unseen.
    expected_card_hash: str | None = None


_RECORD_FIELDS = frozenset({"article_id", "declaration", "packet", "testimony"})
_OPTIONAL_RECORD_FIELDS = frozenset({"expected_card_hash"})


def load_record_manifest(path: str | Path) -> tuple[RecordRequest, ...]:
    """Strictly read a batch of records for ``review record --manifest``.

    Relative packet and testimony paths resolve against the manifest's own
    directory, so a coordinator can write it beside the testimony it lists.
    One declaration may appear once: two testimonies for it leave nothing to
    decide which is meant.
    """

    manifest = Path(path).expanduser()
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewError([ReviewFinding("manifest", "review-records-invalid", f"cannot read {manifest}: {exc}")]) from exc
    if not isinstance(payload, dict) or payload.keys() != {"records", "schema"}:
        raise ReviewError([ReviewFinding("manifest", "review-records-invalid", f"{manifest} is not a records manifest")])
    if payload["schema"] != REVIEW_RECORDS_SCHEMA:
        raise ReviewError(
            [ReviewFinding("manifest", "review-records-invalid", f"{manifest}: expected schema {REVIEW_RECORDS_SCHEMA}")]
        )
    items = payload["records"]
    if not isinstance(items, list) or not items:
        raise ReviewError([ReviewFinding("manifest", "review-records-invalid", f"{manifest}: records must be a non-empty list")])

    base = manifest.resolve().parent
    findings: list[ReviewFinding] = []
    requests: list[RecordRequest] = []
    seen: set[tuple[str, str]] = set()
    for position, item in enumerate(items, start=1):
        context = f"{manifest}: record {position}"
        if (
            not isinstance(item, dict)
            or not _RECORD_FIELDS <= item.keys() <= _RECORD_FIELDS | _OPTIONAL_RECORD_FIELDS
            or not all(isinstance(item[field], str) and item[field] for field in _RECORD_FIELDS)
        ):
            findings.append(
                ReviewFinding("manifest", "review-records-invalid", f"{context} needs exactly {', '.join(sorted(_RECORD_FIELDS))}")
            )
            continue
        expected = item.get("expected_card_hash")
        if expected is not None and (not isinstance(expected, str) or not _HASH.fullmatch(expected)):
            findings.append(ReviewFinding("manifest", "review-records-invalid", f"{context}: invalid expected_card_hash"))
            continue
        key = (item["article_id"], item["declaration"])
        if key in seen:
            findings.append(
                ReviewFinding(
                    item["article_id"],
                    "review-records-invalid",
                    f"{context}: {item['declaration']} is already recorded earlier in this manifest",
                )
            )
            continue
        seen.add(key)
        requests.append(
            RecordRequest(
                article_id=item["article_id"],
                declaration=item["declaration"],
                packet=base / item["packet"],
                testimony=base / item["testimony"],
                expected_card_hash=expected,
            )
        )
    if findings:
        raise ReviewError(findings)
    return tuple(requests)


__all__ = [
    "REVIEW_APPROVAL_SCHEMA",
    "REVIEW_BUNDLE_SCHEMA",
    "REVIEW_PACKET_SCHEMA",
    "REVIEW_RECORDS_SCHEMA",
    "RecordRequest",
    "ReviewArticle",
    "ReviewBundle",
    "ReviewDeclaration",
    "ReviewError",
    "ReviewFinding",
    "build_review_bundle",
    "canonical_statement",
    "load_record_manifest",
    "load_review_bundle",
    "review_findings",
    "validate_review_article",
    "validate_review_bundle",
    "write_review_bundle",
    "write_review_packets",
]
