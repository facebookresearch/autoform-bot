"""Run deterministic, local diagnostics over the canonical runtime graph.

This is a project/runtime doctor, not a worker-fleet preflight.  It reads the
Markdown roadmap and optional Lean sources without invoking Git, subprocesses,
network services, renderers, claims, queues, or state stores.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath

from .audit import AuditFinding, audit_graph
from .graph import GraphValidationError, load_graph
from .runtime import (
    RUNTIME_AUTHORITY,
    RUNTIME_SCHEMA,
    RuntimeGraph,
    RuntimePaths,
    RuntimeProjectionError,
    build_runtime_graph,
    resolve_runtime_paths,
)

_LEAN_FINDING_CODES = frozenset(
    {
        "invalid-lean-root",
        "lean-target-kind-mismatch",
        "lean-target-not-found",
        "missing-lean-target",
    }
)
_CHECK_NAMES = ("blueprint", "runtime", "graph", "references", "audit", "lean targets")
_QUOTED_STRING = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One stable diagnostic result."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorResult:
    """The complete ordered result of a local project diagnosis."""

    checks: tuple[DoctorCheck, ...]

    @property
    def clean(self) -> bool:
        return all(check.ok for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "checks": [asdict(check) for check in self.checks],
            "clean": self.clean,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def diagnose_project(
    project_or_blueprint: str | Path,
    *,
    lean_root: str | Path | None = None,
) -> DoctorResult:
    """Diagnose one project using only its local authored source files."""

    paths: RuntimePaths | None = None
    graph = None
    runtime: RuntimeGraph | None = None
    checks: list[DoctorCheck] = []

    try:
        paths = resolve_runtime_paths(project_or_blueprint)
    except RuntimeProjectionError as error:
        checks.append(DoctorCheck("blueprint", False, _issues(error.issues)))
    except (OSError, RuntimeError, ValueError):
        checks.append(DoctorCheck("blueprint", False, "project or blueprint path cannot be resolved"))
    else:
        checks.append(
            DoctorCheck(
                "blueprint",
                True,
                f"resolved {paths.blueprint_dir.relative_to(paths.project_root).as_posix()}",
            )
        )

    if paths is None:
        return _blocked_result(checks, "blueprint resolution failed", lean_root=lean_root)

    try:
        graph = load_graph(paths.blueprint_dir)
    except GraphValidationError as error:
        reason = _sanitize_issues(error.issues, paths)
        checks.append(DoctorCheck("runtime", False, "canonical graph is invalid"))
        checks.append(DoctorCheck("graph", False, reason))
        checks.append(DoctorCheck("references", False, "not checked because graph validation failed"))
        checks.append(DoctorCheck("audit", False, "not checked because graph validation failed"))
        checks.append(_blocked_lean_check("graph validation failed", lean_root))
        return _result(checks)

    resolved_lean_root, lean_root_valid = _resolve_lean_root(lean_root)
    try:
        runtime = build_runtime_graph(
            graph,
            project_root=paths.project_root,
            lean_root=resolved_lean_root,
        )
    except RuntimeProjectionError as error:
        checks.append(DoctorCheck("runtime", False, _issues(error.issues)))
    else:
        checks.append(
            DoctorCheck(
                "runtime",
                runtime.schema == RUNTIME_SCHEMA and runtime.authority == RUNTIME_AUTHORITY,
                f"{runtime.schema}; {runtime.authority}; revision {runtime.source_revision}",
            )
        )

    if runtime is None:
        checks.append(DoctorCheck("graph", False, "not summarized because runtime projection failed"))
        checks.append(DoctorCheck("references", False, "not checked because runtime projection failed"))
        checks.append(DoctorCheck("audit", False, "not checked because runtime projection failed"))
        checks.append(_blocked_lean_check("runtime projection failed", lean_root))
        return _result(checks)

    checks.append(
        DoctorCheck(
            "graph",
            True,
            (
                f"{runtime.article_count} articles; {runtime.dependency_count} dependencies; "
                f"{runtime.formalizable_count} formalizable; "
                f"{runtime.dispatchable_count} dispatchable; depth {runtime.maximum_depth}"
            ),
        )
    )
    checks.append(
        DoctorCheck(
            "references",
            True,
            "all parents, typed dependencies, and dispatchable leaves are consistent",
        )
    )

    audit = audit_graph(graph, lean_root=resolved_lean_root)
    lean_findings = tuple(finding for finding in audit.findings if finding.code in _LEAN_FINDING_CODES)
    if lean_root is not None and not lean_root_valid:
        lean_findings = (
            AuditFinding(".", "invalid-lean-root", "Lean root does not exist or is not a directory"),
            *lean_findings,
        )
    roadmap_findings = tuple(finding for finding in audit.findings if finding.code not in _LEAN_FINDING_CODES)
    checks.append(_finding_check("audit", roadmap_findings, "roadmap audit passed"))
    if lean_root is None:
        checks.append(DoctorCheck("lean targets", True, "not checked; no Lean root supplied"))
    else:
        checks.append(_finding_check("lean targets", lean_findings, "all asserted local Lean targets resolve"))
    return _result(checks)


def _blocked_result(
    checks: list[DoctorCheck],
    reason: str,
    *,
    lean_root: str | Path | None,
) -> DoctorResult:
    for name in _CHECK_NAMES[len(checks) : -1]:
        checks.append(DoctorCheck(name, False, f"not checked because {reason}"))
    checks.append(_blocked_lean_check(reason, lean_root))
    return _result(checks)


def _blocked_lean_check(reason: str, lean_root: str | Path | None) -> DoctorCheck:
    if lean_root is None:
        return DoctorCheck("lean targets", True, "not checked; no Lean root supplied")
    return DoctorCheck("lean targets", False, f"not checked because {reason}")


def _finding_check(name: str, findings: tuple[AuditFinding, ...], clean_detail: str) -> DoctorCheck:
    if not findings:
        return DoctorCheck(name, True, clean_detail)
    codes = ", ".join(sorted({finding.code for finding in findings}))
    return DoctorCheck(name, False, f"{len(findings)} finding(s): {codes}")


def _resolve_lean_root(lean_root: str | Path | None) -> tuple[Path | None, bool]:
    if lean_root is None:
        return None, True
    try:
        root = Path(lean_root).expanduser().resolve()
        valid = root.is_dir()
    except (OSError, RuntimeError, ValueError):
        return None, False
    return (root if valid else None), valid


def _sanitize_issues(issues: tuple[str, ...], paths: RuntimePaths) -> str:
    sanitized = []
    replacements = (
        (str(paths.blueprint_dir), "<blueprint>"),
        (str(paths.project_root), "<project>"),
    )
    for issue in issues:
        for absolute, label in replacements:
            issue = issue.replace(absolute, label)
        sanitized.append(_redact_absolute_paths(issue))
    return _issues(tuple(sanitized))


def _redact_absolute_paths(issue: str) -> str:
    def replace(match: re.Match[str]) -> str:
        try:
            value = ast.literal_eval(match.group(0))
        except (SyntaxError, ValueError):
            return match.group(0)
        if isinstance(value, str) and (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()):
            return "'<absolute-path>'"
        return match.group(0)

    return _QUOTED_STRING.sub(replace, issue)


def _issues(issues: tuple[str, ...]) -> str:
    return "; ".join(sorted(set(issues)))


def _result(checks: list[DoctorCheck]) -> DoctorResult:
    result = DoctorResult(tuple(checks))
    if tuple(check.name for check in result.checks) != _CHECK_NAMES:
        raise AssertionError("doctor checks are not complete and canonically ordered")
    return result


__all__ = ["DoctorCheck", "DoctorResult", "diagnose_project"]
