#!/usr/bin/env python3
"""Bind Autoform blueprint claims to built Lean and Mathlib artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from autoform_cli.graph import GraphValidationError, load_graph
from autoform_cli.lean import declaration_names

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

try:
    from autoform_cli.lean import declaration_kind, mathlib_module_name
except ImportError:
    # A checked-in helper can be upgraded one commit before its immutable
    # workflow pin. Keep that transition fail-closed while the pin catches up.
    from autoform_cli.audit import _DECLARATION_KEYWORDS as _LEGACY_DECLARATION_KEYWORDS

    def declaration_kind(intent: str | None) -> str | None:
        if intent is None:
            return None
        keywords = _LEGACY_DECLARATION_KEYWORDS.get(intent.strip().casefold())
        if keywords == frozenset({"lemma", "theorem"}):
            return "theorem"
        return next(iter(keywords)) if keywords is not None and len(keywords) == 1 else None

    def mathlib_module_name(source_file: str) -> str | None:
        if not source_file or "\\" in source_file:
            return None
        path = PurePosixPath(source_file)
        if path.is_absolute() or path.as_posix() != source_file:
            return None
        parts = path.parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            return None
        if parts[0] != "Mathlib" and parts[0] != "Mathlib.lean":
            return None
        if not parts[-1].endswith(".lean") or parts[-1] == ".lean":
            return None
        module_parts = [*parts[:-1], parts[-1][: -len(".lean")]]
        if not module_parts or module_parts[0] != "Mathlib":
            return None
        for part in module_parts:
            if not part or not (part[0].isalpha() or part[0] == "_"):
                return None
            if any(not (character.isalnum() or character in "_'") for character in part):
                return None
        return ".".join(module_parts)

_MAX_ILEAN_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_AUDIT_COMMAND_TIMEOUT_SECONDS = 5 * 60
_CANONICAL_MATHLIB_URL = "https://github.com/leanprover-community/mathlib4.git"
_FULL_GIT_REVISION = re.compile(r"[0-9a-f]{40}")
_LAKE_HASH = re.compile(r"[0-9a-f]{16}")
_CACHE_SCHEMA_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TOP_LEVEL_NAME = re.compile(r'^name\s*=\s*("(?:[^"\\]|\\.)*")\s*(?:#.*)?$')


class AuditInputError(ValueError):
    """The root package configuration or artifacts are not safe to audit."""


@dataclass(frozen=True, slots=True)
class BlueprintTarget:
    """One declaration claim loaded from the canonical Markdown graph."""

    article_path: str
    name: str
    expected_kind: str
    owner: str
    expected_module: str | None = None


@dataclass(frozen=True, slots=True)
class _MathlibCheckout:
    project: Path
    checkout: Path
    build_root: Path
    revision: str
    manifest_digest: str


@dataclass(frozen=True, slots=True)
class _FileSignature:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


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


def mathlib_modules_from_lake(
    lean_root: Path,
    targets: tuple[BlueprintTarget, ...],
    *,
    runner: CommandRunner | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Resolve claims through the manifest-pinned canonical Mathlib checkout."""

    command_runner = runner or subprocess.run
    modules = tuple(
        sorted(
            {
                target.expected_module
                for target in targets
                if target.owner == "mathlib" and target.expected_module is not None
            }
        )
    )
    if not modules:
        return ()
    checkout = _mathlib_checkout_from_manifest(
        lean_root,
        runner=command_runner,
        environment=environment,
    )
    signatures: dict[Path, _FileSignature] = {}

    for module in modules:
        target = f"@mathlib/+{module}:ilean"
        try:
            queried = command_runner(
                ["lake", "query", "--json", target],
                cwd=checkout.project,
                capture_output=True,
                text=True,
                check=False,
                env=_audit_subprocess_environment(environment),
                shell=False,
                timeout=_AUDIT_COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AuditInputError(f"cannot query Lake package id 'mathlib': {exc}") from exc
        if queried.returncode != 0:
            detail = queried.stderr.strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise AuditInputError(
                f"Lake package id 'mathlib' does not provide module {module!r}{suffix}"
            )
        try:
            ilean_value = json.loads(queried.stdout)
        except json.JSONDecodeError as exc:
            raise AuditInputError(
                f"Lake returned invalid artifact metadata for package id 'mathlib' module "
                f"{module!r}"
            ) from exc
        if not isinstance(ilean_value, str) or not ilean_value.endswith(".ilean"):
            raise AuditInputError(
                f"Lake returned no ILean artifact for package id 'mathlib' module {module!r}"
            )
        ilean = Path(ilean_value)
        if not ilean.is_absolute():
            ilean = checkout.project / ilean
        signatures.update(
            _validate_mathlib_artifacts(
                ilean,
                module,
                checkout,
                runner=command_runner,
                environment=environment,
            )
        )

    if _mathlib_checkout_from_manifest(
        checkout.project,
        runner=command_runner,
        environment=environment,
    ) != checkout:
        raise AuditInputError("Mathlib manifest or checkout changed during artifact validation")
    for path, expected in signatures.items():
        if _regular_file_signature(path, "Mathlib build artifact") != expected:
            raise AuditInputError(f"Mathlib build artifact changed during validation: {path}")
    return modules


def _mathlib_checkout_from_manifest(
    lean_root: Path,
    *,
    runner: CommandRunner | None = None,
    environment: Mapping[str, str] | None = None,
) -> _MathlibCheckout:
    command_runner = runner or subprocess.run
    if lean_root.is_symlink():
        raise AuditInputError("Lean project root must not be a symbolic link")
    try:
        project = lean_root.resolve(strict=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditInputError(f"cannot resolve Lean project root: {exc}") from exc
    if not project.is_dir():
        raise AuditInputError(f"Lean project root is not a directory: {project}")

    manifest_path = project / "lake-manifest.json"
    manifest_bytes, _ = _read_stable_regular_file(
        manifest_path, "Lake manifest", _MAX_MANIFEST_BYTES
    )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AuditInputError(f"malformed Lake manifest: {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("packages"), list):
        raise AuditInputError("Lake manifest must contain a package list")
    entries = [
        entry
        for entry in manifest["packages"]
        if isinstance(entry, dict) and entry.get("name") == "mathlib"
    ]
    if len(entries) != 1:
        raise AuditInputError("Lake manifest must contain exactly one mathlib package entry")
    entry = entries[0]
    if entry.get("scope") != "":
        raise AuditInputError("Lake manifest Mathlib entry must have the exact package id 'mathlib'")
    if entry.get("type") != "git":
        raise AuditInputError("Lake manifest Mathlib entry must be a Git dependency")
    if entry.get("url") != _CANONICAL_MATHLIB_URL:
        raise AuditInputError(
            f"Lake manifest Mathlib URL must be exactly {_CANONICAL_MATHLIB_URL}"
        )
    revision = entry.get("rev")
    if not isinstance(revision, str) or _FULL_GIT_REVISION.fullmatch(revision) is None:
        raise AuditInputError("Lake manifest Mathlib revision must be a full 40-hex commit")
    if entry.get("subDir") is not None:
        raise AuditInputError("Lake manifest Mathlib dependency must not select a subdirectory")

    packages_dir_value = manifest.get("packagesDir")
    packages_dir = _safe_relative_path(packages_dir_value, "Lake packagesDir")
    packages_root = _resolved_child_directory(project, packages_dir, "Lake packages directory")
    checkout_path = packages_root / "mathlib"
    checkout = _resolved_child_directory(project, checkout_path.relative_to(project), "Mathlib checkout")
    git_marker = checkout / ".git"
    try:
        marker_status = git_marker.lstat()
    except OSError as exc:
        raise AuditInputError(f"Mathlib checkout has no readable .git directory: {checkout}") from exc
    if stat.S_ISLNK(marker_status.st_mode) or not stat.S_ISDIR(marker_status.st_mode):
        raise AuditInputError(f"Mathlib checkout .git must be a real directory: {git_marker}")
    _validate_git_repository_metadata(
        checkout,
        git_marker,
        runner=command_runner,
        environment=environment,
    )

    top = Path(
        _git_output(
            checkout,
            "rev-parse",
            "--show-toplevel",
            label="top level",
            runner=command_runner,
            environment=environment,
        )
    )
    try:
        top = top.resolve(strict=True)
    except OSError as exc:
        raise AuditInputError(f"cannot resolve Mathlib Git top level: {exc}") from exc
    if top != checkout:
        raise AuditInputError("Mathlib package directory is not the Git checkout root")
    head = _git_output(
        checkout,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        label="HEAD",
        runner=command_runner,
        environment=environment,
    )
    if head != revision:
        raise AuditInputError(
            f"Mathlib checkout HEAD {head!r} does not match manifest revision {revision!r}"
        )
    remotes = _git_output(
        checkout,
        "remote",
        label="remote list",
        runner=command_runner,
        environment=environment,
    ).splitlines()
    if remotes != ["origin"]:
        raise AuditInputError("Mathlib checkout must have exactly one Git remote named origin")
    remote_urls = _git_output(
        checkout,
        "config",
        "--local",
        "--get-all",
        "remote.origin.url",
        label="origin URL",
        runner=command_runner,
        environment=environment,
    ).splitlines()
    if remote_urls != [_CANONICAL_MATHLIB_URL]:
        raise AuditInputError(
            f"Mathlib checkout origin must be exactly {_CANONICAL_MATHLIB_URL}"
        )
    status = _git_output(
        checkout,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        label="status",
        allow_empty=True,
        runner=command_runner,
        environment=environment,
    )
    if status:
        raise AuditInputError("Mathlib checkout is dirty")
    untracked_outside_build = _git_output(
        checkout,
        "ls-files",
        "--others",
        "--",
        ":!.lake/**",
        label="untracked files",
        allow_empty=True,
        runner=command_runner,
        environment=environment,
    )
    if untracked_outside_build:
        raise AuditInputError("Mathlib checkout has untracked files outside .lake")
    tracked_flags = _git_output(
        checkout,
        "ls-files",
        "-v",
        label="index flags",
        allow_empty=True,
        runner=command_runner,
        environment=environment,
    ).splitlines()
    if any(not line.startswith("H ") for line in tracked_flags):
        raise AuditInputError("Mathlib checkout uses nonstandard Git index flags")

    return _MathlibCheckout(
        project=project,
        checkout=checkout,
        build_root=checkout / ".lake/build",
        revision=revision,
        manifest_digest=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def _safe_relative_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise AuditInputError(f"{label} must be a nonempty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise AuditInputError(f"{label} must be a confined relative path")
    return Path(*path.parts)


def _resolved_child_directory(root: Path, relative: Path, label: str) -> Path:
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise AuditInputError(f"{label} escapes its owning directory")
    current = root
    for part in relative.parts:
        current = current / part
        try:
            status = current.lstat()
        except OSError as exc:
            raise AuditInputError(f"{label} does not exist: {current}") from exc
        if stat.S_ISLNK(status.st_mode):
            raise AuditInputError(f"{label} contains a symbolic link: {current}")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AuditInputError(f"{label} escapes its owning directory: {current}") from exc
    if not resolved.is_dir():
        raise AuditInputError(f"{label} is not a directory: {resolved}")
    return resolved


def _git_output(
    checkout: Path,
    *arguments: str,
    label: str,
    allow_empty: bool = False,
    runner: CommandRunner | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    try:
        command_runner = runner or subprocess.run
        result = command_runner(
            [
                "git",
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-C",
                str(checkout),
                *arguments,
            ],
            capture_output=True,
            text=True,
            check=False,
            env=_audit_subprocess_environment(environment),
            shell=False,
            timeout=_AUDIT_COMMAND_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        raise AuditInputError(f"cannot inspect Mathlib Git {label}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise AuditInputError(f"cannot inspect Mathlib Git {label}{suffix}")
    output = result.stdout.rstrip("\n")
    if not allow_empty and not output:
        raise AuditInputError(f"Mathlib Git {label} is empty")
    return output


def _audit_subprocess_environment(
    supplied: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the host environment without caller-controlled Git behavior."""

    source = os.environ if supplied is None else supplied
    environment = {key: value for key, value in source.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_COUNT": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_GRAFT_FILE": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _validate_git_repository_metadata(
    checkout: Path,
    git_directory: Path,
    *,
    runner: CommandRunner | None = None,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Reject Git metadata that can rewrite objects or run local programs."""

    command_runner = runner or subprocess.run
    object_directory = _resolved_child_directory(
        git_directory, Path("objects"), "Mathlib Git object database"
    )
    _reject_tree_symlinks(object_directory, "Mathlib Git object database")
    forbidden = (
        (Path("commondir"), "alternate common directory"),
        (Path("config.worktree"), "worktree-specific configuration"),
        (Path("info/attributes"), "private attributes file"),
        (Path("info/grafts"), "graft file"),
        (Path("objects/info/alternates"), "alternate object database"),
        (Path("objects/info/http-alternates"), "HTTP alternate object database"),
    )
    for relative, description in forbidden:
        path = git_directory / relative
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AuditInputError(
                f"cannot inspect Mathlib Git {description}: {path}: {exc}"
            ) from exc
        raise AuditInputError(f"Mathlib checkout uses a Git {description}: {path}")

    config_keys = _git_output(
        checkout,
        "config",
        "--local",
        "--name-only",
        "--list",
        label="local configuration",
        allow_empty=True,
        runner=command_runner,
        environment=environment,
    ).splitlines()
    unsafe_config = sorted(
        key
        for key in config_keys
        if key.casefold().startswith("filter.")
        or key.casefold()
        in {
            "core.attributesfile",
            "include.path",
            "extensions.worktreeconfig",
        }
        or key.casefold().startswith("includeif.")
    )
    if unsafe_config:
        raise AuditInputError(
            "Mathlib checkout uses unsafe local Git configuration: "
            + ", ".join(unsafe_config)
        )

    replacements = _git_output(
        checkout,
        "for-each-ref",
        "--format=%(refname)",
        "refs/replace/",
        label="replacement references",
        allow_empty=True,
        runner=command_runner,
        environment=environment,
    )
    if replacements:
        raise AuditInputError("Mathlib checkout uses Git replacement references")


def _reject_tree_symlinks(root: Path, label: str) -> None:
    try:
        for directory, names, files in os.walk(root, followlinks=False):
            base = Path(directory)
            for name in (*names, *files):
                path = base / name
                if path.is_symlink():
                    raise AuditInputError(f"{label} contains a symbolic link: {path}")
    except OSError as exc:
        raise AuditInputError(f"cannot inspect {label}: {root}: {exc}") from exc


def _validate_mathlib_artifacts(
    ilean: Path,
    module: str,
    checkout: _MathlibCheckout,
    *,
    runner: CommandRunner | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[Path, _FileSignature]:
    command_runner = runner or subprocess.run
    if any(part == ".." for part in ilean.parts):
        raise AuditInputError(f"Mathlib ILean artifact path contains traversal: {ilean}")
    build_root = _resolved_child_directory(
        checkout.checkout, Path(".lake/build"), "Mathlib build directory"
    )
    try:
        relative = ilean.relative_to(build_root)
    except ValueError as exc:
        raise AuditInputError(
            f"Mathlib ILean artifact is outside the validated checkout build root: {ilean}"
        ) from exc
    expected_parts = _module_parts(module, str(ilean))
    source_relative = Path(*expected_parts[:-1], f"{expected_parts[-1]}.lean")
    _reject_path_symlinks(checkout.checkout, source_relative, "Mathlib source module")
    tracked_source = _git_output(
        checkout.checkout,
        "ls-files",
        "--error-unmatch",
        "--",
        source_relative.as_posix(),
        label=f"tracked source for {module}",
        runner=command_runner,
        environment=environment,
    )
    if tracked_source != source_relative.as_posix():
        raise AuditInputError(f"Mathlib module is not tracked at the pinned revision: {module}")
    source = checkout.checkout / source_relative
    source_signature = _regular_file_signature(source, "Mathlib source module")
    expected = Path("lib", "lean", *expected_parts[:-1], f"{expected_parts[-1]}.ilean")
    if relative != expected:
        raise AuditInputError(
            f"Mathlib ILean artifact path does not match module {module!r}: {ilean}"
        )
    _reject_path_symlinks(build_root, relative, "Mathlib ILean artifact")

    display_path = str(ilean)
    metadata_bytes, ilean_signature = _read_stable_regular_file(
        ilean, "Mathlib ILean", _MAX_ILEAN_BYTES
    )
    try:
        metadata = json.loads(metadata_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AuditInputError(f"malformed Mathlib ILean metadata in {ilean}: {exc}") from exc
    actual_module = _module_from_metadata(metadata, ilean.parts, display_path)
    if actual_module != module:
        raise AuditInputError(
            f"Mathlib ILean artifact identifies module {actual_module!r}, not {module!r}: "
            f"{display_path}"
        )

    olean = ilean.with_suffix(".olean")
    _reject_path_symlinks(build_root, olean.relative_to(build_root), "Mathlib OLean artifact")
    olean_signature = _regular_file_signature(olean, "Mathlib OLean artifact")
    trace = ilean.with_suffix(".trace")
    _reject_path_symlinks(build_root, trace.relative_to(build_root), "Mathlib Lake trace")
    trace_bytes, trace_signature = _read_stable_regular_file(trace, "Mathlib Lake trace")
    try:
        trace_metadata = json.loads(trace_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AuditInputError(f"malformed Mathlib Lake trace metadata in {trace}: {exc}") from exc
    _validate_mathlib_trace(trace_metadata, module, str(trace))
    return {
        source: source_signature,
        ilean: ilean_signature,
        olean: olean_signature,
        trace: trace_signature,
    }


def _validate_mathlib_trace(trace: object, module: str, display_path: str) -> None:
    if isinstance(trace, dict) and trace.get("synthetic") is False:
        _validate_trace(trace, module, "mathlib", display_path)
        return
    if not isinstance(trace, dict) or "synthetic" in trace:
        raise AuditInputError(f"invalid Mathlib Lake trace metadata: {display_path}")
    outputs = trace.get("outputs")
    dep_hash = trace.get("depHash")
    schema = trace.get("schemaVersion")
    if (
        not isinstance(schema, str)
        or not _is_iso_date(schema)
        or not isinstance(dep_hash, str)
        or _LAKE_HASH.fullmatch(dep_hash) is None
        or not isinstance(outputs, dict)
    ):
        raise AuditInputError(f"invalid cached Mathlib Lake trace metadata: {display_path}")
    ilean_output = outputs.get("i")
    olean_outputs = outputs.get("o")
    if (
        not isinstance(ilean_output, str)
        or re.fullmatch(r"[0-9a-f]{16}\.ilean", ilean_output) is None
        or not isinstance(olean_outputs, list)
        or not any(
            isinstance(output, str)
            and re.fullmatch(r"[0-9a-f]{16}\.olean", output) is not None
            for output in olean_outputs
        )
    ):
        raise AuditInputError(f"invalid cached Mathlib Lake trace outputs: {display_path}")


def _is_iso_date(value: str) -> bool:
    if _CACHE_SCHEMA_DATE.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _reject_path_symlinks(root: Path, relative: Path, label: str) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            status = current.lstat()
        except OSError as exc:
            raise AuditInputError(f"{label} does not exist: {current}") from exc
        if stat.S_ISLNK(status.st_mode):
            raise AuditInputError(f"{label} must not contain a symbolic link: {current}")


def _read_stable_regular_file(
    path: Path, kind: str, maximum_bytes: int | None = None
) -> tuple[bytes, _FileSignature]:
    try:
        path_status = path.lstat()
    except OSError as exc:
        raise AuditInputError(f"cannot inspect {kind}: {path}: {exc}") from exc
    if stat.S_ISLNK(path_status.st_mode):
        raise AuditInputError(f"{kind} must not be a symbolic link: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuditInputError(f"cannot open {kind} as a regular file: {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AuditInputError(f"{kind} is not a regular file: {path}")
        if (before.st_dev, before.st_ino) != (path_status.st_dev, path_status.st_ino):
            raise AuditInputError(f"{kind} changed before it was opened: {path}")
        if maximum_bytes is not None and before.st_size > maximum_bytes:
            raise AuditInputError(f"{kind} is unexpectedly large: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            if maximum_bytes is not None and sum(map(len, chunks)) > maximum_bytes:
                raise AuditInputError(f"{kind} is unexpectedly large: {path}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    before_signature = _signature(before)
    if _signature(after) != before_signature:
        raise AuditInputError(f"{kind} changed while it was read: {path}")
    return b"".join(chunks), before_signature


def _regular_file_signature(path: Path, kind: str) -> _FileSignature:
    try:
        path_status = path.lstat()
    except OSError as exc:
        raise AuditInputError(f"cannot inspect {kind}: {path}: {exc}") from exc
    if stat.S_ISLNK(path_status.st_mode):
        raise AuditInputError(f"{kind} must not be a symbolic link: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuditInputError(f"cannot open {kind} as a regular file: {path}: {exc}") from exc
    try:
        status = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(status.st_mode):
        raise AuditInputError(f"{kind} is not a regular file: {path}")
    if (status.st_dev, status.st_ino) != (path_status.st_dev, path_status.st_ino):
        raise AuditInputError(f"{kind} changed before it was opened: {path}")
    return _signature(status)


def _signature(status: os.stat_result) -> _FileSignature:
    return _FileSignature(
        device=status.st_dev,
        inode=status.st_ino,
        size=status.st_size,
        modified_ns=status.st_mtime_ns,
        changed_ns=status.st_ctime_ns,
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


def targets_from_blueprint(blueprint: Path) -> tuple[BlueprintTarget, ...]:
    """Load declaration claims from Autoform's canonical Markdown graph."""

    try:
        graph = load_graph(blueprint)
    except GraphValidationError as exc:
        raise AuditInputError("blueprint is invalid: " + "; ".join(exc.issues)) from exc

    targets: list[BlueprintTarget] = []
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        try:
            article_path = node.path.relative_to(graph.blueprint_dir).as_posix()
        except ValueError as exc:
            raise AuditInputError(f"{node_id}: article path escapes the blueprint") from exc

        local_names = declaration_names(node.lean or "")
        mathlib_names = declaration_names(node.mathlib_declaration or "") if node.mathlib else []
        if (
            (node.statement_formalized or node.proof_formalized)
            and not local_names
            and not node.mathlib
        ):
            raise AuditInputError(
                f"{article_path}: formalized local work has no lean declaration target"
            )
        if not local_names and not mathlib_names and not node.mathlib:
            continue

        expected_kind = declaration_kind(node.declaration)
        if expected_kind is None:
            value = node.declaration or ""
            raise AuditInputError(
                f"{article_path}: declaration intent is missing or unsupported: {value!r}"
            )

        for name in local_names:
            _validate_blueprint_name(name, article_path)
            targets.append(BlueprintTarget(article_path, name, expected_kind, "root"))

        if node.mathlib:
            if not mathlib_names:
                raise AuditInputError(
                    f"{article_path}: mathlib is true but mathlib_declaration is missing"
                )
            if not node.mathlib_file:
                raise AuditInputError(f"{article_path}: mathlib is true but mathlib_file is missing")
            module = mathlib_module_name(node.mathlib_file)
            if module is None:
                raise AuditInputError(
                    f"{article_path}: mathlib_file must be a canonical Mathlib/**/*.lean source path"
                )
            for name in mathlib_names:
                _validate_blueprint_name(name, article_path)
                targets.append(
                    BlueprintTarget(article_path, name, expected_kind, "mathlib", module)
                )

    return tuple(
        sorted(
            targets,
            key=lambda target: (
                target.article_path,
                target.owner,
                target.name,
                target.expected_kind,
                target.expected_module or "",
            ),
        )
    )


def _validate_blueprint_name(name: str, article_path: str) -> None:
    try:
        _module_parts(name, article_path)
    except AuditInputError as exc:
        raise AuditInputError(
            f"{article_path}: invalid Lean declaration name in blueprint: {name!r}"
        ) from exc


def render_probe(
    modules: tuple[str, ...],
    targets: tuple[BlueprintTarget, ...] = (),
    mathlib_modules: tuple[str, ...] = (),
) -> str:
    """Render the Lean program that audits *modules* and blueprint claims."""

    if not modules:
        raise AuditInputError("refusing to render an empty kernel-trust audit")
    required_mathlib_modules = {
        target.expected_module
        for target in targets
        if target.owner == "mathlib" and target.expected_module is not None
    }
    missing_mathlib_modules = required_mathlib_modules - set(mathlib_modules)
    if missing_mathlib_modules:
        missing = ", ".join(sorted(missing_mathlib_modules))
        raise AuditInputError(
            "Mathlib blueprint modules lack build trace metadata from Lake package id "
            f"'mathlib': {missing}"
        )
    imports = "\n".join(
        f"import {module}" for module in sorted(set(modules) | required_mathlib_modules)
    )
    target_modules = ", ".join(_lean_name(module) for module in modules)
    validated_mathlib_modules = ", ".join(_lean_name(module) for module in mathlib_modules)
    local_targets = ", ".join(
        f"({json.dumps(target.article_path, ensure_ascii=False)}, {_lean_name(target.name)}, "
        f"{json.dumps(target.expected_kind, ensure_ascii=False)})"
        for target in targets
        if target.owner == "root"
    )
    mathlib_targets = ", ".join(
        f"({json.dumps(target.article_path, ensure_ascii=False)}, {_lean_name(target.name)}, "
        f"{json.dumps(target.expected_kind, ensure_ascii=False)}, "
        f"{_lean_name(target.expected_module or '')})"
        for target in targets
        if target.owner == "mathlib"
    )
    return f"""{imports}
import Lean.Util.CollectAxioms
import Lean.Elab.Command
import Lean.Meta.Instances
import Lean.OriginalConstKind
import Lean.Structure
import Lean.Class

open Lean Elab Command

private def declaringModule? (env : Environment) (declName : Name) : Option Name := do
  let moduleIdx ← env.getModuleIdxFor? declName
  env.header.moduleNames[moduleIdx.toNat]?

private def matchesDeclarationKind
    (env : Environment) (declName : Name) (expected : String) : Bool :=
  match expected with
  | "theorem" => getOriginalConstKind? env declName == some .thm
  | "axiom" => getOriginalConstKind? env declName == some .axiom
  | "opaque" => getOriginalConstKind? env declName == some .opaque
  | "abbrev" =>
      match env.find? declName with
      | some (.defnInfo info) => info.hints == .abbrev
      | _ => false
  | "def" =>
      match env.find? declName with
      | some (.defnInfo info) => info.hints != .abbrev
      | _ => false
  | "instance" => Meta.isInstanceCore env declName
  | "class" => isClass env declName
  | "structure" => isStructure env declName && !isClass env declName
  | "inductive" =>
      getOriginalConstKind? env declName == some .induct && !isStructure env declName
  | _ => false

run_cmd do
  let rootModules : List Name := [{target_modules}]
  let mathlibModules : List Name := [{validated_mathlib_modules}]
  let localTargets : List (String × Name × String) := [{local_targets}]
  let mathlibTargets : List (String × Name × String × Name) := [{mathlib_targets}]
  let allowed : List Name := [``propext, ``Classical.choice, ``Quot.sound]
  let env ← getEnv
  let mut badTargets := false
  for (article, declName, expectedKind) in localTargets do
    if env.find? declName |>.isNone then
      badTargets := true
      logError m!"{{article}}: local declaration does not exist: {{declName}}"
    else
      match declaringModule? env declName with
      | none =>
          badTargets := true
          logError m!"{{article}}: local declaration has no declaring module: {{declName}}"
      | some moduleName =>
          unless rootModules.contains moduleName do
            badTargets := true
            logError m!"{{article}}: local declaration {{declName}} belongs to non-root module {{moduleName}}"
      unless matchesDeclarationKind env declName expectedKind do
        badTargets := true
        logError m!"{{article}}: declaration {{declName}} does not have expected kind {{expectedKind}}"
  for (article, declName, expectedKind, expectedModule) in mathlibTargets do
    if env.find? declName |>.isNone then
      badTargets := true
      logError m!"{{article}}: Mathlib declaration does not exist: {{declName}}"
    else
      match declaringModule? env declName with
      | none =>
          badTargets := true
          logError m!"{{article}}: Mathlib declaration has no declaring module: {{declName}}"
      | some moduleName =>
          if rootModules.contains moduleName then
            badTargets := true
            logError m!"{{article}}: Mathlib declaration {{declName}} is owned by root module {{moduleName}}"
          unless mathlibModules.contains moduleName do
            badTargets := true
            logError m!"{{article}}: Mathlib declaration {{declName}} is not backed by Lake package id mathlib build metadata"
          if moduleName != expectedModule then
            badTargets := true
            logError m!"{{article}}: Mathlib declaration {{declName}} belongs to {{moduleName}}, not {{expectedModule}}"
      unless matchesDeclarationKind env declName expectedKind do
        badTargets := true
        logError m!"{{article}}: declaration {{declName}} does not have expected kind {{expectedKind}}"
  let mut checked : Nat := 0
  let mut badSafety : Array Name := #[]
  let mut badAxioms : Array (Name × Name) := #[]
  for (declName, info) in env.constants do
    if let some moduleIdx := env.getModuleIdxFor? declName then
      let moduleName := env.header.moduleNames[moduleIdx.toNat]!
      if rootModules.contains moduleName then
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
  unless !badTargets && badSafety.isEmpty && badAxioms.isEmpty do
    throwError "blueprint or root-package declarations failed the artifact audit"
  logInfo m!"artifact audit clean ({{checked}} root-package declaration(s) audited)"
"""


def _lean_name(module: str) -> str:
    result = "Name.anonymous"
    for part in _module_parts(module, module):
        result = f"Name.str ({result}) {json.dumps(part, ensure_ascii=False)}"
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
    if len(arguments) != 5:
        print(
            "usage: autoform_audit.py --root-package EVALUATED_CONFIG\n"
            "   or: autoform_audit.py ROOT_PACKAGE ROOT_BUILD_ARCHIVE BLUEPRINT "
            "LEAN_ROOT OUTPUT_PROBE",
            file=sys.stderr,
        )
        return 2
    root_package = arguments[0]
    archive, blueprint, lean_root, output = map(Path, arguments[1:])
    try:
        modules = modules_from_archive(archive, root_package)
        targets = targets_from_blueprint(blueprint)
        mathlib_modules = mathlib_modules_from_lake(lean_root, targets)
        probe = render_probe(modules, targets, mathlib_modules)
        output.write_text(probe, encoding="utf-8")
    except (AuditInputError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"prepared artifact audit for {len(modules)} root-package module(s) "
        f"and {len(targets)} blueprint declaration claim(s) from "
        f"{len(mathlib_modules)} Mathlib module(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
