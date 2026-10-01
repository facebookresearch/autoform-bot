"""Drop imports that another import in the same file already brings in.

Removes exactly the imports that are transitively implied by another import of
the same file, so the set of modules loaded -- the import closure -- is
unchanged. Unlike `#min_imports` or `lake exe shake`, it never asks whether an
import is *used*, so it is safe on unfinished files whose remaining proofs may
still need something nothing currently references.

The redundancy test is ImportGraph's `Environment.findRedundantImports` (the
engine behind `#redundant_imports`), evaluated in a throwaway probe file with
the target's imports, so the answer comes from Lean's own import graph rather
than from parsing source. The probe also recomputes both closures and refuses
to write unless they are identical.

    python -m servers.prover.debloat_imports path/to/File.lean [more.lean ...]
    python -m servers.prover.debloat_imports --dry-run path/to/*.lean

:mod:`servers.prover.grant_imports` runs this on every file it adds imports to.

Before probing, `lake build --no-build` confirms every import's .olean is up to
date: the graph is read from build outputs, and a stale one could misreport
what a module imports. After writing, the file is recompiled with `lake env
lean` and reverted if any error appears that was not there before (unless
--no-verify). Only plain `import X` headers are handled; files using the module
system (`module`, `public import`, ...) are skipped.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path

IMPORT_LINE = re.compile(r"^import\s+([A-Za-z_][A-Za-z0-9_'.]*)\s*(?:--.*)?$")
MODULE_SYSTEM = re.compile(r"^(?:module\b|prelude\b|(?:public|meta|private)\s+(?:meta\s+)?import\b)")
POSITION = re.compile(r"^[^\n]*?:\d+:\d+:\s*")
MARK = "DEBLOAT"

PROBE = """{imports}
import ImportGraph.Imports.Redundant
import ImportGraph.Imports.ImportGraph

open Lean

