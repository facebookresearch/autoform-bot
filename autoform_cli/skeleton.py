"""Extract the trusted surface of each formalized result: its skeleton.

A theorem means what its *statement* means. To agree that a Lean declaration
says what the blueprint claims, a reader has to read the statement and every
definition the statement rests on, transitively -- and nothing else. Proofs are
the kernel's problem. The skeleton is exactly that reading list: a few lines a
person is expected to check, above a proof that may be orders of magnitude
longer and is checked by the kernel instead.

The closure is computed from elaborated terms, never from source text. A lexical
pass cannot see through ``open``, notation, implicit instances, or auto-bound
variables, and every miss silently shrinks the surface a reader is told to
trust. So the module writes a small Lean program, runs it with ``lake env
lean`` against the built project, and reads back one JSON line per declaration.
Only the *type* of a theorem is entered; the type and the *body* of a
definition are, because a definition's body is part of its meaning. Constants
outside the project are the trusted base and are listed by name rather than
expanded, so a reader sees that a statement uses Mathlib's notion rather than a
homemade one.

Generated companions -- constructors, projections, recursors, ``noConfusion``
helpers, matchers -- are folded onto the declaration the reader sees in the
source, so a structure appears once, as the ``structure`` block, rather than as
five auto-generated names. Only companions that Lean's environment records as
generated are folded; name spelling such as ``f.eq_1`` is not evidence.

The output is deterministic and path-free like every other Autoform report: the
same sources produce the same JSON, and nothing here writes into the vault.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import psutil

from .graph import Graph, GraphValidationError, Node, load_graph
from .lean import (
    MANAGED_OUTPUT_SCHEMAS,
    PACKET_SCHEMA,
    PASSAGE_SCHEMA,
    SourceIndex,
    declaration_names,
    index_project,
)

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

SKELETON_SCHEMA = "autoform-skeleton/v4"
SEMANTIC_SCHEMA = "autoform-lean-expr/v4"

#: Every line the probe wants read back starts with this marker, so Lean's own
#: informational output can never be mistaken for a result.
PROBE_MARKER = "AUTOFORM_SKELETON "
# Names the file the probe writes its records to; see run_probe.
PROBE_OUTPUT_ENV = "AUTOFORM_SKELETON_OUTPUT"

#: Module roots whose declarations are never listed as assumptions: they are
#: the language itself, not mathematics a reader might want to double-check.
#: The probe counts a module as core only when its `.olean` also resolves under
#: the toolchain's own `lib/lean`; a dependency module named `Lake.Foo` is
#: external and bound like any other.
_CORE_MODULE_ROOTS = ("Init", "Lean", "Std", "Lake")

#: Lean resolves a module by its root directory on the search path, so a
#: dependency library rooted at `Std` hides the toolchain's `Std` from the probe.
_SHADOWED_CORE_MODULE = re.compile(
    r"object file '[^']*' of module ((?:" + "|".join(_CORE_MODULE_ROOTS) + r")(?:\.\S+)?) does not exist"
)

DEFAULT_PROBE_TIMEOUT = 600.0
#: The Lake freshness check hashes every imported input, which on a Mathlib
#: project can take minutes on its own, so it does not share the probe's budget.
DEFAULT_FRESHNESS_TIMEOUT = 600.0
DEFAULT_PROBE_OUTPUT_LIMIT = 64 * 1024 * 1024
#: The probe states shared subterms once; this bounds the characters of
#: semantic material they may expand to in one run.
_PROBE_MATERIAL_LIMIT = 512 * 1024 * 1024
_PROCESS_TERMINATION_GRACE = 2.0
#: Lake's exit status when ``--no-build`` finds a target that needs rebuilding.
_LAKE_NO_BUILD_EXIT = 3
_PROCESS_TOKEN_ENV = "_AUTOFORM_PROCESS_TOKEN"
_SNAPSHOT_FILE_LIMIT = 64 * 1024 * 1024
_PROJECT_CONTROL_FILES = (
    "lakefile.toml",
    "lakefile.lean",
    "lean-toolchain",
    "lake-manifest.json",
)
#: Compiled parts that identify a boundary module. Module-system builds also
#: write the server and private parts next to the `.olean`.
_MODULE_FILE_KINDS = ("olean", "olean.server", "olean.private")

#: A callable that runs a probe and returns Lean's standard output. The default
#: shells out to ``lake env lean``; tests substitute a fake.
ProbeRunner = Callable[[str, Path], str]


class SkeletonError(RuntimeError):
    """A skeleton could not be extracted from the project."""

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


class _CommandTimedOut(SkeletonError):
    """A bounded command ran out of time; its caller knows which budget to raise."""


#: Shown when no source can be attributed and parsed safely. The signatures
#: and canonical kernel material above it still state the meaning.
_NOT_SHOWN = "-- source not shown: no reliable standalone source is available"


@dataclass(frozen=True, slots=True)
class TrustedDeclaration:
    """One project declaration a reader must agree with."""

    name: str
    kind: str
    module: str
    path: str | None
    start_line: int | None
    end_line: int | None
    signature: str
    #: The raw signature, so project printers cannot disguise a trusted theorem.
    raw_signature: str
    semantic: str
    depends: tuple[str, ...]
    source: str | None = None
    #: UTF-8 byte ranges of the comments in ``source``, as Lean's parser found them.
    source_comments: tuple[tuple[int, int], ...] = ()
    #: True when no source can be attributed and parsed safely.
    source_withheld: bool = False

    @property
    def lines(self) -> int:
        text = self.source if self.source is not None else self.signature
        return text.count("\n") + 1 if text else 0

    def as_dict(self) -> dict[str, object]:
        return {
            "depends": list(self.depends),
            "end_line": self.end_line,
            "kind": self.kind,
            "module": self.module,
            "name": self.name,
            "path": self.path,
            "raw_signature": self.raw_signature,
            "signature": self.signature,
            "semantic": self.semantic,
            "source": self.source,
            "source_comments": [list(item) for item in self.source_comments],
            "source_withheld": self.source_withheld,
            "start_line": self.start_line,
        }


@dataclass(frozen=True, slots=True)
class DeclarationSkeleton:
    """The trusted surface of one root declaration."""

    name: str
    kind: str
    module: str
    path: str | None
    start_line: int | None
    end_line: int | None
    signature: str
    #: The raw signature, so project printers cannot make ``HMul.hMul a b``
    #: read as ``a + b``.
    raw_signature: str
    semantic: str
    lean_version: str
    depends: tuple[str, ...]
    trusted: tuple[TrustedDeclaration, ...]
    assumed: tuple[str, ...]
    assumed_semantics: tuple[tuple[str, str], ...]
    boundary_modules: tuple[tuple[str, str, str], ...]
    axioms: tuple[str, ...]
    axiom_semantics: tuple[tuple[str, str], ...]
    #: The declaration's own source when it is a definition, whose body is its
    #: meaning. A theorem's source holds its proof, which is never shown.
    source: str | None = None
    #: The statement as written in the source, cut before its value. The
    #: elaborated signature is authoritative; this is what the author typed,
    #: shown beside it so neither form can hide what the other shows.
    statement: str | None = None
    #: UTF-8 byte ranges of the comments in ``source`` and ``statement``.
    source_comments: tuple[tuple[int, int], ...] = ()
    statement_comments: tuple[tuple[int, int], ...] = ()
    #: True when the declaration's source or written statement cannot safely
    #: be shown, as for ``TrustedDeclaration.source_withheld``.
    source_withheld: bool = False

    @property
    def defines(self) -> bool:
        """Whether the root is a definition rather than a proposition."""

        return self.kind not in {"theorem", "axiom"}

    @property
    def declaration_lines(self) -> int:
        """Source lines of the root declaration itself, proof included."""
        if self.start_line is None or self.end_line is None:
            return 0
        return self.end_line - self.start_line + 1

    @property
    def skeleton_lines(self) -> int:
        """Lines a reader has to read: the signature plus every trusted span."""
        own = self.source if self.source is not None else self.signature
        head = own.count("\n") + 1 if own else 0
        return head + sum(item.lines for item in self.trusted)

    @property
    def hash(self) -> str:
        """Fingerprint the probe's canonical elaborated meaning and trust boundary.

        An approval or a read-back is testimony about one skeleton and records
        this hash, so the audit can tell when the meaning has moved from under
        the testimony; the evidence hash tells when the text shown has.
        """

        material = {
            "axioms": list(self.axioms),
            "axiom_semantics": [list(item) for item in self.axiom_semantics],
            "assumed": list(self.assumed),
            "assumed_semantics": [list(item) for item in self.assumed_semantics],
            "boundary_modules": [list(item) for item in self.boundary_modules],
            "depends": list(self.depends),
            "kind": self.kind,
            "lean_version": self.lean_version,
            "name": self.name,
            "semantic": self.semantic,
            "semantic_schema": SEMANTIC_SCHEMA,
            "trusted": [
                [item.name, item.kind, item.semantic, list(item.depends)]
                for item in self.trusted
            ],
        }
        return _sha256_id(json.dumps(material, sort_keys=True, ensure_ascii=False).encode())

    @property
    def evidence_hash(self) -> str:
        """Fingerprint the exact proof-free text presented to a reviewer."""

        return _sha256_id(self.blind_text().encode("utf-8"))

    def blind_text(self) -> str:
        """The skeleton with every comment removed, for an auditor who must not see intent.

        A read-back is only evidence if its author did not know what the code
        was meant to say. Docstrings say exactly that, so they are stripped
        along with every other comment. Names stay: they are part of the code.
        """

        lines = [
            f"-- {self.kind} {self.name}",
            f"-- assumed from libraries: {', '.join(self.assumed) if self.assumed else 'none'}",
            f"-- axioms: {', '.join(self.axioms) if self.axioms else 'none'}",
            "",
            self.signature,
            "-- raw signature:",
            self.raw_signature,
            f"-- canonical kernel material: {self.semantic}",
        ]
        own = _without_comments(self.source or "", self.source_comments).strip("\n")
        if own:
            lines.append(own)
        elif self.statement:
            written = _without_comments(self.statement, self.statement_comments).strip("\n")
            if written:
                lines += ["-- as written:", written]
        if self.source_withheld or not (own or self.statement):
            lines.append(_NOT_SHOWN)
        for item in self.trusted:
            body = _without_comments(item.source or "", item.source_comments).strip("\n")
            # The elaborated signature restores what `variable` binders and
            # `open` leave implicit in the source, such as the type of `S`.
            lines += [
                "",
                f"-- {item.kind} {item.name}",
                f"-- signature: {item.signature}",
                f"-- raw signature: {item.raw_signature}",
                f"-- canonical kernel material: {item.semantic}",
            ]
            if body:
                lines.append(body)
            elif item.source_withheld:
                lines.append(_NOT_SHOWN)
        return "\n".join(lines) + "\n"

    def as_dict(self) -> dict[str, object]:
        """The report record, naming what the report's shared tables state once."""

        return {
            "assumed": list(self.assumed),
            "boundary_modules": list(dict.fromkeys(module for module, _, _ in self.boundary_modules)),
            "axioms": list(self.axioms),
            "declaration_lines": self.declaration_lines,
            "end_line": self.end_line,
            "evidence_hash": self.evidence_hash,
            "hash": self.hash,
            "kind": self.kind,
            "lean_version": self.lean_version,
            "module": self.module,
            "name": self.name,
            "raw_signature": self.raw_signature,
            "path": self.path,
            "signature": self.signature,
            "semantic": self.semantic,
            "depends": list(self.depends),
            "skeleton_lines": self.skeleton_lines,
            "source": self.source,
            "source_comments": [list(item) for item in self.source_comments],
            "source_withheld": self.source_withheld,
            "start_line": self.start_line,
            "statement": self.statement,
            "statement_comments": [list(item) for item in self.statement_comments],
            "trusted": [item.name for item in self.trusted],
        }


@dataclass(frozen=True, slots=True)
class NodeSkeleton:
    """The skeletons behind one blueprint article."""

    node_id: str
    article_path: str
    declarations: tuple[DeclarationSkeleton, ...]
    #: The source passage the article cites through a line locator, if any.
    #: This is the reference a faithfulness judge compares against.
    passage: str | None = None
    passage_locator: str | None = None
    #: False when one of the article's declarations is unresolved; the
    #: article then has no hash, since one over the rest would miss changes
    #: to the missing declaration.
    complete: bool = True

    def blind_text(self) -> str:
        """The article's declarations as one blind packet, for a faithfulness judge.

        A source theorem is often formalized by several declarations together,
        an existence half and a uniqueness half, say. Judged one at a time each
        is honestly incomplete; judged together they are the statement. The
        read-back auditor still gets one declaration at a time, since a
        read-back is testimony about one declaration.
        """

        parts = [f"-- article with {len(self.declarations)} declaration(s)"]
        parts += [declaration.blind_text() for declaration in self.declarations]
        return "\n".join(parts)

    @property
    def hash(self) -> str | None:
        """One hash over all of an article's skeletons, printed as the article skeleton."""

        if not self.complete:
            return None
        if len(self.declarations) == 1:
            return self.declarations[0].hash
        joined = "\n".join(sorted(item.hash for item in self.declarations))
        return _sha256_id(joined.encode())

    @property
    def evidence_hash(self) -> str:
        """Fingerprint the exact joint packet presented to a reviewer."""

        return _sha256_id(self.blind_text().encode("utf-8"))

    @property
    def review_hash(self) -> str | None:
        """Fingerprint the joint packet, its cited source passage, and its meaning.

        The meaning hash is bound too: a packet can read the same across a change
        of meaning, and a review recorded against this hash must not survive one.
        """

        if self.hash is None:
            return None
        material = {
            "hash": self.hash,
            "packet": self.evidence_hash,
            "passage": self.passage,
            "passage_locator": self.passage_locator,
        }
        return _sha256_id(json.dumps(material, sort_keys=True, ensure_ascii=False).encode())

    def as_dict(self) -> dict[str, object]:
        return {
            "article_path": self.article_path,
            "declarations": [item.as_dict() for item in self.declarations],
            "evidence_hash": self.evidence_hash,
            "hash": self.hash,
            "node_id": self.node_id,
            "passage": self.passage,
            "passage_locator": self.passage_locator,
            "review_hash": self.review_hash,
        }


