"""Fail-closed proof verification through Autoform's shared Lean runtime."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from autoform_cli.lean import index_project
from autoform_cli.runtime import RuntimeNode
from servers import resolve_lean_file, resolve_lean_project_dir
from servers.lean_client import LeanRuntimeClient, LeanRuntimeError


class RuntimeClient(Protocol):
    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        autostart: bool | None = None,
        response_timeout: float | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class Baseline:
    """Lean inputs and declaration identities captured before one prover attempt."""

    root: Path
    files: dict[str, bytes] = field(default_factory=dict)
    targets: frozenset[str] = frozenset()
    headers: dict[str, bytes] = field(default_factory=dict)
    declaration_types: dict[str, str] = field(default_factory=dict)
    target_contexts: dict[str, tuple[bytes, ...]] = field(default_factory=dict)
    observed_candidates: dict[str, bytes | None] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str = ""
    checks: dict[str, Any] = field(default_factory=dict)


_IGNORED_PARTS = frozenset({".git", ".lake", "build", "lake-packages"})
_CONFIG_NAMES = frozenset({"lakefile.lean", "lakefile.toml", "lake-manifest.json", "lean-toolchain"})
_SORRY = re.compile(r"\b(?:sorry|admit|sorryAx)\b(?!-)")
_ASSUMPTION = re.compile(r"\b(?:axiom|constant)\b")
_UNSAFE_ELABORATION = re.compile(
    r"\b(?:run_cmd|initialize|elab|foreign|extern|syntax|"
    r"macro|macro_rules|native_decide|run_tac|include_str|include_bytes)\b|"
    r"#(?:eval|reduce|run)\b|"
    r"\bunsafe\s+(?:def|abbrev|theorem|instance)\b"
)
_DECLARATION_END = re.compile(r":=|\bwhere\b")
_CLEAN_DIAGNOSTICS = "No diagnostics — file compiles cleanly."
_DIAGNOSTIC_SUMMARY = re.compile(r"^Diagnostics: (\d+) error\(s\), (\d+) warning\(s\)(?:\n|$)")
_MODULE_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")
_TOP_LEVEL_COMMAND = re.compile(
    r"^(?:@\[|attribute\b|open\b|export\b|set_option\b|namespace\b|section\b|"
    r"end\b|variable\b|include\b|omit\b|theorem\b|lemma\b|def\b|abbrev\b|"
    r"instance\b|structure\b|class\b|inductive\b|opaque\b|axiom\b|constant\b)"
)
_ALLOWED_AXIOMS = ("propext", "Classical.choice", "Quot.sound")


def _strip_comments_and_literals(source: str) -> str:
    """Blank nested comments, strings, and complete character literals."""

    output: list[str] = []
    index = 0
    block_depth = 0
    in_string = False
    escaped = False
    while index < len(source):
        pair = source[index : index + 2]
        char = source[index]
        if block_depth:
            if pair == "/-":
                block_depth += 1
                output.extend("  ")
                index += 2
            elif pair == "-/":
                block_depth -= 1
                output.extend("  ")
                index += 2
            else:
                output.append("\n" if char == "\n" else " ")
                index += 1
            continue
        if in_string:
            output.append("\n" if char == "\n" else " ")
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if pair == "--":
            while index < len(source) and source[index] != "\n":
                output.append(" ")
                index += 1
            continue
        if pair == "/-":
            block_depth = 1
            output.extend("  ")
            index += 2
            continue
        char_literal = re.match(r"'(?:\\.|[^'\\])'", source[index:])
        if char_literal:
            value = char_literal.group(0)
            output.extend(" " * len(value))
            index += len(value)
            continue
        output.append(" " if char == '"' else char)
        if char == '"':
            in_string = True
        index += 1
    return "".join(output)


def unsafe_elaboration_directive(source: str) -> str:
    """Return the first proof escape or executable elaboration hook."""

    stripped = _strip_comments_and_literals(source)
    matches = [match for pattern in (_SORRY, _UNSAFE_ELABORATION) if (match := pattern.search(stripped))]
    return min(matches, key=lambda match: match.start()).group(0).strip() if matches else ""


def _relevant_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*.lean")):
        relative = path.relative_to(root)
        if _IGNORED_PARTS.intersection(relative.parts) or not path.is_file():
            continue
        files[relative.as_posix()] = path.read_bytes()
    for name in _CONFIG_NAMES:
        path = root / name
        if path.is_file():
            files[name] = path.read_bytes()
    return files


def _target_files(node: RuntimeNode, project_dir: str) -> list[tuple[str, Path]]:
    if not node.dispatchable or not node.status.can_prove or node.assertions.not_ready:
        raise ValueError(f"runtime node is not ready to prove: {node.id}")
    paths: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for target in node.lean_targets:
        if not target.source_file or target.source_file in seen:
            continue
        _, path = resolve_lean_file(project_dir, target.source_file)
        paths.append((target.source_file, path))
        seen.add(target.source_file)
    if not paths or not node.lean_targets:
        raise ValueError(f"runtime node has no local Lean source target: {node.id}")
    return paths


def _declaration_bounds(
    root: Path,
    name: str,
    source_file: str,
    *,
    index: Any | None = None,
) -> tuple[int, int]:
    source_index = index or index_project(root)
    declaration = source_index.find(name)
    if declaration is None or declaration.path.as_posix() != source_file:
        raise ValueError(f"target declaration does not resolve in {source_file}: {name}")
    start = declaration.line - 1
    following = [
        item.line - 1
        for item in source_index.declarations.values()
        if item.path == declaration.path and item.line > declaration.line
    ]
    source_lines = (root / declaration.path).read_text(encoding="utf-8").splitlines(keepends=True)
    declaration_indent = len(source_lines[start]) - len(source_lines[start].lstrip(" \t"))
    cleaned_lines = _strip_comments_and_literals("".join(source_lines)).splitlines()
    command_end = len(source_lines)
    for line_number in range(start + 1, len(source_lines)):
        raw = source_lines[line_number]
        stripped = raw.lstrip(" \t")
        indent = len(raw) - len(stripped)
        if indent <= declaration_indent and stripped.startswith(("--", "/-")):
            command_end = line_number
            break
        cleaned = cleaned_lines[line_number] if line_number < len(cleaned_lines) else ""
        if indent <= declaration_indent and _TOP_LEVEL_COMMAND.match(cleaned.lstrip()):
            command_end = line_number
            break
    return start, min(command_end, min(following, default=len(source_lines)))


def _declaration_header(root: Path, name: str, source_file: str) -> bytes:
    start, end = _declaration_bounds(root, name, source_file)
    text = (root / source_file).read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    segment = "".join(lines[start:end])
    match = _DECLARATION_END.search(_strip_comments_and_literals(segment))
    if match is None:
        raise ValueError(f"target declaration has no proof boundary: {name}")
    return segment[: match.end()].encode("utf-8")


def _declaration_segment(root: Path, name: str, source_file: str) -> str:
    start, end = _declaration_bounds(root, name, source_file)
    lines = (root / source_file).read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(lines[start:end])


def _declaration_contexts(root: Path, node: RuntimeNode) -> dict[str, tuple[bytes, ...]]:
    """Return the immutable bytes around every target declaration in each target file."""

    source_index = index_project(root)
    by_file: dict[str, list[tuple[int, int]]] = {}
    for target in node.lean_targets:
        if not target.source_file:
            continue
        bounds = _declaration_bounds(
            root,
            target.declaration,
            target.source_file,
            index=source_index,
        )
        by_file.setdefault(target.source_file, []).append(bounds)

    contexts: dict[str, tuple[bytes, ...]] = {}
    for relative, bounds in by_file.items():
        lines = (root / relative).read_text(encoding="utf-8").splitlines(keepends=True)
        ordered = sorted(bounds)
        if any(end > next_start for (_, end), (next_start, _) in zip(ordered, ordered[1:])):
            raise ValueError(f"overlapping target declarations in {relative}")
        cursor = 0
        outside: list[bytes] = []
        for start, end in ordered:
            outside.append("".join(lines[cursor:start]).encode("utf-8"))
            cursor = end
        outside.append("".join(lines[cursor:]).encode("utf-8"))
        contexts[relative] = tuple(outside)
    return contexts


def _declaration_type(
    client: RuntimeClient,
    root: Path,
    name: str,
    source_file: str,
) -> str:
    source_index = index_project(root)
    declaration = source_index.find(name)
    if declaration is None or declaration.path.as_posix() != source_file:
        raise ValueError(f"target declaration does not resolve in {source_file}: {name}")
    start = declaration.line - 1
    line = (root / source_file).read_text(encoding="utf-8").splitlines()[start]
    short_name = name.rsplit(".", 1)[-1]
    name_match = re.search(
        rf"\b{re.escape(declaration.keyword)}\s+({re.escape(short_name)})(?=[\s:(){{}}\[\]⦃⦄,])",
        line,
    )
    if name_match is None:
        raise ValueError(f"target declaration name is not present on its indexed line: {name}")
    prefix = line[: name_match.start(1)]
    name_start = len(prefix.encode("utf-16-le")) // 2
    hover = client.request(
        "lsp.hover",
        {
            "project_dir": str(root),
            "file_path": source_file,
            "line": start,
            "character": name_start + max(0, len(short_name.encode("utf-16-le")) // 4),
        },
    )
    if not isinstance(hover, str) or not hover.strip() or hover.startswith("No hover information"):
        raise ValueError(f"Lean could not report the elaborated type of {name}")
    return hover.strip()


def capture_baseline(
    node: RuntimeNode,
    project_dir: str,
    *,
    runtime: RuntimeClient | None = None,
) -> Baseline:
    root = resolve_lean_project_dir(project_dir)
    targets = frozenset(relative for relative, _ in _target_files(node, str(root)))
    headers: dict[str, bytes] = {}
    declaration_types: dict[str, str] = {}
    client = runtime or LeanRuntimeClient()
    for target in node.lean_targets:
        if target.source_file:
            headers[target.declaration] = _declaration_header(
                root, target.declaration, target.source_file
            )
            declaration_types[target.declaration] = _declaration_type(
                client,
                root,
                target.declaration,
                target.source_file,
            )
    return Baseline(
        root=root,
        files=_relevant_files(root),
        targets=targets,
        headers=headers,
        declaration_types=declaration_types,
        target_contexts=_declaration_contexts(root, node),
    )


def _read_regular_nofollow(path: Path) -> tuple[bytes, tuple[int, int]] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError):
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks), (info.st_dev, info.st_ino)
    finally:
        os.close(descriptor)


def observe_candidates(baseline: Baseline) -> None:
    """Record changed Lean/config bytes attributable to the current attempt."""

    current = _relevant_files(baseline.root)
    baseline.observed_candidates.clear()
    for relative in current.keys() | baseline.files.keys():
        candidate = current.get(relative)
        if candidate != baseline.files.get(relative):
            baseline.observed_candidates[relative] = candidate


def restore_baseline(baseline: Baseline) -> None:
    """Restore only verifier-observed candidate bytes using compare-and-swap."""

    for relative, observed in tuple(baseline.observed_candidates.items()):
        path = baseline.root / relative
        current = _read_regular_nofollow(path)
        if observed is None:
            if current is not None or path.is_symlink():
                continue
        elif current is None or current[0] != observed:
            continue
        original = baseline.files.get(relative)
        if original is None:
            try:
                identity = (path.lstat().st_dev, path.lstat().st_ino)
                if current is not None and identity == current[1]:
                    path.unlink()
            except FileNotFoundError:
                pass
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if observed is None:
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            try:
                os.write(descriptor, original)
            finally:
                os.close(descriptor)
            continue
        descriptor, raw_path = tempfile.mkstemp(prefix=".autoform_restore_", dir=path.parent)
        replacement = Path(raw_path)
        try:
            os.write(descriptor, original)
            os.close(descriptor)
            descriptor = -1
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if current is not None and (info.st_dev, info.st_ino) == current[1]:
                os.replace(replacement, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            replacement.unlink(missing_ok=True)


def _new_forbidden(before: str, after: str) -> str:
    before_clean = _strip_comments_and_literals(before)
    after_clean = _strip_comments_and_literals(after)
    for pattern in (_SORRY, _ASSUMPTION, _UNSAFE_ELABORATION):
        old = Counter(match.group(0) for match in pattern.finditer(before_clean))
        new = Counter(match.group(0) for match in pattern.finditer(after_clean))
        for token, count in new.items():
            if count > old[token]:
                return token
    return ""


def _diagnostics_are_clean(value: str) -> bool:
    if value == _CLEAN_DIAGNOSTICS:
        return True
    summary = _DIAGNOSTIC_SUMMARY.match(value)
    return summary is not None and int(summary.group(1)) == 0


def _lean_name(name: str) -> str:
    expression = "Name.anonymous"
    for part in name.split("."):
        if not part:
            raise ValueError(f"invalid empty component in Lean name: {name!r}")
        expression = f"Name.str ({expression}) {json.dumps(part)}"
    return expression


def _module_name(source_file: str) -> str:
    path = Path(source_file)
    if path.suffix != ".lean" or not path.parts:
        raise ValueError(f"target source is not a Lean module: {source_file}")
    parts = (*path.parts[:-1], path.stem)
    if not all(_MODULE_PART.fullmatch(part) for part in parts):
        raise ValueError(f"target source has a non-importable module name: {source_file}")
    return ".".join(parts)


def _axiom_audit_source(node: RuntimeNode) -> str:
    modules = sorted(
        {
            _module_name(target.source_file)
            for target in node.lean_targets
            if target.source_file
        }
    )
    targets = [
        _lean_name(target.declaration)
        for target in node.lean_targets
        if target.source_file
    ]
    imports = "\n".join(f"import {module}" for module in modules)
    allowed = ", ".join(f"``{name}" for name in _ALLOWED_AXIOMS)
    target_names = ", ".join(targets)
    return f"""{imports}
