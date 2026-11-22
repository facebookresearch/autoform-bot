"""The harness's own verification of an agent's change, before anything is pushed.

Each step records a name, whether it passed, a detail tail and its time. The
agent's word is never enough: the harness diffs against the merge base, scans
the added Lean lines (comments and literals removed) for proof holes and
escapes, builds every changed module, compares the target declarations'
elaborated statements with the base's, and runs the profile's commands.
"""

from __future__ import annotations

import json
import posixpath
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..lean import _without_lean_comments, index_project_names
from ..lean_probe import lean_libraries, module_of
from .gitops import GitError, added_lines, changed_files, git, read_blob, run_command, tree_of_worktree
from .profile import Profile

PROBE = Path(__file__).resolve().parent / "statement_facts.lean"
STANDARD_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})
# Searched in added lines once comments and literals are blanked. `axiom` is matched as a keyword wherever it
# stands, so no modifier (`public`, `private`, `meta`, an attribute) can hide one.
FORBIDDEN = (
    r"\bsorry\b", r"\bsorryAx\b", r"\badmit\b", r"\bnative_decide\b", r"\bimplemented_by\b", r"\bunsafe\b",
    r"\bdebug\.skipKernelTC\b", r"(?<![\w.'])axiom(?![\w'!?])",
)
_PRIVATE = re.compile(r"_private\.[\w.'«»]+?\.0\.")


@dataclass(slots=True)
class Verification:
    ok: bool = True
    steps: list[dict[str, object]] = field(default_factory=list)
    changes: list[tuple[str, str]] = field(default_factory=list)
    #: The tree the steps verified: what a commit of the change must hold.
    tree: str = ""
    _clock: float = field(default_factory=time.monotonic)

    def step(self, name: str, ok: bool, detail: str = "") -> bool:
        now = time.monotonic()
        self.steps.append({"step": name, "ok": ok, "detail": detail[-6000:], "seconds": round(now - self._clock, 1)})
        self._clock = now
        self.ok = self.ok and ok
        return ok

    def failures(self) -> list[dict[str, object]]:
        return [step for step in self.steps if not step["ok"]]

    def brief(self) -> list[dict[str, object]]:
        return [{"step": step["step"], "ok": step["ok"], "seconds": step["seconds"]} for step in self.steps]


def forbidden_hits(text: str, lines: Iterable[int], patterns: Sequence[str]) -> list[tuple[int, str]]:
    """``(line, text)`` of each listed line that holds a forbidden token outside comments and literals."""

    compiled = [re.compile(pattern) for pattern in patterns]
    masked = _without_lean_comments(text, mask_literals=True).split("\n")
    original = text.split("\n")
    hits: list[tuple[int, str]] = []
    for number in sorted(set(lines)):
        if 1 <= number <= len(masked) and any(pattern.search(masked[number - 1]) for pattern in compiled):
            hits.append((number, original[number - 1].strip() if number <= len(original) else ""))
    return hits


def lean_modules(worktree: Path, paths: Iterable[str]) -> list[str]:
    """The Lean modules of ``paths`` that the project's libraries build."""

    try:
        libraries = lean_libraries(worktree)
    except Exception:  # noqa: BLE001 - a project without a readable lakefile builds no module
        return []
    modules = []
    for path in paths:
        module = module_of(worktree / path, libraries)
        if module is not None:
            modules.append(module)
    return sorted(set(modules))


def project_roots(worktree: Path) -> list[str]:
    try:
        libraries = lean_libraries(worktree)
    except Exception:  # noqa: BLE001
        return []
    return sorted({root for library in libraries for root in library.roots})


def locate_modules(worktree: Path, names: Sequence[str]) -> dict[str, str | None]:
    """The module declaring each name in the worktree's sources, as Autoform's lexical index finds it."""

    found: dict[str, str | None] = {name: None for name in names}
    if not names:
        return found
    index = index_project_names(worktree, names)
    libraries = lean_libraries(worktree)
    for name in names:
        location = index.find(name)
        if location is not None:
            found[name] = module_of(worktree / location.path, libraries)
    return found


