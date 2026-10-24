"""Command-line entry point for Autoform's project utilities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import debrief, debrief_orchestrator, status
from .approvals import ApprovalStatus, approval_statuses
from .article_identity import ArticleIdWriteError, plan_article_ids, write_article_ids
from .audit import audit_blueprint
from .claims import (
    CLAIM_TTL_S,
    ClaimBoard,
    ClaimContentionError,
    ClaimTimeoutError,
    ClaimTransportError,
    author_claim_key,
)
from .doctor import diagnose_project
from .dashboard import publication_bound_live_state, serve_dashboard
from .graph import Graph, GraphValidationError, atlas_omissions, catalogs_without_ledger, load_graph
from .graph_views import _countable, _module_inventories
from .impact import (
    IMPORT_CLOSURE_SCOPE,
    MODULE_IMPORT_SCOPE,
    ImpactError,
    format_impact,
    format_module_import_impact,
    module_import_impact,
    revision_impact,
)
from .lean import declaration_names, index_failure_message, index_project
from .library.citations import library_citation_warnings
from .library.generate import GenerateError, check_index, generate_index, write_index
from .library.index import INDEX_FILE
from .library.listing import indexed_packages, list_libraries
from .library.locate import LibraryError, WorkspaceError
from .library.search import LibraryResult, library_argument, search_with_libraries
from .markdown import local_target_issue
from .project import ProjectCatalogError, ProjectCreateError, create_project, inspect_project, load_release_catalog
from .readback import Readback, load_readbacks, prepare_readback, publish_readback, typesetting_warnings
from .render import SOURCES_DIR, PublicationError, publication_issues, render_site
from .review import (
    ReviewBundle,
    ReviewError,
    ReviewFinding,
    build_review_bundle,
    read_regular_file,
    read_review_packet,
    review_findings,
    review_states,
    write_review_packets,
)
from .runtime import RuntimeProjectionError, projection_omissions, resolve_runtime_paths
from .scaffold import ScaffoldError, scaffold_project
from .search import SearchError, search_blueprint, statement_preview
from .site_check import SiteCheckError, check_site
from .skeleton import (
    DEFAULT_PROBE_TIMEOUT,
    SkeletonError,
    extract_skeletons,
    format_report,
    write_packets,
    write_skeleton_report,
)
from .work import WORK_SCHEMA, WorkError, assumption_contract, list_ready_work, work_context


class _Version(argparse.Action):
    """Print the version only when asked, since reading a plugin checkout's commit runs git."""

    def __call__(
        self, parser: argparse.ArgumentParser, namespace: object, values: object, option: str | None = None
    ) -> None:
        print(f"{parser.prog} {debrief_orchestrator.autoform_version()}")
        parser.exit()


