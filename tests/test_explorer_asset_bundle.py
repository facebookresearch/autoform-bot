"""The shipped explorer must match its readable sources and fit the existing runtime budget."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_ASSETS = _ROOT / "autoform_cli/assets"
_BUNDLE = _ASSETS / "blueprint-explorer.min.js"
_SOURCE_NAMES = ("blueprint-navigation.js", "blueprint-dag.js")
_BANNER = re.compile(rb"\A/\* autoform-explorer-source-sha256:([0-9a-f]{64}) \*/\n")


def test_explorer_bundle_digest_matches_exact_readable_source_bytes() -> None:
    source = (_ASSETS / _SOURCE_NAMES[0]).read_bytes() + b"\n" + (_ASSETS / _SOURCE_NAMES[1]).read_bytes()
    source.decode("utf-8")
    bundled = _BUNDLE.read_bytes()
    banner = _BANNER.match(bundled)
    assert banner is not None, "The shipped explorer lacks its exact source-digest banner"
    assert banner.group(1).decode("ascii") == hashlib.sha256(source).hexdigest(), (
        "The shipped explorer is stale; run npm run build:explorer"
    )


def test_explorer_bundle_keeps_the_integrated_client_budget_and_has_no_source_maps_or_local_paths() -> None:
    bundled = _BUNDLE.read_bytes()
    bundled.decode("utf-8")
    assert len(bundled) < 72_000
    assert b"sourceMappingURL=" not in bundled
    assert str(_ROOT).encode("utf-8") not in bundled
    assert b"/Users/" not in bundled and b"/home/runner/" not in bundled


def test_explorer_compiler_is_private_pinned_development_tooling() -> None:
    package = json.loads((_ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((_ROOT / "package-lock.json").read_text(encoding="utf-8"))
    assert package["private"] is True
    assert not package.get("dependencies")
    assert package["devDependencies"] == {"esbuild": "0.28.2", "terser": "5.51.2"}
    assert lock["packages"]["node_modules/esbuild"]["version"] == "0.28.2"
    terser = lock["packages"]["node_modules/terser"]
    assert terser["version"] == "5.51.2"
    assert terser["resolved"] == "https://registry.npmjs.org/terser/-/terser-5.51.2.tgz"
    assert terser["integrity"] == (
        "sha512-bWnjSNscmuI+GJze6ZupnHP8G/cTcsJF+bXCeQknk2SHQsgbNJnLrqiH9jZ2W4STPVXH2mDKKRX3iwPhc9Cn/Q=="
    )
    assert package["scripts"] == {
        "build:explorer": "node scripts/build-explorer.mjs",
        "check:explorer": "node scripts/build-explorer.mjs --check",
    }


def test_explorer_mangles_only_explicitly_private_refs_and_keeps_public_contracts() -> None:
    compiler = (_ROOT / "scripts/build-explorer.mjs").read_text(encoding="utf-8")
    assert re.findall(r"\bmangleProps:\s*([^,\n]+)", compiler) == [r"/^\$/"]
    assert re.search(r"\bmangle:\s*\{\s*properties:\s*false\s*\}", compiler)
    assert "pure_getters: false" in compiler and "unsafe: false" in compiler and "drop_console: false" in compiler
    bundled = _BUNDLE.read_bytes()
    # Public schema, publication attributes, route fields and renderer/history APIs are not private.
    for marker in (
        b"AutoformNavigation",
        b"autoform-dag-search/v1",
        b"autoform-dag-view/v2",
        b"autoformNavigation",
        b".key",
        b"data-page-href",
        b"data-page-title",
        b"data-graph-src",
        b"data-catalog-src",
        b"data-scope-id",
        b".route",
        b".data_url",
        b".result",
        b".catalog",
        b".kind",
        b".scope",
        b".navigation",
        b".parent",
        b".area",
        b".relations",
        b".onCommit",
        b".captureState",
        b".restoreState",
    ):
        assert marker in bundled, f"A public explorer contract was renamed or lost: {marker!r}"


@pytest.mark.parametrize("name", (*_SOURCE_NAMES, _BUNDLE.name))
def test_explorer_scripts_parse_as_classic_scripts(name: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is only needed for development syntax checks, not Python publication")
    result = subprocess.run([node, "--check", str(_ASSETS / name)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
