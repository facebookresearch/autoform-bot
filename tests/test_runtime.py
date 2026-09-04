from __future__ import annotations

import hashlib
import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from autoform_cli import runtime as runtime_module
from autoform_cli.graph import Graph, GraphValidationError, Node, load_graph
from autoform_cli.runtime import (
    RUNTIME_AUTHORITY,
    RUNTIME_SCHEMA,
    RuntimeProjectionError,
    bind_runtime_paths,
    build_runtime_graph,
    load_bound_graph,
    load_runtime_graph,
    resolve_runtime_paths,
)


def _article(
    project: Path,
    relative: str,
    *,
    title: str | None = None,
    prose: str = "A precise mathematical article.",
    statement_dependencies: tuple[str, ...] = (),
    proof_dependencies: tuple[str, ...] = (),
    sources: tuple[str, ...] = (),
    **metadata: str,
) -> Path:
    path = project / "blueprint" / "roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    title = title or path.parent.name.title() if path.name.casefold() == "readme.md" else title or path.stem.title()
    lines = ["---", *(f"{key}: {value}" for key, value in metadata.items()), "---", "", f"# {title}", "", prose]
    if statement_dependencies:
        lines.extend(["", "## Depends on", "", *(f"- [dependency]({target})" for target in statement_dependencies)])
    if proof_dependencies:
        lines.extend(["", "## Proof depends on", "", *(f"- [dependency]({target})" for target in proof_dependencies)])
    if sources:
        lines.extend(["", "## Sources", "", *(f"- [source]({target})" for target in sources)])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    _article(project, "README.md", title="Roadmap")
    _article(project, "chapter/README.md", title="Chapter")
    _article(project, "chapter/section/README.md", title="Section")
    _article(
        project,
        "chapter/section/base.md",
        title="Base",
        article_id="af_0123456789abcdef01234567",
        declaration="definition",
        statement="formalized",
        lean="Project.base",
        origin="background",
    )
    _article(
        project,
        "chapter/section/result.md",
        title="Result",
        declaration="theorem",
        statement="formalized",
        proof="formalized",
        lean="Project.result Project.result_aux",
        mathlib="true",
        mathlib_declaration="Mathlib.Result, Mathlib.ResultAux",
        mathlib_file="Mathlib/Result.lean",
        origin="cited",
        statement_dependencies=("base.md",),
        proof_dependencies=("base.md",),
        sources=("https://example.invalid/paper",),
    )
    return project


def test_loads_identical_runtime_from_project_or_blueprint(tmp_path: Path) -> None:
    project = _project(tmp_path)

    from_project = load_runtime_graph(project)
    from_blueprint = load_runtime_graph(project / "blueprint")

    assert from_project == from_blueprint
    assert from_project.schema == RUNTIME_SCHEMA
    assert from_project.authority == RUNTIME_AUTHORITY
    assert from_project.blueprint_path == "blueprint"
    assert [node.id for node in from_project.nodes] == [
        "chapter",
        "chapter/section",
        "chapter/section/base",
        "chapter/section/result",
        "roadmap",
    ]
    assert from_project.article_count == 5
    assert from_project.formalizable_count == 2
    assert from_project.dispatchable_count == 2
    assert from_project.dependency_count == 1
    assert from_project.maximum_depth == 3


def test_bound_graph_rejects_a_b_a_project_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path / "selected")
    replacement = _project(tmp_path / "replacement")
    retained = tmp_path / "retained-project"
    original_load_graph = runtime_module.load_graph

    def load_replacement(*args, **kwargs):
        project.rename(retained)
        replacement.rename(project)
        try:
            return original_load_graph(*args, **kwargs)
        finally:
            project.rename(replacement)
            retained.rename(project)

    monkeypatch.setattr(runtime_module, "load_graph", load_replacement)

    with bind_runtime_paths(project) as paths:
        with pytest.raises(GraphValidationError, match="blueprint changed"):
            load_bound_graph(paths)
        paths.verify()


