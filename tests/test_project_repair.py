from __future__ import annotations

import errno
import json
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.project import (
    ProjectCreateError,
    ProjectRepairConflict,
    ProjectRepairError,
    create_project,
    repair_project,
)
from autoform_cli.project import repair as repair_module
from autoform_cli.workspace_mutation import initialize_workspace

_RELEASE = "lean-v4.32.2-mathlib-v4.32.2"
_LEAN_4330_RELEASE = "lean-v4.33.0-mathlib-v4.33.0"


def _project(tmp_path: Path, *, release_id: str = _RELEASE) -> Path:
    root = tmp_path / "project"
    create_project(root, package="Project", release_id=release_id)
    return root


def _repair(target: str | Path, **kwargs):
    options = {"title": "Project", "repository_url": ""}
    options.update(kwargs)
    return repair_project(target, **options)


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_repairs_only_missing_overlay_files_and_preserves_existing_bytes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    authored = b"# Authored landing page\n"
    (root / "README.md").write_bytes(authored)
    (root / "mkdocs.yml").unlink()
    (root / "blueprint/coverage/README.md").unlink()
    before = _files(root)

    result = _repair(root)

    assert result.planned == ("blueprint/coverage/README.md", "mkdocs.yml")
    assert result.written == result.planned
    assert result.converged == ()
    assert (root / "README.md").read_bytes() == authored
    after = _files(root)
    for path, content in before.items():
        assert after[path] == content
    assert (root / "mkdocs.yml").is_file()
    assert (root / "blueprint/coverage/README.md").is_file()


def test_dry_run_reports_exact_plan_without_writing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    before = _files(root)

    result = _repair(root, dry_run=True)

    assert result.dry_run
    assert result.planned == ("mkdocs.yml",)
    assert result.written == ()
    assert result.converged == ()
    assert _files(root) == before
    assert not (root / "mkdocs.yml").exists()


def test_repairs_supported_lean_4_33_0_project(tmp_path: Path) -> None:
    root = _project(tmp_path, release_id=_LEAN_4330_RELEASE)
    (root / "mkdocs.yml").unlink()

    result = _repair(root)

    assert result.release == _LEAN_4330_RELEASE
    assert result.written == ("mkdocs.yml",)
    assert (root / "mkdocs.yml").is_file()


