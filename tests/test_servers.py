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
    """Contracts for the persistent Lean REPL server."""

    def test_import_server(self):
        from servers.repl import server  # noqa: F401

    def test_import_core(self):
        from servers.repl import core  # noqa: F401

    def test_import_pool(self):
        from servers.repl import pool  # noqa: F401

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

    def test_project_router_reuses_and_separates_pools(self, tmp_path):
        from servers.repl.projects import LeanReplProjects

        class FakePool:
            def __init__(self, root):
                self.root = root
                self.closed = False

            def shutdown(self):
                self.closed = True

        created = []

        def factory(root):
            pool = FakePool(root)
            created.append(pool)
            return pool

        first = make_lake_project(tmp_path, "first")
        second = make_lake_project(tmp_path, "second")
        projects = LeanReplProjects(factory)

        assert projects.get(str(first)) is projects.get(str(first))
        assert projects.get(str(first)) is not projects.get(str(second))
        assert [pool.root for pool in created] == [first.resolve(), second.resolve()]

        projects.shutdown()
        assert all(pool.closed for pool in created)


# ---------------------------------------------------------------------------
# LSP backend
# ---------------------------------------------------------------------------


class TestLspServer:
    """Contracts for the Lean language-server service."""

    def test_import_server(self):
        from servers.lsp import server  # noqa: F401

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

    def test_project_router_reuses_and_separates_sessions(self, tmp_path):
        from servers.lsp.server import LeanLspProjects

        class FakeSession:
            def __init__(self, root):
                self.root = root
                self.closed = False

            def close(self):
                self.closed = True

        created = []

        def factory(root):
            session = FakeSession(root)
            created.append(session)
            return session

        first = make_lake_project(tmp_path, "first")
        second = make_lake_project(tmp_path, "second")
        projects = LeanLspProjects(factory)

        assert projects.get(str(first)) is projects.get(str(first))
        assert projects.get(str(first)) is not projects.get(str(second))
        assert [session.root for session in created] == [first.resolve(), second.resolve()]

        projects.close()
        assert all(session.closed for session in created)
