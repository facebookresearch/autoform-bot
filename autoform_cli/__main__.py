"""Command-line entry point for Autoform's project utilities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from . import status
from .approvals import (
    ApprovalError,
    ApprovalStatus,
    GitHubReviewVerifier,
    approval_statuses,
    approvals_at,
    current_approvals,
)
from .article_identity import plan_article_ids
from .audit import audit_blueprint
from .claims import CLAIM_TTL_S, ClaimBoard, ClaimTransportError, author_claim_key
from .doctor import diagnose_project
from .dashboard import publication_bound_live_state, serve_dashboard
from .graph import Graph, GraphValidationError, load_graph
from .lean import build_linker, declaration_names
from .project import ProjectCatalogError, inspect_project, load_release_catalog
from .readback import (
    PreparedReadback,
    Readback,
    load_readbacks,
    planned_readback,
    prepare_readback,
    publish_readback,
    readback_conflicts,
)
from .render import PublicationError, publication_issues, render_site
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
from .runtime import RuntimeProjectionError, load_runtime_graph, resolve_runtime_paths
from .scaffold import ScaffoldError, scaffold_project
from .skeleton import (
    DEFAULT_PROBE_TIMEOUT,
    SkeletonError,
    SkeletonReport,
    blueprint_hash,
    extract_skeletons,
    format_report,
    load_skeleton_report,
    write_packets,
    write_skeleton_report,
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
    audit_review = audit.add_mutually_exclusive_group()
    audit_review.add_argument(
        "--review-bundle",
        type=Path,
        help="prepared review evidence; re-extracted and checked against the current Lean project",
    )
    audit_review.add_argument(
        "--review",
        action="store_true",
        help="check review approvals against evidence derived in this run, without a prepared bundle",
    )
    _add_probe_timeout_argument(audit)

    doctor = subparsers.add_parser("doctor", help="diagnose the local Markdown runtime contract")
    doctor.add_argument("project_or_blueprint")
    doctor.add_argument("--lean-root", type=Path, help="Lean project to resolve local targets against")
    doctor.add_argument("--json", action="store_true", help="write stable machine-readable output")

    dashboard = subparsers.add_parser(
        "dashboard",
        help="serve the built publication with a loopback-only live claim overlay",
    )
    dashboard.add_argument(
        "target",
        nargs="?",
        default=".",
        help="project root or blueprint directory (default: current directory)",
    )
    dashboard.add_argument("--site-dir", default="site", help="built MkDocs site directory")
    dashboard.add_argument("--repo", help="claim-board Git repository; defaults to project origin")
    dashboard.add_argument("--scratch", type=Path, help="local bare Git object cache")
    dashboard.add_argument("--host", default="127.0.0.1", help="loopback host")
    dashboard.add_argument(
        "--port",
        type=_port,
        default=0,
        help="local port (default: choose an available port)",
    )

    project = subparsers.add_parser("project", help="inspect local project configuration and releases")
    project_subparsers = project.add_subparsers(dest="project_command", required=True)
    project_inspect = project_subparsers.add_parser(
        "inspect", help="inspect a project without running Lake, Git, or network operations"
    )
    project_inspect.add_argument(
        "target", nargs="?", default=".", help="a path inside the project (default: current directory)"
    )
    project_inspect.add_argument("--json", action="store_true", help="write stable machine-readable output")
    project_versions = project_subparsers.add_parser(
        "versions", help="list bundled known-good Lean and Mathlib releases"
    )
    project_versions.add_argument("--json", action="store_true", help="write stable machine-readable output")
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
    _add_probe_timeout_argument(skeleton)

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
    _add_probe_timeout_argument(review_prepare)

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
    _add_probe_timeout_argument(review_record)

    review_check = review_subparsers.add_parser(
        "check",
        help="check evidence freshness, read-backs, and human approvals",
    )
    review_check.add_argument("blueprint_dir")
    review_evidence = review_check.add_mutually_exclusive_group(required=True)
    review_evidence.add_argument("--lean-root", type=Path, help="built Lean project to extract the skeletons from")
    review_evidence.add_argument(
        "--skeleton-report",
        type=Path,
        metavar="FILE",
        help="use the report `autoform skeleton --output` wrote for every article of this same blueprint, "
        "instead of running Lean; it is refused unless its blueprint hash is this checkout's",
    )
    review_check.add_argument(
        "--bundle",
        type=Path,
        help="the bundle `review prepare` wrote; without it, one is derived from this run's own extraction",
    )
    review_check.add_argument(
        "--authenticate",
        choices=["github"],
        help="also say who approved each current approval, from GitHub pull request reviews "
        "(needs GITHUB_TOKEN and GITHUB_REPOSITORY); without it every approval is self-approved",
    )
    review_check.add_argument("--json", action="store_true", help="write stable machine-readable output")
    _add_probe_timeout_argument(review_check)

    review_authenticate = review_subparsers.add_parser(
        "authenticate",
        help="report who approved each recorded review_approved, without Lean",
    )
    review_authenticate.add_argument("blueprint_dir")
    review_method = review_authenticate.add_mutually_exclusive_group(required=True)
    review_method.add_argument(
        "--github",
        action="store_true",
        help="accept approving pull request reviews by individual code owners "
        "(needs GITHUB_TOKEN and GITHUB_REPOSITORY)",
    )
    review_authenticate.add_argument(
        "--since",
        metavar="REF",
        help="fail when an approval added or changed since REF is not authenticated",
    )
    review_authenticate.add_argument(
        "--trusted-ref",
        metavar="REF",
        default="HEAD",
        help="commit whose CODEOWNERS decides who may approve (default HEAD)",
    )
    review_authenticate.add_argument(
        "--pr",
        type=int,
        metavar="N",
        help="pre-merge gate: only reviews of pull request N count, code owners come from --trusted-ref, "
        "and no Actions run is required yet; needs --since",
    )

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
    render.add_argument(
        "--skeleton-report",
        type=Path,
        metavar="FILE",
        help="with --review or --review-bundle, take the skeletons from this report, as review check does, "
        "instead of running Lean",
    )
    _add_probe_timeout_argument(render)
    render.add_argument(
        "--authenticate",
        choices=["github"],
        help="label approvals by who approved them, from GitHub pull request reviews "
        "(needs GITHUB_TOKEN and GITHUB_REPOSITORY); without it every approval is self-approved",
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
    if args.command == "dashboard":
        return _dashboard(args)
    if args.command == "project":
        return _project(args)
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


def _add_probe_timeout_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=_positive_seconds,
        metavar="SECONDS",
        help=f"seconds the probe helper build and each Lean probe may each run (default {DEFAULT_PROBE_TIMEOUT:g}); "
        "one probe runs per root module, several in parallel, and the budget is per probe, not a deadline "
        "for the whole extraction; the Lake freshness check before them has its own budget",
    )


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
        # Render refuses to publish on these same issues.
        markup = publication_issues(graph, Path(args.blueprint_dir), lean_root=args.lean_root)
    except GraphValidationError as exc:
        for issue in exc.issues:
            print(f"error: {issue}")
        return 1
    for issue in markup:
        print(f"error: {issue}")
    if markup:
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
    cards = None
    if args.review_bundle is not None or args.review:
        if args.lean_root is None:
            flag = "--review" if args.review else "--review-bundle"
            print(f"error: {flag} requires --lean-root", file=sys.stderr)
            return 2
        try:
            _, skeleton, bundle, cards = _current_review(
                args.blueprint_dir,
                lean_root=args.lean_root,
                bundle_path=args.review_bundle,
                timeout=args.timeout,
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
        readbacks=cards,
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


def _dashboard(args: argparse.Namespace) -> int:
    try:
        paths = resolve_runtime_paths(args.target)
        site = Path(args.site_dir).expanduser()
        if not site.is_absolute():
            site = paths.project_root / site
        repo = args.repo or _origin_url(paths.project_root)

        def run(scratch: Path) -> None:
            claims = ClaimBoard(repo, "dashboard-readonly", scratch)
            state = publication_bound_live_state(
                lambda: load_runtime_graph(paths.project_root),
                claims,
                blueprint_dir=paths.blueprint_dir,
                site_dir=site,
            )
            def ready(host: str, port: int) -> None:
                print(f"Dashboard: http://{host}:{port}/", flush=True)
                print(
                    "Static content comes from the built site; live claims remain local-only.",
                    flush=True,
                )

            serve_dashboard(site, state, host=args.host, port=args.port, on_ready=ready)

        if args.scratch is not None:
            run(args.scratch)
        else:
            with tempfile.TemporaryDirectory(prefix="autoform-dashboard-") as temporary:
                run(Path(temporary) / "claims.git")
    except (OSError, RuntimeProjectionError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


def _project(args: argparse.Namespace) -> int:
    try:
        catalog = load_release_catalog()
    except ProjectCatalogError as error:
        if args.json:
            print(json.dumps({"error": {"code": "project-catalog-invalid", "message": str(error)}, "ok": False}))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 1
    if args.project_command == "versions":
        if args.json:
            print(catalog.to_json())
            return 0
        print("Known-good Lean/Mathlib releases:")
        for release in catalog.releases:
            print(f"  {release.id}{' [recommended]' if release.recommended else ''}")
            print(f"    Lean: {release.lean_toolchain}")
            print(f"    Mathlib: {release.mathlib_rev} @ {release.mathlib_commit} ({release.mathlib_git})")
        return 0
    result = inspect_project(args.target, catalog=catalog)
    if args.json:
        print(result.to_json())
    else:
        _print_project_inspection(result)
    return 0 if result.ok else 1


def _print_project_inspection(result) -> None:
    if result.project_root is not None:
        print(f"Project root: {_human_text(result.project_root)}")
    if result.lake is not None:
        version = f" {result.lake.version}" if result.lake.version else ""
        print(f"Lake: {_human_text((result.lake.name or 'unknown package') + version)} ({result.lake.config})")
        for target in result.lake.targets:
            print(f"  {target.kind} {_human_text(target.name)}")
    if result.lean_toolchain is not None:
        print(f"Lean: {_human_text(result.lean_toolchain)}")
    if result.mathlib is not None:
        mathlib = result.mathlib
        where = mathlib.dir if mathlib.type == "path" else f"{mathlib.input_rev} @ {mathlib.rev} ({mathlib.url})"
        print(f"Mathlib: {_human_text(where)} [{mathlib.source}]")
    if result.autoform_paths:
        print(f"Autoform: {', '.join(result.autoform_paths)}")
    release = f" ({result.compatibility.release})" if result.compatibility.release else ""
    print(f"Compatibility: {result.compatibility.status}{release}")
    for diagnostic in result.diagnostics:
        location = f" {diagnostic.path}" if diagnostic.path else ""
        print(f"{diagnostic.severity}[{diagnostic.code}]{location}: {diagnostic.message}", file=sys.stderr)


def _human_text(value: object) -> str:
    """Escape nonprintable characters so project files cannot forge report lines."""

    return "".join(
        character if character.isprintable() else character.encode("unicode_escape").decode("ascii")
        for character in str(value)
    )


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


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0.0
    if not 0 < seconds < float("inf"):
        raise argparse.ArgumentTypeError(f"expected a positive number of seconds, got {value!r}")
    return seconds


def _skeleton(args: argparse.Namespace) -> int:
    if args.passages is not None and args.packets is None:
        print("error: --passages requires --packets", file=sys.stderr)
        return 2
    if args.output is not None and args.packets is not None:
        output = Path(os.path.abspath(args.output.expanduser()))
        trees = [path.expanduser().resolve() for path in (args.packets, args.passages) if path is not None]
        if any(output == tree or output in tree.parents or tree in output.parents for tree in trees):
            print("error: --output must be disjoint from packet and passage directories", file=sys.stderr)
            return 2
    stream = sys.stderr if args.json else sys.stdout
    try:
        report = extract_skeletons(
            args.blueprint_dir,
            lean_root=args.lean_root,
            timeout=args.timeout,
            node_ids=tuple(args.nodes) if args.nodes else None,
        )
        if args.packets is not None and not report.clean:
            print("error: refusing to publish review packets from an incomplete skeleton report", file=sys.stderr)
        elif args.packets is not None:
            written = write_packets(report, args.packets, passages=args.passages, report_path=args.output)
            print(f"{args.packets}: {len(written)} blind packet(s) written", file=stream)
            if args.passages is not None:
                cited = sum(1 for node in report.nodes if node.passage is not None)
                print(f"{args.passages}: {cited} source passage(s) written", file=stream)
        if args.output is not None and (args.packets is None or not report.clean):
            write_skeleton_report(report, args.output)
    except SkeletonError as exc:
        for issue in exc.issues:
            print(f"error: {issue}", file=sys.stderr)
        return 2
    if args.output is not None:
        declarations = sum(len(node.declarations) for node in report.nodes)
        print(
            f"{args.output}: {declarations} skeleton(s) for {len(report.nodes)} article(s)",
            file=stream,
        )
        for issue in report.unresolved:
            print(f"error: {issue.message}", file=stream)
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
    if args.review_command == "authenticate":
        return _review_authenticate(args)
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
        skeleton = extract_skeletons(
            args.blueprint_dir,
            lean_root=args.lean_root,
            timeout=args.timeout,
        )
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
        # it, or whose existing card cannot be safely read: all of them at
        # once, rather than one per extraction.
        _refuse_conflicts(_planned_records(args.blueprint_dir, inputs, model=args.model))
        graph = load_graph(args.blueprint_dir)
        before = _record_snapshot(graph, requests)
        # One extraction serves every record in the batch.
        skeleton = extract_skeletons(
            args.blueprint_dir,
            lean_root=args.lean_root,
            timeout=args.timeout,
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
    except KeyboardInterrupt:
        # The interrupt still ends the process (exit status 130), once the
        # cards filed before it are named. A card whose write it lands in may
        # be filed but not named; running the record again leaves it as it is.
        _report_recorded(written, len(requests))
        raise
    _report_recorded(written, len(requests))
    return 0


def _article_report(report: SkeletonReport, node_id: str) -> SkeletonReport:
    """The part of a shared extraction that one article's validation may see.

    ``validate_review_article`` insists on a report scoped to exactly its own
    article, so that missing evidence cannot pass. A batch extracts several
    articles at once and hands each its own node and unresolved targets.
    """

    return replace(
        report,
        selection="filtered",
        selected_nodes=(node_id,),
        nodes=tuple(node for node in report.nodes if node.node_id == node_id),
        unresolved=tuple(issue for issue in report.unresolved if issue.node_id == node_id),
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

    findings: list[ReviewFinding] = []
    cards: list[PreparedReadback] = []
    for item in inputs:
        try:
            cards.append(
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
            )
        except ValueError as exc:
            findings.append(
                ReviewFinding(
                    item.request.article_id,
                    "review-record-invalid",
                    f"{item.request.declaration}: {exc}",
                )
            )
    if findings:
        raise ReviewError(findings)
    return cards


def _refuse_conflicts(cards: list[PreparedReadback]) -> None:
    """Refuse the batch if publishing would refuse any card, naming every such card.

    An existing card that cannot be safely read (a link, a directory, a FIFO,
    or a file over the card limit) is listed with the conflicts rather than
    ending the listing.
    """

    # A platform that cannot publish is refused once, not once per card.
    readback_conflicts([])
    conflicts: list[str] = []
    for card in cards:
        try:
            conflicts.extend(readback_conflicts([card]))
        except ValueError as exc:
            conflicts.append(f"{card.declaration}: {exc}")
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
            raise ReviewError([_record_selection_finding(request.article_id, request.declaration)])
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
            f"error: {len(written)} of {total} read-back(s) were filed before the failure below; once its cause is "
            "cleared (for a conflict, by setting that record's expected_card_hash to the card hash it found, or "
            "removing it if it found none), running the record again files the rest and leaves these as they are",
            file=sys.stderr,
        )


def _review_check(args: argparse.Namespace) -> int:
    try:
        verifier = _approval_verifier(args.authenticate, publishing=False)
        graph, skeleton, bundle, cards = _current_review(
            args.blueprint_dir,
            lean_root=args.lean_root,
            bundle_path=args.bundle,
            timeout=args.timeout,
            skeleton_path=args.skeleton_report,
        )
        findings = review_findings(graph, bundle, skeleton, readbacks=cards)
        current = current_approvals(graph, bundle, cards)
    except (ApprovalError, GraphValidationError, ReviewError, SkeletonError) as exc:
        for issue in exc.issues:
            print(f"error: {issue}", file=sys.stderr)
        return 2
    failure: ApprovalError | None = None
    try:
        approvals = approval_statuses(graph, current, verifier)
    except ApprovalError as exc:
        # The findings stand; only the approvals go unauthenticated, each saying why.
        failure = exc
        approvals = {
            node_id: ApprovalStatus(node_id, review_hash, reason=str(exc))
            for node_id, review_hash in sorted(current.items())
        }
    if args.json:
        print(
            json.dumps(
                {
                    "approvals": [_approval_json(item) for item in approvals.values()],
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
    if not args.json:
        for item in approvals.values():
            print(_approval_line(item, verified=verifier is not None))
    if failure is not None:
        for issue in failure.issues:
            print(f"error: {issue}", file=sys.stderr)
        return 2
    return 1 if findings else 0


def _review_authenticate(args: argparse.Namespace) -> int:
    """Gate newly recorded approvals on evidence of who made them.

    Whether an approval is current is ``review check``'s job and needs Lean.
    This only asks who approved each recorded hash, so it runs in seconds.
    """

    if args.pr is not None and args.since is None:
        print("error: --pr requires --since, the pull request's base commit", file=sys.stderr)
        return 2
    if args.pr is None and args.since is not None and os.environ.get("GITHUB_EVENT_NAME", "").startswith("pull_request"):
        # Without --pr this waits for a merge the pull request has not had yet.
        print(
            "hint: this is a pull request run; pass --pr with its number to count the reviews of this pull "
            "request before it merges",
            file=sys.stderr,
        )
    try:
        verifier = _approval_verifier("github", trusted_ref=args.trusted_ref, pull_request=args.pr, publishing=False)
        graph = load_graph(args.blueprint_dir)
        recorded = {
            node.id: node.review_approved for node in graph.nodes.values() if node.review_approved is not None
        }
        previous = {} if args.since is None else approvals_at(graph, args.since, list(recorded))
        changed = {node_id: value for node_id, value in recorded.items() if previous.get(node_id) != value}
        statuses = approval_statuses(graph, changed, verifier)
    except (ApprovalError, GraphValidationError) as exc:
        for issue in exc.issues:
            print(f"error: {issue}", file=sys.stderr)
        return 2
    for node_id, review_hash in sorted(recorded.items()):
        if node_id in statuses:
            print(_approval_line(statuses[node_id], verified=True))
        else:
            print(f"{node_id}: unchanged since {args.since} · {review_hash}")
    unauthenticated = [item for item in statuses.values() if not item.authenticated]
    if args.since is not None and unauthenticated:
        # A new review fixes only an approval the gate refused for its reviews; the rest, such as an
        # unanswered request, an oversized answer, or rules that do not require code owner review, need
        # something else.
        if args.pr is not None and all(item.node_id in verifier.unapproved for item in unauthenticated):
            fix = (
                "an individual code owner with write access who is not the pull request's author must approve "
                "its final head commit"
            )
        else:
            fix = "each line above says why"
        print(
            f"error: {len(unauthenticated)} approval{'s' if len(unauthenticated) != 1 else ''} added or changed "
            f"since {args.since} {'are' if len(unauthenticated) != 1 else 'is'} self-approved; {fix}",
            file=sys.stderr,
        )
        return 1
    if args.since is not None:
        print(f"OK: every approval added or changed since {args.since} is authenticated")
    return 0


def _approval_verifier(
    method: str | None, *, trusted_ref: str = "HEAD", pull_request: int | None = None, publishing: bool = True
) -> GitHubReviewVerifier | None:
    """The verifier ``method`` names; ``publishing`` when the run renders the site, which a build that is
    not of the default branch's head, or cannot be shown to be, must not do."""

    if method is None:
        return None
    return GitHubReviewVerifier.from_environment(
        trusted_ref=trusted_ref, pull_request=pull_request, publishing=publishing
    )


