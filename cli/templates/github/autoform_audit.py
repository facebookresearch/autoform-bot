#!/usr/bin/env python3
"""Build a kernel-trust probe from the root package's packed ILean artifacts."""

from __future__ import annotations

import json
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath

_MAX_ILEAN_BYTES = 16 * 1024 * 1024
_TOP_LEVEL_NAME = re.compile(r'^name\s*=\s*("(?:[^"\\]|\\.)*")\s*(?:#.*)?$')


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


def _lean_name(module: str) -> str:
    result = "Name.anonymous"
    for part in _module_parts(module, module):
        result = f"Name.str ({result}) {json.dumps(part)}"
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) == 2 and arguments[0] == "--root-package":
        try:
            print(root_package_from_config(Path(arguments[1])))
        except AuditInputError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    if len(arguments) != 3:
        print(
            "usage: autoform_audit.py --root-package EVALUATED_CONFIG\n"
            "   or: autoform_audit.py ROOT_PACKAGE ROOT_BUILD_ARCHIVE OUTPUT_PROBE",
            file=sys.stderr,
        )
        return 2
    root_package = arguments[0]
    archive, output = map(Path, arguments[1:])
    try:
        modules = modules_from_archive(archive, root_package)
        probe = render_probe(modules)
        output.write_text(probe, encoding="utf-8")
    except (AuditInputError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"prepared kernel-trust audit for {len(modules)} root-package module(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
