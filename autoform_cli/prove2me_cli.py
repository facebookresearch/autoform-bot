"""`autoform prove2me` subcommands; the client logic lives in :mod:`.prove2me`."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import prove2me
from .prove2me import Prove2MeError


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("prove2me", help="find, mirror, and submit Prove2Me mission theorems")
    commands = parser.add_subparsers(dest="prove2me_command", required=True)

    def command(name: str, help: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help)
        sub.add_argument(
            "--workspace",
            help="Prove2Me Lean workspace holding credentials.json "
            "(default: $PROVE2ME_WORKSPACE or ~/prove2me_workspace)",
        )
        sub.add_argument("--json", action="store_true", help="write stable machine-readable output")
        return sub

    setup = command("setup", "clone the workspace, ask for an API key if needed, and pin Mathlib for a mission")
    target = setup.add_mutually_exclusive_group()
    target.add_argument("--mission", help="pin the workspace to this mission's Mathlib environment")
    target.add_argument("--env", help="pin to this mathlib_rev (or 'default') instead")
    setup.add_argument("--no-fetch", action="store_true", help="do not download Mathlib after pinning")
    setup.add_argument("--yes", action="store_true", help="clone and download without asking")
    setup.add_argument(
        "--print-workspace",
        action="store_true",
        help="print only the chosen workspace path on stdout (messages go to stderr)",
    )

    command("auth", "check the API key and report the platform version; never prints the key")

    mission = command("mission", "show a mission's milestones and open frontier")
    mission.add_argument("mission", help="mission URL, URL slug, name, or id")

    fetch = command("fetch", "mirror a theorem and its platform imports into the workspace")
    fetch.add_argument("theorem_id")

    get = command("get", "read-only GET of any API path, for scouting history, discussion, and submissions")
    get.add_argument("path", help="path under /api/v1, e.g. /theorems/<id>/submissions")
    get.add_argument("--param", action="append", default=[], metavar="KEY=VALUE", help="query parameter (repeatable)")

    verify = command("verify", "gate, compile, submit, and wait for the verdict")
    verify.add_argument("theorem_id")
    verify.add_argument("solution", type=Path, help="the solution.lean to submit")
    verify.add_argument("--explanation-file", type=Path, help="Markdown explanation to attach")
    verify.add_argument("--disprove", action="store_true", help="the file proves the negation of the statement")
    verify.add_argument("--dry-run", action="store_true", help="run every local check, then stop before submitting")
    verify.add_argument("--skip-compile", action="store_true", help="do not build the file with Lake first")
    verify.add_argument(
        "--allow-env-mismatch",
        action="store_true",
        help="submit even though the workspace pins a different Mathlib than the target",
    )
    verify.add_argument("--no-wait", action="store_true", help="return once the submission is queued")
    verify.add_argument("--timeout", type=float, default=1800, help="seconds to wait for a verdict (default 1800)")
    verify.add_argument("--compile-timeout", type=float, default=1800, help="seconds Lake may build (default 1800)")

    publish = command("publish", "publish a sketch's child lemmas or a definition, then wait for the compile")
    publish.add_argument("kind", choices=("problem", "definition"))
    publish.add_argument("spec", type=Path, help="JSON body: one object, a list, or {\"problems\": [...]}")
    publish.add_argument(
        "--env",
        required=True,
        help="mathlib_rev to publish in; use the parent theorem's, or 'default' for the platform default",
    )
    publish.add_argument("--private", action="store_true", help="visible only to you until made public")
    publish.add_argument("--dry-run", action="store_true", help="validate the spec, then stop before publishing")
    publish.add_argument("--no-wait", action="store_true", help="return once the jobs are queued")
    publish.add_argument("--timeout", type=float, default=900, help="seconds to wait per job (default 900)")

    propose = command(
        "propose", "compile a mission proposal spec locally, then create or update it as a private draft"
    )
    propose.add_argument("spec", type=Path, help='JSON: {"proposal": {...}, "items": [...]}')
    propose.add_argument("--proposal", metavar="ID", help="update this draft proposal instead of creating one")
    propose.add_argument("--dry-run", action="store_true", help="run every local check, then stop before uploading")
    propose.add_argument("--skip-compile", action="store_true", help="do not build the draft items with Lake first")
    propose.add_argument("--compile-timeout", type=float, default=1800, help="seconds Lake may build (default 1800)")

    explain = command("explain", "replace or clear a submission's explanation")
    explain.add_argument("submission_id")
    source = explain.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="Markdown explanation")
    source.add_argument("--clear", action="store_true")


def run(args: argparse.Namespace) -> int:
    if args.prove2me_command == "setup":
        try:
            return _setup(args)
        except Prove2MeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    handler = {
        "auth": _auth,
        "mission": _mission,
        "fetch": _fetch,
        "get": _get,
        "verify": _verify,
        "publish": _publish,
        "propose": _propose,
        "explain": _explain,
    }[args.prove2me_command]
    workspace = prove2me.workspace_path(args.workspace)
    try:
        with prove2me.Client(prove2me.load_credentials(workspace)) as client:
            return handler(client, args, workspace)
    except Prove2MeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _emit(payload: object) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _setup(args: argparse.Namespace) -> int:
    """Make a workspace ready to work on, asking only for what is missing.

    Credentials live in the default workspace so every per-environment checkout
    shares them. A key is requested only from an interactive terminal, with
    hidden input, and is checked against the platform before it is saved.
    """
    home = prove2me.workspace_path()
    if not home.exists():
        _say(f"cloning the Prove2Me workspace into {home}")
        prove2me.clone_workspace(home)

    lookup = prove2me.workspace_path(args.workspace) if args.workspace else home
    with _authenticated_client(lookup, home) as client:
        env = None
        if args.mission:
            env = prove2me.resolve_environment(client, prove2me.mission_environment(client, args.mission))
        elif args.env:
            env = prove2me.resolve_environment(client, None if args.env == "default" else args.env)

    workspace = lookup if args.workspace else (prove2me.choose_workspace(home, env["mathlib_rev"]) if env else home)
    needs_clone = not workspace.exists()
    needs_fetch = bool(env) and not args.no_fetch and not prove2me.mathlib_ready(workspace)
    if needs_clone or needs_fetch:
        _confirm_heavy_work(args, workspace, env, needs_clone, needs_fetch)
    if needs_clone:
        _say(f"cloning {prove2me.WORKSPACE_REPO} into {workspace}")
        prove2me.clone_workspace(workspace)

    if prove2me.share_credentials(workspace, home):
        _say(f"linked {workspace / 'credentials.json'} to the key in {home}")

    if env:
        for name in prove2me.pin_environment(workspace, env):
            _say(f"wrote {workspace / name} for {env.get('display_name') or env['mathlib_rev']}")
        if needs_fetch:
            steps = [["lake", "exe", "cache", "get"]]
            if not (workspace / "lake-manifest.json").is_file():
                steps.insert(0, ["lake", "update"])
            if (workspace / "Solutions" / "SmokeTest.lean").is_file():
                steps.append(["lake", "build", "Solutions.SmokeTest"])
            for step in steps:
                _say(f"$ {' '.join(step)}")
                if subprocess.run(step, cwd=workspace, stdout=sys.stderr, stderr=sys.stderr).returncode != 0:
                    raise Prove2MeError(
                        f"`{' '.join(step)}` failed in {workspace}; delete that directory before retrying "
                        "if it was created by this setup"
                    )

    pin = prove2me.workspace_mathlib_rev(workspace)
    _say(f"OK: workspace {workspace}" + (f" pinned to Mathlib {pin[:7]}" if pin else " (no Mathlib pinned yet)"))
    if args.json:
        _emit({"workspace": str(workspace), "mathlib_rev": pin, "mathlib_ready": prove2me.mathlib_ready(workspace)})
    elif args.print_workspace:
        print(workspace)
    return 0


# A Mathlib checkout plus its unpacked cache; an existing one measured 15 GB.
_FETCH_BYTES = 16 * 1024**3


def _confirm_heavy_work(args: argparse.Namespace, workspace: Path, env: dict | None,
                        needs_clone: bool, needs_fetch: bool) -> None:
    """Show what setup is about to clone and download, check the disk, and ask.

    Fetching Mathlib is a multi-gigabyte download per environment, so it never
    starts without either a yes from a terminal or an explicit --yes, and never
    on a disk that cannot hold it.
    """
    plan = []
    if env:
        plan.append(f"Mathlib environment: {env.get('display_name') or env['mathlib_rev']}")
    if needs_clone:
        plan.append(f"clone the Prove2Me workspace into {workspace}")
    if needs_fetch:
        plan.append(f"download Mathlib and its prebuilt cache into {workspace}/.lake (about 7-15 GB)")
        existing = next(parent for parent in (workspace, *workspace.parents) if parent.exists())
        free = shutil.disk_usage(existing).free
        if free < _FETCH_BYTES:
            raise Prove2MeError(
                "setup would " + "; ".join(plan[1:]) + f", but only {free / 1024**3:.1f} GB is free "
                f"(needs about {_FETCH_BYTES / 1024**3:.0f} GB). Free some space, or pass --no-fetch"
            )
    _say("setup will:\n  " + "\n  ".join(plan))
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise Prove2MeError("not asking without a terminal; rerun in a terminal or pass --yes")
    _say("Continue? [y/N] ")
    if sys.stdin.readline().strip().lower() not in {"y", "yes"}:
        raise Prove2MeError("setup cancelled")


def _authenticated_client(lookup: Path, home: Path) -> prove2me.Client:
    try:
        client = prove2me.Client(prove2me.load_credentials(lookup))
        client.authenticate()
        return client
    except Prove2MeError as exc:
        problem = str(exc)
    if os.environ.get("PROVE2ME_API_KEY", "").strip():
        raise Prove2MeError(f"{problem}; fix or unset PROVE2ME_API_KEY")
    if not sys.stdin.isatty():
        raise Prove2MeError(f"{problem}; run `autoform prove2me setup` in a terminal to enter a key")

    _say(problem)
    _say("Mint a key at https://prove2.me (account menu, top right > API key). New accounts: "
         "https://prove2.me/start.md")
    for _ in range(3):
        key = getpass.getpass("Prove2Me API key (input hidden): ").strip()
        if not key:
            continue
        credentials = prove2me.Credentials(key, "the key you entered", expires_at=prove2me._key_expiry(key))
        try:
            client = prove2me.Client(credentials)
            client.authenticate()
        except Prove2MeError as exc:
            _say(str(exc))
            continue
        _say(f"saved the key to {prove2me.store_api_key(home, key)} (readable only by you)")
        return client
    raise Prove2MeError("no valid API key entered")


def _auth(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    client.authenticate()
    credentials = client.credentials
    expires = credentials.expires_at.isoformat() if credentials.expires_at else None
    if args.json:
        _emit({"source": credentials.source, "username": credentials.username,
                     "expires_at": expires, "platform_version": client.platform_version})
        return 0
    who = f" as {credentials.username}" if credentials.username else ""
    print(f"OK: authenticated{who} with the key from {credentials.source}")
    print(f"    key expires {expires or 'unknown'}; platform version {client.platform_version}")
    return 0


def _mission(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    frontier = prove2me.mission_frontier(client, prove2me.find_mission(client, args.mission))
    if args.json:
        _emit(frontier)
        return 0
    main = frontier["main_theorem"]
    print(f"{frontier['mission']['name']} ({frontier['mission']['id']})")
    print(f"  goal: {main['theorem_name']} [{main['status']}] {main['theorem_id']}")
    print(f"  milestones: {len(frontier['milestones'])}")
    for milestone in frontier["milestones"]:
        theorem = milestone.get("theorem") or {}
        mark = "x" if milestone.get("completed") else " "
        print(f"    [{mark}] {milestone['title']} -> {theorem.get('theorem_name')} {theorem.get('id')}")
    print(f"  open frontier: {len(frontier['open_leaves'])}")
    for leaf in frontier["open_leaves"]:
        print(f"    {leaf['theorem_name']} {leaf['theorem_id']} (closability {leaf.get('closability', 0)})")
    return 0


def _fetch(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    result = prove2me.fetch_theorem(client, args.theorem_id, workspace)
    theorem = result.theorem
    if args.json:
        _emit({
            "theorem_id": theorem.get("theorem_id"),
            "theorem_name": theorem.get("theorem_name"),
            "mathlib_rev": theorem.get("mathlib_rev"),
            "workspace_mathlib_rev": result.workspace_rev,
            "environment_matches": result.environment_matches,
            "written": result.written,
            "unchanged": result.unchanged,
            "unresolved": result.unresolved,
        })
    else:
        print(f"{theorem['theorem_name']} [{theorem.get('status')}] in Mathlib {theorem.get('mathlib_rev')}")
        for path in result.written:
            print(f"  + {path}")
        for path in result.unchanged:
            print(f"  = {path}")
        for module in result.unresolved:
            print(f"warning: could not find platform module {module}")
        if not result.environment_matches:
            print(
                f"warning: workspace pins Mathlib {result.workspace_rev or 'unknown'}, the target needs "
                f"{theorem.get('mathlib_rev')}; re-pin before compiling (Prove2Me references/lean-setup.md)",
                file=sys.stderr,
            )
    return 0 if not result.unresolved else 1


def _get(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    params = {}
    for pair in args.param:
        key, sep, value = pair.partition("=")
        if not sep:
            raise Prove2MeError(f"--param needs KEY=VALUE: {pair!r}")
        params[key] = value
    print(json.dumps(client.get(args.path, **params), indent=None if args.json else 2, ensure_ascii=False))
    return 0


def _verify(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    """Refuse locally whatever the server would reject, then submit.

    A dry run still reads the target for its name and environment, but stops
    before anything is written to the platform.
    """
    path: Path = args.solution
    if not path.is_file():
        raise Prove2MeError(f"no such file: {path}")
    theorem = client.get(f"/theorems/{args.theorem_id}")
    if not isinstance(theorem, dict) or theorem.get("status") == "Definition":
        raise Prove2MeError(f"{args.theorem_id} is not a provable theorem")
    report: dict = {"theorem_id": args.theorem_id, "theorem_name": theorem.get("theorem_name"),
                    "theorem_status": theorem.get("status"), "file": str(path)}

    problems = prove2me.check_solution(path.read_text(encoding="utf-8"), theorem["theorem_name"])
    workspace_rev = prove2me.workspace_mathlib_rev(workspace)
    if workspace_rev != theorem.get("mathlib_rev") and not args.allow_env_mismatch and not args.skip_compile:
        problems.append(
            f"workspace pins Mathlib {workspace_rev or 'unknown'} but the target is verified in "
            f"{theorem.get('mathlib_rev')}; re-pin, or pass --allow-env-mismatch"
        )
    if not problems and not args.skip_compile:
        # Re-mirror so the type check compares against the statement as published now.
        mirrored = prove2me.fetch_theorem(client, args.theorem_id, workspace)
        problems.extend(f"could not mirror platform module {module}" for module in mirrored.unresolved)
    if not problems and not args.skip_compile:
        # Lake prints nothing until it finishes, and a cold `import Mathlib` can take many minutes.
        print(f"building {path.name} and checking it against {theorem['theorem_name']} with Lake; "
              "files that import all of Mathlib can take many minutes", file=sys.stderr, flush=True)
        ok, output = prove2me.compile_locally(path, workspace, theorem["theorem_name"], args.compile_timeout)
        report["compile_output"] = output[-4000:]
        if not ok:
            problems.append("local build or type check failed")
    report["problems"] = problems

    if problems or args.dry_run:
        report["submitted"] = False
        if args.json:
            _emit(report)
        else:
            for problem in problems:
                print(f"error: {problem}")
            if problems and report.get("compile_output"):
                print(report["compile_output"])
            if not problems:
                print(f"OK: {path} passes the local gates for {theorem['theorem_name']}; dry run, nothing submitted")
        return 1 if problems else 0

    explanation = args.explanation_file.read_text(encoding="utf-8") if args.explanation_file else None
    queued = prove2me.submit(
        client,
        args.theorem_id,
        path,
        proof_type="disprove" if args.disprove else "prove",
        explanation=explanation,
    )
    submission_id = queued["submission_id"]
    report.update(submitted=True, submission_id=submission_id, status=queued.get("status"))
    if not args.json:
        print(f"submitted {submission_id} for {theorem['theorem_name']}")
    if not args.no_wait:
        verdict = prove2me.wait_for_verdict(client, submission_id, timeout=args.timeout)
        report.update(status=verdict.get("status"), error_message=verdict.get("error_message") or "")
    if args.json:
        _emit(report)
    else:
        print(f"status: {report['status']}")
        if report.get("error_message"):
            print(report["error_message"])
        if not explanation:
            print("note: no explanation attached; add one with `autoform prove2me explain`")
    if args.no_wait:
        return 0
    return 0 if report["status"] in prove2me.ACCEPTED else 1


def _publish(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Prove2MeError(f"cannot read {args.spec}: {exc}") from None
    items = prove2me.publish_items(args.kind, spec)
    problems = [problem for item in items for problem in prove2me.check_publish(args.kind, item)]
    if problems or args.dry_run:
        for problem in problems:
            print(f"error: {problem}")
        if not problems:
            print(f"OK: {len(items)} {args.kind}(s) pass the local checks; dry run, nothing published")
        return 1 if problems else 0

    env = None if args.env == "default" else args.env
    jobs = prove2me.publish(client, args.kind, items, env, private=args.private)
    if not args.no_wait:
        for job in jobs:
            state = prove2me.wait_for_job(client, job["job_id"], timeout=args.timeout)
            job.update(status=state.get("status"), theorem_id=state.get("theorem_id"),
                       error_message=state.get("error_message") or "")
    if args.json:
        _emit({"jobs": jobs})
    else:
        for job in jobs:
            line = f"{job['name']}: {job.get('status', 'QUEUED')} job {job['job_id']}"
            if job.get("theorem_id"):
                line += f" -> theorem {job['theorem_id']}"
            print(line)
            if job.get("error_message"):
                print(f"  {job['error_message']}")
    if args.no_wait:
        return 0
    return 0 if all(job.get("status") == "PUBLISHED" for job in jobs) else 1


def _propose(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    """Check and build the spec locally, then upload it as a draft; never submits it.

    Submitting a proposal publishes its items and asks for moderation, and the
    platform leaves that to the user on the website.
    """
    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Prove2MeError(f"cannot read {args.spec}: {exc}") from None
    problems, warnings = prove2me.check_proposal(spec)
    report: dict = {"spec": str(args.spec), "warnings": warnings}

    if not problems and not args.skip_compile:
        env = prove2me.resolve_environment(client, spec["proposal"].get("env") or None)
        pinned = prove2me.workspace_mathlib_rev(workspace)
        if pinned != env["mathlib_rev"]:
            problems.append(
                f"workspace {workspace} pins Mathlib {(pinned or 'unknown')[:7]}, but the proposal targets "
                f"{env['mathlib_rev'][:7]}; run `autoform prove2me setup --env {env['mathlib_rev'][:7]}` and "
                "pass that checkout with --workspace"
            )
        else:
            _say(f"building the draft items in {workspace} against Mathlib {pinned[:7]} with Lake")
            ok, output = prove2me.compile_proposal(spec["items"], workspace, args.compile_timeout)
            report["compile_output"] = output[-4000:]
            if not ok:
                problems.append("local build of the draft items failed")
    report["problems"] = problems

    if problems or args.dry_run:
        report["uploaded"] = False
        if args.json:
            _emit(report)
        else:
            for warning in warnings:
                print(f"warning: {warning}")
            for problem in problems:
                print(f"error: {problem}")
            if problems and report.get("compile_output"):
                print(report["compile_output"])
            if not problems:
                print(f"OK: {len(spec['items'])} item(s) pass the local checks; dry run, nothing uploaded")
        return 1 if problems else 0

    result = prove2me.upload_proposal(client, spec, args.proposal)
    report.update(uploaded=True, **result)
    if args.json:
        _emit(report)
        return 0
    for warning in warnings:
        print(f"warning: {warning}")
    print(f"{result['name']}: proposal {result['proposal_id']} [{result['status']}, {result['visibility']}]")
    for entry in result["items"]:
        print(f"  {entry['action']} {entry['kind']} {entry['name']} -> item {entry['item_id']}")
    for title in result["milestones"]:
        print(f"  milestone: {title}")
    for name in result["not_in_spec"]:
        print(f"warning: {name} is in the proposal but not in the spec; left in place")
    print("Next: open https://prove2.me (homepage > My missions), confirm each item against its read-back, "
          "then click Submit Proposal. Nothing is published until then.")
    return 0


def _explain(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    text = None if args.clear else args.file.read_text(encoding="utf-8")
    prove2me.set_explanation(client, args.submission_id, text)
    print(f"OK: explanation {'cleared' if text is None else 'updated'} on {args.submission_id}")
    return 0
