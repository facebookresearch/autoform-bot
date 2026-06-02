# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Typed configuration for autoform_bot.

Parse YAML once at the boundary into frozen dataclasses.
Downstream code receives structured types — no raw dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.coordination.multinode import NodePickStrategy


@dataclass(frozen=True)
class PipelineConfig:
    """All configuration needed to run the autoformalization pipeline."""

    run_path: Path
    mathlib_path: Path
    agents_per_node: int
    min_agents_per_task: int
    max_agents_per_task: int
    max_concurrent_llm_calls: int
    model: str
    solver: str = "agent"
    num_repls_per_node: int | None = None
    pick_strategy: NodePickStrategy = NodePickStrategy.BIGGEST_FIRST
    lib_name: str = "Formalization"
    book_path: str | None = None
    book_files: list[str] | None = None
    targets_file: Path | None = None
    max_review_cycles: int = 0

    @staticmethod
    def from_yaml(
        cfg: dict,
        *,
        run_path: Path,
        app_dir: Path,
        agents_per_node: int | None = None,
    ) -> PipelineConfig:
        """Single parse point — resolve all paths, apply defaults, return frozen config."""
        workspace_config = cfg.get("workspace", {})
        workers_config = cfg.get("workers", {})
        llm_config = cfg.get("llm", {})
        book_config = cfg.get("book", {})

        # Resolve mathlib path
        mathlib_raw = workspace_config.get("mathlib_path", "submodules/mathlib")
        mathlib_path = Path(mathlib_raw).expanduser()
        repo_root = app_dir.parent.parent
        if not mathlib_path.is_absolute():
            mathlib_path = (repo_root / mathlib_path).resolve()
        else:
            mathlib_path = mathlib_path.resolve()

        raw_strategy = workers_config.get("pick_strategy", "biggest_first")

        # Resolve targets file path
        targets_raw = book_config.get("targets", "targets.yaml")
        targets_path = Path(targets_raw)
        if not targets_path.is_absolute():
            book_data_dir = repo_root / "autoform" / "data" / book_config.get("path", "")
            targets_path = (book_data_dir / targets_path).resolve()
        targets_file = targets_path if targets_path.exists() else None

        return PipelineConfig(
            run_path=run_path,
            mathlib_path=mathlib_path,
            agents_per_node=agents_per_node or workers_config.get("agents_per_node", 2),
            min_agents_per_task=workers_config.get("min_agents_per_task", 1),
            max_agents_per_task=workers_config.get("max_agents_per_task", 1),
            max_concurrent_llm_calls=workers_config.get("max_concurrent_llm_calls", 2),
            model=llm_config.get("model", "Opus 4.6"),
            solver=llm_config.get("solver", "agent"),
            num_repls_per_node=workers_config.get("num_repls_per_node"),
            pick_strategy=NodePickStrategy(raw_strategy),
            lib_name=workspace_config.get("lib_name", "Formalization"),
            book_path=book_config.get("path"),
            book_files=book_config.get("files"),
            targets_file=targets_file,
            max_review_cycles=workers_config.get("max_review_cycles", 0),
        )
