#!/usr/bin/env python3

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Entry point for autoform_bot.

Single-node usage:
    python -m autoform.bot.main run --name=my-run

Multi-node usage (Slurm):
    srun --nodes=N --ntasks-per-node=1 python -m autoform.bot.main run --name=my-run

Single-node and multi-node use the same code path: each node is a WorkerNode,
rank 0 is additionally the coordinator. The local worker on rank 0 runs in a
separate child process so that blocking operations (worktree creation, builds)
cannot stall the coordinator's ZMQ event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import shutil
import subprocess
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import fire
import yaml
from dotenv import load_dotenv

from core.coordination.multinode import get_master_addr, get_master_port, get_rank, get_world_size
from core.coordination.multinode import DistributedExecutor, ZmqTaskServer
from core.coordination.merge_queue import MergeQueue, MergeQueueServer, _MERGE_PORT_OFFSET
from core.inference import InferenceProtocol
from core.trace.store import TraceStore
from core.inference.client import create_inference, lookup_model

from .archive import ArchiveTraceStore, prepare_fresh_run
from .config import PipelineConfig
from .coordinator import LeanCoordinatorNode
from .urls import register_url, cleanup_urls
from .worker_node import LeanWorkerNode
from .workspace import ensure_run_workspace

load_dotenv()
logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent


def load_config(config_path: str | None = None, run_path: Path | None = None) -> dict:
    if config_path is not None:
        config_path = Path(config_path)
    elif run_path is not None and (run_path / "config.yaml").exists():
        config_path = run_path / "config.yaml"
    else:
        config_path = APP_DIR / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def initialize_logging(level: str = "INFO", log_dir: Path | None = None, rank: int = 0) -> None:
    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        stream=sys.stdout,
        format=log_format,
    )
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / f"pipeline_rank{rank}.log")
        file_handler.setLevel(getattr(logging, level.upper()))
        file_handler.setFormatter(logging.Formatter(log_format))
        logging.getLogger().addHandler(file_handler)


def _resolve_run_path(cfg: dict, *, run_path: str | None, name: str | None) -> Path:
    workspace_config = cfg.get("workspace", {})
    if run_path:
        return Path(run_path).expanduser().resolve()
    elif name:
        workspace_root = Path(workspace_config.get("path", ".")).expanduser().resolve()
        return workspace_root / name
    else:
        raise ValueError("Must specify either --run_path (to resume) or --name (to create a new run).")


def _build_config(cfg: dict, resolved_run_path: Path, agents_per_node: int | None) -> PipelineConfig:
    return PipelineConfig.from_yaml(cfg, run_path=resolved_run_path, app_dir=APP_DIR, agents_per_node=agents_per_node)


def _make_inference_factory(pipeline_config: PipelineConfig):
    model_def = lookup_model(pipeline_config.model)

    def make_inference() -> InferenceProtocol:
        return create_inference(model_def)

    return make_inference