def _approval_line(item: ApprovalStatus, *, verified: bool) -> str:
    line = f"{item.node_id}: {item.label} · {item.review_hash}"
    if item.attestation is not None:
        return f"{line} ({item.attestation.reference})" if item.attestation.reference else line
    return f"{line} ({item.reason})" if verified and item.reason else line


def _approval_json(item: ApprovalStatus) -> dict[str, object]:
    attestation = item.attestation
    return {
        "authenticated": item.authenticated,
        "label": item.label,
        "method": None if attestation is None else attestation.method,
        "node_id": item.node_id,
        "reason": item.reason,
        "reference": None if attestation is None else attestation.reference,
        "review_hash": item.review_hash,
        "reviewer": None if attestation is None else attestation.reviewer,
    }


def _current_review(
    blueprint_dir: str | Path,
    *,
    lean_root: Path | None,
    bundle_path: Path | None,
    timeout: float | None = None,
    skeleton_path: Path | None = None,
):
    """The graph, one extraction, a review bundle validated against both, and the cards.

    With ``skeleton_path`` the extraction is the report found there, written by
    ``autoform skeleton --output`` in a job that built the Lean project, and
    no Lean runs here. It must cover every article and name this checkout's
    blueprint hash; a report of other articles is refused, not reconciled.

    With ``bundle_path`` the bundle is the one ``review prepare`` wrote, and it
    must still describe the current tree. Without it, the bundle is derived
    from this same extraction. CI wants the latter: it never trusts a committed
    bundle, and preparing one in a separate command would pay for a second
    extraction of the same unchanged checkout.

    Everything returned describes one state of the blueprint. The graph and the
    read-back cards are read before Lean runs. The extraction refuses a
    blueprint that changes while it runs, and its hash must name the graph read
    here, which covers the time before it took its own snapshot. Cards are not
    articles, so they are read again afterwards and must be unchanged. Callers
    judge the cards returned here rather than reading the vault again. Article
    text and source passages used later, such as a statement compared or
    rendered, are the bytes the graph captured. A caller that loads the
    articles again, as render and audit do, is held to the same state by
    ``build_review_bundle``, which refuses a skeleton report paired with a
    blueprint, cited sources included, other than the one it was extracted
    from.
    """

    graph = load_graph(blueprint_dir)
    cards = _review_cards(graph.blueprint_dir)
    if skeleton_path is not None:
        skeleton = load_skeleton_report(skeleton_path)
        if skeleton.selection != "all":
            raise SkeletonError([f"{skeleton_path} covers selected articles only; review needs every article"])
        if skeleton.blueprint_hash != blueprint_hash(graph):
            raise SkeletonError(
                [
                    f"{skeleton_path} was extracted from blueprint {skeleton.blueprint_hash}, "
                    f"not from this checkout's {blueprint_hash(graph)}"
                ]
            )
    elif lean_root is None:
        raise SkeletonError(["review evidence needs --lean-root or --skeleton-report"])
    else:
        skeleton = extract_skeletons(
            blueprint_dir,
            lean_root=lean_root,
            timeout=timeout,
        )
    changed: list[ReviewFinding] = []
    if skeleton.blueprint_hash != blueprint_hash(graph):
        changed.append(
            ReviewFinding(
                "review",
                "review-snapshot-changed",
                "an article changed while the review evidence was being extracted; rerun once the blueprint is idle",
            )
        )
    if _review_cards(graph.blueprint_dir) != cards:
        changed.append(
            ReviewFinding(
                "review",
                "review-snapshot-changed",
                "a read-back changed while the review evidence was being extracted; rerun once the blueprint is idle",
            )
        )
    if changed:
        raise ReviewError(changed)
    bundle = build_review_bundle(graph, skeleton) if bundle_path is None else load_review_bundle(bundle_path)
    findings = validate_review_bundle(graph, bundle, skeleton)
    if findings:
        raise ReviewError(findings)
    return graph, skeleton, bundle, cards


