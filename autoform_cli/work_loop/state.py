"""The work loop's state directory, shared by every worker of one repository on one machine.

Layout under ``<state>`` (default ``$XDG_STATE_HOME/autoform/work/<owner>__<name>``):

- ``ledger.jsonl``: one JSON line per event (claims, agent runs with their cost,
  pushes, pull requests opened, rounds, stops), the record budgets are counted from;
- ``prs/<number>.json``: what the loop did on each of its pull requests (repairs,
  handled reviews and comments, parked state);
- ``facts/``: statement-fidelity facts of target declarations, per base commit;
- ``failures.jsonl``: tasks whose attempt ended without a pull request;
- ``cache/``: task list and pull-request snapshots shared between workers;
- ``workers/<id>/``: each worker's worktree, transcripts and reports;
- ``STOP``: when present, every worker stops after its current round.

Writers take an advisory ``fcntl`` lock, so the loop runs on Linux and macOS.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


# The open slot files of this process: closing one would free its slot.
_HELD_SLOTS: list[object] = []


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: object) -> float:
    """Seconds since the epoch of a GitHub or ledger timestamp; 0 for anything else."""

    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def default_state_dir(repository: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "autoform" / "work" / repository.lower().replace("/", "__")


class State:
    def __init__(self, root: Path, *, worker: str = "", run: str = "") -> None:
        if fcntl is None:
            raise RuntimeError("the work loop needs Linux or macOS (it locks its state with fcntl)")
        self.root = root
        self.worker = worker
        self.run = run
        for sub in ("prs", "facts", "cache", "workers", "locks"):
            (root / sub).mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- paths and locks

    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.jsonl"

    def worker_dir(self, worker: str | None = None) -> Path:
        path = self.root / "workers" / (worker or self.worker)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @contextlib.contextmanager
    def lock(self, name: str) -> Iterator[None]:
        """Hold the exclusive lock ``locks/<name>.lock`` for the block."""

        with open(self.root / "locks" / f"{name}.lock", "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def site_id(self) -> str:
        """A random name for this state directory, made once: worker ids carry it instead of the machine's name,
        since claims, and so worker ids, can be public."""

        path = self.root / "site-id"
        with self.lock("site-id"):
            if not path.is_file():
                path.write_text(secrets.token_hex(3) + "\n", encoding="utf-8")
            return path.read_text(encoding="utf-8").strip()

    def hold_slot(self, limit: int = 256) -> int:
        """Take the lowest free worker slot and hold it until this process exits; return its number.

        Every worker of a state directory, from one ``--workers`` command or several commands, gets its own
        slot, so its own worktree and claim owner. A slot frees itself when its process ends, however it ends,
        and the next worker to start takes it over with the claims it left.
        """

        for index in range(1, limit + 1):
            handle = open(self.root / "locks" / f"slot-{index}.lock", "a+")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                continue
            _HELD_SLOTS.append(handle)  # kept open, and so locked, until the process exits
            return index
        raise RuntimeError(f"all {limit} worker slots of {self.root} are in use")

    @contextlib.contextmanager
    def try_lock(self, name: str) -> Iterator[bool]:
        """Try ``locks/<name>.lock`` without waiting; yield whether this process holds it."""

        handle = open(self.root / "locks" / f"{name}.lock", "a+")
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()

    # ---------------------------------------------------------------- JSON files

    @staticmethod
    def read_json(path: Path, default: object = None) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    @staticmethod
    def write_json(path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
        tmp.replace(path)

    def append(self, path: Path, row: dict[str, object]) -> None:
        line = json.dumps(row, sort_keys=True, default=str) + "\n"
        with self.lock("append-" + path.name):
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)

    @staticmethod
    def lines(path: Path) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return rows
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    # ---------------------------------------------------------------- the ledger

    def event(self, event: str, **fields: object) -> dict[str, object]:
        row: dict[str, object] = {"at": utc_now(), "event": event, "worker": self.worker, "run": self.run, **fields}
        self.append(self.ledger_path, row)
        return row

    def events(self, *, run: str | None = None, event: str | None = None) -> list[dict[str, object]]:
        return [
            row
            for row in self.lines(self.ledger_path)
            if (run is None or row.get("run") == run) and (event is None or row.get("event") == event)
        ]

    def spent(self, *, run: str | None = None) -> float:
        """Dollars the agents reported or were estimated at, summed over ``agent`` events (of one run)."""

        total = 0.0
        for row in self.events(run=run, event="agent"):
            cost = row.get("cost_usd")
            if isinstance(cost, (int, float)) and cost > 0:
                total += float(cost)
        return round(total, 4)

    def attempts(self, *, run: str | None = None) -> int:
        return len(self.events(run=run, event="author-start"))

    # ---------------------------------------------------------------- pull-request records

    def pr_path(self, number: int) -> Path:
        return self.root / "prs" / f"{number}.json"

    def pr_record(self, number: int) -> dict[str, object]:
        record = self.read_json(self.pr_path(number), {})
        return record if isinstance(record, dict) else {}

    def update_pr(self, number: int, **fields: object) -> dict[str, object]:
        """Update a pull request's record: ``add_<key>`` adds to a set, ``inc_<key>`` adds a number."""

        with self.lock(f"pr-record-{number}"):
            record = self.pr_record(number)
            for key, value in fields.items():
                if key.startswith("add_"):
                    items = record.get(key[4:], [])
                    merged = set(items if isinstance(items, list) else []) | set(value)  # type: ignore[arg-type]
                    record[key[4:]] = sorted(merged)
                elif key.startswith("inc_"):
                    old = record.get(key[4:], 0)
                    record[key[4:]] = (old if isinstance(old, (int, float)) else 0) + value  # type: ignore[operator]
                else:
                    record[key] = value
            record["number"] = number
            self.write_json(self.pr_path(number), record)
            return record

    # ---------------------------------------------------------------- failures and stops

    @property
    def failures_path(self) -> Path:
        return self.root / "failures.jsonl"

    def record_failure(self, task: str, outcome: str, notes: str = "") -> None:
        self.append(
            self.failures_path,
            {"at": utc_now(), "task": task, "outcome": outcome, "notes": notes[:4000], "worker": self.worker},
        )

    def failed_recently(self, days: float = 7.0) -> set[str]:
        cutoff = time.time() - days * 86400
        return {
            str(row["task"])
            for row in self.lines(self.failures_path)
            if isinstance(row.get("task"), str) and parse_time(row.get("at")) >= cutoff
        }

    def stop_file(self) -> Path | None:
        """The STOP file that applies to this worker, if one exists."""

        for path in (self.root / "STOP", self.root / "workers" / self.worker / "STOP"):
            if path.exists():
                return path
        return None
