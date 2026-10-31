"""Guards in ``autoform_cli._managed_output`` that the publishing commands' tests do not reach."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from autoform_cli._managed_output import _install_output, output_identity, replace_outputs, stage_output
from autoform_cli.lean_probe import SkeletonError


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions that bind the process")
def test_output_identity_refuses_a_directory_it_cannot_list(tmp_path: Path) -> None:
    tree = tmp_path / "packets"
    hidden = tree / "sub"
    hidden.mkdir(parents=True)
    (hidden / "packet.md").write_text("old\n", encoding="utf-8")
    hidden.chmod(0)
    try:
        # Path.rglob skips such a directory, so an edit under it would go unseen.
        with pytest.raises(SkeletonError, match="cannot inspect"):
            output_identity(tree)
    finally:
        hidden.chmod(0o755)


def _fail_fsync(descriptor: int) -> None:
    raise OSError("simulated fsync failure")


def _fail_chmod(self: Path, mode: int, **kwargs: object) -> None:
    raise OSError("simulated chmod failure")


@pytest.mark.parametrize("step", ["write", "fsync", "chmod"])
def test_stage_output_removes_a_stage_it_cannot_finish(tmp_path: Path, monkeypatch, step: str) -> None:
    destination = tmp_path / "report.json"
    destination.write_text("old\n", encoding="utf-8")
    # A lone surrogate cannot be encoded as UTF-8, so the write itself fails.
    content = "\ud800" if step == "write" else "new\n"
    if step == "fsync":
        monkeypatch.setattr("autoform_cli._managed_output.os.fsync", _fail_fsync)
    if step == "chmod":
        monkeypatch.setattr(Path, "chmod", _fail_chmod)

    with pytest.raises((OSError, UnicodeEncodeError)):
        stage_output(destination, content)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.json"]
    assert destination.read_text(encoding="utf-8") == "old\n"


def test_stage_output_leaves_a_registered_stage_to_its_owner(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "report.json"
    monkeypatch.setattr("autoform_cli._managed_output.os.fsync", _fail_fsync)
    stages: list[Path] = []

    with pytest.raises(OSError, match="simulated fsync failure"):
        stage_output(destination, "new\n", stages=stages)

    assert len(stages) == 1
    assert stages[0].is_file()


def test_rollback_preserves_output_that_changed_after_its_last_check(tmp_path: Path, monkeypatch) -> None:
    packets, report = tmp_path / "packets", tmp_path / "report.json"
    packet_stage = stage_output(packets)
    (packet_stage / "packet.md").write_text("new\n", encoding="utf-8")
    report_stage = stage_output(report, "{}\n")
    replace = os.replace

    def fail_report_install(stage: Path, destination: Path) -> None:
        if destination == report:
            raise OSError("simulated report install failure")
        _install_output(stage, destination)

    def write_before_quarantine(source: Path, destination: Path) -> None:
        # Another writer adds a file after rollback checked the published tree
        # and before it moves the tree aside.
        if "autoform-rollback" in Path(destination).name:
            (Path(source) / "concurrent.md").write_text("theirs\n", encoding="utf-8")
        replace(source, destination)

    monkeypatch.setattr("autoform_cli._managed_output._install_output", fail_report_install)
    monkeypatch.setattr("autoform_cli._managed_output.os.replace", write_before_quarantine)

    with pytest.raises(SkeletonError, match="changed rolled-back output was preserved for recovery"):
        replace_outputs([(packets, packet_stage, None), (report, report_stage, None)])

    assert not packets.exists()
    quarantines = list(tmp_path.glob(".packets.autoform-rollback-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "concurrent.md").read_text(encoding="utf-8") == "theirs\n"