def _review_cards(blueprint_dir: Path) -> dict[tuple[str, str], Readback]:
    """The vault's read-back cards; a card directory that cannot be listed is a review error, not missing cards."""

    try:
        return load_readbacks(blueprint_dir)
    except ValueError as exc:
        raise ReviewError([ReviewFinding("review", "readback-unlistable", str(exc))]) from exc


def _review_selection_finding(article_id: str, declaration: str) -> ReviewFinding:
    return ReviewFinding(
        article_id,
        "review-selection-missing",
        f"prepared review bundle has no declaration {declaration!r} for article_id {article_id}",
    )


def _record_selection_finding(article_id: str, declaration: str) -> ReviewFinding:
    """The bundle has this declaration, but the current blueprint no longer maps its article_id to it."""

    return ReviewFinding(
        article_id,
        "review-selection-missing",
        f"{declaration}: article_id {article_id} is no longer in the blueprint, or now names another declaration; "
        "rerun review prepare",
    )


def _claim_board(args: argparse.Namespace) -> ClaimBoard:
    worker_id = args.worker_id
    if not worker_id:
        raise ValueError("--worker-id or AUTOFORM_WORKER_ID is required")
    repo = args.repo or _origin_url()
    scratch = args.scratch or _default_claim_scratch(repo, worker_id)
    return ClaimBoard(repo, worker_id, scratch)