def main(argv: Sequence[str] | None = None) -> int:
    if os.name == "posix" and signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        # An ignored SIGCHLD, on Linux even one inherited across exec, has the kernel
        # reap every child, and Popen would report each exit status as 0. Autoform
        # waits for its own children.
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    parser = argparse.ArgumentParser(prog="autoform")
    parser.add_argument(
        "--version", action=_Version, nargs=0, default=argparse.SUPPRESS, help="show the version and exit"
    )
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
        help=(
            "full 40-character Autoform commit the workflows pin, not a branch, tag or short sha "
            "(default: this checkout's HEAD commit; none when --autoform-source is given)"
        ),
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="overwrite files that already exist; a case or Unicode variant of a managed path is still refused",
    )
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

    site = subparsers.add_parser("site", help="inspect a site MkDocs built from rendered source")
    site_subparsers = site.add_subparsers(dest="site_command", required=True)
    site_check = site_subparsers.add_parser(
        "check", help="report dead internal links, missing anchors and statement cards the build lost"
    )
    site_check.add_argument("site_dir", help="the built site: MkDocs's site_dir")
    site_check.add_argument(
        "--source",
        type=Path,
        help="the rendered source the site was built from (MkDocs's docs_dir), to check every statement card",
    )

    project = subparsers.add_parser("project", help="create or inspect local projects and supported releases")
    project_subparsers = project.add_subparsers(dest="project_command", required=True)
    project_new = project_subparsers.add_parser("new", help="atomically create a complete Lean and Autoform project")
    project_new.add_argument("target", nargs="?", help="new project directory (required); it must not exist")
    project_new.add_argument("--package", help="UpperCamelCase Lean package name (required)")
    project_new.add_argument("--release", help="release id from 'project versions' (default: the recommended release)")
    project_new.add_argument(
        "--lean-toolchain",
        help=(
            "Lean release tag such as v4.30.0, not with --release; below v4.27.0 it warns. A pair the catalog "
            "does not list is written without lake-manifest.json, so run 'lake update' in the project"
        ),
    )
    project_new.add_argument(
        "--mathlib-rev",
        help=(
            "Mathlib tag, branch, or commit to require with --lean-toolchain, not with --release "
            "(default: the same tag as the toolchain)"
        ),
    )
    project_new.add_argument(
        "--library",
        metavar="NAME",
        help=(
            "shared Lean library from the bundled library catalog to require at its pinned commit; "
            "it selects the release, so it combines with no version option"
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
    work_import_impact = work_subparsers.add_parser(
        "import-impact",
        help="report the claim and review scope of editing a Lean module's imports: what an added import can "
        "affect, not the modules a removed import can drop from the build",
    )
    work_import_impact.add_argument("selector", help="path-derived node id or durable article_id")
    work_import_impact.add_argument("module", help="project module whose imports may change")
    work_import_impact.add_argument(
        "target", nargs="?", default=".", help="project root or blueprint directory"
    )
    work_import_impact.add_argument(
        "--lean-root", type=Path, required=True, metavar="PATH", help="the built Lean project"
    )
    work_import_impact.add_argument("--json", action="store_true", help="write stable machine-readable output")
    work_import_impact.add_argument(
        "--timeout",
        type=_positive_seconds,
        metavar="SECONDS",
        help=f"seconds the Lean probe may run (default {DEFAULT_PROBE_TIMEOUT:g}); "
        "the Lake freshness check before it has its own budget",
    )
    work_import_impact.add_argument(
        "--module-scope",
        choices=(MODULE_IMPORT_SCOPE, IMPORT_CLOSURE_SCOPE),
        default=MODULE_IMPORT_SCOPE,
        metavar="SCOPE",
        help=f"{MODULE_IMPORT_SCOPE} (default) loads every root library; {IMPORT_CLOSURE_SCOPE} loads, and needs "
        "built, only files that may import MODULE: affected_modules stay exact, affected_articles may over-report",
    )
    search = subparsers.add_parser(
        "search", help="find articles by title, Lean name, or statement text before adding one"
    )
    search.add_argument("target", help="project root or blueprint directory")
    search.add_argument(
        "query",
        help="one quoted argument; every word, with filler such as 'of' dropped and common endings cut, "
        "must occur in an article's title, Lean name, path, or statement, or in a containing article's title; "
        "put -- before one starting with -",
    )
    search.add_argument("--lean-root", type=Path, help="resolve local Lean declaration targets")
    search.add_argument(
        "--state",
        action="append",
        default=[],
        dest="states",
        choices=[state.key for state in status.STATES],
        metavar="KEY",
        help="keep articles in exactly this derived state (repeatable): "
        + ", ".join(state.key for state in status.STATES),
    )
    search.add_argument(
        "--declaration",
        action="append",
        default=[],
        dest="declarations",
        metavar="KIND",
        help="keep articles whose frontmatter declaration is KIND: theorem, lemma, def, ... (repeatable)",
    )
    search.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="show at most N hits from the blueprint and from each --library (default 20)",
    )
    search.add_argument(
        "--library",
        action="append",
        default=[],
        dest="libraries",
        type=_library_argument,
        metavar="NAME[=INDEX]",
        help="also search the index a locked Lake package publishes (repeatable); needs --lean-root. "
        "INDEX reads the index from a file instead of the package's checkout",
    )
    search.add_argument("--json", action="store_true", help="write stable machine-readable output")

    library = subparsers.add_parser("library", help="inspect the shared Lean libraries a project depends on")
    library_subparsers = library.add_subparsers(dest="library_command", required=True)
    library_list = library_subparsers.add_parser(
        "list", help="list the locked Lake packages and whether each publishes an index search can use"
    )
    library_list.add_argument("target", type=Path, help="Lean project root, where lake-manifest.json is")
    library_list.add_argument("--json", action="store_true", help="write stable machine-readable output")
    library_index = library_subparsers.add_parser(
        "index", help="write the index a Lean library publishes, from its current build"
    )
    library_index.add_argument("target", type=Path, help="the library's package root; it must be built")
    library_index.add_argument(
        "--lib",
        action="append",
        default=[],
        dest="libraries",
        metavar="NAME",
        help="index this lean_lib (repeatable); default: every lean_lib of the package",
    )
    library_index.add_argument(
        "--check", action="store_true", help="write nothing; exit 1 when the index at the root is not current"
    )
    library_index.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=None,
        metavar="SECONDS",
        help=f"seconds the Lean probe may run (default {DEFAULT_PROBE_TIMEOUT:g})",
    )

    claim = subparsers.add_parser("claim", help="coordinate temporary node ownership through Git refs")
    claim_subparsers = claim.add_subparsers(dest="claim_command", required=True)
    for operation in ("acquire", "renew", "release"):
        command = claim_subparsers.add_parser(operation)
        command.add_argument("node_id", nargs="+", help="claim target(s); several change all-or-nothing")
        _add_claim_board_arguments(command)
        if operation in {"acquire", "renew"}:
            command.add_argument(
                "--ttl",
                type=int,
                default=CLAIM_TTL_S,
                help=f"seconds from now until the lease expires (default {CLAIM_TTL_S})",
            )
        if operation == "acquire":
            command.add_argument("--note", default="", help="free text stored in the lease; renew keeps it")
    claim_list = claim_subparsers.add_parser("list")
    claim_list.add_argument("node_id", nargs="*", help="list only these claim targets")
    _add_claim_board_arguments(claim_list)
    claim_list.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=120.0,
        metavar="SECONDS",
        help="deadline for the listing's Git commands (default: 120)",
    )
    claim_cleanup = claim_subparsers.add_parser("cleanup")
    _add_claim_board_arguments(claim_cleanup)

    debrief_parser = subparsers.add_parser("debrief", help="feedback forms agents fill in about their work")
    debrief_kinds = debrief_parser.add_subparsers(dest="debrief_kind", required=True)
    orchestrator = debrief_kinds.add_parser(
        "orchestrator", help="feedback on AutoformBot from the agent that led a Setup, Roadmap or Formalize run"
    )
    orchestrator_subparsers = orchestrator.add_subparsers(dest="debrief_command", required=True)
    for operation, help_text in (
        ("form", "print the form for a finished run (or report that orchestrator debriefs are disabled)"),
        ("record", "validate an answer, store it, and draft an issue for any AutoformBot friction"),
    ):
        command = orchestrator_subparsers.add_parser(operation, help=help_text)
        command.add_argument("target", nargs="?", default=".", help="project root or blueprint directory")
        command.add_argument("--skill", required=True, choices=debrief_orchestrator.SKILLS, help="skill that ran")
        if operation == "record":
            command.add_argument("--answer", type=Path, help="answer file (default: standard input)")
            command.add_argument("--json", action="store_true", help="write stable machine-readable output")
    subagent = debrief_kinds.add_parser(
        "subagent", help="opt-in form the agent that worked a formalization attempt fills in"
    )
    debrief_subparsers = subagent.add_subparsers(dest="debrief_command", required=True)
    for operation, help_text in (
        ("form", "print the form for a finished attempt (or report that subagent debriefs are disabled)"),
        ("record", "validate an answer and store it as a new debrief record"),
    ):
        command = debrief_subparsers.add_parser(operation, help=help_text)
        command.add_argument("selector", help="path-derived node id or durable article_id")
        command.add_argument("target", nargs="?", default=".", help="project root or blueprint directory")
        command.add_argument("--phase", required=True, choices=("statement", "proof"), help="phase attempted")
        command.add_argument("--note", default="", help="why the attempt did not succeed, if it did not")
        if operation == "record":
            command.add_argument("--answer", type=Path, help="answer file (default: standard input)")
            command.add_argument(
                "--worker-id",
                default=os.environ.get("AUTOFORM_WORKER_ID"),
                help="stable identity for this agent (or set AUTOFORM_WORKER_ID)",
            )
            command.add_argument("--json", action="store_true", help="write stable machine-readable output")
    debrief_render = debrief_subparsers.add_parser("render", help="rebuild Markdown views of the records")
    debrief_render.add_argument("target", nargs="?", default=".", help="project root or blueprint directory")

    migrate = subparsers.add_parser("migrate", help="plan or apply authored migrations")
    migrate_subparsers = migrate.add_subparsers(dest="migrate_command", required=True)
    article_ids = migrate_subparsers.add_parser(
        "article-ids",
        help="plan durable roadmap article identifiers, or add them with --write",
    )
    article_ids.add_argument("blueprint_dir")
    article_ids_mode = article_ids.add_mutually_exclusive_group()
    article_ids_mode.add_argument(
        "--check",
        action="store_true",
        help="fail when an article is missing article_id frontmatter",
    )
    article_ids_mode.add_argument(
        "--write",
        action="store_true",
        help="add each planned article_id to the frontmatter of the article missing one",
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
    _add_probe_timeout_argument(skeleton)

    review = subparsers.add_parser("review", help="prepare and verify statement-review evidence")
    review_subparsers = review.add_subparsers(dest="review_command", required=True)
    review_prepare = review_subparsers.add_parser(
        "prepare",
        help="write the blind packets of every Lean-mapped article and their manifest",
    )
    review_prepare.add_argument("blueprint_dir")
    review_prepare.add_argument("--lean-root", type=Path, required=True, help="the built Lean project")
    review_prepare.add_argument(
        "--packets", type=Path, required=True, metavar="DIR", help="directory to write the blind packets to"
    )
    _add_probe_timeout_argument(review_prepare)

    review_record = review_subparsers.add_parser(
        "record",
        help="validate and file testimony about one packet `review prepare` wrote",
    )
    review_record.add_argument("blueprint_dir")
    review_record.add_argument(
        "--packets", type=Path, required=True, metavar="DIR", help="blind packet directory from review prepare"
    )
    review_record.add_argument("--article-id", required=True, metavar="AF_ID", help="the article's article_id")
    review_record.add_argument(
        "--declaration", required=True, metavar="LEAN_NAME", help="the declaration whose packet was read back"
    )
    review_record.add_argument(
        "--testimony", type=Path, required=True, help="the read-back file, at most 32 KiB and 500 lines"
    )
    review_record.add_argument(
        "--model", required=True, help="label of the reviewing model; recorded, not authenticated"
    )
    review_record.add_argument(
        "--expected-card-hash",
        help="replace an existing different card only if its current content has this hash",
    )

    review_check = review_subparsers.add_parser(
        "check",
        help="check evidence freshness, read-backs, and human approvals",
    )
    review_check.add_argument("blueprint_dir")
    review_check.add_argument("--lean-root", type=Path, required=True, help="the built Lean project")
    review_check.add_argument("--json", action="store_true", help="write stable machine-readable output")
    _add_probe_timeout_argument(review_check)

    render = subparsers.add_parser("render", help="build the publishable blueprint")
    render.add_argument("blueprint_dir")
    render.add_argument("-o", "--output", default="site-src", help="output directory")
    render.add_argument("--lean-root", type=Path, help="Lean project to link code from")
    render.add_argument("--repository-url", help="project URL, e.g. https://github.com/owner/repo")
    render.add_argument(
        "--ref",
        help="commit or branch to link; code links appear only if its local commit holds "
        "the captured Lean sources, and links into the blueprint only if it also holds the blueprint",
    )
    render.add_argument(
        "--require-declarations",
        action="store_true",
        help="fail when a 'lean:' declaration is not found in the Lean sources, is defined more than once in them, "
        "or cannot be looked up because the sources cannot be read",
    )
    render.add_argument(
        "--review",
        action="store_true",
        help="add each statement's review, judged as review check judges it (needs --lean-root)",
    )
    _add_probe_timeout_argument(render)

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
    if args.command == "search":
        return _search(args)
    if args.command == "library":
        return _library_index(args) if args.library_command == "index" else _library_list(args)
    if args.command == "site":
        return _site_check(args)
    if args.command == "claim":
        return _claim(args)
    if args.command == "debrief":
        return _debrief(args)
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
        help="seconds the probe helper's build, and each Lean probe, may each run "
        f"(default {DEFAULT_PROBE_TIMEOUT:g}); "
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
        needs_pin = result.unpinned and path != ".github/CODEOWNERS.autoform.example" and ".github" in path
        note = "no Autoform ref to pin" if needs_pin else "exists, left alone"
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


def _byte_order_mark_warnings(graph: Graph) -> list[str]:
    """Name each article that starts with a byte-order mark, which hides frontmatter on its first line."""
    warnings = []
    for node in graph.nodes.values():
        try:
            with node.path.open("rb") as article:
                marked = article.read(3) == b"\xef\xbb\xbf"
        except OSError:
            continue
        if marked:
            warnings.append(
                f"{node.id}: starts with a byte-order mark, so frontmatter on its first line is read as text "
                "and its keys are ignored; remove the mark"
            )
    return warnings


def _source_link_issues(graph: Graph) -> tuple[list[str], list[str]]:
    """Name each ``## Sources`` link to a local path that is not a file in the blueprint, or is hidden.

    Returns the errors, then the warnings. The generated workflows gate on
    ``check`` and run ``audit`` only as a report, and render publishes a missing
    or escaping target broken: a page a strict MkDocs build cannot find, a
    repository URL that 404s for a hidden note under ``sources/``, or the link's
    text alone. Render omits hidden files, so a hidden target is refused too,
    unless it is a note under ``sources/`` that exists. Only a repository link
    reaches that note, and render writes one when it links the other source
    notes there, so it is only warned about: it must be committed. Fragments and
    other schemes stay with ``audit``, whose anchor check renders each cited
    note.
    """
    rejected = {
        "source-not-found": "does not resolve to a file",
        "source-escapes-blueprint": "escapes the blueprint directory",
        "malformed-source-url": "is not a valid URL",
        "malformed-source-link": "contains an invalid path",
        "hidden-source-target": "names a hidden path, which render omits",
        "generated-source-target": "names a generated view, which render omits",
    }
    issues, warnings = [], []
    for node in graph.nodes.values():
        for target in node.sources:
            located = target.partition("#")[0]
            issue = local_target_issue(node.path, located, graph.blueprint_dir, label="source")
            if issue is None or issue[0] not in rejected:
                continue
            code = issue[0]
            if code == "hidden-source-target":
                # A link that names a hidden note under sources/, and steps through no other
                # hidden folder on the way, only needs the note committed. No repository link
                # mends any other hidden link.
                written = unquote(urlsplit(located).path)
                inside = Path(os.path.normpath(node.path.parent.resolve() / written)).relative_to(graph.blueprint_dir)
                hidden = [part for part in Path(written).parts if part.startswith(".") and part != ".."]
                if inside.parts[:1] == (SOURCES_DIR,) and hidden == [p for p in inside.parts if p.startswith(".")]:
                    spelled = graph.blueprint_dir / inside
                    if os.path.isfile(spelled):
                        warnings.append(
                            f"{node.id}: source target names a hidden path, which only a repository link reaches, "
                            f"so it must be committed: {target!r}"
                        )
                        continue
                    code = "source-not-found"
            issues.append(f"{node.id}: source target {rejected[code]}: {target!r}")
    return issues, warnings


def _formalizable_container_warnings(graph: Graph) -> list[str]:
    """Name each article that sets ``declaration`` and also contains articles.

    The generated workflows gate on ``check`` and run ``audit``, which reports
    these as ``formalizable-container``, only as a report. Nothing counts or
    schedules a container's own declaration, yet its scope is drawn complete
    once its leaves are proved. The site still builds, so ``check`` warns rather
    than fails. The message names the move because deleting only
    ``declaration:`` silences both commands, and a container left with ``lean:``
    and ``statement:`` is still drawn complete while its own proof is open.
    """
    containers = {node.parent for node in graph.nodes.values() if node.parent is not None}
    return [
        f"{node.id}: formalizable article has contained articles; declaration-sized articles must be leaves; "
        "move the declaration and its lean, statement and proof fields into a leaf article"
        for node in graph.nodes.values()
        if node.formalizable and node.id in containers
    ]


def _check(args: argparse.Namespace) -> int:
    try:
        graph = load_graph(args.blueprint_dir)
    except GraphValidationError as exc:
        for issue in exc.issues:
            print(f"error: {issue}")
        return 1
    source_issues, source_warnings = _source_link_issues(graph)
    if source_issues:
        for issue in source_issues:
            print(f"error: {issue}")
        return 1
    if args.lean_root is not None:
        source_warnings += [_human_text(warning) for warning in library_citation_warnings(graph, args.lean_root)]

    # Check looks each name up and builds no permalink, so it skips the Git
    # work a linker does.
    index = None
    if args.lean_root is not None:
        try:
            index = index_project(args.lean_root)
        except OSError as error:
            print(f"error: {index_failure_message(error)}")
            return 1

    # The book's hero counts the same targets and inventories.
    statuses = status.derive(graph)
    target_statuses = {node_id: statuses[node_id] for node_id in _countable(graph)}
    inventories = len(_module_inventories(graph))
    summary = " · ".join(
        f"{count} {state.label}" for state, count in status.summarize(target_statuses)
    )
    print(f"OK: {len(graph.nodes)} articles, {graph.edge_count} dependencies")
    if summary:
        print(f"    {summary}")
    if inventories:
        label = "module inventory" if inventories == 1 else "module inventories"
        print(f"    {inventories} {label}")

    if index is not None:
        missing = [
            f"{node.id}: declaration defined more than once in {args.lean_root}: {name} ({ambiguous})"
            if (ambiguous := index.ambiguity(name))
            else f"{node.id}: declaration not found in {args.lean_root}: {name}"
            + ("; a name Lean generates is not indexed and belongs in the catalog's ledger" if node.catalog else "")
            for node in graph.nodes.values()
            for name in declaration_names(node.lean or "")
            if index.find(name) is None
        ]
        for issue in missing:
            print(f"error: {issue}")
        if any(": declaration not found in " in issue for issue in missing):
            print(
                "note: check resolves lean: names only in the project's own Lean files, not in Mathlib or other "
                "dependencies under .lake; if a name is a shared library's declaration, record it in the article's "
                "implementation note instead"
            )
        if missing:
            return 1
        print(f"    {len(graph.lean_names())} declaration name(s) resolved in the Lean sources")
    for omission in projection_omissions(graph):
        print(f"warning: {omission}")
    for omission in atlas_omissions(graph):
        print(f"warning: atlas.json: {omission}")
    for node_id in catalogs_without_ledger(graph):
        print(
            f"warning: {node_id}: module catalog links no declaration ledger under blueprint/sources/ from '## Sources'"
        )
    for warning in _byte_order_mark_warnings(graph) + _formalizable_container_warnings(graph) + source_warnings:
        print(f"warning: {warning}")
    # What the site would leave out or show as typed, and what would stop
    # render; none of it fails check.
    for warning in publication_issues(graph, Path(args.blueprint_dir)):
        print(f"warning: {warning}")
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
                f"{counts['INVENTORIED']} inventoried · "
                f"{counts['DEFERRED']} deferred · "
                f"{counts['OUT']} out"
            )
        for finding in result.findings:
            print(f"error: {finding.article_path}: {finding.code}: {finding.reason}")
        for warning in result.warnings:
            print(f"warning: {warning.article_path}: {warning.code}: {warning.message}")
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


