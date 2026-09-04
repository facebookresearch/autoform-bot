from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli.doctor import diagnose_project


def _article(
    project: Path,
    relative: str = "result.md",
    *,
    metadata: tuple[str, ...] = ("declaration: theorem",),
    prose: str = "A precise mathematical statement.",
) -> Path:
    path = project / "blueprint" / "roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    title = path.parent.name.title() if path.name.casefold() == "readme.md" else path.stem.title()
    path.write_text(
        "\n".join(
            [
                "---",
                *metadata,
                "---",
                "",
                f"# {title}",
                "",
                prose,
                "",
                "## Depends on",
                "",
                "No prerequisites.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _clean_project(tmp_path: Path, *, metadata: tuple[str, ...] = ("declaration: theorem",)) -> Path:
    project = tmp_path / "project"
    _article(project, metadata=metadata)
    coverage = project / "blueprint" / "coverage" / "README.md"
    coverage.parent.mkdir(parents=True)
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Narrative | OUT | No formalization target |\n",
        encoding="utf-8",
    )
    return project


def _checks(result) -> dict[str, tuple[bool, str]]:
    return {check.name: (check.ok, check.detail) for check in result.checks}


def test_clean_project_and_blueprint_have_deterministic_ordered_checks(tmp_path: Path) -> None:
    project = _clean_project(tmp_path)

    from_project = diagnose_project(project)
    from_blueprint = diagnose_project(project / "blueprint")

    assert from_project == from_blueprint
    assert from_project.clean
    assert [check.name for check in from_project.checks] == [
        "blueprint",
        "runtime",
        "graph",
        "references",
        "audit",
        "lean targets",
    ]
    assert all(check.ok for check in from_project.checks)
    assert _checks(from_project)["lean targets"][1] == "not checked; no Lean root supplied"
    assert from_project.to_json() == diagnose_project(project).to_json()
    assert str(tmp_path) not in from_project.to_json()


def test_missing_or_invalid_graph_returns_failed_checks_without_host_paths(tmp_path: Path) -> None:
    missing = diagnose_project(tmp_path / "missing")

    assert not missing.clean
    assert _checks(missing)["blueprint"] == (False, "project or blueprint directory does not exist")
    assert len(missing.checks) == 6
    assert _checks(missing)["lean targets"] == (True, "not checked; no Lean root supplied")

    project = _clean_project(tmp_path)
    article = project / "blueprint" / "roadmap" / "result.md"
    article.write_text("# First\n# Second\n", encoding="utf-8")
    invalid = diagnose_project(project)

    assert not invalid.clean
    assert _checks(invalid)["runtime"] == (False, "canonical graph is invalid")
    assert "multiple H1 titles" in _checks(invalid)["graph"][1]
    assert str(tmp_path) not in invalid.to_json()


def test_unsafe_roadmap_entry_invalidates_the_doctor_graph(tmp_path: Path) -> None:
    project = _clean_project(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    try:
        (project / "blueprint/roadmap/linked.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    result = diagnose_project(project)

    assert not result.clean
    assert _checks(result)["runtime"] == (False, "canonical graph is invalid")
    assert _checks(result)["graph"][0] is False
    assert "symbolic links are not supported" in _checks(result)["graph"][1]
    assert _checks(result)["references"] == (
        False,
        "not checked because graph validation failed",
    )


def test_graph_diagnostic_does_not_mix_in_coverage_findings(tmp_path: Path) -> None:
    project = _clean_project(tmp_path)
    (project / "blueprint/coverage/README.md").unlink()
    (project / "blueprint/roadmap/result.md").write_text("no heading\n", encoding="utf-8")

    result = diagnose_project(project)

    graph_detail = _checks(result)["graph"][1]
    assert "missing H1 title" in graph_detail
    assert "coverage contract is missing" not in graph_detail


@pytest.mark.parametrize("directory", ["private", "private'area", 'private"area'])
def test_graph_errors_redact_absolute_authored_paths(tmp_path: Path, directory: str) -> None:
    project = _clean_project(tmp_path)
    article = project / "blueprint" / "roadmap" / "result.md"
    outside = tmp_path / directory / "theorem.md"
    text = article.read_text(encoding="utf-8").replace(
        "No prerequisites.",
        f"- [outside](<{outside}>)",
    )
    article.write_text(text, encoding="utf-8")

    result = diagnose_project(project)

    graph_detail = _checks(result)["graph"][1]
    assert "<absolute-path>" in graph_detail
    assert str(outside) not in graph_detail
    assert str(tmp_path) not in result.to_json()


def test_audit_findings_fail_only_the_audit_check(tmp_path: Path) -> None:
    project = _clean_project(tmp_path)
    (project / "blueprint" / "coverage" / "README.md").write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main theorem | MAPPED | Source audit pending |\n",
        encoding="utf-8",
    )

    result = diagnose_project(project)

    assert not result.clean
    assert _checks(result)["runtime"][0]
    assert _checks(result)["graph"][0]
    assert _checks(result)["references"][0]
    assert _checks(result)["audit"] == (False, "1 finding(s): declared-coverage-gap")


def test_optional_lean_targets_report_success_missing_and_kind_mismatch(tmp_path: Path) -> None:
    project = _clean_project(
        tmp_path,
        metadata=(
            "declaration: theorem",
            "statement: formalized",
            "proof: formalized",
            "lean: Project.result",
        ),
    )
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    source = lean_root / "Project.lean"
    source.write_text("theorem Project.result : True := trivial\n", encoding="utf-8")

    clean = diagnose_project(project, lean_root=lean_root)
    assert clean.clean
    assert _checks(clean)["lean targets"] == (True, "all asserted local Lean targets resolve")

    source.write_text("theorem Project.other : True := trivial\n", encoding="utf-8")
    missing = diagnose_project(project, lean_root=lean_root)
    assert _checks(missing)["lean targets"] == (False, "1 finding(s): lean-target-not-found")
    assert _checks(missing)["audit"][0]

    source.write_text("def Project.result : Nat := 1\n", encoding="utf-8")
    mismatch = diagnose_project(project, lean_root=lean_root)
    assert _checks(mismatch)["lean targets"] == (False, "1 finding(s): lean-target-kind-mismatch")


def test_runtime_projection_failure_is_reported_without_traceback(tmp_path: Path) -> None:
    project = _clean_project(
        tmp_path,
        metadata=("declaration: theorem", "mathlib_file: ../Outside.lean"),
    )

    result = diagnose_project(project)

    assert not result.clean
    assert _checks(result)["runtime"] == (
        False,
        "result: mathlib file must be a portable relative path",
    )
    assert _checks(result)["graph"] == (False, "not summarized because runtime projection failed")
    assert len(result.checks) == 6
    assert str(tmp_path) not in result.to_json()


def test_invalid_lean_root_is_a_sanitized_lean_failure(tmp_path: Path) -> None:
    project = _clean_project(tmp_path)
    lean_root = tmp_path / "missing-lean"

    result = diagnose_project(project, lean_root=lean_root)

    assert not result.clean
    assert _checks(result)["runtime"][0]
    assert _checks(result)["lean targets"][0] is False
    assert _checks(result)["lean targets"][1].startswith("1 finding(s): invalid-lean-root:")
    assert str(tmp_path) not in result.to_json()

    loop = tmp_path / "lean-loop"
    try:
        loop.symlink_to(loop)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    loop_result = diagnose_project(project, lean_root=loop)
    assert len(loop_result.checks) == 6
    assert _checks(loop_result)["lean targets"][0] is False
    assert _checks(loop_result)["lean targets"][1].startswith(
        "1 finding(s): invalid-lean-root:"
    )
    assert str(tmp_path) not in loop_result.to_json()


def test_doctor_reports_the_reason_an_existing_lean_root_is_unsafe(tmp_path: Path) -> None:
    project = _clean_project(tmp_path)
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    outside = tmp_path / "outside.lean"
    outside.write_text("theorem outside : True := trivial\n", encoding="utf-8")
    try:
        (lean_root / "Linked.lean").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    result = diagnose_project(project, lean_root=lean_root)

    assert not result.clean
    assert _checks(result)["lean targets"] == (
        False,
        "1 finding(s): invalid-lean-root: unsafe Lean source Linked.lean: "
        "symbolic links are not supported",
    )


def test_doctor_loads_the_canonical_graph_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _clean_project(tmp_path)
    from autoform_cli import doctor

    original = doctor.load_audit_graph
    calls = 0

    def counted_load_graph(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(doctor, "load_audit_graph", counted_load_graph)

    assert diagnose_project(project).clean
    assert calls == 1


def test_doctor_audit_uses_the_same_captured_blueprint_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _clean_project(tmp_path, metadata=("declaration: theorem", "origin: cited"))
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8")
        + "\n## Sources\n\n- [Paper](../sources/paper.md)\n",
        encoding="utf-8",
    )
    source = project / "blueprint/sources/paper.md"
    source.parent.mkdir()
    stable = b"# Paper\n"
    source.write_bytes(stable)
    from autoform_cli import audit as audit_module

    original = audit_module.audit_graph

    def remove_live_source(*args, **kwargs):
        source.unlink()
        try:
            return original(*args, **kwargs)
        finally:
            source.write_bytes(stable)

    monkeypatch.setattr(audit_module, "audit_graph", remove_live_source)

    result = diagnose_project(project)

    assert result.clean


def test_doctor_reports_project_replacement_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _clean_project(tmp_path / "selected")
    replacement = _clean_project(tmp_path / "replacement")
    held = tmp_path / "held-project"
    from autoform_cli import doctor

    original = doctor.load_audit_graph

    def replace_after_load(*args, **kwargs):
        result = original(*args, **kwargs)
        project.rename(held)
        replacement.rename(project)
        return result

    monkeypatch.setattr(doctor, "load_audit_graph", replace_after_load)

    result = diagnose_project(project)

    assert not result.clean
    assert len(result.checks) == 6
    assert _checks(result)["blueprint"] == (
        False,
        "blueprint directory changed during use",
    )


def test_doctor_is_byte_for_byte_read_only_and_uses_no_network_or_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _clean_project(tmp_path)
    before = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("doctor used an external service")

    monkeypatch.setattr("socket.create_connection", fail)
    monkeypatch.setattr("subprocess.run", fail)
    result = diagnose_project(project)

    after = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}
    assert result.clean
    assert after == before
    assert not (project / "graph.json").exists()
    assert not (project / ".autoform").exists()
    assert not (project / "state").exists()


def test_bundled_example_truthfully_reports_incomplete_coverage(repo_root: Path) -> None:
    project = repo_root / "skills" / "setup" / "assets" / "cabannes-thesis-project"

    result = diagnose_project(project, lean_root=project)

    assert not result.clean
    assert _checks(result)["audit"] == (False, "5 finding(s): declared-coverage-gap")
    assert _checks(result)["lean targets"][0]
