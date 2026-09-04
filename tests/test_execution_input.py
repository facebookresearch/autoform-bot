from __future__ import annotations

import hashlib
import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath

import pytest

import autoform_cli.execution_input as execution_module
from autoform_cli import graph as graph_module
from autoform_cli import workspace as workspace_module
from autoform_cli.execution_input import (
    EXECUTION_INPUT_SCHEMA,
    ExecutionInputError,
    load_execution_input,
)
from autoform_cli.runtime import (
    RUNTIME_SCHEMA,
    RuntimeProjectionError,
    bind_runtime_paths,
    load_runtime_graph,
)
from autoform_cli.workspace_mutation import initialize_workspace, register_blueprint_project


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _project(tmp_path: Path, *, v2: bool = True) -> Path:
    project = tmp_path / "project"
    blueprint = project / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / "README.md").write_text(
        "---\narticle_id: af_000000000000000000000000\n---\n\n# Roadmap\n",
        encoding="utf-8",
    )
    (roadmap / "result.md").write_text(
        "---\n"
        "article_id: af_111111111111111111111111\n"
        "declaration: theorem\n"
        "source_units: [result]\n"
        "---\n\n"
        "# Result\n\nA precise result.\n",
        encoding="utf-8",
    )
    coverage = blueprint / "coverage" / "README.md"
    coverage.parent.mkdir(parents=True)
    if not v2:
        coverage.write_text(
            "# Coverage\n\n"
            "| Area | Coverage | Evidence |\n"
            "| --- | --- | --- |\n"
            "| Result | OUT | Explicitly outside scope |\n",
            encoding="utf-8",
        )
        return project
    artifact = b"The source result.\n"
    source = blueprint / "sources" / "book.md"
    source.parent.mkdir()
    source.write_bytes(artifact)
    coverage.write_text(
        "---\n"
        "schema: autoform-coverage/v2\n"
        "artifact: sources/book.md\n"
        f"artifact_sha256: {_digest(artifact)}\n"
        "---\n\n"
        "# Coverage\n\n"
        "| Unit | Area | Lines | Locator | Unit SHA-256 | Coverage | Evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"| result | Main result | 1-1 | Theorem 1 | {_digest(artifact)} | "
        "DECOMPOSED | [Result](../roadmap/result.md) |\n",
        encoding="utf-8",
    )
    return project


def test_builds_deterministic_deeply_immutable_execution_input(tmp_path: Path) -> None:
    project = _project(tmp_path)

    first = load_execution_input(project)
    second = load_execution_input(project)

    assert first == second
    assert first.schema == EXECUTION_INPUT_SCHEMA
    assert first.runtime.schema == RUNTIME_SCHEMA
    assert first.coverage_schema == "autoform-coverage/v2"
    assert len(first.authority_sha256) == 64
    assert first.lean_source_revision is None
    assert len(first.source_contract_sha256) == 64
    assert first.artifact_path == "sources/book.md"
    assert first.units[0].roadmap_nodes == ("result",)
    assert json.loads(first.to_json()) == first.as_dict()
    assert first.sha256 == second.sha256
    assert str(tmp_path) not in first.to_json()
    with pytest.raises(FrozenInstanceError):
        first.artifact_path = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        first.units[0].unit = "changed"  # type: ignore[misc]


