"""CLI handlers for manifest-managed Autoform workspaces."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .graph import GraphValidationError
from .lean import build_linker, declaration_names
from .runtime import RuntimeProjectionError, bind_runtime_paths, load_bound_graph
from .workspace import (
    WorkspaceDiagnostic,
    WorkspaceInspection,
    discover_workspace,
    inspect_workspace,
)
from .workspace_manifest import (
    BLUEPRINT_LIST_SCHEMA,
    WORKSPACE_CHECK_SCHEMA,
    WORKSPACE_ERROR_SCHEMA,
    WORKSPACE_FILE,
    WorkspaceError,
)
from .workspace_mutation import (
    create_blueprint_project,
    initialize_workspace,
    register_blueprint_project,
)


def add_workspace_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Register workspace and blueprint commands on the main parser."""

    workspace = subparsers.add_parser(
        "workspace", help="configure and inspect a multi-project Autoform workspace"
    )
    workspace_subparsers = workspace.add_subparsers(dest="workspace_command", required=True)
    workspace_init = workspace_subparsers.add_parser(
        "init", help="create a root .autoform.toml without creating a blueprint"
    )
    workspace_init.add_argument(
        "target", nargs="?", default=".", help="repository root (default: current directory)"
    )
    workspace_init.add_argument(
        "--blueprint-root",
        required=True,
        help="repository-relative directory that will contain blueprint projects",
    )
    workspace_init.add_argument(
        "--location", default="blueprints", help="name for the blueprint location"
    )
    workspace_init.add_argument(
        "--json", action="store_true", help="write stable machine-readable output"
    )
    workspace_inspect = workspace_subparsers.add_parser(
        "inspect", help="inspect the root manifest and registered blueprint paths"
    )
    workspace_inspect.add_argument(
        "target",
        nargs="?",
        default=".",
        help="path inside the workspace (default: current directory)",
    )
    workspace_inspect.add_argument(
        "--json", action="store_true", help="write stable machine-readable output"
    )
    workspace_check = workspace_subparsers.add_parser(
        "check", help="validate every blueprint registered in the root manifest"
    )
    workspace_check.add_argument(
        "target",
        nargs="?",
        default=".",
        help="path inside the workspace (default: current directory)",
    )
    workspace_check.add_argument(
        "--lean-root", type=Path, help="Lean project to resolve declarations against"
    )
    workspace_check.add_argument(
        "--json", action="store_true", help="write stable machine-readable output"
    )

    blueprint = subparsers.add_parser(
        "blueprint", help="create and list blueprints registered in a workspace"
    )
    blueprint_subparsers = blueprint.add_subparsers(dest="blueprint_command", required=True)
    blueprint_new = blueprint_subparsers.add_parser(
        "new", help="create one vault and register it in .autoform.toml"
    )
    blueprint_new.add_argument("project_id", help="stable project id used by Autoform commands")
    blueprint_new.add_argument(
        "--workspace", default=".", help="path inside the workspace (default: current directory)"
    )
    blueprint_new.add_argument("--title", help="human title (default: project id)")
    blueprint_new.add_argument("--path", help="immediate child directory (default: project id)")
    blueprint_new.add_argument("--location", help="blueprint-capable location id")
    blueprint_new.add_argument(
        "--json", action="store_true", help="write stable machine-readable output"
    )
    blueprint_register = blueprint_subparsers.add_parser(
        "register", help="register an existing vault without changing its contents"
    )
    blueprint_register.add_argument(
        "project_id", help="stable project id used by Autoform commands"
    )
    blueprint_register.add_argument(
        "--workspace", default=".", help="path inside the workspace (default: current directory)"
    )
    blueprint_register.add_argument("--title", help="human title (default: project id)")
    blueprint_register.add_argument(
        "--path", required=True, help="existing immediate child directory"
    )
    blueprint_register.add_argument("--location", help="blueprint-capable location id")
    blueprint_register.add_argument(
        "--json", action="store_true", help="write stable machine-readable output"
    )
    blueprint_list = blueprint_subparsers.add_parser(
        "list", help="list blueprints registered in .autoform.toml"
    )
    blueprint_list.add_argument(
        "target",
        nargs="?",
        default=".",
        help="path inside the workspace (default: current directory)",
    )
    blueprint_list.add_argument(
        "--json", action="store_true", help="write stable machine-readable output"
    )


