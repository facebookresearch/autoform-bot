"""Command-line entry point for Autoform's project utilities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from . import status
from .article_identity import plan_article_ids
from .audit import audit_blueprint
from .claims import CLAIM_TTL_S, ClaimBoard, ClaimTransportError, author_claim_key
from .doctor import diagnose_project
from .graph import Graph, GraphValidationError, load_graph
from .lean import build_linker, declaration_names
from .readback import PreparedReadback, planned_readback, prepare_readback, publish_readback, readback_conflicts
from .render import PublicationError, render_site
from .review import (
    RecordRequest,
    ReviewBundle,
    ReviewDeclaration,
    ReviewError,
    ReviewFinding,
    build_review_bundle,
    load_record_manifest,
    load_review_bundle,
    review_findings,
    validate_review_article,
    validate_review_bundle,
    write_review_bundle,
    write_review_packets,
)
from .scaffold import ScaffoldError, scaffold_project
from .skeleton import (
    SkeletonError,
    SkeletonReport,
    extract_skeletons,
    format_report,
    write_packets,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autoform")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="write the blueprint vault, site config, and CI")
    init.add_argument("target", nargs="?", default=".", help="project root (default: current directory)")
    init.add_argument("--title", help="human project title (default: the directory name)")
    init.add_argument("--repository-url", default="", help="project URL, e.g. https://github.com/owner/repo")
    init.add_argument(
        "--autoform-source",
        default="",
        help="Autoform Git source the generated workflows install from (default: this checkout's origin)",
    )
    init.add_argument(
        "--autoform-ref",
        default="",
        help="immutable ref the workflows pin (default: this checkout's HEAD commit)",
    )
    init.add_argument("--force", action="store_true", help="overwrite files that already exist")
    init.add_argument("--json", action="store_true", help="write stable machine-readable output")

    check = subparsers.add_parser("check", help="validate a Markdown blueprint")
    check.add_argument("blueprint_dir")
    check.add_argument(
        "--lean-root",
        type=Path,
        help="Lean project to resolve 'lean:' declarations against (enables declaration checking)",
    )

    audit = subparsers.add_parser("audit", help="audit roadmap completeness and checked facts")
    audit.add_argument("blueprint_dir")
    audit.add_argument("--lean-root", type=Path, help="Lean project to resolve local targets against")
    audit.add_argument("--json", action="store_true", help="write stable machine-readable output")
    audit.add_argument(
        "--review-bundle",
        type=Path,
        help="prepared review evidence; re-extracted and checked against the current Lean project",
    )

    doctor = subparsers.add_parser("doctor", help="diagnose the local Markdown runtime contract")
    doctor.add_argument("project_or_blueprint")
    doctor.add_argument("--lean-root", type=Path, help="Lean project to resolve local targets against")
    doctor.add_argument("--json", action="store_true", help="write stable machine-readable output")

    claim = subparsers.add_parser("claim", help="coordinate temporary node ownership through Git refs")
    claim_subparsers = claim.add_subparsers(dest="claim_command", required=True)
    for operation in ("acquire", "renew", "release"):
        command = claim_subparsers.add_parser(operation)
        command.add_argument("node_id")
        _add_claim_board_arguments(command)
        if operation in {"acquire", "renew"}:
            command.add_argument("--ttl", type=int, default=CLAIM_TTL_S)
        if operation == "acquire":
            command.add_argument("--note", default="")
    claim_list = claim_subparsers.add_parser("list")
    _add_claim_board_arguments(claim_list)
    claim_cleanup = claim_subparsers.add_parser("cleanup")
    _add_claim_board_arguments(claim_cleanup)

    migrate = subparsers.add_parser("migrate", help="inspect authored migration contracts")
    migrate_subparsers = migrate.add_subparsers(dest="migrate_command", required=True)
    article_ids = migrate_subparsers.add_parser(
        "article-ids",
        help="plan durable roadmap article identifiers without writing files",
    )
    article_ids.add_argument("blueprint_dir")
    article_ids.add_argument(
        "--check",
        action="store_true",
        help="fail when an article is missing article_id frontmatter",
    )
    article_ids.add_argument("--json", action="store_true", help="write stable machine-readable output")

    skeleton = subparsers.add_parser(
        "skeleton",
        help="extract what a reader must trust for each formalized statement",
    )
    skeleton.add_argument("blueprint_dir")
    skeleton.add_argument(
        "--lean-root",
        type=Path,
        required=True,
        help="built Lean project whose declarations the blueprint names",
    )
    skeleton.add_argument(
        "--node",
        action="append",
        dest="nodes",
        metavar="ID",
        help="restrict to one article id (repeatable)",
    )
    skeleton.add_argument("--json", action="store_true", help="write stable machine-readable output")
    skeleton.add_argument(
        "-o",
        "--output",
        type=Path,
        help="write the JSON report to this file instead of standard output",
    )
    skeleton.add_argument(
        "--packets",
        type=Path,
        metavar="DIR",
        help="also write one comment-stripped packet per skeleton for blind read-back auditors",
    )
    skeleton.add_argument(
        "--passages",
        type=Path,
        metavar="DIR",
        help="with --packets: also write each article's cited source passage, for a faithfulness judge",
    )

    review = subparsers.add_parser("review", help="prepare and verify statement-review evidence")
    review_subparsers = review.add_subparsers(dest="review_command", required=True)
    review_prepare = review_subparsers.add_parser(
        "prepare",
        help="prepare a current-tree evidence bundle and optional blind packets",
    )
    review_prepare.add_argument("blueprint_dir")
    review_prepare.add_argument("--lean-root", type=Path, required=True)
    review_prepare.add_argument("-o", "--output", type=Path, required=True)
    review_prepare.add_argument("--packets", type=Path, metavar="DIR")

    review_record = review_subparsers.add_parser(
        "record",
        help="validate and file testimony about exact prepared packets, one or a batch",
    )
    review_record.add_argument("blueprint_dir")
    review_record.add_argument("--lean-root", type=Path, required=True)
    review_record.add_argument("--bundle", type=Path, required=True)
    review_record.add_argument(
        "--manifest",
        type=Path,
        help="file every record this manifest lists against one extraction, instead of the four flags below",
    )
    review_record.add_argument("--article-id", metavar="AF_ID")
    review_record.add_argument("--declaration", metavar="LEAN_NAME")
    review_record.add_argument("--packet", type=Path)
    review_record.add_argument("--testimony", type=Path)
    review_record.add_argument("--model", required=True)
    review_record.add_argument(
        "--expected-card-hash",
        help="replace an existing different card only if its current content has this hash",
    )

    review_check = review_subparsers.add_parser(
        "check",
        help="check evidence freshness, read-backs, and human approvals",
    )
    review_check.add_argument("blueprint_dir")
    review_check.add_argument("--lean-root", type=Path, required=True)
    review_check.add_argument(
        "--bundle",
        type=Path,
        help="the bundle `review prepare` wrote; without it, one is derived from this run's own extraction",
    )
    review_check.add_argument("--json", action="store_true", help="write stable machine-readable output")

    render = subparsers.add_parser("render", help="build the publishable blueprint")
    render.add_argument("blueprint_dir")
    render.add_argument("-o", "--output", default="site-src", help="output directory")
    render.add_argument("--lean-root", type=Path, help="Lean project to link code from")
    render.add_argument("--repository-url", help="project URL, e.g. https://github.com/owner/repo")
    render.add_argument("--ref", help="commit or branch the code links should pin")
    render.add_argument(
        "--require-declarations",
        action="store_true",
        help="fail when a 'lean:' declaration is not found in the Lean sources",
    )
    render_review = render.add_mutually_exclusive_group()
    render_review.add_argument(
        "--review-bundle",
        type=Path,
        help="prepared current-tree review evidence; adds validated review disclosures",
    )
    render_review.add_argument(
        "--review",
        action="store_true",
        help="add review disclosures from evidence derived in this run, without a prepared bundle",
    )

    args = parser.parse_args(argv)

    if args.command == "init":
        return _init(args)
    if args.command == "check":
        return _check(args)
    if args.command == "audit":
        return _audit(args)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "claim":
        return _claim(args)
    if args.command == "migrate":
        return _migrate(args)
    if args.command == "skeleton":
        return _skeleton(args)
    if args.command == "review":
        return _review(args)
    if args.command == "render":
        return _render(args)
    return 2


def _add_claim_board_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="claim-board Git repository; defaults to this checkout's origin")
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("AUTOFORM_WORKER_ID"),
        help="stable identity for this agent (or set AUTOFORM_WORKER_ID)",
    )
    parser.add_argument("--scratch", type=Path, help="local bare Git object cache")


def _init(args: argparse.Namespace) -> int:
    target = Path(args.target).expanduser()
    title = args.title or target.resolve().name
    try:
        result = scaffold_project(
            target,
            title=title,
            repository_url=args.repository_url,
            autoform_source=args.autoform_source,
            autoform_ref=args.autoform_ref,
            force=args.force,
        )
    except ScaffoldError as error:
        for issue in error.issues:
            print(f"error: {issue}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
        return 0

    print(f"{target}: {len(result.written)} files written")
    for path in result.written:
        print(f"  + {path}")
    for path in result.skipped:
        note = "no Autoform ref to pin" if result.unpinned and ".github" in path else "exists, left alone"
        print(f"  = {path} ({note})")
    print("Next: describe the project in blueprint/README.md, then add chapters "
          "as roadmap/<chapter>/README.md.")
    if result.unpinned:
        # Flush first: stdout is block-buffered when piped, so without this the
        # warning jumps ahead of the file list it is explaining.
        sys.stdout.flush()
        print(
            "\nCI was not written: generated workflows install Autoform from a Git\n"
            "ref, and this Autoform is not running from a checkout, so there is\n"
            "nothing to pin. Re-run with the commit to add them:\n"
            "  autoform init --autoform-ref <40-char-sha>",
            file=sys.stderr,
        )
    return 0


def _check(args: argparse.Namespace) -> int:
    try:
        graph = load_graph(args.blueprint_dir)
    except GraphValidationError as exc:
        for issue in exc.issues:
            print(f"error: {issue}")
        return 1

    statuses = status.derive(graph)
    summary = " · ".join(f"{count} {state.label}" for state, count in status.summarize(statuses))
    print(f"OK: {len(graph.nodes)} articles, {graph.edge_count} dependencies")
    if summary:
        print(f"    {summary}")

    if args.lean_root is None:
        return 0

    linker = build_linker(args.lean_root)
    missing = [
        f"{node.id}: declaration not found in {args.lean_root}: {name}"
        for node in graph.nodes.values()
        for name in declaration_names(node.lean or "")
        if linker.location(name) is None
    ]
    for issue in missing:
        print(f"error: {issue}")
    if missing:
        return 1
    declared = sum(1 for node in graph.nodes.values() if node.lean)
    print(f"    {declared} declaration(s) resolved in the Lean sources")
    return 0


def _audit(args: argparse.Namespace) -> int:
    skeleton = None
    bundle = None
    if args.review_bundle is not None:
        if args.lean_root is None:
            print("error: --review-bundle requires --lean-root", file=sys.stderr)
            return 2
        try:
            _, skeleton, bundle = _current_review(
                args.blueprint_dir,
                lean_root=args.lean_root,
                bundle_path=args.review_bundle,
            )
        except (GraphValidationError, ReviewError, SkeletonError) as exc:
            for issue in exc.issues:
                print(f"error: {issue}", file=sys.stderr)
            return 2
    result = audit_blueprint(
        args.blueprint_dir,
        lean_root=args.lean_root,
        skeleton=skeleton,
        review_bundle=bundle,
    )
    if args.json:
        print(result.to_json())
    else:
        if result.clean:
            print("OK: roadmap audit passed")
        if result.coverage is not None:
            counts = result.coverage.counts
            print(
                "    coverage: "
                f"{counts['MAPPED']} mapped · "
                f"{counts['DECOMPOSED']} decomposed · "
                f"{counts['DEFERRED']} deferred · "
                f"{counts['OUT']} out"
            )
        for finding in result.findings:
            print(f"error: {finding.article_path}: {finding.code}: {finding.reason}")
    return 0 if result.clean else 1


def _doctor(args: argparse.Namespace) -> int:
    result = diagnose_project(args.project_or_blueprint, lean_root=args.lean_root)
    if args.json:
        print(result.to_json())
    else:
        for check in result.checks:
            marker = "PASS" if check.ok else "FAIL"
            print(f"{marker}: {check.name}: {check.detail}")
    return 0 if result.clean else 1


def _claim(args: argparse.Namespace) -> int:
    try:
        board = _claim_board(args)
        operation = args.claim_command
        if operation == "list":
            print(json.dumps(board.list(), sort_keys=True, separators=(",", ":")))
            return 0
        if operation == "cleanup":
            print(f"removed {board.cleanup()} expired claim(s)")
            return 0

        key = author_claim_key(args.node_id)
        if operation == "acquire":
            succeeded = board.acquire(key, ttl=args.ttl, note=args.note)
        elif operation == "renew":
            succeeded = board.renew(key, ttl=args.ttl)
        else:
            succeeded = board.release(key)
        if succeeded:
            past_tense = {"acquire": "acquired", "renew": "renewed", "release": "released"}
            print(f"{past_tense[operation]} {args.node_id} ({key})")
            return 0
        print(f"error: could not {operation} {args.node_id}; ownership is held or unverifiable")
        return 1
    except (ClaimTransportError, ValueError) as exc:
        print(f"error: {exc}")
        return 1


def _migrate(args: argparse.Namespace) -> int:
    if args.migrate_command != "article-ids":
        return 2
    try:
        plan = plan_article_ids(args.blueprint_dir)
    except GraphValidationError as error:
        for issue in error.issues:
            print(f"error: {issue}", file=sys.stderr)
        return 2

    if args.json:
        print(plan.to_json())
    elif plan.complete:
        print(f"OK: {len(plan.entries)} articles have durable article_id metadata")
    else:
        print(f"{plan.missing_count} article(s) need article_id metadata")
        for entry in plan.entries:
            if not entry.assigned:
                print(f"  {entry.article_path}: {entry.article_id}")
    return 1 if args.check and not plan.complete else 0


def _skeleton(args: argparse.Namespace) -> int:
    if args.passages is not None and args.packets is None:
        print("error: --passages requires --packets", file=sys.stderr)
        return 2
    if args.output is not None and args.packets is not None:
        output = args.output.expanduser().resolve()
        packet_outputs = [args.packets.expanduser().resolve()]
        if args.passages is not None:
            packet_outputs.append(args.passages.expanduser().resolve())
        if any(
            output == directory or output in directory.parents or directory in output.parents
            for directory in packet_outputs
        ):
            print("error: --output must be disjoint from packet and passage directories", file=sys.stderr)
            return 2
    try:
        report = extract_skeletons(
            args.blueprint_dir,
            lean_root=args.lean_root,
            node_ids=tuple(args.nodes) if args.nodes else None,
        )
    except SkeletonError as exc:
        for issue in exc.issues:
            print(f"error: {issue}", file=sys.stderr)
        return 2

    if args.packets is not None:
        try:
            written = write_packets(report, args.packets, passages=args.passages)
        except SkeletonError as exc:
            for issue in exc.issues:
                print(f"error: {issue}", file=sys.stderr)
            return 2
        stream = sys.stderr if args.json else sys.stdout
        print(f"{args.packets}: {len(written)} blind packet(s) written", file=stream)
        if args.passages is not None:
            cited = sum(1 for node in report.nodes if node.passage is not None)
            print(f"{args.passages}: {cited} source passage(s) written", file=stream)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report.to_json() + "\n", encoding="utf-8")
        declarations = sum(len(node.declarations) for node in report.nodes)
        stream = sys.stderr if args.json else sys.stdout
        print(
            f"{args.output}: {declarations} skeleton(s) for {len(report.nodes)} article(s)",
            file=stream,
        )
        for issue in report.unresolved:
            print(f"error: {issue}", file=stream)
    elif args.json:
        print(report.to_json())
    else:
        print(format_report(report, lean_root=args.lean_root), end="")
    return 0 if report.clean else 1


def _review(args: argparse.Namespace) -> int:
    if args.review_command == "prepare":
        return _review_prepare(args)
    if args.review_command == "record":
        return _review_record(args)
    if args.review_command == "check":
        return _review_check(args)
    return 2


def _review_prepare(args: argparse.Namespace) -> int:
    output = args.output.expanduser().resolve()
    if args.packets is not None:
        packets = args.packets.expanduser().resolve()
        if output == packets or output in packets.parents or packets in output.parents:
            print("error: --output and --packets must be disjoint", file=sys.stderr)
            return 2
    try:
        graph = load_graph(args.blueprint_dir)
        skeleton = extract_skeletons(args.blueprint_dir, lean_root=args.lean_root)
        bundle = build_review_bundle(graph, skeleton)
        if args.packets is not None:
            written = write_review_packets(bundle, args.packets)
            print(f"{args.packets}: {len(written)} blind packet(s) written")
        destination = write_review_bundle(bundle, args.output)
    except (GraphValidationError, ReviewError, SkeletonError) as exc:
        for issue in exc.issues:
            print(f"error: {issue}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{destination}: prepared {len(bundle.articles)} review article(s) · {bundle.hash}")
    return 0


def _review_record(args: argparse.Namespace) -> int:
    single = (args.article_id, args.declaration, args.packet, args.testimony)
    if args.manifest is not None:
        if any(value is not None for value in single) or args.expected_card_hash is not None:
            print(
                "error: --manifest replaces --article-id, --declaration, --packet, --testimony, "
                "and --expected-card-hash",
                file=sys.stderr,
            )
            return 2
    elif any(value is None for value in single):
        print(
            "error: record needs --manifest, or all of --article-id, --declaration, --packet, and --testimony",
            file=sys.stderr,
        )
        return 2

    written: list[tuple[Path, str]] = []
    requests: tuple[RecordRequest, ...] = ()
    try:
        requests = (
            load_record_manifest(args.manifest)
            if args.manifest is not None
            else (
                RecordRequest(
                    article_id=args.article_id,
                    declaration=args.declaration,
                    packet=args.packet,
                    testimony=args.testimony,
                    expected_card_hash=args.expected_card_hash,
                ),
            )
        )
        bundle = load_review_bundle(args.bundle)
        # Every input is read, and checked against the prepared bundle, before
        # any Lean work: a missing file or a changed packet stops the batch
        # without an extraction.
        inputs = _record_inputs(bundle, requests)
        # So is every card that would replace different content without naming
        # it: all of them at once, rather than one per extraction.
        _refuse_conflicts(_planned_records(args.blueprint_dir, inputs, model=args.model))
        graph = load_graph(args.blueprint_dir)
        before = _record_snapshot(graph, requests)
        # One extraction serves every record in the batch.
        skeleton = extract_skeletons(
            args.blueprint_dir,
            lean_root=args.lean_root,
            node_ids=tuple(sorted({node_id for _, node_id, _, _ in before})),
        )
        # The extraction must describe the blueprint the cards are filed
        # against. Reload it and refuse if any selected article changed while
        # Lean ran; otherwise the evidence below would pair a graph and a Lean
        # state that never coexisted.
        graph = load_graph(args.blueprint_dir)
        if _record_snapshot(graph, requests) != before:
            raise ReviewError(
                [
                    ReviewFinding(
                        "record",
                        "review-snapshot-changed",
                        "an article being recorded changed during extraction; nothing was filed, so rerun the record",
                    )
                ]
            )
        findings = [
            finding
            for article_id, node_id, _, _ in before
            for finding in validate_review_article(graph, bundle, _article_report(skeleton, node_id), article_id)
        ]
        if findings:
            raise ReviewError(findings)
        cards = _prepare_records(graph, skeleton, inputs, model=args.model)
        _refuse_conflicts(cards)
        # Every card has passed every check. Publish them in order; each keeps
        # its own compare-and-swap, and filing identical content is a no-op, so
        # a batch interrupted here is completed by running it again.
        for card in cards:
            written.append((publish_readback(card), card.declaration))
    except (GraphValidationError, ReviewError, SkeletonError) as exc:
        _report_recorded(written, len(requests))
        for issue in exc.issues:
            print(f"error: {issue}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError) as exc:
        _report_recorded(written, len(requests))
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _report_recorded(written, len(requests))
    return 0


def _article_report(report: SkeletonReport, node_id: str) -> SkeletonReport:
    """The part of a shared extraction that one article's validation may see.

    ``validate_review_article`` insists on a report scoped to exactly its own
    article, so that missing evidence cannot pass. A batch extracts several
    articles at once and hands each its own node. An unresolved issue that
    cannot be attributed to a node stays with every article, and fails them all.
    """

    prefixes = tuple(f"{node.node_id}: " for node in report.nodes)
    return replace(
        report,
        nodes=tuple(node for node in report.nodes if node.node_id == node_id),
        unresolved=tuple(
            issue
            for issue in report.unresolved
            if issue.startswith(f"{node_id}: ") or not issue.startswith(prefixes)
        ),
    )


@dataclass(frozen=True, slots=True)
class _RecordInput:
    """One request, the bundle entry it names, and the two texts it points to."""

    request: RecordRequest
    #: The declaration as ``review prepare`` recorded it: packet and hashes.
    prepared: ReviewDeclaration
    #: The packet the auditor read; equal to ``prepared.packet``.
    packet: str
    #: What the auditor wrote about it.
    testimony: str


def _record_inputs(bundle: ReviewBundle, requests: tuple[RecordRequest, ...]) -> list[_RecordInput]:
    """Read each packet and testimony, and check the packet against the bundle.

    Every request is looked at before anything is refused, so one run names
    every unknown declaration, unreadable file, and changed packet at once.
    """

    findings: list[ReviewFinding] = []
    inputs: list[_RecordInput] = []
    for request in requests:
        prepared = bundle.declaration(request.article_id, request.declaration)
        if prepared is None:
            findings.append(_review_selection_finding(request.article_id, request.declaration))
            continue
        try:
            packet_bytes = request.packet.read_bytes()
        except OSError as exc:
            findings.append(_unreadable_input(request, "packet", request.packet, exc))
            continue
        if packet_bytes != prepared.packet.encode("utf-8"):
            findings.append(
                ReviewFinding(
                    request.article_id,
                    "review-packet-mismatch",
                    f"packet bytes do not match the prepared declaration {request.declaration}",
                )
            )
            continue
        try:
            testimony = request.testimony.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            findings.append(_unreadable_input(request, "testimony", request.testimony, exc))
            continue
        # Equal to the bundle's text, so already valid UTF-8.
        inputs.append(_RecordInput(request, prepared, packet_bytes.decode("utf-8"), testimony))
    if findings:
        raise ReviewError(findings)
    return inputs


def _unreadable_input(request: RecordRequest, role: str, path: Path, exc: Exception) -> ReviewFinding:
    return ReviewFinding(
        request.article_id,
        "review-input-unreadable",
        f"{request.declaration}: cannot read the {role} {path}: {exc}",
    )


def _planned_records(
    blueprint: str | Path,
    inputs: list[_RecordInput],
    *,
    model: str,
) -> list[PreparedReadback]:
    """The cards the batch would file if the prepared evidence is current."""

    return [
        planned_readback(
            blueprint,
            article_id=item.request.article_id,
            declaration=item.request.declaration,
            skeleton_hash=item.prepared.skeleton_hash,
            packet_hash=item.prepared.packet_hash,
            model=model,
            text=item.testimony,
            packet_text=item.packet,
            expected_card_hash=item.request.expected_card_hash,
        )
        for item in inputs
    ]


def _refuse_conflicts(cards: list[PreparedReadback]) -> None:
    conflicts = readback_conflicts(cards)
    if conflicts:
        raise ReviewError([ReviewFinding("record", "review-card-conflict", conflict) for conflict in conflicts])


def _record_snapshot(graph: Graph, requests: tuple[RecordRequest, ...]) -> tuple[tuple[str, str, str, str], ...]:
    """What each selected article is right now: its node, file, and source hash."""

    state: dict[str, tuple[str, str, str, str]] = {}
    for request in requests:
        if request.article_id in state:
            continue
        matches = [node for node in graph.nodes.values() if node.article_id == request.article_id]
        if len(matches) != 1:
            raise ReviewError([_review_selection_finding(request.article_id, request.declaration)])
        node = matches[0]
        state[request.article_id] = (request.article_id, node.id, str(node.path), node.source_sha256 or "")
    return tuple(sorted(state.values()))


def _prepare_records(
    graph: Graph,
    skeleton: SkeletonReport,
    inputs: list[_RecordInput],
    *,
    model: str,
) -> list[PreparedReadback]:
    """Build every card against the one extraction, or refuse the batch."""

    findings: list[ReviewFinding] = []
    cards: list[PreparedReadback] = []
    for item in inputs:
        request = item.request
        node = next(node for node in graph.nodes.values() if node.article_id == request.article_id)
        current_node = skeleton.node(node.id)
        current = None if current_node is None else next(
            (item for item in current_node.declarations if item.name == request.declaration),
            None,
        )
        if current is None:
            # Keep selection failures under the same structured error surface
            # as stale or malformed review evidence.
            findings.append(_review_selection_finding(request.article_id, request.declaration))
            continue
        try:
            cards.append(
                prepare_readback(
                    graph.blueprint_dir,
                    article_id=request.article_id,
                    declaration=current,
                    model=model,
                    text=item.testimony,
                    packet_text=item.packet,
                    expected_card_hash=request.expected_card_hash,
                )
            )
        except ValueError as exc:
            findings.append(ReviewFinding(request.article_id, "review-record-invalid", f"{request.declaration}: {exc}"))
    if findings:
        raise ReviewError(findings)
    return cards


def _report_recorded(written: list[tuple[Path, str]], total: int) -> None:
    for path, declaration in written:
        print(f"{path}: recorded read-back for {declaration}")
    if written and len(written) < total:
        print(
            f"error: {len(written)} of {total} read-back(s) were filed before the failure below; "
            "running the same record again files the rest and leaves these as they are",
            file=sys.stderr,
        )


def _review_check(args: argparse.Namespace) -> int:
    try:
        graph, skeleton, bundle = _current_review(
            args.blueprint_dir,
            lean_root=args.lean_root,
            bundle_path=args.bundle,
        )
        findings = review_findings(graph, bundle, skeleton)
    except (GraphValidationError, ReviewError, SkeletonError) as exc:
        for issue in exc.issues:
            print(f"error: {issue}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                {
                    "bundle": bundle.hash,
                    "clean": not findings,
                    "findings": [
                        {"code": item.code, "node_id": item.node_id, "reason": item.reason}
                        for item in findings
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    elif findings:
        for finding in findings:
            print(f"error: {finding.node_id}: {finding.code}: {finding.reason}")
    else:
        print(f"OK: statement reviews match {bundle.hash}")
    return 1 if findings else 0


def _current_review(
    blueprint_dir: str | Path,
    *,
    lean_root: Path,
    bundle_path: Path | None,
):
    """The graph, one extraction, and a review bundle validated against both.

    With ``bundle_path`` the bundle is the one ``review prepare`` wrote, and it
    must still describe the current tree. Without it, the bundle is derived
    from this same extraction. CI wants the latter: it never trusts a committed
    bundle, and preparing one in a separate command would pay for a second
    extraction of the same unchanged checkout.
    """

    graph = load_graph(blueprint_dir)
    skeleton = extract_skeletons(blueprint_dir, lean_root=lean_root)
    bundle = build_review_bundle(graph, skeleton) if bundle_path is None else load_review_bundle(bundle_path)
    findings = validate_review_bundle(graph, bundle, skeleton)
    if findings:
        raise ReviewError(findings)
    return graph, skeleton, bundle


def _review_selection_finding(article_id: str, declaration: str) -> ReviewFinding:
    return ReviewFinding(
        article_id,
        "review-selection-missing",
        f"prepared review bundle has no declaration {declaration!r} for article_id {article_id}",
    )


def _claim_board(args: argparse.Namespace) -> ClaimBoard:
    worker_id = args.worker_id
    if not worker_id:
        raise ValueError("--worker-id or AUTOFORM_WORKER_ID is required")
    repo = args.repo or _origin_url()
    scratch = args.scratch or _default_claim_scratch(repo, worker_id)
    return ClaimBoard(repo, worker_id, scratch)


def _origin_url() -> str:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError("--repo is required outside a Git checkout with an origin remote") from exc
    return result.stdout.strip()


def _default_claim_scratch(repo: str, worker_id: str) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    identity = hashlib.sha256(f"{repo}\0{worker_id}\0{socket.gethostname()}".encode()).hexdigest()[:24]
    return cache / "autoform" / "claims" / identity


def _render(args: argparse.Namespace) -> int:
    try:
        skeleton = None
        bundle = None
        if args.review_bundle is not None or args.review:
            if args.lean_root is None:
                flag = "--review" if args.review else "--review-bundle"
                print(f"error: {flag} requires --lean-root", file=sys.stderr)
                return 2
            _, skeleton, bundle = _current_review(
                args.blueprint_dir,
                lean_root=args.lean_root,
                bundle_path=args.review_bundle,
            )
        report = render_site(
            args.blueprint_dir,
            args.output,
            lean_root=args.lean_root,
            repository_url=args.repository_url,
            ref=args.ref,
            skeleton=skeleton,
            review_bundle=bundle,
        )
    except (GraphValidationError, PublicationError, ReviewError, SkeletonError) as exc:
        for issue in exc.issues:
            print(f"error: {issue}")
        return 1

    print(f"{report.output_dir}: {report.pages} pages, {report.nodes} nodes, {report.linked} code links")
    for issue in report.unresolved:
        print(f"warning: declaration not found in the Lean sources: {issue}")
    if report.unresolved and args.require_declarations:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
