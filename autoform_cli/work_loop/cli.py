"""Command line of the work loop: ``autoform work run`` and ``autoform work status``."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

from .. import lean_probe
from . import gitops
from .agents import AgentSpec, parse_agent_spec, parse_price
from .github import GitHub, GitHubError
from .gitops import GitError
from .profile import ProfileError, load_profile
from .state import State, default_state_dir
from .tasks import TaskError
from .worker import Settings, Worker, WorkerError

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LEAF = Path("skills") / "formalize" / "references" / "leaf.md"


def _positive_int(text: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError("expected a positive whole number")
    return int(text)


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a positive number") from None
    if not value > 0 or value == float("inf"):
        raise argparse.ArgumentTypeError("expected a positive number")
    return value


def _agent(text: str) -> AgentSpec:
    try:
        return parse_agent_spec(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _price(text: str):
    try:
        return parse_price(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--loop", action="store_true", help="keep doing rounds until stopped (default: one round)")
    parser.add_argument("--workers", type=_positive_int, default=1, metavar="N", help="run N workers in parallel")
    parser.add_argument(
        "--agent", type=_agent, action="append", default=[], metavar="SPEC",
        help="claude[:MODEL[:EFFORT]] or codex[:MODEL[:EFFORT]]; repeat to give workers different agents "
        "(default: claude if it is installed, else codex)",
    )
    parser.add_argument("--checkout", type=Path, metavar="DIR", help="the project clone (default: this directory)")
    parser.add_argument("--repo", metavar="OWNER/NAME", help="the repository pull requests target (default: origin's)")
    parser.add_argument("--fork", metavar="OWNER/NAME", help="where branches are pushed (default: your fork)")
    parser.add_argument("--claims-repo", metavar="URL", help="the claim board (default: the profile's, else your fork)")
    parser.add_argument("--profile", type=Path, metavar="FILE", help="a work-loop profile (autoform-work-profile/v1)")
    parser.add_argument("--tasks", dest="tasks_override", metavar="FILE_OR_URL", help="read the task list from here")
    parser.add_argument("--task", dest="only_task", metavar="ID", help="work only this article id or node id")
    parser.add_argument("--phase", action="append", choices=("statement", "proof"), default=[],
                        help="phases to take (default: the profile's)")
    parser.add_argument("--branch-prefix", metavar="PREFIX", help="prefix of pushed branches (default: the profile's)")
    parser.add_argument("--state-dir", type=Path, metavar="DIR",
                        help="the shared state directory (default: $XDG_STATE_HOME/autoform/work/OWNER__NAME)")
    parser.add_argument("--budget", type=_positive_float, metavar="USD", help="stop once the agents cost this much")
    parser.add_argument("--attempt-budget", type=_positive_float, metavar="USD",
                        help="cap one Claude Code run's cost (Claude's --max-budget-usd)")
    parser.add_argument("--price", type=_price, metavar="input=N,cached=N,output=N",
                        help="dollars per million tokens, to cost Codex runs (needed with --budget)")
    parser.add_argument("--max-attempts", type=_positive_int, metavar="N",
                        help="author at most N tasks; with --loop, keep following their pull requests, then exit")
    parser.add_argument("--max-open-prs", type=_positive_int, default=5, metavar="N",
                        help="author nothing new while N loop pull requests are open (default: 5)")
    parser.add_argument("--max-hours", type=_positive_float, metavar="H", help="stop after H hours")
    parser.add_argument("--max-repairs", type=_positive_int, default=3, metavar="N",
                        help="repair rounds per pull request before it is left to a person (default: 3)")
    parser.add_argument("--attempt-minutes", type=_positive_float, default=120.0, metavar="M",
                        help="time limit of one author attempt's agent (default: 120)")
    parser.add_argument("--claude-arg", action="append", default=[], metavar="ARG",
                        help="extra argument for every Claude Code run (repeatable; write --claude-arg=--flag)")
    parser.add_argument("--codex-arg", action="append", default=[], metavar="ARG",
                        help="extra argument for every Codex run (repeatable; write --codex-arg=--flag)")
    parser.add_argument("--triage", type=_agent, metavar="SPEC",
                        help="have this agent rate each task's feasibility (1-5) first, and author only those rated "
                        "at least --min-score")
    parser.add_argument("--min-score", type=int, choices=range(1, 6), default=4, metavar="N",
                        help="the lowest triage score to author (default: 4)")
    parser.add_argument("--label", action="append", default=[], metavar="NAME",
                        help="label each new pull request with this existing label, when your account may")
    parser.add_argument("--no-co-author", action="store_true",
                        help="leave the agent's Co-authored-by trailer out of commits")
    parser.add_argument("--dry-run", action="store_true",
                        help="stop each attempt before its first push, keeping the commit in the worktree")
    parser.add_argument("--worker-index", type=_positive_int, help=argparse.SUPPRESS)
    parser.add_argument("--run-id", help=argparse.SUPPRESS)


def add_status_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--checkout", type=Path, metavar="DIR", help="the project clone (default: this directory)")
    parser.add_argument("--repo", metavar="OWNER/NAME", help="the repository (default: origin's)")
    parser.add_argument("--state-dir", type=Path, metavar="DIR", help="the shared state directory")
    parser.add_argument("--json", action="store_true", help="write stable machine-readable output")


def _origin_repository(checkout: Path) -> str | None:
    url = gitops.git(checkout, "remote", "get-url", "origin", check=False).stdout.strip()
    return gitops.github_repository(url) if url else None


def _resolve_repository(github: GitHub, checkout: Path, explicit: str | None) -> dict[str, object]:
    name = explicit or _origin_repository(checkout)
    if not name:
        raise WorkerError("pass --repo OWNER/NAME: this checkout's origin is not a GitHub repository")
    info = github.repository(name)
    if not explicit and info.get("fork") and isinstance(info.get("source"), dict):
        info = github.repository(str(info["source"]["full_name"]))
    return info


def _child_argv(argv: Sequence[str], index: int, run_id: str) -> list[str]:
    out: list[str] = []
    skip = False
    for item in argv:
        if skip:
            skip = False
            continue
        if item == "--workers":
            skip = True
            continue
        if item.startswith("--workers="):
            continue
        out.append(item)
    return out + ["--worker-index", str(index), "--run-id", run_id]


def supervise(argv: Sequence[str], workers: int, run_id: str, state: State) -> int:
    """Run ``workers`` worker processes; restart one that crashes, stop all on SIGINT or SIGTERM."""

    children: dict[int, lean_probe.Session] = {}
    restarts = dict.fromkeys(range(1, workers + 1), 0)
    stopping = False
    statuses: dict[int, int] = {}

    def spawn(index: int) -> None:
        command = [sys.executable, "-m", "autoform_cli", *_child_argv(argv, index, run_id)]
        children[index] = lean_probe.start_session(command)

    def stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True
        for child in children.values():
            if child.process.poll() is None:
                try:
                    os.killpg(child.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    state.event("supervisor-start", workers=workers, pid=os.getpid())
    for index in range(1, workers + 1):
        spawn(index)
        time.sleep(2)
    while children:
        time.sleep(2)
        for index, child in list(children.items()):
            status = child.process.poll()
            if status is None:
                continue
            del children[index]
            statuses[index] = status
            crashed = status not in (0, 130) and status > 0
            if crashed and not stopping and state.stop_file() is None and restarts[index] < 5:
                restarts[index] += 1
                state.event("worker-restart", index=index, status=status, restarts=restarts[index])
                time.sleep(30 * restarts[index])
                spawn(index)
    state.event("supervisor-stop", statuses=statuses)
    return max(statuses.values(), default=0)


def run_command(args: argparse.Namespace) -> int:
    try:
        return _run(args)
    except (WorkerError, GitHubError, GitError, ProfileError, TaskError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace) -> int:
    if os.name != "posix":
        raise WorkerError("`autoform work run` needs Linux or macOS")
    checkout = gitops.toplevel((args.checkout or Path.cwd()).resolve())
    if not (PLUGIN_ROOT / LEAF).is_file():
        raise WorkerError(f"run the loop from an AutoformBot plugin checkout: {PLUGIN_ROOT / LEAF} is missing")
    github = GitHub()
    login = github.viewer()
    info = _resolve_repository(github, checkout, args.repo)
    repository, base_branch = str(info["full_name"]), str(info.get("default_branch") or "main")
    profile = load_profile(checkout, repository, args.profile)
    state_dir = args.state_dir or default_state_dir(repository)
    run_id = args.run_id or os.environ.get("AUTOFORM_WORK_RUN") or uuid.uuid4().hex[:12]
    agents = list(args.agent)
    if not agents:
        found = next((name for name in ("claude", "codex") if lean_probe.find_program(name)), None)
        if found is None:
            raise WorkerError("install and log in to Claude Code (`claude`) or Codex (`codex`), or pass --agent")
        agents = [AgentSpec(found)]
    costed = agents + ([args.triage] if args.triage else [])
    if args.budget is not None and args.price is None and any(spec.backend == "codex" for spec in costed):
        raise WorkerError("Codex reports tokens, not dollars: pass --price with --budget to cost its runs")
    if args.workers > 1 and args.worker_index is None:
        state = State(state_dir, worker="supervisor", run=run_id)
        print(f"autoform work: {args.workers} workers on {repository}, state in {state_dir}", flush=True)
        return supervise(args.argv, args.workers, run_id, state)
    index = args.worker_index or 1
    worker_id = f"{login}-{socket.gethostname().split('.')[0]}-w{index}"
    fork = args.fork or github.fork_of(repository)
    if fork is None:
        if args.dry_run:
            fork = f"{login}/{repository.split('/', 1)[1]}"
        else:
            fork = github.create_fork(repository)
            print(f"autoform work: forked {repository} as {fork}", flush=True)
    profile_prefix = args.branch_prefix or profile.branch_prefix
    if not profile_prefix.endswith("/"):
        raise WorkerError("--branch-prefix must end with '/'")
    state = State(state_dir, worker=worker_id, run=run_id)
    if args.budget is None and args.loop:
        print("autoform work: no --budget: the agents run until you stop the loop", file=sys.stderr, flush=True)
    settings = Settings(
        repository=repository,
        checkout=checkout,
        base_branch=base_branch,
        push_repository=fork,
        claims_repo=args.claims_repo or profile.claims_repo or gitops.remote_url(fork),
        login=login,
        author=gitops.identity(checkout),
        plugin_root=PLUGIN_ROOT,
        agent=agents[(index - 1) % len(agents)],
        worker_id=worker_id,
        run_id=run_id,
        branch_prefix=profile_prefix,
        loop=args.loop,
        dry_run=args.dry_run,
        tasks_override=args.tasks_override,
        only_task=args.only_task,
        phases=tuple(args.phase) or profile.phases,
        budget=args.budget,
        attempt_budget=args.attempt_budget,
        max_attempts=args.max_attempts,
        max_open_prs=args.max_open_prs,
        max_hours=args.max_hours,
        max_repairs=args.max_repairs,
        attempt_timeout=args.attempt_minutes * 60,
        agent_args={"claude": tuple(args.claude_arg), "codex": tuple(args.codex_arg)},
        triage=args.triage,
        min_score=args.min_score,
        price=args.price,
        co_author=not args.no_co_author,
        labels=tuple(args.label),
    )
    agent_env = dict(os.environ)
    # Claim-board pushes run git in this process's environment: take HTTPS credentials from gh, as pushes do.
    os.environ.update(gitops.credential_env(os.environ, github.gh))
    worker = Worker(settings, profile, state, github, env=agent_env)
    worker.install_signal_handlers()
    print(
        f"autoform work: {worker_id} on {repository} ({profile.source}), agent {settings.agent.label}, "
        f"pushing to {fork}, claims on {settings.claims_repo}, state in {state_dir}",
        flush=True,
    )
    return worker.run()


def status_command(args: argparse.Namespace) -> int:
    try:
        checkout = gitops.toplevel((args.checkout or Path.cwd()).resolve())
        repository = args.repo or _origin_repository(checkout)
    except GitError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not repository and not args.state_dir:
        print("error: pass --repo or --state-dir", file=sys.stderr)
        return 2
    root = args.state_dir or default_state_dir(str(repository))
    if not root.is_dir():
        print(f"error: no work-loop state in {root}", file=sys.stderr)
        return 2
    state = State(root)
    records = sorted(
        (record for record in (state.read_json(path, {}) for path in (root / "prs").glob("*.json"))
         if isinstance(record, dict) and isinstance(record.get("number"), int)),
        key=lambda record: record["number"],
    )
    events = state.lines(state.ledger_path)
    outcomes: dict[str, int] = {}
    for row in events:
        if row.get("event") == "round" and row.get("phase") == "author":
            key = str(row.get("outcome"))
            outcomes[key] = outcomes.get(key, 0) + 1
    report = {
        "schema": "autoform-work-status/v1",
        "state_dir": str(root),
        "spent_usd": state.spent(),
        "attempts": state.attempts(),
        "author_outcomes": outcomes,
        "stop_file": str(root / "STOP") if (root / "STOP").exists() else None,
        "pull_requests": [
            {key: record.get(key) for key in ("number", "url", "task", "branch", "agent", "repairs", "merges",
                                              "ready_head", "parked", "closed", "opened_at")}
            for record in records
        ],
    }
    if args.json:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    print(f"State: {root}")
    print(f"Spent: ${report['spent_usd']:.2f} over {report['attempts']} author attempts")
    if outcomes:
        print("Author outcomes: " + ", ".join(f"{key} {value}" for key, value in sorted(outcomes.items())))
    if report["stop_file"]:
        print(f"STOP file present: {report['stop_file']}")
    for record in report["pull_requests"]:
        if record.get("closed"):
            status = str(record["closed"])
        elif record.get("parked"):
            status = f"parked ({record['parked']})"
        elif record.get("ready_head"):
            status = "merge-ready at " + str(record["ready_head"])[:12]
        else:
            status = "open"
        print(f"#{record['number']} {status} - {record.get('task')} {record.get('url') or ''}".rstrip())
    return 0
