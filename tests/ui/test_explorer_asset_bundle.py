"""The shipped explorer must match its readable sources and fit the existing runtime budget."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from autoform_cli.ui import dag_viewer

_ROOT = Path(__file__).resolve().parents[2]
_ASSETS = _ROOT / "autoform_cli/ui/assets"
_BUNDLE = _ASSETS / "blueprint-explorer.min.js"
_SOURCE_NAMES = ("tex-plain.js", "blueprint-navigation.js", "blueprint-dag.js")
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
    (project / "autoform_cli/ui/assets").mkdir(parents=True)
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
    assets = project / "autoform_cli/ui/assets"
    sources = ("// plain\nwindow.AutoformTexPlain={math(t){return t;}};\n",
               "// café\nwindow.AutoformNavigation={create(){return null;}};\n", "// map\nwindow.Explorer={route:'article.html'};\n")
    for name, source in zip(_SOURCE_NAMES, sources, strict=True):
        data = source.replace("\n", ending).encode("utf-8")
        (assets / name).write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
    compiler = project / "scripts/build-explorer.mjs"
    result = subprocess.run([node, str(compiler)], capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    monkeypatch.setattr(dag_viewer, "__file__", str(project / "autoform_cli/ui/dag_viewer.py"))
    source = "\n".join(dag_viewer._asset_text(name) for name in _SOURCE_NAMES)
    monkeypatch.setattr(dag_viewer, "_script", lambda: source)
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
    assets = project / "autoform_cli/ui/assets"
    (assets / _SOURCE_NAMES[0]).write_bytes(b"\xff")
    for name in _SOURCE_NAMES[1:]:
        (assets / name).write_text("window.Explorer={};\n", encoding="utf-8")
    result = subprocess.run([node, str(project / "scripts/build-explorer.mjs")], capture_output=True, text=True, timeout=45)
    assert result.returncode != 0 and not (assets / _BUNDLE.name).exists()


def test_explorer_bundle_has_no_source_maps_or_local_paths() -> None:
    # Its size budget is the publisher's to enforce (test_publication_rejects_oversized_bundle_...).
    bundled = _BUNDLE.read_bytes()
    bundled.decode("utf-8")
    assert b"sourceMappingURL=" not in bundled
    assert str(_ROOT).encode("utf-8") not in bundled
    assert b"/Users/" not in bundled and b"/home/runner/" not in bundled


def test_explorer_compiler_is_private_development_tooling() -> None:
    """Publication needs no Node: the compiler and the DOM bridge are private development dependencies only. The build
    script, check_jsdom and npm ci each refuse other versions than the lock's."""
    package = json.loads((_ROOT / "package.json").read_text(encoding="utf-8"))
    assert package["private"] is True
    assert not package.get("dependencies")


def test_publication_rejects_stale_bundle_instead_of_using_raw_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    edited = dag_viewer._script() + "\n// unbuilt source edit\n"
    monkeypatch.setattr(dag_viewer, "_script", lambda: edited)
    with pytest.raises(ValueError, match="Explorer bundle is stale"):
        dag_viewer.viewer_script()


def test_importing_the_renderer_reads_no_explorer_asset() -> None:
    """check imports the renderer for its publication warnings, and shows no explorer."""
    probe = (
        "import sys\n"
        "opened = []\n"
        "sys.addaudithook(lambda event, args: event == 'open' and opened.append(str(args[0]).replace(chr(92), '/')))\n"
        "import autoform_cli.ui.render\n"
        "at_import = [path for path in opened if '/ui/assets/' in path]\n"
        "from autoform_cli.ui import dag_viewer\n"
        "dag_viewer._style()\n"
        "print(at_import, any('/ui/assets/' in path for path in opened))\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=_ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    # The hook does see the stylesheet once something asks for it.
    assert result.stdout.split() == ["[]", "True"]


@pytest.mark.parametrize("contents", [None, b"\xff"])
def test_publication_rejects_missing_or_corrupt_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: bytes | None,
) -> None:
    # The sources stay the package's; only the bundle moves.
    source = dag_viewer._script()
    monkeypatch.setattr(dag_viewer, "_script", lambda: source)
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
    source = dag_viewer._script()
    monkeypatch.setattr(dag_viewer, "_script", lambda: source)
    monkeypatch.setattr(dag_viewer, "__file__", str(tmp_path / "dag_viewer.py"))
    assets = tmp_path / "assets"
    assets.mkdir()
    fingerprint = hashlib.sha256(source.encode("utf-8")).hexdigest()
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