def test_unrelated_blueprint_files_do_not_change_execution_authority_generation(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    with bind_runtime_paths(project) as paths:
        before = execution_module._capture_execution_authority(paths)
        (project / "blueprint/sources/unrelated.txt").write_text(
            "ignored\n", encoding="utf-8"
        )
        after = execution_module._capture_execution_authority(paths)

    assert after.generation_revision == before.generation_revision


def test_many_source_targets_list_each_shared_directory_once_per_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    sources = project / "blueprint/sources"
    targets = []
    for index in range(100):
        name = f"reference-{index:03d}.md"
        (sources / name).write_text(f"reference {index}\n", encoding="utf-8")
        targets.append(PurePosixPath("sources", name))
    list_calls = 0
    original = execution_module.os.listdir

    def count_lists(path):
        nonlocal list_calls
        list_calls += 1
        return original(path)

    monkeypatch.setattr(execution_module.os, "listdir", count_lists)

    with bind_runtime_paths(project) as paths:
        selected = execution_module._bind_existing_source_target_paths(
            paths,
            tuple(targets),
        )

    assert len(selected) == 101
    assert list_calls == 4


def test_local_source_target_symlink_is_part_of_execution_authority(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    (project / "blueprint/sources/other.md").symlink_to(outside)
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8")
        + "\n## Sources\n\n- [Other](../sources/other.md)\n",
        encoding="utf-8",
    )

    with pytest.raises(ExecutionInputError, match="execution-authority-unsafe"):
        load_execution_input(project)


def test_cancelled_source_symlink_is_part_of_execution_authority(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside"
    target_directory = outside / "directory"
    target_directory.mkdir(parents=True)
    (outside / "secret.md").write_text("outside\n", encoding="utf-8")
    link = project / "blueprint/sources/link"
    try:
        link.symlink_to(target_directory, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8")
        + "\n## Sources\n\n- [Escaped](../sources/link/../secret.md)\n",
        encoding="utf-8",
    )

    with pytest.raises(ExecutionInputError, match="execution-authority-unsafe"):
        load_execution_input(project)


def test_case_alias_of_local_source_target_is_bound_by_filesystem_identity(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    if not (project / "blueprint/SOURCES").exists():
        pytest.skip("filesystem is case-sensitive")
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    (project / "blueprint/sources/other.md").symlink_to(outside)
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8")
        + "\n## Sources\n\n- [Other](../SOURCES/other.md)\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeProjectionError, match="source target escapes"):
        load_runtime_graph(project)
    with pytest.raises(ExecutionInputError, match="execution-authority-unsafe"):
        load_execution_input(project)


def test_case_alias_of_coverage_artifact_is_bound_by_filesystem_identity(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    sources = blueprint / "sources"
    physical_sources = blueprint / "SOURCES"
    sources.rename(physical_sources)
    if not sources.exists():
        pytest.skip("filesystem is case-sensitive")
    physical_book = physical_sources / "BOOK.md"
    (physical_sources / "book.md").rename(physical_book)

    execution_input = load_execution_input(project)

    assert execution_input.artifact_path == "sources/book.md"
    assert execution_input.artifact_sha256 == _digest(physical_book.read_bytes())


@pytest.mark.parametrize("root_name", ["roadmap", "coverage"])
def test_case_alias_of_contract_root_is_bound_by_filesystem_identity(
    tmp_path: Path,
    root_name: str,
) -> None:
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    authored = blueprint / root_name
    physical = blueprint / root_name.upper()
    authored.rename(physical)
    if not authored.exists():
        pytest.skip("filesystem is case-sensitive")

    execution_input = load_execution_input(project)

    assert execution_input.runtime.article_count == 2
    assert execution_input.coverage_schema == "autoform-coverage/v2"


@pytest.mark.parametrize("relative", ["roadmap/.notes.md", "roadmap/.hidden/note.md"])
def test_hidden_roadmap_markdown_does_not_change_execution_authority(
    tmp_path: Path,
    relative: str,
) -> None:
    project = _project(tmp_path)
    hidden = project / "blueprint" / relative
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text("# Private notes\n", encoding="utf-8")

    direct = load_runtime_graph(project)
    execution = load_execution_input(project)

    assert execution.runtime == direct


def test_execution_input_binds_only_the_selected_workspace_project(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    initialize_workspace(project, blueprint_root="Plans")
    (project / "blueprint").rename(project / "Plans/One")
    register_blueprint_project(project, project_id="one", title="One", path="One")

    first = load_execution_input(project, project_id="one")
    assert first.workspace_project_id == "one"
    assert first.workspace_project_binding_sha256 is not None
    assert first.runtime.blueprint_path == "Plans/One"
    assert first.as_dict()["workspace"] == {
        "blueprint_path": "Plans/One",
        "project_binding_sha256": first.workspace_project_binding_sha256,
        "project_id": "one",
    }

    manifest = project / ".autoform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[locations.notes]\npath = "Notes"\nprovides = ["lean-source"]\n',
        encoding="utf-8",
    )
    (project / "Plans/Two/roadmap").mkdir(parents=True)
    register_blueprint_project(project, project_id="two", title="Two", path="Two")
    second = load_execution_input(project, project_id="one")
    assert (
        second.workspace_project_binding_sha256
        == first.workspace_project_binding_sha256
    )
    assert second.sha256 == first.sha256


def test_execution_input_retries_a_manifest_runtime_aba_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    initialize_workspace(project, blueprint_root="Plans")
    (project / "blueprint").rename(project / "Plans/One")
    register_blueprint_project(project, project_id="one", title="One", path="One")
    other = project / "Plans/Two"
    (other / "roadmap").mkdir(parents=True)
    (other / "roadmap/README.md").write_text("# Other\n", encoding="utf-8")
    original = execution_module.load_runtime_graph
    calls = 0

    def wrong_runtime_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(other)
        return original(*args, **kwargs)

    monkeypatch.setattr(execution_module, "load_runtime_graph", wrong_runtime_once)

    result = load_execution_input(project, project_id="one")

    assert calls >= 2
    assert result.runtime.blueprint_path == "Plans/One"


def test_execution_input_rejects_a_symlinked_path_component(tmp_path: Path) -> None:
    project = _project(tmp_path)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(project.parent, target_is_directory=True)

    with pytest.raises(ExecutionInputError, match="resolved safely"):
        load_execution_input(linked_parent / project.name)


def test_rejected_unregistered_workspace_vault_closes_all_bindings(tmp_path: Path) -> None:
    descriptors = Path("/proc/self/fd")
    if not descriptors.is_dir():
        descriptors = Path("/dev/fd")
    if not descriptors.is_dir():
        pytest.skip("the platform does not expose process file descriptors")
    project = _project(tmp_path)
    initialize_workspace(project, blueprint_root="Plans")
    retained_errors = []
    before = len(tuple(descriptors.iterdir()))

    for _ in range(20):
        try:
            load_execution_input(project / "blueprint")
        except ExecutionInputError as error:
            retained_errors.append(error)

    assert len(retained_errors) == 20
    assert len(tuple(descriptors.iterdir())) == before


def test_legacy_project_snapshot_explicitly_uses_workspace_aware_v3(tmp_path: Path) -> None:
    snapshot = load_execution_input(_project(tmp_path))

    assert snapshot.as_dict()["workspace"] == {
        "blueprint_path": "blueprint",
        "project_binding_sha256": None,
        "project_id": None,
    }
    legacy_payload = snapshot.as_dict()
    legacy_payload["schema"] = "autoform-execution-input/v2"
    legacy_payload.pop("workspace")
    legacy_sha256 = _digest(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    assert snapshot.sha256 != legacy_sha256


def test_runtime_v1_shape_does_not_gain_coverage_fields(tmp_path: Path) -> None:
    runtime = load_runtime_graph(_project(tmp_path))
    node = runtime.as_dict()["nodes"][1]

    assert "source_units" not in node
    assert set(runtime.as_dict()) == {
        "article_count",
        "authority",
        "blueprint_path",
        "dependency_count",
        "dispatchable_count",
        "formalizable_count",
        "maximum_depth",
        "nodes",
        "schema",
        "source_revision",
    }


def test_v1_coverage_is_explicitly_refused_for_execution(tmp_path: Path) -> None:
    project = _project(tmp_path, v2=False)

    with pytest.raises(ExecutionInputError) as raised:
        load_execution_input(project)

    assert [issue.code for issue in raised.value.issues] == ["coverage-v2-required"]


def test_mapped_v2_source_unit_is_refused_for_execution(tmp_path: Path) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace("source_units: [result]\n", ""),
        encoding="utf-8",
    )
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        coverage.read_text(encoding="utf-8").replace(
            "DECOMPOSED | [Result](../roadmap/result.md) |",
            "MAPPED | Roadmap decomposition pending |",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExecutionInputError) as raised:
        load_execution_input(project)

    assert [issue.code for issue in raised.value.issues] == ["coverage-incomplete"]
    assert raised.value.issues[0].reason.endswith("1 unit remains MAPPED")


@pytest.mark.parametrize(
    ("disposition", "evidence"),
    [
        ("DEFERRED", "Deferred to the second formalization milestone"),
        ("OUT", "Excluded from the declared formalization scope"),
    ],
)
def test_terminal_nonroadmap_v2_source_unit_is_accepted_for_execution(
    tmp_path: Path,
    disposition: str,
    evidence: str,
) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace("source_units: [result]\n", ""),
        encoding="utf-8",
    )
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        coverage.read_text(encoding="utf-8").replace(
            "DECOMPOSED | [Result](../roadmap/result.md) |",
            f"{disposition} | {evidence} |",
        ),
        encoding="utf-8",
    )

    execution_input = load_execution_input(project)

    assert execution_input.units[0].disposition == disposition
    assert execution_input.units[0].roadmap_nodes == ()


def test_missing_durable_article_id_is_refused_for_execution(tmp_path: Path) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "article_id: af_111111111111111111111111\n",
            "",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExecutionInputError) as raised:
        load_execution_input(project)

    assert [issue.code for issue in raised.value.issues] == ["article-id-required"]
    assert "result" in raised.value.issues[0].reason


def test_invalid_roadmap_is_reported_through_execution_input_error(tmp_path: Path) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/result.md"
    article.write_text(article.read_text(encoding="utf-8") + "\n# Second title\n", encoding="utf-8")

    with pytest.raises(ExecutionInputError) as raised:
        load_execution_input(project)

    assert [issue.code for issue in raised.value.issues] == ["runtime-invalid"]


def test_autonomous_input_rejects_portable_read_only_filesystem_tier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(workspace_module, "_DIRECTORY_BINDING_SUPPORTED", False)
    monkeypatch.setattr(graph_module, "_DIRECTORY_BINDING_SUPPORTED", False)

    with pytest.raises(ExecutionInputError) as raised:
        load_execution_input(project)

    assert [issue.code for issue in raised.value.issues] == ["strong-binding-required"]


def test_concurrent_authority_change_is_not_snapshotted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/result.md"
    original = execution_module.load_coverage
    calls = 0

    def mutate_after_first(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            article.write_text(article.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")
        return result

    monkeypatch.setattr(execution_module, "load_coverage", mutate_after_first)

    snapshot = load_execution_input(project)

    assert calls >= 2
    assert snapshot.runtime.source_revision == load_runtime_graph(project).source_revision


def test_runtime_aba_hidden_inside_loader_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/result.md"
    stable = article.read_bytes()
    transient = stable + b"\nA transient generation that must not escape.\n"
    original = execution_module.load_runtime_graph
    calls = 0

    def load_transient_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls != 1:
            return original(*args, **kwargs)
        article.write_bytes(transient)
        try:
            return original(*args, **kwargs)
        finally:
            article.write_bytes(stable)

    monkeypatch.setattr(execution_module, "load_runtime_graph", load_transient_once)

    snapshot = load_execution_input(project)

    assert calls == 2
    assert snapshot.runtime.source_revision == load_runtime_graph(project).source_revision


def test_coverage_roadmap_aba_hidden_inside_loader_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/result.md"
    stable = article.read_bytes()
    transient = stable + b"\nA transient coverage-binding generation.\n"
    original = execution_module.load_coverage
    calls = 0

    def load_transient_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls != 1:
            return original(*args, **kwargs)
        article.write_bytes(transient)
        try:
            return original(*args, **kwargs)
        finally:
            article.write_bytes(stable)

    monkeypatch.setattr(execution_module, "load_coverage", load_transient_once)

    snapshot = load_execution_input(project)

    assert calls == 2
    assert snapshot.runtime.source_revision == load_runtime_graph(project).source_revision


def test_lean_aba_hidden_inside_runtime_loader_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "declaration: theorem\n",
            "declaration: theorem\nlean: Project.result\n",
        ),
        encoding="utf-8",
    )
    stable = project / "Project/Stable.lean"
    transient = project / "Project/Transient.lean"
    stable.parent.mkdir()
    stable.write_text("theorem Project.result : True := by trivial\n", encoding="utf-8")
    original = execution_module.load_runtime_graph
    calls = 0

    def load_transient_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls != 1:
            return original(*args, **kwargs)
        stable.rename(transient)
        try:
            return original(*args, **kwargs)
        finally:
            transient.rename(stable)

    monkeypatch.setattr(execution_module, "load_runtime_graph", load_transient_once)

    snapshot = load_execution_input(project, lean_root=project)

    assert calls == 2
    result = snapshot.runtime.get("result")
    assert result is not None
    assert result.lean_targets[0].source_file == "Project/Stable.lean"


def test_lean_source_bytes_are_bound_into_execution_input(tmp_path: Path) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "declaration: theorem\n",
            "declaration: theorem\nlean: Project.result\n",
        ),
        encoding="utf-8",
    )
    source = project / "Project.lean"
    source.write_text("theorem Project.result : True := by trivial\n", encoding="utf-8")

    before = load_execution_input(project, lean_root=project)
    source.write_text("theorem Project.result : True := by\n  trivial\n", encoding="utf-8")
    after = load_execution_input(project, lean_root=project)

    assert before.lean_source_revision != after.lean_source_revision
    assert before.authority_sha256 != after.authority_sha256
    assert before.sha256 != after.sha256
    assert before.source_contract_sha256 == after.source_contract_sha256


