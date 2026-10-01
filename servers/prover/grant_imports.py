"""Turn the provers' import requests from debriefs into Mathlib imports, in two steps.

The proof gate rejects any change outside a node's target declarations, imports
included, so the prover that notices a missing import cannot add it. With
``AUTOFORM_DEBRIEF`` on (:mod:`servers.prover.debrief`) it can ask instead: an
``infrastructure_proposals`` entry of ``kind: "import-export"``, or a
``searched_for`` entry of ``why_hard: "inaccessible-import"``. This reads those
requests back, with a human review step in the middle::

    python -m servers.prover.grant_imports propose --project <lean project>
    # review and edit the plan, then
    python -m servers.prover.grant_imports apply --project <lean project>

``propose`` writes no Lean. It collects every module the provers named, routes
each to the node's Lean file through the runtime graph, checks it exists under
``.lake/packages/mathlib``, and drops the ones already imported. For a module
that does not exist (provers guess paths), it looks for the lemmas the prover
said it wanted and suggests the modules that really declare them. The plan is
written next to the debriefs as ``import-plan.json``.

``apply`` re-checks every approved entry from scratch, inserts the imports,
compiles each touched file with ``lake env lean``, and reverts a file whose
compile fails. Editing the plan can only choose among and correct candidates;
it cannot skip those checks.

Only ``Mathlib.*`` modules are granted. Project-internal imports follow the
roadmap's dependency edges and are out of scope here.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autoform_cli.runtime import load_runtime_graph

from . import debrief

PLAN_FILENAME = "import-plan.json"

# A module path as a prover writes it, in prose or backticks. The trailing
# `(?<!\.)` stops a sentence-final full stop being read as a path component.
MODULE = re.compile(r"\bMathlib(?:\.[A-Za-z0-9_']+)+(?<!\.)")
# Backticked identifiers: the lemma names a prover says it wanted.
IDENT = re.compile(r"`([A-Za-z_][A-Za-z0-9_'.]*)`")
IMPORT = re.compile(r"^import (\S+)", re.MULTILINE)

Compiler = Callable[[Path, str], tuple[bool, str]]


def mathlib_root(project: Path) -> Path:
    return project / ".lake" / "packages" / "mathlib"


def mathlib_path(project: Path, module: str) -> Path:
    return mathlib_root(project) / Path(*module.split(".")).with_suffix(".lean")


def current_imports(project: Path, rel: str) -> set[str]:
    path = project / rel
    if not path.is_file():
        return set()
    return set(IMPORT.findall(path.read_text(encoding="utf-8")))


def harvest(records: Path) -> list[dict[str, Any]]:
    """Every (node, module, wanted lemmas, evidence) a debrief asked for."""
    asks: list[dict[str, Any]] = []
    for line in records.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        node = record.get("node_id") if isinstance(record, dict) else None
        answer = record.get("debrief") if node else None
        if not isinstance(answer, dict):
            continue
        blobs: list[str] = []
        for item in answer.get("infrastructure_proposals") or []:
            if isinstance(item, dict) and item.get("kind") == "import-export":
                blobs.append(" ".join(str(item.get(k, "")) for k in ("name", "what", "evidence")))
        for item in answer.get("searched_for") or []:
            if isinstance(item, dict) and item.get("why_hard") == "inaccessible-import":
                blobs.append(" ".join(str(item.get(k, "")) for k in ("wanted", "searched", "resolution")))
        for blob in blobs:
            idents = sorted({name for name in IDENT.findall(blob) if not name.startswith("Mathlib")})
            for module in sorted(set(MODULE.findall(blob))):
                asks.append({
                    "node": node,
                    "module": module,
                    "wanted": idents,
                    "evidence": " ".join(blob.split())[:400],
                })
    return asks


def node_targets(project: Path) -> dict[str, list[str]]:
    """Roadmap node id -> project-relative Lean files holding its declarations."""
    graph = load_runtime_graph(project, lean_root=project)
    targets: dict[str, list[str]] = {}
    for node in graph.nodes:
        files = sorted({t.source_file for t in node.lean_targets if t.source_file})
        if files:
            targets[node.id] = files
    return targets


def locate_declarations(project: Path, names: set[str]) -> dict[str, list[str]]:
    """Mathlib modules declaring each bare name, for repairing an invented path."""
    root = mathlib_root(project)
    bare = {name.rsplit(".", 1)[-1] for name in names} - {""}
    if not bare or not (root / "Mathlib").is_dir():
        return {}
    patterns = {
        name: re.compile(
            r"^(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+)*"
            rf"(?:theorem|lemma|def|abbrev|instance)\s+{re.escape(name)}\b",
            re.MULTILINE,
        )
        for name in bare
    }
    found: dict[str, list[str]] = defaultdict(list)
    for path in sorted((root / "Mathlib").rglob("*.lean")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for name, pattern in patterns.items():
            # The substring test keeps a whole-Mathlib scan well under a second.
            if name in text and pattern.search(text):
                found[name].append(".".join(path.relative_to(root).with_suffix("").parts))
    return {name: sorted(modules)[:4] for name, modules in found.items()}


def build_plan(
    project: Path,
    asks: list[dict[str, Any]],
    targets: dict[str, list[str]],
) -> dict[str, Any]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    unrouted: list[dict[str, Any]] = []
    for ask in asks:
        files = targets.get(ask["node"])
        if not files:
            unrouted.append(ask)
            continue
        for rel in files:
            entry = merged.setdefault((rel, ask["module"]), {
                "approved": False, "module": ask["module"], "file": rel,
                "status": "", "times_asked": 0, "asked_by": [],
                "wanted": [], "evidence": ask["evidence"],
            })
            entry["times_asked"] += 1
            if ask["node"] not in entry["asked_by"]:
                entry["asked_by"].append(ask["node"])
            entry["wanted"].extend(n for n in ask["wanted"] if n not in entry["wanted"])

    grants: list[dict[str, Any]] = []
    for (rel, module), entry in sorted(merged.items()):
        if module in current_imports(project, rel):
            entry["status"] = "already-imported"
        elif mathlib_path(project, module).is_file():
            entry["status"] = "resolved"
            entry["approved"] = True
        else:
            entry["status"] = "module-not-found"
        grants.append(entry)

    missing = [entry for entry in grants if entry["status"] == "module-not-found"]
    homes = locate_declarations(project, {n for e in missing for n in e["wanted"]})
    for entry in missing:
        already = current_imports(project, entry["file"])
        suggestions: list[str] = []
        for name in entry["wanted"]:
            for module in homes.get(name.rsplit(".", 1)[-1], []):
                if module not in suggestions and module not in already:
                    suggestions.append(module)
        entry["suggested_modules"] = suggestions[:4]

    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "how_to_use": [
            'Set "approved": true on each import you want granted.',
            (
                'A module-not-found entry is a prover\'s guess: correct "module" (see '
                '"suggested_modules") and approve it, or leave it alone.'
            ),
            "You may add whole new entries; apply re-validates everything.",
        ],
        "grants": grants,
        "unrouted": unrouted,
    }


def insert_import(path: Path, module: str) -> None:
    """Insert in sorted position among the Mathlib block, else after the last import."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    new = f"import {module}\n"
    mathlib_at = [i for i, line in enumerate(lines) if line.startswith("import Mathlib.")]
    if mathlib_at:
        block = [lines[i] for i in mathlib_at]
        at = mathlib_at[-1] + 1
        if block == sorted(block):
            at = next((i for i in mathlib_at if lines[i] > new), at)
    else:
        at = max((i for i, line in enumerate(lines) if line.startswith("import ")), default=-1) + 1
    lines.insert(at, new)
    path.write_text("".join(lines), encoding="utf-8")


