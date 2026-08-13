"""Command-line entry point for one claim-backed Deicyde execution round."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import tempfile
import uuid
from pathlib import Path

from .executor import ProverExecutor, backend_factory
from .scheduler import Scheduler


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoform-worker")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--claim-repo", required=True, help="Git repository used for claim refs")
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("AUTOFORM_WORKER_ID")
        or f"{getpass.getuser()}-{socket.gethostname()}-{uuid.uuid4().hex}",
    )
    parser.add_argument("--backend", choices=("claude", "codex", "muse"), default="claude")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-steers", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30 * 60.0)
    parser.add_argument("--claim-ttl", type=float, default=1500.0)
    parser.add_argument("--heartbeat-interval", type=float, default=300.0)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project = args.project.expanduser().resolve()
    executor = ProverExecutor(
        project,
        backend_factory(args.backend, timeout=args.timeout),
        max_steers=args.max_steers,
    )
    with tempfile.TemporaryDirectory(prefix="autoform-claims-") as scratch:
        scheduler = Scheduler.for_project(
            project,
            claim_repo=args.claim_repo,
            worker_id=args.worker_id,
            claim_scratch=scratch,
            executor=executor,
            lean_root=project,
            max_attempts=args.max_attempts,
            claim_ttl=args.claim_ttl,
            heartbeat_interval=args.heartbeat_interval,
        )
        result = scheduler.run_once()
        while result.record is not None and result.record.status.value == "retrying":
            result = scheduler.run_once(node_id=result.item.node.id if result.item is not None else None)

    payload = {
        "detail": result.detail,
        "progressed": result.progressed,
        "item": None,
        "record": None,
    }
    if result.item is not None:
        payload["item"] = {
            "attempt": result.item.attempt,
            "node": result.item.node.id,
            "phase": result.item.phase.value,
            "source_revision": result.item.source_revision,
        }
    if result.record is not None:
        payload["record"] = {
            "attempts": result.record.attempts,
            "detail": result.record.detail,
            "status": result.record.status.value,
        }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(result.detail)
    if not result.progressed:
        return 75
    return 0 if result.record is not None and result.record.status.value == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