def test_execution_input_rejects_symlinked_lean_source(tmp_path: Path) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "declaration: theorem\n",
            "declaration: theorem\nlean: Project.result\n",
        ),
        encoding="utf-8",
    )
    outside = tmp_path / "outside.lean"
    outside.write_text("theorem Project.result : True := by trivial\n", encoding="utf-8")
    (project / "Project.lean").symlink_to(outside)

    with pytest.raises(ExecutionInputError) as raised:
        load_execution_input(project, lean_root=project)

    assert [issue.code for issue in raised.value.issues] == ["lean-source-unsafe"]
    assert "Project.lean" in raised.value.issues[0].reason


def test_execution_input_rejects_special_lean_source_without_opening_it(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable")
    project = _project(tmp_path)
    trap = project / "Trap.lean"
    os.mkfifo(trap)
    try:
        with pytest.raises(ExecutionInputError) as raised:
            load_execution_input(project, lean_root=project)
    finally:
        trap.unlink(missing_ok=True)

    assert [issue.code for issue in raised.value.issues] == ["lean-source-unsafe"]
    assert "Trap.lean" in raised.value.issues[0].reason


def test_execution_input_rejects_lean_root_redirected_before_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "Stable.lean").write_text("theorem stable : True := by trivial\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    held = tmp_path / "held-lean"
    original = execution_module.open_project_sources

    def redirect_before_open(root: str | Path, **kwargs):
        lean_root.rename(held)
        lean_root.symlink_to(outside, target_is_directory=True)
        try:
            return original(root, **kwargs)
        finally:
            lean_root.unlink()
            held.rename(lean_root)

    monkeypatch.setattr(execution_module, "open_project_sources", redirect_before_open)

    with pytest.raises(ExecutionInputError) as raised:
        load_execution_input(project, lean_root=lean_root)

    assert [issue.code for issue in raised.value.issues] == ["lean-source-unsafe"]


def test_execution_input_translates_private_snapshot_materialization_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)

    def fail_materialization(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(execution_module.TreeSnapshot, "materialize", fail_materialization)

    with pytest.raises(ExecutionInputError) as raised:
        load_execution_input(project)

    assert [issue.code for issue in raised.value.issues] == [
        "execution-snapshot-unavailable"
    ]


def _mutate_roadmap(project: Path) -> None:
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8") + "\nA concurrently revised explanation.\n",
        encoding="utf-8",
    )


