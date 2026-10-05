#!/usr/bin/env python3
"""Build a kernel-trust probe from the root package's packed ILean artifacts."""

from __future__ import annotations

import json
import re
import sys
import tarfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import NamedTuple

_MAX_ILEAN_BYTES = 16 * 1024 * 1024
_TOP_LEVEL_NAME = re.compile(r'^name\s*=\s*("(?:[^"\\]|\\.)*")\s*(?:#.*)?$')
_ASSUMPTIONS_SCHEMA = "autoform-assumptions/v1"
_DECLARATION_NAME_PART = r"«([^»]+)»|([^.\s«»]+)"
_DECLARATION_NAME = re.compile(
    rf"(?:{_DECLARATION_NAME_PART})(?:\.(?:{_DECLARATION_NAME_PART}))*"
)


class AuditInputError(ValueError):
    """The root package configuration or artifacts are not safe to audit."""


def root_package_from_config(config: Path) -> str:
    """Read the root package name from Lake's evaluated TOML configuration.

    ``lake translate-config toml`` resolves either supported manifest language
    and writes the package-level ``name`` before any target tables. Keep this
    parser deliberately narrow: an unexpected translation must stop the audit,
    not select a dependency or target name later in the document.
    """

    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AuditInputError(f"cannot read evaluated Lake configuration: {exc}") from exc
    names: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            break
        match = _TOP_LEVEL_NAME.fullmatch(stripped)
        if match is None:
            continue
        try:
            name = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise AuditInputError("evaluated Lake configuration has an invalid package name") from exc
        if not isinstance(name, str) or not name or any(character.isspace() for character in name):
            raise AuditInputError("evaluated Lake configuration has an invalid package name")
        names.append(name)
    if len(names) != 1:
        raise AuditInputError("evaluated Lake configuration must define exactly one root package name")
    return names[0]


def blueprint_policy(blueprint: Path) -> str:
    """Return ``allowed`` or ``forbidden``: may theorems keep a ``sorry`` proof?

    Only ``roadmap/README.md`` sets the policy, in its frontmatter. This reads
    that one key the way the CLI's frontmatter parser does. A reading that
    differs from the CLI fails safe: ``forbidden`` runs the strict audit, and a
    wrong ``allowed`` meets an assumption contract that refuses it.
    """

    path = blueprint / "roadmap" / "README.md"
    try:
        lines = path.read_bytes().decode("utf-8").splitlines()
    except FileNotFoundError:
        return "forbidden"
    except (OSError, UnicodeError) as exc:
        raise AuditInputError(f"cannot read {path}: {exc}") from exc
    if not lines or lines[0].strip() != "---":
        return "forbidden"
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        raise AuditInputError(f"{path}: unterminated frontmatter")
    policy: str | None = None
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = (part.strip() for part in stripped.split(":", 1))
        if key != "open_statements":
            continue
        if policy is not None:
            raise AuditInputError(f"{path}: duplicate frontmatter key 'open_statements'")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not value:
            raise AuditInputError(f"{path}: empty frontmatter value for 'open_statements'")
        policy = value.casefold()
        if policy not in {"allowed", "forbidden"}:
            raise AuditInputError(f"{path}: 'open_statements' accepts allowed or forbidden")
    return policy or "forbidden"


