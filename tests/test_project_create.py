from __future__ import annotations

import json
import multiprocessing
import os
import socket
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.graph import load_graph
from autoform_cli.project import ProjectCreateError, create_project, inspect_project
from autoform_cli.project import create as create_module
from autoform_cli.project import inplace as inplace_module

_RELEASE = "lean-v4.32.2-mathlib-v4.32.2"


class _SimulatedCrash(BaseException):
    pass


def _run_current_project(
    target: str,
    package: str,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.Queue,
) -> None:
    os.chdir(target)
    start.wait()
    try:
        result = create_project(".", package=package, release_id=_RELEASE)
        results.put(("created", result.package))
    except ProjectCreateError as error:
        results.put((error.code, package))


def _crash_current_project(target: str, boundary: str) -> None:
    os.chdir(target)

    def crash(name: str) -> None:
        if name == boundary:
            os._exit(73)

    inplace_module._checkpoint = crash
    create_project(".", package="Benchmark", release_id=_RELEASE)


def test_creation_never_discovers_git_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli import scaffold as scaffold_module

    def forbidden():
        raise AssertionError("project new invoked Git-backed plugin discovery")

    monkeypatch.setattr(scaffold_module, "plugin_pin", forbidden)
    create_project(tmp_path / "Project", package="Project", release_id=_RELEASE)


