"""The shipped explorer must match its readable sources and fit the existing runtime budget."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from autoform_cli import dag_viewer

_ROOT = Path(__file__).resolve().parents[1]
_ASSETS = _ROOT / "autoform_cli/assets"
_BUNDLE = _ASSETS / "blueprint-explorer.min.js"
_SOURCE_NAMES = ("blueprint-navigation.js", "blueprint-dag.js")
_BANNER = re.compile(rb"\A/\* autoform-explorer-source-sha256:([0-9a-f]{64}) \*/\n")


def test_explorer_bundle_digest_matches_canonical_utf8_lf_source_text() -> None:
    source = "\n".join((_ASSETS / name).read_text(encoding="utf-8") for name in _SOURCE_NAMES).encode("utf-8")
    bundled = _BUNDLE.read_text(encoding="utf-8").encode("utf-8")
    banner = _BANNER.match(bundled)
    assert banner is not None, "The shipped explorer lacks its exact source-digest banner"
    assert banner.group(1).decode("ascii") == hashlib.sha256(source).hexdigest(), (
        "The shipped explorer is stale; run npm run build:explorer"
    )


def _compiler_fixture(tmp_path: Path) -> tuple[str, Path]:
    node = shutil.which("node")
    dependencies = _ROOT / "node_modules"
    if node is None or not (dependencies / "esbuild/package.json").is_file() or not (dependencies / "terser/package.json").is_file():
        pytest.skip("The locked development compiler is needed for checkout-portability tests")
    project = tmp_path / "compiler-project"
    (project / "scripts").mkdir(parents=True)
    (project / "autoform_cli/assets").mkdir(parents=True)
    try:
        (project / "node_modules").symlink_to(dependencies, target_is_directory=True)
    except OSError:
        pytest.skip("A temporary compiler dependency link is unavailable on this platform")
    shutil.copyfile(_ROOT / "scripts/build-explorer.mjs", project / "scripts/build-explorer.mjs")
    return node, project


@pytest.mark.parametrize("ending,bom", [("\n", False), ("\r\n", False), ("\r", False), ("\r\n", True)])
def test_real_compiler_and_node_free_publisher_agree_on_checkout_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str, bom: bool,
) -> None:
    node, project = _compiler_fixture(tmp_path)
    assets = project / "autoform_cli/assets"
    for name, source in zip(_SOURCE_NAMES, ("// café\nwindow.AutoformNavigation={create(){return null;}};\n", "// map\nwindow.Explorer={route:'article.html'};\n")):
        data = source.replace("\n", ending).encode("utf-8")
        (assets / name).write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
    compiler = project / "scripts/build-explorer.mjs"
    result = subprocess.run([node, str(compiler)], capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    monkeypatch.setattr(dag_viewer, "__file__", str(project / "autoform_cli/dag_viewer.py"))
    source = "\n".join(dag_viewer._asset_text(name) for name in _SOURCE_NAMES)
    monkeypatch.setattr(dag_viewer, "_SCRIPT", source)
    built = (assets / _BUNDLE.name).read_bytes()
    assert built.startswith(f"/* autoform-explorer-source-sha256:{hashlib.sha256(source.encode()).hexdigest()} */\n".encode())
    assert dag_viewer.viewer_script().encode() == built
    # A checked-in bundle is converted too on an autocrlf checkout. Both
    # development verification and Node-free publication must still agree.
    (assets / _BUNDLE.name).write_bytes(built.replace(b"\n", b"\r\n"))
    checked = subprocess.run([node, str(compiler), "--check"], capture_output=True, text=True, timeout=45)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert dag_viewer.viewer_script().encode() == built


def test_real_compiler_rejects_invalid_utf8_before_writing_a_bundle(tmp_path: Path) -> None:
    node, project = _compiler_fixture(tmp_path)
    assets = project / "autoform_cli/assets"
    (assets / _SOURCE_NAMES[0]).write_bytes(b"\xff")
    (assets / _SOURCE_NAMES[1]).write_text("window.Explorer={};\n", encoding="utf-8")
    result = subprocess.run([node, str(project / "scripts/build-explorer.mjs")], capture_output=True, text=True, timeout=45)
    assert result.returncode != 0 and not (assets / _BUNDLE.name).exists()


def test_explorer_bundle_keeps_the_integrated_client_budget_and_has_no_source_maps_or_local_paths() -> None:
    bundled = _BUNDLE.read_bytes()
    bundled.decode("utf-8")
    assert len(bundled) < 145_000
    assert b"sourceMappingURL=" not in bundled
    assert str(_ROOT).encode("utf-8") not in bundled
    assert b"/Users/" not in bundled and b"/home/runner/" not in bundled


def test_explorer_compiler_is_private_pinned_development_tooling() -> None:
    package = json.loads((_ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((_ROOT / "package-lock.json").read_text(encoding="utf-8"))
    assert package["private"] is True
    assert not package.get("dependencies")
    assert package["devDependencies"] == {"esbuild": "0.28.2", "jsdom": "26.1.0", "terser": "5.51.2"}
    assert not any(name.startswith("node_modules/@dagrejs/") for name in lock["packages"])
    assert lock["packages"]["node_modules/esbuild"]["version"] == "0.28.2"
    dom = lock["packages"]["node_modules/jsdom"]
    assert dom["version"] == "26.1.0" and dom["dev"] is True
    assert dom["resolved"] == "https://registry.npmjs.org/jsdom/-/jsdom-26.1.0.tgz"
    assert dom["integrity"] == (
        "sha512-Cvc9WUhxSMEo4McES3P7oK3QaXldCfNWp7pl2NNeiIFlCoLr3kfq9kb1fxftiwk1FLV7CvpvDfonxtzUDeSOPg=="
    )
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


def test_explorer_mangles_only_explicitly_private_refs_and_keeps_runtime_fields() -> None:
    compiler = (_ROOT / "scripts/build-explorer.mjs").read_text(encoding="utf-8")
    assert re.findall(r"\bmangleProps:\s*([^,\n]+)", compiler) == [r"/^\$/"]
    assert re.search(r"\bmangle:\s*\{\s*properties:\s*false\s*\}", compiler)
    assert "pure_getters: false" in compiler and "unsafe: false" in compiler and "drop_console: false" in compiler
    bundled = _BUNDLE.read_bytes()
    # Internal viewer fields still need their exact runtime names; this does not
    # make payload extensions or controller hooks a public API.
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
        assert marker in bundled, f"A required runtime field was renamed or lost: {marker!r}"


def test_unused_layout_vendor_is_not_packaged_or_exposed() -> None:
    assert not (_ASSETS / "blueprint-dagre.vendor.js").exists()
    for path in (_BUNDLE, _ASSETS / "blueprint-dag.js", _ROOT / "scripts/build-explorer.mjs"):
        assert "AutoformDagre" not in path.read_text(encoding="utf-8")


def test_publication_rejects_stale_bundle_instead_of_using_raw_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dag_viewer, "_SCRIPT", dag_viewer._SCRIPT + "\n// unbuilt source edit\n")
    with pytest.raises(ValueError, match="Explorer bundle is stale"):
        dag_viewer.viewer_script()


@pytest.mark.parametrize("contents", [None, b"\xff"])
def test_publication_rejects_missing_or_corrupt_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: bytes | None,
) -> None:
    monkeypatch.setattr(dag_viewer, "__file__", str(tmp_path / "dag_viewer.py"))
    if contents is not None:
        assets = tmp_path / "assets"
        assets.mkdir()
        (assets / _BUNDLE.name).write_bytes(contents)
    with pytest.raises(ValueError, match="Explorer bundle is missing or corrupt"):
        dag_viewer.viewer_script()


def test_publication_rejects_oversized_bundle_even_with_current_source_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dag_viewer, "__file__", str(tmp_path / "dag_viewer.py"))
    assets = tmp_path / "assets"
    assets.mkdir()
    fingerprint = hashlib.sha256(dag_viewer._SCRIPT.encode("utf-8")).hexdigest()
    (assets / _BUNDLE.name).write_text(
        f"/* autoform-explorer-source-sha256:{fingerprint} */\n" + " " * 145_000, encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Explorer bundle exceeds the JavaScript budget"):
        dag_viewer.viewer_script()


@pytest.mark.parametrize("name", (*_SOURCE_NAMES, _BUNDLE.name))
def test_explorer_scripts_parse_as_classic_scripts(name: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is only needed for development syntax checks, not Python publication")
    result = subprocess.run([node, "--check", str(_ASSETS / name)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
