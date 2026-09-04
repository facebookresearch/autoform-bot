"""Host-facing packaging and plugin-surface checks."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory


def test_main_plugin_surface_excludes_deicyde_orchestration(repo_root):
    skills = {path.parent.name for path in (repo_root / "skills").glob("*/SKILL.md")}
    assert skills == {
        "setup",
        "roadmap",
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
        "minimal-declaration-review.md",
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
    assert len(codex_manifest["interface"]["defaultPrompt"]) == 5
    muse = json.loads((repo_root / ".muse-plugin/plugin.json").read_text())
    assert [command["id"] for command in muse["capabilities"]["commands"]] == [
        "setup",
        "roadmap",
        "human-review",
        "agent-review",
        "develop-plugin",
    ]
    for command in muse["capabilities"]["commands"]:
        assert (repo_root / command["path"]).is_file()


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
            "servers/lean_client.py",
            "servers/lean_runtime.py",
            "servers/lsp/server.py",
            "servers/repl/core.py",
            "servers/repl/server.py",
        } <= names
        assert "autoform_cli/lake.py" not in names
        assert "autoform_cli/templates/github/autoform_audit.py" in names
        assert not any(
            name.startswith(("scripts/", "autoform/", "visualization/", "servers/lean/", "servers/search/"))
            for name in names
        )
        entry_points = archive.read(
            next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        ).decode()
        assert "autoform-lean-runtime = servers.lean_runtime:main" in entry_points
        metadata = archive.read(
            next(name for name in names if name.endswith(".dist-info/METADATA"))
        ).decode()
        assert "Requires-Dist: psutil>=5.9" in metadata
        assert "Requires-Dist: tomli" not in metadata
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
