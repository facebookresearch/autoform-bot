"""One worker of the loop: rounds, pull-request maintenance and author attempts."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import signal
import socket
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..claims import ClaimBoard, ClaimTransportError, author_claim_key
from . import gitops, prstate, verify
from .agents import AgentRunner, AgentSpec, Price, Stopped, Usage, agent_env, read_report
from .github import GitHub, GitHubError
from .gitops import GitError
from .profile import Profile
from .state import State, parse_time, utc_now
from .tasks import TaskError, TaskList, candidates, claim_target, declarations, load_tasks, task_id, tasks_named_by

PROMPTS = Path(__file__).resolve().parent / "prompts"
_MARKER = re.compile(r"<!-- autoform-work (\{.*?\}) -->")
_RULE_FILES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "REVIEWING.md")
_MAX_BODY = 60000
SNAPSHOT_SECONDS = 120.0
TASKS_SECONDS = 600.0
# How many unscored tasks one round may have the triage agent rate before it gives up for the round.
TRIAGE_PER_ROUND = 8
BACKOFF_SECONDS = 900.0
# Round outcomes that count towards stopping a worker after --max-failures in a row, and those that reset it.
FAILED_OUTCOMES = frozenset({"error", "failed", "verify-failed", "publish-check-failed"})
PRODUCTIVE_OUTCOMES = frozenset({"pr-opened", "pushed", "answered", "contested", "no-change", "dry-run-ready"})


class WorkerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    repository: str
    checkout: Path
    base_branch: str
    push_repository: str
    claims_repo: str
    login: str
    author: tuple[str, str]
    plugin_root: Path
    agent: AgentSpec
    worker_id: str
    run_id: str
    branch_prefix: str
    loop: bool = False
    dry_run: bool = False
    tasks_override: str | None = None
    only_task: str | None = None
    phases: tuple[str, ...] = ("statement", "proof")
    budget: float | None = None
    attempt_budget: float | None = None
    max_attempts: int | None = None
    max_open_prs: int = 5
    max_hours: float | None = None
    max_repairs: int = 3
    max_merges: int = 5
    attempt_timeout: float = 7200.0
    fix_timeout: float = 3600.0
    max_failures: int = 5
    idle_seconds: float = 120.0
    agent_args: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    triage: AgentSpec | None = None
    min_score: int = 4
    price: Price | None = None
    co_author: bool = True
    labels: tuple[str, ...] = ()
    started: float = field(default_factory=time.time)


def render(name: str, values: Mapping[str, str]) -> str:
    """Fill ``{{key}}`` fields of a prompt template; an unknown field is an error."""

    text = (PROMPTS / name).read_text(encoding="utf-8")

    def field_value(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"prompt {name} has no value for {key}")
        return values[key]

    return re.sub(r"\{\{(\w+)\}\}", field_value, text)


def parse_marker(body: str) -> dict[str, object]:
    match = _MARKER.search(body or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def marker(fields: Mapping[str, object]) -> str:
    return f"<!-- autoform-work {json.dumps(dict(fields), sort_keys=True, separators=(',', ':'))} -->"


def co_author_line(spec: AgentSpec) -> str:
    if spec.backend == "claude":
        return "Co-authored-by: Claude <noreply@anthropic.com>"
    return "Co-authored-by: Codex <noreply@openai.com>"


def slug(text: str, limit: int = 40) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", text)
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return value[:limit].rstrip("-") or "task"


def pull_request_text(
    report: Mapping[str, object], task: Mapping[str, object], *, profile: Profile, repository: str,
    spec: AgentSpec, login: str, fields: Mapping[str, object],
) -> tuple[str, str]:
    """The title and body to open: the agent's, plus the lines every loop pull request carries."""

    title = " ".join(str(report.get("pr_title") or "").split())[:100]
    if not title:
        names = declarations(task)
        title = f"Prove {names[0].rsplit('.', 1)[-1]}" if names else f"Formalize {task.get('title') or task_id(task)}"
    body = str(report.get("pr_body") or report.get("summary") or "").strip()
    article = str(task.get("article_id") or "")
    lines = [line for line in (f"Tasks: {article}" if article else "", *profile.body_lines) if line]
    lines.append(f"Autoform project: {repository}/{profile.blueprint}")
    disclosure = (
        f"Opened by `autoform work`, AutoformBot's unattended worker loop, for {login}; "
        # The model is left out: an operator's model id can name a private deployment.
        f"the change was written by {'Claude Code' if spec.backend == 'claude' else 'Codex'}"
        " and checked by the loop before it was pushed."
    )
    tail = "\n\n" + "\n".join(lines) + "\n\n---\n" + disclosure + "\n" + marker(fields) + "\n"
    body = body[: _MAX_BODY - len(tail)].rstrip() + tail
    return title, body


