from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from autoform_cli.graph import Graph, Node, load_graph
from autoform_cli.runtime import (
    RUNTIME_AUTHORITY,
    RUNTIME_SCHEMA,
    RuntimeProjectionError,
    build_runtime_graph,
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


def test_preserves_hierarchy_typed_dependencies_and_dispatchability(tmp_path: Path) -> None:
    runtime = load_runtime_graph(_project(tmp_path))
    chapter = runtime.get("chapter")
    base = runtime.get("chapter/section/base")
    result = runtime.get("chapter/section/result")

    assert chapter is not None and not chapter.formalizable and not chapter.dispatchable
    assert base is not None and base.dispatchable
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


def test_rejects_nonportable_authored_file_paths(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = project / "blueprint" / "roadmap" / "chapter" / "section" / "result.md"
    text = result.read_text(encoding="utf-8").replace(
        "mathlib_file: Mathlib/Result.lean",
        r"mathlib_file: ..\outside.lean",
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
        "chapter/section/base: dependency does not name a runtime node: missing",
        "chapter/section/base: dependency union does not match typed dependencies",
    )
    assert str(tmp_path) not in str(error.value)
