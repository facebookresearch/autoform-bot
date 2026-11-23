"""Contracts for Autoform's domain-oriented server boundary."""

from __future__ import annotations

import asyncio

import pytest

from tests.runtime_fixture import make_lake_project
from tests.unexaminable import unexaminable


def required_tool_parameters(server):
    tools = asyncio.run(server.list_tools())
    return {tool.name: set(tool.parameters["required"]) for tool in tools}


class TestProjectResolution:
    """Lean roots are explicit and never inferred from the server cwd."""

    def test_accepts_absolute_lake_project(self, tmp_path):
        from servers import resolve_lean_project_dir

        project = make_lake_project(tmp_path, "lean-project")
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

        project = make_lake_project(tmp_path, "lean-project")
        (project / "Autoform").mkdir()
        (project / "Autoform" / "Main.lean").write_text("example : True := by trivial\n")
        root, lean_file = resolve_lean_file(str(project), "Autoform/Main.lean")
        assert root == project.resolve()
        assert lean_file == project.resolve() / "Autoform/Main.lean"

    def test_rejects_file_outside_declared_project(self, tmp_path):
        from servers import resolve_lean_file

        project = make_lake_project(tmp_path, "lean-project")
        outside = tmp_path / "Outside.lean"
        outside.write_text("example : True := by trivial\n")

        with pytest.raises(ValueError, match="inside project_dir"):
            resolve_lean_file(str(project), str(outside))

    @pytest.mark.parametrize("name", ["Missing.lean", "README.md"])
    def test_rejects_missing_or_non_lean_file(self, tmp_path, name):
        from servers import resolve_lean_file

        project = make_lake_project(tmp_path, "lean-project")
        if name.endswith(".md"):
            (project / name).write_text("not Lean\n")

        with pytest.raises(ValueError, match="file_path"):
            resolve_lean_file(str(project), name)

    def test_names_a_lean_file_by_leans_rule_on_every_python(self, tmp_path):
        # `..lean` is Lean's module `«.»` and `.lean` is no source; Python 3.14's PurePath finds no suffix in either.
        from servers import resolve_lean_file

        project = make_lake_project(tmp_path, "lean-project")
        for name in ("..lean", ".lean"):
            (project / name).write_text("example : True := by trivial\n")

        assert resolve_lean_file(str(project), "..lean")[1] == (project / "..lean").resolve()
        with pytest.raises(ValueError, match="must name a .lean file"):
            resolve_lean_file(str(project), ".lean")

    def test_a_lean_file_that_cannot_be_examined_is_unreadable_on_every_python(self, tmp_path, monkeypatch):
        # From Python 3.14, is_file() gives False for a path it cannot examine, which said the file did not
        # exist. Before 3.14 it raised, as it does again.
        from servers import resolve_lean_file

        project = make_lake_project(tmp_path, "lean-project")
        (project / "Main.lean").write_text("example : True := by trivial\n")
        unexaminable(monkeypatch, project.resolve() / "Main.lean")

        with pytest.raises(PermissionError):
            resolve_lean_file(str(project), "Main.lean")

    def test_lake_metadata_that_cannot_be_examined_is_unreadable_on_every_python(self, tmp_path, monkeypatch):
        # As above: such a lakefile made the project read as one without Lake metadata.
        from servers import resolve_lean_project_dir

        project = make_lake_project(tmp_path, "lean-project")
        unexaminable(monkeypatch, project.resolve() / "lakefile.toml")

        with pytest.raises(PermissionError):
            resolve_lean_project_dir(str(project))

    def test_the_servers_read_a_missing_path_as_autoform_does(self):
        # The servers cannot import autoform_cli, so they keep a copy of its rule.
        import servers
        from autoform_cli import _fs

        assert (servers._ABSENT_ERRNOS, servers._ABSENT_WINERRORS) == (_fs._ABSENT_ERRNOS, _fs._ABSENT_WINERRORS)


# ---------------------------------------------------------------------------
# REPL server
# ---------------------------------------------------------------------------


class TestReplServer:
    """Contracts for the Lean REPL MCP adapter."""

    def test_tools_require_project_dir(self):
        from servers.repl.server import create_repl_server

        server = create_repl_server(object())
        assert server.name == "autoform-repl"
        required = required_tool_parameters(server)
        assert required["run_lean_code"] == {"project_dir", "code"}
        assert required["get_repl_status"] == {"project_dir"}
        assert set(required) == {"run_lean_code", "get_repl_status"}


# ---------------------------------------------------------------------------
# LSP backend
# ---------------------------------------------------------------------------


class TestLspServer:
    """Contracts for the Lean language-server service."""

    def test_tools_require_project_dir(self, monkeypatch):
        from servers.lsp.server import create_lsp_server

        monkeypatch.delenv("AUTOFORM_LSP_TOOLSET", raising=False)
        server = create_lsp_server(object())
        assert server.name == "autoform-lsp"
        required = required_tool_parameters(server)
        assert required["lean_diagnostic_messages"] == {"project_dir", "file_path"}
        assert required["lean_hover"] == {"project_dir", "file_path", "line", "character"}
        assert required["lean_goal"] == {"project_dir", "file_path", "line", "character"}
        assert required["lean_check_buffer"] == {"project_dir", "file_path", "content", "owner"}
        assert required["lean_try_tactics"] == {"project_dir", "file_path", "line", "tactics", "owner"}
        # The battery is fixed, so lean_try_close takes no candidate list.
        assert required["lean_try_close"] == {"project_dir", "file_path", "line", "owner"}
        assert set(required) == {
            "lean_diagnostic_messages",
            "lean_hover",
            "lean_goal",
            "lean_check_buffer",
            "lean_try_tactics",
            "lean_try_close",
        }


_WHERE = {"project_dir": "/lean", "file_path": "Main.lean"}


@pytest.mark.parametrize(
    ("tool", "arguments", "method"),
    [
        ("run_lean_code", {"project_dir": "/lean", "code": "#check Nat", "timeout": None}, "repl.run"),
        ("get_repl_status", {"project_dir": "/lean"}, "repl.status"),
        ("lean_diagnostic_messages", _WHERE, "lsp.diagnostics"),
        ("lean_hover", {**_WHERE, "line": 0, "character": 3}, "lsp.hover"),
        ("lean_goal", {**_WHERE, "line": 1, "character": 2}, "lsp.goal"),
        ("lean_check_buffer", {**_WHERE, "content": "", "owner": "me"}, "lsp.check_buffer"),
        ("lean_try_tactics", {**_WHERE, "line": 1, "tactics": ["rfl"], "owner": "me"}, "lsp.try_tactics"),
    ],
)
def test_a_tool_call_is_one_runtime_request_with_its_arguments(monkeypatch, tool, arguments, method):
    # The stdio adapters own no Lean state. lean_try_close's request is in test_lsp_interactive.py.
    from servers.lsp.server import create_lsp_server
    from servers.repl.server import create_repl_server

    class RecordingRuntime:
        def __init__(self):
            self.calls = []

        def request(self, name, params):
            self.calls.append((name, params))
            return "delegated"

    monkeypatch.delenv("AUTOFORM_LSP_TOOLSET", raising=False)
    runtime = RecordingRuntime()
    server = (create_repl_server if method.startswith("repl.") else create_lsp_server)(runtime)

    asyncio.run(server.call_tool(tool, arguments))

    assert runtime.calls == [(method, arguments)]


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
