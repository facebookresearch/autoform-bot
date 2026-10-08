"""Hypotheses that the proof of a blueprint theorem never uses.

A theorem can compile and be true while one of its hypotheses plays no part.
Either the source states a hypothesis it does not need, or the formal statement
says less than the source, so that the hypothesis has nothing left to do.
Either way a reviewer should know.

The proof answers this exactly. It is a term the kernel has checked; a
hypothesis the term never mentions can be deleted, and the same term proves
what remains. ``autoform probe`` reads the proof of every theorem an article
names in the built environment, the same way ``autoform skeleton`` reads its
statements, and adds nothing to the skeleton report, its packets, or its
hashes. A hypothesis the proof does use may still be unnecessary; only the
other direction is certain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .graph import Graph, GraphValidationError, load_graph
from .lean import declaration_names, index_failure_message, index_project
from .skeleton import (
    DEFAULT_PROBE_TIMEOUT,
    PROBE_OUTPUT_ENV,
    ProbeRunner,
    SkeletonError,
    _lean_name,
    lean_libraries,
    module_of,
    run_probe,
)

REPORT_SCHEMA = "autoform-probe/v1"

#: Every line the probe wants read back starts with this marker, so Lean's own
#: informational output can never be mistaken for a result.
PROBE_MARKER = "AUTOFORM_PROBE "

#: What the probe says about a declaration's proof: read, resting on ``sorry``
#: (whose term uses nothing, so it is not read), or not a theorem.
_PROOFS = frozenset({"proved", "sorry", "none"})
_KINDS = frozenset({"theorem", "axiom", "def", "opaque", "inductive", "other"})


class ProbeError(RuntimeError):
    """The statement probe could not run, or answered something it should not."""

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """A propositional hypothesis that could be deleted from a theorem.

    ``used`` is false only when the proof term never mentions it, which makes
    the weakened statement a theorem with the same proof.
    """

    name: str
    type: str
    used: bool

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "type": self.type, "used": self.used}


@dataclass(frozen=True, slots=True)
class ProbedDeclaration:
    """What the probe found out about one declaration an article names."""

    node_id: str
    name: str
    kind: str
    #: ``proved``, ``sorry`` or ``none`` (not a theorem).
    proof: str
    #: The hypotheses that could be deleted; empty unless ``proof`` is ``proved``.
    #: One that the conclusion or a later binder mentions is not listed.
    hypotheses: tuple[Hypothesis, ...]

    @property
    def findings(self) -> tuple[tuple[str, str], ...]:
        """``(code, reason)`` for every hypothesis the proof never uses."""

        return tuple(
            ("hypothesis-unused", f"the proof never uses {item.name} : {item.type}")
            for item in self.hypotheses
            if not item.used
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "hypotheses": [item.as_dict() for item in self.hypotheses],
            "kind": self.kind,
            "name": self.name,
            "node_id": self.node_id,
            "proof": self.proof,
        }


@dataclass(frozen=True, slots=True)
class UnprobedTarget:
    """One selected declaration the probe could not ask about."""

    node_id: str
    declaration: str
    reason: str

    @property
    def message(self) -> str:
        return f"{self.node_id}: {self.declaration}: {self.reason}"

    def as_dict(self) -> dict[str, str]:
        return {"declaration": self.declaration, "node_id": self.node_id, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class ProbeReport:
    """Every probed declaration, in article order, and every one left unprobed."""

    declarations: tuple[ProbedDeclaration, ...]
    unresolved: tuple[UnprobedTarget, ...]
    schema: str = REPORT_SCHEMA

    @property
    def findings(self) -> tuple[str, ...]:
        """Every finding, as ``node: declaration: code: reason``."""

        return tuple(
            f"{item.node_id}: {item.name}: {code}: {reason}"
            for item in self.declarations
            for code, reason in item.findings
        )

    @property
    def clean(self) -> bool:
        """Every name was probed. Findings are for a reviewer and never fail the
        run: the source may state a hypothesis its theorem does not need."""

        return not self.unresolved

    def as_dict(self) -> dict[str, object]:
        return {
            "declarations": [item.as_dict() for item in self.declarations],
            "findings": list(self.findings),
            "schema": self.schema,
            "unresolved": [item.as_dict() for item in self.unresolved],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def probe_statements(
    blueprint_dir: str | Path,
    *,
    lean_root: str | Path,
    node_ids: tuple[str, ...] | None = None,
    runner: ProbeRunner | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> ProbeReport:
    """Probe every ``lean:`` declaration the selected articles name.

    ``runner`` executes the rendered probe and returns its records; tests pass a
    fake. A declaration the source index cannot place is reported as unresolved
    without running Lean, as ``autoform skeleton`` reports it.
    """

    try:
        graph = load_graph(blueprint_dir)
    except GraphValidationError as exc:
        raise ProbeError(exc.issues) from exc
    root = Path(lean_root).expanduser().resolve()
    try:
        libraries = lean_libraries(root)
        index = index_project(root)
    except SkeletonError as exc:
        raise ProbeError(exc.issues) from exc
    except OSError as error:
        raise ProbeError([index_failure_message(error)]) from error

    unresolved: list[UnprobedTarget] = []
    requests: list[tuple[str, str]] = []
    imports: set[str] = set()
    for node_id, names in _targets(graph, node_ids):
        for name in names:
            location = index.find(name)
            if location is None:
                unresolved.append(UnprobedTarget(node_id, name, "declaration not found in the Lean sources"))
                continue
            module = module_of(root / location.path, libraries)
            if module is None:
                reason = f"source {location.path.as_posix()} is not built by any library target"
                unresolved.append(UnprobedTarget(node_id, name, reason))
                continue
            imports.add(module)
            requests.append((node_id, name))
    if not requests:
        return ProbeReport(declarations=(), unresolved=tuple(unresolved))

    roots = tuple(dict.fromkeys(name for _, name in requests))
    execute = runner or (
        lambda probe, lean_root_: run_probe(probe, lean_root_, timeout=timeout, label="statement probe")
    )
    try:
        output = execute(render_probe(imports=tuple(sorted(imports)), roots=roots), root)
        # An answer is about the sources it was computed from. If they changed
        # while Lean ran, it may describe a state that no longer exists.
        if index_project(root).source_digest != index.source_digest:
            raise ProbeError(["Lean sources changed while the probe ran; retry after the build is idle"])
    except SkeletonError as exc:
        raise ProbeError(exc.issues) from exc
    except OSError as error:
        raise ProbeError([index_failure_message(error)]) from error
    records = parse_probe_output(output, expected_roots=roots)

    declarations: list[ProbedDeclaration] = []
    for node_id, name in requests:
        record = records[name]
        if not record["found"]:
            unresolved.append(UnprobedTarget(node_id, name, "declaration not found in the built environment"))
            continue
        declarations.append(
            ProbedDeclaration(
                node_id=node_id,
                name=name,
                kind=str(record["kind"]),
                proof=str(record["proof"]),
                hypotheses=_hypotheses(record["hypotheses"], root=name),
            )
        )
    return ProbeReport(declarations=tuple(declarations), unresolved=tuple(unresolved))


def _targets(graph: Graph, node_ids: tuple[str, ...] | None) -> list[tuple[str, tuple[str, ...]]]:
    """The articles with ``lean:`` targets, in id order, narrowed to ``node_ids``."""

    targets: list[tuple[str, tuple[str, ...]]] = []
    issues: list[str] = []
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        if not node.lean:
            continue
        names = tuple(declaration_names(node.lean))
        if not names:
            issues.append(f"{node_id}: lean target list contains no declarations")
        elif len(names) != len(set(names)):
            duplicates = sorted({name for name in names if names.count(name) > 1})
            issues.append(f"{node_id}: duplicate Lean declaration target(s): {', '.join(duplicates)}")
        else:
            targets.append((node_id, names))
    if issues:
        raise ProbeError(issues)
    if node_ids is None:
        return targets
    if not node_ids:
        raise ProbeError(["no articles selected"])
    wanted = set(node_ids)
    if len(wanted) != len(node_ids):
        raise ProbeError(["duplicate article selection"])
    unknown = sorted(wanted - set(graph.nodes))
    if unknown:
        raise ProbeError([f"unknown article: {node_id}" for node_id in unknown])
    untargeted = sorted(wanted - {node_id for node_id, _ in targets})
    if untargeted:
        raise ProbeError([f"{node_id}: article has no Lean declaration targets" for node_id in untargeted])
    return [(node_id, names) for node_id, names in targets if node_id in wanted]


def _probe_template() -> str:
    """The Lean probe source, kept under probes/ beside this module."""

    return (Path(__file__).parent / "probes" / "statement_probe.lean").read_text(encoding="utf-8")


def render_probe(*, imports: tuple[str, ...], roots: tuple[str, ...]) -> str:
    """Render the Lean program that probes every root."""

    if not roots:
        raise ProbeError(["refusing to render a probe with no declarations"])
    if not imports:
        raise ProbeError(["refusing to render a probe with no imports"])
    return _probe_template().format(
        imports="\n".join(f"import {module}" for module in sorted(set(imports))),
        marker=PROBE_MARKER,
        output_env=PROBE_OUTPUT_ENV,
        roots=", ".join(f"({json.dumps(name, ensure_ascii=False)}, {_lean_name(name)})" for name in roots),
    )


def parse_probe_output(output: str, *, expected_roots: tuple[str, ...]) -> dict[str, dict[str, object]]:
    """Strictly read one record per requested root out of the probe's output."""

    expected = set(expected_roots)
    records: dict[str, dict[str, object]] = {}
    for line in output.splitlines():
        if not line.startswith(PROBE_MARKER):
            continue
        try:
            record = json.loads(line[len(PROBE_MARKER) :])
        except json.JSONDecodeError as exc:
            raise ProbeError([f"the statement probe wrote a line that is not JSON: {exc}"]) from exc
        if not isinstance(record, dict) or not isinstance(record.get("root"), str):
            raise ProbeError(["the statement probe wrote a record without a root name"])
        root = record["root"]
        if root not in expected:
            raise ProbeError([f"the statement probe answered for an unrequested declaration: {root}"])
        if root in records:
            raise ProbeError([f"the statement probe answered twice for {root}"])
        found = record.get("found")
        if type(found) is not bool:
            raise ProbeError([f"the statement probe wrote a non-boolean found field for {root}"])
        if record.keys() != ({"found", "hypotheses", "kind", "proof", "root"} if found else {"found", "root"}):
            raise ProbeError([f"the statement probe wrote invalid fields for {root}"])
        if found:
            kind, proof = record["kind"], record["proof"]
            if kind not in _KINDS or proof not in _PROOFS:
                raise ProbeError([f"the statement probe wrote an unknown kind or proof state for {root}"])
            if (proof == "none") != (kind != "theorem"):
                raise ProbeError([f"the statement probe read a proof of {root} exactly when it is not a theorem"])
            if _hypotheses(record["hypotheses"], root=root) and proof != "proved":
                raise ProbeError([f"the statement probe listed hypotheses of {root} without reading its proof"])
        records[root] = record
    missing = [root for root in expected_roots if root not in records]
    if missing:
        raise ProbeError([f"the statement probe gave no answer for {missing[0]}"])
    return records