def run_workspace_command(args: argparse.Namespace) -> int:
    try:
        if args.workspace_command == "init":
            result = initialize_workspace(
                args.target,
                blueprint_root=args.blueprint_root,
                location_id=args.location,
            )
            if args.json:
                print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
            else:
                print(f"Created {result.manifest_path}")
                print(f"Blueprint location {result.location_id}: {result.blueprint_root}")
                print(
                    "Next: autoform blueprint new <project-id> "
                    f"--workspace {result.root} --title <title> --path <directory>"
                )
            return 0
        if args.workspace_command == "inspect":
            result = inspect_workspace(args.target)
            try:
                if args.json:
                    print(result.to_json())
                else:
                    print(f"Workspace: {result.workspace.root}")
                    print(
                        f"Manifest: {result.workspace.path.name} "
                        f"({result.workspace.manifest.schema})"
                    )
                    for location in result.workspace.manifest.locations:
                        capabilities = ", ".join(location.provides)
                        print(f"Location {location.id}: {location.path} [{capabilities}]")
                    for project in result.workspace.manifest.projects:
                        relative = result.workspace.blueprint_path(project).relative_to(
                            result.workspace.root
                        )
                        print(f"Project {project.id}: {relative.as_posix()}")
                    for diagnostic in result.diagnostics:
                        location = f" {diagnostic.path}" if diagnostic.path else ""
                        print(
                            f"{diagnostic.severity}[{diagnostic.code}]{location}: "
                            f"{diagnostic.message}",
                            file=sys.stderr,
                        )
                return 0 if result.ok else 1
            finally:
                result.workspace.close()
        if args.workspace_command == "check":
            return _check_workspace(args)
    except WorkspaceError as error:
        _print_workspace_error(error, json_output=getattr(args, "json", False))
        return 1
    return 2


def run_blueprint_command(args: argparse.Namespace) -> int:
    try:
        if args.blueprint_command == "new":
            result = create_blueprint_project(
                args.workspace,
                project_id=args.project_id,
                title=args.title or args.project_id,
                path=args.path,
                location_id=args.location,
            )
            if args.json:
                print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
            else:
                print(f"Created {result.project_id}: {result.blueprint_path}")
                print(f"Previous manifest retained at {result.manifest_backup_path}")
                for path in result.written:
                    print(f"  + {path}")
            return 0
        if args.blueprint_command == "list":
            workspace = discover_workspace(args.target)
            try:
                projects = [
                    {
                        "id": project.id,
                        "path": workspace.blueprint_path(project)
                        .relative_to(workspace.root)
                        .as_posix(),
                        "title": project.title,
                    }
                    for project in workspace.manifest.projects
                ]
                if args.json:
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "projects": projects,
                                "schema": BLUEPRINT_LIST_SCHEMA,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                else:
                    for project in projects:
                        title = f" — {project['title']}" if project["title"] else ""
                        print(f"{project['id']}: {project['path']}{title}")
                return 0
            finally:
                workspace.close()
        if args.blueprint_command == "register":
            result = register_blueprint_project(
                args.workspace,
                project_id=args.project_id,
                title=args.title,
                path=args.path,
                location_id=args.location,
            )
            if args.json:
                print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
            else:
                print(f"Registered {result.project_id}: {result.blueprint_path}")
                print(f"Previous manifest retained at {result.manifest_backup_path}")
            return 0
    except WorkspaceError as error:
        _print_workspace_error(error, json_output=getattr(args, "json", False))
        return 1
    return 2


def _check_workspace(args: argparse.Namespace) -> int:
    inspection = inspect_workspace(args.target)
    try:
        return _check_workspace_inspection(args, inspection)
    finally:
        inspection.workspace.close()


