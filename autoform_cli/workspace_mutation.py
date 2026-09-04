"""Safe filesystem mutations for manifest-managed Autoform workspaces."""

from __future__ import annotations

import ctypes
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomlkit
from tomlkit.items import InlineTable, KeyType, SingleKey, Table

from .scaffold import (
    _BlueprintScaffoldBinding,
    ScaffoldError,
    _DirectoryPublicationError,
    _publish_new_directory,
    _verify_blueprint_scaffold_binding,
    scaffold_blueprint,
)
from .project.create import ProjectCreateError, _rename_noreplace
from .workspace import (
    Workspace,
    _WorkspaceRootBinding,
    _open_workspace_root,
    _reject_case_collisions,
    _reject_existing_symlink_chain,
    discover_workspace,
)
from .workspace_manifest import (
    BLUEPRINT_CHANGE_SCHEMA,
    MAX_MANIFEST_BYTES,
    WORKSPACE_FILE,
    WORKSPACE_INIT_SCHEMA,
    WORKSPACE_SCHEMA,
    WorkspaceError,
    WorkspaceLocation,
    WorkspaceManifest,
    parse_workspace,
    path_keys_overlap,
    portable_name_key,
    portable_path_key,
    uses_reserved_repository_root,
    valid_blueprint_member,
    valid_identifier,
    valid_relative_path,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - unsupported mutation platform
    fcntl = None  # type: ignore[assignment]

_ORIGINAL_SCAFFOLD_BLUEPRINT = scaffold_blueprint
_DIRECTORY_RENAME_NOREPLACE = _rename_noreplace


@dataclass(slots=True)
class _BlueprintBinding:
    workspace: Workspace
    location: WorkspaceLocation
    combined: str
    destination: Path
    root_descriptor: int
    root_identity: tuple[int, int]
    location_descriptors: tuple[int, ...]
    location_identities: tuple[tuple[int, int], ...]
    destination_descriptor: int | None = None
    destination_identity: tuple[int, int] | None = None
    roadmap_descriptor: int | None = None
    roadmap_identity: tuple[int, int] | None = None
    scaffold_binding: _BlueprintScaffoldBinding | None = None

    def close(self) -> None:
        if self.scaffold_binding is not None:
            self.scaffold_binding.close()
        descriptors = (*self.location_descriptors, self.root_descriptor)
        if self.destination_descriptor is not None:
            descriptors = (self.destination_descriptor, *descriptors)
        if self.roadmap_descriptor is not None:
            descriptors = (self.roadmap_descriptor, *descriptors)
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.workspace.close()


@dataclass(frozen=True, slots=True)
class _StagedFile:
    path: Path
    descriptor: int
    identity: tuple[int, int]
    size: int
    sha256: str
    mode: int


@dataclass(frozen=True, slots=True)
class _DirectoryChain:
    root: Path
    root_binding: _WorkspaceRootBinding
    root_descriptor: int
    root_identity: tuple[int, int]
    relative: PurePosixPath
    descriptors: tuple[int, ...]
    identities: tuple[tuple[int, int], ...]
    created: tuple[Path, ...]

    def close(self) -> None:
        for descriptor in reversed(self.descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class WorkspaceInitResult:
    root: Path
    manifest_path: str
    location_id: str
    blueprint_root: str

    def as_dict(self) -> dict[str, object]:
        return {
            "blueprint_root": self.blueprint_root,
            "location_id": self.location_id,
            "manifest": self.manifest_path,
            "ok": True,
            "root": ".",
            "schema": WORKSPACE_INIT_SCHEMA,
        }


@dataclass(frozen=True, slots=True)
class BlueprintCreateResult:
    project_id: str
    blueprint_path: str
    manifest_backup_path: str
    written: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "blueprint_path": self.blueprint_path,
            "manifest_backup_path": self.manifest_backup_path,
            "ok": True,
            "project": self.project_id,
            "schema": BLUEPRINT_CHANGE_SCHEMA,
            "workspace_root": ".",
            "written": list(self.written),
        }


def initialize_workspace(
    target: str | Path,
    *,
    blueprint_root: str,
    location_id: str = "blueprints",
) -> WorkspaceInitResult:
    """Create a root manifest and its blueprint collection without creating a vault."""

    _require_workspace_mutation_support()
    if not valid_identifier(location_id):
        raise WorkspaceError(["location id is not portable"])
    if not valid_relative_path(blueprint_root, allow_dot=False):
        raise WorkspaceError(["blueprint root must be a portable repository-relative path"])
    first_component = PurePosixPath(blueprint_root).parts[0]
    if uses_reserved_repository_root(blueprint_root):
        raise WorkspaceError(
            [f"blueprint root uses reserved repository path: {first_component}"]
        )
    try:
        root = Path(os.path.abspath(Path(target).expanduser()))
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceError(["workspace target cannot be resolved"]) from None
    root_binding = _open_workspace_root(root)
    root_descriptor = root_binding.descriptor
    root_identity = root_binding.identity
    manifest_path = root / WORKSPACE_FILE
    text = _initial_manifest(location_id=location_id, blueprint_root=blueprint_root)
    staged: _StagedFile | None = None
    chain: _DirectoryChain | None = None
    locked = False
    try:
        assert fcntl is not None
        try:
            fcntl.flock(root_descriptor, fcntl.LOCK_EX)
            locked = True
        except OSError:
            raise WorkspaceError(["workspace root could not be locked safely"]) from None
        _workspace_mutation_checkpoint("workspace-init-root-bound")
        _verify_root_binding(
            root,
            root_descriptor,
            root_identity,
            root_binding=root_binding,
        )
        _reject_case_collisions(root, PurePosixPath(WORKSPACE_FILE))
        _reject_case_collisions(root, PurePosixPath(blueprint_root))
        collection = root / PurePosixPath(blueprint_root)
        _reject_existing_symlink_chain(collection, root)
        _preflight_directory_chain(root, PurePosixPath(blueprint_root))
        _verify_root_binding(
            root,
            root_descriptor,
            root_identity,
            root_binding=root_binding,
        )
        try:
            os.stat(WORKSPACE_FILE, dir_fd=root_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError:
            raise WorkspaceError([f"cannot inspect {WORKSPACE_FILE} safely"]) from None
        else:
            raise WorkspaceError([f"{WORKSPACE_FILE} already exists"])
        staged = _stage_new_file(
            manifest_path,
            text.encode("utf-8"),
            parent_descriptor=root_descriptor,
        )
        _verify_root_binding(
            root,
            root_descriptor,
            root_identity,
            root_binding=root_binding,
        )
        try:
            chain = _create_directory_chain(
                root,
                PurePosixPath(blueprint_root),
                root_descriptor=root_descriptor,
                root_identity=root_identity,
                root_binding=root_binding,
            )
        except WorkspaceError as error:
            detail = "; ".join(error.issues)
            raise WorkspaceError(
                [f"{detail}; retained complete staged manifest at {staged.path.name}"]
            ) from None
        try:
            _publish_staged_file(
                manifest_path,
                staged,
                mode=0o644,
                parent_descriptor=root_descriptor,
                final_validator=lambda: _verify_directory_chain(chain),
            )
        except WorkspaceError as error:
            retained = ", ".join(
                path.relative_to(root).as_posix() for path in chain.created
            )
            detail = "; ".join(error.issues)
            if retained:
                detail += f"; retained unregistered directories: {retained}"
            raise WorkspaceError([detail]) from None
    finally:
        if locked:
            try:
                assert fcntl is not None
                fcntl.flock(root_descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        if chain is not None:
            chain.close()
        if staged is not None:
            try:
                os.close(staged.descriptor)
            except OSError:
                pass
        root_binding.close()
    return WorkspaceInitResult(root, WORKSPACE_FILE, location_id, blueprint_root)


def create_blueprint_project(
    start: str | Path,
    *,
    project_id: str,
    title: str,
    path: str | None = None,
    location_id: str | None = None,
) -> BlueprintCreateResult:
    """Create one vault and register it in the root manifest."""

    if not title.strip():
        raise WorkspaceError(["project title must not be empty"])
    member = project_id if path is None else path
    binding = _prepare_blueprint_binding(
        start,
        project_id=project_id,
        member=member,
        location_id=location_id,
    )
    locked = False
    try:
        assert fcntl is not None
        try:
            fcntl.flock(binding.root_descriptor, fcntl.LOCK_EX)
            locked = True
        except OSError:
            raise WorkspaceError(["workspace root could not be locked safely"]) from None
        _verify_blueprint_parent_binding(binding, verify_manifest=False)
        _reject_case_collisions(
            binding.workspace.root,
            PurePosixPath(binding.combined),
        )
        location_descriptor = binding.location_descriptors[-1]
        try:
            destination_descriptor, destination_identity = _publish_new_directory(
                location_descriptor,
                member,
                mode=0o755,
                rename_noreplace=_DIRECTORY_RENAME_NOREPLACE,
                fsync_directory=_fsync_directory_descriptor,
                checkpoint=_workspace_directory_checkpoint,
            )
        except _DirectoryPublicationError as error:
            stage_detail = (
                f"; staged name was {error.staging_name}"
                if error.staging_name is not None
                else ""
            )
            if error.reason == "collision":
                issue = f"blueprint destination already exists: {binding.combined}{stage_detail}"
            elif error.reason == "durability":
                issue = (
                    "blueprint destination could not be committed durably: "
                    f"{binding.combined}{stage_detail}"
                )
            elif error.reason == "changed":
                issue = (
                    f"blueprint destination changed during creation: "
                    f"{binding.combined}{stage_detail}"
                )
            else:
                issue = (
                    f"blueprint destination could not be created: "
                    f"{binding.combined}{stage_detail}"
                )
            raise WorkspaceError([issue]) from None
        binding.destination_descriptor = destination_descriptor
        binding.destination_identity = destination_identity
        _verify_blueprint_binding(
            binding,
            require_roadmap=False,
            verify_manifest=False,
        )
        if scaffold_blueprint is _ORIGINAL_SCAFFOLD_BLUEPRINT:
            retained_bindings: list[_BlueprintScaffoldBinding] = []
            written = scaffold_blueprint(
                binding.destination,
                title=title,
                _directory_descriptor=destination_descriptor,
                _directory_identity=destination_identity,
                _directory_parent_descriptor=location_descriptor,
                _directory_name=member,
                _retained_bindings=retained_bindings,
            )
            if len(retained_bindings) != 1:
                raise WorkspaceError(["blueprint scaffold binding is incomplete"])
            binding.scaffold_binding = retained_bindings[0]
        else:  # Preserve the small monkeypatch seam used by callers and tests.
            written = scaffold_blueprint(binding.destination, title=title)
        _verify_blueprint_binding(
            binding,
            require_roadmap=True,
            verify_manifest=False,
        )
        manifest_backup_path = _append_project(
            binding.workspace.path,
            project_id=project_id,
            title=title.strip(),
            location_id=binding.location.id,
            path=member,
            expected_blueprint_path=binding.combined,
            binding=binding,
        )
    except (OSError, ScaffoldError, WorkspaceError) as error:
        detail = (
            "; ".join(error.issues)
            if isinstance(error, (ScaffoldError, WorkspaceError))
            else "filesystem operation failed"
        )
        raise WorkspaceError(
            [
                f"blueprint creation stopped after reserving {binding.combined!r}: {detail}; "
                "inspect the unregistered directory before retrying"
            ]
        ) from None
    finally:
        if locked:
            try:
                assert fcntl is not None
                fcntl.flock(binding.root_descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        binding.close()

    relative_written = tuple(
        sorted(
            str(PurePosixPath(binding.combined) / PurePosixPath(item))
            for item in written
        )
    )
    return BlueprintCreateResult(
        project_id,
        binding.combined,
        manifest_backup_path,
        relative_written,
    )


def register_blueprint_project(
    start: str | Path,
    *,
    project_id: str,
    title: str | None,
    path: str,
    location_id: str | None = None,
) -> BlueprintCreateResult:
    """Register an existing vault without changing any file inside it."""

    if title is not None and not title.strip():
        raise WorkspaceError(["project title must not be empty"])
    binding = _prepare_blueprint_binding(
        start,
        project_id=project_id,
        member=path,
        location_id=location_id,
    )
    try:
        _reject_case_collisions(
            binding.workspace.root,
            PurePosixPath(binding.combined),
        )
        _reject_existing_symlink_chain(binding.destination, binding.workspace.root)
        try:
            destination_descriptor, destination_identity = _open_child_directory(
                binding.location_descriptors[-1],
                path,
                "blueprint destination",
            )
        except WorkspaceError:
            raise WorkspaceError(
                [f"blueprint directory does not exist: {binding.combined}"]
            ) from None
        binding.destination_descriptor = destination_descriptor
        binding.destination_identity = destination_identity
        _verify_blueprint_binding(
            binding,
            require_roadmap=True,
            verify_manifest=False,
        )
        manifest_backup_path = _append_project(
            binding.workspace.path,
            project_id=project_id,
            title=title.strip() if title is not None else project_id,
            location_id=binding.location.id,
            path=path,
            expected_blueprint_path=binding.combined,
            binding=binding,
        )
    finally:
        binding.close()
    return BlueprintCreateResult(project_id, binding.combined, manifest_backup_path, ())


def _prepare_blueprint_binding(
    start: str | Path,
    *,
    project_id: str,
    member: str,
    location_id: str | None,
) -> _BlueprintBinding:
    _require_workspace_mutation_support()
    if not valid_identifier(project_id):
        raise WorkspaceError(["project id is not portable"])
    if not valid_blueprint_member(member):
        raise WorkspaceError(["blueprint path must name one portable immediate child directory"])

    workspace = discover_workspace(start)
    root_descriptor: int | None = None
    try:
        root = workspace.root
        root_identity = workspace.root_identity
        try:
            root_descriptor = os.dup(workspace.root_descriptor)
            opened_root = os.fstat(root_descriptor)
            if (opened_root.st_dev, opened_root.st_ino) != root_identity:
                raise OSError("workspace root changed")
            _workspace_mutation_checkpoint("project-before-root-lock")
            assert fcntl is not None
            fcntl.flock(root_descriptor, fcntl.LOCK_EX)
        except OSError:
            raise WorkspaceError(["workspace root could not be locked safely"]) from None

        # Discovery precedes the lock only to locate the workspace root. Reload
        # the manifest while holding the root lock so concurrent registrations
        # are composed rather than rejected as changes to a stale generation.
        workspace.close()
        workspace = discover_workspace(root)
        if workspace.root_identity != root_identity:
            raise WorkspaceError(["workspace root changed during use"])

        candidates = tuple(
            location
            for location in workspace.manifest.locations
            if "blueprints" in location.provides
        )
        if location_id is None:
            if len(candidates) != 1:
                choices = ", ".join(item.id for item in candidates) or "none"
                raise WorkspaceError(
                    [f"choose a blueprint location with --location from: {choices}"]
                )
            selected_location_id = candidates[0].id
        else:
            selected_location_id = location_id
        location, combined = _validate_project_registration(
            workspace.manifest,
            project_id=project_id,
            location_id=selected_location_id,
            path=member,
        )

        location_relative = PurePosixPath(location.path)
        workspace.bind_managed_directory(location_relative)
        collection = workspace.root / location_relative
        _reject_existing_symlink_chain(collection, workspace.root)
        if not collection.is_dir():
            raise WorkspaceError([f"blueprint location does not exist: {location.path}"])
        workspace.verify_root_binding()
        location_descriptors, location_identities = _open_relative_directories(
            root_descriptor,
            location_relative,
            "blueprint location",
        )
    except BaseException:
        if root_descriptor is not None:
            os.close(root_descriptor)
        workspace.close()
        raise
    destination = collection / member
    return _BlueprintBinding(
        workspace,
        location,
        combined,
        destination,
        root_descriptor,
        root_identity,
        location_descriptors,
        location_identities,
    )


def _validate_project_registration(
    manifest: WorkspaceManifest,
    *,
    project_id: str,
    location_id: str,
    path: str,
    expected_blueprint_path: str | None = None,
) -> tuple[WorkspaceLocation, str]:
    """Validate one new registry entry against the current manifest."""

    if any(
        portable_name_key(item.id) == portable_name_key(project_id)
        for item in manifest.projects
    ):
        raise WorkspaceError([f"Autoform project {project_id!r} is already registered"])
    location = manifest.location(location_id)
    if "blueprints" not in location.provides:
        raise WorkspaceError([f"workspace location {location_id!r} does not provide blueprints"])

    combined = PurePosixPath(location.path, path).as_posix()
    if uses_reserved_repository_root(combined):
        raise WorkspaceError([f"blueprint path uses reserved repository path: {combined}"])
    if expected_blueprint_path is not None and combined != expected_blueprint_path:
        raise WorkspaceError(["blueprint location changed during registration"])
    combined_key = portable_path_key(combined)
    for project in manifest.projects:
        existing = manifest.blueprint_relative(project).as_posix()
        existing_key = portable_path_key(existing)
        if not path_keys_overlap(combined_key, existing_key):
            continue
        if combined_key == existing_key:
            raise WorkspaceError([f"blueprint path {combined!r} is already registered"])
        raise WorkspaceError(
            [f"blueprint path {combined!r} overlaps registered project {project.id!r}"]
        )
    return location, combined


def _initial_manifest(*, location_id: str, blueprint_root: str) -> str:
    document = tomlkit.document()
    document.add("schema", WORKSPACE_SCHEMA)
    locations = tomlkit.table()
    location = tomlkit.table()
    location.add("path", PurePosixPath(blueprint_root).as_posix())
    location.add("provides", ["blueprints"])
    locations.add(SingleKey(location_id, KeyType.Basic), location)
    document.add("locations", locations)
    document.add("projects", tomlkit.table())
    return tomlkit.dumps(document)


def _preflight_directory_chain(root: Path, relative: PurePosixPath) -> None:
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise WorkspaceError(["blueprint root could not be inspected safely"]) from None
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceError(["blueprint root exists and is not a directory"])


def _manifest_with_project(
    text: str,
    *,
    project_id: str,
    title: str,
    location_id: str,
    path: str,
) -> str:
    """Add a project through a comment-preserving TOML syntax tree."""

    try:
        document = tomlkit.parse(text)
    except (ValueError, RecursionError, MemoryError):
        raise WorkspaceError([f"{WORKSPACE_FILE} is not valid TOML"]) from None
    projects = document.get("projects")
    if projects is None:
        projects = tomlkit.table()
        document.add("projects", projects)
    if not isinstance(projects, (Table, InlineTable)):
        raise WorkspaceError(["projects must be a TOML table"])

    project = tomlkit.inline_table() if isinstance(projects, InlineTable) else tomlkit.table()
    project.add("title", title)
    blueprint = tomlkit.inline_table()
    blueprint.add("location", location_id)
    blueprint.add("path", path)
    project.add("blueprint", blueprint)
    try:
        projects.add(SingleKey(project_id, KeyType.Basic), project)
        return tomlkit.dumps(document)
    except (KeyError, TypeError, ValueError):
        raise WorkspaceError([f"cannot add projects.{project_id} to {WORKSPACE_FILE}"]) from None


def _append_project(
    manifest_path: Path,
    *,
    project_id: str,
    title: str,
    location_id: str,
    path: str,
    expected_blueprint_path: str,
    binding: _BlueprintBinding | None = None,
) -> str:
    own_root_binding: _WorkspaceRootBinding | None = None
    if binding is None:
        own_root_binding = _open_workspace_root(manifest_path.parent.absolute())
        parent_descriptor = own_root_binding.descriptor
    else:
        parent_descriptor = binding.root_descriptor
    descriptor: int | None = None
    staged: _StagedFile | None = None
    exchanged = False
    committed = False
    backup_name: str | None = None
    try:
        if binding is not None:
            _verify_blueprint_binding(
                binding,
                require_roadmap=True,
                verify_manifest=False,
            )
        else:
            assert own_root_binding is not None
            own_root_binding.verify()
        _workspace_mutation_checkpoint("registry-before-read")
        if binding is not None:
            _verify_blueprint_binding(
                binding,
                require_roadmap=True,
                verify_manifest=False,
            )
        else:
            assert own_root_binding is not None
            own_root_binding.verify()
        assert fcntl is not None
        fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
        descriptor = _open_locked_manifest(manifest_path, parent_descriptor=parent_descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceError([f"{WORKSPACE_FILE} is not a regular file"])
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            original = stream.read(MAX_MANIFEST_BYTES + 1)
        if binding is not None:
            _verify_blueprint_binding(
                binding,
                require_roadmap=True,
                verify_manifest=False,
            )
        else:
            assert own_root_binding is not None
            own_root_binding.verify()
        if len(original) > MAX_MANIFEST_BYTES:
            raise WorkspaceError([f"{WORKSPACE_FILE} exceeds the {MAX_MANIFEST_BYTES}-byte limit"])
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError([f"{WORKSPACE_FILE} is not valid UTF-8 TOML"]) from None
        current = parse_workspace(text)
        _validate_project_registration(
            current,
            project_id=project_id,
            location_id=location_id,
            path=path,
            expected_blueprint_path=expected_blueprint_path,
        )

        updated_text = _manifest_with_project(
            text,
            project_id=project_id,
            title=title,
            location_id=location_id,
            path=path,
        )
        parse_workspace(updated_text)
        updated = updated_text.encode("utf-8")
        if len(updated) > MAX_MANIFEST_BYTES:
            raise WorkspaceError(
                [f"updated {WORKSPACE_FILE} would exceed the {MAX_MANIFEST_BYTES}-byte limit"]
            )
        staged = _stage_new_file(
            manifest_path,
            updated,
            parent_descriptor=parent_descriptor,
            mode=stat.S_IMODE(metadata.st_mode),
        )
        if binding is not None:
            _verify_blueprint_binding(
                binding,
                require_roadmap=True,
                verify_manifest=False,
            )
        current_metadata = os.fstat(descriptor)
        try:
            named_metadata = os.stat(
                manifest_path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise WorkspaceError([f"{WORKSPACE_FILE} changed during update"]) from None
        if _file_signature(current_metadata) != _file_signature(metadata) or (
            current_metadata.st_dev,
            current_metadata.st_ino,
        ) != (named_metadata.st_dev, named_metadata.st_ino):
            raise WorkspaceError([f"{WORKSPACE_FILE} changed during update"])
        _verify_descriptor_content(descriptor, original, f"{WORKSPACE_FILE} changed during update")
        _verify_staged_file(parent_descriptor, staged.path.name, staged)
        _rename_exchange(
            parent_descriptor,
            staged.path.name,
            parent_descriptor,
            manifest_path.name,
        )
        exchanged = True
        try:
            _verify_staged_file(parent_descriptor, manifest_path.name, staged)
            _verify_named_content(
                parent_descriptor,
                staged.path.name,
                identity=(metadata.st_dev, metadata.st_ino),
                content=original,
                issue=f"{WORKSPACE_FILE} changed during update",
            )
            if binding is not None:
                _verify_blueprint_binding(
                    binding,
                    require_roadmap=True,
                    verify_manifest=False,
                )
        except WorkspaceError:
            _rollback_manifest_exchange(
                parent_descriptor,
                manifest_path.name,
                staged,
                prior_descriptor=descriptor,
                prior_identity=(metadata.st_dev, metadata.st_ino),
                prior_content=original,
                prior_mode=stat.S_IMODE(metadata.st_mode),
            )
            exchanged = False
            raise
        _fsync_directory_descriptor(parent_descriptor)
        _verify_staged_file(parent_descriptor, manifest_path.name, staged)
        if binding is not None:
            _verify_blueprint_binding(
                binding,
                require_roadmap=True,
                verify_manifest=False,
            )
        committed = True
        backup_name = _retain_displaced_manifest(
            parent_descriptor,
            staged.path.name,
            descriptor,
            (metadata.st_dev, metadata.st_ino),
        )
        _workspace_mutation_checkpoint("registry-backup-published")
        _verify_manifest_recovery(
            parent_descriptor,
            backup_name,
            descriptor,
            (metadata.st_dev, metadata.st_ino),
            original,
            stat.S_IMODE(metadata.st_mode),
        )
        _fsync_directory_descriptor(parent_descriptor)
        _verify_staged_file(parent_descriptor, manifest_path.name, staged)
        if binding is not None:
            _verify_blueprint_binding(
                binding,
                require_roadmap=True,
                verify_manifest=False,
            )
        _verify_manifest_recovery(
            parent_descriptor,
            backup_name,
            descriptor,
            (metadata.st_dev, metadata.st_ino),
            original,
            stat.S_IMODE(metadata.st_mode),
        )
        exchanged = False
        return backup_name
    except WorkspaceError as error:
        if committed and staged is not None and descriptor is not None:
            try:
                return _recover_committed_manifest_update(
                    parent_descriptor,
                    manifest_path.name,
                    staged,
                    descriptor,
                    (metadata.st_dev, metadata.st_ino),
                    original,
                    stat.S_IMODE(metadata.st_mode),
                    binding,
                    backup_name,
                )
            except WorkspaceError:
                raise WorkspaceError(
                    [
                        f"updated {WORKSPACE_FILE} is committed, but its prior-manifest "
                        f"recovery path could not be confirmed after: {error}"
                    ]
                ) from None
        if exchanged and staged is not None:
            try:
                _rollback_manifest_exchange(
                    parent_descriptor,
                    manifest_path.name,
                    staged,
                    prior_descriptor=descriptor,
                    prior_identity=(metadata.st_dev, metadata.st_ino),
                    prior_content=original,
                    prior_mode=stat.S_IMODE(metadata.st_mode),
                )
            except WorkspaceError:
                pass
        raise
    except OSError as error:
        if committed and staged is not None and descriptor is not None:
            try:
                return _recover_committed_manifest_update(
                    parent_descriptor,
                    manifest_path.name,
                    staged,
                    descriptor,
                    (metadata.st_dev, metadata.st_ino),
                    original,
                    stat.S_IMODE(metadata.st_mode),
                    binding,
                    backup_name,
                )
            except WorkspaceError:
                raise WorkspaceError(
                    [
                        f"updated {WORKSPACE_FILE} is committed, but its prior-manifest "
                        f"recovery path could not be confirmed after: {error}"
                    ]
                ) from None
        if exchanged and staged is not None:
            try:
                _rollback_manifest_exchange(
                    parent_descriptor,
                    manifest_path.name,
                    staged,
                    prior_descriptor=descriptor,
                    prior_identity=(metadata.st_dev, metadata.st_ino),
                    prior_content=original,
                    prior_mode=stat.S_IMODE(metadata.st_mode),
                )
            except WorkspaceError:
                pass
        raise WorkspaceError([f"cannot update {WORKSPACE_FILE}"]) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            assert fcntl is not None
            fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        if staged is not None:
            try:
                os.close(staged.descriptor)
            except OSError:
                pass
        if own_root_binding is not None:
            own_root_binding.close()


def _open_locked_manifest(manifest_path: Path, *, parent_descriptor: int) -> int:
    """Lock the inode currently named by the manifest, retrying across replacement."""

    _require_workspace_mutation_support()
    flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _ in range(8):
        try:
            descriptor = os.open(manifest_path.name, flags, dir_fd=parent_descriptor)
        except OSError:
            raise WorkspaceError([f"cannot update {WORKSPACE_FILE}"]) from None
        try:
            assert fcntl is not None
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            opened = os.fstat(descriptor)
            named = os.stat(
                manifest_path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino):
                return descriptor
        except OSError:
            os.close(descriptor)
            raise WorkspaceError([f"cannot update {WORKSPACE_FILE}"]) from None
        os.close(descriptor)
    raise WorkspaceError([f"{WORKSPACE_FILE} changed repeatedly during update"])


def _file_signature(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IMODE(metadata.st_mode),
    )


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _open_child_directory(
    parent_descriptor: int,
    name: str,
    label: str,
) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        expected = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(expected.st_mode):
            raise OSError(f"{label} is not a directory")
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
    except OSError:
        try:
            if descriptor is not None:
                os.close(descriptor)
        except OSError:
            pass
        raise WorkspaceError([f"{label} could not be opened safely"]) from None
    if _identity(opened) != _identity(expected):
        os.close(descriptor)
        raise WorkspaceError([f"{label} changed while it was being opened"])
    return descriptor, _identity(opened)


def _open_relative_directories(
    root_descriptor: int,
    relative: PurePosixPath,
    label: str,
) -> tuple[tuple[int, ...], tuple[tuple[int, int], ...]]:
    descriptors: list[int] = []
    identities: list[tuple[int, int]] = []
    parent = root_descriptor
    try:
        if not relative.parts:
            descriptor = os.dup(root_descriptor)
            opened = os.fstat(descriptor)
            return (descriptor,), (_identity(opened),)
        for part in relative.parts:
            descriptor, identity = _open_child_directory(parent, part, label)
            descriptors.append(descriptor)
            identities.append(identity)
            parent = descriptor
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    return tuple(descriptors), tuple(identities)


def _verify_root_binding(
    path: Path,
    descriptor: int,
    identity: tuple[int, int],
    *,
    root_binding: _WorkspaceRootBinding | None = None,
) -> None:
    try:
        if root_binding is not None:
            if root_binding.path != Path(os.path.abspath(path)):
                raise OSError("workspace root binding path changed")
            root_binding.verify()
        opened = os.fstat(descriptor)
        named = os.fstat(descriptor) if root_binding is not None else path.stat(
            follow_symlinks=False
        )
    except OSError:
        raise WorkspaceError(["workspace root changed during publication"]) from None
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or _identity(opened) != identity
        or _identity(named) != identity
    ):
        raise WorkspaceError(["workspace root changed during publication"])


def _verify_relative_directories(
    root_descriptor: int,
    relative: PurePosixPath,
    descriptors: tuple[int, ...],
    identities: tuple[tuple[int, int], ...],
    *,
    label: str,
) -> None:
    parts = relative.parts
    if not parts:
        parts = (".",)
    if len(parts) != len(descriptors) or len(descriptors) != len(identities):
        raise WorkspaceError([f"{label} binding is incomplete"])
    parent = root_descriptor
    for part, descriptor, identity in zip(parts, descriptors, identities, strict=True):
        try:
            opened = os.fstat(descriptor)
            named = os.fstat(root_descriptor) if part == "." else os.stat(
                part,
                dir_fd=parent,
                follow_symlinks=False,
            )
        except OSError:
            raise WorkspaceError([f"{label} changed during publication"]) from None
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or _identity(opened) != identity
            or _identity(named) != identity
        ):
            raise WorkspaceError([f"{label} changed during publication"])
        parent = descriptor


def _verify_blueprint_parent_binding(
    binding: _BlueprintBinding,
    *,
    verify_manifest: bool = True,
) -> None:
    """Verify the workspace root and selected blueprint collection."""

    assert binding.workspace._root_binding is not None
    _verify_root_binding(
        binding.workspace.root,
        binding.root_descriptor,
        binding.root_identity,
        root_binding=binding.workspace._root_binding,
    )
    _verify_relative_directories(
        binding.root_descriptor,
        PurePosixPath(binding.location.path),
        binding.location_descriptors,
        binding.location_identities,
        label="blueprint location",
    )
    if verify_manifest:
        binding.workspace.verify_root_binding()


def _verify_blueprint_binding(
    binding: _BlueprintBinding,
    *,
    require_roadmap: bool,
    verify_manifest: bool = True,
) -> None:
    _verify_blueprint_parent_binding(binding, verify_manifest=verify_manifest)
    if binding.destination_descriptor is None or binding.destination_identity is None:
        raise WorkspaceError(["blueprint destination binding is incomplete"])
    try:
        opened = os.fstat(binding.destination_descriptor)
        named = os.stat(
            binding.destination.name,
            dir_fd=binding.location_descriptors[-1],
            follow_symlinks=False,
        )
    except OSError:
        raise WorkspaceError(["blueprint destination changed during registration"]) from None
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or _identity(opened) != binding.destination_identity
        or _identity(named) != binding.destination_identity
    ):
        raise WorkspaceError(["blueprint destination changed during registration"])
    if require_roadmap:
        if binding.roadmap_descriptor is None or binding.roadmap_identity is None:
            roadmap_descriptor, roadmap_identity = _open_child_directory(
                binding.destination_descriptor,
                "roadmap",
                "registered blueprint roadmap",
            )
            binding.roadmap_descriptor = roadmap_descriptor
            binding.roadmap_identity = roadmap_identity
        try:
            roadmap_opened = os.fstat(binding.roadmap_descriptor)
            roadmap_named = os.stat(
                "roadmap",
                dir_fd=binding.destination_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise WorkspaceError(["registered blueprint roadmap changed during registration"])
        if (
            not stat.S_ISDIR(roadmap_opened.st_mode)
            or not stat.S_ISDIR(roadmap_named.st_mode)
            or _identity(roadmap_opened) != binding.roadmap_identity
            or _identity(roadmap_named) != binding.roadmap_identity
        ):
            raise WorkspaceError(["registered blueprint roadmap changed during registration"])
    if binding.scaffold_binding is not None:
        try:
            _verify_blueprint_scaffold_binding(binding.scaffold_binding, exact=True)
        except ScaffoldError as error:
            raise WorkspaceError(list(error.issues)) from None


def _verify_descriptor_content(descriptor: int, content: bytes, issue: str) -> None:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size != len(content):
        raise WorkspaceError([issue])
    digest = hashlib.sha256()
    offset = 0
    while offset < len(content):
        chunk = os.pread(descriptor, min(1024 * 1024, len(content) - offset), offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    after = os.fstat(descriptor)
    if (
        offset != len(content)
        or digest.digest() != hashlib.sha256(content).digest()
        or _file_signature(before) != _file_signature(after)
        or _identity(before) != _identity(after)
    ):
        raise WorkspaceError([issue])


def _verify_named_content(
    parent_descriptor: int,
    name: str,
    *,
    identity: tuple[int, int],
    content: bytes,
    issue: str,
    expected_mode: int | None = None,
) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError:
        raise WorkspaceError([issue]) from None
    try:
        opened = os.fstat(descriptor)
        if (
            _identity(named) != identity
            or _identity(opened) != identity
            or (
                expected_mode is not None
                and (
                    stat.S_IMODE(named.st_mode) != expected_mode
                    or stat.S_IMODE(opened.st_mode) != expected_mode
                )
            )
        ):
            raise WorkspaceError([issue])
        _verify_descriptor_content(descriptor, content, issue)
    finally:
        os.close(descriptor)


def _verify_staged_file(
    parent_descriptor: int,
    name: str,
    staged: _StagedFile,
    *,
    published: bool = False,
    expected_mode: int | None = None,
) -> None:
    issue = (
        f"published {name} changed before initialization could continue"
        if published
        else f"staged manifest changed before publication; inspect {staged.path.name}"
    )
    metadata = os.fstat(staged.descriptor)
    required_mode = staged.mode if expected_mode is None else expected_mode
    if (
        not stat.S_ISREG(metadata.st_mode)
        or _identity(metadata) != staged.identity
        or metadata.st_size != staged.size
        or stat.S_IMODE(metadata.st_mode) != required_mode
    ):
        raise WorkspaceError([issue])
    content = _read_descriptor(staged.descriptor, staged.size, issue)
    if hashlib.sha256(content).hexdigest() != staged.sha256:
        raise WorkspaceError([issue])
    _verify_named_content(
        parent_descriptor,
        name,
        identity=staged.identity,
        content=content,
        issue=issue,
        expected_mode=required_mode,
    )


def _read_descriptor(descriptor: int, size: int, issue: str) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        try:
            chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        except OSError:
            raise WorkspaceError([issue]) from None
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    if offset != size:
        raise WorkspaceError([issue])
    return b"".join(chunks)


def _rename_exchange(
    source_parent: int,
    source: str,
    target_parent: int,
    target: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        flag = 0x00000002
    elif hasattr(libc, "renameat2"):
        function = libc.renameat2
        flag = 2
    else:
        raise OSError("atomic exchange unavailable")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if function(source_parent, os.fsencode(source), target_parent, os.fsencode(target), flag) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def _rollback_manifest_exchange(
    parent_descriptor: int,
    manifest_name: str,
    staged: _StagedFile,
    *,
    prior_descriptor: int,
    prior_identity: tuple[int, int],
    prior_content: bytes,
    prior_mode: int,
) -> None:
    try:
        opened_prior = os.fstat(prior_descriptor)
    except OSError:
        raise WorkspaceError(["manifest exchange could not be rolled back safely"]) from None
    if (
        not stat.S_ISREG(opened_prior.st_mode)
        or _identity(opened_prior) != prior_identity
        or stat.S_IMODE(opened_prior.st_mode) != prior_mode
    ):
        raise WorkspaceError(["manifest exchange could not be rolled back safely"])
    _verify_descriptor_content(
        prior_descriptor,
        prior_content,
        "manifest exchange could not be rolled back safely",
    )
    _verify_staged_file(parent_descriptor, manifest_name, staged, published=True)
    _verify_named_content(
        parent_descriptor,
        staged.path.name,
        identity=prior_identity,
        content=prior_content,
        issue="manifest exchange could not be rolled back safely",
        expected_mode=prior_mode,
    )
    _rename_exchange(parent_descriptor, staged.path.name, parent_descriptor, manifest_name)
    _verify_named_content(
        parent_descriptor,
        manifest_name,
        identity=prior_identity,
        content=prior_content,
        issue="displaced manifest could not be restored safely",
        expected_mode=prior_mode,
    )
    _verify_staged_file(parent_descriptor, staged.path.name, staged)
    try:
        _fsync_directory_descriptor(parent_descriptor)
    except OSError:
        raise WorkspaceError(
            ["manifest exchange was restored, but its rollback could not be committed durably"]
        ) from None
    _verify_named_content(
        parent_descriptor,
        manifest_name,
        identity=prior_identity,
        content=prior_content,
        issue="displaced manifest changed after durable restoration",
        expected_mode=prior_mode,
    )
    _verify_staged_file(parent_descriptor, staged.path.name, staged)


def _read_named_bytes(parent_descriptor: int, name: str, size: int) -> bytes:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_descriptor,
    )
    try:
        return _read_descriptor(descriptor, size, f"{WORKSPACE_FILE} changed during update")
    finally:
        os.close(descriptor)


def _retain_displaced_manifest(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    identity: tuple[int, int],
) -> str:
    """Give displaced bytes an explicit recovery name without deleting them."""

    try:
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        opened = os.fstat(descriptor)
    except OSError:
        raise WorkspaceError(["displaced manifest could not be retained safely"]) from None
    if _identity(named) != identity or _identity(opened) != identity:
        raise WorkspaceError(["displaced manifest changed before recovery publication"])
    content = _read_descriptor(
        descriptor,
        opened.st_size,
        "displaced manifest changed before recovery publication",
    )
    digest = hashlib.sha256(content).hexdigest()
    base = f"{WORKSPACE_FILE}.backup-{digest}"
    for attempt in range(32):
        backup_name = base if attempt == 0 else f"{base}-{secrets.token_hex(4)}"
        try:
            _rename_noreplace(parent_descriptor, name, parent_descriptor, backup_name)
            break
        except FileExistsError:
            try:
                collision = os.stat(
                    backup_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                collision_bytes = _read_named_bytes(
                    parent_descriptor,
                    backup_name,
                    collision.st_size,
                )
            except (OSError, WorkspaceError):
                raise WorkspaceError(["manifest recovery-name collision could not be inspected"])
            if hashlib.sha256(collision_bytes).hexdigest() != digest:
                raise WorkspaceError(["manifest recovery-name collision has unexpected bytes"])
    else:
        raise WorkspaceError(["could not allocate a manifest recovery name"])
    _verify_named_content(
        parent_descriptor,
        backup_name,
        identity=identity,
        content=content,
        issue="displaced manifest recovery changed during publication",
    )
    return backup_name


def _recover_committed_manifest_update(
    parent_descriptor: int,
    manifest_name: str,
    staged: _StagedFile,
    prior_descriptor: int,
    prior_identity: tuple[int, int],
    prior_content: bytes,
    prior_mode: int,
    binding: _BlueprintBinding | None,
    backup_name: str | None,
) -> str:
    """Confirm an already durable exchange and expose its exact recovery path."""

    _verify_staged_file(parent_descriptor, manifest_name, staged)
    if binding is not None:
        _verify_blueprint_binding(
            binding,
            require_roadmap=True,
            verify_manifest=False,
        )
    recovery_name = staged.path.name if backup_name is None else backup_name
    _verify_manifest_recovery(
        parent_descriptor,
        recovery_name,
        prior_descriptor,
        prior_identity,
        prior_content,
        prior_mode,
    )
    _fsync_directory_descriptor(parent_descriptor)
    _verify_staged_file(parent_descriptor, manifest_name, staged)
    if binding is not None:
        _verify_blueprint_binding(
            binding,
            require_roadmap=True,
            verify_manifest=False,
        )
    _verify_manifest_recovery(
        parent_descriptor,
        recovery_name,
        prior_descriptor,
        prior_identity,
        prior_content,
        prior_mode,
    )
    return recovery_name


def _verify_manifest_recovery(
    parent_descriptor: int,
    recovery_name: str,
    prior_descriptor: int,
    prior_identity: tuple[int, int],
    prior_content: bytes,
    prior_mode: int,
) -> None:
    issue = "prior manifest recovery path changed"
    try:
        opened = os.fstat(prior_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _identity(opened) != prior_identity
            or stat.S_IMODE(opened.st_mode) != prior_mode
        ):
            raise WorkspaceError([issue])
        _verify_descriptor_content(prior_descriptor, prior_content, issue)
        _verify_named_content(
            parent_descriptor,
            recovery_name,
            identity=prior_identity,
            content=prior_content,
            issue=issue,
            expected_mode=prior_mode,
        )
    except WorkspaceError:
        raise
    except OSError:
        raise WorkspaceError([issue]) from None


def _fsync_directory_descriptor(descriptor: int) -> None:
    os.fsync(descriptor)


def _workspace_mutation_checkpoint(_name: str) -> None:
    """Deterministic race boundary used by adversarial tests."""


def _workspace_directory_checkpoint(
    _event: str,
    _parent_descriptor: int,
    _staging_name: str,
    _target_name: str,
) -> None:
    """Deterministic staged-directory race boundary used by tests."""


def _require_workspace_mutation_support() -> None:
    required = (
        fcntl is not None,
        hasattr(os, "O_NOFOLLOW"),
        hasattr(os, "O_DIRECTORY"),
        _atomic_noreplace_available(),
        _atomic_exchange_available(),
        os.mkdir in os.supports_dir_fd,
        os.open in os.supports_dir_fd,
        os.stat in os.supports_dir_fd,
        os.listdir in os.supports_fd,
    )
    if not all(required):
        raise WorkspaceError(
            ["this platform cannot update a workspace with the required path safety"]
        )


def _atomic_noreplace_available() -> bool:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return False
    return hasattr(libc, "renameatx_np") or hasattr(libc, "renameat2")


def _atomic_exchange_available() -> bool:
    return _atomic_noreplace_available()


def _stage_new_file(
    path: Path,
    content: bytes,
    *,
    parent_descriptor: int,
    mode: int = 0o600,
) -> _StagedFile:
    temporary: Path | None = None
    descriptor: int | None = None
    complete = False
    try:
        for _ in range(32):
            temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
            try:
                descriptor = os.open(
                    temporary.name,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=parent_descriptor,
                )
                break
            except FileExistsError:
                continue
        else:
            raise OSError("could not allocate a unique staged manifest name")
        os.fchmod(descriptor, mode)
        with os.fdopen(os.dup(descriptor), "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        named = os.stat(temporary.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != identity or not stat.S_ISREG(named.st_mode):
            raise WorkspaceError(
                [f"staged manifest changed before publication; inspect {temporary.name}"]
            )
        complete = True
        return _StagedFile(
            temporary,
            descriptor,
            identity,
            len(content),
            hashlib.sha256(content).hexdigest(),
            mode,
        )
    except WorkspaceError:
        raise
    except Exception:
        if temporary is not None:
            raise WorkspaceError(
                [f"could not stage {path.name}; retained staged file at {temporary.name}"]
            ) from None
        raise WorkspaceError([f"could not stage {path.name}"]) from None
    finally:
        if descriptor is not None and not complete:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _publish_staged_file(
    path: Path,
    staged: _StagedFile,
    *,
    mode: int,
    parent_descriptor: int,
    final_validator,
) -> None:
    published = False
    try:
        os.fchmod(staged.descriptor, mode)
        os.fsync(staged.descriptor)
        _verify_staged_file(
            parent_descriptor,
            staged.path.name,
            staged,
            expected_mode=mode,
        )
        _rename_noreplace(
            parent_descriptor,
            staged.path.name,
            parent_descriptor,
            path.name,
        )
        published = True
        _verify_staged_file(
            parent_descriptor,
            path.name,
            staged,
            published=True,
            expected_mode=mode,
        )
        final_validator()
        _fsync_directory_descriptor(parent_descriptor)
        _verify_staged_file(
            parent_descriptor,
            path.name,
            staged,
            published=True,
            expected_mode=mode,
        )
        final_validator()
    except FileExistsError:
        _restrict_staged_file(staged.descriptor)
        raise WorkspaceError(
            [
                f"{path.name} already exists; retained complete staged manifest at "
                f"{staged.path.name}"
            ]
        ) from None
    except ProjectCreateError:
        _restrict_staged_file(staged.descriptor)
        raise WorkspaceError(
            [
                "atomic manifest publication failed; retained complete staged manifest at "
                f"{staged.path.name}"
            ]
        ) from None
    except WorkspaceError:
        if not published:
            _restrict_staged_file(staged.descriptor)
        raise
    except Exception:
        if published:
            raise WorkspaceError(
                [f"published {path.name} but could not confirm final state; inspect it before retrying"]
            ) from None
        _restrict_staged_file(staged.descriptor)
        raise WorkspaceError(
            [
                f"could not publish {path.name}; retained complete staged manifest at "
                f"{staged.path.name}"
            ]
        ) from None


def _restrict_staged_file(descriptor: int) -> None:
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except OSError:
        pass


def _create_directory_chain(
    root: Path,
    relative: PurePosixPath,
    *,
    root_descriptor: int,
    root_identity: tuple[int, int],
    root_binding: _WorkspaceRootBinding,
) -> _DirectoryChain:
    """Create and retain a confined directory chain through publication."""

    created: list[Path] = []
    descriptors: list[int] = []
    identities: list[tuple[int, int]] = []
    parent_descriptor = root_descriptor
    current = root
    try:
        _verify_root_binding(
            root,
            root_descriptor,
            root_identity,
            root_binding=root_binding,
        )
        for part in relative.parts:
            next_path = current / part
            try:
                expected = os.stat(
                    part,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                try:
                    next_descriptor, next_identity = _publish_new_directory(
                        parent_descriptor,
                        part,
                        mode=0o755,
                        rename_noreplace=_DIRECTORY_RENAME_NOREPLACE,
                        fsync_directory=_fsync_directory_descriptor,
                        checkpoint=_workspace_directory_checkpoint,
                    )
                except _DirectoryPublicationError as error:
                    stage_detail = (
                        f"; staged name was {error.staging_name}"
                        if error.staging_name is not None
                        else ""
                    )
                    if error.reason == "durability":
                        issue = f"blueprint root could not be committed durably{stage_detail}"
                    elif error.reason in {"changed", "collision"}:
                        issue = f"blueprint root changed during creation{stage_detail}"
                    else:
                        issue = f"blueprint root could not be created{stage_detail}"
                    raise WorkspaceError([issue]) from None
                created.append(next_path)
            except OSError:
                raise WorkspaceError(["blueprint root could not be inspected safely"]) from None
            else:
                if not stat.S_ISDIR(expected.st_mode):
                    raise WorkspaceError(["blueprint root exists and is not a directory"])
                next_descriptor, next_identity = _open_child_directory(
                    parent_descriptor,
                    part,
                    "blueprint root",
                )
            descriptors.append(next_descriptor)
            identities.append(next_identity)
            _verify_root_binding(
                root,
                root_descriptor,
                root_identity,
                root_binding=root_binding,
            )
            _verify_relative_directories(
                root_descriptor,
                PurePosixPath(*relative.parts[: len(descriptors)]),
                tuple(descriptors),
                tuple(identities),
                label="blueprint root",
            )
            parent_descriptor = next_descriptor
            current = next_path
    except WorkspaceError as error:
        retained = ", ".join(path.relative_to(root).as_posix() for path in created)
        detail = "; ".join(error.issues)
        if retained:
            detail += f"; retained created directories: {retained}"
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise WorkspaceError([detail]) from None
    return _DirectoryChain(
        root,
        root_binding,
        root_descriptor,
        root_identity,
        relative,
        tuple(descriptors),
        tuple(identities),
        tuple(created),
    )


def _verify_directory_chain(chain: _DirectoryChain) -> None:
    _verify_root_binding(
        chain.root,
        chain.root_descriptor,
        chain.root_identity,
        root_binding=chain.root_binding,
    )
    _verify_relative_directories(
        chain.root_descriptor,
        chain.relative,
        chain.descriptors,
        chain.identities,
        label="blueprint root",
    )


def _fsync_directory(path: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | directory_flag | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # The file itself was already flushed and published. Directory fsync is
        # an extra durability measure, not grounds to report a failed mutation
        # after the user-visible path has appeared.
        return


__all__ = [
    "BlueprintCreateResult",
    "WorkspaceInitResult",
    "create_blueprint_project",
    "initialize_workspace",
    "register_blueprint_project",
]
