"""Contracts for Autoform's domain-oriented server boundary."""

from __future__ import annotations

import asyncio

import pytest


def make_lake_project(tmp_path, name: str = "lean-project"):
    project = tmp_path / name
    project.mkdir()
    (project / "lakefile.toml").write_text('[package]\nname = "Test"\n')
    return project


def required_tool_parameters(server):
    tools = asyncio.run(server.list_tools())
    return {tool.name: set(tool.parameters["required"]) for tool in tools}


class TestProjectResolution:
    """Lean roots are explicit and never inferred from the server cwd."""

    def test_accepts_absolute_lake_project(self, tmp_path):
        from servers import resolve_lean_project_dir

        project = make_lake_project(tmp_path)
        assert resolve_lean_project_dir(str(project)) == project.resolve()

    @pytest.mark.parametrize("value", ["", ".", "relative/project"])
    def test_rejects_missing_or_relative_project(self, value):
        from servers import resolve_lean_project_dir

        with pytest.raises(ValueError, match="project_dir"):
            resolve_lean_project_dir(value)

    def test_rejects_directory_without_lake_metadata(self, tmp_path):
        from servers import resolve_lean_project_dir

        with pytest.raises(ValueError, match="not a Lake project"):
            resolve_lean_project_dir(str(tmp_path))

    def test_resolves_relative_file_from_project(self, tmp_path):
        from servers import resolve_lean_file

        project = make_lake_project(tmp_path)
        (project / "Autoform").mkdir()
        (project / "Autoform" / "Main.lean").write_text("example : True := by trivial\n")
        root, lean_file = resolve_lean_file(str(project), "Autoform/Main.lean")
        assert root == project.resolve()
        assert lean_file == project.resolve() / "Autoform/Main.lean"

    def test_rejects_file_outside_declared_project(self, tmp_path):
        from servers import resolve_lean_file

        project = make_lake_project(tmp_path)
        outside = tmp_path / "Outside.lean"
        outside.write_text("example : True := by trivial\n")

        with pytest.raises(ValueError, match="inside project_dir"):
            resolve_lean_file(str(project), str(outside))

    @pytest.mark.parametrize("name", ["Missing.lean", "README.md"])
    def test_rejects_missing_or_non_lean_file(self, tmp_path, name):
        from servers import resolve_lean_file

        project = make_lake_project(tmp_path)
        if name.endswith(".md"):
            (project / name).write_text("not Lean\n")

        with pytest.raises(ValueError, match="file_path"):
            resolve_lean_file(str(project), name)


# ---------------------------------------------------------------------------
# REPL server
# ---------------------------------------------------------------------------


class TestReplServer:
    """Contracts for the Lean REPL MCP adapter."""

    def test_create_server(self):
        from servers.repl.server import create_repl_server

        server = create_repl_server(object())
        assert server is not None
        assert server.name == "autoform-repl"

    def test_tools_require_project_dir(self):
        from servers.repl.server import create_repl_server

        required = required_tool_parameters(create_repl_server(object()))
        assert required["run_lean_code"] == {"project_dir", "code"}
        assert required["get_repl_status"] == {"project_dir"}
        assert set(required) == {"run_lean_code", "get_repl_status"}


# ---------------------------------------------------------------------------
# LSP backend
# ---------------------------------------------------------------------------


class TestLspServer:
    """Contracts for the Lean language-server service."""

    def test_create_server(self):
        from servers.lsp.server import create_lsp_server

        server = create_lsp_server(object())
        assert server is not None
        assert server.name == "autoform-lsp"

    def test_tools_require_project_dir(self):
        from servers.lsp.server import create_lsp_server

        required = required_tool_parameters(create_lsp_server(object()))
        assert required["lean_diagnostic_messages"] == {"project_dir", "file_path"}
        assert required["lean_hover"] == {"project_dir", "file_path", "line", "character"}
        assert set(required) == {"lean_diagnostic_messages", "lean_hover"}


def test_the_runtime_fingerprint_covers_every_module_the_daemon_runs(repo_root):
    """A live daemon is replaced only when a file in `_RUNTIME_SOURCES` changes, so every
    servers module the daemon imports must be listed, apart from servers/__init__.py,
    which the fingerprint reads too, and package files that hold only a docstring."""
    import ast
    import json
    import subprocess
    import sys
    from pathlib import Path

    script = (
        "import json, sys\n"
        "import servers.lean_runtime\n"
        "from servers.lean_client import _RUNTIME_SOURCES\n"
        "loaded = [m.__file__ for m in list(sys.modules.values()) if m.__name__.split('.')[0] == 'servers']\n"
        "print(json.dumps([sorted(loaded), [str(p) for p in _RUNTIME_SOURCES]]))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=repo_root, capture_output=True, text=True, check=True)
    loaded, sources = json.loads(result.stdout)
    fingerprinted = {Path(path).resolve() for path in sources} | {(repo_root / "servers" / "__init__.py").resolve()}

    def docstring_only(path: Path) -> bool:
        body = ast.parse(path.read_text(encoding="utf-8")).body
        return all(isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) for node in body)

    missing = [
        path
        for path in (Path(name).resolve() for name in loaded)
        if path not in fingerprinted and not (path.name == "__init__.py" and docstring_only(path))
    ]
    assert not missing, f"add these to _RUNTIME_SOURCES in servers/lean_client.py: {missing}"