def _mutate_coverage_source(project: Path) -> bytes:
    source = project / "blueprint/sources/book.md"
    previous = source.read_bytes()
    replacement = b"The concurrently revised source result.\n"
    source.write_bytes(replacement)
    contract = project / "blueprint/coverage/README.md"
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            _digest(previous), _digest(replacement)
        ),
        encoding="utf-8",
    )
    return replacement


@pytest.mark.parametrize("authority", ["runtime", "coverage"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_execution_input_retries_mutations_at_every_authority_read_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
    timing: str,
) -> None:
    project = _project(tmp_path)
    attribute = "load_runtime_graph" if authority == "runtime" else "load_coverage"
    original = getattr(execution_module, attribute)
    mutated = False
    calls = 0
    replacement: bytes | None = None

    def mutate_once(*args, **kwargs):
        nonlocal calls, mutated, replacement
        calls += 1
        if not mutated and timing == "before":
            replacement = (
                _mutate_coverage_source(project)
                if authority == "coverage"
                else (_mutate_roadmap(project) or None)
            )
            mutated = True
        result = original(*args, **kwargs)
        if not mutated and timing == "after":
            replacement = (
                _mutate_coverage_source(project)
                if authority == "coverage"
                else (_mutate_roadmap(project) or None)
            )
            mutated = True
        return result

    monkeypatch.setattr(execution_module, attribute, mutate_once)

    snapshot = load_execution_input(project)

    assert calls >= 2
    assert snapshot.runtime.source_revision == load_runtime_graph(project).source_revision
    if replacement is not None:
        assert snapshot.artifact_sha256 == _digest(replacement)


def test_execution_input_fails_closed_after_bounded_continuous_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    original = execution_module.load_runtime_graph
    calls = 0

    def mutate_before_every_read(*args, **kwargs):
        nonlocal calls
        calls += 1
        _mutate_roadmap(project)
        return original(*args, **kwargs)

    monkeypatch.setattr(execution_module, "load_runtime_graph", mutate_before_every_read)

    with pytest.raises(ExecutionInputError) as raised:
        load_execution_input(project)

    assert calls == execution_module._EXECUTION_INPUT_READ_ATTEMPTS
    assert [issue.code for issue in raised.value.issues] == ["execution-input-changed"]


def test_execution_input_retries_a_transient_lean_capture_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    (project / "Main.lean").write_text("theorem result : True := trivial\n", encoding="utf-8")
    original = execution_module.BoundProjectSources.capture
    calls = 0

    def fail_once(sources):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise execution_module.TreeSnapshotError(
                "directory tree changed while it was captured"
            )
        return original(sources)

    monkeypatch.setattr(execution_module.BoundProjectSources, "capture", fail_once)

    snapshot = load_execution_input(project, lean_root=project)

    assert calls > 1
    assert snapshot.lean_source_revision is not None


@pytest.mark.parametrize(
    "failure",
    [RuntimeProjectionError(["transient projection failure"]), OSError("transient read failure")],
)
def test_execution_input_retries_final_path_projection_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    project = _project(tmp_path)
    original = execution_module.resolve_runtime_paths
    calls = 0

    def fail_first_final_read(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure
        return original(*args, **kwargs)

    monkeypatch.setattr(execution_module, "resolve_runtime_paths", fail_first_final_read)

    snapshot = load_execution_input(project)

    assert calls == 4
    assert snapshot.runtime.blueprint_path == "blueprint"
