"""Host-facing packaging and plugin-surface checks."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from autoform_cli.markdown import local_target_issue, markdown_links

import pytest


def _shipped_path(repo_root: Path, value: str) -> Path:
    root = repo_root.resolve()
    declared = Path(value)
    assert not declared.is_absolute()
    resolved = (root / declared).resolve()
    assert resolved.is_relative_to(root)
    return resolved


def test_plugin_surface_advertises_cli_backed_orchestration(repo_root):
    skills = {path.parent.name for path in (repo_root / "skills").glob("*/SKILL.md")}
    assert skills == {
        "setup",
        "roadmap",
        "orchestrate",
        "human-review",
        "agent-review",
        "develop-plugin",
    }

    review_dir = repo_root / "skills" / "agent-review"
    references = {
        "faithfulness.md",
        "proof-integrity.md",
        "code-quality.md",
        "mathlib-style.md",
        "roadmap-quality.md",
        "thesis-review-case.md",
    }
    assert {path.name for path in (review_dir / "references").glob("*.md")} == references
    skill_text = (review_dir / "SKILL.md").read_text()
    assert all(f"references/{name}" in skill_text for name in references)

    codex = json.loads((repo_root / ".mcp.json").read_text())
    claude = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())
    expected = {"autoform-lsp", "autoform-repl"}
    assert set(codex["mcpServers"]) == expected
    assert set(claude["mcpServers"]) == expected
    assert "hooks" not in claude

    expected_modules = {
        "autoform-lsp": "servers.lsp.server",
        "autoform-repl": "servers.repl.server",
    }
    for config in (codex, claude):
        for name, module in expected_modules.items():
            assert config["mcpServers"][name]["args"][-2:] == ["-m", module]

    codex_manifest = json.loads((repo_root / ".codex-plugin/plugin.json").read_text())
    assert len(codex_manifest["interface"]["defaultPrompt"]) == 6
    assert any("Autoform CLI" in prompt for prompt in codex_manifest["interface"]["defaultPrompt"])
    muse = json.loads((repo_root / ".muse-plugin/plugin.json").read_text())
    assert [command["id"] for command in muse["capabilities"]["commands"]] == [
        "setup",
        "roadmap",
        "orchestrate",
        "human-review",
        "agent-review",
        "develop-plugin",
    ]
    assert _shipped_path(repo_root, muse["compat"]["manifestDir"]) == (
        repo_root / ".muse-plugin"
    ).resolve()
    for command in muse["capabilities"]["commands"]:
        assert _shipped_path(repo_root, command["path"]).is_file()


def test_shipped_skill_links_resolve_within_the_plugin(repo_root):
    documents = sorted((repo_root / "skills").glob("*/SKILL.md"))
    documents += sorted((repo_root / "skills").glob("*/references/**/*.md"))
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for line_number, target in markdown_links(text):
            issue = local_target_issue(document, target, repo_root, label="skill")
            assert issue is None, (
                f"{document.relative_to(repo_root)}:{line_number}: {issue[1]}"
            )


def test_plugin_manifests_reference_shipped_paths_and_modules(repo_root):
    root = repo_root.resolve()
    expected_modules = {
        "autoform-lsp": "servers.lsp.server",
        "autoform-repl": "servers.repl.server",
    }

    codex = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    for key in ("skills", "mcpServers"):
        assert _shipped_path(root, codex[key]).exists()
    for key in ("composerIcon", "logo"):
        assert _shipped_path(root, codex["interface"][key]).is_file()

    marketplace = json.loads((root / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    for plugin in marketplace["plugins"]:
        assert _shipped_path(root, plugin["source"]) == root

    mappings = [
        json.loads((root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"],
        json.loads((root / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))[
            "mcpServers"
        ],
    ]
    for servers in mappings:
        assert set(servers) == set(expected_modules)
        for server_id, module in expected_modules.items():
            assert servers[server_id]["args"][-2:] == ["-m", module]
            assert _shipped_path(root, f"{module.replace('.', '/')}.py").is_file()

    muse = json.loads((root / ".muse-plugin/plugin.json").read_text(encoding="utf-8"))
    muse_servers = {server["id"]: server for server in muse["capabilities"]["mcpServers"]}
    assert set(muse_servers) == set(expected_modules)
    for server_id, module in expected_modules.items():
        assert muse_servers[server_id]["command"][-3:] == ["python", "-m", module]
        assert _shipped_path(root, f"{module.replace('.', '/')}.py").is_file()


def test_mcp_launchers_use_plugin_only_as_the_uv_project(repo_root):
    codex = json.loads((repo_root / ".mcp.json").read_text())
    for server in codex["mcpServers"].values():
        assert server["cwd"] == "${CLAUDE_PLUGIN_ROOT}"
        assert server["args"][:3] == ["run", "--project", "${CLAUDE_PLUGIN_ROOT}"]

    claude = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())
    for server in claude["mcpServers"].values():
        assert server["cwd"] == "${CLAUDE_PLUGIN_ROOT}"
        assert server["args"][:3] == ["run", "--project", "${CLAUDE_PLUGIN_ROOT}"]
        assert "LEAN_PROJECT_DIR" not in json.dumps(server)


@pytest.mark.installed_wheel
def test_wheel_contains_only_the_minimal_runtime(repo_root, tmp_path):
    dist = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    wheel, = dist.glob("*.whl")
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert {
            "autoform_cli/__main__.py",
            "autoform_cli/graph.py",
            "autoform_cli/visualize.py",
            "autoform_cli/project/create.py",
            "autoform_cli/project/repair.py",
            "autoform_cli/project/releases.json",
            "autoform_cli/ready.py",
            "servers/lean_client.py",
            "servers/lean_runtime.py",
            "servers/lsp/server.py",
            "servers/repl/core.py",
            "servers/repl/server.py",
        } <= names
        assert "autoform_cli/lake.py" not in names
        assert not any(name.startswith(("autoform_worker/", "servers/prover/")) for name in names)
        assert "autoform_cli/templates/github/autoform_audit.py" in names
        assert not any(
            name.startswith(("scripts/", "autoform/", "visualization/", "servers/lean/", "servers/search/"))
            for name in names
        )
        entry_points = archive.read(
            next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        ).decode()
        assert "autoform-lean-runtime = servers.lean_runtime:main" in entry_points
        assert "autoform-worker" not in entry_points
        metadata = archive.read(
            next(name for name in names if name.endswith(".dist-info/METADATA"))
        ).decode()
        assert "Requires-Dist: psutil>=5.9" in metadata
        assert "Requires-Dist: tomli>=2.0; python_version < '3.11'" in metadata
        assert "Provides-Extra: repl" in metadata
        archive.extractall(site)

    with TemporaryDirectory(prefix="autoform-wheel-", dir="/tmp") as runtime_dir:
        probe = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                """
