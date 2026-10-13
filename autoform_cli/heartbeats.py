"""Measure the heartbeats each blueprint target's elaboration uses.

``autoform audit`` reports where a raised ``maxHeartbeats`` sits; only Lean
can say whether the declaration needs it. ``autoform heartbeats`` elaborates a
copy of each target's declaring file in which the target is wrapped in a
counting command. The command lifts the limit for that one declaration, the
way Mathlib's ``#count_heartbeats in`` does, and reports the heartbeats it
used, in the units ``set_option maxHeartbeats`` takes. Nothing else in the copy
changes, so every other declaration elaborates exactly as in the build.

The count is then compared with the setting in force. A declaration under a
raised limit that fits Lean's default does not need it; one that does need
more is told the least ``200000 * 2^k`` that suffices, which is Mathlib's
rounding, so the limit keeps headroom as the library around it changes.

The counting command is defined in the copy itself, so this needs only Lean,
not Mathlib. Files using the module system (``module``) or ``prelude`` are
not measured yet.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .elaboration_limits import _ANY_OPTION_IN, _ATTRIBUTES_ONLY, _WRAPPER_IN, LIMIT_DEFAULTS, LimitSetting
from .elaboration_limits import limits_in_force
from .graph import GraphValidationError, load_graph
from .lean import SourceIndex, _without_lean_comments, index_failure_message, index_project
from .skeleton import (
    DEFAULT_FRESHNESS_TIMEOUT,
    DEFAULT_PROBE_TIMEOUT,
    SkeletonError,
    UnresolvedTarget,
    _check_artifacts_fresh,
    _CommandTimedOut,
    _run_bounded_command,
    _signal_guard,
    check_configuration_unchanged,
    check_inputs_unchanged,
    lean_libraries,
    locate_declaration,
    resolve_lean_root,
    select_lean_targets,
    snapshot_inputs,
)

REPORT_SCHEMA = "autoform-heartbeats/v1"
DEFAULT_LIMIT = LIMIT_DEFAULTS["maxHeartbeats"]

#: Every count the probe reports starts with this marker.
PROBE_MARKER = "AUTOFORM_HEARTBEATS "
_COUNT = "#autoform_count_heartbeats"
_COUNTER = f"""\
open Lean Elab Command in
elab "{_COUNT} " name:str "in" ppLine cmd:command : command => do
  let start ← IO.getNumHeartbeats
  try
    elabCommand (← `(command| set_option maxHeartbeats 0 in $cmd))
  finally
    let used := ((← IO.getNumHeartbeats) - start) / 1000
    logInfo m!"{PROBE_MARKER}{{used}} {{name.getString}}"
"""
_IMPORT = re.compile(r"import\s")
_COUNT_RECORD = re.compile(rf"{PROBE_MARKER}(\d+) (.+)$")
_ERROR = re.compile(r"^.*?:(\d+):\d+: error(?:\([^)]*\))?: (.*)$")

#: Run one rendered probe for the module that declares its targets; return Lean's output.
ProbeRunner = Callable[[str, Path, str], str]


@dataclass(frozen=True, slots=True)
class Measurement:
    """The heartbeats one ``lean:`` target used, and the limits it runs under.

    ``used`` is ``None`` when Lean did not finish the declaration; ``errors``
    then says why. ``limits`` are its ``maxHeartbeats`` settings, as the audit
    reads them.
    """

    node_id: str
    name: str
    path: str
    line: int
    limits: tuple[LimitSetting, ...]
    used: int | None
    errors: tuple[str, ...] = ()

    @property
    def needed(self) -> int | None:
        """The least ``200000 * 2^k`` covering ``used``, or ``None`` if the default does."""

        if self.used is None or self.used < DEFAULT_LIMIT:
            return None
        needed = DEFAULT_LIMIT
        while needed < self.used:
            needed *= 2
        return needed

    @property
    def findings(self) -> tuple[str, ...]:
        if self.errors:
            return (f"elaboration failed: {self.errors[0]}",)
        if self.used is None:
            return ()
        found = []
        for limit in self.limits:
            if not limit.raised:
                continue
            option = f"`set_option maxHeartbeats {limit.value}` at {self.path}:{limit.line}"
            if self.needed is None:
                found.append(f"uses {self.used} heartbeats, within the default {DEFAULT_LIMIT}; {option} is not needed")
            elif limit.unbounded or limit.scope in {"file", "section"}:
                found.append(
                    f"uses {self.used} heartbeats; put `set_option maxHeartbeats {self.needed} in` on it "
                    f"instead of {option}"
                )
            elif limit.value > self.needed:
                found.append(f"uses {self.used} heartbeats; lower {option} to {self.needed}")
        return tuple(found)

    def as_dict(self) -> dict[str, object]:
        return {
            "errors": list(self.errors),
            "limits": [
                {"line": limit.line, "scope": limit.scope, "value": limit.value} for limit in self.limits
            ],
            "line": self.line,
            "name": self.name,
            "needed": self.needed,
            "node_id": self.node_id,
            "path": self.path,
            "used": self.used,
        }


@dataclass(frozen=True, slots=True)
class HeartbeatsReport:
    """Every target measured, in article order, and every one left unmeasured."""

    measurements: tuple[Measurement, ...]
    unresolved: tuple[UnresolvedTarget, ...]
    schema: str = REPORT_SCHEMA

    @property
    def findings(self) -> tuple[str, ...]:
        """Every finding, as ``node: declaration: reason``."""

        return tuple(
            f"{item.node_id}: {item.name}: {finding}" for item in self.measurements for finding in item.findings
        )

    @property
    def clean(self) -> bool:
        """Every target was measured. Findings advise and never fail the run."""

        return not self.unresolved and not any(item.errors for item in self.measurements)

    def as_dict(self) -> dict[str, object]:
        return {
            "findings": list(self.findings),
            "measurements": [item.as_dict() for item in self.measurements],
            "schema": self.schema,
            "unresolved": [item.as_dict() for item in self.unresolved],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def measure_heartbeats(
    blueprint_dir: str | Path,
    *,
    lean_root: str | Path,
    node_ids: tuple[str, ...] | None = None,
    runner: ProbeRunner | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> HeartbeatsReport:
    """Measure the selected articles' ``lean:`` targets, one declaring file at a time.

    Without ``node_ids``, only targets running under a raised or unbounded
    ``maxHeartbeats`` are measured, since each file costs a full elaboration.
    ``runner`` executes a rendered probe; tests pass a fake.
    """

    try:
        graph = load_graph(blueprint_dir)
    except GraphValidationError as exc:
        raise SkeletonError(exc.issues) from exc
    root = resolve_lean_root(lean_root)
    _, selected, _ = select_lean_targets(graph, node_ids)
    activity = "the heartbeats probe ran"
    inputs = snapshot_inputs(graph, root)
    libraries = lean_libraries(root)
    check_configuration_unchanged(root, inputs, activity=activity)
    try:
        index = index_project(root)
    except OSError as error:
        raise SkeletonError([index_failure_message(error)]) from error

    unresolved: list[UnresolvedTarget] = []
    by_file: dict[Path, list[tuple[str, str, int, tuple[LimitSetting, ...]]]] = {}
    modules: dict[Path, str] = {}
    sources: dict[Path, str] = {}
    for node, names in selected:
        for name in names:
            module, reason = locate_declaration(name, index=index, libraries=libraries, lean_root=root)
            declaration = index.find(name)
            if module is None or declaration is None:
                unresolved.append(UnresolvedTarget(node.id, name, reason or ""))
                continue
            path = declaration.path
            if path not in sources:
                try:
                    sources[path] = (root / path).read_text(encoding="utf-8")
                except (OSError, UnicodeError) as error:
                    raise SkeletonError([f"cannot read {path.as_posix()}: {error}"]) from error
            lines = _without_lean_comments(sources[path]).splitlines()
            limits = tuple(
                setting
                for setting in limits_in_force(lines, declaration.line)
                if setting.option == "maxHeartbeats"
            )
            if node_ids is None and not any(limit.raised for limit in limits):
                continue
            by_file.setdefault(path, []).append((node.id, name, declaration.line, limits))
            modules[path] = module

    execute = runner or (lambda probe, lean_root_, module: _run_probe(probe, lean_root_, module, timeout=timeout))
    measurements: list[Measurement] = []
    for path in sorted(by_file):
        targets = by_file[path]
        text = sources[path]
        reason = _unsupported(text)
        if reason is not None:
            unresolved.extend(UnresolvedTarget(node_id, name, reason) for node_id, name, _, _ in targets)
            continue
        probe, origin = render_probe(text, {name: line for _, name, line, _ in targets})
        try:
            output = execute(probe, root, modules[path])
        except SkeletonError as exc:
            reason = "; ".join(exc.issues)
            unresolved.extend(UnresolvedTarget(node_id, name, reason) for node_id, name, _, _ in targets)
            continue
        counts, errors, inserted = parse_probe_output(output, origin, _spans(text, index, path, targets))
        for node_id, name, line, limits in targets:
            if name not in counts and not errors.get(name):
                reason = "Lean reported no heartbeat count"
                unresolved.append(UnresolvedTarget(node_id, name, f"{reason}: {inserted[0]}" if inserted else reason))
                continue
            measurements.append(
                Measurement(
                    node_id=node_id,
                    name=name,
                    path=path.as_posix(),
                    line=line,
                    limits=limits,
                    used=None if errors.get(name) else counts.get(name),
                    errors=tuple(errors.get(name, ())),
                )
            )
    check_inputs_unchanged(graph, root, index, inputs, activity=activity)
    order = {node.id: position for position, (node, _) in enumerate(selected)}
    measurements.sort(key=lambda item: (order[item.node_id], item.name))
    return HeartbeatsReport(
        measurements=tuple(measurements),
        unresolved=tuple(sorted(set(unresolved), key=lambda item: (item.node_id, item.declaration, item.reason))),
    )


def render_probe(text: str, targets: dict[str, int]) -> tuple[str, list[int | None]]:
    """Wrap each target, given by its declaration line, in the counting command.

    Returns the probe and, for each of its lines, the source line it came from
    (``None`` for an inserted one), so Lean's messages can be placed.
    """

    raw = text.splitlines(keepends=True)
    stripped = _without_lean_comments(text).splitlines()
    stripped += [""] * (len(raw) - len(stripped))
    header_end = 0
    for number, line in enumerate(stripped, start=1):
        if _IMPORT.match(line.strip()):
            header_end = number
        elif line.strip():
            break
    # The import ends the header and the counter is its first command.
    insertions: dict[int, list[str]] = {header_end + 1: ["import Lean.Elab.Command\n", _COUNTER]}
    for name, line in targets.items():
        start = _wrapped_start(raw, stripped, line)
        insertions.setdefault(start, []).append(f"{_COUNT} {json.dumps(name, ensure_ascii=False)} in\n")
    if raw and not raw[-1].endswith(("\n", "\r")):
        raw[-1] += "\n"

    probe: list[str] = []
    origin: list[int | None] = []
    for number in range(1, len(raw) + 2):
        for inserted in insertions.get(number, ()):
            probe.append(inserted)
            origin.extend([None] * inserted.count("\n"))
        if number <= len(raw):
            probe.append(raw[number - 1])
            origin.append(number)
    return "".join(probe), origin


def parse_probe_output(
    output: str, origin: list[int | None], spans: dict[str, tuple[int, int]]
) -> tuple[dict[str, int], dict[str, list[str]], list[str]]:
    """Read each target's count, the errors Lean placed inside its source span,
    and the errors in the lines the probe inserted."""

    counts: dict[str, int] = {}
    errors: dict[str, list[str]] = {}
    inserted: list[str] = []
    for line in output.splitlines():
        record = _COUNT_RECORD.search(line)
        if record is not None:
            name = record.group(2).strip()
            if name in spans:
                counts[name] = int(record.group(1))
            continue
        error = _ERROR.match(line)
        if error is None:
            continue
        generated = int(error.group(1))
        source_line = origin[generated - 1] if 0 < generated <= len(origin) else None
        if source_line is None:
            inserted.append(error.group(2).strip())
            continue
        for name, (first, last) in spans.items():
            if first <= source_line <= last:
                errors.setdefault(name, []).append(f"line {source_line}: {error.group(2).strip()}")
    return counts, errors, inserted


def format_heartbeats(report: HeartbeatsReport) -> str:
    """The report for a person: one line per target, then findings and gaps."""

    lines: list[str] = []
    current = None
    for item in report.measurements:
        if item.node_id != current:
            lines.append(item.node_id)
            current = item.node_id
        if item.used is None:
            lines.append(f"  {item.name}: not measured")
            continue
        raised = [limit for limit in item.limits if limit.raised]
        under = ", ".join(f"{limit.value} ({limit.scope})" for limit in raised) or f"the default {DEFAULT_LIMIT}"
        lines.append(f"  {item.name}: {item.used} heartbeats under {under}")
    lines += [f"finding: {finding}" for finding in report.findings]
    lines += [f"error: {item.message}" for item in report.unresolved]
    return "\n".join(lines) + "\n" if lines else "no Lean declarations run under a raised heartbeat limit\n"


def _wrapped_start(raw: list[str], stripped: list[str], line: int) -> int:
    """The first line of the declaration at ``line`` with its docstring, comments,
    attributes, and ``... in`` wrappers, where a command wrapping it must go."""

    start = line
    while start > 1:
        above = start - 1
        text = raw[above - 1].strip()
        if not text:
            break
        if not stripped[above - 1].strip():
            if text.endswith("-/"):
                # The end of a comment block, which may contain blank lines.
                while above > 1 and "/-" not in raw[above - 1]:
                    above -= 1
            start = above
            continue
        if _wraps_next_command(stripped[above - 1].strip()):
            start = above
            continue
        break
    return start


def _wraps_next_command(line: str) -> bool:
    rest = line
    while wrapper := _ANY_OPTION_IN.match(rest):
        rest = rest[wrapper.end() :]
    return not rest or bool(_WRAPPER_IN.match(rest) or _ATTRIBUTES_ONLY.match(rest))


def _spans(
    text: str, index: SourceIndex, path: Path, targets: list[tuple[str, str, int, tuple[LimitSetting, ...]]]
) -> dict[str, tuple[int, int]]:
    """Each target's source lines, from its wrappers to the line before the next declaration."""

    raw = text.splitlines()
    stripped = _without_lean_comments(text).splitlines()
    stripped += [""] * (len(raw) - len(stripped))
    starts = sorted(declaration.line for declaration in index.declarations.values() if declaration.path == path)
    spans = {}
    for _, name, line, _ in targets:
        following = [start for start in starts if start > line]
        last = following[0] - 1 if following else len(raw)
        spans[name] = (_wrapped_start(raw, stripped, line), last)
    return spans


def _unsupported(text: str) -> str | None:
    """Why the probe cannot add its import to this file's header, if it cannot."""

    for line in _without_lean_comments(text).splitlines():
        if line.strip() == "module":
            return "files using the module system (`module`) are not measured yet"
        if line.strip() == "prelude":
            return "`prelude` files cannot import the counting command"
        if line.strip():
            return None
    return None


def _run_probe(probe: str, lean_root: Path, module: str, *, timeout: float) -> str:
    """Elaborate ``probe`` with ``lake env lean`` once ``module``'s imports are fresh."""

    lake = shutil.which("lake")
    if lake is None:
        raise SkeletonError(["lake is not on PATH; measuring heartbeats requires a Lean toolchain"])
    _check_artifacts_fresh(
        lake, lean_root, (module,), timeout=DEFAULT_FRESHNESS_TIMEOUT, label="heartbeats probe"
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    with _signal_guard(), tempfile.TemporaryDirectory(prefix="autoform-heartbeats-") as scratch:
        source = Path(scratch) / "AutoformHeartbeatsProbe.lean"
        source.write_text(probe, encoding="utf-8")
        try:
            result = _run_bounded_command(
                [lake, "env", "lean", str(source)],
                cwd=lean_root,
                timeout=timeout,
                context="lake env lean",
                env=env,
            )
        except _CommandTimedOut as exc:
            raise SkeletonError(
                [f"lake env lean timed out after {timeout:g} seconds; rerun with --timeout <seconds>"]
            ) from exc
    return result.stdout + result.stderr


__all__ = [
    "PROBE_MARKER",
    "REPORT_SCHEMA",
    "HeartbeatsReport",
    "Measurement",
    "format_heartbeats",
    "measure_heartbeats",
    "parse_probe_output",
    "render_probe",
]
