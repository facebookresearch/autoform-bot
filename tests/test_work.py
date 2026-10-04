from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli import __main__ as cli, work as work_module
from autoform_cli.runtime import load_runtime_graph
from autoform_cli.work import WORK_SCHEMA, WorkError, list_ready_work, work_context


def _article(project: Path, name: str, *, title: str, metadata: list[str], depends: str = "") -> None:
    path = project / "blueprint/roadmap/chapter" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ["---", *metadata, "---", "", f"# {title}", "", "A precise statement."]
    if depends:
        text.extend(["", "## Depends on", "", f"- [dependency]({depends})"])
    path.write_text("\n".join(text) + "\n", encoding="utf-8")


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    _article(project, "README.md", title="Chapter", metadata=[])
    _article(
        project,
        "base.md",
        title="Base",
        metadata=[
            "article_id: af_000000000000000000000001",
            "declaration: definition",
            "statement: formalized",
            "lean: Project.base",
        ],
    )
    _article(
        project,
        "state.md",
        title="State me",
        metadata=[
            "article_id: af_000000000000000000000002",
            "declaration: theorem",
        ],
        depends="base.md",
    )
    _article(
        project,
        "prove.md",
        title="Prove me",
        metadata=[
            "article_id: af_000000000000000000000003",
            "declaration: theorem",
            "statement: formalized",
            "lean: Project.prove",
        ],
        depends="base.md",
    )
    _article(
        project,
        "blocked.md",
        title="Blocked",
        metadata=[
            "article_id: af_000000000000000000000004",
            "declaration: theorem",
        ],
        depends="state.md",
    )
    return project


def test_lists_only_the_markdown_derived_ready_frontier(tmp_path: Path) -> None:
    frontier = list_ready_work(_project(tmp_path))

    assert frontier.as_dict()["schema"] == WORK_SCHEMA
    assert [(item.node_id, item.phase, item.claim_target) for item in frontier.items] == [
        ("chapter/prove", "proof", "af_000000000000000000000003"),
        ("chapter/state", "statement", "af_000000000000000000000002"),
    ]


def test_context_accepts_durable_identity_and_reports_blockers(tmp_path: Path) -> None:
    project = _project(tmp_path)
    revision, ready = work_context(project, "af_000000000000000000000003")
    _, blocked = work_context(project, "chapter/blocked")

    assert len(revision) == 64
    assert ready.ready and ready.phase == "proof"
    assert len(ready.article_revision) == 64
    assert ready.article_path == "blueprint/roadmap/chapter/prove.md"
    assert ready.dependencies == ("chapter/base",)
    assert [target.as_dict() for target in ready.lean_targets] == [
        {"declaration": "Project.prove", "source_file": None}
    ]
    assert blocked.phase is None
    assert blocked.blockers == ("chapter/state",)


def test_frontier_requires_durable_identity_for_unfinished_leaves(tmp_path: Path) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/chapter/state.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "article_id: af_000000000000000000000002\n",
            "",
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkError, match="durable article_id"):
        list_ready_work(project)
    _, context = work_context(project, "chapter/state")
    assert not context.ready
    assert context.blockers == ("roadmap:missing-article-id",)


def test_frontier_requires_article_revision_for_unfinished_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    runtime = load_runtime_graph(project)
    runtime_without_revision = replace(
        runtime,
        nodes=tuple(
            replace(node, source_sha256=None) if node.id == "chapter/state" else node
            for node in runtime.nodes
        ),
    )
    monkeypatch.setattr(
        work_module,
        "load_runtime_graph",
        lambda *_args, **_kwargs: runtime_without_revision,
    )

    with pytest.raises(WorkError, match="durable article revision"):
        list_ready_work(project)
    _, context = work_context(project, "chapter/state")
    assert not context.ready
    assert context.blockers == ("roadmap:missing-article-revision",)


def test_work_cli_emits_stable_json(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)

    assert cli.main(["work", "list", str(project), "--json"]) == 0
    frontier = json.loads(capsys.readouterr().out)
    assert frontier["schema"] == WORK_SCHEMA
    assert [item["phase"] for item in frontier["items"]] == ["proof", "statement"]

    assert cli.main(
        [
            "work",
            "context",
            "af_000000000000000000000002",
            str(project),
            "--json",
        ]
    ) == 0
    context = json.loads(capsys.readouterr().out)
    assert context["item"]["claim_target"] == "af_000000000000000000000002"
    assert len(context["item"]["article_revision"]) == 64
