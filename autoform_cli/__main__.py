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
from pathlib import Path

from . import status
from .article_identity import plan_article_ids
from .audit import audit_blueprint
from .claims import CLAIM_TTL_S, ClaimBoard, ClaimTransportError, author_claim_key
from .doctor import diagnose_project
from .dashboard import publication_bound_live_state, serve_dashboard
from .graph import GraphValidationError, load_graph
from .impact import ImpactError, format_impact, revision_impact
from .lean import build_linker, declaration_names
from .project import ProjectCatalogError, inspect_project, load_release_catalog
from .render import PublicationError, render_site
from .runtime import RuntimeProjectionError, load_runtime_graph, resolve_runtime_paths
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
    for article in contract.articles:
        assumes = f"assumes {', '.join(article.assumes)}"
        if article.open:
            # An open statement is not conditional: its own proof is missing.
            line = f"open: {article.id} ({', '.join(article.declarations)})"
            print(_human_text(f"{line} {assumes}" if article.assumes else line))
        elif article.assumes:
            print(_human_text(f"conditional: {article.id} {assumes}"))
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
