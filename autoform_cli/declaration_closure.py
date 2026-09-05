"""Deterministic Lean declaration-closure extraction for review tooling."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .lean import Declaration, build_linker, index_project

_NAME = re.compile(r"^[\w'.]+$", re.UNICODE)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DEFINITION_KEYWORDS = frozenset(
    {"abbrev", "class", "def", "inductive", "instance", "opaque", "structure"}
)
_NODE_MARKER = "AUTOFORM_DECLARATION_NODE\t"
_EDGE_MARKER = "AUTOFORM_DECLARATION_EDGE\t"
_SOURCE_MARKER = "AUTOFORM_DECLARATION_SOURCE\t"


class DeclarationClosureError(RuntimeError):
    """Raised when an exact closure cannot be computed."""


@dataclass(frozen=True, slots=True)
class ClosureReport:
    root: Path
    base: str
    head: str
    dirty: bool
    modules: tuple[str, ...]
    roots: tuple[Declaration, ...]
    reachable: tuple[Declaration, ...]
    dependency_edges: tuple[tuple[str, str], ...]

    @property
    def definitions(self) -> tuple[Declaration, ...]:
        return tuple(d for d in self.reachable if d.keyword in _DEFINITION_KEYWORDS)

    def as_dict(self) -> dict[str, object]:
        linker = build_linker(self.root, ref=self.head)

        def item(declaration: Declaration) -> dict[str, object]:
            return {
                "keyword": declaration.keyword,
                "line": declaration.line,
                "name": declaration.name,
                "path": declaration.path.as_posix(),
                "url": None if self.dirty else linker.url(declaration.name),
            }

        return {
            "base": self.base,
            "definitions": [item(d) for d in self.definitions],
            "dependency_edges": [
                {"declaration": declaration, "depends_on": dependency}
                for declaration, dependency in self.dependency_edges
            ],
            "dirty": self.dirty,
            "head": self.head,
            "modules": list(self.modules),
            "reachable": [item(d) for d in self.reachable],
            "root": str(self.root),
            "roots": [item(d) for d in self.roots],
            "schema": "autoform-declaration-closure/v1",
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def declaration_closure(
    lean_root: str | Path,
    *,
    base: str,
    modules: list[str] | tuple[str, ...],
    roots: list[str] | tuple[str, ...],
) -> ClosureReport:
    """Return the exact PR-changed declaration closure of *roots*."""
    root = Path(lean_root).expanduser().resolve()
    if not root.is_dir():
        raise DeclarationClosureError(f"Lean root does not exist: {root}")
    if not modules:
        raise DeclarationClosureError("at least one --module is required")
    if not roots:
        raise DeclarationClosureError("at least one --root is required")
    for label, values in (("module", modules), ("root", roots)):
        invalid = [value for value in values if not _NAME.fullmatch(value)]
        if invalid:
            raise DeclarationClosureError(f"invalid Lean {label} name: {invalid[0]!r}")

    index = index_project(root)
    missing = [name for name in roots if index.find(name) is None]
    if missing:
        raise DeclarationClosureError(f"root declaration not found in sources: {missing[0]}")

    changed = _changed_declarations(root, base, index.declarations.values())
    allowed = sorted(changed)
    _run(root, ["lake", "build", *modules], "lake build")
    source = _lean_driver(modules, roots, allowed)
    with tempfile.TemporaryDirectory(prefix="autoform-declaration-closure-") as directory:
        driver = Path(directory) / "Main.lean"
        driver.write_text(source, encoding="utf-8")
        output = _run(root, ["lake", "env", "lean", str(driver)], "Lean elaboration")

    nodes, edges, source_names = _parse_lean_output(output)
    ordered = _dependency_order(nodes, edges)
    names = [source_names[name] for name in ordered if name in source_names]
    reachable = tuple(index.declarations[name] for name in names if name in changed)
    dependency_edges = _source_dependency_edges(source_names, edges)
    root_declarations = tuple(index.declarations[name] for name in roots)
    return ClosureReport(
        root=root,
        base=_git(root, "rev-parse", base),
        head=_git(root, "rev-parse", "HEAD"),
        dirty=bool(_git(root, "status", "--porcelain", "--untracked-files=all")),
        modules=tuple(modules),
        roots=root_declarations,
        reachable=reachable,
        dependency_edges=dependency_edges,
    )


def _parse_lean_output(
    output: str,
) -> tuple[set[str], set[tuple[str, str]], dict[str, str]]:
    nodes: set[str] = set()
    edges: set[tuple[str, str]] = set()
    source_names: dict[str, str] = {}
    for line in output.splitlines():
        if line.startswith(_NODE_MARKER):
            nodes.add(line.removeprefix(_NODE_MARKER))
        elif line.startswith(_EDGE_MARKER):
            declaration, dependency = line.removeprefix(_EDGE_MARKER).split("\t", 1)
            edges.add((declaration, dependency))
        elif line.startswith(_SOURCE_MARKER):
            actual, display = line.removeprefix(_SOURCE_MARKER).split("\t", 1)
            source_names[actual] = display
    return nodes, edges, source_names


def _dependency_order(nodes: set[str], edges: set[tuple[str, str]]) -> list[str]:
    """Order dependencies before dependants, deterministically within cycles."""
    dependencies: dict[str, set[str]] = {name: set() for name in nodes}
    for declaration, dependency in edges:
        if declaration in nodes and dependency in nodes:
            dependencies[declaration].add(dependency)

    order: list[str] = []
    permanent: set[str] = set()
    temporary: set[str] = set()

    def visit(name: str) -> None:
        if name in permanent or name in temporary:
            return
        temporary.add(name)
        for dependency in sorted(dependencies[name]):
            visit(dependency)
        temporary.remove(name)
        permanent.add(name)
        order.append(name)

    for name in sorted(nodes):
        visit(name)
    return order


def _source_dependency_edges(
    source_names: dict[str, str], edges: set[tuple[str, str]]
) -> tuple[tuple[str, str], ...]:
    """Collapse generated Lean constants between source declarations."""
    dependencies: dict[str, set[str]] = {}
    for declaration, dependency in edges:
        dependencies.setdefault(declaration, set()).add(dependency)
    result: set[tuple[str, str]] = set()
    for source, display in source_names.items():
        pending = list(dependencies.get(source, ()))
        seen: set[str] = set()
        while pending:
            dependency = pending.pop()
            if dependency in seen:
                continue
            seen.add(dependency)
            if dependency in source_names:
                result.add((display, source_names[dependency]))
            else:
                pending.extend(dependencies.get(dependency, ()))
    return tuple(sorted(result))


def _changed_declarations(
    root: Path, base: str, declarations: Iterable[Declaration]
) -> dict[str, Declaration]:
    by_path: dict[Path, list[Declaration]] = {}
    for declaration in declarations:
        by_path.setdefault(declaration.path, []).append(declaration)

    prefix = Path(_git(root, "rev-parse", "--show-prefix"))

    def local_path(raw: str) -> Path:
        path = Path(raw)
        if prefix == Path("."):
            return path
        try:
            return path.relative_to(prefix)
        except ValueError:
            return path

    statuses: dict[Path, str] = {}
    output = _git(root, "diff", "--name-status", "--find-renames", base, "--", "*.lean")
    for line in output.splitlines():
        fields = line.split("\t")
        if not fields:
            continue
        status = fields[0][0]
        path = local_path(fields[-1])
        if status != "D":
            statuses[path] = status
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--", "*.lean")
    statuses.update({local_path(line): "A" for line in untracked.splitlines() if line})

    changed: dict[str, Declaration] = {}
    for path, status in statuses.items():
        declarations_in_file = sorted(by_path.get(path, []), key=lambda d: d.line)
        if status == "A":
            changed.update((d.name, d) for d in declarations_in_file)
            continue
        added_lines = _added_lines(root, base, path)
        for index, declaration in enumerate(declarations_in_file):
            next_line = (
                declarations_in_file[index + 1].line
                if index + 1 < len(declarations_in_file)
                else 1 << 60
            )
            if any(declaration.line <= line < next_line for line in added_lines):
                changed[declaration.name] = declaration
    return changed


def _added_lines(root: Path, base: str, path: Path) -> set[int]:
    output = _git(root, "diff", "--unified=0", "--no-color", base, "--", path.as_posix())
    lines: set[int] = set()
    for line in output.splitlines():
        match = _HUNK.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        lines.update(range(start, start + count))
    return lines


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise DeclarationClosureError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def _run(root: Path, command: list[str], stage: str) -> str:
    try:
        result = subprocess.run(
            command, cwd=root, capture_output=True, text=True, check=False
        )
    except OSError as error:
        raise DeclarationClosureError(f"{stage} could not start: {error}") from error
    if result.returncode:
        detail = "\n".join((result.stdout + "\n" + result.stderr).strip().splitlines()[-40:])
        raise DeclarationClosureError(f"{stage} failed; exact closure unavailable:\n{detail}")
    return result.stdout


def _lean_driver(
    modules: Sequence[str], roots: Sequence[str], allowed: Sequence[str]
) -> str:
    imports = "\n".join(f"import {name}" for name in modules)
    root_names = ", ".join(f"`{name}" for name in roots)
    allowed_names = ", ".join(f"`{name}" for name in allowed)
    return f"""{imports}
