#!/usr/bin/env python3
"""Run each mutant alone on CI and require its tests to fail.

Usage: teeth.py MUTANTS_JSON
Each mutant is {"id", "file", "find", "replace", "tests"}. The union of all
tests runs unmutated first and must pass. A mutant counts as killed only when
pytest exits 1 (tests failed); a collection or usage error is reported apart.
Every run starts with no bytecode caches and writes none: Python trusts a
cached .pyc whose source has the same size and mtime second, and two
mutants of one file often match on both.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys


def clear_bytecode():
    for root, dirs, _ in os.walk("."):
        dirs[:] = [name for name in dirs if name not in (".git", ".venv")]
        if "__pycache__" in dirs:
            shutil.rmtree(os.path.join(root, "__pycache__"))
            dirs.remove("__pycache__")


def run(tests):
    clear_bytecode()
    command = ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider", *tests]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(command, check=False, env=env).returncode


mutants = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
union = sorted({test for mutant in mutants for test in mutant["tests"]})
baseline = run(union)
rows = []
bad = baseline != 0
for mutant in mutants:
    path = pathlib.Path(mutant["file"])
    source = path.read_text(encoding="utf-8")
    count = source.count(mutant["find"])
    if count != 1:
        rows.append((mutant["id"], f"BAD find occurs {count} times"))
        bad = True
        continue
    path.write_text(source.replace(mutant["find"], mutant["replace"]), encoding="utf-8")
    try:
        code = run(mutant["tests"])
    finally:
        path.write_text(source, encoding="utf-8")
    if code == 1:
        rows.append((mutant["id"], "killed"))
    elif code == 0:
        rows.append((mutant["id"], "SURVIVED"))
        bad = True
    else:
        rows.append((mutant["id"], f"BAD pytest exit {code}"))
        bad = True
print(f"TEETH BASELINE exit={baseline} over {len(union)} tests")
for mutant_id, outcome in rows:
    print(f"TEETH MUTANT {mutant_id}: {outcome}")
sys.exit(1 if bad else 0)
