"""Deterministic Lean declaration-closure extraction for review tooling."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .lean import Declaration, build_linker, index_project

_NAME = re.compile(r"^[\w'.]+$", re.UNICODE)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DEFINITION_KEYWORDS = frozenset(
    {"abbrev", "class", "def", "inductive", "instance", "opaque", "structure"}
)
_MARKER = "AUTOFORM_DECLARATION_CLOSURE\t"


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

    names = sorted(
        {
            line.removeprefix(_MARKER)
            for line in output.splitlines()
            if line.startswith(_MARKER)
        }
    )
    reachable = tuple(index.declarations[name] for name in names if name in changed)
    root_declarations = tuple(index.declarations[name] for name in roots)
    return ClosureReport(
        root=root,
        base=_git(root, "rev-parse", base),
        head=_git(root, "rev-parse", "HEAD"),
        dirty=bool(_git(root, "status", "--porcelain", "--untracked-files=all")),
        modules=tuple(modules),
        roots=root_declarations,
        reachable=reachable,
    )


def _changed_declarations(
    root: Path, base: str, declarations: object
) -> dict[str, Declaration]:
    by_path: dict[Path, list[Declaration]] = {}
    for declaration in declarations:
        by_path.setdefault(declaration.path, []).append(declaration)

    statuses: dict[Path, str] = {}
    output = _git(root, "diff", "--name-status", "--find-renames", base, "--", "*.lean")
    for line in output.splitlines():
        fields = line.split("\t")
        if not fields:
            continue
        status = fields[0][0]
        path = Path(fields[-1])
        if status != "D":
            statuses[path] = status
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--", "*.lean")
    statuses.update({Path(line): "A" for line in untracked.splitlines() if line})

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


def _lean_driver(modules: object, roots: object, allowed: object) -> str:
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
  let env ← getEnv
  match visit env allowed roots with
  | .error message => throwError message
  | .ok names =>
      for name in names.toList do
        let shown := displayName name
        if allowed.contains shown then
          liftIO <| IO.println ("{_MARKER}" ++ shown.toString)

#autoform_declaration_closure
"""


__all__ = ["ClosureReport", "DeclarationClosureError", "declaration_closure"]
