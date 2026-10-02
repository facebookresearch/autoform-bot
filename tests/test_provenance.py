from __future__ import annotations

import json
import marshal
import os
import py_compile
import shutil
import stat
import subprocess
import sys
import time
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
            b"[build-system]\n"
            b'requires = ["hatchling>=1.27"]\n'
            b'build-backend = "hatchling.build"\n'
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
    roots = provenance._source_roots(
        all_files,
        (
            *provenance._SHIPPED_ROOTS,
            *optional_roots,
            "autoform_cli",
            "servers",
        ),
    )
    files: dict[str, provenance._ManifestEntry] = {}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if relative not in all_files:
            continue
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


def _claude_transformed_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    registry_version: str | None = None,
) -> tuple[Path, str, provenance._SourceLayout]:
    checkout = tmp_path / "checkout"
    _write_plugin(checkout)
    source_manifest = {
        "description": "verified",
        "name": "autoform",
        "schemaVersion": 1,
        "version": "0.5.0",
    }
    for relative in (".claude-plugin/plugin.json", ".muse-plugin/plugin.json"):
        (checkout / relative).write_text(
            json.dumps(source_manifest, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    _git(checkout, "init", "-q")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-q", "-m", "source")
    _git(checkout, "remote", "add", "origin", _SOURCE)
    revision = _git(checkout, "rev-parse", "HEAD")
    layout = _layout(checkout)
    installed_version = registry_version or f"0.5.0+deicyde.{revision[:7]}"

    installed = (
        tmp_path
        / ".claude/plugins/cache/market/autoform"
        / installed_version.replace("+", "-", 1)
    )
    shutil.copytree(checkout, installed, ignore=shutil.ignore_patterns(".git"))
    installed_manifest = {**source_manifest, "version": installed_version}
    for relative in (".claude-plugin/plugin.json", ".muse-plugin/plugin.json"):
        (installed / relative).write_text(
            json.dumps(installed_manifest, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    (installed / "BUILD_COMMIT").write_text(f"{revision}\n", encoding="ascii")

    registry = tmp_path / ".claude/plugins/installed_plugins.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps(
            {
                "plugins": {
                    "autoform@market": [
                        {
                            "gitCommitSha": revision,
                            "installPath": str(installed),
                            "version": installed_version,
                        }
                    ]
                }
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
    _mock_fetch(monkeypatch, layout)
    return installed, revision, layout


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


def test_verifies_exact_claude_host_metadata_transform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    installed, revision, _ = _claude_transformed_install(tmp_path, monkeypatch)

    assert provenance.verify_plugin_provenance(installed) == provenance.PluginProvenance(
        _SOURCE, revision
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "registry-version",
        "manifest-field",
        "duplicate-key",
        "build-commit",
        "missing-build",
        "partial-overlay",
        "cache-leaf",
        "missing-registry-sha",
        "duplicate-registry",
        "duplicate-empty-registry",
        "cachebuster-label",
        "type-confusion",
        "nonfinite",
    ],
)
def test_rejects_invalid_claude_host_metadata_transform(
    tamper: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    registry_version = "0.5.0+wrong" if tamper == "registry-version" else None
    installed, _, layout = _claude_transformed_install(
        tmp_path,
        monkeypatch,
        registry_version=registry_version,
    )
    manifest = installed / ".claude-plugin/plugin.json"
    if tamper == "manifest-field":
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["description"] = "tampered"
        manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    elif tamper == "duplicate-key":
        installed_version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
        manifest.write_text(
            '{"name":"autoform","version":'
            f'{json.dumps(installed_version)},"version":{json.dumps(installed_version)},'
            '"description":"verified","schemaVersion":1}\n',
            encoding="utf-8",
        )
    elif tamper == "build-commit":
        (installed / "BUILD_COMMIT").write_text(f'{"f" * 40}\n', encoding="ascii")
    elif tamper == "missing-build":
        (installed / "BUILD_COMMIT").unlink()
    elif tamper == "partial-overlay":
        (installed / ".muse-plugin/plugin.json").write_bytes(
            layout.files[".muse-plugin/plugin.json"].content
        )
    elif tamper == "cache-leaf":
        moved = installed.with_name(f"{installed.name}-wrong")
        installed.rename(moved)
        registry = Path(provenance._CLAUDE_PLUGIN_REGISTRY)
        registry_payload = json.loads(registry.read_text(encoding="utf-8"))
        registry_payload["plugins"]["autoform@market"][0]["installPath"] = str(moved)
        registry.write_text(json.dumps(registry_payload), encoding="utf-8")
        installed = moved
    elif tamper in {
        "missing-registry-sha",
        "duplicate-registry",
        "duplicate-empty-registry",
    }:
        registry = Path(provenance._CLAUDE_PLUGIN_REGISTRY)
        registry_payload = json.loads(registry.read_text(encoding="utf-8"))
        entry = registry_payload["plugins"]["autoform@market"][0]
        if tamper == "missing-registry-sha":
            entry.pop("gitCommitSha")
        elif tamper == "duplicate-registry":
            registry_payload["plugins"]["autoform@market"].append(dict(entry))
        else:
            registry_payload["plugins"]["autoform@market"].append(
                {"installPath": str(installed)}
            )
        registry.write_text(json.dumps(registry_payload), encoding="utf-8")
    elif tamper == "cachebuster-label":
        registry = Path(provenance._CLAUDE_PLUGIN_REGISTRY)
        registry_payload = json.loads(registry.read_text(encoding="utf-8"))
        entry = registry_payload["plugins"]["autoform@market"][0]
        evil_version = entry["version"].replace("+deicyde.", "+evil.")
        moved = installed.with_name(evil_version.replace("+", "-", 1))
        installed.rename(moved)
        entry["version"] = evil_version
        entry["installPath"] = str(moved)
        registry.write_text(json.dumps(registry_payload), encoding="utf-8")
        for relative in (".claude-plugin/plugin.json", ".muse-plugin/plugin.json"):
            target = moved / relative
            payload = json.loads(target.read_text(encoding="utf-8"))
            payload["version"] = evil_version
            target.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        installed = moved
    elif tamper == "type-confusion":
        muse = installed / ".muse-plugin/plugin.json"
        payload = json.loads(muse.read_text(encoding="utf-8"))
        payload["schemaVersion"] = True
        muse.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    elif tamper == "nonfinite":
        muse = installed / ".muse-plugin/plugin.json"
        payload = json.loads(muse.read_text(encoding="utf-8"))
        payload["schemaVersion"] = float("nan")
        muse.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(provenance.ProvenanceError):
        provenance.verify_plugin_provenance(installed)


def test_codex_install_cannot_claim_claude_host_transform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _write_plugin(source)
    source_manifest = b'{"name":"autoform","version":"0.5.0"}\n'
    for relative in (".claude-plugin/plugin.json", ".muse-plugin/plugin.json"):
        (source / relative).write_bytes(source_manifest)
    layout = _layout(source)
    installed = tmp_path / "installed"
    shutil.copytree(source, installed)
    for relative in (".claude-plugin/plugin.json", ".muse-plugin/plugin.json"):
        (installed / relative).write_bytes(
            b'{"name":"autoform","version":"0.5.0+forged"}\n'
        )
    (installed / "BUILD_COMMIT").write_text(f"{_REVISION}\n", encoding="ascii")
    _write_record(installed)
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError):
        provenance.verify_plugin_provenance(installed)


def test_claude_manifest_comparison_has_an_explicit_depth_bound() -> None:
    left: list[object] = []
    right: list[object] = []
    left_cursor = left
    right_cursor = right
    for _ in range(provenance._MAX_JSON_DEPTH + 1):
        left_child: list[object] = []
        right_child: list[object] = []
        left_cursor.append(left_child)
        right_cursor.append(right_child)
        left_cursor = left_child
        right_cursor = right_child

    assert not provenance._json_type_exact(left, right)


@pytest.mark.parametrize(
    ("source", "installed"),
    [
        (b'{"value":0.10000000000000001}', b'{"value":0.1}'),
        (b'{"value":1e999}', b'{"value":9e999}'),
    ],
)
def test_claude_manifest_numbers_are_compared_losslessly(
    source: bytes, installed: bytes
) -> None:
    message = "invalid"
    assert not provenance._json_type_exact(
        provenance._decode_json_object(source, message),
        provenance._decode_json_object(installed, message),
    )


def test_claude_manifest_rejects_unbounded_decimal_exponents() -> None:
    with pytest.raises(provenance.ProvenanceError, match="invalid"):
        provenance._decode_json_object(
            b'{"value":1e999999999999999999999999999999999999999}',
            "invalid",
        )


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


def test_recognized_derived_directory_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    (root / ".venv/lib/python3.13/site-packages").mkdir(parents=True)
    (root / ".venv/lib/python3.13/site-packages/injected.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    _mock_fetch(monkeypatch, layout)

    assert provenance.verify_plugin_provenance(root).revision == _REVISION


def test_untracked_claude_host_configuration_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    (root / ".claude").mkdir()
    (root / ".claude/settings.json").write_text("{}\n", encoding="utf-8")
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="unverified"):
        provenance.verify_plugin_provenance(root)


@pytest.mark.parametrize("relative", [".venv", ".lake", ".pytest_cache", "site"])
def test_derived_directory_alias_must_be_a_real_directory(
    relative: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / relative).symlink_to(outside, target_is_directory=True)
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="derived"):
        provenance.verify_plugin_provenance(root)


def test_derived_file_must_be_regular_and_non_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    derived = root / ".zuliprc"
    derived.write_text("local state\n", encoding="utf-8")
    derived.chmod(0o755)
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="derived"):
        provenance.verify_plugin_provenance(root)


def test_tracked_top_level_importable_code_outside_the_boundary_is_rejected(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_plugin(source)
    (source / "sitecustomize.py").write_text("raise RuntimeError\n", encoding="utf-8")

    with pytest.raises(provenance._GitFailure):
        _layout(source)


def test_tracked_nested_importable_code_is_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _write_plugin(source)
    nested = source / "tests/evil.py"
    nested.parent.mkdir()
    nested.write_text("VALUE = 1\n", encoding="utf-8")
    layout = _layout(source)
    installed = tmp_path / "installed"
    shutil.copytree(source, installed)
    _write_record(installed)
    (installed / "tests/evil.py").write_text("VALUE = 2\n", encoding="utf-8")
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="installed Autoform"):
        provenance.verify_plugin_provenance(installed)


def test_tracked_non_python_runtime_payload_is_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _write_plugin(source)
    payload = source / "helpers/runner.sh"
    payload.parent.mkdir()
    payload.write_text("exit 0\n", encoding="utf-8")
    layout = _layout(source)
    installed = tmp_path / "installed"
    shutil.copytree(source, installed)
    _write_record(installed)
    (installed / "helpers/runner.sh").write_text("exit 1\n", encoding="utf-8")
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="installed Autoform"):
        provenance.verify_plugin_provenance(installed)


def test_untracked_non_derived_file_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    (root / "unverified.txt").write_text("not in the recorded commit\n", encoding="utf-8")
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="unverified"):
        provenance.verify_plugin_provenance(root)


