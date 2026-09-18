from __future__ import annotations

import json
import marshal
import os
import py_compile
import shutil
import stat
import subprocess
import sys
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
    paths = [path for path in sorted(root.rglob("*")) if path.is_file()]
    all_files = {
        path.relative_to(root).as_posix()
        for path in paths
        if ".git" not in path.relative_to(root).parts
    }
    optional_roots = {
        shipped_root
        for shipped_root in provenance._OPTIONAL_SHIPPED_ROOTS
        if any(provenance._under_root(relative, shipped_root) for relative in all_files)
    }
    roots = tuple(
        sorted(
            (
                *provenance._SHIPPED_ROOTS,
                *optional_roots,
                "autoform_cli",
                "servers",
            )
        )
    )
    files: dict[str, provenance._ManifestEntry] = {}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if relative not in all_files:
            continue
        if (
            relative in provenance._SHIPPED_FILES
            or relative in provenance._OPTIONAL_SHIPPED_FILES
            or any(
                provenance._under_root(relative, shipped_root)
                for shipped_root in roots
            )
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
    ref_name: str | None = "main",
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


def test_verifies_a_codex_record_without_a_requested_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    _write_record(root, ref_name=None)
    calls = _mock_fetch(monkeypatch, layout)

    result = provenance.verify_plugin_provenance(root)

    assert result == provenance.PluginProvenance(_SOURCE, _REVISION)
    assert calls == [(_SOURCE, _REVISION)]


def test_verifies_a_claude_cache_copy_at_its_recorded_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    checkout = tmp_path / "checkout"
    revision, _ = _checkout(checkout)
    installed = tmp_path / ".claude/plugins/cache/market/autoform/0.5.0"
    _write_plugin(installed)
    layout = _layout(installed)

    registry = tmp_path / ".claude/plugins/installed_plugins.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps(
            {
                "plugins": {
                    "autoform@market": [
                        {"installPath": str(tmp_path / "other-scope")},
                        {"installPath": str(installed)},
                        {
                            "gitCommitSha": revision,
                            "installPath": str(installed),
                        }
                    ]
                },
                "version": 2,
            }
        ),
        encoding="utf-8",
    )
    marketplaces = tmp_path / ".claude/plugins/known_marketplaces.json"
    marketplaces.write_text(
        json.dumps({"market": {"installLocation": str(checkout)}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(provenance, "_CLAUDE_PLUGIN_REGISTRY", registry)
    monkeypatch.setattr(provenance, "_CLAUDE_MARKETPLACE_REGISTRY", marketplaces)
    calls = _mock_fetch(monkeypatch, layout)

    result = provenance.verify_plugin_provenance(installed)

    assert result == provenance.PluginProvenance(_SOURCE, revision)
    assert calls == [(_SOURCE, revision)]


def test_verifies_a_claude_cache_under_the_configured_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    revision, _ = _checkout(checkout)
    config = tmp_path / "claude-config"
    installed = config / "plugins/cache/market/autoform/0.5.0"
    _write_plugin(installed)
    layout = _layout(installed)
    registry = config / "plugins/installed_plugins.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps(
            {
                "plugins": {
                    "autoform@market": [
                        {"gitCommitSha": revision, "installPath": str(installed)}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (config / "plugins/known_marketplaces.json").write_text(
        json.dumps({"market": {"installLocation": str(checkout)}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    calls = _mock_fetch(monkeypatch, layout)

    result = provenance.verify_plugin_provenance(installed)

    assert result == provenance.PluginProvenance(_SOURCE, revision)
    assert calls == [(_SOURCE, revision)]


def test_claude_cache_detection_is_scoped_to_the_configured_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookalike = tmp_path / "cache/market/autoform/1.0"
    lookalike.mkdir(parents=True)
    monkeypatch.setattr(
        provenance,
        "_read_bounded_path",
        lambda *args, **kwargs: pytest.fail("lookalike path read Claude registries"),
    )

    assert provenance._claude_marketplace_candidate(lookalike) is None


def test_malformed_claude_registry_paths_use_the_stable_error_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    installed = tmp_path / ".claude/plugins/cache/market/autoform/0.5.0"
    installed.mkdir(parents=True)
    registry = tmp_path / ".claude/plugins/installed_plugins.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps(
            {
                "plugins": {
                    "autoform@market": [
                        {"gitCommitSha": _REVISION, "installPath": "~missing-user/plugin"}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(provenance, "_CLAUDE_PLUGIN_REGISTRY", registry)

    with pytest.raises(provenance.ProvenanceError, match="installation registry"):
        provenance._claude_install_revision(installed, "market", "autoform")


@pytest.mark.parametrize("escaped_path", ["/tmp/\\u0000checkout", "/tmp/\\ud800checkout"])
def test_malformed_claude_marketplace_path_uses_the_stable_error_contract(
    escaped_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    installed = tmp_path / ".claude/plugins/cache/market/autoform/0.5.0"
    installed.mkdir(parents=True)
    plugins = tmp_path / ".claude/plugins/installed_plugins.json"
    plugins.parent.mkdir(parents=True, exist_ok=True)
    plugins.write_text('{"plugins": {}}', encoding="utf-8")
    marketplaces = tmp_path / ".claude/plugins/known_marketplaces.json"
    marketplaces.write_text(
        f'{{"market":{{"installLocation":"{escaped_path}"}}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(provenance, "_CLAUDE_PLUGIN_REGISTRY", plugins)
    monkeypatch.setattr(provenance, "_CLAUDE_MARKETPLACE_REGISTRY", marketplaces)

    with pytest.raises(provenance.ProvenanceError, match="marketplace registry"):
        provenance._claude_marketplace_candidate(installed)


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


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("agents/injected.md", "agent instructions\n"),
        ("Agents/injected.md", "agent instructions\n"),
        ("bin/injected", "#!/bin/sh\nexit 0\n"),
        ("commands/injected.md", "command instructions\n"),
        ("COMMANDS/injected.md", "command instructions\n"),
        ("hooks/hooks.json", "{}\n"),
        ("Hooks/hooks.json", "{}\n"),
        ("monitors/monitors.json", "{}\n"),
        ("output-styles/injected.md", "style instructions\n"),
        ("scripts/injected.sh", "exit 0\n"),
        ("themes/theme.json", "{}\n"),
        ("workflows/workflow.json", "{}\n"),
        (".app.json", "{}\n"),
        (".APP.json", "{}\n"),
        (".lsp.json", "{}\n"),
        (".npmrc", "registry=https://example.test\n"),
        ("package.json", "{}\n"),
        ("Package.json", "{}\n"),
        ("package-lock.json", "{}\n"),
        ("bun.lock", "lock\n"),
        ("bun.lockb", "lock\n"),
        ("npm-shrinkwrap.json", "{}\n"),
        ("bunfig.toml", "[install]\n"),
        ("plugin.json", "{}\n"),
        ("Plugin.json", "{}\n"),
        ("mcp.json", "{}\n"),
        ("MCP.json", "{}\n"),
        ("settings.json", "{}\n"),
        ("Settings.json", "{}\n"),
    ],
)
def test_untracked_plugin_surfaces_are_rejected_when_absent_from_source(
    relative: str,
    content: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, layout = _installed_copy(tmp_path)
    injected = root / relative
    injected.parent.mkdir(parents=True, exist_ok=True)
    injected.write_text(content, encoding="utf-8")
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="unverified plugin"):
        provenance.verify_plugin_provenance(root)


@pytest.mark.parametrize(
    "relative",
    [
        "Hooks/hooks.json",
        "BIN/injected",
        "COMMANDS/injected.md",
        "MONITORS/monitors.json",
        "Output-Styles/injected.md",
        "Scripts/injected.sh",
        "Themes/theme.json",
        "WORKFLOWS/workflow.json",
        ".APP.json",
        ".LSP.json",
        ".NPMRC",
        "Package.json",
        "Package-Lock.json",
        "BUN.LOCK",
        "BUN.LOCKB",
        "NPM-SHRINKWRAP.json",
        "BUNFIG.TOML",
        "Plugin.json",
        "MCP.json",
        "Settings.json",
    ],
)
def test_remote_plugin_surface_case_alias_is_rejected(
    relative: str,
) -> None:
    with pytest.raises(provenance._GitFailure):
        provenance._require_canonical_optional_surfaces([relative])


def test_canonical_optional_plugin_surfaces_are_valid_source_paths() -> None:
    provenance._require_canonical_optional_surfaces(
        [
            "hooks/hooks.json",
            "scripts/helper.sh",
            ".app.json",
            ".lsp.json",
            ".npmrc",
            "bunfig.toml",
            "plugin.json",
        ]
    )


@pytest.mark.parametrize(
    "relative",
    [
        ".app.json",
        ".lsp.json",
        ".npmrc",
        "bun.lock",
        "bun.lockb",
        "bunfig.toml",
        "mcp.json",
        "npm-shrinkwrap.json",
        "package-lock.json",
        "package.json",
        "plugin.json",
        "settings.json",
    ],
)
def test_verified_optional_root_plugin_file_is_accepted(
    relative: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _write_plugin(source)
    (source / relative).write_text("{}\n", encoding="utf-8")
    layout = _layout(source)
    installed = tmp_path / "installed"
    shutil.copytree(source, installed)
    _write_record(installed)
    _mock_fetch(monkeypatch, layout)

    assert provenance.verify_plugin_provenance(installed).revision == _REVISION


@pytest.mark.parametrize(
    "package",
    [
        {"workspaces": ["packages/*"]},
        {"dependencies": {"helper": "file:packages/helper"}},
        {"devDependencies": {"helper": "link:../helper"}},
        {"optionalDependencies": {"helper": "workspace:*"}},
        {"peerDependencies": {"helper": "../helper"}},
        {"overrides": {"helper": {"child": "FILE:../child"}}},
        {"resolutions": {"helper": "~/helper"}},
        {"dependencies": {"helper": "."}},
        {"dependencies": {"helper": ".."}},
        {"dependencies": {"helper": "GIT+FILE:../helper"}},
        {"dependencies": {"helper": " file:../helper"}},
    ],
)
def test_package_manifest_cannot_escape_the_verified_tree(
    package: dict[str, object],
) -> None:
    with pytest.raises(provenance._GitFailure):
        provenance._validate_package_manifest(json.dumps(package).encode())


def test_package_manifest_dependency_policy_has_a_depth_bound() -> None:
    nested: dict[str, object] = {"leaf": "1.0.0"}
    for index in range(provenance._MAX_JSON_DEPTH + 1):
        nested = {f"level-{index}": nested}

    with pytest.raises(provenance._GitFailure):
        provenance._validate_package_manifest(json.dumps({"overrides": nested}).encode())


def test_verified_optional_plugin_directories_are_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _write_plugin(source)
    for relative in (
        "agents/agent.md",
        "bin/helper",
        "commands/command.md",
        "hooks/hooks.json",
        "monitors/monitors.json",
        "output-styles/style.md",
        "scripts/helper.sh",
        "themes/theme.json",
        "workflows/workflow.json",
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    layout = _layout(source)
    installed = tmp_path / "installed"
    shutil.copytree(source, installed)
    _write_record(installed)
    _mock_fetch(monkeypatch, layout)

    assert provenance.verify_plugin_provenance(installed).revision == _REVISION


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


def test_current_interpreter_bytecode_tag_case_alias_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    source = root / "autoform_cli/__init__.py"
    cached = Path(py_compile.compile(os.fspath(source), doraise=True))
    current_tag = sys.implementation.cache_tag
    assert current_tag is not None
    alias = cached.with_name(cached.name.replace(current_tag, current_tag.upper()))
    cached.rename(alias)
    _mock_fetch(monkeypatch, layout)

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


@pytest.mark.parametrize("error", [ValueError("integer too large"), MemoryError()])
def test_installer_json_decoder_failures_use_the_stable_error_contract(
    error: Exception,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _installed_copy(tmp_path)
    monkeypatch.setattr(
        provenance.json,
        "loads",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(provenance.ProvenanceError, match="installer record"):
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


def test_deep_remote_pyproject_fails_with_a_bounded_error() -> None:
    pyproject = (
        '[project]\nname = "autoform"\n'
        '[project.scripts]\nautoform = "autoform_cli.__main__:main"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["autoform_cli"]\n'
        f'unrelated = {"[" * 2_000}0{"]" * 2_000}\n'
    ).encode()

    with pytest.raises(provenance._GitFailure):
        provenance._package_roots(pyproject)


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