def test_creation_accepts_only_a_complete_workflow_pin(tmp_path: Path) -> None:
    target = tmp_path / "Project"
    source = "https://example.test/owner/autoform.git"
    revision = "A" * 40

    result = create_project(
        target,
        package="Project",
        release_id=_RELEASE,
        autoform_source=source,
        autoform_ref=revision,
    )

    assert result.workflows_pinned
    workflow = (target / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert f'AUTOFORM_SOURCE: "{source}"' in workflow
    assert f'AUTOFORM_REF: "{revision.lower()}"' in workflow


@pytest.mark.parametrize(
    ("source", "revision"),
    [
        ("https://example.test/owner/autoform.git", ""),
        ("", "1" * 40),
        ("https://example.test/owner/autoform.git", "main"),
        ("https://user:secret@example.test/autoform.git", "1" * 40),
    ],
)
def test_creation_rejects_invalid_provenance_before_writing(
    source: str, revision: str, tmp_path: Path
) -> None:
    target = tmp_path / "Project"

    with pytest.raises(ProjectCreateError) as raised:
        create_project(
            target,
            package="Project",
            release_id=_RELEASE,
            autoform_source=source,
            autoform_ref=revision,
        )

    assert raised.value.code == "project-provenance-invalid"
    assert not target.exists()
    assert not list(tmp_path.glob(".Project.autoform-new-*"))


def test_creation_with_an_explicit_pin_stays_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli import provenance, scaffold as scaffold_module

    def forbidden(*args, **kwargs):
        raise AssertionError("project new crossed its offline boundary")

    monkeypatch.setattr(scaffold_module, "plugin_pin", forbidden)
    monkeypatch.setattr(provenance, "verify_plugin_provenance", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    result = create_project(
        tmp_path / "Project",
        package="Project",
        release_id=_RELEASE,
        autoform_source="https://example.test/owner/autoform.git",
        autoform_ref="1" * 40,
    )

    assert result.workflows_pinned


def test_creates_complete_supported_project(tmp_path: Path) -> None:
    target = tmp_path / "FiniteFlat"
    result = create_project(target, package="FiniteFlat", release_id=_RELEASE)

    assert result.package == "FiniteFlat"
    assert result.release == _RELEASE
    assert result.target == "FiniteFlat"
    assert (target / "lean-toolchain").read_text(encoding="utf-8") == (
        "leanprover/lean4:v4.32.2\n"
    )
    assert (target / "lakefile.toml").read_text(encoding="utf-8") == (
        'name = "FiniteFlat"\n'
        'version = "0.1.0"\n'
        'defaultTargets = ["FiniteFlat"]\n\n'
        '[[require]]\n'
        'name = "mathlib"\n'
        'git = "https://github.com/leanprover-community/mathlib4.git"\n'
        'rev = "v4.32.2"\n\n'
        '[[lean_lib]]\n'
        'name = "FiniteFlat"\n'
        'srcDir = "src"\n'
    )
    assert (target / "src/FiniteFlat.lean").read_text(encoding="utf-8") == (
        "import Mathlib\n\n"
        "namespace FiniteFlat\n\n"
        "/-- Marker declaration for the initial project build. -/\n"
        "def autoformProjectInitialized : Bool := true\n\n"
        "end FiniteFlat\n"
    )
    inspection = inspect_project(target)
    assert inspection.ok
    assert inspection.compatibility.status == "supported"
    assert inspection.compatibility.release == _RELEASE
    assert set(load_graph(target / "blueprint").nodes) == {"roadmap"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not list(tmp_path.glob(".FiniteFlat.autoform-new-*"))


def test_creates_in_current_directory_without_replacing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Benchmark"
    target.mkdir(mode=0o750)
    target.chmod(0o750)
    monkeypatch.chdir(target)
    before = os.stat(".")

    result = create_project(".", package="Benchmark", release_id=_RELEASE)

    after = os.stat(".")
    assert result.target == "."
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert stat.S_IMODE(after.st_mode) == 0o750
    assert inspect_project(Path(".")).ok
    assert not Path(inplace_module.MARKER).exists()


@pytest.mark.parametrize("unsupported", ["platform", "filesystem", "noreplace"])
def test_current_directory_rejects_unsupported_safety_before_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsupported: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    if unsupported == "platform":
        monkeypatch.setattr(inplace_module.sys, "platform", "win32")
    elif unsupported == "filesystem":
        monkeypatch.setattr(
            inplace_module, "_filesystem_supported", lambda descriptor: False
        )
    else:
        monkeypatch.setattr(inplace_module, "_noreplace_function", lambda: None)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raised.value.code == "project-create-safety-unavailable"
    assert not os.listdir(".")


def test_current_directory_rejects_missing_directory_durability_before_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_sync(descriptor: int) -> None:
        raise OSError("directory sync unavailable")

    monkeypatch.setattr(inplace_module.os, "fsync", fail_sync)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raised.value.code == "project-create-safety-unavailable"
    assert not os.listdir(".")


def test_current_directory_rejects_missing_fstatfs_before_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(inplace_module.ctypes, "CDLL", lambda *args, **kwargs: object())

    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raised.value.code == "project-create-safety-unavailable"
    assert not os.listdir(".")


@pytest.mark.parametrize("kind", ["hidden", "symlink", "fifo"])
def test_current_directory_requires_literal_emptiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.chdir(tmp_path)
    entry = Path(".hidden")
    if kind == "hidden":
        entry.write_text("keep\n", encoding="utf-8")
    elif kind == "symlink":
        entry.symlink_to("missing")
    else:
        os.mkfifo(entry)
    before = entry.lstat()

    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raised.value.code == "project-target-not-empty"
    after = entry.lstat()
    assert (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode)) == (
        before.st_dev,
        before.st_ino,
        stat.S_IFMT(before.st_mode),
    )
    assert not Path(inplace_module.MARKER).exists()


def test_current_directory_detects_substitution_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Benchmark"
    moved = tmp_path / "moved"
    target.mkdir()
    monkeypatch.chdir(target)
    original = create_module._validate_staged_project

    def substitute(stage: Path, release) -> None:
        original(stage, release)
        target.rename(moved)
        target.mkdir()
        (target / "FOREIGN").write_text("keep\n", encoding="utf-8")

    monkeypatch.setattr(create_module, "_validate_staged_project", substitute)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raised.value.code == "project-target-changed"
    assert (target / "FOREIGN").read_text(encoding="utf-8") == "keep\n"
    assert not any(moved.iterdir())


@pytest.mark.parametrize("boundary", ["prepared:.gitignore", "renamed:.gitignore"])
def test_current_directory_recovers_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    original = inplace_module._checkpoint

    def crash(name: str) -> None:
        if name == boundary:
            raise _SimulatedCrash

    monkeypatch.setattr(inplace_module, "_checkpoint", crash)
    with pytest.raises(_SimulatedCrash):
        create_project(".", package="Benchmark", release_id=_RELEASE)
    assert Path(inplace_module.MARKER).is_dir()

    monkeypatch.setattr(inplace_module, "_checkpoint", original)
    result = create_project(".", package="Benchmark", release_id=_RELEASE)

    assert result.target == "."
    assert inspect_project(Path(".")).ok
    assert not Path(inplace_module.MARKER).exists()


@pytest.mark.parametrize(
    "boundary",
    [
        "committed",
        "stage-removed",
        "metadata-removed",
        "journal-removed",
    ],
)
def test_current_directory_recovers_cleanup_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    original = inplace_module._checkpoint

    def crash(name: str) -> None:
        if name == boundary:
            raise _SimulatedCrash

    monkeypatch.setattr(inplace_module, "_checkpoint", crash)
    with pytest.raises(_SimulatedCrash):
        create_project(".", package="Benchmark", release_id=_RELEASE)
    assert Path(inplace_module.MARKER).is_dir()

    monkeypatch.setattr(inplace_module, "_checkpoint", original)
    result = create_project(".", package="Benchmark", release_id=_RELEASE)

    assert result.target == "."
    assert inspect_project(Path(".")).ok
    assert not Path(inplace_module.MARKER).exists()


def test_current_directory_recovers_after_marker_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original = inplace_module._checkpoint

    def crash(name: str) -> None:
        if name == "marker-removed":
            raise _SimulatedCrash

    monkeypatch.setattr(inplace_module, "_checkpoint", crash)
    with pytest.raises(_SimulatedCrash):
        create_project(".", package="Benchmark", release_id=_RELEASE)
    assert not Path(inplace_module.MARKER).exists()
    assert inspect_project(Path(".")).ok

    monkeypatch.setattr(inplace_module, "_checkpoint", original)
    result = create_project(".", package="Benchmark", release_id=_RELEASE)

    assert result.target == "."
    assert inspect_project(Path(".")).ok


def test_current_directory_preserves_ambiguous_empty_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original = inplace_module._checkpoint

    def crash(name: str) -> None:
        if name == "manifest-removed":
            raise _SimulatedCrash

    monkeypatch.setattr(inplace_module, "_checkpoint", crash)
    with pytest.raises(_SimulatedCrash):
        create_project(".", package="Benchmark", release_id=_RELEASE)
    marker = Path(inplace_module.MARKER)
    assert marker.is_dir()
    assert not os.listdir(marker)

    monkeypatch.setattr(inplace_module, "_checkpoint", original)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raised.value.code == "project-recovery-required"
    assert marker.is_dir()


def test_current_directory_preserves_corrupt_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original = inplace_module._checkpoint

    def crash(name: str) -> None:
        if name == "prepared:.gitignore":
            raise _SimulatedCrash

    monkeypatch.setattr(inplace_module, "_checkpoint", crash)
    with pytest.raises(_SimulatedCrash):
        create_project(".", package="Benchmark", release_id=_RELEASE)
    journal = Path(inplace_module.MARKER) / inplace_module.JOURNAL
    journal.write_text("not json\n", encoding="utf-8")

    monkeypatch.setattr(inplace_module, "_checkpoint", original)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raised.value.code == "project-recovery-required"
    assert journal.read_text(encoding="utf-8") == "not json\n"


def test_current_directory_recovers_a_torn_final_journal_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original = inplace_module._write_all
    interrupted = False

    def tear_append(descriptor: int, payload: bytes) -> None:
        nonlocal interrupted
        if not interrupted and b'"kind":"prepared"' in payload:
            interrupted = True
            os.write(descriptor, payload[: len(payload) // 2])
            os.fsync(descriptor)
            raise _SimulatedCrash
        original(descriptor, payload)

    monkeypatch.setattr(inplace_module, "_write_all", tear_append)
    with pytest.raises(_SimulatedCrash):
        create_project(".", package="Benchmark", release_id=_RELEASE)

    monkeypatch.setattr(inplace_module, "_write_all", original)
    result = create_project(".", package="Benchmark", release_id=_RELEASE)

    assert result.target == "."
    assert inspect_project(Path(".")).ok
    assert not Path(inplace_module.MARKER).exists()


def test_current_directory_recovers_a_torn_initial_journal_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original = inplace_module._write_all
    interrupted = False

    def tear_append(descriptor: int, payload: bytes) -> None:
        nonlocal interrupted
        if not interrupted and b'"kind":"begin"' in payload:
            interrupted = True
            os.write(descriptor, payload[: len(payload) // 2])
            os.fsync(descriptor)
            raise _SimulatedCrash
        original(descriptor, payload)

    monkeypatch.setattr(inplace_module, "_write_all", tear_append)
    with pytest.raises(_SimulatedCrash):
        create_project(".", package="Benchmark", release_id=_RELEASE)

    monkeypatch.setattr(inplace_module, "_write_all", original)
    result = create_project(".", package="Benchmark", release_id=_RELEASE)

    assert result.target == "."
    assert inspect_project(Path(".")).ok
    assert not Path(inplace_module.MARKER).exists()


def test_current_directory_preserves_an_invalid_unterminated_journal_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original = inplace_module._checkpoint

    def crash(name: str) -> None:
        if name == "prepared:.gitignore":
            raise _SimulatedCrash

    monkeypatch.setattr(inplace_module, "_checkpoint", crash)
    with pytest.raises(_SimulatedCrash):
        create_project(".", package="Benchmark", release_id=_RELEASE)
    journal = Path(inplace_module.MARKER) / inplace_module.JOURNAL
    with journal.open("ab") as output:
        output.write(b"not-a-valid-successor")

    monkeypatch.setattr(inplace_module, "_checkpoint", original)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raised.value.code == "project-recovery-required"
    assert journal.read_bytes().endswith(b"not-a-valid-successor")


def test_current_directory_rejects_different_recovery_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original = inplace_module._checkpoint

    def crash(name: str) -> None:
        if name == "prepared:.gitignore":
            raise _SimulatedCrash

    monkeypatch.setattr(inplace_module, "_checkpoint", crash)
    with pytest.raises(_SimulatedCrash):
        create_project(".", package="Benchmark", release_id=_RELEASE)

    monkeypatch.setattr(inplace_module, "_checkpoint", original)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Different", release_id=_RELEASE)

    assert raised.value.code == "project-recovery-required"
    assert Path(inplace_module.MARKER).is_dir()


def test_current_directory_preserves_foreign_mutation_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def mutate(name: str) -> None:
        if name == "renamed:.gitignore":
            Path(".gitignore").write_text("foreign\n", encoding="utf-8")
            raise OSError("injected failure")

    monkeypatch.setattr(inplace_module, "_checkpoint", mutate)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raised.value.code == "project-recovery-required"
    assert Path(".gitignore").read_text(encoding="utf-8") == "foreign\n"
    assert Path(inplace_module.MARKER).is_dir()


def test_current_directory_preserves_destination_raced_after_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def occupy(name: str) -> None:
        if name == "prepared:.gitignore":
            Path(".gitignore").write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(inplace_module, "_checkpoint", occupy)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raised.value.code == "project-recovery-required"
    assert Path(".gitignore").read_text(encoding="utf-8") == "foreign\n"
    assert Path(inplace_module.MARKER).is_dir()


def test_current_directory_preserves_substituted_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    moved = tmp_path / "owned-marker"

    def substitute(name: str) -> None:
        if name == "prepared:.gitignore":
            Path(inplace_module.MARKER).rename(moved)
            Path(inplace_module.MARKER).mkdir()
            (Path(inplace_module.MARKER) / "FOREIGN").write_text(
                "keep\n", encoding="utf-8"
            )

    monkeypatch.setattr(inplace_module, "_checkpoint", substitute)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raised.value.code == "project-recovery-required"
    assert (Path(inplace_module.MARKER) / "FOREIGN").read_text(
        encoding="utf-8"
    ) == "keep\n"
    assert (moved / inplace_module.MANIFEST).is_file()


def test_current_directory_rolls_back_only_after_complete_ownership_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def fail(name: str) -> None:
        if name == "renamed:.gitignore":
            raise OSError("injected failure")

    monkeypatch.setattr(inplace_module, "_checkpoint", fail)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raised.value.code == "project-create-failed"
    assert not os.listdir(".")


def test_current_directory_does_not_delete_a_root_swapped_before_rollback_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original_checkpoint = inplace_module._checkpoint
    original_owned_remainder = inplace_module._entry_is_owned_remainder
    rollback_started = False
    publication_failed = False
    raced = False

    def fail_publication(name: str) -> None:
        nonlocal publication_failed, rollback_started
        if name == "renamed:.gitignore" and not publication_failed:
            publication_failed = True
            raise OSError("injected publication failure")
        if name == "rollback-started":
            rollback_started = True

    def swap_after_audit(parent_descriptor, name, expected):
        nonlocal raced
        matched = original_owned_remainder(parent_descriptor, name, expected)
        target = os.fstat(parent_descriptor)
        current = Path(".").stat()
        if (
            matched
            and rollback_started
            and not raced
            and name == ".gitignore"
            and (target.st_dev, target.st_ino) == (current.st_dev, current.st_ino)
        ):
            raced = True
            Path(".gitignore").rename("owned.gitignore")
            Path(".gitignore").write_text("foreign\n", encoding="utf-8")
        return matched

    monkeypatch.setattr(inplace_module, "_checkpoint", fail_publication)
    monkeypatch.setattr(
        inplace_module, "_entry_is_owned_remainder", swap_after_audit
    )
    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raced
    assert raised.value.code == "project-recovery-required"
    assert Path(".gitignore").read_text(encoding="utf-8") == "foreign\n"
    assert Path("owned.gitignore").is_file()
    assert Path(inplace_module.MARKER).is_dir()

    monkeypatch.setattr(inplace_module, "_checkpoint", original_checkpoint)


@pytest.mark.parametrize(
    "rollback_boundary", ["rollback-started", "rollback-removed:.gitignore"]
)
def test_current_directory_resumes_interrupted_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollback_boundary: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    original = inplace_module._checkpoint
    publication_failed = False

    def crash(name: str) -> None:
        nonlocal publication_failed
        if name == "renamed:.gitignore" and not publication_failed:
            publication_failed = True
            raise OSError("injected publication failure")
        if name == rollback_boundary:
            raise _SimulatedCrash

    monkeypatch.setattr(inplace_module, "_checkpoint", crash)
    with pytest.raises(_SimulatedCrash):
        create_project(".", package="Benchmark", release_id=_RELEASE)
    assert Path(inplace_module.MARKER).is_dir()

    monkeypatch.setattr(inplace_module, "_checkpoint", original)
    result = create_project(".", package="Benchmark", release_id=_RELEASE)

    assert result.target == "."
    assert inspect_project(Path(".")).ok
    assert not Path(inplace_module.MARKER).exists()


def test_current_directory_resumes_partially_deleted_nested_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original = inplace_module._checkpoint
    publication_failed = False

    def crash(name: str) -> None:
        nonlocal publication_failed
        if name == "renamed:.gitignore" and not publication_failed:
            publication_failed = True
            raise OSError("injected publication failure")
        if name == "rollback-node-removed:blueprint/README.md":
            raise _SimulatedCrash

    monkeypatch.setattr(inplace_module, "_checkpoint", crash)
    with pytest.raises(_SimulatedCrash):
        create_project(".", package="Benchmark", release_id=_RELEASE)
    assert Path(inplace_module.MARKER).is_dir()

    monkeypatch.setattr(inplace_module, "_checkpoint", original)
    result = create_project(".", package="Benchmark", release_id=_RELEASE)

    assert result.target == "."
    assert inspect_project(Path(".")).ok
    assert not Path(inplace_module.MARKER).exists()


@pytest.mark.parametrize(
    "cleanup_boundary", ["stage-removed", "metadata-removed", "journal-removed"]
)
def test_current_directory_resumes_interrupted_rollback_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_boundary: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    original = inplace_module._checkpoint
    publication_failed = False

    def crash(name: str) -> None:
        nonlocal publication_failed
        if name == "renamed:.gitignore" and not publication_failed:
            publication_failed = True
            raise OSError("injected publication failure")
        if name == cleanup_boundary:
            raise _SimulatedCrash

    monkeypatch.setattr(inplace_module, "_checkpoint", crash)
    with pytest.raises(_SimulatedCrash):
        create_project(".", package="Benchmark", release_id=_RELEASE)
    assert Path(inplace_module.MARKER).is_dir()

    monkeypatch.setattr(inplace_module, "_checkpoint", original)
    result = create_project(".", package="Benchmark", release_id=_RELEASE)

    assert result.target == "."
    assert inspect_project(Path(".")).ok
    assert not Path(inplace_module.MARKER).exists()


def test_current_directory_rolls_back_late_noreplace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def unavailable(*args, **kwargs):
        raise inplace_module.InPlaceCreateError(
            "project-create-safety-unavailable", "injected no-replace failure"
        )

    monkeypatch.setattr(inplace_module, "_rename_noreplace", unavailable)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(".", package="Benchmark", release_id=_RELEASE)

    assert raised.value.code == "project-create-safety-unavailable"
    assert not os.listdir(".")


def test_current_directory_closes_every_opened_parent_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    opened: list[int] = []
    original = inplace_module._open_absolute_directory

    def recording_open(path: Path) -> int:
        descriptor = original(path)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(inplace_module, "_open_absolute_directory", recording_open)
    create_project(".", package="Benchmark", release_id=_RELEASE)

    assert len(opened) >= 2
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_current_directory_does_not_leak_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor_directory = Path("/dev/fd")
    if not descriptor_directory.is_dir():
        descriptor_directory = Path("/proc/self/fd")
    if not descriptor_directory.is_dir():
        pytest.skip("platform does not expose process descriptors")
    monkeypatch.chdir(tmp_path)
    before = len(os.listdir(descriptor_directory))

    create_project(".", package="Benchmark", release_id=_RELEASE)

    assert len(os.listdir(descriptor_directory)) == before


def test_current_directory_recovers_after_process_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_current_project,
        args=(str(tmp_path), "renamed:.gitignore"),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 73
    assert (tmp_path / inplace_module.MARKER).is_dir()

    monkeypatch.chdir(tmp_path)
    create_project(".", package="Benchmark", release_id=_RELEASE)
    assert inspect_project(Path(".")).ok
    assert not Path(inplace_module.MARKER).exists()


def test_current_directory_process_race_has_one_winner(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_run_current_project,
            args=(str(tmp_path), package, start, results),
        )
        for package in ("Alpha", "Beta")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=30)
    assert all(process.exitcode == 0 for process in processes)
    outcomes = [results.get(timeout=5) for _ in processes]
    winners = [package for outcome, package in outcomes if outcome == "created"]
    losers = [outcome for outcome, _ in outcomes if outcome != "created"]
    assert len(winners) == 1
    assert losers == ["project-target-not-empty"]
    winner = winners[0]
    assert f'name = "{winner}"' in (tmp_path / "lakefile.toml").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    "package",
    [
        "",
        "finiteFlat",
        "Finite_Flat",
        "Finite.Flat",
        "../FiniteFlat",
        "Finite Flat",
        'Finite"Flat',
        "Type",
        "Sort",
        "Prop",
        "Mathlib",
    ],
)
def test_rejects_invalid_package_before_writing(tmp_path: Path, package: str) -> None:
    target = tmp_path / "project"
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package=package, release_id=_RELEASE)
    assert raised.value.code == "project-name-invalid"
    assert not target.exists()
    assert not list(tmp_path.glob(".project.autoform-new-*"))


def test_rejects_unknown_release_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "project"
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id="unknown")
    assert raised.value.code == "project-release-unknown"
    assert not target.exists()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "broken-symlink"])