@pytest.mark.parametrize(
    "relative",
    [
        "sitecustomize.PY",
        "hook.PYC",
        "hook.PYO",
        "hook.PTH",
        "payload.SO",
        "payload.PYD",
        "payload.DYLIB",
    ],
)
def test_importable_suffix_checks_are_case_insensitive(relative: str) -> None:
    assert provenance._looks_importable(relative)


def test_untracked_nested_non_code_file_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    (root / "assets/unverified.txt").write_text("extra\n", encoding="utf-8")
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="installed Autoform tree"):
        provenance.verify_plugin_provenance(root)


def test_untracked_empty_directory_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    (root / "unverified-directory").mkdir()
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="unverified"):
        provenance.verify_plugin_provenance(root)


def test_tracked_sitecustomize_package_is_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _write_plugin(source)
    hook = source / "sitecustomize/__init__.py"
    hook.parent.mkdir()
    hook.write_text("VALUE = 1\n", encoding="utf-8")
    layout = _layout(source)
    installed = tmp_path / "installed"
    shutil.copytree(source, installed)
    _write_record(installed)
    (installed / "sitecustomize/__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="installed Autoform"):
        provenance.verify_plugin_provenance(installed)


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
        '[build-system]\nrequires = ["hatchling>=1.27"]\n'
        'build-backend = "hatchling.build"\n'
        '[project]\nname = "autoform"\n'
        '[project.scripts]\nautoform = "autoform_cli.__main__:main"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["autoform_cli"]\n'
        f'unrelated = {"[" * 2_000}0{"]" * 2_000}\n'
    ).encode()

    with pytest.raises(provenance._GitFailure):
        provenance._package_roots(pyproject)


