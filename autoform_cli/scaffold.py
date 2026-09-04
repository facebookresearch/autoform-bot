"""Write Autoform vault files deterministically instead of describing them.

The legacy project scaffold creates ``blueprint/`` with a landing page,
``roadmap/``, ``coverage/``, and ``sources/`` rather than asking an agent to
imitate the bundled example. Agents improvise: a real project came back with
chapter pages as siblings of their directories rather than as
``<chapter>/README.md``, which parses cleanly and publishes a book with no
chapters at all. The internal vault structure is fixed, so both legacy and
workspace scaffolds write it.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .provenance import normalize_git_source

_TEMPLATES = Path(__file__).resolve().parent / "templates"

#: Template paths whose leading dot is dropped on disk so packaging tools and
#: ignore rules do not swallow them.
_DOTTED = {
    "gitignore": ".gitignore",
    "blueprint/gitignore": "blueprint/.gitignore",
    "github": ".github",
}

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_TEMPLATE_PLACEHOLDER = re.compile(r"\{\{(?P<name>[A-Z][A-Z0-9_]*)\}\}")
_WORKSPACE_MANIFEST = ".autoform.toml"
_DIRECTORY_STAGE_ATTEMPTS = 32
_DIRECTORY_STAGE_PREFIX = ".afd-"


class _DirectoryPublicationError(OSError):
    """A staged directory could not be bound and published safely."""

    def __init__(self, reason: str, staging_name: str | None = None) -> None:
        self.reason = reason
        self.staging_name = staging_name
        super().__init__(reason)


def _normalize_autoform_source(source: str, *, allow_github_scp: bool = False) -> str | None:
    """Compatibility wrapper for explicit workflow-source validation."""

    return normalize_git_source(source, allow_github_scp=allow_github_scp)


def plugin_pin() -> tuple[str, str]:
    """Return verified all-or-nothing provenance for legacy callers."""

    # Import at call time so direct imports of ``autoform_cli.scaffold`` remain
    # independent of the project package's initialization order.
    from .provenance import plugin_pin as verified_plugin_pin

    return verified_plugin_pin()


class ScaffoldError(ValueError):
    """The project could not be scaffolded safely."""

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True, slots=True)
class ScaffoldResult:
    """What a scaffold run wrote, and what it left alone."""

    project: str
    written: tuple[str, ...]
    skipped: tuple[str, ...]
    unpinned: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "project": self.project,
            "written": list(self.written),
            "skipped": list(self.skipped),
            "unpinned": self.unpinned,
        }


@dataclass(frozen=True, slots=True)
class _ScaffoldDirectoryBinding:
    descriptor: int
    identity: tuple[int, int]
    mode: int


@dataclass(frozen=True, slots=True)
class _ScaffoldFileBinding:
    descriptor: int
    identity: tuple[int, int]
    size: int
    sha256: str
    mode: int


@dataclass(slots=True)
class _BlueprintScaffoldBinding:
    """Descriptors and exact entries retained until project registration."""

    root: Path
    root_descriptor: int
    root_identity: tuple[int, int]
    root_mode: int
    root_issue: str
    root_parent_descriptor: int | None
    root_name: str | None
    directories: dict[tuple[str, ...], _ScaffoldDirectoryBinding]
    files: dict[tuple[str, ...], _ScaffoldFileBinding]
    expected_entries: dict[tuple[str, ...], frozenset[str]]
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for file_binding in self.files.values():
            if file_binding is None:
                continue
            try:
                os.close(file_binding.descriptor)
            except OSError:
                pass
        for directory_binding in reversed(tuple(self.directories.values())):
            try:
                os.close(directory_binding.descriptor)
            except OSError:
                pass
        try:
            os.close(self.root_descriptor)
        except OSError:
            pass

    def __del__(self) -> None:
        self.close()


def _destination(relative: str) -> str:
    for template_prefix, real_prefix in _DOTTED.items():
        if relative == template_prefix:
            return real_prefix
        if relative.startswith(f"{template_prefix}/"):
            return real_prefix + relative[len(template_prefix) :]
    return relative


def _yaml_scalar(value: str) -> str:
    """Serialize *value* as a quoted YAML scalar.

    JSON strings are valid YAML double-quoted scalars. Using the standard JSON
    serializer preserves the value while escaping line breaks, tabs, nulls,
    quotes, backslashes, and every other control character that could otherwise
    alter the generated document.
    """
    return json.dumps(value, ensure_ascii=False)


def _render(text: str, substitutions: dict[str, str]) -> str:
    """Substitute tokens from the original template exactly once.

    Replacement values are user-controlled in several templates. A sequential
    series of ``str.replace`` calls can reinterpret token-shaped text inside an
    earlier value, corrupting YAML and Markdown or exposing another generated
    value. A single regex pass never scans replacement content again.
    """

    return _TEMPLATE_PLACEHOLDER.sub(
        lambda match: substitutions.get(match.group("name"), match.group(0)),
        text,
    )


def _atomic_write(destination: Path, content: bytes, *, mode: int) -> None:
    """Replace *destination* from a same-directory temporary file.

    Replacing rather than truncating is essential when an existing destination
    has hard links: ``--force`` must not modify another path to the old inode.
    """

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _within(path: Path, root: Path) -> bool:
    """True when *path* resolves to somewhere at or beneath *root*.

    Resolution follows symlinks, so this is what confines the scaffold: it is
    not enough to reject a symlinked project root, because a link one level
    down -- `project/blueprint` pointing elsewhere -- redirects the whole vault
    out of the project, and `--force` would then overwrite files there.
    """
    try:
        path.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _require_exclusive_scaffold_support() -> None:
    required = (
        hasattr(os, "O_DIRECTORY"),
        hasattr(os, "O_NOFOLLOW"),
        _atomic_directory_publication_available(),
        os.open in os.supports_dir_fd,
        os.mkdir in os.supports_dir_fd,
        os.listdir in os.supports_fd,
    )
    if not all(required):
        raise ScaffoldError(
            ["this platform cannot scaffold a blueprint with the required path safety"]
        )


def _atomic_directory_publication_available() -> bool:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return False
    return hasattr(libc, "renameatx_np") or hasattr(libc, "renameat2")


def _open_directory_chain(path: Path) -> int:
    """Open an absolute directory path without following any component link."""

    _require_exclusive_scaffold_support()
    absolute = path.absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(absolute.anchor, flags)
        for part in absolute.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise ScaffoldError([f"cannot open blueprint directory safely: {path}"]) from None


def _scaffold_directory_checkpoint(
    _event: str,
    _parent_descriptor: int,
    _staging_name: str,
    _target_name: str,
) -> None:
    """Deterministic race boundary used by adversarial tests."""


def _rename_directory_noreplace(
    source_parent: int,
    source: str,
    target_parent: int,
    target: str,
) -> None:
    # Import at call time because project.create imports this module.
    from .project.create import _rename_noreplace

    _rename_noreplace(source_parent, source, target_parent, target)


def _stat_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _verify_directory_binding(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    identity: tuple[int, int],
    *,
    mode: int | None = None,
    require_empty: bool = False,
) -> None:
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError:
        raise _DirectoryPublicationError("changed") from None
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or _stat_identity(opened) != identity
        or _stat_identity(named) != identity
        or (mode is not None and stat.S_IMODE(opened.st_mode) != mode)
        or (mode is not None and stat.S_IMODE(named.st_mode) != mode)
    ):
        raise _DirectoryPublicationError("changed")
    if require_empty:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        scan_descriptor: int | None = None
        try:
            scan_descriptor = os.open(".", flags, dir_fd=descriptor)
            if os.listdir(scan_descriptor):
                raise _DirectoryPublicationError("changed")
        except _DirectoryPublicationError:
            raise
        except OSError:
            raise _DirectoryPublicationError("changed") from None
        finally:
            if scan_descriptor is not None:
                try:
                    os.close(scan_descriptor)
                except OSError:
                    pass


def _publish_new_directory(
    parent_descriptor: int,
    name: str,
    *,
    mode: int,
    rename_noreplace: Callable[[int, str, int, str], None],
    fsync_directory: Callable[[int], None],
    checkpoint: Callable[[str, int, str, str], None],
) -> tuple[int, tuple[int, int]]:
    """Bind a private staged directory, then publish it without replacement.

    Portable ``mkdirat`` does not return a descriptor or inode. The immediate
    no-follow stat below is therefore the first observable identity boundary;
    substitution before it cannot be distinguished portably. A short,
    target-independent 128-bit name minimizes that unavoidable interval and
    uses a fixed 37-byte staging budget independent of the public component's
    own filesystem name limit.
    """

    staging_name: str | None = None
    descriptor: int | None = None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        for _ in range(_DIRECTORY_STAGE_ATTEMPTS):
            candidate = f"{_DIRECTORY_STAGE_PREFIX}{secrets.token_hex(16)}"
            # The source and destination must never name the same directory
            # entry. The stage alphabet is ASCII, so casefold covers the
            # additional alias relevant to case-insensitive Darwin volumes.
            if candidate == name or candidate.casefold() == name.casefold():
                continue
            try:
                os.mkdir(candidate, mode=0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                continue
            except OSError:
                raise _DirectoryPublicationError("create", candidate) from None
            staging_name = candidate
            break
        else:
            raise _DirectoryPublicationError("create")

        try:
            created = os.stat(
                staging_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise _DirectoryPublicationError("changed", staging_name) from None
        if not stat.S_ISDIR(created.st_mode):
            raise _DirectoryPublicationError("changed", staging_name)
        created_identity = _stat_identity(created)
        checkpoint("identity-captured-before-bind", parent_descriptor, staging_name, name)
        try:
            descriptor = os.open(staging_name, flags, dir_fd=parent_descriptor)
        except OSError:
            raise _DirectoryPublicationError("changed", staging_name) from None
        try:
            _verify_directory_binding(
                parent_descriptor,
                staging_name,
                descriptor,
                created_identity,
                require_empty=True,
            )
            os.fchmod(descriptor, mode)
            _verify_directory_binding(
                parent_descriptor,
                staging_name,
                descriptor,
                created_identity,
                mode=mode,
                require_empty=True,
            )
        except _DirectoryPublicationError:
            raise
        except OSError:
            raise _DirectoryPublicationError("changed", staging_name) from None

        checkpoint("bound-before-publication", parent_descriptor, staging_name, name)
        try:
            rename_noreplace(parent_descriptor, staging_name, parent_descriptor, name)
        except FileExistsError:
            raise _DirectoryPublicationError("collision", staging_name) from None
        except Exception:
            raise _DirectoryPublicationError("publish", staging_name) from None
        try:
            _verify_directory_binding(
                parent_descriptor,
                name,
                descriptor,
                created_identity,
                mode=mode,
                require_empty=True,
            )
            fsync_directory(parent_descriptor)
        except _DirectoryPublicationError:
            raise
        except OSError:
            raise _DirectoryPublicationError("durability", staging_name) from None
        checkpoint("published-after-parent-fsync", parent_descriptor, staging_name, name)
        _verify_directory_binding(
            parent_descriptor,
            name,
            descriptor,
            created_identity,
            mode=mode,
            require_empty=True,
        )
        return descriptor, created_identity
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _open_or_create_directory(parent_descriptor: int, name: str) -> int:
    try:
        descriptor, _ = _publish_new_directory(
            parent_descriptor,
            name,
            mode=0o755,
            rename_noreplace=_rename_directory_noreplace,
            fsync_directory=os.fsync,
            checkpoint=_scaffold_directory_checkpoint,
        )
        return descriptor
    except _DirectoryPublicationError as error:
        stage_detail = (
            f"; staged name was {error.staging_name}"
            if error.staging_name is not None
            else ""
        )
        if error.reason == "collision":
            try:
                collided = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            except OSError:
                collided = None
            if collided is not None and stat.S_ISLNK(collided.st_mode):
                issue = f"cannot open blueprint directory safely: {name}{stage_detail}"
            else:
                issue = f"blueprint directory changed during creation: {name}{stage_detail}"
        elif error.reason == "durability":
            issue = f"cannot commit blueprint directory durably: {name}{stage_detail}"
        elif error.reason == "changed":
            issue = f"blueprint directory changed during creation: {name}{stage_detail}"
        else:
            issue = f"cannot create blueprint directory: {name}{stage_detail}"
        raise ScaffoldError([issue]) from None


def _verify_scaffold_directories(
    root_descriptor: int,
    bindings: dict[tuple[str, ...], _ScaffoldDirectoryBinding],
) -> None:
    for parts, binding in bindings.items():
        parent_descriptor = (
            root_descriptor
            if len(parts) == 1
            else bindings[parts[:-1]].descriptor
        )
        try:
            _verify_directory_binding(
                parent_descriptor,
                parts[-1],
                binding.descriptor,
                binding.identity,
                mode=binding.mode,
            )
        except _DirectoryPublicationError:
            raise ScaffoldError(
                [f"blueprint directory changed during scaffold: {'/'.join(parts)}"]
            ) from None


def _exclusive_write_at(
    parent_descriptor: int,
    name: str,
    content: bytes,
    *,
    mode: int,
) -> _ScaffoldFileBinding:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(name, flags, mode, dir_fd=parent_descriptor)
    except FileExistsError:
        raise ScaffoldError([f"blueprint destination already exists: {name}"]) from None
    except OSError:
        raise ScaffoldError([f"cannot create blueprint file safely: {name}"]) from None
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(os.dup(descriptor), "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        identity = _stat_identity(opened)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or identity != _stat_identity(named)
            or opened.st_size != len(content)
            or named.st_size != len(content)
            or stat.S_IMODE(opened.st_mode) != mode
            or stat.S_IMODE(named.st_mode) != mode
        ):
            raise OSError("created blueprint file changed")
        return _ScaffoldFileBinding(
            descriptor,
            identity,
            len(content),
            hashlib.sha256(content).hexdigest(),
            mode,
        )
    except OSError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise ScaffoldError([f"cannot write blueprint file safely: {name}"]) from None


def _verify_scaffold_file(
    parent_descriptor: int,
    name: str,
    binding: _ScaffoldFileBinding,
) -> None:
    try:
        before = os.fstat(binding.descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or _stat_identity(before) != binding.identity
            or _stat_identity(named) != binding.identity
            or before.st_size != binding.size
            or named.st_size != binding.size
            or stat.S_IMODE(before.st_mode) != binding.mode
            or stat.S_IMODE(named.st_mode) != binding.mode
        ):
            raise OSError("blueprint file binding changed")
        digest = hashlib.sha256()
        offset = 0
        while offset < binding.size:
            chunk = os.pread(
                binding.descriptor,
                min(1024 * 1024, binding.size - offset),
                offset,
            )
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
        after = os.fstat(binding.descriptor)
        named_after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            offset != binding.size
            or digest.hexdigest() != binding.sha256
            or _stat_identity(after) != binding.identity
            or _stat_identity(named_after) != binding.identity
            or after.st_size != binding.size
            or named_after.st_size != binding.size
            or stat.S_IMODE(after.st_mode) != binding.mode
            or stat.S_IMODE(named_after.st_mode) != binding.mode
        ):
            raise OSError("blueprint file content changed")
    except OSError:
        raise ScaffoldError([f"blueprint file changed during scaffold: {name}"]) from None


def _bind_existing_scaffold_file(
    parent_descriptor: int,
    name: str,
    content: bytes,
    *,
    mode: int,
) -> _ScaffoldFileBinding:
    """Compatibility path for wrappers around the private write seam."""

    descriptor: int | None = None
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        expected = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        binding = _ScaffoldFileBinding(
            descriptor,
            _stat_identity(opened),
            len(content),
            hashlib.sha256(content).hexdigest(),
            mode,
        )
        if _stat_identity(expected) != binding.identity:
            raise OSError("blueprint file changed while binding")
        _verify_scaffold_file(parent_descriptor, name, binding)
        return binding
    except (OSError, ScaffoldError):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise ScaffoldError([f"cannot retain blueprint file safely: {name}"]) from None


def _scaffold_parent_descriptor(
    binding: _BlueprintScaffoldBinding,
    parts: tuple[str, ...],
) -> int:
    return (
        binding.root_descriptor
        if not parts
        else binding.directories[parts].descriptor
    )


def _verify_blueprint_scaffold_binding(
    binding: _BlueprintScaffoldBinding,
    *,
    exact: bool,
) -> None:
    """Verify descriptor identities, contents, modes, and the generated tree."""

    if binding._closed:
        raise ScaffoldError(["blueprint scaffold binding is closed"])
    try:
        opened_root = os.fstat(binding.root_descriptor)
        named_root = (
            binding.root.stat(follow_symlinks=False)
            if binding.root_parent_descriptor is None or binding.root_name is None
            else os.stat(
                binding.root_name,
                dir_fd=binding.root_parent_descriptor,
                follow_symlinks=False,
            )
        )
    except OSError:
        raise ScaffoldError([binding.root_issue]) from None
    if (
        not stat.S_ISDIR(opened_root.st_mode)
        or not stat.S_ISDIR(named_root.st_mode)
        or _stat_identity(opened_root) != binding.root_identity
        or _stat_identity(named_root) != binding.root_identity
        or stat.S_IMODE(opened_root.st_mode) != binding.root_mode
        or stat.S_IMODE(named_root.st_mode) != binding.root_mode
    ):
        raise ScaffoldError([binding.root_issue])
    _verify_scaffold_directories(binding.root_descriptor, binding.directories)
    for parts, file_binding in binding.files.items():
        parent = _scaffold_parent_descriptor(binding, parts[:-1])
        _verify_scaffold_file(parent, parts[-1], file_binding)
    for parts, expected in binding.expected_entries.items():
        if parts and parts not in binding.directories:
            if exact:
                raise ScaffoldError(
                    [f"generated blueprint directory is missing: {'/'.join(parts)}"]
                )
            continue
        descriptor = _scaffold_parent_descriptor(binding, parts)
        try:
            actual = frozenset(os.listdir(descriptor))
        except OSError:
            raise ScaffoldError(
                [f"cannot inspect generated blueprint entries: {'/'.join(parts) or '.'}"]
            ) from None
        if actual - expected or (exact and actual != expected):
            raise ScaffoldError(
                [f"generated blueprint entries changed: {'/'.join(parts) or '.'}"]
            )
    _verify_scaffold_directories(binding.root_descriptor, binding.directories)
    try:
        opened_root = os.fstat(binding.root_descriptor)
        named_root = (
            binding.root.stat(follow_symlinks=False)
            if binding.root_parent_descriptor is None or binding.root_name is None
            else os.stat(
                binding.root_name,
                dir_fd=binding.root_parent_descriptor,
                follow_symlinks=False,
            )
        )
    except OSError:
        raise ScaffoldError([binding.root_issue]) from None
    if (
        _stat_identity(opened_root) != binding.root_identity
        or _stat_identity(named_root) != binding.root_identity
        or stat.S_IMODE(opened_root.st_mode) != binding.root_mode
        or stat.S_IMODE(named_root.st_mode) != binding.root_mode
    ):
        raise ScaffoldError([binding.root_issue])


def _scaffold_binding_checkpoint(
    _event: str,
    _relative: str,
    _binding: _BlueprintScaffoldBinding,
) -> None:
    """Deterministic file and durability race boundary used by tests."""


def scaffold_project(
    target: str | Path,
    *,
    title: str,
    repository_url: str = "",
    autoform_source: str = "",
    autoform_ref: str = "",
    force: bool = False,
    discover_plugin_pin: bool = True,
) -> ScaffoldResult:
    """Write the blueprint vault, site config, and CI into *target*.

    Existing files are never overwritten unless *force* is set; they come back
    in ``skipped`` so a repair run reports exactly what it left in place.
    """

    requested = Path(target).expanduser()
    issues: list[str] = []
    if not title.strip():
        issues.append("project title must not be empty")
    # Checked before resolve(), which would collapse the link and hide it.
    if requested.is_symlink():
        issues.append(f"refusing to scaffold into a symlink: {requested}")
    root = requested.resolve()
    if root.exists() and not root.is_dir():
        issues.append(f"target exists and is not a directory: {root}")
    if (root / _WORKSPACE_MANIFEST).exists() or (root / _WORKSPACE_MANIFEST).is_symlink():
        issues.append(
            "refusing the legacy single-vault scaffold in a manifest-managed workspace; "
            "use 'autoform blueprint new'"
        )
    # A branch name or an abbreviated sha is the same silent failure this
    # gate exists to prevent, just supplied by hand: CI would reinstall a
    # different Autoform later and break a project that was passing.
    # Git treats a sha case-insensitively and always prints lowercase, so an
    # uppercase one pasted from a web UI is valid input, not a mistake.
    given_ref = autoform_ref.strip().lower()
    if autoform_ref and not _FULL_SHA.fullmatch(given_ref):
        issues.append(
            f"--autoform-ref must be a full 40-character commit sha, not {given_ref!r}; "
            "branches and abbreviated shas do not stay put"
        )
    given_source = ""
    if autoform_source:
        normalized = _normalize_autoform_source(autoform_source)
        if normalized is None:
            issues.append(
                "--autoform-source must be a safe credential-free HTTPS Git URL ending in .git"
            )
        else:
            given_source = normalized
    if issues:
        raise ScaffoldError(issues)

    if bool(autoform_source) != bool(autoform_ref):
        issues.append("--autoform-source and --autoform-ref must be provided together")
    if issues:
        raise ScaffoldError(issues)

    # Explicit provenance is already a complete caller choice. Discovery is a
    # network verification step and must not run merely to be discarded.
    pinned_source, pinned_ref = (
        plugin_pin()
        if discover_plugin_pin and not (given_source and given_ref)
        else ("", "")
    )
    safe_pinned_source = _normalize_autoform_source(pinned_source, allow_github_scp=True)
    if safe_pinned_source is None or not _FULL_SHA.fullmatch(pinned_ref.lower()):
        pinned_source, pinned_ref = "", ""
    else:
        pinned_source, pinned_ref = safe_pinned_source, pinned_ref.lower()
    source = given_source or pinned_source
    ref = given_ref or pinned_ref
    unpinned = not source or not ref
    substitutions = {
        "PROJECT_TITLE_YAML": _yaml_scalar(title.strip()),
        "REPO_URL_YAML": _yaml_scalar(repository_url.strip()),
        "PROJECT_TITLE": title.strip(),
        "REPO_URL": repository_url.strip(),
        "AUTOFORM_SOURCE": source,
        "AUTOFORM_REF": ref,
        "AUTOFORM_SOURCE_YAML": _yaml_scalar(source),
        "AUTOFORM_REF_YAML": _yaml_scalar(ref),
    }

    written: list[str] = []
    skipped: list[str] = []
    for template in sorted(_TEMPLATES.rglob("*")):
        relative_path = template.relative_to(_TEMPLATES)
        if (
            not template.is_file()
            or "__pycache__" in relative_path.parts
            or template.suffix == ".pyc"
        ):
            continue
        relative = relative_path.as_posix()
        if unpinned and relative.startswith("github/"):
            skipped.append(_destination(relative))
            continue
        destination = root / _destination(relative)
        # Confine every write, not just the root. Reject links outright before
        # checking whether the destination should be skipped: `exists()` is
        # false for a dangling symlink, but opening that path still follows the
        # link and can create a file outside the project.
        probe = root
        for part in Path(_destination(relative)).parts:
            probe = probe / part
            if probe.is_symlink() or (probe.exists() and not _within(probe, root)):
                raise ScaffoldError(
                    [f"refusing to write outside the project through a link: {probe}"]
                )
        if destination.exists() and not force:
            skipped.append(_destination(relative))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if template.suffix in {".js", ".html"} or relative.endswith("gitignore"):
            content = template.read_bytes()
        else:
            rendered = _render(template.read_text(encoding="utf-8"), substitutions)
            content = rendered.encode("utf-8")
        _atomic_write(destination, content, mode=stat.S_IMODE(template.stat().st_mode))
        written.append(_destination(relative))

    return ScaffoldResult(title.strip(), tuple(written), tuple(skipped), unpinned)


def scaffold_blueprint(
    target: str | Path,
    *,
    title: str,
    _directory_descriptor: int | None = None,
    _directory_identity: tuple[int, int] | None = None,
    _directory_parent_descriptor: int | None = None,
    _directory_name: str | None = None,
    _retained_bindings: list[_BlueprintScaffoldBinding] | None = None,
) -> tuple[str, ...]:
    """Write only the vault files beneath ``templates/blueprint`` into *target*.

    This is the workspace counterpart to :func:`scaffold_project`: shared
    repository files stay at the workspace root, while each registered project
    receives an independent roadmap, coverage contract, and source notes.
    """

    _require_exclusive_scaffold_support()
    requested = Path(target).expanduser().absolute()
    issues: list[str] = []
    if not title.strip():
        issues.append("project title must not be empty")
    if _directory_descriptor is None:
        if requested.is_symlink():
            issues.append(f"refusing to scaffold into a symlink: {requested}")
        if not requested.exists():
            issues.append(f"target directory does not exist: {requested}")
        elif not requested.is_dir():
            issues.append(f"target exists and is not a directory: {requested}")
    elif (_directory_parent_descriptor is None) != (_directory_name is None):
        issues.append("blueprint directory parent binding is incomplete")
    if issues:
        raise ScaffoldError(issues)

    root_descriptor = (
        _open_directory_chain(requested)
        if _directory_descriptor is None
        else os.dup(_directory_descriptor)
    )
    retained = False
    scaffold_binding: _BlueprintScaffoldBinding | None = None
    try:
        opened = os.fstat(root_descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise ScaffoldError([f"blueprint directory binding is no longer a directory: {requested}"])
        if _directory_identity is not None and (opened.st_dev, opened.st_ino) != _directory_identity:
            raise ScaffoldError([f"blueprint directory binding changed: {requested}"])
        try:
            if os.listdir(root_descriptor):
                raise ScaffoldError([f"target directory is not empty: {requested}"])
        except OSError:
            raise ScaffoldError([f"cannot inspect blueprint directory: {requested}"]) from None

        substitutions = {"PROJECT_TITLE": title.strip()}
        source_root = _TEMPLATES / "blueprint"
        rendered: list[tuple[str, bytes, int]] = []
        expected_entries: dict[tuple[str, ...], set[str]] = {(): set()}
        for template in sorted(source_root.rglob("*")):
            relative_path = template.relative_to(source_root)
            if (
                not template.is_file()
                or "__pycache__" in relative_path.parts
                or template.suffix == ".pyc"
            ):
                continue
            relative = relative_path.as_posix()
            destination_relative = ".gitignore" if relative == "gitignore" else relative
            destination_parts = Path(destination_relative).parts
            for index, part in enumerate(destination_parts):
                parent_parts = tuple(destination_parts[:index])
                expected_entries.setdefault(parent_parts, set()).add(part)
                if index < len(destination_parts) - 1:
                    expected_entries.setdefault(tuple(destination_parts[: index + 1]), set())
            if template.suffix in {".js", ".html"} or relative.endswith("gitignore"):
                content = template.read_bytes()
            else:
                content = _render(template.read_text(encoding="utf-8"), substitutions).encode(
                    "utf-8"
                )
            rendered.append(
                (destination_relative, content, stat.S_IMODE(template.stat().st_mode))
            )

        scaffold_binding = _BlueprintScaffoldBinding(
            requested,
            root_descriptor,
            _stat_identity(opened),
            stat.S_IMODE(opened.st_mode),
            (
                "blueprint destination changed during scaffold"
                if _directory_identity is not None
                else "blueprint root changed during scaffold"
            ),
            _directory_parent_descriptor,
            _directory_name,
            {},
            {},
            {parts: frozenset(names) for parts, names in expected_entries.items()},
        )
        written: list[str] = []
        for destination_relative, content, mode in rendered:
            destination_parts = Path(destination_relative).parts
            parent_descriptor = root_descriptor
            parts: tuple[str, ...] = ()
            for part in destination_parts[:-1]:
                parts = (*parts, part)
                directory_binding = scaffold_binding.directories.get(parts)
                if directory_binding is None:
                    descriptor = _open_or_create_directory(parent_descriptor, part)
                    try:
                        opened_directory = os.fstat(descriptor)
                    except OSError:
                        os.close(descriptor)
                        raise ScaffoldError(
                            [f"cannot retain blueprint directory safely: {'/'.join(parts)}"]
                        ) from None
                    directory_binding = _ScaffoldDirectoryBinding(
                        descriptor,
                        _stat_identity(opened_directory),
                        0o755,
                    )
                    scaffold_binding.directories[parts] = directory_binding
                parent_descriptor = directory_binding.descriptor
            _verify_blueprint_scaffold_binding(scaffold_binding, exact=False)
            name = destination_parts[-1]
            file_binding = _exclusive_write_at(
                parent_descriptor,
                name,
                content,
                mode=mode,
            )
            _verify_scaffold_directories(
                scaffold_binding.root_descriptor,
                scaffold_binding.directories,
            )
            if file_binding is None:
                file_binding = _bind_existing_scaffold_file(
                    parent_descriptor,
                    name,
                    content,
                    mode=mode,
                )
            scaffold_binding.files[tuple(destination_parts)] = file_binding
            _verify_blueprint_scaffold_binding(scaffold_binding, exact=False)
            written.append(destination_relative)

        _verify_blueprint_scaffold_binding(scaffold_binding, exact=True)
        for parts in sorted(
            scaffold_binding.expected_entries,
            key=lambda item: (-len(item), item),
        ):
            relative = "/".join(parts) or "."
            _verify_blueprint_scaffold_binding(scaffold_binding, exact=True)
            try:
                _scaffold_binding_checkpoint("before-parent-fsync", relative, scaffold_binding)
                os.fsync(_scaffold_parent_descriptor(scaffold_binding, parts))
                _scaffold_binding_checkpoint("after-parent-fsync", relative, scaffold_binding)
            except OSError:
                raise ScaffoldError(
                    [f"cannot commit generated blueprint entries durably: {relative}"]
                ) from None
            _verify_blueprint_scaffold_binding(scaffold_binding, exact=True)
        if _retained_bindings is not None:
            _retained_bindings.append(scaffold_binding)
            retained = True
        return tuple(written)
    finally:
        if scaffold_binding is None:
            os.close(root_descriptor)
        elif not retained:
            scaffold_binding.close()


__all__ = [
    "ScaffoldError",
    "ScaffoldResult",
    "plugin_pin",
    "scaffold_blueprint",
    "scaffold_project",
]
