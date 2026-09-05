from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autoform_cli import declaration_closure as closure_module
from autoform_cli.__main__ import main
from autoform_cli.declaration_closure import ClosureReport, declaration_closure
from autoform_cli.lean import Declaration


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _project(tmp_path: Path) -> tuple[Path, str]:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "autoform@example.invalid")
    _git(tmp_path, "config", "user.name", "Autoform Test")
    source = tmp_path / "Demo.lean"
    source.write_text("def Existing : Nat := 0\n", encoding="utf-8")
    _git(tmp_path, "add", "Demo.lean")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    source.write_text(
        "def Existing : Nat := 0\n"
        "def Foundation : Nat := Existing + 1\n"
        "def Added : Nat := Foundation + 1\n"
        "theorem Root : Added = 2 := by rfl\n",
        encoding="utf-8",
    )
    return tmp_path, base


def test_lean_driver_uses_elaborated_types_and_definition_values() -> None:
    source = closure_module._lean_driver(
        ["Demo"], ["Root"], ["Added", "Root"]
    )

    assert "| .thmInfo _ => fromType" in source
    assert "value.value.getUsedConstantsAsSet" in source
    assert "NameSet.ofList value.ctors" in source
    assert "privateToUserName?" in source
    assert "AUTOFORM_DECLARATION_NODE\t" in source
    assert "AUTOFORM_DECLARATION_EDGE\t" in source
    assert "AUTOFORM_DECLARATION_SOURCE\t" in source


def test_generated_structure_constants_preserve_source_dependency_order() -> None:
    nodes = {"Root", "Root.mk", "WitnessData"}
    edges = {("Root", "Root.mk"), ("Root.mk", "WitnessData")}
    source_names = {"Root": "Root", "WitnessData": "WitnessData"}

    order = closure_module._dependency_order(nodes, edges)
    source_order = [source_names[name] for name in order if name in source_names]

    assert source_order == ["WitnessData", "Root"]
    assert closure_module._source_dependency_edges(source_names, edges) == (
        ("Root", "WitnessData"),
    )


def test_source_facing_definition_is_not_duplicated_as_a_dependency(
    tmp_path: Path,
) -> None:
    dependency = Declaration("Dependency", Path("Demo.lean"), 1, "def")
    root = Declaration("Root", Path("Demo.lean"), 2, "def")
    report = ClosureReport(
        root=tmp_path,
        base="base",
        head="head",
        dirty=True,
        modules=("Demo",),
        roots=(root,),
        reachable=(dependency, root),
        dependency_edges=(("Root", "Dependency"),),
    )

    assert report.definitions == (dependency,)


def test_imported_module_disambiguates_duplicate_source_names() -> None:
    draft = Declaration(
        "sameName", Path("Blueprint/Example/Suggested.lean"), 10, "theorem"
    )
    production = Declaration(
        "sameName", Path("MathlibExt/Project/Result.lean"), 20, "theorem"
    )

    assert closure_module._resolve_occurrence(
        (draft, production), "MathlibExt.Project.Result"
    ) == production


def test_declaration_closure_filters_to_changed_source_declarations(
    tmp_path: Path, monkeypatch
) -> None:
    root, base = _project(tmp_path)

    def fake_run(_root: Path, _command: list[str], stage: str) -> str:
        if stage == "lake build":
            return ""
        return (
            "AUTOFORM_DECLARATION_NODE\tRoot\n"
            "AUTOFORM_DECLARATION_NODE\tAdded\n"
            "AUTOFORM_DECLARATION_NODE\tFoundation\n"
            "AUTOFORM_DECLARATION_EDGE\tRoot\tAdded\n"
            "AUTOFORM_DECLARATION_EDGE\tAdded\tFoundation\n"
            "AUTOFORM_DECLARATION_SOURCE\tRoot\tRoot\tDemo\n"
            "AUTOFORM_DECLARATION_SOURCE\tAdded\tAdded\tDemo\n"
            "AUTOFORM_DECLARATION_SOURCE\tFoundation\tFoundation\tDemo\n"
        )

    monkeypatch.setattr(closure_module, "_run", fake_run)
    report = declaration_closure(
        root, base=base, modules=["Demo"], roots=["Root"]
    )

    assert [declaration.name for declaration in report.reachable] == [
        "Foundation",
        "Added",
        "Root",
    ]
    assert [declaration.name for declaration in report.definitions] == [
        "Foundation",
        "Added",
    ]
    assert report.dependency_edges == (
        ("Added", "Foundation"),
        ("Root", "Added"),
    )
    assert report.dirty is True
    assert report.as_dict()["definitions"][0]["url"] is None


def test_declaration_closure_normalizes_a_nested_lean_root(
    tmp_path: Path, monkeypatch
) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "autoform@example.invalid")
    _git(tmp_path, "config", "user.name", "Autoform Test")
    lean_root = tmp_path / "consumer"
    lean_root.mkdir()
    source = lean_root / "Demo.lean"
    source.write_text("def Existing : Nat := 0\n", encoding="utf-8")
    _git(tmp_path, "add", "consumer/Demo.lean")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    source.write_text(
        "def Existing : Nat := 0\ndef Added : Nat := 1\ntheorem Root : Added = 1 := by rfl\n",
        encoding="utf-8",
    )

    def fake_run(_root: Path, _command: list[str], stage: str) -> str:
        return (
            ""
            if stage == "lake build"
            else (
                "AUTOFORM_DECLARATION_NODE\tAdded\n"
                "AUTOFORM_DECLARATION_NODE\tRoot\n"
                "AUTOFORM_DECLARATION_SOURCE\tAdded\tAdded\tDemo\n"
                "AUTOFORM_DECLARATION_SOURCE\tRoot\tRoot\tDemo\n"
            )
        )

    monkeypatch.setattr(closure_module, "_run", fake_run)
    report = declaration_closure(
        lean_root, base=base, modules=["Demo"], roots=["Root"]
    )
    assert [declaration.name for declaration in report.definitions] == ["Added"]


def test_declaration_closure_cli_emits_stable_json(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root, base = _project(tmp_path)

    def fake_run(_root: Path, _command: list[str], stage: str) -> str:
        return (
            ""
            if stage == "lake build"
            else "AUTOFORM_DECLARATION_NODE\tRoot\nAUTOFORM_DECLARATION_SOURCE\tRoot\tRoot\tDemo\n"
        )

    monkeypatch.setattr(closure_module, "_run", fake_run)
    assert main(
        [
            "declaration-closure",
            "--lean-root",
            str(root),
            "--base",
            base,
            "--module",
            "Demo",
            "--root",
            "Root",
            "--json",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "autoform-declaration-closure/v1"
    assert payload["roots"][0]["name"] == "Root"
    assert payload["dependency_edges"] == []


def test_declaration_closure_cli_fails_closed_when_build_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root, base = _project(tmp_path)

    def fake_run(_root: Path, _command: list[str], _stage: str) -> str:
        raise closure_module.DeclarationClosureError(
            "lake build failed; exact closure unavailable"
        )

    monkeypatch.setattr(closure_module, "_run", fake_run)
    assert main(
        [
            "declaration-closure",
            "--lean-root",
            str(root),
            "--base",
            base,
            "--module",
            "Demo",
            "--root",
            "Root",
        ]
    ) == 1
    assert capsys.readouterr().err == (
        "error: lake build failed; exact closure unavailable\n"
    )


def test_build_cache_does_not_make_snapshot_dirty(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".lake").mkdir()
    (tmp_path / ".lake/cache").write_text("generated\n", encoding="utf-8")

    assert closure_module._has_relevant_changes(tmp_path) is False