@pytest.mark.parametrize(
    ("build_system", "project_fields", "optional_fields"),
    [
        (
            'requires = ["hatchling>=1.27"]\n'
            'build-backend = "hatchling.build"\n'
            'backend-path = ["backend"]\n',
            "",
            "",
        ),
        (
            'requires = ["hatchling>=1.27"]\n'
            'build-backend = "backend.build"\n',
            "",
            "",
        ),
        (
            'requires = ["hatchling @ file:///tmp/hatchling"]\n'
            'build-backend = "hatchling.build"\n',
            "",
            "",
        ),
        (
            'requires = ["hatchling>=1.27"]\n'
            'build-backend = "hatchling.build"\n',
            'dependencies = ["helper @ file:///tmp/helper"]\n',
            "",
        ),
        (
            'requires = ["hatchling>=1.27"]\n'
            'build-backend = "hatchling.build"\n',
            "",
            '[project.optional-dependencies]\ndev = ["helper @ ../helper"]\n',
        ),
        (
            'requires = ["hatchling>=1.27"]\n'
            'build-backend = "hatchling.build"\n',
            'dynamic = ["dependencies"]\n',
            "",
        ),
        (
            'requires = ["hatchling>=1.27"]\n'
            'build-backend = "hatchling.build"\n',
            'dynamic = ["version"]\n',
            "",
        ),
        (
            'requires = ["hatchling>=1.27"]\n'
            'build-backend = "hatchling.build"\n',
            "",
            '[tool.uv.sources]\nhelper = { path = "../helper" }\n',
        ),
        (
            'requires = ["hatchling>=1.27"]\n'
            'build-backend = "hatchling.build"\n',
            "",
            '[dependency-groups]\ndev = ["helper @ file:///tmp/helper"]\n',
        ),
        (
            'requires = ["hatchling>=1.27"]\n'
            'build-backend = "hatchling.build"\n',
            "",
            '[tool.hatch.build.hooks.custom]\npath = "tools/hatch_build.py"\n',
        ),
    ],
)
def test_python_build_and_dependencies_cannot_execute_local_code(
    build_system: str,
    project_fields: str,
    optional_fields: str,
) -> None:
    pyproject = (
        "[build-system]\n"
        f"{build_system}"
        "[project]\n"
        'name = "autoform"\n'
        f"{project_fields}"
        "[project.scripts]\n"
        'autoform = "autoform_cli.__main__:main"\n'
        "[tool.hatch.build.targets.wheel]\n"
        'packages = ["autoform_cli"]\n'
        f"{optional_fields}"
    ).encode()

    with pytest.raises(provenance._GitFailure):
        provenance._package_roots(pyproject)


