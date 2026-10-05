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
    assert set(frontier) == {"items", "open_statements", "schema", "source_revision"}
    assert frontier["schema"] == WORK_SCHEMA
    assert frontier["open_statements"] is False
    assert frontier["source_revision"] == source_revision
    assert [item["phase"] for item in frontier["items"]] == ["proof", "statement"]
    assert frontier["items"][0] == {
        "article_id": "af_000000000000000000000003",
        "article_path": "blueprint/roadmap/chapter/prove.md",
        "article_revision": prove_revision,
        "assumes": [],
        "blockers": [],
        "claim_target": "af_000000000000000000000003",
        "dependencies": ["chapter/base"],
        "lean_targets": [{"declaration": "Project.prove", "source_file": "Project.lean"}],
        "node_id": "chapter/prove",
        "open_statements": False,
        "phase": "proof",
        "ready": True,
        "revision": False,
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
    with pytest.raises(BrokenPipeError):
        cli.main(["work", "assumptions", str(project)])


def test_node_ids_cannot_impersonate_article_ids(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/af_000000000000000000000002.md").write_text(
        "# Impostor\n", encoding="utf-8"
    )

    assert cli.main(["work", "context", "af_000000000000000000000002", str(project)]) == 2
    assert "node id has the form of an article_id" in capsys.readouterr().err


def _policy_project(tmp_path: Path, policy: str | None) -> Path:
    """`_project` plus an open theorem, a reduction proved from it, and articles resting on them.

    *policy* is the `open_statements` value in `roadmap/README.md`; ``None``
    writes no roadmap page, so the project keeps the default strict policy.
    """
    project = _project(tmp_path)
    if policy is not None:
        (project / "blueprint/roadmap/README.md").write_text(
            f"---\nopen_statements: {policy}\n---\n\n# Roadmap\n", encoding="utf-8"
        )
    stated = ["declaration: theorem", "statement: formalized"]
    _article(
        project,
        "open.md",
        title="Open",
        metadata=["article_id: af_00000000000000000000000b", *stated, "lean: Project.open_thm Project.open_aux"],
        depends="base.md",
    )
    _article(
        project,
        "reduction.md",
        title="Reduction",
        metadata=["article_id: af_00000000000000000000000c", *stated, "proof: formalized", "lean: Project.reduction"],
        proof_depends="open.md",
    )
    _article(
        project,
        "uses.md",
        title="Uses",
        metadata=["article_id: af_00000000000000000000000d", *stated, "lean: Project.uses"],
        depends="open.md",
    )
    _article(
        project,
        "corollary.md",
        title="Corollary",
        metadata=["article_id: af_00000000000000000000000e", "declaration: theorem"],
        depends="reduction.md",
    )
    # The contract lists the upstream article, never open, but not the one that names no declaration.
    _article(
        project,
        "upstream.md",
        title="Upstream",
        metadata=["declaration: theorem", "mathlib: true", "lean: Project.upstream"],
    )
    _article(project, "unnamed.md", title="Unnamed", metadata=["declaration: def", "statement: formalized"])
    return project


def _blocked_articles(project: Path) -> None:
    """Add articles whose prerequisites block them differently under the two policies."""
    _article(
        project,
        "waits.md",
        title="Waits",
        metadata=["article_id: af_000000000000000000000010", "declaration: theorem"],
        depends="state.md",
        proof_depends="prove.md",
    )
    _edit(project, "waits.md", "- [dependency](prove.md)", "- [dependency](prove.md)\n- [dependency](state.md)")
    _article(
        project,
        "stuck.md",
        title="Stuck",
        metadata=[
            "article_id: af_000000000000000000000011",
            "declaration: theorem",
            "statement: formalized",
            "lean: Project.stuck",
        ],
        depends="blocked.md",
        proof_depends="prove.md",
    )
    _edit(project, "stuck.md", "- [dependency](prove.md)", "- [dependency](prove.md)\n- [dependency](state.md)")
    _article(
        project,
        "main.md",
        title="Main",
        metadata=[
            "article_id: af_000000000000000000000012",
            "declaration: theorem",
            "statement: formalized",
            "lean: Project.main",
        ],
        proof_depends="prove.md",
    )


def _blockers(project: Path) -> dict[str, tuple[str | None, tuple[str, ...]]]:
    selectors = ("chapter/waits", "chapter/stuck", "chapter/main")
    items = {selector: work_context(project, selector)[1] for selector in selectors}
    return {selector: (item.phase, item.blockers) for selector, item in items.items()}


def test_strict_blockers_list_unstated_statement_prerequisites_then_unproved_proof_ones(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _blocked_articles(project)

    assert _blockers(project) == {
        "chapter/waits": (None, ("chapter/state", "chapter/prove")),
        "chapter/stuck": (None, ("chapter/blocked", "chapter/prove", "chapter/state")),
        "chapter/main": (None, ("chapter/prove",)),
    }


def test_open_blockers_wait_only_for_prerequisites_to_be_stated(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/README.md").write_text(
        "---\nopen_statements: allowed\n---\n\n# Roadmap\n", encoding="utf-8"
    )
    _blocked_articles(project)

    assert _blockers(project) == {
        # Unstated, so only its statement prerequisites can hold it back.
        "chapter/waits": (None, ("chapter/state",)),
        # Stated: prove is stated too, so only the unstated prerequisites remain.
        "chapter/stuck": (None, ("chapter/blocked", "chapter/state")),
        "chapter/main": ("proof", ()),
    }
    _, main = work_context(project, "chapter/main")
    assert main.assumes == ("chapter/prove",)
    assert "chapter/main" in {item.node_id for item in list_ready_work(project).items}


def test_open_work_holds_a_definition_until_its_proof_prerequisites_are_stated(tmp_path: Path) -> None:
    """A definition cannot land open, so its body needs every prerequisite's declaration."""
    project = _policy_project(tmp_path, "allowed")
    _article(
        project,
        "data.md",
        title="Data",
        metadata=["article_id: af_000000000000000000000013", "declaration: definition"],
        proof_depends="state.md",
    )

    _, data = work_context(project, "chapter/data")
    assert (data.phase, data.blockers) == (None, ("chapter/state",))
    assert "chapter/data" not in {item.node_id for item in list_ready_work(project).items}

    # An open statement is stated, so it is enough.
    _edit(project, "data.md", "- [dependency](state.md)", "- [dependency](open.md)")
    _, data = work_context(project, "chapter/data")
    assert (data.phase, data.blockers, data.assumes) == ("statement", (), ("chapter/open",))


def test_strict_work_text_is_unchanged(tmp_path: Path, capsys) -> None:
    project = _policy_project(tmp_path, None)
    revision = load_runtime_graph(project).source_revision
    uses = hashlib.sha256((project / "blueprint/roadmap/chapter/uses.md").read_bytes()).hexdigest()

    assert cli.main(["work", "list", str(project)]) == 0
    assert capsys.readouterr().out == (
        "statement: chapter/corollary [af_00000000000000000000000e] - Corollary\n"
        "proof: chapter/open [af_00000000000000000000000b] - Open\n"
        "proof: chapter/prove [af_000000000000000000000003] - Prove me\n"
        "statement: chapter/state [af_000000000000000000000002] - State me\n"
        "proof: chapter/uses [af_00000000000000000000000d] - Uses\n"
    )

    assert cli.main(["work", "context", "chapter/uses", str(project)]) == 0
    assert capsys.readouterr().out == (
        "Uses (chapter/uses)\n"
        "State: can_prove\n"
        "Phase: proof\n"
        "Claim target: af_00000000000000000000000d\n"
        "Article: blueprint/roadmap/chapter/uses.md\n"
        f"Article revision: {uses}\n"
        f"Graph source revision: {revision}\n"
        "Dependencies: chapter/open\n"
        "Lean: Project.uses\n"
    )


def test_open_work_text_names_the_policy_and_what_each_item_assumes(tmp_path: Path, capsys) -> None:
    project = _policy_project(tmp_path, "allowed")
    revision = load_runtime_graph(project).source_revision
    uses = hashlib.sha256((project / "blueprint/roadmap/chapter/uses.md").read_bytes()).hexdigest()

    assert cli.main(["work", "list", str(project)]) == 0
    assert capsys.readouterr().out == (
        "Open statements: allowed (a statement may land with a sorry proof)\n"
        "statement: chapter/corollary [af_00000000000000000000000e] - Corollary\n"
        "  assumes: chapter/open\n"
        "proof: chapter/open [af_00000000000000000000000b] - Open\n"
        "proof: chapter/prove [af_000000000000000000000003] - Prove me\n"
        "statement: chapter/state [af_000000000000000000000002] - State me\n"
        "proof: chapter/uses [af_00000000000000000000000d] - Uses\n"
        "  assumes: chapter/open\n"
    )

    assert cli.main(["work", "context", "chapter/uses", str(project)]) == 0
    assert capsys.readouterr().out == (
        "Uses (chapter/uses)\n"
        "State: can_prove\n"
        "Phase: proof\n"
        "Open statements: allowed\n"
        "Assumes: chapter/open\n"
        "Claim target: af_00000000000000000000000d\n"
        "Article: blueprint/roadmap/chapter/uses.md\n"
        f"Article revision: {uses}\n"
        f"Graph source revision: {revision}\n"
        "Dependencies: chapter/open\n"
        "Lean: Project.uses\n"
    )

    assert cli.main(["work", "context", "chapter/prove", str(project)]) == 0
    context = capsys.readouterr().out.splitlines()
    assert "Open statements: allowed" in context
    assert not any(line.startswith("Assumes:") for line in context)

    assert cli.main(["work", "list", str(project), "--json"]) == 0
    frontier = json.loads(capsys.readouterr().out)
    assert frontier["open_statements"] is True
    assert {item["node_id"]: item["assumes"] for item in frontier["items"]} == {
        "chapter/corollary": ["chapter/open"],
        "chapter/open": [],
        "chapter/prove": [],
        "chapter/state": [],
        "chapter/uses": ["chapter/open"],
    }
    assert all(item["open_statements"] is True for item in frontier["items"])


def test_open_work_list_names_the_policy_even_when_nothing_is_ready(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    _article(project, "README.md", title="Chapter", metadata=[])
    (project / "blueprint/roadmap/README.md").write_text(
        "---\nopen_statements: allowed\n---\n\n# Roadmap\n", encoding="utf-8"
    )

    assert cli.main(["work", "list", str(project)]) == 0
    assert capsys.readouterr().out == (
        "Open statements: allowed (a statement may land with a sorry proof)\n"
        "No ready formalization work.\n"
    )


def _contract_article(
    node_id: str,
    article_id: str | None,
    state: str,
    declarations: list[str],
    *,
    open_: bool = False,
    assumes: tuple[str, ...] = (),
    allowed: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "allowed_open_declarations": list(allowed),
        "article_id": article_id,
        "assumes": list(assumes),
        "declarations": declarations,
        "id": node_id,
        "open": open_,
        "state": state,
    }


def test_work_assumptions_under_the_strict_policy_lists_articles_with_nothing_open(
    tmp_path: Path, capsys
) -> None:
    project = _policy_project(tmp_path, "forbidden")

    assert cli.main(["work", "assumptions", str(project), "--json"]) == 0
    output = capsys.readouterr().out
    contract = json.loads(output)
    assert output == json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n"
    assert contract == {
        "schema": work_module.ASSUMPTIONS_SCHEMA,
        "open_statements": False,
        "source_revision": load_runtime_graph(project).source_revision,
        "articles": [
            _contract_article("chapter/base", "af_000000000000000000000001", "fully_proved", ["Project.base"]),
            _contract_article(
                "chapter/open", "af_00000000000000000000000b", "can_prove", ["Project.open_thm", "Project.open_aux"]
            ),
            _contract_article("chapter/prove", "af_000000000000000000000003", "can_prove", ["Project.prove"]),
            _contract_article("chapter/reduction", "af_00000000000000000000000c", "proved", ["Project.reduction"]),
            _contract_article("chapter/upstream", None, "mathlib", ["Project.upstream"]),
            _contract_article("chapter/uses", "af_00000000000000000000000d", "can_prove", ["Project.uses"]),
        ],
    }

    assert cli.main(["work", "assumptions", str(project)]) == 0
    assert capsys.readouterr().out == "Open statements: forbidden\n"


def test_work_assumptions_under_the_open_policy_bounds_each_article(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _policy_project(tmp_path, "allowed")
    open_declarations = ("Project.open_aux", "Project.open_thm")

    assert cli.main(["work", "assumptions", str(project), "--json"]) == 0
    contract = json.loads(capsys.readouterr().out)
    assert contract == {
        "schema": "autoform-assumptions/v1",
        "open_statements": True,
        "source_revision": load_runtime_graph(project).source_revision,
        "articles": [
            _contract_article("chapter/base", "af_000000000000000000000001", "fully_proved", ["Project.base"]),
            # Declarations keep their authored order; the allowance is sorted.
            _contract_article(
                "chapter/open",
                "af_00000000000000000000000b",
                "can_prove",
                ["Project.open_thm", "Project.open_aux"],
                open_=True,
                allowed=open_declarations,
            ),
            _contract_article(
                "chapter/prove",
                "af_000000000000000000000003",
                "can_prove",
                ["Project.prove"],
                open_=True,
                allowed=("Project.prove",),
            ),
            _contract_article(
                "chapter/reduction",
                "af_00000000000000000000000c",
                "conditional",
                ["Project.reduction"],
                assumes=("chapter/open",),
                allowed=open_declarations,
            ),
            _contract_article("chapter/upstream", None, "mathlib", ["Project.upstream"]),
            _contract_article(
                "chapter/uses",
                "af_00000000000000000000000d",
                "can_prove",
                ["Project.uses"],
                open_=True,
                assumes=("chapter/open",),
                allowed=(*open_declarations, "Project.uses"),
            ),
        ],
    }

    monkeypatch.chdir(project)
    assert cli.main(["work", "assumptions", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == contract

    assert cli.main(["work", "assumptions", str(project / "blueprint")]) == 0
    assert capsys.readouterr().out == (
        "Open statements: allowed\n"
        "open: chapter/open (Project.open_thm, Project.open_aux)\n"
        "open: chapter/prove (Project.prove)\n"
        "conditional: chapter/reduction assumes chapter/open\n"
        "open: chapter/uses (Project.uses) assumes chapter/open\n"
    )


@pytest.mark.parametrize("policy", ["forbidden", "allowed"])
def test_work_assumptions_keeps_a_retracted_theorem_while_its_lean_names_the_old_declaration(
    tmp_path: Path, capsys, policy: str
) -> None:
    """Roadmap retracts a statement but keeps `lean:`; the old sorry stays declared until Formalize restates it."""
    project = _policy_project(tmp_path, policy)
    _edit(project, "open.md", "statement: formalized\n", "statement: retracted\n")
    allowed = ("Project.open_aux", "Project.open_thm") if policy == "allowed" else ()

    assert cli.main(["work", "assumptions", str(project), "--json"]) == 0
    articles = {article["id"]: article for article in json.loads(capsys.readouterr().out)["articles"]}

    assert articles["chapter/open"] == _contract_article(
        "chapter/open",
        "af_00000000000000000000000b",
        "can_state",
        ["Project.open_thm", "Project.open_aux"],
        open_=policy == "allowed",
        allowed=allowed,
    )
    assert articles["chapter/reduction"]["state"] == ("conditional" if policy == "allowed" else "proved")
    assert articles["chapter/reduction"]["allowed_open_declarations"] == list(allowed)


@pytest.mark.parametrize("policy", ["forbidden", "allowed"])
def test_work_assumptions_bounds_a_proof_recorded_without_its_statement(
    tmp_path: Path, capsys, policy: str
) -> None:
    """`proof: formalized` without `statement: formalized` still declares Lean the CI probe must bound."""
    project = _policy_project(tmp_path, policy)
    _edit(project, "reduction.md", "statement: formalized\n", "")
    allowed = ("Project.open_aux", "Project.open_thm") if policy == "allowed" else ()

    assert cli.main(["work", "assumptions", str(project), "--json"]) == 0
    articles = {article["id"]: article for article in json.loads(capsys.readouterr().out)["articles"]}

    assert articles["chapter/reduction"] == _contract_article(
        "chapter/reduction",
        "af_00000000000000000000000c",
        "conditional" if policy == "allowed" else "proved",
        ["Project.reduction"],
        assumes=("chapter/open",) if policy == "allowed" else (),
        allowed=allowed,
    )


def test_work_assumptions_does_not_open_a_never_stated_theorem_naming_a_draft_lean(tmp_path: Path, capsys) -> None:
    """A draft `lean:` name on a theorem that was never stated is no assumption, so CI rejects its sorry."""
    project = _policy_project(tmp_path, "allowed")
    _edit(project, "open.md", "statement: formalized\n", "")

    assert cli.main(["work", "assumptions", str(project), "--json"]) == 0
    articles = {article["id"]: article for article in json.loads(capsys.readouterr().out)["articles"]}

    assert articles["chapter/open"] == _contract_article(
        "chapter/open", "af_00000000000000000000000b", "can_state", ["Project.open_thm", "Project.open_aux"]
    )
    assert articles["chapter/reduction"] == _contract_article(
        "chapter/reduction", "af_00000000000000000000000c", "proved", ["Project.reduction"]
    )
    assert not any(
        name in article["allowed_open_declarations"]
        for article in articles.values()
        for name in ("Project.open_thm", "Project.open_aux")
    )


def test_work_assumptions_text_labels_only_conditional_articles_as_conditional(tmp_path: Path, capsys) -> None:
    """A conditional article without `lean:` is named too; an unproved one that assumes something is not conditional."""
    project = _policy_project(tmp_path, "allowed")
    _article(
        project,
        "bare.md",
        title="Bare",
        metadata=["declaration: theorem", "statement: formalized", "proof: formalized"],
        proof_depends="open.md",
    )
    _article(
        project,
        "old.md",
        title="Old",
        metadata=["declaration: def", "statement: retracted", "lean: Project.old"],
        proof_depends="open.md",
    )

    assert cli.main(["work", "assumptions", str(project), "--json"]) == 0
    articles = {article["id"]: article for article in json.loads(capsys.readouterr().out)["articles"]}
    assert "chapter/bare" not in articles
    assert articles["chapter/old"] == _contract_article(
        "chapter/old",
        None,
        "can_state",
        ["Project.old"],
        assumes=("chapter/open",),
        allowed=("Project.open_aux", "Project.open_thm"),
    )

    # chapter/corollary assumes chapter/open too, but names no declaration and is not proved.
    assert cli.main(["work", "assumptions", str(project)]) == 0
    assert capsys.readouterr().out == (
        "Open statements: allowed\n"
        "conditional: chapter/bare assumes chapter/open\n"
        "unproved: chapter/old assumes chapter/open\n"
        "open: chapter/open (Project.open_thm, Project.open_aux)\n"
        "open: chapter/prove (Project.prove)\n"
        "conditional: chapter/reduction assumes chapter/open\n"
        "open: chapter/uses (Project.uses) assumes chapter/open\n"
    )


def test_work_flags_a_retracted_article_as_a_revision(tmp_path: Path, capsys) -> None:
    project = _policy_project(tmp_path, "allowed")
    _edit(project, "open.md", "statement: formalized\n", "statement: retracted\n")

    assert cli.main(["work", "list", str(project), "--json"]) == 0
    items = {item["node_id"]: item for item in json.loads(capsys.readouterr().out)["items"]}
    assert (items["chapter/open"]["phase"], items["chapter/open"]["revision"]) == ("statement", True)
    assert {node_id for node_id, item in items.items() if item["revision"]} == {"chapter/open"}

    assert cli.main(["work", "list", str(project)]) == 0
    lines = capsys.readouterr().out.splitlines()
    revision = "  revision: start from `autoform work impact`"
    opened = lines.index("statement: chapter/open [af_00000000000000000000000b] - Open")
    assert lines[opened + 1] == revision
    assert lines.count(revision) == 1

    for selector, flagged in (("chapter/open", True), ("chapter/prove", False)):
        assert cli.main(["work", "context", selector, str(project), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["item"]["revision"] is flagged
        assert cli.main(["work", "context", selector, str(project)]) == 0
        context = capsys.readouterr().out.splitlines()
        assert ("Revision: the statement was retracted; start from `autoform work impact`" in context) is flagged


def test_work_assumptions_reports_errors_on_stderr_with_exit_2(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli.main(["work", "assumptions", str(tmp_path / "missing")]) == 2
    assert capsys.readouterr().err == "error: project or blueprint directory does not exist\n"

    project = _policy_project(tmp_path, "maybe")
    assert cli.main(["work", "assumptions", str(project), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: roadmap:2: 'open_statements' accepts allowed or forbidden\n"

    for failure in (
        PermissionError(13, "Permission denied", "/private/secret/blueprint"),
        RuntimeError("Symlink loop from '/private/secret/blueprint'"),
    ):

        def unreadable(*_args, failure: Exception = failure, **_kwargs):
            raise failure

        monkeypatch.setattr(cli, "assumption_contract", unreadable)
        assert cli.main(["work", "assumptions", str(project)]) == 2
        assert capsys.readouterr().err == "error: project or blueprint path cannot be read\n"
