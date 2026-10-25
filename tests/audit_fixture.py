"""Load the generated audit helper and run a Lake project the way the audit step does."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType


_TEMPLATE = Path("autoform_cli/templates/github/autoform_audit.py")


def _load_helper(repo_root: Path) -> ModuleType:
    path = repo_root / _TEMPLATE
    spec = importlib.util.spec_from_file_location("autoform_audit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(project: Path, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=180)


def _run_probe(project: Path, probe: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run *probe* as the audit step does: plain lean, given Lake's search path."""

    clean = {key: value for key, value in os.environ.items() if key != "LEAN_PATH"}
    lean_path = subprocess.run(
        ["lake", "env", "printenv", "LEAN_PATH"], cwd=project, env=clean, capture_output=True, text=True, timeout=180
    )
    assert lean_path.returncode == 0, lean_path.stdout + lean_path.stderr
    clean["AUTOFORM_AUDIT_LEAN_PATH"] = lean_path.stdout.rstrip("\n")
    clean["LEAN_ABORT_ON_PANIC"] = "1"
    return subprocess.run(
        ["lean", str(probe)], cwd=project, env={**clean, **env}, capture_output=True, text=True, timeout=180
    )


def _audit_step(workflow: Path) -> str:
    text = workflow.read_text(encoding="utf-8")
    _, found, step = text.partition("      - name: Audit every root-package declaration\n        run: |\n")
    assert found
    lines = []
    for line in step.splitlines():
        if line and not line.startswith(" " * 10):
            break
        lines.append(line[10:])
    return "\n".join(lines) + "\n"


_WORKFLOW = Path(__file__).parents[1] / "autoform_cli/templates/github/workflows/autoform-verify.yml"


def _ends_with_the_success_line(stdout: str) -> bool:
    """Whether *stdout* ends as the audit step requires, by the step's own pattern."""

    found = re.search(r"grep -Eq '([^']+)'", _audit_step(_WORKFLOW))
    assert found
    lines = stdout.splitlines()
    return bool(lines) and re.search(found.group(1), lines[-1]) is not None