def test_second_repair_is_a_noop(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    first = _repair(root)
    after_first = _files(root)
    second = _repair(root)

    assert first.written == ("mkdocs.yml",)
    assert second.planned == ()
    assert second.written == ()
    assert second.converged == ()
    assert _files(root) == after_first


def test_legacy_repair_refuses_a_manifest_managed_workspace(tmp_path: Path) -> None:
    root = _project(tmp_path)
    initialize_workspace(root, blueprint_root="Plans")

    with pytest.raises(ProjectRepairError) as raised:
        _repair(root, dry_run=True)

    assert any(
        conflict.code == "project-repair-workspace-unsupported"
        for conflict in raised.value.conflicts
    )


def test_aggregate_conflicts_produce_zero_writes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    shutil.rmtree(root / "theme")
    (root / "theme").write_bytes(b"authored blocker\n")
    (root / "mkdocs.yml").unlink()
    before = _files(root)

    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-conflict"
    assert {conflict.code for conflict in raised.value.conflicts} == {
        "project-repair-parent-not-directory"
    }
    assert _files(root) == before
    assert not (root / "mkdocs.yml").exists()


def test_nested_target_is_rejected_without_writes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    nested = root / "src"
    before = _files(root)

    with pytest.raises(ProjectRepairError) as raised:
        _repair(nested)

    assert raised.value.conflicts[0].code == "project-repair-target-invalid"
    assert _files(root) == before


def test_missing_managed_parent_is_a_zero_write_conflict(tmp_path: Path) -> None:
    root = _project(tmp_path)
    shutil.rmtree(root / "blueprint/coverage")
    before = _files(root)

    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert any(
        conflict.code == "project-repair-parent-missing"
        and conflict.path == "blueprint/coverage"
        for conflict in raised.value.conflicts
    )
    assert _files(root) == before
    assert not (root / "blueprint/coverage").exists()


def test_malformed_or_unsupported_project_produces_zero_writes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lean-toolchain").write_text("leanprover/lean4:v0.0.0\n", encoding="utf-8")
    (root / "mkdocs.yml").unlink()
    before = _files(root)

    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert any(
        conflict.code == "project-repair-release-indeterminate"
        for conflict in raised.value.conflicts
    )
    assert _files(root) == before


def test_existing_managed_files_are_authoritative(tmp_path: Path) -> None:
    root = _project(tmp_path)
    authored = b"not generated yaml, but deliberately preserved\n"
    (root / "mkdocs.yml").write_bytes(authored)

    result = _repair(root)

    assert result.planned == ()
    assert "mkdocs.yml" in result.preserved
    assert (root / "mkdocs.yml").read_bytes() == authored


def test_concurrent_repairs_serialize_without_overwriting(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def run() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(_repair(root))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert sum(result.written == ("mkdocs.yml",) for result in results) == 1
    assert sum(result.planned == () for result in results) == 1
    assert all(result.converged == () for result in results)
    assert (root / "mkdocs.yml").is_file()


def test_different_concurrent_winner_is_retained_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original = repair_module._rename_noreplace

    def competing(source_parent, source, target_parent, target):
        if target == "mkdocs.yml":
            descriptor = repair_module.os.open(
                target,
                repair_module.os.O_WRONLY
                | repair_module.os.O_CREAT
                | repair_module.os.O_EXCL,
                0o644,
                dir_fd=target_parent,
            )
            try:
                repair_module.os.write(descriptor, b"concurrent authored content\n")
            finally:
                repair_module.os.close(descriptor)
        return original(source_parent, source, target_parent, target)

    monkeypatch.setattr(repair_module, "_rename_noreplace", competing)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert (root / "mkdocs.yml").read_bytes() == b"concurrent authored content\n"
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name


def test_repair_does_not_discover_git_provenance_or_run_subprocesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    def forbidden(*args, **kwargs):
        raise AssertionError("repair invoked a forbidden external operation")

    from autoform_cli import scaffold as scaffold_module

    monkeypatch.setattr(scaffold_module, "plugin_pin", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    result = _repair(root)
    assert result.written == ("mkdocs.yml",)


def test_root_substitution_after_planning_is_a_zero_write_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original = repair_module._plan

    def substitute(*args, **kwargs):
        plan = original(*args, **kwargs)
        moved = root.with_name("original-project")
        root.rename(moved)
        root.mkdir()
        shutil.copy2(moved / "lakefile.toml", root / "lakefile.toml")
        shutil.copy2(moved / "lean-toolchain", root / "lean-toolchain")
        return plan

    monkeypatch.setattr(repair_module, "_plan", substitute)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-race-conflict"
    assert raised.value.written == ()
    assert not (root / "mkdocs.yml").exists()


def test_parent_substitution_at_publish_retains_the_detached_file_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    missing.unlink()
    detached = tmp_path / "detached-blueprint"
    original = repair_module._rename_noreplace

    def substitute(source_parent, source, target_parent, target):
        (root / "blueprint").rename(detached)
        (root / "blueprint/coverage").mkdir(parents=True)
        return original(source_parent, source, target_parent, target)

    monkeypatch.setattr(repair_module, "_rename_noreplace", substitute)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ("blueprint/coverage/README.md",)
    assert (detached / "coverage/README.md").is_file()
    assert not (root / "blueprint/coverage/README.md").exists()


def test_root_open_failure_uses_repair_error_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _project(tmp_path)

    def unavailable(*args, **kwargs):
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "The platform cannot traverse the target safely.",
        )

    monkeypatch.setattr(repair_module, "_open_parent", unavailable)

    assert main(["project", "repair", str(root), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["schema"] == "autoform-project-repair/v1"
    assert result["error"]["code"] == "project-repair-safety-unavailable"
    assert result["error"]["conflicts"][0]["code"] == (
        "project-repair-safety-unavailable"
    )


def test_preflight_io_failure_uses_repair_error_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _project(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("injected preflight failure")

    monkeypatch.setattr(repair_module, "_descriptor_identity", fail)

    assert main(["project", "repair", str(root), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["schema"] == "autoform-project-repair/v1"
    assert result["error"]["code"] == "project-repair-failed"
    assert result["error"]["conflicts"][0]["code"] == "project-repair-io-failed"
    assert result["written"] == []


def test_fifo_concurrent_winner_is_rejected_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original = repair_module._rename_noreplace

    def competing(source_parent, source, target_parent, target):
        if target == "mkdocs.yml":
            repair_module.os.mkfifo(target, dir_fd=target_parent)
        return original(source_parent, source, target_parent, target)

    monkeypatch.setattr(repair_module, "_rename_noreplace", competing)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-recovery-required"
    assert (root / "mkdocs.yml").is_fifo()
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name


def test_configuration_change_during_staging_prevents_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original = repair_module.os.write
    changed = False

    def mutate_configuration(descriptor, content):
        nonlocal changed
        count = original(descriptor, content)
        if not changed:
            changed = True
            (root / "lean-toolchain").write_text(
                "leanprover/lean4:v0.0.0\n", encoding="utf-8"
            )
        return count

    monkeypatch.setattr(repair_module.os, "write", mutate_configuration)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert not (root / "mkdocs.yml").exists()
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name


def test_staging_write_failure_retains_temporary_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    def fail_write(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(repair_module.os, "write", fail_write)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name


def test_retained_temporary_descriptor_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    temporary_descriptor = None

    def fail_write(descriptor, *args, **kwargs):
        nonlocal temporary_descriptor
        temporary_descriptor = descriptor
        raise OSError("injected write failure")

    monkeypatch.setattr(repair_module.os, "write", fail_write)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert temporary_descriptor is not None
    with pytest.raises(OSError) as closed:
        repair_module.os.fstat(temporary_descriptor)
    assert closed.value.errno == errno.EBADF
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name


def test_child_descriptor_is_closed_when_device_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    root_descriptor = repair_module._open_root(root)
    original_fstat = repair_module.os.fstat
    child_descriptor = None

    def fail_child(descriptor):
        nonlocal child_descriptor
        if descriptor != root_descriptor:
            child_descriptor = descriptor
            raise OSError("injected child metadata failure")
        return original_fstat(descriptor)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(repair_module.os, "fstat", fail_child)
            with pytest.raises(OSError):
                repair_module._managed_path_state(
                    root_descriptor, "blueprint/coverage/README.md"
                )
        assert child_descriptor is not None
        with pytest.raises(OSError) as closed:
            original_fstat(child_descriptor)
        assert closed.value.errno == errno.EBADF
    finally:
        repair_module.os.close(root_descriptor)


def test_concurrent_result_closes_descriptor_on_non_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    target = root / "mkdocs.yml"
    item = repair_module._PlannedFile(
        "mkdocs.yml",
        target.read_bytes(),
        stat.S_IMODE(target.stat().st_mode),
    )
    root_descriptor = repair_module._open_root(root)
    original_fstat = repair_module.os.fstat
    winner_descriptor = None

    def interrupt(descriptor):
        nonlocal winner_descriptor
        winner_descriptor = descriptor
        raise KeyboardInterrupt

    try:
        with monkeypatch.context() as patch:
            patch.setattr(repair_module.os, "fstat", interrupt)
            with pytest.raises(KeyboardInterrupt):
                repair_module._concurrent_result(root_descriptor, "mkdocs.yml", item)
        assert winner_descriptor is not None
        with pytest.raises(OSError) as closed:
            original_fstat(winner_descriptor)
        assert closed.value.errno == errno.EBADF
    finally:
        repair_module.os.close(root_descriptor)


def test_winner_close_failure_preserves_validation_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    target = root / "mkdocs.yml"
    target.unlink()
    original_manifest = repair_module._require_file_manifest
    original_close = repair_module.os.close
    winner_descriptor = None
    close_failed = False

    def publish_competitor(source_parent, source, target_parent, name):
        target.write_bytes((root / source).read_bytes())
        target.chmod(0o644)
        raise FileExistsError

    def fail_winner_validation(parent, name, descriptor, identity, item):
        nonlocal winner_descriptor
        if name == "mkdocs.yml":
            winner_descriptor = descriptor
            raise OSError("injected winner validation failure")
        return original_manifest(parent, name, descriptor, identity, item)

    def fail_winner_close(descriptor):
        nonlocal close_failed
        if descriptor == winner_descriptor and not close_failed:
            close_failed = True
            original_close(descriptor)
            raise OSError("injected winner close failure")
        return original_close(descriptor)

    monkeypatch.setattr(repair_module, "_rename_noreplace", publish_competitor)
    monkeypatch.setattr(repair_module, "_require_file_manifest", fail_winner_validation)
    monkeypatch.setattr(repair_module.os, "close", fail_winner_close)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert close_failed
    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert [conflict.code for conflict in raised.value.conflicts] == [
        "project-repair-race-conflict",
        "project-repair-close-failed",
        "project-repair-recovery-required",
    ]
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name
    assert target.is_file()


def test_render_failure_uses_repair_error_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(repair_module, "scaffold_project", fail)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root, dry_run=True)
    assert raised.value.code == "project-repair-failed"
    assert raised.value.conflicts[0].code == "project-repair-render-failed"


def test_unsupported_atomic_publish_uses_repair_error_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    def unavailable(*args, **kwargs):
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "Atomic no-replace publication is unavailable.",
        )

    monkeypatch.setattr(repair_module, "_rename_noreplace", unavailable)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert not (root / "mkdocs.yml").exists()
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name


@pytest.mark.parametrize("capability", ["filesystem", "rename"])
def test_unsupported_publication_is_rejected_before_staging(
    capability: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    if capability == "filesystem":
        monkeypatch.setattr(repair_module, "_filesystem_supported", lambda descriptor: False)
    else:
        monkeypatch.setattr(repair_module, "_noreplace_function", lambda: None)

    dry_run = _repair(root, dry_run=True)
    assert dry_run.planned == ("mkdocs.yml",)

    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-safety-unavailable"
    assert raised.value.written == ()
    assert not (root / "mkdocs.yml").exists()
    assert not list(root.rglob(".*.autoform-repair-*"))


def test_post_publish_fsync_failure_reports_written_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original = repair_module.os.fsync

    def fail_directory(descriptor: int) -> None:
        if stat.S_ISDIR(repair_module.os.fstat(descriptor).st_mode):
            raise OSError("injected")
        original(descriptor)

    monkeypatch.setattr(repair_module.os, "fsync", fail_directory)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-failed"
    assert raised.value.written == ("mkdocs.yml",)
    assert (root / "mkdocs.yml").is_file()


def test_post_publish_close_failure_reports_written_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original_write = repair_module.os.write
    original_close = repair_module.os.close
    staged_descriptor = None
    close_failed = False

    def track_write(descriptor, content):
        nonlocal staged_descriptor
        staged_descriptor = descriptor
        return original_write(descriptor, content)

    def fail_staged_close(descriptor):
        nonlocal close_failed
        if descriptor == staged_descriptor and not close_failed:
            close_failed = True
            original_close(descriptor)
            raise OSError("injected close failure")
        return original_close(descriptor)

    monkeypatch.setattr(repair_module.os, "write", track_write)
    monkeypatch.setattr(repair_module.os, "close", fail_staged_close)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert close_failed
    assert raised.value.code == "project-repair-failed"
    assert raised.value.written == ("mkdocs.yml",)
    assert (root / "mkdocs.yml").is_file()


def test_close_failure_preserves_pending_recovery_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    missing.unlink()
    original_rename = repair_module._rename_noreplace
    original_write = repair_module.os.write
    original_close = repair_module.os.close
    staged_descriptor = None
    close_failed = False

    def make_unsafe(source_parent, source, target_parent, target):
        result = original_rename(source_parent, source, target_parent, target)
        root.chmod(0o777)
        return result

    def track_write(descriptor, content):
        nonlocal staged_descriptor
        staged_descriptor = descriptor
        return original_write(descriptor, content)

    def fail_staged_close(descriptor):
        nonlocal close_failed
        if descriptor == staged_descriptor and not close_failed:
            close_failed = True
            original_close(descriptor)
            raise OSError("injected close failure")
        return original_close(descriptor)

    monkeypatch.setattr(repair_module, "_rename_noreplace", make_unsafe)
    monkeypatch.setattr(repair_module.os, "write", track_write)
    monkeypatch.setattr(repair_module.os, "close", fail_staged_close)
    try:
        with pytest.raises(ProjectRepairError) as raised:
            repair_project(root)
        assert close_failed
        assert raised.value.code == "project-repair-recovery-required"
        assert raised.value.written == ("blueprint/coverage/README.md",)
        assert {conflict.code for conflict in raised.value.conflicts} == {
            "project-repair-recovery-required",
            "project-repair-close-failed",
        }
        assert missing.is_file()
    finally:
        root.chmod(0o755)


def test_root_close_failure_reports_files_already_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original_open_root = repair_module._open_root
    original_close = repair_module.os.close
    root_descriptor = None
    close_failed = False

    def capture_root(path):
        nonlocal root_descriptor
        root_descriptor = original_open_root(path)
        return root_descriptor

    def fail_root_close(descriptor):
        nonlocal close_failed
        if descriptor == root_descriptor and not close_failed:
            close_failed = True
            original_close(descriptor)
            raise OSError("injected root close failure")
        return original_close(descriptor)

    monkeypatch.setattr(repair_module, "_open_root", capture_root)
    monkeypatch.setattr(repair_module.os, "close", fail_root_close)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert close_failed
    assert raised.value.code == "project-repair-failed"
    assert raised.value.written == ("mkdocs.yml",)
    assert raised.value.conflicts[-1].code == "project-repair-close-failed"
    assert (root / "mkdocs.yml").is_file()


def test_dry_run_rejects_same_unsafe_root_as_apply(tmp_path: Path) -> None:
    root = _project(tmp_path)
    root.chmod(0o777)
    try:
        with pytest.raises(ProjectRepairError) as raised:
            _repair(root, dry_run=True)
        assert any(
            conflict.code == "project-repair-parent-unsafe"
            for conflict in raised.value.conflicts
        )
    finally:
        root.chmod(0o755)


def test_cli_json_reports_dry_run_and_conflicts(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    assert main(
        [
            "project",
            "repair",
            str(root),
            "--title",
            "Project",
            "--repository-url",
            "",
            "--dry-run",
            "--json",
        ]
    ) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["schema"] == "autoform-project-repair/v1"
    assert dry_run["planned"] == ["mkdocs.yml"]
    assert dry_run["written"] == []

    (root / "blueprint/roadmap/README.md").unlink()
    (root / "blueprint/roadmap").rmdir()
    (root / "blueprint/roadmap").write_bytes(b"blocker\n")
    assert main(
        [
            "project",
            "repair",
            str(root),
            "--title",
            "Project",
            "--repository-url",
            "",
            "--json",
        ]
    ) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["error"]["code"] == "project-repair-conflict"
    assert failed["written"] == []


def test_cli_text_error_reports_files_already_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _project(tmp_path)
    (root / "blueprint/coverage/README.md").unlink()
    (root / "mkdocs.yml").unlink()
    original = repair_module._publish

    def fail_second(root, root_descriptor, root_identity, item, inspection):
        if item.path == "mkdocs.yml":
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "injected-repair-failure",
                        "Injected failure after an earlier publication.",
                        item.path,
                    ),
                ),
                code="project-repair-failed",
            )
        return original(root, root_descriptor, root_identity, item, inspection)

    monkeypatch.setattr(repair_module, "_publish", fail_second)
    assert (
        main(
            [
                "project",
                "repair",
                str(root),
                "--title",
                "Project",
                "--repository-url",
                "",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "files already published:" in captured.err
    assert "blueprint/coverage/README.md" in captured.err


def test_missing_parameterized_file_requires_exact_inputs(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root)

    assert raised.value.written == ()
    assert [
        (conflict.code, conflict.path) for conflict in raised.value.conflicts
    ] == [("project-repair-input-required", "mkdocs.yml")]
    assert not (root / "mkdocs.yml").exists()


def test_explicit_empty_repository_url_is_not_omission(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    with pytest.raises(ProjectRepairError):
        repair_project(root, title="Project")
    result = repair_project(root, title="Project", repository_url="")

    assert result.written == ("mkdocs.yml",)
    assert b'repo_url: ""' in (root / "mkdocs.yml").read_bytes()


def test_unpinned_project_can_repair_static_files_without_workflow_inputs(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    missing.unlink()

    result = repair_project(root)

    assert result.written == ("blueprint/coverage/README.md",)
    assert not (root / ".github").exists()


def test_partial_workflow_state_requires_explicit_provenance(tmp_path: Path) -> None:
    root = _project(tmp_path)
    workflows = root / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "autoform-verify.yml").write_text("authored\n", encoding="utf-8")

    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root)

    assert raised.value.written == ()
    assert any(
        conflict.code == "project-repair-input-required"
        and conflict.path == ".github/workflows/blueprint-pages.yml"
        for conflict in raised.value.conflicts
    )
    assert not (root / ".github/autoform_audit.py").exists()


@pytest.mark.parametrize("source,ref", [("", ""), ("", "0" * 40)])
def test_blank_workflow_provenance_is_rejected(
    tmp_path: Path, source: str, ref: str
) -> None:
    root = _project(tmp_path)

    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root, autoform_source=source, autoform_ref=ref)

    assert raised.value.code == "project-repair-input-invalid"
    assert raised.value.written == ()


def test_reserved_temporary_file_requires_manual_recovery(tmp_path: Path) -> None:
    root = _project(tmp_path)
    orphan = root / ".mkdocs.yml.autoform-repair-0123456789abcdef"
    orphan.write_bytes(b"unverified\n")
    before = _files(root)

    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root)

    assert raised.value.written == ()
    assert any(
        conflict.code == "project-repair-recovery-required"
        and conflict.path == orphan.name
        for conflict in raised.value.conflicts
    )
    assert _files(root) == before


def test_ancestor_symlink_target_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(root.parent, target_is_directory=True)

    with pytest.raises(ProjectRepairError) as raised:
        repair_project(alias / root.name)

    assert raised.value.written == ()
    assert raised.value.conflicts[0].code == "project-repair-target-invalid"


def test_root_substitution_inside_publish_retains_detached_file_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    missing.unlink()
    detached = tmp_path / "detached-project"
    original = repair_module._rename_noreplace

    def substitute(source_parent, source, target_parent, target):
        root.rename(detached)
        root.mkdir()
        shutil.copy2(detached / "lakefile.toml", root / "lakefile.toml")
        shutil.copy2(detached / "lean-toolchain", root / "lean-toolchain")
        return original(source_parent, source, target_parent, target)

    monkeypatch.setattr(repair_module, "_rename_noreplace", substitute)
    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ("blueprint/coverage/README.md",)
    assert (detached / "blueprint/coverage/README.md").is_file()
    assert not (root / "blueprint/coverage/README.md").exists()


def test_root_permission_change_at_publish_retains_file_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    missing.unlink()
    original = repair_module._rename_noreplace
    original_unlink = repair_module.os.unlink
    published = False

    def make_unsafe(source_parent, source, target_parent, target):
        nonlocal published
        result = original(source_parent, source, target_parent, target)
        published = True
        root.chmod(0o777)
        return result

    def reject_published_unlink(path, *args, **kwargs):
        if published and path == "README.md" and kwargs.get("dir_fd") is not None:
            raise AssertionError("published recovery path must not be unlinked by name")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(repair_module, "_rename_noreplace", make_unsafe)
    monkeypatch.setattr(repair_module.os, "unlink", reject_published_unlink)
    try:
        with pytest.raises(ProjectRepairError) as raised:
            repair_project(root)
        assert raised.value.code == "project-repair-recovery-required"
        assert raised.value.written == ("blueprint/coverage/README.md",)
        assert missing.is_file()
    finally:
        root.chmod(0o755)


def test_temporary_content_mutation_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    missing.unlink()
    original = repair_module._rename_noreplace

    def mutate(source_parent, source, target_parent, target):
        descriptor = repair_module.os.open(
            source, repair_module.os.O_WRONLY | repair_module.os.O_TRUNC, dir_fd=source_parent
        )
        try:
            repair_module.os.write(descriptor, b"foreign bytes\n")
        finally:
            repair_module.os.close(descriptor)
        return original(source_parent, source, target_parent, target)

    monkeypatch.setattr(repair_module, "_rename_noreplace", mutate)
    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ("blueprint/coverage/README.md",)
    assert missing.read_bytes() == b"foreign bytes\n"


def test_concurrent_winner_replacement_is_not_reported_as_converged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    expected = missing.read_bytes()
    missing.unlink()
    original_rename = repair_module._rename_noreplace
    original_result = repair_module._concurrent_result

    def competing(source_parent, source, target_parent, target):
        descriptor = repair_module.os.open(
            target,
            repair_module.os.O_WRONLY
            | repair_module.os.O_CREAT
            | repair_module.os.O_EXCL,
            0o644,
            dir_fd=target_parent,
        )
        try:
            repair_module.os.write(descriptor, expected)
        finally:
            repair_module.os.close(descriptor)
        return original_rename(source_parent, source, target_parent, target)

    def replace_after_read(parent_descriptor, name, item):
        result = original_result(parent_descriptor, name, item)
        repair_module.os.unlink(name, dir_fd=parent_descriptor)
        descriptor = repair_module.os.open(
            name,
            repair_module.os.O_WRONLY
            | repair_module.os.O_CREAT
            | repair_module.os.O_EXCL,
            0o644,
            dir_fd=parent_descriptor,
        )
        try:
            repair_module.os.write(descriptor, b"foreign bytes\n")
        finally:
            repair_module.os.close(descriptor)
        return result

    monkeypatch.setattr(repair_module, "_rename_noreplace", competing)
    monkeypatch.setattr(repair_module, "_concurrent_result", replace_after_read)
    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert missing.read_bytes() == b"foreign bytes\n"
    temporary, = (root / "blueprint/coverage").glob(
        ".README.md.autoform-repair-*"
    )
    assert raised.value.conflicts[-1].path == temporary.relative_to(root).as_posix()


def test_parameter_map_covers_every_scaffold_placeholder() -> None:
    from autoform_cli import scaffold as scaffold_module

    input_for_placeholder = {
        "PROJECT_TITLE": "title",
        "PROJECT_TITLE_YAML": "title",
        "REPO_URL_YAML": "repository-url",
        "AUTOFORM_SOURCE_YAML": "autoform-source",
        "AUTOFORM_REF_YAML": "autoform-ref",
    }
    found = set()
    for template in scaffold_module._TEMPLATES.rglob("*"):
        relative_path = template.relative_to(scaffold_module._TEMPLATES)
        if (
            not template.is_file()
            or "__pycache__" in relative_path.parts
            or template.suffix == ".pyc"
        ):
            continue
        relative = relative_path.as_posix()
        destination = scaffold_module._destination(relative)
        for match in scaffold_module._TEMPLATE_PLACEHOLDER.finditer(
            template.read_text(encoding="utf-8")
        ):
            placeholder = match.group("name")
            found.add(placeholder)
            assert input_for_placeholder[placeholder] in repair_module._REQUIRED_INPUTS[
                destination
            ]

    assert found == set(input_for_placeholder)
