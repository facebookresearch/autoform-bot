"""Command-line entry point for Autoform's project utilities."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shlex
import socket
import stat
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
from .impact import ImpactError, format_impact, revision_impact
from .lean import build_linker, declaration_names, index_failure_message
from .project import (
    ProjectCatalogError,
    ProjectCreateError,
    create_project,
    inspect_project,
    load_release_catalog,
)
from .readback import (
    TESTIMONY_MAX_BYTES,
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
from .work import WORK_SCHEMA, WorkError, assumption_contract, list_ready_work, work_context


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
        help=(
            "Autoform Git source the generated workflows install from "
            "(default: a safe remote locally known to contain this checkout's HEAD)"
        ),
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

    project = subparsers.add_parser(
        "project", help="create or inspect local projects and supported releases"
    )
    project_subparsers = project.add_subparsers(dest="project_command", required=True)
    project_new = project_subparsers.add_parser(
        "new", help="atomically create a complete Lean and Autoform project"
    )
    project_new.add_argument(
        "target", nargs="?", help="new project directory (required); it must not exist"
    )
    project_new.add_argument("--package", help="UpperCamelCase Lean package name (required)")
    project_new.add_argument(
        "--release", help="release id from 'project versions' (default: the recommended release)"
    )
    project_new.add_argument(
        "--lean-toolchain",
        help=(
            "Lean release tag such as v4.30.0; a pair the catalog does not list is written "
            "without lake-manifest.json, so run 'lake update' in the project"
        ),
    )
    project_new.add_argument(
        "--mathlib-rev",
        help=(
            "Mathlib tag, branch, or commit to require with --lean-toolchain "
            "(default: the same tag as the toolchain)"
        ),
    )
    project_new.add_argument(
        "--autoform-source",
        default="",
        help=(
            "Autoform Git source the generated workflows install from (default: a safe remote "
            "locally known to contain the HEAD of this checkout, or of the marketplace checkout "
            "an installed copy came from)"
        ),
    )
    project_new.add_argument(
        "--autoform-ref",
        default="",
        help=(
            "full 40-character Autoform commit the workflows pin (default: the HEAD commit of "
            "that checkout; none when --autoform-source is given)"
        ),
    )
    project_new.add_argument("--json", action="store_true", help="write stable machine-readable output")
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

    work = subparsers.add_parser("work", help="inspect the Markdown-derived formalization frontier")
    work_subparsers = work.add_subparsers(dest="work_command", required=True)
    work_list = work_subparsers.add_parser("list", help="list ready formalization leaves")
    work_list.add_argument(
        "target", nargs="?", default=".", help="project root or blueprint directory"
    )
    work_list.add_argument("--lean-root", type=Path, help="resolve local Lean declaration targets")
    work_list.add_argument("--json", action="store_true", help="write stable machine-readable output")
    work_context_parser = work_subparsers.add_parser(
        "context", help="show one article's phase, dependencies, sources, and claim target"
    )
    work_context_parser.add_argument("selector", help="path-derived node id or durable article_id")
    work_context_parser.add_argument(
        "target", nargs="?", default=".", help="project root or blueprint directory"
    )
    work_context_parser.add_argument(
        "--lean-root", type=Path, help="resolve local Lean declaration targets"
    )
    work_context_parser.add_argument(
        "--json", action="store_true", help="write stable machine-readable output"
    )
    work_assumptions = work_subparsers.add_parser(
        "assumptions", help="list open statements and the open statements each stated article rests on"
    )
    work_assumptions.add_argument(
        "target", nargs="?", default=".", help="project root or blueprint directory"
    )
    work_assumptions.add_argument("--json", action="store_true", help="write stable machine-readable output")
    work_impact = work_subparsers.add_parser(
        "impact", help="show which articles and helpers a revision of an article's Lean declarations affects"
    )
    work_impact.add_argument("selector", help="path-derived node id or durable article_id")
    work_impact.add_argument("target", nargs="?", default=".", help="project root or blueprint directory")
    work_impact.add_argument(
        "--lean-root", type=Path, required=True, metavar="PATH", help="the built Lean project"
    )
    work_impact.add_argument(
        "--declaration",
        action="append",
        default=[],
        dest="declarations",
        metavar="NAME",
        help="revise this project-local constant instead of the article's lean: declarations (repeatable)",
    )
    work_impact.add_argument("--json", action="store_true", help="write stable machine-readable output")
    work_impact.add_argument(
        "--timeout",
        type=_positive_seconds,
        metavar="SECONDS",
        help=f"seconds the Lean probe may run (default {DEFAULT_PROBE_TIMEOUT:g}); "
        "the Lake freshness check before it has its own budget",
    )
    claim = subparsers.add_parser("claim", help="coordinate temporary node ownership through Git refs")
    claim_subparsers = claim.add_subparsers(dest="claim_command", required=True)
    for operation in ("acquire", "renew", "release"):
        command = claim_subparsers.add_parser(operation)
        command.add_argument("node_id", nargs="+", help="claim target(s); several change all-or-nothing")
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
    if args.command == "work":
        return _work(args)
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

    linker = None
    if args.lean_root is not None:
        try:
            linker = build_linker(args.lean_root)
        except OSError as error:
            print(f"error: {index_failure_message(error)}")
            return 1

    statuses = status.derive(graph)
    summary = " · ".join(f"{count} {state.label}" for state, count in status.summarize(statuses))
    print(f"OK: {len(graph.nodes)} articles, {graph.edge_count} dependencies")
    if summary:
        print(f"    {summary}")

    if linker is None:
        return 0

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
        if args.project_command == "new":
            if args.target is None:
                raise ProjectCreateError("project-target-invalid", "A new project directory is required.")
            if args.package is None:
                raise ProjectCreateError(
                    "project-name-invalid", "--package is required (an UpperCamelCase Lean package name)."
                )
            target = os.path.expanduser(args.target)
            existed = os.path.lexists(target)
            try:
                result = create_project(
                    args.target,
                    package=args.package,
                    release_id=args.release,
                    lean_toolchain=args.lean_toolchain,
                    mathlib_rev=args.mathlib_rev,
                    autoform_source=args.autoform_source,
                    autoform_ref=args.autoform_ref,
                )
            except KeyboardInterrupt:
                # An interrupt during create_project's final cleanup can follow a
                # successful publication, so look before saying nothing was published.
                if not existed and os.path.lexists(target):
                    message = (
                        "Project creation was interrupted after the target was created, so it may be "
                        "this run's complete project. Check it with autoform project inspect before "
                        "using or removing it."
                    )
                else:
                    message = (
                        "Project creation was interrupted and no project was published. A hidden "
                        ".autoform-new-* stage may remain in the target parent; inspect it before removal."
                    )
                error = ProjectCreateError("project-create-interrupted", message)
                if args.json:
                    print(error.to_json())
                else:
                    print(f"error[{error.code}]: {error.message}", file=sys.stderr)
                return 130
            if args.json:
                print(result.to_json())
            else:
                label = result.release or f"unlisted: {result.lean_toolchain}, Mathlib {result.mathlib_rev}"
                print(_ascii_text(f"Created {result.package} at {result.target} ({label})"))
                # Flush first so the warnings never appear ahead of the line they qualify.
                sys.stdout.flush()
                for code, message in result.warnings:
                    print(_ascii_text(f"warning[{code}]: {message}"), file=sys.stderr)
                if not result.workflows_pinned:
                    init_command = (
                        'uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform init '
                        f"{_shell_quote_one_line(target)}"
                    )
                    if args.autoform_source:
                        # Without the source, init would pin this checkout's origin instead.
                        init_command += f" --autoform-source {shlex.quote(args.autoform_source)}"
                    print(
                        "warning: workflows were omitted because no immutable Autoform pin was "
                        f"available; add them with: {init_command} --autoform-ref <40-char-sha>",
                        file=sys.stderr,
                    )
            return 0
        catalog = load_release_catalog()
    except ProjectCreateError as error:
        if args.json:
            print(error.to_json())
        else:
            print(f"error[{error.code}]: {error.message}", file=sys.stderr)
        return 1
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


def _work(args: argparse.Namespace) -> int:
    if args.work_command == "assumptions":
        return _work_assumptions(args)
    if args.work_command == "impact":
        return _work_impact(args)
    # Only loading the roadmap can fail on the project's paths; printing the
    # result stays outside, so an output error is not reported as one.
    try:
        if args.work_command == "list":
            frontier = list_ready_work(args.target, lean_root=args.lean_root)
        else:
            source_revision, item = work_context(
                args.target,
                args.selector,
                lean_root=args.lean_root,
            )
    except (GraphValidationError, RuntimeProjectionError) as error:
        for issue in error.issues:
            print(f"error: {_human_text(issue)}", file=sys.stderr)
        return 2
    except WorkError as error:
        print(f"error: {_human_text(error)}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError):
        print("error: project, blueprint, or Lean root path cannot be read", file=sys.stderr)
        return 2

    if args.work_command == "list":
        if args.json:
            print(frontier.to_json())
            return 0
        if frontier.open_statements:
            print("Open statements: allowed (a statement may land with a sorry proof)")
        if not frontier.items:
            print("No ready formalization work.")
            return 0
        for item in frontier.items:
            durable = f" [{item.article_id}]" if item.article_id else ""
            print(_human_text(f"{item.phase}: {item.node_id}{durable} - {item.title}"))
            if item.assumes:
                print(_human_text("  assumes: " + ", ".join(item.assumes)))
            if item.revision:
                print("  revision: start from `autoform work impact`")
        return 0

    if args.json:
        print(
            json.dumps(
                {
                    "schema": WORK_SCHEMA,
                    "source_revision": source_revision,
                    "item": item.as_dict(),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    if item.phase:
        phase = item.phase
    elif item.blockers:
        phase = "not ready"
    else:
        phase = "none (already formalized)"
    print(_human_text(f"{item.title} ({item.node_id})"))
    print(f"State: {item.state}")
    print(f"Phase: {phase}")
    if item.revision:
        print("Revision: the statement was retracted; start from `autoform work impact`")
    if item.open_statements:
        print("Open statements: allowed")
        if item.assumes:
            print(_human_text("Assumes: " + ", ".join(item.assumes)))
    print(_human_text(f"Claim target: {item.claim_target}"))
    if item.blockers:
        print(_human_text("Blocked by: " + ", ".join(item.blockers)))
    print(_human_text(f"Article: {item.article_path}"))
    print(f"Article revision: {item.article_revision or 'unknown'}")
    print(f"Graph source revision: {source_revision}")
    if item.dependencies:
        print(_human_text("Dependencies: " + ", ".join(item.dependencies)))
    if item.source_targets:
        print(_human_text("Sources: " + ", ".join(item.source_targets)))
    for target in item.lean_targets:
        location = f" ({target.source_file})" if target.source_file else ""
        print(_human_text(f"Lean: {target.declaration}{location}"))
    return 0


def _work_assumptions(args: argparse.Namespace) -> int:
    # As in `_work`, only loading the roadmap is reported as a path error.
    try:
        contract = assumption_contract(args.target)
        # The text report also names conditional articles without `lean:`,
        # which the contract leaves out because CI has nothing to check there.
        runtime = None if args.json else load_runtime_graph(args.target)
    except (GraphValidationError, RuntimeProjectionError) as error:
        for issue in error.issues:
            print(f"error: {_human_text(issue)}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError):
        print("error: project or blueprint path cannot be read", file=sys.stderr)
        return 2

    if args.json:
        print(contract.to_json())
        return 0
    print(_human_text(f"Open statements: {'allowed' if contract.open_statements else 'forbidden'}"))
    listed = {article.id: article for article in contract.articles}
    for node in sorted(runtime.nodes, key=lambda candidate: candidate.id):
        article = listed.get(node.id)
        assumes = f"assumes {', '.join(node.status.assumes)}"
        if article is not None and article.open:
            # An open statement is not conditional: its own proof is missing.
            line = f"open: {article.id} ({', '.join(article.declarations)})"
            print(_human_text(f"{line} {assumes}" if article.assumes else line))
        elif node.status.state == "conditional":
            print(_human_text(f"conditional: {node.id} {assumes}"))
        elif article is not None and article.assumes:
            # Not proved, so nothing is conditional yet; its proof would rest on these.
            print(_human_text(f"unproved: {article.id} {assumes}"))
    return 0


def _work_impact(args: argparse.Namespace) -> int:
    try:
        report = revision_impact(
            args.target,
            args.selector,
            lean_root=args.lean_root,
            declarations=args.declarations,
            timeout=args.timeout,
        )
    except (GraphValidationError, RuntimeProjectionError) as error:
        for issue in error.issues:
            print(f"error: {_human_text(issue)}", file=sys.stderr)
        return 2
    except (WorkError, ImpactError) as error:
        print(f"error: {_human_text(error)}", file=sys.stderr)
        return 2
    except SkeletonError as error:
        # Probe failures carry Lean's multi-line output; escape it line by line.
        for issue in error.issues:
            for index, line in enumerate(str(issue).splitlines() or [""]):
                print(("error: " if index == 0 else "") + _human_text(line), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError):
        print("error: project, blueprint, or Lean root path cannot be read", file=sys.stderr)
        return 2

    if args.json:
        print(report.to_json())
        return 0
    for line in format_impact(report):
        print(_human_text(line))
    return 0


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


def _ascii_text(value: object) -> str:
    """Escape untrusted text into one unambiguous printable ASCII line.

    `project new` reports after publishing, when a non-UTF-8 stream must not
    turn success into a traceback.
    """

    return ascii(str(value))[1:-1]


def _shell_quote_one_line(value: str) -> str:
    """Quote one filesystem argument without letting it forge another line.

    Ordinary printable paths use the standard shell spelling. POSIX project
    creation can also accept control bytes in filenames; Bash and Zsh ANSI-C
    quoting keeps those paths executable while spelling every byte on one line.
    """

    if all(character.isprintable() and character not in "\r\n" for character in value):
        return shlex.quote(value)
    pieces: list[str] = []
    for byte in os.fsencode(value):
        if byte == 0x27:
            pieces.append("\\'")
        elif byte == 0x5C:
            pieces.append("\\\\")
        elif 0x20 <= byte <= 0x7E:
            pieces.append(chr(byte))
        else:
            pieces.append(f"\\x{byte:02x}")
    return "$'" + "".join(pieces) + "'"


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

        past_tense = {"acquire": "acquired", "renew": "renewed", "release": "released"}
        if len(args.node_id) > 1:
            # Several targets change in one atomic push, so a failure holds none of them.
            targets: dict[str, str] = {}
            for node_id in args.node_id:
                key = author_claim_key(node_id)
                if key in targets:
                    print(f"error: duplicate claim target: {node_id}", file=sys.stderr)
                    return 2
                targets[key] = node_id
            if operation == "acquire":
                result = board.acquire_many(list(targets), ttl=args.ttl, note=args.note)
            elif operation == "renew":
                result = board.renew_many(list(targets), ttl=args.ttl)
            else:
                result = board.release_many(list(targets))
            if result:
                for key, node_id in targets.items():
                    print(f"{past_tense[operation]} {node_id} ({key})")
                return 0
            blocking = ", ".join(targets.get(key, key) for key in result.blocking)
            reason = f"{result.reason}: {blocking}" if blocking else result.reason
            print(
                f"error: could not {operation} {', '.join(args.node_id)}; "
                f"no claim was {past_tense[operation]}: {reason}"
            )
            return 1

        node_id = args.node_id[0]
        key = author_claim_key(node_id)
        if operation == "acquire":
            succeeded = board.acquire(key, ttl=args.ttl, note=args.note)
        elif operation == "renew":
            succeeded = board.renew(key, ttl=args.ttl)
        else:
            succeeded = board.release(key)
        if succeeded:
            print(f"{past_tense[operation]} {node_id} ({key})")
            return 0
        print(f"error: could not {operation} {node_id}; ownership is held or unverifiable")
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
        # So is a platform that cannot publish, ahead of advice that only helps
        # where cards can be filed.
        readback_conflicts([])
        # So is every record whose article_id the blueprint no longer has,
        # ahead of the card check: the hash that check asks for cannot help it.
        graph = load_graph(args.blueprint_dir)
        before = _record_snapshot(graph, requests)
        # And so is every card that would replace different content without
        # naming it, or whose existing card cannot be safely read: all of them
        # at once, rather than one per extraction.
        _refuse_conflicts(_planned_records(args.blueprint_dir, inputs, model=args.model))
        # Each card's article is read again before the card is written, by its
        # name rather than the file it resolves to, so that a link pointed at
        # another file is seen.
        articles = {
            article_id: (_named_article_path(graph, node_id), digest) for article_id, node_id, _, digest in before
        }
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
            # Its article is read again just before the card is written. That
            # narrows the window without closing it: an edit after this read,
            # while the card is written, goes unseen, and so does an edit to a
            # source the article cites, which only the reload above compares.
            _refuse_changed_article(card, *articles[card.article_id])
            try:
                path = publish_readback(card)
            except ValueError as exc:
                # Named as the conflict check names it: the partial-batch
                # message points to "that record", so the error must say which.
                raise ValueError(f"{card.declaration}: {exc}") from exc
            written.append((path, card.declaration))
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
        # The limit applies after "\r\n" is read as "\n", which at most halves
        # a testimony. So at most one byte past twice the limit is read, which
        # marks a testimony over it, and a huge file costs no more than two
        # testimonies do.
        try:
            with request.testimony.open("rb") as handle:
                raw = handle.read(2 * TESTIMONY_MAX_BYTES + 1)
        except OSError as exc:
            findings.append(_unreadable_input(request, "testimony", request.testimony, exc))
            continue
        if len(raw) - raw.count(b"\r\n") > TESTIMONY_MAX_BYTES:
            findings.append(
                ReviewFinding(
                    request.article_id,
                    "review-record-invalid",
                    f"{request.declaration}: unsafe read-back testimony: {request.testimony} is over the "
                    f"{TESTIMONY_MAX_BYTES}-byte limit",
                )
            )
            continue
        try:
            # Decoded as a text file is read: "\r\n" and a lone "\r" end a
            # line as "\n" does.
            testimony = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeError as exc:
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
    gone: list[ReviewFinding] = []
    for request in requests:
        if request.article_id in state:
            continue
        matches = [node for node in graph.nodes.values() if node.article_id == request.article_id]
        if len(matches) != 1:
            # Every such record is named, so one run lists all there are to drop or update.
            gone.append(_record_selection_finding(request))
            continue
        node = matches[0]
        state[request.article_id] = (request.article_id, node.id, str(node.path), node.source_sha256 or "")
    if gone:
        raise ReviewError(gone)
    return tuple(sorted(state.values()))


def _named_article_path(graph: Graph, node_id: str) -> str:
    """The path the blueprint names an article by, which for a link is the link.

    ``load_graph`` keeps the file a link resolves to, and reading that file
    again would miss the link being pointed at another one. The name is the
    candidate for the article's id that resolves to the loaded file; if none
    does, the article changed after the load, which the reload after the
    extraction refuses.
    """

    roadmap = graph.blueprint_dir / "roadmap"
    path = str(graph.nodes[node_id].path)
    names = (roadmap / f"{node_id}.md", roadmap / node_id / "README.md", roadmap / "README.md")
    return next((str(name) for name in names if os.path.realpath(name) == path), path)


def _refuse_changed_article(card: PreparedReadback, path: str, digest: str) -> None:
    """Refuse to file ``card`` unless its article's file still holds the bytes its evidence was checked against."""

    # Opened without waiting for a writer, so a FIFO put at the path, or a link
    # to one, is refused unread instead of holding the batch.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    not_regular = (
        "is no longer a regular file, so its card was not filed; restore the article, or drop its records, "
        "and rerun the record"
    )
    try:
        descriptor = os.open(path, flags)
        try:
            regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
            content = hashlib.sha256()
            while regular and (block := os.read(descriptor, 64 * 1024)):
                content.update(block)
        finally:
            os.close(descriptor)
        if not regular:
            what = not_regular
        elif content.hexdigest() == digest:
            return
        else:
            what = (
                "changed after its evidence was checked, so its card was not filed; rerun the record, after review "
                "prepare if the change is to that evidence"
            )
    except (FileNotFoundError, NotADirectoryError):
        # A link left naming a missing file is still there; only its target is gone.
        gone = "now links to a missing file" if os.path.islink(path) else "was deleted after its evidence was checked"
        what = f"{gone}, so its card was not filed; restore the article, or drop its records, and rerun the record"
    except OSError as exc:
        what = (
            f"cannot be read ({exc.strerror or exc}), so its card was not filed; rerun the record once it can be read"
        )
        # A socket cannot be opened, and a link that loops, or a chain of links
        # too long to follow, cannot be followed: none of them reads as a
        # regular file, however often the record is rerun.
        if exc.errno == errno.ELOOP or (os.path.exists(path) and not os.path.isfile(path)):
            what = not_regular
    raise ReviewError(
        [ReviewFinding(card.article_id, "review-snapshot-changed", f"{card.declaration}: article {path} {what}")]
    )


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
        # validate_review_article matched this article's extracted declarations
        # to the bundle's, where _record_inputs found this one.
        current = next(item for item in skeleton.declarations(node.id) if item.name == request.declaration)
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


def _record_selection_finding(request: RecordRequest) -> ReviewFinding:
    """The bundle has this declaration, but the current blueprint no longer has its article_id."""

    # A card's path is keyed by its article_id: the card a re-review names
    # stays under the old one, so a record taking the new one must not name it.
    # A packet is named by its content, so a different one is text the
    # testimony was not written from.
    drop_hash = " and drop its expected_card_hash" if request.expected_card_hash is not None else ""
    return ReviewFinding(
        request.article_id,
        "review-selection-missing",
        f"{request.declaration}: article_id {request.article_id} is no longer in the blueprint; drop the record, "
        f"or rerun review prepare and take its article_id from the new packet manifest{drop_hash}; if that manifest "
        "names a different packet for it, the record needs that packet and a testimony written from it",
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