def test_preserves_hierarchy_typed_dependencies_and_dispatchability(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = load_runtime_graph(project)
    chapter = runtime.get("chapter")
    base = runtime.get("chapter/section/base")
    result = runtime.get("chapter/section/result")

    assert chapter is not None and not chapter.formalizable and not chapter.dispatchable
    assert base is not None and base.dispatchable
    assert base.article_id == "af_0123456789abcdef01234567"
    assert base.source_sha256 == hashlib.sha256(
        (project / base.article_path).read_bytes()
    ).hexdigest()
    assert base.status.state == "fully_proved"
    assert base.status.defined
    assert result is not None
    assert result.parent == "chapter/section"
    assert result.depth == 3
    assert result.statement_dependencies == ("chapter/section/base",)
    assert result.proof_dependencies == ("chapter/section/base",)
    assert result.dependencies == ("chapter/section/base",)
    assert result.assertions.statement_formalized
    assert result.assertions.proof_formalized
    assert not result.assertions.not_ready
    assert result.status.can_state
    assert result.status.can_prove
    assert result.status.proved
    assert result.status.fully_proved
    assert not result.status.defined
    assert result.dispatchable


def test_exposes_provenance_mathlib_and_optional_lean_locations(tmp_path: Path) -> None:
    project = _project(tmp_path)
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "Project.lean").write_text(
        "def Project.base : Nat := 1\n"
        "theorem Project.result : True := trivial\n"
        "lemma Project.result_aux : True := trivial\n",
        encoding="utf-8",
    )

    runtime = load_runtime_graph(project, lean_root=lean_root)
    result = runtime.get("chapter/section/result")

    assert result is not None
    assert result.origin == "cited"
    assert result.source_targets == ("https://example.invalid/paper",)
    assert [(target.declaration, target.source_file) for target in result.lean_targets] == [
        ("Project.result", "Project.lean"),
        ("Project.result_aux", "Project.lean"),
    ]
    assert result.mathlib
    assert result.mathlib_declarations == ("Mathlib.Result", "Mathlib.ResultAux")
    assert result.mathlib_file == "Mathlib/Result.lean"


def test_translates_unsafe_lean_source_failures(tmp_path: Path) -> None:
    project = _project(tmp_path)
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    outside = tmp_path / "Outside.lean"
    outside.write_text("def escaped : Nat := 0\n", encoding="utf-8")
    try:
        (lean_root / "Linked.lean").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(RuntimeProjectionError, match="cannot be indexed safely"):
        load_runtime_graph(project, lean_root=lean_root)


def test_rejects_source_target_through_symlink_outside_blueprint(tmp_path: Path) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "blueprint/sources").symlink_to(outside, target_is_directory=True)
    article = project / "blueprint/roadmap/chapter/section/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "https://example.invalid/paper",
            "../../../sources/paper.md",
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeProjectionError, match="source target escapes"):
        load_runtime_graph(project)


@pytest.mark.parametrize("direct_builder", [False, True])
def test_rejects_a_source_symlink_component_cancelled_by_parent_navigation(
    tmp_path: Path,
    direct_builder: bool,
) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside"
    target_directory = outside / "directory"
    target_directory.mkdir(parents=True)
    (outside / "secret.md").write_text("outside\n", encoding="utf-8")
    sources = project / "blueprint/sources"
    sources.mkdir()
    try:
        (sources / "link").symlink_to(target_directory, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    article = project / "blueprint/roadmap/chapter/section/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "https://example.invalid/paper",
            "../../../sources/link/../secret.md",
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeProjectionError, match="source target escapes"):
        if direct_builder:
            build_runtime_graph(load_graph(project / "blueprint"), project_root=project)
        else:
            load_runtime_graph(project)


@pytest.mark.parametrize("direct_builder", [False, True])
def test_malformed_source_url_is_reported_as_runtime_error(
    tmp_path: Path,
    direct_builder: bool,
) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/chapter/section/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "https://example.invalid/paper",
            "http://[",
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeProjectionError, match="unsupported location"):
        if direct_builder:
            build_runtime_graph(load_graph(project / "blueprint"), project_root=project)
        else:
            load_runtime_graph(project)


def test_direct_builder_translates_invalid_lean_root(tmp_path: Path) -> None:
    project = _project(tmp_path)

    with pytest.raises(RuntimeProjectionError, match="cannot be indexed safely"):
        build_runtime_graph(
            load_graph(project / "blueprint"),
            project_root=project,
            lean_root="invalid\x00lean-root",
        )