import sys
from pathlib import Path
site = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(site))
from autoform_cli import graph, visualize
from servers import lean_client, lean_runtime
from servers.lsp import server as lsp_server
from servers.repl import server as repl_server
assert Path(graph.__file__).resolve().is_relative_to(site)
assert Path(lean_client.__file__).resolve().is_relative_to(site)
assert Path(lean_runtime.__file__).resolve().is_relative_to(site)
assert Path(lsp_server.__file__).resolve().is_relative_to(site)
assert Path(repl_server.__file__).resolve().is_relative_to(site)
assert Path(visualize.__file__).resolve().is_relative_to(site)
client = lean_client.LeanRuntimeClient(socket_path=sys.argv[2], startup_timeout=15)
try:
    assert client.ensure_running()["install_id"] == lean_client.INSTALL_ID
finally:
    client.stop()
""",
                str(site),
                str(Path(runtime_dir) / "runtime.sock"),
            ],
            # Deliberately run beside the source checkout. The installed client
            # must launch the installed daemon, not import this cwd's servers/.
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
    assert probe.returncode == 0, probe.stderr

    environment = tmp_path / "wheel-venv"
    created = subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(environment)],
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0, created.stderr
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    installed = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    command = environment / ("Scripts/autoform.exe" if sys.platform == "win32" else "bin/autoform")
    outside = tmp_path / "outside"
    outside.mkdir()
    project = outside / "project"
    versions = subprocess.run(
        [str(command), "project", "versions", "--json"],
        cwd=outside,
        capture_output=True,
        text=True,
    )
    assert versions.returncode == 0, versions.stderr
    assert json.loads(versions.stdout)["schema"] == "autoform-project-release-catalog/v1"
    creation = subprocess.run(
        [
            str(command),
            "project",
            "new",
            str(project),
            "--package",
            "WheelProject",
            "--release",
            "lean-v4.32.2-mathlib-v4.32.2",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
    )
    assert creation.returncode == 0, creation.stderr
    assert json.loads(creation.stdout)["package"] == "WheelProject"
    (project / "mkdocs.yml").unlink()
    repair_inputs = ["--title", "WheelProject", "--repository-url", ""]
    repair_preview = subprocess.run(
        [
            str(command),
            "project",
            "repair",
            str(project),
            *repair_inputs,
            "--dry-run",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
    )
    assert repair_preview.returncode == 0, repair_preview.stderr
    assert json.loads(repair_preview.stdout)["planned"] == ["mkdocs.yml"]
    repaired = subprocess.run(
        [str(command), "project", "repair", str(project), *repair_inputs, "--json"],
        cwd=outside,
        capture_output=True,
        text=True,
    )
    assert repaired.returncode == 0, repaired.stderr
    assert json.loads(repaired.stdout)["written"] == ["mkdocs.yml"]
    repaired_again = subprocess.run(
        [str(command), "project", "repair", str(project), *repair_inputs, "--json"],
        cwd=outside,
        capture_output=True,
        text=True,
    )
    assert repaired_again.returncode == 0, repaired_again.stderr
    assert json.loads(repaired_again.stdout)["planned"] == []
    inspection = subprocess.run(
        [str(command), "project", "inspect", str(project), "--json"],
        cwd=outside,
        capture_output=True,
        text=True,
    )
    assert inspection.returncode == 0, inspection.stderr
    assert json.loads(inspection.stdout)["lake"]["name"] == "WheelProject"
