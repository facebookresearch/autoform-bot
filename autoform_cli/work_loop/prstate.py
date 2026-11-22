"""Reading a pull request's state: checks, the review bot's verdict, people's comments, and what to do next.

These functions take the plain data :func:`.github.normalize_pr` returns and
decide nothing by themselves about GitHub: the worker acts on their answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .state import parse_time

# The hidden mark the loop puts in every comment and pull-request body it writes.
LOOP_MARK = "<!-- autoform-work"
OK = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
CANCELLED = frozenset({"CANCELLED", "STALE"})
# A check waiting for a maintainer, such as a first-time contributor's workflow run that needs approval.
WAITING = frozenset({"ACTION_REQUIRED"})


@dataclass(frozen=True, slots=True)
class CheckSummary:
    pending: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()
    waiting: tuple[str, ...] = ()
    passed: tuple[str, ...] = ()
    missing_required: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Action:
    """What a worker should do with one pull request: ``kind`` is ``human``, ``merge-base``, ``fix-review``,
    ``fix-ci``, ``park``, ``wait``, ``ready`` or ``closed``."""

    kind: str
    reason: str
    data: object = field(default=None, compare=False)


def latest_checks(pr: Mapping[str, object]) -> dict[tuple[str, str], dict[str, object]]:
    """The latest run of each check, by workflow and name; a re-run replaces the run it repeats."""

    latest: dict[tuple[str, str], dict[str, object]] = {}
    for check in pr.get("checks") or []:  # type: ignore[union-attr]
        key = (str(check.get("workflow") or ""), str(check.get("name") or ""))
        if key not in latest or str(check.get("started_at") or "") >= str(latest[key].get("started_at") or ""):
            latest[key] = check
    return latest


def check_label(key: tuple[str, str]) -> str:
    return f"{key[0]} / {key[1]}" if key[0] else key[1]


def summarize_checks(
    pr: Mapping[str, object], *, required: Iterable[str] | None = None, ignore: Iterable[str] = ()
) -> CheckSummary:
    """Sort the latest checks into pending, failed, cancelled, waiting and passed; ``ignore`` names checks the
    review bot runs, which its verdict stands for. A required check with no run yet counts as missing."""

    ignored = set(ignore)
    buckets: dict[str, list[str]] = {"pending": [], "failed": [], "cancelled": [], "waiting": [], "passed": []}
    seen: set[str] = set()
    for key, check in sorted(latest_checks(pr).items()):
        if key[1] in ignored:
            continue
        seen.add(key[1])
        status, conclusion = str(check.get("status") or ""), str(check.get("conclusion") or "")
        if status != "COMPLETED":
            bucket = "pending"
        elif conclusion in OK:
            bucket = "passed"
        elif conclusion in CANCELLED:
            bucket = "cancelled"
        elif conclusion in WAITING:
            bucket = "waiting"
        else:
            bucket = "failed"
        buckets[bucket].append(check_label(key))
    missing = tuple(sorted(name for name in (required or ()) if name not in seen and name not in ignored))
    return CheckSummary(**{name: tuple(values) for name, values in buckets.items()}, missing_required=missing)


def review_verdict(
    pr: Mapping[str, object], *, bot: str | None, markers: Iterable[str] = ()
) -> dict[str, object] | None:
    """The review bot's latest approving or change-requesting review, of any commit; ``None`` without a bot."""

    if not bot:
        return None
    login = bot.removesuffix("[bot]")
    wanted = tuple(markers)
    verdicts = [
        review
        for review in pr.get("reviews") or []  # type: ignore[union-attr]
        if str(review.get("author") or "").removesuffix("[bot]") == login
        and review.get("state") in ("APPROVED", "CHANGES_REQUESTED")
        and (not wanted or any(marker in str(review.get("body") or "") for marker in wanted))
    ]
    return max(verdicts, key=lambda review: parse_time(review.get("submitted_at"))) if verdicts else None