def test_remote_uv_constraints_are_part_of_the_verified_package_contract() -> None:
    pyproject = (
        "[build-system]\n"
        'requires = ["hatchling>=1.27"]\n'
        'build-backend = "hatchling.build"\n'
        "[project]\n"
        'name = "autoform"\n'
        'dependencies = ["fastmcp>=3"]\n'
        "[project.scripts]\n"
        'autoform = "autoform_cli.__main__:main"\n'
        "[tool.hatch.build.targets.wheel]\n"
        'packages = ["autoform_cli", "servers"]\n'
        "[tool.uv]\n"
        "constraint-dependencies = [\n"
        '  "cryptography<49; sys_platform == \'darwin\' and platform_machine == \'x86_64\'",\n'
        "]\n"
    ).encode()

    assert provenance._package_roots(pyproject) == ("autoform_cli", "servers")


def test_uv_constraints_cannot_reference_local_code() -> None:
    pyproject = (
        "[build-system]\n"
        'requires = ["hatchling>=1.27"]\n'
        'build-backend = "hatchling.build"\n'
        "[project]\n"
        'name = "autoform"\n'
        "[project.scripts]\n"
        'autoform = "autoform_cli.__main__:main"\n'
        "[tool.hatch.build.targets.wheel]\n"
        'packages = ["autoform_cli"]\n'
        "[tool.uv]\n"
        'constraint-dependencies = ["helper @ file:///tmp/helper"]\n'
    ).encode()

    with pytest.raises(provenance._GitFailure):
        provenance._package_roots(pyproject)


