"""Ready tasks: where they come from, which are free, and which one a worker tries next.

A task is an ``autoform work list --json`` item (``autoform-work/v2``). A
profile may name a command that prints them, such as a project's own picker;
otherwise the loop runs ``autoform work list`` on the checkout and treats a
task as taken when an open pull request's title, body or branch names its
article id.
"""

from __future__ import annotations

import importlib.util
import json
import random
import re
import sys
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .gitops import run_command
from .profile import Profile

ARTICLE_ID = re.compile(r"(?<![0-9A-Za-z])af_[0-9a-f]{24}(?![0-9A-Za-z])", re.IGNORECASE)


class TaskError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TaskList:
    """Every ready task (``items``), the free ones, and where they came from."""

    items: tuple[dict[str, object], ...]
    free: tuple[dict[str, object], ...]
    source: str
    commit: str | None = None
    taken: int = 0


def task_id(task: Mapping[str, object]) -> str:
    return str(task.get("article_id") or task.get("node_id") or "")


def claim_target(task: Mapping[str, object]) -> str:
    return str(task.get("claim_target") or task.get("article_id") or task.get("node_id") or "")


def declarations(task: Mapping[str, object]) -> list[str]:
    return [
        str(target["declaration"])
        for target in task.get("lean_targets") or []  # type: ignore[union-attr]
        if isinstance(target, Mapping) and target.get("declaration")
    ]


def _items(data: object, source: str) -> list[dict[str, object]]:
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data = data["items"]
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise TaskError(f"{source} is not a task list (`autoform work list --json` items)")
    return [item for item in data if item.get("ready", True) and task_id(item)]


def read_task_file(source: str, *, timeout: float = 120) -> object:
    """JSON from a file or an HTTPS URL."""

    try:
        if re.match(r"https?://", source):
            with urllib.request.urlopen(source, timeout=timeout) as response:  # noqa: S310 - https task lists
                return json.loads(response.read())
        return json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TaskError(f"cannot read the task list {source}: {error}") from None


def names_article_id(texts: Iterable[str], article_id: str) -> bool:
    wanted = article_id.lower()
    return any(match.group(0).lower() == wanted for text in texts for match in ARTICLE_ID.finditer(text))


def generic_taken(items: Sequence[Mapping[str, object]], pulls: Sequence[Mapping[str, object]]) -> set[str]:
    """Task ids an open pull request takes by naming the article id in its title, body or branch."""

    named = {
        match.group(0).lower()
        for pull in pulls
        for text in (pull.get("title"), pull.get("body"), pull.get("branch"))
        if isinstance(text, str)
        for match in ARTICLE_ID.finditer(text)
    }
    return {task_id(item) for item in items if str(item.get("article_id") or "").lower() in named}


def load_tasks(
    profile: Profile,
    checkout: Path,
    *,
    tasks_override: str | None,
    env: Mapping[str, str],
    open_pulls: Callable[[], Sequence[Mapping[str, object]]],
    timeout: float = 900,
) -> TaskList:
    """The ready tasks and the free ones, from the profile's command or from ``autoform work list``."""

    if profile.task_command:
        command = list(profile.task_command)
        if tasks_override:
            if not profile.tasks_option:
                raise TaskError("--tasks needs a profile whose [tasks] tasks_option says how to pass it")
            command += [profile.tasks_option, tasks_override]
        result = run_command(command, cwd=checkout, env=env, timeout=timeout)
        if result.returncode != 0:
            raise TaskError(f"the task command failed: {(result.stderr or result.stdout).strip()[-2000:]}")
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise TaskError("the task command did not print JSON") from None
        if isinstance(data, dict) and isinstance(data.get("next"), list):
            # A picker that already left out taken tasks, such as atlas-lean's next_task.py --json.
            free = _items(data["next"], "the task command's next list")
            source = str(data.get("tasks") or tasks_override or "")
            items = _items(read_task_file(source), source) if source else free
            return TaskList(
                items=tuple(items), free=tuple(free), source=source or "task command",
                commit=str(data.get("commit") or "") or None, taken=int(data.get("taken") or 0),
            )
        items = _items(data, "the task command's output")
        commit = str(data.get("source_revision") or data.get("commit") or "") if isinstance(data, dict) else ""
        source = "task command"
    else:
        if tasks_override:
            data = read_task_file(tasks_override)
            source = tasks_override
        else:
            result = run_command(
                [sys.executable, "-m", "autoform_cli", "work", "list", str(checkout), "--json"],
                cwd=checkout, env=env, timeout=timeout,
            )
            if result.returncode != 0:
                raise TaskError(f"`autoform work list` failed: {(result.stderr or result.stdout).strip()[-2000:]}")
            try:
                data = json.loads(result.stdout)
            except ValueError:
                raise TaskError("`autoform work list --json` did not print JSON") from None
            source = "autoform work list"
        items = _items(data, source)
        commit = str(data.get("source_revision") or data.get("commit") or "") if isinstance(data, dict) else ""
    taken = generic_taken(items, open_pulls())
    free = [item for item in items if task_id(item) not in taken]
    return TaskList(items=tuple(items), free=tuple(free), source=source, commit=commit or None, taken=len(taken))


def candidates(
    tasks: Sequence[Mapping[str, object]],
    *,
    phases: Iterable[str],
    skip: Iterable[str] = (),
    only: str | None = None,
    rng: random.Random | None = None,
    limit: int | None = 25,
) -> list[dict[str, object]]:
    """Up to ``limit`` free tasks to try, in a random order, so that independent workers rarely pick the same."""

    allowed, skipped = set(phases), set(skip)
    pool = [
        dict(task)
        for task in tasks
        if task.get("phase") in allowed
        and task_id(task) not in skipped
        and claim_target(task) not in skipped
        and (only is None or only in (task.get("article_id"), task.get("node_id")))
    ]
    (rng or random.Random()).shuffle(pool)
    return pool if limit is None else pool[:limit]


def load_taken_function(checkout: Path, module_path: str) -> Callable[..., Mapping[str, Sequence[int]]]:
    """``taken(items, pulls)`` from a Python file of the checkout, such as atlas-lean's ``scripts/next_task.py``."""

    path = (checkout / module_path).resolve()
    if not path.is_relative_to(checkout.resolve()) or not path.is_file():
        raise TaskError(f"[tasks] taken_module {module_path} is not a file of the checkout")
    spec = importlib.util.spec_from_file_location("autoform_work_taken", path)
    if spec is None or spec.loader is None:
        raise TaskError(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, "taken", None)
    if not callable(function):
        raise TaskError(f"{module_path} has no taken(items, pulls) function")
    return function


def tasks_named_by(
    profile: Profile,
    checkout: Path,
    items: Sequence[Mapping[str, object]],
    *,
    title: str,
    body: str,
    branch: str,
    body_html: Callable[[], str],
) -> set[str]:
    """Ids of the tasks a pull request with this title, body and branch would take."""

    if profile.taken_module:
        taken = load_taken_function(checkout, profile.taken_module)
        pull = {"number": 0, "title": title, "body": body, "body_html": body_html(), "head": {"ref": branch}}
        held = taken([dict(item) for item in items], [pull])
        by_node = {str(item.get("node_id")): task_id(item) for item in items}
        return {by_node.get(str(node), str(node)) for node in held}
    return generic_taken(items, [{"title": title, "body": body, "branch": branch}])
