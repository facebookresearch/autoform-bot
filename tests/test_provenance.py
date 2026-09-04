from __future__ import annotations

import json
import marshal
import os
import py_compile
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from autoform_cli import provenance


_SOURCE = "https://example.test/owner/autoform.git"
_REVISION = "1" * 40


def _write_plugin(root: Path) -> None:
    files = {
        ".claude-plugin/plugin.json": b"{}\n",
        ".codex-plugin/plugin.json": b"{}\n",
        ".muse-plugin/plugin.json": b"{}\n",
        ".mcp.json": b"{}\n",
        "assets/payload.txt": b"payload\n",
        "autoform_cli/__init__.py": b"VALUE = 1\n",
        "servers/__init__.py": b"SERVER = 1\n",
        "skills/setup/SKILL.md": b"# Setup\n",
        "uv.lock": b"version = 1\n",
        "pyproject.toml": (
            b"[project]\n"
            b'name = "autoform"\n'
            b"[project.scripts]\n"
            b'autoform = "autoform_cli.__main__:main"\n'
            b"[tool.hatch.build.targets.wheel]\n"
            b'packages = ["autoform_cli", "servers"]\n'
        ),
    }
    for relative, content in files.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def _layout(root: Path) -> provenance._SourceLayout:
    roots = tuple(sorted((*provenance._SHIPPED_ROOTS, "autoform_cli", "servers")))
    files: dict[str, provenance._ManifestEntry] = {}
    all_files: set[str] = set()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if ".git" in path.relative_to(root).parts or not path.is_file():
            continue
        all_files.add(relative)
        if relative in provenance._SHIPPED_FILES or any(
            provenance._under_root(relative, shipped_root) for shipped_root in roots
        ):
            mode = 0o100755 if path.stat().st_mode & 0o111 else 0o100644
            files[relative] = provenance._ManifestEntry(mode=mode, content=path.read_bytes())
    return provenance._SourceLayout(
        files=files,
        all_files=frozenset(all_files),
        roots=roots,
        package_roots=("autoform_cli", "servers"),
    )


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


def _checkout(root: Path) -> tuple[str, provenance._SourceLayout]:
    _write_plugin(root)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "source")
    _git(root, "remote", "add", "origin", _SOURCE)
    return _git(root, "rev-parse", "HEAD"), _layout(root)


def _write_record(
    root: Path,
    *,
    source: str = _SOURCE,
    revision: str = _REVISION,
    ref_name: str = "main",
    sparse_paths: object = (),
) -> None:
    (root / provenance.INSTALL_RECORD).write_text(
        json.dumps(
            {
                "ref_name": ref_name,
                "revision": revision,
                "source": source,
                "source_type": "git",
                "sparse_paths": list(sparse_paths) if isinstance(sparse_paths, tuple) else sparse_paths,
            }
        ),
        encoding="utf-8",
    )


def _installed_copy(tmp_path: Path) -> tuple[Path, provenance._SourceLayout]:
    source = tmp_path / "source"
    _write_plugin(source)
    layout = _layout(source)
    installed = tmp_path / "installed"
    shutil.copytree(source, installed)
    _write_record(installed)
    return installed, layout


