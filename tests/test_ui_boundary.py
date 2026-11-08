"""Contract modules never import the display code.

autoform_cli/ui/__init__.py states the rule this test enforces. Every command
except render, site check, dashboard and check loads no display code; check
loads the renderer for its publication warnings, so a broken UI module can
still stop it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_UI = "autoform_cli.ui"
_ENTRY = "autoform_cli/__main__.py"
#: The functions in _ENTRY that may import ui/: the (module, names) one may import, or None for any.
_HANDLERS: dict[str, tuple[str, tuple[str, ...]] | None] = {
    "_check": (f"{_UI}.render", ("publication_issues",)),
    "_dashboard": None,
    "_render": None,
    "_site_check": None,
}
#: A string naming a ui/ module (importlib.import_module, also relatively; __import__; `-m`), or an
#: object in one ("autoform_cli.ui.render:render_site", as pkgutil.resolve_name reads it), or one
#: built at runtime: f"autoform_cli.ui.{name}", ".ui." + name, "%s.ui.render" % __package__. A relative
#: __import__ passes its level apart from the name, so no string scan sees it.
_UI_STRING = re.compile(r"(?:autoform_cli|\{\w*\}|%s|\.*)\.ui(?:\.\w+)*(?:\.(?:\{\w*\}|%s)?)?(?::[\w.]+)?")


def _is_ui(module: str) -> bool:
    return module == _UI or module.startswith(f"{_UI}.")


def _ui_imports(path: Path, repo_root: Path) -> list[tuple[int, str, tuple[str, ...] | None, str | None]]:
    """Each import of ui/ in *path*: its line, the module, the names it takes
    (None for a string), and the top-level function whose body holds it, or
    the class; None elsewhere, as for a function's decorators and defaults,
    which run when the module loads."""
    relative = path.relative_to(repo_root)
    package = list(relative.parts[:-1])  # where the module's relative imports start
    found: list[tuple[int, str, tuple[str, ...] | None, str | None]] = []
    for top in ast.parse(path.read_text(encoding="utf-8"), relative.as_posix()).body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            outside = [child for child in ast.iter_child_nodes(top) if child not in top.body]
            scopes = [(top.name, top.body), (None, outside)]
        elif isinstance(top, ast.ClassDef):
            scopes = [(f"class {top.name}", [top])]
        else:
            scopes = [(None, [top])]
        for holder, roots in scopes:
            for node in (node for root in roots for node in ast.walk(root)):
                if isinstance(node, ast.Import):
                    found += [(node.lineno, alias.name, (), holder) for alias in node.names if _is_ui(alias.name)]
                elif isinstance(node, ast.ImportFrom):
                    base = package[: len(package) - node.level + 1] if node.level else []
                    module = ".".join([*base, *filter(None, [node.module])])
                    names = tuple(alias.name for alias in node.names)
                    if _is_ui(module):
                        found.append((node.lineno, module, names, holder))
                    else:  # from autoform_cli import ui
                        subs = (f"{module}.{name}" for name in names)
                        found += [(node.lineno, sub, (), holder) for sub in subs if _is_ui(sub)]
                elif (
                    isinstance(node, ast.Constant) and isinstance(node.value, str) and _UI_STRING.fullmatch(node.value)
                ):
                    found.append((node.lineno, node.value, None, holder))
    return found


def test_contract_modules_never_import_the_ui(repo_root: Path) -> None:
    entry = {holder for _, _, _, holder in _ui_imports(repo_root / _ENTRY, repo_root)}
    assert set(_HANDLERS) <= entry, (
        f"the scan sees no ui/ import in {sorted(set(_HANDLERS) - entry)} of {_ENTRY}; fix the scanner,"
        " or this test if a handler no longer imports ui/"
    )
    ui = repo_root / "autoform_cli" / "ui"
    found = []
    for path in sorted((repo_root / "autoform_cli").rglob("*.py")):
        if ui in path.parents:
            continue
        where = path.relative_to(repo_root).as_posix()
        for line, module, names, holder in _ui_imports(path, repo_root):
            if where == _ENTRY and holder in _HANDLERS and _HANDLERS[holder] in (None, (module, names)):
                continue
            taken = f" ({', '.join(names)})" if names else ""
            found.append(f"{where}:{line}: {module}{taken}" + (f" in {holder}" if holder else ""))
    assert not found, (
        f"autoform_cli/ must not import {_UI}, so that commands other than render, site check, dashboard and check"
        f" load no display code; only {_ENTRY} may, inside {', '.join(_HANDLERS)} (_check for publication_issues"
        " alone):\n" + "\n".join(found)
    )
