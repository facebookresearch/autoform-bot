from __future__ import annotations

import hashlib
import io
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli import __main__ as cli, work as work_module
from autoform_cli.runtime import load_runtime_graph
from autoform_cli.work import WORK_SCHEMA, WorkError, list_ready_work, work_context


def _article(
    project: Path,
    name: str,
    *,
    title: str,
    metadata: list[str],
    depends: str = "",
    proof_depends: str = "",
) -> None:
    path = project / "blueprint/roadmap/chapter" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ["---", *metadata, "---", "", f"# {title}", "", "A precise statement."]
    if depends:
        text.extend(["", "## Depends on", "", f"- [dependency]({depends})"])
    if proof_depends:
        text.extend(["", "## Proof depends on", "", f"- [dependency]({proof_depends})"])
    path.write_text("\n".join(text) + "\n", encoding="utf-8")


def _edit(project: Path, name: str, old: str, new: str) -> Path:
    path = project / "blueprint/roadmap/chapter" / name
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    _article(project, "README.md", title="Chapter", metadata=["article_id: af_0000000000000000000000c0"])
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


def test_proof_prerequisites_gate_both_phases(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(
        project,
        "lemma.md",
        title="Lemma",
        metadata=[
            "article_id: af_000000000000000000000006",
            "declaration: lemma",
            "statement: formalized",
            "lean: Project.lemma",
        ],
        depends="base.md",
    )
    _article(
        project,
        "main.md",
        title="Main",
        metadata=[
            "article_id: af_000000000000000000000007",
            "declaration: theorem",
            "statement: formalized",
            "lean: Project.main",
        ],
        proof_depends="lemma.md",
    )
    _article(
        project,
        "corollary.md",
        title="Corollary",
        metadata=[
            "article_id: af_000000000000000000000008",
            "declaration: theorem",
        ],
        depends="base.md",
        proof_depends="lemma.md",
    )
    _article(
        project,
        "construction.md",
        title="Construction",
        metadata=[
            "article_id: af_00000000000000000000000a",
            "declaration: def",
        ],
        depends="base.md",
        proof_depends="lemma.md",
    )

    assert [(item.node_id, item.phase) for item in list_ready_work(project).items] == [
        ("chapter/lemma", "proof"),
        ("chapter/prove", "proof"),
        ("chapter/state", "statement"),
    ]
    for selector in ("chapter/main", "chapter/corollary", "chapter/construction"):
        _, item = work_context(project, selector)
        assert not item.ready and item.phase is None
        assert item.blockers == ("chapter/lemma",)
        assert "chapter/lemma" in item.dependencies

    _edit(project, "lemma.md", "statement: formalized\n", "statement: formalized\nproof: formalized\n")
    phases = {item.node_id: item.phase for item in list_ready_work(project).items}
    assert phases["chapter/main"] == "proof"
    assert phases["chapter/corollary"] == "statement"
    assert phases["chapter/construction"] == "statement"
    assert "chapter/lemma" not in phases


def test_not_ready_leaves_and_containers_are_never_dispatched(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(
        project,
        "placeholder.md",
        title="Placeholder",
        metadata=[
            "article_id: af_000000000000000000000005",
            "declaration: theorem",
            "not_ready: true",
        ],
        depends="base.md",
    )

    listed = {item.node_id for item in list_ready_work(project).items}
    _, placeholder = work_context(project, "chapter/placeholder")
    _, chapter = work_context(project, "af_0000000000000000000000c0")

    assert "chapter/placeholder" not in listed and "chapter" not in listed
    assert placeholder.phase is None and placeholder.blockers == ("roadmap:not-ready",)
    assert chapter.phase is None and chapter.blockers == ("roadmap:not-a-formalizable-leaf",)


def test_finished_articles_and_containers_need_no_identity(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _edit(project, "base.md", "article_id: af_000000000000000000000001\n", "")
    _edit(project, "README.md", "article_id: af_0000000000000000000000c0\n", "")

    assert [item.node_id for item in list_ready_work(project).items] == ["chapter/prove", "chapter/state"]
    _, base = work_context(project, "chapter/base")
    _, chapter = work_context(project, "chapter")
    assert base.phase is None and base.blockers == ()
    assert chapter.blockers == ("roadmap:not-a-formalizable-leaf",)


def test_proof_without_statement_returns_to_roadmap(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _edit(
        project,
        "state.md",
        "declaration: theorem\n",
        "declaration: theorem\nproof: formalized\nlean: Project.state\n",
    )

    assert "chapter/state" not in {item.node_id for item in list_ready_work(project).items}
    _, item = work_context(project, "chapter/state")
    assert item.phase is None
    assert item.blockers == ("roadmap:proof-without-statement",)


def test_article_revision_tracks_only_its_own_article(tmp_path: Path) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/chapter/state.md"
    _, before = work_context(project, "chapter/state")
    assert before.article_revision == hashlib.sha256(article.read_bytes()).hexdigest()

    _edit(project, "prove.md", "A precise statement.", "A precise statement, now explained.")
    _, after_sibling = work_context(project, "chapter/state")
    assert after_sibling.article_revision == before.article_revision

    _edit(project, "state.md", "A precise statement.", "A sharper statement.")
    _, after_edit = work_context(project, "chapter/state")
    assert after_edit.article_revision == hashlib.sha256(article.read_bytes()).hexdigest()
    assert after_edit.article_revision != before.article_revision


def test_frontier_requires_durable_identity_for_unfinished_leaves(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _edit(project, "state.md", "article_id: af_000000000000000000000002\n", "")

    with pytest.raises(WorkError, match="durable article_id.*autoform migrate article-ids"):
        list_ready_work(project)
    _, context = work_context(project, "chapter/state")
    assert not context.ready
    assert context.blockers == ("roadmap:missing-article-id",)


def test_missing_identity_is_reported_before_not_ready(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _edit(project, "state.md", "article_id: af_000000000000000000000002\n", "not_ready: true\n")

    with pytest.raises(WorkError, match="durable article_id"):
        list_ready_work(project)
    _, context = work_context(project, "chapter/state")
    assert context.blockers == ("roadmap:missing-article-id",)


def test_a_dependency_listed_in_both_sections_is_reported_once(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(
        project,
        "twice.md",
        title="Twice",
        metadata=["article_id: af_000000000000000000000009", "declaration: theorem"],
        depends="state.md",
        proof_depends="state.md",
    )

    _, item = work_context(project, "chapter/twice")
    assert item.blockers == ("chapter/state",)


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
    (project / "Project.lean").write_text(
        "namespace Project\n\ntheorem prove : True := trivial\n\nend Project\n",
        encoding="utf-8",
    )
    source_revision = load_runtime_graph(project).source_revision
    prove_revision = hashlib.sha256(
        (project / "blueprint/roadmap/chapter/prove.md").read_bytes()
    ).hexdigest()

    assert cli.main(["work", "list", str(project), "--lean-root", str(project), "--json"]) == 0
    frontier = json.loads(capsys.readouterr().out)
    assert set(frontier) == {"items", "schema", "source_revision"}
    assert frontier["schema"] == WORK_SCHEMA
    assert frontier["source_revision"] == source_revision
    assert [item["phase"] for item in frontier["items"]] == ["proof", "statement"]
    assert frontier["items"][0] == {
        "article_id": "af_000000000000000000000003",
        "article_path": "blueprint/roadmap/chapter/prove.md",
        "article_revision": prove_revision,
        "blockers": [],
        "claim_target": "af_000000000000000000000003",
        "dependencies": ["chapter/base"],
        "lean_targets": [{"declaration": "Project.prove", "source_file": "Project.lean"}],
        "node_id": "chapter/prove",
        "phase": "proof",
        "ready": True,
        "source_targets": [],
        "state": "can_prove",
        "title": "Prove me",
    }

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
    assert set(context) == {"item", "schema", "source_revision"}
    assert context["schema"] == WORK_SCHEMA
    assert context["source_revision"] == source_revision
    assert context["item"]["claim_target"] == "af_000000000000000000000002"
    assert len(context["item"]["article_revision"]) == 64

    assert cli.main(["work", "context", "chapter/blocked", str(project), "--json"]) == 0
    blocked = json.loads(capsys.readouterr().out)["item"]
    assert blocked["ready"] is False and blocked["phase"] is None
    assert blocked["blockers"] == ["chapter/state"]
    assert blocked["dependencies"] == ["chapter/state"]


def test_work_cli_context_reports_lean_locations_and_sources(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)
    (project / "Project.lean").write_text(
        "namespace Project\n\ntheorem prove : True := trivial\n\nend Project\n",
        encoding="utf-8",
    )
    article = project / "blueprint/roadmap/chapter/prove.md"
    article.write_text(
        article.read_text(encoding="utf-8") + "\n## Sources\n\n- [paper](https://example.invalid/paper)\n",
        encoding="utf-8",
    )
    command = ["work", "context", "af_000000000000000000000003", str(project), "--lean-root", str(project)]

    assert cli.main([*command, "--json"]) == 0
    item = json.loads(capsys.readouterr().out)["item"]
    assert item["lean_targets"] == [{"declaration": "Project.prove", "source_file": "Project.lean"}]
    assert item["source_targets"] == ["https://example.invalid/paper"]

    assert cli.main(command) == 0
    context = capsys.readouterr().out.splitlines()
    assert "Lean: Project.prove (Project.lean)" in context
    assert "Sources: https://example.invalid/paper" in context


def test_work_cli_human_output_is_ascii_and_escapes_project_text(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)
    _edit(project, "state.md", "# State me", "# State me\x1b[2J")

    assert cli.main(["work", "list", str(project)]) == 0
    listed = capsys.readouterr().out
    listed.encode("ascii")
    assert "\x1b" not in listed
    assert "statement: chapter/state [af_000000000000000000000002] - State me\\x1b[2J" in listed.splitlines()

    assert cli.main(["work", "context", "chapter/state", str(project)]) == 0
    output = capsys.readouterr().out
    output.encode("ascii")
    assert "\x1b" not in output
    context = output.splitlines()
    assert "State me\\x1b[2J (chapter/state)" in context
    assert "Phase: statement" in context
    assert "Claim target: af_000000000000000000000002" in context
    assert "Dependencies: chapter/base" in context
    assert any(line.startswith("Article revision: ") and len(line) == 18 + 64 for line in context)
    assert any(line.startswith("Graph source revision: ") and len(line) == 23 + 64 for line in context)

    assert cli.main(["work", "context", "chapter/base", str(project)]) == 0
    assert "Phase: none (already formalized)" in capsys.readouterr().out.splitlines()

    assert cli.main(["work", "context", "chapter/blocked", str(project)]) == 0
    blocked = capsys.readouterr().out.splitlines()
    assert "Phase: not ready" in blocked
    assert "Blocked by: chapter/state" in blocked

    empty = tmp_path / "empty"
    _article(empty, "README.md", title="Chapter", metadata=[])
    assert cli.main(["work", "list", str(empty)]) == 0
    assert capsys.readouterr().out == "No ready formalization work.\n"


def test_work_cli_reports_errors_on_stderr_with_exit_2(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)

    assert cli.main(["work", "context", "chapter/state.md", str(project)]) == 2
    assert capsys.readouterr().err.startswith("error: no article matches 'chapter/state.md'")

    assert cli.main(["work", "list", str(project), "--lean-root", str(tmp_path / "missing")]) == 2
    assert capsys.readouterr().err == "error: Lean root does not exist or is not a directory\n"

    for name in ("bad-one.md", "bad-two.md"):
        _article(project, name, title=name, metadata=["article_id: not-an-id"])
    assert cli.main(["work", "list", str(project)]) == 2
    errors = capsys.readouterr().err.splitlines()
    assert len(errors) == 2 and all("malformed article_id" in line for line in errors)

    for failure in (
        PermissionError(13, "Permission denied", "/private/secret/blueprint"),
        RuntimeError("Symlink loop from '/private/secret/blueprint'"),
    ):

        def unreadable(*_args, failure: Exception = failure, **_kwargs):
            raise failure

        monkeypatch.setattr(cli, "list_ready_work", unreadable)
        assert cli.main(["work", "list", str(project)]) == 2
        error = capsys.readouterr().err
        assert error.startswith("error: ") and "/private/secret" not in error


def test_work_cli_does_not_report_output_errors_as_unreadable_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)

    class ClosedPipe(io.StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    with pytest.raises(BrokenPipeError):
        cli.main(["work", "list", str(project)])


def test_node_ids_cannot_impersonate_article_ids(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/af_000000000000000000000002.md").write_text(
        "# Impostor\n", encoding="utf-8"
    )

    assert cli.main(["work", "context", "af_000000000000000000000002", str(project)]) == 2
    assert "node id has the form of an article_id" in capsys.readouterr().err