import Lean.Util.CollectAxioms
import Lean.Elab.Command

open Lean Elab Command

run_cmd do
  let allowed : List Name := [{allowed}]
  let targets : List Name := [{target_names}]
  let env ← getEnv
  let mut missing : Array Name := #[]
  let mut bad : Array (Name × Name) := #[]
  for target in targets do
    if (env.find? target).isNone then
      missing := missing.push target
    else
      for usedAxiom in (← Lean.collectAxioms target) do
        unless allowed.contains usedAxiom do
          bad := bad.push (target, usedAxiom)
  for target in missing do
    logError m!"target declaration is not exported: {{target}}"
  for (target, usedAxiom) in bad do
    logError m!"{{target}} depends on unexpected axiom {{usedAxiom}}"
  unless missing.isEmpty && bad.isEmpty do
    throwError "target declarations failed the kernel trust audit"
"""


def _run_axiom_audit(
    client: RuntimeClient,
    root: Path,
    node: RuntimeNode,
) -> tuple[str, str]:
    audit_dir = root / ".lake" / "autoform-verify"
    audit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix="AutoformVerify_",
        suffix=".lean",
        dir=audit_dir,
    )
    os.close(descriptor)
    path = Path(raw_path)
    try:
        path.write_text(_axiom_audit_source(node), encoding="utf-8")
        diagnostics = client.request(
            "lsp.diagnostics",
            {
                "project_dir": str(root),
                "file_path": path.relative_to(root).as_posix(),
            },
        )
        if not isinstance(diagnostics, str):
            return path.name, repr(diagnostics)
        return path.name, diagnostics
    finally:
        path.unlink(missing_ok=True)


def verify_proof(
    node: RuntimeNode,
    project_dir: str,
    *,
    baseline: Baseline | None = None,
    runtime: RuntimeClient | None = None,
) -> VerifyResult:
    """Verify confined target edits and clean shared-runtime diagnostics."""

    checks: dict[str, Any] = {"node": node.id, "targets": []}
    try:
        root = resolve_lean_project_dir(project_dir)
        targets = _target_files(node, str(root))
        current = _relevant_files(root)
    except (OSError, UnicodeError, ValueError) as error:
        return VerifyResult(False, str(error), checks)

    changed_contexts: list[str] = []
    if baseline is not None:
        observe_candidates(baseline)
        protected = baseline.files.keys() - baseline.targets
        changed_protected = sorted(
            relative for relative in protected if current.get(relative) != baseline.files[relative]
        )
        created = sorted(current.keys() - baseline.files.keys())
        missing = sorted(baseline.files.keys() - current.keys())
        if changed_protected or created or missing:
            affected = changed_protected + created + missing
            return VerifyResult(False, f"prover changed non-target Lean/config inputs: {affected}", checks)
        try:
            contexts = _declaration_contexts(root, node)
        except (OSError, UnicodeError, ValueError) as error:
            return VerifyResult(False, str(error), checks)
        changed_contexts = sorted(
            relative
            for relative in baseline.targets
            if contexts.get(relative) != baseline.target_contexts.get(relative)
        )

    changed: list[str] = []
    for relative, path in targets:
        raw = current.get(relative)
        if raw is None:
            return VerifyResult(False, f"Lean target disappeared: {relative}", checks)
        if baseline is None or baseline.files.get(relative) != raw:
            changed.append(relative)
        try:
            source = raw.decode("utf-8")
            before = (
                baseline.files.get(relative, b"").decode("utf-8")
                if baseline is not None
                else ""
            )
        except UnicodeError as error:
            return VerifyResult(False, f"cannot decode Lean target {relative}: {error}", checks)
        forbidden = _new_forbidden(before, source)
        if forbidden:
            return VerifyResult(False, f"{relative} introduced forbidden token {forbidden!r}", checks)
        checks["targets"].append({"file": relative, "static": "clean"})

    if changed_contexts:
        return VerifyResult(
            False,
            f"prover changed bytes outside target declarations: {changed_contexts}",
            checks,
        )
    if baseline is not None and not changed:
        return VerifyResult(False, "the prover did not change a canonical Lean target", checks)
    checks["changed_targets"] = changed

    client = runtime or LeanRuntimeClient()
    for target in node.lean_targets:
        if not target.source_file:
            continue
        try:
            header = _declaration_header(root, target.declaration, target.source_file)
            segment = _declaration_segment(root, target.declaration, target.source_file)
        except (OSError, UnicodeError, ValueError) as error:
            return VerifyResult(False, str(error), checks)
        if baseline is not None:
            try:
                declaration_type = _declaration_type(
                    client,
                    root,
                    target.declaration,
                    target.source_file,
                )
            except (LeanRuntimeError, OSError, UnicodeError, ValueError) as error:
                return VerifyResult(False, str(error), checks)
            if declaration_type != baseline.declaration_types.get(target.declaration):
                return VerifyResult(
                    False,
                    f"target declaration elaborated type changed: {target.declaration}",
                    checks,
                )
            if header != baseline.headers.get(target.declaration):
                return VerifyResult(
                    False,
                    f"target declaration header changed: {target.declaration}",
                    checks,
                )
        forbidden = unsafe_elaboration_directive(segment)
        if forbidden:
            return VerifyResult(
                False,
                f"target declaration {target.declaration} contains forbidden token {forbidden!r}",
                checks,
            )

    for item in checks["targets"]:
        relative = item["file"]
        try:
            diagnostics = client.request(
                "lsp.diagnostics",
                {"project_dir": str(root), "file_path": relative},
            )
        except LeanRuntimeError as error:
            return VerifyResult(False, f"Lean verification failed for {relative}: {error}", checks)
        item["lsp"] = diagnostics
        if not isinstance(diagnostics, str) or not _diagnostics_are_clean(diagnostics):
            return VerifyResult(False, f"Lean diagnostics were not a recognized clean result for {relative}: {diagnostics!r}", checks)

    try:
        audit_file, audit_diagnostics = _run_axiom_audit(client, root, node)
    except (LeanRuntimeError, OSError, UnicodeError, ValueError) as error:
        return VerifyResult(False, f"Lean kernel trust audit failed: {error}", checks)
    checks["axiom_audit"] = {
        "file": audit_file,
        "diagnostics": audit_diagnostics,
        "allowed": list(_ALLOWED_AXIOMS),
    }
    if not _diagnostics_are_clean(audit_diagnostics):
        return VerifyResult(
            False,
            f"Lean kernel trust audit rejected target declarations: {audit_diagnostics!r}",
            checks,
        )

    checks["declarations"] = [target.declaration for target in node.lean_targets]
    return VerifyResult(True, checks=checks)


__all__ = [
    "Baseline",
    "VerifyResult",
    "capture_baseline",
    "restore_baseline",
    "unsafe_elaboration_directive",
    "verify_proof",
]
