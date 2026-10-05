from __future__ import annotations

import json
from pathlib import Path

import autoform_cli.beam as beam_module
from autoform_cli import __main__ as cli
from autoform_cli.beam import (
    BEAM_INSPECTION_SCHEMA,
    BeamInspection,
    BeamIssue,
    inspect_beam_runtime,
    load_beam_lock,
)


def _identity(**changes: object) -> dict[str, object]:
    return {
        "name": "lean-beam-mcp",
        "version": "0.2.0-beta",
        "mcp_protocol": "2026-07-28",
        "source_commit": "d5dc8fe9d3928899bf55968a93d9e309d9fad1bc",
        "runtime_current": True,
        "runtime_active": False,
        **changes,
    }


def test_packaged_lock_is_the_preview_contract() -> None:
    lock = load_beam_lock()

    assert lock["commit"] == "d5dc8fe9d3928899bf55968a93d9e309d9fad1bc"
    assert lock["development_only"] is True


def test_runtime_inspection_accepts_only_the_exact_public_identity(monkeypatch) -> None:
    monkeypatch.setattr(beam_module, "_resolve_executable", lambda _command: "/opt/beam/lean-beam-mcp")

    async def probe(_executable: str, _timeout: float) -> dict[str, object]:
        return _identity()

    monkeypatch.setattr(beam_module, "_probe_identity", probe)

    result = inspect_beam_runtime()
    payload = json.loads(result.to_json())

    assert result.ok
    assert payload["schema"] == BEAM_INSPECTION_SCHEMA
    assert payload["command"] == "/opt/beam/lean-beam-mcp"
    assert payload["observed"] == _identity()
    assert payload["required_host_controls"]


def test_runtime_inspection_fails_closed_on_identity_drift(monkeypatch) -> None:
    monkeypatch.setattr(beam_module, "_resolve_executable", lambda _command: "/opt/beam/lean-beam-mcp")

    async def probe(_executable: str, _timeout: float) -> dict[str, object]:
        return _identity(source_commit="f" * 40, runtime_current=False, source_dirty=True)

    monkeypatch.setattr(beam_module, "_probe_identity", probe)

    result = inspect_beam_runtime()

    assert not result.ok
    assert [issue.code for issue in result.issues] == [
        "beam-identity-mismatch",
        "beam-identity-mismatch",
        "beam-source-dirty",
    ]


def test_runtime_inspection_reports_a_missing_executable(monkeypatch) -> None:
    monkeypatch.setattr(beam_module, "_resolve_executable", lambda _command: None)

    result = inspect_beam_runtime("missing-beam")

    assert not result.ok
    assert result.command is None
    assert result.issues == (
        BeamIssue("beam-not-found", "Lean Beam executable was not found: missing-beam"),
    )


def test_beam_doctor_cli_emits_the_admission_report(monkeypatch, capsys) -> None:
    expected = load_beam_lock()
    result = BeamInspection("/beam", expected, _identity(), ("deny save",), ())
    monkeypatch.setattr(cli, "inspect_beam_runtime", lambda command, timeout: result)

    assert cli.main(["beam", "doctor", "--command", "/beam", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert payload["schema"] == BEAM_INSPECTION_SCHEMA


def test_source_checkout_lock_fallback_is_present(repo_root: Path) -> None:
    assert (repo_root / "lean-beam.lock.json").is_file()