def statement_text(worktree: Path, names: Sequence[str], *, limit: int = 6000) -> str:
    """The source of each declaration, docstring included, as the lexical index finds it; else its name."""

    if not names:
        return ""
    index = index_project_names(worktree, names)
    parts: list[str] = []
    for name in names:
        location = index.find(name)
        if location is None:
            parts.append(name)
            continue
        try:
            lines = (worktree / location.path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            parts.append(name)
            continue
        start = max(0, location.line - 1)
        while start > 0 and lines[start - 1].strip() and start > location.line - 40:
            start -= 1
        end = location.line
        while end < len(lines) and lines[end].strip() and end < location.line + 80:
            end += 1
        parts.append("\n".join(lines[start:end]))
    return "\n\n".join(parts)[:limit]


def statement_facts(worktree: Path, modules: Sequence[str], declarations: Sequence[str], *,
                    env: Mapping[str, str], timeout: float = 3600) -> dict[str, dict[str, object]]:
    roots = project_roots(worktree)
    if not roots:
        raise GitError("the project's lakefile names no library root")
    result = run_command(
        ["lake", "env", "lean", "--run", str(PROBE), ",".join(roots), *modules, "--", *declarations],
        cwd=worktree, env=env, timeout=timeout,
    )
    if result.returncode != 0:
        raise GitError(f"the statement probe failed: {(result.stderr or result.stdout).strip()[-3000:]}")
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if not lines:
        raise GitError("the statement probe printed no facts")
    facts = json.loads(lines[-1])
    if not isinstance(facts, dict):
        raise GitError("the statement probe printed no facts")
    return facts


def _plain(value: object) -> object:
    return _PRIVATE.sub("_private.0.", value) if isinstance(value, str) else value


def fidelity_problems(base: Mapping[str, Mapping[str, object]], head: Mapping[str, Mapping[str, object]], *,
                      phase: str) -> list[str]:
    """What makes the head's target declarations differ from the base's statements.

    For a proof task, each declaration must now be a theorem (no ``theorem_wanted``
    placeholder) whose statement and universe parameters equal the base's, whose
    statement still uses every project constant the base's did with the same
    type and value, and whose axioms are the standard three.
    """

    problems: list[str] = []
    for name, before in base.items():
        after = head.get(name) or {}
        if not after.get("found"):
            problems.append(f"{name}: not found in the changed modules")
            continue
        if phase == "proof" and (after.get("kind") != "theorem" or after.get("wanted")):
            what = "theorem_wanted placeholder" if after.get("wanted") else str(after.get("kind"))
            problems.append(f"{name}: is a {what}, not a proved theorem")
        if _plain(after.get("statement")) != _plain(before.get("statement")):
            problems.append(
                f"{name}: the statement differs from the base's\n  base: {str(before.get('statement'))[:1500]}\n"
                f"  head: {str(after.get('statement'))[:1500]}"
            )
        if after.get("levelParams") != before.get("levelParams"):
            problems.append(f"{name}: universe parameters {after.get('levelParams')} != {before.get('levelParams')}")
        extra = set(after.get("axioms") or []) - STANDARD_AXIOMS  # type: ignore[arg-type]
        if extra:
            problems.append(f"{name}: depends on axioms {sorted(extra)}")
        now = {_plain(item.get("name")): item for item in after.get("closure") or []}  # type: ignore[union-attr]
        for item in before.get("closure") or []:  # type: ignore[union-attr]
            other = now.get(_plain(item.get("name")))
            if other is None:
                problems.append(f"{name}: the statement no longer uses {item.get('name')}")
                continue
            for key in ("kind", "type", "valueHash", "ctorTypes", "levelParams"):
                if json.dumps(_plain(other.get(key)), sort_keys=True) != json.dumps(_plain(item.get(key)), sort_keys=True):
                    problems.append(f"{name}: {item.get('name')} changed ({key})")
                    break
    return problems


def _linked_sources_changed(worktree: Path, tree: str, changed: set[str]) -> list[str]:
    """Lean files of ``tree`` that are symbolic links to a changed file: Lean reads the change through them, the
    scan would not, so they are refused rather than followed."""

    found: list[str] = []
    for entry in git(worktree, "ls-tree", "-r", "-z", "--full-tree", tree).stdout.split("\0"):
        meta, _, name = entry.partition("\t")
        if not name.endswith(".lean") or not meta.startswith("120000 "):
            continue
        target = git(worktree, "cat-file", "blob", meta.split()[2]).stdout
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
        if resolved in changed and name not in changed:
            found.append(f"{name}: a symbolic link to the changed {resolved}; the loop scans Lean files only as "
                         "regular files")
    return found


def run_steps(worktree: Path, *, base: str, profile: Profile, env: Mapping[str, str],
              targets: Mapping[str, Mapping[str, object]] | None, phase: str | None,
              build_timeout: float = 7200, command_timeout: float = 10800) -> Verification:
    """Verify the worktree's change from ``base``: what a pull request may contain before it is pushed.

    ``targets`` holds the base's statement facts of a proof task's declarations,
    or ``None`` when the profile turns the fidelity check off or the task is a
    statement task.
    """

    result = Verification()
    try:
        # One snapshot: the file list, the scanned text and the added lines all come from this tree, which is
        # what a commit of the change holds.
        verified_tree = result.tree = tree_of_worktree(worktree)
        result.changes = changed_files(worktree, base, verified_tree)
    except GitError as error:
        result.step("read the change", False, str(error))
        return result
    if not result.step("the change is not empty", bool(result.changes), json.dumps(result.changes)[:3000]):
        return result
    lean_files = [path for status, path in result.changes if path.endswith(".lean") and not status.startswith("D")]
    hits: list[str] = []
    patterns = FORBIDDEN + tuple(profile.forbidden)
    for path in lean_files:
        try:
            mode = git(worktree, "ls-tree", "-z", verified_tree, "--", path).stdout.split(" ", 1)[0]
            if mode == "120000":
                # Lean follows a link to source this scan would not read: links are refused, not followed.
                hits.append(f"{path}: a symbolic link; the loop scans Lean files only as regular files")
                continue
            text = read_blob(worktree, verified_tree, path)
            added = added_lines(worktree, base, path, verified_tree)
        except GitError as error:
            hits.append(f"{path}: unreadable: {error}")
            continue
        for number, line in forbidden_hits(text, added, patterns):
            hits.append(f"{path}:{number}: {line}")
    hits += _linked_sources_changed(worktree, verified_tree, {path for _, path in result.changes})
    result.step("no proof holes or escapes in added Lean lines", not hits, "\n".join(hits))
    deleted = [path for status, path in result.changes if path.endswith(".lean") and status.startswith("D")]
    importers: list[str] = []
    try:
        libraries = lean_libraries(worktree) if deleted else ()
    except Exception:  # noqa: BLE001 - without a readable lakefile, a module is named after its path
        libraries = ()
    for path in deleted:
        module = module_of(worktree / path, libraries) or path[: -len(".lean")].replace("/", ".")
        # `[public] [meta] import [all] Module`, the module system's forms included.
        space = "[[:space:]]"
        pattern = rf"^{space}*(public{space}+)?(meta{space}+)?import{space}+(all{space}+)?{re.escape(module)}({space}|$)"
        found = git(worktree, "grep", "-l", "-z", "-E", pattern, verified_tree, "--", "*.lean", check=False)
        importers += [f"{name.split(':', 1)[-1]} imports the deleted {module}"
                      for name in found.stdout.split("\0") if name]
    result.step("nothing imports a deleted module", not importers, "\n".join(importers))
    modules = lean_modules(worktree, lean_files)
    if modules:
        built = run_command(["lake", "build", *modules], cwd=worktree, env=env, timeout=build_timeout)
        result.step(f"lake build {' '.join(modules)[:300]}", built.returncode == 0,
                    (built.stdout or "")[-4000:] + (built.stderr or "")[-3000:])
    if targets and result.ok:
        try:
            head_facts = statement_facts(worktree, modules, list(targets), env=env) if modules else {}
        except GitError as error:
            result.step("statement fidelity", False, str(error))
        else:
            problems = fidelity_problems(targets, head_facts, phase=phase or "proof") if modules else [
                "no changed module declares the task's declarations"
            ]
            result.step("statement fidelity: same statements, definitions and standard axioms", not problems,
                        "\n".join(problems))
    if result.ok:
        for command in profile.verify_commands:
            done = run_command(list(command), cwd=worktree, env=env, timeout=command_timeout)
            result.step(" ".join(command), done.returncode == 0,
                        (done.stdout or "")[-4000:] + (done.stderr or "")[-2000:])
            if done.returncode != 0:
                break
    # The build and the commands read the worktree: it must still be the snapshot.
    if tree_of_worktree(worktree) != verified_tree:
        now = git(worktree, "-c", "core.quotePath=false", "diff", "--cached", "--name-status", "--no-renames",
                  verified_tree, check=False).stdout
        result.step("the checks left the change as it was verified", False,
                    "the checks changed these files after they were read:\n" + now[:3000])
    return result


def publish_check(worktree: Path, *, profile: Profile, base: str, head: str, metadata: Mapping[str, str],
                  scratch: Path, env: Mapping[str, str], public: Sequence[str] = (),
                  timeout: float = 1800) -> tuple[bool, str]:
    """Run the profile's check of what becomes public (commits, branch, title, body), if it has one.

    ``public`` names commits that are already public, such as the base branch's tip or the branch's earlier
    head, passed with the profile's ``publish_check_public`` option so their history is not checked again.
    """

    if not profile.publish_check:
        return True, ""
    path = scratch / "pull-request-metadata.json"
    path.write_text(json.dumps(dict(metadata)), encoding="utf-8")
    values = {"base": base, "head": head, "metadata": str(path)}
    command = [part.format(**values) for part in profile.publish_check]
    if profile.publish_check_public:
        for commit in dict.fromkeys(public):
            command += [profile.publish_check_public, commit]
    done = run_command(command, cwd=worktree, env=env, timeout=timeout)
    return done.returncode == 0, ((done.stdout or "") + (done.stderr or ""))[-4000:]
