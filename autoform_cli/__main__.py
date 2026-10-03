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
from pathlib import Path

from . import status
from .article_identity import plan_article_ids
from .audit import audit_blueprint
from .claims import CLAIM_TTL_S, ClaimBoard, ClaimTransportError, author_claim_key
from .doctor import diagnose_project
from .graph import GraphValidationError, load_graph
from .lean import build_linker, declaration_names
from .project import ProjectCatalogError, inspect_project, load_release_catalog
from .provenance import ProvenanceError, resolve_plugin_provenance
from .render import PublicationError, render_site
from .scaffold import ScaffoldError, scaffold_project
from .skeleton import (
    DEFAULT_PROBE_TIMEOUT,
    SkeletonError,
    extract_skeletons,
    format_report,
    run_probe,
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
        help="Autoform Git source for generated workflows (default: recorded installation source)",
    )
    init.add_argument(
        "--autoform-ref",
        default="",
        help="full commit SHA for generated workflows (default: recorded installed commit)",
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

    doctor = subparsers.add_parser("doctor", help="diagnose the local Markdown runtime contract")
    doctor.add_argument("project_or_blueprint")
    doctor.add_argument("--lean-root", type=Path, help="Lean project to resolve local targets against")
    doctor.add_argument("--json", action="store_true", help="write stable machine-readable output")

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
    project_provenance = project_subparsers.add_parser(
        "provenance",
        help="read Autoform's recorded immutable source and commit",
    )
    project_provenance.add_argument(
        "--json", action="store_true", help="write stable machine-readable output"
    )

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
        help="also write one comment-stripped packet per skeleton for blind auditors",
    )
    skeleton.add_argument(
        "--passages",
        type=Path,
        metavar="DIR",
        help="with --packets: also write each article's cited source passage, for a faithfulness judge",
    )
    skeleton.add_argument(
        "--timeout",
        type=_positive_seconds,
        metavar="SECONDS",
        help=f"seconds the Lean probe may run (default {DEFAULT_PROBE_TIMEOUT:g}); "
        "the Lake freshness check before it has its own budget",
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

    args = parser.parse_args(argv)

    if args.command == "init":
        return _init(args)
    if args.command == "check":
        return _check(args)
    if args.command == "audit":
        return _audit(args)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "project":
        return _project(args)
    if args.command == "claim":
        return _claim(args)
    if args.command == "migrate":
        return _migrate(args)
    if args.command == "skeleton":
        return _skeleton(args)
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
        if args.json:
            print(
                json.dumps(
                    {
                        "error": {"code": "scaffold-invalid", "message": str(error)},
                        "ok": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 2
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
        print(f"  = {path} (exists, left alone)")
    print("Next: describe the project in blueprint/README.md, then add chapters "
          "as roadmap/<chapter>/README.md.")
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
    result = audit_blueprint(args.blueprint_dir, lean_root=args.lean_root)
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


def _project(args: argparse.Namespace) -> int:
    if args.project_command == "provenance":
        try:
            result = resolve_plugin_provenance()
        except ProvenanceError as error:
            return _print_provenance_error(args, error)
        return _print_provenance_result(args, result)
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


def _print_provenance_result(args: argparse.Namespace, result) -> int:
    if args.json:
        print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
    else:
        print(f"Source: {result.source}")
        print(f"Revision: {result.revision}")
    return 0


def _print_provenance_error(args: argparse.Namespace, error) -> int:
    if args.json:
        print(
            json.dumps(
                {
                    "error": {"code": error.code, "message": error.message},
                    "ok": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        print(f"error[{error.code}]: {error.message}", file=sys.stderr)
    return 1


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
            runner=None
            if args.timeout is None
            else lambda probe, root: run_probe(probe, root, timeout=args.timeout),
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
        report = render_site(
            args.blueprint_dir,
            args.output,
            lean_root=args.lean_root,
            repository_url=args.repository_url,
            ref=args.ref,
        )
    except (GraphValidationError, PublicationError) as exc:
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