@dataclass(frozen=True, slots=True)
class UnresolvedTarget:
    """One selected blueprint declaration for which no skeleton was produced."""

    node_id: str
    declaration: str
    reason: str

    @property
    def message(self) -> str:
        return f"{self.node_id}: {self.declaration}: {self.reason}"

    def as_dict(self) -> dict[str, str]:
        return {
            "declaration": self.declaration,
            "node_id": self.node_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SkeletonReport:
    """Every skeleton the blueprint names, in article order."""

    blueprint_hash: str
    targets: tuple[tuple[str, tuple[str, ...]], ...]
    selection: str
    selected_nodes: tuple[str, ...]
    nodes: tuple[NodeSkeleton, ...]
    unresolved: tuple[UnresolvedTarget, ...]
    schema: str = SKELETON_SCHEMA
    semantic_schema: str = SEMANTIC_SCHEMA

    @property
    def clean(self) -> bool:
        expected = {
            (node_id, declaration)
            for node_id, declarations in self.targets
            if node_id in self.selected_nodes
            for declaration in declarations
        }
        actual = {
            (node.node_id, declaration.name)
            for node in self.nodes
            for declaration in node.declarations
        }
        return not self.unresolved and actual == expected

    def node(self, node_id: str) -> NodeSkeleton | None:
        """Return the skeleton record of one article, if it has one."""

        return next((node for node in self.nodes if node.node_id == node_id), None)

    def declarations(self, node_id: str) -> tuple[DeclarationSkeleton, ...]:
        """Return the skeletons behind one article, or none."""

        node = self.node(node_id)
        return () if node is None else node.declarations

    def as_dict(self) -> dict[str, object]:
        # Roots in one project share most of what they trust; each shared
        # item is stated once here and named by every declaration that uses it.
        trusted: dict[str, object] = {}
        semantics: dict[str, object] = {}
        modules: dict[str, object] = {}
        for node in self.nodes:
            for declaration in node.declarations:
                for item in declaration.trusted:
                    _share(trusted, item.name, item.as_dict(), table_name="trusted declaration")
                for name, semantic in (*declaration.assumed_semantics, *declaration.axiom_semantics):
                    _share(semantics, name, semantic, table_name="semantic material")
                files: dict[str, list[list[str]]] = {}
                for module, kind, digest in declaration.boundary_modules:
                    files.setdefault(module, []).append([kind, digest])
                for module, identity in files.items():
                    _share(modules, module, identity, table_name="module identity")
        return {
            "blueprint_hash": self.blueprint_hash,
            "boundary_modules": modules,
            "nodes": [node.as_dict() for node in self.nodes],
            "schema": self.schema,
            "selection": {
                "mode": self.selection,
                "node_count": len(self.selected_nodes),
                "nodes": list(self.selected_nodes),
            },
            "semantic_schema": self.semantic_schema,
            "semantics": semantics,
            "target_count": len(self.targets),
            "targets": [
                {"declarations": list(declarations), "node_id": node_id}
                for node_id, declarations in self.targets
            ],
            "trusted": trusted,
            "unresolved": [issue.as_dict() for issue in self.unresolved],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _share(table: dict[str, object], name: str, value: object, *, table_name: str) -> None:
    if table.setdefault(name, value) != value:
        raise ValueError(f"conflicting {table_name} for {name} in one skeleton report")


def load_skeleton_report(path: str | Path) -> SkeletonReport:
    """Read a report written by :meth:`SkeletonReport.to_json` back into memory."""

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkeletonError([f"cannot read skeleton report {path}: {exc}"]) from exc
    if (
        not isinstance(data, dict)
        or data.get("schema") != SKELETON_SCHEMA
        or data.get("semantic_schema") != SEMANTIC_SCHEMA
        or data.keys()
        != {
            "blueprint_hash",
            "boundary_modules",
            "nodes",
            "schema",
            "selection",
            "semantic_schema",
            "semantics",
            "target_count",
            "targets",
            "trusted",
            "unresolved",
        }
    ):
        raise SkeletonError([f"{path} is not an {SKELETON_SCHEMA} report"])
    blueprint_hash = data["blueprint_hash"]
    if not isinstance(blueprint_hash, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", blueprint_hash
    ):
        raise SkeletonError([f"{path} contains an invalid blueprint hash"])
    targets = _report_targets(data["targets"])
    if type(data["target_count"]) is not int or data["target_count"] != len(targets):
        raise SkeletonError([f"{path} contains an invalid target count"])
    selection = data["selection"]
    if not isinstance(selection, dict) or selection.keys() != {"mode", "node_count", "nodes"}:
        raise SkeletonError([f"{path} contains malformed skeleton selection data"])
    mode = selection["mode"]
    if mode not in {"all", "filtered"}:
        raise SkeletonError([f"{path} contains an invalid skeleton selection mode"])
    selected_nodes = _report_string_tuple(selection["nodes"], "selected articles")
    if type(selection["node_count"]) is not int or selection["node_count"] != len(
        selected_nodes
    ):
        raise SkeletonError([f"{path} contains an invalid selected article count"])
    target_ids = tuple(node_id for node_id, _ in targets)
    if tuple(sorted(selected_nodes)) != selected_nodes or not set(selected_nodes) <= set(target_ids):
        raise SkeletonError([f"{path} contains an invalid skeleton article selection"])
    if mode == "all" and selected_nodes != target_ids:
        raise SkeletonError([f"{path} contains an incomplete all-article selection"])
    if mode == "filtered" and not selected_nodes:
        raise SkeletonError([f"{path} contains an empty filtered article selection"])
    raw_nodes = data["nodes"]
    unresolved = _report_unresolved(data["unresolved"])
    if not isinstance(raw_nodes, list):
        raise SkeletonError([f"{path} contains malformed skeleton report data"])
    declarations_by_node = dict(targets)
    tables = _report_tables(data)
    used: dict[str, set[str]] = {table: set() for table in tables}
    nodes = tuple(
        _node_from_dict(node, targets=declarations_by_node, tables=tables, used=used) for node in raw_nodes
    )
    if any(used[table] != tables[table].keys() for table in tables):
        raise SkeletonError([f"{path} contains unreferenced shared entries"])
    if len({node.node_id for node in nodes}) != len(nodes):
        raise SkeletonError([f"{path} contains duplicate skeleton article ids"])
    if tuple(node.node_id for node in nodes) != selected_nodes:
        raise SkeletonError([f"{path} does not contain exactly its selected articles"])
    actual_targets: set[tuple[str, str]] = set()
    for node in nodes:
        expected = declarations_by_node[node.node_id]
        actual = tuple(declaration.name for declaration in node.declarations)
        if not set(actual) <= set(expected):
            raise SkeletonError([f"{path} contains an untargeted declaration for {node.node_id}"])
        actual_targets.update((node.node_id, declaration) for declaration in actual)
    expected_targets = {
        (node_id, declaration)
        for node_id, declarations in targets
        if node_id in selected_nodes
        for declaration in declarations
    }
    unresolved_targets = {(issue.node_id, issue.declaration) for issue in unresolved}
    if unresolved_targets != expected_targets - actual_targets:
        raise SkeletonError([f"{path} contains mismatched unresolved declarations"])
    report = SkeletonReport(
        blueprint_hash=blueprint_hash,
        targets=targets,
        selection=mode,
        selected_nodes=selected_nodes,
        nodes=nodes,
        unresolved=unresolved,
    )
    return report


def _report_targets(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, list):
        raise SkeletonError(["malformed targets in skeleton report"])
    targets: list[tuple[str, tuple[str, ...]]] = []
    for item in value:
        if not isinstance(item, dict) or item.keys() != {"declarations", "node_id"}:
            raise SkeletonError(["malformed target in skeleton report"])
        node_id = _report_string(item["node_id"], "target article id")
        declarations = _report_string_tuple(
            item["declarations"], f"target declarations for {node_id}"
        )
        if not declarations:
            raise SkeletonError([f"empty target declarations for {node_id} in skeleton report"])
        targets.append((node_id, declarations))
    target_ids = tuple(node_id for node_id, _ in targets)
    if tuple(sorted(target_ids)) != target_ids or len(set(target_ids)) != len(target_ids):
        raise SkeletonError(["invalid target article order in skeleton report"])
    return tuple(targets)


def _report_unresolved(value: object) -> tuple[UnresolvedTarget, ...]:
    if not isinstance(value, list):
        raise SkeletonError(["malformed unresolved declarations in skeleton report"])
    unresolved: list[UnresolvedTarget] = []
    for item in value:
        if not isinstance(item, dict) or item.keys() != {"declaration", "node_id", "reason"}:
            raise SkeletonError(["malformed unresolved declaration in skeleton report"])
        unresolved.append(
            UnresolvedTarget(
                node_id=_report_string(item["node_id"], "unresolved article id"),
                declaration=_report_string(item["declaration"], "unresolved declaration"),
                reason=_report_string(item["reason"], "unresolved reason"),
            )
        )
    keys = tuple((issue.node_id, issue.declaration) for issue in unresolved)
    if len(set(keys)) != len(keys) or tuple(sorted(keys)) != keys:
        raise SkeletonError(["invalid unresolved declaration order in skeleton report"])
    return tuple(unresolved)


_NODE_REPORT_FIELDS = frozenset(
    {
        "article_path",
        "declarations",
        "evidence_hash",
        "hash",
        "node_id",
        "passage",
        "passage_locator",
        "review_hash",
    }
)
_DECLARATION_REPORT_FIELDS = frozenset(
    {
        "assumed",
        "boundary_modules",
        "axioms",
        "declaration_lines",
        "depends",
        "end_line",
        "evidence_hash",
        "hash",
        "kind",
        "lean_version",
        "module",
        "name",
        "raw_signature",
        "path",
        "semantic",
        "signature",
        "skeleton_lines",
        "source",
        "source_comments",
        "source_withheld",
        "start_line",
        "statement",
        "statement_comments",
        "trusted",
    }
)
_TRUSTED_REPORT_FIELDS = frozenset(
    {
        "depends",
        "end_line",
        "kind",
        "module",
        "name",
        "path",
        "raw_signature",
        "semantic",
        "signature",
        "source",
        "source_comments",
        "source_withheld",
        "start_line",
    }
)


def _report_tables(data: dict[str, object]) -> dict[str, dict[str, object]]:
    """Read the shared tables that report declarations refer to by name."""

    tables: dict[str, dict[str, object]] = {}
    for table in ("boundary_modules", "semantics", "trusted"):
        value = data[table]
        if not isinstance(value, dict):
            raise SkeletonError([f"malformed shared {table} table in skeleton report"])
        tables[table] = value
    tables["trusted"] = {
        name: _trusted_from_dict(item, root=name) for name, item in tables["trusted"].items()
    }
    for name, trusted in tables["trusted"].items():
        if not isinstance(trusted, TrustedDeclaration) or trusted.name != name:
            raise SkeletonError([f"mismatched shared trusted declaration {name} in skeleton report"])
    for name, semantic in tables["semantics"].items():
        _validate_semantic_material(_report_string(semantic, f"semantic material for {name}"), context=name)
    tables["boundary_modules"] = {
        module: _report_module_identities(
            [[module, *file] if isinstance(file, list) else file for file in files]
            if isinstance(files, list)
            else files,
            context=module,
        )
        for module, files in tables["boundary_modules"].items()
    }
    if not all(tables["boundary_modules"].values()):
        raise SkeletonError(["empty shared module identity in skeleton report"])
    return tables


def _node_from_dict(
    item: object,
    *,
    targets: dict[str, tuple[str, ...]],
    tables: dict[str, dict[str, object]],
    used: dict[str, set[str]],
) -> NodeSkeleton:
    if not isinstance(item, dict) or item.keys() != _NODE_REPORT_FIELDS:
        raise SkeletonError(["malformed article in skeleton report"])
    node_id = _report_string(item.get("node_id"), "article id")
    article_path = _report_string(item.get("article_path"), f"article path for {node_id}")
    raw_declarations = item.get("declarations")
    if not isinstance(raw_declarations, list):
        raise SkeletonError([f"malformed declarations for {node_id} in skeleton report"])
    declarations = tuple(_declaration_from_dict(value, tables=tables, used=used) for value in raw_declarations)
    if len({declaration.name for declaration in declarations}) != len(declarations):
        raise SkeletonError([f"duplicate declarations for {node_id} in skeleton report"])
    passage = _report_optional_string(item.get("passage"), f"passage for {node_id}")
    locator = _report_optional_string(item.get("passage_locator"), f"passage locator for {node_id}")
    if (passage is None) != (locator is None):
        raise SkeletonError([f"mismatched passage fields for {node_id} in skeleton report"])
    node = NodeSkeleton(
        node_id=node_id,
        article_path=article_path,
        declarations=declarations,
        passage=passage,
        passage_locator=locator,
        complete={declaration.name for declaration in declarations} == set(targets.get(node_id, ())),
    )
    if item.get("hash") != node.hash:
        raise SkeletonError([f"invalid article hash for {node_id} in skeleton report"])
    if item.get("evidence_hash") != node.evidence_hash:
        raise SkeletonError([f"invalid article evidence hash for {node_id} in skeleton report"])
    if item.get("review_hash") != node.review_hash:
        raise SkeletonError([f"invalid article review hash for {node_id} in skeleton report"])
    return node


def _declaration_from_dict(
    item: object, *, tables: dict[str, dict[str, object]], used: dict[str, set[str]]
) -> DeclarationSkeleton:
    if not isinstance(item, dict) or item.keys() != _DECLARATION_REPORT_FIELDS:
        raise SkeletonError(["malformed declaration in skeleton report"])
    name = _report_string(item.get("name"), "declaration name")
    kind = _report_kind(item.get("kind"), name)
    semantic = _report_string(item.get("semantic"), f"semantic material for {name}")
    _validate_semantic_material(semantic, context=name, kind=kind)

    def shared(field: str, table: str, what: str) -> list[tuple[str, object]]:
        names = _report_string_tuple(item.get(field), f"{field} for {name}")
        if not set(names) <= tables[table].keys():
            raise SkeletonError([f"mismatched {what} for {name}"])
        used[table].update(names)
        return [(key, tables[table][key]) for key in names]

    assumed_semantics = tuple((key, str(value)) for key, value in shared("assumed", "semantics", "assumption semantics"))
    assumed = tuple(key for key, _ in assumed_semantics)
    axiom_semantics = tuple((key, str(value)) for key, value in shared("axioms", "semantics", "axiom semantics"))
    axioms = tuple(key for key, _ in axiom_semantics)
    boundary_modules = tuple(
        entry
        for _, entries in shared("boundary_modules", "boundary_modules", "boundary module identities")
        if isinstance(entries, tuple)
        for entry in entries
    )
    if assumed and not boundary_modules:
        raise SkeletonError([f"missing boundary module identities for {name}"])
    trusted = tuple(
        value for _, value in shared("trusted", "trusted", "trusted declarations") if isinstance(value, TrustedDeclaration)
    )
    source = _report_optional_string(item.get("source"), f"source for {name}")
    if kind in {"theorem", "axiom"} and source is not None:
        raise SkeletonError([f"proof-bearing source is forbidden for {kind} {name}"])
    source_withheld = _report_withheld(item.get("source_withheld"), source, name)
    start_line = _report_optional_int(item.get("start_line"), f"start line for {name}")
    entries = [(name, kind, source, start_line, source_withheld)]
    entries += [(value.name, value.kind, value.source, value.start_line, value.source_withheld) for value in trusted]
    for entry_name, entry_kind, entry_source, entry_start, withheld in entries:
        missing_required = _source_required(
            entry_name, entry_kind, entry_source, entry_start is not None
        )
        if missing_required and not (withheld and entry_start is not None):
            raise SkeletonError([f"required source is missing for {entry_kind} {entry_name}"])
    statement = _report_optional_string(item.get("statement"), f"statement for {name}")
    declaration = DeclarationSkeleton(
        name=name,
        kind=kind,
        module=_report_string(item.get("module"), f"module for {name}"),
        path=_report_optional_string(item.get("path"), f"path for {name}"),
        start_line=start_line,
        end_line=_report_optional_int(item.get("end_line"), f"end line for {name}"),
        signature=_report_string(item.get("signature"), f"signature for {name}"),
        raw_signature=_report_string(item.get("raw_signature"), f"raw signature for {name}"),
        semantic=semantic,
        lean_version=_report_string(item.get("lean_version"), f"Lean version for {name}"),
        depends=_report_string_tuple(item.get("depends"), f"dependencies for {name}"),
        trusted=trusted,
        assumed=assumed,
        assumed_semantics=assumed_semantics,
        boundary_modules=boundary_modules,
        axioms=axioms,
        axiom_semantics=axiom_semantics,
        source=source,
        statement=statement,
        source_comments=_comment_ranges(item.get("source_comments"), source, context=f"the source of {name}"),
        statement_comments=_comment_ranges(
            item.get("statement_comments"), statement, context=f"the statement of {name}"
        ),
        source_withheld=source_withheld,
    )
    _validate_report_ranges(declaration.start_line, declaration.end_line, context=name)
    if item.get("hash") != declaration.hash:
        raise SkeletonError([f"invalid declaration hash for {name} in skeleton report"])
    if item.get("evidence_hash") != declaration.evidence_hash:
        raise SkeletonError([f"invalid declaration evidence hash for {name} in skeleton report"])
    if item.get("declaration_lines") != declaration.declaration_lines:
        raise SkeletonError([f"invalid declaration line count for {name} in skeleton report"])
    if item.get("skeleton_lines") != declaration.skeleton_lines:
        raise SkeletonError([f"invalid skeleton line count for {name} in skeleton report"])
    return declaration


def _trusted_from_dict(item: object, *, root: str) -> TrustedDeclaration:
    if not isinstance(item, dict) or item.keys() != _TRUSTED_REPORT_FIELDS:
        raise SkeletonError([f"malformed trusted declaration for {root}"])
    name = _report_string(item.get("name"), f"trusted declaration name for {root}")
    kind = _report_kind(item.get("kind"), name)
    semantic = _report_string(item.get("semantic"), f"semantic material for {name}")
    _validate_semantic_material(semantic, context=name, kind=kind)
    source = _report_optional_string(item.get("source"), f"source for {name}")
    if kind in {"theorem", "axiom"} and source is not None:
        raise SkeletonError([f"proof-bearing source is forbidden for {kind} {name}"])
    trusted = TrustedDeclaration(
        name=name,
        kind=kind,
        module=_report_string(item.get("module"), f"module for {name}"),
        path=_report_optional_string(item.get("path"), f"path for {name}"),
        start_line=_report_optional_int(item.get("start_line"), f"start line for {name}"),
        end_line=_report_optional_int(item.get("end_line"), f"end line for {name}"),
        signature=_report_string(item.get("signature"), f"signature for {name}"),
        raw_signature=_report_string(item.get("raw_signature"), f"raw signature for {name}"),
        semantic=semantic,
        depends=_report_string_tuple(item.get("depends"), f"dependencies for {name}"),
        source=source,
        source_comments=_comment_ranges(item.get("source_comments"), source, context=f"the source of {name}"),
        source_withheld=_report_withheld(item.get("source_withheld"), source, name),
    )
    _validate_report_ranges(trusted.start_line, trusted.end_line, context=name)
    return trusted


def _report_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise SkeletonError([f"invalid {context} in skeleton report"])
    return value


def _report_optional_string(value: object, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SkeletonError([f"invalid {context} in skeleton report"])
    return value


def _report_withheld(value: object, source: str | None, name: str) -> bool:
    if type(value) is not bool or (value and source is not None):
        raise SkeletonError([f"invalid withheld source flag for {name} in skeleton report"])
    return value


def _report_optional_int(value: object, context: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise SkeletonError([f"invalid {context} in skeleton report"])
    return value


def _report_string_tuple(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise SkeletonError([f"invalid {context} in skeleton report"])
    if len(value) != len(set(value)):
        raise SkeletonError([f"duplicate {context} in skeleton report"])
    return tuple(value)


def _report_kind(value: object, context: str) -> str:
    if not isinstance(value, str) or value not in _DECLARATION_KINDS:
        raise SkeletonError([f"invalid declaration kind for {context} in skeleton report"])
    return value


def _report_module_identities(value: object, *, context: str) -> tuple[tuple[str, str, str], ...]:
    entries = _module_file_entries(value, context=context)
    for module, file_kind, digest in entries:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise SkeletonError([f"invalid {file_kind} identity for module {module} in skeleton report"])
    return entries


def _validate_report_ranges(start: int | None, end: int | None, *, context: str) -> None:
    if (start is None) != (end is None) or (
        start is not None and end is not None and (start < 1 or end < start)
    ):
        raise SkeletonError([f"invalid source range for {context} in skeleton report"])


def _sha256_id(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _without_comments(text: str, comments: tuple[tuple[int, int], ...]) -> str:
    """Blank the comment ranges Lean found without joining surrounding tokens."""

    data = text.encode("utf-8")
    kept: list[bytes] = []
    cursor = 0
    for start, end in comments:
        kept.append(data[cursor:start])
        removed = data[start:end].decode("utf-8")
        kept.append("".join(character if character in "\r\n\t" else " " for character in removed).encode("utf-8"))
        cursor = end
    kept.append(data[cursor:])
    return "\n".join(line.rstrip() for line in b"".join(kept).decode("utf-8").splitlines() if line.strip())


def _comment_ranges(value: object, text: str | None, *, context: str) -> tuple[tuple[int, int], ...]:
    """Validate comment ranges: sorted, disjoint UTF-8 byte spans that open a comment."""

    if text is None:
        if value != []:
            raise SkeletonError([f"invalid comment ranges for {context}"])
        return ()
    data = text.encode("utf-8")
    ranges: list[tuple[int, int]] = []
    previous = 0
    for item in value if isinstance(value, list) else [None]:
        if not isinstance(item, list) or len(item) != 2 or not all(type(n) is int for n in item):
            raise SkeletonError([f"invalid comment ranges for {context}"])
        start, end = item
        if start < previous or end <= start or end > len(data) or data[start : start + 2] not in {b"--", b"/-"}:
            raise SkeletonError([f"invalid comment ranges for {context}"])
        ranges.append((start, end))
        previous = end
    try:
        _without_comments(text, tuple(ranges))
    except UnicodeDecodeError as exc:
        raise SkeletonError([f"invalid comment ranges for {context}"]) from exc
    return tuple(ranges)


def _read_snapshot_pass(
    path: Path, *, keep_content: bool
) -> tuple[bytes, tuple[int, str]] | None:
    """Read one bounded regular-file pass."""

    try:
        path_metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkeletonError([f"cannot inspect skeleton input {path}: {exc}"]) from exc
    if not stat.S_ISREG(path_metadata.st_mode):
        raise SkeletonError([f"skeleton input is not a regular file: {path}"])
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SkeletonError([f"skeleton input is not a regular file: {path}"]) from exc
        raise SkeletonError([f"cannot open skeleton input {path}: {exc}"]) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SkeletonError([f"skeleton input is not a regular file: {path}"])
        # The file opened must be the one inspected, not whatever replaced it.
        if (metadata.st_dev, metadata.st_ino) != (path_metadata.st_dev, path_metadata.st_ino):
            raise SkeletonError([f"skeleton input changed while it was read: {path}"])
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        size = 0
        while True:
            block = os.read(descriptor, 64 * 1024)
            if not block:
                break
            size += len(block)
            if size > _SNAPSHOT_FILE_LIMIT:
                raise SkeletonError(
                    [f"skeleton input exceeds the {_SNAPSHOT_FILE_LIMIT}-byte limit: {path}"]
                )
            digest.update(block)
            if keep_content:
                chunks.append(block)
    except OSError as exc:
        raise SkeletonError([f"cannot read skeleton input {path}: {exc}"]) from exc
    finally:
        os.close(descriptor)
    return b"".join(chunks), (size, digest.hexdigest())


def _read_snapshot_file(path: Path) -> tuple[bytes, tuple[int, str]] | None:
    """Read a bounded regular file twice to reject concurrent content changes."""

    captured = _read_snapshot_pass(path, keep_content=True)
    verified = _read_snapshot_pass(path, keep_content=False)
    captured_fingerprint = None if captured is None else captured[1]
    verified_fingerprint = None if verified is None else verified[1]
    if captured_fingerprint != verified_fingerprint:
        raise SkeletonError([f"skeleton input changed while it was read: {path}"])
    return captured


def _snapshot_regular_file(path: Path) -> tuple[int, str] | None:
    captured = _read_snapshot_file(path)
    return None if captured is None else captured[1]


def _project_control_snapshot(root: Path) -> tuple[tuple[str, tuple[int, str] | None], ...]:
    """Fingerprint the Lake inputs that select the compiled environment."""

    return tuple(
        (name, _snapshot_regular_file(root / name)) for name in _PROJECT_CONTROL_FILES
    )


def _graph_snapshot(graph: Graph) -> tuple[tuple[str, str, str], ...]:
    """Fingerprint the exact Markdown articles used to choose declarations."""

    return tuple(
        (
            node.id,
            _article_path(node, graph),
            node.source_sha256 or "",
        )
        for node in sorted(graph.nodes.values(), key=lambda item: item.id)
    )


def blueprint_hash(graph: Graph) -> str:
    """Identify the exact, path-independent blueprint snapshot behind a report."""

    material = json.dumps(
        _graph_snapshot(graph), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return _sha256_id(material.encode("utf-8"))


def _remember_descendants(
    process: subprocess.Popen[bytes],
    descendants: dict[tuple[int, float], psutil.Process],
) -> None:
    """Retain handles for children that may outlive their immediate parent."""

    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except (psutil.Error, OSError):
        return
    for child in children:
        try:
            descendants[(child.pid, child.create_time())] = child
        except (psutil.Error, OSError):
            continue


def _remember_tagged_processes(
    token: str,
    descendants: dict[tuple[int, float], psutil.Process],
    *,
    root_pid: int,
) -> None:
    """Find descendants that escaped the original parent and process group."""

    for candidate in psutil.process_iter():
        if candidate.pid in {os.getpid(), root_pid}:
            continue
        try:
            if candidate.environ().get(_PROCESS_TOKEN_ENV) == token:
                descendants[(candidate.pid, candidate.create_time())] = candidate
        except (psutil.Error, OSError):
            continue


def _process_is_alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, OSError):
        return False


def _process_group_is_alive(pid: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.perf_counter())


def _join_readers(readers: list[threading.Thread], *, deadline: float) -> bool:
    for reader in readers:
        reader.join(timeout=_remaining(deadline))
    return not any(reader.is_alive() for reader in readers)


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    descendants: dict[tuple[int, float], psutil.Process],
    *,
    deadline: float,
    token: str,
) -> None:
    """Best-effort termination of a command and every descendant observed."""

    _remember_descendants(process, descendants)
    _remember_tagged_processes(token, descendants, root_pid=process.pid)
    children = [
        child
        for child in descendants.values()
        if child.pid != process.pid and _process_is_alive(child)
    ]
    phase_start = time.perf_counter()
    available = _remaining(deadline)
    process_deadline = phase_start + available * 0.8
    term_deadline = phase_start + available * 0.4
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    else:  # pragma: no cover - Windows-specific best effort
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.Error:
                pass

    if process.poll() is None:
        try:
            process.wait(timeout=_remaining(term_deadline))
        except (OSError, subprocess.TimeoutExpired):
            pass
    if children and _remaining(term_deadline) > 0:
        try:
            psutil.wait_procs(children, timeout=_remaining(term_deadline))
        except (psutil.Error, OSError):
            pass

    if os.name == "posix" and _process_group_is_alive(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    _remember_tagged_processes(token, descendants, root_pid=process.pid)
    for child in descendants.values():
        if _process_is_alive(child):
            try:
                child.kill()
            except psutil.Error:
                pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=_remaining(process_deadline))
    except (OSError, subprocess.TimeoutExpired):
        pass
    live_children = [
        child for child in descendants.values() if _process_is_alive(child)
    ]
    if live_children and _remaining(process_deadline) > 0:
        try:
            psutil.wait_procs(live_children, timeout=_remaining(process_deadline))
        except (psutil.Error, OSError):
            pass


class _CommandSignalled(BaseException):
    """A termination signal arrived while a bounded command was running."""


_GUARDED_SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP")
_ACTIVE_SIGNAL_GUARD: _SignalGuard | None = None


class _SignalGuard:
    """Route termination signals through the running command's cleanup.

    A signal that arrives while no started command is registered is deferred, so
    none can land between spawning a process group and remembering it; repeats
    during cleanup are swallowed.  On exit the previous handlers are restored and
    the first signal is re-delivered to them, so a default disposition still ends
    the process with the conventional status.
    """

    def __init__(self) -> None:
        self.previous: dict[int, object] = {}
        self.received: int | None = None
        self.armed = False

    def _handle(self, signum: int, frame: object) -> None:
        if self.received is None:
            self.received = signum
            if self.armed:
                self.armed = False
                raise _CommandSignalled(signum)

    def arm(self) -> None:
        """Let the next signal interrupt the command that was just registered."""

        if not self.previous:
            return
        if self.received is not None:
            raise _CommandSignalled(self.received)
        self.armed = True

    def disarm(self) -> None:
        self.armed = False

    def __enter__(self) -> _SignalGuard:
        global _ACTIVE_SIGNAL_GUARD
        for name in _GUARDED_SIGNALS:
            signum = getattr(signal, name)
            previous = signal.getsignal(signum)
            if previous in (signal.SIG_IGN, None):
                continue
            self.previous[signum] = previous
            signal.signal(signum, self._handle)
        _ACTIVE_SIGNAL_GUARD = self
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        global _ACTIVE_SIGNAL_GUARD
        _ACTIVE_SIGNAL_GUARD = None
        self.armed = False
        for signum, previous in self.previous.items():
            signal.signal(signum, previous)  # type: ignore[arg-type]
        if self.received is None:
            return False
        signal.raise_signal(self.received)
        if isinstance(exc, _CommandSignalled):
            name = signal.Signals(self.received).name
            raise SkeletonError([f"interrupted by {name}"]) from None
        return False


def _signal_guard() -> contextlib.AbstractContextManager[_SignalGuard]:
    """Guard signals in the main POSIX thread; reuse an enclosing guard."""

    if os.name != "posix" or threading.current_thread() is not threading.main_thread():
        return contextlib.nullcontext(_SignalGuard())
    if _ACTIVE_SIGNAL_GUARD is not None:
        return contextlib.nullcontext(_ACTIVE_SIGNAL_GUARD)
    return _SignalGuard()


def _run_bounded_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    context: str,
    env: dict[str, str] | None = None,
    output_limit: int = DEFAULT_PROBE_OUTPUT_LIMIT,
) -> subprocess.CompletedProcess[str]:
    """Run one command with bounded output, time, and descendant lifetime."""

    with _signal_guard() as guard:
        try:
            return _run_registered_command(
                command,
                cwd=cwd,
                timeout=timeout,
                context=context,
                env=env,
                output_limit=output_limit,
                guard=guard,
            )
        finally:
            guard.disarm()


def _run_registered_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    context: str,
    env: dict[str, str] | None = None,
    output_limit: int,
    guard: _SignalGuard,
) -> subprocess.CompletedProcess[str]:
    if timeout <= 0:
        raise _CommandTimedOut([f"{context} timed out"])
    if output_limit < 1:
        raise ValueError("output_limit must be positive")
    popen_options: dict[str, object] = {}
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif os.name == "nt":  # pragma: no cover - Windows-specific best effort
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process: subprocess.Popen[bytes] | None = None
    readers: list[threading.Thread] = []
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    capture_lock = threading.Lock()
    overflow = threading.Event()
    reader_failure = threading.Event()
    reader_errors: list[BaseException] = []
    captured = 0

    def drain(name: str, stream: object) -> None:
        nonlocal captured
        try:
            while True:
                block = stream.read(64 * 1024)  # type: ignore[attr-defined]
                if not block:
                    return
                with capture_lock:
                    remaining = max(0, output_limit - captured)
                    if remaining:
                        chunks[name].append(block[:remaining])
                    if len(block) > remaining:
                        overflow.set()
                    captured = min(output_limit + 1, captured + len(block))
        except (OSError, ValueError) as exc:
            reader_errors.append(exc)
            reader_failure.set()
        finally:
            try:
                stream.close()  # type: ignore[attr-defined]
            except OSError:
                pass

    descendants: dict[tuple[int, float], psutil.Process] = {}
    token = secrets.token_hex(16)
    process_env = os.environ.copy() if env is None else env.copy()
    process_env[_PROCESS_TOKEN_ENV] = token
    deadline = time.monotonic() + timeout
    cleanup_deadline: float | None = None
    failure: SkeletonError | None = None
    terminated = False
    try:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=process_env,
                close_fds=True,
                **popen_options,
            )
        except OSError as exc:
            raise SkeletonError([f"{context} failed: {exc}"]) from exc
        guard.arm()
        assert process.stdout is not None and process.stderr is not None
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            reader = threading.Thread(target=drain, args=(name, stream), daemon=True)
            reader.start()
            readers.append(reader)
        while process.poll() is None:
            _remember_descendants(process, descendants)
            if reader_failure.is_set():
                failure = SkeletonError(
                    [f"{context} output could not be read: {reader_errors[0]}"]
                )
                break
            if overflow.is_set():
                failure = SkeletonError(
                    [f"{context} exceeded the {output_limit}-byte output limit"]
                )
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = _CommandTimedOut([f"{context} timed out after {timeout:g} seconds"])
                break
            overflow.wait(min(0.05, remaining))
        _remember_descendants(process, descendants)
        _remember_tagged_processes(token, descendants, root_pid=process.pid)
        live_descendants = any(
            _process_is_alive(descendant) for descendant in descendants.values()
        )
        live_group = _process_group_is_alive(process.pid)
        if failure is None and (live_descendants or live_group):
            failure = SkeletonError([f"{context} left descendant processes running"])
        cleanup_deadline = time.perf_counter() + _PROCESS_TERMINATION_GRACE
        if failure is not None:
            _terminate_process_tree(
                process,
                descendants,
                deadline=cleanup_deadline,
                token=token,
            )
            terminated = True
        reader_deadline = cleanup_deadline
        if failure is None:
            reader_start = time.perf_counter()
            reader_deadline = reader_start + _remaining(cleanup_deadline) * 0.2
        if not _join_readers(readers, deadline=reader_deadline):
            if failure is None:
                failure = SkeletonError(
                    [f"{context} left descendant processes holding its output pipes open"]
                )
            if not terminated:
                _terminate_process_tree(
                    process,
                    descendants,
                    deadline=cleanup_deadline,
                    token=token,
                )
                terminated = True
            _join_readers(readers, deadline=cleanup_deadline)
        if failure is not None:
            raise failure
        if overflow.is_set():
            raise SkeletonError([f"{context} exceeded the {output_limit}-byte output limit"])
        if reader_errors:
            raise SkeletonError([f"{context} output could not be read: {reader_errors[0]}"])
        try:
            stdout = b"".join(chunks["stdout"]).decode("utf-8")
            stderr = b"".join(chunks["stderr"]).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkeletonError([f"{context} emitted invalid UTF-8 output"]) from exc
        return subprocess.CompletedProcess(command, process.returncode, stdout=stdout, stderr=stderr)
    except BaseException:
        if cleanup_deadline is None:
            cleanup_deadline = time.perf_counter() + _PROCESS_TERMINATION_GRACE
        if process is not None and not terminated:
            _terminate_process_tree(
                process,
                descendants,
                deadline=cleanup_deadline,
                token=token,
            )
        _join_readers(readers, deadline=cleanup_deadline)
        raise
    finally:
        try:
            if process is not None:
                for index, stream in enumerate((process.stdout, process.stderr)):
                    if stream is None:
                        continue
                    if index < len(readers) and readers[index].is_alive():
                        continue
                    else:
                        stream.close()
        finally:
            # A re-raised exception retains this frame.  Drop Popen so its
            # Windows process handle does not keep an exited PID allocated.
            process = None


def evidence_hash_of(packet: str) -> str:
    """The evidence hash of packet text, however it reached the caller.

    A read-back card shows its packet verbatim, so a reader can hash what the
    card displays and compare it with what the card records.
    """

    return _sha256_id(packet.encode("utf-8"))


# --------------------------------------------------------------------------- #
# Lean project layout
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LeanLibrary:
    """One ``lean_lib`` target: where its modules live and what they are called."""

    name: str
    src_dir: Path
    roots: tuple[str, ...]


def lean_libraries(lean_root: str | Path) -> tuple[LeanLibrary, ...]:
    """Read the project's library targets from its Lake configuration.

    A TOML manifest is read directly. A Lean manifest is evaluated by Lake
    itself through ``lake translate-config``, the same way the generated verify
    workflow reads the root package name, so both manifest languages are
    handled without a second parser for Lean syntax.
    """

    root = Path(lean_root).expanduser().resolve()
    toml = root / "lakefile.toml"
    toml_snapshot = _read_snapshot_file(toml)
    lakefile = root / "lakefile.lean"
    lakefile_snapshot = _read_snapshot_file(lakefile)
    if toml_snapshot is not None:
        text = toml_snapshot[0]
    elif lakefile_snapshot is not None:
        text = _translate_lakefile(root)
    else:
        raise SkeletonError([f"no lakefile.toml or lakefile.lean in {root}"])
    try:
        config = tomllib.loads(text.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SkeletonError([f"cannot parse the Lake configuration of {root}: {exc}"]) from exc

    libraries: list[LeanLibrary] = []
    for entry in config.get("lean_lib", []) or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = entry["name"]
        src_dir = root / str(entry.get("srcDir", "."))
        roots = entry.get("roots")
        if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
            roots = [name]
        libraries.append(LeanLibrary(name=name, src_dir=src_dir.resolve(), roots=tuple(roots)))
    if not libraries:
        package = config.get("name")
        if isinstance(package, str) and package:
            libraries.append(LeanLibrary(name=package, src_dir=root, roots=(package,)))
    if not libraries:
        raise SkeletonError([f"the Lake configuration of {root} declares no library"])
    return tuple(libraries)


def _translate_lakefile(root: Path) -> bytes:
    lake = shutil.which("lake")
    if lake is None:
        raise SkeletonError(["lake is not on PATH, so lakefile.lean cannot be evaluated"])
    with tempfile.TemporaryDirectory(prefix="autoform-skeleton-") as scratch:
        target = Path(scratch) / "lakefile.toml"
        result = _run_bounded_command(
            [lake, "translate-config", "toml", str(target)],
            cwd=root,
            timeout=120,
            context="lake translate-config",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:300]
            raise SkeletonError([f"lake translate-config failed: {detail}"])
        translated = _read_snapshot_file(target)
        if translated is None:
            raise SkeletonError(["lake translate-config failed: translated configuration is missing"])
        return translated[0]


def module_of(path: Path, libraries: tuple[LeanLibrary, ...]) -> str | None:
    """Return the Lean module name of a source file, or ``None`` if no library holds it."""

    resolved = path.resolve()
    best: tuple[int, str] | None = None
    for library in libraries:
        try:
            relative = resolved.relative_to(library.src_dir)
        except ValueError:
            continue
        if relative.suffix != ".lean":
            continue
        module = ".".join(relative.with_suffix("").parts)
        depth = len(library.src_dir.parts)
        if best is None or depth > best[0]:
            best = (depth, module)
    return None if best is None else best[1]


def path_of(module: str, libraries: tuple[LeanLibrary, ...], lean_root: Path) -> str | None:
    """Return the repository-relative source path of ``module``, if it exists."""

    parts = module.split(".")
    for library in libraries:
        candidate = library.src_dir.joinpath(*parts).with_suffix(".lean")
        if candidate.is_file():
            return _relative(candidate, lean_root)
    return None


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #

def _probe_template() -> str:
    """The Lean probe, kept beside the other generated-file sources under templates/."""

    return (Path(__file__).parent / "probes" / "skeleton_probe.lean").read_text(encoding="utf-8")


def render_probe(
    *,
    imports: tuple[str, ...],
    roots: tuple[str, ...],
    project_roots: tuple[str, ...],
) -> str:
    """Render the Lean program that extracts the skeleton of every root."""

    if not roots:
        raise SkeletonError(["refusing to render a probe with no declarations"])
    if not imports:
        raise SkeletonError(["refusing to render a probe with no imports"])
    return _probe_template().format(
        core_roots=", ".join(_lean_name(name) for name in _CORE_MODULE_ROOTS),
        imports="\n".join(f"import {module}" for module in sorted(set(imports))),
        marker=PROBE_MARKER,
        output_env=PROBE_OUTPUT_ENV,
        output_limit=DEFAULT_PROBE_OUTPUT_LIMIT,
        project_roots=", ".join(_lean_name(name) for name in sorted(set(project_roots))),
        roots=", ".join(f"({json.dumps(name, ensure_ascii=False)}, {_lean_name(name)})" for name in roots),
    )


def _lean_name(name: str) -> str:
    """Spell a Lean name as a term without trusting Lean to parse it."""

    result = "Name.anonymous"
    for part, quoted in _lean_name_parts(name):
        if not quoted and part.isascii() and part.isdigit():
            result = f"Name.num ({result}) {int(part)}"
        else:
            result = f"Name.str ({result}) {json.dumps(part, ensure_ascii=False)}"
    return result


def _lean_name_parts(name: str) -> tuple[tuple[str, bool], ...]:
    """Parse the dot-separated surface spelling of a Lean ``Name``.

    Guillemets quote one name component, so ``A.«b.c d»`` has two components,
    not three. Constructing the name structurally keeps all such spellings out
    of generated Lean syntax.
    """

    parts: list[tuple[str, bool]] = []
    index = 0
    while index < len(name):
        if name[index] == "«":
            close = name.find("»", index + 1)
            if close < 0 or close == index + 1:
                raise SkeletonError([f"invalid Lean declaration name: {name!r}"])
            parts.append((name[index + 1 : close], True))
            index = close + 1
        else:
            end = name.find(".", index)
            end = len(name) if end < 0 else end
            part = name[index:end]
            if not part or any(character.isspace() for character in part) or "«" in part or "»" in part:
                raise SkeletonError([f"invalid Lean declaration name: {name!r}"])
            parts.append((part, False))
            index = end
        if index == len(name):
            break
        if name[index] != ".":
            raise SkeletonError([f"invalid Lean declaration name: {name!r}"])
        index += 1
        if index == len(name):
            raise SkeletonError([f"invalid Lean declaration name: {name!r}"])
    if not parts:
        raise SkeletonError(["invalid empty Lean declaration name"])
    return tuple(parts)


def _probe_modules(probe: str) -> tuple[str, ...]:
    """Read the generated probe's leading project imports."""

    modules: list[str] = []
    for line in probe.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("import "):
            break
        module = stripped.removeprefix("import ").strip()
        if not module or any(character.isspace() for character in module):
            raise SkeletonError(["the generated skeleton probe contains an invalid import"])
        modules.append(module)
    if not modules:
        raise SkeletonError(["the generated skeleton probe contains no project imports"])
    return tuple(dict.fromkeys(modules))


def _check_artifacts_fresh(
    lake: str, lean_root: Path, modules: tuple[str, ...], *, timeout: float
) -> None:
    """Ask Lake to prove that imported artifacts match their exact inputs.

    ``--rehash`` distrusts every cached ``.hash`` sidecar and hashes the inputs
    afresh; Lake has no mode that does so without rewriting those sidecars, so
    the project's build directory must be writable.
    """

    result = _run_bounded_command(
        [lake, "--rehash", "--no-build", "build", *modules],
        cwd=lean_root,
        timeout=timeout,
        context="cannot verify Lean build freshness",
    )
    if result.returncode == _LAKE_NO_BUILD_EXIT:
        detail = (result.stderr or result.stdout).strip()
        raise SkeletonError([f"Lean build artifacts are stale; run `lake build` before extracting skeletons\n{detail}"])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SkeletonError(
            [
                f"cannot verify Lean build freshness: `lake --rehash --no-build build` exited with status "
                f"{result.returncode}; the check rewrites `.hash` files under `.lake`, which must be writable"
                f"\n{detail}"
            ]
        )


def run_probe(
    probe: str,
    lean_root: Path,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    freshness_timeout: float = DEFAULT_FRESHNESS_TIMEOUT,
) -> str:
    """Run ``probe`` with ``lake env lean`` inside the built project.

    ``freshness_timeout`` bounds the Lake freshness check that runs first and
    ``timeout`` the probe itself; neither spends the other's budget.
    """

    lake = shutil.which("lake")
    if lake is None:
        raise SkeletonError(["lake is not on PATH; a built Lean project is required to extract skeletons"])
    if not (lean_root / "lake-manifest.json").is_file():
        raise SkeletonError(
            ["lake-manifest.json is missing; run `lake build` before extracting skeletons"]
        )
    modules = _probe_modules(probe)
    _check_artifacts_fresh(lake, lean_root, modules, timeout=freshness_timeout)
    deadline = time.monotonic() + timeout
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    with _signal_guard(), tempfile.TemporaryDirectory(prefix="autoform-skeleton-") as scratch:
        source = Path(scratch) / "AutoformSkeletonProbe.lean"
        source.write_text(probe, encoding="utf-8")
        # Records go to their own file: on stdout any other write could split one.
        records = Path(scratch) / "records.out"
        env[PROBE_OUTPUT_ENV] = str(records)
        try:
            result = _run_bounded_command(
                [lake, "env", "lean", str(source)],
                cwd=lean_root,
                timeout=max(0.0, deadline - time.monotonic()),
                context="lake env lean",
                env=env,
            )
        except _CommandTimedOut as exc:
            raise SkeletonError(
                [
                    f"lake env lean timed out after {timeout:g} seconds; "
                    "rerun with --timeout <seconds> for large projects"
                ]
            ) from exc
        output = _read_probe_records(records) if result.returncode == 0 else ""
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        shadowed = _SHADOWED_CORE_MODULE.search(detail)
        if shadowed:
            root = shadowed.group(1).split(".", 1)[0]
            raise SkeletonError(
                [
                    f"the skeleton probe cannot load toolchain module {shadowed.group(1)}: a dependency "
                    f"library probably provides modules under `{root}`, which hides the toolchain's own `{root}`; "
                    f"rename that library's modules\n{detail}"
                ]
            )
        raise SkeletonError([f"the skeleton probe failed; is the project built with `lake build`?\n{detail}"])
    return output or result.stdout


def _read_probe_records(records: Path) -> str:
    """Read the probe's records file under the same byte limit as its captured output."""

    try:
        with records.open("rb") as handle:
            data = handle.read(DEFAULT_PROBE_OUTPUT_LIMIT + 1)
    except FileNotFoundError:
        return ""
    if len(data) > DEFAULT_PROBE_OUTPUT_LIMIT:
        raise SkeletonError([f"lake env lean exceeded the {DEFAULT_PROBE_OUTPUT_LIMIT}-byte output limit"])
    return data.decode("utf-8")


_FOUND_RECORD_FIELDS = frozenset(
    {
        "assumed",
        "boundary_modules",
        "assumed_semantics",
        "axiom_semantics",
        "axioms",
        "depends",
        "found",
        "kind",
        "lean_version",
        "module",
        "range",
        "root",
        "raw_signature",
        "semantic",
        "semantic_schema",
        "signature",
        "source",
        "source_comments",
        "statement_comments",
        "statement_source",
        "trusted",
    }
)
_TRUSTED_RECORD_FIELDS = frozenset(
    {
        "depends",
        "kind",
        "module",
        "name",
        "range",
        "raw_signature",
        "semantic",
        "semantic_schema",
        "signature",
        "source",
        "source_comments",
        "source_name",
    }
)
_DECLARATION_KINDS = frozenset(
    {"axiom", "class", "constructor", "def", "inductive", "instance", "opaque", "quot", "recursor", "structure", "theorem"}
)


def parse_probe_output(
    text: str, *, expected_roots: tuple[str, ...] | None = None
) -> dict[str, dict[str, object]]:
    """Return strictly validated probe records, keyed by requested root name."""

    records: dict[str, dict[str, object]] = {}
    tables: dict[str, dict[str, object]] = {table: {} for table in _PROBE_TABLES}
    expected = None if expected_roots is None else set(expected_roots)
    for line in text.splitlines():
        if not line.startswith(PROBE_MARKER):
            continue
        try:
            record = json.loads(line[len(PROBE_MARKER) :])
        except json.JSONDecodeError as exc:
            raise SkeletonError([f"the skeleton probe emitted invalid JSON: {exc}"]) from exc
        if isinstance(record, dict) and "table" in record:
            _read_probe_table(record, tables)
            continue
        if not isinstance(record, dict) or not isinstance(record.get("root"), str) or not record["root"]:
            raise SkeletonError(["the skeleton probe emitted a record without a root name"])
        root = record["root"]
        if root in records:
            raise SkeletonError([f"the skeleton probe emitted duplicate records for {root}"])
        if expected is not None and root not in expected:
            raise SkeletonError([f"the skeleton probe emitted an unrequested root: {root}"])
        records[root] = record
    expand = _expand_probe_material(tables)
    # Shared entries are validated once, however many roots name them.
    checked: set[int] = set()
    for root, record in records.items():
        records[root] = _resolve_probe_record(record, tables, root=root, expand=expand)
        _validate_probe_record(records[root], root=root, checked=checked)
    return records


#: What a probe table entry's value must be: a trusted declaration's record,
#: an external constant's semantic material, a module's compiled files, or a
#: fragment of semantic material. Material is a list of text pieces and
#: fragment numbers, which expands to its text.
_PROBE_TABLES: dict[str, type] = {"fragment": list, "module": list, "semantic": list, "trusted": dict}


def _expand_probe_material(tables: dict[str, dict[str, object]]) -> Callable[[object, str], str]:
    """Replace the semantic material in ``tables`` with its text; return the expander.

    A fragment refers only to earlier fragments, so expansion terminates; the
    characters it produces across the run are bounded.
    """

    fragments: dict[int, list[object]] = {}
    for name, pieces in tables.pop("fragment").items():
        number = int(name) if name.isdecimal() and name.isascii() else -1
        if str(number) != name or not isinstance(pieces, list) or not _material_pieces(pieces, below=number):
            raise SkeletonError([f"the skeleton probe emitted a malformed fragment {name}"])
        fragments[number] = pieces
    remaining = _PROBE_MATERIAL_LIMIT

    def expand(pieces: object, context: str) -> str:
        nonlocal remaining
        if (
            not isinstance(pieces, list)
            or not _material_pieces(pieces, below=None)
            or not all(isinstance(piece, str) or piece in fragments for piece in pieces)
        ):
            raise SkeletonError([f"the skeleton probe emitted invalid semantic material for {context}"])
        text: list[str] = []
        stack = [iter(pieces)]
        while stack:
            piece = next(stack[-1], None)
            if piece is None:
                stack.pop()
            elif isinstance(piece, str):
                remaining -= len(piece)
                if remaining < 0:
                    raise SkeletonError(
                        [f"the skeleton probe's semantic material exceeds {_PROBE_MATERIAL_LIMIT} characters"]
                    )
                text.append(piece)
            elif isinstance(piece, int):
                stack.append(iter(fragments[piece]))
        return "".join(text)

    semantics = tables["semantic"]
    for name, pieces in semantics.items():
        semantics[name] = expand(pieces, name)
    for name, item in tables["trusted"].items():
        if isinstance(item, dict) and "semantic" in item:
            item["semantic"] = expand(item["semantic"], name)
    return expand


def _material_pieces(pieces: object, *, below: int | None) -> bool:
    """Whether ``pieces`` is text and fragment numbers (each below ``below``)."""

    return isinstance(pieces, list) and all(
        isinstance(piece, str)
        or (type(piece) is int and piece >= 0 and (below is None or piece < below))
        for piece in pieces
    )


def _read_probe_table(record: dict[str, object], tables: dict[str, dict[str, object]]) -> None:
    table, name, value = record.get("table"), record.get("name"), record.get("value")
    if (
        record.keys() != {"name", "table", "value"}
        or not isinstance(table, str)
        or table not in _PROBE_TABLES
        or not isinstance(name, str)
        or not name
        or not isinstance(value, _PROBE_TABLES[table])
        or (isinstance(value, dict) and value.get("name") != name)
    ):
        raise SkeletonError(["the skeleton probe emitted a malformed shared table entry"])
    if name in tables[table]:
        raise SkeletonError([f"the skeleton probe emitted duplicate {table} entries for {name}"])
    tables[table][name] = value


def _resolve_probe_record(
    record: dict[str, object],
    tables: dict[str, dict[str, object]],
    *,
    root: str,
    expand: Callable[[object, str], str],
) -> dict[str, object]:
    """Replace a root record's names with the shared entries they refer to."""

    if record.get("found") is not True:
        return record
    if record.keys() != _FOUND_RECORD_FIELDS - {"assumed_semantics", "axiom_semantics"}:
        raise SkeletonError([f"the skeleton probe emitted invalid fields for {root}"])

    def entries(field: str, table: str) -> list[tuple[str, object]]:
        names = record[field]
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise SkeletonError([f"the skeleton probe emitted an invalid {field} field for {root}"])
        missing = [name for name in names if name not in tables[table]]
        if missing:
            raise SkeletonError([f"the skeleton probe emitted no {table} entry for {missing[0]} ({root})"])
        return [(name, tables[table][name]) for name in names]

    resolved = dict(record)
    resolved["semantic"] = expand(record["semantic"], root)
    resolved["trusted"] = [item for _, item in entries("trusted", "trusted")]
    resolved["assumed_semantics"] = [[name, value] for name, value in entries("assumed", "semantic")]
    resolved["axiom_semantics"] = [[name, value] for name, value in entries("axioms", "semantic")]
    resolved["boundary_modules"] = [
        [module, *file] if isinstance(file, list) else file
        for module, files in entries("boundary_modules", "module")
        if isinstance(files, list)
        for file in files
    ]
    return resolved


def _validate_probe_record(record: dict[str, object], *, root: str, checked: set[int] | None = None) -> None:
    found = record.get("found")
    if type(found) is not bool:
        raise SkeletonError([f"the skeleton probe emitted a non-boolean found field for {root}"])
    expected_fields = _FOUND_RECORD_FIELDS if found else frozenset({"found", "root"})
    if record.keys() != expected_fields:
        raise SkeletonError([f"the skeleton probe emitted invalid fields for {root}"])
    if not found:
        return
    _require_kind(record.get("kind"), context=root)
    _require_nonempty_string(record.get("lean_version"), field="lean_version", context=root)
    _require_semantic(record, context=root, kind=str(record["kind"]))
    _require_nonempty_string(record.get("module"), field="module", context=root)
    _require_nonempty_string(record.get("signature"), field="signature", context=root)
    _require_nonempty_string(record.get("raw_signature"), field="raw_signature", context=root)
    _require_range(record.get("range"), context=root)
    _require_probe_source(record.get("source"), kind=str(record["kind"]), context=root)
    _require_probe_comments(record, "source", "source_comments", context=root)
    statement = record.get("statement_source")
    if statement is not None and not isinstance(statement, str):
        raise SkeletonError([f"the skeleton probe emitted an invalid statement_source field for {root}"])
    _require_probe_comments(record, "statement_source", "statement_comments", context=root)
    for field in ("depends", "assumed", "axioms"):
        _require_string_list(record.get(field), field=field, context=root)
    _require_semantic_pairs(
        record.get("assumed_semantics"),
        names=record["assumed"],
        field="assumed_semantics",
        context=root,
    )
    boundary_modules = _require_module_files(record.get("boundary_modules"), context=root)
    if record["assumed"] and not boundary_modules:
        raise SkeletonError([f"the skeleton probe omitted boundary module files for {root}"])
    _require_semantic_pairs(
        record.get("axiom_semantics"),
        names=record["axioms"],
        field="axiom_semantics",
        context=root,
    )
    trusted = record.get("trusted")
    if not isinstance(trusted, list) or not all(isinstance(item, dict) for item in trusted):
        raise SkeletonError([f"the skeleton probe emitted an invalid trusted field for {root}"])
    seen: set[str] = set()
    for item in trusted:
        if checked is None or id(item) not in checked:
            _validate_trusted_record(item, root=root)
            if checked is not None:
                checked.add(id(item))
        name = item["name"]
        if name in seen:
            raise SkeletonError([f"the skeleton probe emitted duplicate trusted declaration {name} for {root}"])
        seen.add(name)


def _validate_trusted_record(record: dict[str, object], *, root: str) -> None:
    name = record.get("name")
    context = f"{root} trusted declaration {name}" if isinstance(name, str) and name else root
    if record.keys() != _TRUSTED_RECORD_FIELDS:
        raise SkeletonError([f"the skeleton probe emitted invalid trusted fields for {context}"])
    _require_nonempty_string(name, field="name", context=root)
    _require_nonempty_string(record.get("source_name"), field="source_name", context=context)
    _require_kind(record.get("kind"), context=context)
    _require_semantic(record, context=context, kind=str(record["kind"]))
    _require_nonempty_string(record.get("module"), field="module", context=context)
    _require_nonempty_string(record.get("signature"), field="signature", context=context)
    _require_nonempty_string(record.get("raw_signature"), field="raw_signature", context=context)
    _require_range(record.get("range"), context=context)
    _require_probe_source(record.get("source"), kind=str(record["kind"]), context=context)
    _require_probe_comments(record, "source", "source_comments", context=context)
    _require_string_list(record.get("depends"), field="depends", context=context)


def _require_semantic(record: dict[str, object], *, context: str, kind: str) -> None:
    if record.get("semantic_schema") != SEMANTIC_SCHEMA:
        raise SkeletonError([f"the skeleton probe emitted an unsupported semantic schema for {context}"])
    semantic = record.get("semantic")
    _require_nonempty_string(semantic, field="semantic", context=context)
    assert isinstance(semantic, str)
    _validate_semantic_material(semantic, context=context, kind=kind)


def _require_kind(value: object, *, context: str) -> None:
    if not isinstance(value, str) or value not in _DECLARATION_KINDS:
        raise SkeletonError([f"the skeleton probe emitted an invalid declaration kind for {context}"])


def _require_nonempty_string(value: object, *, field: str, context: str) -> None:
    if not isinstance(value, str) or not value:
        raise SkeletonError([f"the skeleton probe emitted an invalid {field} field for {context}"])


def _require_probe_source(value: object, *, kind: str, context: str) -> None:
    if value is not None and not isinstance(value, str):
        raise SkeletonError([f"the skeleton probe emitted an invalid source field for {context}"])
    if kind in {"theorem", "axiom"} and value is not None:
        raise SkeletonError([f"the skeleton probe emitted proof-bearing source for {context}"])


def _probe_record_issue(record: dict[str, object]) -> str | None:
    """Why a validated probe record cannot yield a skeleton, confined to its node.

    A declaration whose source cannot be located leaves nothing faithful to
    show a reviewer. That is a gap in this node's evidence, not in the probe
    run, so other nodes still extract. Source Lean cannot read outside its file
    (for example, a statement that uses `local notation`) is only withheld: the
    signatures and canonical kernel material still state its meaning. A
    partial root or trusted dependency has no kernel meaning to bind, so its
    node is refused as well.
    """

    root = str(record["root"])
    trusted = record["trusted"]
    assert isinstance(trusted, list)
    safety = [(root, record)] + [(str(item.get("source_name") or ""), item) for item in trusted]
    for source_name, item in safety:
        issue = _local_safety_issue(source_name, str(item["semantic"]))
        if issue is not None:
            return issue
    entries = [(root, root, record)]
    entries += [(str(item["name"]), f"{root} trusted declaration {item['name']}", item) for item in trusted]
    for name, context, item in entries:
        if _source_required(name, str(item["kind"]), item.get("source"), item.get("range") is not None):
            return f"the skeleton probe omitted required source for {context}"
    return None


def _source_required(name: str, kind: str, source: object, has_range: bool) -> bool:
    """Whether a declaration lacks the source text a reviewer must be shown.

    Lean reserves internal-detail spellings for helpers such as `f._unary`,
    `f._f`, `root._auto_1` and `S.x._default`. Their elaborated material is
    bound by the hash, but the spelling alone cannot prove which source made
    them, so a missing source is stated rather than borrowed from their parent.
    Any ordinary name without its own source is refused.
    """

    if kind in {"theorem", "axiom"} or (isinstance(source, str) and source.strip()):
        return False
    return has_range or not _internal_detail(name)


def _internal_detail(name: str) -> bool:
    """Mirror Lean's `Name.isInternalDetail` on a probe-emitted name."""

    parts = _lean_name_parts(name)
    return any(part.startswith("_") or (part.isdigit() and not quoted) for part, quoted in parts) or bool(
        re.fullmatch(r"(?:eq|match|proof|omega)_[0-9_]*", parts[-1][0])
    )


def _require_probe_comments(record: dict[str, object], text_field: str, field: str, *, context: str) -> None:
    """Comment ranges are ``null`` when Lean could not parse the text on its own."""

    value = record.get(field)
    if value is None:
        return
    text = record.get(text_field)
    try:
        _comment_ranges(value, text if isinstance(text, str) else None, context=context)
    except SkeletonError as exc:
        raise SkeletonError([f"the skeleton probe emitted an invalid {field} field for {context}"]) from exc


def _shown_source(
    text: str | None, comments: object, *, name: str
) -> tuple[str | None, tuple[tuple[int, int], ...], bool]:
    """Source a reader can be shown, its comment ranges, and whether it was withheld.

    Only Lean knows where a comment starts: a project token such as ``=--`` is
    code to it. Text Lean could not parse alone is shown only if it has nothing
    that could start a comment; otherwise it is withheld, and the reader gets
    the signatures and canonical kernel material, which state its meaning.
    """

    if text is None:
        return None, (), False
    if comments is None:
        if "--" in text or "/-" in text:
            return None, (), True
        return text, (), False
    return text, _comment_ranges(comments, text, context=name), False


def _require_range(value: object, *, context: str) -> None:
    if value is None:
        return
    if not isinstance(value, list) or len(value) != 2:
        raise SkeletonError([f"the skeleton probe emitted an invalid range for {context}"])
    start, end = value
    if type(start) is not int or type(end) is not int or start < 1 or end < start:
        raise SkeletonError([f"the skeleton probe emitted an invalid range for {context}"])


def _require_string_list(value: object, *, field: str, context: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise SkeletonError([f"the skeleton probe emitted an invalid {field} field for {context}"])
    if len(value) != len(set(value)):
        raise SkeletonError([f"the skeleton probe emitted duplicate {field} entries for {context}"])


def _validate_semantic_material(
    semantic: str, *, context: str, kind: str | None = None
) -> None:
    try:
        payload = json.loads(semantic)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SkeletonError([f"invalid elaborated semantic material for {context}"]) from exc
    if not isinstance(payload, dict) or payload.keys() != {"generated", "root"}:
        raise SkeletonError([f"invalid elaborated semantic material for {context}"])
    expected = _semantic_keys_for_kind(kind) if kind is not None else None
    _validate_semantic_payload(payload["root"], context=context, expected=expected)
    generated = payload["generated"]
    if not isinstance(generated, list):
        raise SkeletonError([f"invalid elaborated semantic material for {context}"])
    names: list[str] = []
    for entry in generated:
        if (
            not isinstance(entry, dict)
            or entry.keys() != {"material", "name"}
            or not _valid_semantic_name(entry["name"])
        ):
            raise SkeletonError([f"invalid elaborated semantic material for {context}"])
        names.append(json.dumps(entry["name"], sort_keys=True, separators=(",", ":")))
        _validate_semantic_payload(entry["material"], context=context, expected=None)
    if len(names) != len(set(names)):
        raise SkeletonError([f"invalid elaborated semantic material for {context}"])


def _valid_semantic_name(value: object) -> bool:
    depth = 0
    while value is not None:
        if not isinstance(value, dict) or len(value) != 1:
            return False
        if "str" in value:
            pair = value["str"]
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not isinstance(pair[1], str)
            ):
                return False
        elif "num" in value:
            pair = value["num"]
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or type(pair[1]) is not int
                or pair[1] < 0
            ):
                return False
        else:
            return False
        value = pair[0]
        depth += 1
        if depth > 256:
            return False
    return True


def _semantic_keys_for_kind(kind: str) -> set[str]:
    return {
        "def": {"safety", "type", "value"},
        "instance": {"safety", "type", "value"},
        "opaque": {"safety", "type", "value"},
        "class": {"constructors", "safety", "type"},
        "inductive": {"constructors", "safety", "type"},
        "structure": {"constructors", "safety", "type"},
    }.get(kind, {"safety", "type"})


def _validate_semantic_payload(
    payload: object, *, context: str, expected: set[str] | None
) -> None:
    allowed = (
        {"safety", "type"},
        {"safety", "type", "value"},
        {"constructors", "safety", "type"},
    )
    if not isinstance(payload, dict) or (
        expected is not None and payload.keys() != expected
    ) or (expected is None and set(payload) not in allowed):
        raise SkeletonError([f"invalid elaborated semantic material for {context}"])
    if payload.get("safety") not in {"safe", "unsafe", "partial"}:
        raise SkeletonError([f"invalid elaborated semantic material for {context}"])


def _semantic_pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise SkeletonError(["invalid semantic identities in skeleton report"])
    pairs: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) and part for part in item)
        ):
            raise SkeletonError(["invalid semantic identities in skeleton report"])
        _validate_semantic_material(item[1], context=item[0])
        pairs.append((item[0], item[1]))
    if len({name for name, _ in pairs}) != len(pairs):
        raise SkeletonError(["duplicate semantic identities in skeleton report"])
    return tuple(pairs)


