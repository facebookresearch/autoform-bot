"""Host-facing packaging and plugin-surface checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


def test_main_plugin_surface_excludes_deicyde_orchestration(repo_root):
    skills = {path.parent.name for path in (repo_root / "skills").glob("*/SKILL.md")}
    assert skills == {
        "setup",
        "roadmap",
        "formalize",
        "human-review",
        "agent-review",
        "develop-plugin",
    }

    review_dir = repo_root / "skills" / "agent-review"
    references = {
        "faithfulness.md",
        "readback-faithfulness.md",
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
    interface = codex_manifest["interface"]
    assert len(interface["shortDescription"]) <= 30
    assert interface["category"] == "Developer Tools"
    default_prompts = interface["defaultPrompt"]
    assert len(default_prompts) == 3
    assert all(len(prompt) <= 128 and "\n" not in prompt for prompt in default_prompts)
    assert any(
        "source-grounded Autoform roadmap" in prompt and "persistent Goal" in prompt
        for prompt in default_prompts
    )
    assert any("Formalize the ready Markdown roadmap frontier" in prompt for prompt in default_prompts)
    assert not any(
        "claim-backed workers" in prompt
        for prompt in default_prompts
    )
    muse = json.loads((repo_root / ".muse-plugin/plugin.json").read_text())
    assert [command["id"] for command in muse["capabilities"]["commands"]] == [
        "setup",
        "roadmap",
        "formalize",
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


@pytest.mark.skipif(os.name != "posix", reason="project new requires POSIX publication")
def test_copied_plugin_project_entrypoints_need_no_autoform_on_path(repo_root, tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    for name in ("LICENSE", "pyproject.toml", "uv.lock"):
        shutil.copy2(repo_root / name, plugin / name)
    for package in ("autoform_cli", "servers"):
        shutil.copytree(
            repo_root / package,
            plugin / package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    environment = os.environ.copy()
    environment["PATH"] = str(empty_path)
    environment.pop("VIRTUAL_ENV", None)
    uv = shutil.which("uv")
    assert uv is not None
    assert shutil.which("autoform", path=environment["PATH"]) is None

    def run(*arguments: str, cwd: Path = tmp_path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                uv,
                "run",
                "--python",
                sys.executable,
                "--project",
                str(plugin),
                "autoform",
                *arguments,
            ],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )

    versions = run("project", "versions", "--json")
    assert versions.returncode == 0, versions.stderr
    release = json.loads(versions.stdout)["releases"][0]

    parent = tmp_path / "consumer"
    parent.mkdir(mode=0o700)
    target = parent / "CopiedProject"
    created = run(
        "project",
        "new",
        str(target),
        "--package",
        "CopiedProject",
        "--release",
        release["id"],
        "--json",
        cwd=parent,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    payload = json.loads(created.stdout)
    assert payload["schema"] == "autoform-project-creation/v1"
    assert payload["release"] == release["id"]
    assert (target / ".gitignore").read_text(encoding="utf-8").splitlines() == [
        ".lake/",
        "site/",
        "site-src/",
        "*.log",
        ".claude/worktrees/",
    ]

    inspection = run("project", "inspect", str(target), "--json", cwd=parent)
    assert inspection.returncode == 0, inspection.stderr
    report = json.loads(inspection.stdout)
    assert report["compatibility"] == {
        "recommended_release": release["id"],
        "release": release["id"],
        "status": "supported",
    }


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
            "autoform_cli/probes/skeleton_probe.lean",
            "autoform_cli/project/README.md",
            "autoform_cli/project/_lake_metadata.py",
            "autoform_cli/project/_snapshot.py",
            "autoform_cli/visualize.py",
            "autoform_cli/project/create.py",
            "autoform_cli/project/creation-release-lean-v4.32.2-mathlib-v4.32.2.json",
            "autoform_cli/project/release-manifest-lean-v4.32.2-mathlib-v4.32.2.json",
            "autoform_cli/project/releases.json",
            "servers/lean_client.py",
            "servers/lean_runtime.py",
            "servers/lsp/server.py",
            "servers/repl/core.py",
            "servers/repl/server.py",
        } <= names
        assert "autoform_cli/lake.py" not in names
        assert "autoform_cli/templates/github/autoform_audit.py" in names
        assert "autoform_cli/assets/blueprint-live.js" in names
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
        assert "Requires-Dist: tomli<2.4,>=2.3.1" in metadata
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
    outside.mkdir(mode=0o700)
    outside.chmod(0o755)
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
    assert creation.returncode == 0, creation.stdout + creation.stderr
    assert json.loads(creation.stdout)["package"] == "WheelProject"
    inspection = subprocess.run(
        [str(command), "project", "inspect", str(project), "--json"],
        cwd=outside,
        capture_output=True,
        text=True,
    )
    assert inspection.returncode == 0, inspection.stderr
    assert json.loads(inspection.stdout)["lake"]["name"] == "WheelProject"