def lake_compile(project: Path, rel: str) -> tuple[bool, str]:
    built = subprocess.run(
        ["lake", "env", "lean", rel], cwd=project, capture_output=True, text=True, check=False
    )
    return built.returncode == 0, built.stdout + built.stderr


def validate(project: Path, entry: dict[str, Any]) -> str | None:
    """Why an approved entry must be rejected, or ``None`` if it may be applied."""
    module, rel = str(entry.get("module", "")), str(entry.get("file", ""))
    if not module.startswith("Mathlib."):
        return "not a Mathlib module"
    if not mathlib_path(project, module).is_file():
        return "module does not exist on disk"
    path = (project / rel).resolve()
    if not path.is_relative_to(project.resolve()) or path.suffix != ".lean":
        return f"target is not a Lean file in the project: {rel}"
    if not path.is_file():
        return f"target file missing: {rel}"
    if module in current_imports(project, rel):
        return "already imported"
    return None


def apply_plan(
    project: Path,
    plan: dict[str, Any],
    *,
    dry_run: bool = False,
    compile: Compiler = lake_compile,
) -> int:
    approved = [g for g in plan.get("grants", []) if isinstance(g, dict) and g.get("approved")]
    if not approved:
        print("nothing approved in the plan; nothing to do")
        return 0

    per_file: dict[str, list[str]] = defaultdict(list)
    for entry in approved:
        why = validate(project, entry)
        if why:
            print(f"  reject  {entry.get('module')} -> {entry.get('file')}\n            {why}")
        elif entry["module"] not in per_file[entry["file"]]:
            per_file[entry["file"]].append(entry["module"])
    if not per_file:
        print("\nno approved entry survived validation")
        return 1

    if dry_run:
        print("\ndry run; would add:")
        for rel, modules in sorted(per_file.items()):
            for module in sorted(modules):
                print(f"  + {module} -> {rel}")
        return 0

    failures = 0
    for rel, modules in sorted(per_file.items()):
        path = project / rel
        before = path.read_text(encoding="utf-8")
        for module in sorted(modules):
            insert_import(path, module)
        print(f"\n  {rel}: +{len(modules)} import(s); compiling ...")
        ok, output = compile(project, rel)
        if ok:
            for module in sorted(modules):
                print(f"    kept    {module}")
            continue
        path.write_text(before, encoding="utf-8")
        failures += 1
        print(f"    REVERTED all {len(modules)} (compile failed)")
        for line in [l for l in output.splitlines() if "error" in l.lower()][-3:]:
            print(f"      {line[:150]}")
    return 1 if failures else 0