def _site_check(args: argparse.Namespace) -> int:
    try:
        report = check_site(args.site_dir, args.source)
    except SiteCheckError as exc:
        print(f"error: {_human_text(exc)}")
        return 1

    print(f"{_human_text(args.site_dir)}: {report.pages} pages, {len(report.issues)} problem(s)")
    for issue in report.issues:
        print(f"warning: {_human_text(issue)}")
    # It informs: what it finds never fails the build.
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
                    library=args.library,
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
                raise ProjectCreateError("project-create-interrupted", message) from None
            if args.json:
                print(result.to_json())
            else:
                label = result.release or f"unlisted: {result.lean_toolchain}, Mathlib {result.mathlib_rev}"
                print(_ascii_text(f"Created {result.package} at {result.target} ({label})"))
                if result.library is not None:
                    library = result.library
                    print(_ascii_text(f"Library: {library.name} @ {library.commit} ({library.git})"))
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
        return 130 if error.code == "project-create-interrupted" else 1
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
    if args.work_command in {"impact", "import-impact"}:
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


def _search(args: argparse.Namespace) -> int:
    # Only reading the blueprint and the libraries can fail on the project's
    # paths; printing the result stays outside, so an output error is not
    # reported as one.
    options = {
        "lean_root": args.lean_root,
        "states": args.states,
        "declarations": args.declarations,
        "limit": args.limit,
    }
    libraries: tuple[LibraryResult, ...] = ()
    try:
        if args.libraries:
            # A library that cannot be verified refuses the whole call: the
            # blueprint hits alone would read as "this result is new".
            combined = search_with_libraries(args.target, args.query, libraries=args.libraries, **options)
            result, libraries = combined.blueprint, combined.libraries
        else:
            combined = None
            result = search_blueprint(args.target, args.query, **options)
    except (GraphValidationError, RuntimeProjectionError) as error:
        for issue in error.issues:
            print(f"error: {_human_text(issue)}", file=sys.stderr)
        return 2
    except (SearchError, LibraryError, WorkspaceError) as error:
        print(f"error: {_human_text(error)}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError):
        print("error: project, blueprint, or Lean root path cannot be read", file=sys.stderr)
        return 2

    notes = []
    if result.unused_declarations:
        kinds = ", ".join(result.unused_declarations)
        notes.append(_human_text(f"note: no article declares kind {kinds}, so the search used only the other kinds"))
    note = None if args.libraries else _unsearched_library_note(args.target, args.lean_root)
    if note:
        notes.append(note)
    if args.json:
        print(result.to_json() if combined is None else combined.to_json())
        for note in notes:
            print(note, file=sys.stderr)
        return 0
    # Every count comes first, and each hit's statement on its first line, so output cut short still shows them.
    if result.hits:
        print(f"{len(result.hits)} of {result.total_matches} matching article(s) shown.")
    else:
        print("No matching articles.")
    for library in libraries:
        if library.hits:
            shown = f"{len(library.hits)} of {library.total_matches} matching declaration(s) shown."
            print(_human_text(f"Library {library.name}: {shown}"))
        else:
            print(_human_text(f"Library {library.name}: no matching declarations."))
    for hit in result.hits:
        durable = f" [{hit.article_id}]" if hit.article_id else ""
        preview = statement_preview(hit)
        statement = f": {preview}" if preview else ""
        print(_human_text(f"{hit.title} ({hit.node_id}){durable}{statement}"))
        summary = [
            hit.declaration or "no declaration",
            hit.state,
            f"used by {hit.used_by_count}",
        ]
        if hit.shared_title:
            summary.append("title shared with another article")
        print(_human_text("  " + ", ".join(summary)))
        for target in hit.lean_targets:
            location = f" ({target.source_file}:{target.line})" if target.source_file else ""
            print(_human_text(f"  Lean: {target.declaration}{location}"))
        if hit.mathlib_declarations:
            print(_human_text("  Mathlib: " + ", ".join(hit.mathlib_declarations)))
        print("  Matched: " + ", ".join(hit.matched_fields))
    for library in libraries:
        _print_library(library)
    if notes:
        sys.stdout.flush()
        for note in notes:
            print(note, file=sys.stderr)
    return 0


def _unsearched_library_note(target: str, lean_root: str | None) -> str | None:
    """Name the locked packages that publish an index a blueprint-only search skipped.

    An empty blueprint answers every query with no match, which reads as "this
    result is new" although a library the project depends on may prove it.
    The note is best effort: any failure to read the project leaves it out.
    """

    candidates = [Path(lean_root)] if lean_root else [Path(target), Path(target).parent]
    for root in candidates:
        try:
            if not (root / "lake-manifest.json").is_file():
                continue
            names = indexed_packages(root)
        except (LibraryError, WorkspaceError, OSError, ValueError):
            return None
        if not names:
            return None
        listed = " and ".join(names)
        flags = " ".join(f"--library {name}" for name in names)
        verb, pronoun = ("publishes a search index", "it") if len(names) == 1 else ("publish search indexes", "them")
        return _human_text(
            f"note: {listed} {verb} this search did not read; search {pronoun} too with --lean-root {root} {flags}"
        )
    return None


def _print_library(library: LibraryResult) -> None:
    print(_human_text(f"Library {library.name} @ {library.revision[:12]}"))
    for difference in library.differences:
        what = difference.what if difference.name is None else difference.name
        if difference.project is None:
            held = "the project has no lean-toolchain" if difference.name is None else "the project does not lock it"
        else:
            held = f"project: {difference.project}"
        print(_human_text(f"  differs: {what} {difference.library} ({held})"))
    for hit in library.hits:
        declaration = hit.declaration
        heading = f"{declaration.name} ({declaration.kind}, {declaration.status})"
        signature = _one_line(declaration.signature)
        print(_human_text(f"{heading}: {signature}" if signature else heading))
        print(_human_text(f"  {hit.source_file}:{declaration.line}"))
        if hit.import_line is not None:
            print(_human_text(f"  {hit.import_line}"))
        if declaration.docstring:
            print(_human_text(f"  Docstring: {_one_line(declaration.docstring)}"))
        print("  Matched: " + ", ".join(hit.matched_fields))


def _one_line(text: str) -> str:
    """Cut library text to what fits one report line; the JSON output carries it whole."""

    text = " ".join(text.split())
    return text if len(text) <= 200 else text[:197].rstrip() + "..."


def _library_index(args: argparse.Namespace) -> int:
    options = {"libraries": tuple(args.libraries), "timeout": args.timeout}
    try:
        if args.check:
            reasons = check_index(args.target, **options)
            for reason in reasons:
                print(f"stale: {_human_text(reason)}", file=sys.stderr)
            if not reasons:
                print(f"{INDEX_FILE} is current.")
            return 1 if reasons else 0
        generated = generate_index(args.target, **options)
        write_index(args.target, generated)
    except GenerateError as error:
        print(f"error: {_human_text(error)}", file=sys.stderr)
        return 2
    except OSError as error:
        # Every write is reported by `write_index`; what is left is a file of the package that cannot be read.
        what = error.filename if error.filename is not None else args.target
        print(f"error: {_human_text(f'{what} cannot be read: {error.strerror or error}')}", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"error: {_human_text(f'the package root cannot be used: {error}')}", file=sys.stderr)
        return 2
    index = generated.index
    wanted = sum(declaration.status == "wanted" for declaration in index.declarations)
    twice = f", {len(generated.shared)} declared in more than one module" if generated.shared else ""
    print(
        f"Wrote {INDEX_FILE}: {_count(len(index.modules), 'module')}, "
        f"{_count(len(index.declarations), 'declaration')} ({wanted} wanted{twice})."
    )
    # Flush first: stdout is block-buffered when piped, and the warnings explain the count.
    sys.stdout.flush()
    for shared in generated.shared:
        print(
            _human_text(
                f"warning: {shared.name} is declared in {shared.module} and also in {', '.join(shared.others)}; "
                f"it is indexed once, under {shared.module}"
            ),
            file=sys.stderr,
        )
    for shared in generated.shared_left_out:
        print(
            _human_text(
                f"warning: {shared.name} is left out: its declaration in {shared.module} depends on sorry or "
                f"another axiom; {', '.join(shared.others)} {'declares' if len(shared.others) == 1 else 'declare'} it too"
            ),
            file=sys.stderr,
        )
    if generated.left_out:
        verb = "depends" if generated.left_out == 1 else "depend"
        print(
            f"Left out {_count(generated.left_out, 'declaration')} that {verb} on sorry or another axiom.",
            file=sys.stderr,
        )
    return 0


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _library_list(args: argparse.Namespace) -> int:
    try:
        listing = list_libraries(args.target)
    except WorkspaceError as error:
        print(f"error: {_human_text(error)}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError):
        print("error: Lean root path cannot be read", file=sys.stderr)
        return 2

    if args.json:
        print(listing.to_json())
        return 0
    if not listing.packages:
        print("No locked packages.")
        return 0
    for package in listing.packages:
        where = package.type if package.revision is None else f"{package.type} {package.revision[:12]}"
        verdict = "usable" if package.usable else f"not usable: {package.reason}"
        print(_human_text(f"{package.name}  {where}  {verdict}"))
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
    # A conditional article is proved, so the loader made its `lean:` name a
    # declaration, and the contract lists it.
    for article in contract.articles:
        assumes = f"assumes {', '.join(article.assumes)}"
        if article.open:
            # An open statement is not conditional: its own proof is missing.
            line = f"open: {article.id} ({', '.join(article.declarations)})"
            print(_human_text(f"{line} {assumes}" if article.assumes else line))
        elif article.state == "conditional":
            print(_human_text(f"conditional: {article.id} {assumes}"))
        elif article.assumes:
            # Not proved, so nothing is conditional yet; its proof would rest on these.
            print(_human_text(f"unproved: {article.id} {assumes}"))
    return 0


def _work_impact(args: argparse.Namespace) -> int:
    try:
        if args.work_command == "impact":
            report = revision_impact(
                args.target,
                args.selector,
                lean_root=args.lean_root,
                declarations=args.declarations,
                timeout=args.timeout,
            )
        else:
            report = module_import_impact(
                args.target,
                args.selector,
                args.module,
                lean_root=args.lean_root,
                timeout=args.timeout,
                module_scope=args.module_scope,
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
    lines = format_impact(report) if args.work_command == "impact" else format_module_import_impact(report)
    for line in lines:
        print(_human_text(line))
    return 0


def _read_answer(args: argparse.Namespace) -> str:
    if args.answer is not None:
        with args.answer.open("rb") as handle:
            raw = handle.read(debrief.MAX_ANSWER_BYTES + 1)
    else:
        raw = sys.stdin.buffer.read(debrief.MAX_ANSWER_BYTES + 1)
    return raw.decode("utf-8", "replace")


def _debrief(args: argparse.Namespace) -> int:
    if args.debrief_kind == "orchestrator":
        return _debrief_orchestrator(args)
    try:
        if args.debrief_command == "render":
            root = debrief.debrief_root(args.target)
            written = debrief.render(root)
            print(f"rendered {len(written)} debrief report(s) into {_human_text(root / debrief.VIEWS)}")
            return 0
        if args.debrief_command == "form":
            if not debrief.enabled():
                print(
                    f"Subagent debriefs are disabled ({debrief.SUBAGENT_DEBRIEF_ENV} is not set, "
                    "or this platform cannot store them); skip this step."
                )
                return 0
            print(debrief.form(args.target, args.selector, phase=args.phase, note=args.note))
            return 0
        entry, path = debrief.record(
            args.target,
            args.selector,
            _read_answer(args),
            phase=args.phase,
            note=args.note,
            worker_id=args.worker_id,
        )
    except (GraphValidationError, RuntimeProjectionError) as error:
        for issue in error.issues:
            print(f"error: {_human_text(issue)}", file=sys.stderr)
        return 2
    except (debrief.DebriefError, OSError) as error:
        print(f"error: {_human_text(error)}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(
            {"attempt_id": entry.attempt_id, "outcome": entry.outcome, "path": str(path)},
            sort_keys=True,
            separators=(",", ":"),
        ))
    else:
        print(_human_text(f"recorded debrief {entry.attempt_id} ({entry.outcome_label}) at {path}"))
    return 0


def _debrief_orchestrator(args: argparse.Namespace) -> int:
    try:
        if args.debrief_command == "form":
            if not debrief_orchestrator.enabled():
                print(
                    f"Orchestrator debriefs are disabled ({debrief_orchestrator.DEBRIEF_ENV} is off, "
                    "or this platform cannot store them); skip this step."
                )
                return 0
            print(debrief_orchestrator.form(args.skill))
            return 0
        entry, path, issue = debrief_orchestrator.record(args.target, _read_answer(args), skill=args.skill)
    except (GraphValidationError, RuntimeProjectionError) as error:
        for issue_text in error.issues:
            print(f"error: {_human_text(issue_text)}", file=sys.stderr)
        return 2
    except (debrief.DebriefError, OSError) as error:
        print(f"error: {_human_text(error)}", file=sys.stderr)
        return 2
    if args.json:
        draft = None
        if issue is not None:
            draft = {"path": str(issue.path), "url": issue.url, "url_has_body": issue.url_has_body}
        print(json.dumps(
            {"run_id": entry.run_id, "path": str(path), "issue": draft},
            sort_keys=True,
            separators=(",", ":"),
        ))
        return 0
    print(_human_text(f"recorded orchestrator debrief {entry.run_id} at {path}"))
    if issue is None:
        print("no AutoformBot friction reported; no issue drafted")
        return 0
    print(_human_text(f"drafted an issue for {debrief_orchestrator.ISSUE_REPOSITORY} at {issue.path}"))
    if issue.url_has_body:
        print("nothing has been sent; the draft ends with a link that opens the issue form pre-filled with the "
              "draft as it was recorded")
    else:
        print("nothing has been sent; the draft is too long for a link, so its footer opens the issue form with "
              "the title only")
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
        revision = f"{mathlib.input_rev} @ {mathlib.rev}" if mathlib.input_rev else mathlib.rev
        where = mathlib.dir if mathlib.type == "path" else f"{revision} ({mathlib.url})"
        print(f"Mathlib: {_human_text(where)} [{mathlib.source}]")
    if result.autoform_paths:
        print(f"Autoform: {', '.join(result.autoform_paths)}")
    release = f" ({result.compatibility.release})" if result.compatibility.release else ""
    print(f"Compatibility: {result.compatibility.status}{release}")
    for diagnostic in result.diagnostics:
        location = f" {diagnostic.path}" if diagnostic.path else ""
        print(f"{diagnostic.severity}[{diagnostic.code}]{location}: {diagnostic.message}", file=sys.stderr)


def _library_argument(value: str):
    try:
        return library_argument(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


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
    if args.claim_command == "list":
        return _claim_list(args)
    try:
        board = _claim_board(args)
        operation = args.claim_command
        if operation == "cleanup":
            print(f"removed {board.cleanup()} expired claim(s)")
            _warn_about_ref_locks(board)
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


def _claim_list(args: argparse.Namespace) -> int:
    """List claims; stdout carries only the JSON, and a failure's line on stderr says what kind it is."""
    targets: dict[str, str] = {}
    for node_id in args.node_id:
        key = author_claim_key(node_id)
        if key in targets:
            print(f"error: duplicate claim target: {node_id}", file=sys.stderr)
            return 2
        targets[key] = node_id

    def progress(step: str) -> None:
        print(f"claim list: {step}...", file=sys.stderr, flush=True)

    try:
        board = _claim_board(args)
        leases = board.list(list(targets) or None, timeout=args.timeout, progress=progress)
    except ClaimTimeoutError as exc:
        print(
            f"error: timeout: no answer from the claim board within {args.timeout:g}s while {exc.step}",
            file=sys.stderr,
        )
        return 1
    except ClaimContentionError as exc:
        print(f"error: contention: {exc}", file=sys.stderr)
        return 1
    except ClaimTransportError as exc:
        print(f"error: transport: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(leases, sort_keys=True, separators=(",", ":")))
    _warn_about_ref_locks(board)
    return 0


def _warn_about_ref_locks(board: ClaimBoard) -> None:
    # Lock files are not refs, so neither list's nor cleanup's own output can show one.
    for lock in board.ref_locks():
        print(
            f"warning: {lock.describe()}; if no Git process is using the claim board, delete it",
            file=sys.stderr,
        )


def _migrate(args: argparse.Namespace) -> int:
    if args.migrate_command != "article-ids":
        return 2
    try:
        if args.write:
            # Beside --json, stdout carries only the plan, so the writes go to stderr.
            stream = sys.stderr if args.json else sys.stdout
            for entry in write_article_ids(args.blueprint_dir):
                print(f"{entry.article_path}: added article_id {entry.article_id}", file=stream)
        plan = plan_article_ids(args.blueprint_dir)
    except ArticleIdWriteError as error:
        for entry in error.written:
            print(f"{entry.article_path}: added article_id {entry.article_id}", file=sys.stderr)
        for issue in error.issues:
            print(f"error: {issue}", file=sys.stderr)
        if error.written:
            print(f"error: added {len(error.written)} article_id(s) before stopping", file=sys.stderr)
        return 2
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
    return 2


def _print_refusal(exc: GraphValidationError | PublicationError | ReviewError | SkeletonError, *, stream=None) -> None:
    """Print why review evidence was refused, naming the article each finding is about."""

    stream = stream or sys.stderr
    if isinstance(exc, ReviewError):
        for finding in exc.findings:
            print(f"error: {_finding_text(finding)}", file=stream)
        return
    for issue in exc.issues:
        print(f"error: {issue}", file=stream)


def _review_prepare(args: argparse.Namespace) -> int:
    try:
        graph = load_graph(args.blueprint_dir)
        skeleton = extract_skeletons(args.blueprint_dir, lean_root=args.lean_root, timeout=args.timeout)
        bundle = build_review_bundle(graph, skeleton)
        written = write_review_packets(bundle, args.packets)
    except (GraphValidationError, ReviewError, SkeletonError) as exc:
        _print_refusal(exc)
        return 2
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{args.packets}: {len(written)} blind packet(s) for {len(bundle.articles)} review article(s)")
    return 0


def _review_record(args: argparse.Namespace) -> int:
    """File a card for one packet, without Lean.

    Whether the card is current is ``review check``'s to judge, against its
    own extraction: a card filed from a stale packet is reported invalid
    there, and no approval can be computed from it.
    """

    try:
        statement_hash, packet_hash, packet = read_review_packet(args.packets, args.article_id, args.declaration)
        try:
            testimony = read_regular_file(args.testimony).decode("utf-8")
        except (OSError, UnicodeError) as exc:
            reason = f"{args.declaration}: cannot read the testimony {args.testimony}: {exc}"
            raise ReviewError([ReviewFinding(args.article_id, "review-input-unreadable", reason)]) from exc
        graph = load_graph(args.blueprint_dir)
        if not any(node.article_id == args.article_id for node in graph.nodes.values()):
            reason = f"current graph has no article with article_id {args.article_id}"
            raise ReviewError([ReviewFinding(args.article_id, "review-article-missing", reason)])
        card = prepare_readback(
            args.blueprint_dir,
            article_id=args.article_id,
            declaration=args.declaration,
            statement_hash=statement_hash,
            packet_hash=packet_hash,
            model=args.model,
            text=testimony,
            packet_text=packet,
            expected_card_hash=args.expected_card_hash,
        )
        path = publish_readback(card)
    except (GraphValidationError, ReviewError) as exc:
        for issue in exc.issues:
            print(f"error: {issue}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: {args.declaration}: {exc}", file=sys.stderr)
        return 2
    print(f"{path}: recorded read-back for {args.declaration}")
    # Recorded either way: the site shows such a formula as typed.
    for warning in typesetting_warnings(testimony):
        print(f"warning: {args.declaration}: {warning}")
    return 0


def _review_check(args: argparse.Namespace) -> int:
    try:
        graph, bundle, cards = _current_review(args.blueprint_dir, lean_root=args.lean_root, timeout=args.timeout)
    except (GraphValidationError, ReviewError, SkeletonError) as exc:
        _print_refusal(exc)
        return 2
    states = review_states(graph, bundle, cards)
    findings = review_findings(graph, bundle, cards, states)
    approvals = approval_statuses(states)
    if args.json:
        print(
            json.dumps(
                {
                    "approvals": [_approval_json(item) for item in approvals.values()],
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
        return 1 if findings else 0
    for finding in findings:
        print(f"error: {_finding_text(finding)}")
    if not findings:
        print(f"OK: {len(bundle.articles)} statement review(s) complete and current")
    for item in approvals.values():
        print(_approval_line(item))
    return 1 if findings else 0


def _finding_text(finding: ReviewFinding) -> str:
    """A review finding as one line: the article it is about, when it is about one, then its code and reason."""

    where = f"{finding.node_id}: " if finding.node_id else ""
    return f"{where}{finding.code}: {finding.reason}"


def _approval_line(item: ApprovalStatus) -> str:
    return f"{item.node_id}: {item.label} · {item.review_hash}"


def _approval_json(item: ApprovalStatus) -> dict[str, object]:
    return {"label": item.label, "node_id": item.node_id, "review_hash": item.review_hash}


def _current_review(
    blueprint_dir: str | Path,
    *,
    lean_root: Path,
    timeout: float | None = None,
) -> tuple[Graph, ReviewBundle, dict[tuple[str, str], Readback]]:
    """The graph, review evidence derived from one extraction of it, and the cards.

    Everything returned describes one state of the blueprint. The graph and the
    read-back cards are read before Lean runs. The extraction refuses a
    blueprint that changes while it runs, and :func:`build_review_bundle`
    refuses one whose hash does not name the graph read here, which covers the
    time before the extraction took its own snapshot. Cards are not articles,
    so they are read again afterwards and must be unchanged. Callers judge the
    cards returned here rather than reading the vault again, the statements
    compared are read again when the evidence is built and must still hash as
    the graph recorded, and a passage edited since the extraction fails the
    blueprint-hash comparison.
    """

    graph = load_graph(blueprint_dir)
    cards = _review_cards(graph.blueprint_dir)
    skeleton = extract_skeletons(blueprint_dir, lean_root=lean_root, timeout=timeout)
    if _review_cards(graph.blueprint_dir) != cards:
        reason = "a read-back changed while the review evidence was being extracted; rerun once the blueprint is idle"
        raise ReviewError([ReviewFinding("", "review-snapshot-changed", reason)])
    return graph, build_review_bundle(graph, skeleton), cards


def _review_cards(blueprint_dir: Path) -> dict[tuple[str, str], Readback]:
    """The vault's read-back cards; a card directory that cannot be listed is a review error, not missing cards."""

    try:
        return load_readbacks(blueprint_dir)
    except ValueError as exc:
        raise ReviewError([ReviewFinding("", "readback-unlistable", str(exc))]) from exc


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
    if not args.review and args.timeout is not None:
        print("error: --timeout requires --review", file=sys.stderr)
        return 2
    if args.review and args.lean_root is None:
        print("error: --review requires --lean-root", file=sys.stderr)
        return 2
    # Render runs the extraction once its checks of the output and the inputs
    # pass, so a render they refuse costs no Lean.
    review = partial(extract_skeletons, args.blueprint_dir, lean_root=args.lean_root, timeout=args.timeout)
    try:
        report = render_site(
            args.blueprint_dir,
            args.output,
            lean_root=args.lean_root,
            repository_url=args.repository_url,
            ref=args.ref,
            review=review if args.review else None,
        )
    except (GraphValidationError, PublicationError, ReviewError, SkeletonError) as exc:
        _print_refusal(exc, stream=sys.stdout)
        return 1

    print(f"{report.output_dir}: {report.pages} pages, {report.nodes} nodes, {report.linked} code links")
    for issue in (*report.unresolved, *report.warnings):
        print(f"warning: {issue}")
    # A declaration that was never looked up is not known to exist.
    if (report.unresolved or report.unchecked) and args.require_declarations:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
