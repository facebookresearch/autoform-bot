from __future__ import annotations

import json
import pickle
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from autoform_cli.graph import Graph, Node, load_graph
from autoform_cli.lean import LeanSourceError
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
        article_id="af_000000000000000000000001",
        declaration="definition",
        statement="formalized",
        lean="Project.base",
        origin="background",
    )
    _article(
        project,
        "chapter/section/result.md",
        title="Result",
        article_id="af_000000000000000000000002",
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

    assert RUNTIME_SCHEMA == "autoform-runtime/v4"
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
    assert from_project.nodes[0].as_dict()["catalog"] is None


def test_preserves_hierarchy_typed_dependencies_and_dispatchability(tmp_path: Path) -> None:
    runtime = load_runtime_graph(_project(tmp_path))
    chapter = runtime.get("chapter")
    base = runtime.get("chapter/section/base")
    result = runtime.get("chapter/section/result")

    assert chapter is not None and not chapter.formalizable and not chapter.dispatchable
    assert base is not None and base.dispatchable
    assert base.article_id == "af_000000000000000000000001"
    assert len(base.source_sha256) == 64
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


def test_runtime_exposes_a_settled_module_catalog_without_dispatching_it(tmp_path: Path) -> None:
    project = _project(tmp_path)
    ledger = project / "blueprint/sources/catalog.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("# Catalog declarations\n", encoding="utf-8")
    _article(
        project,
        "chapter/catalog.md",
        title="Existing module",
        catalog="module",
        lean="Project.base",
        statement="formalized",
        proof="formalized",
        sources=("../../sources/catalog.md",),
    )

    catalog = load_runtime_graph(project).get("chapter/catalog")

    assert catalog is not None
    assert catalog.catalog == "module"
    assert catalog.as_dict()["catalog"] == "module"
    assert not catalog.formalizable
    assert not catalog.dispatchable
    assert catalog.status.state == "fully_proved"


def test_runtime_node_loads_the_previous_v3_pickle_shape(tmp_path: Path) -> None:
    node = load_runtime_graph(_project(tmp_path)).nodes[0]
    previous_state = node.__getstate__()[:-1]
    restored = object.__new__(type(node))

    restored.__setstate__(previous_state)

    assert restored == node
    assert restored.catalog is None


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_runtime_node_pickle_round_trip(tmp_path: Path, protocol: int) -> None:
    node = replace(load_runtime_graph(_project(tmp_path)).nodes[0], catalog="module")

    restored = pickle.loads(pickle.dumps(node, protocol=protocol))

    assert restored == node
    assert restored.catalog == "module"


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
    base = next(node for node in payload["nodes"] if node["id"] == "chapter/section/base")
    assert base["article_id"] == "af_000000000000000000000001"
    assert base["source_sha256"] == runtime.get("chapter/section/base").source_sha256
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


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        (None, "Lean sources could not be indexed"),
        (
            "permission denied: Project/Secret.lean",
            "Lean sources could not be indexed: permission denied: Project/Secret.lean",
        ),
    ],
)
def test_runtime_translates_source_index_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str | None, message: str
) -> None:
    project = _project(tmp_path)

    def fail_index(root: Path, **_kwargs):
        if reason is not None:
            raise LeanSourceError(reason)
        raise OSError(f"private host detail: {root}")

    monkeypatch.setattr("autoform_cli.runtime.index_project", fail_index)

    with pytest.raises(RuntimeProjectionError) as error:
        load_runtime_graph(project, lean_root=project)

    assert error.value.issues == (message,)
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


def test_adapter_rejects_a_hand_built_dispatchable_catalog(tmp_path: Path) -> None:
    project = _project(tmp_path)
    canonical = load_graph(project / "blueprint")
    nodes = dict(canonical.nodes)
    base = nodes["chapter/section/base"]
    nodes[base.id] = replace(base, catalog="module")

    with pytest.raises(RuntimeProjectionError, match="catalog node carries"):
        build_runtime_graph(Graph(canonical.blueprint_dir, nodes), project_root=project)


