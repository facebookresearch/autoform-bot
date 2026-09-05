from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autoform_cli import declaration_closure as closure_module
from autoform_cli.__main__ import main
from autoform_cli.declaration_closure import declaration_closure


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
        "def Added : Nat := Existing + 1\n"
        "theorem Root : Added = 1 := by rfl\n",
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
    assert "AUTOFORM_DECLARATION_CLOSURE\t" in source


def test_declaration_closure_filters_to_changed_source_declarations(
    tmp_path: Path, monkeypatch
) -> None:
    root, base = _project(tmp_path)

    def fake_run(_root: Path, _command: list[str], stage: str) -> str:
        if stage == "lake build":
            return ""
        return (
            "AUTOFORM_DECLARATION_CLOSURE\tExisting\n"
            "AUTOFORM_DECLARATION_CLOSURE\tAdded\n"
            "AUTOFORM_DECLARATION_CLOSURE\tRoot\n"
        )

    monkeypatch.setattr(closure_module, "_run", fake_run)
    report = declaration_closure(
        root, base=base, modules=["Demo"], roots=["Root"]
    )

    assert [declaration.name for declaration in report.reachable] == ["Added", "Root"]
    assert [declaration.name for declaration in report.definitions] == ["Added"]
    assert report.dirty is True
    assert report.as_dict()["definitions"][0]["url"] is None


def test_declaration_closure_cli_emits_stable_json(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root, base = _project(tmp_path)

    def fake_run(_root: Path, _command: list[str], stage: str) -> str:
        return "" if stage == "lake build" else "AUTOFORM_DECLARATION_CLOSURE\tRoot\n"

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