def test_never_overwrites_existing_target(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "project"
    if kind == "file":
        target.write_bytes(b"authored\n")
    elif kind == "directory":
        target.mkdir()
        (target / "authored").write_bytes(b"authored\n")
    else:
        real = tmp_path / "real"
        if kind == "symlink":
            real.mkdir()
        target.symlink_to(real, target_is_directory=True)
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes())
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.is_symlink()
    )

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-target-exists"
    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes())
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    assert after == before


def test_normal_macos_tmp_alias_is_supported() -> None:
    if not Path("/tmp").is_symlink():
        pytest.skip("platform has no /tmp alias")
    parent = Path("/tmp") / f"autoform-new-test-{os.getpid()}"
    parent.mkdir()
    target = parent / "Project"
    try:
        create_project(target, package="Project", release_id=_RELEASE)
        assert inspect_project(parent.resolve() / "Project").ok
    finally:
        import shutil

        shutil.rmtree(parent, ignore_errors=True)


def test_rejects_nonsticky_shared_parent(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(parent / "Project", package="Project", release_id=_RELEASE)
    assert raised.value.code == "project-parent-unsafe"


def test_injected_build_failure_leaves_no_target_or_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"

    def fail(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(create_module, "_build_staged_project", fail)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)
    assert raised.value.code == "project-create-failed"
    assert not target.exists()
    assert not list(tmp_path.glob(".project.autoform-new-*"))


def test_injected_validation_failure_leaves_no_target_or_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"

    def fail(*args, **kwargs):
        raise ProjectCreateError("project-create-validation-failed", "invalid")

    monkeypatch.setattr(create_module, "_validate_staged_project", fail)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)
    assert raised.value.code == "project-create-validation-failed"
    assert not target.exists()
    assert not list(tmp_path.glob(".project.autoform-new-*"))