def _mock_fetch(
    monkeypatch: pytest.MonkeyPatch,
    layout: provenance._SourceLayout,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def fetch(source: str, revision: str, scratch: Path) -> provenance._SourceLayout:
        assert scratch.is_dir()
        calls.append((source, revision))
        return layout

    monkeypatch.setattr(provenance, "_fetch_source_layout", fetch)
    return calls


def test_verifies_an_exact_clean_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "checkout"
    revision, layout = _checkout(root)
    calls = _mock_fetch(monkeypatch, layout)

    result = provenance.verify_plugin_provenance(root)

    assert result.source == _SOURCE
    assert result.revision == revision
    assert calls == [(_SOURCE, revision)]


def test_verifies_a_copied_install_from_the_codex_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    calls = _mock_fetch(monkeypatch, layout)

    result = provenance.verify_plugin_provenance(root)

    assert result.source == _SOURCE
    assert result.revision == _REVISION
    assert calls == [(_SOURCE, _REVISION)]


def test_enclosing_consumer_checkout_is_not_plugin_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumer = tmp_path / "consumer"
    plugin = consumer / ".venv/lib/python3.13/site-packages/autoform"
    _write_plugin(plugin)
    _git(consumer, "init", "-q")
    _git(consumer, "add", ".")
    _git(consumer, "commit", "-q", "-m", "consumer")
    _git(consumer, "remote", "add", "origin", "https://example.test/consumer.git")

    def forbidden(*args: object, **kwargs: object) -> provenance._SourceLayout:
        raise AssertionError("an enclosing checkout reached remote verification")

    monkeypatch.setattr(provenance, "_fetch_source_layout", forbidden)
    with pytest.raises(provenance.ProvenanceError, match="No trustworthy"):
        provenance.verify_plugin_provenance(plugin)


def test_checkout_and_record_must_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "checkout"
    _, layout = _checkout(root)
    _write_record(root, revision="2" * 40)
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="conflict"):
        provenance.verify_plugin_provenance(root)


def test_checkout_and_record_can_jointly_attest_the_same_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "checkout"
    revision, layout = _checkout(root)
    _write_record(root, revision=revision, ref_name=revision.upper())
    _mock_fetch(monkeypatch, layout)

    result = provenance.verify_plugin_provenance(root)

    assert result == provenance.PluginProvenance(_SOURCE, revision)


def test_malformed_present_record_invalidates_an_otherwise_valid_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "checkout"
    _, layout = _checkout(root)
    (root / provenance.INSTALL_RECORD).write_text("{}", encoding="utf-8")
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="record"):
        provenance.verify_plugin_provenance(root)


