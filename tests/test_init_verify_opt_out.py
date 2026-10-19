from __future__ import annotations

import json
from pathlib import Path

from autoform_cli import scaffold as scaffold_module
from autoform_cli.__main__ import main
from autoform_cli.scaffold import scaffold_project

_SOURCE = "https://example.test/autoform.git"
_REF = "1" * 40
_VERIFY = ".github/workflows/autoform-verify.yml"
_WORKFLOWS = Path(__file__).resolve().parents[1] / "autoform_cli" / "templates" / "github" / "workflows"
_STUB = scaffold_module._VERIFY_WORKFLOW_OFF


def _scaffold(project: Path, **kwargs):
    return scaffold_project(project, title="Opt Out", autoform_source=_SOURCE, autoform_ref=_REF, **kwargs)


def _surface(text: str) -> str:
    """A workflow's name, triggers and permissions: what a deploy or required check sees."""

    start = text.index("name: autoform verify\n")
    end = min(text.index(marker, start) for marker in ("\nenv:", "\nconcurrency:", "\njobs:") if marker in text[start:])
    return text[start:end].strip()


def test_the_stub_keeps_the_verify_workflow_name_triggers_and_job() -> None:
    real = (_WORKFLOWS / "autoform-verify.yml").read_text(encoding="utf-8")
    stub = _STUB.decode("utf-8")

    assert _surface(stub) == _surface(real)
    assert "jobs:\n  build:\n    name: build\n" in real
    assert "jobs:\n  build:\n    name: build\n" in stub
    assert stub.startswith("# autoform: verify workflow turned off\n")
    assert "AUTOFORM" not in stub.replace("autoform init", "")


def test_opting_out_writes_the_stub_and_keeps_the_audit_helper(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"

    assert main(["init", str(project), "--autoform-source", _SOURCE, "--autoform-ref", _REF,
                 "--no-verify-workflow", "--json"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"project", "written", "skipped", "unpinned"}
    assert _VERIFY in result["written"]
    assert ".github/autoform_audit.py" in result["written"]
    assert ".github/workflows/blueprint-pages.yml" in result["written"]
    assert (project / _VERIFY).read_bytes() == _STUB


def test_init_keeps_a_turned_off_workflow_even_with_force(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    _scaffold(project, verify_workflow=False)
    stub = (project / _VERIFY).read_bytes()

    for force in (False, True):
        result = _scaffold(project, force=force)
        assert _VERIFY in result.skipped
        assert _VERIFY not in result.written
        assert result.verify_workflow_off == _VERIFY
        assert (project / _VERIFY).read_bytes() == stub

    assert main(["init", str(project), "--autoform-source", _SOURCE, "--autoform-ref", _REF, "--force"]) == 0
    assert f"  = {_VERIFY} (verification turned off on purpose, kept)\n" in capsys.readouterr().out
    # An explicit source with no ref leaves the workflows out, but the stub is still reported as turned off.
    assert main(["init", str(project), "--autoform-source", _SOURCE]) == 0
    assert f"  = {_VERIFY} (verification turned off on purpose, kept)\n" in capsys.readouterr().out


def test_a_project_comment_above_the_marker_keeps_it_turned_off(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _scaffold(project, verify_workflow=False)
    path = project / _VERIFY
    path.write_bytes(b"# Kept off: our own CI builds the Lean.\n" + path.read_bytes())

    result = _scaffold(project, force=True)

    assert _VERIFY in result.skipped
    assert path.read_bytes().startswith(b"# Kept off: our own CI builds the Lean.\n")


def test_deleting_the_stub_brings_the_verify_workflow_back(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _scaffold(project, verify_workflow=False)
    (project / _VERIFY).unlink()
    fresh = tmp_path / "fresh"
    _scaffold(fresh)

    result = _scaffold(project)

    assert _VERIFY in result.written
    assert result.verify_workflow_off is None
    assert (project / _VERIFY).read_bytes() == (fresh / _VERIFY).read_bytes()


def test_opting_out_replaces_a_verify_workflow_already_there_and_nothing_else(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _scaffold(project)
    readme = project / "blueprint" / "README.md"
    readme.write_text("# Edited\n", encoding="utf-8")

    result = _scaffold(project, verify_workflow=False)

    assert result.written == (_VERIFY,)
    assert (project / _VERIFY).read_bytes() == _STUB
    assert readme.read_text(encoding="utf-8") == "# Edited\n"
