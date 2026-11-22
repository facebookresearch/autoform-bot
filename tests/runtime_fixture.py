"""A Lake project stub, a Lean runtime configuration and fake Lean resources for runtime tests that start no Lean."""

from __future__ import annotations

from servers.lean_runtime import LeanRuntimeConfig


def make_lake_project(tmp_path, name: str):
    project = tmp_path / name
    project.mkdir()
    (project / "lakefile.toml").write_text(f'[package]\nname = "{name}"\n')
    return project


def runtime_config(**overrides):
    values = {
        "max_projects": 2,
        "idle_seconds": 1800.0,
        "total_repl_workers": 2,
        "repl_workers_per_project": 1,
        "repl_project_limit": 2,
        "repl_command": ("lake", "exe", "repl"),
        "repl_header_command": ("lake", "env", "python", "-c", "header"),
        "lsp_command": ("lake", "serve"),
        "lsp_timeout": 60.0,
        "max_lsp_request_seconds": 600.0,
        "repl_request_timeout": 30.0,
        "max_repl_request_seconds": 240.0,
        "rpc_read_timeout": 1.0,
        "max_connections": 8,
        "response_timeout": 900.0,
        "diagnostics_command": ("lake", "--no-build", "lean"),
    }
    values.update(overrides)
    return LeanRuntimeConfig(**values)


class FakePool:
    """A project's REPL slot pool that records each call and answers it cleanly."""

    def __init__(self, root):
        self.root = root
        self.capacity = 1
        self._shutdown = False
        self.requires_retirement = False
        self.calls = []

    def run(self, code, **kwargs):
        self.calls.append((code, kwargs))
        return {"messages": []}

    def get_memory_usage(self):
        return 0.25

    def shutdown(self):
        self._shutdown = True


class FakeLsp:
    """A project's language server session that is alive until it is closed or retired."""

    def __init__(self, root):
        self.root = root
        self.closed = False

    def close(self):
        self.closed = True

    def retire(self):
        self.closed = True

    def is_alive(self):
        return not self.closed
