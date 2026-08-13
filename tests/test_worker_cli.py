from __future__ import annotations

import json
from types import SimpleNamespace

from autoform_worker import cli
from autoform_worker.scheduler import LifecycleRecord, LifecycleStatus


def _result(status: LifecycleStatus | None, attempt: int = 1, detail: str = "result"):
    item = None
    record = None
    if status is not None:
        item = SimpleNamespace(
            attempt=attempt,
            node=SimpleNamespace(id="target"),
            phase=SimpleNamespace(value="statement"),
            source_revision="revision",
        )
        record = LifecycleRecord(status=status, attempts=attempt, detail=detail)
    return SimpleNamespace(item=item, record=record, detail=detail, progressed=item is not None)


class _FakeScheduler:
    def __init__(self, results) -> None:
        self._results = iter(results)
        self.calls: list[str | None] = []

    def run_once(self, *, node_id: str | None = None):
        self.calls.append(node_id)
        return next(self._results)


def _patch_worker_construction(monkeypatch, scheduler: _FakeScheduler) -> None:
    monkeypatch.setattr(cli, "backend_factory", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "ProverExecutor", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli.Scheduler, "for_project", lambda *args, **kwargs: scheduler)


def test_default_worker_ids_are_unique_per_parser_invocation(monkeypatch) -> None:
    monkeypatch.delenv("AUTOFORM_WORKER_ID", raising=False)
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "worker")
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "host")

    first = cli._parser().parse_args(["--claim-repo", "claims"]).worker_id
    second = cli._parser().parse_args(["--claim-repo", "claims"]).worker_id

    assert first.startswith("worker-host-")
    assert second.startswith("worker-host-")
    assert first != second


def test_worker_id_environment_override_is_preserved(monkeypatch) -> None:
    monkeypatch.setenv("AUTOFORM_WORKER_ID", "stable-worker")

    args = cli._parser().parse_args(["--claim-repo", "claims"])

    assert args.worker_id == "stable-worker"


def test_main_retries_retryable_result_until_success(monkeypatch, tmp_path, capsys) -> None:
    scheduler = _FakeScheduler(
        [
            _result(LifecycleStatus.RETRYING, 1, "temporary failure"),
            _result(LifecycleStatus.SUCCEEDED, 2, "completed"),
        ]
    )
    _patch_worker_construction(monkeypatch, scheduler)

    exit_code = cli.main(
        [
            "--project",
            str(tmp_path),
            "--claim-repo",
            "claims",
            "--max-attempts",
            "2",
            "--json",
        ]
    )

    assert exit_code == 0
    assert scheduler.calls == [None, "target"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["record"] == {"attempts": 2, "detail": "completed", "status": "succeeded"}


def test_main_stops_after_retry_exhaustion(monkeypatch, tmp_path, capsys) -> None:
    scheduler = _FakeScheduler(
        [
            _result(LifecycleStatus.RETRYING, 1, "temporary failure"),
            _result(LifecycleStatus.RETRYING, 2, "still failing"),
            _result(LifecycleStatus.FAILED, 3, "retry limit reached"),
        ]
    )
    _patch_worker_construction(monkeypatch, scheduler)

    exit_code = cli.main(
        [
            "--project",
            str(tmp_path),
            "--claim-repo",
            "claims",
            "--max-attempts",
            "3",
        ]
    )

    assert exit_code == 1
    assert scheduler.calls == [None, "target", "target"]
    assert capsys.readouterr().out.strip() == "retry limit reached"


def test_main_preserves_single_no_work_round(monkeypatch, tmp_path, capsys) -> None:
    scheduler = _FakeScheduler([_result(None, detail="no ready work")])
    _patch_worker_construction(monkeypatch, scheduler)

    exit_code = cli.main(["--project", str(tmp_path), "--claim-repo", "claims"])

    assert exit_code == 75
    assert scheduler.calls == [None]
    assert capsys.readouterr().out.strip() == "no ready work"