def modules_from_archive(archive: Path, root_package: str) -> tuple[str, ...]:
    """Return modules proven to be built as part of *root_package*."""

    if not root_package or any(character.isspace() for character in root_package):
        raise AuditInputError("root package name is empty or malformed")
    modules: dict[str, str] = {}
    members: dict[str, tarfile.TarInfo] = {}
    try:
        packed = tarfile.open(archive, mode="r:*")
    except (OSError, tarfile.TarError) as exc:
        raise AuditInputError(f"cannot read root-package build archive: {exc}") from exc

    with packed:
        for member in packed:
            if not member.name.endswith((".ilean", ".olean", ".trace")):
                continue
            parts = _safe_member_parts(member.name)
            display_path = "/".join(parts)
            if display_path in members:
                raise AuditInputError(f"duplicate build archive member: {display_path}")
            if not member.isfile():
                raise AuditInputError(f"build archive member is not a regular file: {display_path}")
            members[display_path] = member

        for display_path, member in sorted(members.items()):
            if not display_path.endswith(".ilean"):
                continue
            if member.size > _MAX_ILEAN_BYTES:
                raise AuditInputError(f"ILean archive member is unexpectedly large: {display_path}")
            metadata = _read_json(packed, member, "ILean", display_path)
            parts = PurePosixPath(display_path).parts
            module = _module_from_metadata(metadata, parts, display_path)
            stem = display_path[: -len(".ilean")]
            olean_path = f"{stem}.olean"
            trace_path = f"{stem}.trace"
            if olean_path not in members:
                raise AuditInputError(f"ILean artifact has no matching OLean: {display_path}")
            trace_member = members.get(trace_path)
            if trace_member is None:
                raise AuditInputError(f"ILean artifact has no matching Lake trace: {display_path}")
            trace = _read_json(packed, trace_member, "Lake trace", trace_path)
            _validate_trace(trace, module, root_package, trace_path)
            previous = modules.get(module)
            if previous is not None:
                raise AuditInputError(
                    f"module {module!r} has duplicate ILean artifacts: {previous} and {display_path}"
                )
            modules[module] = display_path

    if not modules:
        raise AuditInputError("root-package build archive contains no ILean artifacts")
    return tuple(sorted(modules))