import Lean.Util.FoldConsts

open Lean Elab Command

private def directDependencies (info : ConstantInfo) : NameSet :=
  let fromType := info.type.getUsedConstantsAsSet
  match info with
  | .thmInfo _ => fromType
  | .defnInfo value => fromType ++ value.value.getUsedConstantsAsSet
  | .opaqueInfo value => fromType ++ value.value.getUsedConstantsAsSet
  | .inductInfo value => fromType ++ NameSet.ofList value.ctors
  | _ => fromType

private def displayName (name : Name) : Name :=
  privateToUserName? name |>.getD name

private def belongsTo (allowed : NameSet) (name : Name) : Bool := Id.run do
  let mut current := displayName name
  while !current.isAnonymous do
    if allowed.contains current then return true
    current := current.getPrefix
  return false

private partial def visit (env : Environment) (allowed : NameSet)
    (pending : List Name) (seen : NameSet := {{}}) : Except String NameSet := do
  match pending with
  | [] => pure seen
  | name :: rest =>
      if seen.contains name then
        visit env allowed rest seen
      else
        let some info := env.find? name
          | throw s!"unknown declaration: {{name}}"
        let next := (directDependencies info).toList.filter (belongsTo allowed)
        visit env allowed (next ++ rest) (seen.insert name)

elab "#autoform_declaration_closure" : command => do
  let roots : List Name := [{root_names}]
  let allowed : NameSet := NameSet.ofList [{allowed_names}]
  let sourceNames : NameSet := allowed ++ NameSet.ofList roots
  let env ← getEnv
  match visit env sourceNames roots with
  | .error message => throwError message
  | .ok names =>
      for name in names.toList do
        liftIO <| IO.println ("{_NODE_MARKER}" ++ name.toString)
        let some info := env.find? name | continue
        for dependency in (directDependencies info).toList do
          if belongsTo sourceNames dependency then
            liftIO <| IO.println ("{_EDGE_MARKER}" ++ name.toString ++ "\t" ++ dependency.toString)
        let shown := displayName name
        if sourceNames.contains shown then
          liftIO <| IO.println ("{_SOURCE_MARKER}" ++ name.toString ++ "\t" ++ shown.toString)

#autoform_declaration_closure
"""


__all__ = ["ClosureReport", "DeclarationClosureError", "declaration_closure"]
