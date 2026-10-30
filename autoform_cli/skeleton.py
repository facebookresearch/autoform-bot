"""Extract the trusted surface of each formalized result: its skeleton.

The skeleton is the reading list a person must check to agree that a Lean
declaration says what the blueprint claims: its statement and, transitively,
every project definition that statement rests on. Proofs are the kernel's
problem. The closure comes from elaborated terms, read back from a small probe
run with ``lake env lean``, never from source text. The output is deterministic
and has project paths relative to the project; a failure reason quotes other
paths as Lean printed them. Nothing here writes into the vault. The user-facing
contract is the ``autoform skeleton`` section of ``autoform_cli/README.md``;
the mechanics below are what it leaves out.

Probe. For each module that declares a selected target, the probe imports that
module and the helper module ``autoform-skeleton-helper`` (compiled with the
project's toolchain and no project module in scope, its constants all in the
``AutoformSkeleton`` namespace) and makes one fully qualified call, with no
``open`` or ``set_option``. It sets exactly two printing options,
``pp.funBinderTypes`` and ``pp.coercions.types``, so a reader sees what each
binder ranges over and where each cast lands. A lexical closure would miss what
``open``, notation, implicit instances, and auto-bound variables bring in, and
every miss would silently shrink the trusted surface. Each name is probed in the
module whose source the lexical index finds it in; a name with one public
declaration beside private ones is probed in the public one's module. An error
the probe meets while reading one declaration leaves only that declaration
unresolved. A probe fails on its own with a nonzero exit, a signal, a timeout,
or malformed or ambiguous records. A declaration's packet reads as it does to a
file that imports its module: it has the module's global notation, instances,
and attributes, including any the module declares after the declaration, but
not its ``local notation``, local instances, or the ``open`` and
``set_option`` commands in effect at the declaration. The output and material
limits apply to a whole probe, while a declaration that resolves has the same
hash under any selection.

Folding. Only companions that Lean's environment records as generated are
folded onto their parent; an internal-looking name alone is not proof of
provenance. Rangeless internal-detail declarations, such as ``f._unary``,
``f._f``, and ``S.x._default``, are bound by their elaborated material and
marked ``-- source not shown`` rather than passed off as their parent's source.
A structure appears once, as its ``structure`` block. ``partial def`` is
detected from the environment, not the source text; it is refused because its
kernel face is an opaque constant.

Report. Declarations in one project share most of what they trust, so the
report states each boundary module's identity once, in ``boundary_modules``,
and each trusted declaration's and external constant's semantic material once
per root module that reaches it, in ``trusted`` and ``semantics``, keyed by
root module, then name: a name means what the root module's environment
declares under it. A report therefore grows with the number of root modules
times the material they share. The probe states each elaborated subterm of 256
bytes or more once, since proof terms repeat large subterms; the report keeps
each material's full text. Two probes that disagree about one module's files
stop the command. The report identifies the blueprint by a hash of every
article's bytes and of the passage each article cites; a report whose
selection, articles, and unresolved declarations disagree cannot be built.
Review evidence needs a report that selects every article and whose targets are
the blueprint's ``lean:`` names. It quotes each trusted definition's source and
records theorem dependencies by elaborated signature.

Resources. ``lean_probe`` runs the probes. Its docstring states their CPU,
time, output, and process bounds and how a failure reason reads. The parsed
records, at most 512 Mi characters of semantic material per probed module,
stay in memory until the report is built.

Hashes. The drift hash takes macro expansion, synthesized instance bodies,
types, and definition bodies, but not local source spelling or comments; proof
axioms, toolchain changes, and unrelated edits in an external module or its
imports may also rotate it. It compares reports across builds. For the
statement hash the probe closes over the statement first and then over the
axioms' types; the statement's trusted declarations are those the root's
``depends`` reach, and the packet, which shows only them, has no ``-- axioms:``
line. A spelling-only edit changes only the packet.

Drift hash boundary. The compiled identities are every part a module-system
build writes for each module of the transitive external boundary. Only modules
whose ``.olean`` lies in the toolchain's own ``lib/lean`` count as core and
stay outside the boundary; a dependency module named ``Lake.Foo`` is external
like any other. Compiler-generated matcher and recursor bodies stay in the hash
although they are folded out of the reading list.

Packets. Every item's signature is also printed raw, bypassing project
notation, unexpanders, and custom delaborators, so an ``infixl " + " =>
HMul.hMul`` cannot make a product read as a sum in any signature. A
definition's body, a definition root's own source included, is shown as
written, where such notation still applies; for those, and for structures and
generated companions, only the canonical kernel material states the meaning
without notation. A theorem's statement as written is cut before a ``:=``
value or a structure-style ``where`` value by Lean's parser, with its enclosing
namespaces and the file's opened namespaces in scope; a proof Lean cannot
parse does not stop the cut. Its comments are found by parsing the statement
alone with ``:= sorry`` as its value, so editing only the proof changes neither
the statement shown nor what is stripped from it, unless it makes an
equation-style form parse or stop parsing outside its file. The printed
signature shows binders that ``variable`` and ``include`` inject and the type
every cast lands in; the written form shows what the pretty-printer elides.

Outputs. ``--packets``, ``--passages``, and ``--output`` replace their trees
through temporary files, hard links, and atomic renames, so a concurrent change
detected before commit aborts publication instead of being overwritten.
Combined, they are staged together and a failed commit restores the previous
set: failure atomicity, not simultaneous visibility, since each rename is
atomic on its own.

Passages. A ``## Sources`` link with a ``#L<start>-L<end>`` fragment that names
a regular Markdown file in the blueprint, in any letter case, is a note; any
other such local link is a locator, whatever its suffix, and external URLs are
skipped. A first local locator names no text when it points outside the
blueprint; names an invalid path, a symlink loop, a link that cannot be read,
or a missing, unreadable, oversized or non-regular file; or names non-UTF-8
text or no lines of its file.

Comment stripping. Lean's parser locates comments, parsing each source in its
root module's environment. A source is parsed with the tokens of a file that imports
its root module and the scoped notation of the namespaces the declaration sits
in and the file opens above it, but not the file's ``local`` notation. Source
with a known non-builtin token containing ``--`` or ``/-`` is withheld unless
that token was globally active through an import of the source's own module,
since the probe cannot reconstruct when a same-module token was declared or
where a scoped token was active. What the probe cannot see, and so can get
wrong (showing code as a comment or a comment as code): a ``local`` token; a
token without ``--`` or ``/-`` that still moves where a comment or a string
starts (with ``++"`` a token, ``x ++" -- y "`` holds a comment, and without it
a string), when the probe has it and the file did not have it there, as for one
declared later in the same module, or the reverse, as for a scoped token the
file activated in a way the probe does not reconstruct; and, under the module
system, a token that only a private import's imports declare, which Lean does
not load for the file.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from dataclasses import field as dataclass_field
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from . import lean_probe
from ._fs import resolved_path
from ._managed_output import (
    PACKET_MANIFEST,
    output_identity,
    remove_output,
    replace_outputs,
    stage_output,
    validate_managed_output,
)
from .graph import Graph, GraphValidationError, Node, load_graph
from .lean import (
    PACKET_SCHEMA,
    PASSAGE_SCHEMA,
    LeanSourceError,
    SourceIndex,
    declaration_names,
    index_failure_message,
    index_project,
    resolve_lean_root,
)
from .lean_probe import (
    DEFAULT_PROBE_TIMEOUT,
    LeanLibrary,
    SkeletonError,
    lean_libraries,
    lean_name_parts,
    module_of,
    path_of,
    project_control_snapshot,
    render_probe,
    run_module_probes,
)

SKELETON_SCHEMA = "autoform-skeleton/v5"
SEMANTIC_SCHEMA = "autoform-lean-expr/v4"

#: The probe states shared subterms once; this bounds the characters of
#: semantic material they may expand to in one probe.
_PROBE_MATERIAL_LIMIT = 512 * 1024 * 1024
#: Compiled parts that identify a boundary module. Module-system builds also
#: write the server and private parts next to the `.olean`.
_MODULE_FILE_KINDS = ("olean", "olean.server", "olean.private")

#: A callable that runs a probe and returns Lean's standard output. The default
#: shells out to ``lake env lean``; tests substitute a fake.
ProbeRunner = Callable[[str, Path], str]


#: Shown when no source can be attributed and parsed safely. The signatures
#: and canonical kernel material above it still state the meaning.
_NOT_SHOWN = "-- source not shown: no reliable standalone source is available"

#: A report's ``trusted``, ``semantics``, and ``boundary_modules`` tables.
_Tables = tuple[dict[str, dict[str, object]], dict[str, dict[str, object]], dict[str, object]]


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
        shown = asdict(self)
        shown["depends"] = list(self.depends)
        shown["source_comments"] = [list(item) for item in self.source_comments]
        return shown


@dataclass(frozen=True, slots=True, kw_only=True)
class DeclarationSkeleton(TrustedDeclaration):
    """The trusted surface of one root declaration.

    ``source`` is the declaration's own source when it is a definition, whose
    body is its meaning; a theorem's source holds its proof, which is never
    shown. ``source_withheld`` also covers a written statement that cannot
    safely be shown.
    """

    lean_version: str
    trusted: tuple[TrustedDeclaration, ...]
    assumed: tuple[str, ...]
    assumed_semantics: tuple[tuple[str, str], ...]
    boundary_modules: tuple[tuple[str, str, str], ...]
    axioms: tuple[str, ...]
    axiom_semantics: tuple[tuple[str, str], ...]
    #: The part of ``assumed`` the statement reaches, and the modules that bind
    #: it; what only the proof's axioms reach is in the full fields alone.
    statement_assumed: tuple[str, ...]
    statement_boundary_modules: tuple[str, ...]
    #: The statement as written in the source, cut before its value. The
    #: elaborated signature is authoritative; this is what the author typed,
    #: shown beside it so neither form can hide what the other shows.
    statement: str | None = None
    #: UTF-8 byte ranges of the comments in ``statement``.
    statement_comments: tuple[tuple[int, int], ...] = ()
    #: Hashes and packet text, each computed once: a report states them
    #: several times, and each fingerprint encodes the whole trusted closure.
    _cache: dict[str, str] = dataclass_field(default_factory=dict, init=False, repr=False, compare=False)

    @property
    def declaration_lines(self) -> int:
        """Source lines of the root declaration itself, proof included."""
        if self.start_line is None or self.end_line is None:
            return 0
        return self.end_line - self.start_line + 1

    @property
    def skeleton_lines(self) -> int:
        """Lines of the trust surface: the signature plus every trusted span,
        including those only the proof's axioms reach, which the packet omits."""
        return self.lines + sum(item.lines for item in self.trusted)

    def __post_init__(self) -> None:
        modules = {module for module, _, _ in self.boundary_modules}
        if (
            not set(self.statement_assumed) <= set(self.assumed)
            or not set(self.statement_boundary_modules) <= modules
            or (self.statement_assumed and not self.statement_boundary_modules)
        ):
            raise SkeletonError([f"mismatched statement closure for {self.name}"])

    @property
    def statement_trusted(self) -> tuple[TrustedDeclaration, ...]:
        """The trusted declarations the statement reaches through ``depends``;
        the others only the proof's axioms reach."""

        by_name = {item.name: item for item in self.trusted}
        reached: set[str] = set()
        work = list(self.depends)
        while work:
            name = work.pop()
            if name in by_name and name not in reached:
                reached.add(name)
                work += by_name[name].depends
        return tuple(item for item in self.trusted if item.name in reached)

    @property
    def hash(self) -> str:
        """Fingerprint the probe's canonical elaborated meaning and trust boundary."""

        if "hash" not in self._cache:
            modules = {module for module, _, _ in self.boundary_modules}
            self._cache["hash"] = self._fingerprint(self.trusted, self.assumed, modules, axioms=True)
        return self._cache["hash"]

    @property
    def statement_hash(self) -> str:
        """Fingerprint what the statement means: :attr:`hash` without the axioms
        and what only they reach. Unlike :attr:`hash`, editing only a theorem's
        proof, ``sorry`` to a proof included, leaves it unchanged."""

        if "statement_hash" not in self._cache:
            self._cache["statement_hash"] = self._fingerprint(
                self.statement_trusted, self.statement_assumed, set(self.statement_boundary_modules), axioms=False
            )
        return self._cache["statement_hash"]

    def _fingerprint(
        self, trusted: tuple[TrustedDeclaration, ...], assumed: tuple[str, ...], modules: set[str], *, axioms: bool
    ) -> str:
        names = set(assumed)
        material = {
            "assumed": list(assumed),
            "assumed_semantics": [list(item) for item in self.assumed_semantics if item[0] in names],
            "boundary_modules": [list(item) for item in self.boundary_modules if item[0] in modules],
            "depends": list(self.depends),
            "kind": self.kind,
            "lean_version": self.lean_version,
            "name": self.name,
            "semantic": self.semantic,
            "semantic_schema": SEMANTIC_SCHEMA,
            "trusted": [[item.name, item.kind, item.semantic, list(item.depends)] for item in trusted],
        }
        if axioms:
            material |= {"axioms": list(self.axioms), "axiom_semantics": [list(item) for item in self.axiom_semantics]}
        return _sha256_id(json.dumps(material, sort_keys=True, ensure_ascii=False).encode())

    @property
    def evidence_hash(self) -> str:
        """Fingerprint the exact proof-free text presented to a reviewer."""

        if "evidence_hash" not in self._cache:
            self._cache["evidence_hash"] = _sha256_id(self.blind_text().encode("utf-8"))
        return self._cache["evidence_hash"]

    def blind_text(self) -> str:
        """The skeleton with every comment removed, for an auditor who must not see intent.

        Testimony about what the code literally asserts is only evidence if
        its author did not know what the code was meant to say. Docstrings say
        exactly that, so they are stripped along with every other comment.
        Names stay: they are part of the code.

        It shows what the statement rests on, never what only the proof's
        axioms reach, so editing a theorem's proof leaves the packet unchanged
        unless it decides whether an equation-style statement can be cut at all.
        """

        if "packet" not in self._cache:
            self._cache["packet"] = self._packet()
        return self._cache["packet"]

    def _packet(self) -> str:
        lines = [
            f"-- {self.kind} {self.name}",
            f"-- assumed from libraries: {', '.join(self.statement_assumed) or 'none'}",
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
        for item in self.statement_trusted:
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

        shown = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.init and not item.name.endswith("_semantics")
        }
        shown.update(
            {
                "assumed": list(self.assumed),
                "statement_assumed": list(self.statement_assumed),
                "statement_boundary_modules": list(self.statement_boundary_modules),
                "axioms": list(self.axioms),
                "depends": list(self.depends),
                "source_comments": [list(item) for item in self.source_comments],
                "statement_comments": [list(item) for item in self.statement_comments],
            }
        )
        return shown | {
            "boundary_modules": list(dict.fromkeys(module for module, _, _ in self.boundary_modules)),
            "declaration_lines": self.declaration_lines,
            "evidence_hash": self.evidence_hash,
            "hash": self.hash,
            "skeleton_lines": self.skeleton_lines,
            "statement_hash": self.statement_hash,
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
    _cache: dict[str, str] = dataclass_field(default_factory=dict, init=False, repr=False, compare=False)

    def blind_text(self) -> str:
        """The article's declarations as one blind packet, for a faithfulness judge.

        A source theorem is often formalized by several declarations together,
        an existence half and a uniqueness half, say. Judged one at a time each
        is honestly incomplete; judged together they are the statement. An
        auditor asked what one declaration asserts still gets that declaration
        alone.
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

        if "evidence_hash" not in self._cache:
            self._cache["evidence_hash"] = _sha256_id(self.blind_text().encode("utf-8"))
        return self._cache["evidence_hash"]

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
        return asdict(self)


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
    #: The shared tables, built once when the report is checked.
    _tables: _Tables = dataclass_field(init=False, repr=False, compare=False)

    @property
    def clean(self) -> bool:
        expected = {(target, name) for target, names in self.targets if target in self.selected_nodes for name in names}
        actual = {(node.node_id, declaration.name) for node in self.nodes for declaration in node.declarations}
        return not self.unresolved and actual == expected

    def node(self, node_id: str) -> NodeSkeleton | None:
        """Return the skeleton record of one article, if it has one."""

        return next((node for node in self.nodes if node.node_id == node_id), None)

    def declarations(self, node_id: str) -> tuple[DeclarationSkeleton, ...]:
        """Return the skeletons behind one article, or none."""

        node = self.node(node_id)
        return () if node is None else node.declarations

    def __post_init__(self) -> None:
        issue = self._incoherence()
        if issue is not None:
            raise SkeletonError([f"skeleton report {issue}"])
        object.__setattr__(self, "_tables", self._shared_tables())

    def _incoherence(self) -> str | None:
        """Why the selection, articles, and unresolved targets disagree, if they do.

        Every report, however built, names its complete target set, selects
        articles from it, holds exactly those articles, and lists as
        unresolved exactly the selected targets it holds no skeleton for.
        """

        if self.selection not in ("all", "filtered"):
            return "has an invalid selection mode"
        target_ids = tuple(node_id for node_id, _ in self.targets)
        if tuple(sorted(set(target_ids))) != target_ids or not all(
            declarations and len(set(declarations)) == len(declarations) for _, declarations in self.targets
        ):
            return "contains an invalid target set"
        selected = self.selected_nodes
        if tuple(sorted(selected)) != selected or not set(selected) <= set(target_ids):
            return "contains an invalid skeleton article selection"
        if self.selection == "all" and self.selected_nodes != target_ids:
            return "contains an incomplete all-article selection"
        if self.selection == "filtered" and not self.selected_nodes:
            return "contains an empty filtered article selection"
        node_ids = tuple(node.node_id for node in self.nodes)
        if len(set(node_ids)) != len(node_ids):
            return "contains duplicate skeleton article ids"
        if node_ids != self.selected_nodes:
            return "does not contain exactly its selected articles"
        declarations_by_node = dict(self.targets)
        actual: set[tuple[str, str]] = set()
        for node in self.nodes:
            names = {declaration.name for declaration in node.declarations}
            if not names <= set(declarations_by_node[node.node_id]):
                return f"contains an untargeted declaration for {node.node_id}"
            actual.update((node.node_id, name) for name in names)
        expected = {(node_id, name) for node_id in self.selected_nodes for name in declarations_by_node[node_id]}
        unresolved = [(issue.node_id, issue.declaration) for issue in self.unresolved]
        if len(set(unresolved)) != len(unresolved) or set(unresolved) != expected - actual:
            return "contains mismatched unresolved declarations"
        return None

    def _shared_tables(self) -> _Tables:
        """The ``trusted``, ``semantics``, and ``boundary_modules`` tables.

        Each shared item is stated once and named by every declaration that
        uses it. Two root modules may print one trusted declaration differently,
        or see different constants under one name, so those two tables are keyed
        by root module first. A module identity is the digest of its compiled
        files, the same under every root, so it is keyed by module name alone.
        """

        trusted: dict[str, dict[str, object]] = {}
        semantics: dict[str, dict[str, object]] = {}
        modules: dict[str, object] = {}
        for node in self.nodes:
            for declaration in node.declarations:
                for item in declaration.trusted:
                    _share(
                        trusted.setdefault(declaration.module, {}),
                        item.name,
                        item.as_dict(),
                        table_name=f"trusted declaration under root module {declaration.module}",
                    )
                for name, semantic in (*declaration.assumed_semantics, *declaration.axiom_semantics):
                    _share(
                        semantics.setdefault(declaration.module, {}),
                        name,
                        semantic,
                        table_name=f"semantic material under root module {declaration.module}",
                    )
                files: dict[str, list[list[str]]] = {}
                for module, kind, digest in declaration.boundary_modules:
                    files.setdefault(module, []).append([kind, digest])
                for module, identity in files.items():
                    _share(modules, module, identity, table_name="module identity")
        return trusted, semantics, modules

    def as_dict(self) -> dict[str, object]:
        # Fresh tables, so a caller's edit to the record cannot reach the report.
        return self._record(self._shared_tables())

    def _record(self, tables: _Tables) -> dict[str, object]:
        trusted, semantics, modules = tables
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
        return json.dumps(self._record(self._tables), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _share(table: dict[str, object], name: str, value: object, *, table_name: str) -> None:
    if table.setdefault(name, value) != value:
        raise SkeletonError([f"conflicting {table_name} for {name} in one skeleton report"])


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


def _blueprint_hash(graph: Graph, passages: dict[str, tuple[str | None, str | None, str]]) -> str:
    """Identify the exact, path-independent blueprint snapshot behind a report:
    every article, and the passage each cites or why it has none."""

    material = json.dumps(
        [(*row, *passages[row[0]]) for row in _graph_snapshot(graph)],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _sha256_id(material.encode("utf-8"))


def blueprint_hash(graph: Graph) -> str:
    """The blueprint hash of ``graph`` as it is now: its articles, and each
    article's cited passage, read once for this call."""

    return _blueprint_hash(graph, _source_passages(graph))


def evidence_hash_of(packet: str) -> str:
    """The evidence hash of packet text, however it reached the caller.

    A read-back card shows its packet verbatim, so a reader can hash what the
    card displays and compare it with what the card records.
    """

    return _sha256_id(packet.encode("utf-8"))


# --------------------------------------------------------------------------- #
# Probe records
# --------------------------------------------------------------------------- #

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
        "statement_assumed",
        "statement_boundary_modules",
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
    # Not `splitlines`: Lean's JSON printer leaves U+0085, U+2028 and U+2029 unescaped.
    for line in text.split("\n"):
        if not line.startswith(lean_probe.PROBE_MARKER):
            continue
        try:
            record = json.loads(line[len(lean_probe.PROBE_MARKER) :])
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
        if "error" in record and (record.keys() != {"error", "root"} or not isinstance(record["error"], str)):
            raise SkeletonError([f"the skeleton probe emitted an invalid error record for {root}"])
        records[root] = record
    expand = _expand_probe_material(tables)
    # Shared entries are validated once, however many roots name them.
    checked: set[int] = set()
    for root, record in records.items():
        if "error" in record:
            # The probe failed on this root alone; the caller reports why.
            continue
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
    characters it produces across the probe's output are bounded.
    """

    fragments: dict[int, list[object]] = {}
    for name, pieces in tables.pop("fragment").items():
        number = int(name) if name.isdecimal() and name.isascii() else -1
        if str(number) != name or not _material_pieces(pieces, below=number):
            raise SkeletonError([f"the skeleton probe emitted a malformed fragment {name}"])
        fragments[number] = pieces
    remaining = _PROBE_MATERIAL_LIMIT

    def expand(pieces: object, context: str) -> str:
        nonlocal remaining
        if not _material_pieces(pieces, below=None):
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
            elif piece in fragments:
                stack.append(iter(fragments[piece]))
            else:
                raise SkeletonError([f"the skeleton probe emitted invalid semantic material for {context}"])
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
        for file in files  # type: ignore[attr-defined]
    ]
    return resolved


def _validate_probe_record(record: dict[str, object], *, root: str, checked: set[int]) -> None:
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
    for field in ("module", "signature", "raw_signature"):
        _require_nonempty_string(record.get(field), field=field, context=root)
    _require_range(record.get("range"), context=root)
    _require_probe_source(record.get("source"), kind=str(record["kind"]), context=root)
    _require_probe_comments(record, "source", "source_comments", context=root)
    statement = record.get("statement_source")
    if statement is not None and not isinstance(statement, str):
        raise SkeletonError([f"the skeleton probe emitted an invalid statement_source field for {root}"])
    _require_probe_comments(record, "statement_source", "statement_comments", context=root)
    for field in ("depends", "assumed", "axioms", "statement_assumed", "statement_boundary_modules"):
        _require_string_list(record.get(field), field=field, context=root)
    for name, semantic in record["assumed_semantics"]:  # type: ignore[attr-defined]
        _validate_semantic_material(semantic, context=name)
    boundary_modules = _module_file_entries(record.get("boundary_modules"), context=root)
    if record["assumed"] and not boundary_modules:
        raise SkeletonError([f"the skeleton probe omitted boundary module files for {root}"])
    for name, semantic in record["axiom_semantics"]:  # type: ignore[attr-defined]
        _validate_semantic_material(semantic, context=name)
    seen: set[str] = set()
    for item in record["trusted"]:  # type: ignore[attr-defined]
        if id(item) not in checked:
            _validate_trusted_record(item, root=root)
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
    for field in ("module", "signature", "raw_signature"):
        _require_nonempty_string(record.get(field), field=field, context=context)
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

    parts = lean_name_parts(name)
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
    if value is not None and not (
        isinstance(value, list) and len(value) == 2 and all(type(line) is int for line in value) and 1 <= value[0] <= value[1]
    ):
        raise SkeletonError([f"the skeleton probe emitted an invalid range for {context}"])


def _require_string_list(value: object, *, field: str, context: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise SkeletonError([f"the skeleton probe emitted an invalid {field} field for {context}"])
    if len(value) != len(set(value)):
        raise SkeletonError([f"the skeleton probe emitted duplicate {field} entries for {context}"])


_SEMANTIC_SHAPES = (
    frozenset({"safety", "type"}),
    frozenset({"safety", "type", "value"}),
    frozenset({"constructors", "safety", "type"}),
)
_KIND_SEMANTIC_SHAPE = {
    **dict.fromkeys(("def", "instance", "opaque"), _SEMANTIC_SHAPES[1]),
    **dict.fromkeys(("class", "inductive", "structure"), _SEMANTIC_SHAPES[2]),
}


def _validate_semantic_material(semantic: str, *, context: str, kind: str | None = None) -> None:
    shapes = _SEMANTIC_SHAPES if kind is None else (_KIND_SEMANTIC_SHAPE.get(kind, _SEMANTIC_SHAPES[0]),)
    try:
        payload = json.loads(semantic)
    except (RecursionError, ValueError):
        payload = None
    generated = payload.get("generated") if isinstance(payload, dict) else None
    entries = generated if isinstance(generated, list) else []
    if not (
        isinstance(payload, dict)
        and payload.keys() == {"generated", "root"}
        and _semantic_payload_ok(payload["root"], shapes)
        and isinstance(generated, list)
        and all(
            isinstance(entry, dict)
            and entry.keys() == {"material", "name"}
            and _valid_semantic_name(entry["name"])
            and _semantic_payload_ok(entry["material"], _SEMANTIC_SHAPES)
            for entry in entries
        )
        and len({json.dumps(entry["name"], sort_keys=True, separators=(",", ":")) for entry in entries})
        == len(entries)
    ):
        raise SkeletonError([f"invalid elaborated semantic material for {context}"])


def _semantic_payload_ok(payload: object, shapes: tuple[frozenset[str], ...]) -> bool:
    return isinstance(payload, dict) and payload.keys() in shapes and payload["safety"] in {"safe", "unsafe", "partial"}


def _valid_semantic_name(value: object) -> bool:
    for _ in range(257):
        if value is None:
            return True
        if not isinstance(value, dict) or len(value) != 1:
            return False
        ((tag, pair),) = value.items()
        if not (isinstance(pair, list) and len(pair) == 2):
            return False
        if not (tag == "str" and isinstance(pair[1], str) or tag == "num" and type(pair[1]) is int and pair[1] >= 0):
            return False
        value = pair[0]
    return False


def _semantic_pairs(value: object) -> tuple[tuple[str, str], ...]:
    return tuple(map(tuple, value))  # type: ignore[call-overload]


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
        path = lean_root / raw_path
        try:
            resolved = path.resolve(strict=True)
            key = (file_kind, str(resolved))
            digest = cache.get(key)
            if digest is None:
                before = resolved.stat()
                content = resolved.read_bytes()
                after = resolved.stat()
        except (OSError, RuntimeError) as exc:
            raise SkeletonError([f"cannot bind assumed module {module} to {file_kind} file {path}: {exc}"]) from exc
        if digest is None:
            identity = [(m.st_dev, m.st_ino, m.st_size, m.st_mtime_ns, m.st_ctime_ns) for m in (before, after)]
            if identity[0] != identity[1] or (
                snapshot_started_ns is not None and max(after.st_mtime_ns, after.st_ctime_ns) > snapshot_started_ns
            ):
                raise SkeletonError(
                    [f"assumed module {module} changed during skeleton extraction; retry after the build is idle"]
                )
            digest = cache[key] = _sha256_id(file_kind.encode("utf-8") + b"\0" + content)
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
    timeout: float | None = None,
) -> SkeletonReport:
    """Extract the skeleton of every ``lean:`` declaration the blueprint names.

    Each root module gets its own probe, whose only project import is that
    module, so ``node_ids``, which narrows the report to those articles, never
    changes a declaration's evidence. ``runner`` executes one rendered probe and
    returns Lean's standard output; the default runs ``lake env lean`` after one
    Lake freshness check and one build of the probe's helpers, and ``timeout``
    bounds that build and each probe. A custom ``runner`` bounds its own probes,
    so it cannot be combined with ``timeout``, and may be called from several
    threads at once. A declaration the lexical index cannot place is reported
    as unresolved without running Lean, exactly as
    ``autoform check --lean-root`` reports it.
    """

    if runner is not None and timeout is not None:
        raise ValueError("timeout bounds the default runner's probes; a custom runner bounds its own")
    try:
        graph = load_graph(blueprint_dir)
    except GraphValidationError as exc:
        raise SkeletonError(exc.issues) from exc
    try:
        root = resolve_lean_root(lean_root)
    except LeanSourceError as error:
        raise SkeletonError([str(error)]) from error
    activity = "skeletons were being extracted"
    inputs = snapshot_inputs(graph, root)
    libraries = lean_libraries(root)
    check_configuration_unchanged(root, inputs, activity=activity)
    try:
        index = index_project(root)
    except OSError as error:
        raise SkeletonError([index_failure_message(error)]) from error
    probing: Callable[[tuple[str, ...]], contextlib.AbstractContextManager[object]] | None = None
    if runner is None:
        probing, runner = lean_probe.default_runner(root, timeout=DEFAULT_PROBE_TIMEOUT if timeout is None else timeout)

    report = extract_graph_skeletons(
        graph, lean_root=root, libraries=libraries, index=index, runner=runner, node_ids=node_ids, probing=probing
    )
    current = check_inputs_unchanged(graph, root, index, inputs, activity=activity)
    if _blueprint_hash(current, _source_passages(current)) != report.blueprint_hash:
        raise SkeletonError([f"source passage changed while {activity}; retry after the project is idle"])
    return report


@dataclass(frozen=True, slots=True)
class InputsSnapshot:
    """The blueprint and the project configuration, as they were before Lean ran."""

    graph: object
    configuration: object


def snapshot_inputs(graph: Graph, lean_root: Path) -> InputsSnapshot:
    """Record the inputs a Lean run is about, to refuse its answer if they change."""

    return InputsSnapshot(graph=_graph_snapshot(graph), configuration=project_control_snapshot(lean_root))


def check_configuration_unchanged(lean_root: Path, snapshot: InputsSnapshot, *, activity: str) -> None:
    """Refuse when the Lake configuration changed since ``snapshot``."""

    if project_control_snapshot(lean_root) != snapshot.configuration:
        raise SkeletonError(
            [f"Lean project configuration changed while {activity}; retry after the project is idle"]
        )


def check_inputs_unchanged(
    graph: Graph, lean_root: Path, index: SourceIndex, snapshot: InputsSnapshot, *, activity: str
) -> Graph:
    """Refuse an answer whose Lean sources, Lake configuration or blueprint changed
    while Lean ran, and return the blueprint as it is now.

    ``activity`` names what was running, as in "skeletons were being extracted".
    """

    try:
        current_index = index_project(lean_root)
    except OSError as error:
        raise SkeletonError([index_failure_message(error)]) from error
    if current_index.source_digest != index.source_digest:
        raise SkeletonError([f"Lean sources changed while {activity}; retry after the build is idle"])
    check_configuration_unchanged(lean_root, snapshot, activity=activity)
    try:
        current_graph: Graph | None = load_graph(graph.blueprint_dir)
    except GraphValidationError:
        current_graph = None
    if current_graph is None or _graph_snapshot(current_graph) != snapshot.graph:
        raise SkeletonError([f"the blueprint changed while {activity}; retry after the project is idle"])
    return current_graph


def select_lean_targets(
    graph: Graph, node_ids: tuple[str, ...] | None = None
) -> tuple[list[tuple[Node, tuple[str, ...]]], list[tuple[Node, tuple[str, ...]]], str]:
    """Every article's ``lean:`` targets in id order, the ones ``node_ids`` selects,
    and ``all`` or ``filtered`` for whether it narrowed them."""

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
    if node_ids is None:
        return targets, targets, "all"
    if not node_ids:
        raise SkeletonError(["no articles selected"])
    if len(node_ids) != len(set(node_ids)):
        raise SkeletonError(["duplicate article selection"])
    wanted = set(node_ids)
    unknown = sorted(wanted - set(graph.nodes))
    if unknown:
        raise SkeletonError([f"unknown article: {node_id}" for node_id in unknown])
    untargeted = sorted(wanted - {node.id for node, _ in targets})
    if untargeted:
        raise SkeletonError(
            [f"{node_id}: article has no Lean declaration targets" for node_id in untargeted]
        )
    return targets, [(node, names) for node, names in targets if node.id in wanted], "filtered"


def locate_declaration(
    name: str, *, index: SourceIndex, libraries: tuple[LeanLibrary, ...], lean_root: Path
) -> tuple[str | None, str | None]:
    """The module that builds ``name``, or ``None`` and the reason no module does."""

    location = index.find(name)
    if location is None:
        if ambiguous := index.ambiguity(name):
            return None, f"declaration defined more than once in the Lean sources: {ambiguous}"
        return None, "declaration not found in the Lean sources"
    module = module_of(lean_root / location.path, libraries)
    if module is None:
        return None, f"source {location.path.as_posix()} is not built by any library target"
    return module, None


def extract_graph_skeletons(
    graph: Graph,
    *,
    lean_root: Path,
    libraries: tuple[LeanLibrary, ...],
    index: SourceIndex,
    runner: ProbeRunner,
    node_ids: tuple[str, ...] | None = None,
    probing: Callable[[tuple[str, ...]], contextlib.AbstractContextManager[object]] | None = None,
) -> SkeletonReport:
    """Extract skeletons for an already loaded graph.

    ``probing``, when given, is entered with every module about to be probed and
    held while the probes run, and only then; the default runner's freshness
    check, helper build, scratch directory and signal guard live there.
    """

    targets, selected, selection = select_lean_targets(graph, node_ids)
    # Every article's passage, so the blueprint hash does not depend on the selection.
    passages = _source_passages(graph)
    # An article whose cited passage cannot be found cannot be judged for
    # faithfulness, so its declarations are unresolved rather than shown alone.
    broken_passages = {node.id: passages[node.id][2] for node, _ in selected if passages[node.id][2]}

    # Lean prints signatures, and parses sources, with the notation, tokens,
    # and attributes its imports bring into scope, so each root module gets its
    # own probe whose only project import is that module.
    unresolved: list[UnresolvedTarget] = []
    roots: dict[str, str] = {}
    for node, names in selected:
        for name in names:
            module, reason = locate_declaration(name, index=index, libraries=libraries, lean_root=lean_root)
            if node.id in broken_passages:
                # Every refusal is named at once, so mending the passage brings no second one.
                reason = "; ".join(filter(None, (broken_passages[node.id], reason)))
                unresolved.append(UnresolvedTarget(node.id, name, reason))
                continue
            if module is None:
                unresolved.append(UnresolvedTarget(node.id, name, reason or ""))
                continue
            roots.setdefault(name, module)
    groups: dict[str, list[str]] = {}
    for name, module in roots.items():
        groups.setdefault(module, []).append(name)

    records: dict[str, dict[str, object]] = {}
    # A probe that fails on its own (exit status, timeout, malformed records)
    # leaves only its module's roots unresolved, and an error the probe caught
    # on one root leaves only that root unresolved. Lake, freshness, helper,
    # toolchain, and snapshot failures concern every module and still abort the
    # extraction.
    failed_modules: dict[str, str] = {}
    snapshot_started_ns: int | None = None
    if groups:
        project_roots = tuple(root for library in libraries for root in library.roots)
        programs = {
            module: render_probe(imports=(module,), roots=tuple(groups[module]), project_roots=project_roots)
            for module in sorted(groups)
        }
        snapshot_started_ns = time.time_ns()

        def probe(module: str) -> dict[str, dict[str, object]]:
            # Parsed in the worker, as soon as the probe ends.
            return parse_probe_output(runner(programs[module], lean_root), expected_roots=tuple(groups[module]))

        with contextlib.nullcontext() if probing is None else probing(tuple(sorted(groups))):
            results = run_module_probes(list(programs), probe)
        for module in sorted(groups):
            result = results[module]
            if isinstance(result, SkeletonError):
                # run_probe already cut Lean's output; a second cut would drop the end of what it kept.
                failed_modules[module] = lean_probe.stable_paths(
                    f"probe of module {module} failed: {result}", lean_root
                )
            else:
                records.update(result)  # type: ignore[arg-type]

    nodes: list[NodeSkeleton] = []
    module_hashes: dict[tuple[str, str], str] = {}
    for node, names in selected:
        declarations: list[DeclarationSkeleton] = []
        for name in names:
            if name not in roots or node.id in broken_passages:
                continue
            module = roots[name]
            if module in failed_modules:
                unresolved.append(UnresolvedTarget(node.id, name, failed_modules[module]))
                continue
            record = records.get(name)
            if record is None:
                unresolved.append(UnresolvedTarget(node.id, name, "the probe returned no record"))
                continue
            if "error" in record:
                reason = f"the probe failed on this declaration: {record['error']}"
                unresolved.append(UnresolvedTarget(node.id, name, lean_probe.stable_paths(reason, lean_root)))
                continue
            if not record.get("found"):
                unresolved.append(
                    UnresolvedTarget(
                        node.id,
                        name,
                        "not in the built environment after importing "
                        f"{module}; check the `lean:` target and declaring source",
                    )
                )
                continue
            if record.get("module") != module:
                # The packet was printed in the environment of the module its
                # source was found in, which is not the one Lean declares it in.
                reason = f"Lean declares it in module {record.get('module')}, not in {module} where its source was found"
                unresolved.append(UnresolvedTarget(node.id, name, reason))
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
        passage, locator, _ = passages[node.id]
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
        blueprint_hash=_blueprint_hash(graph, passages),
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


def source_passage(
    node: Node, blueprint: Path, *, issues: list[str] | None = None, files: dict[Path, bytes | str] | None = None
) -> tuple[str | None, str | None]:
    """Return the passage an article cites through a line locator, and the locator.

    A ``## Sources`` link to a non-Markdown file inside the blueprint with a
    ``#L<start>-L<end>`` fragment names the exact source text the statement
    came from. The first such link wins. Markdown files are notes, not
    passages, and are ignored here, but a link that names no regular file in
    the blueprint is reported whatever its suffix, as check reports it. When
    the first locator names no text there is no passage, and the reason is
    appended to ``issues`` if given. ``files`` keeps each file read, or why it
    could not be, so passages cut with one ``files`` come from one read of each
    file.
    """

    def broken(target: str, why: str) -> tuple[None, None]:
        if issues is not None:
            issues.append(f"source locator {target} {why}")
        return None, None

    files = {} if files is None else files
    for target in node.sources:
        try:
            parsed = urlsplit(target)
        except ValueError:
            continue
        if parsed.scheme or parsed.netloc:
            continue
        path = unquote(parsed.path)
        match = _LINE_LOCATOR.fullmatch(unquote(parsed.fragment))
        if match is None or not path:
            continue
        if "\x00" in path:
            return broken(target, "contains an invalid path")
        spelled = node.path.parent / path
        if not Path(os.path.normpath(spelled)).is_relative_to(blueprint.resolve()):
            return broken(target, "points outside the blueprint")
        # Each way a locator's file cannot be read is this article's problem, so
        # the other articles still extract.
        try:
            metadata = os.stat(spelled)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENAMETOOLONG):
                return broken(target, "names a path that cannot be resolved")
            if error.errno in (errno.EACCES, errno.EPERM):
                return broken(target, "names a file that cannot be read")
            return broken(target, "names a missing file")
        candidate = resolved_path(spelled)
        if candidate is None:
            return broken(target, "names a path that cannot be resolved")
        if not candidate.is_relative_to(blueprint.resolve()):
            return broken(target, "points outside the blueprint")
        if not stat.S_ISREG(metadata.st_mode):
            return broken(target, "names something other than a regular file")
        # As audit judges a link that names a file: a Markdown file, in any
        # letter case, is a note, whose fragment names a heading.
        if candidate.suffix.casefold() == ".md":
            continue
        if metadata.st_size > lean_probe.SNAPSHOT_FILE_LIMIT:
            return broken(target, f"names a file over the {lean_probe.SNAPSHOT_FILE_LIMIT}-byte limit")
        try:
            # Opening it tests readability; `os.access` ignores Windows ACLs.
            os.close(os.open(candidate, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)))
        except OSError:
            return broken(target, "names a file that cannot be read")
        if candidate not in files:
            files[candidate] = _read_cited_source(candidate)
        content = files[candidate]
        if isinstance(content, str):
            return broken(target, content)
        try:
            # Lines are what an editor or `sed` counts: newline-separated. Python's
            # `splitlines` also breaks on form feeds, which `pdftotext` writes
            # between pages, and every locator into such a file would then drift
            # by one line per page.
            lines = content.decode("utf-8").split("\n")
        except UnicodeError:
            return broken(target, "names a file that is not readable UTF-8 text")
        if lines and lines[-1] == "":
            lines.pop()
        try:
            start = int(match.group(1))
            end = int(match.group(2) or start)
        except ValueError:
            # By default `int` refuses more than 4,300 digits, far past any file's last line.
            return broken(target, "names no lines of its file")
        if start < 1 or end < start or end > len(lines):
            return broken(target, "names no lines of its file")
        relative = candidate.relative_to(blueprint.resolve()).as_posix()
        return "\n".join(lines[start - 1 : end]), f"{relative}#L{start}-L{end}"
    return None, None


def _read_cited_source(path: Path) -> bytes | str:
    """The bytes of a cited source, or why it has none, in words without its path."""

    if os.path.lexists(path) and not os.path.isfile(path):
        return "names something other than a regular file"
    try:
        captured = lean_probe.read_snapshot_file(path)
    except SkeletonError:
        return "names a file that cannot be read"
    return "names a missing file" if captured is None else captured[0]


def _source_passages(graph: Graph) -> dict[str, tuple[str | None, str | None, str]]:
    """Every article's cited passage, its locator, and why it has none ("" when nothing is wrong),
    all cut from one read of each cited file."""

    files: dict[Path, bytes | str] = {}
    passages: dict[str, tuple[str | None, str | None, str]] = {}
    for node in graph.nodes.values():
        issues: list[str] = []
        passage, locator = source_passage(node, graph.blueprint_dir, issues=issues, files=files)
        passages[node.id] = (passage, locator, "; ".join(issues))
    return passages


def _article_path(node: Node, graph: Graph) -> str:
    path = node.path
    return path.relative_to(graph.blueprint_dir).as_posix() if path.is_relative_to(graph.blueprint_dir) else path.name


def _declaration(
    record: dict[str, object],
    *,
    libraries: tuple[LeanLibrary, ...],
    lean_root: Path,
    index: SourceIndex,
    module_hashes: dict[tuple[str, str], str],
    snapshot_started_ns: int | None,
) -> DeclarationSkeleton:
    trusted = [_trusted(item, libraries=libraries, lean_root=lean_root, index=index) for item in record["trusted"]]
    name = str(record["root"])
    module = str(record["module"])
    start, end = record["range"] or (None, None)
    source, source_comments, source_withheld = _shown_source(record["source"], record["source_comments"], name=name)
    written, written_comments, statement_withheld = _shown_source(
        record["statement_source"], record["statement_comments"], name=name
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
    return DeclarationSkeleton(
        name=name,
        kind=str(record["kind"]),
        module=module,
        path=path,
        start_line=start,
        end_line=end,
        signature=str(record["signature"]),
        raw_signature=str(record["raw_signature"]),
        semantic=str(record["semantic"]),
        lean_version=str(record["lean_version"]),
        depends=tuple(record["depends"]),
        trusted=tuple(_dependency_order(trusted)),
        assumed=tuple(record["assumed"]),
        assumed_semantics=_semantic_pairs(record.get("assumed_semantics")),
        boundary_modules=_hash_module_files(
            record.get("boundary_modules"),
            lean_root=lean_root,
            cache=module_hashes,
            snapshot_started_ns=snapshot_started_ns,
        ),
        axioms=tuple(record["axioms"]),
        axiom_semantics=_semantic_pairs(record.get("axiom_semantics")),
        statement_assumed=tuple(record["statement_assumed"]),
        statement_boundary_modules=tuple(record["statement_boundary_modules"]),
        source=source,
        statement=statement,
        source_comments=source_comments,
        statement_comments=statement_comments,
        source_withheld=source_withheld,
    )


def _trusted(
    item: dict[str, object],
    *,
    libraries: tuple[LeanLibrary, ...],
    lean_root: Path,
    index: SourceIndex,
) -> TrustedDeclaration:
    name = str(item["name"])
    module = str(item["module"])
    start, end = item["range"] or (None, None)
    path = _source_path(name, module, libraries=libraries, lean_root=lean_root, index=index)
    source, source_comments, source_withheld = _shown_source(item["source"], item["source_comments"], name=name)
    if source is None and start is None and _internal_detail(name):
        source_withheld = True
    return TrustedDeclaration(
        name=name,
        kind=str(item["kind"]),
        module=module,
        path=path,
        start_line=start,
        end_line=end,
        signature=str(item["signature"]),
        raw_signature=str(item["raw_signature"]),
        semantic=str(item["semantic"]),
        depends=tuple(item["depends"]),
        source=source,
        source_comments=source_comments,
        source_withheld=source_withheld,
    )


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


def _dependency_order(items: list[TrustedDeclaration]) -> list[TrustedDeclaration]:
    """Order trusted declarations so each is read after what it rests on.

    Ties are broken by name, so the order is a function of the sources alone.
    """

    by_name = {item.name: item for item in items}
    order: list[TrustedDeclaration] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen or name not in by_name:
            return
        seen.add(name)
        for dependency in sorted(by_name[name].depends):
            visit(dependency)
        order.append(by_name[name])

    for name in sorted(by_name):
        visit(name)
    return order


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def format_report(report: SkeletonReport, *, lean_root: Path | None = None) -> str:
    """Render the report as the text a reviewer reads.

    Source excerpts captured by the probe travel inside the report. The
    ``lean_root`` argument remains for call compatibility and is never reread.
    """

    out: list[str] = []
    for node in report.nodes:
        if len(node.declarations) > 1:
            article = node.hash or "none: a declaration is unresolved"
            out += [f"## {node.node_id} · article skeleton {article}", ""]
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
            out += ["", f"   {_trust_summary(declaration)} · skeleton {declaration.hash}"]
            if declaration.assumed:
                out.append(f"   assumes: {', '.join(declaration.assumed)}")
            out.append(f"   axioms: {', '.join(declaration.axioms) if declaration.axioms else 'none'}")
            for item in declaration.trusted:
                out += ["", f"   -- {item.kind} {item.name}  ({_where(item)})"]
                out.extend(f"   -- {line}" for line in item.signature.splitlines())
                if item.source is None:
                    out.extend(f"   {line}" for line in item.signature.splitlines())
                    if item.source_withheld:
                        out.append(f"   {_NOT_SHOWN}")
                else:
                    out.extend(f"   {line}" for line in item.source.splitlines())
            out.append("")
        if not node.declarations:
            out += [f"== {node.node_id} · no skeleton", ""]
    for issue in report.unresolved:
        out.append(f"error: {issue.message}")
    return "\n".join(out).rstrip("\n") + "\n"


#: The joint packet of an article's declarations, what a faithfulness judge reads.
ARTICLE_PACKET = "article.lean"


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


#: The names Windows keeps for devices, which no file there may take before its first dot.
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{digit}" for digit in range(10)), *(f"LPT{digit}" for digit in range(10))}
)


def declaration_filename(name: str, *, suffix: str = ".lean") -> str:
    """Return a portable, collision-resistant filename for a Lean name."""

    if not name:
        raise ValueError("a declaration name cannot be empty")
    if not suffix.startswith(".") or any(character in suffix for character in "/\\\0\r\n"):
        raise ValueError(f"invalid declaration filename suffix: {suffix!r}")
    encoded = quote(name, safe="").replace("~", "%7E")
    head, dot, _ = encoded.partition(".")
    if dot and head.upper() in _WINDOWS_DEVICE_NAMES:
        # Windows refuses a file whose name before its first dot is a device's;
        # without a dot, the digest follows the name there.
        encoded = f"%{ord(encoded[0]):02X}{encoded[1:]}"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    if not encoded or len(os.fsencode(encoded)) > 180:
        encoded = "declaration"
    return f"{encoded}--{digest}{suffix}"


def _stage_report_output(report: SkeletonReport, destination: Path) -> tuple[Path, tuple[int, int, str] | None]:
    """Write a report to a same-directory stage and capture the old identity."""

    identity = output_identity(destination)
    if identity is not None and not destination.is_file():
        raise SkeletonError([f"report output exists and is not a regular file: {destination}"])
    return stage_output(destination, report.to_json() + "\n"), identity


def _paths_overlap(first: Path, second: Path) -> bool:
    first, second = first.resolve(), second.resolve()
    return first.is_relative_to(second) or second.is_relative_to(first)


def _output_path(value: str | Path, kind: str) -> Path:
    requested = Path(value).expanduser()
    if requested.is_symlink():
        raise SkeletonError([f"refusing symlink {kind} output: {requested}"])
    return Path(os.path.abspath(requested))


def write_skeleton_report(report: SkeletonReport, destination: str | Path) -> Path:
    """Failure-atomically replace one JSON skeleton report."""

    output = _output_path(destination, "report")
    stage: Path | None = None
    try:
        stage, identity = _stage_report_output(report, output)
        replace_outputs([(output, stage, identity)])
    except OSError as exc:
        raise SkeletonError([f"could not prepare skeleton report output: {exc}"]) from exc
    finally:
        if stage is not None:
            with contextlib.suppress(OSError):
                remove_output(stage)
    return output


def write_packets(
    report: SkeletonReport,
    directory: str | Path,
    *,
    passages: str | Path | None = None,
    report_path: str | Path | None = None,
) -> list[Path]:
    """Write one comment-stripped packet per skeleton.

    Each packet holds only what an auditor may see: the comment-stripped
    skeleton. Nothing in it names the article, the source, or the intent, but
    its file name, under the article's path, names the article and the
    declaration, so a blind auditor is given the packets ``review prepare``
    writes instead, named by the hash of their bytes. The manifest beside the
    packets maps each file back to its article and hashes.

    With ``passages``, the source passage each article cites is written to a
    second directory, one ``passage.txt`` per article. A faithfulness judge
    gets a packet and its passage; an auditor of one declaration gets the
    packet alone, which is why the two never share a directory.

    With ``report_path``, the JSON report joins the same failure-atomic
    publication transaction.
    """

    if not report.clean:
        raise SkeletonError(
            ["refusing to publish review packets from an incomplete skeleton report"]
        )

    root = _output_path(directory, "packet")
    passages_root = None if passages is None else _output_path(passages, "packet")
    if passages_root is not None and _paths_overlap(root, passages_root):
        raise SkeletonError(["packet and passage output directories must be disjoint"])
    report_destination = None if report_path is None else _output_path(report_path, "report")
    if report_destination is not None and (
        _paths_overlap(root, report_destination)
        or (passages_root is not None and _paths_overlap(passages_root, report_destination))
    ):
        raise SkeletonError(["report output must be disjoint from packet and passage directories"])
    root_identity = validate_managed_output(root, kind="packets")
    passages_identity = (
        validate_managed_output(passages_root, kind="passages")
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
    # Written after the manifests: a capture that lists a stage before its
    # marker is complete then finds no folder to descend into.
    files: list[tuple[Path, str | bytes]] = []
    try:
        packet_stage = stage_output(root)
        passages_stage = stage_output(passages_root) if passages_root is not None else None
        if report_destination is not None:
            report_stage, report_identity = _stage_report_output(report, report_destination)
        for node in report.nodes:
            node_path = _safe_node_path(node.node_id)
            passage_path: str | None = None
            if passages_stage is not None and node.passage is not None:
                passage_path = (node_path / "passage.txt").as_posix()
                passage_bytes = (node.passage + "\n").encode("utf-8")
                files.append((passages_stage / passage_path, passage_bytes))
                passage_manifest.append(
                    {
                        "hash": _sha256_id(passage_bytes),
                        "node_id": node.node_id,
                        "passage": passage_path,
                        "review_hash": node.review_hash,
                    }
                )
            article_relative = node_path / ARTICLE_PACKET
            if node.declarations:
                files.append((packet_stage / article_relative, node.blind_text()))
                written.append(root / article_relative)
            for declaration in node.declarations:
                relative = node_path / declaration_filename(declaration.name)
                files.append((packet_stage / relative, declaration.blind_text()))
                written.append(root / relative)
                entry = {
                    "article_packet": article_relative.as_posix(),
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
        for stage, kind, entries, schema in (
            (packet_stage, "packets", manifest, PACKET_SCHEMA),
            (passages_stage, "passages", passage_manifest, PASSAGE_SCHEMA),
        ):
            if stage is not None:
                payload = json.dumps({"kind": kind, kind: entries, "schema": schema}, indent=2, sort_keys=True)
                # Staged under a name that is neither the marker nor Lean source, then
                # renamed: a capture sees no marker or the whole one, never a partial file.
                os.replace(stage_output(stage / PACKET_MANIFEST, payload + "\n"), stage / PACKET_MANIFEST)
        for path, content in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
        outputs = [(root, packet_stage, root_identity)]
        if passages_root is not None and passages_stage is not None:
            outputs.append((passages_root, passages_stage, passages_identity))
        if report_destination is not None and report_stage is not None:
            outputs.append((report_destination, report_stage, report_identity))
        replace_outputs(outputs)
    except OSError as exc:
        raise SkeletonError([f"could not prepare skeleton output: {exc}"]) from exc
    finally:
        for stage in (packet_stage, passages_stage, report_stage):
            if stage is not None:
                with contextlib.suppress(OSError):
                    remove_output(stage)
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
    "SEMANTIC_SCHEMA",
    "SKELETON_SCHEMA",
    "DeclarationSkeleton",
    "InputsSnapshot",
    "NodeSkeleton",
    "PACKET_MANIFEST",
    "ProbeRunner",
    "SkeletonError",
    "SkeletonReport",
    "TrustedDeclaration",
    "blueprint_hash",
    "check_configuration_unchanged",
    "check_inputs_unchanged",
    "declaration_filename",
    "evidence_hash_of",
    "extract_graph_skeletons",
    "extract_skeletons",
    "format_report",
    "locate_declaration",
    "parse_probe_output",
    "select_lean_targets",
    "snapshot_inputs",
    "source_passage",
    "validate_managed_output",
    "write_packets",
    "write_skeleton_report",
]