def _print_summary(plan: dict[str, Any], asks: int, out: Path) -> None:
    by_status: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in plan["grants"]:
        by_status[entry["status"]].append(entry)
    print(f"harvested {asks} import request(s)\n")
    sections = (
        ("resolved", "READY TO GRANT      (pre-approved in the plan)"),
        ("module-not-found", "NEEDS A HUMAN       (module does not exist; prover guessed)"),
        ("already-imported", "SKIPPED             (already in scope)"),
    )
    for status, title in sections:
        rows = by_status.get(status) or []
        if not rows:
            continue
        print(f"{title} ({len(rows)})")
        for rel in sorted({r["file"] for r in rows}):
            print(f"  {rel}")
            for entry in sorted((r for r in rows if r["file"] == rel), key=lambda r: r["module"]):
                who = ", ".join(n.rsplit("/", 1)[-1] for n in entry["asked_by"])
                print(f"    {entry['module']}\n        asked {entry['times_asked']}x by {who}")
                if status != "module-not-found":
                    continue
                if entry["wanted"]:
                    print(f"        wanted: {', '.join(entry['wanted'][:4])}")
                for module in entry.get("suggested_modules") or []:
                    print(f"        try instead: {module}")
                if not entry.get("suggested_modules"):
                    print("        no module found declaring those names")
        print()
    if plan["unrouted"]:
        print(f"UNROUTED ({len(plan['unrouted'])})  node has no resolvable Lean target")
        for ask in plan["unrouted"][:10]:
            print(f"  {ask['node']}  {ask['module']}")
        print()
    print(f"plan written to {out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", type=Path, default=Path("."), help="Lean project directory")
    parser.add_argument(
        "--debriefs", type=Path,
        help="debrief directory (default: AUTOFORM_DEBRIEF_DIR or <project>/.autoform/debriefs)",
    )
    parser.add_argument("--plan", type=Path, help=f"plan path (default: <debriefs>/{PLAN_FILENAME})")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("propose", help="harvest debriefs into a reviewable plan")
    apply_cmd = sub.add_parser("apply", help="apply the approved entries of a plan")
    apply_cmd.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    project = args.project.resolve()
    debriefs = args.debriefs or debrief.debrief_dir(project)
    plan_path = args.plan or debriefs / PLAN_FILENAME

    if args.command == "propose":
        records = debriefs / debrief.RECORDS_FILENAME
        if not records.is_file():
            sys.exit(f"no {debrief.RECORDS_FILENAME} under {debriefs}")
        asks = harvest(records)
        plan = build_plan(project, asks, node_targets(project))
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        _print_summary(plan, len(asks), plan_path)
        return 0

    if not plan_path.is_file():
        sys.exit(f"no plan at {plan_path}")
    return apply_plan(
        project,
        json.loads(plan_path.read_text(encoding="utf-8")),
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