def human_comments(
    pr: Mapping[str, object], *, handled: Iterable[str] = (), bots: Iterable[str] = ()
) -> list[dict[str, object]]:
    """People's comments the loop has not handled: not a bot's, not one the loop wrote, not a resolved thread."""

    done, extra = set(handled), set(bots)
    return [
        comment
        for comment in pr.get("comments") or []  # type: ignore[union-attr]
        if not comment.get("bot")
        and str(comment.get("author") or "") not in extra
        and LOOP_MARK not in str(comment.get("body") or "")
        and comment.get("id") not in done
        and not (comment.get("kind") == "line" and comment.get("resolved"))
    ]


def merge_ready(pr: Mapping[str, object], checks: CheckSummary, verdict: Mapping[str, object] | None, *,
                review_bot: str | None) -> tuple[bool, str]:
    """Whether a maintainer could merge the head as it is, and if not, what it waits for."""

    if pr.get("draft"):
        return False, "draft"
    if checks.pending or checks.missing_required:
        return False, "waiting for checks: " + ", ".join(checks.pending + checks.missing_required)
    if checks.waiting:
        return False, "waiting for a maintainer to approve workflow runs: " + ", ".join(checks.waiting)
    if checks.failed:
        return False, "failed checks: " + ", ".join(checks.failed)
    if checks.cancelled:
        return False, "cancelled checks need a re-run: " + ", ".join(checks.cancelled)
    if review_bot:
        if verdict is None or verdict.get("commit") != pr.get("head"):
            return False, f"waiting for {review_bot}'s review of the head"
        if verdict.get("state") != "APPROVED":
            return False, f"{review_bot} requested changes"
    if pr.get("mergeable") != "MERGEABLE":
        return False, f"GitHub reports mergeable={pr.get('mergeable') or 'UNKNOWN'}"
    return True, "merge-ready"


def next_action(
    pr: Mapping[str, object],
    record: Mapping[str, object],
    *,
    required: Iterable[str] | None,
    review_bot: str | None,
    review_markers: Iterable[str] = (),
    review_checks: Iterable[str] = (),
    base_failures: Iterable[str] = (),
    bots: Iterable[str] = (),
    max_repairs: int = 3,
    max_merges: int = 5,
) -> Action:
    """The one thing to do next with a loop pull request, in the order a round takes them."""

    if pr.get("state") != "OPEN":
        return Action("closed", str(pr.get("state") or "unknown").lower())
    if record.get("parked"):
        return Action("wait", f"parked: {record['parked']}")
    handled = set(record.get("handled") or [])  # type: ignore[arg-type]
    people = human_comments(pr, handled=handled, bots=bots)
    if people:
        # A person's request is answered even when the repair budget is spent.
        return Action("human", f"{len(people)} comment(s) from people", people)
    repairs, merges = int(record.get("repairs") or 0), int(record.get("merges") or 0)
    head = pr.get("head")
    if pr.get("mergeable") == "CONFLICTING":
        if f"merge:{head}" in handled:
            return Action("wait", "merging the base branch into this head already failed; it needs a person")
        if merges >= max_merges:
            return Action("park", f"it still conflicts with the base branch after {merges} merges")
        return Action("merge-base", "conflicts with the base branch")
    verdict = review_verdict(pr, bot=review_bot, markers=review_markers)
    if verdict and verdict.get("commit") == head and verdict.get("state") == "CHANGES_REQUESTED":
        if verdict.get("id") not in handled:
            if repairs >= max_repairs:
                return Action("park", f"{review_bot} still requests changes after {repairs} repair rounds")
            return Action("fix-review", f"{review_bot} requested changes", verdict)
    checks = summarize_checks(pr, required=required, ignore=review_checks)
    red_on_base = set(base_failures)
    failing = [name for name in checks.failed if name.rsplit(" / ", 1)[-1] not in red_on_base]
    if failing and f"ci:{head}" not in handled:
        if repairs >= max_repairs:
            return Action("park", f"checks still fail after {repairs} repair rounds: {', '.join(failing)}")
        return Action("fix-ci", "failed checks: " + ", ".join(failing), failing)
    ready, why = merge_ready(pr, checks, verdict, review_bot=review_bot)
    return Action("ready" if ready else "wait", why)
