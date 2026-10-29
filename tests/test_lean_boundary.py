"""Process and import boundaries that a review can miss.

Within autoform_cli/, only lean_probe.py looks programs up on PATH or starts a
process other than Git or taskkill, so every Lean and Lake run gets its
timeouts, output caps and descendant cleanup. servers/ never imports
autoform_cli, because the runtime daemon's code fingerprint
(servers/lean_client.py) covers servers/ only. autoform_cli never imports the
top-level servers package by name, because under `python -m autoform_cli` a
consumer's own `servers` would be found first.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

_RUNNER = "autoform_cli/lean_probe.py"
# Programs any module may start, named by the literal first element of the argument list.
# taskkill stops a Git command's process tree on Windows (claims.py).
_ANY_MODULE = {"git", "taskkill"}


def _starts_a_process(name: str) -> bool:
    """Whether a call to the dotted *name* starts a process or searches PATH."""
    module, _, function = name.partition(".")
    if module == "subprocess":
        return function in {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
    if module == "os":
        return function in {"system", "popen", "startfile"} or function.startswith(("exec", "spawn", "posix_spawn"))
    return name in {"asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell", "pty.spawn", "shutil.which"}


def _process_starts(path: Path, where: str) -> list[tuple[int, str, str | None]]:
    """Each process start or PATH search in *path*: line, function, literal program name."""
    tree = ast.parse(path.read_text(encoding="utf-8"), where)
    imported = {}  # local name -> dotted name, so `from subprocess import Popen as P` resolves
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.partition(".")[0]
                imported[alias.asname or top] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                imported[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    # A name bound once to a list literal, as in `git = ["git", ...]` then `run([*git, ...])`.
    stores = Counter(
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    )
    heads = {
        node.targets[0].id: node.value.elts[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and stores[node.targets[0].id] == 1
        and isinstance(node.value, (ast.List, ast.Tuple))
        and node.value.elts
    }
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parts, target = [], node.func
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if not isinstance(target, ast.Name) or target.id not in imported:
            continue
        name = ".".join([imported[target.id], *reversed(parts)])
        if not _starts_a_process(name):
            continue
        command = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "args"), None)
        first = command.elts[0] if isinstance(command, (ast.List, ast.Tuple)) and command.elts else None
        if isinstance(first, ast.Starred) and isinstance(first.value, ast.Name):
            first = heads.get(first.value.id)
        program = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
        found.append((node.lineno, name, program))
    return found


def test_only_lean_probe_starts_programs_other_than_git(repo_root: Path) -> None:
    found = []
    for path in sorted((repo_root / "autoform_cli").rglob("*.py")):
        where = path.relative_to(repo_root).as_posix()
        if where == _RUNNER:
            assert _process_starts(path, where), f"the scan no longer finds {_RUNNER}'s own process starts"
            continue
        found += [
            f"{where}:{line}: {name}({program or '...'})"
            for line, name, program in _process_starts(path, where)
            if name == "shutil.which" or program not in _ANY_MODULE
        ]
    assert not found, (
        f"only {_RUNNER} may search PATH or start a program other than git or taskkill;"
        ' elsewhere, name the program as a literal, as in ["git", ...]:\n' + "\n".join(found)
    )


def test_servers_never_import_autoform_cli(repo_root: Path) -> None:
    found = []
    for path in sorted((repo_root / "servers").rglob("*.py")):
        where = path.relative_to(repo_root).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), where)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = [node.value]  # importlib.import_module("autoform_cli..."), __import__, `-m autoform_cli`
            else:
                continue
            found += [f"{where}:{node.lineno}: {name}" for name in names if re.fullmatch(r"autoform_cli(\.\w+)*", name)]
    assert not found, "servers/ must not import autoform_cli (the daemon fingerprints servers/ only):\n" + "\n".join(
        found
    )


def test_autoform_cli_never_imports_servers(repo_root: Path) -> None:
    found = []
    for path in sorted((repo_root / "autoform_cli").rglob("*.py")):
        where = path.relative_to(repo_root).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), where)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            found += [f"{where}:{node.lineno}: {name}" for name in names if re.fullmatch(r"servers(\.\w+)*", name)]
    assert not found, (
        "autoform_cli/ must not import the top-level servers package"
        " (under python -m autoform_cli, a consumer's own servers is found first):\n" + "\n".join(found)
    )
