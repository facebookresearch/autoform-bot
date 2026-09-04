"""Command-line entry point for Autoform's project utilities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import status
from .article_identity import plan_article_ids
from .audit import audit_blueprint
from .claims import (
    CLAIM_TTL_S,
    ClaimBoard,
    ClaimTransportError,
    _claim_git_environment,
    author_claim_key,
    claim_repository_is_remote,
    pin_claim_repository,
    pin_claim_scratch,
    resource_claim_key,
    workspace_author_claim_key,
)
from .doctor import diagnose_project
from .execution_input import ExecutionInputError
from .graph import ARTICLE_ID_PATTERN, GraphValidationError
from .lean import build_linker, declaration_names
from .project import (
    ProjectCatalogError,
    ProjectCreateError,
    ProjectRepairError,
    create_project,
    inspect_project,
    load_release_catalog,
    repair_project,
)
from .provenance import ProvenanceError, verify_plugin_provenance
from .ready import READY_SCHEMA, list_ready_work
from .render import PublicationError, render_site
from .runtime import (
    RuntimePaths,
    RuntimeProjectionError,
    bind_runtime_paths,
    load_bound_graph,
)
from .scaffold import ScaffoldError, scaffold_project
from .workspace_cli import add_workspace_parsers, run_blueprint_command, run_workspace_command

_CLAIM_TEMP_DIRECTORY = Path(tempfile.gettempdir()).resolve()


@dataclass(frozen=True, slots=True)
class _ClaimBoardIdentity:
    repo: str
    repo_identity: tuple[int, int] | None
    session_id: str
    scratch: Path
    scratch_identity: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class _PinnedDirectory:
    path: Path
    identity: tuple[tuple[int, int, int | None], ...]

    @staticmethod
    def _snapshot(path: Path, *, label: str) -> tuple[tuple[int, int, int | None], ...]:
        snapshot: list[tuple[int, int, int | None]] = []
        temp_ancestors = frozenset(
            (_CLAIM_TEMP_DIRECTORY, *_CLAIM_TEMP_DIRECTORY.parents)
        )
        for component in reversed((path, *path.parents)):
            try:
                info = component.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"{label} cannot be inspected safely") from exc
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"{label} must have only real directory components")
            changed_at_ns = None if component in temp_ancestors else info.st_ctime_ns
            snapshot.append((info.st_dev, info.st_ino, changed_at_ns))
        return tuple(snapshot)

    @classmethod
    def capture(cls, path: Path, *, label: str) -> _PinnedDirectory:
        before = cls._snapshot(path, label=label)
        after = cls._snapshot(path, label=label)
        if before != after:
            raise ValueError(f"{label} changed while its path was being inspected")
        return cls(path=path, identity=after)

    def verify(self, *, label: str) -> None:
        try:
            current = self._snapshot(self.path, label=label)
        except ValueError as exc:
            raise ValueError(f"{label} was replaced while resolving the claim") from exc
        if current != self.identity:
            raise ValueError(f"{label} was replaced while resolving the claim")


@dataclass(frozen=True, slots=True)
class _ResolvedClaimTarget:
    key: str
    label: str
    compatibility_keys: tuple[str, ...]
    canonical_keys: tuple[str, ...]
    board_identity: _ClaimBoardIdentity


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
        help="Autoform Git source for generated workflows (default: verified installation source)",
    )
    init.add_argument(
        "--autoform-ref",
        default="",
        help="full commit for generated workflows (default: verified installation revision)",
    )
    init.add_argument("--force", action="store_true", help="overwrite files that already exist")
    init.add_argument("--json", action="store_true", help="write stable machine-readable output")

    check = subparsers.add_parser("check", help="validate a Markdown blueprint")
    check.add_argument("blueprint_dir")
    check.add_argument("--project", help="registered workspace project id")
    check.add_argument(
        "--lean-root",
        type=Path,
        help="Lean project to resolve 'lean:' declarations against (enables declaration checking)",
    )

    audit = subparsers.add_parser("audit", help="audit roadmap completeness and checked facts")
    audit.add_argument("blueprint_dir")
    audit.add_argument("--project", help="registered workspace project id")
    audit.add_argument("--lean-root", type=Path, help="Lean project to resolve local targets against")
    audit.add_argument("--json", action="store_true", help="write stable machine-readable output")

    doctor = subparsers.add_parser("doctor", help="diagnose the local Markdown runtime contract")
    doctor.add_argument("project_or_blueprint")
    doctor.add_argument("--project", help="registered workspace project id")
    doctor.add_argument("--lean-root", type=Path, help="Lean project to resolve local targets against")
    doctor.add_argument("--json", action="store_true", help="write stable machine-readable output")

    ready = subparsers.add_parser(
        "ready", help="list formalization work whose authored prerequisites are satisfied"
    )
    ready.add_argument("project_or_blueprint")
    ready.add_argument("--project", help="registered workspace project id")
    ready.add_argument("--lean-root", type=Path, help="Lean project to bind into the execution input")
    ready.add_argument("--json", action="store_true", help="write stable machine-readable output")

    project = subparsers.add_parser("project", help="inspect local project configuration and releases")
    project_subparsers = project.add_subparsers(dest="project_command", required=True)
    project_new = project_subparsers.add_parser(
        "new", help="atomically create a complete Lean and Autoform project"
    )
    project_new.add_argument(
        "target",
        nargs="?",
        help="new absent directory, or '.' for the empty current directory",
    )
    project_new.add_argument("--package", help="UpperCamelCase Lean package name")
    project_new.add_argument("--release", help="release id from 'project versions'")
    project_new.add_argument(
        "--autoform-source",
        default="",
        help="trusted Autoform Git source for generated workflows",
    )
    project_new.add_argument(
        "--autoform-ref",
        default="",
        help="full 40-character Autoform commit for generated workflows",
    )
    project_new.add_argument("--json", action="store_true", help="write stable machine-readable output")
    project_repair = project_subparsers.add_parser(
        "repair", help="conservatively add unambiguous missing project files"
    )
    project_repair.add_argument("target", help="existing project directory")
    project_repair.add_argument(
        "--title", help="exact human project title for missing generated files"
    )
    project_repair.add_argument(
        "--repository-url",
        help="exact project URL for a missing site configuration (empty is allowed)",
    )
    project_repair.add_argument(
        "--autoform-source",
        help="exact Autoform Git source for missing workflows",
    )
    project_repair.add_argument(
        "--autoform-ref",
        help="exact immutable Autoform commit for missing workflows",
    )
    project_repair.add_argument("--dry-run", action="store_true", help="report without writing")
    project_repair.add_argument("--json", action="store_true", help="write stable machine-readable output")
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
        help="verify immutable provenance for this Autoform installation",
    )
    project_provenance.add_argument(
        "--json", action="store_true", help="write stable machine-readable output"
    )

    add_workspace_parsers(subparsers)

    claim = subparsers.add_parser(
        "claim", help="coordinate temporary article and resource ownership through Git refs"
    )
    claim_subparsers = claim.add_subparsers(dest="claim_command", required=True)
    for operation in ("acquire", "renew", "release"):
        command = claim_subparsers.add_parser(operation)
        command.add_argument("node_id", nargs="?", help="roadmap path id or exact article_id")
        command.add_argument("--resource", help="claim a raw shared resource instead of an article")
        command.add_argument(
            "--blueprint",
            default=".",
            help="project or blueprint directory used to resolve the article (default: current directory)",
        )
        command.add_argument("--project", help="registered workspace project id")
        _add_claim_board_arguments(command)
        if operation in {"acquire", "renew"}:
            command.add_argument("--ttl", type=int, default=CLAIM_TTL_S)
        if operation == "acquire":
            command.add_argument("--note", default="")
    claim_list = claim_subparsers.add_parser("list")
    _add_claim_board_arguments(claim_list)
    claim_cleanup = claim_subparsers.add_parser("cleanup")
    _add_claim_board_arguments(claim_cleanup)
    claim_cleanup.add_argument(
        "--blueprint",
        help="project or blueprint directory required to retire legacy author refs safely",
    )
    claim_cleanup.add_argument("--project", help="registered workspace project id")

    migrate = subparsers.add_parser("migrate", help="inspect authored migration contracts")
    migrate_subparsers = migrate.add_subparsers(dest="migrate_command", required=True)
    article_ids = migrate_subparsers.add_parser(
        "article-ids",
        help="plan durable roadmap article identifiers without writing files",
    )
    article_ids.add_argument("blueprint_dir")
    article_ids.add_argument("--project", help="registered workspace project id")
    article_ids.add_argument(
        "--check",
        action="store_true",
        help="fail when an article is missing article_id frontmatter",
    )
    article_ids.add_argument("--json", action="store_true", help="write stable machine-readable output")

    render = subparsers.add_parser("render", help="build the publishable blueprint")
    render.add_argument("blueprint_dir")
    render.add_argument("--project", help="registered workspace project id")
    render.add_argument(
        "-o",
        "--output",
        help="output directory (default: site-src, or site-src/<project-id> in a workspace)",
    )
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
    if args.command == "ready":
        return _ready(args)
    if args.command == "project":
        return _project(args)
    if args.command == "workspace":
        return run_workspace_command(args)
    if args.command == "blueprint":
        return run_blueprint_command(args)
    if args.command == "claim":
        return _claim(args)
    if args.command == "migrate":
        return _migrate(args)
    if args.command == "render":
        return _render(args)
    return 2


def _add_claim_board_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="claim-board Git repository; defaults to this checkout's origin")
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("AUTOFORM_WORKER_ID"),
        help="display identity for this agent (or set AUTOFORM_WORKER_ID)",
    )
    parser.add_argument(
        "--session-id",
        default=os.environ.get("AUTOFORM_CLAIM_SESSION_ID"),
        help="stable work session identity (or set AUTOFORM_CLAIM_SESSION_ID)",
    )
    parser.add_argument("--scratch", type=Path, help="local bare Git object cache")
    parser.add_argument(
        "--object-format",
        choices=("sha1", "sha256"),
        default=os.environ.get("AUTOFORM_GIT_OBJECT_FORMAT"),
        help="Git object format for an empty network claim repository",
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
            "ref, and this installation has no verified source and commit.\n"
            "Re-run with the complete pair to add them:\n"
            "  autoform init --autoform-source <git-url> "
            "--autoform-ref <40-char-sha>",
            file=sys.stderr,
        )
    return 0


def _check(args: argparse.Namespace) -> int:
    try:
        with _resolved_blueprint(args.blueprint_dir, args.project) as paths:
            graph = load_bound_graph(paths)

            statuses = status.derive(graph)
            summary = " · ".join(
                f"{count} {state.label}" for state, count in status.summarize(statuses)
            )
            missing: list[str] = []
            if args.lean_root is None:
                paths.verify()
            else:
                linker = build_linker(args.lean_root)
                missing = [
                    f"{node.id}: declaration not found in {args.lean_root}: {name}"
                    for node in graph.nodes.values()
                    for name in declaration_names(node.lean or "")
                    if linker.location(name) is None
                ]
                paths.verify()
    except (GraphValidationError, RuntimeProjectionError) as exc:
        for issue in exc.issues:
            print(f"error: {issue}")
        return 1
    except OSError as exc:
        print(f"error: {exc}")
        return 1

    print(f"OK: {len(graph.nodes)} articles, {graph.edge_count} dependencies")
    if summary:
        print(f"    {summary}")
    if args.lean_root is None:
        return 0
    for issue in missing:
        print(f"error: {issue}")
    if missing:
        return 1
    declared = sum(1 for node in graph.nodes.values() if node.lean)
    print(f"    {declared} declaration(s) resolved in the Lean sources")
    return 0


def _audit(args: argparse.Namespace) -> int:
    try:
        with _resolved_blueprint(args.blueprint_dir, args.project) as paths:
            result = audit_blueprint(
                paths.blueprint_dir,
                lean_root=args.lean_root,
                _expected_blueprint_identity=paths.blueprint_identity,
                _expected_roadmap_identity=paths.roadmap_identity,
            )
            paths.verify()
    except RuntimeProjectionError as error:
        if args.json:
            print(json.dumps({"clean": False, "errors": list(error.issues)}, sort_keys=True, separators=(",", ":")))
        else:
            for issue in error.issues:
                print(f"error: {issue}")
        return 1
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
    result = diagnose_project(
        args.project_or_blueprint,
        lean_root=args.lean_root,
        project_id=args.project,
    )
    if args.json:
        print(result.to_json())
    else:
        for check in result.checks:
            marker = "PASS" if check.ok else "FAIL"
            print(f"{marker}: {check.name}: {check.detail}")
    return 0 if result.clean else 1


def _ready(args: argparse.Namespace) -> int:
    try:
        result = list_ready_work(
            args.project_or_blueprint,
            lean_root=args.lean_root,
            project_id=args.project,
        )
    except ExecutionInputError as error:
        if args.json:
            print(
                json.dumps(
                    {
                        "blocked_items": [],
                        "errors": [
                            {"code": issue.code, "reason": issue.reason}
                            for issue in error.issues
                        ],
                        "items": [],
                        "schema": READY_SCHEMA,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            for issue in error.issues:
                print(f"error: {issue.code}: {issue.reason}")
        return 1
    if args.json:
        print(result.to_json())
        return 0
    for item in result.items:
        print(
            f"READY: {item.phase}: {item.article_id}: "
            f"{item.node_id} ({item.article_path})"
        )
    if not result.items:
        print("BLOCKED: no ready work" if result.blocked_items else "OK: no ready work")
    for item in result.blocked_items:
        dependencies = ", ".join(item.blocked_by) or "none"
        print(
            f"BLOCKED: {item.phase}: {item.article_id}: {item.node_id} "
            f"({item.article_path}): {', '.join(item.reasons)}; blocked by: {dependencies}"
        )
    print(
        f"    {len(result.items)} ready · {result.blocked} blocked · "
        f"{result.complete} complete"
    )
    return 0


@contextmanager
def _resolved_blueprint(
    target: str | Path,
    project_id: str | None,
) -> Iterator[RuntimePaths]:
    with bind_runtime_paths(target, project_id=project_id) as paths:
        yield paths


def _project(args: argparse.Namespace) -> int:
    try:
        if args.project_command == "new":
            result = create_project(
                args.target,
                package=args.package,
                release_id=args.release,
                autoform_source=args.autoform_source,
                autoform_ref=args.autoform_ref,
            )
            if args.json:
                print(result.to_json())
            else:
                print(f"Created {result.package} at {result.target} ({result.release})")
                if not result.workflows_pinned:
                    print("warning: workflows were omitted because no immutable Autoform pin was available")
            return 0
        if args.project_command == "repair":
            result = repair_project(
                args.target,
                dry_run=args.dry_run,
                title=args.title,
                repository_url=args.repository_url,
                autoform_source=args.autoform_source,
                autoform_ref=args.autoform_ref,
            )
            if args.json:
                print(result.to_json())
            else:
                action = "Would add" if result.dry_run else "Added"
                print(f"{action} {len(result.planned if result.dry_run else result.written)} file(s)")
                for path in result.planned if result.dry_run else result.written:
                    print(f"  {path}")
            return 0
        if args.project_command == "inspect":
            result = inspect_project(args.target)
            if args.json:
                print(result.to_json())
            else:
                _print_project_inspection(result)
            return 0 if result.ok else 1
        if args.project_command == "versions":
            catalog = load_release_catalog()
            if args.json:
                print(catalog.to_json())
            else:
                print("Supported Lean/Mathlib releases:")
                for release in catalog.releases:
                    suffix = " [recommended]" if release.recommended else ""
                    print(f"  {release.id}{suffix}")
                    print(f"    Lean: {release.lean.toolchain}")
                    print(f"    Mathlib: {release.mathlib.revision} ({release.mathlib.git})")
            return 0
        if args.project_command == "provenance":
            result = verify_plugin_provenance()
            if args.json:
                print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
            else:
                print(f"Source: {result.source}")
                print(f"Revision: {result.revision}")
            return 0
    except ProjectRepairError as error:
        if getattr(args, "json", False):
            print(error.to_json())
        else:
            print(f"error[{error.code}]: {error.message}", file=sys.stderr)
            for conflict in error.conflicts:
                location = f" {conflict.path}" if conflict.path else ""
                print(f"  {conflict.code}{location}: {conflict.message}", file=sys.stderr)
            if error.written:
                print("  files already published:", file=sys.stderr)
                for path in error.written:
                    print(f"    {path}", file=sys.stderr)
        return 1
    except ProjectCreateError as error:
        if getattr(args, "json", False):
            print(error.to_json())
        else:
            print(f"error[{error.code}]: {error.message}", file=sys.stderr)
        return 1
    except ProjectCatalogError:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "error": {
                            "code": "project-catalog-invalid",
                            "message": "The bundled project release catalog is invalid.",
                        },
                        "ok": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            print("error: bundled project release catalog is invalid", file=sys.stderr)
        return 1
    except ProvenanceError as error:
        if getattr(args, "json", False):
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
    return 2


def _print_project_inspection(result) -> None:
    if result.project_root is not None:
        print(f"Project: {result.project_root}")
    if result.lake is not None:
        package = result.lake.name or "unknown package"
        version = f" {result.lake.version}" if result.lake.version else ""
        print(f"Lake: {package}{version} ({result.lake.path})")
        for target in result.lake.targets:
            source_parts = [
                part
                for part in (result.lake.package_src_dir, target.src_dir)
                if part is not None
            ]
            source = PurePosixPath(*source_parts).as_posix() if source_parts else "."
            modules = target.roots or ((target.root,) if target.root is not None else ())
            module_note = f", roots: {', '.join(modules)}" if modules else ""
            print(f"  {target.kind} {target.name} (srcDir: {source}{module_note})")
    if result.lean is not None:
        print(f"Lean: {result.lean.toolchain}")
    if result.mathlib is not None:
        print(f"Mathlib: {result.mathlib.revision or 'none'} ({result.mathlib.git or 'none'})")
    if result.autoform.manifest_path is not None:
        print(f"Autoform workspace: {result.autoform.manifest_path}")
        for path in result.autoform.blueprint_paths:
            print(f"  blueprint: {path}")
    elif result.autoform.blueprint_path is not None:
        print(f"Autoform blueprint: {result.autoform.blueprint_path}")
    print(
        f"Compatibility: {result.compatibility.status}"
        + (f" ({result.compatibility.release})" if result.compatibility.release else "")
    )
    for diagnostic in result.diagnostics:
        location = f" {diagnostic.path}" if diagnostic.path else ""
        print(
            f"{diagnostic.severity}[{diagnostic.code}]{location}: {diagnostic.message}",
            file=sys.stderr,
        )


def _claim(args: argparse.Namespace) -> int:
    try:
        operation = args.claim_command
        if operation == "list":
            board = _claim_board(args, require_identity=False)
            print(json.dumps(board.list(), sort_keys=True, separators=(",", ":")))
            return 0
        if operation == "cleanup":
            canonical_keys = None
            board_identity = None
            if args.blueprint is not None:
                try:
                    with bind_runtime_paths(args.blueprint, project_id=args.project) as paths:
                        paths.require_strong_binding(operation="claim cleanup")
                        blueprint = paths.blueprint_dir
                        project_pin = _PinnedDirectory.capture(
                            paths.project_root,
                            label="claim project",
                        )
                        blueprint_pin = _PinnedDirectory.capture(
                            blueprint,
                            label="claim blueprint",
                        )
                        graph = load_bound_graph(paths)
                        board_identity = _resolve_claim_board_identity(
                            args,
                            context=paths.project_root,
                            require_identity=False,
                        )
                        project_pin.verify(label="claim project")
                        blueprint_pin.verify(label="claim blueprint")
                        key_factory = (
                            (lambda article_id: workspace_author_claim_key(
                                paths.workspace_project_id, article_id
                            ))
                            if paths.workspace_project_id is not None
                            else author_claim_key
                        )
                        canonical_keys = tuple(
                            key_factory(node.article_id)
                            for node in graph.nodes.values()
                            if node.article_id is not None
                        )
                        paths.verify()
                except RuntimeProjectionError as exc:
                    raise ValueError(str(exc)) from exc
                except GraphValidationError as exc:
                    raise ValueError("; ".join(exc.issues)) from exc
            board = _claim_board(
                args,
                identity=board_identity,
                require_identity=False,
            )
            print(
                f"recovered {board.cleanup(canonical_keys=canonical_keys)} "
                "expired or unsafe-timestamp claim(s)"
            )
            return 0

        target = _resolve_claim_target(args)
        board = _claim_board(args, identity=target.board_identity)
        if operation in {"acquire", "renew"} and target.compatibility_keys:
            if not board.prepare_v2_claim(
                target.key,
                target.compatibility_keys,
                canonical_keys=target.canonical_keys,
            ):
                print(
                    f"error: could not {operation} {target.label}; "
                    "a live legacy v1 claim or incompatible path claim blocks v2 rollout"
                )
                return 1
        if operation == "acquire":
            succeeded = board.acquire(target.key, ttl=args.ttl, note=args.note)
        elif operation == "renew":
            succeeded = board.renew(target.key, ttl=args.ttl)
        else:
            succeeded = board.release(target.key)
        if succeeded:
            past_tense = {"acquire": "acquired", "renew": "renewed", "release": "released"}
            print(f"{past_tense[operation]} {target.label} ({target.key})")
            return 0
        print(f"error: could not {operation} {target.label}; ownership is held or unverifiable")
        return 1
    except (ClaimTransportError, ValueError) as exc:
        print(f"error: {exc}")
        return 1


def _migrate(args: argparse.Namespace) -> int:
    if args.migrate_command != "article-ids":
        return 2
    try:
        with _resolved_blueprint(args.blueprint_dir, args.project) as paths:
            graph = load_bound_graph(paths)
            plan = plan_article_ids(paths.blueprint_dir, _graph=graph)
            paths.verify()
    except (GraphValidationError, RuntimeProjectionError) as error:
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


def _resolve_claim_board_identity(
    args: argparse.Namespace,
    *,
    context: str | Path | None = None,
    require_identity: bool = True,
) -> _ClaimBoardIdentity:
    repo = args.repo
    session_id = args.session_id
    context_pin = None
    if repo is None or (require_identity and session_id is None):
        if context is None:
            context = getattr(args, "blueprint", None) or "."
        context = Path(context).expanduser().resolve()
        context_pin = _PinnedDirectory.capture(context, label="claim context")
    if repo is None:
        assert context is not None
        repo = _origin_url(context)
    if session_id is None and require_identity:
        assert context is not None
        session_id = _worktree_claim_session_id(context)
    if session_id is None:
        session_id = "claim-maintenance"
    if context_pin is not None:
        context_pin.verify(label="claim context")
    normalized_repo, repo_identity = pin_claim_repository(repo)
    scratch, scratch_identity = pin_claim_scratch(
        args.scratch or _default_claim_scratch(normalized_repo, session_id)
    )
    return _ClaimBoardIdentity(
        repo=normalized_repo,
        repo_identity=repo_identity,
        session_id=session_id,
        scratch=scratch,
        scratch_identity=scratch_identity,
    )


def _claim_board(
    args: argparse.Namespace,
    *,
    identity: _ClaimBoardIdentity | None = None,
    require_identity: bool = True,
) -> ClaimBoard:
    worker_id = args.worker_id or ("claim-maintenance" if not require_identity else None)
    if worker_id is None:
        raise ValueError("--worker-id or AUTOFORM_WORKER_ID is required")
    identity = identity or _resolve_claim_board_identity(args, require_identity=require_identity)
    return ClaimBoard(
        identity.repo,
        worker_id,
        identity.scratch,
        session_id=identity.session_id,
        expected_object_format=args.object_format,
        expected_repo_identity=identity.repo_identity,
        expected_scratch_identity=identity.scratch_identity,
    )


def _resolve_claim_target(
    args: argparse.Namespace,
) -> _ResolvedClaimTarget:
    article_target = args.node_id
    resource = args.resource
    if article_target and resource:
        raise ValueError("article target and --resource are mutually exclusive")
    if resource:
        if ARTICLE_ID_PATTERN.fullmatch(resource):
            raise ValueError("resource names must not use the reserved article_id format")
        identity = _resolve_claim_board_identity(args)
        return _ResolvedClaimTarget(
            resource_claim_key(resource),
            resource,
            (author_claim_key(resource),),
            (),
            identity,
        )
    if not article_target:
        raise ValueError("an article target or --resource is required")

    try:
        with bind_runtime_paths(args.blueprint, project_id=args.project) as paths:
            paths.require_strong_binding(operation="claim mutation")
            blueprint = paths.blueprint_dir
            project_pin = _PinnedDirectory.capture(paths.project_root, label="claim project")
            blueprint_pin = _PinnedDirectory.capture(blueprint, label="claim blueprint")
            graph = load_bound_graph(paths)
            matches = [
                node
                for node in graph.nodes.values()
                if article_target == node.id or article_target == node.article_id
            ]
            if not matches:
                if article_target == "lake-build":
                    raise ValueError(
                        f"article target {article_target!r} does not exist in {blueprint}; "
                        "use --resource lake-build for the shared build lock"
                    )
                raise ValueError(
                    f"article target {article_target!r} does not exist in {blueprint}"
                )
            if len(matches) != 1:
                matching_paths = ", ".join(sorted(node.id for node in matches))
                raise ValueError(
                    f"article target {article_target!r} is ambiguous: {matching_paths}"
                )
            node = matches[0]
            if node.article_id is None:
                raise ValueError(
                    f"article {node.id!r} has no durable article_id; "
                    f"run 'autoform migrate article-ids {blueprint}' and add the proposed ID"
                )
            if paths.workspace_project_id is None:
                key = author_claim_key(node.article_id)
                compatibility_keys = (author_claim_key(node.id),)
                canonical_keys = tuple(
                    author_claim_key(candidate.article_id)
                    for candidate in graph.nodes.values()
                    if candidate.article_id is not None
                )
            else:
                key = workspace_author_claim_key(paths.workspace_project_id, node.article_id)
                compatibility_keys = (
                    author_claim_key(node.article_id),
                    author_claim_key(node.id),
                )
                canonical_keys = tuple(
                    workspace_author_claim_key(
                        paths.workspace_project_id,
                        candidate.article_id,
                    )
                    for candidate in graph.nodes.values()
                    if candidate.article_id is not None
                )
            identity = _resolve_claim_board_identity(args, context=paths.project_root)
            project_pin.verify(label="claim project")
            blueprint_pin.verify(label="claim blueprint")
            paths.verify()
            return _ResolvedClaimTarget(
                key,
                node.id,
                compatibility_keys,
                canonical_keys,
                identity,
            )
    except RuntimeProjectionError as exc:
        raise ValueError(str(exc)) from exc
    except GraphValidationError as exc:
        raise ValueError("; ".join(exc.issues)) from exc


def _origin_url(project_or_blueprint: str | Path = ".") -> str:
    target = Path(project_or_blueprint).expanduser().resolve()
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "config",
                "--local",
                "--no-includes",
                "--get",
                "remote.origin.url",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            env=_claim_git_environment(),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError("--repo is required outside a Git checkout with an origin remote") from exc
    origin = result.stdout.strip()
    if not claim_repository_is_remote(origin):
        origin_path = Path(origin).expanduser()
        if not origin_path.is_absolute():
            try:
                root_result = subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                    env=_claim_git_environment(),
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                raise ValueError("could not resolve the relative origin repository") from exc
            origin_path = Path(root_result.stdout.strip()) / origin_path
        return str(origin_path.resolve())
    return origin


def _worktree_claim_session_id(project_or_blueprint: str | Path = ".") -> str:
    target = Path(project_or_blueprint).expanduser().resolve()
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "rev-parse",
                "--show-toplevel",
                "--absolute-git-dir",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            env=_claim_git_environment(),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            "--session-id or AUTOFORM_CLAIM_SESSION_ID is required outside a Git worktree"
        ) from exc
    lines = result.stdout.splitlines()
    if len(lines) != 2:
        raise ValueError("could not determine a stable Git worktree identity")
    root = Path(lines[0]).resolve()
    git_dir = Path(lines[1]).resolve()
    try:
        root_stat = root.stat(follow_symlinks=False)
        git_dir_stat = git_dir.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError("could not inspect the Git worktree identity") from exc
    token = _worktree_claim_token(git_dir)
    identity = (
        f"{socket.gethostname()}\0{token}\0{root_stat.st_dev}:{root_stat.st_ino}"
        f"\0{git_dir_stat.st_dev}:{git_dir_stat.st_ino}"
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"worktree-{digest}"


def _worktree_claim_token(git_dir: Path) -> str:
    token_path = git_dir / "autoform-claim-session"
    stored_token = _read_worktree_claim_token(token_path)
    if stored_token is not None:
        return stored_token

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    token = secrets.token_hex(32)
    temporary_path = git_dir / f".autoform-claim-session-{secrets.token_hex(16)}"
    try:
        descriptor = os.open(temporary_path, flags, 0o600)
    except OSError as exc:
        raise ValueError("could not create the Git worktree claim identity") from exc
    try:
        try:
            os.write(descriptor, f"{token}\n".encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary_path, token_path, follow_symlinks=False)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ValueError("could not install the Git worktree claim identity") from exc
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass

    stored_token = _read_worktree_claim_token(token_path)
    if stored_token is None:
        raise ValueError("could not install the Git worktree claim identity")
    return stored_token


def _read_worktree_claim_token(token_path: Path) -> str | None:
    read_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        read_flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(token_path, read_flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("could not read the Git worktree claim identity") from exc
    try:
        try:
            token_info = os.fstat(descriptor)
            path_info = token_path.stat(follow_symlinks=False)
            raw_token = os.read(descriptor, 256)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ValueError("could not read the Git worktree claim identity") from exc
    if not stat.S_ISREG(token_info.st_mode):
        raise ValueError("Git worktree claim identity must be a regular file")
    if (token_info.st_dev, token_info.st_ino) != (path_info.st_dev, path_info.st_ino):
        raise ValueError("Git worktree claim identity changed while it was read")
    try:
        stored_token = raw_token.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("Git worktree claim identity is malformed") from exc
    if len(raw_token) != token_info.st_size or not re.fullmatch(
        r"[0-9a-f]{64}\n?",
        stored_token,
    ):
        raise ValueError("Git worktree claim identity is malformed")
    return stored_token.rstrip("\n")


def _default_claim_scratch(repo: str, session_id: str) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    identity = hashlib.sha256(f"{repo}\0{session_id}\0{socket.gethostname()}".encode()).hexdigest()[:24]
    return cache / "autoform" / "claims" / identity


def _render(args: argparse.Namespace) -> int:
    try:
        with bind_runtime_paths(args.blueprint_dir, project_id=args.project) as paths:
            blueprint_dir = paths.blueprint_dir
            output = args.output
            if output is None:
                output = (
                    str(Path("site-src") / paths.workspace_project_id)
                    if paths.workspace_project_id is not None
                    else "site-src"
                )
            report = render_site(
                blueprint_dir,
                output,
                lean_root=args.lean_root,
                repository_url=args.repository_url,
                ref=args.ref,
                _expected_blueprint_identity=paths.blueprint_identity,
                _expected_roadmap_identity=paths.roadmap_identity,
            )
            paths.verify()
    except (GraphValidationError, PublicationError, RuntimeProjectionError) as exc:
        for issue in exc.issues:
            print(f"error: {issue}")
        return 1

    print(f"{report.output_dir}: {report.pages} pages, {report.nodes} nodes, {report.linked} code links")
    for issue in report.unresolved:
        print(f"warning: declaration not found in the Lean sources: {issue}")
    for issue in report.warnings:
        print(f"warning: {issue}")
    if report.unresolved and args.require_declarations:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
