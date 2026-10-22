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
_SOURCE_NAMES = ("blueprint-dagre.vendor.js", "blueprint-navigation.js", "blueprint-dag.js")
_BANNER = re.compile(rb"\A/\* autoform-explorer-source-sha256:([0-9a-f]{64}) \*/\n")


def test_explorer_bundle_digest_matches_exact_readable_source_bytes() -> None:
    source = b"\n".join((_ASSETS / name).read_bytes() for name in _SOURCE_NAMES)
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
    assert len(bundled) < 132_000
    assert b"sourceMappingURL=" not in bundled
    assert str(_ROOT).encode("utf-8") not in bundled
    assert b"/Users/" not in bundled and b"/home/runner/" not in bundled


def test_explorer_compiler_is_private_pinned_development_tooling() -> None:
    package = json.loads((_ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((_ROOT / "package-lock.json").read_text(encoding="utf-8"))
    assert package["private"] is True
    assert not package.get("dependencies")
    assert package["devDependencies"] == {"@dagrejs/dagre": "3.1.1", "esbuild": "0.28.2", "terser": "5.51.2"}
    dagre = lock["packages"]["node_modules/@dagrejs/dagre"]
    assert dagre["version"] == "3.1.1"
    assert dagre["resolved"] == "https://registry.npmjs.org/@dagrejs/dagre/-/dagre-3.1.1.tgz"
    assert dagre["integrity"] == "sha512-zroZB1dFOFiGgv4Xcrn1DckB1o4aOikPqD2NDQPV0WM//CXGcS6xiD0rNkqHmw6FEg4tabt4nxPLwgCWT+Vb2A=="
    graphlib = lock["packages"]["node_modules/@dagrejs/graphlib"]
    assert graphlib["version"] == "4.0.5"
    assert graphlib["resolved"] == "https://registry.npmjs.org/@dagrejs/graphlib/-/graphlib-4.0.5.tgz"
    assert graphlib["integrity"] == "sha512-7xrBTqIts3o+PMUZX97wSc+7TUbW+/rULzGNCTP6yooNVDXbzw4Wutg/H/xOutTB/c/k0YqOAavgPh4/Zk9PFA=="
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


def test_layout_engine_is_packaged_private_and_keeps_its_license() -> None:
    vendor = (_ASSETS / "blueprint-dagre.vendor.js").read_text(encoding="utf-8")
    bundled = _BUNDLE.read_text(encoding="utf-8")
    assert "var AutoformDagre = (function ()" in vendor
    assert "return dagre.default || dagre" in vendor
    assert "sourceMappingURL=" not in vendor
    for text in (vendor, bundled):
        assert "Copyright (c) 2012-2014 Chris Pettitt" in text
        assert "Permission is hereby granted, free of charge" in text
        assert "THE SOFTWARE IS PROVIDED" in text


def test_packaged_layout_engine_exposes_real_graph_and_layout_without_global_collision() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for the layout-engine packaging check")
    result = subprocess.run([
        node, "-e", """
        const fs=require('fs'),vm=require('vm'),assert=require('assert');
        const sandbox={structuredClone}; vm.createContext(sandbox);
        vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
        assert.equal(sandbox.dagre,undefined);
        const engine=sandbox.AutoformDagre;
        assert.equal(engine.version,'3.1.1');
        assert.equal(typeof engine.graphlib.Graph,'function');
        const graph=new engine.graphlib.Graph().setGraph({rankdir:'LR'}).setDefaultEdgeLabel(()=>({}));
        graph.setNode('a',{width:196,height:96}); graph.setNode('b',{width:196,height:96});graph.setEdge('a','b');
        engine.layout(graph);
        assert(graph.node('a').x<graph.node('b').x);assert(graph.edge('a','b').points.length>=2);
        """, str(_ASSETS / "blueprint-dagre.vendor.js"),
    ], capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


@pytest.mark.parametrize("name", (*_SOURCE_NAMES, _BUNDLE.name))
def test_explorer_scripts_parse_as_classic_scripts(name: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is only needed for development syntax checks, not Python publication")
    result = subprocess.run([node, "--check", str(_ASSETS / name)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
