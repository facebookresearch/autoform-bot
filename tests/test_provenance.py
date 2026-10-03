from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autoform_cli import provenance


SOURCE = "https://example.test/owner/autoform.git"
REVISION = "1" * 40


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.test",
            "-c",
            "user.name=Test",
            *arguments,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _checkout(root: Path, *, source: str = SOURCE) -> str:
    root.mkdir(parents=True)
    (root / "autoform_cli").mkdir()
    (root / "autoform_cli/__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "source")
    _git(root, "remote", "add", "origin", source)
    return _git(root, "rev-parse", "HEAD")


def _write_codex_record(
    root: Path,
    *,
    source: str = SOURCE,
    revision: str = REVISION,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / provenance.INSTALL_RECORD).write_text(
        json.dumps(
            {
                "ref_name": "main",
                "revision": revision,
                "source": source,
                "source_type": "git",
                "sparse_paths": [],
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (SOURCE, SOURCE),
        ("https://EXAMPLE.test/owner/autoform.git", SOURCE),
        ("https://example.test/owner/autoform", None),
        ("git@github.com:owner/autoform.git", None),
        ("https://user@example.test/owner/autoform.git", None),
        ("https://example.test/owner/../autoform.git", None),
        ("https://example.test/owner/autoform.git?token=secret", None),
        ("file:///tmp/autoform.git", None),
    ],
)
def test_normalize_git_source(source: str, expected: str | None) -> None:
    assert provenance.normalize_git_source(source) == expected


def test_normalize_checkout_source_adds_suffix_and_converts_github_scp() -> None:
    assert provenance.normalize_git_source(
        "git@github.com:owner/autoform",
        allow_github_scp=True,
        add_git_suffix=True,
    ) == "https://github.com/owner/autoform.git"


def test_clean_checkout_records_its_origin_and_head(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    revision = _checkout(root)

    assert provenance.resolve_plugin_provenance(root) == provenance.PluginProvenance(
        SOURCE,
        revision,
    )


def test_checkout_rejects_tracked_changes(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    _checkout(root)
    (root / "autoform_cli/__init__.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(provenance.ProvenanceError, match="tracked changes"):
        provenance.resolve_plugin_provenance(root)


def test_checkout_disables_executable_fsmonitor_configuration(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    _checkout(root)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(root, "config", "core.fsmonitor", str(hook))

    provenance.resolve_plugin_provenance(root)

    assert not marker.exists()


def test_checkout_rejects_linked_git_metadata(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    _checkout(root)
    metadata = tmp_path / "metadata"
    (root / ".git").rename(metadata)
    (root / ".git").symlink_to(metadata, target_is_directory=True)

    with pytest.raises(provenance.ProvenanceError, match="checkout metadata"):
        provenance.resolve_plugin_provenance(root)


def test_checkout_ignores_untracked_host_and_test_state(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    revision = _checkout(root)
    (root / ".claude").mkdir()
    (root / ".claude/settings.local.json").write_text("{}\n", encoding="utf-8")
    cache = root / "tests/__pycache__"
    cache.mkdir(parents=True)
    (cache / "test_example.cpython-313-pytest-9.1.0.pyc").write_bytes(b"cache")

    assert provenance.resolve_plugin_provenance(root).revision == revision


def test_codex_record_resolves_without_git_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "installed"
    _write_codex_record(root)
    monkeypatch.setattr(
        provenance.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("installer records must not trigger Git or network"),
    )

    assert provenance.resolve_plugin_provenance(root) == provenance.PluginProvenance(
        SOURCE,
        REVISION,
    )


def test_generic_build_sidecar_is_supported(tmp_path: Path) -> None:
    root = tmp_path / "installed"
    root.mkdir()
    (root / provenance.PROVENANCE_RECORD).write_text(
        json.dumps({"source": SOURCE, "revision": REVISION, "version": "0.5.0"}),
        encoding="utf-8",
    )

    assert provenance.resolve_plugin_provenance(root).as_dict() == {
        "ok": True,
        "revision": REVISION,
        "source": SOURCE,
    }


def test_records_must_agree(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    _checkout(root)
    _write_codex_record(root, revision=REVISION)

    with pytest.raises(provenance.ProvenanceError, match="conflict"):
        provenance.resolve_plugin_provenance(root)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"source_type": "directory", "source": SOURCE, "revision": REVISION},
        {"source_type": "git", "source": "file:///tmp/repo", "revision": REVISION},
        {"source_type": "git", "source": SOURCE, "revision": "main"},
    ],
)
def test_invalid_codex_record_fails_closed(tmp_path: Path, payload: dict[str, str]) -> None:
    root = tmp_path / "installed"
    root.mkdir()
    (root / provenance.INSTALL_RECORD).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(provenance.ProvenanceError, match="Codex installation record"):
        provenance.resolve_plugin_provenance(root)


def test_install_record_cannot_be_a_symlink(tmp_path: Path) -> None:
    root = tmp_path / "installed"
    root.mkdir()
    outside = tmp_path / "record.json"
    outside.write_text(
        json.dumps({"source_type": "git", "source": SOURCE, "revision": REVISION}),
        encoding="utf-8",
    )
    (root / provenance.INSTALL_RECORD).symlink_to(outside)

    with pytest.raises(provenance.ProvenanceError, match="Codex installation record"):
        provenance.resolve_plugin_provenance(root)


def _claude_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    build_commit: str | None = None,
) -> Path:
    config = tmp_path / "claude"
    plugins = config / "plugins"
    checkout = tmp_path / "marketplace-checkout"
    _checkout(checkout)
    installed = plugins / "cache/market/autoform/0.5.0-publisher.arbitrary"
    installed.mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "autoform@market": [
                        {
                            "gitCommitSha": REVISION,
                            "installPath": str(installed),
                            "version": "0.5.0+publisher.arbitrary",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (plugins / "known_marketplaces.json").write_text(
        json.dumps({"market": {"installLocation": str(checkout)}}),
        encoding="utf-8",
    )
    if build_commit is not None:
        (installed / "BUILD_COMMIT").write_text(f"{build_commit}\n", encoding="ascii")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return installed


def test_claude_uses_registry_revision_without_a_personal_version_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = _claude_install(tmp_path, monkeypatch, build_commit=REVISION)

    assert provenance.resolve_plugin_provenance(installed) == provenance.PluginProvenance(
        SOURCE,
        REVISION,
    )


def test_claude_rejects_a_conflicting_optional_build_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = _claude_install(tmp_path, monkeypatch, build_commit="2" * 40)

    with pytest.raises(provenance.ProvenanceError, match="conflicts"):
        provenance.resolve_plugin_provenance(installed)


def test_missing_provenance_is_an_explicit_error(tmp_path: Path) -> None:
    with pytest.raises(provenance.ProvenanceError, match="No immutable"):
        provenance.resolve_plugin_provenance(tmp_path)


def test_invalid_claude_configuration_path_uses_the_stable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "invalid\npath")

    with pytest.raises(provenance.ProvenanceError, match="configuration directory"):
        provenance.resolve_plugin_provenance(tmp_path)


def test_plugin_pin_keeps_legacy_all_or_nothing_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provenance, "plugin_root", lambda: tmp_path)

    assert provenance.plugin_pin() == ("", "")


def test_cli_reports_recorded_identity_without_install_verification(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from autoform_cli import __main__ as cli

    monkeypatch.setattr(
        cli,
        "resolve_plugin_provenance",
        lambda: provenance.PluginProvenance(SOURCE, REVISION),
    )

    assert cli.main(["project", "provenance", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "revision": REVISION,
        "source": SOURCE,
    }


def test_cli_reports_recorded_identity_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from autoform_cli import __main__ as cli

    def unavailable() -> provenance.PluginProvenance:
        raise provenance.ProvenanceError("unavailable")

    monkeypatch.setattr(cli, "resolve_plugin_provenance", unavailable)

    assert cli.main(["project", "provenance", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": {
            "code": "project-provenance-unavailable",
            "message": "unavailable",
        },
        "ok": False,
    }