def test_workspace_substitution_fails_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original = create_module._validate_staged_project

    def substitute(stage: Path, release) -> None:
        original(stage, release)
        workspace = stage.parent
        moved = workspace.with_name(f"{workspace.name}-owned")
        workspace.rename(moved)
        workspace.mkdir(mode=0o700)
        (workspace / "FOREIGN").write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(create_module, "_validate_staged_project", substitute)
    with pytest.raises(ProjectCreateError):
        create_project(target, package="Project", release_id=_RELEASE)
    assert not target.exists()
    assert any(path.name == "FOREIGN" for path in tmp_path.rglob("FOREIGN"))


def test_stage_open_failure_removes_the_owned_empty_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original = create_module._open_stage

    def fail_first_open(parent_descriptor: int, stage_name: str) -> int:
        if stage_name.startswith(".project.autoform-new-"):
            raise OSError("injected stage open failure")
        return original(parent_descriptor, stage_name)

    monkeypatch.setattr(create_module, "_open_stage", fail_first_open)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-failed"
    assert not target.exists()
    assert not list(tmp_path.glob(".project.autoform-new-*"))


def test_cleanup_never_deletes_a_substituted_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original = create_module._remove_owned_stage
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "KEEP").write_text("keep\n", encoding="utf-8")

    def substitute(parent_descriptor, stage_name, stage_descriptor, identity):
        original_name = f"{stage_name}-owned"
        os.rename(stage_name, original_name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
        os.mkdir(stage_name, dir_fd=parent_descriptor)
        replacement = tmp_path / stage_name
        (replacement / "FOREIGN").write_text("foreign\n", encoding="utf-8")
        return original(parent_descriptor, stage_name, stage_descriptor, identity)

    def fail(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(create_module, "_build_staged_project", fail)
    monkeypatch.setattr(create_module, "_remove_owned_stage", substitute)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)
    assert raised.value.code == "project-cleanup-failed"
    assert (foreign / "KEEP").read_text(encoding="utf-8") == "keep\n"
    assert any(path.name == "FOREIGN" for path in tmp_path.rglob("FOREIGN"))


def test_concurrent_creation_has_exactly_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    barrier = threading.Barrier(2)
    original = create_module._validate_staged_project

    def synchronized(stage: Path, release) -> None:
        original(stage, release)
        barrier.wait(timeout=10)

    monkeypatch.setattr(create_module, "_validate_staged_project", synchronized)
    results: list[str] = []

    def run() -> None:
        try:
            create_project(target, package="Project", release_id=_RELEASE)
            results.append("created")
        except ProjectCreateError as error:
            results.append(error.code)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == ["created", "project-target-exists"]
    assert inspect_project(target).ok
    assert not list(tmp_path.glob(".project.autoform-new-*"))


@pytest.mark.parametrize(
    "arguments, code",
    [
        (["project", "new", "--json"], "project-target-invalid"),
        (["project", "new", "project", "--release", _RELEASE, "--json"], "project-name-invalid"),
        (["project", "new", "project", "--package", "Project", "--json"], "project-release-unknown"),
    ],
)
def test_cli_missing_creation_options_are_json(
    arguments: list[str], code: str, capsys
) -> None:
    assert main(arguments) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]["code"] == code
    assert captured.err == ""