def _hypotheses(value: object, *, root: str) -> tuple[Hypothesis, ...]:
    """Strictly read the hypotheses listed for one theorem."""

    if not isinstance(value, list):
        raise ProbeError([f"malformed hypotheses for {root}"])
    hypotheses: list[Hypothesis] = []
    for item in value:
        if not isinstance(item, dict) or item.keys() != {"name", "type", "used"}:
            raise ProbeError([f"malformed hypothesis entry for {root}"])
        name, type_, used = item["name"], item["type"], item["used"]
        if not isinstance(name, str) or not name or not isinstance(type_, str) or not type_ or type(used) is not bool:
            raise ProbeError([f"malformed hypothesis entry for {root}"])
        hypotheses.append(Hypothesis(name=name, type=type_, used=used))
    return tuple(hypotheses)


def format_probe_report(report: ProbeReport) -> str:
    """The report for a person: one line per declaration, then findings and gaps."""

    lines: list[str] = []
    current = None
    for item in report.declarations:
        if item.node_id != current:
            lines.append(item.node_id)
            current = item.node_id
        if item.proof == "sorry":
            summary = ": the proof rests on sorry, so it was not read"
        elif item.hypotheses:
            unused = sum(not hypothesis.used for hypothesis in item.hypotheses)
            summary = f": {unused} of {len(item.hypotheses)} hypotheses unused"
        else:
            summary = ""
        lines.append(f"  {item.name} ({item.kind}){summary}")
    lines += [f"finding: {finding}" for finding in report.findings]
    lines += [f"error: {item.message}" for item in report.unresolved]
    return "\n".join(lines) + "\n" if lines else "no Lean declarations to probe\n"


__all__ = [
    "PROBE_MARKER",
    "REPORT_SCHEMA",
    "Hypothesis",
    "ProbeError",
    "ProbeReport",
    "ProbedDeclaration",
    "UnprobedTarget",
    "format_probe_report",
    "parse_probe_output",
    "probe_statements",
    "render_probe",
]