def _local_safety_issue(source_name: str, semantic: str) -> str | None:
    """Refuse partial declarations by the Lean environment's own record.

    The source text is not consulted: `partial` may sit on its own line, come
    from a macro, or mark a `where` helper, and generated names are not indexed.
    """

    payload = json.loads(semantic)
    materials = [payload["root"], *(entry["material"] for entry in payload["generated"])]
    if any(isinstance(material, dict) and material.get("safety") == "partial" for material in materials):
        return f"partial declaration {source_name} cannot be included in a trusted skeleton"
    return None


def _require_semantic_pairs(
    value: object,
    *,
    names: object,
    field: str,
    context: str,
) -> None:
    try:
        pairs = _semantic_pairs(value)
    except SkeletonError as exc:
        raise SkeletonError(
            [f"the skeleton probe emitted an invalid {field} field for {context}"]
        ) from exc
    pair_names = [name for name, _ in pairs]
    matches = isinstance(names, list) and pair_names == names
    if not matches:
        raise SkeletonError(
            [f"the skeleton probe emitted mismatched {field} entries for {context}"]
        )


def _module_file_entries(value: object, *, context: str) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(value, list):
        raise SkeletonError([f"invalid assumed module files for {context}"])
    entries: list[tuple[str, str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 3
            or not all(isinstance(part, str) and part for part in item)
            or item[1] not in _MODULE_FILE_KINDS
        ):
            raise SkeletonError([f"invalid assumed module files for {context}"])
        entries.append((item[0], item[1], item[2]))
    if len({(module, kind) for module, kind, _ in entries}) != len(entries):
        raise SkeletonError([f"duplicate assumed module files for {context}"])
    if {module for module, _, _ in entries} != {module for module, kind, _ in entries if kind == "olean"}:
        raise SkeletonError([f"missing assumed module olean for {context}"])
    return tuple(entries)


def _require_module_files(value: object, *, context: str) -> tuple[tuple[str, str, str], ...]:
    return _module_file_entries(value, context=context)


def _hash_module_files(
    value: object,
    *,
    lean_root: Path,
    cache: dict[tuple[str, str], str],
    snapshot_started_ns: int | None = None,
) -> tuple[tuple[str, str, str], ...]:
    """Hash boundary compiled artifacts without allowing their checkout paths into the digest."""

    identities: list[tuple[str, str, str]] = []
    for module, file_kind, raw_path in _module_file_entries(value, context="probe output"):
        path = Path(raw_path)
        if not path.is_absolute():
            path = lean_root / path
        try:
            resolved = path.resolve(strict=True)
            key = (file_kind, str(resolved))
            digest = cache.get(key)
            if digest is None:
                before = resolved.stat()
                content = resolved.read_bytes()
                after = resolved.stat()
                identity_before = (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                identity_after = (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                if identity_before != identity_after or (
                    snapshot_started_ns is not None
                    and max(after.st_mtime_ns, after.st_ctime_ns) > snapshot_started_ns
                ):
                    raise SkeletonError(
                        [f"assumed module {module} changed during skeleton extraction; retry after the build is idle"]
                    )
                digest = _sha256_id(file_kind.encode("utf-8") + b"\0" + content)
                cache[key] = digest
        except SkeletonError:
            raise
        except (OSError, RuntimeError) as exc:
            raise SkeletonError([f"cannot bind assumed module {module} to {file_kind} file {path}: {exc}"]) from exc
        identities.append((module, file_kind, digest))
    return tuple(identities)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def extract_skeletons(
    blueprint_dir: str | Path,
    *,
    lean_root: str | Path,
    runner: ProbeRunner | None = None,
    node_ids: tuple[str, ...] | None = None,
) -> SkeletonReport:
    """Extract the skeleton of every ``lean:`` declaration the blueprint names.

    ``runner`` executes the rendered probe and returns Lean's standard output.
    A declaration the lexical index cannot place is reported as unresolved
    without running Lean, exactly as ``autoform check --lean-root`` reports it.
    """

    try:
        graph = load_graph(blueprint_dir)
    except GraphValidationError as exc:
        raise SkeletonError(exc.issues) from exc
    root = Path(lean_root).expanduser().resolve()
    graph_snapshot = _graph_snapshot(graph)
    control_snapshot = _project_control_snapshot(root)
    libraries = lean_libraries(root)
    if _project_control_snapshot(root) != control_snapshot:
        raise SkeletonError(
            ["Lean project configuration changed while skeletons were being extracted; retry after the project is idle"]
        )
    index = index_project(root)
    report = extract_graph_skeletons(
        graph,
        lean_root=root,
        libraries=libraries,
        index=index,
        runner=runner or run_probe,
        node_ids=node_ids,
    )
    if index_project(root).source_digest != index.source_digest:
        raise SkeletonError(["Lean sources changed during skeleton extraction; retry after the build is idle"])
    if _project_control_snapshot(root) != control_snapshot:
        raise SkeletonError(
            ["Lean project configuration changed while skeletons were being extracted; retry after the project is idle"]
        )
    try:
        current_graph = load_graph(graph.blueprint_dir)
    except GraphValidationError as exc:
        raise SkeletonError(
            ["the blueprint changed while skeletons were being extracted; retry after the project is idle"]
        ) from exc
    if _graph_snapshot(current_graph) != graph_snapshot:
        raise SkeletonError(
            ["the blueprint changed while skeletons were being extracted; retry after the project is idle"]
        )
    for node in report.nodes:
        current = current_graph.nodes.get(node.node_id)
        if current is None or source_passage(current, current_graph.blueprint_dir) != (
            node.passage,
            node.passage_locator,
        ):
            raise SkeletonError(
                [f"{node.node_id}: source passage changed while skeletons were being extracted; retry after the project is idle"]
            )
    return report


def extract_graph_skeletons(
    graph: Graph,
    *,
    lean_root: Path,
    libraries: tuple[LeanLibrary, ...],
    index: SourceIndex,
    runner: ProbeRunner,
    node_ids: tuple[str, ...] | None = None,
) -> SkeletonReport:
    """Extract skeletons for an already loaded graph."""

    targets: list[tuple[Node, tuple[str, ...]]] = []
    target_issues: list[str] = []
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        if not node.lean:
            continue
        names = tuple(declaration_names(node.lean))
        if not names:
            target_issues.append(f"{node.id}: lean target list contains no declarations")
        elif len(names) != len(set(names)):
            duplicates = sorted({name for name in names if names.count(name) > 1})
            target_issues.append(
                f"{node.id}: duplicate Lean declaration target(s): {', '.join(duplicates)}"
            )
        else:
            targets.append((node, names))
    if target_issues:
        raise SkeletonError(target_issues)
    target_ids = {node.id for node, _ in targets}
    selected = targets
    selection = "all"
    if node_ids is not None:
        if not node_ids:
            raise SkeletonError(["no articles selected"])
        if len(node_ids) != len(set(node_ids)):
            raise SkeletonError(["duplicate article selection"])
        wanted = set(node_ids)
        unknown = sorted(wanted - set(graph.nodes))
        if unknown:
            raise SkeletonError([f"unknown article: {node_id}" for node_id in unknown])
        untargeted = sorted(wanted - target_ids)
        if untargeted:
            raise SkeletonError(
                [f"{node_id}: article has no Lean declaration targets" for node_id in untargeted]
            )
        selected = [(node, names) for node, names in selected if node.id in wanted]
        selection = "filtered"
    passages: dict[str, tuple[str | None, str | None]] = {}
    # An article whose cited passage cannot be found cannot be judged for
    # faithfulness, so its declarations are unresolved rather than shown alone.
    broken_passages: dict[str, str] = {}
    for node, _ in selected:
        passage_issues: list[str] = []
        passages[node.id] = source_passage(node, graph.blueprint_dir, issues=passage_issues)
        if passage_issues:
            broken_passages[node.id] = "; ".join(passage_issues)

    unresolved: list[UnresolvedTarget] = []
    imports: set[str] = set()
    roots: list[str] = []
    for node, names in selected:
        for name in names:
            if node.id in broken_passages:
                unresolved.append(UnresolvedTarget(node.id, name, broken_passages[node.id]))
                continue
            location = index.find(name)
            module = None if location is None else module_of(lean_root / location.path, libraries)
            if location is None:
                unresolved.append(
                    UnresolvedTarget(node.id, name, "declaration not found in the Lean sources")
                )
                continue
            if module is None:
                unresolved.append(
                    UnresolvedTarget(
                        node.id,
                        name,
                        f"source {location.path.as_posix()} is not built by any library target",
                    )
                )
                continue
            imports.add(module)
            if name not in roots:
                roots.append(name)

    records: dict[str, dict[str, object]] = {}
    snapshot_started_ns: int | None = None
    if roots:
        program = render_probe(
            imports=tuple(sorted(imports)),
            roots=tuple(roots),
            project_roots=tuple(root for library in libraries for root in library.roots),
        )
        snapshot_started_ns = time.time_ns()
        records = parse_probe_output(runner(program, lean_root), expected_roots=tuple(roots))

    nodes: list[NodeSkeleton] = []
    module_hashes: dict[tuple[str, str], str] = {}
    for node, names in selected:
        declarations: list[DeclarationSkeleton] = []
        for name in names:
            if name not in roots or node.id in broken_passages:
                continue
            record = records.get(name)
            if record is None:
                unresolved.append(UnresolvedTarget(node.id, name, "the probe returned no record"))
                continue
            if not record.get("found"):
                unresolved.append(
                    UnresolvedTarget(
                        node.id,
                        name,
                        "not in the built environment; run `lake build`",
                    )
                )
                continue
            issue = _probe_record_issue(record)
            if issue is not None:
                unresolved.append(UnresolvedTarget(node.id, name, issue))
                continue
            declarations.append(
                _declaration(
                    record,
                    libraries=libraries,
                    lean_root=lean_root,
                    index=index,
                    module_hashes=module_hashes,
                    snapshot_started_ns=snapshot_started_ns,
                )
            )
        passage, locator = passages[node.id]
        nodes.append(
            NodeSkeleton(
                node_id=node.id,
                article_path=_article_path(node, graph),
                declarations=tuple(declarations),
                passage=passage,
                passage_locator=locator,
                complete={item.name for item in declarations} == set(names),
            )
        )
    return SkeletonReport(
        blueprint_hash=blueprint_hash(graph),
        targets=tuple((node.id, names) for node, names in targets),
        selection=selection,
        selected_nodes=tuple(node.id for node, _ in selected),
        nodes=tuple(nodes),
        unresolved=tuple(
            sorted(
                set(unresolved),
                key=lambda issue: (issue.node_id, issue.declaration, issue.reason),
            )
        ),
    )


_TRAILING_VALUE = re.compile(r"(?::=\s*(?:by)?|\bwhere)\s*\Z")
_LINE_LOCATOR = re.compile(r"\AL(\d+)(?:-L(\d+))?\Z")


def _statement(value: object) -> str | None:
    """Normalize the parser's cut: drop a trailing `:=`, `:= by`, or `where`."""

    if not isinstance(value, str) or not value.strip():
        return None
    return _TRAILING_VALUE.sub("", value).rstrip()


def source_passage(node: Node, blueprint: Path, *, issues: list[str] | None = None) -> tuple[str | None, str | None]:
    """Return the passage an article cites through a line locator, and the locator.

    A ``## Sources`` link to a non-Markdown file inside the blueprint with a
    ``#L<start>-L<end>`` fragment names the exact source text the statement
    came from. The first such link wins. Markdown targets are notes, not
    passages, and are ignored here. When the first locator names no text there
    is no passage, and the reason is appended to ``issues`` if given.
    """

    def broken(target: str, why: str) -> tuple[None, None]:
        if issues is not None:
            issues.append(f"source locator {target} {why}")
        return None, None

    for target in node.sources:
        parsed = urlsplit(target)
        path = unquote(parsed.path)
        fragment = unquote(parsed.fragment)
        match = _LINE_LOCATOR.fullmatch(fragment or "")
        if parsed.scheme or parsed.netloc or match is None or not path or path.endswith(".md"):
            continue
        try:
            candidate = (node.path.parent / path).resolve()
            candidate.relative_to(blueprint.resolve())
        except ValueError:
            return broken(target, "points outside the blueprint")
        try:
            captured = _read_snapshot_file(candidate)
            if captured is None:
                return broken(target, "names a missing file")
            # Lines are what an editor or `sed` counts: newline-separated. Python's
            # `splitlines` also breaks on form feeds, which `pdftotext` writes
            # between pages, and every locator into such a file would then drift
            # by one line per page.
            lines = captured[0].decode("utf-8").split("\n")
            if lines and lines[-1] == "":
                lines.pop()
        except (ValueError, UnicodeError):
            return broken(target, "names a file that is not readable UTF-8 text")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end > len(lines):
            return broken(target, "names no lines of its file")
        relative = candidate.relative_to(blueprint.resolve()).as_posix()
        return "\n".join(lines[start - 1 : end]), f"{relative}#L{start}-L{end}"
    return None, None


def _article_path(node: Node, graph: Graph) -> str:
    try:
        return node.path.relative_to(graph.blueprint_dir).as_posix()
    except ValueError:
        return node.path.name


def _declaration(
    record: dict[str, object],
    *,
    libraries: tuple[LeanLibrary, ...],
    lean_root: Path,
    index: SourceIndex,
    module_hashes: dict[tuple[str, str], str],
    snapshot_started_ns: int | None,
) -> DeclarationSkeleton:
    trusted_records = record.get("trusted")
    trusted = [
        _trusted(item, libraries=libraries, lean_root=lean_root, index=index)
        for item in (trusted_records if isinstance(trusted_records, list) else [])
        if isinstance(item, dict)
    ]
    name = str(record["root"])
    semantic = str(record["semantic"])
    module = str(record.get("module") or "")
    start, end = _range(record.get("range"))
    source, source_comments, source_withheld = _shown_source(
        _optional_probe_string(record.get("source")), record.get("source_comments"), name=name
    )
    written, written_comments, statement_withheld = _shown_source(
        _optional_probe_string(record.get("statement_source")), record.get("statement_comments"), name=name
    )
    if written is None and source is None and start is not None:
        statement_withheld = True
    source_withheld = source_withheld or statement_withheld
    statement = _statement(written)
    # `_statement` only trims the end, so the ranges still apply up to its length.
    limit = len((statement or "").encode("utf-8"))
    statement_comments = _comment_ranges(
        [[start, min(end, limit)] for start, end in written_comments if start < limit],
        statement,
        context=f"the statement of {name}",
    )
    path = _source_path(name, module, libraries=libraries, lean_root=lean_root, index=index)
    declaration = DeclarationSkeleton(
        name=name,
        kind=str(record.get("kind") or "unknown"),
        module=module,
        path=path,
        start_line=start,
        end_line=end,
        signature=str(record.get("signature") or ""),
        raw_signature=str(record["raw_signature"]),
        semantic=semantic,
        lean_version=str(record["lean_version"]),
        depends=tuple(_strings(record.get("depends"))),
        trusted=tuple(_dependency_order(trusted)),
        assumed=tuple(_strings(record.get("assumed"))),
        assumed_semantics=_semantic_pairs(record.get("assumed_semantics")),
        boundary_modules=_hash_module_files(
            record.get("boundary_modules"),
            lean_root=lean_root,
            cache=module_hashes,
            snapshot_started_ns=snapshot_started_ns,
        ),
        axioms=tuple(_strings(record.get("axioms"))),
        axiom_semantics=_semantic_pairs(record.get("axiom_semantics")),
        source=source,
        statement=statement,
        source_comments=source_comments,
        statement_comments=statement_comments,
        source_withheld=source_withheld,
    )
    return declaration


def _trusted(
    item: dict[str, object],
    *,
    libraries: tuple[LeanLibrary, ...],
    lean_root: Path,
    index: SourceIndex,
) -> TrustedDeclaration:
    name = str(item.get("name") or "")
    semantic = str(item["semantic"])
    module = str(item.get("module") or "")
    start, end = _range(item.get("range"))
    path = _source_path(name, module, libraries=libraries, lean_root=lean_root, index=index)
    source, source_comments, source_withheld = _shown_source(
        _optional_probe_string(item.get("source")), item.get("source_comments"), name=name
    )
    if source is None and start is None and _internal_detail(name):
        source_withheld = True
    trusted = TrustedDeclaration(
        name=name,
        kind=str(item.get("kind") or "unknown"),
        module=module,
        path=path,
        start_line=start,
        end_line=end,
        signature=str(item.get("signature") or ""),
        raw_signature=str(item["raw_signature"]),
        semantic=semantic,
        depends=tuple(_strings(item.get("depends"))),
        source=source,
        source_comments=source_comments,
        source_withheld=source_withheld,
    )
    return trusted


def _optional_probe_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _source_path(
    name: str,
    module: str,
    *,
    libraries: tuple[LeanLibrary, ...],
    lean_root: Path,
    index: SourceIndex,
) -> str | None:
    # The module name is authoritative: it is what Lean compiled. The lexical
    # index is only a fallback for a module whose file the library layout
    # cannot place.
    path = path_of(module, libraries, lean_root) if module else None
    if path is not None:
        return path
    location = index.find(name)
    return None if location is None else location.path.as_posix()


def _range(value: object) -> tuple[int | None, int | None]:
    if isinstance(value, list) and len(value) == 2 and all(isinstance(item, int) for item in value):
        return value[0], value[1]
    return None, None


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _dependency_order(items: list[TrustedDeclaration]) -> list[TrustedDeclaration]:
    """Order trusted declarations so each is read after what it rests on.

    Ties are broken by name, so the order is a function of the sources alone.
    """

    by_name = {item.name: item for item in items}
    order: list[TrustedDeclaration] = []
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in done or name in visiting or name not in by_name:
            return
        visiting.add(name)
        for dependency in sorted(by_name[name].depends):
            visit(dependency)
        visiting.discard(name)
        done.add(name)
        order.append(by_name[name])

    for name in sorted(by_name):
        visit(name)
    return order


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def source_excerpt(item: TrustedDeclaration | DeclarationSkeleton, lean_root: Path) -> str | None:
    """Return the source lines of ``item``, or ``None`` when they cannot be read."""

    if item.path is None or item.start_line is None or item.end_line is None:
        return None
    root = lean_root.resolve()
    candidate = (root / item.path).resolve()
    try:
        candidate.relative_to(root)
        lines = candidate.read_text(encoding="utf-8").split("\n")
    except (ValueError, OSError, UnicodeError):
        return None
    if item.start_line < 1 or item.end_line > len(lines) or item.end_line < item.start_line:
        return None
    return "\n".join(lines[item.start_line - 1 : item.end_line])


def format_report(report: SkeletonReport, *, lean_root: Path | None = None) -> str:
    """Render the report as the text a reviewer reads.

    Source excerpts captured by the probe travel inside the report. The
    ``lean_root`` argument remains for call compatibility and is never reread.
    """

    out: list[str] = []
    for node in report.nodes:
        if len(node.declarations) > 1:
            article = node.hash or "none: a declaration is unresolved"
            out.append(f"## {node.node_id} · article skeleton {article}")
            out.append("")
        for declaration in node.declarations:
            out.append(f"== {node.node_id} · {declaration.kind} {declaration.name}")
            out.extend(f"   {line}" for line in declaration.signature.splitlines())
            if declaration.source is not None:
                out.extend(f"   {line}" for line in declaration.source.splitlines())
            elif declaration.statement is not None:
                out.append("   -- as written:")
                out.extend(f"   {line}" for line in declaration.statement.splitlines())
            if declaration.source_withheld or (declaration.source is None and declaration.statement is None):
                out.append(f"   {_NOT_SHOWN}")
            out.append("")
            out.append(f"   {_trust_summary(declaration)} · skeleton {declaration.hash}")
            if declaration.assumed:
                out.append(f"   assumes: {', '.join(declaration.assumed)}")
            out.append(f"   axioms: {', '.join(declaration.axioms) if declaration.axioms else 'none'}")
            for item in declaration.trusted:
                out.append("")
                out.append(f"   -- {item.kind} {item.name}  ({_where(item)})")
                out.extend(f"   -- {line}" for line in item.signature.splitlines())
                excerpt = item.source
                if excerpt is None:
                    out.extend(f"   {line}" for line in item.signature.splitlines())
                    if item.source_withheld:
                        out.append(f"   {_NOT_SHOWN}")
                else:
                    out.extend(f"   {line}" for line in excerpt.splitlines())
            out.append("")
        if not node.declarations:
            out.append(f"== {node.node_id} · no skeleton")
            out.append("")
    for issue in report.unresolved:
        out.append(f"error: {issue.message}")
    return "\n".join(out).rstrip("\n") + "\n"


PACKET_MANIFEST = "manifest.json"
#: The joint packet of an article's declarations, what a faithfulness judge reads.
ARTICLE_PACKET = "article.lean"


def _output_identity(path: Path) -> tuple[int, int, str] | None:
    """Return the identity of an output path without following its final link."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkeletonError([f"cannot inspect skeleton output {path}: {exc}"]) from exc
    if path.is_symlink():
        raise SkeletonError([f"refusing symlink packet output: {path}"])
    digest = hashlib.sha256()
    if path.is_file():
        try:
            digest.update(b"file\0")
            digest.update(path.read_bytes())
        except OSError as exc:
            raise SkeletonError([f"cannot inspect skeleton output {path}: {exc}"]) from exc
        return metadata.st_dev, metadata.st_ino, digest.hexdigest()
    if not path.is_dir():
        raise SkeletonError([f"skeleton output is not a regular file or directory: {path}"])
    digest.update(b"directory\0")
    try:
        for child in sorted(path.rglob("*"), key=lambda candidate: candidate.relative_to(path).as_posix()):
            relative = child.relative_to(path).as_posix()
            if child.is_symlink():
                raise SkeletonError([f"refusing symlink packet output: {child}"])
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            if child.is_dir():
                digest.update(b"directory\0")
            elif child.is_file():
                digest.update(b"file\0")
                digest.update(child.read_bytes())
                digest.update(b"\0")
            else:
                raise SkeletonError([f"refusing special file in packet output: {child}"])
    except OSError as exc:
        raise SkeletonError([f"cannot inspect skeleton output {path}: {exc}"]) from exc
    return metadata.st_dev, metadata.st_ino, digest.hexdigest()


def _validate_managed_output(
    path: Path,
    *,
    kind: str,
    schema: str | None = None,
) -> tuple[int, int, str] | None:
    identity = _output_identity(path)
    if identity is None:
        return identity
    if not path.is_dir():
        raise SkeletonError([f"packet output exists and is not a directory: {path}"])
    try:
        if not any(path.iterdir()):
            return identity
    except OSError as exc:
        raise SkeletonError([f"cannot inspect skeleton output {path}: {exc}"]) from exc
    manifest = path / PACKET_MANIFEST
    if manifest.is_symlink() or not manifest.is_file():
        raise SkeletonError([f"refusing to overwrite non-Autoform packet output: {path}"])
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkeletonError([f"refusing to overwrite non-Autoform packet output: {path}"]) from exc
    if not isinstance(payload, dict):
        raise SkeletonError([f"refusing to overwrite non-Autoform packet output: {path}"])
    entries = payload.get(kind)
    schema_matches = (
        payload.get("schema") == schema
        if schema is not None
        else (kind, payload.get("schema")) in MANAGED_OUTPUT_SCHEMAS
    )
    if (
        payload.get("kind") != kind
        or not schema_matches
        or not isinstance(entries, list)
    ):
        raise SkeletonError([f"refusing to overwrite non-Autoform packet output: {path}"])
    return identity


def validate_managed_output(
    path: Path,
    *,
    kind: str,
    schema: str | None = None,
) -> tuple[int, int, str] | None:
    """Validate an output tree against its exact producer schema."""

    return _validate_managed_output(path, kind=kind, schema=schema)


def _safe_node_path(node_id: str) -> Path:
    path = Path(*node_id.split("/"))
    if (
        not node_id
        or path.is_absolute()
        or path.as_posix() != node_id
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SkeletonError([f"unsafe article id in packet output: {node_id!r}"])
    return path


def declaration_filename(name: str, *, suffix: str = ".lean") -> str:
    """Return a portable, collision-resistant filename for a Lean name."""

    if not name:
        raise ValueError("a declaration name cannot be empty")
    if not suffix.startswith(".") or any(character in suffix for character in "/\\\0\r\n"):
        raise ValueError(f"invalid declaration filename suffix: {suffix!r}")
    pieces: list[str] = []
    for character in name:
        if character.isascii() and (character.isalnum() or character in {".", "_", "-"}):
            pieces.append(character)
        else:
            pieces.extend(f"%{byte:02X}" for byte in character.encode("utf-8"))
    encoded = "".join(pieces)
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    if not encoded or len(os.fsencode(encoded)) > 180:
        encoded = "declaration"
    return f"{encoded}--{digest}{suffix}"


def _packet_filename(name: str) -> str:
    return declaration_filename(name)


def _stage_output(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    try:
        if not destination.is_symlink() and destination.is_dir():
            existing_mode = destination.stat().st_mode & 0o7777
    except OSError as exc:
        raise SkeletonError([f"cannot inspect skeleton output {destination}: {exc}"]) from exc
    for _ in range(100):
        stage = destination.with_name(
            f".{destination.name}.autoform-stage-{secrets.token_hex(8)}"
        )
        try:
            stage.mkdir()
        except FileExistsError:
            continue
        try:
            if existing_mode is not None:
                stage.chmod(existing_mode)
        except OSError:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return stage
    raise SkeletonError([f"cannot allocate staging directory beside {destination}"])


def stage_managed_output(destination: Path) -> Path:
    """Create a transaction stage beside a managed output directory."""

    return _stage_output(destination)


def _stage_report_output(
    report: SkeletonReport,
    destination: Path,
) -> tuple[Path, tuple[int, int, str] | None]:
    """Write a report to a same-directory stage and capture the old identity."""

    identity = _output_identity(destination)
    existing_mode: int | None = None
    if identity is not None:
        if not destination.is_file():
            raise SkeletonError([f"report output exists and is not a regular file: {destination}"])
        existing_mode = destination.stat().st_mode & 0o7777
    destination.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        stage = destination.with_name(
            f".{destination.name}.autoform-stage-{secrets.token_hex(8)}"
        )
        try:
            with stage.open("x", encoding="utf-8") as stream:
                stream.write(report.to_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            if existing_mode is not None:
                stage.chmod(existing_mode)
        except FileExistsError:
            continue
        except BaseException:
            stage.unlink(missing_ok=True)
            raise
        return stage, identity
    raise SkeletonError([f"cannot allocate staging file beside {destination}"])


def _remove_output(path: Path) -> None:
    """Remove one transaction-owned file or directory without following links."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename ``source`` only when ``destination`` is absent."""

    if os.name == "nt":  # pragma: no cover - Windows-specific path
        os.rename(source, destination)
        return

    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform.startswith("linux"):
        try:
            rename = library.renameat2
        except AttributeError as exc:
            raise SkeletonError(
                ["atomic no-replace rename is unavailable on this Linux system"]
            ) from exc
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, destination_bytes, 1)
    elif sys.platform == "darwin":
        try:
            rename = library.renamex_np
        except AttributeError as exc:
            raise SkeletonError(
                ["atomic no-replace rename is unavailable on this macOS system"]
            ) from exc
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(source_bytes, destination_bytes, 0x00000004)
    else:
        raise SkeletonError(
            [f"atomic no-replace rename is unsupported on {sys.platform}"]
        )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _install_output(stage: Path, destination: Path) -> None:
    """Install a stage without overwriting a concurrent output."""

    metadata = stage.lstat()
    if stat.S_ISREG(metadata.st_mode):
        os.link(stage, destination)
        stage.unlink()
    else:
        _rename_no_replace(stage, destination)


def _preflight_output_installs(
    outputs: list[tuple[Path, Path, tuple[int, int, str] | None]],
) -> None:
    """Prove each destination filesystem supports its artifact install."""

    checked: set[tuple[int, int]] = set()
    for destination, stage, _ in outputs:
        try:
            stage_metadata = stage.lstat()
            device = destination.parent.stat().st_dev
        except OSError as exc:
            raise SkeletonError([f"cannot inspect skeleton output stage {stage}: {exc}"]) from exc
        artifact_kind = stat.S_IFMT(stage_metadata.st_mode)
        if artifact_kind not in {stat.S_IFREG, stat.S_IFDIR}:
            raise SkeletonError([f"skeleton output stage is not a file or directory: {stage}"])
        key = (device, artifact_kind)
        if key in checked:
            continue
        checked.add(key)
        probe_root = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.autoform-preflight-",
                dir=destination.parent,
            )
        )
        source = probe_root / "source"
        target = probe_root / "target"
        try:
            if artifact_kind == stat.S_IFREG:
                source.touch(exist_ok=False)
            else:
                source.mkdir()
            _install_output(source, target)
        except BaseException:
            try:
                _remove_output(probe_root)
            except OSError as cleanup_exc:
                warnings.warn(
                    f"could not remove output preflight artifacts at {probe_root}: {cleanup_exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            raise
        try:
            _remove_output(probe_root)
        except OSError as cleanup_exc:
            raise SkeletonError(
                [f"could not remove output preflight artifacts at {probe_root}: {cleanup_exc}"]
            ) from cleanup_exc


def _replace_outputs(
    outputs: list[tuple[Path, Path, tuple[int, int, str] | None]],
) -> None:
    """Commit staged outputs with rollback, but not cross-path linearizability.

    Each rename is atomic. Readers can still observe the interval between the
    renames; avoiding that requires one shared generation pointer rather than
    another rollback branch.
    """

    _preflight_output_installs(outputs)
    for destination, _, identity in outputs:
        if _output_identity(destination) != identity:
            raise SkeletonError([f"packet output changed during publication: {destination}"])
    backups: dict[Path, tuple[Path, tuple[int, int, str]]] = {}
    installs: dict[Path, tuple[Path, tuple[int, int, str]]] = {}
    try:
        for destination, _, identity in outputs:
            if identity is None:
                continue
            backup = destination.with_name(
                f".{destination.name}.autoform-backup-{secrets.token_hex(8)}"
            )
            backups[destination] = (backup, identity)
            os.replace(destination, backup)
            if _output_identity(backup) != identity:
                raise SkeletonError([f"packet output changed during publication: {destination}"])
        for destination, stage, _ in outputs:
            stage_identity = _output_identity(stage)
            if stage_identity is None:
                raise SkeletonError([f"skeleton output stage disappeared: {stage}"])
            installs[destination] = (stage, stage_identity)
            _install_output(stage, destination)
    except BaseException as exc:
        conflicts: set[Path] = set()
        rollback_issues: list[str] = []
        for destination, (stage, expected) in reversed(installs.items()):
            try:
                stage_identity = _output_identity(stage)
                destination_identity = _output_identity(destination)
                if stage_identity == expected and destination_identity is None:
                    continue
                if destination_identity != expected:
                    if destination_identity is None:
                        continue
                    conflicts.add(destination)
                    rollback_issues.append(
                        f"published output changed during rollback and was preserved at {destination}"
                    )
                    continue
                quarantine = destination.with_name(
                    f".{destination.name}.autoform-rollback-{secrets.token_hex(8)}"
                )
                os.replace(destination, quarantine)
                try:
                    quarantine_identity = _output_identity(quarantine)
                    if quarantine_identity != expected:
                        rollback_issues.append(
                            f"changed rolled-back output was preserved for recovery at {quarantine}"
                        )
                        continue
                    _remove_output(quarantine)
                except (OSError, SkeletonError) as cleanup_exc:
                    rollback_issues.append(
                        f"could not remove rolled-back output preserved for recovery at {quarantine}: {cleanup_exc}"
                    )
            except (OSError, SkeletonError) as rollback_exc:
                if _output_identity(destination) is not None:
                    conflicts.add(destination)
                rollback_issues.append(
                    f"could not roll back skeleton output {destination}: {rollback_exc}"
                )
        for destination, (backup, expected) in backups.items():
            if destination in conflicts:
                rollback_issues.append(f"previous output was preserved for recovery at {backup}")
                continue
            try:
                backup_identity = _output_identity(backup)
                destination_identity = _output_identity(destination)
                if backup_identity is not None and destination_identity is None:
                    _install_output(backup, destination)
                elif backup_identity == expected:
                    rollback_issues.append(f"previous output was preserved for recovery at {backup}")
                elif backup_identity is None and destination_identity == expected:
                    continue
                elif backup_identity is not None:
                    rollback_issues.append(f"changed backup was preserved for recovery at {backup}")
                else:
                    rollback_issues.append(
                        f"could not find previous skeleton output for {destination} during rollback"
                    )
            except (OSError, SkeletonError) as rollback_exc:
                rollback_issues.append(
                    f"could not restore skeleton output {destination}; previous output remains at {backup}: {rollback_exc}"
                )
        if not isinstance(exc, (OSError, SkeletonError)):
            raise
        issues = list(exc.issues) if isinstance(exc, SkeletonError) else [f"could not publish skeleton output: {exc}"]
        raise SkeletonError([*issues, *rollback_issues]) from exc
    for backup, expected in backups.values():
        try:
            backup_identity = _output_identity(backup)
            if backup_identity is None:
                warnings.warn(
                    f"skeleton output backup disappeared before cleanup: {backup}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue
            if backup_identity != expected:
                warnings.warn(
                    f"skeleton output backup changed before cleanup and was preserved at {backup}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue
            _remove_output(backup)
        except (OSError, SkeletonError) as cleanup_exc:
            warnings.warn(
                f"could not remove previous skeleton output preserved at {backup}: {cleanup_exc}",
                RuntimeWarning,
                stacklevel=2,
            )


def replace_managed_outputs(
    outputs: list[tuple[Path, Path, tuple[int, int, str] | None]],
) -> None:
    """Publish staged managed outputs with the skeleton transaction protocol."""

    _replace_outputs(outputs)


def _paths_overlap(first: Path, second: Path) -> bool:
    first_resolved = first.resolve()
    second_resolved = second.resolve()
    return (
        first_resolved == second_resolved
        or first_resolved in second_resolved.parents
        or second_resolved in first_resolved.parents
    )


def write_skeleton_report(report: SkeletonReport, destination: str | Path) -> Path:
    """Failure-atomically replace one JSON skeleton report."""

    requested = Path(destination).expanduser()
    if requested.is_symlink():
        raise SkeletonError([f"refusing symlink report output: {requested}"])
    output = Path(os.path.abspath(requested))
    stage: Path | None = None
    try:
        stage, identity = _stage_report_output(report, output)
        _replace_outputs([(output, stage, identity)])
        stage = None
    except OSError as exc:
        raise SkeletonError([f"could not prepare skeleton report output: {exc}"]) from exc
    finally:
        if stage is not None:
            try:
                _remove_output(stage)
            except OSError:
                pass
    return output


def write_packets(
    report: SkeletonReport,
    directory: str | Path,
    *,
    passages: str | Path | None = None,
    report_path: str | Path | None = None,
) -> list[Path]:
    """Write one blind packet per skeleton for independent read-back auditors.

    Each packet holds only what the auditor may see: the comment-stripped
    skeleton. Nothing names the article, the source, or the intent. The
    manifest beside the packets maps each file back to its article and hash so
    the read-backs can be filed and checked, without the auditor reading it.

    With ``passages``, the source passage each article cites is written to a
    second directory, one ``passage.txt`` per article. A faithfulness judge
    gets a packet and its passage; a read-back auditor gets the packet alone,
    which is why the two never share a directory.

    With ``report_path``, the JSON report joins the same failure-atomic
    publication transaction.
    """

    if not report.clean:
        raise SkeletonError(
            ["refusing to publish review packets from an incomplete skeleton report"]
        )

    requested_root = Path(directory).expanduser()
    if requested_root.is_symlink():
        raise SkeletonError([f"refusing symlink packet output: {requested_root}"])
    root = Path(os.path.abspath(requested_root))
    requested_passages = Path(passages).expanduser() if passages is not None else None
    if requested_passages is not None and requested_passages.is_symlink():
        raise SkeletonError([f"refusing symlink packet output: {requested_passages}"])
    passages_root = (
        Path(os.path.abspath(requested_passages)) if requested_passages is not None else None
    )
    if passages_root is not None and _paths_overlap(root, passages_root):
        raise SkeletonError(["packet and passage output directories must be disjoint"])
    requested_report = Path(report_path).expanduser() if report_path is not None else None
    if requested_report is not None and requested_report.is_symlink():
        raise SkeletonError([f"refusing symlink report output: {requested_report}"])
    report_destination = (
        Path(os.path.abspath(requested_report)) if requested_report is not None else None
    )
    if report_destination is not None and (
        _paths_overlap(root, report_destination)
        or (passages_root is not None and _paths_overlap(passages_root, report_destination))
    ):
        raise SkeletonError(["report output must be disjoint from packet and passage directories"])
    root_identity = _validate_managed_output(root, kind="packets")
    passages_identity = (
        _validate_managed_output(passages_root, kind="passages")
        if passages_root is not None
        else None
    )
    packet_stage: Path | None = None
    passages_stage: Path | None = None
    report_stage: Path | None = None
    report_identity: tuple[int, int, str] | None = None
    written: list[Path] = []
    manifest: list[dict[str, str]] = []
    passage_manifest: list[dict[str, str]] = []
    try:
        packet_stage = _stage_output(root)
        passages_stage = _stage_output(passages_root) if passages_root is not None else None
        if report_destination is not None:
            report_stage, report_identity = _stage_report_output(report, report_destination)
        for node in report.nodes:
            node_path = _safe_node_path(node.node_id)
            passage_path: str | None = None
            if passages_stage is not None and node.passage is not None:
                passage_relative = node_path / "passage.txt"
                target = passages_stage / passage_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                passage_bytes = (node.passage + "\n").encode("utf-8")
                target.write_bytes(passage_bytes)
                passage_path = passage_relative.as_posix()
                passage_manifest.append(
                    {
                        "hash": _sha256_id(passage_bytes),
                        "node_id": node.node_id,
                        "passage": passage_path,
                        "review_hash": node.review_hash,
                    }
                )
            if node.declarations:
                article_relative = node_path / ARTICLE_PACKET
                article = packet_stage / article_relative
                article.parent.mkdir(parents=True, exist_ok=True)
                article.write_text(node.blind_text(), encoding="utf-8")
                written.append(root / article_relative)
            for declaration in node.declarations:
                relative = node_path / _packet_filename(declaration.name)
                path = packet_stage / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(declaration.blind_text(), encoding="utf-8")
                written.append(root / relative)
                entry = {
                    "article_packet": (node_path / ARTICLE_PACKET).as_posix(),
                    "article_packet_hash": node.evidence_hash,
                    "declaration": declaration.name,
                    "hash": declaration.hash,
                    "node_id": node.node_id,
                    "packet": relative.as_posix(),
                    "packet_hash": declaration.evidence_hash,
                    "review_hash": node.review_hash,
                }
                if passage_path is not None:
                    entry["passage"] = passage_path
                    entry["passage_locator"] = node.passage_locator or ""
                manifest.append(entry)
        (packet_stage / PACKET_MANIFEST).write_text(
            json.dumps(
                {"kind": "packets", "packets": manifest, "schema": PACKET_SCHEMA},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if passages_stage is not None:
            (passages_stage / PACKET_MANIFEST).write_text(
                json.dumps(
                    {
                        "kind": "passages",
                        "passages": passage_manifest,
                        "schema": PASSAGE_SCHEMA,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        outputs = [(root, packet_stage, root_identity)]
        if passages_root is not None and passages_stage is not None:
            outputs.append((passages_root, passages_stage, passages_identity))
        if report_destination is not None and report_stage is not None:
            outputs.append((report_destination, report_stage, report_identity))
        _replace_outputs(outputs)
        packet_stage = None
        passages_stage = None
        report_stage = None
    except OSError as exc:
        raise SkeletonError([f"could not prepare skeleton output: {exc}"]) from exc
    finally:
        for stage in (packet_stage, passages_stage, report_stage):
            if stage is not None:
                try:
                    _remove_output(stage)
                except OSError:
                    pass
    return written


def _trust_summary(declaration: DeclarationSkeleton) -> str:
    count = len(declaration.trusted)
    noun = "declaration" if count == 1 else "declarations"
    summary = f"trusts {count} local {noun}, {_plural(declaration.skeleton_lines, 'line')} to read"
    if declaration.declaration_lines:
        summary += f"; the declaration itself spans {_plural(declaration.declaration_lines, 'line')}"
    return summary


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _where(item: TrustedDeclaration) -> str:
    location = item.path or item.module or "?"
    if item.start_line is not None and item.end_line is not None:
        span = f"{item.start_line}" if item.start_line == item.end_line else f"{item.start_line}-{item.end_line}"
        return f"{location}:{span}"
    return location


__all__ = [
    "ARTICLE_PACKET",
    "DEFAULT_FRESHNESS_TIMEOUT",
    "DEFAULT_PROBE_TIMEOUT",
    "PROBE_MARKER",
    "SEMANTIC_SCHEMA",
    "SKELETON_SCHEMA",
    "DeclarationSkeleton",
    "LeanLibrary",
    "NodeSkeleton",
    "PACKET_MANIFEST",
    "ProbeRunner",
    "SkeletonError",
    "SkeletonReport",
    "TrustedDeclaration",
    "blueprint_hash",
    "declaration_filename",
    "evidence_hash_of",
    "extract_graph_skeletons",
    "extract_skeletons",
    "format_report",
    "lean_libraries",
    "load_skeleton_report",
    "module_of",
    "parse_probe_output",
    "path_of",
    "render_probe",
    "replace_managed_outputs",
    "run_probe",
    "source_excerpt",
    "source_passage",
    "stage_managed_output",
    "validate_managed_output",
    "write_packets",
    "write_skeleton_report",
]
