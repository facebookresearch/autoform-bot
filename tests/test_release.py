"""The release version: one number that every manifest, the lock and the changelog carry."""

from __future__ import annotations

import importlib.metadata
import json
import re
from pathlib import Path

import pytest
import tomli

from autoform_cli import debrief_orchestrator
from autoform_cli.__main__ import main


def _project_version(repo_root: Path) -> tuple[str, str]:
    project = tomli.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return project["name"], project["version"]


def test_every_manifest_and_the_lock_name_the_project_version(repo_root: Path) -> None:
    """pyproject.toml sets the version, and the lock, the Claude and Muse
    manifests, and the base of Codex's cachebuster version must name the same
    one, or a release installs a plugin that reports another."""

    name, version = _project_version(repo_root)
    lock = tomli.loads((repo_root / "uv.lock").read_text(encoding="utf-8"))
    manifests = {
        "uv.lock": next(package["version"] for package in lock["package"] if package["name"] == name),
        ".claude-plugin/plugin.json": json.loads((repo_root / ".claude-plugin/plugin.json").read_text())["version"],
        ".muse-plugin/plugin.json": json.loads((repo_root / ".muse-plugin/plugin.json").read_text())["version"],
        ".codex-plugin/plugin.json": json.loads((repo_root / ".codex-plugin/plugin.json").read_text())["version"].split(
            "+", 1
        )[0],
    }

    assert manifests == dict.fromkeys(manifests, version)


def test_the_readme_installs_the_release_tag(repo_root: Path) -> None:
    """Each installer in the README pins the release's tag, so a release that
    leaves the README behind would install another version for new users."""

    tag = f"v{_project_version(repo_root)[1]}"
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    installation = readme.split("\n## Installation\n", 1)[1].split("\n## ", 1)[0]
    commands = "".join(re.findall(r"^```bash\n(.*?)^```$", installation, re.M | re.S)).splitlines()
    for command in (
        f"claude plugin marketplace add facebookresearch/autoform-bot@{tag}",
        f"codex plugin marketplace add facebookresearch/autoform-bot --ref {tag}",
        f"muse plugins marketplace add autoform 'https://github.com/facebookresearch/autoform-bot.git#{tag}'",
    ):
        assert command in commands, command


def test_the_changelog_names_the_project_version_as_its_newest_release(repo_root: Path) -> None:
    """A release moves Unreleased under a heading naming the new version, so a
    bump that leaves its changes under Unreleased fails here."""

    changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    releases = re.findall(r"^## (?!Unreleased$)(\S+)", changelog, re.MULTILINE)

    assert releases[:1] == [_project_version(repo_root)[1]]


@pytest.mark.parametrize(
    ("installed", "commit", "printed"),
    [
        ("0.6.0", "745a88599469", "autoform 0.6.0 (745a88599469)"),
        ("0.6.0", None, "autoform 0.6.0"),
        (None, None, "autoform unknown"),
    ],
)
def test_version_names_the_installed_release_and_the_checkout_commit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    installed: str | None,
    commit: str | None,
    printed: str,
) -> None:
    """`--version` prints what a debrief records: the installed version, or
    "unknown" outside an installed release, and a plugin checkout's commit."""

    def version(name: str) -> str:
        if installed is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(importlib.metadata, "version", version)
    monkeypatch.setattr(debrief_orchestrator, "_git", lambda root, *arguments: commit)
    with pytest.raises(SystemExit) as exited:
        main(["--version"])

    assert exited.value.code == 0
    assert capsys.readouterr().out == printed + "\n"


def test_the_version_is_read_only_for_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def unread() -> str:
        raise AssertionError("reading the version runs git, so only --version may read it")

    monkeypatch.setattr(debrief_orchestrator, "autoform_version", unread)
    with pytest.raises(SystemExit) as exited:
        main(["--help"])

    assert exited.value.code == 0