def _lock_with_helper(source: str, *, artifact: str = "") -> bytes:
    return (
        "version = 1\n"
        "[[package]]\n"
        'name = "autoform"\n'
        'source = { editable = "." }\n'
        "[[package]]\n"
        'name = "helper"\n'
        f"source = {{ {source} }}\n"
        f"{artifact}"
    ).encode()


def test_uv_lock_accepts_only_the_local_autoform_project() -> None:
    provenance._validate_uv_lock(
        _lock_with_helper(
            'registry = "https://pypi.org/simple"',
            artifact=(
                'wheels = [{ url = "https://files.pythonhosted.org/helper.whl", '
                f'hash = "sha256:{"0" * 64}", size = 1, '
                'upload-time = "2026-01-01T00:00:00Z" }]\n'
            ),
        )
    )


@pytest.mark.parametrize(
    "lock",
    [
        _lock_with_helper('directory = "../helper"'),
        _lock_with_helper('editable = "../helper"'),
        _lock_with_helper('registry = "https://pypi.org/simple"'),
        _lock_with_helper('registry = "file:///tmp/simple"'),
        _lock_with_helper(
            'registry = "https://pypi.org/simple"',
            artifact='wheels = [{ url = "file:///tmp/helper.whl" }]\n',
        ),
        _lock_with_helper(
            'registry = "https://pypi.org/simple"',
            artifact=(
                'wheels = [{ url = "https://files.pythonhosted.org/helper.whl", '
                'size = 1 }]\n'
            ),
        ),
        _lock_with_helper(
            'registry = "https://pypi.org/simple"',
            artifact=(
                'wheels = [{ url = "https://files.pythonhosted.org/helper.whl", '
                'hash = "sha256:00", size = 1 }]\n'
            ),
        ),
        _lock_with_helper(
            'registry = "https://pypi.org/simple"',
            artifact=(
                'wheels = [{ url = "https://files.pythonhosted.org/helper.whl", '
                f'hash = "sha256:{"0" * 64}", size = 0 }}]\n'
            ),
        ),
        _lock_with_helper(
            'registry = "https://pypi.org/simple"',
            artifact=(
                'wheels = [{ url = "https://files.pythonhosted.org/helper.whl", '
                f'hash = "sha256:{"0" * 64}", size = 1, path = "../evil" }}]\n'
            ),
        ),
        _lock_with_helper(
            'registry = "https://pypi.org/simple"',
            artifact=(
                'wheels = [{ url = "https://files.pythonhosted.org/helper\\tbad.whl", '
                f'hash = "sha256:{"0" * 64}", size = 1 }}]\n'
            ),
        ),
        (
            "version = 1\n"
            "[[package]]\n"
            'name = "autoform"\n'
            'source = { editable = "../autoform" }\n'
        ).encode(),
    ],
)
def test_uv_lock_rejects_local_or_credentialed_sources(lock: bytes) -> None:
    with pytest.raises(provenance._GitFailure):
        provenance._validate_uv_lock(lock)


@pytest.mark.parametrize(
    "relative",
    ["uv.toml", "UV.TOML", ".python-version", ".venv/bin/python"],
)
def test_runtime_uv_configuration_is_outside_the_verified_boundary(relative: str) -> None:
    with pytest.raises(provenance._GitFailure):
        provenance._require_canonical_optional_surfaces([relative])


def test_source_tree_cannot_claim_claude_build_metadata() -> None:
    with pytest.raises(provenance._GitFailure):
        provenance._require_canonical_optional_surfaces(["BUILD_COMMIT"])


def test_installed_runtime_uv_configuration_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout = _installed_copy(tmp_path)
    (root / "uv.toml").write_text('index-url = "https://example.test/simple"\n')
    _mock_fetch(monkeypatch, layout)

    with pytest.raises(provenance.ProvenanceError, match="runtime configuration"):
        provenance.verify_plugin_provenance(root)