def test_cli_json_is_stable_and_path_free(tmp_path: Path, capsys) -> None:
    target = tmp_path / "project"
    assert main(
        [
            "project",
            "new",
            str(target),
            "--package",
            "Project",
            "--release",
            _RELEASE,
            "--json",
        ]
    ) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["target"] == "project"
    assert str(tmp_path) not in captured.out
    assert captured.err == ""

    duplicate = tmp_path / "project"
    assert main(
        [
            "project",
            "new",
            str(duplicate),
            "--package",
            "Project",
            "--release",
            _RELEASE,
            "--json",
        ]
    ) == 1
    failed = capsys.readouterr()
    assert json.loads(failed.out)["error"]["code"] == "project-target-exists"
    assert failed.err == ""


def test_cli_creates_in_current_directory(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(
        [
            "project",
            "new",
            ".",
            "--package",
            "Benchmark",
            "--release",
            _RELEASE,
            "--json",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["target"] == "."


def test_cli_threads_explicit_workflow_pin_in_current_directory(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    source = "https://example.test/owner/autoform.git"
    revision = "6" * 40
    monkeypatch.chdir(tmp_path)

    assert main(
        [
            "project",
            "new",
            ".",
            "--package",
            "Benchmark",
            "--release",
            _RELEASE,
            "--autoform-source",
            source,
            "--autoform-ref",
            revision,
            "--json",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["workflows_pinned"] is True
    workflow = Path(".github/workflows/autoform-verify.yml").read_text(
        encoding="utf-8"
    )
    assert f'AUTOFORM_SOURCE: "{source}"' in workflow
    assert f'AUTOFORM_REF: "{revision}"' in workflow


def test_cli_threads_the_explicit_workflow_pin(tmp_path: Path, capsys) -> None:
    target = tmp_path / "Pinned"
    source = "https://example.test/owner/autoform.git"
    revision = "5" * 40

    assert main(
        [
            "project",
            "new",
            os.fspath(target),
            "--package",
            "Pinned",
            "--release",
            _RELEASE,
            "--autoform-source",
            source,
            "--autoform-ref",
            revision,
            "--json",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["workflows_pinned"] is True
    workflow = (target / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert f'AUTOFORM_SOURCE: "{source}"' in workflow
    assert f'AUTOFORM_REF: "{revision}"' in workflow