partial def debloatClosure (g : NameMap (Array Name)) : List Name → NameSet → NameSet
  | [], acc => acc
  | n :: rest, acc =>
    if acc.contains n then debloatClosure g rest acc
    else debloatClosure g (((g.find? n).getD #[]).toList ++ rest) (acc.insert n)

#eval show CoreM Unit from do
  let env ← getEnv
  let original : Array Name := #[{names}]
  let redundant := env.findRedundantImports original
  let kept := original.filter (fun n => !redundant.contains n)
  let g := env.importGraph
  let before := debloatClosure g original.toList {{}}
  let after := debloatClosure g kept.toList {{}}
  for n in original do
    if redundant.contains n then IO.println s!"{mark}-REDUNDANT {{n}}"
  IO.println s!"{mark}-CLOSURE {{before.size}} {{after.size}} {{after.toList.all before.contains}}"
"""


class DebloatSkipped(Exception):
    pass


def find_project(start: Path) -> Path:
    for parent in [start, *start.parents]:
        if (parent / "lakefile.toml").is_file() or (parent / "lakefile.lean").is_file():
            return parent
    raise SystemExit(f"no lakefile found above {start}")


def header_imports(text: str) -> list[tuple[int, str]]:
    """(line index, module) for each header import; stops at the first other code."""
    imports: list[tuple[int, str]] = []
    in_block = 0
    for index, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if in_block:
            in_block += line.count("/-") - line.count("-/")
            continue
        if not line or line.startswith("--"):
            continue
        if line.startswith("/-"):
            in_block = line.count("/-") - line.count("-/")
            continue
        if MODULE_SYSTEM.match(line):
            raise DebloatSkipped("uses the module system; not supported")
        match = IMPORT_LINE.match(line)
        if not match:
            break
        imports.append((index, match.group(1)))
    return imports


def run(args: list[str], project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=project, capture_output=True, text=True, check=False)


def ensure_fresh(project: Path, modules: list[str]) -> None:
    checked = run(["lake", "build", "--no-build", *modules], project)
    if checked.returncode != 0:
        raise DebloatSkipped(
            "some imports are not built or are out of date; run\n"
            f"        lake build {' '.join(modules)}\n      and retry"
        )


def find_redundant(project: Path, modules: list[str]) -> list[str]:
    probe = PROBE.format(
        imports="\n".join(f"import {m}" for m in modules),
        names=", ".join(f'"{m}".toName' for m in modules),
        mark=MARK,
    )
    with tempfile.NamedTemporaryFile("w", suffix=".lean", delete=False) as handle:
        handle.write(probe)
        probe_path = Path(handle.name)
    try:
        result = run(["lake", "env", "lean", str(probe_path)], project)
    finally:
        probe_path.unlink(missing_ok=True)
    output = result.stdout + result.stderr
    closure = [l.split()[1:] for l in output.splitlines() if l.startswith(f"{MARK}-CLOSURE")]
    if result.returncode != 0 or not closure:
        raise DebloatSkipped("import probe failed:\n" + "\n".join(f"        {l}" for l in output.splitlines()[-10:]))
    before, after, subset = closure[0]
    if before != after or subset != "true":
        raise DebloatSkipped(f"closure check failed ({before} vs {after} modules); not writing")
    return [l.split()[1] for l in output.splitlines() if l.startswith(f"{MARK}-REDUNDANT")]


def errors(project: Path, rel: str) -> list[str] | None:
    """Position-free error messages from compiling ``rel``; ``None`` if it compiled cleanly."""
    result = run(["lake", "env", "lean", rel], project)
    found = sorted(
        POSITION.sub("", line)
        for line in (result.stdout + result.stderr).splitlines()
        if re.match(r"^[^\n]*?:\d+:\d+:\s*error", line)
    )
    return found if result.returncode != 0 else None


def debloat(
    path: Path,
    project: Path,
    *,
    dry_run: bool = False,
    verify: bool = True,
    assume_clean: bool = False,
) -> bool:
    """Remove redundant imports from ``path``; ``True`` if the file changed (or would).

    ``assume_clean`` skips the pre-removal compile when the caller has just seen
    the file compile without errors.
    """
    rel = str(path.resolve().relative_to(project))
    text = path.read_text(encoding="utf-8")
    imports = header_imports(text)
    if len(imports) < 2:
        print(f"{rel}: {len(imports)} import(s); nothing to do")
        return False
    modules = [module for _, module in imports]
    ensure_fresh(project, modules)
    redundant = set(find_redundant(project, modules))
    if not redundant:
        print(f"{rel}: {len(modules)} imports, none redundant")
        return False

    print(f"{rel}: {len(modules)} -> {len(modules) - len(redundant)} imports")
    for module in modules:
        if module in redundant:
            print(f"    - {module}")
    if dry_run:
        return True

    baseline = errors(project, rel) if verify and not assume_clean else None
    drop = {index for index, module in imports if module in redundant}
    lines = text.splitlines(keepends=True)
    path.write_text("".join(l for i, l in enumerate(lines) if i not in drop), encoding="utf-8")
    if verify:
        after = errors(project, rel)
        new = sorted(set(after or []) - set(baseline or []))
        if new:
            path.write_text(text, encoding="utf-8")
            print("    REVERTED: new errors after removal:")
            for line in new[:5]:
                print(f"      {line[:160]}")
            return False
        print("    recompiled: no new errors")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--no-verify", action="store_true", help="skip the recompile check")
    parser.add_argument("--project", type=Path, help="Lean project root (default: nearest lakefile)")
    args = parser.parse_args(argv)

    project = (args.project or find_project(args.files[0].resolve().parent)).resolve()
    failed = False
    for path in args.files:
        try:
            debloat(path, project, dry_run=args.dry_run, verify=not args.no_verify)
        except DebloatSkipped as reason:
            print(f"{path}: skipped: {reason}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