def test_serialization_is_deterministic_relative_and_deeply_immutable(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runtime = load_runtime_graph(project)
    payload = runtime.as_dict()

    assert json.loads(runtime.to_json()) == payload
    assert runtime.to_json() == load_runtime_graph(project).to_json()
    assert str(tmp_path) not in runtime.to_json()
    assert all(not Path(node.article_path).is_absolute() for node in runtime.nodes)
    assert isinstance(runtime.nodes, tuple)
    assert isinstance(runtime.nodes[0].dependencies, tuple)
    with pytest.raises(FrozenInstanceError):
        runtime.schema = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        runtime.nodes[0].title = "changed"  # type: ignore[misc]
    payload["nodes"][0]["title"] = "changed"  # type: ignore[index]
    assert runtime.nodes[0].title != "changed"


def test_revision_tracks_exact_articles_and_is_location_independent(tmp_path: Path) -> None:
    first_project = _project(tmp_path / "first")
    second_project = _project(tmp_path / "second")
    first = load_runtime_graph(first_project)
    second = load_runtime_graph(second_project)

    assert first.source_revision == second.source_revision
    article = first_project / "blueprint" / "roadmap" / "chapter" / "section" / "result.md"
    article.write_text(article.read_text(encoding="utf-8") + "\nMore exposition.\n", encoding="utf-8")

    changed = load_runtime_graph(first_project)
    assert changed.source_revision != first.source_revision


def test_loading_is_read_only_and_never_creates_graph_json(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}

    load_runtime_graph(project)

    after = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}
    assert after == before
    assert not (project / "graph.json").exists()
    assert not (project / "blueprint" / "graph.json").exists()


def test_rejects_symlinked_roadmap_content_and_ambiguous_input(tmp_path: Path) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    symlink = project / "blueprint" / "roadmap" / "linked.md"
    try:
        symlink.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(RuntimeProjectionError, match="symbolic link") as error:
        load_runtime_graph(project)
    assert str(tmp_path) not in str(error.value)

    ambiguous = tmp_path / "ambiguous"
    (ambiguous / "roadmap").mkdir(parents=True)
    (ambiguous / "blueprint" / "roadmap").mkdir(parents=True)
    with pytest.raises(RuntimeProjectionError, match="ambiguous"):
        resolve_runtime_paths(ambiguous)


def test_runtime_preflight_does_not_traverse_reparse_point_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import runtime as runtime_module

    project = _project(tmp_path)
    junction = project / "blueprint/roadmap/junction"
    junction.mkdir()
    _article(project, "junction/hidden.md", title="Hidden")
    original = runtime_module._path_is_reparse_point

    monkeypatch.setattr(
        runtime_module,
        "_path_is_reparse_point",
        lambda path, metadata: path == junction or original(path, metadata),
    )

    with pytest.raises(RuntimeProjectionError, match="reparse point"):
        resolve_runtime_paths(project)


def test_bound_graph_prefers_generation_change_over_stale_parse_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path / "selected")
    replacement_project = _project(tmp_path / "replacement")
    blueprint = project / "blueprint"
    held = project / "held-blueprint"
    replacement = replacement_project / "blueprint"

    def replace_then_fail(*_args, **_kwargs):
        blueprint.rename(held)
        replacement.rename(blueprint)
        raise GraphValidationError(["stale generation error"])

    monkeypatch.setattr(runtime_module, "load_graph", replace_then_fail)

    with bind_runtime_paths(project) as paths:
        with pytest.raises(RuntimeProjectionError, match="blueprint directory changed"):
            load_bound_graph(paths)


def test_allows_confined_parent_relative_sources_and_rejects_escapes(tmp_path: Path) -> None:
    project = _project(tmp_path)
    source = project / "blueprint" / "sources" / "paper.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Paper\n", encoding="utf-8")
    result = project / "blueprint" / "roadmap" / "chapter" / "section" / "result.md"
    text = result.read_text(encoding="utf-8").replace(
        "https://example.invalid/paper",
        "../../../sources/paper.md",
    )
    result.write_text(text, encoding="utf-8")

    runtime = load_runtime_graph(project)
    assert runtime.get("chapter/section/result").source_targets == ("../../../sources/paper.md",)  # type: ignore[union-attr]

    result.write_text(text.replace("../../../sources/paper.md", "../../../../outside.md"), encoding="utf-8")
    with pytest.raises(RuntimeProjectionError, match="source target escapes") as error:
        load_runtime_graph(project)
    assert str(tmp_path) not in str(error.value)