class Worker:
    def __init__(self, settings: Settings, profile: Profile, state: State, github: GitHub,
                 runner: AgentRunner | None = None, *, env: Mapping[str, str] | None = None,
                 rng: random.Random | None = None, sleep=time.sleep) -> None:
        self.s = settings
        self.profile = profile
        self.state = state
        self.github = github
        self.env = dict(os.environ if env is None else env)
        scratch = state.worker_dir() / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.scratch = scratch
        path = os.pathsep.join([str(Path(sys.executable).parent), self.env.get("PATH", "")])
        self.agent_env = {**self.env, "PATH": path, "AUTOFORM_PLUGIN_ROOT": str(settings.plugin_root)}
        # Agents, and the checks run on what they wrote, see no credentials (see agents.agent_env).
        self.check_env = agent_env(self.agent_env, scratch=scratch, checkout=settings.checkout)
        self.runner = runner or AgentRunner(env=self.agent_env, scratch=scratch, checkout=settings.checkout,
                                            extra_args=settings.agent_args, price=settings.price)
        self.worktree = state.worker_dir() / "worktree"
        self.board = ClaimBoard(settings.claims_repo, settings.worker_id,
                                state.root / "claims-scratch" / settings.worker_id,
                                host=settings.worker_id.rsplit("-w", 1)[0])
        self._pushed = False
        self.rng = rng or random.Random()
        self.sleep = sleep
        self.stopping = threading.Event()
        self.stop_reason = ""
        self.failures = 0
        self._required: tuple[float, frozenset[tuple[str, int | None]]] = (0.0, frozenset())
        self._base_failures: dict[str, tuple[float, frozenset[str]]] = {}

    # ---------------------------------------------------------------- the loop

    def request_stop(self, reason: str) -> None:
        self.stop_reason = self.stop_reason or reason
        self.stopping.set()
        self.runner.stop()

    def install_signal_handlers(self) -> None:
        def handler(signum: int, frame: object) -> None:
            self.request_stop(f"signal {signal.Signals(signum).name}")

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def run(self) -> int:
        self.state.event("worker-start", agent=self.s.agent.label, loop=self.s.loop, dry_run=self.s.dry_run,
                         host=socket.gethostname(), pid=os.getpid())
        self.release_stale_claims()
        status = 0
        while True:
            reason = self.should_stop()
            if reason:
                self.state.event("worker-stop", reason=reason)
                print(f"autoform work: {self.s.worker_id} stopped: {reason}", flush=True)
                return status
            try:
                result = self.round()
            except Stopped:
                continue
            except (GitHubError, GitError, TaskError, ClaimTransportError, WorkerError, OSError) as error:
                result = {"phase": "error", "outcome": "error", "error": str(error)[-2000:]}
                status = 1
            if result.get("outcome") in FAILED_OUTCOMES:
                self.failures += 1
            elif result.get("outcome") in PRODUCTIVE_OUTCOMES:
                self.failures = 0
            self.state.event("round", **result)
            print(f"autoform work: {self.s.worker_id}: {json.dumps(result, sort_keys=True, default=str)[:600]}", flush=True)
            if result.get("outcome") in ("done", "nothing-left"):
                self.state.event("worker-stop", reason=str(result.get("outcome")))
                return 0
            if not self.s.loop:
                return status if result.get("phase") != "error" else 1
            pause = self.s.idle_seconds if result.get("phase") in ("idle", "error") else 5.0
            self.stopping.wait(pause)

    def should_stop(self) -> str:
        if self.stopping.is_set():
            return self.stop_reason or "stop requested"
        stop = self.state.stop_file()
        if stop is not None:
            return f"{stop} exists"
        if self.s.budget is not None and self.state.spent(run=self.s.run_id) >= self.s.budget:
            return f"the budget of ${self.s.budget:g} is spent"
        halted = self.state.root / f"budget-stop-{self.s.run_id}"
        if halted.is_file():
            return halted.read_text(encoding="utf-8").strip() or "the run's budget cannot be kept"
        if self.s.max_hours is not None and time.time() - self.s.started >= self.s.max_hours * 3600:
            return f"ran {self.s.max_hours:g} hours"
        if self.failures >= self.s.max_failures:
            return f"{self.failures} rounds in a row failed"
        return ""

    def round(self) -> dict[str, object]:
        if not self.s.dry_run:
            self.retry_releases()
            self.recover_pending()
        prs = self.loop_pull_requests()
        # A dry run changes nothing on GitHub, so it leaves the loop's pull requests alone.
        result = None if self.s.dry_run else self.maintain(prs)
        if result is not None:
            return result
        if self.should_stop():
            return {"phase": "idle", "outcome": "stopping"}
        if self.s.max_attempts is not None and self.state.attempts(run=self.s.run_id) >= self.s.max_attempts:
            open_work = [pr for pr in prs if not self.state.pr_record(int(pr["number"])).get("parked")
                         and self.state.pr_record(int(pr["number"])).get("ready_head") != pr["head"]]
            # A pushed branch still waiting for its pull request is work too: its claim is held until then.
            pending = self.pending_records()
            if self.s.loop and (open_work or pending):
                return {"phase": "idle", "outcome": "following", "open": len(open_work), "pending": len(pending)}
            return {"phase": "idle", "outcome": "done", "reason": f"{self.s.max_attempts} attempts made"}
        if self.capacity_used(prs) >= self.s.max_open_prs:
            return {"phase": "idle", "outcome": "backpressure", "open": len(prs)}
        if self.backing_off():
            return {"phase": "idle", "outcome": "rate-limit backoff"}
        return self.author(prs)

    # ---------------------------------------------------------------- shared caches

    def cached(self, name: str, max_age: float, compute):
        path = self.state.root / "cache" / f"{name}.json"
        with self.state.lock(f"cache-{name}"):
            data = self.state.read_json(path)
            if isinstance(data, dict) and time.time() - float(data.get("at", 0)) < max_age:
                return data["value"]
            value = compute()
            self.state.write_json(path, {"at": time.time(), "value": value})
            return value

    def invalidate(self, name: str) -> None:
        (self.state.root / "cache" / f"{name}.json").unlink(missing_ok=True)

    def invalidate_pull_requests(self) -> None:
        self.invalidate(f"pull-requests-{self.s.login.lower()}")

    def loop_pull_requests(self, max_age: float = SNAPSHOT_SECONDS) -> list[dict[str, object]]:
        """The loop's open pull requests on the repository, oldest first, from a snapshot shared by workers."""

        def fetch() -> list[dict[str, object]]:
            return self.github.open_pull_requests_by(self.s.repository, self.s.login)

        prs = [pr for pr in self.cached(f"pull-requests-{self.s.login.lower()}", max_age, fetch) if self.owns(pr)]
        if not self.s.dry_run:
            # Recording a closure releases its claim: a dry run, which releases nothing, records nothing either.
            self.notice_closed(prs)
        return sorted(prs, key=lambda pr: parse_time(pr.get("created_at")))

    def owns(self, pr: Mapping[str, object]) -> bool:
        """Whether this worker tends ``pr``: the operator opened it with the loop's marker, against the base
        branch, from a branch under the prefix on the repository this worker pushes to."""

        return (
            str(pr.get("author") or "").lower() == self.s.login.lower()
            and bool(parse_marker(str(pr.get("body") or "")).get("task"))
            and str(pr.get("base") or "") == self.s.base_branch
            and str(pr.get("head_repository") or "").lower() == self.s.push_repository.lower()
            and str(pr.get("branch") or "").startswith(self.s.branch_prefix)
        )

    def required_checks(self) -> frozenset[tuple[str, int | None]]:
        at, value = self._required
        if time.time() - at > 1800:
            value = self.github.required_checks(self.s.repository, self.s.base_branch)
            self._required = (time.time(), value)
        return value

    def base_failures(self, ref: str) -> frozenset[str]:
        """Checks that failed on the base branch's tip, read again every ten minutes."""

        at, value = self._base_failures.get(ref, (0.0, frozenset()))
        if time.time() - at > 600:
            value = self.github.failed_checks_on(self.s.repository, ref)
            self._base_failures[ref] = (time.time(), value)
        return value

    def backing_off(self) -> bool:
        try:
            return time.time() < float((self.state.root / "backoff-until").read_text())
        except (OSError, ValueError):
            return False

    def back_off(self) -> None:
        (self.state.root / "backoff-until").write_text(str(time.time() + BACKOFF_SECONDS))
        self.state.event("backoff", seconds=BACKOFF_SECONDS)

    def fetch_base(self) -> str:
        with self.state.lock("git"):
            return gitops.fetch_branch(self.s.checkout, self.s.repository, self.s.base_branch, env=self.env)

    # ---------------------------------------------------------------- claims

    def board_for(self, owner: str, repository: str | None = None) -> ClaimBoard:
        # The board may be public: its leases name the worker, never this machine.
        repository = repository or self.s.claims_repo
        scratch = self.state.root / "claims-scratch" / owner / hashlib.sha1(str(repository).encode()).hexdigest()[:10]
        return ClaimBoard(repository, owner, scratch, host=owner.rsplit("-w", 1)[0])

    def claim_note(self, task: Mapping[str, object], **extra: object) -> str:
        return json.dumps({"worker": self.s.worker_id, "task": task_id(task), "since": utc_now(), **extra},
                          sort_keys=True)

    def release(self, target: str, reason: str, owner: str | None = None, pr: int | None = None,
                board_repository: str | None = None) -> bool:
        """Release a claim this worker holds or, for a pull request's claim, the worker that opened it holds.

        A release that fails or is refused is kept as an obligation and retried every round until the claim
        is gone; returns whether it is gone now.
        """

        if self.s.dry_run:
            return True
        owner = owner or self.s.worker_id
        repository = board_repository or str(self.s.claims_repo)
        same = owner == self.s.worker_id and repository == str(self.s.claims_repo)
        board = self.board if same else self.board_for(owner, repository)
        key = author_claim_key(target)
        tag = hashlib.sha1(json.dumps([self.origin(), repository, owner]).encode()).hexdigest()[:10]
        obligation = self.state.root / "releases" / f"{tag}-{key.replace('/', '__')}.json"
        # Written first, so that no crash between here and the release forgets it.
        self.state.write_json(obligation, {"target": target, "owner": owner, "reason": reason, "pr": pr,
                                           "board": repository, "at": utc_now(), **self.origin()})
        try:
            # True also when the claim is already gone; False when another owner holds it or the update lost a race.
            gone = board.release(key) or not any(lease.get("owner") == owner for lease in board.list([key]))
        except (ClaimTransportError, ValueError) as error:
            self.state.event("claim-release-failed", target=target, reason=reason, error=str(error)[-500:])
            gone = False
        else:
            self.state.event("released" if gone else "release-refused", target=target, reason=reason)
        if gone:
            obligation.unlink(missing_ok=True)
            if pr is not None:
                self.state.update_pr(pr, claim_released=True)
        self.invalidate("claims")
        return gone

    def retry_releases(self) -> None:
        for path in sorted((self.state.root / "releases").glob("*.json")):
            data = self.state.read_json(path, {})
            if isinstance(data, dict) and data.get("target") and self.mine(data):
                with self.state.try_lock("release-" + path.stem) as held:
                    if held:
                        number = data.get("pr")
                        self.release(str(data["target"]), str(data.get("reason") or "retried"),
                                     owner=str(data.get("owner") or "") or None,
                                     pr=number if isinstance(number, int) else None,
                                     board_repository=str(data.get("board") or "") or None)

    def release_stale_claims(self) -> None:
        """Release this worker's claims that name no pull request: a previous run stopped mid-attempt."""

        try:
            leases = self.board.list()
        except (ClaimTransportError, ValueError) as error:
            self.state.event("claim-list-failed", error=str(error)[-500:])
            return
        # A pushed branch's claim stays: recover_pending opens its pull request.
        pending = {str(data.get("claim")) for data in self.pending_records()}
        for lease in leases:
            if lease.get("owner") != self.s.worker_id or lease.get("_malformed"):
                continue
            try:
                note = json.loads(str(lease.get("note") or "{}"))
            except ValueError:
                note = {}
            if isinstance(note, dict) and not note.get("pr") and lease.get("target") and lease["target"] not in pending:
                self.release(str(lease["target"]), "left by a stopped run")

    def claimed_by_others(self) -> set[str]:
        def fetch() -> list[list[str]]:
            return sorted([str(lease.get("target")), str(lease.get("owner"))] for lease in self.board.list()
                          if lease.get("target"))

        try:
            leases = self.cached("claims", 120.0, fetch)
        except (ClaimTransportError, ValueError):
            return set()
        return {target for target, owner in leases if owner != self.s.worker_id}

    # ---------------------------------------------------------------- maintenance

    def notice_closed(self, open_prs: Sequence[Mapping[str, object]]) -> None:
        """Record loop pull requests that were merged or closed, and release their claims."""

        open_numbers = {int(pr["number"]) for pr in open_prs}
        for path in (self.state.root / "prs").glob("*.json"):
            record = self.state.read_json(path, {})
            if not isinstance(record, dict) or record.get("closed") or not isinstance(record.get("number"), int):
                continue
            if str(record.get("push_repository") or self.s.push_repository).lower() != self.s.push_repository.lower():
                continue
            number = int(record["number"])
            if number in open_numbers:
                continue
            with self.state.try_lock(f"pr-{number}") as held:
                if not held:
                    continue
                try:
                    pr = self.github.pull_request(self.s.repository, number)
                except GitHubError:
                    continue
                if pr.get("state") == "OPEN":
                    continue
                outcome = "merged" if pr.get("merged_at") else "closed"
                if record.get("claim") and not record.get("claim_released"):
                    # The release, or its obligation that later rounds retry, comes before the closure is recorded.
                    self.release(str(record["claim"]), f"pull request {outcome}", owner=record.get("worker"),
                                 pr=number, board_repository=record.get("claims_repo"))
                self.state.update_pr(number, closed=outcome, closed_at=utc_now())
                self.state.event("pr-" + outcome, pr=number, task=record.get("task"))

    def maintain(self, prs: Sequence[dict[str, object]]) -> dict[str, object] | None:
        """Do the first maintenance unit a loop pull request needs; ``None`` when none needs an agent."""

        required = self.required_checks()
        for pr in prs:
            number = int(pr["number"])
            record = self.state.pr_record(number)
            if record.get("pending_park") and not record.get("parked"):
                with self.state.try_lock(f"pr-{number}") as held:
                    record = self.state.pr_record(number)
                    if held and record.get("pending_park") and not record.get("parked"):
                        fresh = self.github.pull_request(self.s.repository, number)
                        if fresh.get("state") == "OPEN" and self.owns(fresh):
                            self.park(fresh, str(record["pending_park"]), public=str(record.get("pending_public") or ""))
                        else:
                            # Closed, or no longer the loop's: the notice is not ours to post any more.
                            self.state.update_pr(number, pending_park=None, pending_public=None)
                            self.state.event("park-notice-dropped", pr=number)
                continue
            action = self.decide(pr, record, required)
            if action.kind == "wait":
                continue
            with self.state.try_lock(f"pr-{number}") as held:
                if not held:
                    continue
                pr = self.github.pull_request(self.s.repository, number)
                if not self.owns(pr):
                    continue
                record = self.state.pr_record(number)
                action = self.decide(pr, record, required)
                if action.kind in ("wait", "closed"):
                    continue
                if action.kind == "ready":
                    self.mark_ready(pr, record)
                    continue
                if action.kind == "park":
                    self.park(pr, action.reason, public=action.reason)
                    continue
                result = self.repair(pr, record, action)
                self.invalidate_pull_requests()
                return result
        return None

    def decide(self, pr: Mapping[str, object], record: Mapping[str, object],
               required: frozenset[str] | None) -> prstate.Action:
        return prstate.next_action(
            pr, record, required=required, review_bot=self.profile.review_bot,
            review_markers=self.profile.review_markers, review_checks=self.profile.review_checks,
            base_failures=self.base_failures(self.s.base_branch), max_repairs=self.s.max_repairs,
            max_merges=self.s.max_merges,
        )

    def mark_ready(self, pr: Mapping[str, object], record: Mapping[str, object]) -> None:
        number = int(pr["number"])
        if record.get("ready_head") == pr.get("head"):
            return
        self.state.update_pr(number, ready_head=pr.get("head"), ready_at=utc_now())
        self.state.event("merge-ready", pr=number, head=pr.get("head"), task=record.get("task"))
        claim = record.get("claim") or parse_marker(str(pr.get("body") or "")).get("claim")
        if claim and not record.get("claim_released"):
            # Recorded as released only once it is; otherwise the obligation is retried every round.
            self.release(str(claim), "merge-ready; the open pull request is the public claim",
                         owner=record.get("worker"), pr=number, board_repository=record.get("claims_repo"))

    def posted(self, pr: Mapping[str, object], **fields: object) -> bool:
        """Whether the operator already posted a loop comment whose marker holds ``fields``: a write whose
        response was lost may have happened, and is not made twice."""

        for comment in pr.get("comments") or []:  # type: ignore[union-attr]
            if str(comment.get("author") or "").lower() != self.s.login.lower():
                continue
            found = parse_marker(str(comment.get("body") or ""))
            if found and all(found.get(key) == value for key, value in fields.items()):
                return True
        return False

    def answered_ids(self, pr: Mapping[str, object]) -> set[str]:
        """The comment ids the operator's loop replies on ``pr`` say they answer."""

        ids: set[str] = set()
        for comment in pr.get("comments") or []:  # type: ignore[union-attr]
            if str(comment.get("author") or "").lower() == self.s.login.lower():
                found = parse_marker(str(comment.get("body") or ""))
                if found.get("reply") and isinstance(found.get("to"), list):
                    ids.update(str(item) for item in found["to"])
        return ids

    def park(self, pr: Mapping[str, object], reason: str, public: str = "") -> None:
        """Stop changing a pull request, saying so once in a comment. The comment gives ``public``, a fixed
        sentence or one made of GitHub's own check names, never ``reason``, which may quote local output."""

        number = int(pr["number"])
        why = public or "it needs a decision or a repair the loop could not make"
        body = (
            f"The worker loop stopped changing this pull request: {why}. It needs a person now; "
            "the loop will not push to it again.\n\n" + marker({"parked": True})
        )
        if self.s.dry_run:
            return
        # Recorded before the notice goes out, so that a failed post is retried (maintain) rather than forgotten.
        self.state.update_pr(number, pending_park=reason, pending_public=why)
        if not self.posted(pr, parked=True):
            self.github.comment(self.s.repository, number, body)
        self.state.update_pr(number, parked=reason, parked_at=utc_now(), pending_park=None, pending_public=None)
        self.state.event("parked", pr=number, reason=reason)

    def repair(self, pr: dict[str, object], record: Mapping[str, object], action: prstate.Action) -> dict[str, object]:
        number = int(pr["number"])
        out: dict[str, object] = {"phase": action.kind, "pr": number, "reason": action.reason}
        head = str(pr["head"])
        push_url = gitops.remote_url(str(pr["head_repository"] or self.s.push_repository))
        with self.state.lock("git"):
            fetched = gitops.fetch_commit(self.s.checkout, push_url, str(pr["branch"]), env=self.env)
        if fetched != head:
            return {**out, "outcome": "moved", "detail": f"the branch is at {fetched}, GitHub said {head}"}
        base_tip = self.fetch_base()
        self.ensure_worktree(base_tip)
        # The statements are read on the base the change is diffed against: the base tip after a merge of it,
        # else where the branch left the base.
        base = base_tip if action.kind == "merge-base" else gitops.merge_base(self.s.checkout, head, base_tip)
        targets, problem = self.targets_at(pr, record, base)
        if problem:
            self.park(pr, problem, public="the base branch no longer has this task's wanted statement as it was")
            return {**out, "outcome": "parked", "reason": problem}
        gitops.reset_worktree(self.worktree, head)
        gitops.prepare_lake(self.worktree, self.s.checkout)
        handled_key = {"merge-base": f"merge:{head}", "fix-ci": f"ci:{head}"}.get(action.kind)
        if action.kind == "fix-review":
            handled_key = str((action.data or {}).get("id"))  # type: ignore[union-attr]
        values = self.pr_values(pr, record)
        if action.kind == "merge-base":
            merged = gitops.git(self.worktree, "merge", "--no-ff", "--no-commit", base_tip, check=False)
            report: dict[str, object] = {"status": "done"}
            if merged.returncode != 0:
                usage, report = self.agent_round("merge_base.md", {**values, "base": base_tip}, label=f"merge-{number}")
                out["usage"] = usage.as_dict()
                if usage.rate_limited:
                    self.back_off()
                    return {**out, "outcome": "rate-limited"}
            self.state.update_pr(number, inc_merges=1)
            if report.get("status") == "obsolete":
                reason = "the base branch now proves or removes this task's declarations; close it"
                self.park(pr, reason, public=reason)
                return {**out, "outcome": "obsolete"}
            if report.get("status") != "done":
                self.state.update_pr(number, add_handled=[handled_key])
                return {**out, "outcome": str(report.get("status") or "failed"), "notes": report.get("notes")}
            return {**out, **self.finish(pr, record, base, targets, f"Merge {self.s.base_branch} into {pr['branch']}",
                                         handled_key)}
        if action.kind == "fix-review":
            review = action.data or {}
            findings = self.review_text(pr, review)  # type: ignore[arg-type]
            prompt, values = "fix_review.md", {**values, "findings": findings}
        elif action.kind == "fix-ci":
            prompt, values = "fix_ci.md", {**values, "findings": self.ci_text(pr, action.data or [])}  # type: ignore[arg-type]
        else:
            people = action.data or []
            # Comments a reply already answers (one whose response was lost, say) are not answered again.
            answered = self.answered_ids(pr) & {str(comment.get("id")) for comment in people}  # type: ignore[union-attr]
            if answered:
                self.state.update_pr(number, add_handled=sorted(answered))
            people = [comment for comment in people if str(comment.get("id")) not in answered]  # type: ignore[union-attr]
            ids = [str(comment.get("id")) for comment in people]  # type: ignore[union-attr]
            if not people:
                return {**out, "outcome": "answered", "comments": sorted(answered)}
            prompt, values = "human.md", {**values, "findings": self.comments_text(people)}  # type: ignore[arg-type]
            handled_key = None
            out["comments"] = ids
        usage, report = self.agent_round(prompt, values, label=f"{action.kind}-{number}")
        out["usage"] = usage.as_dict()
        if usage.rate_limited:
            self.back_off()
            return {**out, "outcome": "rate-limited"}
        status = str(report.get("status") or "failed")
        changed = bool(gitops.git(self.worktree, "status", "--porcelain").stdout.strip())
        if action.kind == "human":
            # The comments count as handled only once answered, or once the pull request is durably parked.
            ids = list(out["comments"])  # type: ignore[call-overload]

            def park_for(reason: str, public: str) -> dict[str, object]:
                self.park(pr, reason, public=public)
                self.state.update_pr(number, add_handled=ids)
                return {**out, "outcome": "parked"}

            reply = str(report.get("reply") or "").strip()
            if status not in ("done", "reply") or (status == "reply" and not reply):
                return park_for("a person's comment needs a person: " + str(report.get("notes") or status)[:300],
                                "a comment asks for something the loop leaves to a person")
            if status == "done" and changed:
                result = self.finish(pr, record, base, targets,
                                     str(report.get("commit_message") or "Address review comments"), None)
                if result.get("outcome") != "pushed":
                    return {**park_for(f"a change a person asked for did not pass: {result.get('outcome')}",
                                       "a change a comment asks for did not pass the loop's checks"), **result,
                            "outcome": "parked"}
            if reply and not self.reply(pr, reply, to=sorted(ids)):
                return park_for("the reply to a person did not pass the publication check",
                                "a reply the loop wrote did not pass the repository's publication check")
            self.state.update_pr(number, add_handled=ids)
            return {**out, "outcome": "answered"}
        self.state.update_pr(number, inc_repairs=1)
        if status != "done":
            self.state.update_pr(number, add_handled=[handled_key])
            return {**out, "outcome": status, "notes": str(report.get("notes") or "")[:1000]}
        if not changed:
            contested = report.get("contested") or []
            if action.kind == "fix-review" and contested:
                lines = [f"- **{item.get('finding')}**: {item.get('reason')}" for item in contested if isinstance(item, dict)]
                if not self.reply(pr, "Checked the review's findings; no change made:\n\n" + "\n".join(lines),
                                  review=str(handled_key)):
                    self.park(pr, "the reply contesting the review did not pass the publication check",
                              public="a reply the loop wrote did not pass the repository's publication check")
                    self.state.update_pr(number, add_handled=[handled_key])
                    return {**out, "outcome": "parked"}
            self.state.update_pr(number, add_handled=[handled_key])
            return {**out, "outcome": "contested" if contested else "no-change"}
        return {**out, **self.finish(pr, record, base, targets,
                                     str(report.get("commit_message") or "Address review findings"), handled_key)}

    def finish(self, pr: Mapping[str, object], record: Mapping[str, object], base: str,
               targets: dict[str, dict[str, object]] | None, message: str, handled_key: str | None) -> dict[str, object]:
        """Verify, commit and push a repair of a loop pull request, diffed against ``base``."""

        number = int(pr["number"])
        check = verify.run_steps(self.worktree, base=base, profile=self.profile, env=self.check_env,
                                 targets=targets, phase=str(record.get("phase") or "proof"))
        if not check.ok:
            if handled_key:
                self.state.update_pr(number, add_handled=[handled_key])
            return {"outcome": "verify-failed", "verify": check.brief(), "failed": check.failures()[:3]}
        sha = gitops.commit(self.worktree, message.strip().splitlines()[0][:200] or "Update", author=self.s.author,
                            tree=check.tree)
        ok, detail = verify.publish_check(self.worktree, profile=self.profile, base=base, head=sha,
                                          metadata={"branch": str(pr["branch"])}, scratch=self.scratch,
                                          env=self.check_env, public=(base, str(pr["head"])))
        if not ok:
            if handled_key:
                self.state.update_pr(number, add_handled=[handled_key])
            return {"outcome": "publish-check-failed", "detail": detail[-2000:]}
        if self.s.dry_run:
            return {"outcome": "dry-run", "head": sha}
        gitops.safe_push(self.worktree, url=gitops.remote_url(str(pr["head_repository"] or self.s.push_repository)),
                         commit_sha=sha, branch=str(pr["branch"]), old=str(pr["head"]), prefix=self.s.branch_prefix,
                         protected=self.protected(), gh=self.github.gh, env=self.env)
        if handled_key:
            self.state.update_pr(number, add_handled=[handled_key])
        self.state.event("pushed", pr=number, branch=pr["branch"], head=sha)
        return {"outcome": "pushed", "head": sha, "verify": check.brief()}

    def reply(self, pr: Mapping[str, object], text: str, **about: object) -> bool:
        """Post an agent-written reply once it passes the publication check; whether it is on the pull request.

        ``about`` (the comments or review it answers) goes into its marker, so a reply whose response was lost
        is found rather than posted twice.
        """

        if self.posted(pr, reply=True, **about):
            return True
        ok, detail = verify.publish_check(self.worktree, profile=self.profile, base=str(pr["head"]),
                                          head=str(pr["head"]), metadata={"body": text}, scratch=self.scratch,
                                          env=self.check_env, public=(str(pr["head"]),))
        if not ok:
            self.state.event("reply-withheld", pr=pr["number"], detail=detail[-1000:])
            return False
        if not self.s.dry_run:
            self.github.comment(self.s.repository, int(pr["number"]),
                                text + "\n\n" + marker({"reply": True, **about}))
        return True

    def pr_values(self, pr: Mapping[str, object], record: Mapping[str, object]) -> dict[str, str]:
        block = f"#{pr['number']}: {pr['title']}\nBranch: {pr['branch']}\n\n{pr.get('body') or ''}"
        return {"number": str(pr["number"]), "head": str(pr["head"]), "pr_block": block[:20000]}

    def review_text(self, pr: Mapping[str, object], review: Mapping[str, object]) -> str:
        lines = [str(review.get("body") or "")]
        for comment in pr.get("comments") or []:  # type: ignore[union-attr]
            if comment.get("kind") == "line" and comment.get("review") == review.get("id"):
                lines.append(f"\n### On `{comment.get('path')}` line {comment.get('line')}\n\n{comment.get('body')}")
        return "\n".join(lines)[:30000]

    def ci_text(self, pr: Mapping[str, object], failing: Sequence[str]) -> str:
        chunks = []
        for key, check in prstate.latest_checks(pr).items():
            if prstate.check_label(key) not in failing:
                continue
            log = ""
            job = re.search(r"/job/(\d+)", str(check.get("url") or ""))
            if job:
                text = self.github.job_log(self.s.repository, job.group(1))
                errors = [line for line in text.splitlines() if re.search(r"error|fail|✖", line, re.IGNORECASE)]
                log = "\n".join(errors[-80:]) + "\n--- last lines ---\n" + "\n".join(text.splitlines()[-60:])
            chunks.append(f"### {prstate.check_label(key)}: {check.get('conclusion')} ({check.get('url')})\n{log}")
        return "\n\n".join(chunks)[-30000:]

    @staticmethod
    def comments_text(comments: Sequence[Mapping[str, object]]) -> str:
        return "\n\n".join(
            f"### {comment.get('kind')} by @{comment.get('author')} at {comment.get('created_at')}"
            + (f" on `{comment.get('path')}`" if comment.get("path") else "")
            + f"\n\n{comment.get('body')}"
            for comment in comments
        )[:30000]

    # ---------------------------------------------------------------- agents

    def common_values(self) -> dict[str, str]:
        rules = [name for name in _RULE_FILES if (self.worktree / name).is_file()]
        return {
            "repository": self.s.repository,
            "worktree": str(self.worktree),
            "rules_files": ", ".join(f"`{name}`" for name in rules) or "the repository's README",
            "plugin_root": str(self.s.plugin_root),
            "instructions": self.profile.instructions,
            "report_path": str(self.report_path),
        }

    @property
    def report_path(self) -> Path:
        return self.state.worker_dir() / "report.json"

    def agent_round(self, prompt_name: str, values: Mapping[str, str], *, label: str,
                    timeout: float | None = None, cwd: Path | None = None) -> tuple[Usage, dict[str, object]]:
        self.report_path.unlink(missing_ok=True)
        merged = {**self.common_values(), **values}
        prompt = render("common.md", merged) + "\n" + render(prompt_name, merged)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        transcript = self.state.worker_dir() / "transcripts" / f"{stamp}-{label}.jsonl"
        usage = self.run_agent(self.s.agent, prompt, label=label, cwd=cwd or self.worktree, transcript=transcript,
                               timeout=timeout or self.s.fix_timeout, budget_usd=self.s.attempt_budget)
        report = read_report(self.report_path)
        self.report_path.unlink(missing_ok=True)
        return usage, report

    def account(self, usage: Usage, label: str, cap: float | None = None) -> None:
        """Record an agent run's cost. A run that ended without saying what it cost counts as --attempt-budget,
        its cap; with no cap, a budgeted loop stops rather than count it as free."""

        estimated = False
        if usage.cost_usd is None and self.s.budget is not None:
            # Only Claude Code enforces --attempt-budget (as --max-budget-usd), so only its runs have a known cap.
            if cap is not None and usage.backend == "claude":
                usage.cost_usd, estimated = cap, True
            else:
                reason = f"the cost of agent run {label} is unknown, so the budget cannot be kept"
                # For the whole run: no peer, and no restart, may spend past it.
                (self.state.root / f"budget-stop-{self.s.run_id}").write_text(reason + "\n", encoding="utf-8")
                self.request_stop(reason)
        self.state.event("agent", label=label, cost_estimated=estimated, **usage.as_dict())

    def run_agent(self, spec: AgentSpec, prompt: str, *, label: str, **options: object) -> Usage:
        """Run an agent and account for its cost, a stopped run's included. Nothing is dispatched once the
        worker should stop (a budget spent, a run-wide stop, a STOP file)."""

        reason = self.should_stop()
        if reason:
            self.request_stop(reason)
            raise Stopped(None)
        cap = options.get("budget_usd")
        cap = float(cap) if isinstance(cap, (int, float)) else None
        try:
            usage = self.runner.run(spec, prompt, **options)  # type: ignore[arg-type]
        except Stopped as stop:
            if stop.usage is not None:
                self.account(stop.usage, label, cap)
            raise
        self.account(usage, label, cap)
        return usage

    # ---------------------------------------------------------------- authoring

    def ensure_worktree(self, start: str) -> bool:
        with self.state.lock("git"):
            gitops.ensure_worktree(self.s.checkout, self.worktree, start)
        self.refresh_env()
        return True

    def refresh_env(self) -> None:
        """Clean the environment of agents and checks against the configuration Git reads where they run: the
        checkout's and, once it exists, the worker worktree's own."""

        places = [self.s.checkout, *([self.worktree] if (self.worktree / ".git").exists() else [])]
        self.check_env = agent_env(self.agent_env, scratch=self.scratch, checkout=places)
        if hasattr(self.runner, "env"):
            self.runner.env = self.check_env

    def task_list(self, max_age: float = TASKS_SECONDS) -> TaskList:
        def fetch() -> dict[str, object]:
            env = dict(self.env)
            try:
                env.setdefault("GH_TOKEN", self.github.token())
            except GitHubError:
                pass
            tasks = load_tasks(self.profile, self.s.checkout, tasks_override=self.s.tasks_override, env=env,
                               open_pulls=lambda: self.github.open_pull_request_texts(self.s.repository))
            return {"items": list(tasks.items), "free": list(tasks.free), "source": tasks.source,
                    "commit": tasks.commit, "taken": tasks.taken}

        data = self.cached("tasks", max_age, fetch)
        return TaskList(items=tuple(data["items"]), free=tuple(data["free"]), source=str(data["source"]),
                        commit=data.get("commit"), taken=int(data.get("taken") or 0))

    def origin(self) -> dict[str, str]:
        """Who made a pending publication or a release obligation: only a worker of the same origin acts on it."""

        return {"login": self.s.login.lower(), "repository": self.s.repository.lower(),
                "push_repository": self.s.push_repository.lower(), "claims_repo": str(self.s.claims_repo)}

    def mine(self, record: Mapping[str, object]) -> bool:
        return all(str(record.get(key) or "") == value for key, value in self.origin().items())

    def pending_records(self, *, everyone: bool = False) -> list[dict[str, object]]:
        """Branches this origin pushed whose pull request is not known to be open yet (see recover_pending);
        with ``everyone``, every origin's (they all hold a task and a slot)."""

        records = [self.state.read_json(path, {}) for path in sorted((self.state.root / "pending").glob("*.json"))]
        return [record for record in records if isinstance(record, dict) and record.get("branch")
                and (everyone or self.mine(record))]

    def capacity_used(self, prs: Sequence[Mapping[str, object]]) -> int:
        """Open pull requests (on GitHub, or opened here and not seen closed), pushed branches waiting for theirs,
        and other workers' attempts under way: what --max-open-prs bounds."""

        numbers = {int(pr["number"]) for pr in prs}
        for path in (self.state.root / "prs").glob("*.json"):
            record = self.state.read_json(path, {})
            if (isinstance(record, dict) and isinstance(record.get("number"), int) and not record.get("closed")
                    and str(record.get("push_repository") or "").lower() == self.s.push_repository.lower()):
                numbers.add(int(record["number"]))
        return len(numbers) + len(self.pending_records()) + len(self.attempts_in_flight())

    def attempts_in_flight(self) -> list[str]:
        """Workers of this state directory with an author attempt under way (their process still runs)."""

        live = []
        for path in (self.state.root / "attempts").glob("*.json"):
            data = self.state.read_json(path, {})
            pid = data.get("pid") if isinstance(data, dict) else None
            try:
                if isinstance(pid, int) and pid != os.getpid():
                    os.kill(pid, 0)
                    live.append(path.stem)
            except ProcessLookupError:
                path.unlink(missing_ok=True)
            except PermissionError:
                live.append(path.stem)
        return live

    def author(self, prs: Sequence[Mapping[str, object]] = ()) -> dict[str, object]:
        tasks = self.task_list()
        own = {str(parse_marker(str(pr.get("body") or "")).get("task")) for pr in prs}
        scores = self.triage_scores()
        low = {task for task, score in scores.items() if score < self.s.min_score} if self.s.triage else set()
        pending = {str(data.get("task")) for data in self.pending_records(everyone=True)}
        skip = self.state.failed_recently() | self.claimed_by_others() | own | low | pending
        picks = candidates(tasks.free, phases=self.s.phases, skip=skip, only=self.s.only_task, rng=self.rng)
        if self.s.triage and not self.s.only_task:
            # Free tasks already rated feasible (one a stopped attempt released, say) come before new picks.
            rated = [task for task in candidates(tasks.free, phases=self.s.phases, skip=skip, rng=self.rng, limit=None)
                     if scores.get(task_id(task), 0) >= self.s.min_score]
            known = {task_id(task) for task in rated}
            picks = rated + [task for task in picks if task_id(task) not in known]
        if picks and self.s.triage and not self.s.only_task:
            picks = self.feasible(picks, scores)
            if not picks:
                return {"phase": "idle", "outcome": "no-feasible-task", "scored": len(scores)}
        if not picks:
            if self.s.only_task:
                return {"phase": "idle", "outcome": "done", "reason": f"task {self.s.only_task} is not free"}
            return {"phase": "idle", "outcome": "no-free-task", "free": len(tasks.free)}
        marker_file = self.state.root / "attempts" / f"{self.s.worker_id}.json"
        with self.state.lock("author-start"):
            # Under the lock, so that workers sharing --max-attempts or --max-open-prs cannot both take the last
            # attempt or the last open pull request.
            if self.s.max_attempts is not None and self.state.attempts(run=self.s.run_id) >= self.s.max_attempts:
                return {"phase": "idle", "outcome": "following", "reason": "the last attempt was taken"}
            # A fresh read: another worker may have opened a pull request since this round began.
            if self.capacity_used(self.loop_pull_requests(max_age=0)) >= self.s.max_open_prs:
                return {"phase": "idle", "outcome": "backpressure", "open": len(prs)}
            for task in picks:
                target = claim_target(task)
                # A dry run claims nothing: it only checks the claim is free.
                if self.s.dry_run or self.board.acquire(author_claim_key(target), note=self.claim_note(task),
                                                        target=target):
                    break
            else:
                return {"phase": "idle", "outcome": "claims-taken", "tried": len(picks)}
            self.state.write_json(marker_file, {"pid": os.getpid(), "task": task_id(task), "at": utc_now()})
            self.state.event("author-start", task=task_id(task), title=task.get("title"), agent=self.s.agent.label)
        self._pushed = False
        try:
            return self.attempt(task, target)
        except BaseException:
            # A pushed branch keeps its claim: the next round opens its pull request (recover_pending).
            if not self._pushed:
                self.release(target, "the attempt stopped")
            raise
        finally:
            marker_file.unlink(missing_ok=True)

    def triage_scores(self) -> dict[str, int]:
        """The triage agent's scores so far, by task id, from every worker of this state directory."""

        scores: dict[str, int] = {}
        for row in self.state.lines(self.state.root / "triage.jsonl"):
            if isinstance(row.get("score"), int) and isinstance(row.get("task"), str):
                scores[row["task"]] = int(row["score"])
        return scores

    def feasible(self, picks: Sequence[dict[str, object]], scores: Mapping[str, int]) -> list[dict[str, object]]:
        """The picks the triage agent rates at least ``--min-score``, rating at most a few new ones per round."""

        assert self.s.triage is not None
        base_tip = self.fetch_base()
        self.ensure_worktree(base_tip)
        gitops.reset_worktree(self.worktree, base_tip)
        chosen: list[dict[str, object]] = []
        rated = 0
        for task in picks:
            score = scores.get(task_id(task))
            if score is None:
                if rated >= TRIAGE_PER_ROUND:
                    break
                rated += 1
                score = self.rate(task)
            if score is not None and score >= self.s.min_score:
                chosen.append(task)
                break
        return chosen

    def rate(self, task: Mapping[str, object]) -> int | None:
        assert self.s.triage is not None
        values = {
            "minutes": str(int(self.s.attempt_timeout // 60)),
            "title": str(task.get("title") or ""),
            "article_path": str(task.get("article_path") or ""),
            "statement": verify.statement_text(self.worktree, declarations(task)) or "(no statement found)",
        }
        transcript = self.state.worker_dir() / "transcripts" / f"{time.strftime('%Y%m%d-%H%M%S')}-triage.jsonl"
        usage = self.run_agent(self.s.triage, render("triage.md", values), label=f"triage-{task_id(task)[:15]}",
                               cwd=self.scratch, transcript=transcript, timeout=900)
        match = re.search(r"\{[^{}]*\}", usage.final_text)
        score: int | None = None
        reason = ""
        if match:
            try:
                verdict = json.loads(match.group(0))
                value = verdict.get("score")
                score = value if isinstance(value, int) and 1 <= value <= 5 else None
                reason = str(verdict.get("reason") or "")
            except ValueError:
                pass
        if usage.rate_limited:
            self.back_off()
            return None
        self.state.append(self.state.root / "triage.jsonl", {"at": utc_now(), "task": task_id(task), "score": score,
                                                              "reason": reason[:300], "worker": self.s.worker_id})
        return score

    def abandon(self, task: Mapping[str, object], target: str, outcome: str, notes: str = "",
                record: bool = True) -> dict[str, object]:
        self.release(target, outcome)
        # A dry run's outcomes stay out of the state a real run reads.
        if record and not self.s.dry_run:
            self.state.record_failure(task_id(task), outcome, notes)
        return {"phase": "author", "task": task_id(task), "outcome": outcome, "notes": notes[-2000:]}

    def base_facts(self, task: Mapping[str, object], commit: str) -> dict[str, dict[str, object]] | str:
        """The base's statement facts of a proof task's declarations, or why there are none."""

        names = declarations(task)
        cache = self.state.root / "facts" / f"{task_id(task)}-{commit[:16]}.json"
        cached = self.state.read_json(cache)
        if isinstance(cached, dict) and set(cached) == set(names):
            return cached
        modules = verify.locate_modules(self.worktree, names)
        missing = [name for name, module in modules.items() if module is None]
        if missing:
            return f"the base's sources declare no buildable {', '.join(missing)}"
        wanted = sorted({module for module in modules.values() if module})
        built = gitops.run_command(["lake", "build", *wanted], cwd=self.worktree, env=self.check_env, timeout=7200)
        if built.returncode != 0:
            return "the base does not build: " + (built.stdout + built.stderr)[-1500:]
        facts = verify.statement_facts(self.worktree, wanted, names, env=self.check_env)
        absent = [name for name in names if not (facts.get(name) or {}).get("found")]
        if absent:
            return f"the statement probe found no {', '.join(absent)} on the base"
        proved = [name for name in names if not facts[name].get("wanted")
                  and not set(facts[name].get("axioms") or []) - verify.STANDARD_AXIOMS]
        if proved and len(proved) == len(names):
            return "the base already proves " + ", ".join(proved)
        self.state.write_json(cache, facts)
        return facts

    def targets_at(self, pr: Mapping[str, object], record: Mapping[str, object],
                   base: str) -> tuple[dict[str, dict[str, object]] | None, str]:
        """The statement facts a repair of a proof pull request is checked against, read on ``base``.

        Returns the facts (``None`` when the fidelity check does not apply) and, when ``base`` no longer has
        the task's wanted declarations, why the pull request needs a person.
        """

        found = parse_marker(str(pr.get("body") or ""))
        phase = str(record.get("phase") or found.get("phase") or "proof")
        names = record.get("declarations") or found.get("declarations") or []
        if not self.profile.statement_fidelity or phase != "proof" or not names:
            return None, ""
        task = {"article_id": record.get("task") or found.get("task"),
                "lean_targets": [{"declaration": name} for name in names]}  # type: ignore[union-attr]
        gitops.reset_worktree(self.worktree, base)
        gitops.prepare_lake(self.worktree, self.s.checkout)
        facts = self.base_facts(task, base)
        if isinstance(facts, str):
            return None, f"the base branch changed under it: {facts}"
        return facts, ""

    def attempt(self, task: dict[str, object], target: str) -> dict[str, object]:
        base_tip = self.fetch_base()
        self.ensure_worktree(base_tip)
        gitops.reset_worktree(self.worktree, base_tip)
        lake = gitops.prepare_lake(self.worktree, self.s.checkout)
        phase = str(task.get("phase") or "proof")
        facts: dict[str, dict[str, object]] | None = None
        if phase == "proof" and self.profile.statement_fidelity and declarations(task):
            found = self.base_facts(task, base_tip)
            if isinstance(found, str):
                return self.abandon(task, target, "stale-task", found)
            facts = found
        values = {
            **self.common_values(),
            "task_block": "```json\n" + json.dumps(task, indent=1, sort_keys=True)[:20000] + "\n```",
            "phase": phase,
            "base": base_tip,
            "minutes": str(int(self.s.attempt_timeout // 60)),
            "verify_hint": "".join(f"; `{' '.join(command)}`" for command in self.profile.verify_commands) + ".",
            "template_hint": (
                "Fill in the repository's template, `.github/pull_request_template.md`, ticking only what is true."
                if (self.worktree / ".github" / "pull_request_template.md").is_file() else ""
            ),
        }
        usage, report = self.agent_round("author.md", values, label=f"author-{task_id(task)[:15]}",
                                         timeout=self.s.attempt_timeout)
        result: dict[str, object] = {"phase": "author", "task": task_id(task), "usage": usage.as_dict(), "lake": lake}
        if usage.rate_limited:
            self.back_off()
            self.release(target, "rate limited")
            return {**result, "outcome": "rate-limited"}
        status = str(report.get("status") or "failed")
        if status != "done":
            outcome = "blocked" if status == "blocked" else "failed"
            notes = str(report.get("notes") or usage.error or ("timed out" if usage.timed_out else ""))
            return {**result, **self.abandon(task, target, outcome, notes)}
        check = verify.run_steps(self.worktree, base=base_tip, profile=self.profile, env=self.check_env,
                                 targets=facts, phase=phase)
        rounds = [check.brief()]
        for attempt in range(2):
            if check.ok:
                break
            findings = "\n\n".join(f"### {step['step']}\n```\n{step['detail']}\n```" for step in check.failures())
            fix_usage, fixed = self.agent_round(
                "fix_verify.md", {**values, "findings": findings[-30000:]}, label=f"fixverify-{task_id(task)[:15]}-{attempt}"
            )
            if fix_usage.rate_limited or fixed.get("status") != "done":
                break
            report = {**report, **{key: value for key, value in fixed.items() if value}}
            check = verify.run_steps(self.worktree, base=base_tip, profile=self.profile, env=self.check_env,
                                     targets=facts, phase=phase)
            rounds.append(check.brief())
        result["verify"] = rounds
        if not check.ok:
            failed = "\n".join(f"{step['step']}: {str(step['detail'])[-500:]}" for step in check.failures())
            return {**result, **self.abandon(task, target, "verify-failed", failed)}
        return {**result, **self.publish(task, target, report, base_tip, check)}

    def branch_name(self, task: Mapping[str, object]) -> str:
        names = declarations(task)
        stem = names[0].rsplit(".", 1)[-1] if names else str(task.get("title") or "task")
        base = f"{self.s.branch_prefix}{slug(stem)}-{str(task.get('article_id') or task_id(task))[3:9]}"
        url = gitops.remote_url(self.s.push_repository)
        name, n = base, 1
        while gitops.remote_branch(url, name, gh=self.github.gh, env=self.env) is not None:
            n += 1
            name = f"{base}-{n}"
        return name

    def protected(self) -> list[str]:
        """Branches never pushed: the base branch, the push repository's default branch, ``main`` and ``master``."""

        # Read again before every push: the push repository's default branch can change.
        default = str(self.github.repository(self.s.push_repository).get("default_branch") or "")
        return sorted({self.s.base_branch, "main", "master", *([default] if default else [])})

    def publish(self, task: dict[str, object], target: str, report: Mapping[str, object], base_tip: str,
                check: verify.Verification) -> dict[str, object]:
        names = declarations(task)
        fields = {"v": 1, "task": task_id(task), "claim": target, "phase": task.get("phase"), "declarations": names}
        title, body = pull_request_text(report, task, profile=self.profile, repository=self.s.repository,
                                        spec=self.s.agent, login=self.s.login, fields=fields)
        branch = self.branch_name(task)
        tasks = self.task_list(max_age=TASKS_SECONDS)
        # The verified tree is what gets committed: the text repair below runs outside the worktree.
        verified_tree = check.tree
        # The picker that decides what the text takes runs in this process, which holds the push credentials,
        # so it comes from the base commit, never from the agent's edit.
        taken_root = self.worktree
        if self.profile.taken_module:
            # The module's whole directory at the base, so that it finds the files beside it.
            parent = Path(self.profile.taken_module).parent.as_posix()
            taken_root = gitops.export_tree(self.worktree, base_tip, "" if parent == "." else parent,
                                            self.scratch / "taken")
        for attempt in range(2):
            named = tasks_named_by(self.profile, taken_root, tasks.items, title=title, body=body, branch=branch,
                                   body_html=lambda: self.github.render_markdown(self.s.repository, body))
            others = sorted(named - {task_id(task)})
            if not others:
                break
            if attempt:
                return self.abandon(task, target, "names-other-tasks",
                                    "the pull-request text would take other tasks: " + ", ".join(others[:10]))
            by_id = {task_id(item): item for item in tasks.items}
            other_names = sorted({name for other in others for name in declarations(by_id.get(other, {}))})
            _, fixed = self.agent_round("fix_text.md", {
                "task_block": "```json\n" + json.dumps(task, indent=1, sort_keys=True)[:20000] + "\n```",
                "findings": ", ".join(f"`{name}`" for name in other_names[:20]) or ", ".join(others[:10]),
            }, label=f"fixtext-{task_id(task)[:15]}", cwd=self.scratch)
            if gitops.tree_of_worktree(self.worktree) != verified_tree:
                return self.abandon(task, target, "changed-after-verification",
                                    "the text repair changed files after they were verified")
            report = {**report, **{key: value for key, value in fixed.items() if value and key != "status"}}
            title, body = pull_request_text(report, task, profile=self.profile, repository=self.s.repository,
                                            spec=self.s.agent, login=self.s.login, fields=fields)
        subject = str(report.get("commit_message") or title).strip().splitlines()[0][:200]
        message = subject + "\n\n" + f"Tasks: {task.get('article_id') or task_id(task)}"
        if self.s.co_author:
            message += "\n\n" + co_author_line(self.s.agent)
        sha = gitops.commit(self.worktree, message, author=self.s.author, tree=verified_tree)
        ok, detail = verify.publish_check(self.worktree, profile=self.profile, base=base_tip, head=sha,
                                          metadata={"branch": branch, "title": title, "body": body},
                                          scratch=self.scratch, env=self.check_env, public=(base_tip,))
        if not ok:
            return self.abandon(task, target, "publish-check-failed", detail)
        fresh = self.task_list(max_age=0)
        if task_id(task) not in {task_id(item) for item in fresh.free}:
            return self.abandon(task, target, "taken-before-push",
                                "another pull request took the task, or it is no longer ready", record=False)
        if self.s.dry_run:
            self.release(target, "dry run")
            self.state.event("dry-run-ready", task=task_id(task), branch=branch, head=sha, title=title)
            (self.state.worker_dir() / f"dry-run-{task_id(task)}.md").write_text(f"# {title}\n\n{body}", encoding="utf-8")
            return {"outcome": "dry-run-ready", "branch": branch, "head": sha, "worktree": str(self.worktree)}
        tag = hashlib.sha1(json.dumps(self.origin()).encode()).hexdigest()[:10]
        pending = self.state.root / "pending" / f"{tag}-{slug(branch, 80)}.json"
        self.state.write_json(pending, {"branch": branch, "title": title, "body": body, "task": task_id(task),
                                        "claim": target, "head": sha, "worker": self.s.worker_id, "at": time.time(),
                                        "declarations": names, "phase": task.get("phase"), **self.origin()})
        url = gitops.remote_url(self.s.push_repository)
        try:
            gitops.safe_push(self.worktree, url=url, commit_sha=sha, branch=branch, old=None,
                             prefix=self.s.branch_prefix, protected=self.protected(), gh=self.github.gh, env=self.env)
        except GitError:
            # The push may have landed even so: keep the claim and let recover_pending look.
            self._pushed = True
            raise
        self._pushed = True
        self.state.event("pushed", task=task_id(task), branch=branch, head=sha)
        # A lost response leaves the pending record, and with it the claim, for recover_pending to reconcile.
        number, link = self.open_pull_request(branch, title, body)
        self.after_open(number, link, task, target, branch, sha, names)
        pending.unlink(missing_ok=True)
        return {"outcome": "pr-opened", "pr": number, "url": link, "branch": branch, "head": sha,
                "verify": check.brief()}

    def open_pull_request(self, branch: str, title: str, body: str) -> tuple[int, str]:
        owner = self.s.push_repository.split("/", 1)[0]
        return self.github.create_pull_request(self.s.repository, base=self.s.base_branch, head=f"{owner}:{branch}",
                                               title=title, body=body)

    def after_open(self, number: int, link: str, task: Mapping[str, object], target: str, branch: str, sha: str,
                   names: Sequence[str], owner: str | None = None) -> None:
        """Record an opened pull request once; ``owner`` is the worker holding its claim (default: this one)."""

        owner = owner or self.s.worker_id
        self.state.event("pr-opened", pr=number, url=link, task=task_id(task), branch=branch, head=sha)
        self.state.update_pr(number, task=task_id(task), claim=target, branch=branch, opened_at=utc_now(),
                             phase=task.get("phase"), declarations=list(names), agent=self.s.agent.label,
                             worker=owner, url=link, push_repository=self.s.push_repository,
                             claims_repo=str(self.s.claims_repo))
        board = self.board if owner == self.s.worker_id else self.board_for(owner)
        try:
            board.acquire(author_claim_key(target), note=json.dumps(
                {"worker": owner, "task": task_id(task), "since": utc_now(), "pr": number}, sort_keys=True),
                target=target)
        except (ClaimTransportError, ValueError) as error:
            self.state.event("claim-note-failed", pr=number, error=str(error)[-500:])
        if self.s.labels:
            self.github.add_labels(self.s.repository, number, self.s.labels)
        self.invalidate_pull_requests()

    def recover_pending(self) -> None:
        """Open the pull request of a branch whose push succeeded but whose pull request was never opened."""

        for path in (self.state.root / "pending").glob("*.json"):
            data = self.state.read_json(path, {})
            # Another operator's, or another fork's, publication is theirs to reconcile.
            if not isinstance(data, dict) or not self.mine(data) or time.time() - float(data.get("at", 0)) < 600:
                continue
            with self.state.try_lock("pending-" + path.stem) as held:
                if not held or not path.exists():
                    continue
                branch = str(data.get("branch") or "")
                owner = self.s.push_repository.split("/", 1)[0]
                task = {"article_id": data.get("task"), "phase": data.get("phase")}
                # The claim stays with the worker that pushed, whoever reconciles.
                holder = str(data.get("worker") or self.s.worker_id)
                existing = self.github.find_pull_request(self.s.repository, f"{owner}:{branch}")
                if existing is not None:
                    # Opened after all: finish its bookkeeping once.
                    number = int(existing["number"])
                    if not self.state.pr_record(number).get("opened_at"):
                        self.after_open(number, str(existing.get("url") or ""), task, str(data.get("claim")), branch,
                                        str(data.get("head")), list(data.get("declarations") or []), owner=holder)
                elif gitops.remote_branch(gitops.remote_url(self.s.push_repository), branch,
                                          gh=self.github.gh, env=self.env) == data.get("head"):
                    number, link = self.open_pull_request(branch, str(data["title"]), str(data["body"]))
                    self.after_open(number, link, task, str(data.get("claim")), branch, str(data.get("head")),
                                    list(data.get("declarations") or []), owner=holder)
                elif data.get("claim"):
                    self.release(str(data["claim"]), "the push of its branch did not land", owner=holder)
                path.unlink(missing_ok=True)