def _read_json(
    packed: tarfile.TarFile, member: tarfile.TarInfo, kind: str, display_path: str
) -> object:
    source = packed.extractfile(member)
    if source is None:
        raise AuditInputError(f"cannot read {kind} archive member: {display_path}")
    try:
        return json.loads(source.read().decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AuditInputError(f"malformed {kind} metadata in {display_path}: {exc}") from exc


def _validate_trace(trace: object, module: str, root_package: str, display_path: str) -> None:
    if not isinstance(trace, dict) or trace.get("synthetic") is not False:
        raise AuditInputError(f"invalid Lake trace metadata: {display_path}")
    strings = set(_json_strings(trace))
    if f"Module.name: {module}" not in strings:
        raise AuditInputError(f"Lake trace does not identify module {module!r}: {display_path}")
    if f"Package.id?: (some {root_package})" not in strings:
        raise AuditInputError(
            f"Lake trace does not identify root package {root_package!r}: {display_path}"
        )


def _json_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _json_strings(key)
            yield from _json_strings(item)


def _safe_member_parts(name: str) -> tuple[str, ...]:
    path = PurePosixPath(name)
    parts = path.parts
    while parts and parts[0] == ".":
        parts = parts[1:]
    if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise AuditInputError(f"unsafe ILean archive member path: {name!r}")
    return parts


def _module_from_metadata(metadata: object, parts: tuple[str, ...], display_path: str) -> str:
    if not isinstance(metadata, dict):
        raise AuditInputError(f"ILean metadata is not an object: {display_path}")
    module = metadata.get("module")
    if not isinstance(module, str):
        raise AuditInputError(f"ILean metadata has an invalid module name: {display_path}")
    module_parts = _module_parts(module, display_path)
    if not isinstance(metadata.get("version"), int):
        raise AuditInputError(f"ILean metadata has no integer version: {display_path}")
    for field in ("decls", "references"):
        if not isinstance(metadata.get(field), dict):
            raise AuditInputError(f"ILean metadata has an invalid {field} field: {display_path}")
    if not isinstance(metadata.get("directImports"), list):
        raise AuditInputError(f"ILean metadata has an invalid directImports field: {display_path}")

    expected_suffix = (*module_parts[:-1], f"{module_parts[-1]}.ilean")
    if len(parts) < len(expected_suffix) or parts[-len(expected_suffix) :] != expected_suffix:
        raise AuditInputError(
            f"ILean module {module!r} does not match its archive path: {display_path}"
        )
    return module


def _module_parts(module: str, display_path: str) -> tuple[str, ...]:
    """Parse Lake's pretty-printed module name without accepting Lean syntax."""

    parts: list[str] = []
    index = 0
    while index < len(module):
        if module[index] == "«":
            end = module.find("»", index + 1)
            if end < 0:
                raise AuditInputError(f"ILean metadata has an invalid module name: {display_path}")
            part = module[index + 1 : end]
            index = end + 1
        else:
            end = module.find(".", index)
            if end < 0:
                end = len(module)
            part = module[index:end]
            if not part or not (part[0].isalpha() or part[0] == "_"):
                raise AuditInputError(f"ILean metadata has an invalid module name: {display_path}")
            if any(not (character.isalnum() or character in "_'") for character in part):
                raise AuditInputError(f"ILean metadata has an invalid module name: {display_path}")
            index = end
        if (
            not part
            or any(ord(character) < 32 or character in "/\\«»" for character in part)
            or part in {".", ".."}
        ):
            raise AuditInputError(f"ILean metadata has an invalid module name: {display_path}")
        parts.append(part)
        if index == len(module):
            break
        if module[index] != ".":
            raise AuditInputError(f"ILean metadata has an invalid module name: {display_path}")
        index += 1
        if index == len(module):
            raise AuditInputError(f"ILean metadata has an invalid module name: {display_path}")
    if not parts:
        raise AuditInputError(f"ILean metadata has an invalid module name: {display_path}")
    return tuple(parts)


def render_probe(modules: tuple[str, ...]) -> str:
    """Render the Lean program that audits exactly *modules*."""

    if not modules:
        raise AuditInputError("refusing to render an empty kernel-trust audit")
    imports = "\n".join(f"import {module}" for module in modules)
    target_modules = ", ".join(_lean_name(module) for module in modules)
    return f"""{imports}
import Lean.Util.CollectAxioms
import Lean.Elab.Command

open Lean Elab Command

run_cmd do
  let targetModules : List Name := [{target_modules}]
  let allowed : List Name := [``propext, ``Classical.choice, ``Quot.sound]
  let env ← getEnv
  let mut checked : Nat := 0
  let mut badSafety : Array Name := #[]
  let mut badAxioms : Array (Name × Name) := #[]
  for (declName, info) in env.constants do
    if let some moduleIdx := env.getModuleIdxFor? declName then
      let moduleName := env.header.moduleNames[moduleIdx.toNat]!
      if targetModules.contains moduleName then
        checked := checked + 1
        if info.isUnsafe || info.isPartial then
          badSafety := badSafety.push declName
        for usedAxiom in (← Lean.collectAxioms declName) do
          unless allowed.contains usedAxiom do
            badAxioms := badAxioms.push (declName, usedAxiom)
  for declName in badSafety do
    logError m!"unsafe or partial declaration: {{declName}}"
  for (declName, usedAxiom) in badAxioms do
    logError m!"{{declName}} depends on unexpected axiom {{usedAxiom}}"
  if checked == 0 then
    throwError "kernel-trust audit found no root-package declarations"
  unless badSafety.isEmpty && badAxioms.isEmpty do
    throwError "root-package declarations failed the kernel-trust audit"
  logInfo m!"kernel trust clean ({{checked}} root-package declaration(s) audited)"
"""


class ArticleDeclaration(NamedTuple):
    """One ``lean:`` declaration of a stated article, as the contract records it."""

    name: str
    article: str
    is_open: bool
    allowed: tuple[str, ...]


def load_assumption_contract(path: Path) -> tuple[ArticleDeclaration, ...]:
    """Read ``autoform work assumptions --json`` output and refuse anything unexpected.

    The Markdown is the only authority on which statements are open: an open
    article's theorems may keep a ``sorry`` proof, and each article's
    declarations may rest only on the open statements its Markdown
    dependencies reach.
    """

    try:
        contract = json.loads(
            path.read_bytes().decode("utf-8"),
            object_pairs_hook=_unique_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, ValueError) as exc:
        raise AuditInputError(f"cannot read the assumption contract: {exc}") from exc
    if not isinstance(contract, dict) or contract.get("schema") != _ASSUMPTIONS_SCHEMA:
        raise AuditInputError(f"the assumption contract is not an {_ASSUMPTIONS_SCHEMA} object")
    if contract.get("open_statements") is not True:
        raise AuditInputError(
            "the assumption contract says roadmap/README.md does not allow open statements; "
            "refusing to accept any sorry"
        )
    articles = contract.get("articles")
    if not isinstance(articles, list):
        raise AuditInputError("the assumption contract has no articles list")
    entries: list[ArticleDeclaration] = []
    article_ids: set[str] = set()
    open_owners: dict[str, str] = {}
    proved_owners: dict[str, str] = {}
    for article in articles:
        if not isinstance(article, dict):
            raise AuditInputError("the assumption contract lists an article that is not an object")
        article_id = article.get("id")
        if not isinstance(article_id, str) or not article_id or not _printable(article_id):
            raise AuditInputError(f"the assumption contract lists an invalid article id: {article_id!r}")
        if article_id in article_ids:
            raise AuditInputError(f"the assumption contract lists {article_id} twice")
        article_ids.add(article_id)
        is_open = article.get("open")
        declarations = article.get("declarations")
        allowed = article.get("allowed_open_declarations")
        assumes = article.get("assumes")
        if (
            not isinstance(is_open, bool)
            or not isinstance(declarations, list)
            or not declarations
            or not isinstance(allowed, list)
            or not isinstance(assumes, list)
            or not all(isinstance(assumed, str) for assumed in assumes)
            or not isinstance(article.get("state"), str)
            or not isinstance(article.get("article_id"), (str, type(None)))
        ):
            raise AuditInputError(f"the assumption contract has a malformed entry for {article_id}")
        for name in (*declarations, *allowed):
            _declaration_name_parts(name, article_id)
        owners = open_owners if is_open else proved_owners
        for name in declarations:
            owners.setdefault(name, article_id)
        allowed_names = tuple(dict.fromkeys(allowed))
        for name in dict.fromkeys(declarations):
            entries.append(ArticleDeclaration(name, article_id, is_open, allowed_names))
    for name, owner in open_owners.items():
        if name in proved_owners:
            raise AuditInputError(
                f"{name} is an open statement of {owner} but {proved_owners[name]} records it as proved"
            )
    for entry in entries:
        for name in entry.allowed:
            if name not in open_owners:
                raise AuditInputError(
                    f"the assumption contract lets {entry.article} rest on {name}, "
                    "which no open article declares"
                )
    return tuple(entries)


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant {value}")


def _printable(text: str) -> bool:
    """Reject control characters and lone surrogates before they reach Lean source."""

    return all(unicodedata.category(character) not in {"Cc", "Cs"} for character in text)


def _declaration_name_parts(name: object, article: str) -> tuple[tuple[str, bool], ...]:
    """Parse a declaration name as the CLI does: guillemets quote one component."""

    if not isinstance(name, str) or not _DECLARATION_NAME.fullmatch(name) or not _printable(name):
        raise AuditInputError(f"{article} names an invalid Lean declaration: {name!r}")
    return tuple(
        (quoted, True) if quoted else (plain, False)
        for quoted, plain in re.findall(_DECLARATION_NAME_PART, name)
    )


def render_open_probe(
    modules: tuple[str, ...], declarations: tuple[ArticleDeclaration, ...]
) -> str:
    """Render the Lean program that audits *modules* against the assumption contract.

    The strict audit's rules hold with one exception: a theorem that an open
    article names may keep ``sorry`` in its proof. Whatever reaches such a
    statement is reported as conditional on it, and must belong to an article
    whose Markdown dependencies reach it.
    """

    if not modules:
        raise AuditInputError("refusing to render an empty open-statement audit")
    imports = "\n".join(f"import {module}" for module in modules)
    target_modules = ", ".join(_lean_name(module) for module in modules)
    # One string literal, not a Lean term: a term with thousands of entries
    # exceeds the elaborator's and code generator's recursion limits.
    table = json.dumps(
        [
            [
                _json_declaration_name(entry.name),
                entry.article,
                entry.is_open,
                [_json_declaration_name(name) for name in entry.allowed],
            ]
            for entry in declarations
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    articles = json.dumps(table, ensure_ascii=False)
    return f"""{imports}
import Lean.Util.CollectAxioms
import Lean.Elab.Command
import Lean.Data.Json

open Lean Elab Command

-- No namespace: inside one, Lean resolves a name to a constant under that
-- namespace before any other, so a root module could define, say,
-- `<namespace>.Json.parse` and silently replace the parser. At the top level a
-- root constant with a helper's name fails as already declared, and one that
-- matches a library name makes the reference ambiguous; both fail closed. The
-- parser is fully qualified, so a project's own `Json.parse` does not even do that.

/-- A name spelled as its components: strings, and numbers for numeric ones. -/
def autoformOpenAuditNameOf (json : Json) : Except String Name := do
  let mut name := Name.anonymous
  for part in (← json.getArr?) do
    match part with
    | .str text => name := Name.str name text
    | _ => name := Name.num name (← part.getNat?)
  return name

/-- Per article declaration: its name, its article, whether the article is an
open statement, and the open statements the declaration may rest on. -/
def autoformOpenAuditReadArticles (text : String) : Except String (Array (Name × String × Bool × Array Name)) := do
  (← (← _root_.Lean.Json.parse text).getArr?).mapM fun entry => do
    let declName ← autoformOpenAuditNameOf (← entry.getArrVal? 0)
    let article ← (← entry.getArrVal? 1).getStr?
    let isOpen ← (← entry.getArrVal? 2).getBool?
    let allowedOpen ← (← (← entry.getArrVal? 3).getArr?).mapM autoformOpenAuditNameOf
    return (declName, article, isOpen, allowedOpen)

/-- The types and constructors of one mutual inductive block refer to each
other. Nothing else in a safe environment does, so the walk treats a block as
one node and needs no cycle handling. -/
def autoformOpenAuditBlockOf (env : Environment) (declName : Name) : Name :=
  let induct := match env.find? declName with
    | some (.ctorInfo info) => info.induct
    | _ => declName
  match env.find? induct with
  | some (.inductInfo info) => info.all.head?.getD induct
  | _ => declName

/-- The constants a node refers to, as `collectAxioms` follows them, except that
an open statement's proof is not entered: a dependent rests on the statement,
whatever its proof uses. -/
def autoformOpenAuditEdges (env : Environment) (openSet : Std.HashSet Name) (node : Name) : Array Name :=
  match env.find? node with
  | some (.inductInfo info) =>
    info.all.foldl (init := (#[] : Array Name)) fun used induct =>
      match env.find? induct with
      | some (.inductInfo block) =>
        block.ctors.foldl (init := used ++ block.type.getUsedConstants) fun used ctor =>
          used ++ ((env.find? ctor).map (·.type.getUsedConstants)).getD #[]
      | _ => used
  | some (.thmInfo info) =>
    if openSet.contains node then info.type.getUsedConstants
    else info.type.getUsedConstants ++ info.value.getUsedConstants
  | some (.defnInfo info) => info.type.getUsedConstants ++ info.value.getUsedConstants
  | some (.opaqueInfo info) => info.type.getUsedConstants ++ info.value.getUsedConstants
  | some info => info.type.getUsedConstants
  | none => #[]

/-- The open statements a node rests on. A constant outside the root package
contributes none: the audit requires those to be free of `sorry`. -/
partial def autoformOpenAuditOpenHits (env : Environment) (isRoot : Name → Bool) (openSet : Std.HashSet Name)
    (node : Name) : StateM (Std.HashMap Name (Array Name)) (Array Name) := do
  if let some known := (← get).get? node then
    return known
  -- In progress. Only unsafe recursion comes back here, and the audit rejects it.
  modify (·.insert node #[])
  let mut hits : Array Name := if openSet.contains node then #[node] else #[]
  for used in autoformOpenAuditEdges env openSet node do
    if used == ``sorryAx || !isRoot used then
      continue
    let target := autoformOpenAuditBlockOf env used
    if target == node then
      continue
    for hit in (← autoformOpenAuditOpenHits env isRoot openSet target) do
      unless hits.contains hit do
        hits := hits.push hit
  hits := hits.qsort Name.lt
  modify (·.insert node hits)
  return hits

def autoformOpenAuditNameList (names : Array Name) : MessageData :=
  MessageData.joinSep (names.toList.map MessageData.ofName) ", "

run_cmd do
  let targetModules : List Name := [{target_modules}]
  let allowed : List Name := [``propext, ``Classical.choice, ``Quot.sound]
  let articles ← match autoformOpenAuditReadArticles {articles} with
    | .ok articles => pure articles
    | .error message => throwError "cannot read the article table: {{message}}"
  let env ← getEnv
  let isRoot (declName : Name) : Bool :=
    match env.getModuleIdxFor? declName with
    | some moduleIdx => targetModules.contains env.header.moduleNames[moduleIdx.toNat]!
    | none => false
  let mut openSet : Std.HashSet Name := {{}}
  for (declName, _, isOpen, _) in articles do
    if isOpen && isRoot declName then
      if let some (.thmInfo _) := env.find? declName then
        openSet := openSet.insert declName
  let mut roots : Array Name := #[]
  for (declName, _) in env.constants do
    if isRoot declName then
      roots := roots.push declName
  roots := roots.qsort Name.lt
  let mut errors : Array MessageData := #[]
  let mut hitCache : Std.HashMap Name (Array Name) := {{}}
  let mut externalSorry : Std.HashMap Name Bool := {{}}
  for declName in roots do
    let some info := env.find? declName | continue
    let reported := errors.size
    if info.isUnsafe || info.isPartial then
      errors := errors.push m!"unsafe or partial declaration: {{declName}}"
    let usedAxioms ← Lean.collectAxioms declName
    for usedAxiom in usedAxioms do
      unless usedAxiom == ``sorryAx || allowed.contains usedAxiom do
        errors := errors.push m!"{{declName}} depends on unexpected axiom {{usedAxiom}}"
    let typeConstants := info.type.getUsedConstants
    -- Values are read by kind: `ConstantInfo.value?` hides theorem proofs by
    -- default, and a hidden proof would pass this check vacuously.
    let valueConstants := match info with
      | .thmInfo val => val.value.getUsedConstants
      | .defnInfo val => val.value.getUsedConstants
      | .opaqueInfo val => val.value.getUsedConstants
      | _ => #[]
    if typeConstants.contains ``sorryAx then
      errors := errors.push m!"{{declName}} has sorry in its statement; state the claim in full and keep sorry only in the proof"
    else if valueConstants.contains ``sorryAx && !openSet.contains declName then
      errors := errors.push m!"{{declName}} contains sorry but is not an open statement: only a theorem that an open article's lean: names may keep a sorry, written directly in its own proof, not in a helper, where clause or definition"
    let mut external : Array Name := #[]
    for used in typeConstants ++ valueConstants do
      if used == ``sorryAx || isRoot used || external.contains used then
        continue
      external := external.push used
      let mut tainted := false
      if let some known := externalSorry.get? used then
        tainted := known
      else
        tainted := (← Lean.collectAxioms used).contains ``sorryAx
        externalSorry := externalSorry.insert used tainted
      if tainted then
        errors := errors.push m!"{{declName}} uses {{used}}, which is outside the root package and depends on sorry"
    let (hits, cache) := Id.run ((autoformOpenAuditOpenHits env isRoot openSet
      (autoformOpenAuditBlockOf env declName)).run hitCache)
    hitCache := cache
    if errors.size == reported && usedAxioms.contains ``sorryAx && hits.isEmpty then
      errors := errors.push m!"{{declName}} depends on sorry outside every declared open statement"
  let mut openCount : Nat := 0
  let mut conditionalCount : Nat := 0
  for (declName, article, isOpen, allowedOpen) in articles do
    match env.find? declName with
    | none =>
      errors := errors.push m!"{{declName}} [{{article}}] is not a declaration of the Lean build; fix the article's lean: name or build the module that declares it"
    | some info =>
      if !isRoot declName then
        if (← Lean.collectAxioms declName).contains ``sorryAx then
          errors := errors.push m!"{{declName}} [{{article}}] is outside the root package and depends on sorry"
        else
          logInfo m!"sorry-free: {{declName}} [{{article}}]"
      else
        let (hits, cache) := Id.run ((autoformOpenAuditOpenHits env isRoot openSet
          (autoformOpenAuditBlockOf env declName)).run hitCache)
        hitCache := cache
        let undeclared := hits.filter (fun hit => !allowedOpen.contains hit)
        unless undeclared.isEmpty do
          errors := errors.push m!"{{declName}} [{{article}}] rests on open statement(s) {{autoformOpenAuditNameList undeclared}}, which its article's Markdown dependencies do not reach; add the dependency to the article or stop using them"
        if openSet.contains declName then
          unless isOpen do
            errors := errors.push m!"{{declName}} [{{article}}] is an open statement, but its article records it as proved"
          openCount := openCount + 1
          let ownSorry := match info with
            | .thmInfo val => val.value.getUsedConstants.contains ``sorryAx
            | _ => false
          if ownSorry then
            logInfo m!"open statement (proof is sorry): {{declName}} [{{article}}]"
          else if (← Lean.collectAxioms declName).contains ``sorryAx then
            logInfo m!"open statement (proof depends on sorry elsewhere): {{declName}} [{{article}}]"
          else
            logInfo m!"open statement (proof is sorry-free; record proof: formalized): {{declName}} [{{article}}]"
        else if !hits.isEmpty then
          conditionalCount := conditionalCount + 1
          logInfo m!"conditional: {{declName}} [{{article}}] rests on open statement(s) {{autoformOpenAuditNameList hits}}"
        else unless (← Lean.collectAxioms declName).contains ``sorryAx do
          logInfo m!"sorry-free: {{declName}} [{{article}}]"
  for error in errors do
    logError error
  if roots.isEmpty then
    throwError "open-statement audit found no root-package declarations"
  unless errors.isEmpty do
    throwError "root-package declarations failed the open-statement audit"
  logInfo m!"kernel trust clean except declared open statements ({{roots.size}} root-package declaration(s) audited; {{openCount}} open statement(s), {{conditionalCount}} conditional declaration(s))"
"""


def _lean_name(module: str) -> str:
    result = "Name.anonymous"
    for part in _module_parts(module, module):
        result = f"Name.str ({result}) {json.dumps(part)}"
    return result


def _json_declaration_name(name: str) -> list[str | int]:
    """Spell a declaration name as its components, as the skeleton probe does."""

    return [
        int(part) if not quoted and part.isascii() and part.isdigit() else part
        for part, quoted in _declaration_name_parts(name, name)
    ]


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) == 2 and arguments[0] in {"--root-package", "--policy"}:
        read = root_package_from_config if arguments[0] == "--root-package" else blueprint_policy
        try:
            print(read(Path(arguments[1])))
        except AuditInputError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    contract: Path | None = None
    if len(arguments) == 5 and arguments[0] == "--open-statements":
        contract = Path(arguments[1])
        arguments = arguments[2:]
    if len(arguments) != 3 or arguments[0] in {"--policy", "--open-statements"}:
        print(
            "usage: autoform_audit.py --root-package EVALUATED_CONFIG\n"
            "   or: autoform_audit.py --policy BLUEPRINT_DIR\n"
            "   or: autoform_audit.py ROOT_PACKAGE ROOT_BUILD_ARCHIVE OUTPUT_PROBE\n"
            "   or: autoform_audit.py --open-statements CONTRACT ROOT_PACKAGE ROOT_BUILD_ARCHIVE OUTPUT_PROBE",
            file=sys.stderr,
        )
        return 2
    root_package = arguments[0]
    archive, output = map(Path, arguments[1:])
    try:
        modules = modules_from_archive(archive, root_package)
        if contract is None:
            probe = render_probe(modules)
        else:
            declarations = load_assumption_contract(contract)
            probe = render_open_probe(modules, declarations)
        output.write_text(probe, encoding="utf-8")
    except (AuditInputError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if contract is None:
        print(f"prepared kernel-trust audit for {len(modules)} root-package module(s)")
    else:
        candidates = len({entry.name for entry in declarations if entry.is_open})
        print(
            f"prepared open-statement audit for {len(modules)} root-package module(s) "
            f"and {candidates} open statement candidate(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