def test_git_environment_removes_every_inherited_git_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/tmp/foreign")
    monkeypatch.setenv("git_work_tree", "/tmp/foreign-worktree")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "malicious")

    monkeypatch.setenv("HOME", os.fspath(tmp_path / "inherited-home"))
    monkeypatch.setenv("USERPROFILE", os.fspath(tmp_path / "inherited-profile"))
    home = tmp_path / "empty-home"
    home.mkdir()

    environment = provenance._git_environment(home)

    assert environment["HOME"] == os.fspath(home)
    assert environment["USERPROFILE"] == os.fspath(home)
    assert environment["XDG_CONFIG_HOME"] == os.fspath(home)
    assert environment["NETRC"] == os.devnull
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert not any(
        key.upper().startswith("GIT_")
        for key in environment
        if key
        not in {
            "GIT_ASKPASS",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_NO_LAZY_FETCH",
            "GIT_OPTIONAL_LOCKS",
            "GIT_TERMINAL_PROMPT",
        }
    )


def _blob_object(root: Path, relative: str) -> provenance._TreeObject:
    raw = _git(root, "ls-tree", "HEAD", relative)
    mode, kind, object_id = raw.split("\t", 1)[0].split(" ")
    return provenance._TreeObject(mode=int(mode, 8), kind=kind, object_id=object_id)


@pytest.mark.skipif(os.name != "posix", reason="safe provenance inspection is POSIX-only")
def test_git_blob_batch_deduplicates_objects_and_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "first.txt").write_bytes(b"same")
    (root / "second.txt").write_bytes(b"same")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "source")
    first = _blob_object(root, "first.txt")
    second = _blob_object(root, "second.txt")
    assert first.object_id == second.object_id

    real_popen = provenance.subprocess.Popen
    cat_file_processes = 0

    def tracked_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal cat_file_processes
        command = args[0]
        if isinstance(command, list) and "cat-file" in command:
            cat_file_processes += 1
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(provenance.subprocess, "Popen", tracked_popen)
    contents = provenance._read_git_blobs(
        root,
        [first, second] * 100,
        deadline=time.monotonic() + 10,
    )

    assert contents == {first.object_id: b"same"}
    assert cat_file_processes == 1


@pytest.mark.skipif(os.name != "posix", reason="safe provenance inspection is POSIX-only")
def test_git_blob_batch_rejects_one_oversized_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "payload.bin").write_bytes(b"oversized")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "source")
    payload = _blob_object(root, "payload.bin")
    monkeypatch.setattr(provenance, "_MAX_SHIPPED_FILE_BYTES", 4)

    with pytest.raises(provenance._GitFailure):
        provenance._read_git_blobs(
            root,
            [payload],
            deadline=time.monotonic() + 10,
        )


@pytest.mark.skipif(os.name != "posix", reason="safe provenance inspection is POSIX-only")
def test_git_blob_batch_rejects_aggregate_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "first.bin").write_bytes(b"first")
    (root / "second.bin").write_bytes(b"second")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "source")
    objects = [_blob_object(root, "first.bin"), _blob_object(root, "second.bin")]
    monkeypatch.setattr(provenance, "_MAX_SHIPPED_TOTAL_BYTES", 8)

    with pytest.raises(provenance._GitFailure):
        provenance._read_git_blobs(
            root,
            objects,
            deadline=time.monotonic() + 10,
        )


@pytest.mark.skipif(os.name != "posix", reason="safe provenance inspection is POSIX-only")
def test_blobless_fetch_check_rejects_an_unrequested_present_blob(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "payload.bin").write_bytes(b"payload")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "source")
    payload = _blob_object(root, "payload.bin")

    with pytest.raises(provenance._GitFailure):
        provenance._require_blob_presence(
            root,
            [payload],
            set(),
            deadline=time.monotonic() + 10,
        )
    provenance._require_blob_presence(
        root,
        [payload],
        {payload.object_id},
        deadline=time.monotonic() + 10,
    )


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-specific")
def test_git_blob_batch_timeout_kills_the_complete_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "descendant-survived"
    executable = tmp_path / "bin/git"
    executable.parent.mkdir()
    executable.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys, time\n"
        f"code = \"import pathlib, time; time.sleep(1.5); pathlib.Path({os.fspath(marker)!r}).write_text('alive')\"\n"
        "subprocess.Popen([sys.executable, '-c', code])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{executable.parent}{os.pathsep}{os.environ['PATH']}")
    entry = provenance._TreeObject(mode=0o100644, kind="blob", object_id="1" * 40)

    with pytest.raises(provenance._GitFailure):
        provenance._read_git_blobs(
            tmp_path,
            [entry],
            deadline=time.monotonic() + 1,
        )
    time.sleep(1.5)

    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-specific")