def _log_session_event(run_path: Path, event_type: str, reason: str | None = None) -> None:
    """Append a session lifecycle event to ``traces/sessions.jsonl``."""
    import time

    entry: dict = {"type": event_type, "timestamp": time.time()}
    if reason is not None:
        entry["reason"] = reason
    sessions_path = run_path / "traces" / "sessions.jsonl"
    sessions_path.parent.mkdir(parents=True, exist_ok=True)
    with open(sessions_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _build_worker_node(
    rank: int,
    pipeline_config: PipelineConfig,
    run_path: Path,
    make_inference,
    trace_store: TraceStore,
    run_id: str | None = None,
) -> LeanWorkerNode:
    from core.agent import load_agent_definition
    from tools.execution.lean.repl.server import ReplConfig

    repl_binary = APP_DIR.parent.parent / "submodules" / "repl" / ".lake" / "build" / "bin" / "repl"
    mathlib_path = pipeline_config.mathlib_path
    repl_config = (
        ReplConfig(
            cwd=str(mathlib_path),
            repl_command=tuple(["lake", "env", str(repl_binary)]),
            num_repls=pipeline_config.num_repls_per_node,
        )
        if repl_binary.exists() and mathlib_path.exists()
        else None
    )

    return LeanWorkerNode(
        rank=rank,
        host=get_master_addr(),
        port=get_master_port(),
        num_agents=pipeline_config.agents_per_node,
        inference_factory=make_inference,
        worker_def=load_agent_definition(APP_DIR / "agents" / "worker"),
        reviewer_def=load_agent_definition(APP_DIR / "agents" / "reviewer"),
        code_path=run_path / "code",
        allowed_paths=[run_path / "book", run_path / "skills"],
        repl_config=repl_config,
        trace_store=trace_store,
        run_id=run_id,
        max_review_cycles=pipeline_config.max_review_cycles,
        solver=pipeline_config.solver,
    )


# ---------------------------------------------------------------------------
# Coordinator (rank 0)
# ---------------------------------------------------------------------------


async def run_coordinator(
    pipeline_config: PipelineConfig,
    run_path: Path,
    make_inference,
    trace_store: TraceStore,
    num_workers: int,
    server: ZmqTaskServer,
    port: int | None = None,
    test_tasks: int = 0,
    run_id: str | None = None,
) -> dict:
    executor = DistributedExecutor(
        server=server,
        num_workers=num_workers,
        min_agents_per_task=pipeline_config.min_agents_per_task,
        max_agents_per_task=pipeline_config.max_agents_per_task,
        pick_strategy=pipeline_config.pick_strategy,
    )

    coordinator = LeanCoordinatorNode(
        config=pipeline_config,
        executor=executor,
        inference_factory=make_inference,
        trace_store=trace_store,
        test_tasks=test_tasks,
        run_id=run_id,
    )

    # Merge queue — bors-style batch merge train.
    code_path = run_path / "code"

    async def _lake_build(staging_path: Path) -> tuple[bool, str]:
        from .concurrent import _update_root_imports

        await asyncio.to_thread(_update_root_imports, staging_path)
        result = await asyncio.to_thread(
            subprocess.run,
            ["lake", "build"],
            cwd=staging_path,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        if result.returncode != 0:
            output = result.stdout + result.stderr
            return False, output[:3000]
        return True, ""

    def _on_batch_merged(pre_hash: str, post_hash: str, agent_ids: list[str]) -> None:
        """Trigger merge eval once per batch."""
        if not coordinator._targets:
            return

        from .merge_eval import run_merge_eval

        batch_id = f"batch-{post_hash[:8]}"

        async def _run() -> None:
            try:
                md_path, fix_task_ids = await run_merge_eval(
                    task_id=batch_id,
                    code_path=coordinator.code_path / coordinator.config.lib_name,
                    repo_path=coordinator.code_path,
                    pre_hash=pre_hash,
                    post_hash=post_hash,
                    targets=coordinator._targets,
                    mathlib_path=coordinator.config.mathlib_path,
                    book_path=coordinator.book_path,
                    merge_reports_path=coordinator.merge_reports_path,
                    inference_factory=coordinator.inference_factory,
                    tracker=coordinator.goal_tracker,
                    trace_store=coordinator.trace_store,
                    worktrees_dir=coordinator._worktrees_dir,
                    task_tracker=coordinator.tracker,
                )
                if md_path is not None:
                    msg = f"__merge_eval__:{batch_id}:{md_path}"
                    if fix_task_ids:
                        msg += f":{','.join(fix_task_ids)}"
                    coordinator.report_queue.put_nowait(msg)
            except Exception:
                logger.exception("Merge eval failed for batch %s", batch_id)

        task = asyncio.create_task(_run())
        coordinator._merge_eval_tasks.append(task)
        task.add_done_callback(coordinator._merge_eval_tasks.remove)

    def _on_merge_step(phase: str, agent_id: str, success: bool, duration_ms: float, error: str | None) -> None:
        """Record per-agent merge queue steps into the active trace context."""
        from core.trace.step_trace import _current_step_ctx, StepRecord

        ctx = _current_step_ctx.get(None)
        if ctx is None:
            return
        ctx._record(
            StepRecord(
                function=phase,
                timestamp=time.time(),
                duration_ms=duration_ms,
                success=success,
                error=error[:500] if error else None,
                args_summary={"agent_id": agent_id},
            )
        )

    merge_queue = MergeQueue(
        code_path,
        _lake_build,
        batch_size=pipeline_config.agents_per_node * num_workers,
        batch_timeout=120.0,
        on_batch_merged=_on_batch_merged,
        trace_store=trace_store,
        on_step=_on_merge_step,
    )
    merge_port = get_master_port() + _MERGE_PORT_OFFSET
    merge_server = MergeQueueServer(merge_queue, port=merge_port)
    merge_queue_task = asyncio.create_task(merge_queue.run())
    merge_server_task = asyncio.create_task(merge_server.run())
    logger.info("Merge queue listening on port %d", merge_port)

    from core.interaction.server import start_registry_server

    registry_port = start_registry_server(port=port)
    registry_host = get_master_addr()
    registry_url = f"http://{registry_host}:{registry_port}"
    register_url(run_path, "registry", 0, registry_url)
    logger.info("Registry API running at %s", registry_url)

    # Control plane — allows the visualizer to trigger graceful shutdown.
    from .control import start_control_server

    loop = asyncio.get_running_loop()
    coordinator_task = asyncio.current_task()

    def _request_shutdown():
        logger.warning("Control plane shutdown requested — cancelling coordinator task")
        loop.call_soon_threadsafe(coordinator_task.cancel)

    control_port = start_control_server(shutdown_callback=_request_shutdown, rank=0)
    control_host = get_master_addr()
    control_url = f"http://{control_host}:{control_port}"
    register_url(run_path, "control", 0, control_url)
    logger.info("Control plane API running at %s", control_url)

    try:
        return await coordinator.run()
    finally:
        merge_queue.stop()
        merge_server.stop()
        merge_queue_task.cancel()
        merge_server_task.cancel()


# ---------------------------------------------------------------------------
# Local worker (runs in a child process on rank 0, or directly on rank 1+)
# ---------------------------------------------------------------------------


def _run_local_worker(
    rank: int,
    cfg: dict,
    run_path: Path,
    agents_per_node: int | None,
    log_level: str,
    run_id: str | None = None,
) -> None:
    """Build a worker node and run it. Used as both a child-process target and direct call."""
    from core.coordination.multinode.zmq_queue import ZmqTaskClient

    print(f"[rank {rank}] Waiting for workspace to be ready...", flush=True)
    try:
        with ZmqTaskClient(host=get_master_addr(), port=get_master_port(), rank=rank) as client:
            client.send({"type": "waiting", "rank": rank})
            while True:
                msg = client.recv(timeout_ms=1000)
                if msg and msg.get("type") == "ready":
                    if run_id is None:
                        run_id = msg.get("run_id")
                    break
    except KeyboardInterrupt:
        print(f"[rank {rank}] Interrupted during startup", flush=True)
        return
    print(f"[rank {rank}] Workspace ready", flush=True)

    initialize_logging(log_level, log_dir=run_path / "logs", rank=rank)

    pipeline_config = _build_config(cfg, run_path, agents_per_node)
    make_inference = _make_inference_factory(pipeline_config)
    trace_store = ArchiveTraceStore(
        run_path / "traces",
        run_path / "archive" / "traces",
    )
    node = _build_worker_node(
        rank=rank,
        pipeline_config=pipeline_config,
        run_path=run_path,
        make_inference=make_inference,
        trace_store=trace_store,
        run_id=run_id,
    )

    from core.interaction.server import start_registry_server

    registry_port = start_registry_server()
    hostname = socket.gethostname()
    registry_url = f"http://{hostname}:{registry_port}"
    register_url(run_path, "registry", rank, registry_url)
    logger.info("Registry API running at %s", registry_url)

    # Start control server on remote workers (rank 1+).
    # The local worker on rank 0's node (rank == world_size) skips this
    # because the coordinator already runs a control server on that node.
    world_size = get_world_size()
    if rank != world_size:
        from .control import start_control_server

        control_port = start_control_server(rank=rank)
        control_url = f"http://{hostname}:{control_port}"
        register_url(run_path, "control", rank, control_url)
        logger.info("Control plane API running at %s", control_url)

    asyncio.run(node.run())


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run(
    config: str | None = None,
    agents_per_node: int | None = None,
    run_path: str | None = None,
    name: str | None = None,
    nuke: bool = False,
    fresh: bool = False,
    port: int | None = None,
    test_tasks: int = 0,
) -> None:
    """Auto-detects rank and runs coordinator, worker node, or both."""
    cfg = load_config(config)
    rank = get_rank()
    world_size = get_world_size()
    print(f"[rank {rank}] Config loaded, resolving run path...", flush=True)
    resolved_run_path = _resolve_run_path(cfg, run_path=run_path, name=name)
    if fresh and not resolved_run_path.exists():
        print(f"[rank {rank}] --fresh ignored: {resolved_run_path} does not exist, starting from scratch", flush=True)
        fresh = False
    # On resume (no explicit --config), prefer the snapshotted config if it exists.
    if config is None and (resolved_run_path / "config.yaml").exists():
        cfg = load_config(run_path=resolved_run_path)
    pipeline_config = _build_config(cfg, resolved_run_path, agents_per_node)
    print(f"[rank {rank}] Pipeline config ready", flush=True)

    log_level = cfg.get("logging", {}).get("level", "INFO")

    if rank == 0:
        # Bind ZMQ server early so workers can connect for readiness signaling
        # before the workspace is set up.
        server = ZmqTaskServer(port=get_master_port())

        # Spawn local worker early so it can import/init while workspace is created.
        # It will block on a ZMQ "ready" signal until workspace setup completes.
        local_worker_rank = 1 if world_size == 1 else world_size
        worker_proc = multiprocessing.Process(
            target=_run_local_worker,
            args=(local_worker_rank, cfg, resolved_run_path, agents_per_node, log_level),
            daemon=False,
        )
        worker_proc.start()

        books_source: Path | None = None
        book_files: list[str] | None = None
        if pipeline_config.book_path:
            repo_root = APP_DIR.parent.parent
            books_source = (repo_root / "autoform" / "data" / pipeline_config.book_path).resolve()
            if not books_source.exists():
                print(f"Warning: Books source not found: {books_source}")
                books_source = None
            else:
                book_files = pipeline_config.book_files
        print(f"[rank 0] Initializing workspace at {resolved_run_path} (nuke={nuke})...", flush=True)
        ensure_run_workspace(
            run_path=resolved_run_path,
            books_source=books_source,
            book_files=book_files,
            nuke=nuke,
            lib_name=pipeline_config.lib_name,
        )
        print("[rank 0] Workspace ready", flush=True)

        import subprocess as _sp

        code_dir = resolved_run_path / "code"
        if code_dir.exists():
            # Unfreeze packages from a previous run (resume case).
            packages_dir = code_dir / ".lake" / "packages"
            if packages_dir.exists():
                _sp.run(["chmod", "-R", "u+w", str(packages_dir)], capture_output=True)

            # Build all transitive packages so their .lake/ dirs exist.
            # Without this, worktree lake builds try to create them and
            # fail on frozen packages.
            print("[rank 0] Building transitive packages...", flush=True)
            pkg_result = _sp.run(
                [
                    "lake",
                    "build",
                    "batteries",
                    "aesop",
                    "Qq",
                    "proofwidgets",
                    "plausible",
                    "LeanSearchClient",
                    "importGraph",
                    "Cli",
                ],
                cwd=code_dir,
                capture_output=True,
                text=True,
                timeout=3600,
            )
            if pkg_result.returncode != 0:
                print("[rank 0] WARNING: Package build had errors (non-fatal):", flush=True)
                print((pkg_result.stderr or "")[-1000:], flush=True)

            print("[rank 0] Running initial `lake build`...", flush=True)
            build_result = _sp.run(
                ["lake", "build"],
                cwd=code_dir,
                capture_output=True,
                text=True,
                timeout=3600,
            )
            if build_result.returncode != 0:
                print("[rank 0] FATAL: Initial `lake build` failed. Workers cannot proceed.", flush=True)
                print(build_result.stderr[-2000:] if build_result.stderr else "", flush=True)
                sys.exit(1)
            print("[rank 0] Initial build succeeded", flush=True)

            # Freeze packages so concurrent worktree builds can't corrupt
            # oleans that REPLs have mmap'd.
            if packages_dir.exists():
                _sp.run(["chmod", "-R", "a-w", str(packages_dir)], capture_output=True)
                print("[rank 0] Froze .lake/packages (read-only)", flush=True)

        print("[rank 0] Signaling workers...", flush=True)

        run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

        # GC old worktree runs BEFORE signaling workers, so workers don't
        # race with deletion when they start creating worktrees.
        from .utils.gc_worktrees import gc_worktrees

        try:
            gc_worktrees(resolved_run_path, max_age_hours=0)
        except Exception:
            print("[rank 0] WARNING: Startup worktree GC failed", flush=True)

        # Pack loose git objects to prevent timeouts during the run.
        if code_dir.exists():
            _sp.run(["git", "-C", str(code_dir), "gc", "--prune=now"], capture_output=True)

        if fresh:
            print("[rank 0] --fresh: pruning terminal tasks and resetting orchestrator trace...", flush=True)
            prepare_fresh_run(resolved_run_path)

        # Reply "ready" to each worker as it checks in. Workers send "waiting"
        # whenever they start; we reply after workspace setup is done.
        # Timeout so we don't hang forever if a worker crashes during startup.
        import time

        signaled: set[int] = set()
        deadline = time.monotonic() + 3600.0
        while len(signaled) < world_size:
            if time.monotonic() > deadline:
                print(
                    f"[rank 0] WARNING: Only {len(signaled)}/{world_size} workers "
                    f"checked in within 3600s — proceeding with partial workers",
                    flush=True,
                )
                break
            result = server.recv(timeout_ms=1000)
            if result is None:
                continue
            r, msg = result
            if msg.get("type") == "waiting":
                server.send(r, {"type": "ready", "run_id": run_id})
                signaled.add(r)
                print(f"[rank 0] Signaled worker rank {r} ({len(signaled)}/{world_size})", flush=True)
            else:
                server.requeue(r, msg)

        # Snapshot config into the run dir so eval/resume use the same settings.
        # Always overwrite when an explicit --config is provided (user intent).
        run_config = resolved_run_path / "config.yaml"
        src_config = Path(config) if config else APP_DIR / "config.yaml"
        if src_config.exists() and (config or not run_config.exists()):
            shutil.copy2(src_config, run_config)

        initialize_logging(log_level, log_dir=resolved_run_path / "logs", rank=rank)

        logger.info("=" * 70)
        logger.info("AUTOFORM PIPELINE V1  rank=%d  world=%d", rank, world_size)
        logger.info(
            "agents_per_node=%d  total_agents=%d",
            pipeline_config.agents_per_node,
            pipeline_config.agents_per_node * world_size,
        )
        logger.info("=" * 70)
        logger.info("Local worker spawned as PID %d (rank %d)", worker_proc.pid, local_worker_rank)

        make_inference = _make_inference_factory(pipeline_config)
        trace_store = ArchiveTraceStore(
            resolved_run_path / "traces",
            resolved_run_path / "archive" / "traces",
        )
        _stop_reason = "crashed"  # default; overwritten by except/else
        try:
            _log_session_event(resolved_run_path, "start")
            summary = asyncio.run(
                run_coordinator(
                    pipeline_config,
                    resolved_run_path,
                    make_inference,
                    trace_store,
                    num_workers=world_size,
                    server=server,
                    port=port,
                    test_tasks=test_tasks,
                    run_id=run_id,
                )
            )
        except KeyboardInterrupt:
            logger.info("Coordinator interrupted by KeyboardInterrupt")
            summary = None
            _stop_reason = "interrupted"
        except Exception:
            logger.exception("Coordinator crashed")
            summary = None
            _stop_reason = "crashed"
        else:
            _stop_reason = "completed"
        finally:
            _log_session_event(resolved_run_path, "stop", reason=_stop_reason)
            cleanup_urls(resolved_run_path)
            worker_proc.join(timeout=30)
            if worker_proc.is_alive():
                logger.warning("Local worker PID %d did not exit — terminating", worker_proc.pid)
                worker_proc.terminate()
                worker_proc.join(timeout=5)
    else:
        # Remote worker node (rank 1+)
        _run_local_worker(rank, cfg, resolved_run_path, agents_per_node, log_level)
        summary = None

    if summary:
        print(f"\nCompleted: {summary.get('completed', 0)}/{summary.get('total_tasks', 0)}")


if __name__ == "__main__":
    fire.Fire({"run": run})
