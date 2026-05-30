# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""LeanWorkerNode — Lean-specific worker node for the autoform pipeline.

Extends the generic WorkerNode with an agent pool and concurrent task execution.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from core.agent import AgentDefinition
from core.coordination.pool import AgentPool
from core.coordination.concurrent_agents import ConcurrentAgents
from core.inference import InferenceProtocol
from core.task import Task
from core.trace import TraceStore
from core.coordination.multinode import WorkerNode
from core.coordination.concurrent_agents import FailureCause
from core.coordination.merge_queue import MergeQueueClient, _MERGE_PORT_OFFSET
from tools.execution.lean.repl.server import ReplConfig

from .concurrent import LeanConcurrentAgents
from .pool import create_lean_pool

logger = logging.getLogger(__name__)


class LeanWorkerNode(WorkerNode):
    """A compute node that owns a Lean agent pool and serves tasks from the coordinator.

    Args:
        rank: This node's rank in the distributed job.
        host: Coordinator's hostname (rank 0).
        port: Coordinator's ZMQ port.
        num_agents: Number of agents (worker + reviewer pairs) on this node.
        inference_factory: Factory that creates a fresh InferenceProtocol per agent.
        worker_def: Agent definition for workers.
        reviewer_def: Agent definition for reviewers.
        code_path: Path to the Lean git repository for this node.
        allowed_paths: Extra paths agents can read (book, skills).
        repl_config: REPL server configuration; None disables REPL.
        trace_store: Optional trace store for agent traces.
    """

    def __init__(
        self,
        rank: int,
        host: str,
        port: int,
        num_agents: int,
        inference_factory: Callable[[], InferenceProtocol],
        worker_def: AgentDefinition,
        reviewer_def: AgentDefinition,
        code_path: Path,
        allowed_paths: list[Path],
        repl_config: ReplConfig | None = None,
        trace_store: TraceStore | None = None,
        run_id: str | None = None,
        max_review_cycles: int = 0,
        solver: str = "agent",
    ) -> None:
        super().__init__(rank=rank, host=host, port=port)
        self._code_path = code_path
        self._num_agents = num_agents
        self._inference_factory = inference_factory
        self._worker_def = worker_def
        self._reviewer_def = reviewer_def
        self._allowed_paths = allowed_paths
        self._repl_config = repl_config
        self._trace_store = trace_store
        self._run_id = run_id
        self._max_review_cycles = max_review_cycles
        self._solver = solver
        self._pool: AgentPool | None = None
        self._concurrent: ConcurrentAgents | None = None
        self._merge_client: MergeQueueClient | None = None
        self._merge_listener: asyncio.Task | None = None

    @property
    def capacity(self) -> int:
        """Number of agents currently available to take on a task."""
        return len(self._pool.available()) if self._pool is not None else 0

    async def initialize(self) -> None:
        """Create pool and warm up all agents (REPL + LSP start, worktrees ready).

        When ``solver == "aristotle"`` the worker pool is built from
        ``AristotleAgent``s (Mode B): same worktrees, but Aristotle solves each
        target instead of a tool-calling agent. The build gate and merge path
        below are unchanged.
        """
        if self._solver == "aristotle":
            from .aristotle_agent import create_aristotle_pool

            logger.info("[rank %d] solver=aristotle: building Aristotle worker pool", self.rank)
            self._pool = create_aristotle_pool(
                repo_root=self._code_path,
                num_agents=self._num_agents,
                agent_id_prefix=f"rank{self.rank}",
                run_id=self._run_id,
            )
        else:
            self._pool = create_lean_pool(
                repo_root=self._code_path,
                num_agents=self._num_agents,
                inference_factory=self._inference_factory,
                worker_def=self._worker_def,
                reviewer_def=self._reviewer_def,
                agent_id_prefix=f"rank{self.rank}",
                allowed_paths=self._allowed_paths,
                trace_store=self._trace_store,
                repl_config=self._repl_config,
                run_id=self._run_id,
            )
        self._merge_client = MergeQueueClient(
            host=self.host,
            port=self.port + _MERGE_PORT_OFFSET,
            rank=self.rank,
        )
        self._merge_listener = asyncio.create_task(self._merge_client.run())

        self._concurrent = LeanConcurrentAgents(
            repo_root=self._code_path,
            inference_factory=self._inference_factory,
            allowed_paths=self._allowed_paths,
            trace_store=self._trace_store,
            merge_client=self._merge_client,
            max_review_cycles=self._max_review_cycles,
        )

        logger.info("[rank %d] Initializing pool (%d agents)...", self.rank, self._pool.size)
        await self._pool.initialize()

    async def shutdown(self) -> None:
        """Shut down the pool and merge client."""
        if self._merge_client:
            self._merge_client.stop()
        if self._merge_listener:
            self._merge_listener.cancel()
            try:
                await self._merge_listener
            except asyncio.CancelledError:
                pass
        if self._pool is not None:
            await self._pool.shutdown()
            logger.info("[rank %d] Pool shutdown complete", self.rank)

    async def _execute(self, msg: dict, send_queue: asyncio.Queue) -> None:
        """Check out agents, run one task, send result back."""
        task = Task(
            id=msg["task_id"],
            title=msg.get("title", ""),
            description=msg.get("description", ""),
            metadata=msg.get("metadata", {}),
        )
        n_agents = msg.get("n_agents", 1)
        attempt_number = msg.get("attempt_number", 1)

        agents = self._pool.checkout(n_agents)
        if not agents:
            await send_queue.put(
                {
                    "type": "result",
                    "rank": self.rank,
                    "task_id": task.id,
                    "success": False,
                    "winner_id": None,
                    "error": "No agents available",
                    "failure_cause": FailureCause.INFRASTRUCTURE,
                    "capacity": self.capacity,
                }
            )
            return

        for agent in agents:
            if hasattr(agent, "escalation_logger"):
                agent.escalation_logger.task_id = task.id

        try:
            result = await self._concurrent.run_task(
                task,
                agents,
                get_reviewer=self._pool.get_reviewer,
                attempt_number=attempt_number,
                trace_store=self._trace_store,
            )
        except Exception as e:
            logger.exception("[rank %d] Task %s raised", self.rank, task.id)
            for agent in agents:
                if hasattr(agent, "escalation_logger"):
                    agent.escalation_logger.task_id = None
            self._pool.checkin(agents)
            result_msg = {
                "type": "result",
                "rank": self.rank,
                "task_id": task.id,
                "success": False,
                "winner_id": None,
                "error": str(e),
                "failure_cause": FailureCause.INFRASTRUCTURE,
                "capacity": self.capacity,
            }
        else:
            for agent in agents:
                if hasattr(agent, "escalation_logger"):
                    agent.escalation_logger.task_id = None
            self._pool.checkin(agents)
            result_msg = {
                "type": "result",
                "rank": self.rank,
                "task_id": task.id,
                "success": result.success,
                "winner_id": result.winner_id,
                "error": result.error,
                "failure_cause": result.failure_cause,
                "pre_merge_hash": result.pre_merge_hash,
                "post_merge_hash": result.post_merge_hash,
                "capacity": self.capacity,
            }

        await send_queue.put(result_msg)
        logger.info("[rank %d] Task %s done (success=%s)", self.rank, task.id, result_msg["success"])