def test_rejects_percent_encoded_windows_drive_source_target(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = project / "blueprint/roadmap/chapter/section/result.md"
    result.write_text(
        result.read_text(encoding="utf-8").replace(
            "https://example.invalid/paper",
            "C%3Aoutside.md",
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeProjectionError, match="source target escapes"):
        load_runtime_graph(project)


def test_portable_runtime_validates_local_source_targets_without_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import graph as graph_module
    from autoform_cli import workspace as workspace_module

    project = _project(tmp_path)
    source = project / "blueprint/sources/paper.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Paper\n", encoding="utf-8")
    result = project / "blueprint/roadmap/chapter/section/result.md"
    text = result.read_text(encoding="utf-8").replace(
        "https://example.invalid/paper",
        "../../../sources/paper.md",
    )
    result.write_text(text, encoding="utf-8")
    monkeypatch.setattr(workspace_module, "_DIRECTORY_BINDING_SUPPORTED", False)
    monkeypatch.setattr(graph_module, "_DIRECTORY_BINDING_SUPPORTED", False)

    runtime = load_runtime_graph(project)
    assert runtime.get("chapter/section/result").source_targets == (
        "../../../sources/paper.md",
    )

    original = runtime_module._path_is_reparse_point

    def mark_sources_as_reparse(path: Path, metadata: os.stat_result) -> bool:
        return path.name == "sources" or original(path, metadata)

    monkeypatch.setattr(runtime_module, "_path_is_reparse_point", mark_sources_as_reparse)
    with pytest.raises(RuntimeProjectionError, match="symbolic link or reparse point"):
        load_runtime_graph(project)


@pytest.mark.parametrize("authored_path", [r"..\outside.lean", "C:outside.lean"])
def test_rejects_nonportable_authored_file_paths(
    tmp_path: Path,
    authored_path: str,
) -> None:
    project = _project(tmp_path)
    result = project / "blueprint" / "roadmap" / "chapter" / "section" / "result.md"
    text = result.read_text(encoding="utf-8").replace(
        "mathlib_file: Mathlib/Result.lean",
        f"mathlib_file: {authored_path}",
    )
    result.write_text(text, encoding="utf-8")

    with pytest.raises(RuntimeProjectionError, match="mathlib file must be a portable relative path") as error:
        load_runtime_graph(project)

    assert str(tmp_path) not in str(error.value)


def test_adapter_rejects_inconsistent_hand_built_graph_without_host_paths(tmp_path: Path) -> None:
    project = _project(tmp_path)
    canonical = load_graph(project / "blueprint")
    base = canonical.nodes["chapter/section/base"]
    invalid = Node(
        id=base.id,
        title=base.title,
        path=base.path,
        dependencies=("missing",),
        statement_dependencies=(),
        proof_dependencies=(),
        declaration=base.declaration,
    )
    graph = Graph(canonical.blueprint_dir, {invalid.id: invalid})

    with pytest.raises(RuntimeProjectionError) as error:
        build_runtime_graph(graph, project_root=project)

    assert error.value.issues == (
        "chapter/section/base: article source digest is unavailable",
        "chapter/section/base: dependency does not name a runtime node: missing",
        "chapter/section/base: dependency union does not match typed dependencies",
    )
    assert str(tmp_path) not in str(error.value)


def test_adapter_rejects_hand_built_article_symlink_outside_blueprint(tmp_path: Path) -> None:
    project = tmp_path / "project"
    roadmap = project / "blueprint/roadmap"
    roadmap.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    content = b"# Outside\n"
    outside.write_bytes(content)
    article = roadmap / "evil.md"
    article.symlink_to(outside)
    node = Node(
        id="evil",
        title="Evil",
        path=article,
        dependencies=(),
        source_sha256=hashlib.sha256(content).hexdigest(),
    )

    with pytest.raises(RuntimeProjectionError, match="symbolic link"):
        build_runtime_graph(Graph(project / "blueprint", {node.id: node}), project_root=project)