@pytest.mark.parametrize("operation", ["run", "batch"])
def test_git_cancellation_kills_the_complete_process_group(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "descendant-survived"
    ready = tmp_path / "descendant-started"
    executable = tmp_path / "bin/git"
    executable.parent.mkdir()
    executable.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, subprocess, sys, time\n"
        f"code = \"import pathlib, time; time.sleep(1); pathlib.Path({os.fspath(marker)!r}).write_text('alive')\"\n"
        "subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"pathlib.Path({os.fspath(ready)!r}).write_text('ready')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{executable.parent}{os.pathsep}{os.environ['PATH']}")

    class InterruptingSelector:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def register(self, *_args: object) -> None:
            return None

        def select(self, _timeout: float) -> list[object]:
            deadline = time.monotonic() + 5
            while not ready.exists():
                if time.monotonic() >= deadline:
                    raise AssertionError("fake Git did not start its descendant")
                time.sleep(0.01)
            raise KeyboardInterrupt

    monkeypatch.setattr(provenance.selectors, "DefaultSelector", InterruptingSelector)
    entry = provenance._TreeObject(mode=0o100644, kind="blob", object_id="1" * 40)

    with pytest.raises(KeyboardInterrupt):
        if operation == "run":
            provenance._run_git(["fetch"], cwd=tmp_path, timeout=5)
        else:
            provenance._read_git_blobs(
                tmp_path,
                [entry],
                deadline=time.monotonic() + 5,
            )
    time.sleep(1.5)

    assert ready.is_file()
    assert not marker.exists()


def test_source_fetch_uses_one_deadline_and_blobless_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = {
        ".claude-plugin/plugin.json": "2" * 40,
        ".codex-plugin/plugin.json": "3" * 40,
        ".muse-plugin/plugin.json": "4" * 40,
        ".mcp.json": "5" * 40,
        "assets/payload.txt": "6" * 40,
        "autoform_cli/__init__.py": "7" * 40,
        "pyproject.toml": "8" * 40,
        "skills/setup/SKILL.md": "9" * 40,
        "uv.lock": "a" * 40,
    }
    listing = b"".join(
        f"100644 blob {object_id}\t{relative}\0".encode()
        for relative, object_id in paths.items()
    )
    pyproject = (
        b"[build-system]\n"
        b'requires = ["hatchling>=1.27"]\n'
        b'build-backend = "hatchling.build"\n'
        b"[project]\n"
        b'name = "autoform"\n'
        b"[project.scripts]\n"
        b'autoform = "autoform_cli.__main__:main"\n'
        b"[tool.hatch.build.targets.wheel]\n"
        b'packages = ["autoform_cli"]\n'
    )
    deadlines: list[float | None] = []
    commands: list[list[str]] = []

    def fake_run_git(
        arguments: list[str],
        *,
        cwd: Path,
        timeout: float = 15,
        deadline: float | None = None,
        max_stdout_bytes: int = provenance._MAX_GIT_TEXT_BYTES,
        stdin_bytes: bytes | None = None,
    ) -> bytes:
        del cwd, timeout, max_stdout_bytes, stdin_bytes
        deadlines.append(deadline)
        commands.append(arguments)
        if arguments[:2] == ["rev-parse", "--verify"]:
            return f"{_REVISION}\n".encode()
        if arguments and arguments[0] == "ls-tree":
            return listing
        return b""

    helper_deadlines: list[float] = []
    presence_calls: list[set[str]] = []
    object_fetches: list[set[str]] = []

    def fake_presence(
        repository: Path,
        objects: object,
        expected: object,
        *,
        deadline: float,
    ) -> None:
        del repository, objects
        helper_deadlines.append(deadline)
        presence_calls.append(set(expected))  # type: ignore[arg-type]

    def fake_fetch_objects(
        repository: Path, object_ids: object, *, deadline: float
    ) -> None:
        del repository
        helper_deadlines.append(deadline)
        object_fetches.append(set(object_ids))  # type: ignore[arg-type]

    contents = {object_id: b"content" for object_id in paths.values()}
    contents[paths["pyproject.toml"]] = pyproject
    contents[paths["uv.lock"]] = (
        b"version = 1\n"
        b"[[package]]\n"
        b'name = "autoform"\n'
        b'source = { editable = "." }\n'
    )

    def fake_read_blobs(
        repository: Path,
        objects: object,
        *,
        deadline: float,
        known: dict[str, bytes] | None = None,
    ) -> dict[str, bytes]:
        del repository
        helper_deadlines.append(deadline)
        result = dict(known or {})
        for entry in objects:  # type: ignore[union-attr]
            result[entry.object_id] = contents[entry.object_id]
        return result

    monkeypatch.setattr(provenance, "_run_git", fake_run_git)
    monkeypatch.setattr(provenance, "_require_blob_presence", fake_presence)
    monkeypatch.setattr(provenance, "_fetch_git_objects", fake_fetch_objects)
    monkeypatch.setattr(provenance, "_read_git_blobs", fake_read_blobs)

    layout = provenance._fetch_source_layout(_SOURCE, _REVISION, tmp_path)

    assert layout.files["pyproject.toml"].content == pyproject
    assert deadlines and deadlines[0] is not None
    assert set(deadlines + helper_deadlines) == {deadlines[0]}
    fetch = next(arguments for arguments in commands if arguments and arguments[0] == "fetch")
    assert "--filter=blob:none" in fetch
    metadata_ids = {paths["pyproject.toml"], paths["uv.lock"]}
    selected_ids = set(paths.values())
    assert presence_calls == [set(), metadata_ids, selected_ids]
    assert object_fetches == [metadata_ids, selected_ids - metadata_ids]


def test_selected_git_object_fetch_uses_sorted_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], bytes | None]] = []

    def fake_run_git(
        arguments: list[str],
        *,
        cwd: Path,
        timeout: float = 15,
        deadline: float | None = None,
        max_stdout_bytes: int = provenance._MAX_GIT_TEXT_BYTES,
        stdin_bytes: bytes | None = None,
    ) -> bytes:
        del cwd, timeout, deadline, max_stdout_bytes
        calls.append((arguments, stdin_bytes))
        return b""

    monkeypatch.setattr(provenance, "_run_git", fake_run_git)
    provenance._fetch_git_objects(
        tmp_path,
        ["b" * 40, "a" * 40, "b" * 40],
        deadline=time.monotonic() + 10,
    )

    assert calls == [
        (
            [
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--recurse-submodules=no",
                "--filter=blob:none",
                "--stdin",
                "origin",
            ],
            f'{"a" * 40}\n{"b" * 40}\n'.encode("ascii"),
        )
    ]


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-specific")
def test_git_timeout_kills_the_complete_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "descendant-survived"
    executable = tmp_path / "bin/git"
    executable.parent.mkdir()
    executable.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys, time\n"
        f"code = \"import pathlib, time; time.sleep(1.5); pathlib.Path({os.fspath(marker)!r}).write_text('alive')\"\n"
        "subprocess.Popen([sys.executable, '-c', code])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{executable.parent}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(provenance._GitFailure):
        provenance._run_git(["fetch"], cwd=tmp_path, timeout=1)
    time.sleep(1.5)

    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-specific")
def test_failed_git_leader_does_not_leave_its_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "descendant-survived"
    executable = tmp_path / "bin/git"
    executable.parent.mkdir()
    executable.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        f"code = \"import pathlib, time; time.sleep(1.5); pathlib.Path({os.fspath(marker)!r}).write_text('alive')\"\n"
        "subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{executable.parent}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(provenance._GitFailure):
        provenance._run_git(["fetch"], cwd=tmp_path, timeout=5)
    time.sleep(1.5)

    assert not marker.exists()


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


@pytest.mark.skipif(
    os.environ.get("AUTOFORM_PROVENANCE_NETWORK") != "1",
    reason="set AUTOFORM_PROVENANCE_NETWORK=1 for the clean-checkout integration test",
)
def test_live_clean_checkout_provenance(repo_root: Path) -> None:
    verified = provenance.verify_plugin_provenance(repo_root)

    assert verified.source.startswith("https://")
    assert len(verified.revision) == 40