def test_dirty_checkout_is_not_attested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "checkout"
    _, layout = _checkout(root)
    (root / "autoform_cli/__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="installed Autoform"):
        provenance.verify_plugin_provenance(root)


def test_mutation_after_an_earlier_boundary_scan_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    _mock_fetch(monkeypatch, layout)
    original = provenance._scan_boundary_directory
    mutated = False

    def mutate_between_roots(descriptor, prefix, **kwargs):
        nonlocal mutated
        if prefix == "servers" and not mutated:
            mutated = True
            (root / "autoform_cli/__init__.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
        return original(descriptor, prefix, **kwargs)

    monkeypatch.setattr(provenance, "_scan_boundary_directory", mutate_between_roots)

    with pytest.raises(provenance.ProvenanceError, match="installed Autoform"):
        provenance.verify_plugin_provenance(root)


@pytest.mark.parametrize("change", ["modified", "missing", "extra", "mode", "direct-pyc"])
def test_shipped_or_importable_drift_is_rejected(
    change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    source = root / "autoform_cli/__init__.py"
    if change == "modified":
        source.write_text("VALUE = 2\n", encoding="utf-8")
    elif change == "missing":
        source.unlink()
    elif change == "extra":
        (root / "injected.py").write_text("VALUE = 2\n", encoding="utf-8")
    elif change == "mode":
        source.chmod(0o755)
    else:
        py_compile.compile(
            os.fspath(source),
            cfile=os.fspath(source.with_suffix(".pyc")),
            doraise=True,
        )
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="installed Autoform"):
        provenance.verify_plugin_provenance(root)


def test_symlink_in_the_shipped_boundary_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    payload = root / "assets/payload.txt"
    payload.unlink()
    payload.symlink_to(root / "uv.lock")
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="link or special file"):
        provenance.verify_plugin_provenance(root)


def test_recognized_derived_state_and_non_importable_files_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    (root / "NOTES.txt").write_text("local note\n", encoding="utf-8")
    (root / ".venv/lib/python3.13/site-packages").mkdir(parents=True)
    (root / ".venv/lib/python3.13/site-packages/injected.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    _mock_fetch(monkeypatch, layout)

    assert provenance.verify_plugin_provenance(root).revision == _REVISION


@pytest.mark.parametrize("optimization", [0, 1, 2])
def test_current_interpreter_bytecode_is_accepted_only_when_it_matches_source(
    optimization: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    source = root / "autoform_cli/__init__.py"
    cached = Path(
        py_compile.compile(
            os.fspath(source), doraise=True, optimize=optimization
        )
    )
    _mock_fetch(monkeypatch, layout)

    assert provenance.verify_plugin_provenance(root).revision == _REVISION

    content = cached.read_bytes()
    malicious = compile(
        b"VALUE = 9\n",
        os.fspath(source),
        "exec",
        dont_inherit=True,
        optimize=optimization,
    )
    cached.write_bytes(content[:16] + marshal.dumps(malicious))
    with pytest.raises(provenance.ProvenanceError, match="bytecode cache"):
        provenance.verify_plugin_provenance(root)


def test_stale_interpreter_cache_is_ignored_only_with_verified_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    cache = root / "autoform_cli/__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-999.pyc").write_bytes(b"not executable here")
    _mock_fetch(monkeypatch, layout)

    assert provenance.verify_plugin_provenance(root).revision == _REVISION

    (root / "autoform_cli/__init__.py").unlink()
    with pytest.raises(provenance.ProvenanceError):
        provenance.verify_plugin_provenance(root)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "source_type": "archive",
            "source": _SOURCE,
            "revision": _REVISION,
            "ref_name": "main",
            "sparse_paths": [],
        },
        {
            "source_type": "git",
            "source": "https://user:secret@example.test/autoform.git",
            "revision": _REVISION,
            "ref_name": "main",
            "sparse_paths": [],
        },
        {
            "source_type": "git",
            "source": _SOURCE,
            "revision": "1" * 12,
            "ref_name": "main",
            "sparse_paths": [],
        },
        {
            "source_type": "git",
            "source": _SOURCE,
            "revision": _REVISION,
            "ref_name": "2" * 40,
            "sparse_paths": [],
        },
        {
            "source_type": "git",
            "source": _SOURCE,
            "revision": _REVISION,
            "ref_name": "main",
            "sparse_paths": "skills",
        },
    ],
)
def test_malformed_codex_records_fail_before_remote_access(
    payload: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _installed_copy(tmp_path)
    (root / provenance.INSTALL_RECORD).write_text(json.dumps(payload), encoding="utf-8")

    def forbidden(*args: object, **kwargs: object) -> provenance._SourceLayout:
        raise AssertionError("malformed record reached remote verification")

    monkeypatch.setattr(provenance, "_fetch_source_layout", forbidden)
    with pytest.raises(provenance.ProvenanceError):
        provenance.verify_plugin_provenance(root)


@pytest.mark.parametrize("kind", ["duplicate", "oversized", "symlink"])
def test_untrusted_record_file_shapes_are_rejected(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _installed_copy(tmp_path)
    record = root / provenance.INSTALL_RECORD
    if kind == "duplicate":
        record.write_text(
            '{"source_type":"git","source":"one","source":"two"}',
            encoding="utf-8",
        )
    elif kind == "oversized":
        record.write_bytes(b" " * (provenance.MAX_INSTALL_RECORD_BYTES + 1))
    else:
        outside = tmp_path / "record.json"
        outside.write_text("{}", encoding="utf-8")
        record.unlink()
        record.symlink_to(outside)

    monkeypatch.setattr(
        provenance,
        "_fetch_source_layout",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("remote access")),
    )
    with pytest.raises(provenance.ProvenanceError, match="record"):
        provenance.verify_plugin_provenance(root)


def test_unreachable_revision_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _installed_copy(tmp_path)

    def unreachable(*args: object, **kwargs: object) -> provenance._SourceLayout:
        raise provenance._GitFailure

    monkeypatch.setattr(provenance, "_fetch_source_layout", unreachable)
    with pytest.raises(provenance.ProvenanceError, match="could not be verified"):
        provenance.verify_plugin_provenance(root)


def test_git_environment_removes_every_inherited_git_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/tmp/foreign")
    monkeypatch.setenv("git_work_tree", "/tmp/foreign-worktree")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "malicious")

    environment = provenance._git_environment()

    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert not any(
        key.upper().startswith("GIT_")
        for key in environment
        if key not in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_OPTIONAL_LOCKS", "GIT_ASKPASS", "GIT_TERMINAL_PROMPT"}
    )