def test_adapter_rejects_a_hand_built_unknown_catalog_kind(tmp_path: Path) -> None:
    project = _project(tmp_path)
    canonical = load_graph(project / "blueprint")
    nodes = dict(canonical.nodes)
    roadmap = nodes["roadmap"]
    nodes[roadmap.id] = replace(roadmap, catalog="book")

    with pytest.raises(RuntimeProjectionError, match="unsupported catalog kind"):
        build_runtime_graph(Graph(canonical.blueprint_dir, nodes), project_root=project)


def _policy_project(tmp_path: Path, policy: str | None) -> Path:
    """`_project` plus an open theorem, a reduction proved from it, and a statement waiting on a gap."""
    project = _project(tmp_path)
    if policy is not None:
        _article(project, "README.md", title="Roadmap", open_statements=policy)
    theorem = {"declaration": "theorem", "statement": "formalized"}
    _article(project, "chapter/section/open.md", title="Open", lean="Project.open_thm", **theorem)
    _article(
        project,
        "chapter/section/reduction.md",
        title="Reduction",
        lean="Project.reduction",
        proof="formalized",
        proof_dependencies=("open.md",),
        **theorem,
    )
    _article(project, "chapter/section/gap.md", title="Gap", declaration="theorem")
    _article(
        project,
        "chapter/section/waiting.md",
        title="Waiting",
        lean="Project.waiting",
        proof_dependencies=("gap.md",),
        **theorem,
    )
    return project


def _status_payloads(project: Path) -> tuple[bool, dict[str, dict[str, object]]]:
    payload = json.loads(load_runtime_graph(project).to_json())
    return payload["open_statements"], {
        node["id"].removeprefix("chapter/section/"): node["status"] for node in payload["nodes"]
    }


def test_strict_runtime_readiness_comes_from_the_derived_status(tmp_path: Path) -> None:
    """Runtime readiness once ignored proof prerequisites that `work list` enforced."""
    open_statements, statuses = _status_payloads(_policy_project(tmp_path, None))

    assert open_statements is False
    reduction = statuses["reduction"]
    assert (reduction["state"], reduction["can_state"], reduction["can_prove"]) == ("proved", False, False)
    assert (reduction["assumes"], reduction["waiting_on"]) == ([], [])
    waiting = statuses["waiting"]
    assert (waiting["state"], waiting["can_state"], waiting["can_prove"]) == ("stated", False, False)
    assert waiting["waiting_on"] == ["chapter/section/gap"]
    assert all(status["assumes"] == [] for status in statuses.values())


def test_open_runtime_records_the_policy_and_what_each_proof_assumes(tmp_path: Path) -> None:
    open_statements, statuses = _status_payloads(_policy_project(tmp_path, "allowed"))

    assert open_statements is True
    reduction = statuses["reduction"]
    assert (reduction["state"], reduction["can_state"], reduction["can_prove"]) == ("conditional", True, True)
    assert reduction["assumes"] == ["chapter/section/open"]
    assert reduction["waiting_on"] == []
    assert not reduction["fully_proved"]
    waiting = statuses["waiting"]
    assert (waiting["state"], waiting["can_state"], waiting["can_prove"]) == ("stated", True, False)
    assert (waiting["assumes"], waiting["waiting_on"]) == ([], ["chapter/section/gap"])
    assert statuses["open"]["state"] == "can_prove"
    assert statuses["open"]["assumes"] == []


def test_runtime_assertions_record_a_retracted_statement(tmp_path: Path) -> None:
    project = _policy_project(tmp_path, "allowed")
    _article(
        project,
        "chapter/section/open.md",
        title="Open",
        declaration="theorem",
        statement="retracted",
        lean="Project.open_thm",
    )

    payload = json.loads(load_runtime_graph(project).to_json())
    nodes = {node["id"].removeprefix("chapter/section/"): node for node in payload["nodes"]}

    assert nodes["open"]["assertions"] == {
        "not_ready": False,
        "proof_formalized": False,
        "statement_formalized": False,
        "statement_retracted": True,
    }
    assert nodes["reduction"]["assertions"]["statement_retracted"] is False
    # The old declaration stays in the build, so the reduction still rests on it.
    assert nodes["reduction"]["status"]["assumes"] == ["chapter/section/open"]