def _origin_url(root: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            cwd=root,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError("--repo is required outside a Git checkout with an origin remote") from exc
    return result.stdout.strip()


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def _default_claim_scratch(repo: str, worker_id: str) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    identity = hashlib.sha256(f"{repo}\0{worker_id}\0{socket.gethostname()}".encode()).hexdigest()[:24]
    return cache / "autoform" / "claims" / identity


def _render(args: argparse.Namespace) -> int:
    try:
        skeleton = None
        bundle = None
        cards = None
        if args.authenticate is not None and args.review_bundle is None and not args.review:
            print("error: --authenticate requires --review or --review-bundle", file=sys.stderr)
            return 2
        verifier = _approval_verifier(args.authenticate)
        if args.skeleton_report is not None and args.review_bundle is None and not args.review:
            print("error: --skeleton-report requires --review or --review-bundle", file=sys.stderr)
            return 2
        if args.review_bundle is not None or args.review:
            if args.lean_root is None and args.skeleton_report is None:
                flag = "--review" if args.review else "--review-bundle"
                print(f"error: {flag} requires --lean-root or --skeleton-report", file=sys.stderr)
                return 2
            _, skeleton, bundle, cards = _current_review(
                args.blueprint_dir,
                lean_root=args.lean_root,
                bundle_path=args.review_bundle,
                timeout=args.timeout,
                skeleton_path=args.skeleton_report,
            )
        report = render_site(
            args.blueprint_dir,
            args.output,
            lean_root=args.lean_root,
            repository_url=args.repository_url,
            ref=args.ref,
            skeleton=skeleton,
            review_bundle=bundle,
            readbacks=cards,
            approval_verifier=verifier,
        )
    except (ApprovalError, GraphValidationError, PublicationError, ReviewError, SkeletonError) as exc:
        for issue in exc.issues:
            print(f"error: {issue}")
        return 1

    print(f"{report.output_dir}: {report.pages} pages, {report.nodes} nodes, {report.linked} code links")
    # The site shows why only on hover, so the build log says it too.
    for node_id, reason in sorted(verifier.reasons.items() if verifier is not None else ()):
        print(f"warning: {node_id} is self-approved: {reason}")
    for issue in report.unresolved:
        print(f"warning: declaration not found in the Lean sources: {issue}")
    if report.unresolved and args.require_declarations:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