def test_unsupported_descriptor_platform_fails_before_remote_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _installed_copy(tmp_path)
    monkeypatch.setattr(provenance.os, "supports_dir_fd", set())
    monkeypatch.setattr(
        provenance,
        "_fetch_source_layout",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("remote access")),
    )

    with pytest.raises(provenance.ProvenanceError, match="platform"):
        provenance.verify_plugin_provenance(root)


def test_inherited_git_dir_cannot_redirect_checkout_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "checkout"
    revision, layout = _checkout(root)
    foreign = tmp_path / "foreign"
    _checkout(foreign)
    _git(foreign, "remote", "set-url", "origin", "https://example.test/foreign.git")
    (foreign / "autoform_cli/__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(foreign, "add", ".")
    _git(foreign, "commit", "-q", "-m", "foreign")
    monkeypatch.setenv("GIT_DIR", os.fspath(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", os.fspath(foreign))
    calls = _mock_fetch(monkeypatch, layout)

    result = provenance.verify_plugin_provenance(root)

    assert result.revision == revision
    assert calls == [(_SOURCE, revision)]


@pytest.mark.parametrize(
    ("source", "normalized"),
    [
        ("https://EXAMPLE.test/owner/repo.git", "https://example.test/owner/repo.git"),
        ("git@github.com:owner/repo", "https://github.com/owner/repo.git"),
        ("https://example.test/owner/repo", "https://example.test/owner/repo.git"),
        ("https://user@example.test/repo.git", None),
        ("https://example.test/repo.git?token=secret", None),
        ("file:///tmp/repo.git", None),
    ],
)
def test_trusted_source_normalization(source: str, normalized: str | None) -> None:
    assert provenance.normalize_git_source(
        source,
        allow_github_scp=True,
        add_git_suffix=True,
    ) == normalized


def test_plugin_pin_is_all_or_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = provenance.PluginProvenance(_SOURCE, _REVISION)
    monkeypatch.setattr(provenance, "verify_plugin_provenance", lambda: expected)
    assert provenance.plugin_pin() == (_SOURCE, _REVISION)

    def unavailable() -> provenance.PluginProvenance:
        raise provenance.ProvenanceError("unavailable")

    monkeypatch.setattr(provenance, "verify_plugin_provenance", unavailable)
    assert provenance.plugin_pin() == ("", "")


def test_cli_reports_stable_provenance_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from autoform_cli import __main__ as cli

    monkeypatch.setattr(
        cli,
        "verify_plugin_provenance",
        lambda: provenance.PluginProvenance(_SOURCE, _REVISION),
    )
    assert cli.main(["project", "provenance", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "revision": _REVISION,
        "source": _SOURCE,
    }


def test_cli_reports_stable_provenance_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from autoform_cli import __main__ as cli

    def unavailable() -> provenance.PluginProvenance:
        raise provenance.ProvenanceError("unavailable")

    monkeypatch.setattr(cli, "verify_plugin_provenance", unavailable)
    assert cli.main(["project", "provenance", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": {
            "code": "project-provenance-unavailable",
            "message": "unavailable",
        },
        "ok": False,
    }


def test_expected_modes_are_compared_as_executable_or_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    source = root / "assets/payload.txt"
    source.chmod(source.stat().st_mode | stat.S_IXUSR)
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="installed Autoform"):
        provenance.verify_plugin_provenance(root)