def _check_workspace_inspection(
    args: argparse.Namespace,
    inspection: WorkspaceInspection,
) -> int:
    workspace = inspection.workspace
    diagnostics = list(inspection.diagnostics)
    if not workspace.manifest.projects:
        diagnostics.append(
            WorkspaceDiagnostic(
                "error",
                "projects-empty",
                "Workspace verification requires at least one registered blueprint.",
                WORKSPACE_FILE,
            )
        )
    results: list[dict[str, object]] = []
    linker = None
    if args.lean_root is not None:
        try:
            linker = build_linker(args.lean_root)
        except OSError as error:
            diagnostics.append(
                WorkspaceDiagnostic(
                    "error",
                    "invalid-lean-root",
                    str(error),
                    str(args.lean_root),
                )
            )
    failed = any(item.severity == "error" for item in diagnostics)
    workspace.verify_root_binding()
    workspace.verify_managed_directory_snapshots()
    for project in workspace.manifest.projects:
        blueprint_dir = workspace.blueprint_path(project)
        expected_project_binding = workspace.project_binding_sha256(project)
        issues: list[str] = []
        articles = 0
        dependencies = 0
        try:
            with bind_runtime_paths(workspace.root, project_id=project.id) as paths:
                if (
                    paths.workspace_root_identity != workspace.root_identity
                    or paths.workspace_manifest_sha256 != workspace.manifest_sha256
                    or paths.workspace_project_binding_sha256 != expected_project_binding
                ):
                    raise RuntimeProjectionError(
                        ["workspace changed while registered projects were checked"]
                    )
                workspace.verify_root_binding()
                workspace.verify_managed_directory_snapshots()
                blueprint_dir = paths.blueprint_dir
                graph = load_bound_graph(paths)
                if linker is not None:
                    issues.extend(
                        f"{node.id}: declaration not found: {name}"
                        for node in graph.nodes.values()
                        for name in declaration_names(node.lean or "")
                        if linker.location(name) is None
                    )
                paths.verify()
                workspace.verify_managed_directory_snapshots()
                workspace.verify_root_binding()
        except (GraphValidationError, RuntimeProjectionError, WorkspaceError) as error:
            issues.extend(error.issues)
        else:
            articles = len(graph.nodes)
            dependencies = graph.edge_count
        failed = failed or bool(issues)
        results.append(
            {
                "articles": articles,
                "blueprint_path": blueprint_dir.relative_to(workspace.root).as_posix(),
                "dependencies": dependencies,
                "issues": issues,
                "ok": not issues,
                "project": project.id,
            }
        )
    workspace.verify_managed_directory_snapshots()
    workspace.verify_root_binding()

    if args.json:
        print(
            json.dumps(
                {
                    "diagnostics": [item.as_dict() for item in diagnostics],
                    "ok": not failed,
                    "projects": results,
                    "schema": WORKSPACE_CHECK_SCHEMA,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        for result in results:
            marker = "OK" if result["ok"] else "FAIL"
            print(
                f"{marker}: {result['project']}: {result['articles']} articles, "
                f"{result['dependencies']} dependencies ({result['blueprint_path']})"
            )
            for issue in result["issues"]:
                print(f"error: {result['project']}: {issue}")
        for diagnostic in diagnostics:
            location = f" {diagnostic.path}" if diagnostic.path else ""
            print(
                f"{diagnostic.severity}: {diagnostic.code}{location}: {diagnostic.message}",
                file=sys.stderr if diagnostic.severity == "error" else sys.stdout,
            )
    return 1 if failed else 0


def _print_workspace_error(error: WorkspaceError, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "errors": list(error.issues),
                    "ok": False,
                    "schema": WORKSPACE_ERROR_SCHEMA,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    for issue in error.issues:
        print(f"error: {issue}", file=sys.stderr)


__all__ = ["add_workspace_parsers", "run_blueprint_command", "run_workspace_command"]
